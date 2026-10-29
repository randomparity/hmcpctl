"""Contract tests for the post-run recovery check.

Every case drives the checks through a stub call path. The stub refuses any
tool outside the read-only allowlist, so a mutating call added later fails
every case rather than one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shlex
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import click
import pytest
from conftest import live_fixture
from typer.main import get_command

from hmcpctl import cli
from hmcpctl.client.client_parse import _parse_feed
from hmcpctl.client.client_storage import mapping_lpar_uuid, storage_mapping_id

SCRIPTS_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
import live_test_recovery as recovery  # noqa: E402
from live_test import storage_lifecycle  # noqa: E402
from live_test.observation import CallFailure  # noqa: E402
from live_test.storage import scsi_adapter_listing  # noqa: E402

_MARKER = "pcie-deadbeef"
_SYSTEM = "sys-R1"
_LPAR = f"live-pcie-{_MARKER}"
_DRC = "21010020"
_BASELINE = "none"
#: The one profile read ADR 0165 Decision 1 admits, as a literal.
_PROFILE_READ = f"lssyscfg -r prof -m {_SYSTEM} -F lpar_name,name,io_slots --header"


@pytest.fixture
def unarmed_creation_results(monkeypatch, tmp_path, request):
    """Use the real ST58 arm and runner writer; replace only the MCP boundary."""
    from live_test import config as settings
    from live_test import creation_memory_layout as arm

    from hmcpctl.config import HMCConfig

    @asynccontextmanager
    async def served_client():
        yield object()

    def no_hmc(*_args, **_kwargs):
        pytest.fail("unarmed ST58 must not construct an HMC client")

    monkeypatch.setattr(arm, "HMCClient", no_hmc)
    monkeypatch.setattr(recovery.runner, "served_client", served_client)
    monkeypatch.setattr(recovery.runner, "served_schemas", AsyncMock(return_value={}))
    emitted = []
    original_emit = recovery.runner.evidence.emit_observations

    def emit(state, *args):
        emitted.extend(state.observations)
        return original_emit(state, *args)

    monkeypatch.setattr(recovery.runner.evidence, "emit_observations", emit)
    destination = tmp_path / "results.json"
    config = settings.LiveTestConfig(**getattr(request, "param", {}))
    assert (
        asyncio.run(
            recovery.runner.main(
                group=arm.GROUP,
                results_path=str(destination),
                config=config,
                hmc_config=HMCConfig.from_mapping({}),
            )
        )
        == 0
    )
    assert not emitted
    assert not destination.with_name("results-observations.json").exists()
    document = json.loads(destination.read_text())
    assert set(document) == {"run", "config", "hmc", "artifacts", "results"}
    assert document["artifacts"] == dataclasses.asdict(settings.LiveTestArtifacts())
    assert len(document["results"]) == 1
    assert document["results"][0]["data"] == "None"
    assert document["results"][0]["result"] == "skipped"
    return destination, document


@pytest.mark.parametrize(
    "unarmed_creation_results",
    [
        {},
        {"creation_memory_layout_hpt_ratio": "1:128"},
        {"creation_memory_layout_fresh_vios_delete_authorized": True},
        {
            "creation_memory_layout_fresh_vios_delete_authorized": True,
            "creation_memory_layout_hpt_ratio": "1:128",
        },
        {
            "creation_memory_layout_hpt_ratio": "1:128",
            "creation_memory_layout_ppt_ratio": "1:256",
        },
    ],
    indirect=True,
)
def test_finished_unarmed_creation_recovery_is_skipped(
    unarmed_creation_results, monkeypatch, capsys
):
    destination, document = unarmed_creation_results
    original = destination.read_bytes()
    capsys.readouterr()

    def no_checks(*_args, **_kwargs):
        pytest.fail("unarmed recovery must not bootstrap or read an HMC")

    bootstrap = Mock(return_value=False)
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", bootstrap)
    monkeypatch.setattr(recovery, "_run_checks", no_checks)
    assert recovery.main(["--results", str(destination)]) == 0
    bootstrap.assert_not_called()
    output = capsys.readouterr()
    assert "SKIPPED" in output.out
    assert "no fixture work" in output.out
    assert "not baseline, configured-value or runtime proof" in output.out
    assert "CLEAN" not in output.out + output.err
    assert destination.read_bytes() == original
    assert json.loads(original) == document


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("run", "partial"), True),
        (("run", "partial"), None),
        (("run", "partial"), 0),
        (("run", "partial"), "missing"),
        (("run", "finished"), "missing"),
        (("run", "finished"), None),
        (("run", "finished"), "unfinished"),
        (("run", "finished"), "2026-10-25T12:00:00"),
        (("run", "tested_commit"), "missing"),
        (("run", "tested_commit"), "not-a-commit"),
        (("run", "tree_clean"), "missing"),
        (("run", "schema_version"), None),
        (("run", "detach_probe"), True),
        (("run", "group"), "all"),
        (("run", "subtasks"), [58, 1]),
        (("run", "subtasks"), [58, 58]),
        (("run", "subtasks"), [58.0]),
        (("run",), "missing"),
        (("artifacts", "creation_memory_layout"), "missing"),
        (("artifacts", "creation_memory_layout"), None),
        (("artifacts", "creation_memory_layout"), {"phase": "intent"}),
        (("artifacts", "system_uuid"), "prior-system"),
        (("artifacts", "lp3_baseline"), {"attempted": True}),
        (("artifacts", "test_user_uuid"), "prior-user"),
        (("artifacts",), []),
        (("config", "creation_memory_layout_hpt_ratio"), "bad token"),
        (("config", "creation_memory_layout_ppt_ratio"), "bad token"),
        (("config", "creation_memory_layout_fresh_vios_delete_authorized"), "false"),
        (("config",), {}),
        (("hmc",), None),
        (("hmc", "port"), "443"),
        (("results",), []),
        (("results", 0, "subtask"), 58.0),
        (("results", 0, "tool"), "other"),
        (("results", 0, "status"), "PASS"),
        (("results", 0, "status"), "FAIL"),
        (("results", 0, "data"), None),
        (("results", 0, "data"), {}),
        (("results", 0, "data"), "missing"),
        (("results", 0, "timestamp"), None),
        (("results", 0, "timestamp"), "unfinished"),
        (("results", 0, "extra"), True),
        (
            ("results", 0, "note"),
            "requires named group, HMC configuration and durable checkpoint",
        ),
        (("results", 0, "note"), "prior invocation/restored journal refused"),
        (("results", 0, "note"), ""),
        (("results", 0, "result"), "passed"),
        (("extra",), True),
    ],
)
def test_unarmed_creation_recovery_refuses_incomplete_or_contradictory_record(
    unarmed_creation_results, monkeypatch, capsys, path, value
):
    destination, document = unarmed_creation_results
    changed = deepcopy(document)
    parent = changed
    for key in path[:-1]:
        parent = parent[key]
    if value == "missing":
        del parent[path[-1]]
    else:
        parent[path[-1]] = value
    destination.write_text(json.dumps(changed))
    original = destination.read_bytes()
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "_run_checks", AsyncMock(return_value=[]))
    capsys.readouterr()
    assert recovery.main(["--results", str(destination)]) == 2
    output = capsys.readouterr()
    assert "CLEAN" not in output.out + output.err
    assert "SKIPPED" not in output.out
    assert destination.read_bytes() == original


@pytest.mark.parametrize("fault", ["armed", "extra-skip", "extra-attempt"])
def test_unarmed_creation_recovery_refuses_armed_or_extra_work(
    unarmed_creation_results, monkeypatch, capsys, fault
):
    destination, document = unarmed_creation_results
    if fault == "armed":
        document["config"].update(
            creation_memory_layout_hpt_ratio="1:128",
            creation_memory_layout_ppt_ratio="1:256",
            creation_memory_layout_fresh_vios_delete_authorized=True,
        )
    else:
        row = dict(document["results"][0])
        if fault == "extra-attempt":
            row.update(tool="hmc_create_lpar", status="FAIL", data="unknown")
        document["results"].append(row)
    destination.write_text(json.dumps(document))
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "_run_checks", AsyncMock(return_value=[]))
    capsys.readouterr()
    assert recovery.main(["--results", str(destination)]) == 2
    output = capsys.readouterr()
    assert "CLEAN" not in output.out + output.err
    assert "SKIPPED" not in output.out


def _readback(io_slots: str, profile: str = "default") -> str:
    """The admitted table: the fixture profile's row among rows it must not read.

    The foreign rows list the slot, so selecting one of them reads as drift.
    """
    return (
        "lpar_name,name,io_slots\n"
        f'{_LPAR},{profile},"{io_slots}"\n'
        f'{_LPAR},other_profile,"{_DRC}/none/1"\n'
        f'vios-1,{profile},"{_DRC}/none/1,21030030/none/0"\n'
    )


def _stamped(token: str) -> str:
    """An ADR 0064 ownership stamp as the HMC returns it in a description."""
    return (
        "{'UUID': '11111111-2222-3333-4444-555555555555'} "
        f"[hmcpctl owner:hmcpctl created:2026-09-21] [caller {token}]"
    )


_INPUTS = recovery.RecoveryInputs(
    system_name=_SYSTEM,
    run_marker=_MARKER,
    fixture_lpar=_LPAR,
    drc_index=_DRC,
    baseline_io_slots=_BASELINE,
    profile_name="default",
)


def _caller(responses: dict[str, object], seen: list[str] | None = None):
    """A stub call path that enforces the same guard the real one does."""

    async def call(tool: str, **arguments):
        recovery.guard_read_only(tool, arguments)
        if seen is not None:
            seen.append(tool)
        response = responses.get(tool)
        if response is None:
            return "FAIL", None
        if isinstance(response, CallFailure):
            return "FAIL", response
        return "PASS", response

    return call


#: How the HMC answers a lookup for a partition it does not have: a failing
#: command carrying HSCL8012 (ADR 0162), not a successful empty answer.
_NOT_FOUND = CallFailure(
    "HMCCLIError",
    f"HMCCLIError: HSCL8012 The partition named {_LPAR} was not found.",
    "",
    None,
    False,
)

#: A system with nothing left behind: no partition, slot unowned, profile at
#: its baseline.
_CLEAN = {
    "hmc_get_lpar_description": _NOT_FOUND,
    "hmc_get_lpar_state": "Not Activated",
    "hmc_list_dedicated_pcie_slots": {
        "items": [{"drc_index": _DRC, "owner_lpar": None}]
    },
    "hmc_run_command": _readback(_BASELINE),
}


def _responses(**overrides) -> dict:
    return {**_CLEAN, **overrides}


# ---------------------------------------------------------------------------
# Each stranded condition is detected (Validation 9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clean_system_yields_no_findings():
    assert await recovery.check(_caller(_responses()), _INPUTS) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["not activated", "Not Activated"])
async def test_a_surviving_marked_partition_is_reported(state):
    """Not Activated (#950): `rmsyscfg` alone is a legal remedy, in REST's case or the CLI's."""
    responses = _responses(
        hmc_get_lpar_description=_stamped(_MARKER), hmc_get_lpar_state=state
    )

    findings = await recovery.check(_caller(responses), _INPUTS)

    assert [f.what for f in findings] == ["surviving partition"]
    assert _LPAR in findings[0].detail
    assert "rmsyscfg" in findings[0].remedy
    assert "chsysstate" not in findings[0].remedy


@pytest.mark.asyncio
async def test_a_running_surviving_partition_needs_a_shutdown_first():
    """Running (#950): the HMC refuses `rmsyscfg` outside Not Activated."""
    responses = _responses(
        hmc_get_lpar_description=_stamped(_MARKER), hmc_get_lpar_state="Running"
    )

    findings = await recovery.check(_caller(responses), _INPUTS)

    assert [f.what for f in findings] == ["surviving partition"]
    remedy = findings[0].remedy
    assert "chsysstate" in remedy
    assert "shutdown --immed" in remedy
    assert remedy.index("chsysstate") < remedy.index("rmsyscfg")


@pytest.mark.asyncio
async def test_an_unreadable_state_on_a_surviving_partition_is_not_clean():
    """A remedy cannot be chosen without knowing the state (#950)."""
    responses = _responses(
        hmc_get_lpar_description=_stamped(_MARKER), hmc_get_lpar_state=None
    )

    with pytest.raises(recovery.StateUnreadable, match="could not read the state"):
        await recovery.check(_caller(responses), _INPUTS)


@pytest.mark.asyncio
async def test_a_partition_of_the_same_name_from_another_run_is_not_claimed():
    """The marker is the ownership proof; a name collision is not this run's."""
    responses = _responses(hmc_get_lpar_description=_stamped("pcie-someoneelse"))

    assert await recovery.check(_caller(responses), _INPUTS) == []


@pytest.mark.asyncio
async def test_this_runs_marker_in_a_stamp_this_checkout_cannot_read_is_not_clean():
    """A run on pre-rename code stamped `[hmc-mcp ...]`, which this code reads as
    unowned. Returning "not ours" there reported CLEAN while the partition survived
    (#899 review). The marker is per-run random, so seeing it is enough to refuse.
    """
    old_stamp = f"[hmc-mcp owner:hmc-mcp created:2026-09-21] [caller {_MARKER}]"
    responses = _responses(hmc_get_lpar_description=old_stamp)

    with pytest.raises(recovery.StateUnreadable, match="tested commit"):
        await recovery.check(_caller(responses), _INPUTS)


@pytest.mark.asyncio
async def test_a_stranded_slot_is_reported():
    responses = _responses(
        hmc_list_dedicated_pcie_slots={
            "items": [{"drc_index": _DRC, "owner_lpar": _LPAR}]
        }
    )

    findings = await recovery.check(_caller(responses), _INPUTS)

    assert [f.what for f in findings] == ["stranded slot"]
    assert _DRC in findings[0].detail
    assert "io_slots-" in findings[0].remedy


@pytest.mark.asyncio
async def test_a_slot_owned_by_the_literal_string_null_reads_as_unowned():
    """The HMC spells an unowned slot `null` in some responses."""
    responses = _responses(
        hmc_list_dedicated_pcie_slots={
            "items": [{"drc_index": _DRC, "owner_lpar": "null"}]
        }
    )

    assert await recovery.check(_caller(responses), _INPUTS) == []


@pytest.mark.asyncio
async def test_profile_drift_from_the_baseline_is_reported():
    """Drift is only reachable while the fixture survives: see `_surviving`."""
    responses = _responses(
        hmc_get_lpar_description=_stamped(_MARKER),
        hmc_run_command=_readback(f"{_DRC}/none/0"),
    )

    findings = await recovery.check(_caller(responses), _INPUTS)

    drift = [f for f in findings if f.what == "profile drift"]
    assert len(drift) == 1
    assert "still listed" in drift[0].detail
    assert _BASELINE in drift[0].remedy


@pytest.mark.asyncio
async def test_profile_drift_to_an_unrelated_value_is_still_reported():
    """Drift is drift; the arm's own Guard B refuses on any mismatch."""
    responses = _responses(
        hmc_get_lpar_description=_stamped(_MARKER),
        hmc_run_command=_readback("21030030/none/0"),
    )

    findings = await recovery.check(_caller(responses), _INPUTS)

    drift = [f for f in findings if f.what == "profile drift"]
    assert len(drift) == 1
    assert "still listed" not in drift[0].detail


@pytest.mark.asyncio
async def test_a_deleted_fixture_is_not_asked_for_its_profile():
    """A profile dies with its partition; asking answers HSCL8012.

    Without this gate every clean run would read that refusal as an unreadable
    system and exit 2.
    """
    seen: list[str] = []

    findings = await recovery.check(_caller(_responses(), seen), _INPUTS)

    assert findings == []
    assert "hmc_run_command" not in seen


@pytest.mark.asyncio
async def test_every_condition_at_once_is_reported_together():
    responses = _responses(
        hmc_get_lpar_description=_stamped(_MARKER),
        hmc_list_dedicated_pcie_slots={
            "items": [{"drc_index": _DRC, "owner_lpar": _LPAR}]
        },
        hmc_run_command=_readback(f"{_DRC}/none/0"),
    )

    findings = await recovery.check(_caller(responses), _INPUTS)

    assert [f.what for f in findings] == [
        "surviving partition",
        "stranded slot",
        "profile drift",
    ]


@pytest.mark.asyncio
async def test_an_unreadable_profile_is_not_clean():
    """A failed read is not evidence of a clean profile. Exit 2, not exit 0."""
    responses = _responses(
        hmc_get_lpar_description=_stamped(_MARKER), hmc_run_command=None
    )

    with pytest.raises(recovery.StateUnreadable, match="could not read profile"):
        await recovery.check(_caller(responses), _INPUTS)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        pytest.param(
            f"lpar_name,name,io_slots\n{_LPAR},default,none\n{_LPAR},default,none\n",
            "2 rows",
            id="two-rows-for-the-profile",
        ),
        pytest.param(
            f"lpar_name,name,io_slots\n{_LPAR},other_profile,none\n",
            "0 rows",
            id="no-row-for-the-profile",
        ),
        pytest.param("none\n", "header", id="headerless"),
        pytest.param(_readback(f"{_DRC}//0"), "unadmitted io_slots", id="empty-pool"),
    ],
)
@pytest.mark.asyncio
async def test_a_profile_answer_outside_the_admitted_form_is_unreadable(answer, reason):
    responses = _responses(
        hmc_get_lpar_description=_stamped(_MARKER), hmc_run_command=answer
    )

    with pytest.raises(recovery.StateUnreadable, match=reason):
        await recovery.check(_caller(responses), _INPUTS)


@pytest.mark.asyncio
async def test_an_unreadable_system_carries_the_findings_already_confirmed():
    """Exit 2 must not swallow what was found before the read failed."""
    responses = _responses(
        hmc_get_lpar_description=_stamped(_MARKER), hmc_run_command=None
    )

    with pytest.raises(recovery.StateUnreadable) as raised:
        await recovery.check(_caller(responses), _INPUTS)

    assert [f.what for f in raised.value.findings] == ["surviving partition"]


@pytest.mark.asyncio
async def test_a_partition_lookup_failing_for_another_reason_is_not_clean():
    """Only HSCL8012 means "no such partition"; any other failure is unreadable.

    Reading every failed lookup as "gone" reported a system clean after, say, an
    authentication refusal, while a partition carrying this run's marker survived.
    """
    lost = CallFailure("HMCCLIError", "HMCCLIError: connection lost", "", None, False)
    responses = _responses(hmc_get_lpar_description=lost)

    with pytest.raises(recovery.StateUnreadable, match="could not look up"):
        await recovery.check(_caller(responses), _INPUTS)


@pytest.mark.asyncio
async def test_an_unreadable_partition_lookup_keeps_the_stranded_slot():
    lost = CallFailure("HMCCLIError", "HMCCLIError: connection lost", "", None, False)
    responses = _responses(
        hmc_get_lpar_description=lost,
        hmc_list_dedicated_pcie_slots={
            "items": [{"drc_index": _DRC, "owner_lpar": "someone"}]
        },
    )

    with pytest.raises(recovery.StateUnreadable) as raised:
        await recovery.check(_caller(responses), _INPUTS)

    assert [f.what for f in raised.value.findings] == ["stranded slot"]


@pytest.mark.asyncio
async def test_a_partition_lookup_answering_no_description_is_not_clean():
    responses = _responses(hmc_get_lpar_description={"unexpected": "shape"})

    with pytest.raises(recovery.StateUnreadable, match="could not look up"):
        await recovery.check(_caller(responses), _INPUTS)


@pytest.mark.asyncio
async def test_an_unlistable_system_is_not_clean():
    """The slot listing is the reachability probe; failing it is never clean."""
    responses = _responses(hmc_list_dedicated_pcie_slots=None)

    with pytest.raises(recovery.StateUnreadable, match="could not list"):
        await recovery.check(_caller(responses), _INPUTS)


@pytest.mark.asyncio
async def test_a_run_with_no_drc_index_still_probes_then_checks_the_partition():
    inputs = recovery.RecoveryInputs(_SYSTEM, _MARKER, _LPAR, None, None, "default")
    seen: list[str] = []

    await recovery.check(_caller(_responses(), seen), inputs)

    assert seen == ["hmc_list_dedicated_pcie_slots", "hmc_get_lpar_description"]


# ---------------------------------------------------------------------------
# No mutating call is ever issued (Validation 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_tool_the_checks_call_is_on_the_read_only_allowlist():
    seen: list[str] = []

    await recovery.check(_caller(_responses(), seen), _INPUTS)

    assert set(seen) <= recovery._READ_ONLY_TOOLS
    assert seen  # the checks did run


@pytest.mark.parametrize(
    "tool", ["hmc_delete_lpar", "hmc_create_lpar", "hmc_assign_dedicated_slot"]
)
def test_a_mutating_tool_is_refused(tool):
    with pytest.raises(recovery.MutatingCallRefused, match="read-only allowlist"):
        recovery.guard_read_only(tool, {})


@pytest.mark.parametrize(
    "command",
    ["chsyscfg -r prof -i x", "rmsyscfg -r lpar -n x", "mksyscfg -r lpar", ""],
)
def test_run_command_is_refused_for_anything_but_lssyscfg(command):
    """`hmc_run_command` shares one tool between reads and mutations."""
    with pytest.raises(recovery.MutatingCallRefused, match="read-only only for"):
        recovery.guard_read_only("hmc_run_command", {"cmd": command})


def test_run_command_is_allowed_for_lssyscfg():
    recovery.guard_read_only("hmc_run_command", {"cmd": _PROFILE_READ})


@pytest.mark.asyncio
async def test_the_guard_is_on_the_call_path_not_only_in_review():
    """A check that reached for a mutation would raise, not silently succeed."""

    async def call(tool: str, **arguments):
        recovery.guard_read_only(tool, arguments)
        return "PASS", None

    with pytest.raises(recovery.MutatingCallRefused):
        await call("hmc_delete_lpar", lpar_name_or_uuid=_LPAR)


# ---------------------------------------------------------------------------
# The profile read is the arm's own, not a second spelling of it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_profile_read_is_the_admitted_form_selected_by_profile_name():
    """ADR 0165 Decision 1: three fields, `--header`, no `--filter`.

    A partition may carry several profiles, so the fixture's row is chosen by
    `lpar_name` and `name` both. A real VIOS partition with two profiles is
    where selecting on the partition alone surfaced.
    """
    sent: list[str] = []

    async def call(tool: str, **arguments):
        recovery.guard_read_only(tool, arguments)
        if tool == "hmc_run_command":
            sent.append(arguments["cmd"])
            return "PASS", _readback(_BASELINE, profile="fixture-profile")
        return "PASS", _CLEAN[tool]

    inputs = recovery.RecoveryInputs(
        _SYSTEM, _MARKER, _LPAR, _DRC, _BASELINE, "fixture-profile"
    )

    assert await recovery._profile_drift(call, inputs) is None
    assert sent == [_PROFILE_READ]


def test_recovery_reads_the_profile_with_the_arms_own_builders():
    """One definition of the admitted read, so the two cannot drift apart."""
    from live_test import dedicated_pcie

    assert (
        recovery.profile_io_slot_rows_command
        is dedicated_pcie.profile_io_slot_rows_command
    )
    assert recovery.select_profile_io_slots is dedicated_pcie.select_profile_io_slots


def test_an_unset_profile_name_falls_back_to_the_arms_default():
    """The run records an empty string; the fallback must be the arm's.

    A second spelling would query, and tell the operator to repair, a profile
    the arm never touched.
    """
    from live_test import dedicated_pcie

    document = _document()
    document["config"]["dedicated_pcie_profile_name"] = ""

    inputs = recovery.inputs_from_document(document)

    assert inputs.profile_name == dedicated_pcie._DEFAULT_DEDICATED_PROFILE


# ---------------------------------------------------------------------------
# The allowlisted tools exist on the server the checks actually talk to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_read_only_tool_is_registered_on_the_composed_server():
    """An allowlist of tools the server does not serve guards nothing.

    `hmc_run_command` is an opt-in escape hatch. Composing without it left the
    profile-drift check calling a tool that answered "Unknown tool", which the
    check then read as no drift — a clean verdict on an unexamined system.
    Every case above stubs the call path, so only this one sees it.
    """
    async with recovery.runner.served_client() as client:
        registered = {tool.name for tool in await client.list_tools()}

    assert recovery._READ_ONLY_TOOLS <= registered


@pytest.mark.asyncio
async def test_the_checks_run_through_the_live_runs_served_client(monkeypatch):
    """The test above holds only if the checks use the very client it inspects."""
    served = object()
    seen = []

    @asynccontextmanager
    async def served_client():
        yield served

    async def no_findings(call, checks, *, users=False):
        return []

    monkeypatch.setattr(recovery.runner, "served_client", served_client)
    monkeypatch.setattr(
        recovery, "_read_only_caller", lambda client, state: seen.append(client)
    )
    monkeypatch.setattr(recovery, "check_run", no_findings)

    assert await recovery._run_checks(((recovery.check, _INPUTS),)) == []
    assert seen == [served]


# ---------------------------------------------------------------------------
# Inputs come from the results document
# ---------------------------------------------------------------------------


def _document(**artifact_overrides) -> dict:
    return {
        "run": {"subtasks": [24]},
        "config": {
            "dedicated_pcie_system_name": _SYSTEM,
            "dedicated_pcie_profile_name": "default",
        },
        "artifacts": {
            "pcie_run_marker": _MARKER,
            "pcie_fixture_lpar": _LPAR,
            "pcie_drc_index": _DRC,
            "pcie_baseline_io_slots": _BASELINE,
            **artifact_overrides,
        },
    }


def test_inputs_are_read_from_the_documents_artifact_fields():
    inputs = recovery.inputs_from_document(_document())

    assert inputs == _INPUTS


def test_a_document_recording_no_fixture_yields_no_inputs():
    """A run that never reached ST29 created nothing to look for."""
    assert recovery.inputs_from_document(_document(pcie_run_marker=None)) is None


@pytest.mark.parametrize(
    "document", ["not a dict", {}, {"artifacts": {}}, {"config": {}}, []]
)
def test_a_malformed_document_yields_no_inputs(document):
    assert recovery.inputs_from_document(document) is None


def test_a_run_that_stopped_before_the_baseline_still_yields_inputs():
    """The ST29 fields alone are enough to look for a surviving partition."""
    inputs = recovery.inputs_from_document(_document(pcie_baseline_io_slots=None))

    assert inputs is not None
    assert inputs.run_marker == _MARKER
    assert inputs.baseline_io_slots is None


def test_a_document_with_no_fixture_exits_zero_without_contacting_the_hmc(
    tmp_path, monkeypatch, capsys
):
    def forbidden():
        raise AssertionError("bootstrapped credentials with nothing to check")

    monkeypatch.setattr(recovery.runner, "_bootstrap_config", forbidden)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(_document(pcie_run_marker=None)), encoding="utf-8")

    assert recovery.main(["--results", str(path)]) == 0
    assert "CLEAN" in capsys.readouterr().out


def test_an_unreadable_document_exits_two_not_zero(tmp_path, capsys):
    """Could-not-determine is not clean."""
    assert recovery.main(["--results", str(tmp_path / "absent.json")]) == 2
    assert "cannot read" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_a_clean_report_names_the_marker_it_matched_on(capsys):
    recovery._report(_document(), [], [], [])

    output = capsys.readouterr().out
    assert "CLEAN" in output
    assert _MARKER in output


def test_a_stranded_report_prints_the_command_and_disclaims_acting(capsys):
    finding = recovery.Finding("stranded slot", "detail here", "hmc chsyscfg ...")

    recovery._report(_document(), [finding], [], [])

    output = capsys.readouterr().out
    assert "STRANDED" in output
    assert "hmc chsyscfg ..." in output
    assert "issues no mutating call" in output
    assert "CLEAN" not in output


# ---------------------------------------------------------------------------
# The read-only guard admits the test-partition reads and nothing more
# ---------------------------------------------------------------------------

_TEST_SYSTEM = "sys-E2"
_TEST_LPAR = "lt-lpar"
_VIOS = "vios-uuid-1"
_VIOS_ID = 2
_VG = "vg-uuid-1"
_ISO = "lt.iso"
_BLANK = "hmcpctl_live_0a1b2c3d"
_LISTING = (
    f"lshwres -r virtualio --rsubtype scsi -m {_TEST_SYSTEM} --level lpar "
    f"--filter lpar_ids={_VIOS_ID} -F slot_num,remote_lpar_name,remote_slot_num"
)


def test_run_command_is_allowed_for_lshwres():
    recovery.guard_read_only("hmc_run_command", {"cmd": _LISTING})


def test_the_vmedia_adapter_listings_pass_the_read_only_guard():
    """ST19 reads both adapter listings through the command recovery admits."""
    for attribute, value in (("lpar_ids", _VIOS_ID), ("lpar_names", _TEST_LPAR)):
        recovery.guard_read_only(
            "hmc_run_command",
            {"cmd": scsi_adapter_listing(_TEST_SYSTEM, attribute, value)},
        )


@pytest.mark.parametrize("character", list(";|&$`<>()") + ["\n"])
def test_run_command_refuses_a_shell_metacharacter(character):
    """A command built from a results document must not chain a second one."""
    with pytest.raises(recovery.MutatingCallRefused, match="metacharacter"):
        recovery.guard_read_only(
            "hmc_run_command", {"cmd": f"lshwres -m x{character}rmsyscfg"}
        )


def test_every_allowlisted_tool_is_a_read():
    """`hmc_run_command` is the one exception, bounded by its command prefix."""
    from hmcpctl.server import TOOL_SECURITY

    for tool in recovery._READ_ONLY_TOOLS - {"hmc_run_command"}:
        assert TOOL_SECURITY[tool].effect == "read", tool


def test_every_mutating_vmedia_call_has_a_trigger():
    """A new mutating vMedia call must not land outside every witnessed class."""
    import re

    from hmcpctl.server import TOOL_SECURITY

    source = (SCRIPTS_ROOT / "live_test" / "vmedia.py").read_text(encoding="utf-8")
    # Every tool-name literal, not only `state.call(client, ...)`: ST19 calls
    # through a helper that takes the name as its first argument.
    named = set(re.findall(r'"(hmc_\w+)"', source)) & set(TOOL_SECURITY)
    # The arm sends only `scsi_adapter_listing`'s lshwres through it, pinned below.
    named.discard("hmc_run_command")
    mutating = {tool for tool in named if TOOL_SECURITY[tool].effect != "read"}

    assert mutating
    assert mutating <= recovery._VMEDIA_MUTATIONS


# ---------------------------------------------------------------------------
# Which test-partition classes a document makes applicable
# ---------------------------------------------------------------------------


def _row(subtask: int, tool: str, status: str = "PASS", data=None) -> dict:
    return {"subtask": subtask, "tool": tool, "status": status, "data": data}


def _lpar_document(subtasks, rows=(), **artifacts) -> dict:
    return {
        "run": {"subtasks": list(subtasks), "group": "vmedia"},
        "config": {
            "system_name": _TEST_SYSTEM,
            "lp3_name": _TEST_LPAR,
            "iso_media_name": _ISO,
        },
        "artifacts": {
            "vios_uuid": _VIOS,
            "vios_partition_id": _VIOS_ID,
            "vg_uuid": _VG,
            "vmedia_repo_created": False,
            "vmedia_iso_name": None,
            "vmedia_orig_boot_order": [],
            **artifacts,
        },
        "results": list(rows),
    }


_VMEDIA_ROWS = (
    _row(16, "hmc_create_media_repository"),
    _row(19, "hmc_mount_optical_media"),
    _row(
        20, "hmc_read_lpar_boot_order (baseline)", data={"pending_boot_string": "/a /b"}
    ),
    _row(20, "hmc_set_lpar_boot_order (boot device list)"),
    _row(20, "hmc_power_on_lpar"),
)


def _inputs(document):
    return recovery.lpar_inputs_from_document(
        document, recovery.dispatched_subtasks(document)
    )


def test_a_vmedia_document_makes_every_class_applicable():
    inputs = _inputs(
        _lpar_document(
            range(16, 23),
            _VMEDIA_ROWS,
            vmedia_iso_name="lt_0a1b2c3d.iso",
            vmedia_blank_name=_BLANK,
        )
    )

    assert inputs.vmedia_ran and inputs.repository_owned and inputs.powered_on
    assert inputs.boot_written and inputs.boot_baseline == "/a /b"
    assert inputs.iso_names == {"lt_0a1b2c3d.iso", _BLANK}
    assert not inputs.provisioned


@pytest.mark.parametrize(
    "name", [_ISO, "operator.iso", "hmcpctl_live_xyz", "lt_0A1B2C3D.iso"]
)
def test_only_a_run_tagged_medium_is_the_runs(name):
    """The configured ISO name, or a name an older arm recorded, may be an operator's."""
    inputs = _inputs(_lpar_document(range(16, 23), _VMEDIA_ROWS, vmedia_iso_name=name))

    assert inputs.iso_names == frozenset()


def test_a_teardown_only_run_reads_the_ownership_it_restored():
    """ST22 acts on artifacts an earlier invocation recorded (subset restore)."""
    document = _lpar_document(
        [22],
        [_row(22, "hmc_set_lpar_boot_order (boot order restore guard)", "FAIL")],
        vmedia_repo_created=True,
        vmedia_orig_boot_order=["/a", "/b"],
    )

    inputs = _inputs(document)

    assert inputs.vmedia_ran and inputs.repository_owned and inputs.boot_written
    assert inputs.boot_baseline == "/a /b"
    assert not inputs.powered_on


def test_media_calls_do_not_make_the_repository_the_runs():
    """The arm writes its own media into an operator's repository (#1347)."""
    rows = [
        _row(18, "hmc_upload_iso (http)"),
        _row(19, "hmc_create_optical_media (blank)"),
        _row(19, "hmc_delete_optical_media (blank)"),
    ]

    assert not _inputs(_lpar_document([18, 19], rows)).repository_owned


def test_the_repository_group_falls_back_for_an_older_document():
    newer = _inputs(_lpar_document([19], vmedia_vg_uuid="vg-uuid-2"))
    older = _inputs(_lpar_document([19]))

    assert newer.vg_uuid == "vg-uuid-2"
    assert older.vg_uuid == _VG


def test_a_skipped_power_on_was_never_made_and_a_failed_one_was():
    skipped = _lpar_document([20], [_row(20, "hmc_power_on_lpar", "SKIP")])
    failed = _lpar_document([20], [_row(20, "hmc_power_on_lpar", "FAIL")])

    assert not _inputs(skipped).powered_on
    assert _inputs(failed).powered_on


def test_a_bare_cec_power_on_is_not_the_test_partitions():
    document = _lpar_document(
        [25], [_row(25, "hmc_power_on_lpar (no partition profile)")]
    )

    assert _inputs(document) is None


def test_a_round2_provision_triggers_only_the_adapter_class():
    document = _lpar_document(range(16), [_row(14, "hmc_provision_lpar (live)")])

    inputs = _inputs(document)

    assert inputs.provisioned
    assert not (inputs.vmedia_ran or inputs.repository_owned or inputs.powered_on)
    assert not inputs.boot_written


def test_a_dedicated_document_yields_no_partition_inputs():
    assert _inputs(_document()) is None


@pytest.mark.parametrize(
    "run",
    [None, {}, {"subtasks": "16"}, {"subtasks": [16, "17"]}, {"subtasks": [True]}],
)
def test_unrecorded_subtasks_are_unknown(run):
    document = _lpar_document([16])
    document["run"] = run

    assert recovery.dispatched_subtasks(document) is None


# ---------------------------------------------------------------------------
# Each test-partition class is detected, and clean when it is not stranded
# ---------------------------------------------------------------------------


def _optical(media: str) -> dict:
    return {
        "Storage": {"VirtualOpticalMedia": {"MediaName": media}},
        "AssociatedLogicalPartition": {"href": "https://hmc/LogicalPartition/LP-1"},
    }


_LPAR_CLEAN = {
    "hmc_list_optical_mappings": [_optical("operator.iso")],
    "hmc_run_command": f"5,{_TEST_LPAR},3\n6,other-lpar,3\n",
    "hmc_list_storage_mappings": [{"id": "vhost0/vtscsi0"}],
    "hmc_get_media_repository": None,
    "hmc_list_optical_media": [{"name": "operator.iso", "size_mib": 1024}],
    "hmc_get_lpar_state": "not activated",
    "hmc_read_lpar_boot_order": {"pending_boot_string": "/a  /b"},
}


def _lpar_responses(**overrides) -> dict:
    return {**_LPAR_CLEAN, **overrides}


def _lpar_caller(responses: dict, seen: list | None = None):
    """Like `_caller`, but a `None` response is an answer, not a failure."""

    async def call(tool: str, **arguments):
        recovery.guard_read_only(tool, arguments)
        if seen is not None:
            seen.append((tool, arguments))
        if tool not in responses:
            return "FAIL", None
        response = responses[tool]
        if isinstance(response, CallFailure):
            return "FAIL", response
        return "PASS", response

    return call


_ALL = _inputs(_lpar_document(range(16, 23), _VMEDIA_ROWS, vmedia_blank_name=_BLANK))


@pytest.mark.asyncio
async def test_a_clean_test_partition_yields_no_findings():
    seen: list = []

    assert (
        await recovery.check_test_partition(_lpar_caller(_LPAR_CLEAN, seen), _ALL) == []
    )
    assert {tool for tool, _ in seen} == set(_LPAR_CLEAN)


@pytest.mark.asyncio
async def test_the_adapter_listing_is_the_1237_command_by_vios_id():
    seen: list = []

    await recovery.check_test_partition(_lpar_caller(_LPAR_CLEAN, seen), _ALL)

    assert ("hmc_run_command", {"cmd": _LISTING}) in seen


@pytest.mark.parametrize(
    ("overrides", "what", "remedy"),
    [
        (
            {"hmc_list_optical_mappings": [_optical(_BLANK)]},
            "optical mapping left",
            f"hmcpctl storage unmount-optical-media {_VIOS} --lpar {_TEST_LPAR} --name {_BLANK}",
        ),
        (
            {"hmc_list_optical_media": [{"name": _BLANK, "size_mib": 1024}]},
            "run media left",
            f"hmcpctl storage delete-media {_VIOS} --vg {_VG} --name {_BLANK}",
        ),
        (
            {"hmc_run_command": f"5,{_TEST_LPAR},3\n7,{_TEST_LPAR},4\n"},
            "unmapped server adapter",
            f"-o r --id {_VIOS_ID} -s <slot>",
        ),
        (
            {"hmc_get_media_repository": {"RepositoryName": "VMLibrary"}},
            "media repository left",
            f"hmcpctl storage delete-media-repo {_VIOS} --vg {_VG}",
        ),
        (
            {"hmc_get_lpar_state": "running"},
            "test partition running",
            f"hmcpctl lpars power-off {_TEST_LPAR} --system {_TEST_SYSTEM}",
        ),
        (
            {"hmc_read_lpar_boot_order": {"pending_boot_string": "/b /a"}},
            "boot string drift",
            f"hmcpctl lpars set-boot-order {_TEST_SYSTEM} {_TEST_LPAR} /a /b",
        ),
        (
            {"hmc_read_lpar_boot_order": {"pending_boot_string": None}},
            "boot string drift",
            "set-boot-order",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_stranded_class_is_reported_with_its_remedy(overrides, what, remedy):
    findings = await recovery.check_test_partition(
        _lpar_caller(_lpar_responses(**overrides)), _ALL
    )

    assert [finding.what for finding in findings] == [what]
    assert remedy in findings[0].remedy
    if remedy.startswith("hmcpctl storage"):
        root = get_command(cli.app)
        storage = root.commands["storage"]
        for command in (
            findings[0].remedy.replace("<each ISO>", "install.iso").split("; ")
        ):
            tokens = shlex.split(command)
            leaf = storage.get_command(click.Context(storage), tokens[2])
            assert leaf is not None
            with leaf.make_context(tokens[2], tokens[3:]):
                pass


@pytest.mark.asyncio
async def test_no_listed_adapter_reads_as_zero():
    responses = _lpar_responses(
        hmc_run_command="No results were found.", hmc_list_storage_mappings=[]
    )

    assert await recovery.check_test_partition(_lpar_caller(responses), _ALL) == []


@pytest.mark.asyncio
async def test_the_captured_unmapped_adapter_is_reported_by_slot():
    """The #1250 capture: two listed adapters toward the partition, one mapped.

    The mappings are the ones REST links to the partition in the same window, so the
    adapter-only mapping it also carried is excluded exactly as the tool excludes it.
    """
    listing = live_fixture("cli-vios-scsi-unmapped-adapter")
    capture = live_fixture("rest-vios-scsi-mapping-unmapped-adapter")
    resource = _parse_feed(capture["body"], capture["path"])[0]["Resource"]
    linked = [
        {"id": storage_mapping_id(mapping)}
        for mapping in resource["VirtualSCSIMappings"]["VirtualSCSIMapping"]
        if mapping_lpar_uuid(mapping) == "00000002-ABCD-4EF0-8ABC-000000000002"
    ]
    responses = _lpar_responses(
        hmc_run_command=listing["stdout"], hmc_list_storage_mappings=linked
    )
    inputs = dataclasses.replace(_ALL, lpar_name="sys-R1-lp3")

    findings = await recovery.check_test_partition(_lpar_caller(responses), inputs)

    assert [(finding.what, finding.detail) for finding in findings] == [
        (
            "unmapped server adapter",
            (
                f"VIOS {_VIOS_ID} has 2 vSCSI server adapter(s) toward sys-R1-lp3 "
                "(slots 3, 5) but 1 mapped"
            ),
        )
    ]


@pytest.mark.asyncio
async def test_a_class_whose_trigger_is_absent_makes_no_call():
    seen: list = []
    inputs = _inputs(_lpar_document(range(16), [_row(14, "hmc_provision_lpar (live)")]))

    await recovery.check_test_partition(_lpar_caller(_LPAR_CLEAN, seen), inputs)

    assert {tool for tool, _ in seen} == {
        "hmc_run_command",
        "hmc_list_storage_mappings",
    }


@pytest.mark.parametrize(
    "tool",
    [
        "hmc_list_optical_mappings",
        "hmc_run_command",
        "hmc_list_storage_mappings",
        "hmc_get_media_repository",
        "hmc_list_optical_media",
        "hmc_get_lpar_state",
        "hmc_read_lpar_boot_order",
    ],
)
@pytest.mark.asyncio
async def test_each_failed_read_is_unreadable(tool):
    responses = {key: value for key, value in _LPAR_CLEAN.items() if key != tool}

    with pytest.raises(recovery.StateUnreadable):
        await recovery.check_test_partition(_lpar_caller(responses), _ALL)


@pytest.mark.parametrize(
    ("artifact", "value", "message"),
    [
        ("vios_uuid", None, "vios_uuid"),
        ("vg_uuid", None, "vg_uuid"),
        ("vios_partition_id", "2", "vios_partition_id"),
    ],
)
@pytest.mark.asyncio
async def test_a_missing_document_field_is_unreadable(artifact, value, message):
    inputs = _inputs(_lpar_document(range(16, 23), _VMEDIA_ROWS, **{artifact: value}))

    with pytest.raises(recovery.StateUnreadable, match=message):
        await recovery.check_test_partition(_lpar_caller(_LPAR_CLEAN), inputs)


@pytest.mark.asyncio
async def test_a_boot_write_with_no_baseline_is_unreadable():
    inputs = _inputs(
        _lpar_document([22], [_row(22, "hmc_set_lpar_boot_order (guard)", "PASS")])
    )

    with pytest.raises(recovery.StateUnreadable, match="baseline"):
        await recovery.check_test_partition(_lpar_caller(_LPAR_CLEAN), inputs)


@pytest.mark.asyncio
async def test_a_mapping_without_an_adapter_id_is_unreadable_and_names_the_slots():
    responses = _lpar_responses(hmc_list_storage_mappings=[{"id": None}])

    with pytest.raises(recovery.StateUnreadable, match="slots 5"):
        await recovery.check_test_partition(_lpar_caller(responses), _ALL)


@pytest.mark.parametrize("listing", ["5", "x,lt-lpar", "5;lt-lpar"])
@pytest.mark.asyncio
async def test_a_malformed_adapter_listing_is_unreadable(listing):
    responses = _lpar_responses(hmc_run_command=listing)

    with pytest.raises(recovery.StateUnreadable, match="server adapter"):
        await recovery.check_test_partition(_lpar_caller(responses), _ALL)


@pytest.mark.asyncio
async def test_an_optical_entry_it_cannot_read_is_unreadable():
    responses = _lpar_responses(hmc_list_optical_mappings=[{"Storage": {}}])

    with pytest.raises(recovery.StateUnreadable, match="optical"):
        await recovery.check_test_partition(_lpar_caller(responses), _ALL)


@pytest.mark.asyncio
async def test_an_unreadable_class_does_not_stop_the_others():
    responses = _lpar_responses(
        hmc_get_media_repository={"RepositoryName": "VMLibrary"},
    )
    del responses["hmc_list_optical_mappings"]

    with pytest.raises(recovery.StateUnreadable) as raised:
        await recovery.check_test_partition(_lpar_caller(responses), _ALL)

    assert [finding.what for finding in raised.value.findings] == [
        "media repository left"
    ]


@pytest.mark.asyncio
async def test_pcie_findings_survive_an_unreadable_partition_class():
    responses = {
        **_responses(
            hmc_list_dedicated_pcie_slots={
                "items": [{"drc_index": _DRC, "owner_lpar": "someone"}]
            }
        ),
        **{
            key: value for key, value in _LPAR_CLEAN.items() if key != "hmc_run_command"
        },
        "hmc_get_lpar_state": "Not Activated",
    }
    responses["hmc_run_command"] = _readback(_BASELINE)

    with pytest.raises(recovery.StateUnreadable) as raised:
        await recovery.check_run(
            _lpar_caller(responses),
            (
                (recovery.check, _INPUTS),
                (recovery.check_test_partition, _ALL),
            ),
        )

    assert "stranded slot" in [finding.what for finding in raised.value.findings]


# ---------------------------------------------------------------------------
# main: exit codes and what the report names
# ---------------------------------------------------------------------------


def _main(tmp_path, monkeypatch, document, findings=None, raises=None):
    """Run `main` over *document*; return (exit code, whether the HMC was contacted)."""
    contacted = []

    async def run_checks(checks, *, users=False):
        pcie = dict(checks).get(recovery.check)
        partition = dict(checks).get(recovery.check_test_partition)
        lpar_config = dict(checks).get(recovery.check_lpar_config)
        contacted.append((pcie, partition, users, lpar_config))
        if raises is not None:
            raise raises
        return findings or []

    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "_run_checks", run_checks)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return recovery.main(["--results", str(path)]), bool(contacted)


_FINDING = recovery.Finding("test partition running", "detail", "hmcpctl ...")


def test_a_clean_vmedia_run_exits_zero(tmp_path, monkeypatch, capsys):
    document = _lpar_document(range(16, 23), _VMEDIA_ROWS)

    assert _main(tmp_path, monkeypatch, document) == (0, True)
    assert "CLEAN" in capsys.readouterr().out


def test_a_stranded_vmedia_run_exits_one(tmp_path, monkeypatch):
    document = _lpar_document(range(16, 23), _VMEDIA_ROWS)

    assert _main(tmp_path, monkeypatch, document, [_FINDING]) == (1, True)


def test_a_round2_run_is_not_witnessed_and_exits_two(tmp_path, monkeypatch, capsys):
    """Its partition and network changes are checked by hand."""
    document = _lpar_document(n for n in range(16) if n != 11)

    assert _main(tmp_path, monkeypatch, document) == (2, False)
    output = capsys.readouterr().out
    assert "NOT WITNESSED" in output
    assert "CLEAN" not in output


def test_a_finding_beside_unwitnessed_subtasks_exits_two_and_prints_both(
    tmp_path, monkeypatch, capsys
):
    document = _lpar_document(range(26), _VMEDIA_ROWS)

    assert _main(tmp_path, monkeypatch, document, [_FINDING]) == (2, True)
    output = capsys.readouterr().out
    assert "STRANDED" in output
    # 0, 2 and 3 only read, so they are witnessed (#629, #1348).
    assert "NOT WITNESSED  subtasks 1, 4," in output


def test_a_document_with_no_subtasks_exits_two(tmp_path, monkeypatch, capsys):
    document = _lpar_document(range(16, 23), _VMEDIA_ROWS)
    del document["run"]

    assert _main(tmp_path, monkeypatch, document) == (2, False)
    assert "run.subtasks" in capsys.readouterr().err


def test_an_unreadable_class_exits_two_with_its_findings(tmp_path, monkeypatch, capsys):
    document = _lpar_document(range(16, 23), _VMEDIA_ROWS)
    unreadable = recovery.StateUnreadable("could not read X", [_FINDING])

    assert _main(tmp_path, monkeypatch, document, raises=unreadable) == (2, True)
    captured = capsys.readouterr()
    assert "STRANDED" in captured.out
    assert "NOT confirmed clean" in captured.err


def test_the_report_names_the_run_it_witnessed(capsys):
    document = _lpar_document([16])
    document["run"].update(tested_commit="abc1234", finished="2026-10-01T00:00:00")

    recovery._report(document, [], [], [])

    output = capsys.readouterr().out
    assert "abc1234" in output
    assert "2026-10-01T00:00:00" in output


@pytest.mark.asyncio
async def test_an_unreadable_pcie_check_still_reads_the_test_partition():
    responses = {
        **{key: value for key, value in _LPAR_CLEAN.items()},
        "hmc_get_lpar_state": "running",
    }

    with pytest.raises(recovery.StateUnreadable) as raised:
        await recovery.check_run(
            _lpar_caller(responses),
            (
                (recovery.check, _INPUTS),
                (recovery.check_test_partition, _ALL),
            ),
        )

    assert "test partition running" in [
        finding.what for finding in raised.value.findings
    ]


@pytest.mark.asyncio
async def test_more_mapped_adapters_than_listed_ones_is_unreadable():
    """The surplus could cancel out an unmapped adapter, so the count cannot judge."""
    responses = _lpar_responses(
        hmc_list_storage_mappings=[{"id": "vhost0/vtscsi0"}, {"id": "vhost1/vtopt0"}]
    )

    with pytest.raises(recovery.StateUnreadable, match="2 mapped"):
        await recovery.check_test_partition(_lpar_caller(responses), _ALL)


@pytest.mark.parametrize("state", ["not activated", "Not Activated"])
@pytest.mark.asyncio
async def test_a_powered_off_test_partition_reads_clean_in_either_spelling(state):
    """REST answers lower case; the CLI answers title case."""
    responses = _lpar_responses(hmc_get_lpar_state=state)

    assert await recovery.check_test_partition(_lpar_caller(responses), _ALL) == []


# ---------------------------------------------------------------------------
# An interrupted run is never confirmed clean (#1340)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("partial", [True, "yes", 1])
@pytest.mark.parametrize("findings", [[], [_FINDING]])
def test_a_partial_run_exits_two_whatever_it_found(
    tmp_path, monkeypatch, capsys, partial, findings
):
    """The call in flight at the stop has no row, so its class was never checked."""
    document = _lpar_document(range(16, 23), _VMEDIA_ROWS)
    document["run"].update(partial=partial, finished="2026-10-01T00:00:00")

    assert _main(tmp_path, monkeypatch, document, findings) == (2, True)
    captured = capsys.readouterr()
    assert captured.out.startswith("PARTIAL ")
    assert "interrupted 2026-10-01T00:00:00" in captured.out
    assert "CLEAN" not in captured.out
    assert ("STRANDED" in captured.out) == bool(findings)
    assert "NOT confirmed clean" in captured.err


@pytest.mark.parametrize("partial", [False, None])
def test_a_complete_or_older_run_is_judged_on_its_findings(
    tmp_path, monkeypatch, capsys, partial
):
    """`None` stands for a document written before #1336, which has no key."""
    document = _lpar_document(range(16, 23), _VMEDIA_ROWS)
    if partial is not None:
        document["run"]["partial"] = partial

    assert _main(tmp_path, monkeypatch, document) == (0, True)
    output = capsys.readouterr().out
    assert output.startswith("recovery check for")
    assert "PARTIAL" not in output
    assert "CLEAN" in output


# ---------------------------------------------------------------------------
# The users arm (subtask 11, #632) is witnessed by its reserved prefix
# ---------------------------------------------------------------------------

_CONSOLE = {"uuid": "console-1"}


def _users(*names: str) -> list[dict]:
    """A `hmc_list_users` answer in the parser's shape (entry UUID, wrapped UserID)."""
    return [
        {"UUID": f"uuid-{index}", "Resource": {"UserID": {"text": name}}}
        for index, name in enumerate(names)
    ]


@pytest.mark.asyncio
async def test_a_surviving_scratch_user_is_reported_whoever_the_document_names():
    """A run killed before writing its document leaves an earlier run's name there."""
    call = _caller(
        {
            "hmc_get_console_info": _CONSOLE,
            "hmc_list_users": _users("operator", "hmcpctl-live-0badf00d"),
        }
    )

    findings = await recovery.check_run(call, (), users=True)

    assert [finding.remedy for finding in findings] == [
        "rmhmcusr -u hmcpctl-live-0badf00d"
    ]


@pytest.mark.asyncio
async def test_no_scratch_user_means_no_finding():
    call = _caller(
        {"hmc_get_console_info": _CONSOLE, "hmc_list_users": _users("operator")}
    )

    assert await recovery.check_run(call, (), users=True) == []


@pytest.mark.asyncio
async def test_an_unlistable_user_table_is_not_clean():
    call = _caller({"hmc_get_console_info": _CONSOLE})

    with pytest.raises(recovery.StateUnreadable, match="HMC user list"):
        await recovery.check_run(call, (), users=True)


@pytest.mark.asyncio
async def test_a_user_list_it_cannot_map_is_not_clean():
    """An entry with no UUID could hide a scratch user from the prefix scan."""
    listing = [*_users("operator"), {"Resource": {"UserID": {"text": "x"}}}]
    call = _caller({"hmc_get_console_info": _CONSOLE, "hmc_list_users": listing})

    with pytest.raises(recovery.StateUnreadable, match="HMC user list"):
        await recovery.check_run(call, (), users=True)


def _users_document(**artifacts) -> dict:
    return {
        "run": {"subtasks": [11], "group": "users"},
        "config": {},
        "artifacts": artifacts,
        "results": [_row(11, "hmc_create_user")],
    }


def test_a_users_run_is_witnessed_and_its_stranded_user_exits_one(
    tmp_path, monkeypatch
):
    document = _users_document(test_user_name="hmcpctl-live-0badf00d")
    finding = recovery.Finding("HMC user x", "detail", "rmhmcusr -u x")

    assert _main(tmp_path, monkeypatch, document, [finding]) == (1, True)


def test_a_clean_users_run_exits_zero(tmp_path, monkeypatch, capsys):
    document = _users_document(test_user_name=None)

    assert _main(tmp_path, monkeypatch, document) == (0, True)
    assert "CLEAN" in capsys.readouterr().out


def test_a_pre_632_document_with_an_st11_create_exits_two(tmp_path, monkeypatch):
    """Its configured user carries no prefix, so the scan cannot witness it."""
    document = _users_document(test_user_uuid="configured-user-uuid")

    assert _main(tmp_path, monkeypatch, document) == (2, False)


def test_a_pre_632_round2_document_still_runs_its_partition_checks(
    tmp_path, monkeypatch, capsys
):
    """The unreadable ST11 must not hide the provisioned test partition's residue."""
    document = _lpar_document(
        range(16),
        [_row(11, "hmc_create_user"), _row(14, "hmc_provision_lpar (live)")],
        test_user_uuid="configured-user-uuid",
    )

    assert _main(tmp_path, monkeypatch, document, [_FINDING]) == (2, True)
    output = capsys.readouterr().out
    assert "STRANDED" in output and "predates the users arm" in output


# ---------------------------------------------------------------------------
# The lpar-config arm (ST39, #1345)
# ---------------------------------------------------------------------------

_LPAR_CONFIG = recovery.LparConfigInputs(_SYSTEM)
_LISTING_CMD = f"lssyscfg -r lpar -m {_SYSTEM} -F name,state"


def _lpar_config_caller(listing: str | None, seen: list[str]):
    async def call(tool: str, **arguments):
        recovery.guard_read_only(tool, arguments)
        seen.append(arguments.get("cmd", tool))
        return ("PASS", listing) if listing is not None else ("FAIL", None)

    return call


@pytest.mark.asyncio
async def test_lpar_config_scratch_partition_is_reported():
    seen: list[str] = []
    listing = (
        "lpar-A,Running\n"
        "hmcpctl-live-lpar-0a1b2c3d,Not Activated\n"
        "hmcpctl-live-lpar-0a1b2c3e-rn,Open Firmware\n"
    )

    findings = await recovery.check_run(
        _lpar_config_caller(listing, seen),
        ((recovery.check_lpar_config, _LPAR_CONFIG),),
    )

    assert seen == [_LISTING_CMD]
    assert [f.what for f in findings] == [
        "partition hmcpctl-live-lpar-0a1b2c3d",
        "partition hmcpctl-live-lpar-0a1b2c3e-rn",
    ]
    assert findings[0].remedy == (
        f"rmsyscfg -r lpar -m {_SYSTEM} -n hmcpctl-live-lpar-0a1b2c3d"
    )
    assert findings[1].remedy.startswith(
        f"chsysstate -m {_SYSTEM} -r lpar -n hmcpctl-live-lpar-0a1b2c3e-rn "
        "-o shutdown --immed; rmsyscfg"
    )


@pytest.mark.asyncio
async def test_lpar_config_clean_system_has_no_finding():
    call = _lpar_config_caller("lpar-A,Running\n", [])

    assert (
        await recovery.check_run(call, ((recovery.check_lpar_config, _LPAR_CONFIG),))
        == []
    )


@pytest.mark.asyncio
async def test_lpar_config_unreadable_listing_is_not_clean():
    with pytest.raises(recovery.StateUnreadable, match="partitions of"):
        await recovery.check_run(
            _lpar_config_caller(None, []), ((recovery.check_lpar_config, _LPAR_CONFIG),)
        )


@pytest.mark.parametrize(
    "subtask,group",
    [
        (39, "lpar-config"),
        (47, "lpar-settings"),
        (49, "lpar-physical-io"),
        (50, "lpar-memory-quantities"),
        (56, "lpar-processor-quantities"),
    ],
)
def test_lpar_config_run_is_witnessed(tmp_path, monkeypatch, capsys, subtask, group):
    document = {
        "run": {"subtasks": [subtask], "group": group},
        "config": {"system_name": _SYSTEM},
        "artifacts": {},
        "results": [],
    }

    seen = []

    async def run_checks(checks, *, users=False):
        lpar_config = dict(checks).get(recovery.check_lpar_config)
        seen.append(lpar_config)
        return []

    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "_run_checks", run_checks)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert recovery.main(["--results", str(path)]) == 0
    assert seen == [recovery.LparConfigInputs(_SYSTEM)]
    assert "CLEAN" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The lpar-power arm (ST41, #1346)
# ---------------------------------------------------------------------------

_LPAR_POWER = recovery.LparPowerInputs(_SYSTEM, "datavg")
_LSVG = f"viosvrcmd -m {_SYSTEM} --id 2 -c 'lsvg -lv datavg'"


def _lpar_power_caller(
    partitions: str | None,
    mappings: list | None,
    adapters: str = "",
    volumes: str = "datavg:\nLV NAME TYPE LPs PPs PVs LV STATE MOUNT POINT\n",
):
    async def call(tool: str, **arguments):
        recovery.guard_read_only(tool, arguments)
        if tool == "hmc_list_storage_mappings":
            return ("PASS", mappings) if mappings is not None else ("FAIL", None)
        if tool == "hmc_list_volume_groups":
            return "PASS", [{"name": "datavg", "uuid": "vg"}]
        if arguments["cmd"] == _LSVG:
            return "PASS", volumes
        if "virtualio" in arguments["cmd"]:
            return "PASS", adapters
        return ("PASS", partitions) if partitions is not None else ("FAIL", None)

    return call


@pytest.mark.asyncio
async def test_lpar_power_residue_is_reported_with_its_commands():
    listing = (
        "lpar-A,Running,aixlinux,1\n"
        "vios-A,Running,vioserver,2\n"
        "hmcpctl-live-pwr-0a1b2c3d-p,Open Firmware,aixlinux,9\n"
    )
    mappings = [
        {"id": "vhost0/vtscsi0", "backing_name": "lv_op"},
        {"id": "vhost3/vtscsi3", "backing_name": "lppwr0a1b2c3d"},
    ]
    adapters = "2,lpar-A,3\n5,hmcpctl-live-pwr-0a1b2c3d-p,2\n"
    volumes = (
        "datavg:\nLV NAME TYPE LPs PPs PVs LV STATE MOUNT POINT\n"
        "lv_op jfs2 1 1 1 open/syncd N/A\nlppwr0a1b2c3d jfs2 1 1 1 open/syncd N/A\n"
    )

    findings = await recovery.check_run(
        _lpar_power_caller(listing, mappings, adapters, volumes),
        ((recovery.check_lpar_power, _LPAR_POWER),),
    )

    assert [f.what for f in findings] == [
        "partition hmcpctl-live-pwr-0a1b2c3d-p",
        "mapping vhost3/vtscsi3 on VIOS vios-A",
        "vSCSI server adapter 5 on VIOS vios-A",
        "volume lppwr0a1b2c3d on VIOS vios-A",
    ]
    assert findings[0].remedy.startswith(
        f"chsysstate -m {_SYSTEM} -r lpar -n hmcpctl-live-pwr-0a1b2c3d-p -o shutdown"
    )
    assert 'rmvdev -vtd vtscsi3"' in findings[1].remedy
    assert findings[2].remedy == (
        f"chhwres -r virtualio --rsubtype scsi -m {_SYSTEM} -o r -p vios-A -s 5"
    )
    assert (
        findings[3].remedy
        == f'viosvrcmd -m {_SYSTEM} --id 2 -c "rmlv -f lppwr0a1b2c3d"'
    )


@pytest.mark.asyncio
async def test_lpar_power_clean_system_has_no_finding():
    call = _lpar_power_caller(
        "lpar-A,Running,aixlinux,1\nvios-A,Running,vioserver,2\n",
        [{"id": "vhost0/vtscsi0", "backing_name": "lv_op"}],
        "2,lpar-A,3\n",
    )

    assert (
        await recovery.check_run(call, ((recovery.check_lpar_power, _LPAR_POWER),))
        == []
    )


@pytest.mark.parametrize("listing", [None, 42, "", "  ", "datavg:", "not a listing"])
@pytest.mark.asyncio
async def test_lpar_power_malformed_pass_volume_inventory_is_unreadable(listing):
    call = _lpar_power_caller("vios-A,Running,vioserver,2\n", [], volumes=listing)

    with pytest.raises(recovery.StateUnreadable, match="volumes of"):
        await recovery.check_run(call, ((recovery.check_lpar_power, _LPAR_POWER),))


@pytest.mark.asyncio
async def test_lpar_power_unreadable_mappings_are_not_clean():
    call = _lpar_power_caller("vios-A,Running,vioserver,2\n", None)

    with pytest.raises(recovery.StateUnreadable, match="storage mappings"):
        await recovery.check_run(call, ((recovery.check_lpar_power, _LPAR_POWER),))


def test_the_guard_admits_only_the_exact_volume_listing():
    recovery.guard_read_only("hmc_run_command", {"cmd": _LSVG})
    for refused in (
        f"viosvrcmd -m {_SYSTEM} --id 2 -c 'rmlv -f lppwr0a1b2c3d'",
        f"viosvrcmd -m {_SYSTEM} --id 2 -c 'lsvg -lv datavg; rmlv x'",
    ):
        with pytest.raises(recovery.MutatingCallRefused):
            recovery.guard_read_only("hmc_run_command", {"cmd": refused})


@pytest.mark.parametrize("detach_probe", [False, True])
def test_lpar_power_run_is_witnessed(tmp_path, monkeypatch, capsys, detach_probe):
    document = {
        "run": {"subtasks": [41], "group": "lpar-power"},
        "config": {"system_name": _SYSTEM},
        "artifacts": {},
        "results": [],
    }
    if detach_probe:
        document["run"]["detach_probe"] = True
    seen = []

    async def run_checks(checks, *, users=False):
        lpar_power = dict(checks).get(recovery.check_lpar_power)
        seen.append(lpar_power)
        return []

    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "_run_checks", run_checks)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert recovery.main(["--results", str(path)]) == 0
    assert seen == [recovery.LparPowerInputs(_SYSTEM)]
    assert "CLEAN" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The vios-backup arm (ST37, #1349)
# ---------------------------------------------------------------------------

_VIOS_INPUTS = recovery.VIOSBackupInputs(
    system_name=_SYSTEM,
    lpar_name="sys-R1-lp3",
    vios="vios-A",
    vios_uuid="0000000A-ABCD-4EF0-8ABC-00000000000A",
    backup_name="hmcpctl-live-st37-0a1b2c3d",
    mapping_id="vhost0/lp3-disk",
    backing="lp3-vd1",
)
_MAPPED = [{"id": "vhost0/lp3-disk", "backing_name": "lp3-vd1"}]


def _vios_document(subtasks=(37,), **artifacts) -> dict:
    return {
        "run": {"subtasks": list(subtasks)},
        "config": {"system_name": _SYSTEM, "lp3_name": "sys-R1-lp3"},
        "artifacts": {
            "vios_backup_vios": "vios-A",
            "vios_backup_vios_uuid": "0000000A-ABCD-4EF0-8ABC-00000000000A",
            "vios_backup_name": "hmcpctl-live-st37-0a1b2c3d",
            "vios_backup_mapping": "vhost0/lp3-disk",
            "vios_backup_backing": "lp3-vd1",
            **artifacts,
        },
    }


@pytest.mark.asyncio
async def test_a_clean_vios_backup_run_yields_no_findings():
    seen: list[str] = []
    responses = {"hmc_list_vios_backups": [], "hmc_list_storage_mappings": _MAPPED}

    assert (
        await recovery.check_vios_backup(_caller(responses, seen), _VIOS_INPUTS) == []
    )
    assert set(seen) == {"hmc_list_vios_backups", "hmc_list_storage_mappings"}


@pytest.mark.asyncio
async def test_a_kept_backup_is_reported_with_its_rmviosbk():
    responses = {
        "hmc_list_vios_backups": [
            {"name": "hmcpctl-live-st37-0a1b2c3d", "type": "viosioconfig"}
        ],
        "hmc_list_storage_mappings": _MAPPED,
    }

    (finding,) = await recovery.check_vios_backup(_caller(responses), _VIOS_INPUTS)

    assert finding.remedy == (
        "rmviosbk -t viosioconfig -m sys-R1 -p vios-A -f hmcpctl-live-st37-0a1b2c3d"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rendered",
    ["hmcpctl-live-st37-0a1b2c3d.tar.gz", "vios-A/hmcpctl-live-st37-0a1b2c3d"],
)
async def test_a_kept_backup_rendered_differently_is_still_reported(rendered):
    responses = {
        "hmc_list_vios_backups": [{"name": rendered, "type": "viosioconfig"}],
        "hmc_list_storage_mappings": _MAPPED,
    }

    (finding,) = await recovery.check_vios_backup(_caller(responses), _VIOS_INPUTS)

    assert finding.what == "VIOS backup left"


@pytest.mark.asyncio
async def test_a_missing_disk_mapping_is_reported_with_its_mkvdev():
    responses = {"hmc_list_vios_backups": [], "hmc_list_storage_mappings": []}

    (finding,) = await recovery.check_vios_backup(_caller(responses), _VIOS_INPUTS)

    assert finding.remedy == (
        'viosvrcmd -m sys-R1 -p vios-A -c "mkvdev -vdev lp3-vd1 -vadapter vhost0 '
        '-dev lp3-disk"'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        {"hmc_list_storage_mappings": _MAPPED},
        {"hmc_list_vios_backups": []},
    ],
    ids=["backups-unreadable", "mappings-unreadable"],
)
async def test_an_unreadable_vios_listing_is_not_clean(responses):
    with pytest.raises(recovery.StateUnreadable):
        await recovery.check_vios_backup(_caller(responses), _VIOS_INPUTS)


@pytest.mark.asyncio
async def test_a_mapping_name_that_is_not_a_device_name_is_not_used():
    inputs = recovery.VIOSBackupInputs(
        **{**vars(_VIOS_INPUTS), "mapping_id": "vhost0/x;rmdev"}
    )

    with pytest.raises(recovery.StateUnreadable, match="plain device names"):
        await recovery.check_vios_backup(_caller({}), inputs)


def test_vios_backup_inputs_need_subtask_37_and_its_artifacts():
    assert recovery.vios_backup_inputs_from_document(_vios_document(), [37]) == (
        _VIOS_INPUTS
    )
    assert recovery.vios_backup_inputs_from_document(_vios_document(), [16]) is None
    assert (
        recovery.vios_backup_inputs_from_document(
            _vios_document(vios_backup_name=None), [37]
        )
        is None
    )


def test_a_vios_backup_run_is_witnessed(tmp_path, monkeypatch, capsys):
    async def checks(checks, *, users=False):
        pcie = dict(checks).get(recovery.check)
        partition = dict(checks).get(recovery.check_test_partition)
        vios = dict(checks).get(recovery.check_vios_backup)
        assert pcie is None and partition is None
        return await recovery.check_vios_backup(
            _caller({"hmc_list_vios_backups": [], "hmc_list_storage_mappings": []}),
            vios,
        )

    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "_run_checks", checks)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(_vios_document()), encoding="utf-8")

    assert recovery.main(["--results", str(path)]) == 1
    output = capsys.readouterr().out
    assert "disk mapping missing" in output
    assert "NOT WITNESSED" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backups", "what"),
    [
        (
            [{"name": "hmcpctl-live-st37-0a1b2c3d", "type": "viosioconfig"}],
            "VIOS off baseline, backup kept",
        ),
        ([], "VIOS off baseline, backup gone"),
    ],
)
async def test_an_off_baseline_run_is_never_clean(backups, what):
    inputs = recovery.VIOSBackupInputs(**{**vars(_VIOS_INPUTS), "off_baseline": True})
    responses = {"hmc_list_vios_backups": backups, "hmc_list_storage_mappings": _MAPPED}

    (finding,) = await recovery.check_vios_backup(_caller(responses), inputs)

    assert finding.what == what


def test_the_kept_backup_row_marks_the_run_off_baseline():
    document = _vios_document()
    document["results"] = [
        {"tool": "final compare (lsmap -all -net)", "status": "FAIL"},
        {"tool": recovery.vios_backup.KEPT_ROW, "status": "FAIL"},
    ]

    inputs = recovery.vios_backup_inputs_from_document(document, [37])

    assert inputs is not None and inputs.off_baseline
    compared_only = _vios_document()
    compared_only["results"] = [
        {"tool": "final compare (lsmap -all)", "status": "FAIL"}
    ]
    assert not recovery.vios_backup_inputs_from_document(
        compared_only, [37]
    ).off_baseline


# ---------------------------------------------------------------------------
# The network arm (ST9, #629)
# ---------------------------------------------------------------------------

_LPAR_CNA = {
    "UUID": "0000C0A0-0000-4000-8000-000000000000",
    "Resource": {"PortVLANID": "1"},
}
_FC_ROW = {
    "name": "vios-A",
    "lpar_id": "1",
    "port_name": "fcs0",
    "port_label": "prod-a",
}


def _network_document(**artifacts):
    rows = [
        {"subtask": 9, "tool": "hmc_create_virtual_network (create)", "status": "PASS"},
        {
            "subtask": 9,
            "tool": "hmc_list_adapters (ClientNetworkAdapter, baseline)",
            "status": "PASS",
            "data": [_LPAR_CNA],
        },
        {
            "subtask": 9,
            "tool": "hmc_list_vios_fc_port_labels (FC-port labels, baseline)",
            "status": "PASS",
            "data": [_FC_ROW],
        },
        {
            "subtask": 9,
            "tool": "hmc_list_vios_vfc_group_labels (vFC group labels, baseline)",
            "status": "PASS",
            "data": [{"name": "prod-group"}],
        },
    ]
    return {
        "run": {"group": "network", "subtasks": [2, 9]},
        "config": {"system_name": "sys-one", "lp3_name": "lp-three"},
        "artifacts": {"test_vlan_id": 3101, **artifacts},
        "results": rows,
    }


def _network_inputs():
    inputs = recovery.network_inputs_from_document(_network_document(), [2, 9])
    assert inputs is not None
    return inputs


def _clean_hmc(**overrides):
    responses = {
        "hmc_list_virtual_networks": [
            {"UUID": "net-1", "Resource": {"NetworkVLANID": "3100", "NetworkName": "x"}}
        ],
        "hmc_list_adapters": [_LPAR_CNA],
        "hmc_list_vios_vfc_group_labels": [{"name": "prod-group"}],
        "hmc_list_vios_fc_port_labels": [_FC_ROW],
    }
    responses.update(overrides)
    return responses


def test_network_inputs_come_from_the_runs_own_st9_rows():
    inputs = _network_inputs()
    assert inputs.vlan == 3101
    assert set(inputs.adapters) == {"ClientNetworkAdapter"}
    assert inputs.fc_labels == [_FC_ROW]
    assert recovery.network_inputs_from_document(_network_document(), [2]) is None
    skipped = _network_document()
    skipped["results"] = [{"subtask": 9, "tool": "network arm", "status": "SKIP"}]
    assert recovery.network_inputs_from_document(skipped, [9]) is None


def test_a_vlan_never_created_is_not_witnessed_from_a_restored_artifact():
    document = _network_document()
    document["results"] = document["results"][1:]
    assert recovery.network_inputs_from_document(document, [9]).vlan is None


@pytest.mark.asyncio
async def test_a_clean_network_run_yields_no_findings():
    assert await recovery.check_network(_caller(_clean_hmc()), _network_inputs()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "what"),
    [
        (
            {
                "hmc_list_virtual_networks": [
                    {
                        "UUID": "net-9",
                        "Resource": {
                            "NetworkVLANID": "3101",
                            "NetworkName": "anything",
                        },
                    }
                ]
            },
            "network left on the test VLAN",
        ),
        (
            {
                "hmc_list_adapters": [
                    _LPAR_CNA,
                    {"UUID": "00000001-0000-4000-8000-000000000000", "Resource": {}},
                ]
            },
            "ClientNetworkAdapter off its baseline",
        ),
        (
            {"hmc_list_vios_fc_port_labels": [{**_FC_ROW, "port_label": "hmcl-x"}]},
            "FC-port label off its original",
        ),
        (
            {"hmc_list_vios_vfc_group_labels": [{"name": "hmcl-0a1b2c3d"}]},
            "vFC group label left",
        ),
    ],
)
async def test_each_network_residue_is_a_finding(overrides, what):
    findings = await recovery.check_network(
        _caller(_clean_hmc(**overrides)), _network_inputs()
    )
    assert [finding.what for finding in findings] == [what]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool", ["hmc_list_virtual_networks", "hmc_list_vios_vfc_group_labels"]
)
async def test_an_unreadable_network_listing_is_not_clean(tool):
    with pytest.raises(recovery.StateUnreadable):
        await recovery.check_network(
            _caller(_clean_hmc(**{tool: None})), _network_inputs()
        )


def test_a_network_run_is_witnessed_and_can_exit_clean(tmp_path, monkeypatch, capsys):
    async def checks(checks, *, users=False):
        network_inputs = dict(checks).get(recovery.check_network)
        assert network_inputs is not None
        return await recovery.check_network(_caller(_clean_hmc()), network_inputs)

    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "_run_checks", checks)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(_network_document()), encoding="utf-8")

    assert recovery.main(["--results", str(path)]) == 0
    output = capsys.readouterr().out
    assert "CLEAN" in output
    assert "NOT WITNESSED" not in output


# ---------------------------------------------------------------------------
# The storage arm (#1348)
# ---------------------------------------------------------------------------

_RUN_DISK = "hpctl0a1b2c3d"
_GROUP = "datavg"
_VOLUMES = f"{_GROUP}:\nLV NAME  TYPE  LPs  PPs  PVs  LV STATE  MOUNT POINT\nop-disk  jfs2  4  4  1  open/syncd  N/A\n"


def _storage_document(**artifacts) -> dict:
    document = _lpar_document([0, 3, 40], **artifacts)
    document["run"]["group"] = "storage"
    document["config"]["vdisk_volume_group_name"] = _GROUP
    return document


def _storage_caller(
    volumes: str,
    mappings: list,
    seen: list | None = None,
    adapters: str = f"5,{_TEST_LPAR},3\n",
):
    """Answer the volume listing and the adapter listing by their commands."""

    async def call(tool: str, **arguments):
        recovery.guard_read_only(tool, arguments)
        if seen is not None:
            seen.append((tool, arguments))
        if tool == "hmc_run_command":
            if "lsvg -lv" in arguments["cmd"]:
                return "PASS", volumes
            return "PASS", adapters
        if tool == "hmc_list_storage_mappings":
            return "PASS", mappings
        return "FAIL", None

    return call


_STORAGE_MAPPINGS = [{"id": "vhost0/vtscsi0", "backing_name": "op-disk"}]


def test_a_storage_document_applies_only_its_classes():
    inputs = _inputs(_storage_document())

    assert inputs.storage_ran and inputs.volume_group == _GROUP
    assert not (inputs.vmedia_ran or inputs.provisioned or inputs.powered_on)


@pytest.mark.asyncio
async def test_a_clean_storage_run_reads_volumes_mappings_and_adapters():
    seen: list = []
    call = _storage_caller(_VOLUMES, _STORAGE_MAPPINGS, seen)

    assert await recovery.check_test_partition(call, _inputs(_storage_document())) == []
    assert (
        "hmc_run_command",
        {"cmd": storage_lifecycle.volume_listing(_TEST_SYSTEM, _VIOS_ID, _GROUP)},
    ) in seen


@pytest.mark.asyncio
async def test_a_run_disk_and_its_mapping_left_are_reported_by_prefix():
    """Found by name shape, so a run killed before its document was written is too."""
    call = _storage_caller(
        _VOLUMES + f"{_RUN_DISK}  jfs2  2  2  1  open/syncd  N/A\n",
        [*_STORAGE_MAPPINGS, {"id": "vhost1/vtscsi1", "backing_name": _RUN_DISK}],
        adapters=f"5,{_TEST_LPAR},3\n6,{_TEST_LPAR},4\n",
    )

    findings = await recovery.check_test_partition(call, _inputs(_storage_document()))

    assert [finding.what for finding in findings] == [
        "run disk mapping left",
        "run disk left",
    ]
    assert f"detach-mapping {_VIOS} vhost1/vtscsi1" in findings[0].remedy
    assert f"--name {_RUN_DISK}" in findings[1].remedy


@pytest.mark.asyncio
async def test_an_operator_volume_is_never_the_runs():
    call = _storage_caller(
        _VOLUMES + "hpctl-operator  jfs2  2  2  1  open/syncd  N/A\n", _STORAGE_MAPPINGS
    )

    assert await recovery.check_test_partition(call, _inputs(_storage_document())) == []


@pytest.mark.asyncio
async def test_an_unparsable_volume_listing_is_unreadable():
    call = _storage_caller("Cluster does not exist.", _STORAGE_MAPPINGS)

    with pytest.raises(recovery.StateUnreadable, match="volumes in datavg"):
        await recovery.check_test_partition(call, _inputs(_storage_document()))


@pytest.mark.asyncio
async def test_a_group_name_the_guard_cannot_carry_is_unreadable_not_refused():
    document = _storage_document()
    document["config"]["vdisk_volume_group_name"] = "data vg"

    with pytest.raises(recovery.StateUnreadable, match="cannot carry"):
        await recovery.check_test_partition(
            _storage_caller(_VOLUMES, _STORAGE_MAPPINGS), _inputs(document)
        )


@pytest.mark.parametrize("group", ["datavg", "example-lt-609-vg", "vg.2"])
def test_the_guard_admits_exactly_the_volume_listing(group):
    """Every name `_run_disk_left` accepts, the runner's hyphenated default included."""
    recovery.guard_read_only(
        "hmc_run_command",
        {"cmd": storage_lifecycle.volume_listing("sys-A", 3, group)},
    )


@pytest.mark.parametrize(
    "command",
    [
        "viosvrcmd -m sys-A --id 3 -c 'rmlv -f hpctl0a1b2c3d'",
        "viosvrcmd -m sys-A --id 3 -c 'lsvg -lv datavg; rmlv x'",
        "viosvrcmd -m sys-A -p vios-A -c 'lsvg -lv datavg'",
        "viosvrcmd -m sys-A --id 3 -c 'lsvg -lv datavg' ; rmsyscfg",
        "viosvrcmd -m 'sys A' --id 3 -c 'lsvg -lv datavg'",
    ],
)
def test_the_guard_refuses_any_other_vios_command(command):
    with pytest.raises(recovery.MutatingCallRefused):
        recovery.guard_read_only("hmc_run_command", {"cmd": command})


def test_a_clean_storage_run_exits_zero(tmp_path, monkeypatch, capsys):
    assert _main(tmp_path, monkeypatch, _storage_document()) == (0, True)
    assert "CLEAN" in capsys.readouterr().out


@pytest.mark.parametrize("subtasks,pending", [([42], None), ([0, 3], "hpvg1234abcd")])
def test_st42_or_pending_group_applies_to_recovery(subtasks, pending):
    document = _lpar_document(subtasks, storage_volume_group_name=pending)
    inputs = recovery.lpar_inputs_from_document(document, subtasks)
    assert inputs is not None and inputs.applies and inputs.scratch_vg_ran
    assert inputs.scratch_vg_pending == pending


@pytest.mark.parametrize(
    "pending,names,expected",
    [
        (None, [], None),
        (None, [{"name": "rootvg"}, {"name": "hpvg1234abcd"}], "run volume group left"),
        ("hpvg1234abcd", [], "scratch group restoration unconfirmed"),
    ],
)
@pytest.mark.asyncio
async def test_scratch_recovery_is_read_only_and_keeps_uncertainty(
    pending, names, expected
):
    document = _lpar_document([42], storage_volume_group_name=pending)
    inputs = recovery.lpar_inputs_from_document(document, [42])
    seen = []
    finding = await recovery._run_volume_group_left(
        _caller({"hmc_list_volume_groups": names}, seen), inputs
    )
    assert (finding.what if finding else None) == expected
    assert seen == ["hmc_list_volume_groups"]


@pytest.mark.parametrize("pending,data", [("rootvg", []), (None, [{}]), (None, None)])
@pytest.mark.asyncio
async def test_scratch_recovery_refuses_invalid_or_unreadable_state(pending, data):
    document = _lpar_document([42], storage_volume_group_name=pending)
    inputs = recovery.lpar_inputs_from_document(document, [42])
    with pytest.raises(recovery.StateUnreadable):
        await recovery._run_volume_group_left(
            _caller({"hmc_list_volume_groups": data}), inputs
        )


@pytest.mark.asyncio
async def test_selected_checks_continue_after_unreadable_and_keep_users_distinct(
    monkeypatch,
):
    seen = []
    inputs = object()
    first = recovery.Finding("first", "detail", "remedy")
    partial = recovery.Finding("partial", "detail", "remedy")
    last = recovery.Finding("last", "detail", "remedy")

    async def users(call):
        seen.append("users")
        raise recovery.StateUnreadable("users unreadable", [first])

    async def successful(call, value):
        assert value is inputs
        seen.append("first")
        return [first]

    async def unreadable(call, value):
        assert value is inputs
        seen.append("partial")
        raise recovery.StateUnreadable("arm unreadable", [partial])

    async def sibling(call, value):
        assert value is inputs
        seen.append("last")
        return [last]

    monkeypatch.setattr(recovery, "_scratch_users_left", users)
    with pytest.raises(recovery.StateUnreadable) as error:
        await recovery.check_run(
            None,
            ((successful, inputs), (unreadable, inputs), (sibling, inputs)),
            users=True,
        )
    assert seen == ["users", "first", "partial", "last"]
    assert str(error.value) == "users unreadable; arm unreadable"
    assert error.value.findings == [first, partial, last]


@pytest.mark.parametrize(
    "selected,users",
    [((), False), ((), True), ((1, 4), False), (tuple(range(7)), True)],
)
def test_main_selects_ordered_inputs_once_and_bootstraps_only_when_needed(
    tmp_path, monkeypatch, selected, users
):
    decoders = (
        "inputs_from_document",
        "lpar_inputs_from_document",
        "vios_backup_inputs_from_document",
        "network_inputs_from_document",
        "lpar_config_inputs_from_document",
        "lpar_power_inputs_from_document",
        "round2_inputs_from_document",
    )
    checkers = (
        recovery.check,
        recovery.check_test_partition,
        recovery.check_vios_backup,
        recovery.check_network,
        recovery.check_lpar_config,
        recovery.check_lpar_power,
        recovery.check_round2,
    )
    # False-valued decoded objects must survive selection unchanged.
    values = [[] for _ in checkers]
    decoded = []
    for index, decoder in enumerate(decoders):

        def decode(*args, index=index):
            decoded.append(index)
            return values[index] if index in selected else None

        monkeypatch.setattr(recovery, decoder, decode)
    monkeypatch.setattr(recovery, "users_witnessed", lambda *args: users)
    bootstrapped = []
    monkeypatch.setattr(
        recovery.runner, "_bootstrap_config", lambda: bootstrapped.append(True) or True
    )
    forwarded = []

    async def run(checks, *, users=False):
        assert len(decoded) == 7
        assert isinstance(checks, tuple)
        assert [checker for checker, _ in checks] == [checkers[i] for i in selected]
        assert all(
            value is values[i] for (_, value), i in zip(checks, selected, strict=True)
        )
        forwarded.append(users)
        return []

    monkeypatch.setattr(recovery, "_run_checks", run)
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"run": {"subtasks": []}}))
    assert recovery.main(["--results", str(path)]) == 0
    assert len(decoded) == 7
    assert bootstrapped == ([True] if users or selected else [])
    assert forwarded == ([users] if users or selected else [])


@pytest.mark.asyncio
async def test_selected_checker_uses_actual_read_only_wrapper(monkeypatch):
    served = object()
    calls = []

    @asynccontextmanager
    async def served_client():
        yield served

    async def state_call(self, client, tool, **arguments):
        assert client is served
        calls.append((tool, arguments))
        return "PASS", {}

    value = object()

    async def checker(call, inputs):
        assert inputs is value
        await call("hmc_get_console_info")
        await call("hmc_power_on_lpar")
        return []

    monkeypatch.setattr(recovery.runner, "served_client", served_client)
    monkeypatch.setattr(recovery.runner.RunState, "call", state_call)
    with pytest.raises(recovery.MutatingCallRefused):
        await recovery._run_checks(((checker, value),))
    assert calls == [("hmc_get_console_info", {})]


def test_system_profiles_recovery(tmp_path, monkeypatch, capsys):
    from unittest.mock import AsyncMock

    from live_test import system_profiles

    path = tmp_path / "results.json"
    path.write_text(
        json.dumps(
            {"run": {"group": "system-profiles", "subtasks": [44], "partial": True}}
        )
    )
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    check = AsyncMock(side_effect=ValueError("partial native run"))
    monkeypatch.setattr(system_profiles, "recover_document", check)
    assert recovery.main(["--results", str(path)]) == 2
    assert "CLEAN" not in capsys.readouterr().out
    check.assert_awaited_once()


def _creation_memory_document(phase="complete"):
    from live_test import config as settings

    from hmcpctl.config import HMCConfig

    system = "00000000-0000-0000-0000-000000000001"
    journal = {
        "invocation_id": "a" * 32,
        "candidate_commit": "b" * 40,
        "system_uuid": system,
        "hmc_identity": recovery.runner._hmc_identity(HMCConfig.from_mapping({})),
        "phase": phase,
        "lpar_name": "hmcpctl-live-lpar-deadbeef",
        "lpar_token": "lparcfg-deadbeef",
        "lpar_uuid": None,
        "vios_name": "hmcpctl-live-c06-vios-cafefeed",
        "vios_uuid": None,
        "preabsence": {"lpar": True, "vios": True},
        "create_returned": {"lpar": False, "vios": False},
        "fresh_vios_delete_authorized": True,
        "requested": {
            "lpar": {
                "min_num_huge_pages": 0,
                "desired_num_huge_pages": 0,
                "max_num_huge_pages": 0,
                "hpt_ratio": "1:128",
                "ppt_ratio": "1:256",
                "kvm_reserved_mem_percent": "0",
            },
            "vios": {"hpt_ratio": "1:128", "ppt_ratio": "1:256"},
        },
        "baseline": {
            "pools": {
                "partitions": [],
                "available_processing_units": "10.0",
                "available_memory_mib": "8192",
            },
            "vios": [],
        },
        "outcome": {"lpar": "not-attempted", "vios": "not-attempted"},
        "deletion": {"lpar": "confirmed-absent", "vios": "confirmed-absent"},
    }
    artifacts = dataclasses.asdict(
        settings.LiveTestArtifacts(creation_memory_layout=journal)
    )
    return {
        "run": {
            "group": "creation-memory-layout",
            "subtasks": [58],
            "tested_commit": "b" * 40,
        },
        "hmc": journal["hmc_identity"],
        "config": json.loads(json.dumps(dataclasses.asdict(settings.LiveTestConfig()))),
        "artifacts": artifacts,
        "results": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "preflight",
        "lpar-create-pending",
        "lpar-create-returned",
        "lpar-delete-pending",
        "lpar-deleted",
        "vios-create-pending",
        "vios-create-returned",
        "vios-delete-pending",
        "vios-deleted",
        "complete",
        "stopped-uncertain",
    ],
)
@pytest.mark.parametrize(
    "fault", ["", "orphan", "foreign", "system", "unreadable", "baseline"]
)
async def test_creation_memory_recovery_all_phases_are_read_only(
    monkeypatch, phase, fault
):
    document = _creation_memory_document(phase)
    inputs = recovery.creation_memory_layout_inputs_from_document(document, [58])
    from hmcpctl.config import HMCConfig

    monkeypatch.setattr(recovery, "HMCConfig", lambda: HMCConfig.from_mapping({}))
    seen = []

    async def call(tool, **arguments):
        recovery.guard_read_only(tool, arguments)
        seen.append(tool)
        if fault == "unreadable":
            return "FAIL", None
        if tool == "hmc_get_system":
            return "PASS", {
                "UUID": "00000000-0000-0000-0000-000000000009"
                if fault == "system"
                else inputs.journal["system_uuid"],
                "Resource": {"SystemName": "canonical-system"},
            }
        if tool == "hmc_list_vios":
            return "PASS", [] if fault != "orphan" else [
                {
                    "UUID": "00000000-0000-0000-0000-000000000003",
                    "Resource": {"PartitionName": "hmcpctl-live-c06-vios-cafefeed"},
                }
            ]
        command = arguments["cmd"]
        if command.endswith("-F name"):
            return "PASS", "foreign" if fault == "baseline" else ""
        return "PASS", "10.0" if command.endswith(
            "curr_avail_sys_proc_units"
        ) else "8192,256"

    if fault == "foreign":
        inputs.journal["hmc_identity"]["host"] = "foreign.test"
    if fault in {"foreign", "system", "unreadable"}:
        with pytest.raises(recovery.StateUnreadable):
            await recovery.check_creation_memory_layout(call, inputs)
    else:
        findings = await recovery.check_creation_memory_layout(call, inputs)
        assert bool(findings) == (
            phase != "complete" or fault in {"orphan", "baseline"}
        )
    assert set(seen) <= {"hmc_get_system", "hmc_run_command", "hmc_list_vios"}


@pytest.mark.asyncio
@pytest.mark.parametrize("uppercase", [False, True])
async def test_creation_memory_recovery_compares_canonical_observed_uuid(
    monkeypatch, uppercase
):
    from hmcpctl.config import HMCConfig

    document = _creation_memory_document()
    journal = document["artifacts"]["creation_memory_layout"]
    system = "aaaaaaaa-0000-0000-0000-000000000001"
    vios = "bbbbbbbb-0000-0000-0000-000000000002"
    journal["system_uuid"] = system
    journal["baseline"]["vios"] = [{"uuid": vios, "name": "existing"}]
    inputs = recovery.creation_memory_layout_inputs_from_document(document, [58])
    monkeypatch.setattr(recovery, "HMCConfig", lambda: HMCConfig.from_mapping({}))

    async def call(tool, **arguments):
        recovery.guard_read_only(tool, arguments)
        if tool == "hmc_get_system":
            return "PASS", {
                "UUID": system.upper() if uppercase else system,
                "Resource": {"SystemName": "canonical-system"},
            }
        if tool == "hmc_list_vios":
            return "PASS", [
                {
                    "UUID": vios.upper() if uppercase else vios,
                    "Resource": {"PartitionName": "existing"},
                }
            ]
        command = arguments["cmd"]
        if command.endswith("-F name"):
            return "PASS", ""
        return "PASS", "10.0" if command.endswith(
            "curr_avail_sys_proc_units"
        ) else "8192,256"

    assert await recovery.check_creation_memory_layout(call, inputs) == []


@pytest.mark.parametrize("field", ["system_uuid", "lpar_uuid", "vios_uuid", "baseline"])
def test_creation_memory_saved_uuid_case_is_still_strict(field):
    from live_test import config as settings

    artifacts = _creation_memory_document()["artifacts"]
    identity = "AAAAAAAA-0000-0000-0000-000000000001"
    journal = artifacts["creation_memory_layout"]
    if field == "baseline":
        journal["baseline"]["vios"] = [{"uuid": identity, "name": "existing"}]
    else:
        journal[field] = identity
    with pytest.raises(ValueError, match="canonical UUID|VIOS baseline"):
        settings.decode_artifacts(artifacts)


def test_creation_memory_journal_unknown_keys_types_and_saved_grant_refuse():
    from copy import deepcopy

    from live_test import config as settings

    original = _creation_memory_document()["artifacts"]
    for key, value in [
        ("unknown", 1),
        ("phase", "invented"),
        ("lpar_uuid", "invalid"),
        ("fresh_vios_delete_authorized", "true"),
        ("preabsence", {"lpar": True}),
        ("baseline", {}),
        ("requested", {}),
        ("hmc_identity", {}),
    ]:
        artifacts = deepcopy(original)
        artifacts["creation_memory_layout"][key] = value
        with pytest.raises((ValueError, TypeError)):
            settings.decode_artifacts(artifacts)
    assert (
        settings.decode_artifacts(original).creation_memory_layout[
            "fresh_vios_delete_authorized"
        ]
        is True
    )


@pytest.mark.asyncio
async def test_legacy_non_st58_round2_restore_recovery_keeps_valid_inputs(
    tmp_path, monkeypatch
):
    from live_test import baseline
    from live_test import config as settings

    from hmcpctl.config import HMCConfig

    config = settings.LiveTestConfig()
    hmc = HMCConfig.from_mapping({"host": "hmc.test"})
    tables = {name: [] for name in baseline.TABLES}
    tables.update(
        system=[
            {
                "name": config.system_name,
                "type_model": "9009-42A",
                "serial_num": "REDACTED",
            }
        ],
        partitions=[
            {
                "lpar_id": "1",
                "name": "vios",
                "lpar_env": "vioserver",
                "state": "Not Activated",
            }
        ],
        vios=[
            {"id": "1", "name": "vios", "uuid": "00000000-0000-0000-0000-000000000003"}
        ],
    )
    snapshot = {
        "version": 1,
        "scope": {"host": "hmc.test", "system": config.system_name, "vios_ids": [1]},
        "tables": tables,
    }
    artifacts = settings.LiveTestArtifacts(
        vios_partition_id=1,
        lp3_baseline={
            "round2_snapshot": snapshot,
            "description": "baseline",
            "proc_compat": {"profile": "default_profile", "profile_mode": "default"},
        },
    )
    document = {
        "run": {"group": "round2"},
        "config": json.loads(json.dumps(dataclasses.asdict(config))),
        "hmc": recovery.runner._hmc_identity(hmc),
        "artifacts": dataclasses.asdict(artifacts),
        "results": [],
    }
    for key in (
        "creation_memory_layout_hpt_ratio",
        "creation_memory_layout_ppt_ratio",
        "creation_memory_layout_fresh_vios_delete_authorized",
    ):
        del document["config"][key]
    del document["artifacts"]["creation_memory_layout"]
    inputs = recovery.round2_inputs_from_document(
        document, recovery.runner.SUBTASK_GROUPS["round2"]
    )
    assert inputs.snapshot == snapshot
    path = tmp_path / "old.json"
    path.write_text(json.dumps(document))
    state = recovery.runner.RunState(config=config)
    assert recovery.runner._restore_artifacts_from_results(state, hmc, str(path))
    assert state.artifacts.creation_memory_layout == {}
    assert recovery.creation_memory_layout_inputs_from_document(document, [1]) is None
    from unittest.mock import AsyncMock

    monkeypatch.setattr(recovery, "HMCConfig", lambda: hmc)
    capture = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(recovery.independent_baseline, "capture", capture)

    async def call(tool, **arguments):
        recovery.guard_read_only(tool, arguments)
        return (
            "PASS",
            inputs.description
            if tool == "hmc_get_lpar_description"
            else inputs.compatibility,
        )

    assert await recovery.check_round2(call, inputs) == []
    capture.assert_awaited_once_with(hmc, config.system_name, [1])


@pytest.mark.parametrize("value", [None, [], {"unknown": True}])
def test_creation_memory_artifact_explicit_malformed_values_never_default(value):
    from live_test import config as settings

    artifacts = _creation_memory_document()["artifacts"]
    artifacts["creation_memory_layout"] = value
    with pytest.raises((ValueError, TypeError)):
        settings.decode_artifacts(artifacts)
    artifacts = _creation_memory_document()["artifacts"]
    artifacts["unknown"] = None
    with pytest.raises(ValueError):
        settings.decode_artifacts(artifacts)


@pytest.mark.parametrize(
    "phase",
    [
        "before-create",
        "unknown-create",
        "post-create",
        "pre-update",
        "during-submit",
        "after-cleanup",
        "complete",
    ],
)
@pytest.mark.asyncio
async def test_sharing_recovery_selects_prefix_for_every_phase(phase):
    document = {
        "run": {"subtasks": [59], "partial": phase != "complete"},
        "config": {"system_name": _SYSTEM},
        "results": [],
    }
    inputs = recovery.lpar_config_inputs_from_document(document, [59])
    assert inputs is not None, (
        "ST59 header must select the scratch prefix before any UUID is known"
    )
    assert 59 in recovery._WITNESSED_SUBTASKS
    seen = []

    async def call(tool, **arguments):
        recovery.guard_read_only(tool, arguments)
        seen.append((tool, arguments))
        return (
            "PASS",
            "hmcpctl-live-lpar-11111111,not activated\nhmcpctl-live-lpar-22222222,not activated\nforeign,not activated",
        )

    findings = await recovery.check_lpar_config(call, inputs)
    assert len(findings) == 2
    assert len(seen) == 1
    assert all("hmcpctl-live-lpar-" in finding.what for finding in findings)


@pytest.mark.parametrize("system", [None, "", 17, [], {}, " \t\n"])
def test_sharing_recovery_refuses_unreadable_system_before_bootstrap(
    tmp_path, monkeypatch, capsys, system
):
    document = {
        "run": {"subtasks": [59]},
        "config": {"system_name": system},
        "results": [],
    }
    with pytest.raises(ValueError, match="system_name"):
        recovery.lpar_config_inputs_from_document(document, [59])
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document))

    def forbidden():
        raise AssertionError("unreadable context must refuse before credentials/HMC")

    monkeypatch.setattr(recovery.runner, "_bootstrap_config", forbidden)
    assert recovery.main(["--results", str(path)]) == 2
    assert "CLEAN" not in capsys.readouterr().out


def test_sharing_recovery_keeps_legacy_decoder():
    for subtask in (39, 47, 48, 49, 50, 51, 52, 53, 55, 56, 58, 59):
        inputs = recovery.lpar_config_inputs_from_document(
            {"config": {"system_name": _SYSTEM}}, [subtask]
        )
        assert inputs is not None and inputs.system_name == _SYSTEM
    assert recovery.lpar_config_inputs_from_document({}, [39]) is None


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"run": {"subtasks": None}},
        {
            "run": {"subtasks": [59], "partial": True},
            "config": {"system_name": _SYSTEM},
        },
        "unreadable-json",
    ],
)
def test_sharing_recovery_incomplete_never_claims_clean(
    tmp_path, monkeypatch, capsys, document
):
    path = tmp_path / "results.json"
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)

    async def clean(checks, *, users=False):
        return []

    monkeypatch.setattr(recovery, "_run_checks", clean)
    assert recovery.main(["--results", str(path)]) == 2
    assert "CLEAN" not in capsys.readouterr().out


def test_serial_procedure_exact_read_only_dispatch(monkeypatch):
    from live_test import lpar_serial_adapter

    dispatch = Mock(return_value=2)
    monkeypatch.setattr(lpar_serial_adapter, "main", dispatch)
    assert recovery.main(["--serial-procedure", "--results", "r"]) == 2
    dispatch.assert_called_once_with(["recover", "--results", "r"])


@pytest.mark.parametrize(
    "extra",
    [
        ["--packet-sha256", "s"],
        ["--output", "o"],
        ["--source-packet", "p"],
    ],
)
def test_serial_recovery_rejects_other_authority_or_output(monkeypatch, extra):
    from live_test import lpar_serial_adapter

    dispatch = Mock()
    monkeypatch.setattr(lpar_serial_adapter, "main", dispatch)
    with pytest.raises(SystemExit) as result:
        recovery.main(["--serial-procedure", "--results", "r", *extra])
    assert result.value.code == 2
    dispatch.assert_not_called()


@pytest.mark.parametrize("credentials", [False, True])
def test_processor_move_route_refuses_unarmed_proof(
    tmp_path, monkeypatch, capsys, credentials
):
    from hmcpctl.config import HMCConfig

    document = {
        "run": {
            "group": "lpar-processor-move",
            "subtasks": [61],
            "tree_clean": False,
            "partial": True,
        },
        "results": [],
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: credentials)
    monkeypatch.setattr(recovery, "HMCConfig", lambda: HMCConfig.from_mapping({}))
    assert recovery.main(["--results", str(path)]) == 2
    captured = capsys.readouterr()
    assert "CLEAN:" not in captured.out
    assert ("unconfirmed" if credentials else "credentials unavailable") in captured.err


def test_vios_source_recovery_exact_route(monkeypatch):
    from live_test import vios_ame_source

    route = Mock(return_value=2)
    monkeypatch.setattr(vios_ame_source, "main", route)
    assert (
        recovery.main(
            [
                "--vios-ame-source-packet",
                "p.json",
                "--packet-sha256",
                "a" * 64,
                "--results",
                "capture/results.json",
                "--output",
                "private",
            ]
        )
        == 2
    )
    route.assert_called_once_with(
        [
            "recover",
            "--packet",
            "p.json",
            "--packet-sha256",
            "a" * 64,
            "--results",
            "capture/results.json",
            "--output",
            "private",
        ]
    )


@pytest.mark.parametrize("other", ["--source-packet", "--activation-source-packet"])
def test_vios_source_recovery_packets_are_exclusive(monkeypatch, other):
    from live_test import vios_ame_source

    route = Mock()
    monkeypatch.setattr(vios_ame_source, "main", route)
    with pytest.raises(SystemExit) as caught:
        recovery.main(
            [
                "--vios-ame-source-packet",
                "p",
                other,
                "q",
                "--packet-sha256",
                "a" * 64,
                "--output",
                "o",
            ]
        )
    assert caught.value.code == 2
    route.assert_not_called()


@pytest.mark.parametrize("missing", ["--packet-sha256", "--output"])
def test_vios_source_recovery_requires_each_binding(monkeypatch, missing):
    from live_test import vios_ame_source

    route = Mock()
    monkeypatch.setattr(vios_ame_source, "main", route)
    args = ["--vios-ame-source-packet", "p"]
    for flag, value in {"--packet-sha256": "a" * 64, "--output": "o"}.items():
        if flag != missing:
            args.extend([flag, value])
    with pytest.raises(SystemExit) as caught:
        recovery.main(args)
    assert caught.value.code == 2
    route.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_statistics_recovery_routes_without_clean_claim(
    tmp_path, monkeypatch, capsys, failure
):
    from live_test import memory_statistics

    document = {"run": {"group": "lpar-memory-statistics"}}
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    readback = AsyncMock(side_effect=ValueError("incomplete") if failure else None)
    monkeypatch.setattr(memory_statistics, "recover_document", readback)
    assert recovery.main(["--memory-statistics", "--results", str(path)]) == (
        2 if failure else 0
    )
    readback.assert_awaited_once()
    output = capsys.readouterr()
    assert "CLEAN" not in output.out
    if failure:
        assert "unconfirmed" in output.err
    else:
        assert "reset verification unavailable" in output.out


def test_vios_boot_source_recovery_exact_route(monkeypatch):
    from live_test import vios_boot_source

    route = Mock(return_value=2)
    monkeypatch.setattr(vios_boot_source, "main", route)
    assert (
        recovery.main(
            [
                "--vios-boot-source-packet",
                "p.json",
                "--packet-sha256",
                "a" * 64,
                "--results",
                "capture/results.json",
                "--output",
                "private",
            ]
        )
        == 2
    )
    route.assert_called_once_with(
        [
            "recover",
            "--packet",
            "p.json",
            "--packet-sha256",
            "a" * 64,
            "--results",
            "capture/results.json",
            "--output",
            "private",
        ]
    )


@pytest.mark.parametrize("other", ["--source-packet", "--activation-source-packet"])
def test_vios_boot_source_recovery_packets_are_exclusive(monkeypatch, other):
    from live_test import vios_boot_source

    route = Mock()
    monkeypatch.setattr(vios_boot_source, "main", route)
    with pytest.raises(SystemExit) as caught:
        recovery.main(
            [
                "--vios-boot-source-packet",
                "p",
                other,
                "q",
                "--packet-sha256",
                "a" * 64,
                "--output",
                "o",
            ]
        )
    assert caught.value.code == 2
    route.assert_not_called()


@pytest.mark.parametrize("missing", ["--packet-sha256", "--output"])
def test_vios_boot_source_recovery_requires_each_binding(monkeypatch, missing):
    from live_test import vios_boot_source

    route = Mock()
    monkeypatch.setattr(vios_boot_source, "main", route)
    args = ["--vios-boot-source-packet", "p"]
    for flag, value in {"--packet-sha256": "a" * 64, "--output": "o"}.items():
        if flag != missing:
            args.extend([flag, value])
    with pytest.raises(SystemExit) as caught:
        recovery.main(args)
    assert caught.value.code == 2
    route.assert_not_called()


def _c07_document(phase="complete"):
    from live_test import config as settings

    from hmcpctl.config import HMCConfig

    config = settings.LiveTestConfig(
        system_name="00000000-0000-0000-0000-000000000001",
        creation_memory_case="ame-zero-vios",
        creation_memory_fresh_delete_authorized=True,
    )
    journal = {
        "invocation_id": "a" * 32,
        "candidate_commit": "b" * 40,
        "system_uuid": config.system_name,
        "hmc_identity": recovery.runner._hmc_identity(
            HMCConfig.from_mapping({"host": "hmc.test", "user": "test"})
        ),
        "case": "ame-zero-vios",
        "kind": "VirtualIOServer",
        "name": "hmcpctl-live-c07-vios-deadbeef",
        "token": None,
        "uuid": "00000000-0000-0000-0000-000000000002",
        "phase": phase,
        "preabsence": True,
        "create_returned": True,
        "fresh_delete_authorized": True,
        "requested": {"mem_mode": "ded", "mem_expansion": "0"},
        "baseline": {
            "pools": {
                "partitions": [],
                "available_processing_units": "10.0",
                "available_memory_mib": "8192",
            },
            "vios": [],
        },
        "outcome": "created-verified",
        "deletion": "confirmed-absent",
    }
    return {
        "run": {
            "group": "creation-memory",
            "subtasks": [65],
            "partial": False,
            "tested_commit": "b" * 40,
        },
        "hmc": deepcopy(journal["hmc_identity"]),
        "config": json.loads(json.dumps(dataclasses.asdict(config))),
        "artifacts": dataclasses.asdict(
            settings.LiveTestArtifacts(creation_memory=journal)
        ),
        "results": [],
    }


@pytest.mark.parametrize(
    "path,value",
    [
        (("unknown",), 1),
        (("requested", "unknown"), 1),
        (("requested", "mem_expansion"), 0),
        (("requested", "mem_expansion"), "0.0"),
        (("hmc_identity", "port"), True),
        (("hmc_identity", "unknown"), 1),
        (("token",), "not-a-vios-token"),
        (("uuid",), "invalid"),
        (("invocation_id",), "invalid"),
        (("candidate_commit",), "invalid"),
        (("preabsence",), 1),
        (("baseline", "unknown"), []),
        (("baseline", "pools", "partitions"), ["duplicate", "duplicate"]),
        (("baseline", "vios"), [{"uuid": "invalid", "name": "paging"}]),
        (("deletion",), "clean"),
        (("phase",), "invalid"),
    ],
)
def test_c07_closed_journal_decoder_refuses_malformed_intent(path, value):
    from live_test import config as settings

    document = _c07_document()
    node = document["artifacts"]["creation_memory"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises((ValueError, TypeError)):
        settings.decode_artifacts(document["artifacts"])


@pytest.mark.parametrize(
    "fault", ["journal", "candidate", "hmc", "config", "group", "selection"]
)
def test_c07_inputs_refuse_unbound_or_missing_journal(fault):
    document = _c07_document()
    if fault == "journal":
        document["artifacts"]["creation_memory"] = {}
    elif fault == "candidate":
        document["run"]["tested_commit"] = "c" * 40
    elif fault == "hmc":
        document["hmc"]["host"] = "foreign.test"
    elif fault == "config":
        document["config"]["creation_memory_case"] = "ame-positive-vios"
    elif fault == "group":
        document["run"]["group"] = "all"
    else:
        document["run"]["subtasks"] = [65, 1]
    with pytest.raises((ValueError, TypeError)):
        recovery.creation_memory_inputs_from_document(document, [65])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "prepared",
        "create-pending",
        "create-returned",
        "delete-pending",
        "deleted",
        "complete",
        "stopped-uncertain",
    ],
)
@pytest.mark.parametrize(
    "fault",
    [
        "",
        "residual",
        "baseline",
        "system",
        "unreadable",
        "candidate",
        "hmc",
        "renamed",
        "duplicate",
    ],
)
async def test_c07_recovery_all_phases_are_read_only_and_never_resume(
    monkeypatch, phase, fault
):
    from live_test import creation_memory

    from hmcpctl.config import HMCConfig

    document = _c07_document(phase)
    j = document["artifacts"]["creation_memory"]
    inputs = recovery.creation_memory_inputs_from_document(document, [65])
    monkeypatch.setattr(
        recovery,
        "HMCConfig",
        lambda: HMCConfig.from_mapping(
            {"host": "foreign.test" if fault == "hmc" else "hmc.test", "user": "test"}
        ),
    )
    monkeypatch.setattr(
        creation_memory,
        "_candidate",
        lambda: ("c" if fault == "candidate" else "b") * 40,
    )
    calls = []

    async def call(tool, **args):
        recovery.guard_read_only(tool, args)
        calls.append(tool)
        if fault == "unreadable":
            return "FAIL", None
        if tool == "hmc_get_system":
            return "PASS", {
                "UUID": "invalid" if fault == "system" else j["system_uuid"],
                "Resource": {"SystemName": "system-test"},
            }
        rows = (
            [
                {
                    "UUID": j["uuid"],
                    "Resource": {
                        "PartitionName": "renamed" if fault == "renamed" else j["name"]
                    },
                }
            ]
            if fault in {"residual", "renamed", "duplicate"}
            else []
        )
        if fault == "duplicate":
            rows *= 2
        if tool == "hmc_list_lpars":
            return "PASS", {"entries": [], "unreadable_systems": []}
        if tool == "hmc_list_vios":
            return "PASS", {"entries": rows}
        if args["cmd"].endswith("-F name"):
            return "PASS", "\n".join(r["Resource"]["PartitionName"] for r in rows)
        if args["cmd"].endswith("curr_avail_sys_proc_units"):
            return "PASS", "10.0"
        return "PASS", "4096,256" if fault == "baseline" else "8192,256"

    if fault in {"unreadable", "system", "candidate", "hmc", "duplicate"}:
        with pytest.raises(recovery.StateUnreadable):
            await recovery.check_creation_memory(call, inputs)
    else:
        findings = await recovery.check_creation_memory(call, inputs)
        assert bool(findings) is (phase != "complete" or fault != "")
    assert all(
        t in {"hmc_get_system", "hmc_run_command", "hmc_list_lpars", "hmc_list_vios"}
        for t in calls
    )


def test_c07_real_unarmed_writer_recovery_route_and_forgery(
    tmp_path, monkeypatch, capsys
):
    from live_test import config as settings
    from live_test import creation_memory as arm

    from hmcpctl.config import HMCConfig

    @asynccontextmanager
    async def served():
        yield object()

    monkeypatch.setattr(recovery.runner, "served_client", served)
    monkeypatch.setattr(recovery.runner, "served_schemas", AsyncMock(return_value={}))
    monkeypatch.setattr(
        arm, "HMCClient", lambda *args: pytest.fail("unarmed must not connect")
    )
    path = tmp_path / "result.json"
    assert (
        asyncio.run(
            recovery.runner.main(
                group=arm.GROUP,
                results_path=str(path),
                config=settings.LiveTestConfig(),
                hmc_config=HMCConfig.from_mapping({}),
            )
        )
        == 0
    )
    document = json.loads(path.read_text())
    assert recovery.creation_memory_skip_reason(document, [65])
    monkeypatch.setattr(
        recovery.runner,
        "_bootstrap_config",
        lambda: pytest.fail("unarmed recovery must not authenticate"),
    )
    assert recovery.main(["--results", str(path)]) == 0
    assert "SKIPPED  ST65" in capsys.readouterr().out
    for fault in ("partial", "note", "missing-journal", "extra-row", "armed"):
        forged = deepcopy(document)
        if fault == "partial":
            forged["run"]["partial"] = True
        elif fault == "note":
            forged["results"][0]["note"] = "different"
        elif fault == "missing-journal":
            forged["artifacts"].pop("creation_memory")
        elif fault == "extra-row":
            forged["results"].append(deepcopy(forged["results"][0]))
        else:
            forged["config"]["creation_memory_case"] = "ame-zero-vios"
            forged["config"]["creation_memory_fresh_delete_authorized"] = True
        assert recovery.creation_memory_skip_reason(forged, [65]) is None
        path.write_text(json.dumps(forged))
        assert recovery.main(["--results", str(path)]) == 2
        assert "CLEAN" not in capsys.readouterr().out


def test_c07_saved_journal_is_bounded_before_semantic_decoding():
    from live_test import config as settings

    document = _c07_document()
    document["artifacts"]["creation_memory"]["baseline"]["pools"]["partitions"] = [
        "x" * (5 * 1024 * 1024)
    ]
    with pytest.raises(ValueError, match="5 MiB"):
        settings.decode_artifacts(document["artifacts"])


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_statistics_raw_recovery_refuses_nonregular_without_bootstrap(
    tmp_path, monkeypatch, kind
):
    import os

    path = tmp_path / "record"
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    bootstrap = Mock(side_effect=AssertionError("nonregular record reached bootstrap"))
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", bootstrap)
    assert recovery.main(["--memory-statistics", "--results", str(path)]) == 2
    bootstrap.assert_not_called()


def test_boot_start_recovery_uses_original_results_without_effect_grant(monkeypatch):
    from unittest.mock import Mock

    from live_test import lpar_boot_start

    route = Mock(return_value=2)
    monkeypatch.setattr(lpar_boot_start, "main", route)
    assert recovery.main(["--boot-start-procedure", "--results", "private.json"]) == 2
    route.assert_called_once_with(["recover", "--results", "private.json"])
