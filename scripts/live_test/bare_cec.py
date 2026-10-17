"""Bare-CEC live arm — issue #876 (dispatched as subtask 25, recorded as rows 35).

Creates a run-unique owner-stamped partition, gives it a dedicated slot through
`hmc_assign_dedicated_pcie_slot`, records a PowerOn with no partition profile,
activates it to SMS against its own profile, inspects that job, reads its
reference codes, captures its console, exercises the PowerOff variants, then
unassigns and deletes it. Every promoting step goes through `record_verified`,
under scenario `st35-bare-cec`.

Design: docs/workflow/specs/2026-09-22-bare-cec-live-arm-design.md. The
configuration, baseline, fixture-create and cleanup guards are the dedicated
arm's (`pcie.py`, ADR 0163), so preflight and `live_test_recovery.py` read this
arm exactly as they read that one. Its own rows are numbered 35; the shared
helpers keep writing rows 29, 30 and 34 with their dedicated-arm wording.

Configuration, from the ADR 0115 `.env`:
  LIVE_TEST_DEDICATED_PCIE_* — the dedicated arm's four keys, unchanged
  LIVE_TEST_ACCEPT_PLATFORM_DUMP — `true` runs the dumprestart step; `false` or
      unset SKIPs it; any other value SKIPs the arm

The arm runs only when `HMC_AUTHORIZE_POWER_OPERATIONS` is on, so the evidence
covers the ownership-guarded power path.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from hmcpctl.config import HMCConfig
from hmcpctl.jobs import (
    FAILED_JOB_STATUSES,
    SUCCESSFUL_JOB_STATUSES,
    JobOutcome,
    job_identifier,
    job_outcome,
)
from hmcpctl.ssh.profiles import (
    parse_profile_io_slot_rows,
    profile_io_slot_rows_command,
)
from hmcpctl.ssh.transport import HMCCLIError

from . import cli, pcie
from .metrics import _as_outcome
from .observation import CLEANUP, Assertion, CallFailure, ExpectedOutcome

if TYPE_CHECKING:
    from live_test_runner import LiveTestConfig, RunState

_ROW = 35
_SCENARIO = "st35-bare-cec"

#: Explicit shared-processor sizing, so the create does not lean on the SSH
#: path's processing-unit defaults (#938). Small enough for a firmware boot on
#: any system the envelope admits; unmeasured until the #879 window.
_RESOURCES: dict[str, Any] = {
    "min_memory": 1024,
    "desired_memory": 2048,
    "max_memory": 4096,
    "dedicated": False,
    "min_procs": 0.1,
    "desired_procs": 0.5,
    "max_procs": 1.0,
    "min_vcpus": 1,
    "desired_vcpus": 1,
    "max_vcpus": 1,
    "uncapped": True,
}

_JOB_TIMEOUT_S = 900
_JOB_POLL_INTERVAL_S = 10
#: Five minutes for a state to settle after its job completes. Unmeasured until
#: the #879 window: a FAIL whose data shows `starting` may be this budget, not a
#: refusal.
_STATE_POLL_ATTEMPTS = 30
_STATE_POLL_DELAY_S = 10.0

_NOT_ACTIVATED = frozenset({"not activated"})
_FIRMWARE_STATES = frozenset({"open firmware", "running"})
_SETTLED_STATES = _NOT_ACTIVATED | _FIRMWARE_STATES

_UUID_AT_END = re.compile(
    r"/([0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12})/?\Z"
)

#: Before #939, `hmc_create_lpar` left a new partition without an applied profile, so a
#: PowerOn naming no profile had no current configuration to activate and HSCL3680 was
#: expected. Since #939 (PR #986) `hmc_create_lpar` applies the profile by default, so
#: this PowerOn normally targets an applied partition, where the outcome is unconfirmed
#: (#879 records it) — except a create whose `apply_profile` step failed
#: (`_apply_created_profile` catches `HMCCLIError` without raising) leaves it unapplied,
#: where HSCL3680 still applies. Transient: an environment refusal, not a product gap.
_NO_PROFILE_ACTIVATION_REFUSED = ExpectedOutcome(
    operation="lpar.power_on",
    variant="no-current-configuration",
    reason="a PowerOn naming no profile normally targets an applied partition (#939) "
    "with an unconfirmed outcome on hardware — #879 records it; a create whose "
    "apply_profile step failed leaves it unapplied, where HSCL3680 still applies",
    error_codes=frozenset({"HSCL3680"}),
    transient=True,
)
#: osshutdown asks the operating system to shut down over RMC; a partition at
#: SMS has neither, so the HMC refuses it. Observed on a V10R3 HMC (issue #1160)
#: as HSCL0DB4, whose message says the running image does not support remote
#: execution of the task and never names RMC.
_OSSHUTDOWN_WITHOUT_RMC = ExpectedOutcome(
    operation="lpar.power_off",
    variant="osshutdown-without-rmc",
    reason="osshutdown needs an operating system with an active RMC connection; "
    "a partition at SMS has neither — expected refusal",
    error_codes=frozenset({"HSCL0DB4"}),
    transient=True,
)


@dataclass
class _Run:
    """What the arm has established, read by the teardown and its deferred rows."""

    fixture: pcie._DedicatedFixture
    accept_dump: bool
    create_attempted: bool = False
    create_ok: bool = False
    partition_created: bool = False
    assign_holds: tuple[bool, bool] | None = None
    slot_assigned: bool = False


def _record_held(
    state: RunState,
    tool: str,
    *,
    assertions: list[Assertion],
    cleanup: str,
    data: Any,
) -> None:
    """Record a step judged as `record_verified` would judge it, without promoting it.

    The lpar-power arm (subtask 41) owns these operations' observations, and the
    catalog keeps one per operation, so this arm's would overwrite it (#1346).
    """
    if cleanup not in CLEANUP:
        raise ValueError(f"cleanup disposition is not a known value: {cleanup!r}")
    unmet = [item.id for item in assertions if not item.holds]
    if cleanup not in {"passed", "not-required"}:
        unmet.append(f"cleanup {cleanup}")
    note = "unmet: " + ", ".join(unmet) if unmet else ""
    state.record(_ROW, tool, "FAIL" if unmet else "PASS", data, note)


def _power_operations_authorized() -> bool:
    """Whether the server this run builds enforces the ADR 0092 power guard."""
    try:
        return HMCConfig().authorize_power_operations
    except ValueError:
        return False


def _dump_opt_in(config: LiveTestConfig) -> bool | None:
    """The operator's platform-dump decision, or None for an unrecognised value."""
    return {"": False, "false": False, "true": True}.get(
        config.accept_platform_dump.strip().lower()
    )


def _field(data: Any, name: str) -> Any:
    """Read *name* from a tool result served as a mapping or as a generated model."""
    if isinstance(data, dict):
        return data.get(name)
    return getattr(data, name, None)


def _power_job(data: Any) -> dict[str, Any] | None:
    """The job a power tool returned: PowerOff returns it, PowerOn wraps it."""
    if isinstance(data, dict) and "Resource" in data:
        return data
    job = _field(data, "job")
    return job if isinstance(job, dict) else None


def _job_failure(status: str, data: Any) -> CallFailure | None:
    """The failure a completed `wait=true` power call carried inside its job.

    `wait=true` returns the last polled job without raising, so a refused
    activation arrives as a PASS carrying a failed job, and an expired wait as a
    PASS carrying a job that is not terminal yet. Both are failures here.
    """
    job = _power_job(data) if status == "PASS" else None
    if job is None:
        return None
    outcome = job_outcome("", job)
    if outcome.timed_out:
        message = f"JobTimedOut: job still {outcome.status!r} when the wait expired"
        return CallFailure("JobTimedOut", message, "", None, False)
    if outcome.status in FAILED_JOB_STATUSES:
        return CallFailure("JobFailed", f"JobFailed: {outcome.error}", "", None, False)
    return None


def _job_assertions(outcome: JobOutcome | None, job_id: str) -> list[Assertion]:
    """ST12's job postconditions, restated: the registry test reads them per module."""
    return [
        Assertion("job-found", bool(outcome and outcome.found)),
        Assertion("job-identity-matches", bool(outcome and outcome.job_id == job_id)),
        Assertion(
            "job-status-successful",
            bool(outcome and outcome.status in SUCCESSFUL_JOB_STATUSES),
        ),
    ]


def _identity_matches(fixture: pcie._DedicatedFixture, observed: Any) -> bool:
    return observed.caller_token == fixture.run_marker and (
        observed.lpar_uuid == fixture.lpar_uuid
    )


def _manual_recovery(fixture: pcie._DedicatedFixture, why: str) -> str:
    """The recovery for a fixture that may still be active and hold its slot."""
    arm = fixture.config
    system, name = shlex.quote(arm.system_name), shlex.quote(fixture.lpar_name)
    return (
        f"MANUAL RECOVERY REQUIRED: partition {fixture.lpar_name!r} on "
        f"{arm.system_name!r} {why}, so its slot was not unassigned and it was "
        "NOT deleted. Once you have confirmed it is this run's, in order: "
        f"`chsysstate -m {system} -r lpar -n {name} -o shutdown --immed`; once it "
        f"is Not Activated and its profile still lists slot {fixture.drc_index!r}, "
        f"`{pcie._change_io_slots_command(fixture, add=False)}`; then "
        f"`rmsyscfg -r lpar -m {system} -n {name}`."
    )


async def _read_state(
    client: Client,
    state: RunState,
    fixture: pcie._DedicatedFixture,
    wanted: frozenset[str],
    attempts: int = _STATE_POLL_ATTEMPTS,
) -> str | None:
    """Poll the partition state until it is in *wanted*; return the last one read."""
    observed = None
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(_STATE_POLL_DELAY_S)
        st, data = await state.call(
            client,
            "hmc_get_lpar_state",
            lpar_name_or_uuid=fixture.lpar_uuid,
            system_name_or_uuid=fixture.config.system_name,
        )
        observed = data if st == "PASS" and isinstance(data, str) else None
        if observed in wanted:
            break
    return observed


async def _power_on(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture, profile_uuid: str
) -> tuple[str, Any, CallFailure | None]:
    """Activate the fixture to SMS against *profile_uuid* and wait; record nothing."""
    st, data = await state.call(
        client,
        "hmc_power_on_lpar",
        lpar_name_or_uuid=fixture.lpar_uuid,
        system_name_or_uuid=fixture.config.system_name,
        partition_profile_uuid=profile_uuid,
        boot_mode="sms",
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    return st, data, _job_failure(st, data)


async def _power_off(
    client: Client,
    state: RunState,
    fixture: pcie._DedicatedFixture,
    *,
    immediate: bool,
    restart: bool = False,
    operation: str = "shutdown",
) -> tuple[str, Any, CallFailure | None]:
    """Submit one PowerOff for the fixture and wait for it; record nothing."""
    st, data = await state.call(
        client,
        "hmc_power_off_lpar",
        lpar_name_or_uuid=fixture.lpar_uuid,
        system_name_or_uuid=fixture.config.system_name,
        immediate=immediate,
        restart=restart,
        operation=operation,
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    return st, data, _job_failure(st, data)


def _record_power(
    state: RunState, label: str, st: str, data: Any, failure: CallFailure | None
) -> bool:
    if failure is not None:
        state.record(_ROW, label, "FAIL", failure)
        return False
    state.record(_ROW, label, st, data)
    return st == "PASS"


def _record_state(
    state: RunState, label: str, observed: str | None, wanted: frozenset[str]
) -> bool:
    reached = observed in wanted
    state.record(
        _ROW,
        label,
        "PASS" if reached else "FAIL",
        f"partition state {observed!r}, expected one of {sorted(wanted)}",
    )
    return reached


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


async def _activation_profile_uuid(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture
) -> str | None:
    """The UUID of the fixture's one profile, which must be the configured one.

    A PowerOn names a partition profile by UUID. The ADR 0165 table shows the
    fixture's profiles by name; `AssociatedPartitionProfile` links the one the
    partition was created with. Only when the table holds exactly the configured
    profile is that link known to name it.
    """
    arm = fixture.config
    names: list[str] | None = None
    st, data = await state.call(
        client, "hmc_run_command", cmd=profile_io_slot_rows_command(arm.system_name)
    )
    if st == "PASS" and isinstance(data, str):
        try:
            names = [
                row["name"]
                for row in parse_profile_io_slot_rows(data)
                if row["lpar_name"] == fixture.lpar_name
            ]
        except HMCCLIError:
            names = None
    st_lpar, lpar = await state.call(
        client,
        "hmc_get_lpar",
        lpar_name_or_uuid=fixture.lpar_uuid,
        system_name_or_uuid=arm.system_name,
    )
    # `hmc_get_lpar` serves the Atom entry: the partition's fields sit under `Resource`.
    resource = _field(lpar, "Resource") if st_lpar == "PASS" else None
    link = _field(resource, "AssociatedPartitionProfile")
    if isinstance(link, dict) and "@attrs" in link:
        link = link["@attrs"]
    href = link.get("href") if isinstance(link, dict) else None
    match = _UUID_AT_END.search(href) if isinstance(href, str) else None
    profile_uuid = match.group(1) if match and names == [arm.profile_name] else None
    state.record(
        _ROW,
        "activation profile",
        "PASS" if profile_uuid else "FAIL",
        f"profiles of {fixture.lpar_name!r}: {names!r}, expected [{arm.profile_name!r}]; "
        f"associated profile uuid {match.group(1) if match else None!r}",
    )
    return profile_uuid


async def _assign(client: Client, state: RunState, run: _Run) -> bool:
    fixture = run.fixture
    arm = fixture.config
    st, data = await state.call(
        client,
        "hmc_assign_dedicated_pcie_slot",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.lpar_uuid,
        profile_name=arm.profile_name,
        drc_index=fixture.drc_index,
    )
    state.record(_ROW, "hmc_assign_dedicated_pcie_slot (call)", st, data)
    # Read back whatever the call reported: a lost response has still written.
    applied = await pcie._read_profile_io_slots(client, state, fixture)
    run.slot_assigned = applied is not None and pcie._io_slots_contains(
        applied, str(fixture.drc_index)
    )
    if run.slot_assigned:
        fixture.applied_io_slots = applied
    run.assign_holds = (st == "PASS", run.slot_assigned)
    state.record(
        _ROW,
        "profile io_slots readback (post-assign)",
        "PASS" if run.slot_assigned else "FAIL",
        f"io_slots={applied!r} expected to list drc_index={fixture.drc_index!r}",
    )
    return st == "PASS" and run.slot_assigned


async def _no_profile_activation(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture
) -> bool:
    """Record what a PowerOn naming no profile does to a fresh partition (L3).

    Returns True once the partition is back at Not Activated, whatever the
    PowerOn did; False when that cannot be established.
    """
    label = "hmc_power_on_lpar (no partition profile)"
    st, data = await state.call(
        client,
        "hmc_power_on_lpar",
        expected=[_NO_PROFILE_ACTIVATION_REFUSED],
        lpar_name_or_uuid=fixture.lpar_uuid,
        system_name_or_uuid=fixture.config.system_name,
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    failure = _job_failure(st, data)
    if failure is None:
        state.record_with_expected(
            _ROW, label, st, data, [_NO_PROFILE_ACTIVATION_REFUSED]
        )
    elif _NO_PROFILE_ACTIVATION_REFUSED.matches(failure):
        state.record(
            _ROW, label, "SKIP", failure, _NO_PROFILE_ACTIVATION_REFUSED.reason
        )
    else:
        state.record(_ROW, label, "FAIL", failure)
        if failure.exception_type == "JobTimedOut":
            return False
    observed = await _read_state(client, state, fixture, _SETTLED_STATES)
    state.record(
        _ROW,
        "no-profile activation outcome",
        "PASS" if observed in _SETTLED_STATES else "FAIL",
        f"partition state after a PowerOn naming no profile: {observed!r}",
    )
    if observed in _NOT_ACTIVATED:
        return True
    if observed not in _FIRMWARE_STATES:
        return False
    st, data, failure = await _power_off(client, state, fixture, immediate=True)
    off_label = "hmc_power_off_lpar (after no-profile activation)"
    if not _record_power(state, off_label, st, data, failure):
        return False
    observed = await _read_state(client, state, fixture, _NOT_ACTIVATED)
    return _record_state(
        state, "state after no-profile power off", observed, _NOT_ACTIVATED
    )


async def _activate_to_sms(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture, profile_uuid: str
) -> bool:
    st, data, failure = await _power_on(client, state, fixture, profile_uuid)
    job = _power_job(data) if st == "PASS" else None
    job_status = job_outcome("", job).status if job is not None else None
    reached = None
    if st == "PASS" and failure is None:
        reached = await _read_state(client, state, fixture, _FIRMWARE_STATES)
        state.record_verified(
            _ROW,
            "hmc_get_lpar_state",
            operation="lpar.get_state",
            scenario=_SCENARIO,
            assertions=[Assertion("state-read-returned-a-state", reached is not None)],
            cleanup="not-required",
            data={"partition_state": reached},
        )
    _record_held(
        state,
        "hmc_power_on_lpar",
        assertions=[
            Assertion(
                "activation-job-successful",
                failure is None and job_status in SUCCESSFUL_JOB_STATUSES,
            ),
            Assertion("lpar-reached-firmware", reached in _FIRMWARE_STATES),
        ],
        cleanup="not-required",
        data=failure or {"job_status": job_status, "partition_state": reached},
    )
    job_id = job_identifier(job) if job is not None else None
    if job_id is None:
        state.skip(_ROW, "hmc_get_job", "the activation returned no job identifier")
        state.skip(
            _ROW, "hmc_wait_for_job", "the activation returned no job identifier"
        )
    else:
        await _inspect_job(client, state, job_id)
    if reached in _FIRMWARE_STATES:
        await cli.observe_state(state, fixture.lpar_uuid, reached)
    return reached in _FIRMWARE_STATES


async def _inspect_job(client: Client, state: RunState, job_id: str) -> None:
    await cli.observe_job(state, job_id)
    st, data = await state.call(client, "hmc_get_job", job_id=job_id)
    state.record_verified(
        _ROW,
        "hmc_get_job",
        operation="job.get",
        scenario=_SCENARIO,
        assertions=_job_assertions(
            job_outcome(
                job_id, data if st == "PASS" and isinstance(data, dict) else None
            ),
            job_id,
        ),
        cleanup="not-required",
        data=data,
    )
    st, data = await state.call(
        client, "hmc_wait_for_job", job_id=job_id, timeout_seconds=60, poll_interval=5
    )
    state.record_verified(
        _ROW,
        "hmc_wait_for_job",
        operation="job.wait",
        scenario=_SCENARIO,
        assertions=_job_assertions(_as_outcome(data) if st == "PASS" else None, job_id),
        cleanup="not-required",
        data=data,
    )


async def _observe(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture
) -> None:
    """Read reference codes and capture the console of the activated partition."""
    arm = fixture.config
    st, data = await state.call(
        client,
        "hmc_read_lpar_refcodes",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.lpar_name,
        count=5,
    )
    rows = data if st == "PASS" and isinstance(data, list) else None
    state.record_verified(
        _ROW,
        "hmc_read_lpar_refcodes",
        operation="lpar.list_refcodes",
        scenario=_SCENARIO,
        assertions=[
            # Non-empty: the tool answers an empty list for a partition it does
            # not know too, so an empty read proves nothing about the fixture.
            Assertion("refcodes-returned", bool(rows)),
            Assertion(
                "refcodes-name-the-fixture",
                bool(rows)
                and all(
                    isinstance(row, dict) and row.get("lpar_name") == fixture.lpar_name
                    for row in rows
                ),
            ),
        ],
        cleanup="not-required",
        data=data,
    )

    await cli.observe_refcodes(state, arm.system_name, fixture.lpar_name)

    # The idle cap equals the duration: a partition parked at SMS has already
    # drawn its menu and prints nothing more, which the default 10 s would read
    # as the end of the capture.
    st, data = await state.call(
        client,
        "hmc_capture_lpar_console",
        lpar_name_or_uuid=fixture.lpar_uuid,
        system_name_or_uuid=arm.system_name,
        duration_seconds=30.0,
        idle_timeout_seconds=30.0,
    )
    capture = data if st == "PASS" and isinstance(data, dict) else {}
    _record_held(
        state,
        "hmc_capture_lpar_console",
        assertions=[
            Assertion(
                "console-captured",
                st == "PASS" and capture.get("stop_reason") != "error",
            ),
            Assertion("console-released", capture.get("released") is True),
        ],
        cleanup="not-required",
        # The console bytes are HMC output this document keeps unredacted, so
        # only their shape is recorded.
        data=data
        if st != "PASS"
        else {
            key: capture.get(key)
            for key in ("stop_reason", "released", "bytes_captured")
        },
    )


async def _power_off_to_not_activated(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture
) -> bool:
    st, data, failure = await _power_off(client, state, fixture, immediate=True)
    job = _power_job(data) if st == "PASS" else None
    job_status = job_outcome("", job).status if job is not None else None
    observed = (
        await _read_state(client, state, fixture, _NOT_ACTIVATED)
        if st == "PASS" and failure is None
        else None
    )
    _record_held(
        state,
        "hmc_power_off_lpar",
        assertions=[
            Assertion(
                "power-off-job-successful",
                failure is None and job_status in SUCCESSFUL_JOB_STATUSES,
            ),
            Assertion("lpar-not-activated", observed in _NOT_ACTIVATED),
        ],
        cleanup="not-required",
        data=failure or {"job_status": job_status, "partition_state": observed},
    )
    return observed in _NOT_ACTIVATED


async def _reactivate_and_restart(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture, profile_uuid: str
) -> bool:
    st, data, failure = await _power_on(client, state, fixture, profile_uuid)
    if not _record_power(state, "hmc_power_on_lpar (re-activate)", st, data, failure):
        return False
    observed = await _read_state(client, state, fixture, _FIRMWARE_STATES)
    if not _record_state(
        state, "state after re-activation", observed, _FIRMWARE_STATES
    ):
        return False
    st, data, failure = await _power_off(
        client, state, fixture, immediate=True, restart=True
    )
    if not _record_power(
        state, "hmc_power_off_lpar (immediate restart)", st, data, failure
    ):
        return False
    observed = await _read_state(client, state, fixture, _FIRMWARE_STATES)
    return _record_state(
        state, "state after immediate restart", observed, _FIRMWARE_STATES
    )


async def _osshutdown_refusal(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture
) -> bool:
    """Record osshutdown on a partition with no OS and no RMC as the expected refusal."""
    label = "hmc_power_off_lpar (osshutdown)"
    st, data = await state.call(
        client,
        "hmc_power_off_lpar",
        expected=[_OSSHUTDOWN_WITHOUT_RMC],
        lpar_name_or_uuid=fixture.lpar_uuid,
        system_name_or_uuid=fixture.config.system_name,
        operation="osshutdown",
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    failure = _job_failure(st, data)
    if failure is None:
        state.record_with_expected(_ROW, label, st, data, [_OSSHUTDOWN_WITHOUT_RMC])
    elif _OSSHUTDOWN_WITHOUT_RMC.matches(failure):
        state.record(_ROW, label, "SKIP", failure, _OSSHUTDOWN_WITHOUT_RMC.reason)
    else:
        state.record(_ROW, label, "FAIL", failure)
        if failure.exception_type == "JobTimedOut":
            return False
    observed = await _read_state(client, state, fixture, _FIRMWARE_STATES)
    return _record_state(state, "state after osshutdown", observed, _FIRMWARE_STATES)


async def _dump_restart(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture
) -> bool:
    # The crash is its own tool and grant (ADR 0188), not a hmc_power_off_lpar operation.
    st, data = await state.call(
        client,
        "hmc_dump_restart_lpar",
        lpar_name_or_uuid=fixture.lpar_uuid,
        system_name_or_uuid=fixture.config.system_name,
        allow_dump_restart=True,
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    failure = _job_failure(st, data)
    if not _record_power(state, "hmc_dump_restart_lpar", st, data, failure):
        return False
    observed = await _read_state(client, state, fixture, _FIRMWARE_STATES)
    return _record_state(state, "state after dumprestart", observed, _FIRMWARE_STATES)


async def _run_steps(client: Client, state: RunState, run: _Run) -> None:
    """Steps 1-13 of the spec; any unmet precondition returns to the teardown."""
    fixture = run.fixture
    run.create_attempted = True
    run.create_ok = await pcie.create_fixture_partition(
        client, state, fixture, resources=_RESOURCES
    )
    run.partition_created = fixture.created
    if not run.create_ok:
        return
    profile_uuid = await _activation_profile_uuid(client, state, fixture)
    if profile_uuid is None or not await _assign(client, state, run):
        return
    if not await _no_profile_activation(client, state, fixture):
        return
    if not await _activate_to_sms(client, state, fixture, profile_uuid):
        return
    await _observe(client, state, fixture)
    if not await _power_off_to_not_activated(client, state, fixture):
        return
    if not await _reactivate_and_restart(client, state, fixture, profile_uuid):
        return
    if not await _osshutdown_refusal(client, state, fixture):
        return
    if not run.accept_dump:
        state.skip(
            _ROW,
            "hmc_dump_restart_lpar",
            "LIVE_TEST_ACCEPT_PLATFORM_DUMP is not true — dumprestart crashes the "
            "partition and takes a platform dump, so it runs only on opt-in",
        )
    elif not await _dump_restart(client, state, fixture):
        return
    state.skip(_ROW, "network boot", "PowerOn has no network boot yet (#868)")


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


async def _unassign(client: Client, state: RunState, run: _Run) -> bool:
    fixture = run.fixture
    arm = fixture.config
    st, data = await state.call(
        client,
        "hmc_unassign_dedicated_pcie_slot",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.lpar_uuid,
        profile_name=arm.profile_name,
        drc_index=fixture.drc_index,
    )
    # A lost response has still written, so the readback decides.
    after = await pcie._read_profile_io_slots(client, state, fixture)
    restored = after is not None and after == fixture.baseline_io_slots
    state.record_verified(
        _ROW,
        "hmc_unassign_dedicated_pcie_slot",
        operation="pcie.unassign_dedicated_slot",
        scenario=_SCENARIO,
        assertions=[
            Assertion("unassign-call-succeeded", st == "PASS"),
            Assertion("profile-restored-to-baseline", restored),
        ],
        cleanup="not-required",
        data=data
        if st != "PASS"
        else f"io_slots={after!r} baseline={fixture.baseline_io_slots!r}",
    )
    return restored


async def _slot_released(
    client: Client, state: RunState, fixture: pcie._DedicatedFixture
) -> bool:
    """Whether no partition owns the slot and no profile lists it; unreadable is False."""
    arm = fixture.config
    st, data = await state.call(
        client, "hmc_list_dedicated_pcie_slots", system_name_or_uuid=arm.system_name
    )
    slot = None
    if st == "PASS" and isinstance(data, dict):
        slot = next(
            (
                item
                for item in data.get("items") or []
                if isinstance(item, dict) and item.get("drc_index") == fixture.drc_index
            ),
            None,
        )
    unowned = slot is not None and (slot.get("owner_lpar") or "").strip() in (
        "",
        "null",
    )
    state.record_verified(
        _ROW,
        "hmc_list_dedicated_pcie_slots",
        operation="pcie.list_dedicated_slots",
        scenario=_SCENARIO,
        assertions=[
            Assertion("list-call-succeeded", st == "PASS"),
            Assertion("fixture-slot-listed", slot is not None),
            Assertion("fixture-slot-unowned", unowned),
        ],
        cleanup="not-required",
        data=data if st != "PASS" else {"listed": slot is not None, "unowned": unowned},
    )
    if not unowned:
        return False
    st, data = await state.call(
        client, "hmc_run_command", cmd=profile_io_slot_rows_command(arm.system_name)
    )
    if st != "PASS" or not isinstance(data, str):
        return False
    try:
        rows = parse_profile_io_slot_rows(data)
    except HMCCLIError:
        return False
    return not pcie._profile_lists_slot(rows, str(fixture.drc_index))


async def _delete(client: Client, state: RunState, run: _Run) -> bool:
    """Guard C, then delete by UUID and judge the delete by readback."""
    fixture = run.fixture
    final = await pcie._read_dedicated_state(client, state, fixture)
    if (
        not _identity_matches(fixture, final)
        or final.profile_io_slots != fixture.baseline_io_slots
    ):
        await pcie.cleanup_dedicated(client, state, fixture)
        return False
    st, data = await state.call(
        client,
        "hmc_delete_lpar",
        system_name_or_uuid=fixture.config.system_name,
        lpar_name_or_uuid=fixture.lpar_uuid,
    )
    absent = await pcie.name_absent(client, state, fixture)
    if not absent:
        state.record(_ROW, "hmc_delete_lpar (call)", st, data)
        await pcie.cleanup_dedicated(client, state, fixture)
        return False
    # Gone, whatever the call reported: a lost response has still deleted.
    fixture.created = False
    released = await _slot_released(client, state, fixture)
    _record_held(
        state,
        "hmc_delete_lpar",
        assertions=[
            Assertion("delete-call-succeeded", st == "PASS"),
            Assertion("lpar-name-absent", absent),
            Assertion("slot-released", released),
        ],
        cleanup="not-required",
        data=data,
    )
    return st == "PASS" and released


async def _teardown_fixture(client: Client, state: RunState, run: _Run) -> bool:
    """Power off, unassign and delete, each decided on live state.

    Returns True only when the partition is gone and its slot released. A
    partition this arm cannot prove its own is left untouched with a
    manual-recovery row; drift once it is powered off goes to
    `pcie.cleanup_dedicated`, whose guards re-decide on live state.
    """
    fixture = run.fixture
    if not run.create_ok:
        # Never activated, so the shared guards' delete precondition holds.
        await pcie.cleanup_dedicated(client, state, fixture)
        return False
    # Read twice before concluding a mismatch: a transient read failure reads as
    # a missing token or UUID. The partition may be active here, and the shared
    # guards would unassign and try to delete it without powering it off.
    observed = await pcie._read_dedicated_state(client, state, fixture)
    if not _identity_matches(fixture, observed):
        observed = await pcie._read_dedicated_state(client, state, fixture)
    if not _identity_matches(fixture, observed):
        state.record(
            _ROW,
            "bare-cec teardown: identity not confirmed",
            "FAIL",
            _manual_recovery(
                fixture,
                f"could not be confirmed as this run's (read caller token "
                f"{observed.caller_token!r} and UUID {observed.lpar_uuid!r}, expected "
                f"{fixture.run_marker!r} and {fixture.lpar_uuid!r}) and may still be "
                "active; this run did not power it off",
            ),
        )
        return False
    lpar_state = await _read_state(client, state, fixture, _NOT_ACTIVATED, attempts=1)
    if lpar_state not in _NOT_ACTIVATED:
        st, data, failure = await _power_off(client, state, fixture, immediate=True)
        _record_power(state, "hmc_power_off_lpar (teardown)", st, data, failure)
        lpar_state = await _read_state(client, state, fixture, _NOT_ACTIVATED)
        if lpar_state not in _NOT_ACTIVATED:
            state.record(
                _ROW,
                "bare-cec teardown: partition not powered off",
                "FAIL",
                _manual_recovery(
                    fixture, f"is in state {lpar_state!r} and could not be powered off"
                ),
            )
            return False
        observed = await pcie._read_dedicated_state(client, state, fixture)
    at_baseline = (
        fixture.baseline_io_slots is not None
        and observed.profile_io_slots == fixture.baseline_io_slots
    )
    if not at_baseline and (
        fixture.applied_io_slots is None
        or observed.profile_io_slots != fixture.applied_io_slots
        or not await _unassign(client, state, run)
    ):
        # Drift, or a removal that did not restore the baseline: the shared
        # guards re-read and refuse or recover on live state.
        await pcie.cleanup_dedicated(client, state, fixture)
        return False
    return await _delete(client, state, run)


def _record_create_and_assign(state: RunState, run: _Run, clean: bool) -> None:
    """Record create and assign once the teardown has decided their cleanup.

    Unassign and delete record where they run; one the teardown never reached
    has no observation, as in every other arm — its FAIL rows say why.
    """
    fixture = run.fixture
    cleanup = "passed" if clean else "failed"
    if run.create_attempted:
        _record_held(
            state,
            "hmc_create_lpar",
            assertions=[
                Assertion("lpar-uuid-resolved", fixture.lpar_uuid is not None),
                Assertion("ownership-and-baseline-confirmed", run.create_ok),
            ],
            cleanup=cleanup if run.partition_created else "not-required",
            data=f"lpar={fixture.lpar_name!r} run_marker={fixture.run_marker!r}",
        )
    if run.assign_holds is not None:
        call_ok, listed = run.assign_holds
        state.record_verified(
            _ROW,
            "hmc_assign_dedicated_pcie_slot",
            operation="pcie.assign_dedicated_slot",
            scenario=_SCENARIO,
            assertions=[
                Assertion("assign-call-succeeded", call_ok),
                Assertion("profile-lists-slot", listed),
            ],
            cleanup=cleanup,
            data=f"drc_index={fixture.drc_index!r} applied={fixture.applied_io_slots!r}",
        )


async def _teardown(client: Client, state: RunState, run: _Run) -> None:
    print("\n=== ST35: Bare-CEC teardown (issue #876) ===")
    clean = False
    try:
        if run.fixture.created:
            clean = await _teardown_fixture(client, state, run)
    except Exception as exc:  # noqa: BLE001 - the deferred rows below must still be written
        state.record(
            _ROW,
            "bare-cec teardown raised",
            "FAIL",
            f"{type(exc).__name__}: {exc} — MANUAL RECOVERY may be required; run "
            "scripts/live_test_recovery.py against this run's results",
        )
    finally:
        _record_create_and_assign(state, run, clean)


async def exercise_bare_cec(client: Client, state: RunState) -> None:
    """Admit, run the bare-CEC steps, and tear down on every exit."""
    print("\n============================")
    print("=== Bare-CEC Live Test (issue #876) ===")
    print("============================")
    if not _power_operations_authorized():
        state.skip(
            _ROW,
            "bare-cec power authorization",
            "HMC_AUTHORIZE_POWER_OPERATIONS is not true; this arm's evidence must "
            "cover the ownership-guarded power path — SKIP bare-cec arm",
        )
        return
    accept_dump = _dump_opt_in(state.config)
    if accept_dump is None:
        state.skip(
            _ROW,
            "bare-cec platform-dump opt-in",
            "LIVE_TEST_ACCEPT_PLATFORM_DUMP must be true, false or unset — SKIP "
            "bare-cec arm rather than guess whether a platform dump is wanted",
        )
        return
    if any(row.get("subtask") == 29 for row in state.results):
        state.skip(
            _ROW,
            "bare-cec fixture artifacts",
            "the dedicated arm already ran in this process; both arms record one "
            "fixture in the pcie_* artifacts the recovery check reads, so a second "
            "would hide the first — run the bare-cec group on its own",
        )
        return
    fixture = await pcie.capture_dedicated_baseline(client, state)
    if fixture is None:
        return
    run = _Run(fixture, accept_dump)
    try:
        await _run_steps(client, state, run)
    except Exception as exc:  # noqa: BLE001 - recorded as FAIL; the teardown still runs
        state.record(
            _ROW,
            "bare-cec arm raised before teardown",
            "FAIL",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        await _teardown(client, state, run)
