"""ADR 0190 operation store: location, privacy, lock, bounds and status (#1218)."""

from __future__ import annotations

import asyncio
import dataclasses
import fcntl
import json
import os
import sqlite3
from contextlib import closing
from typing import get_args

import pytest

from hmcpctl.operations.logical import models, store
from hmcpctl.operations.logical.store import OperationRefused


def _reason(fn, *args, **kwargs) -> str:
    with pytest.raises(OperationRefused) as info:
        fn(*args, **kwargs)
    return info.value.reason


def _open_read():
    with store.read_session() as conn:
        return conn


def _open_write():
    with store.session() as conn:
        return conn


def _seed(conn, operation_id, *, agent="agent-a", request_id=None, state="running"):
    with store.write_transaction(conn):
        store.insert_operation(
            conn,
            operation_id=operation_id,
            agent_id=agent,
            request_id=request_id or f"r{int(operation_id, 16)}",
            tool="hmc_test_tool",
            connection="<default>",
            host="hmc.test",
            digest="d" * 64,
            request_json='{"arguments":{}}',
        )
        if state != "running":
            outcome = {"terminal": "completed", "paused": "needs_attention"}.get(state)
            store.set_state(conn, operation_id, state, outcome)


def _oid(n: int) -> str:
    return f"{n:032x}"


def test_record_fields_are_described():
    for record in (
        models.EffectRecord,
        models.OperationEvent,
        models.OperationRecord,
        models.OperationPage,
    ):
        assert record.__module__ == "hmcpctl.operations.logical.models"
        for item in dataclasses.fields(record):
            assert item.metadata.get("description"), (record.__name__, item.name)


def test_state_dir_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv(store.STATE_DIR_ENV, str(tmp_path / "s"))
    assert store.state_dir() == tmp_path / "s"


def test_state_dir_linux_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv(store.STATE_DIR_ENV)
    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert store.state_dir() == tmp_path / "hmcpctl"


@pytest.mark.parametrize("value", ["state", "~/hmc-state", "./s"])
def test_relative_state_dir_is_refused(monkeypatch, value):
    monkeypatch.setenv(store.STATE_DIR_ENV, value)
    assert _reason(store.state_dir) == "state_dir_unresolved"


def test_relative_xdg_state_home_is_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv(store.STATE_DIR_ENV)
    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert store.state_dir() == tmp_path / ".local" / "state" / "hmcpctl"


def test_state_dir_linux_default(monkeypatch, tmp_path):
    monkeypatch.delenv(store.STATE_DIR_ENV)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert store.state_dir() == tmp_path / ".local" / "state" / "hmcpctl"


def test_state_dir_macos(monkeypatch, tmp_path):
    monkeypatch.delenv(store.STATE_DIR_ENV)
    monkeypatch.setattr(store.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        store.state_dir()
        == tmp_path / "Library" / "Application Support" / "hmcpctl-state"
    )


def test_state_dir_other_platform_refuses(monkeypatch):
    monkeypatch.delenv(store.STATE_DIR_ENV)
    monkeypatch.setattr(store.sys, "platform", "win32")
    assert _reason(store.state_dir) == "state_dir_unresolved"


def test_state_dir_unresolvable_home_refuses(monkeypatch):
    monkeypatch.delenv(store.STATE_DIR_ENV)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(store.sys, "platform", "linux")

    def no_home():
        raise RuntimeError("no home")

    monkeypatch.setattr(store.Path, "home", staticmethod(no_home))
    assert _reason(store.state_dir) == "state_dir_unresolved"


def test_read_without_store_creates_nothing():
    assert _open_read() is None
    assert not store.state_dir().exists()


def test_session_creates_private_store():
    _open_write()
    root = store.state_dir()
    assert root.stat().st_mode & 0o777 == 0o700
    for name in (store.DB_NAME, store.SENTINEL_NAME):
        assert (root / name).stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in root.iterdir()) == [
        store.DB_NAME,
        store.SENTINEL_NAME,
    ]


def test_directory_created_concurrently_is_used(monkeypatch):
    root = store.state_dir()
    real = os.path.lexists

    def raced(path):
        if os.fspath(path) == os.fspath(root) and not real(path):
            root.mkdir(mode=0o700, parents=True)
            return False
        return real(path)

    monkeypatch.setattr(store.os.path, "lexists", raced)
    _open_write()
    assert (root / store.DB_NAME).exists()


@pytest.mark.parametrize("name", ["", store.DB_NAME, store.SENTINEL_NAME])
def test_group_or_other_bits_are_not_private(name):
    _open_write()
    path = store.state_dir() / name if name else store.state_dir()
    path.chmod(0o750 if not name else 0o640)
    assert _reason(_open_read) == "store_not_private"
    assert _reason(_open_write) == "store_not_private"


def test_symlinked_directory_is_not_private(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setenv(store.STATE_DIR_ENV, str(tmp_path / "link"))
    assert _reason(_open_write) == "store_not_private"


def test_sentinel_without_db_is_lost():
    _open_write()
    (store.state_dir() / store.DB_NAME).unlink()
    assert _reason(_open_read) == "store_lost"
    assert _reason(_open_write) == "store_lost"


def test_db_without_sentinel_recovers_on_write_only():
    _open_write()
    sentinel = store.state_dir() / store.SENTINEL_NAME
    sentinel.unlink()
    assert _open_read() is not None
    assert not sentinel.exists()
    _open_write()
    assert sentinel.exists()


def test_sentinel_naming_another_store_is_corrupt():
    _open_write()
    (store.state_dir() / store.SENTINEL_NAME).write_text("0" * 32)
    assert _reason(_open_read) == "store_corrupt"


def test_garbage_db_is_corrupt():
    _open_write()
    (store.state_dir() / store.DB_NAME).write_bytes(b"not a database at all" * 100)
    assert _reason(_open_read) == "store_corrupt"


def test_unknown_schema_version_is_corrupt():
    _open_write()
    raw = store._connect(store.state_dir() / store.DB_NAME)
    raw.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    raw.close()
    assert _reason(_open_read) == "store_corrupt"


def _hold_lock_as_other_process(pid_text: str) -> int:
    _open_write()
    fd = os.open(store.state_dir() / store.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.pwrite(fd, pid_text.encode(), 0)
    return fd


def test_lock_refused_names_holder():
    fd = _hold_lock_as_other_process("4242\n")
    try:
        with pytest.raises(OperationRefused) as info:
            store.acquire_execution_lock()
        assert info.value.reason == "execution_lock_held"
        assert "process 4242" in str(info.value)
    finally:
        os.close(fd)


def test_lock_holder_text_is_never_echoed():
    fd = _hold_lock_as_other_process("$(rm -rf)\n")
    try:
        with pytest.raises(OperationRefused) as info:
            store.acquire_execution_lock()
        assert "process unknown" in str(info.value)
    finally:
        os.close(fd)


def test_lock_marks_running_interrupted_and_intents_uncertain():
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            store.record_intent(conn, _oid(1), "create", "test.create", "lpar:x")
    store.acquire_execution_lock()
    store.acquire_execution_lock()
    with store.session() as conn:
        record = store.read_record(conn, _oid(1))
    assert record.state == "interrupted"
    assert record.effects[0].status == "uncertain"
    assert record.next_actions == ("resume", "abandon")
    lock = store.state_dir() / store.LOCK_NAME
    assert lock.read_text().strip() == str(os.getpid())
    assert lock.stat().st_mode & 0o777 == 0o600


def test_replaced_lock_file_is_reacquired_with_recovery():
    store.acquire_execution_lock()
    lock = store.state_dir() / store.LOCK_NAME
    lock.unlink()
    with store.session() as conn:
        _seed(conn, _oid(1))
    store.acquire_execution_lock()
    with store.session() as conn:
        assert store.read_record(conn, _oid(1)).state == "interrupted"
    fd = os.open(lock, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_busy_store_is_unreadable(monkeypatch):
    _open_write()
    monkeypatch.setattr(store, "BUSY_TIMEOUT_SECONDS", 0.05)
    blocker = store._connect(store.state_dir() / store.DB_NAME)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert (
            _reason(
                store.operation_status,
                agent_id="agent-a",
                connection="<default>",
                host="hmc.test",
            )
            == "store_unreadable"
        )
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_prune_terminal_after_30_days_keeps_ledger(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(store, "_clock", lambda: now)
    with store.session() as conn:
        _seed(conn, _oid(1), state="terminal")
        with store.write_transaction(conn):
            store.add_ledger(
                conn,
                _oid(1),
                key="disk",
                kind="disk",
                system_uuid="sys",
                partition_uuid=None,
                identity={"name": "d1"},
            )
    now += store.RETENTION_SECONDS + 1
    with store.session() as conn:
        _seed(conn, _oid(2))
        assert store.find_operation(conn, "agent-a", "r1") is None
        assert conn.execute("SELECT count(*) FROM ledger").fetchone()[0] == 1


def test_store_full_refuses_new_operations():
    with store.session() as conn:
        with store.write_transaction(conn):
            conn.executemany(
                "INSERT INTO ledger VALUES (?, 'k', 'disk', 'sys', NULL, '{}', 0)",
                ((f"op{i}",) for i in range(store.STORE_CAPACITY)),
            )
        assert _reason(_seed, conn, _oid(1)) == "store_full"


def test_event_cap_keeps_newest():
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            for n in range(store.MAX_EVENTS_KEPT + 50):
                store.add_event(conn, _oid(1), "phase", {"phase": f"p{n}"})
        assert (
            conn.execute("SELECT count(*) FROM events").fetchone()[0]
            == store.MAX_EVENTS_KEPT
        )
        record = store.read_record(conn, _oid(1))
    assert len(record.events) == store.STATUS_EVENTS
    assert record.events_truncated is True
    assert record.events[-1].detail == {"phase": f"p{store.MAX_EVENTS_KEPT + 49}"}


def test_status_filters_agent_and_paginates(monkeypatch):
    ticks = iter(range(1, 100))
    monkeypatch.setattr(store, "_clock", lambda: float(next(ticks)))
    with store.session() as conn:
        for n in (1, 2, 3):
            _seed(conn, _oid(n))
        _seed(conn, _oid(9), agent="agent-b")
    first = store.operation_status(
        agent_id="agent-a", connection="<default>", host="hmc.test", limit=2
    )
    assert [r.operation_id for r in first.operations] == [_oid(3), _oid(2)]
    assert first.truncated and first.next_cursor
    second = store.operation_status(
        agent_id="agent-a",
        connection="<default>",
        host="hmc.test",
        limit=2,
        cursor=first.next_cursor,
    )
    assert [r.operation_id for r in second.operations] == [_oid(1)]
    assert not second.truncated and second.next_cursor is None
    other = store.operation_status(
        agent_id="agent-a",
        connection="<default>",
        host="hmc.test",
        operation_id=_oid(9),
    )
    assert other.operations == ()


def test_status_filters_by_state_and_request_id():
    with store.session() as conn:
        _seed(conn, _oid(1), request_id="alpha")
        _seed(conn, _oid(2), request_id="beta", state="terminal")
    assert [
        r.request_id
        for r in store.operation_status(
            agent_id="agent-a",
            connection="<default>",
            host="hmc.test",
            state="terminal",
        ).operations
    ] == ["beta"]
    assert [
        r.operation_id
        for r in store.operation_status(
            agent_id="agent-a",
            connection="<default>",
            host="hmc.test",
            request_id="alpha",
        ).operations
    ] == [_oid(1)]


def test_status_without_store_is_empty():
    page = store.operation_status(
        agent_id="agent-a", connection="<default>", host="hmc.test"
    )
    assert page == models.OperationPage((), 50, False, None)
    assert not store.state_dir().exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation_id": "XYZ"},
        {"request_id": "bad id"},
        {"limit": 0},
        {"limit": 51},
        {"cursor": "!!!"},
        {"cursor": "W10="},
        {"cursor": "x" * 300},
        {"cursor": "WzEuMCwgIm5vdC1oZXgiXQ=="},
    ],
)
def test_status_rejects_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        store.operation_status(
            agent_id="agent-a", connection="<default>", host="hmc.test", **kwargs
        )


def test_status_reports_a_lost_store():
    _open_write()
    (store.state_dir() / store.DB_NAME).unlink()
    assert (
        _reason(
            store.operation_status,
            agent_id="agent-a",
            connection="<default>",
            host="hmc.test",
        )
        == "store_lost"
    )


def test_status_never_returns_the_request():
    with store.session() as conn:
        _seed(conn, _oid(1))
    record = store.operation_status(
        agent_id="agent-a", connection="<default>", host="hmc.test"
    ).operations[0]
    assert "request_json" not in json.dumps(dataclasses.asdict(record))


def test_status_lists_only_the_callers_connection():
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            store.insert_operation(
                conn,
                operation_id=_oid(2),
                agent_id="agent-a",
                request_id="prod-op",
                tool="hmc_test_tool",
                connection="prod",
                host="hmc.prod",
                digest="d" * 64,
                request_json='{"arguments":{}}',
            )
    lab = store.operation_status(
        agent_id="agent-a", connection="<default>", host="hmc.test"
    )
    prod = store.operation_status(
        agent_id="agent-a", connection="prod", host="hmc.prod"
    )
    assert [r.operation_id for r in lab.operations] == [_oid(1)]
    assert [r.operation_id for r in prod.operations] == [_oid(2)]


def test_status_lists_only_the_callers_hmc_host():
    """<default> binds late (ADR 0038): the same label on another HMC is not this caller's."""
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            store.insert_operation(
                conn,
                operation_id=_oid(2),
                agent_id="agent-a",
                request_id="elsewhere",
                tool="hmc_test_tool",
                connection="<default>",
                host="other.hmc",
                digest="d" * 64,
                request_json='{"arguments":{}}',
            )
    page = store.operation_status(
        agent_id="agent-a", connection="<default>", host="hmc.test"
    )
    assert [r.operation_id for r in page.operations] == [_oid(1)]


_NICKNAME_CONFIG = """\
[profiles.lab]
host = "lab-hmc.example.test"
user = "admin"

[nicknames]
big-iron = "lab"
"""


@pytest.fixture
def lab_config(tmp_path, monkeypatch):
    """A home holding one ``lab`` profile and its ``big-iron`` nickname."""
    from hmcpctl.config import config_dir

    for name in ("XDG_CONFIG_HOME", "APPDATA", "HMC_HOST", "HMC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = config_dir() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NICKNAME_CONFIG, encoding="utf-8")


def test_connection_label_matches_the_access_policy_default_token(lab_config):
    from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN

    assert store.DEFAULT_CONNECTION == DEFAULT_CONNECTION_TOKEN
    assert store.connection_label(None, tool="t") == DEFAULT_CONNECTION_TOKEN
    assert store.connection_label("lab", tool="t") == "lab"


def test_connection_label_resolves_a_nickname_to_its_profile_key(lab_config):
    assert store.connection_label("big-iron", tool="t") == "lab"


def test_connection_label_follows_hmc_host_to_the_default(lab_config, monkeypatch):
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    assert store.connection_label("lab", tool="t") == store.DEFAULT_CONNECTION


def test_connection_label_refuses_an_unconfigured_profile(lab_config):
    with pytest.raises(ValueError, match="names no configured connection"):
        store.connection_label("never-configured", tool="t")


def test_connection_label_sanitizes_unreadable_configuration(lab_config):
    from hmcpctl.config import ConfigError, config_dir

    path = config_dir() / "config.toml"
    path.write_text("[broken", encoding="utf-8")
    with pytest.raises(
        ValueError, match="t cannot resolve the configured HMC connection"
    ) as raised:
        store.connection_label("lab", tool="t")
    assert str(path) not in str(raised.value)
    assert isinstance(raised.value.__cause__, ConfigError)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in ("state", "outcome", "effect_status", "event_kind")
        for value in ("private-invalid-value", 0, [], None)
        if not (field == "outcome" and value is None)
    ],
)
def test_invalid_vocabulary_write_leaves_rows_and_events_unchanged(field, value):
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            store.record_intent(conn, _oid(1), "step", "test.write", "target")
        before = tuple(conn.execute("SELECT * FROM operations").fetchone())
        before_effect = tuple(conn.execute("SELECT * FROM effects").fetchone())
        before_events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        with pytest.raises(OperationRefused) as caught:
            if field == "event_kind":
                store.add_event(conn, _oid(1), value, {})
            elif field == "effect_status":
                store.record_outcome(conn, _oid(1), "step", value, None)
            else:
                store.set_state(
                    conn,
                    _oid(1),
                    value if field == "state" else "running",
                    value if field == "outcome" else None,
                )
        assert caught.value.reason == f"invalid_{field}"
        if isinstance(value, str):
            assert value not in str(caught.value)
        assert tuple(conn.execute("SELECT * FROM operations").fetchone()) == before
        assert tuple(conn.execute("SELECT * FROM effects").fetchone()) == before_effect
        assert (
            conn.execute("SELECT count(*) FROM events").fetchone()[0] == before_events
        )


@pytest.mark.parametrize(
    ("field", "reader"),
    [
        (field, reader)
        for field in ("state", "outcome")
        for reader in ("get", "find", "record", "status")
    ]
    + [("effect_status", reader) for reader in ("effects", "record", "status")]
    + [("event_kind", reader) for reader in ("record", "status")],
)
def test_corrupt_vocabulary_never_decodes_as_a_typed_record(field, reader):
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            store.record_intent(conn, _oid(1), "step", "test.write", "target")
            if field == "event_kind":
                conn.execute("UPDATE events SET kind = ?", ("private-invalid-value",))
            elif field == "effect_status":
                conn.execute(
                    "UPDATE effects SET status = ?", ("private-invalid-value",)
                )
            else:
                conn.execute(
                    f"UPDATE operations SET {field} = ?", ("private-invalid-value",)
                )
        reads = {
            "get": lambda: store.get_operation(conn, _oid(1)),
            "find": lambda: store.find_operation(conn, "agent-a", "r1"),
            "record": lambda: store.read_record(conn, _oid(1)),
            "effects": lambda: store.load_effects(conn, _oid(1)),
            "status": lambda: store.operation_status(
                agent_id="agent-a", connection="<default>", host="hmc.test"
            ),
        }
        with pytest.raises(OperationRefused) as caught:
            reads[reader]()
        assert caught.value.reason == "store_corrupt"
        assert "private-invalid-value" not in str(caught.value)


@pytest.mark.parametrize(
    ("state", "outcome"),
    [(state, None) for state in get_args(models.OperationState)]
    + [("terminal", outcome) for outcome in get_args(models.OperationOutcome)],
)
def test_every_declared_operation_value_roundtrips(state, outcome):
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            store.set_state(conn, _oid(1), state, outcome)
        record = store.read_record(conn, _oid(1))
    assert (record.state, record.outcome) == (state, outcome)


@pytest.mark.parametrize("status", get_args(models.EffectStatus))
def test_every_declared_effect_status_roundtrips(status):
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            store.record_intent(conn, _oid(1), "step", "test.write", "target")
            store.record_outcome(conn, _oid(1), "step", status, None)
        assert store.load_effects(conn, _oid(1))["step"].status == status


@pytest.mark.parametrize("kind", ["phase", "intent", "outcome"])
def test_every_declared_event_kind_roundtrips(kind):
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            store.add_event(conn, _oid(1), kind, {"step": "probe"})
        record = store.read_record(conn, _oid(1))
    assert record.events[-1].kind == kind
    assert record.events[-1].detail == {"step": "probe"}


def test_event_truncation_sentinel_is_not_decoded():
    with store.session() as conn:
        _seed(conn, _oid(1))
        with store.write_transaction(conn):
            for n in range(store.STATUS_EVENTS + 1):
                store.add_event(conn, _oid(1), "phase", {"phase": f"p{n}"})
            conn.execute("UPDATE events SET kind = 'old-invalid-kind' WHERE seq = 1")
        record = store.read_record(conn, _oid(1))
    assert record.events_truncated is True
    assert len(record.events) == store.STATUS_EVENTS
    assert all(event.kind == "phase" for event in record.events)


def test_write_transaction_preserves_sqlite_trigger_failure():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(
            "CREATE TABLE example (value INTEGER);"
            "CREATE TRIGGER abort_insert BEFORE INSERT ON example "
            "BEGIN SELECT RAISE(ROLLBACK, 'original write failure'); END;"
        )
        with (
            pytest.raises(sqlite3.IntegrityError, match="original write failure"),
            store.write_transaction(conn),
        ):
            conn.execute("INSERT INTO example VALUES (1)")
        assert not conn.in_transaction
        assert conn.execute("SELECT * FROM example").fetchall() == []


@pytest.mark.parametrize(
    "primary", [ValueError("write failed"), asyncio.CancelledError()]
)
def test_write_transaction_preserves_primary_when_rollback_fails(primary):
    class RollbackFailure(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("controlled rollback failure")
            return super().execute(sql, parameters)

    with closing(sqlite3.connect(":memory:", factory=RollbackFailure)) as conn:
        with pytest.raises(type(primary)) as caught, store.write_transaction(conn):
            raise primary
        assert caught.value is primary
        assert any("OperationalError" in note for note in primary.__notes__)
        assert all(
            "controlled rollback failure" not in note for note in primary.__notes__
        )
        conn.rollback()


def test_write_transaction_rolls_back_body_failure():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE example (value INTEGER)")
        primary = ValueError("write failed")
        with pytest.raises(ValueError) as caught, store.write_transaction(conn):
            conn.execute("INSERT INTO example VALUES (1)")
            raise primary
        assert caught.value is primary
        assert not conn.in_transaction
        assert conn.execute("SELECT * FROM example").fetchall() == []
