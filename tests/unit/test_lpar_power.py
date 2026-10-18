"""hmc_power_lpar's operation body and classifiers on the ADR 0195 engine (#1223, ADR 0199)."""

from __future__ import annotations

import re

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.operations.logical import engine, store
from hmcpctl.operations.logical.engine import OperationRequest
from hmcpctl.operations.lpar import power

SYS = "0000000a-0000-4000-8000-000000000000"
LPAR = "0000000a-0000-4000-b000-000000000000"
ARGS = {"lpar_name_or_uuid": "web1", "system_name_or_uuid": "sysA"}


class FakeHMC:
    """One system holding one partition; a successful job moves it to ``after``."""

    def __init__(
        self,
        state="not activated",
        *,
        rmc="active",
        status="COMPLETED_OK",
        after=None,
        authorize_power=False,
    ):
        self.config = HMCConfig.from_mapping(
            {"host": "hmc.test", "authorize_power_operations": authorize_power}
        )
        self.state, self.rmc, self.status, self.after = state, rmc, status, after
        self.submits: list[tuple[str, str]] = []
        self.waits = 0
        self.listings = 0
        self.list_error: Exception | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def list_system_summaries(self):
        return [{"UUID": SYS, "SystemName": "sysA"}]

    async def list_logical_partitions(self, system_uuid):
        assert system_uuid == SYS
        self.listings += 1
        if self.list_error is not None:
            raise self.list_error
        resource = {
            "PartitionName": "web1",
            "PartitionState": self.state,
            "ResourceMonitoringControlState": self.rmc,
        }
        return [{"UUID": LPAR, "Resource": resource}]

    async def get_quick_property(self, kind, uuid, name):
        assert (kind, uuid, name) == ("LogicalPartition", LPAR, "PartitionState")
        return self.state

    async def submit_job(self, path, document):
        self.submits.append((path, document))
        return {"Resource": {"JobID": f"J{len(self.submits)}"}}

    async def wait_for_job_entry(self, job_id, timeout, poll):
        self.waits += 1
        if self.status == "COMPLETED_OK" and self.after is not None:
            self.state = self.after
        return {"Resource": {"JobID": job_id, "Status": self.status}}


@pytest.fixture
def hmc(monkeypatch):
    fake = FakeHMC()
    monkeypatch.setattr(power, "open_client", lambda _connection: fake)
    monkeypatch.setattr(power, "POLL_SECONDS", 0)
    return fake


def _run(action, mode="graceful", *, continuation="none", request_id="r1", **extra):
    arguments = {**ARGS, "action": action, "mode": mode, **extra}
    request = OperationRequest(
        "hmc_power_lpar", "agent-a", "<default>", "hmc.test", request_id, arguments
    )
    body = power.power_body(action, mode, "sysA", "web1")
    return engine.submit(
        request,
        body,
        continuation=continuation,
        wait_seconds=10,
        classifiers=power.POWER_CLASSIFIERS,
    )


def _end(record):
    return record.state, record.outcome


def test_start_submits_power_on_and_verifies_running(hmc):
    hmc.after = "running"
    record = _run("start")
    assert _end(record) == ("terminal", "completed")
    assert [path for path, _ in hmc.submits] == [
        f"/rest/api/uom/LogicalPartition/{LPAR}/do/PowerOn"
    ]
    assert record.result == {
        "action": "start",
        "mode": "graceful",
        "system_uuid": SYS,
        "lpar_uuid": LPAR,
        "already_in_state": False,
        "observed_state": "running",
        "job_id": "J1",
    }
    assert [(e.key, e.kind, e.status) for e in record.effects] == [
        ("power", "lpar.power_on", "applied")
    ]
    assert (record.system_uuid, record.partition_uuid) == (SYS, LPAR)


@pytest.mark.parametrize(
    ("action", "state"),
    [("start", "running"), ("start", "open firmware"), ("stop", "not activated")],
)
def test_already_in_state_completes_without_a_job(hmc, action, state):
    hmc.state = state
    record = _run(action)
    assert _end(record) == ("terminal", "completed")
    assert record.result["already_in_state"] is True
    assert record.result["observed_state"] == state
    assert hmc.submits == [] and record.effects == ()


_PARAMETER = re.compile(
    r"<ParameterName[^>]*>(\w+)</ParameterName>\s*<ParameterValue[^>]*>(\w+)<"
)


@pytest.mark.parametrize(
    ("action", "mode", "expected"),
    [
        ("stop", "graceful", ("osshutdown", "false", "false")),
        ("stop", "immediate", ("shutdown", "true", "false")),
        ("restart", "graceful", ("osshutdown", "false", "true")),
        ("restart", "immediate", ("shutdown", "true", "true")),
    ],
)
def test_power_off_maps_mode_and_action_onto_one_job(hmc, action, mode, expected):
    hmc.state = "running"
    hmc.after = "not activated" if action == "stop" else "running"
    record = _run(action, mode)
    assert _end(record) == ("terminal", "completed")
    ((path, document),) = hmc.submits
    assert path.endswith(f"/LogicalPartition/{LPAR}/do/PowerOff")
    parameters = dict(_PARAMETER.findall(document))
    observed = (parameters["operation"], parameters["immediate"], parameters["restart"])
    assert observed == expected
    assert record.effects[0].kind == power.EFFECT_KINDS[action]


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_graceful_without_rmc_fails_naming_immediate_and_writes_nothing(hmc, action):
    hmc.state, hmc.rmc = "running", "inactive"
    record = _run(action)
    assert _end(record) == ("terminal", "failed")
    assert "mode=immediate" in record.warnings[0]
    assert hmc.submits == [] and record.effects == ()


@pytest.mark.parametrize(
    ("action", "state", "names"),
    [("restart", "not activated", "action=start"), ("start", "error", "'error'")],
)
def test_refused_states_fail_before_any_write(hmc, action, state, names):
    hmc.state = state
    record = _run(action, "immediate" if action == "restart" else "graceful")
    assert _end(record) == ("terminal", "failed")
    assert names in record.warnings[0]
    assert hmc.submits == []


def test_unknown_partition_fails_without_echoing_the_selector(hmc):
    request = OperationRequest(
        "hmc_power_lpar",
        "agent-a",
        "<default>",
        "hmc.test",
        "r1",
        {**ARGS, "action": "start"},
    )
    body = power.power_body("start", "graceful", "sysA", "secret-name")
    record = engine.submit(
        request, body, wait_seconds=10, classifiers=power.POWER_CLASSIFIERS
    )
    assert _end(record) == ("terminal", "failed")
    assert "secret-name" not in record.warnings[0]
    assert "no partition" in record.warnings[0]


def test_ownership_refusal_fails_before_the_write(hmc, monkeypatch):
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "authorize_power_operations": True}
    )
    hmc.state = "running"

    async def refuse(*_args, **_kwargs):
        raise PermissionError("owned by agent-b")

    monkeypatch.setattr(power, "resolve_and_authorize_lpar_mutation", refuse)
    record = _run("stop", "immediate")
    assert _end(record) == ("terminal", "failed")
    assert "ownership" in record.warnings[0]
    assert hmc.submits == []


def test_job_timeout_pauses_and_resume_never_resubmits(hmc):
    hmc.state, hmc.status = "running", "RUNNING"
    record = _run("restart", "immediate")
    assert _end(record) == ("paused", "needs_attention")
    assert record.result["job_id"] == "J1"
    hmc.status, hmc.after = "COMPLETED_OK", "running"
    record = _run("restart", "immediate", continuation="resume")
    assert _end(record) == ("terminal", "completed")
    assert len(hmc.submits) == 1 and hmc.waits == 2


def test_failed_job_is_terminal_failed(hmc):
    hmc.state, hmc.status = "running", "FAILED_BEFORE_COMPLETION"
    record = _run("stop", "immediate")
    assert _end(record) == ("terminal", "failed")
    assert (
        "J1" in record.warnings[0] and "FAILED_BEFORE_COMPLETION" in record.warnings[0]
    )


def test_unsettled_state_pauses_for_attention(hmc, monkeypatch):
    monkeypatch.setattr(power, "SETTLE_SECONDS", 0)
    hmc.state = "running"
    hmc.after = "shutting down"
    record = _run("stop", "immediate")
    assert _end(record) == ("paused", "needs_attention")
    assert record.result["observed_state"] == "shutting down"


def test_start_that_lands_in_error_is_failed(hmc):
    hmc.after = "error"
    record = _run("start")
    assert _end(record) == ("terminal", "failed")
    assert "activation failed" in record.warnings[0]


def test_start_with_immediate_is_refused():
    with pytest.raises(ValueError, match="stop and restart only"):
        power.check_inputs("start", "immediate")
    with pytest.raises(ValueError, match="action must be"):
        power.check_inputs("crash", "graceful")
    with pytest.raises(ValueError, match="mode must be"):
        power.check_inputs("stop", "forced")


def test_delegation_row_names_registered_specialists():
    from hmcpctl.server_tools.catalog import TOOL_SECURITY

    assert dict(power.DELEGATED) == {
        "start": "hmc_power_on_lpar",
        "stop": "hmc_power_off_lpar",
        "restart": "hmc_power_off_lpar",
    }
    assert set(power.DELEGATED.values()) <= set(TOOL_SECURITY)


def _crashed(action):
    """A running record whose power intent never got an outcome, as a dead process leaves."""
    request = OperationRequest(
        "hmc_power_lpar",
        "agent-a",
        "<default>",
        "hmc.test",
        "r1",
        {
            **ARGS,
            "action": action,
            "mode": "immediate" if action != "start" else "graceful",
        },
    )
    request_json, digest = engine.canonical_request(request)
    with store.session() as conn, store.write_transaction(conn):
        store.insert_operation(
            conn,
            operation_id="0" * 32,
            agent_id="agent-a",
            request_id="r1",
            tool="hmc_power_lpar",
            connection="<default>",
            host="hmc.test",
            digest=digest,
            request_json=request_json,
        )
        store.set_partition(conn, "0" * 32, SYS, LPAR)
        store.record_intent(
            conn, "0" * 32, "power", power.EFFECT_KINDS[action], f"lpar:{LPAR}"
        )


def test_open_power_on_applied_from_state_completes_without_a_job_poll(hmc):
    _crashed("start")
    hmc.state = "running"
    record = _run("start", continuation="resume")
    assert _end(record) == ("terminal", "completed")
    assert hmc.submits == [] and hmc.waits == 0
    assert record.effects[0].identity == {"job_id": None}


@pytest.mark.parametrize("state", ["not activated", "error"])
def test_open_power_on_is_never_resubmitted(hmc, state):
    _crashed("start")
    hmc.state = state
    record = _run("start", continuation="resume")
    assert _end(record) == ("paused", "needs_attention")
    assert hmc.submits == []


@pytest.mark.parametrize(
    ("state", "end"),
    [
        ("not activated", ("terminal", "completed")),
        ("running", ("paused", "needs_attention")),
    ],
)
def test_open_power_off_is_never_resubmitted(hmc, state, end):
    _crashed("stop")
    hmc.state = state
    record = _run("stop", "immediate", continuation="resume")
    assert _end(record) == end
    assert hmc.submits == []


def test_open_restart_always_needs_attention(hmc):
    _crashed("restart")
    hmc.state = "running"
    record = _run("restart", "immediate", continuation="resume")
    assert _end(record) == ("paused", "needs_attention")
    assert "no live check" in record.warnings[0]
    assert hmc.submits == []


def test_replay_after_the_write_does_not_resolve_again(hmc):
    hmc.state, hmc.status = "running", "RUNNING"
    _run("stop", "immediate")
    hmc.list_error = HMCError("HTTP 503")
    hmc.status, hmc.after = "COMPLETED_OK", "not activated"
    record = _run("stop", "immediate", continuation="resume")
    assert _end(record) == ("terminal", "completed")
    assert hmc.listings == 1 and len(hmc.submits) == 1
    assert (record.result["system_uuid"], record.result["lpar_uuid"]) == (SYS, LPAR)


def test_a_read_error_after_the_write_pauses_rather_than_fails(hmc, monkeypatch):
    hmc.state = "running"

    async def broken(*_args):
        raise HMCError("HTTP 503")

    monkeypatch.setattr(hmc, "wait_for_job_entry", broken)
    record = _run("restart", "immediate")
    assert _end(record) == ("paused", "needs_attention")
    assert record.effects[0].status == "applied"


@pytest.mark.parametrize(
    ("state", "mode", "submits"),
    [
        ("shutting down", "immediate", 0),
        ("migrating running", "immediate", 0),
        ("error", "graceful", 0),
        ("error", "immediate", 1),
    ],
)
def test_stop_proceeds_only_from_activated_or_error_with_immediate(
    hmc, state, mode, submits
):
    hmc.state, hmc.after = state, "not activated"
    record = _run("stop", mode)
    assert len(hmc.submits) == submits
    if not submits:
        assert _end(record) == ("terminal", "failed")
        assert repr(state) in record.warnings[0]


def test_the_guard_is_keyed_by_the_canonical_system_uuid(hmc):
    hmc.state, hmc.status = "running", "RUNNING"
    held = _run("stop", "immediate")
    assert _end(held) == ("paused", "needs_attention")
    other = OperationRequest(
        "hmc_power_lpar",
        "agent-a",
        "<default>",
        "hmc.test",
        "r2",
        {**ARGS, "action": "restart", "mode": "immediate"},
    )
    body = power.power_body("restart", "immediate", SYS.upper(), LPAR.upper())
    record = engine.submit(
        other, body, wait_seconds=10, classifiers=power.POWER_CLASSIFIERS
    )
    assert _end(record) == ("terminal", "failed")
    assert "partition_busy" in record.warnings[0]
    assert len(hmc.submits) == 1


def test_start_waits_out_a_lagging_not_activated_read(hmc, monkeypatch):
    reads = iter(["not activated", "starting"])

    async def lagging(*_args):
        return next(reads)

    hmc.after = "not activated"
    monkeypatch.setattr(hmc, "get_quick_property", lagging)
    record = _run("start")
    assert _end(record) == ("terminal", "completed")
    assert record.result["observed_state"] == "starting"


def test_start_never_activated_pauses_rather_than_fails(hmc, monkeypatch):
    monkeypatch.setattr(power, "SETTLE_SECONDS", 0)
    hmc.after = "not activated"
    record = _run("start")
    assert _end(record) == ("paused", "needs_attention")
    assert record.result["observed_state"] == "not activated"


def test_replay_with_an_unreadable_job_settles_from_state(hmc, monkeypatch):
    hmc.state, hmc.status = "running", "RUNNING"
    _run("stop", "immediate")

    async def purged(*_args):
        raise HMCError("job not found", status_code=404)

    monkeypatch.setattr(hmc, "wait_for_job_entry", purged)
    hmc.state = "not activated"
    record = _run("stop", "immediate", continuation="resume")
    assert _end(record) == ("terminal", "completed")
    assert len(hmc.submits) == 1


def test_a_failed_job_names_the_partition_state(hmc):
    hmc.state, hmc.status = "running", "COMPLETED_WITH_WARNINGS"
    record = _run("stop", "immediate")
    assert _end(record) == ("terminal", "failed")
    assert "'running'" in record.warnings[0]


@pytest.mark.parametrize(
    ("error", "end"),
    [
        (HMCError("job not found", status_code=404), ("paused", "needs_attention")),
        (HMCError("unavailable", status_code=503), ("paused", "needs_attention")),
    ],
)
def test_a_replayed_restart_without_job_evidence_never_completes(
    hmc, monkeypatch, error, end
):
    hmc.state, hmc.status = "running", "RUNNING"
    _run("restart", "immediate")

    async def unreadable(*_args):
        raise error

    monkeypatch.setattr(hmc, "wait_for_job_entry", unreadable)
    record = _run("restart", "immediate", continuation="resume")
    assert _end(record) == end
    assert len(hmc.submits) == 1


def test_a_replayed_stop_with_a_transient_job_error_pauses(hmc, monkeypatch):
    hmc.state, hmc.status = "running", "RUNNING"
    _run("stop", "immediate")

    async def unavailable(*_args):
        raise HMCError("unavailable", status_code=503)

    monkeypatch.setattr(hmc, "wait_for_job_entry", unavailable)
    hmc.state = "not activated"
    record = _run("stop", "immediate", continuation="resume")
    assert _end(record) == ("paused", "needs_attention")
