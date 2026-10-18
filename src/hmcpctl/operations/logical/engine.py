"""Run logical operations against the durable store (ADR 0190, ADR 0195).

A warning keeps at most 512 characters of an exception's text or a classifier's reason, and
``hmc_operation_status`` returns it to the agent: a body, effect write or classifier must
raise messages that carry no request arguments.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from . import store
from .models import (
    EffectRecord,
    EffectStatus,
    OperationOutcome,
    OperationRecord,
    OperationState,
)
from .store import OperationRefused

EXCLUDED_ARGUMENTS = frozenset({"continuation", "wait_seconds", "hold_id"})
CONTINUATIONS = ("none", "resume", "abandon", "boot")
MAX_WAIT_SECONDS = 600
_EFFECT_KEY = re.compile(r"^[a-z0-9_.:-]{1,128}$")
_EFFECT_KIND = re.compile(r"^[a-z0-9_.]{1,64}$")
_MAX_TARGET = 256
_PAUSING = frozenset({"ready_to_boot", "needs_attention"})
# A run starts with no earlier worker live on its operation, so an ``intended`` effect it
# loads is as unknown as an ``uncertain`` one (a failed outcome write leaves it there).
_OPEN = frozenset({"intended", "uncertain"})
_MAX_WARNING = 512
_ABSENT = object()
_LOG = logging.getLogger(__name__)


class OperationFailed(Exception):
    """The operation cannot proceed; every effect it recorded is accounted for."""


class EffectNotApplied(Exception):
    """Raised by an effect's ``write`` when the HMC's answer proves nothing changed."""


@dataclass(frozen=True)
class OperationRequest:
    """One logical-tool call; ``connection`` is ``store.connection_label(profile, tool=...)``."""

    tool: str
    agent_id: str
    connection: str
    host: str
    request_id: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class BodyResult:
    outcome: Literal[
        "completed", "configured", "ready_to_boot", "boot_started", "needs_attention"
    ]
    result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Classification:
    status: Literal["applied", "not_applied", "needs_attention"]
    identity: Mapping[str, Any] | None = None
    reason: str | None = None


Body = Callable[["OperationContext"], Awaitable[BodyResult]]
Classifier = Callable[["OperationContext", EffectRecord], Awaitable[Classification]]
_WORKERS: dict[str, threading.Thread] = {}
_WORKERS_GUARD = threading.Lock()
_ADMISSION = threading.Lock()


def canonical_request(request: OperationRequest) -> tuple[str, str]:
    """Return the canonical request text and its SHA-256 digest (ADR 0190 Decision 3)."""
    arguments = {
        k: v for k, v in request.arguments.items() if k not in EXCLUDED_ARGUMENTS
    }
    text = json.dumps(
        {"tool": request.tool, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    encoded = text.encode("utf-8")
    if len(encoded) > store.MAX_REQUEST_BYTES:
        raise OperationRefused(
            "request_too_large",
            f"the request is {len(encoded)} bytes; the limit is {store.MAX_REQUEST_BYTES}",
        )
    return text, hashlib.sha256(encoded).hexdigest()


def recorded_arguments(
    agent_id: str, request_id: str, tool: str
) -> Mapping[str, Any] | None:
    """Return the arguments *tool* recorded for (*agent_id*, *request_id*), or None.

    A continuation re-authorizes as a fresh call (ADR 0190 Decision 6), so a tool needs
    the recorded targets before ``submit``. Reading never creates the store.
    """
    store.validate_request_id(request_id)
    with store.read_session() as conn:
        row = None if conn is None else store.find_operation(conn, agent_id, request_id)
    if row is None or row["tool"] != tool:
        return None
    return json.loads(row["request_json"])["arguments"]


def _check_inputs(
    request: OperationRequest, continuation: str, wait_seconds: int
) -> None:
    store.validate_request_id(request.request_id)
    if continuation not in CONTINUATIONS:
        raise OperationRefused(
            "invalid_continuation",
            f"continuation must be one of {', '.join(CONTINUATIONS)}",
        )
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, int)
        or not 0 <= wait_seconds <= MAX_WAIT_SECONDS
    ):
        raise OperationRefused(
            "invalid_wait_seconds",
            f"wait_seconds must be an integer from 0 to {MAX_WAIT_SECONDS}",
        )


def submit(
    request: OperationRequest,
    body: Body,
    *,
    classifiers: Mapping[str, Classifier] | None = None,
    continuation: str = "none",
    wait_seconds: int = 60,
) -> OperationRecord:
    """Start, repeat, or continue one logical operation and return its record.

    The caller has already authorized this call (ADR 0189). The call returns within
    *wait_seconds*; work continues in this process after it returns.
    Supply the body's live checks again on each continuation. Their mapping is
    copied before admission; omitted kinds retain unknown outcomes (ADR 0236).
    """
    _check_inputs(request, continuation, wait_seconds)
    request_json, digest = canonical_request(request)
    execution_classifiers = dict(classifiers) if classifiers is not None else {}
    # Lock recovery, admission and worker start are one step, so no concurrent call in this
    # process can see a running record whose worker it has not counted and orphan it.
    with _ADMISSION:
        with _WORKERS_GUARD:
            live = frozenset(_WORKERS)
        store.acquire_execution_lock(live)
        with store.session() as conn, store.write_transaction(conn):
            row = store.find_operation(conn, request.agent_id, request.request_id)
            if row is None:
                operation_id, run = _admit_new(
                    conn, request, request_json, digest, continuation
                )
            else:
                operation_id, run = _admit_existing(
                    conn, row, request, digest, continuation
                )
        if run:
            _start_worker(operation_id, body, continuation, execution_classifiers)
    join(operation_id, wait_seconds)
    with store.session() as conn:
        return store.read_record(conn, operation_id)


def _admit_new(
    conn: sqlite3.Connection,
    request: OperationRequest,
    request_json: str,
    digest: str,
    continuation: str,
) -> tuple[str, bool]:
    if continuation != "none":
        raise OperationRefused(
            "not_found",
            f"no operation has request_id {request.request_id}; omit continuation to start one",
        )
    operation_id = secrets.token_hex(16)
    store.insert_operation(
        conn,
        operation_id=operation_id,
        agent_id=request.agent_id,
        request_id=request.request_id,
        tool=request.tool,
        connection=request.connection,
        host=request.host,
        digest=digest,
        request_json=request_json,
    )
    return operation_id, True


def _conflicting_arguments(row: sqlite3.Row, arguments: Mapping[str, Any]) -> list[str]:
    stored = json.loads(row["request_json"])["arguments"]
    return sorted(
        key
        for key, value in arguments.items()
        if key not in EXCLUDED_ARGUMENTS
        and stored.get(key, _ABSENT) != json.loads(json.dumps(value))
    )


def _check_binding(
    row: sqlite3.Row, request: OperationRequest, digest: str, continuation: str
) -> None:
    """Refuse a request whose connection, tool or arguments differ from the record's."""
    if (row["connection"], row["host"]) != (request.connection, request.host):
        raise OperationRefused(
            "connection_mismatch",
            f"request_id {request.request_id} belongs to another connection or HMC; continue "
            "it with the profile that started it, or use a new request_id",
        )
    if row["tool"] != request.tool:
        raise OperationRefused(
            "request_conflict",
            f"request_id {request.request_id} belongs to {row['tool']}; continue it with that "
            "tool, or use a new request_id",
        )
    if continuation == "none":
        conflicts = ["arguments"] if row["digest"] != digest else []
    else:
        conflicts = _conflicting_arguments(row, request.arguments)
    if conflicts:
        raise OperationRefused(
            "request_conflict",
            f"request_id {request.request_id} was used with different {', '.join(conflicts)}; "
            "use a new request_id",
        )


def _admit_existing(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    request: OperationRequest,
    digest: str,
    continuation: str,
) -> tuple[str, bool]:
    _check_binding(row, request, digest, continuation)
    operation_id = row["operation_id"]
    state = row["state"]
    if state == "running" and not _has_worker(operation_id):
        store.interrupt_orphan(conn, operation_id)
        state = "interrupted"
    if continuation == "none" or state == "terminal":
        return operation_id, False
    if state == "running":
        raise OperationRefused(
            "running", "the operation is running; poll hmc_operation_status"
        )
    accepted = store.accepted_continuations(state, row["outcome"])
    if continuation not in accepted:
        raise OperationRefused(
            "continuation_not_accepted",
            f"a {state}/{row['outcome']} operation accepts {', '.join(accepted)}",
        )
    if continuation == "abandon":
        store.set_state(conn, operation_id, "terminal", "abandoned")
        return operation_id, False
    store.set_state(conn, operation_id, "running", None)
    return operation_id, True


def _start_worker(
    operation_id: str,
    body: Body,
    continuation: str,
    classifiers: Mapping[str, Classifier],
) -> None:
    thread = threading.Thread(
        target=_work,
        args=(operation_id, body, continuation, classifiers),
        name=f"hmcpctl-operation-{operation_id[:8]}",
        daemon=True,
    )
    with _WORKERS_GUARD:
        _WORKERS[operation_id] = thread
    try:
        thread.start()
    except BaseException:
        # Unregistered, the running record is an orphan the next continuation recovers.
        with _WORKERS_GUARD:
            if _WORKERS.get(operation_id) is thread:
                del _WORKERS[operation_id]
        raise


def _has_worker(operation_id: str) -> bool:
    with _WORKERS_GUARD:
        return operation_id in _WORKERS


def join(operation_id: str, timeout: float) -> None:
    """Wait up to *timeout* seconds for this process's worker on *operation_id*.

    A worker that finishes may already have a successor started by a resume, so the
    wait follows the map until no worker is registered or the time is spent.
    """
    deadline, joined = time.monotonic() + timeout, None
    while True:
        with _WORKERS_GUARD:
            thread = _WORKERS.get(operation_id)
        if thread is None or thread is joined:
            return
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            return
        joined = thread


def _work(
    operation_id: str,
    body: Body,
    continuation: str,
    classifiers: Mapping[str, Classifier],
) -> None:
    try:
        asyncio.run(_run(operation_id, body, continuation, classifiers))
    except Exception:  # noqa: BLE001 - a worker must never die silently; logged below
        _LOG.exception(
            "logical operation %s could not record its end; it reads as running until "
            "this process exits",
            operation_id,
        )
    finally:
        with _WORKERS_GUARD:
            # A resume may already have registered a successor; never remove it.
            if _WORKERS.get(operation_id) is threading.current_thread():
                del _WORKERS[operation_id]


def _end(
    operation_id: str,
    state: OperationState,
    outcome: OperationOutcome,
    warnings: tuple[str, ...] = (),
    result_json: str | None = None,
) -> None:
    with store.session() as conn, store.write_transaction(conn):
        store.set_state(
            conn,
            operation_id,
            state,
            outcome,
            warnings=warnings,
            result_json=result_json,
        )


def _warning(detail: str, prefix: str = "", suffix: str = "") -> str:
    """Cut *detail* so the warning fits ``_MAX_WARNING`` with its fixed text intact."""
    return prefix + detail[: _MAX_WARNING - len(prefix) - len(suffix)] + suffix


def _failure(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _result_json(result: BodyResult) -> str:
    text = json.dumps(dict(result.result), sort_keys=True)
    if len(text.encode("utf-8")) > store.MAX_RESULT_BYTES:
        raise ValueError(f"the operation result exceeds {store.MAX_RESULT_BYTES} bytes")
    return text


async def _run(
    operation_id: str,
    body: Body,
    continuation: str,
    classifiers: Mapping[str, Classifier],
) -> None:
    ctx = OperationContext.load(operation_id, continuation, classifiers)
    warnings = await ctx.reconcile()
    if warnings:
        _end(operation_id, "paused", "needs_attention", warnings)
        return
    try:
        result = await body(ctx)
        result_json = _result_json(result)
    except (OperationFailed, EffectNotApplied) as exc:
        definite = not ctx.has_uncertain()
        _end(
            operation_id,
            "terminal" if definite else "paused",
            "failed" if definite else "needs_attention",
            (_warning(_failure(exc)),),
        )
        return
    except Exception as exc:  # noqa: BLE001 - any body defect pauses for attention
        _end(operation_id, "paused", "needs_attention", (_warning(_failure(exc)),))
        return
    if ctx.has_uncertain():
        _end(
            operation_id,
            "paused",
            "needs_attention",
            (ctx.uncertain_warning(),),
            result_json,
        )
        return
    state: OperationState = "paused" if result.outcome in _PAUSING else "terminal"
    _end(operation_id, state, result.outcome, (), result_json)


def _check_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    plain = dict(identity)
    if (
        len(json.dumps(plain, sort_keys=True).encode("utf-8"))
        > store.MAX_IDENTITY_BYTES
    ):
        raise ValueError(f"an effect identity exceeds {store.MAX_IDENTITY_BYTES} bytes")
    return plain


class OperationContext:
    """What an operation body sees: its request, and the journal its writes go through."""

    def __init__(
        self,
        row: sqlite3.Row,
        effects: dict[str, EffectRecord],
        continuation: str,
        classifiers: Mapping[str, Classifier],
    ) -> None:
        self.operation_id: str = row["operation_id"]
        self.agent_id: str = row["agent_id"]
        self.connection: str = row["connection"]
        self.continuation = continuation
        self.arguments: Mapping[str, Any] = json.loads(row["request_json"])["arguments"]
        self._phase: str = row["phase"]
        self._partition: tuple[str, str] | None = (
            None
            if row["partition_uuid"] is None
            else (row["system_uuid"], row["partition_uuid"])
        )
        self._effects = effects
        self._classifiers = classifiers

    @classmethod
    def load(
        cls, operation_id: str, continuation: str, classifiers: Mapping[str, Classifier]
    ) -> OperationContext:
        with store.session() as conn:
            row = store.get_operation(conn, operation_id)
            return cls(
                row, store.load_effects(conn, operation_id), continuation, classifiers
            )

    def has_uncertain(self) -> bool:
        return any(effect.status in _OPEN for effect in self._effects.values())

    def uncertain_warning(self) -> str:
        keys = sorted(k for k, e in self._effects.items() if e.status in _OPEN)
        return f"the body returned with effects of unknown outcome: {', '.join(keys)}"

    @property
    def partition(self) -> tuple[str, str] | None:
        """The (system UUID, partition UUID) this operation guards, once it holds one."""
        return self._partition

    def recorded(self, key: str) -> EffectRecord | None:
        """Return the journaled effect for *key*, or None if it was never attempted."""
        return self._effects.get(key)

    def phase(self, name: str) -> None:
        if name == self._phase:
            return
        with store.session() as conn, store.write_transaction(conn):
            store.set_phase(conn, self.operation_id, name)
        self._phase = name

    def guard(self, system_uuid: str, partition_uuid: str) -> None:
        """Hold the ADR 0190 partition guard for this operation until it is terminal."""
        wanted = (system_uuid, partition_uuid)
        if self._partition == wanted:
            return
        if self._partition is not None:
            raise ValueError(
                f"operation {self.operation_id} already guards another partition"
            )
        with store.session() as conn, store.write_transaction(conn):
            holder = store.guard_holder(
                conn, self.operation_id, system_uuid, partition_uuid
            )
            if holder is not None:
                raise OperationFailed(
                    f"partition_busy: operation {holder} holds this partition; "
                    "wait for it or abandon it"
                )
            store.set_partition(conn, self.operation_id, system_uuid, partition_uuid)
        self._partition = wanted

    def ledger_add(
        self,
        key: str,
        kind: str,
        system_uuid: str,
        partition_uuid: str | None,
        identity: Mapping[str, Any],
    ) -> None:
        plain = _check_identity(identity)
        with store.session() as conn, store.write_transaction(conn):
            store.add_ledger(
                conn,
                self.operation_id,
                key=key,
                kind=kind,
                system_uuid=system_uuid,
                partition_uuid=partition_uuid,
                identity=plain,
            )

    def _settle(
        self, key: str, status: EffectStatus, identity: dict[str, Any] | None
    ) -> None:
        with store.session() as conn, store.write_transaction(conn):
            store.record_outcome(conn, self.operation_id, key, status, identity)
        self._effects[key] = dataclasses.replace(
            self._effects[key], status=status, identity=identity
        )

    async def effect(
        self,
        key: str,
        kind: str,
        target: str,
        write: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        """Journal one HMC write: intent, then the write, then its outcome (ADR 0195)."""
        if not _EFFECT_KEY.fullmatch(key) or not _EFFECT_KIND.fullmatch(kind):
            raise ValueError(f"effect key {key!r} or kind {kind!r} is malformed")
        if len(target) > _MAX_TARGET:
            raise ValueError(f"effect target exceeds {_MAX_TARGET} characters")
        recorded = self._effects.get(key)
        if recorded is not None and recorded.status == "applied":
            return recorded.identity or {}
        if recorded is not None and recorded.status != "not_applied":
            raise RuntimeError(
                f"effect {key} is {recorded.status}; it must be reconciled first"
            )
        if recorded is None and len(self._effects) >= store.MAX_EFFECTS:
            raise OperationFailed(
                f"effect_limit: an operation records at most {store.MAX_EFFECTS} effects"
            )
        with store.session() as conn, store.write_transaction(conn):
            store.record_intent(conn, self.operation_id, key, kind, target)
        self._effects[key] = EffectRecord(key, kind, target, "intended", None)
        try:
            identity = _check_identity(await write())
        except EffectNotApplied:
            self._settle(key, "not_applied", None)
            raise
        except BaseException:
            self._settle(key, "uncertain", None)
            raise
        self._settle(key, "applied", identity)
        return identity

    async def reconcile(self) -> tuple[str, ...]:
        """Classify every open effect by its live check; return what stays open."""
        warnings = []
        for effect in [e for e in self._effects.values() if e.status in _OPEN]:
            warning = await self._classify(effect)
            if warning is not None:
                warnings.append(warning)
        return tuple(warnings)

    async def _classify(self, effect: EffectRecord) -> str | None:
        classifier = self._classifiers.get(effect.kind)
        if classifier is None:
            return (
                f"effect {effect.key} ({effect.kind}) has no live check; inspect "
                f"{effect.target}, then abandon"
            )
        try:
            verdict = await classifier(self, effect)
            identity = (
                None
                if verdict.status != "applied"
                else _check_identity(verdict.identity or {})
            )
        except Exception as exc:  # noqa: BLE001 - a failing live check stays open
            return _warning(
                _failure(exc),
                f"effect {effect.key} live check failed (",
                "); retry resume",
            )
        if verdict.status == "needs_attention":
            reason = verdict.reason or "live state does not match the record"
            return _warning(reason, f"effect {effect.key}: ")
        self._settle(effect.key, verdict.status, identity)
        return None
