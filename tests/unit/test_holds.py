"""ADR 0193 hold store and hook body on the ADR 0190 store (#1851, ADR 0327)."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing

import pytest

from hmcpctl.call_context import HOLD_EXEMPT_TOOLS, CallContext, calling, current_call
from hmcpctl.operations.logical import holds, store
from hmcpctl.operations.logical.holds import HoldRefused
from hmcpctl.operations.logical.store import OperationRefused

SYS = "0b3f9a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
LPAR = "7d1e2f3a-4b5c-4d6e-8f70-819a2b3c4d5e"
OTHER = "11111111-2222-4333-8444-555555555555"


def _reason(fn, *args, **kwargs) -> str:
    with pytest.raises(OperationRefused) as info:
        fn(*args, **kwargs)
    return info.value.reason


def _place(**overrides) -> holds.Hold:
    arguments = {
        "consumer_label": "kdive",
        "agent_id": "agent-a",
        "connection": "<default>",
    }
    arguments.update(overrides)
    return holds.place_hold(SYS, LPAR, **arguments)


def _schema_one_store() -> None:
    """Write a store exactly as schema 1 left it, with one operation row."""
    root = store.state_dir()
    root.mkdir(mode=0o700, parents=True)
    db = root / store.DB_NAME
    with closing(sqlite3.connect(db)) as conn:
        for statement in store._SCHEMA:
            if "holds" not in statement:
                conn.execute(statement)
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (("store_id", "a" * 32), ("schema_version", "1")),
        )
        conn.execute(
            "INSERT INTO operations (operation_id, agent_id, request_id, tool, connection,"
            " host, digest, request_json, state, phase, warnings_json, created_at,"
            " updated_at) VALUES (?, 'agent-a', 'r1', 't', '<default>', 'h', 'd', '{}',"
            " 'running', 'validating', '[]', 0, 0)",
            ("0" * 32,),
        )
        conn.commit()
    db.chmod(0o600)
    (root / store.SENTINEL_NAME).write_text("a" * 32)
    (root / store.SENTINEL_NAME).chmod(0o600)


def _schema_version() -> str:
    with closing(sqlite3.connect(store.state_dir() / store.DB_NAME)) as conn:
        return conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]


@pytest.mark.parametrize("opener", [store.session, store.read_session])
def test_schema_one_store_upgrades_in_place_keeping_its_rows(opener):
    _schema_one_store()
    with opener() as conn:
        assert conn is not None
        assert conn.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM holds").fetchone()[0] == 0
    assert _schema_version() == "2"


def test_two_connections_upgrading_together_both_succeed():
    _schema_one_store()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def open_store() -> None:
        try:
            barrier.wait()
            with store.read_session() as conn:
                assert conn is not None
                conn.execute("SELECT count(*) FROM holds").fetchone()
        except BaseException as error:  # noqa: BLE001 - reported by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=open_store) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert _schema_version() == "2"


def test_unknown_schema_version_still_refuses():
    with store.session():
        pass
    with closing(sqlite3.connect(store.state_dir() / store.DB_NAME)) as conn:
        conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
        conn.commit()
    assert _reason(holds.find_hold, LPAR) == "store_corrupt"


def test_place_hold_mints_an_id_and_repeat_returns_it():
    first = _place()
    assert len(first.hold_id) == 32 and int(first.hold_id, 16) >= 0
    assert (first.system_uuid, first.partition_uuid) == (SYS, LPAR)
    assert first.created_at.endswith("Z")
    assert _place() == first


def test_place_hold_from_another_agent_or_label_is_refused_without_the_id():
    first = _place()
    for overrides in ({"agent_id": "agent-b"}, {"consumer_label": "other"}):
        with pytest.raises(OperationRefused) as info:
            _place(**overrides)
        assert info.value.reason == "partition_held"
        assert first.hold_id not in str(info.value)
        assert "kdive" in str(info.value) and "agent-a" in str(info.value)


@pytest.mark.parametrize("label", ["", "x" * 65, "bad\nlabel", 7])
def test_place_hold_validates_the_label(label):
    assert _reason(_place, consumer_label=label) == "invalid_consumer_label"


def test_place_hold_refuses_while_an_operation_guards_the_partition():
    with store.session() as conn, store.write_transaction(conn):
        store.insert_operation(
            conn,
            operation_id="f" * 32,
            agent_id="agent-a",
            request_id="r1",
            tool="hmc_power_lpar",
            connection="<default>",
            host="hmc.test",
            digest="d" * 64,
            request_json="{}",
        )
        store.set_partition(conn, "f" * 32, SYS.upper(), LPAR.upper())
    with pytest.raises(OperationRefused) as info:
        _place()
    assert info.value.reason == "partition_guarded"
    assert "f" * 32 in str(info.value)


def test_holds_count_toward_store_capacity(monkeypatch):
    monkeypatch.setattr(store, "STORE_CAPACITY", 1)
    _place()
    assert (
        _reason(
            holds.place_hold,
            SYS,
            OTHER,
            consumer_label="k",
            agent_id="a",
            connection="<default>",
        )
        == "store_full"
    )
    with (
        store.session() as conn,
        store.write_transaction(conn),
        pytest.raises(OperationRefused) as info,
    ):
        store.insert_operation(
            conn,
            operation_id="e" * 32,
            agent_id="agent-a",
            request_id="r2",
            tool="t",
            connection="<default>",
            host="h",
            digest="d",
            request_json="{}",
        )
    assert info.value.reason == "store_full"


def test_uuids_are_case_insensitive():
    placed = holds.place_hold(
        SYS.upper(), LPAR.upper(), consumer_label="k", agent_id="a", connection="c"
    )
    assert placed.partition_uuid == LPAR
    assert holds.find_hold(LPAR.upper()) == placed
    assert holds.find_hold(LPAR) == placed


def test_release_by_hold_id_or_placing_agent():
    placed = _place()
    assert _reason(holds.release_hold, SYS, LPAR, agent_id="agent-b") == "hold_mismatch"
    assert (
        _reason(holds.release_hold, SYS, LPAR, agent_id="agent-b", hold_id="0" * 32)
        == "hold_mismatch"
    )
    assert (
        holds.release_hold(SYS, LPAR, agent_id="agent-b", hold_id=placed.hold_id)
        == placed
    )
    assert holds.find_hold(LPAR) is None
    _place()
    assert holds.release_hold(SYS, LPAR, agent_id="agent-a") is not None
    assert holds.release_hold(SYS, LPAR, agent_id="agent-a") is None


def test_mismatch_never_names_the_hold_id():
    placed = _place()
    with pytest.raises(OperationRefused) as info:
        holds.release_hold(SYS, LPAR, agent_id="agent-b", hold_id="wrong")
    assert placed.hold_id not in str(info.value)


def test_list_holds_operator_release_and_presence():
    assert holds.holds_present() is False
    assert holds.list_holds() == ()
    placed = _place()
    assert holds.holds_present() is True
    assert holds.list_holds() == (placed,)
    assert holds.operator_release(LPAR.upper()) == (placed,)
    assert holds.operator_release(LPAR) == ()
    assert holds.list_holds() == ()


def test_reads_never_create_a_store():
    assert holds.find_hold(LPAR) is None
    assert holds.holds_present() is False
    assert not store.state_dir().exists()


def _enforce(context: CallContext | None) -> None:
    if context is None:
        holds.enforce_hold(LPAR)
        return
    with calling(context):
        holds.enforce_hold(LPAR)


def test_enforce_passes_without_a_hold_or_a_store():
    _enforce(None)
    _enforce(CallContext("hmc_power_lpar", read=False, hold_id=None))


def test_enforce_refuses_a_mutation_without_the_matching_id():
    placed = _place()
    for context in (
        None,
        CallContext("hmc_power_lpar", read=False, hold_id=None),
        CallContext("hmc_power_lpar", read=False, hold_id="0" * 32),
        CallContext(None, read=False, hold_id="ünicode"),
    ):
        with pytest.raises(HoldRefused) as info:
            _enforce(context)
        assert info.value.reason == "partition_held"
        assert placed.hold_id not in str(info.value)
        assert "hmcpctl holds release" in str(info.value)


def test_enforce_passes_reads_exempt_tools_and_the_matching_id():
    placed = _place()
    _enforce(CallContext("hmc_lpar_summary", read=True, hold_id=None))
    for tool in HOLD_EXEMPT_TOOLS:
        _enforce(CallContext(tool, read=False, hold_id=None))
    _enforce(CallContext("hmc_power_lpar", read=False, hold_id=placed.hold_id))
    with calling(CallContext("hmc_power_lpar", read=False, hold_id=placed.hold_id)):
        holds.enforce_hold(LPAR.upper())


def test_enforce_refuses_on_an_unusable_store():
    with store.session():
        pass
    (store.state_dir() / store.DB_NAME).write_bytes(b"garbage" * 200)
    with calling(CallContext("hmc_lpar_summary", read=True, hold_id=None)):
        holds.enforce_hold(LPAR)
    assert _reason(_enforce, None) == "store_corrupt"


def test_calling_restores_the_outer_context():
    outer = CallContext("hmc_invoke_tool", read=False, hold_id=None)
    inner = CallContext("hmc_power_lpar", read=False, hold_id="x")
    assert current_call() is None
    with calling(outer):
        with calling(inner):
            assert current_call() == inner
        assert current_call() == outer
    assert current_call() is None


def test_a_schema_one_store_naming_another_sentinel_is_refused_unchanged():
    _schema_one_store()
    (store.state_dir() / store.SENTINEL_NAME).write_text("b" * 32)
    assert _reason(holds.find_hold, LPAR) == "store_corrupt"
    assert _schema_version() == "1"
