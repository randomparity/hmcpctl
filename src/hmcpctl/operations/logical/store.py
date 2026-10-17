"""Local SQLite store and execution lock for durable logical operations (ADR 0190)."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Collection, Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...authorization.connection_scope import UNRESOLVED, selected_connection
from ..lpar.workflow_contract import (
    EffectRecord,
    OperationEvent,
    OperationPage,
    OperationRecord,
)

STATE_DIR_ENV = "HMCPCTL_STATE_DIR"
# The connection label of the environment connection: the access policy's own
# DEFAULT_CONNECTION_TOKEN, restated so operations/ does not import authorization/.
DEFAULT_CONNECTION = "<default>"
DB_NAME = "operations.sqlite3"
SENTINEL_NAME = "store-id"
LOCK_NAME = "execution.lock"
SCHEMA_VERSION = "1"
MAX_REQUEST_BYTES = 65_536
MAX_RESULT_BYTES = 65_536
MAX_IDENTITY_BYTES = 4_096
MAX_EFFECTS = 256
MAX_EVENTS_KEPT = 1_024
STATUS_EVENTS = 200
MAX_PAGE = 50
RETENTION_SECONDS = 30 * 86_400
STORE_CAPACITY = 10_000
BUSY_TIMEOUT_SECONDS = 5.0

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_CURSOR = 256

_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE operations (
        operation_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
        tool TEXT NOT NULL, connection TEXT NOT NULL, host TEXT NOT NULL,
        digest TEXT NOT NULL, request_json TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT,
        phase TEXT NOT NULL, system_uuid TEXT, partition_uuid TEXT, result_json TEXT,
        warnings_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        terminal_at REAL, UNIQUE (agent_id, request_id))""",
    """CREATE UNIQUE INDEX partition_guard ON operations (system_uuid, partition_uuid)
        WHERE state != 'terminal' AND partition_uuid IS NOT NULL""",
    """CREATE TABLE effects (
        operation_id TEXT NOT NULL REFERENCES operations ON DELETE CASCADE,
        key TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
        status TEXT NOT NULL, identity_json TEXT, PRIMARY KEY (operation_id, key))""",
    """CREATE TABLE events (
        operation_id TEXT NOT NULL REFERENCES operations ON DELETE CASCADE,
        seq INTEGER NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
        detail_json TEXT NOT NULL, PRIMARY KEY (operation_id, seq))""",
    """CREATE TABLE ledger (
        operation_id TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
        system_uuid TEXT NOT NULL, partition_uuid TEXT, identity_json TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY (operation_id, key))""",
)


class OperationRefused(ValueError):
    """A logical operation or status read refused before acting (ADR 0190)."""

    def __init__(self, reason: str, action: str) -> None:
        super().__init__(f"{reason}: {action}")
        self.reason = reason


def _clock() -> float:
    return time.time()


def state_dir() -> Path:
    """Return the ADR 0190 state directory without creating it."""
    override = os.environ.get(STATE_DIR_ENV, "")
    if override:
        # A relative path would resolve against each process's cwd, so the server and the
        # CLI could each get a private store and lock without noticing.
        if not Path(override).is_absolute():
            raise OperationRefused(
                "state_dir_unresolved",
                f"{STATE_DIR_ENV} must be an absolute path (got {override!r}); "
                "expand ~ and give the full path",
            )
        return Path(override)
    try:
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "hmcpctl-state"
        if sys.platform.startswith("linux"):
            # The XDG base-directory spec says to ignore a relative XDG_STATE_HOME.
            xdg = Path(os.environ.get("XDG_STATE_HOME", ""))
            base = xdg if xdg.is_absolute() else Path.home() / ".local" / "state"
            return base / "hmcpctl"
    except RuntimeError as exc:
        raise OperationRefused(
            "state_dir_unresolved",
            f"the home directory cannot be resolved ({exc}); set {STATE_DIR_ENV}",
        ) from exc
    raise OperationRefused(
        "state_dir_unresolved",
        f"platform {sys.platform!r} has no default state directory; set {STATE_DIR_ENV}",
    )


def _corrupt(detail: str) -> OperationRefused:
    return OperationRefused(
        "store_corrupt",
        f"{state_dir() / DB_NAME} is not a usable operation store ({detail}); restore the "
        "newest backup (docs/mcp-server.md, Logical operation state), or stop every hmcpctl "
        "process and delete the whole state directory to discard every record",
    )


def _check_private(path: Path, *, directory: bool) -> None:
    st = os.lstat(path)
    shape_ok = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if shape_ok and st.st_uid == os.geteuid() and not st.st_mode & 0o077:
        return
    wanted = (
        "a directory with mode 0700" if directory else "a regular file with mode 0600"
    )
    raise OperationRefused(
        "store_not_private",
        f"{path} must be {wanted} owned by uid {os.geteuid()} (observed "
        f"{stat.filemode(st.st_mode)}, uid {st.st_uid}); fix it with chmod or chown",
    )


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _read_store_id(conn: sqlite3.Connection) -> str:
    try:
        meta = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM meta")
        }
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        raise _corrupt("no meta table") from exc
    except sqlite3.DatabaseError as exc:
        raise _corrupt(type(exc).__name__) from exc
    if meta.get("schema_version") != SCHEMA_VERSION or not meta.get("store_id"):
        raise _corrupt("unknown schema version")
    return meta["store_id"]


def _write_private_file(path: Path, text: str) -> None:
    staging = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(staging, path)


def _create_store(root: Path) -> sqlite3.Connection:
    """Build the schema in a staging file, then link it into place without replacing."""
    db = root / DB_NAME
    staging = root / f".{DB_NAME}.{secrets.token_hex(4)}"
    os.close(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    store_id = secrets.token_hex(16)
    try:
        conn = _connect(staging)
        try:
            with write_transaction(conn):
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    (("store_id", store_id), ("schema_version", SCHEMA_VERSION)),
                )
        finally:
            conn.close()
        try:
            os.link(staging, db)
        except FileExistsError:
            return _open_existing(root, create=True)
    finally:
        staging.unlink(missing_ok=True)
    _write_private_file(root / SENTINEL_NAME, store_id)
    return _connect(db)


def _read_sentinel(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise _corrupt(f"{SENTINEL_NAME} is not text") from exc


def _open_existing(root: Path, *, create: bool) -> sqlite3.Connection:
    db, sentinel = root / DB_NAME, root / SENTINEL_NAME
    _check_private(db, directory=False)
    conn = _connect(db)
    try:
        store_id = _read_store_id(conn)
        if os.path.lexists(sentinel):
            _check_private(sentinel, directory=False)
            if _read_sentinel(sentinel) != store_id:
                raise _corrupt(f"{SENTINEL_NAME} names another store")
        elif create:
            _write_private_file(sentinel, store_id)
    except BaseException:
        conn.close()
        raise
    return conn


def _open(*, create: bool) -> sqlite3.Connection | None:
    root = state_dir()
    if not os.path.lexists(root):
        if not create:
            return None
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private(root, directory=True)
    db, sentinel = root / DB_NAME, root / SENTINEL_NAME
    if os.path.lexists(db):
        return _open_existing(root, create=create)
    if os.path.lexists(sentinel):
        raise OperationRefused(
            "store_lost",
            f"{sentinel} exists but {db} is missing; restore the newest backup "
            "(docs/mcp-server.md, Logical operation state), or stop every hmcpctl process and "
            f"delete the whole state directory {root} to discard every record",
        )
    return _create_store(root) if create else None


@contextmanager
def _translated() -> Generator[None]:
    try:
        yield
    except sqlite3.IntegrityError:
        raise
    except sqlite3.OperationalError as exc:
        raise OperationRefused(
            "store_unreadable",
            f"the operation store could not be used ({exc}); retry, and check {STATE_DIR_ENV}",
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise _corrupt(str(exc)) from exc
    except OSError as exc:
        raise OperationRefused(
            "store_unreadable",
            f"the state directory could not be used ({exc.strerror}); check {STATE_DIR_ENV}",
        ) from exc


@contextmanager
def session() -> Generator[sqlite3.Connection]:
    """Open (creating if needed) the store for one unit of work."""
    with _translated():
        conn = _open(create=True)
        if conn is None:
            raise OperationRefused(
                "store_unreadable", f"no store was created; check {STATE_DIR_ENV}"
            )
        try:
            yield conn
        finally:
            conn.close()


@contextmanager
def read_session() -> Generator[sqlite3.Connection | None]:
    """Open the store for reading; yield None when none has been created."""
    with _translated():
        conn = _open(create=False)
        try:
            yield conn
        finally:
            if conn is not None:
                conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


_LOCKS: dict[Path, int] = {}
_LOCKS_GUARD = threading.Lock()


def _open_lock_file(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    if (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == os.geteuid()
        and not st.st_mode & 0o077
    ):
        return fd
    os.close(fd)
    raise OperationRefused(
        "store_not_private",
        f"{path} must be a regular file with mode 0600 owned by uid {os.geteuid()} "
        f"(observed {stat.filemode(st.st_mode)}); fix it with chmod or chown",
    )


def _lock_holder(fd: int) -> str:
    text = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
    return text if text.isdigit() else "unknown"


def _still_locked(root: Path, fd: int) -> bool:
    """Whether *fd* is still the lock file on disk; deleting the directory replaces it."""
    try:
        on_disk = os.stat(root / LOCK_NAME)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (held.st_dev, held.st_ino) == (on_disk.st_dev, on_disk.st_ino)


def _recover_interrupted(conn: sqlite3.Connection, live: Collection[str]) -> None:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise _corrupt("integrity check failed")
    keep = tuple(live)
    skip = f" AND operation_id NOT IN ({', '.join('?' * len(keep))})" if keep else ""
    with write_transaction(conn):
        conn.execute(
            f"UPDATE effects SET status = 'uncertain' WHERE status = 'intended'{skip}",
            keep,
        )
        conn.execute(
            "UPDATE operations SET state = 'interrupted', updated_at = ?"
            f" WHERE state = 'running'{skip}",
            (_clock(), *keep),
        )


def acquire_execution_lock(live_operation_ids: Collection[str] = ()) -> None:
    """Take this process's execution lock once; refuse while another process holds it.

    Recovery on acquisition skips *live_operation_ids*: operations whose worker is alive
    in this process, which a replaced lock file must not turn into orphans.
    """
    root = state_dir()
    with _LOCKS_GUARD:
        if root in _LOCKS and _still_locked(root, _LOCKS[root]):
            return
        if root in _LOCKS:
            os.close(_LOCKS.pop(root))
        try:
            import fcntl
        except ImportError as exc:
            raise OperationRefused(
                "state_dir_unresolved",
                "logical operations need an fcntl lock (Linux or macOS)",
            ) from exc
        with session() as conn, _translated():
            fd = _open_lock_file(root / LOCK_NAME)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = _lock_holder(fd)
                os.close(fd)
                raise OperationRefused(
                    "execution_lock_held",
                    f"process {holder} runs logical operations on this host; retry after it exits",
                ) from None
            try:
                os.ftruncate(fd, 0)
                os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
                _recover_interrupted(conn, live_operation_ids)
            except BaseException:
                os.close(fd)
                raise
        _LOCKS[root] = fd


def release_execution_locks() -> None:
    """Release every execution lock this process holds (tests and shutdown)."""
    with _LOCKS_GUARD:
        for fd in _LOCKS.values():
            os.close(fd)
        _LOCKS.clear()


def validate_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise OperationRefused(
            "invalid_request_id", "request_id is 1-64 characters from A-Z a-z 0-9 . _ -"
        )


def accepted_continuations(state: str, outcome: str | None) -> tuple[str, ...]:
    """Return the continuations ADR 0190 Decision 6 accepts in *state*."""
    if state == "interrupted" or (state == "paused" and outcome == "needs_attention"):
        return ("resume", "abandon")
    if state == "paused" and outcome == "ready_to_boot":
        return ("boot", "abandon")
    return ()


def find_operation(
    conn: sqlite3.Connection, agent_id: str, request_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM operations WHERE agent_id = ? AND request_id = ?",
        (agent_id, request_id),
    ).fetchone()


def get_operation(conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        raise OperationRefused(
            "not_found",
            f"operation {operation_id} is not in the store; terminal operations are pruned "
            f"after {RETENTION_SECONDS // 86_400} days",
        )
    return row


def insert_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    agent_id: str,
    request_id: str,
    tool: str,
    connection: str,
    host: str,
    digest: str,
    request_json: str,
) -> None:
    """Prune expired terminal operations, check capacity, and insert a running record."""
    now = _clock()
    conn.execute(
        "DELETE FROM operations WHERE state = 'terminal' AND terminal_at < ?",
        (now - RETENTION_SECONDS,),
    )
    used = conn.execute(
        "SELECT (SELECT count(*) FROM operations WHERE state != 'terminal')"
        " + (SELECT count(*) FROM ledger)"
    ).fetchone()[0]
    if used >= STORE_CAPACITY:
        raise OperationRefused(
            "store_full",
            f"{used} live operations and ledger entries reach the {STORE_CAPACITY} limit; "
            "finish or abandon operations first",
        )
    conn.execute(
        "INSERT INTO operations (operation_id, agent_id, request_id, tool, connection, host,"
        " digest, request_json, state, phase, warnings_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', 'validating', '[]', ?, ?)",
        (
            operation_id,
            agent_id,
            request_id,
            tool,
            connection,
            host,
            digest,
            request_json,
            now,
            now,
        ),
    )


def set_state(
    conn: sqlite3.Connection,
    operation_id: str,
    state: str,
    outcome: str | None,
    *,
    warnings: tuple[str, ...] = (),
    result_json: str | None = None,
) -> None:
    now = _clock()
    conn.execute(
        "UPDATE operations SET state = ?, outcome = ?, warnings_json = ?,"
        " result_json = COALESCE(?, result_json), updated_at = ?, terminal_at = ?"
        " WHERE operation_id = ?",
        (
            state,
            outcome,
            json.dumps(list(warnings)),
            result_json,
            now,
            now if state == "terminal" else None,
            operation_id,
        ),
    )


def interrupt_orphan(conn: sqlite3.Connection, operation_id: str) -> None:
    """Mark a running record with no live worker interrupted, as lock acquisition does."""
    conn.execute(
        "UPDATE effects SET status = 'uncertain' WHERE operation_id = ? AND status = 'intended'",
        (operation_id,),
    )
    conn.execute(
        "UPDATE operations SET state = 'interrupted', updated_at = ? WHERE operation_id = ?",
        (_clock(), operation_id),
    )


def add_event(
    conn: sqlite3.Connection, operation_id: str, kind: str, detail: Mapping[str, Any]
) -> None:
    seq = conn.execute(
        "SELECT coalesce(max(seq), 0) + 1 FROM events WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO events (operation_id, seq, at, kind, detail_json) VALUES (?, ?, ?, ?, ?)",
        (operation_id, seq, _clock(), kind, json.dumps(dict(detail), sort_keys=True)),
    )
    conn.execute(
        "DELETE FROM events WHERE operation_id = ? AND seq <= ?",
        (operation_id, seq - MAX_EVENTS_KEPT),
    )


def set_phase(conn: sqlite3.Connection, operation_id: str, phase: str) -> None:
    conn.execute(
        "UPDATE operations SET phase = ?, updated_at = ? WHERE operation_id = ?",
        (phase, _clock(), operation_id),
    )
    add_event(conn, operation_id, "phase", {"phase": phase})


def guard_holder(
    conn: sqlite3.Connection, operation_id: str, system_uuid: str, partition_uuid: str
) -> str | None:
    row = conn.execute(
        "SELECT operation_id FROM operations WHERE system_uuid = ? AND partition_uuid = ?"
        " AND state != 'terminal' AND operation_id != ?",
        (system_uuid, partition_uuid, operation_id),
    ).fetchone()
    return None if row is None else row["operation_id"]


def set_partition(
    conn: sqlite3.Connection, operation_id: str, system_uuid: str, partition_uuid: str
) -> None:
    conn.execute(
        "UPDATE operations SET system_uuid = ?, partition_uuid = ? WHERE operation_id = ?",
        (system_uuid, partition_uuid, operation_id),
    )


def _effect(row: sqlite3.Row) -> EffectRecord:
    identity = (
        None if row["identity_json"] is None else json.loads(row["identity_json"])
    )
    return EffectRecord(row["key"], row["kind"], row["target"], row["status"], identity)


def load_effects(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, EffectRecord]:
    rows = conn.execute(
        "SELECT * FROM effects WHERE operation_id = ? ORDER BY seq", (operation_id,)
    )
    return {row["key"]: _effect(row) for row in rows}


def record_intent(
    conn: sqlite3.Connection, operation_id: str, key: str, kind: str, target: str
) -> None:
    seq = conn.execute(
        "SELECT coalesce(max(seq), 0) + 1 FROM effects WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO effects (operation_id, key, seq, kind, target, status)"
        " VALUES (?, ?, ?, ?, ?, 'intended')"
        " ON CONFLICT (operation_id, key) DO UPDATE SET kind = excluded.kind,"
        " target = excluded.target, status = 'intended', identity_json = NULL",
        (operation_id, key, seq, kind, target),
    )
    add_event(
        conn, operation_id, "intent", {"key": key, "kind": kind, "target": target}
    )


def record_outcome(
    conn: sqlite3.Connection,
    operation_id: str,
    key: str,
    status: str,
    identity: Mapping[str, Any] | None,
) -> None:
    conn.execute(
        "UPDATE effects SET status = ?, identity_json = ? WHERE operation_id = ? AND key = ?",
        (
            status,
            None if identity is None else json.dumps(dict(identity), sort_keys=True),
            operation_id,
            key,
        ),
    )
    add_event(conn, operation_id, "outcome", {"key": key, "status": status})


def add_ledger(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    key: str,
    kind: str,
    system_uuid: str,
    partition_uuid: str | None,
    identity: Mapping[str, Any],
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ledger (operation_id, key, kind, system_uuid, partition_uuid,"
        " identity_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            operation_id,
            key,
            kind,
            system_uuid,
            partition_uuid,
            json.dumps(dict(identity), sort_keys=True),
            _clock(),
        ),
    )


def _iso(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds")
    return moment.replace("+00:00", "Z")


def read_record(conn: sqlite3.Connection, operation_id: str) -> OperationRecord:
    row = get_operation(conn, operation_id)
    newest = conn.execute(
        "SELECT * FROM events WHERE operation_id = ? ORDER BY seq DESC LIMIT ?",
        (operation_id, STATUS_EVENTS + 1),
    ).fetchall()
    events = tuple(
        OperationEvent(r["seq"], _iso(r["at"]), r["kind"], json.loads(r["detail_json"]))
        for r in reversed(newest[:STATUS_EVENTS])
    )
    return OperationRecord(
        operation_id=row["operation_id"],
        request_id=row["request_id"],
        tool=row["tool"],
        connection=row["connection"],
        system_uuid=row["system_uuid"],
        partition_uuid=row["partition_uuid"],
        state=row["state"],
        phase=row["phase"],
        outcome=row["outcome"],
        effects=tuple(load_effects(conn, operation_id).values()),
        events=events,
        events_truncated=len(newest) > STATUS_EVENTS,
        warnings=tuple(json.loads(row["warnings_json"])),
        next_actions=accepted_continuations(row["state"], row["outcome"]),
        result=None if row["result_json"] is None else json.loads(row["result_json"]),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


_INVALID_CURSOR = "invalid_cursor: pass next_cursor from a previous page unchanged"


def _encode_cursor(created_at: float, operation_id: str) -> str:
    return base64.urlsafe_b64encode(
        json.dumps([created_at, operation_id]).encode()
    ).decode()


def _decode_cursor(cursor: str) -> tuple[float, str]:
    if len(cursor) > _MAX_CURSOR:
        raise ValueError(_INVALID_CURSOR)
    try:
        created_at, operation_id = json.loads(
            base64.urlsafe_b64decode(cursor.encode("ascii"))
        )
    except (ValueError, TypeError, binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError(_INVALID_CURSOR) from exc
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, int | float)
        or not isinstance(operation_id, str)
        or not _OPERATION_ID.fullmatch(operation_id)
    ):
        raise ValueError(_INVALID_CURSOR)
    return float(created_at), operation_id


def connection_label(profile: str | None, *, tool: str) -> str:
    """Return the connection the access policy authorizes *profile* as (ADR 0038).

    ``HMC_HOST`` collapses every profile to ``<default>`` and a nickname resolves to its
    profile key, so records are labelled and filtered by the connection actually used.
    """
    connection = selected_connection(profile, tool=tool)
    if connection == UNRESOLVED:
        raise ValueError(f"{tool}: profile {profile!r} names no configured connection")
    return DEFAULT_CONNECTION if connection is None else connection


def operation_status(
    *,
    agent_id: str,
    connection: str,
    host: str,
    operation_id: str | None = None,
    request_id: str | None = None,
    state: str | None = None,
    outcome: str | None = None,
    limit: int = MAX_PAGE,
    cursor: str | None = None,
) -> OperationPage:
    """Return one page of *agent_id*'s operations on *connection* at *host*, newest first.

    Filtering on the connection keeps a caller whose policy grants one connection from
    reading records another process made on a connection it is not granted; filtering on
    the host matches the (connection, host) pair ``submit`` binds, since ``<default>``
    binds late (ADR 0038).
    """
    if operation_id is not None and not _OPERATION_ID.fullmatch(operation_id):
        raise ValueError(
            "invalid_operation_id: operation_id is 32 lower-case hex digits"
        )
    if request_id is not None:
        validate_request_id(request_id)
    if not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"invalid_limit: limit must be from 1 to {MAX_PAGE}")
    after = None if cursor is None else _decode_cursor(cursor)
    clauses = ["agent_id = ?", "connection = ?", "host = ?"]
    params: list[Any] = [agent_id, connection, host]
    filters = (
        ("operation_id", operation_id),
        ("request_id", request_id),
        ("state", state),
        ("outcome", outcome),
    )
    for column, value in filters:
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if after is not None:
        clauses.append("(created_at < ? OR (created_at = ? AND operation_id < ?))")
        params.extend((after[0], after[0], after[1]))
    with read_session() as conn:
        if conn is None:
            return OperationPage((), limit, False, None)
        conn.execute("BEGIN")
        rows = conn.execute(
            f"SELECT operation_id, created_at FROM operations WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at DESC, operation_id DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        records = tuple(read_record(conn, row["operation_id"]) for row in rows[:limit])
        conn.execute("COMMIT")
    truncated = len(rows) > limit
    last = rows[limit - 1] if truncated else None
    next_cursor = (
        None
        if last is None
        else _encode_cursor(last["created_at"], last["operation_id"])
    )
    return OperationPage(records, limit, truncated, next_cursor)
