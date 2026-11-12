"""Recorded-original target authorization through the composed MCP wrapper (ADR 0334)."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.authorization.connection_scope import ConnectionScopeError
from hmcpctl.authorization.target_scope import TargetScopeError
from hmcpctl.config import HMCConfig
from hmcpctl.operations.logical import engine, store
from hmcpctl.operations.lpar.decommission.operation import (
    DEFAULT_ARGUMENTS,
    TOOL_NAME,
)
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import decommission as tool

SYSTEM = "original-system"
LPAR = "original-partition"
SYSTEM_UUID = "11111111-1111-1111-1111-111111111111"
LPAR_UUID = "22222222-2222-2222-2222-222222222222"
VIOS = "33333333-3333-3333-3333-333333333333"


def _grant(system=SYSTEM, lpar=LPAR, connection="<default>", *, storage=True):
    return {
        "tools": [TOOL_NAME, *(tool.DELEGATED if storage else ())],
        "connections": [connection],
        "targets": {
            "managed_system": [system],
            "lpar": [lpar],
            **({"vios": [VIOS]} if storage else {}),
        },
    }


def _application(grants):
    policy = compile_access_policy(
        {"policies": {"scoped": {"grants": grants}}},
        "scoped",
        TOOL_SECURITY,
        "test-policy",
    )
    return create_mcp(policy, catalog="registered")


def _callable(grants=None):
    application = _application(grants if grants is not None else [_grant()])
    return asyncio.run(application.get_tool(TOOL_NAME)).fn


def _record(
    *,
    cleanup="retain",
    system=SYSTEM,
    lpar=LPAR,
    name=TOOL_NAME,
    connection="<default>",
    host="hmc.test",
    partition=True,
):
    arguments = {
        **DEFAULT_ARGUMENTS,
        "system_name_or_uuid": system,
        "lpar_name_or_uuid": lpar,
        "storage_cleanup": cleanup,
    }
    request = engine.OperationRequest(
        name, "hmcpctl", connection, host, "request-1", arguments
    )
    request_json, digest = engine.canonical_request(request)
    with store.session() as conn, store.write_transaction(conn):
        store.insert_operation(
            conn,
            operation_id="a" * 32,
            agent_id="hmcpctl",
            request_id="request-1",
            tool=name,
            connection=connection,
            host=host,
            digest=digest,
            request_json=request_json,
        )
        store.set_state(conn, "a" * 32, "paused", "needs_attention")
        if partition:
            store.set_partition(conn, "a" * 32, SYSTEM_UUID, LPAR_UUID)


@pytest.fixture
def boundary(monkeypatch):
    config = HMCConfig.from_mapping({"host": "hmc.test", "user": "test-user"})
    monkeypatch.setattr(tool, "build_config", lambda **_: config)
    monkeypatch.setattr(
        "hmcpctl.authorization.dispatch_scope.selected_connection",
        lambda token, **_: token,
    )
    monkeypatch.setattr(store, "connection_label", lambda *_, **__: "<default>")
    preview = Mock(side_effect=AssertionError("unexpected HMC preview"))
    monkeypatch.setattr(tool, "with_client", preview)
    monkeypatch.setattr(tool, "owned_vios", lambda _: frozenset({VIOS}))
    executions = []

    async def body(_context):
        executions.append("body")
        return engine.BodyResult("completed")

    monkeypatch.setattr(tool, "decommission_body", lambda *_, **__: body)
    events = []
    monkeypatch.setattr(
        "hmcpctl.authorization.dispatch_scope.audit.record_authorization",
        lambda **event: events.append(event),
    )
    return events, executions, preview


@pytest.mark.parametrize("continuation", ["resume", "abandon"])
@pytest.mark.parametrize("cleanup", ["retain", "delete_owned"])
@pytest.mark.parametrize(
    "supplied",
    [
        {},
        {"system_name_or_uuid": SYSTEM},
        {"system_name_or_uuid": SYSTEM, "lpar_name_or_uuid": LPAR},
    ],
)
def test_covered_continuation(boundary, continuation, cleanup, supplied):
    _record(cleanup=cleanup)
    result = _callable()(
        request_id="request-1",
        continuation=continuation,
        exclusive_writer_window=True,
        **supplied,
    )
    assert result.operation.outcome == (
        "completed" if continuation == "resume" else "abandoned"
    )
    events, executions, preview = boundary
    own = [event for event in events if event["tool"] == TOOL_NAME]
    assert len(own) == 1 and own[0]["decision"] == "allow"
    assert [target.value for target in own[0]["targets"]] == [SYSTEM, LPAR]
    assert executions == (["body"] if continuation == "resume" else [])
    preview.assert_not_called()


@pytest.mark.parametrize("continuation", ["resume", "abandon"])
@pytest.mark.parametrize(
    "supplied", [{}, {"system_name_or_uuid": "allowed", "lpar_name_or_uuid": "allowed"}]
)
def test_recorded_target_denied(boundary, monkeypatch, continuation, supplied):
    _record()
    submit = Mock(side_effect=AssertionError("unauthorized submit"))
    monkeypatch.setattr(engine, "submit", submit)
    with pytest.raises(TargetScopeError) as error:
        _callable([_grant("allowed", "allowed")])(
            request_id="request-1", continuation=continuation, **supplied
        )
    assert SYSTEM not in str(error.value) and LPAR not in str(error.value)
    assert [e["decision"] for e in boundary[0]] == ["deny"]
    submit.assert_not_called()
    boundary[2].assert_not_called()


def test_denied_connection_does_not_read_record(boundary, monkeypatch):
    reader = Mock(side_effect=AssertionError("record disclosure"))
    monkeypatch.setattr(engine, "recorded_arguments", reader)
    with pytest.raises(ConnectionScopeError):
        _callable()(request_id="request-1", continuation="resume", profile="other")
    reader.assert_not_called()
    assert [e["decision"] for e in boundary[0]] == ["deny"]


def test_denied_tool_is_not_registered(boundary):
    grant = _grant()
    grant["tools"] = ["hmc_list_lpars"]
    grant["targets"] = "all-targets"
    assert asyncio.run(_application([grant]).get_tool(TOOL_NAME)) is None


def test_split_grants_deny(boundary):
    _record()
    with pytest.raises(TargetScopeError):
        _callable([_grant("other", "other"), _grant(connection="other")])(
            request_id="request-1", continuation="resume"
        )
    assert [e["decision"] for e in boundary[0]] == ["deny"]


@pytest.mark.parametrize("case", ["missing", "wrong-tool", "absent", "blank", "number"])
def test_unavailable_record_denied(boundary, case):
    if case != "missing":
        options = {"name": "hmc_provision_lpar"} if case == "wrong-tool" else {}
        if case in {"absent", "blank", "number"}:
            options["lpar"] = {"absent": None, "blank": " ", "number": 7}[case]
        _record(**options)
    grant = _grant()
    grant["targets"] = "all-targets"
    with pytest.raises(TargetScopeError):
        _callable([grant])(request_id="request-1", continuation="resume")
    assert [(e["decision"], e["reason"]) for e in boundary[0]] == [
        ("deny", "target-selector-unreadable")
    ]
    boundary[2].assert_not_called()


@pytest.mark.parametrize("continuation", ["resume", "abandon"])
def test_conflicting_selector_never_previews(boundary, continuation):
    _record(cleanup="delete_owned", partition=False)
    grant = _grant()
    grant["targets"] = "all-targets"
    with pytest.raises(store.OperationRefused) as error:
        _callable([grant])(
            request_id="request-1",
            continuation=continuation,
            lpar_name_or_uuid="replacement",
            exclusive_writer_window=True,
        )
    assert error.value.reason == "request_conflict"
    boundary[2].assert_not_called()


@pytest.mark.parametrize("grant_uuid", [False, True])
def test_name_and_uuid_are_not_interchangeable(boundary, grant_uuid):
    _record()
    grants = [_grant(SYSTEM_UUID, LPAR_UUID)] if grant_uuid else [_grant()]
    if grant_uuid:
        with pytest.raises(TargetScopeError):
            _callable(grants)(request_id="request-1", continuation="abandon")
    else:
        assert (
            _callable(grants)(
                request_id="request-1", continuation="abandon"
            ).operation.outcome
            == "abandoned"
        )


def test_uuid_original_selectors_are_preserved(boundary):
    _record(system=SYSTEM_UUID, lpar=LPAR_UUID)
    assert (
        _callable([_grant(SYSTEM_UUID, LPAR_UUID)])(
            request_id="request-1", continuation="abandon"
        ).operation.outcome
        == "abandoned"
    )


@pytest.mark.parametrize("binding", [{"host": "other.test"}, {"connection": "other"}])
def test_engine_binding_remains_enforced(boundary, binding):
    _record(**binding)
    with pytest.raises(store.OperationRefused) as error:
        _callable()(request_id="request-1", continuation="abandon")
    assert error.value.reason == "connection_mismatch"


def test_owned_storage_still_requires_specialists(boundary):
    _record(cleanup="delete_owned")
    with pytest.raises(PermissionError):
        _callable([_grant(storage=False)])(
            request_id="request-1", continuation="resume", exclusive_writer_window=True
        )
    assert not boundary[1]


def test_lookup_follows_connection_check_without_preliminary_allow(
    boundary, monkeypatch
):
    _record()
    original = engine.recorded_arguments
    observed = []

    def read(*args):
        observed.append([event["decision"] for event in boundary[0]])
        return original(*args)

    monkeypatch.setattr(engine, "recorded_arguments", read)
    _callable()(request_id="request-1", continuation="abandon")
    assert observed == [[], ["allow"]]


@pytest.mark.parametrize(
    "failure", [OSError("private-store-path"), ValueError("private-config")]
)
def test_record_read_error_is_safe_and_audited(boundary, monkeypatch, failure):
    monkeypatch.setattr(engine, "recorded_arguments", Mock(side_effect=failure))
    with pytest.raises(TargetScopeError) as error:
        _callable()(request_id="request-1", continuation="resume")
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__
    assert [(e["decision"], e["reason"], e["targets"]) for e in boundary[0]] == [
        ("deny", "target-selector-unreadable", None)
    ]
    boundary[2].assert_not_called()


def test_new_preview_keeps_raw_authorization_without_record_lookup(
    boundary, monkeypatch
):
    reader = Mock(side_effect=AssertionError("new preview read a record"))
    monkeypatch.setattr(engine, "recorded_arguments", reader)
    boundary[2].side_effect = RuntimeError("preview reached")
    with pytest.raises(RuntimeError, match="preview reached"):
        _callable()(
            request_id="new-request",
            system_name_or_uuid=SYSTEM,
            lpar_name_or_uuid=LPAR,
            dry_run=True,
        )
    reader.assert_not_called()
    assert [event["decision"] for event in boundary[0]] == ["allow"]
