"""Independent pcie recovery admission and read-only witnesses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hmcpctl.operations.lpar.ownership import parse_lpar_ownership_caller_token
from hmcpctl.ssh.profiles import profile_io_slot_rows_command
from hmcpctl.ssh.transport import HMCCLIError
from live_test.arms.dedicated_pcie import (
    _DEFAULT_DEDICATED_PROFILE,
    _io_slots_contains,
    partition_not_found,
    select_profile_io_slots,
)
from live_test.recovery.common import NOT_ACTIVATED, Finding, StateUnreadable


@dataclass(frozen=True)
class RecoveryInputs:
    """What one run created, as recorded in its results document."""

    system_name: str
    run_marker: str
    fixture_lpar: str
    drc_index: str | None
    baseline_io_slots: str | None
    profile_name: str


def inputs_from_document(document: Any) -> RecoveryInputs | None:
    """Read a run's recovery inputs, or `None` when it recorded none.

    A run that never reached ST29 created nothing, so an absent marker means
    there is nothing to look for — not that the check could not run.
    """
    if not isinstance(document, dict):
        return None
    artifacts = document.get("artifacts")
    config = document.get("config")
    if not isinstance(artifacts, dict) or not isinstance(config, dict):
        return None
    marker = artifacts.get("pcie_run_marker")
    fixture = artifacts.get("pcie_fixture_lpar")
    system = config.get("dedicated_pcie_system_name")
    if not marker or not fixture or not system:
        return None
    return RecoveryInputs(
        system_name=str(system),
        run_marker=str(marker),
        fixture_lpar=str(fixture),
        drc_index=artifacts.get("pcie_drc_index"),
        baseline_io_slots=artifacts.get("pcie_baseline_io_slots"),
        # The arm's own default, imported rather than restated: a run that
        # left the key unset records an empty string here, and a second
        # spelling of the fallback would query and remediate a profile name
        # the arm never used.
        profile_name=str(
            config.get("dedicated_pcie_profile_name") or _DEFAULT_DEDICATED_PROFILE
        ),
    )


async def _read_slots(call, inputs: RecoveryInputs) -> list[dict[str, Any]]:
    """The system's dedicated slots, and the reachability probe for every check.

    This runs first and raises rather than returning empty. It proves the HMC is
    answering, but not that every later lookup will succeed: a later check still
    reads only the HMC's "no such partition" answer as absent, and any other
    failure as unreadable.
    """
    status, data = await call(
        "hmc_list_dedicated_pcie_slots", system_name_or_uuid=inputs.system_name
    )
    if status != "PASS" or not isinstance(data, dict):
        raise StateUnreadable(
            f"could not list dedicated slots on {inputs.system_name} ({status})"
        )
    return [item for item in (data.get("items") or []) if isinstance(item, dict)]


async def _surviving_fixture(call, inputs: RecoveryInputs) -> Finding | None:
    """Whether a partition this run created is still there.

    Ownership is confirmed by the run marker before reporting, so a partition
    that merely shares the name is never attributed to this run — the same rule
    the arm applies before it will delete anything.

    Only HSCL8012, the HMC's "no such partition" answer, reads as gone (see
    `partition_not_found`). Any other failure -- an authentication refusal, a
    lost connection, a different HSCL code -- or an answer that is not a
    description says nothing about whether the partition survives, so it raises
    rather than reporting the system clean. So does a description carrying this
    run's marker in a stamp this code cannot parse.
    """
    status, data = await call(
        "hmc_get_lpar_description",
        system_name_or_uuid=inputs.system_name,
        lpar_name_or_uuid=inputs.fixture_lpar,
    )
    if partition_not_found(status, data):
        return None
    if status != "PASS" or not isinstance(data, str):
        raise StateUnreadable(
            f"could not look up {inputs.fixture_lpar} on {inputs.system_name} "
            f"({status}): {getattr(data, 'message', data)!s}"
        )
    if parse_lpar_ownership_caller_token(data) != inputs.run_marker:
        # The marker is per-run random, so a description that names it came
        # from this run even when its stamp does not parse — typically a run on
        # pre-rename code, whose `[hmc-mcp ...]` stamp this code reads as unowned.
        if f"[caller {inputs.run_marker}]" in data:
            raise StateUnreadable(
                f"{inputs.fixture_lpar} on {inputs.system_name} carries this run's "
                f"marker {inputs.run_marker} in a stamp this checkout cannot read; "
                "re-run the check from the run's tested commit"
            )
        return None
    state_status, state = await call(
        "hmc_get_lpar_state",
        system_name_or_uuid=inputs.system_name,
        lpar_name_or_uuid=inputs.fixture_lpar,
    )
    if state_status != "PASS" or not isinstance(state, str):
        raise StateUnreadable(
            f"could not read the state of {inputs.fixture_lpar} on "
            f"{inputs.system_name} ({state_status}), so its remedy cannot be chosen"
        )
    remedy = f"hmc rmsyscfg -m {inputs.system_name} -r lpar -n {inputs.fixture_lpar}"
    # REST's PartitionState is lower case ("not activated"); only the CLI title-cases it.
    if state.strip().lower() != NOT_ACTIVATED.lower():
        # The HMC refuses to delete a partition outside Not Activated (#950): a
        # run interrupted while its fixture is running needs a shutdown first.
        remedy = (
            f"hmc chsysstate -m {inputs.system_name} -r lpar -n {inputs.fixture_lpar} "
            f"-o shutdown --immed; once it is {NOT_ACTIVATED}, {remedy}"
        )
    return Finding(
        "surviving partition",
        f"{inputs.fixture_lpar} on {inputs.system_name} still exists in state "
        f"{state!r} and carries this run's marker {inputs.run_marker}",
        remedy,
    )


def _stranded_slot(
    slots: list[dict[str, Any]], inputs: RecoveryInputs
) -> Finding | None:
    """Whether the dedicated slot is still owned rather than back in the pool."""
    if inputs.drc_index is None:
        return None
    for item in slots:
        if item.get("drc_index") != inputs.drc_index:
            continue
        owner = (item.get("owner_lpar") or "").strip()
        if not owner or owner == "null":
            return None
        return Finding(
            "stranded slot",
            f"dedicated slot {inputs.drc_index} on {inputs.system_name} is owned "
            f"by {owner}; the run left it assigned",
            f"hmc chsyscfg -m {inputs.system_name} -r prof -i "
            f'"name={inputs.profile_name},lpar_name={owner},'
            f'io_slots-={inputs.drc_index}//0"',
        )
    return None


async def _profile_drift(call, inputs: RecoveryInputs) -> Finding | None:
    """Whether the fixture profile's io_slots still differs from its baseline."""
    if inputs.baseline_io_slots is None or inputs.drc_index is None:
        return None
    status, data = await call(
        "hmc_run_command",
        cmd=profile_io_slot_rows_command(inputs.system_name),
    )
    if status != "PASS" or not isinstance(data, str):
        raise StateUnreadable(
            f"could not read profile io_slots for {inputs.fixture_lpar} ({status})"
        )
    try:
        observed = select_profile_io_slots(
            data, inputs.fixture_lpar, inputs.profile_name
        )
    except HMCCLIError as error:
        raise StateUnreadable(
            f"profile io_slots for {inputs.fixture_lpar} is unreadable: {error}"
        ) from error
    if observed == inputs.baseline_io_slots:
        return None
    still_assigned = _io_slots_contains(observed, inputs.drc_index)
    return Finding(
        "profile drift",
        f"profile io_slots for {inputs.fixture_lpar} is {observed!r}, not the "
        f"captured baseline {inputs.baseline_io_slots!r}"
        + (f"; slot {inputs.drc_index} is still listed" if still_assigned else ""),
        f"hmc chsyscfg -m {inputs.system_name} -r prof -i "
        f'"name={inputs.profile_name},lpar_name={inputs.fixture_lpar},'
        f'io_slots={inputs.baseline_io_slots}"',
    )


async def check(call, inputs: RecoveryInputs) -> list[Finding]:
    """Every stranded condition, in the order an operator should clear them."""
    slots = await _read_slots(call, inputs)
    stranded = _stranded_slot(slots, inputs)
    try:
        fixture = await _surviving_fixture(call, inputs)
    except StateUnreadable as unreadable:
        unreadable.findings = [stranded] if stranded else []
        raise
    findings = [finding for finding in (fixture, stranded) if finding]
    if fixture is None:
        # A profile belongs to its partition. With the fixture gone there is no
        # profile left to have drifted, and asking for one answers HSCL8012 —
        # which would otherwise be read as an unreadable system and exit 2 on
        # every clean run.
        return findings
    try:
        drift = await _profile_drift(call, inputs)
    except StateUnreadable as unreadable:
        unreadable.findings = findings
        raise
    return findings + [drift] if drift else findings
