"""Tests for multi-agent LPAR ownership helpers (issue #132)."""

from __future__ import annotations

import asyncio
import datetime
import inspect
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hmcpctl.audit import records as audit
from hmcpctl.audit import sink as audit_sink
from hmcpctl.config import validate_agent_id
from hmcpctl.operations.lpar import core as operations_lpar
from hmcpctl.operations.lpar import ownership as lpar_ownership
from hmcpctl.operations.lpar.ownership import (
    authorize_decommission_lpar_ownership_snapshot,
    authorize_lpar_mutation,
    parse_lpar_ownership_caller_token,
    resolve_and_authorize_lpar_names,
)

# ---------------------------------------------------------------------------
# validate_agent_id
# ---------------------------------------------------------------------------


def test_validate_agent_id_valid():
    validate_agent_id("alice")  # plain name
    validate_agent_id("agent-1")  # hyphens ok
    validate_agent_id("agent.1")  # dots ok
    validate_agent_id("a" * 64)  # max length


def test_validate_agent_id_reserved():
    with pytest.raises(ValueError, match="reserved"):
        validate_agent_id("hmcpctl")


def test_validate_agent_id_empty():
    with pytest.raises(ValueError, match="empty"):
        validate_agent_id("")


def test_validate_agent_id_too_long():
    with pytest.raises(ValueError, match="64"):
        validate_agent_id("a" * 65)


def test_validate_agent_id_comma():
    with pytest.raises(ValueError, match="comma"):
        validate_agent_id("alice,eve")


def test_validate_agent_id_equals():
    with pytest.raises(ValueError, match="="):
        validate_agent_id("key=val")


def test_validate_agent_id_bracket():
    with pytest.raises(ValueError, match="bracket"):
        validate_agent_id("alice[1]")


def test_validate_agent_id_slash():
    with pytest.raises(ValueError, match="HMC REST API"):
        validate_agent_id("team/agent")


def test_validate_agent_id_colon():
    with pytest.raises(ValueError, match="colon"):
        validate_agent_id("team:agent")


def test_validate_agent_id_space():
    with pytest.raises(ValueError, match="space"):
        validate_agent_id("alice smith")


def test_validate_agent_id_double_quote():
    with pytest.raises(ValueError, match="double quote"):
        validate_agent_id('agent"x')


def test_validate_agent_id_backslash():
    with pytest.raises(ValueError, match="backslash"):
        validate_agent_id("agent\\x")


def test_validate_agent_id_non_ascii():
    with pytest.raises(ValueError, match="printable ASCII"):
        validate_agent_id("alicé")


def test_validate_agent_id_control_char():
    with pytest.raises(ValueError, match="printable ASCII"):
        validate_agent_id("alice\n")


# ---------------------------------------------------------------------------
# stamp_lpar_ownership
# ---------------------------------------------------------------------------


from hmcpctl.config import HMCConfig  # noqa: E402
from hmcpctl.ssh.lpar import stamp_lpar_ownership  # noqa: E402 (after validate tests)


def _config():
    return HMCConfig.from_mapping({"host": "hmc.test", "user": "u", "password": "p"})


@pytest.fixture(autouse=True)
def fixed_ownership_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep local-date ownership assertions deterministic across midnight."""

    class FixedDate(datetime.date):
        @classmethod
        def today(cls) -> FixedDate:
            return cls(2026, 1, 15)

    monkeypatch.setattr(datetime, "date", FixedDate)


def test_stamp_returns_token_on_success():
    config = _config()
    with patch(
        "hmcpctl.ssh.lpar.set_lpar_description", new=AsyncMock(return_value="")
    ) as mock_set:
        token = asyncio.run(
            stamp_lpar_ownership(config, "sys1", "lpar1", agent_id="alice")
        )
    today = datetime.date.today().isoformat()  # noqa: DTZ011 - mirrors the ownership stamp's own local-date basis in ssh/lpar.py; changing one side alone makes them disagree for part of every day
    assert token == f"[hmcpctl owner:alice created:{today}]"
    mock_set.assert_awaited_once()
    # verify set_lpar_description was called with the token as the description arg
    call_args = mock_set.call_args.args
    assert call_args[3] == token


def test_stamp_default_agent_id():
    config = _config()
    with patch("hmcpctl.ssh.lpar.set_lpar_description", new=AsyncMock(return_value="")):
        token = asyncio.run(
            stamp_lpar_ownership(config, "sys1", "lpar1")  # no agent_id
        )
    assert token is not None
    assert "owner:hmcpctl" in token


def test_stamp_returns_none_on_ssh_error():
    config = _config()
    from hmcpctl.ssh.transport import HMCCLIError

    with patch(
        "hmcpctl.ssh.lpar.set_lpar_description",
        new=AsyncMock(side_effect=HMCCLIError("SSH failed")),
    ):
        token = asyncio.run(
            stamp_lpar_ownership(config, "sys1", "lpar1", agent_id="alice")
        )
    assert token is None  # swallowed — best-effort


def test_token_format():
    config = _config()
    today = datetime.date.today().isoformat()  # noqa: DTZ011 - mirrors the ownership stamp's own local-date basis in ssh/lpar.py; changing one side alone makes them disagree for part of every day
    with patch("hmcpctl.ssh.lpar.set_lpar_description", new=AsyncMock(return_value="")):
        token = asyncio.run(
            stamp_lpar_ownership(config, "sys1", "lpar1", agent_id="my-agent")
        )
    assert token == f"[hmcpctl owner:my-agent created:{today}]"
    # token must pass the existing description validator
    from hmcpctl.ssh.lpar import validate_lpar_description

    validate_lpar_description(token)  # no exception


@pytest.mark.parametrize("ownership_override", [False, True])
def test_resolve_and_authorize_lpar_names_forwards_resolution_and_override(
    ownership_override,
):
    hmc = AsyncMock()
    resolve_system = AsyncMock(return_value="system-uuid")
    resolve_lpar = AsyncMock(return_value="lpar-uuid")
    resolve_names = AsyncMock(return_value=("system-name", "lpar-name"))
    authorize = AsyncMock()
    with (
        patch.object(lpar_ownership, "resolve_system_uuid", resolve_system),
        patch.object(lpar_ownership, "resolve_lpar_uuid", resolve_lpar),
        patch.object(lpar_ownership, "resolve_lpar_ownership_names", resolve_names),
        patch.object(lpar_ownership, "authorize_lpar_mutation", authorize),
    ):
        result = asyncio.run(
            resolve_and_authorize_lpar_names(
                hmc,
                "system-selector",
                "lpar-selector",
                ownership_override=ownership_override,
            )
        )

    assert result == ("system-name", "lpar-name")
    resolve_system.assert_awaited_once_with(hmc, "system-selector")
    resolve_lpar.assert_awaited_once_with(
        hmc, "lpar-selector", system_name_or_uuid="system-uuid"
    )
    resolve_names.assert_awaited_once_with(
        hmc, "system-uuid", "system-selector", "lpar-uuid"
    )
    authorize.assert_awaited_once_with(
        hmc,
        "system-name",
        "lpar-name",
        ownership_override=ownership_override,
    )


@pytest.mark.parametrize("ownership_override", [False, True])
def test_required_system_rejects_partition_uuid_from_another_system(ownership_override):
    hmc = AsyncMock()
    # The foreign system has a same-named partition: name resolution alone
    # must not make the asserted system appear to contain the UUID.
    hmc.list_logical_partitions.return_value = [
        {"UUID": "different-lpar-uuid", "Resource": {"PartitionName": "aix1"}}
    ]
    resolve_system = AsyncMock(return_value="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    resolve_names = AsyncMock()
    authorize = AsyncMock()
    with (
        patch.object(lpar_ownership, "resolve_system_uuid", resolve_system),
        patch.object(lpar_ownership, "resolve_lpar_ownership_names", resolve_names),
        patch.object(lpar_ownership, "authorize_lpar_mutation", authorize),
        pytest.raises(ValueError, match="does not belong to managed system"),
    ):
        asyncio.run(
            resolve_and_authorize_lpar_names(
                hmc,
                "selected-system",
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                ownership_override=ownership_override,
            )
        )

    hmc.list_logical_partitions.assert_awaited_once_with(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )
    resolve_names.assert_not_awaited()
    authorize.assert_not_awaited()


# Issue #1026: _partition_name and resolve_lpar_ownership_names read
# PartitionName through resource_identity.lpar_name_from_uuid, which rejects a
# non-string or whitespace-only name instead of coercing it with str().


def test_partition_name_rejects_whitespace_only_name():
    hmc = AsyncMock()
    hmc.get_logical_partition.return_value = {"Resource": {"PartitionName": "   "}}
    with pytest.raises(ValueError, match="No LPAR 'lpar-selector' found"):
        asyncio.run(lpar_ownership._partition_name(hmc, "lpar-uuid", "lpar-selector"))


def test_resolve_lpar_ownership_names_rejects_whitespace_only_partition_name():
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": "sys-uuid", "SystemName": "sys1"}
    ]
    hmc.get_logical_partition.return_value = {"Resource": {"PartitionName": "  "}}
    with pytest.raises(ValueError, match="LPAR 'lpar-uuid' has no partition name"):
        asyncio.run(
            lpar_ownership.resolve_lpar_ownership_names(
                hmc, "sys-uuid", "sys1", "lpar-uuid"
            )
        )


@pytest.mark.parametrize("ownership_override", [False, True])
def test_optional_system_rejects_partition_uuid_from_another_system(ownership_override):
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {
            "UUID": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "SystemName": "selected-system",
        }
    ]
    hmc.list_logical_partitions.return_value = [
        {"UUID": "different-lpar-uuid", "Resource": {"PartitionName": "aix1"}}
    ]
    resolve_system = AsyncMock(return_value="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    authorize = AsyncMock()
    with (
        patch.object(lpar_ownership, "resolve_system_uuid", resolve_system),
        patch.object(lpar_ownership, "authorize_lpar_mutation", authorize),
        pytest.raises(ValueError, match="does not belong to managed system"),
    ):
        asyncio.run(
            lpar_ownership.resolve_and_authorize_lpar_mutation(
                hmc,
                "selected-system",
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                ownership_override=ownership_override,
            )
        )

    hmc.list_logical_partitions.assert_awaited_once_with(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )
    authorize.assert_not_awaited()


@pytest.mark.parametrize(
    ("description", "agent_id", "allowed"),
    [
        ("legacy partition", "alice", True),
        ("[hmc-mcp owner:alice created:2026-08-14]", "alice", True),
        ("[hmcpctl owner:alice created:2026-08-14]", "alice", True),
        ("[hmcpctl owner:bob created:2026-08-14]", "alice", False),
        ("[hmcpctl owner:broken]", "alice", False),
    ],
)
def test_authorize_lpar_mutation(description, agent_id, allowed):
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": agent_id})}
    )()
    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=AsyncMock(return_value=description),
    ):
        if allowed:
            asyncio.run(authorize_lpar_mutation(hmc, "sys1", "lpar1"))
        else:
            with pytest.raises(PermissionError, match="ownership_override=true"):
                asyncio.run(authorize_lpar_mutation(hmc, "sys1", "lpar1"))


# Spec test -> node id (docs/workflow/specs/2026-08-19-authorization-audit-events-design.md)
#   26a test_authorize_lpar_mutation_override_is_audited
#   26b test_the_override_record_is_bounded_and_escaped
#   26c test_operations_lpar_does_not_resolve_the_audit_logger
#   26d test_the_override_still_reaches_stderr_without_a_sink
#   26e test_both_override_call_sites_emit_and_normal_access_does_not


def _override_records(caplog):
    """Parsed audit records captured at WARNING on the audit logger."""
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == audit_sink.AUDIT_LOGGER_NAME
    ]


def test_authorize_lpar_mutation_override_is_audited(caplog):
    """Spec 26a. Replaces the pre-convergence test that read `extra=` attributes
    off `hmcpctl.operations.lpar` — exactly what convergence removes."""
    hmc = type("StubHMC", (), {"config": _config()})()
    read = AsyncMock()
    with (
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        caplog.at_level(logging.WARNING),
    ):
        asyncio.run(
            authorize_lpar_mutation(hmc, "sys1", "lpar1", ownership_override=True)
        )
    read.assert_not_awaited()

    records = _override_records(caplog)
    assert len(records) == 1, (
        "an absence assertion over an empty capture proves nothing"
    )
    assert records[0] == {
        "time": records[0]["time"],
        "event": "ownership-override",
        "system": "sys1",
        "lpar": "lpar1",
        "host": "hmc.test",
        "attribution": {
            "claim": "hmcpctl",
            "source": "config:agent_id",
            "verified": False,
        },
    }
    assert [r for r in caplog.records if r.name == "hmcpctl.operations.lpar"] == []


def test_the_override_record_host_comes_from_the_client_config(caplog):
    """#271. `host` is the emitter's own ``HMCConfig.host``, and an unset one —
    the config default — renders as the empty string that it is."""
    hmc = type(
        "StubHMC",
        (),
        {"config": _config().model_copy(update={"host": "", "agent_id": None})},
    )()
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description", new=AsyncMock()
        ),
        caplog.at_level(logging.WARNING),
    ):
        asyncio.run(
            authorize_lpar_mutation(hmc, "sys1", "lpar1", ownership_override=True)
        )
    records = _override_records(caplog)
    assert len(records) == 1, (
        "an absence assertion over an empty capture proves nothing"
    )
    assert records[0]["host"] == ""


def test_the_override_record_is_bounded_and_escaped(caplog):
    """Spec 26b. The same bound and the same escaping as the other record."""
    hmc = type("StubHMC", (), {"config": _config()})()
    hostile = "A" * 500
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description", new=AsyncMock()
        ),
        caplog.at_level(logging.WARNING),
    ):
        asyncio.run(
            authorize_lpar_mutation(hmc, hostile, "x\ny‮z", ownership_override=True)
        )
    raw = [
        r.getMessage() for r in caplog.records if r.name == audit_sink.AUDIT_LOGGER_NAME
    ]
    assert len(raw) == 1
    assert raw[0].isascii() and "\n" not in raw[0]
    record = json.loads(raw[0])
    assert len(record["system"]) == audit.MAX_VALUE_LENGTH


def test_operations_lpar_does_not_resolve_the_audit_logger():
    """Spec 26c. `audit` is the only module that names the reserved logger."""
    source = Path(operations_lpar.__file__).read_text()
    assert audit_sink.AUDIT_LOGGER_NAME not in source


def test_the_override_still_reaches_stderr_without_a_sink(caplog, capsys):
    """Spec 26d. A CLI user, whose process never called `install_audit_sink`.

    The mechanism is `logging.lastResort`, which `callHandlers` consults only
    after an ancestor walk finds zero handlers — and pytest keeps one on the
    root, so the root handlers must be cleared for this to mean anything.
    Written naively it passes for an unrelated reason.
    """
    hmc = type("StubHMC", (), {"config": _config()})()
    saved = list(logging.root.handlers)
    logging.root.handlers.clear()
    try:
        with patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description", new=AsyncMock()
        ):
            asyncio.run(
                authorize_lpar_mutation(hmc, "sys1", "lpar1", ownership_override=True)
            )
        captured = capsys.readouterr()
    finally:
        logging.root.handlers[:] = saved
    assert captured.out == ""
    assert json.loads(captured.err.strip())["event"] == "ownership-override"


def test_both_override_call_sites_emit_and_normal_access_does_not(caplog):
    """Spec 26e."""
    hmc = type("StubHMC", (), {"config": _config()})()
    # The decommission path reads a description before authorizing it, so the
    # stub must return a real one; the mutation path never awaits it at all.
    read = AsyncMock(return_value="legacy partition")
    with (
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        caplog.at_level(logging.WARNING),
    ):
        asyncio.run(
            authorize_lpar_mutation(hmc, "sys1", "lpar1", ownership_override=True)
        )
        asyncio.run(
            authorize_decommission_lpar_ownership_snapshot(
                hmc, "sys2", "lpar2", ownership_override=True
            )
        )
    assert [record["system"] for record in _override_records(caplog)] == [
        "sys1",
        "sys2",
    ]


def test_authorize_lpar_mutation_normal_access_has_no_override_audit(caplog):
    hmc = type("StubHMC", (), {"config": _config()})()
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value="legacy partition"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        asyncio.run(authorize_lpar_mutation(hmc, "sys1", "lpar1"))

    assert _override_records(caplog) == []


# #467 / ADR 0100. The denial half of the same guard: one record per refused
# ADR 0011 check, emitted from `_authorize_lpar_ownership_description` before the
# `PermissionError`, on every guarded operation.


def _denied(hmc, description, run, caplog):
    """Run *run* against a stubbed description, expect a refusal, return the records."""
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=description),
        ),
        caplog.at_level(logging.WARNING),
        pytest.raises(PermissionError, match="ownership_override=true"),
    ):
        asyncio.run(run(hmc))
    return _override_records(caplog)


def test_a_foreign_owner_denial_records_both_halves_of_the_comparison(caplog):
    """The claimed owner and the acting agent, on the branch that compared them."""
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    records = _denied(
        hmc,
        "[hmcpctl owner:bob created:2026-08-14]",
        lambda h: authorize_lpar_mutation(h, "sys1", "lpar1"),
        caplog,
    )

    assert len(records) == 1, (
        "an absence assertion over an empty capture proves nothing"
    )
    assert records[0] == {
        "time": records[0]["time"],
        "event": "ownership-denied",
        "operation": "lpar-mutation",
        "denial": "foreign-owner",
        "system": "sys1",
        "lpar": "lpar1",
        "owner": "bob",
        "host": "hmc.test",
        "attribution": {
            "claim": "alice",
            "source": "config:agent_id",
            "verified": False,
        },
    }
    assert [r for r in caplog.records if r.name == "hmcpctl.operations.lpar"] == []


def test_a_malformed_token_denial_is_recorded_as_its_own_branch(caplog):
    """The other denial branch, distinguishable from the first by `denial` alone.

    `owner` is `null` here because nothing parsed — the branch refuses before any
    comparison is reached, so the record carries the actor and no counterparty.
    """
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    records = _denied(
        hmc,
        "[hmcpctl owner:broken]",
        lambda h: authorize_lpar_mutation(h, "sys1", "lpar1"),
        caplog,
    )

    assert len(records) == 1, (
        "an absence assertion over an empty capture proves nothing"
    )
    assert records[0]["denial"] == "malformed-token"
    assert records[0]["owner"] is None
    assert records[0]["attribution"]["claim"] == "alice"


def test_an_unconfigured_agent_is_recorded_under_the_literal_the_guard_compared(caplog):
    """`HMCConfig.agent_id` defaults to `None` and the guard compares `hmcpctl`.

    Recording the bare field would leave an unconfigured deployment's denial and
    override records naming different actors and therefore unjoinable.
    """
    hmc = type("StubHMC", (), {"config": _config()})()
    records = _denied(
        hmc,
        "[hmcpctl owner:bob created:2026-08-14]",
        lambda h: authorize_lpar_mutation(h, "sys1", "lpar1"),
        caplog,
    )

    assert len(records) == 1
    assert records[0]["attribution"]["claim"] == "hmcpctl"


def test_the_decommission_entry_point_records_its_own_operation(caplog):
    """`operation` names which guard entry point refused, not which tool called it."""
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    records = _denied(
        hmc,
        "[hmcpctl owner:bob created:2026-08-14]",
        lambda h: authorize_decommission_lpar_ownership_snapshot(
            h, "sys2", "lpar2", ownership_override=False
        ),
        caplog,
    )

    assert len(records) == 1
    assert records[0]["operation"] == "lpar-decommission-snapshot"
    assert records[0]["system"] == "sys2"


@pytest.mark.parametrize(
    "authorize",
    [
        lambda h: authorize_lpar_mutation(h, "sys1", "lpar1"),
        lambda h: authorize_decommission_lpar_ownership_snapshot(
            h, "sys1", "lpar1", ownership_override=False
        ),
    ],
)
def test_ssh_ownership_read_failure_names_guard_and_remedy(authorize):
    """An unavailable ownership precheck tells operators how to proceed."""
    hmc = type("StubHMC", (), {"config": _config()})()
    cause = HMCCLIError("SSH command failed: connection refused")
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(side_effect=cause),
        ),
        pytest.raises(HMCCLIError) as exc_info,
    ):
        asyncio.run(authorize(hmc))

    assert "ADR 0011 ownership precheck" in str(exc_info.value)
    assert "LPAR 'lpar1'" in str(exc_info.value)
    assert "managed system 'sys1'" in str(exc_info.value)
    assert "ownership_override=true" in str(exc_info.value)
    assert "connection refused" in str(exc_info.value)
    assert exc_info.value.__cause__ is cause


def test_ownership_override_skips_ssh_read():
    hmc = type("StubHMC", (), {"config": _config()})()
    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description", new=AsyncMock()
    ) as read:
        asyncio.run(
            authorize_lpar_mutation(hmc, "sys1", "lpar1", ownership_override=True)
        )
    read.assert_not_awaited()


def test_a_new_guard_entry_point_cannot_omit_the_operation():
    """ADR 0100 §4. `operation` is required, so forgetting it is a type error.

    Defaulted, a third entry point that forgot the argument would file its
    refusals under an existing operation's name — and a stream that asserts
    something false is worse than one that is silent. Checked on the signature
    rather than by calling, because the point is what a *checker* sees.
    """
    parameter = inspect.signature(
        lpar_ownership.authorize_lpar_ownership_description
    ).parameters["operation"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_a_permitted_mutation_emits_no_denial_record(caplog):
    """The control: the record must mark refusals, not every guarded call."""
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value="[hmcpctl owner:alice created:2026-08-14]"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        asyncio.run(authorize_lpar_mutation(hmc, "sys1", "lpar1"))

    assert _override_records(caplog) == []


def test_an_override_emits_the_override_record_and_no_denial(caplog):
    """The two ownership events stay disjoint: a bypass is not a refusal.

    This is what the rejected `decision` arm would have blurred — an
    `event == "ownership-override"` filter must keep counting approved bypasses
    and nothing else.
    """
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value="[hmcpctl owner:bob created:2026-08-14]"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        asyncio.run(
            authorize_lpar_mutation(hmc, "sys1", "lpar1", ownership_override=True)
        )

    assert [record["event"] for record in _override_records(caplog)] == [
        "ownership-override"
    ]


def test_the_denial_still_reaches_stderr_without_a_sink(capsys):
    """ADR 0100 §3, and the same mechanism `test_the_override_still_reaches_stderr…`
    pins: `logging.lastResort` is consulted only when the ancestor walk finds zero
    handlers, and it drops anything below `WARNING`. So this is what asserts the
    level — a denial recorded at `INFO` would leave stderr empty here.
    """
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    saved = list(logging.root.handlers)
    logging.root.handlers.clear()
    try:
        with (
            patch(
                "hmcpctl.operations.lpar.ownership.get_lpar_description",
                new=AsyncMock(return_value="[hmcpctl owner:bob created:2026-08-14]"),
            ),
            pytest.raises(PermissionError),
        ):
            asyncio.run(authorize_lpar_mutation(hmc, "sys1", "lpar1"))
        captured = capsys.readouterr()
    finally:
        logging.root.handlers[:] = saved

    assert captured.out == ""
    assert json.loads(captured.err.strip())["event"] == "ownership-denied"


def test_the_denial_record_is_bounded_and_escaped(caplog):
    """The same bound and the same escaping as every other record on this stream.

    `owner` is HMC-supplied text parsed out of an operator-authored description,
    so it is the field this record adds that most needs both.
    """
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    hostile = "[hmcpctl owner:" + "B" * 500 + " created:2026-08-14]"
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=hostile),
        ),
        caplog.at_level(logging.WARNING),
        pytest.raises(PermissionError),
    ):
        asyncio.run(authorize_lpar_mutation(hmc, "A" * 500, "x\ny‮z"))

    raw = [
        r.getMessage() for r in caplog.records if r.name == audit_sink.AUDIT_LOGGER_NAME
    ]
    assert len(raw) == 1
    assert raw[0].isascii() and "\n" not in raw[0]
    record = json.loads(raw[0])
    assert len(record["system"]) == audit.MAX_VALUE_LENGTH
    assert len(record["owner"]) == audit.MAX_VALUE_LENGTH


from hmcpctl.ssh.lpar import validate_caller_token  # noqa: E402


def test_validate_caller_token_accepts_tracker_ids():
    validate_caller_token("CHG12345")  # ticket key
    validate_caller_token("2026/08/batch-7")  # slashes, digits
    validate_caller_token("owner@team:42")  # colon round-trips (spec guarantee 6)
    validate_caller_token("a" * 64)  # length boundary


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty string is a violation, not an omission
        "a" * 65,  # too long
        "a,b",  # comma: -i record delimiter
        "a=b",  # equals: -i record delimiter
        'a"b',  # double quote: -i record escape
        "a[b",  # bracket: breaks the [caller ...] framing
        "a]b",
        "a\\b",  # backslash: unverified -i behaviour (ADR 0045)
        "a b",  # whitespace
        "alicé",  # non-ASCII
        "a\nb",  # control character
    ],
)
def test_validate_caller_token_rejects(bad):
    with pytest.raises(ValueError, match="caller_token"):
        validate_caller_token(bad)


def test_validate_caller_token_rejects_non_string():
    with pytest.raises(ValueError, match="string"):
        validate_caller_token(42)  # type: ignore[arg-type]


def test_stamp_composes_caller_segment():
    config = _config()
    today = datetime.date.today().isoformat()  # noqa: DTZ011 - mirrors the ownership stamp's own local-date basis in ssh/lpar.py; changing one side alone makes them disagree for part of every day
    with patch(
        "hmcpctl.ssh.lpar.set_lpar_description", new=AsyncMock(return_value="")
    ) as mock_set:
        token = asyncio.run(
            stamp_lpar_ownership(
                config, "sys1", "lpar1", agent_id="alice", caller_token="CHG-1"
            )
        )
    assert token == f"[hmcpctl owner:alice created:{today}] [caller CHG-1]"
    assert mock_set.call_args.args[3] == token
    # still a valid HMC description
    from hmcpctl.ssh.lpar import validate_lpar_description

    validate_lpar_description(token)


def test_stamp_without_caller_token_unchanged():
    config = _config()
    today = datetime.date.today().isoformat()  # noqa: DTZ011 - mirrors the ownership stamp's own local-date basis in ssh/lpar.py; changing one side alone makes them disagree for part of every day
    with patch("hmcpctl.ssh.lpar.set_lpar_description", new=AsyncMock(return_value="")):
        token = asyncio.run(stamp_lpar_ownership(config, "sys1", "lpar1"))
    assert token == f"[hmcpctl owner:hmcpctl created:{today}]"


@pytest.mark.parametrize("character", ['"', "\\"])
def test_agent_id_breaking_stamp_grammar_rejected_at_construction(character):
    """An agent_id that would break the stamp's grammar never configures.

    validate_agent_id forbids '"' and '\\' (ADR 0065), mirroring
    validate_caller_token: HMCConfig construction raises ValueError instead
    of letting every ADR 0011 ownership stamp silently degrade to None in
    stamp_lpar_ownership's best-effort catch.
    """
    agent_id = f"agent{character}x"
    with pytest.raises(ValueError):
        validate_agent_id(agent_id)
    with pytest.raises(ValueError):
        HMCConfig(
            host="hmc.test",
            user="u",
            password="p",
            agent_id=agent_id,
        )


def test_stamp_bad_caller_token_raises_unswallowed():
    config = _config()
    with (
        patch(
            "hmcpctl.ssh.lpar.set_lpar_description", new=AsyncMock(return_value="")
        ) as mock_set,
        pytest.raises(ValueError, match="caller_token"),
    ):
        asyncio.run(stamp_lpar_ownership(config, "sys1", "lpar1", caller_token=""))
    mock_set.assert_not_awaited()  # rejected before any SSH traffic


def test_parse_caller_token_round_trip():
    description = "[hmcpctl owner:alice created:2026-08-21] [caller JIRA-1:x/y]"
    assert parse_lpar_ownership_caller_token(description) == "JIRA-1:x/y"


def test_parse_caller_token_absent():
    assert (
        parse_lpar_ownership_caller_token("[hmcpctl owner:a created:2026-08-21]")
        is None
    )
    assert (
        parse_lpar_ownership_caller_token(
            "[hmc-mcp owner:a created:2026-08-21] [caller OLD-1]"
        )
        is None
    )
    assert parse_lpar_ownership_caller_token("plain legacy description") is None


@pytest.mark.parametrize(
    "description",
    [
        "[caller JIRA-1] [hmcpctl owner:a created:2026-08-21]",  # misordered
        "[hmcpctl owner:a created:2026-08-21] [caller X] [caller Y]",  # duplicated
        "[hmcpctl owner:a created:2026-08-21][caller X]",  # missing space
        "[hmcpctl owner:a created:2026-08-21] [caller ]",  # empty segment
        "[hmcpctl owner:bogus created:x] [caller X]",  # malformed anchor
        "[hmcpctl owner:a created:2026-08-21] [caller]",  # bare bracket, no space
        "[hmcpctl owner:a created:2026-08-21] [Caller X]",  # lowercased prefix mismatch
    ],
)
def test_parse_caller_token_spoofed_yields_none(description):
    assert parse_lpar_ownership_caller_token(description) is None


def test_owner_parse_unaffected_by_caller_segment():
    """ADR 0011 ownership parse keeps working on combined descriptions (spec g5)."""
    from hmcpctl.operations.lpar.ownership import parse_lpar_ownership_owner

    description = "[hmcpctl owner:alice created:2026-08-21] [caller JIRA-1]"
    assert parse_lpar_ownership_owner(description) == "alice"


# ---------------------------------------------------------------------------
# set_lpar_ownership_description (issue #376, ADR 0066)
# ---------------------------------------------------------------------------


def _patch_restamp_resolution():
    """Patch the operation's name resolution to fixed stubs."""
    return (
        patch(
            "hmcpctl.operations.lpar.ownership.resolve_system_uuid",
            new=AsyncMock(return_value="sys-uuid"),
        ),
        patch(
            "hmcpctl.operations.lpar.ownership.resolve_lpar_uuid",
            new=AsyncMock(return_value="lpar-uuid"),
        ),
        patch(
            "hmcpctl.operations.lpar.ownership.resolve_lpar_ownership_names",
            new=AsyncMock(return_value=("sys1", "lpar1")),
        ),
    )


def _run_set_ownership_description(description, *, ownership_override=False):
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    write = AsyncMock(return_value="chsyscfg ok")
    patches = (
        *(_p for _p in _patch_restamp_resolution()),
        patch(
            "hmcpctl.operations.lpar.ownership.set_lpar_description",
            new=write,
        ),
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=description),
        ),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = asyncio.run(
            lpar_ownership.set_lpar_ownership_description(
                hmc,
                "sys1",
                "lpar1",
                description,
                ownership_override=ownership_override,
            )
        )
    return result, write


def test_set_lpar_ownership_description_writes_owned_lpar():
    """An LPAR owned by the calling agent accepts a guarded rewrite."""
    result, write = _run_set_ownership_description(
        "[hmcpctl owner:alice created:2026-08-14]"
    )
    assert result == "chsyscfg ok"
    write.assert_awaited_once()
    assert write.call_args.args == (
        _config().model_copy(update={"agent_id": "alice"}),
        "sys1",
        "lpar1",
        "[hmcpctl owner:alice created:2026-08-14]",
    )


def test_set_lpar_ownership_description_rejects_foreign_owned():
    """A foreign-owned token blocks the write and issues no SSH traffic."""
    read = AsyncMock(return_value="[hmcpctl owner:bob created:2026-08-14]")
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    write = AsyncMock()
    patches = (
        *(_p for _p in _patch_restamp_resolution()),
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        patch("hmcpctl.operations.lpar.ownership.set_lpar_description", new=write),
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        pytest.raises(PermissionError, match="owned by 'bob'"),
    ):
        asyncio.run(
            lpar_ownership.set_lpar_ownership_description(
                hmc, "sys1", "lpar1", "replacement"
            )
        )
    write.assert_not_awaited()


def test_set_lpar_ownership_description_writes_unowned_lpar():
    """An LPAR with no ownership token can receive its first stamp."""
    read = AsyncMock(return_value="")
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    write = AsyncMock(return_value="ok")
    patches = (
        *(_p for _p in _patch_restamp_resolution()),
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        patch("hmcpctl.operations.lpar.ownership.set_lpar_description", new=write),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = asyncio.run(
            lpar_ownership.set_lpar_ownership_description(
                hmc, "sys1", "lpar1", "first stamp"
            )
        )
    assert result == "ok"
    write.assert_awaited_once()


def test_set_lpar_ownership_description_override_bypasses_guard(caplog):
    """ownership_override=True skips the ownership read and writes anyway."""
    read = AsyncMock(return_value="[hmcpctl owner:bob created:2026-08-14]")
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    write = AsyncMock(return_value="ok")
    patches = (
        *(_p for _p in _patch_restamp_resolution()),
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        patch("hmcpctl.operations.lpar.ownership.set_lpar_description", new=write),
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        caplog.at_level(logging.WARNING),
    ):
        result = asyncio.run(
            lpar_ownership.set_lpar_ownership_description(
                hmc,
                "sys1",
                "lpar1",
                "replacement",
                ownership_override=True,
            )
        )
    assert result == "ok"
    read.assert_not_awaited()
    write.assert_awaited_once()
    assert write.call_args.args[3] == "replacement"
    records = _override_records(caplog)
    assert len(records) == 1
    assert records[0]["event"] == "ownership-override"


@pytest.mark.parametrize("bad", ["em\u2014dash", "a,b", "a=b", "line\nbreak"])
def test_set_lpar_ownership_description_rejects_invalid_text(bad):
    """Validation fires before any name resolution or network activity."""
    resolve_system = AsyncMock()
    hmc = type("StubHMC", (), {"config": _config()})()
    write = AsyncMock()
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.resolve_system_uuid", new=resolve_system
        ),
        patch("hmcpctl.operations.lpar.ownership.set_lpar_description", new=write),
        pytest.raises(ValueError),
    ):
        asyncio.run(
            lpar_ownership.set_lpar_ownership_description(hmc, "sys1", "lpar1", bad)
        )
    resolve_system.assert_not_awaited()
    write.assert_not_awaited()


def test_set_lpar_ownership_description_restamps_failed_create_stamp():
    """Re-stamp path: an unowned LPAR receives an ADR 0011 + ADR 0064 token."""
    read = AsyncMock(return_value="")
    today = datetime.date.today().isoformat()  # noqa: DTZ011 - mirrors the ownership stamp's own local-date basis in ssh/lpar.py; changing one side alone makes them disagree for part of every day
    token = f"[hmcpctl owner:alice created:{today}] [caller JIRA-42]"
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": "alice"})}
    )()
    write = AsyncMock(return_value="ok")
    patches = (
        *(_p for _p in _patch_restamp_resolution()),
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        patch("hmcpctl.operations.lpar.ownership.set_lpar_description", new=write),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = asyncio.run(
            lpar_ownership.set_lpar_ownership_description(hmc, "sys1", "lpar1", token)
        )
    assert result == "ok"
    assert write.call_args.args[3] == token
    assert result == "ok"
    assert write.call_args.args[3] == token


def _set_description_over(current, description, *, agent_id="alice"):
    """Run the operation against *current* and return the read and write mocks."""
    hmc = type(
        "StubHMC", (), {"config": _config().model_copy(update={"agent_id": agent_id})}
    )()
    read = AsyncMock(return_value=current)
    write = AsyncMock(return_value="ok")
    patches = (
        *(_p for _p in _patch_restamp_resolution()),
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        patch("hmcpctl.operations.lpar.ownership.set_lpar_description", new=write),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        asyncio.run(
            lpar_ownership.set_lpar_ownership_description(
                hmc, "sys1", "lpar1", description
            )
        )
    return read, write


def test_set_description_keeps_the_captured_stamp_and_caller_token():
    """#1169 regression, in the shape the live capture recorded.

    Before: the write replaced ``[hmcpctl owner:hmcpctl created:...] [caller
    w1161]`` with ``w1161 probe``, and the unstamped partition was then deleted
    with no override.
    """
    stamp = "[hmcpctl owner:hmcpctl created:2026-09-29] [caller w1161]"
    read, write = _set_description_over(stamp, "w1161 probe", agent_id=None)
    read.assert_awaited_once()
    assert write.call_args.args[3] == f"{stamp} w1161 probe"
    assert lpar_ownership.parse_lpar_ownership_owner(write.call_args.args[3]) == (
        "hmcpctl"
    )
    assert (
        lpar_ownership.parse_lpar_ownership_caller_token(write.call_args.args[3])
        == "w1161"
    )


@pytest.mark.parametrize(
    ("current", "text", "written"),
    [
        (
            "[hmcpctl owner:alice created:2026-08-14]",
            "notes",
            "[hmcpctl owner:alice created:2026-08-14] notes",
        ),
        (
            "[hmcpctl owner:alice created:2026-08-14] [caller T-1] old notes",
            "new notes",
            "[hmcpctl owner:alice created:2026-08-14] [caller T-1] new notes",
        ),
        (
            "[hmcpctl owner:alice created:2026-08-14] [caller T-1] old notes",
            "",
            "[hmcpctl owner:alice created:2026-08-14] [caller T-1]",
        ),
        ("free text, no stamp", "replacement", "replacement"),
        (
            "lead [hmcpctl owner:alice created:2026-08-14] [caller T-1] tail",
            "new",
            "[hmcpctl owner:alice created:2026-08-14] [caller T-1] new",
        ),
        (
            "[hmcpctl owner:alice created:2026-08-14] [caller T-1] see [caller U]",
            "new",
            "[hmcpctl owner:alice created:2026-08-14] [caller T-1] new",
        ),
        (
            (
                "[hmcpctl owner:alice created:2026-08-14] x "
                "[hmcpctl owner:bob created:2026-08-15] [caller T-2]"
            ),
            "new",
            "[hmcpctl owner:alice created:2026-08-14] new",
        ),
    ],
)
def test_set_description_replaces_only_the_user_text(current, text, written):
    """Plain text replaces the field, keeping the first stamp and its own caller."""
    _, write = _set_description_over(current, text)
    assert write.call_args.args[3] == written


def test_set_description_with_its_own_stamp_is_written_as_given():
    """ADR 0066 handover: the owner writes a complete stamp naming the new owner."""
    handover = "[hmcpctl owner:bob created:2026-09-30] [caller T-2] pool"
    _, write = _set_description_over(
        "[hmcpctl owner:alice created:2026-08-14] [caller T-1]", handover
    )
    assert write.call_args.args[3] == handover


@pytest.mark.parametrize(
    "bad",
    [
        "[hmcpctl owner:alice]",
        "[hmcpctl owner:alice created:2026-8-1] notes",
        "[caller T-1] notes",
        "notes [hmcpctl",
    ],
)
@pytest.mark.parametrize("ownership_override", [False, True])
def test_set_description_refuses_a_stamp_that_does_not_parse(bad, ownership_override):
    """A malformed stamp would lock the partition; refused before any read, even
    with an override."""
    resolve_system = AsyncMock()
    hmc = type("StubHMC", (), {"config": _config()})()
    write = AsyncMock()
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.resolve_system_uuid", new=resolve_system
        ),
        patch("hmcpctl.operations.lpar.ownership.set_lpar_description", new=write),
        pytest.raises(ValueError, match="well-formed ownership stamp"),
    ):
        asyncio.run(
            lpar_ownership.set_lpar_ownership_description(
                hmc, "sys1", "lpar1", bad, ownership_override=ownership_override
            )
        )
    resolve_system.assert_not_awaited()
    write.assert_not_awaited()


def test_set_description_refuses_a_malformed_current_stamp():
    """The guard still refuses a malformed current token without an override."""
    hmc = type("StubHMC", (), {"config": _config()})()
    write = AsyncMock()
    patches = (
        *(_p for _p in _patch_restamp_resolution()),
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value="[hmcpctl owner:alice]"),
        ),
        patch("hmcpctl.operations.lpar.ownership.set_lpar_description", new=write),
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        pytest.raises(PermissionError, match="malformed"),
    ):
        asyncio.run(
            lpar_ownership.set_lpar_ownership_description(hmc, "sys1", "lpar1", "x")
        )
    write.assert_not_awaited()


# ---------------------------------------------------------------------------
# create_and_stamp_lpar stamp_policy (issue #377, ADR 0067)
# ---------------------------------------------------------------------------


from hmcpctl.documents.lpar import LparResources  # noqa: E402
from hmcpctl.errors import HMCError  # noqa: E402
from hmcpctl.operations.lpar.core import (  # noqa: E402
    LparCreation,
    create_and_stamp_lpar,
)
from hmcpctl.ssh.transport import HMCCLIError  # noqa: E402

_STAMP_FAILURES = [
    HMCCLIError("SSH command failed"),
    OSError("connection dropped"),
    ValueError("composed description broke the -i grammar"),
]
_STAMP_FAILURE_IDS = ["HMCCLIError", "OSError", "ValueError"]


def _creation(**overrides):
    kwargs = {
        "name": "newlpar",
        "partition_type": "AIX/Linux",
        # A processor value: a create with no resource value goes straight to
        # mksyscfg (#1164), and a memory value would read the system (#1166).
        "resources": LparResources(desired_vcpus=1),
    }
    kwargs.update(overrides)
    return LparCreation(**kwargs)


def _create_hmc():
    """Stub client whose create path succeeds and returns a partition entry."""
    hmc = type(
        "StubCreateHMC",
        (),
        {"config": _config().model_copy(update={"agent_id": "alice"})},
    )()
    hmc.find_partition_by_name = AsyncMock(return_value=None)
    hmc.create_logical_partition = AsyncMock(
        return_value={"UUID": "lpar-uuid-377", "Resource": {"PartitionName": "newlpar"}}
    )
    return hmc


def _run_create(hmc, creation, *, set_description=None):
    """Run create_and_stamp_lpar against stubbed name resolution and SSH write."""
    if set_description is None:
        today = datetime.date.today().isoformat()  # noqa: DTZ011 - mirrors the ownership stamp's own local-date basis in ssh/lpar.py; changing one side alone makes them disagree for part of every day
        set_description = AsyncMock(
            return_value=f"[hmcpctl owner:alice created:{today}]"
        )
    with (
        patch("hmcpctl.ssh.lpar.set_lpar_description", new=set_description),
        patch.object(
            operations_lpar,
            "resolve_system_uuid",
            new=AsyncMock(return_value="sys-uuid"),
        ),
        patch.object(
            lpar_ownership, "_resolve_system_name", new=AsyncMock(return_value="sys1")
        ),
    ):
        result = asyncio.run(create_and_stamp_lpar(hmc, "sys1", creation))
    return result


@pytest.mark.parametrize("failure", _STAMP_FAILURES, ids=_STAMP_FAILURE_IDS)
def test_best_effort_stamp_failure_unchanged(failure):
    """ADR 0011 default is byte-for-byte unchanged for every failure class."""
    result = _run_create(
        _create_hmc(), _creation(), set_description=AsyncMock(side_effect=failure)
    )
    assert result.resource_created is True
    assert result.ownership_stamped is False
    assert result.warnings == ("ownership stamp failed for LPAR 'newlpar'",)


def test_default_stamp_policy_is_best_effort():
    creation = LparCreation("n", "AIX/Linux", LparResources())
    assert creation.stamp_policy == "best-effort"


@pytest.mark.parametrize("failure", _STAMP_FAILURES, ids=_STAMP_FAILURE_IDS)
def test_required_stamp_failure_raises_with_name_and_uuid(failure):
    """Under 'required' every swallowed failure class raises after the create."""
    hmc = _create_hmc()
    with pytest.raises(HMCError) as excinfo:
        _run_create(
            hmc,
            _creation(stamp_policy="required"),
            set_description=AsyncMock(side_effect=failure),
        )
    message = str(excinfo.value)
    assert "'newlpar'" in message
    assert "lpar-uuid-377" in message
    hmc.create_logical_partition.assert_awaited_once()  # raised after the create


def test_required_stamped_success_returns_result():
    """A confirmed stamp under 'required' returns normally."""
    result = _run_create(_create_hmc(), _creation(stamp_policy="required"))
    assert result.resource_created is True
    assert result.ownership_stamped is True
    assert result.warnings == ()


def test_best_effort_stamped_success_returns_result():
    result = _run_create(_create_hmc(), _creation())
    assert result.ownership_stamped is True
    assert result.warnings == ()


def test_required_skips_raise_when_system_name_unresolved():
    """An unresolved system name leaves an untagged LPAR — required rejects it."""
    hmc = _create_hmc()
    with (
        patch.object(
            operations_lpar,
            "resolve_system_uuid",
            new=AsyncMock(return_value="sys-uuid"),
        ),
        patch.object(
            lpar_ownership,
            "_resolve_system_name",
            new=AsyncMock(return_value="sys-uuid"),  # fallback equals the uuid
        ),
        pytest.raises(HMCError) as excinfo,
    ):
        asyncio.run(
            create_and_stamp_lpar(hmc, "sys1", _creation(stamp_policy="required"))
        )
    message = str(excinfo.value)
    assert "'newlpar'" in message
    assert "lpar-uuid-377" in message


def test_best_effort_skip_warning_unchanged_when_system_name_unresolved():
    hmc = _create_hmc()
    with (
        patch.object(
            operations_lpar,
            "resolve_system_uuid",
            new=AsyncMock(return_value="sys-uuid"),
        ),
        patch.object(
            lpar_ownership,
            "_resolve_system_name",
            new=AsyncMock(return_value="sys-uuid"),  # fallback equals the uuid
        ),
    ):
        result = asyncio.run(create_and_stamp_lpar(hmc, "sys1", _creation()))
    assert result.ownership_stamped is None
    assert result.warnings == (
        (
            "ownership stamp skipped for LPAR 'newlpar': "
            "could not resolve the managed-system name"
        ),
    )


def test_required_rejects_unknown_policy_before_traffic():
    hmc = _create_hmc()
    with pytest.raises(ValueError, match="stamp_policy"):
        _run_create(hmc, _creation(stamp_policy="mandatory"))  # type: ignore[arg-type]
    hmc.find_partition_by_name.assert_not_awaited()
