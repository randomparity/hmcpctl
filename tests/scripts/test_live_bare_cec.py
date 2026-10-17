"""Behavioural tests for the bare-CEC live arm (issue #876) and its wrapper.

The arm runs against the production `RunState` — its `call` dispatch guard,
`record`, `record_with_expected` and `record_verified` — with only the MCP
client replaced. The client answers from a small model of one partition, so a
dispatch the served tool schemas would reject fails here as `InvalidDispatch`,
exactly as it would against a real HMC.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
import live_bare_cec as wrapper  # noqa: E402
import live_test_runner as runner  # noqa: E402
from live_test import bare_cec, pcie  # noqa: E402
from live_test.observation import CLEANUP, Assertion  # noqa: E402

from hmcpctl.errors import HMCError  # noqa: E402
from hmcpctl.ssh.profiles import profile_io_slot_rows_command  # noqa: E402
from hmcpctl.ssh.transport import HMCCLIError  # noqa: E402

_SYSTEM = "sys-one"
_DRC = "21010020"
_ASSIGNED = f"{_DRC}/none/0"
_LPAR_UUID = "0A1B2C3D-0000-4000-8000-000000000001"
_PROFILE_UUID = "0A1B2C3D-0000-4000-8000-000000000002"
_JOB_ID = "4711"
_PROFILE_READ = profile_io_slot_rows_command(_SYSTEM)

#: The operations whose evidence this arm produces (issue #876).
_PROMOTED = {
    "pcie.assign_dedicated_slot",
    "pcie.unassign_dedicated_slot",
    "job.get",
    "job.wait",
    "lpar.list_refcodes",
    "lpar.get_state",
    "pcie.list_dedicated_slots",
}
#: Judged but not promoted: the lpar-power arm owns their observations (#1346).
_HELD = {
    "lpar.create": "hmc_create_lpar",
    "lpar.power_on": "hmc_power_on_lpar",
    "lpar.power_off": "hmc_power_off_lpar",
    "lpar.capture_console": "hmc_capture_lpar_console",
    "lpar.delete": "hmc_delete_lpar",
}

_DEFAULT = object()
_REAL_AUTHORIZATION_CHECK = bare_cec._power_operations_authorized


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict[str, Any]]:
    """The input schemas the live runner's server really serves."""

    async def served() -> dict[str, dict[str, Any]]:
        async with runner.served_client() as client:
            return await runner.served_schemas(client)

    return asyncio.run(served())


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    async def cli_read(_state, arguments):
        if arguments[:2] == ["lpars", "state"]:
            return "open firmware"
        if arguments[:2] == ["lpars", "refcodes"]:
            return json.dumps([{"lpar_name": arguments[3], "refcode": "C2001150"}])
        assert arguments[:2] == ["jobs", "show"]
        return json.dumps(
            {"job_id": arguments[2], "found": True, "status": "COMPLETED_OK"}
        )

    monkeypatch.setattr(bare_cec.cli, "execute", cli_read)
    monkeypatch.setattr(bare_cec, "_STATE_POLL_DELAY_S", 0)
    monkeypatch.setattr(pcie, "_ABSENCE_REREAD_DELAY_S", 0)
    monkeypatch.setattr(bare_cec, "_power_operations_authorized", lambda: True)


def _job(status: str = "COMPLETED_OK", error: str | None = None) -> dict[str, Any]:
    results = (
        {"JobParameter": [{"ParameterName": "result", "ParameterValue": error}]}
        if error
        else {}
    )
    return {"UUID": _JOB_ID, "Resource": {"Status": status, "Results": results}}


class World:
    """One managed system holding one slot and, once created, one partition."""

    def __init__(self) -> None:
        self.created = False
        self.marker = ""
        self.name = ""
        self.io_slots = "none"
        self.lpar_state = "not activated"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: tool -> fn(kwargs) returning a value, an exception to raise, or _DEFAULT.
        self.overrides: dict[str, Any] = {}

    # -- views -------------------------------------------------------------

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    def calls_to(self, tool: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == tool]

    # -- model -------------------------------------------------------------

    def respond(self, tool: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((tool, kwargs))
        override = self.overrides.get(tool)
        if override is not None:
            value = override(kwargs)
            if value is not _DEFAULT:
                return value
        return getattr(self, "_" + tool)(kwargs)

    def _hmc_run_command(self, kwargs: dict[str, Any]) -> str:
        cmd = kwargs["cmd"]
        if cmd == "lshmc -V":
            return "Version: 10\nRelease: 3\nService Pack: 1060"
        if "type_model" in cmd:
            return "8375-42A"
        if cmd == _PROFILE_READ:
            rows = (
                [f'{self.name},default_profile,"{self.io_slots}"']
                if self.created
                else []
            )
            return "\n".join(["lpar_name,name,io_slots", *rows]) + "\n"
        if "io_slots-" in cmd:
            self.io_slots = "none"
            return ""
        raise AssertionError(f"unmodelled command {cmd!r}")

    def _hmc_list_dedicated_pcie_slots(self, _kwargs: dict[str, Any]) -> dict[str, Any]:
        owner = self.name if self.created and self.lpar_state != "not activated" else ""
        return {
            "capability": "available",
            "items": [
                {"drc_index": _DRC, "description": "Ethernet", "owner_lpar": owner}
            ],
        }

    def _hmc_create_lpar(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.created, self.name, self.marker = (
            True,
            kwargs["name"],
            kwargs["caller_token"],
        )
        return {
            "resource_created": True,
            "lpar": {"UUID": _LPAR_UUID},
            "ownership_stamped": True,
            "warnings": [],
        }

    def _hmc_get_lpar(self, _kwargs: dict[str, Any]) -> dict[str, Any] | None:
        if not self.created:
            return None
        href = (
            "https://hmc.example.test/rest/api/uom/LogicalPartition/"
            f"{_LPAR_UUID}/LogicalPartitionProfile/{_PROFILE_UUID}"
        )
        # The Atom entry `hmc_get_lpar` serves on a real HMC: identity at the
        # top, the partition's own fields under `Resource`.
        return {
            "UUID": _LPAR_UUID,
            "ResourceType": "LogicalPartition",
            "title": "LogicalPartition",
            "link": f"https://hmc.example.test/rest/api/uom/LogicalPartition/{_LPAR_UUID}",
            "Resource": {
                "PartitionName": self.name,
                "AssociatedPartitionProfile": {"href": href, "rel": "related"},
            },
        }

    def _hmc_get_lpar_description(self, kwargs: dict[str, Any]) -> Any:
        if not self.created:
            return HMCCLIError(
                f"HSCL8012 The partition named {kwargs['lpar_name_or_uuid']} was not found."
            )
        return f"[hmcpctl owner:hmcpctl created:2026-09-22] [caller {self.marker}]"

    def _hmc_assign_dedicated_pcie_slot(self, _kwargs: dict[str, Any]) -> None:
        self.io_slots = _ASSIGNED

    def _hmc_unassign_dedicated_pcie_slot(self, _kwargs: dict[str, Any]) -> None:
        self.io_slots = "none"

    def _hmc_power_on_lpar(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if kwargs.get("partition_profile_uuid") is None:
            job = _job("COMPLETED_WITH_ERROR", "HSCL3680 No current configuration")
        else:
            self.lpar_state, job = "open firmware", _job()
        return {"already_running": False, "job": job, "message": None}

    def _hmc_power_off_lpar(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if kwargs.get("operation") == "osshutdown":
            return _job(
                "COMPLETED_WITH_ERROR",
                "HSCL0DB4 An Operating System Shutdown cannot be performed because "
                "the operating system image running does not support remote "
                "execution of this task from the management console.",
            )
        self.lpar_state = "open firmware" if kwargs.get("restart") else "not activated"
        return _job()

    def _hmc_dump_restart_lpar(self, _kwargs: dict[str, Any]) -> dict[str, Any]:
        self.lpar_state = "open firmware"
        return _job()

    def _hmc_get_lpar_state(self, _kwargs: dict[str, Any]) -> str:
        return self.lpar_state

    def _hmc_get_job(self, _kwargs: dict[str, Any]) -> dict[str, Any]:
        return _job()

    def _hmc_wait_for_job(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": kwargs["job_id"],
            "status": "COMPLETED_OK",
            "timed_out": False,
            "error": None,
            "job": _job(),
            "found": True,
            "job_href": None,
        }

    def _hmc_read_lpar_refcodes(self, _kwargs: dict[str, Any]) -> list[dict[str, str]]:
        return [{"lpar_name": self.name, "time_stamp": "t", "refcode": "CA00E1DC"}]

    def _hmc_capture_lpar_console(self, _kwargs: dict[str, Any]) -> dict[str, Any]:
        return {
            "system": _SYSTEM,
            "partition": self.name,
            "stop_reason": "duration",
            "released": True,
            "error": None,
            "bytes_captured": 3,
            "data_base64": "U01T",
        }

    def _hmc_delete_lpar(self, _kwargs: dict[str, Any]) -> str:
        if self.lpar_state != "not activated":
            return HMCError(
                "partition is not in the not activated state", status_code=409
            )
        self.created = False
        return f"Deleted LPAR {_LPAR_UUID}"


class FakeClient:
    def __init__(self, world: World) -> None:
        self.world = world

    async def call_tool(self, tool: str, kwargs: dict[str, Any]) -> Any:
        value = self.world.respond(tool, kwargs)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return SimpleNamespace(data=None, content=[SimpleNamespace(text="null")])
        return SimpleNamespace(data=value)


def _state(schemas, **config: str) -> runner.RunState:
    state = runner.RunState(
        config=runner.LiveTestConfig(
            dedicated_pcie_system_name=_SYSTEM,
            dedicated_pcie_lpar_prefix="live-",
            dedicated_pcie_drc_index=_DRC,
            **config,
        )
    )
    state.schemas = schemas
    return state


def _run(world: World, state: runner.RunState) -> None:
    asyncio.run(bare_cec.exercise_bare_cec(FakeClient(world), state))


def _rows(state: runner.RunState, status: str) -> list[dict[str, Any]]:
    return [row for row in state.results if row["status"] == status]


def _row(state: runner.RunState, tool: str) -> dict[str, Any]:
    return next(row for row in state.results if row["tool"] == tool)


def _observations(state: runner.RunState) -> dict[str, dict[str, Any]]:
    return {item["operation"]: item["observation"] for item in state.observations}


def _held(state: runner.RunState) -> dict[str, dict[str, Any]]:
    """Each judged-but-held step's row, keyed by its operation, when it ran."""
    held = {}
    for operation, tool in _HELD.items():
        rows = [row for row in state.results if row["tool"] == tool]
        assert len(rows) <= 1, f"{tool} recorded more than once"
        if rows:
            held[operation] = {
                "result": "passed" if rows[0]["status"] == "PASS" else "failed",
                "unmet": rows[0]["note"],
            }
    return held


def _assert_torn_down(world: World) -> None:
    assert not world.created
    assert world.io_slots == "none"


# ---------------------------------------------------------------------------
# The whole arm
# ---------------------------------------------------------------------------


def test_happy_path_promotes_every_operation_and_leaves_nothing(schemas):
    world, state = World(), _state(schemas)

    _run(world, state)

    assert _rows(state, "FAIL") == []
    observations = _observations(state)
    assert set(observations) == _PROMOTED
    assert {o["result"] for o in observations.values()} == {"passed"}
    assert {o["scenario"] for o in observations.values()} == {"st35-bare-cec"}
    held = _held(state)
    assert set(held) == set(_HELD)
    assert {item["unmet"] for item in held.values()} == {""}
    ids = [item["observation"]["id"] for item in state.observations]
    assert len(ids) == len(set(ids)), (
        "a duplicate id discards the observations document"
    )
    assert observations["pcie.assign_dedicated_slot"]["cleanup"] == "passed"
    assert set(observations["lpar.get_state"]["assertions"]) == {
        "state-read-returned-a-state"
    }
    assert set(observations["pcie.list_dedicated_slots"]["assertions"]) == {
        "list-call-succeeded",
        "fixture-slot-listed",
        "fixture-slot-unowned",
    }
    _assert_torn_down(world)


def test_happy_path_issues_the_issue_876_sequence(schemas):
    world, state = World(), _state(schemas)

    _run(world, state)

    power = [
        (
            tool,
            kwargs.get("partition_profile_uuid"),
            kwargs.get("operation"),
            kwargs.get("restart"),
        )
        for tool, kwargs in world.calls
        if tool in {"hmc_power_on_lpar", "hmc_power_off_lpar"}
    ]
    assert power == [
        ("hmc_power_on_lpar", None, None, None),
        ("hmc_power_on_lpar", _PROFILE_UUID, None, None),
        ("hmc_power_off_lpar", None, "shutdown", False),
        ("hmc_power_on_lpar", _PROFILE_UUID, None, None),
        ("hmc_power_off_lpar", None, "shutdown", True),
        ("hmc_power_off_lpar", None, "osshutdown", None),
        ("hmc_power_off_lpar", None, "shutdown", False),
    ]
    assert all(
        kwargs["immediate"] is True
        for kwargs in world.calls_to("hmc_power_off_lpar")
        if kwargs.get("operation") == "shutdown"
    )
    tools = world.tools()
    order = [
        "hmc_create_lpar",
        "hmc_assign_dedicated_pcie_slot",
        "hmc_get_job",
        "hmc_wait_for_job",
        "hmc_read_lpar_refcodes",
        "hmc_capture_lpar_console",
        "hmc_unassign_dedicated_pcie_slot",
        "hmc_delete_lpar",
    ]
    assert [tools.index(tool) for tool in order] == sorted(
        tools.index(tool) for tool in order
    )
    (create,) = world.calls_to("hmc_create_lpar")
    assert create["resources"] == bare_cec._RESOURCES
    assert create["resources"]["desired_procs"] == 0.5
    sms = world.calls_to("hmc_power_on_lpar")[1]
    assert sms["boot_mode"] == "sms" and sms["wait"] is True
    assert world.calls_to("hmc_get_job") == [{"job_id": _JOB_ID}]
    (capture,) = world.calls_to("hmc_capture_lpar_console")
    assert capture["duration_seconds"] == 30.0
    assert capture["idle_timeout_seconds"] == 30.0
    assert _row(state, "hmc_dump_restart_lpar")["status"] == "SKIP"
    assert "#868" in _row(state, "network boot")["note"]
    assert "data_base64" not in _row(state, "hmc_capture_lpar_console")["data"]


def test_expected_refusals_arriving_as_failed_jobs_record_skip(schemas):
    world, state = World(), _state(schemas)

    _run(world, state)

    no_profile = _row(state, "hmc_power_on_lpar (no partition profile)")
    osshutdown = _row(state, "hmc_power_off_lpar (osshutdown)")
    assert (no_profile["status"], osshutdown["status"]) == ("SKIP", "SKIP")
    assert "HSCL3680" in no_profile["data"]
    assert "HSCL0DB4" in osshutdown["data"]
    assert state.gaps == [], "an environment refusal is not a product gap"


def test_no_profile_reason_does_not_claim_the_profile_was_never_applied():
    reason = bare_cec._NO_PROFILE_ACTIVATION_REFUSED.reason
    assert "never applied" not in reason
    assert "unconfirmed" in reason
    assert "#879" in reason


def test_a_raised_refusal_matching_the_declaration_records_skip(schemas):
    world, state = World(), _state(schemas)
    world.overrides["hmc_power_on_lpar"] = lambda kwargs: (
        HMCError("HSCL3680 No current configuration", status_code=400)
        if kwargs.get("partition_profile_uuid") is None
        else _DEFAULT
    )

    _run(world, state)

    assert _row(state, "hmc_power_on_lpar (no partition profile)")["status"] == "SKIP"
    assert _rows(state, "FAIL") == []


def test_an_unmatched_refusal_is_a_failure_not_a_skip(schemas):
    world, state = World(), _state(schemas)
    world.overrides["hmc_power_off_lpar"] = lambda kwargs: (
        _job("COMPLETED_WITH_ERROR", "HSCL9999 something else")
        if kwargs.get("operation") == "osshutdown"
        else _DEFAULT
    )

    _run(world, state)

    assert [row["tool"] for row in _rows(state, "FAIL")] == [
        "hmc_power_off_lpar (osshutdown)"
    ]
    _assert_torn_down(world)


def test_a_no_profile_activation_that_boots_is_powered_off_before_sms(schemas):
    world, state = World(), _state(schemas)

    def boots(kwargs: dict[str, Any]) -> Any:
        if kwargs.get("partition_profile_uuid") is None:
            world.lpar_state = "open firmware"
            return {"already_running": False, "job": _job(), "message": None}
        return _DEFAULT

    world.overrides["hmc_power_on_lpar"] = boots

    _run(world, state)

    assert _row(state, "hmc_power_on_lpar (no partition profile)")["status"] == "PASS"
    assert "'open firmware'" in _row(state, "no-profile activation outcome")["data"]
    tools = [
        t for t in world.tools() if t in {"hmc_power_on_lpar", "hmc_power_off_lpar"}
    ]
    assert tools[:3] == ["hmc_power_on_lpar", "hmc_power_off_lpar", "hmc_power_on_lpar"]
    assert _held(state)["lpar.power_on"]["result"] == "passed"


def test_platform_dump_runs_only_on_opt_in(schemas):
    world, state = World(), _state(schemas, accept_platform_dump="TRUE")

    _run(world, state)

    (dump,) = world.calls_to("hmc_dump_restart_lpar")
    assert dump["allow_dump_restart"] is True
    assert dump["wait"] is True
    assert all(
        "allow_dump_restart" not in k for k in world.calls_to("hmc_power_off_lpar")
    )
    assert _row(state, "state after dumprestart")["status"] == "PASS"
    _assert_torn_down(world)


def test_a_timed_out_activation_fails_ends_the_steps_and_still_tears_down(schemas):
    world, state = World(), _state(schemas)

    def hangs(kwargs: dict[str, Any]) -> Any:
        if kwargs.get("partition_profile_uuid") is None:
            return _DEFAULT
        world.lpar_state = "starting"
        return {"already_running": False, "job": _job("RUNNING"), "message": None}

    world.overrides["hmc_power_on_lpar"] = hangs

    _run(world, state)

    assert _held(state)["lpar.power_on"]["result"] == "failed"
    assert "lpar.get_state" not in _observations(state)
    assert "JobTimedOut" in _row(state, "hmc_power_on_lpar")["data"]
    assert "hmc_read_lpar_refcodes" not in world.tools()
    assert "hmc_power_off_lpar (teardown)" in [row["tool"] for row in state.results]
    assert _held(state)["lpar.delete"]["result"] == "passed"
    _assert_torn_down(world)


def test_a_slot_still_owned_after_delete_fails_the_slot_list_observation(schemas):
    world, state = World(), _state(schemas)
    world.overrides["hmc_list_dedicated_pcie_slots"] = lambda _kwargs: (
        {"items": [{"drc_index": _DRC, "owner_lpar": "someone"}]}
        if "hmc_delete_lpar" in world.tools()
        else _DEFAULT
    )

    _run(world, state)

    listing = _observations(state)["pcie.list_dedicated_slots"]
    assert listing["result"] == "failed"
    assert (
        _row(state, "hmc_list_dedicated_pcie_slots")["note"]
        == "unmet: fixture-slot-unowned"
    )


# ---------------------------------------------------------------------------
# Admission: nothing is created
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "label"),
    [
        (
            lambda mp, state: mp.setattr(
                bare_cec, "_power_operations_authorized", lambda: False
            ),
            "bare-cec power authorization",
        ),
        (
            lambda _mp, state: object.__setattr__(
                state.config, "accept_platform_dump", "yes"
            ),
            "bare-cec platform-dump opt-in",
        ),
        (
            lambda _mp, state: state.record(
                29, "dedicated slot selection", "PASS", "x"
            ),
            "bare-cec fixture artifacts",
        ),
    ],
)
def test_each_admission_refusal_skips_before_touching_the_hmc(
    schemas, monkeypatch, setup, label
):
    world, state = World(), _state(schemas)
    setup(monkeypatch, state)

    _run(world, state)

    assert world.calls == []
    assert _row(state, label)["status"] == "SKIP"
    assert state.observations == []


def test_power_operations_authorization_reads_the_server_config(monkeypatch):
    monkeypatch.setenv("HMC_AUTHORIZE_POWER_OPERATIONS", "true")
    assert _REAL_AUTHORIZATION_CHECK() is True
    monkeypatch.setenv("HMC_AUTHORIZE_POWER_OPERATIONS", "false")
    assert _REAL_AUTHORIZATION_CHECK() is False


# ---------------------------------------------------------------------------
# Failure paths and the teardown
# ---------------------------------------------------------------------------


def test_an_exception_mid_arm_still_powers_off_unassigns_and_deletes(
    schemas, monkeypatch
):
    world, state = World(), _state(schemas)

    async def explode(*_args: Any) -> None:
        raise RuntimeError("scenario bug")

    monkeypatch.setattr(bare_cec, "_observe", explode)

    _run(world, state)

    raised = _row(state, "bare-cec arm raised before teardown")
    assert "RuntimeError: scenario bug" in raised["data"]
    tools = world.tools()
    teardown_off = max(i for i, t in enumerate(tools) if t == "hmc_power_off_lpar")
    assert (
        teardown_off
        < tools.index("hmc_unassign_dedicated_pcie_slot")
        < tools.index("hmc_delete_lpar")
    )
    assert _observations(state)["pcie.unassign_dedicated_slot"]["result"] == "passed"
    _assert_torn_down(world)


def test_a_partition_that_will_not_power_off_is_left_with_recovery_commands(
    schemas, monkeypatch
):
    world, state = World(), _state(schemas)
    _explodes_after_activation(monkeypatch)
    world.overrides["hmc_power_off_lpar"] = lambda _k: _job(
        "COMPLETED_WITH_ERROR", "HSCL1234 no"
    )

    _run(world, state)

    recovery = _row(state, "bare-cec teardown: partition not powered off")
    assert recovery["status"] == "FAIL"
    for command in ("chsysstate", "io_slots-", "rmsyscfg"):
        assert command in recovery["data"]
    assert "hmc_unassign_dedicated_pcie_slot" not in world.tools()
    assert "hmc_delete_lpar" not in world.tools()
    held = _held(state)
    assert held["lpar.create"]["result"] == "failed"
    assert "cleanup failed" in held["lpar.create"]["unmet"]
    assert "pcie.unassign_dedicated_slot" not in _observations(state)
    assert "lpar.delete" not in held
    assert world.created


def _explodes_after_activation(monkeypatch) -> None:
    async def explode(*_args: Any) -> None:
        raise RuntimeError("scenario bug")

    monkeypatch.setattr(bare_cec, "_observe", explode)


def _assert_left_untouched_with_recovery(state: runner.RunState, world: World) -> None:
    recovery = _row(state, "bare-cec teardown: identity not confirmed")
    assert recovery["status"] == "FAIL"
    for command in ("chsysstate", "io_slots-", "rmsyscfg"):
        assert command in recovery["data"]
    assert "hmc_delete_lpar" not in world.tools()
    assert not any("io_slots-" in k["cmd"] for k in world.calls_to("hmc_run_command"))
    assert world.io_slots == _ASSIGNED
    assert world.lpar_state == "open firmware"


def test_a_foreign_identity_on_an_active_partition_is_left_with_recovery(
    schemas, monkeypatch
):
    world, state = World(), _state(schemas)
    _explodes_after_activation(monkeypatch)
    world.overrides["hmc_get_lpar_description"] = lambda _k: (
        "[hmcpctl owner:hmcpctl created:2026-09-22] [caller someone-else]"
        if world.lpar_state != "not activated"
        else _DEFAULT
    )

    _run(world, state)

    _assert_left_untouched_with_recovery(state, world)
    assert world.calls_to("hmc_power_off_lpar") == []


def test_a_transient_identity_read_is_re_read_before_teardown_acts(
    schemas, monkeypatch
):
    world, state = World(), _state(schemas)
    _explodes_after_activation(monkeypatch)
    failures = iter([HMCError("transient 503", status_code=503)])
    world.overrides["hmc_get_lpar"] = lambda _k: (
        next(failures, _DEFAULT) if world.lpar_state == "open firmware" else _DEFAULT
    )

    _run(world, state)

    assert _held(state)["lpar.delete"]["result"] == "passed"
    _assert_torn_down(world)


def test_an_identity_that_stays_unreadable_is_never_mutated(schemas, monkeypatch):
    """Two failed reads: the partition may be active, so nothing is issued."""
    world, state = World(), _state(schemas)
    _explodes_after_activation(monkeypatch)
    world.overrides["hmc_get_lpar"] = lambda _k: (
        HMCError("transient 503", status_code=503)
        if world.lpar_state == "open firmware"
        else _DEFAULT
    )

    _run(world, state)

    _assert_left_untouched_with_recovery(state, world)


def test_a_never_confirmed_fixture_goes_to_the_shared_guards(schemas):
    """A create whose stamp never landed was never activated: cleanup_dedicated decides."""
    world, state = World(), _state(schemas)
    world.overrides["hmc_create_lpar"] = lambda kwargs: {
        **world._hmc_create_lpar(kwargs),
        "ownership_stamped": False,
    }

    _run(world, state)

    assert "hmc_power_on_lpar" not in world.tools()
    assert _held(state)["lpar.create"]["result"] == "failed"
    assert any(row["subtask"] == 34 for row in state.results)


def test_an_empty_refcode_read_is_not_promoted(schemas):
    """A partition with no reference codes reads [], so [] proves nothing."""
    world, state = World(), _state(schemas)
    world.overrides["hmc_read_lpar_refcodes"] = lambda _k: []

    _run(world, state)

    refcodes = _observations(state)["lpar.list_refcodes"]
    assert refcodes["result"] == "failed"
    assert refcodes["assertions"] == []


def test_a_delete_whose_response_was_lost_is_judged_by_readback(schemas):
    world, state = World(), _state(schemas)

    def deletes_then_loses(kwargs: dict[str, Any]) -> Any:
        world._hmc_delete_lpar(kwargs)
        return HMCCLIError("connection lost")

    world.overrides["hmc_delete_lpar"] = deletes_then_loses

    _run(world, state)

    delete = _held(state)["lpar.delete"]
    assert delete["result"] == "failed"
    assert delete["unmet"] == "unmet: delete-call-succeeded"
    assert len(world.calls_to("hmc_delete_lpar")) == 1, (
        "no second delete of a gone partition"
    )
    _assert_torn_down(world)


def test_a_transient_name_read_after_a_delete_is_re_read_not_alarmed(schemas):
    world, state = World(), _state(schemas)
    lost = iter([HMCCLIError("ssh: connection reset")])
    world.overrides["hmc_get_lpar_description"] = lambda _k: (
        _DEFAULT if world.created else next(lost, _DEFAULT)
    )

    _run(world, state)

    assert _held(state)["lpar.delete"]["result"] == "passed"
    assert not any(row["subtask"] == 34 for row in state.results)
    _assert_torn_down(world)


def test_a_missing_profile_link_stops_before_assigning_and_deletes(schemas):
    world, state = World(), _state(schemas)
    world.overrides["hmc_get_lpar"] = lambda _k: {
        "UUID": _LPAR_UUID,
        "Resource": {"PartitionName": world.name},
    }

    _run(world, state)

    assert _row(state, "activation profile")["status"] == "FAIL"
    assert "hmc_assign_dedicated_pcie_slot" not in world.tools()
    held = _held(state)
    assert held["lpar.create"]["result"] == "passed"
    assert held["lpar.delete"]["result"] == "passed"
    assert "pcie.assign_dedicated_slot" not in _observations(state)
    _assert_torn_down(world)


def test_the_dedicated_arm_create_still_sends_no_resources(schemas):
    """The shared create sends `resources` only when a caller supplies them."""
    world, state = World(), _state(schemas)
    fixture = pcie._DedicatedFixture(
        config=pcie._DedicatedConfig(_SYSTEM, "live-", "default_profile", _DRC),
        run_marker="pcie-00000000",
        lpar_name="live-pcie-00000000",
        probe_lpar_name="live-pcie-00000000-createtime",
        drc_index=_DRC,
    )

    assert asyncio.run(pcie.create_fixture_partition(FakeClient(world), state, fixture))
    (create,) = world.calls_to("hmc_create_lpar")
    assert "resources" not in create


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------


def test_wrapper_dispatches_its_own_group_through_the_argument_entry_point(monkeypatch):
    seen = []
    monkeypatch.setattr(
        runner, "_run_from_arguments", lambda argv: seen.append(argv) or 0
    )

    assert wrapper.main([]) == 0
    assert seen == [["--group", "bare-cec"]]


def test_wrapper_relays_the_runners_exit_status(monkeypatch):
    monkeypatch.setattr(runner, "_run_from_arguments", lambda _argv: 3)

    assert wrapper.main([]) == 3


def test_wrapper_group_is_one_the_runner_knows():
    assert runner.SUBTASK_GROUPS[wrapper.GROUP] == [25]
    assert runner.SUBTASKS[25] is bare_cec.exercise_bare_cec


def test_wrapper_help_explains_itself_instead_of_starting_a_run(monkeypatch, capsys):
    def forbidden(_argv):
        raise AssertionError("--help started a live run")

    monkeypatch.setattr(runner, "_run_from_arguments", forbidden)

    with pytest.raises(SystemExit) as exit_info:
        wrapper.main(["--help"])

    assert exit_info.value.code == 0
    assert "mutates a managed system" in capsys.readouterr().out


def test_wrapper_rejects_an_unknown_option_before_any_run(monkeypatch):
    def forbidden(_argv):
        raise AssertionError("ran despite an unknown option")

    monkeypatch.setattr(runner, "_run_from_arguments", forbidden)

    with pytest.raises(SystemExit) as exit_info:
        wrapper.main(["--results-file", "x.json"])

    assert exit_info.value.code != 0


def test_record_held_refuses_an_unknown_cleanup_value():
    recorded: list[Any] = []
    state: Any = SimpleNamespace(record=lambda *args: recorded.append(args))

    with pytest.raises(ValueError, match="cleanup disposition is not a known value"):
        bare_cec._record_held(
            state,
            "hmc_delete_lpar (judged)",
            assertions=[Assertion("delete-call-succeeded", True)],
            cleanup="fialed",
            data="",
        )

    assert recorded == []


@pytest.mark.parametrize("cleanup", sorted(CLEANUP))
def test_record_held_judges_cleanup_as_record_verified_does(cleanup):
    recorded: list[Any] = []
    state: Any = SimpleNamespace(record=lambda *args: recorded.append(args))

    bare_cec._record_held(
        state,
        "hmc_delete_lpar (judged)",
        assertions=[Assertion("delete-call-succeeded", True)],
        cleanup=cleanup,
        data="",
    )

    expected = "PASS" if cleanup in {"passed", "not-required"} else "FAIL"
    assert [args[-3] for args in recorded] == [expected]


def test_cli_steps_run_on_fixture_and_keep_mcp_observations(schemas):
    world = World()
    state = _state(schemas)
    _run(world, state)
    rows = [row for row in state.results if "cli_observation" in row]
    assert {row["cli_observation"]["operation"] for row in rows} == {
        "job.get",
        "lpar.get_state",
        "lpar.list_refcodes",
    }
    assert all(row["result"] == "passed" for row in rows)
    assert set(_observations(state)) == _PROMOTED
    _assert_torn_down(world)


def test_cli_failure_keeps_existing_fixture_cleanup(schemas, monkeypatch):
    async def failed(*args):
        raise bare_cec.cli.CLIError("safe test failure")

    monkeypatch.setattr(bare_cec.cli, "execute", failed)
    world = World()
    state = _state(schemas)
    _run(world, state)
    assert any(row["tool"].startswith("CLI ") for row in _rows(state, "FAIL"))
    assert all(
        row["result"] == "failed" for row in state.results if "cli_observation" in row
    )
    _assert_torn_down(world)
