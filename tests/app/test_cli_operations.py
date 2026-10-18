"""Local cross-agent operator commands, governed by ADR 0190 (#1286)."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.client.core import HMCClient
from hmcpctl.operations.logical import store

RUNNER = CliRunner()
OID = "1" * 32


@pytest.fixture(autouse=True)
def no_hmc(monkeypatch):
    connect = Mock(side_effect=AssertionError("operator commands must stay local"))
    monkeypatch.setattr(HMCClient, "__init__", connect)
    yield
    connect.assert_not_called()


def seed(*, state="paused", outcome="needs_attention", operation_id=OID, agent="other"):
    with store.session() as conn, store.write_transaction(conn):
        store.insert_operation(
            conn,
            operation_id=operation_id,
            agent_id=agent,
            request_id="same-request",
            tool="hmc_provision_lpar",
            connection="other-profile",
            host="hmc.example.test",
            digest="d" * 64,
            request_json='{"arguments":{}}',
        )
        store.set_state(conn, operation_id, state, outcome)


def invoke(*words):
    result = RUNNER.invoke(app, ["operations", *words])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_list_across_agents_connections_and_bound_events(monkeypatch):
    monkeypatch.setattr(store, "_clock", lambda: 1000.0)
    for n in range(1, 52):
        seed(operation_id=f"{n:032x}", agent=f"agent-{n}")
    with store.session() as conn, store.write_transaction(conn):
        for n in range(205):
            store.add_event(conn, f"{51:032x}", "phase", {"n": n})
    page = invoke("list")
    assert len(page["operations"]) == 50
    assert page["truncated"] is True
    first = page["operations"][0]
    assert first["operation_id"] == f"{51:032x}"
    assert len(first["events"]) == 200
    assert first["events"][0]["detail"] == {"n": 5}
    assert first["events_truncated"] is True
    following = invoke("list", "--cursor", page["next_cursor"])
    assert [r["operation_id"] for r in following["operations"]] == [f"{1:032x}"]
    assert following["next_cursor"] is None
    assert (
        store.operation_status(
            agent_id="caller", connection="<default>", host="hmc.example.test"
        ).operations
        == ()
    )


@pytest.mark.parametrize(
    "state,outcome",
    [("interrupted", None), ("paused", "needs_attention"), ("paused", "ready_to_boot")],
)
def test_abandon_releases_guard_and_preserves_ledger(state, outcome):
    seed(state=state, outcome=outcome)
    with store.session() as conn, store.write_transaction(conn):
        conn.execute("UPDATE operations SET system_uuid='sys', partition_uuid='part'")
        store.add_ledger(
            conn,
            OID,
            key="disk",
            kind="disk",
            system_uuid="sys",
            partition_uuid="part",
            identity={"name": "owned"},
        )
        store.record_intent(conn, OID, "create", "disk", "disk")
    record = invoke("abandon", OID)
    assert (record["state"], record["outcome"]) == ("terminal", "abandoned")
    assert record["effects"][0]["status"] == "intended"
    with store.session() as conn:
        assert conn.execute("SELECT count(*) FROM ledger").fetchone()[0] == 1
        with store.write_transaction(conn):
            store.insert_operation(
                conn,
                operation_id="2" * 32,
                agent_id="new",
                request_id="new",
                tool="hmc_provision_lpar",
                connection="other-profile",
                host="hmc.example.test",
                digest="d" * 64,
                request_json='{"arguments":{}}',
            )
            conn.execute(
                "UPDATE operations SET system_uuid='sys', partition_uuid='part' "
                "WHERE operation_id=?",
                ("2" * 32,),
            )


def test_running_is_refused_even_without_execution_lock():
    seed(state="running", outcome=None)
    result = RUNNER.invoke(app, ["operations", "abandon", OID])
    assert result.exit_code == 1
    assert "running" in result.stderr
    assert invoke("list")["operations"][0]["state"] == "running"


def test_terminal_is_returned_without_change():
    seed(state="terminal", outcome="failed")
    before = invoke("list")["operations"][0]
    assert invoke("abandon", OID) == before


def test_invalid_paused_continuation_refuses():
    seed(state="paused", outcome="completed")
    result = RUNNER.invoke(app, ["operations", "abandon", OID])
    assert result.exit_code == 1
    assert "continuation_not_accepted" in result.stderr


@pytest.mark.parametrize(
    "args,reason",
    [
        (["list", "--limit", "0"], "Invalid value"),
        (["list", "--limit", "51"], "Invalid value"),
        (["list", "--cursor", "bad"], "invalid_cursor"),
        (["abandon", "bad"], "invalid_operation_id"),
        (["abandon", OID], "not_found"),
    ],
)
def test_bad_inputs_and_missing_records(args, reason):
    result = RUNNER.invoke(app, ["operations", *args])
    assert result.exit_code != 0
    assert reason in result.stderr
    assert not store.state_dir().exists()


def test_empty_list_does_not_create_store():
    assert invoke("list") == {
        "operations": [],
        "limit": 50,
        "truncated": False,
        "next_cursor": None,
    }
    assert not store.state_dir().exists()


@pytest.mark.parametrize("command", [["list"], ["abandon", OID]])
@pytest.mark.parametrize(
    "failure,reason",
    [
        ("corrupt", "store_corrupt"),
        ("private", "store_not_private"),
        ("lost", "store_lost"),
        ("unreadable", "store_unreadable"),
    ],
)
def test_commands_refuse_damaged_store(monkeypatch, command, failure, reason):
    seed()
    db = store.state_dir() / store.DB_NAME
    if failure == "corrupt":
        db.write_bytes(b"not SQLite")
    elif failure == "private":
        db.chmod(0o640)
    elif failure == "lost":
        db.unlink()
    else:

        def unreadable(*args, **kwargs):
            raise sqlite3.OperationalError("permission denied")

        monkeypatch.setattr(store, "_connect", unreadable)
    result = RUNNER.invoke(app, ["operations", *command])
    assert result.exit_code == 1
    assert reason in result.stderr
    if failure == "lost":
        assert not db.exists()


def test_local_help_describes_operator_boundary():
    for args in (
        ["--help"],
        ["operations", "--help"],
        ["operations", "list", "--help"],
        ["operations", "abandon", "--help"],
    ):
        result = RUNNER.invoke(app, args)
        assert result.exit_code == 0, result.output
    assert "HMC" in result.output


def test_abandon_rolls_back_when_store_fails_during_transition(monkeypatch):
    seed()
    read_record = store.read_record
    reads = 0

    def fail_second_read(conn, operation_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise sqlite3.OperationalError("read failed")
        return read_record(conn, operation_id)

    monkeypatch.setattr(store, "read_record", fail_second_read)
    result = RUNNER.invoke(app, ["operations", "abandon", OID])
    assert result.exit_code == 1
    assert "store_unreadable" in result.stderr
    with store.read_session() as conn:
        row = store.get_operation(conn, OID)
        assert (row["state"], row["outcome"]) == ("paused", "needs_attention")


def test_commands_do_not_acquire_execution_lock(monkeypatch):
    seed()
    acquire = Mock(
        side_effect=AssertionError("operator commands must not interrupt running work")
    )
    monkeypatch.setattr(store, "acquire_execution_lock", acquire)
    invoke("list", "--limit", "1")
    invoke("abandon", OID)
    acquire.assert_not_called()
