"""Durable logical-operation records and state vocabulary (ADR 0190)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

OperationState = Literal["running", "interrupted", "paused", "terminal"]
OperationOutcome = Literal[
    "completed",
    "configured",
    "ready_to_boot",
    "boot_started",
    "needs_attention",
    "failed",
    "abandoned",
]
EffectStatus = Literal["intended", "applied", "not_applied", "uncertain"]


@dataclass(frozen=True)
class EffectRecord:
    """One journaled HMC write of a logical operation (ADR 0190 Decision 4)."""

    key: str = field(
        metadata={"description": "Step key, stable across runs of the request."}
    )
    kind: str = field(
        metadata={"description": "Effect kind, which selects its live check."}
    )
    target: str = field(metadata={"description": "What the write acts on."})
    status: EffectStatus = field(
        metadata={"description": "intended, applied, not_applied or uncertain."}
    )
    identity: dict[str, Any] | None = field(
        metadata={"description": "Identities the write created or confirmed, or null."}
    )


@dataclass(frozen=True)
class OperationEvent:
    """A phase change, effect intent or effect outcome."""

    seq: int = field(
        metadata={"description": "Event sequence number within the operation."}
    )
    at: str = field(metadata={"description": "UTC time, ISO 8601 with a Z suffix."})
    kind: Literal["phase", "intent", "outcome"] = field(
        metadata={"description": "phase, intent or outcome."}
    )
    detail: dict[str, Any] = field(metadata={"description": "The event's fields."})


@dataclass(frozen=True)
class OperationRecord:
    """Durable state of one logical operation (ADR 0190 Decision 6)."""

    operation_id: str = field(
        metadata={"description": "Server-minted id, 32 hex digits."}
    )
    request_id: str = field(metadata={"description": "The caller's request id."})
    tool: str = field(metadata={"description": "The logical tool that started it."})
    connection: str = field(metadata={"description": "Profile key, or <default>."})
    system_uuid: str | None = field(
        metadata={"description": "Managed system of the guarded partition, or null."}
    )
    partition_uuid: str | None = field(
        metadata={"description": "The partition this operation guards, or null."}
    )
    state: OperationState = field(
        metadata={"description": "running, interrupted, paused or terminal."}
    )
    phase: str = field(metadata={"description": "The tool's current phase name."})
    outcome: OperationOutcome | None = field(
        metadata={"description": "Outcome, or null while running or interrupted."}
    )
    effects: tuple[EffectRecord, ...] = field(
        metadata={"description": "Journaled writes in order, at most 256."}
    )
    events: tuple[OperationEvent, ...] = field(
        metadata={"description": "The newest 200 events, oldest first."}
    )
    events_truncated: bool = field(
        metadata={"description": "Whether older events exist beyond those returned."}
    )
    warnings: tuple[str, ...] = field(
        metadata={"description": "Why the operation paused or failed."}
    )
    next_actions: tuple[str, ...] = field(
        metadata={"description": "Continuations this state accepts."}
    )
    result: dict[str, Any] | None = field(
        metadata={"description": "The tool's own result at its last end, or null."}
    )
    created_at: str = field(metadata={"description": "UTC creation time, ISO 8601."})
    updated_at: str = field(
        metadata={"description": "UTC time of the last change, ISO 8601."}
    )


@dataclass(frozen=True)
class OperationPage:
    """A bounded page of operation records, newest first."""

    operations: tuple[OperationRecord, ...] = field(
        metadata={"description": "At most `limit` records."}
    )
    limit: int = field(metadata={"description": "The page size requested, 1-50."})
    truncated: bool = field(metadata={"description": "Whether more records follow."})
    next_cursor: str | None = field(
        metadata={
            "description": "Pass as `cursor` for the next page; null on the last."
        }
    )
