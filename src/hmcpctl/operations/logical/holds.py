"""External-management holds on the ADR 0190 store (ADR 0193, ADR 0327).

A hold binds this host's hmcpctl front ends only: the MCP server and the CLI install
:func:`enforce_hold` as the partition guard, and the library leaves it unset. Every function
lower-cases its UUID arguments, so a hold matches whatever case a caller spells it in.
Refusals and listings never carry a ``hold_id``; :func:`place_hold` returns it to its caller.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from dataclasses import dataclass

from ...call_context import HOLD_EXEMPT_TOOLS, current_call
from . import store
from .store import OperationRefused

MAX_CONSUMER_LABEL = 64


@dataclass(frozen=True)
class Hold:
    """One partition's hold; ``created_at`` is ISO-8601 UTC."""

    system_uuid: str
    partition_uuid: str
    hold_id: str
    consumer_label: str
    agent_id: str
    connection: str
    created_at: str


def _hold(row: sqlite3.Row) -> Hold:
    return Hold(
        system_uuid=row["system_uuid"],
        partition_uuid=row["partition_uuid"],
        hold_id=row["hold_id"],
        consumer_label=row["consumer_label"],
        agent_id=row["agent_id"],
        connection=row["connection"],
        created_at=store._iso(row["created_at"]),
    )


def describe(hold: Hold) -> str:
    """Name a hold for a refusal or listing, never by its ``hold_id``."""
    return (
        f"held for {hold.consumer_label!r} by agent {hold.agent_id!r} since "
        f"{hold.created_at}"
    )


def _validate_label(label: object) -> str:
    if (
        not isinstance(label, str)
        or not 1 <= len(label) <= MAX_CONSUMER_LABEL
        or not label.isprintable()
    ):
        raise OperationRefused(
            "invalid_consumer_label",
            f"consumer_label is 1-{MAX_CONSUMER_LABEL} printable characters",
        )
    return label


def _row(conn: sqlite3.Connection, partition_uuid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM holds WHERE partition_uuid = ? ORDER BY system_uuid LIMIT 1",
        (partition_uuid.lower(),),
    ).fetchone()


def place_hold(
    system_uuid: str,
    partition_uuid: str,
    *,
    consumer_label: str,
    agent_id: str,
    connection: str,
) -> Hold:
    """Record the partition's one hold, or return it to the agent that placed it with this label.

    Refuses ``partition_guarded`` while a non-terminal logical operation guards the partition
    and ``partition_held`` when another agent or label holds it (ADR 0193 Decision 1).
    """
    label = _validate_label(consumer_label)
    system, partition = system_uuid.lower(), partition_uuid.lower()
    with store.session() as conn, store.write_transaction(conn):
        guard = conn.execute(
            "SELECT operation_id FROM operations WHERE lower(system_uuid) = ?"
            " AND lower(partition_uuid) = ? AND state != 'terminal'",
            (system, partition),
        ).fetchone()
        if guard is not None:
            raise OperationRefused(
                "partition_guarded",
                f"operation {guard['operation_id']} is running on partition {partition}; "
                "let it finish or abandon it first",
            )
        existing = _row(conn, partition)
        if existing is not None:
            held = _hold(existing)
            if (held.agent_id, held.consumer_label) == (agent_id, label):
                return held
            raise OperationRefused(
                "partition_held",
                f"partition {partition} is already {describe(held)}; release that hold first",
            )
        store.check_capacity(conn)
        conn.execute(
            "INSERT INTO holds (system_uuid, partition_uuid, hold_id, consumer_label,"
            " agent_id, connection, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                system,
                partition,
                secrets.token_hex(16),
                label,
                agent_id,
                connection,
                store._clock(),
            ),
        )
        return _hold(
            conn.execute(
                "SELECT * FROM holds WHERE system_uuid = ? AND partition_uuid = ?",
                (system, partition),
            ).fetchone()
        )


def _matches(presented: str | None, hold_id: str) -> bool:
    return presented is not None and hmac.compare_digest(
        presented.encode("utf-8"), hold_id.encode("utf-8")
    )


def release_hold(
    system_uuid: str,
    partition_uuid: str,
    *,
    agent_id: str,
    hold_id: str | None = None,
) -> Hold | None:
    """Delete the hold given its ``hold_id`` or from the placing agent; None when unheld.

    Writes nothing to the HMC and never deletes the partition (ADR 0193 Decision 1).
    """
    system, partition = system_uuid.lower(), partition_uuid.lower()
    with store.session() as conn, store.write_transaction(conn):
        row = conn.execute(
            "SELECT * FROM holds WHERE system_uuid = ? AND partition_uuid = ?",
            (system, partition),
        ).fetchone()
        if row is None:
            return None
        held = _hold(row)
        if not (_matches(hold_id, held.hold_id) or agent_id == held.agent_id):
            raise OperationRefused(
                "hold_mismatch",
                f"partition {partition} is {describe(held)}; release it with its hold_id or "
                "from the agent that placed it",
            )
        conn.execute(
            "DELETE FROM holds WHERE system_uuid = ? AND partition_uuid = ?",
            (system, partition),
        )
    return held


def find_hold(partition_uuid: str) -> Hold | None:
    """Return the hold on *partition_uuid* on any system; never creates a store."""
    with store.read_session() as conn:
        row = None if conn is None else _row(conn, partition_uuid)
    return None if row is None else _hold(row)


def holds_present() -> bool:
    """Whether any partition is held; False when no store exists."""
    with store.read_session() as conn:
        return conn is not None and (
            conn.execute("SELECT 1 FROM holds LIMIT 1").fetchone() is not None
        )


def list_holds() -> tuple[Hold, ...]:
    """Every hold, oldest first, for the local CLI operator."""
    with store.read_session() as conn:
        if conn is None:
            return ()
        rows = conn.execute(
            "SELECT * FROM holds ORDER BY created_at, system_uuid, partition_uuid"
        ).fetchall()
    return tuple(_hold(row) for row in rows)


def operator_release(partition_uuid: str) -> tuple[Hold, ...]:
    """Release every hold on *partition_uuid* for the local operator (ADR 0193 Decision 7)."""
    partition = partition_uuid.lower()
    with store.read_session() as conn:
        if conn is None:
            return ()
        with store.write_transaction(conn):
            rows = conn.execute(
                "SELECT * FROM holds WHERE partition_uuid = ?", (partition,)
            ).fetchall()
            conn.execute("DELETE FROM holds WHERE partition_uuid = ?", (partition,))
    return tuple(_hold(row) for row in rows)


def enforce_hold(partition_uuid: str) -> None:
    """Refuse a guarded mutation of a held partition without its ``hold_id`` (ADR 0193).

    Reads and the exempt tools pass. A store ADR 0190 Decision 9 refuses raises here too, so
    unknown state refuses (ADR 0193 Decision 5).
    """
    call = current_call()
    if call is not None and (call.read or call.tool in HOLD_EXEMPT_TOOLS):
        return
    held = find_hold(partition_uuid)
    if held is None or _matches(None if call is None else call.hold_id, held.hold_id):
        return
    raise OperationRefused(
        "partition_held",
        f"partition {held.partition_uuid} is {describe(held)}; present its hold_id, or ask "
        "the operator to release it with hmcpctl holds release",
    )
