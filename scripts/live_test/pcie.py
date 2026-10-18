"""Live validation scenarios for reversible PCIe assignment — issue #217.

SR-IOV arm (ST23–ST28, subtask 23): Exercises reversible SR-IOV logical-port
assignment on the configured system and LPAR.  The LPAR must be Not Activated
before this module runs.

Dedicated-slot arm (ST29–ST34, subtask 24): Exercises reversible dedicated PCIe
slot assignment via a run-unique owner-stamped LPAR.  It SKIPs outside the
ADR 0053-admitted HMC release and system model (V10R3 M1060 / 8375-42A), because
the io_slots profile grammar this arm issues has only been probed on a Power8
documentation row and must not be executed on an unidentified environment.
Configuration is required — no fallback to an arbitrary system.

Admitted environment (ADR 0053; matched exactly per ADR 0166 decision 3 by
operations/virtualization/pcie.py `_is_exact_admitted_environment`):
  `lshmc -V` Version 10 · Release 3 · Service Pack 1060 · managed-system model 8375-42A

SR-IOV test structure (ST23–ST28):
  ST23 — Baseline: read adapter/physport/logport inventory; confirm lp3 profile is clean
  ST24 — Assign the configured logical port to the configured LPAR
  ST25 — Verify effective + profile readback after assign
  ST26 — Unassign; verify logical port is unconfigured and profile is restored
  ST27 — Reassign on existing LPAR (same port, same capacity)
  ST28 — Cleanup: unassign again; final profile + inventory confirm; PASS/SKIP/FAIL

Dedicated-slot test structure (ST29–ST34, subtask 24):
  ST29 — Baseline: read HMC release/model, list dedicated slots, select one unassigned
  ST30 — Create-time assignment on a probe partition, cleaned up before the
         run-unique fixture LPAR is created (ADR 0166)
  ST31 — Assign selected slot through hmc_assign_dedicated_pcie_slot
  ST32 — Verify profile readback after assign
  ST33 — Unassign (io_slots-), verify exact baseline restored, reassign (io_slots+)
  ST34 — Cleanup: slot removal then LPAR delete, each only on an exact match

Configuration for the dedicated arm, read from the ADR 0115 `.env` (never from
the ambient environment — this arm creates and deletes partitions on the system
it is pointed at, so an exported value must not be able to redirect it):
  LIVE_TEST_DEDICATED_PCIE_SYSTEM_NAME — managed-system name; the arm SKIPs if unset
  LIVE_TEST_DEDICATED_PCIE_LPAR_PREFIX — name prefix for the run-unique fixture; SKIPs if unset
  LIVE_TEST_DEDICATED_PCIE_PROFILE_NAME — profile name (default: default_profile)
  LIVE_TEST_DEDICATED_PCIE_DRC_INDEX — specific DRC index; auto-selects the
      first slot no partition owns and no partition profile lists if absent

Missing hardware or a wrong LPAR state produces SKIP per arm, not FAIL.
Any cleanup mutation failure records manual-recovery evidence and halts further
cleanup (does not attempt additional mutations on an unknown state).
"""

from __future__ import annotations

import asyncio
import re
import shlex
import sys
import uuid
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from hmcpctl.operations.lpar.ownership import parse_lpar_ownership_caller_token

# Imported rather than restated so the arm's SKIP envelope cannot drift from the one
# the product's admission gates enforce (ADR 0166 decision 3); a copied predicate or
# literal would go stale silently the first time the admitted release moves.
from hmcpctl.operations.virtualization.pcie import (
    _ADMITTED_RELEASE_FIELDS,
    _ADMITTED_SYSTEM_MODEL,
    _is_exact_admitted_environment,
)
from hmcpctl.ssh.commands import HMC_NO_RESULTS, build_attribute_record
from hmcpctl.ssh.profiles import (
    parse_profile_io_slot_rows,
    parse_profile_io_slots,
    profile_io_slot_rows_command,
)
from hmcpctl.ssh.transport import HMCCLIError

from .observation import (
    CLEANUP,
    Assertion,
    CallFailure,
    judge_create_result,
    plain_data,
)

if TYPE_CHECKING:
    from live_test_runner import RunState

    from live_test.config import LiveTestConfig


# ---------------------------------------------------------------------------
# SR-IOV state snapshot helpers
# ---------------------------------------------------------------------------

_SRIOV_SCENARIO = "st23-sriov-logical-port"


@dataclass
class _SriovEvidence:
    """What the assign and reassign readbacks showed, recorded once cleanup has decided.

    Both add something cleanup must undo, so their observations carry cleanup's
    outcome rather than claiming none was needed (`bare_cec._record_create_and_assign`).
    """

    assign_changed: bool = False
    assign: tuple[bool, bool, bool] | None = None  # configured, owner, capacity
    reassign: tuple[bool, bool] | None = None  # configured, owner


def _sriov_changed(data: object) -> bool:
    """Whether an SR-IOV tool result, in any shape it is served, reports a mutation.

    Both operations return `changed=False` from an idempotent no-op: an assign
    of a port already assigned as asked, an unassign of a profile reading
    `none`. Such a call exercised no mutation path, so it backs no observation.
    """
    if isinstance(data, dict):
        return data.get("changed") is True
    if isinstance(data, str):
        return "changed=True" in data
    # The live client serves the result dataclass as a generated model, not a
    # mapping (as `metrics._as_outcome` also has to handle), so read the field.
    return getattr(data, "changed", None) is True


def _profile_read_clean(profile_ports: str | None) -> bool:
    """Profile ports read back as empty; an unreadable profile (None) is not."""
    return profile_ports in ("none", "")


@dataclass
class _SriovState:
    """Point-in-time SR-IOV state captured for one logical port."""

    configured: bool  # True → appears in hmc_list_sriov_logical_ports with owner
    profile_ports: str | None  # sriov_eth_logical_ports value from profile
    owner_lpar: str | None
    capacity_percent: float | None


async def _read_sriov_state(client: Client, state: RunState) -> _SriovState:
    """Read current SR-IOV state for the test logical port and lp3 profile."""
    config = state.config

    # Read configured logical ports
    st, data = await state.call(
        client,
        "hmc_list_sriov_logical_ports",
        system_name_or_uuid=config.system_name,
        adapter_id=str(config.sriov_adapter_id),
        logical_port_id=config.sriov_logical_port_id,
    )
    configured = False
    owner_lpar = None
    capacity_percent = None
    if st == "PASS" and isinstance(data, dict):
        items = data.get("items") or []
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("logical_port_id") == config.sriov_logical_port_id
                and item.get("availability") not in ("unconfigured", None, "")
                and item.get("owner_lpar")
            ):
                configured = True
                owner_lpar = item.get("owner_lpar")
                cap = item.get("capacity_percent")
                capacity_percent = float(cap) if cap is not None else None
                break

    # Read profile SR-IOV eth logical ports field
    st_prof, data_prof = await state.call(
        client,
        "hmc_run_command",
        cmd=(
            f"lssyscfg -r prof -m {config.system_name} "
            f"--filter 'lpar_names={config.lp3_name},profile_names={config.sriov_profile_name}' "
            f"-F sriov_eth_logical_ports"
        ),
    )
    profile_ports: str | None = None
    if st_prof == "PASS" and isinstance(data_prof, str):
        profile_ports = data_prof.strip()

    return _SriovState(configured, profile_ports, owner_lpar, capacity_percent)


def _sriov_state_summary(s: _SriovState) -> str:
    return (
        f"configured={s.configured} owner={s.owner_lpar!r} "
        f"capacity={s.capacity_percent}% profile_ports={s.profile_ports!r}"
    )


# ---------------------------------------------------------------------------
# ST23 — Baseline: inventory + lp3 profile clean check
# ---------------------------------------------------------------------------


def _adapter_is_healthy(data: object, adapter_id: str) -> bool:
    """Return whether the selected adapter is in healthy SR-IOV mode.

    `adapter_id` is compared against `hmc_list_sriov_adapters` rows, which project
    it as `str` (`SriovAdapter.adapter_id`), so the caller converts before calling.
    Comparing the numeric config value directly makes every row unequal and reports
    a healthy adapter as absent.
    """
    items = data.get("items") or [] if isinstance(data, dict) else []
    return any(
        isinstance(item, dict)
        and item.get("adapter_id") == adapter_id
        and item.get("mode") == "sriov"
        and item.get("availability") == "1"
        for item in items
    )


def _available_capacity(data: object) -> float:
    """Calculate remaining physical-port capacity from logical-port inventory."""
    used = 0.0
    items = data.get("items") or [] if isinstance(data, dict) else []
    for index, item in enumerate(items):
        if (
            isinstance(item, dict)
            and item.get("availability") not in ("unconfigured", None, "")
            and item.get("capacity_percent") is not None
        ):
            try:
                used += float(item["capacity_percent"])
            except (ValueError, TypeError) as error:
                raise ValueError(
                    f"logical-port row {index} has invalid capacity_percent"
                ) from error
    return 100.0 - used


def _logical_port_is_configured(data: object, logical_port_id: str) -> bool:
    """Return whether the selected logical port has an effective assignment.

    `logical_port_id` is compared against `hmc_list_sriov_logical_ports` rows,
    which project it as `str` (`SriovLogicalPort.logical_port_id`). Comparing the
    numeric config value directly is never equal, so a port that is still
    configured after cleanup reports as unconfigured - a wrong answer in a cleanup
    assertion rather than a loud failure.
    """
    items = data.get("items") or [] if isinstance(data, dict) else []
    return any(
        isinstance(item, dict)
        and item.get("logical_port_id") == logical_port_id
        and item.get("availability") not in ("unconfigured", None, "")
        for item in items
    )


async def _verify_cleanup_inventory(
    client: Client, state: RunState
) -> tuple[bool, bool]:
    """Record final logical-port and profile checks after cleanup.

    Returns (restored, profile_clean): restored only when the inventory read
    shows the port unconfigured and the profile is clean.
    """
    config = state.config
    st, data = await state.call(
        client,
        "hmc_list_sriov_logical_ports",
        system_name_or_uuid=config.system_name,
        adapter_id=str(config.sriov_adapter_id),
        logical_port_id=config.sriov_logical_port_id,
    )
    state.record(28, "hmc_list_sriov_logical_ports (final)", st, data)
    unconfigured = False
    if st == "PASS":
        still_configured = _logical_port_is_configured(
            data, config.sriov_logical_port_id
        )
        state.record(
            28,
            "sriov final inventory check",
            "FAIL" if still_configured else "PASS",
            (
                f"MANUAL RECOVERY REQUIRED: logical port {config.sriov_logical_port_id} "
                "is still configured after cleanup"
                if still_configured
                else f"logical port {config.sriov_logical_port_id} is unconfigured — baseline restored"
            ),
        )
        unconfigured = not still_configured

    final_state = await _read_sriov_state(client, state)
    profile_clean = final_state.profile_ports in (None, "none", "")
    read_clean = _profile_read_clean(final_state.profile_ports)
    state.record(
        28,
        "lp3 profile final check",
        "PASS" if profile_clean else "FAIL",
        (
            f"MANUAL RECOVERY REQUIRED: profile sriov_eth_logical_ports="
            f"{final_state.profile_ports!r} after cleanup — "
            f"run: chsyscfg -r prof -m {config.system_name} "
            f'-i "name={config.sriov_profile_name},lpar_name={config.lp3_name},'
            f'sriov_eth_logical_ports=none" to recover'
            if not profile_clean
            else "sriov_eth_logical_ports=none — lp3 profile restored to baseline"
        ),
    )
    return unconfigured and read_clean, read_clean


async def _check_sriov_adapter_health(client: Client, state: RunState) -> bool:
    """Require an available, healthy SR-IOV adapter before any mutation arm."""
    config = state.config
    st, data = await state.call(
        client,
        "hmc_list_sriov_adapters",
        system_name_or_uuid=config.system_name,
        adapter_id=str(config.sriov_adapter_id),
    )
    state.record(23, "hmc_list_sriov_adapters (baseline)", st, data)
    if st != "PASS":
        state.record(
            23,
            "baseline-abort",
            "FAIL",
            "SR-IOV adapter inventory failed — cannot proceed",
        )
        return False

    # Gate on capability-available
    if isinstance(data, dict) and data.get("capability") == "capability-unavailable":
        reason = data.get("unavailable_reason", "")
        state.skip(
            23,
            "hmc_list_sriov_adapters (capability)",
            f"SR-IOV capability unavailable on this environment: {reason}",
        )
        return False

    # Confirm adapter is in SR-IOV mode and healthy
    if not _adapter_is_healthy(data, str(config.sriov_adapter_id)):
        state.skip(
            23,
            "hmc_list_sriov_adapters (health check)",
            f"adapter {config.sriov_adapter_id!r} is not in healthy sriov mode; SKIP SR-IOV arm",
        )
        return False
    state.record(
        23,
        "hmc_list_sriov_adapters (health check)",
        "PASS",
        f"adapter {config.sriov_adapter_id} in healthy sriov mode",
    )
    return True


async def _check_sriov_physical_port_capacity(client: Client, state: RunState) -> bool:
    """Require the selected physical port and sufficient available capacity."""
    config = state.config
    st, data = await state.call(
        client,
        "hmc_list_sriov_physical_ports",
        system_name_or_uuid=config.system_name,
        adapter_id=str(config.sriov_adapter_id),
        physical_port_id=str(config.sriov_physical_port_id),
    )
    state.record(23, "hmc_list_sriov_physical_ports (baseline)", st, data)
    if st != "PASS":
        state.skip(
            23,
            "sriov physical port check",
            "physical port inventory failed: SKIP SR-IOV arm",
        )
        return False

    # Capacity check: hmc_list_sriov_logical_ports returns configured+unconfigured ports.
    # The configured capacity must be available on the configured physical port.
    st_lp, data_lp = await state.call(
        client,
        "hmc_list_sriov_logical_ports",
        system_name_or_uuid=config.system_name,
        adapter_id=str(config.sriov_adapter_id),
        physical_port_id=str(config.sriov_physical_port_id),
    )
    available = _available_capacity(data_lp) if st_lp == "PASS" else 0.0
    state.record(
        23,
        "sriov capacity check (pre-test)",
        "PASS" if available >= config.sriov_capacity_percent else "SKIP",
        f"phys_port {config.sriov_physical_port_id}: available={available}% needed={config.sriov_capacity_percent}%",
    )
    if available < config.sriov_capacity_percent:
        state.skip(
            23,
            "sriov assign arm",
            f"phys_port {config.sriov_physical_port_id} has only {available}% capacity remaining "
            f"(need {config.sriov_capacity_percent}%); all unconfigured logical ports are T1-addressed "
            "and hmcpctl's location-code check blocks cross-port assignment — SKIP assign arm. "
            "NOTE: chhwres assigns T1 logical ports to phys_port 1 (T2) successfully "
            "at the firmware layer; the location-code check is an hmcpctl admission gate, "
            "not a firmware constraint.",
        )
        return False

    return True


async def _check_sriov_logical_port_clean(client: Client, state: RunState) -> bool:
    """Require the selected logical port to be unconfigured before mutation."""
    config = state.config
    st, data = await state.call(
        client,
        "hmc_list_sriov_logical_ports",
        system_name_or_uuid=config.system_name,
        adapter_id=str(config.sriov_adapter_id),
        logical_port_id=config.sriov_logical_port_id,
    )
    state.record(23, "hmc_list_sriov_logical_ports (baseline)", st, data)
    if st != "PASS":
        state.skip(
            23,
            "sriov logical port baseline",
            "logical port inventory failed: SKIP SR-IOV arm",
        )
        return False
    if _logical_port_is_configured(data, config.sriov_logical_port_id):
        state.skip(
            23,
            "sriov logical port precondition",
            f"logical port {config.sriov_logical_port_id} is already configured (not a clean baseline); "
            "SKIP SR-IOV arm to avoid mutating a port this run does not own",
        )
        return False
    state.record(
        23,
        "sriov logical port precondition",
        "PASS",
        f"logical port {config.sriov_logical_port_id} is unconfigured — clean baseline confirmed",
    )
    return True


async def _check_sriov_profile_clean(client: Client, state: RunState) -> bool:
    """Require the profile to be empty or already scoped to this test port."""
    config = state.config
    sriov_state = await _read_sriov_state(client, state)
    state.record(
        23,
        "lp3 profile sriov_eth_logical_ports (baseline)",
        "PASS",
        _sriov_state_summary(sriov_state),
    )
    # Accept two clean starting states:
    #   (a) profile is none — no prior assignment
    #   (b) profile already contains exactly our test port (e.g. written manually
    #       ahead of this run so the unassign path can be exercised) — the assign
    #       operation will detect idempotence and the unassign will clear it.
    profile_has_our_port = sriov_state.profile_ports not in (
        None,
        "none",
        "",
    ) and f":{config.sriov_logical_port_id}:" in str(sriov_state.profile_ports)
    profile_clean = sriov_state.profile_ports in (None, "none", "")
    if not profile_clean and not profile_has_our_port:
        state.skip(
            23,
            "lp3 profile precondition",
            f"lp3 {config.sriov_profile_name} already has sriov_eth_logical_ports={sriov_state.profile_ports!r} "
            f"(not our test port {config.sriov_logical_port_id}); "
            "SKIP SR-IOV arm to avoid overwriting an existing assignment",
        )
        return False
    state.record(
        23,
        "lp3 profile precondition",
        "PASS",
        (
            f"sriov_eth_logical_ports contains our test port {config.sriov_logical_port_id} — "
            "profile ready for assign (idempotent) + unassign round-trip"
            if profile_has_our_port
            else "sriov_eth_logical_ports=none — lp3 profile is clean"
        ),
    )
    return True


async def capture_sriov_baseline(client: Client, state: RunState) -> bool:
    """Record ordered SR-IOV prerequisites and stop at the first failed stage."""
    print("\n=== ST23: SR-IOV Baseline (issue #217) ===")
    for check in (
        _check_sriov_adapter_health,
        _check_sriov_physical_port_capacity,
        _check_sriov_logical_port_clean,
        _check_sriov_profile_clean,
    ):
        if not await check(client, state):
            return False
    return True


# ---------------------------------------------------------------------------
# ST24 — Assign logical port to lp3
# ---------------------------------------------------------------------------


async def assign_sriov_to_lp3(
    client: Client, state: RunState, evidence: _SriovEvidence
) -> bool:
    """Assign test logical port to lp3.  Returns False if the call failed."""
    config = state.config
    print("\n=== ST24: SR-IOV Assign (issue #217) ===")
    st, data = await state.call(
        client,
        "hmc_assign_sriov_logical_port",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        adapter_id=str(config.sriov_adapter_id),
        physical_port_id=str(config.sriov_physical_port_id),
        logical_port_id=config.sriov_logical_port_id,
        capacity_percent=config.sriov_capacity_percent,
        profile_name=config.sriov_profile_name,
        ownership_override=True,
    )
    state.record(24, "hmc_assign_sriov_logical_port", st, data)
    evidence.assign_changed = st == "PASS" and _sriov_changed(data)
    return st == "PASS"


# ---------------------------------------------------------------------------
# ST25 — Verify effective + profile readback after assign
# ---------------------------------------------------------------------------


async def verify_sriov_assigned(
    client: Client, state: RunState, assign_ok: bool, evidence: _SriovEvidence
) -> bool:
    """Verify the logical port is configured on lp3 after assign."""
    config = state.config
    print("\n=== ST25: SR-IOV Post-Assign Verify (issue #217) ===")
    sriov_state = await _read_sriov_state(client, state)
    state.record(
        25,
        "sriov post-assign state",
        "PASS" if sriov_state.configured else "FAIL",
        _sriov_state_summary(sriov_state),
    )

    # Verify owner
    owner_ok = sriov_state.owner_lpar == config.lp3_name
    state.record(
        25,
        "sriov owner check",
        "PASS" if owner_ok else "FAIL",
        f"expected owner={config.lp3_name!r}, got {sriov_state.owner_lpar!r}",
    )

    # Verify capacity
    cap_ok = (
        abs((sriov_state.capacity_percent or 0.0) - config.sriov_capacity_percent)
        < 0.01
    )
    state.record(
        25,
        "sriov capacity check",
        "PASS" if cap_ok else "FAIL",
        f"expected {config.sriov_capacity_percent}%, got {sriov_state.capacity_percent}%",
    )

    # Profile readback — informational for the dynamic path on a Not Activated LPAR.
    # chhwres -o a updates the effective layer; the HMC does not auto-update the
    # profile.  Record the value but do not gate the pass/fail decision on it.
    state.record(
        25,
        "sriov profile readback (informational)",
        "PASS",
        f"profile sriov_eth_logical_ports={sriov_state.profile_ports!r} "
        "(dynamic assign does not update the profile for Not Activated LPARs)",
    )
    if assign_ok and evidence.assign_changed:
        evidence.assign = (sriov_state.configured, owner_ok, cap_ok)

    return sriov_state.configured and owner_ok and cap_ok


# ---------------------------------------------------------------------------
# ST26 — Unassign; verify baseline restored
# ---------------------------------------------------------------------------


async def unassign_sriov_from_lp3(client: Client, state: RunState) -> bool:
    """Unassign the test logical port from lp3.  Returns False if the call failed."""
    config = state.config
    print("\n=== ST26: SR-IOV Unassign (issue #217) ===")
    st, data = await state.call(
        client,
        "hmc_unassign_sriov_logical_port",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        adapter_id=str(config.sriov_adapter_id),
        physical_port_id=str(config.sriov_physical_port_id),
        logical_port_id=config.sriov_logical_port_id,
        profile_name=config.sriov_profile_name,
        ownership_override=True,
    )
    state.record(26, "hmc_unassign_sriov_logical_port", st, data)
    if st != "PASS":
        return False

    # Verify: profile restored to none.
    # The unassign_sriov_logical_port operation is a profile-only path (chsyscfg).
    # It clears sriov_eth_logical_ports in the profile but does NOT issue chhwres -o r
    # to remove the effective assignment — that only happens at next activation.
    # The effective layer is expected to remain configured; only the profile is checked.
    sriov_state = await _read_sriov_state(client, state)
    profile_clean = sriov_state.profile_ports in (None, "none", "")
    state.record(
        26,
        "sriov post-unassign profile verify",
        "PASS" if profile_clean else "FAIL",
        f"profile_ports={sriov_state.profile_ports!r} (effective still has port — expected for profile-only unassign path)",
    )
    state.record(
        26,
        "sriov post-unassign effective (informational)",
        "PASS",
        f"effective configured={sriov_state.configured} owner={sriov_state.owner_lpar!r} "
        "(profile-only unassign does not touch effective layer; port remains until next activation)",
    )
    if not _sriov_changed(data):
        # An idempotent no-op dispatched nothing to verify (see _sriov_changed).
        return profile_clean
    state.record_verified(
        26,
        "hmc_unassign_sriov_logical_port (verified)",
        operation="sriov.unassign_logical_port",
        scenario=_SRIOV_SCENARIO,
        assertions=[
            # A failed call returned above, before any readback.
            Assertion("unassign-call-succeeded", True),
            Assertion(
                "profile-ports-cleared", _profile_read_clean(sriov_state.profile_ports)
            ),
        ],
        cleanup="not-required",
        data=f"profile_ports={sriov_state.profile_ports!r}",
    )
    return profile_clean


# ---------------------------------------------------------------------------
# ST27 — Reassign on existing LPAR (prove round-trip)
# ---------------------------------------------------------------------------


async def reassign_sriov_to_lp3(
    client: Client, state: RunState, evidence: _SriovEvidence
) -> bool:
    """Re-assign the same port to prove the round-trip path."""
    config = state.config
    print("\n=== ST27: SR-IOV Reassign (issue #217) ===")
    st, data = await state.call(
        client,
        "hmc_assign_sriov_logical_port",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        adapter_id=str(config.sriov_adapter_id),
        physical_port_id=str(config.sriov_physical_port_id),
        logical_port_id=config.sriov_logical_port_id,
        capacity_percent=config.sriov_capacity_percent,
        profile_name=config.sriov_profile_name,
        ownership_override=True,
    )
    state.record(27, "hmc_assign_sriov_logical_port (reassign)", st, data)
    if st != "PASS":
        return False

    # Verify ownership
    sriov_state = await _read_sriov_state(client, state)
    ok = sriov_state.configured and sriov_state.owner_lpar == config.lp3_name
    if _sriov_changed(data):
        evidence.reassign = (
            sriov_state.configured,
            sriov_state.owner_lpar == config.lp3_name,
        )
    state.record(
        27,
        "sriov post-reassign verify",
        "PASS" if ok else "FAIL",
        _sriov_state_summary(sriov_state),
    )
    return ok


# ---------------------------------------------------------------------------
# ST28 — Cleanup: final unassign + inventory confirm
# ---------------------------------------------------------------------------


async def cleanup_sriov(client: Client, state: RunState) -> bool:
    """Unassign the test port (cleanup); return whether the baseline is confirmed restored."""
    config = state.config
    print("\n=== ST28: SR-IOV Cleanup (issue #217) ===")

    # Re-read before mutating — guard before any cleanup action
    sriov_state = await _read_sriov_state(client, state)
    state.record(
        28,
        "sriov pre-cleanup state",
        "PASS",
        _sriov_state_summary(sriov_state),
    )

    # Only attempt cleanup if we own the port
    if not sriov_state.configured:
        state.record(
            28,
            "sriov cleanup: port already unconfigured",
            "PASS",
            "no cleanup action required",
        )
        restored, _ = await _verify_cleanup_inventory(client, state)
        return restored
    if sriov_state.owner_lpar != config.lp3_name:
        state.record(
            28,
            "sriov cleanup: owner mismatch",
            "FAIL",
            f"MANUAL RECOVERY REQUIRED: logical port {config.sriov_logical_port_id} is assigned "
            f"to {sriov_state.owner_lpar!r} — expected {config.lp3_name!r}. "
            "Do not unassign — another LPAR owns this port.",
        )
        return False
    # Step 1: profile unassign (clears sriov_eth_logical_ports via chsyscfg)
    st, data = await state.call(
        client,
        "hmc_unassign_sriov_logical_port",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        adapter_id=str(config.sriov_adapter_id),
        physical_port_id=str(config.sriov_physical_port_id),
        logical_port_id=config.sriov_logical_port_id,
        profile_name=config.sriov_profile_name,
        ownership_override=True,
    )
    state.record(28, "hmc_unassign_sriov_logical_port (cleanup)", st, data)
    if st != "PASS":
        state.record(
            28,
            "sriov cleanup: unassign failed",
            "FAIL",
            f"MANUAL RECOVERY REQUIRED: profile unassign failed — "
            f"logical port {config.sriov_logical_port_id} may still be in profile and effective layer. "
            f"Run: chhwres -r sriov --rsubtype logport -m {config.system_name} "
            f"-o r -p {config.lp3_name} "
            f'-a "adapter_id={config.sriov_adapter_id},logical_port_id={config.sriov_logical_port_id}" '
            f"to recover. Error: {str(data)[:400]}",
        )
        return False

    # Step 2: effective removal (chhwres -o r) — the profile-only unassign
    # does not touch the effective layer.  Remove it explicitly so the
    # port returns to the unconfigured pool.
    st2, data2 = await state.call(
        client,
        "hmc_run_command",
        cmd=(
            f"chhwres -r sriov --rsubtype logport"
            f" -m {config.system_name}"
            f" -o r -p {config.lp3_name}"
            f' -a "adapter_id={config.sriov_adapter_id},logical_port_id={config.sriov_logical_port_id}"'
        ),
    )
    state.record(28, "chhwres -o r (effective cleanup)", st2, data2)
    if st2 != "PASS":
        state.record(
            28,
            "sriov cleanup: effective removal failed",
            "FAIL",
            f"MANUAL RECOVERY REQUIRED: effective removal failed — "
            f"logical port {config.sriov_logical_port_id} still assigned to {config.lp3_name!r}. "
            f"Run manually: chhwres -r sriov --rsubtype logport -m {config.system_name} "
            f"-o r -p {config.lp3_name} "
            f'-a "adapter_id={config.sriov_adapter_id},logical_port_id={config.sriov_logical_port_id}" '
            f"Error: {str(data2)[:400]}",
        )
        return False

    restored, profile_clean = await _verify_cleanup_inventory(client, state)
    if not _sriov_changed(data):
        # The profile already read `none`; the raw chhwres did the removal.
        return restored
    state.record_verified(
        28,
        "hmc_unassign_sriov_logical_port (cleanup verified)",
        operation="sriov.unassign_logical_port",
        scenario=_SRIOV_SCENARIO,
        assertions=[
            # Both failed calls returned above with a manual-recovery row.
            Assertion("unassign-call-succeeded", True),
            Assertion("profile-ports-cleared", profile_clean),
        ],
        cleanup="passed" if restored else "failed",
        data="final logical-port inventory and profile after cleanup",
    )
    return restored


# ---------------------------------------------------------------------------
# Top-level orchestrator: ST23–ST28 as a single subtask entry
# ---------------------------------------------------------------------------


async def exercise_sriov_assignment(client: Client, state: RunState) -> None:
    """Orchestrate the full SR-IOV assign/verify/unassign/reassign/cleanup sequence."""
    print("\n============================")
    print("=== SR-IOV Live Test (issue #217) ===")
    print("============================")

    # Phase 1: Baseline
    baseline_ok = await capture_sriov_baseline(client, state)
    if not baseline_ok:
        print("  SR-IOV baseline check failed or SKIP — halting SR-IOV arm")
        await cleanup_sriov(client, state)
        return

    evidence = _SriovEvidence()
    try:
        # Phase 2: Assign
        assign_ok = await assign_sriov_to_lp3(client, state, evidence)

        # Phase 3: Verify assign (always run, even if assign failed — documents state)
        verify_ok = await verify_sriov_assigned(client, state, assign_ok, evidence)

        # Phase 4: Unassign (only if assign succeeded and verification passed)
        if assign_ok and verify_ok:
            unassign_ok = await unassign_sriov_from_lp3(client, state)
        else:
            state.skip(
                26,
                "hmc_unassign_sriov_logical_port",
                f"skipping unassign: assign_ok={assign_ok} verify_ok={verify_ok}",
            )
            unassign_ok = False

        # Phase 5: Reassign (only if unassign succeeded — proves round-trip)
        if unassign_ok:
            await reassign_sriov_to_lp3(client, state, evidence)
        else:
            state.skip(
                27,
                "hmc_assign_sriov_logical_port (reassign)",
                f"skipping reassign: unassign_ok={unassign_ok}",
            )
    finally:
        active_error = sys.exception()
        restored = False
        try:
            # Phase 6: Cleanup — always runs after a successful baseline.
            restored = await cleanup_sriov(client, state)
        except BaseException as cleanup_error:
            if active_error is None:
                raise
            active_error.add_note(f"SR-IOV cleanup failed: {cleanup_error}")
        finally:
            _record_sriov_assignments(state, evidence, restored)


def _record_sriov_assignments(
    state: RunState, evidence: _SriovEvidence, restored: bool
) -> None:
    """Record assign and reassign once cleanup has decided their cleanup."""
    cleanup = "passed" if restored else "failed"
    if evidence.assign is not None:
        configured, owner_ok, cap_ok = evidence.assign
        state.record_verified(
            25,
            "hmc_assign_sriov_logical_port (verified)",
            operation="sriov.assign_logical_port",
            scenario=_SRIOV_SCENARIO,
            assertions=[
                # Set only when the call passed and dispatched a change (see _sriov_changed).
                Assertion("assign-call-succeeded", True),
                Assertion("logical-port-configured", configured),
                Assertion("owner-is-target-lpar", owner_ok),
                Assertion("capacity-matches", cap_ok),
            ],
            cleanup=cleanup,
            data=f"configured={configured} owner_ok={owner_ok} capacity_ok={cap_ok}",
        )
    if evidence.reassign is not None:
        configured, owner_ok = evidence.reassign
        state.record_verified(
            27,
            "hmc_assign_sriov_logical_port (reassign verified)",
            operation="sriov.assign_logical_port",
            scenario=_SRIOV_SCENARIO,
            assertions=[
                Assertion("assign-call-succeeded", True),
                Assertion("logical-port-configured", configured),
                Assertion("owner-is-target-lpar", owner_ok),
            ],
            cleanup=cleanup,
            data=f"configured={configured} owner_ok={owner_ok}",
        )


# ---------------------------------------------------------------------------
# Dedicated PCIe arm — ST29–ST34 (subtask 24)
# ---------------------------------------------------------------------------

_DEFAULT_DEDICATED_PROFILE = "default_profile"
_DEDICATED_SCENARIO = "st29-dedicated-pcie"
_IO_SLOTS_SCENARIO = "st36-io-slots"


@dataclass(frozen=True)
class _DedicatedConfig:
    system_name: str
    lpar_prefix: str
    profile_name: str
    drc_index: str | None


@dataclass
class _DedicatedFixture:
    config: _DedicatedConfig
    run_marker: str
    lpar_name: str
    probe_lpar_name: str
    lpar_uuid: str | None = None
    drc_index: str | None = None
    baseline_io_slots: str | None = None
    applied_io_slots: str | None = None
    created: bool = False
    probe_created: bool = False
    probe_lpar_uuid: str | None = None
    #: The assertion values of each addition cleanup must undo, keyed by step;
    #: recorded once the teardown has decided (`_record_dedicated_additions`).
    evidence: dict[str, tuple[bool, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class _DedicatedState:
    slot_owner: str | None
    profile_io_slots: str | None
    lpar_uuid: str | None
    caller_token: str | None


#: Characters this arm refuses in a configured value. Deliberately *stricter*
#: than `ssh/commands.py:_RECORD_DELIMITERS`, which is the set
#: `build_attribute_record` and `build_filter` raise on: those two refuse only
#: ``,``, ``=`` and ``"``, and pass ``[``, ``]`` and ``\`` straight through.
#: The three they pass through would break parsing of ADR 0064's ``[caller …]``
#: ownership stamp, which this arm reads back to decide whether a partition is
#: its own — so do not delete this check as redundant with the builders.
#: Refusing here also moves the rejection earlier: the builders raise at
#: command-construction time, which first happens *after* the fixture
#: partition exists, so an unvalidated value would abandon a created partition
#: with no cleanup and no results file. This turns that into the ST29
#: configuration SKIP, before anything is created.
_RECORD_DELIMITERS = ',="[]\\'


def _config_value_safe(value: str) -> bool:
    """Whether *value* can cross the HMC record grammar unchanged."""
    return not any(
        character in _RECORD_DELIMITERS or character < " " for character in value
    )


def _dedicated_config(live: LiveTestConfig) -> _DedicatedConfig | None:
    """Resolve the arm's explicit configuration, or None when it is absent.

    Reads the validated ADR 0115 configuration rather than ``os.environ``: the
    managed system this arm creates and deletes partitions on must come from
    the reviewed ``.env``, which an ambient export cannot override.

    Never falls back to ``LiveTestConfig.system_name``: issue #217 requires an
    explicitly configured managed system and forbids running against an
    arbitrary one.
    """
    system_name = live.dedicated_pcie_system_name.strip()
    lpar_prefix = live.dedicated_pcie_lpar_prefix.strip()
    if not system_name or not lpar_prefix:
        return None
    profile_name = (
        live.dedicated_pcie_profile_name.strip() or _DEFAULT_DEDICATED_PROFILE
    )
    drc_index = live.dedicated_pcie_drc_index.strip() or None
    if not all(
        _config_value_safe(value)
        for value in (lpar_prefix, profile_name, drc_index or "")
    ):
        return None
    return _DedicatedConfig(system_name, lpar_prefix, profile_name, drc_index)


def _new_run_marker() -> str:
    """Return a run-unique ADR 0064 caller token for this invocation."""
    return f"pcie-{uuid.uuid4().hex[:8]}"


def _dedicated_state_summary(s: _DedicatedState) -> str:
    return (
        f"slot_owner={s.slot_owner!r} profile_io_slots={s.profile_io_slots!r} "
        f"lpar_uuid={s.lpar_uuid!r} caller_token={s.caller_token!r}"
    )


def select_profile_io_slots(output: str, lpar_name: str, profile_name: str) -> str:
    """Return one profile's exact `io_slots` from the ADR 0165-admitted readback.

    The admitted read answers for every profile on the system, so the arm's
    profile is chosen here by exact `lpar_name` and `name` rather than by a
    `--filter` ADR 0165 does not admit. A partition may carry several profiles,
    so `lpar_name` alone is not a selection. The value is returned as read, for
    the guards' exact comparisons, once `parse_profile_io_slots` accepts it.
    Public so the recovery check selects and validates exactly as the arm does.

    Raises:
        HMCCLIError: If *output* is not the admitted table, holds other than
            exactly one row for the profile, or renders `io_slots` in a form
            ADR 0165 does not admit.
    """
    values = [
        row["io_slots"]
        for row in parse_profile_io_slot_rows(output)
        if row["lpar_name"] == lpar_name and row["name"] == profile_name
    ]
    if len(values) != 1:
        raise HMCCLIError(
            f"profile io_slots readback holds {len(values)} rows for profile "
            f"{profile_name!r} of {lpar_name!r}; expected exactly 1"
        )
    parse_profile_io_slots(values[0])
    return values[0]


def _change_io_slots_command(
    fixture: _DedicatedFixture,
    *,
    add: bool,
    drc_index: str | None = None,
    required: bool = False,
) -> str:
    """Return the documented profile mutation, without --force (ADR 0055).

    *drc_index* defaults to the arm's slot; *required* writes `is_required=1`,
    which only the io_slots scenario's setup and restore issue (#912).
    """
    arm = fixture.config
    element = f"{drc_index or fixture.drc_index}//{1 if required else 0}"
    record = build_attribute_record(
        [
            ("name", arm.profile_name),
            ("io_slots+" if add else "io_slots-", element),
            ("lpar_name", fixture.lpar_name),
        ]
    )
    return (
        f"chsyscfg -r prof -m {shlex.quote(arm.system_name)} -i {shlex.quote(record)}"
    )


def _io_slots_contains(io_slots: str, drc_index: str) -> bool:
    """Whether `io_slots` lists *drc_index* as a slot, by entry not by substring.

    `io_slots` renders as comma-separated `drc_index/pool_id/is_required`
    triples, with the literal `none` as `pool_id` for a slot in no pool, or as
    the whole value `none` when the profile holds no slot (ADR 0165). A plain
    `drc in io_slots` also matches a substring of a listed DRC index, or a run
    of characters spanning the `/` and `,` separators — so a failed `io_slots+`
    could still be recorded as a PASS against an unchanged profile, corrupting
    the ADR 0053 evidence this arm exists to produce.

    Raises:
        HMCCLIError: If *io_slots* is not in the admitted rendering.
    """
    return any(slot.drc_index == drc_index for slot in parse_profile_io_slots(io_slots))


async def _read_profile_io_slots(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> str | None:
    """Return the profile's exact `io_slots` value, or None when unreadable.

    Anything `select_profile_io_slots` refuses reads as unreadable, the empty
    answer included: a profile with no slots reads `none`, so reading an empty
    response as "no slots" would hand the guards a baseline nothing established.
    """
    st, data = await state.call(
        client,
        "hmc_run_command",
        cmd=profile_io_slot_rows_command(fixture.config.system_name),
    )
    if st != "PASS" or not isinstance(data, str):
        return None
    try:
        return select_profile_io_slots(
            data, fixture.lpar_name, fixture.config.profile_name
        )
    except HMCCLIError:
        return None


async def _read_dedicated_state(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> _DedicatedState:
    """Read live slot ownership, profile io_slots, LPAR UUID, and caller token."""
    arm = fixture.config
    slot_owner = None
    st, data = await state.call(
        client,
        "hmc_list_dedicated_pcie_slots",
        system_name_or_uuid=arm.system_name,
    )
    if st == "PASS" and isinstance(data, dict):
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("drc_index") == fixture.drc_index:
                raw_owner = item.get("owner_lpar") or ""
                slot_owner = raw_owner.strip() or None
                if slot_owner == "null":
                    slot_owner = None
                break

    profile_io_slots = await _read_profile_io_slots(client, state, fixture)

    lpar_uuid = None
    st_lpar, data_lpar = await state.call(
        client,
        "hmc_get_lpar",
        lpar_name_or_uuid=fixture.lpar_name,
        system_name_or_uuid=arm.system_name,
    )
    if st_lpar == "PASS" and isinstance(data_lpar, dict):
        lpar_uuid = data_lpar.get("UUID") or data_lpar.get("uuid")

    caller_token = None
    st_desc, data_desc = await state.call(
        client,
        "hmc_get_lpar_description",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.lpar_name,
    )
    if st_desc == "PASS" and isinstance(data_desc, str):
        caller_token = parse_lpar_ownership_caller_token(data_desc)

    return _DedicatedState(slot_owner, profile_io_slots, lpar_uuid, caller_token)


async def _admit_dedicated_environment(
    client: Client, state: RunState, config: _DedicatedConfig
) -> bool:
    """Record the HMC release and system model; admit only the exact ADR 0166 §3 pair."""
    st_v, version = await state.call(client, "hmc_run_command", cmd="lshmc -V")
    st_m, model = await state.call(
        client,
        "hmc_run_command",
        cmd=(f"lssyscfg -r sys -m {shlex.quote(config.system_name)} -F type_model"),
    )
    if st_v != "PASS" or st_m != "PASS":
        state.skip(
            29,
            "dedicated admitted environment",
            "could not read the HMC release or the managed-system type-model; "
            "the dedicated arm mutates through raw profile grammar and will "
            "not do so on an unidentified environment — SKIP dedicated arm",
        )
        return False
    version_text = str(version).strip()
    model_text = str(model).strip()
    # The arm mutates through raw profile grammar rather than through the product's
    # operations, so nothing else enforces the envelope on this path.
    admitted = _is_exact_admitted_environment(version_text, model_text)
    state.record(
        29,
        "dedicated admitted environment",
        "PASS" if admitted else "SKIP",
        f"hmc_release={version_text!r} system_model={model_text!r} "
        f"admitted={_ADMITTED_RELEASE_FIELDS!r}/{_ADMITTED_SYSTEM_MODEL!r}",
    )
    if not admitted:
        state.skip(
            29,
            "dedicated admitted environment (envelope)",
            f"HMC release {version_text!r} / model {model_text!r} is outside "
            "the exactly matched ADR 0166 §3 envelope for dedicated profile grammar; "
            "issuing io_slots mutations here would use a grammar this "
            "repository has not probed — SKIP dedicated arm",
        )
    return admitted


#: The slot ST29 picks when none is configured; preflight prints it as its prediction.
AUTO_SELECTED_SLOT = "(first slot no partition owns and no partition profile lists)"


def _slot_unowned(row: dict[str, Any]) -> bool:
    """Whether an inventory row names no owning partition (`null` is the CLI's none)."""
    owner = (row.get("owner_lpar") or "").strip()
    return not owner or owner == "null"


def _profile_lists_slot(profile_rows: list[dict[str, str]], drc_index: str) -> bool:
    """Whether any profile lists *drc_index*, decided as the #882 holder check decides it.

    Only rows that mention the DRC are parsed, as `_other_holders` in
    `operations/virtualization/pcie.py` does, so one unrelated profile in an
    unadmitted rendering cannot rule out every slot. A mentioning row the parser
    refuses counts as listing the slot: the operation refuses that slot too.
    """
    for row in profile_rows:
        if drc_index not in row["io_slots"]:
            continue
        try:
            if _io_slots_contains(row["io_slots"], drc_index):
                return True
        except HMCCLIError:
            return True
    return False


async def _auto_select_slot(
    client: Client,
    state: RunState,
    arm: _DedicatedConfig,
    inventoried: int,
    unassigned: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick the first unowned slot no partition profile lists, or SKIP.

    Inventory ownership is the running state; a profile can list a slot no
    partition owns, and assigning it is what the #882 holder check refuses. So
    a slot is eligible only when the ADR 0165-admitted table shows no profile
    listing it, and an unreadable table rules out every slot (#916).
    """
    if not unassigned:
        state.skip(
            29,
            "dedicated slot selection",
            f"no unassigned dedicated PCIe slot on {arm.system_name!r} "
            f"({inventoried} slot(s) inventoried, all owned) — SKIP dedicated arm",
        )
        return None
    st, data = await state.call(
        client,
        "hmc_run_command",
        cmd=profile_io_slot_rows_command(arm.system_name),
    )
    # The failure rides as a `CallFailure`, never in the note: `record` persists
    # only a CallFailure's redacted message and writes the note verbatim, and a
    # transport error names the HMC host. An unadmitted readback is recorded as
    # the parser's refusal rather than as the raw output, which is not redacted.
    profile_rows = None
    failure = data
    if st == "PASS":
        try:
            if not isinstance(data, str):
                raise HMCCLIError(
                    f"profile readback is {type(data).__name__}, not text"
                )
            profile_rows = parse_profile_io_slot_rows(data)
        except HMCCLIError as error:
            failure = CallFailure(
                "HMCCLIError", f"HMCCLIError: {error}", "", None, False
            )
    if profile_rows is None:
        state.record(
            29,
            "dedicated slot selection",
            "SKIP",
            failure,
            f"could not read the partition profiles on {arm.system_name!r} in the "
            "ADR 0165-admitted form; without them no unowned slot can be shown to "
            "be listed by no profile, and assigning a listed one is refused — "
            "SKIP dedicated arm",
        )
        return None
    eligible = [
        row
        for row in unassigned
        if not _profile_lists_slot(profile_rows, str(row.get("drc_index")))
    ]
    if not eligible:
        state.skip(
            29,
            "dedicated slot selection",
            f"all {len(unassigned)} unowned dedicated PCIe slot(s) on "
            f"{arm.system_name!r} are listed by a partition profile, which the "
            "assignment refuses — SKIP dedicated arm rather than select a slot "
            "the operation will refuse",
        )
        return None
    return eligible[0]


# ---------------------------------------------------------------------------
# Read-only inventory phase — ST29, scenario st29-pcie-inventory (#630)
# ---------------------------------------------------------------------------

_INVENTORY_SCENARIO = "st29-pcie-inventory"
_READ_FAILED = [Assertion("read-succeeded", False)]
#: The `pci_class` the `eth` filter of `hmc_list_io_slots` selects (V10R3 default listing).
_ETH_PCI_CLASS = "0200"


def _listed_items(status: str, data: object) -> list[dict[str, Any]] | None:
    """A passed read's `InventoryResult` items when `available`, else None."""
    if status != "PASS" or not isinstance(data, dict):
        return None
    items = data.get("items")
    if data.get("capability") != "available" or not isinstance(items, list):
        return None
    return items if all(isinstance(item, dict) for item in items) else None


def _rows(status: str, data: object) -> list[dict[str, Any]] | None:
    """A passed read's list of mappings, else None."""
    if status != "PASS" or not isinstance(data, list):
        return None
    return data if all(isinstance(row, dict) for row in data) else None


def _decimal(value: object) -> Decimal | None:
    """A served decimal (a string or a number on the wire), or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _ids(items: list[dict[str, Any]], key: str = "drc_index") -> list[str | None]:
    """Each item's *key* when it is text, else None, so every id is hashable."""
    return [value if isinstance(value, str) else None for value in _values(items, key)]


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _values(items: list[dict[str, Any]], key: str) -> list[object]:
    return [item.get(key) for item in items]


def record_dedicated_listing(state: RunState, status: str, data: object) -> None:
    """Record the ST29 dedicated-slot listing as an observation, before any SKIP."""
    items = _listed_items(status, data)
    state.record_verified(
        29,
        "hmc_list_dedicated_pcie_slots (inventory)",
        operation="pcie.list_dedicated_slots",
        scenario=_INVENTORY_SCENARIO,
        assertions=_READ_FAILED
        if items is None
        else [
            Assertion(
                "slot-rows-identified",
                bool(items)
                and all(_text(i) for i in _ids(items))
                and len(set(_ids(items))) == len(items),
            ),
            # An unowned slot's owner is the CLI's literal `null` until normalized (#1195).
            Assertion(
                "owners-normalized",
                all(item.get("owner_lpar") != "null" for item in items),
            ),
        ],
        cleanup="not-required",
        data=data if items is None else f"{len(items)} slot(s)",
    )


async def record_inventory_reads(
    client: Client, state: RunState, arm: _DedicatedConfig, dedicated: object
) -> None:
    """Observe the read-only PCIe, SR-IOV and vNIC inventory; never gates the arm.

    Runs before slot selection and before anything is created. Every call is a
    read; a failed or unexpected answer is recorded as a failed observation.
    """
    print("\n=== ST29: PCIe, SR-IOV and vNIC inventory reads (issue #630) ===")
    await _record_io_slots(client, state, arm, _listed_items("PASS", dedicated) or [])
    adapter_id = await _record_sriov_adapters(client, state, arm)
    if adapter_id is _UNREAD:
        _skip_sriov_reads(state, "the adapter listing could not be read")
    elif not isinstance(adapter_id, str):
        _skip_sriov_reads(state, "no adapter in SR-IOV mode is listed (gap)")
    elif ports := await _record_sriov_physical_ports(client, state, arm, adapter_id):
        await _record_sriov_logical_ports(client, state, arm, adapter_id, ports)
    else:
        # A logical port is checked against its listed parent, so none can be here.
        _skip_sriov_reads(state, "no physical port was read", "logical-port read")
    await _record_vnics(client, state, arm)


#: What `_record_sriov_adapters` returns when the adapter listing was not read.
_UNREAD = object()


def _skip_sriov_reads(
    state: RunState, why: str, what: str = "physical- and logical-port reads"
) -> None:
    state.skip(29, "SR-IOV port inventory", f"{why} — SKIP the {what}")


async def _record_io_slots(
    client: Client,
    state: RunState,
    arm: _DedicatedConfig,
    dedicated: list[dict[str, Any]],
) -> None:
    rows = _rows(
        *await state.call(
            client, "hmc_list_io_slots", system_name_or_uuid=arm.system_name
        )
    )
    eth = _rows(
        *await state.call(
            client,
            "hmc_list_io_slots",
            system_name_or_uuid=arm.system_name,
            pci_class="eth",
        )
    )
    expected = {
        drc
        for row, drc in zip(rows or [], _ids(rows or []), strict=True)
        if row.get("pci_class") == _ETH_PCI_CLASS
    }
    state.record_verified(
        29,
        "hmc_list_io_slots",
        operation="io_slot.list",
        scenario=_INVENTORY_SCENARIO,
        assertions=_READ_FAILED
        if rows is None or eth is None
        else [
            Assertion(
                "slot-rows-identified",
                bool(rows) and all(isinstance(i, str) for i in _ids(rows)),
            ),
            Assertion(
                "matches-dedicated-inventory", set(_ids(rows)) == set(_ids(dedicated))
            ),
            # With no Ethernet-class slot, the filter must select nothing.
            Assertion(
                "class-filter-exact",
                len(eth) == len(expected) and set(_ids(eth)) == expected,
            ),
        ],
        cleanup="not-required",
        data=f"{len(rows or [])} slot(s)",
    )


async def _record_sriov_adapters(
    client: Client, state: RunState, arm: _DedicatedConfig
) -> object:
    """Record the adapter listing; return the first SR-IOV-mode adapter's ID.

    None when no adapter is in SR-IOV mode, `_UNREAD` when the listing was not read.
    """
    status, data = await state.call(
        client, "hmc_list_sriov_adapters", system_name_or_uuid=arm.system_name
    )
    items = _listed_items(status, data)
    if items == []:
        state.record(29, "hmc_list_sriov_adapters (empty)", "PASS", data)
        return None
    adapter_id = next(
        (
            item["adapter_id"]
            for item in items or []
            if item.get("mode") == "sriov" and isinstance(item.get("adapter_id"), str)
        ),
        None,
    )
    selected = None
    if adapter_id is not None:
        selected = _listed_items(
            *await state.call(
                client,
                "hmc_list_sriov_adapters",
                system_name_or_uuid=arm.system_name,
                adapter_id=str(adapter_id),
            )
        )
    state.record_verified(
        29,
        "hmc_list_sriov_adapters",
        operation="pcie.list_sriov_adapters",
        scenario=_INVENTORY_SCENARIO,
        assertions=_READ_FAILED
        if items is None
        else [
            Assertion("capability-available", True),
            # A dedicated-mode adapter has no ID: the HMC lists it as `null` (#1202).
            Assertion(
                "adapter-rows-parsed",
                all(
                    (item.get("mode") == "sriov" and _text(item.get("adapter_id")))
                    or (
                        item.get("mode") == "dedicated"
                        and item.get("adapter_id") is None
                    )
                    for item in items
                ),
            ),
            # Without an SR-IOV-mode adapter there is no ID to filter by.
            *(
                [
                    Assertion(
                        "adapter-filter-selects-one",
                        _ids(selected or [], "adapter_id") == [adapter_id],
                    )
                ]
                if adapter_id is not None
                else []
            ),
        ],
        cleanup="not-required",
        data=data if items is None else f"selected adapter_id={adapter_id!r}",
    )
    return _UNREAD if items is None else adapter_id


async def _record_sriov_physical_ports(
    client: Client, state: RunState, arm: _DedicatedConfig, adapter_id: str
) -> set[str]:
    """Record the adapter's physical ports; return the port IDs read."""
    status, data = await state.call(
        client,
        "hmc_list_sriov_physical_ports",
        system_name_or_uuid=arm.system_name,
        adapter_id=str(adapter_id),
    )
    items = _listed_items(status, data)
    refused = False
    if items is not None:
        # The negative: the inventory refuses a port read with no adapter selector.
        # A transport failure is not that refusal, so the message decides.
        _, bare = await state.call(
            client, "hmc_list_sriov_physical_ports", system_name_or_uuid=arm.system_name
        )
        refused = (
            isinstance(bare, CallFailure) and "adapter_id is required" in bare.message
        )
    carried = [
        _decimal(value)
        for item in items or []
        if (value := item.get("minimum_capacity_granularity_percent")) is not None
    ]
    state.record_verified(
        29,
        "hmc_list_sriov_physical_ports",
        operation="pcie.list_sriov_physical_ports",
        scenario=_INVENTORY_SCENARIO,
        assertions=_READ_FAILED
        if items is None
        else [
            Assertion("capability-available", True),
            Assertion("ports-listed", bool(items)),
            # `null` is allowed per port (`eth_capacity_granularity`), but not everywhere.
            Assertion(
                "granularity-positive",
                bool(carried) and all(v is not None and v > 0 for v in carried),
            ),
            Assertion("adapter-required-refused", refused),
        ],
        cleanup="not-required",
        data=data if items is None else f"{len(items)} port(s)",
    )
    return {port for port in _ids(items or [], "physical_port_id") if port is not None}


def _logical_port_bounded(item: dict[str, Any]) -> bool:
    """Configured: 0 < capacity <= maximum <= 100. Unconfigured: no capacity."""
    if item.get("availability") == "unconfigured":
        return item.get("capacity_percent") is None
    capacity = _decimal(item.get("capacity_percent"))
    maximum = _decimal(item.get("maximum_capacity_percent"))
    return (
        capacity is not None and maximum is not None and 0 < capacity <= maximum <= 100
    )


async def _record_sriov_logical_ports(
    client: Client,
    state: RunState,
    arm: _DedicatedConfig,
    adapter_id: str,
    ports: set[str],
) -> None:
    status, data = await state.call(
        client,
        "hmc_list_sriov_logical_ports",
        system_name_or_uuid=arm.system_name,
        adapter_id=str(adapter_id),
    )
    items = _listed_items(status, data)
    if items == []:
        state.record(29, "hmc_list_sriov_logical_ports (empty)", "PASS", data)
        return
    state.record_verified(
        29,
        "hmc_list_sriov_logical_ports",
        operation="pcie.list_sriov_logical_ports",
        scenario=_INVENTORY_SCENARIO,
        assertions=_READ_FAILED
        if items is None
        else [
            Assertion("capability-available", True),
            Assertion(
                "ports-belong-to-adapter",
                set(_ids(items, "adapter_id")) == {adapter_id},
            ),
            Assertion(
                "parents-are-listed-ports",
                set(_ids(items, "physical_port_id")) <= ports,
            ),
            Assertion(
                "configured-capacity-bounded",
                all(_logical_port_bounded(item) for item in items),
            ),
        ],
        cleanup="not-required",
        data=data if items is None else f"{len(items)} logical port(s)",
    )


def _vnic_command(system_name: str) -> str:
    return (
        "lshwres -r virtualio --rsubtype vnic --level lpar "
        f"-m {shlex.quote(system_name)} -F lpar_name"
    )


async def _record_vnics(client: Client, state: RunState, arm: _DedicatedConfig) -> None:
    """Read the vNICs of the first partition the system lists one for."""
    status, data = await state.call(
        client, "hmc_run_command", cmd=_vnic_command(arm.system_name)
    )
    names = (
        [
            line.strip()
            for line in data.splitlines()
            if line.strip() and line.strip() != HMC_NO_RESULTS
        ]
        if status == "PASS" and isinstance(data, str)
        else None
    )
    if names is None:
        # The harness's own discovery read failed; `hmc_list_vnics` never ran.
        state.record(29, "vNIC discovery", "FAIL", data)
        return
    if not names:
        state.record(29, "hmc_list_vnics (empty)", "PASS", "no partition has a vNIC")
        return
    lpar_name = names[0]
    status, data = await state.call(
        client,
        "hmc_list_vnics",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=lpar_name,
    )
    rows = _rows(status, data)
    state.record_verified(
        29,
        "hmc_list_vnics",
        operation="vnic.list",
        scenario=_INVENTORY_SCENARIO,
        assertions=_READ_FAILED
        if rows is None
        else [
            Assertion(
                "vnic-rows-parsed",
                bool(rows)
                and all(
                    row.get("lpar_name") == lpar_name
                    and str(row.get("slot_num", "")).isdecimal()
                    for row in rows
                ),
            )
        ],
        cleanup="not-required",
        data=data if rows is None else f"{len(rows)} vNIC(s)",
    )


async def capture_dedicated_baseline(
    client: Client, state: RunState, *, inventory: bool = False
) -> _DedicatedFixture | None:
    """Resolve configuration and select an unassigned dedicated slot.

    With *inventory*, the dedicated arm's own call, the read-only inventory
    phase (#630) runs too; the bare-cec arm reuses this baseline without it.
    Returns the fixture to create, or None when the arm must be skipped.
    """
    print("\n=== ST29: Dedicated PCIe Baseline (issue #217) ===")
    arm = _dedicated_config(state.config)
    if arm is None:
        state.skip(
            29,
            "dedicated pcie configuration",
            "LIVE_TEST_DEDICATED_PCIE_SYSTEM_NAME and "
            "LIVE_TEST_DEDICATED_PCIE_LPAR_PREFIX are not both "
            "set, or a configured LPAR prefix, profile name, or DRC index "
            f"carries one of the HMC record delimiters {_RECORD_DELIMITERS!r}. "
            "The dedicated arm requires an explicitly configured managed "
            "system and LPAR name prefix, never falls back to a default "
            "system, and refuses a value that cannot cross the record "
            "grammar unchanged — SKIP dedicated arm",
        )
        return None

    if not await _admit_dedicated_environment(client, state, arm):
        return None

    st, data = await state.call(
        client,
        "hmc_list_dedicated_pcie_slots",
        system_name_or_uuid=arm.system_name,
    )
    state.record(29, "hmc_list_dedicated_pcie_slots (baseline)", st, data)
    if inventory:
        record_dedicated_listing(state, st, data)
    if st != "PASS" or not isinstance(data, dict):
        state.skip(
            29,
            "dedicated slot inventory",
            "dedicated-slot inventory failed — SKIP dedicated arm",
        )
        return None
    if inventory:
        await record_inventory_reads(client, state, arm, data)
    # No `capability == "capability-unavailable"` branch: `list_dedicated_slots`
    # returns the literal "available" unconditionally, so such a branch could
    # never execute and would advertise a SKIP path that does not exist. A
    # failing read is already covered above.

    rows = [item for item in data.get("items") or [] if isinstance(item, dict)]
    unassigned = [row for row in rows if _slot_unowned(row)]
    if arm.drc_index is not None:
        selected = next(
            (row for row in unassigned if row.get("drc_index") == arm.drc_index),
            None,
        )
        if selected is None:
            state.skip(
                29,
                "dedicated slot selection",
                f"configured drc_index {arm.drc_index!r} is absent from the "
                "inventory or already owned — SKIP dedicated arm rather than "
                "mutate a slot this run did not select",
            )
            return None
    else:
        selected = await _auto_select_slot(client, state, arm, len(rows), unassigned)
        if selected is None:
            return None

    run_marker = _new_run_marker()
    lpar_name = f"{arm.lpar_prefix}{run_marker}"
    fixture = _DedicatedFixture(
        config=arm,
        run_marker=run_marker,
        lpar_name=lpar_name,
        probe_lpar_name=f"{lpar_name}-createtime",
        drc_index=str(selected.get("drc_index")),
    )
    _record_fixture_artifacts(state, fixture)
    state.record(
        29,
        "dedicated slot selection",
        "PASS",
        f"selected drc_index={fixture.drc_index!r} "
        f"description={selected.get('description')!r} on "
        f"{arm.system_name!r}; fixture lpar={fixture.lpar_name!r} "
        f"run_marker={run_marker!r}",
    )
    return fixture


def _record_fixture_artifacts(state: RunState, fixture: _DedicatedFixture) -> None:
    """Mirror what this arm created into the run's artifacts.

    `live_test_recovery.py` checks teardown from outside the run that attempted
    it, and needs the per-run marker, the fixture name, the slot and the
    captured baseline. All four exist only inside this module otherwise, and the
    marker is random per run, so nothing downstream can reconstruct them.

    Recording only: no guard reads these fields and no cleanup decision turns on
    them. Called again after the baseline is captured, since that is ST30 while
    the rest is known at ST29.
    """
    state.artifacts.pcie_run_marker = fixture.run_marker
    state.artifacts.pcie_fixture_lpar = fixture.lpar_name
    state.artifacts.pcie_drc_index = fixture.drc_index
    state.artifacts.pcie_baseline_io_slots = fixture.baseline_io_slots


def partition_not_found(status: str, data: object) -> bool:
    """Whether a failed partition lookup is the HMC's "no such partition" answer.

    HSCL8012 is the HMC's message for a partition name it does not have: "The
    partition named ... was not found" (seen live in ADR 0162; IBM HSCL reference
    `docs/refs/ibm-hsc-ref/HSCL80xx.md:147`). Every other
    failure -- a lost connection, an authentication refusal, any other HSCL code --
    says nothing about whether the partition exists, so it is not this.

    It is also not proof of absence: IBM's recovery action for HSCL8012 includes
    rebuilding the managed system (`docs/refs/ibm-hsc-ref/HSCL80xx.md:157`), so a
    stale HMC inventory can answer it too.
    Callers treat it as the best available evidence, not a guarantee, and
    `_created_despite_failure` asks twice before believing it (#906).
    """
    return (
        status != "PASS"
        and isinstance(data, CallFailure)
        and "HSCL8012" in data.message
    )


#: Seconds between a failed create's HSCL8012 lookup and the one re-read that
#: must repeat it before absence is confirmed. Unmeasured: how long a lost REST
#: create, or the CLI view of one, can lag is the #879 live window's question.
_ABSENCE_REREAD_DELAY_S = 10.0


class _Absence(Enum):
    """Why a failed create's readback found no partition of this run's."""

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


async def _created_despite_failure(
    client: Client, state: RunState, fixture: _DedicatedFixture, lpar_name: str
) -> str | _Absence:
    """Return the UUID of *lpar_name* when a failed create in fact created it.

    Applies to a create the invariant every ``chsyscfg`` in this module already
    obeys: a command whose response was lost has still executed. A create that
    times out after the HMC made the partition would otherwise leave an orphan
    the run believes it never made — and for the create-time probe, one holding
    the dedicated slot — with no cleanup and no manual-recovery row.

    Ownership is confirmed by the run marker before claiming the partition, so
    a name collision with something this run did not create is never adopted.
    Absence is confirmed by HSCL8012 (see `partition_not_found`), or by a readback
    that answered with something other than this run's marker. Any other failed
    read, or an empty description (a partition whose ownership stamp never
    landed), leaves it unconfirmed. HSCL8012 is the best available evidence rather
    than proof: IBM documents a stale HMC inventory as one of its causes.

    A create still in flight, or an SSH view lagging the REST create, can
    answer HSCL8012 for a partition that then appears, so the first HSCL8012
    is re-read once after `_ABSENCE_REREAD_DELAY_S` and only a second one
    confirms absence (#906). The re-read's answer is then judged like any other.
    """

    async def lookup() -> tuple[str, object]:
        return await state.call(
            client,
            "hmc_get_lpar_description",
            system_name_or_uuid=fixture.config.system_name,
            lpar_name_or_uuid=lpar_name,
        )

    st, data = await lookup()
    if partition_not_found(st, data):
        await asyncio.sleep(_ABSENCE_REREAD_DELAY_S)
        st, data = await lookup()
    if partition_not_found(st, data):
        return _Absence.CONFIRMED
    if st != "PASS" or not isinstance(data, str) or not data.strip():
        return _Absence.UNCONFIRMED
    if parse_lpar_ownership_caller_token(data) != fixture.run_marker:
        return _Absence.CONFIRMED
    uuid_match = re.search(r"'UUID':\s*'([0-9A-Fa-f-]{36})'", data)
    return uuid_match.group(1) if uuid_match else ""


async def name_absent(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> bool:
    """Whether the fixture's name answers HSCL8012 after its delete.

    Only a readable description carrying this run's marker means the partition
    is still there. Any other answer — a lost connection, an SSH view lagging
    the REST delete — is read once more after `_ABSENCE_REREAD_DELAY_S`,
    as `_created_despite_failure` does (#906), before it is believed.
    """

    async def lookup() -> tuple[str, Any]:
        return await state.call(
            client,
            "hmc_get_lpar_description",
            system_name_or_uuid=fixture.config.system_name,
            lpar_name_or_uuid=fixture.lpar_name,
        )

    st, data = await lookup()
    if partition_not_found(st, data):
        return True
    if st == "PASS" and isinstance(data, str):
        return False
    await asyncio.sleep(_ABSENCE_REREAD_DELAY_S)
    st, data = await lookup()
    return partition_not_found(st, data)


def _record_held(
    state: RunState,
    subtask: int,
    tool: str,
    *,
    assertions: list[Assertion],
    cleanup: str,
    data: Any,
) -> None:
    """Record a step judged as `record_verified` would judge it, without promoting it.

    The lpar-power arm (subtask 41) owns the `lpar.create` and `lpar.delete`
    observations, and the catalog keeps one per operation, so this arm's would
    overwrite it (#1389).
    """
    if cleanup not in CLEANUP:
        raise ValueError(f"cleanup disposition is not a known value: {cleanup!r}")
    unmet = [item.id for item in assertions if not item.holds]
    if cleanup not in {"passed", "not-required"}:
        unmet.append(f"cleanup {cleanup}")
    note = "unmet: " + ", ".join(unmet) if unmet else ""
    state.record(subtask, tool, "FAIL" if unmet else "PASS", data, note)


async def _probe_create_time_assignment(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> bool:
    """Exercise create-time dedicated assignment, then remove the probe partition.

    Returns False only when a probe partition is known to exist and its cleanup
    did not complete; the fixture must not be created then, because the fixture's
    assign would ask for a slot another profile may still list. A failed create
    whose partition cannot be confirmed either way returns True with a recovery
    row: the fixture's own assign then refuses a slot another profile lists.
    That probe is not retried at final cleanup, and its row says so; the
    manual-recovery row is this run's only record of it (#906).
    """
    arm = fixture.config
    st, data = await state.call(
        client,
        "hmc_create_lpar",
        system_name_or_uuid=arm.system_name,
        name=fixture.probe_lpar_name,
        caller_token=fixture.run_marker,
        assignments={
            "dedicated": [
                {
                    "profile_name": arm.profile_name,
                    "drc_index": fixture.drc_index,
                }
            ]
        },
    )
    record_status, reason = judge_create_result(st, data)
    state.record(
        30,
        "hmc_create_lpar (create-time dedicated assignment)",
        record_status,
        data,
        reason,
    )
    if st == "PASS":
        fixture.probe_created = True
        data = plain_data(data)
        if isinstance(data, dict) and isinstance(data.get("lpar"), dict):
            fixture.probe_lpar_uuid = data["lpar"].get("UUID") or data["lpar"].get(
                "uuid"
            )
    else:
        # A lost response is not a refusal: read back before believing
        # nothing was created.
        probe_uuid = await _created_despite_failure(
            client, state, fixture, fixture.probe_lpar_name
        )
        if probe_uuid is _Absence.CONFIRMED:
            return True
        if probe_uuid is _Absence.UNCONFIRMED:
            state.record(
                30,
                "create-time probe partition not confirmed absent",
                "FAIL",
                "MANUAL RECOVERY REQUIRED (check): the create-time probe reported "
                f"{st} and no partition {fixture.probe_lpar_name!r} carrying this "
                f"run's marker {fixture.run_marker!r} could be confirmed on "
                f"{arm.system_name!r}. A lost response may still have created it "
                f"holding slot {fixture.drc_index!r}; if it exists with that marker, "
                "remove the slot from its profile and then delete it. This run "
                "will not retry cleanup of it: it could not confirm the partition "
                "is this run's.",
            )
            return True
        fixture.probe_created = True
        fixture.probe_lpar_uuid = probe_uuid or None
        state.record(
            30,
            "create-time probe created a partition despite reporting failure",
            "FAIL",
            "MANUAL RECOVERY REQUIRED (if cleanup below does not clear it): "
            f"the create-time probe reported {st} but partition "
            f"{fixture.probe_lpar_name!r} exists on {arm.system_name!r} "
            f"carrying this run's marker {fixture.run_marker!r}. Cleanup "
            "will attempt to remove it.",
        )

    probe = replace(fixture, lpar_name=fixture.probe_lpar_name)
    io_slots = await _read_profile_io_slots(client, state, probe)
    landed = io_slots is not None and _io_slots_contains(
        io_slots, str(fixture.drc_index)
    )
    state.record(
        30,
        "create-time assignment profile readback",
        "PASS" if st == "PASS" and landed else "FAIL",
        f"probe io_slots={io_slots!r} expected to contain drc_index="
        f"{fixture.drc_index!r} (create status {st})",
    )
    cleaned = await _cleanup_probe_partition(client, state, fixture)
    _record_held(
        state,
        30,
        "hmc_create_lpar (create-time judged)",
        assertions=[
            Assertion("create-call-succeeded", st == "PASS"),
            Assertion("profile-lists-slot", landed),
        ],
        cleanup="passed" if cleaned else "failed",
        data=f"probe io_slots={io_slots!r} drc_index={fixture.drc_index!r}",
    )
    if cleaned:
        fixture.probe_created = False
    return cleaned


async def create_dedicated_fixture(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> bool:
    """Create the run-unique owner-stamped fixture LPAR.

    Returns True when the fixture exists and its UUID was resolved, which is
    the only state in which the arm may mutate hardware.
    """
    print("\n=== ST30: Dedicated PCIe Fixture Create (issue #217) ===")

    # Create-time assignment on its own probe partition (ADR 0166). With ADR
    # 0055's gate lifted inside the envelope this creates a second partition
    # holding the slot, so it is cleaned up before the fixture exists: the slot
    # is never listed by two profiles at once.
    if not await _probe_create_time_assignment(client, state, fixture):
        state.skip(
            30,
            "dedicated fixture create",
            "the create-time probe partition could not be cleaned up, so the "
            "slot may still be listed by it — the fixture is not created and "
            "the slot is not assigned again; cleanup retries the probe",
        )
        return False
    return await create_fixture_partition(client, state, fixture)


def _fixture_creation_identity(
    data: Any, current_uuid: str | None
) -> tuple[str | None, Any]:
    """Read identity claims from a structured result or its text representation.

    These claims do not replace the lifecycle's authoritative ownership checks.
    Preserve a previously captured UUID when text carries no new identity.
    """
    lpar_uuid = current_uuid
    stamped = None
    if isinstance(data, dict):
        body = data.get("lpar")
        if isinstance(body, dict):
            lpar_uuid = body.get("UUID") or body.get("uuid")
        stamped = data.get("ownership_stamped")
    elif isinstance(data, str):
        uuid_match = re.search(r"'UUID':\s*'([0-9A-Fa-f-]{36})'", data)
        if uuid_match and not lpar_uuid:
            lpar_uuid = uuid_match.group(1)
        if "ownership_stamped=True" in data:
            stamped = True
        elif "ownership_stamped=False" in data:
            stamped = False
    return lpar_uuid, stamped


async def create_fixture_partition(
    client: Client,
    state: RunState,
    fixture: _DedicatedFixture,
    *,
    resources: dict[str, Any] | None = None,
) -> bool:
    """Create the fixture partition and confirm its identity and profile baseline.

    Returns True only when the partition exists, carries this run's ownership
    stamp, has a resolved UUID and a readable baseline `io_slots`. The bare-cec
    arm shares it and passes explicit *resources*; the dedicated arm passes none,
    so its create call is the tool's default sizing.
    """
    arm = fixture.config
    # Two literal dispatches rather than a splat: the static dispatch guards in
    # tests/test_live_runner.py read every keyword each call site passes.
    if resources is None:
        st, data = await state.call(
            client,
            "hmc_create_lpar",
            system_name_or_uuid=arm.system_name,
            name=fixture.lpar_name,
            caller_token=fixture.run_marker,
        )
    else:
        st, data = await state.call(
            client,
            "hmc_create_lpar",
            system_name_or_uuid=arm.system_name,
            name=fixture.lpar_name,
            caller_token=fixture.run_marker,
            resources=resources,
        )
    record_status, reason = judge_create_result(st, data)
    state.record(30, "hmc_create_lpar (fixture)", record_status, data, reason)
    if st != "PASS":
        # Same invariant as the probe above: confirm the partition is really
        # absent rather than assuming a failed create created nothing.
        stray_uuid = await _created_despite_failure(
            client, state, fixture, fixture.lpar_name
        )
        if stray_uuid is _Absence.UNCONFIRMED:
            state.record(
                30,
                "fixture partition not confirmed absent",
                "FAIL",
                "MANUAL RECOVERY REQUIRED (check): the fixture create reported "
                f"{st} and no partition {fixture.lpar_name!r} carrying this run's "
                f"marker {fixture.run_marker!r} could be confirmed on "
                f"{arm.system_name!r}. A lost response may still have created it; "
                "if it exists with that marker, delete it.",
            )
            state.skip(
                30,
                "dedicated fixture create",
                "fixture LPAR create failed and its absence could not be confirmed "
                "— SKIP dedicated arm; see the manual-recovery row",
            )
            return False
        if stray_uuid is _Absence.CONFIRMED:
            state.skip(
                30,
                "dedicated fixture create",
                "fixture LPAR create failed and no partition carrying this "
                "run's marker exists — SKIP dedicated arm; nothing to clean up",
            )
            return False
        fixture.created = True
        fixture.lpar_uuid = stray_uuid or None
        state.record(
            30,
            "fixture create reported failure but created a partition",
            "FAIL",
            "MANUAL RECOVERY REQUIRED (if cleanup below does not clear it): "
            f"fixture create reported {st} but partition {fixture.lpar_name!r} "
            f"exists on {arm.system_name!r} carrying this run's marker "
            f"{fixture.run_marker!r}. SKIP the arm; cleanup will attempt to "
            "remove it.",
        )
        state.skip(
            30,
            "dedicated fixture create",
            "fixture LPAR create failed — SKIP dedicated arm; the partition it "
            "created despite failing is handed to cleanup",
        )
        return False
    fixture.created = True

    fixture.lpar_uuid, stamped = _fixture_creation_identity(data, fixture.lpar_uuid)
    if isinstance(data, dict):
        state.record(
            30,
            "fixture ownership stamp",
            "PASS" if stamped is True else "FAIL",
            f"ownership_stamped={stamped!r} warnings={data.get('warnings')!r}",
        )
    elif isinstance(data, str) and stamped is not None:
        state.record(
            30,
            "fixture ownership stamp",
            "PASS" if stamped is True else "FAIL",
            f"ownership_stamped={stamped!r} (parsed from response text)",
        )

    if stamped is not True:
        # When stamped is None the live runner did not parse the response as a
        # dict (firmware returned a non-JSON body). Fall back to reading the
        # description to confirm the caller token actually landed before
        # blocking — a None return is an unknown, not a confirmed failure.
        if stamped is None:
            st_desc, data_desc = await state.call(
                client,
                "hmc_get_lpar_description",
                system_name_or_uuid=arm.system_name,
                lpar_name_or_uuid=fixture.lpar_name,
            )
            confirmed_token = (
                parse_lpar_ownership_caller_token(data_desc)
                if st_desc == "PASS" and isinstance(data_desc, str)
                else None
            )
            if confirmed_token == fixture.run_marker:
                stamped = True
                state.record(
                    30,
                    "fixture ownership stamp (confirmed via description)",
                    "PASS",
                    f"caller_token={confirmed_token!r} confirmed in description; "
                    "response did not carry ownership_stamped in parseable form",
                )
        if stamped is not True:
            # The caller token is the half of the identity Guard A checks first
            # and refuses on unconditionally. Without it, assigning a slot here
            # does not risk a stranded slot on a partition cleanup may not touch
            # — it guarantees one. `False` means the stamp and the caller segment
            # were both lost; `None` means the stamp was skipped.
            state.skip(
                30,
                "fixture identity (ownership stamp)",
                f"fixture {fixture.lpar_name!r} was created but its ADR 0064 "
                f"ownership stamp did not land (ownership_stamped={stamped!r}), "
                "so cleanup could never prove this run owns it; no hardware will "
                "be mutated — proceeding directly to cleanup",
            )
            return False

    if not fixture.lpar_uuid:
        st_get, data_get = await state.call(
            client,
            "hmc_get_lpar",
            lpar_name_or_uuid=fixture.lpar_name,
            system_name_or_uuid=arm.system_name,
        )
        if st_get == "PASS" and isinstance(data_get, dict):
            fixture.lpar_uuid = data_get.get("UUID") or data_get.get("uuid")

    if not fixture.lpar_uuid:
        state.skip(
            30,
            "fixture identity",
            f"fixture {fixture.lpar_name!r} was created but its UUID could not "
            "be resolved; no hardware will be mutated without a captured "
            "identity — proceeding directly to cleanup",
        )
        return False

    fixture.baseline_io_slots = await _read_profile_io_slots(client, state, fixture)
    _record_fixture_artifacts(state, fixture)
    state.record(
        30,
        "fixture profile io_slots (baseline)",
        "PASS" if fixture.baseline_io_slots is not None else "FAIL",
        f"lpar_uuid={fixture.lpar_uuid!r} "
        f"baseline io_slots={fixture.baseline_io_slots!r}",
    )
    return fixture.baseline_io_slots is not None


async def assign_dedicated_slot(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> bool:
    """Assign the selected slot to the fixture profile and confirm by readback."""
    arm = fixture.config
    print("\n=== ST31: Dedicated PCIe Assign (issue #217) ===")

    # The operation, not the raw grammar: inside the envelope it now mutates and
    # verifies (ADR 0166), so issuing `io_slots+` after it would add the slot a
    # second time.
    st, data = await state.call(
        client,
        "hmc_assign_dedicated_pcie_slot",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.lpar_name,
        profile_name=arm.profile_name,
        drc_index=fixture.drc_index,
    )
    state.record(31, "hmc_assign_dedicated_pcie_slot", st, data)

    # Read back whichever way the operation reported. `RunState.call` returns
    # FAIL for any raised exception, and the SSH transport raises when its
    # timeout expires — after the HMC has already executed chsyscfg. Returning
    # early on a FAIL would leave the run believing it had not written
    # something it had, which is the belief cleanup must never hold.
    applied = await _read_profile_io_slots(client, state, fixture)
    assigned = applied is not None and _io_slots_contains(
        applied, str(fixture.drc_index)
    )
    if assigned:
        fixture.applied_io_slots = applied
    fixture.evidence["assign"] = (st == "PASS", assigned)
    state.record(
        31,
        "profile io_slots readback (post-assign)",
        "PASS" if assigned else "FAIL",
        f"io_slots={applied!r} expected to contain drc_index="
        f"{fixture.drc_index!r} (command status {st})",
    )
    return st == "PASS" and assigned


async def verify_dedicated_assigned(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> bool:
    """Verify the assignment through profile and inventory readback."""
    print("\n=== ST32: Dedicated PCIe Post-Assign Verify (issue #217) ===")
    observed = await _read_dedicated_state(client, state, fixture)
    profile_ok = observed.profile_io_slots is not None and _io_slots_contains(
        observed.profile_io_slots, str(fixture.drc_index)
    )
    state.record(
        32,
        "dedicated post-assign profile verify",
        "PASS" if profile_ok else "FAIL",
        _dedicated_state_summary(observed),
    )
    # Inventory ownership is informational: a profile-only assignment is not
    # expected to move the effective layer for a partition that has never
    # activated, the same asymmetry the SR-IOV arm records at ST25.
    state.record(
        32,
        "dedicated post-assign inventory owner (informational)",
        "PASS",
        f"inventory owner_lpar={observed.slot_owner!r} "
        "(a profile assignment does not change effective slot ownership until "
        "the partition activates)",
    )
    return profile_ok


async def unassign_dedicated_slot(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> bool:
    """Remove the slot and require the exact captured baseline to return."""
    print("\n=== ST33: Dedicated PCIe Unassign (issue #217) ===")
    st, data = await state.call(
        client,
        "hmc_run_command",
        cmd=_change_io_slots_command(fixture, add=False),
    )
    state.record(33, "chsyscfg io_slots- (unassign)", st, data)

    # Same reason as the assign: a removal whose response was lost has still
    # removed, and the run must not believe otherwise.
    observed = await _read_profile_io_slots(client, state, fixture)
    restored = observed is not None and observed == fixture.baseline_io_slots
    state.record(
        33,
        "profile io_slots exact baseline restore",
        "PASS" if restored else "FAIL",
        f"io_slots={observed!r} baseline={fixture.baseline_io_slots!r} "
        f"(command status {st})",
    )
    state.record_verified(
        33,
        "chsyscfg-io-slots-remove",
        operation="command.run",
        scenario=_DEDICATED_SCENARIO,
        assertions=[
            Assertion("remove-command-succeeded", st == "PASS"),
            Assertion("profile-restored-to-baseline", restored),
        ],
        cleanup="not-required",
        data=f"io_slots={observed!r} baseline={fixture.baseline_io_slots!r}",
    )
    return st == "PASS" and restored


async def reassign_dedicated_slot(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> bool:
    """Reassign the same slot to the existing fixture, proving the round trip."""
    print("\n=== ST33: Dedicated PCIe Reassign (issue #217) ===")
    st, data = await state.call(
        client,
        "hmc_run_command",
        cmd=_change_io_slots_command(fixture, add=True),
    )
    state.record(33, "chsyscfg io_slots+ (reassign)", st, data)

    # The reassign is the LAST mutation before cleanup, so a lost response or
    # a lost confirming read here is the most dangerous of the three; read
    # back regardless.
    applied = await _read_profile_io_slots(client, state, fixture)
    ok = applied is not None and _io_slots_contains(applied, str(fixture.drc_index))
    if ok:
        fixture.applied_io_slots = applied
    fixture.evidence["reassign"] = (st == "PASS", ok)
    state.record(
        33,
        "profile io_slots readback (post-reassign)",
        "PASS" if ok else "FAIL",
        f"io_slots={applied!r} expected to contain drc_index="
        f"{fixture.drc_index!r} (command status {st})",
    )
    return st == "PASS" and ok


async def _cleanup_probe_partition(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> bool:
    """Delete the create-time probe partition, on its own caller token.

    Returns True only when the probe was deleted. Independent of the fixture's
    own guards: a refusal here records recovery evidence and returns, and must
    not stop the fixture from being cleaned up. The probe was created with
    `caller_token=fixture.run_marker`, so a partition of that name carrying a
    different token is not this run's.
    """
    arm = fixture.config
    st_desc, data_desc = await state.call(
        client,
        "hmc_get_lpar_description",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.probe_lpar_name,
    )
    probe_token = (
        parse_lpar_ownership_caller_token(data_desc)
        if st_desc == "PASS" and isinstance(data_desc, str)
        else None
    )
    if probe_token != fixture.run_marker:
        state.record(
            34,
            "dedicated cleanup: probe run-marker mismatch",
            "FAIL",
            "MANUAL RECOVERY REQUIRED: the create-time probe partition "
            f"{fixture.probe_lpar_name!r} on {arm.system_name!r} does not "
            f"carry this run's caller token (read {probe_token!r}, expected "
            f"{fixture.run_marker!r}); it was NOT deleted. Inspect it and "
            "remove it by hand once identified.",
        )
        return False

    # Hardware before the partition, decided on the probe's LIVE profile: a
    # create-time assignment that landed holds the slot, and one that did not
    # has nothing to remove — issuing `io_slots-` then would fail and strand
    # the partition instead. An unreadable profile proves neither, so it
    # refuses both the removal and the delete.
    probe = replace(
        fixture, lpar_name=fixture.probe_lpar_name, lpar_uuid=fixture.probe_lpar_uuid
    )
    removal = f"`{_change_io_slots_command(probe, add=False)}`"
    before = await _read_profile_io_slots(client, state, probe)
    if before is None:
        state.record(
            34,
            "dedicated cleanup: probe profile unreadable",
            "FAIL",
            "MANUAL RECOVERY REQUIRED: the create-time probe partition "
            f"{fixture.probe_lpar_name!r} on {arm.system_name!r} could not have "
            "its io_slots read, so this run cannot tell whether it holds slot "
            f"{fixture.drc_index!r}; it was NOT deleted. If it lists the slot, "
            f"run {removal}, then delete {fixture.probe_lpar_name!r}.",
        )
        return False
    if _io_slots_contains(before, str(fixture.drc_index)):
        st_rm, data_rm = await state.call(
            client, "hmc_run_command", cmd=_change_io_slots_command(probe, add=False)
        )
        state.record(34, "chsyscfg io_slots- (probe partition)", st_rm, data_rm)
        after = await _read_profile_io_slots(client, state, probe)
        if after is None or _io_slots_contains(after, str(fixture.drc_index)):
            state.record(
                34,
                "dedicated cleanup: probe slot removal failed",
                "FAIL",
                "MANUAL RECOVERY REQUIRED: dedicated slot "
                f"{fixture.drc_index!r} could not be confirmed removed from the "
                f"create-time probe partition {fixture.probe_lpar_name!r} on "
                f"{arm.system_name!r} (io_slots={after!r}); the partition was "
                "NOT deleted, because deleting it would strand the slot. Run "
                f"{removal} and then delete {fixture.probe_lpar_name!r}.",
            )
            return False

    # Act on the identity, not the name, for the same reason Guard C does.
    st, data = await state.call(
        client,
        "hmc_delete_lpar",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.probe_lpar_uuid or fixture.probe_lpar_name,
    )
    state.record(34, "hmc_delete_lpar (create-time probe partition)", st, data)
    if st != "PASS":
        state.record(
            34,
            "dedicated cleanup: probe partition delete failed",
            "FAIL",
            "MANUAL RECOVERY REQUIRED: the create-time probe partition "
            f"{fixture.probe_lpar_name!r} on {arm.system_name!r} still "
            f"exists and must be removed by hand. Error: {str(data)[:400]}",
        )
        return False
    return True


async def cleanup_dedicated(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> str | None:
    """Remove the slot, then delete the fixture — each only on an exact match.

    Returns the fixture delete call's status, or None when no delete was issued.
    """
    arm = fixture.config
    print("\n=== ST34: Dedicated PCIe Cleanup (issue #217) ===")

    # Reached with the probe still present only when ST30's own cleanup of it
    # did not complete (or the arm raised before it ran), so this is the one
    # retry. It is a different partition with its own identity, so its outcome
    # never gates the fixture's.
    if fixture.probe_created and await _cleanup_probe_partition(client, state, fixture):
        fixture.probe_created = False
    if not fixture.created:
        return None

    # Guard A — fixture identity, re-read immediately before any mutation.
    observed = await _read_dedicated_state(client, state, fixture)
    state.record(
        34, "dedicated pre-cleanup state", "PASS", _dedicated_state_summary(observed)
    )
    recovery = (
        f"MANUAL RECOVERY REQUIRED: fixture {fixture.lpar_name!r} on "
        f"{arm.system_name!r} could not be confirmed as this run's; no "
        "mutation was attempted. Inspect it and, once you have confirmed it "
        f"is this run's fixture, remove slot {fixture.drc_index!r} with "
        f"`{_change_io_slots_command(fixture, add=False)}` and then delete the "
        "partition."
    )
    if observed.caller_token != fixture.run_marker:
        state.record(
            34,
            "dedicated cleanup: run-marker mismatch",
            "FAIL",
            f"{recovery} Expected caller token {fixture.run_marker!r}, read "
            f"{observed.caller_token!r}.",
        )
        return None
    if fixture.lpar_uuid is not None and observed.lpar_uuid != fixture.lpar_uuid:
        state.record(
            34,
            "dedicated cleanup: uuid mismatch",
            "FAIL",
            f"{recovery} Expected UUID {fixture.lpar_uuid!r}, read "
            f"{observed.lpar_uuid!r}.",
        )
        return None

    # Guard B — remove the slot before the partition, decided on LIVE state.
    #
    # Deliberately not gated on anything this run believes it did — not a
    # `slot_assigned` flag, and not `applied_io_slots is not None`. Both are
    # false on reachable paths where the profile really does carry the DRC
    # index: an assign or reassign whose response was lost to a timeout, and
    # one whose confirming read failed. On each, a belief-gated branch skips
    # removal and Guard C deletes a partition with a slot still on it. The
    # profile is the fact; a flag is only a memory of it. An unreadable value
    # (None) is also unequal to the baseline, so it enters the branch and is
    # refused inside it.
    drifted = (
        fixture.baseline_io_slots is not None
        and observed.profile_io_slots != fixture.baseline_io_slots
    )
    if drifted:
        if (
            fixture.applied_io_slots is None
            or observed.profile_io_slots != fixture.applied_io_slots
        ):
            state.record(
                34,
                "dedicated cleanup: profile drift",
                "FAIL",
                "MANUAL RECOVERY REQUIRED: the fixture profile's io_slots is "
                f"{observed.profile_io_slots!r}, which is neither the captured "
                f"baseline ({fixture.baseline_io_slots!r}) nor the value this "
                f"run applied ({fixture.applied_io_slots!r}), so this run "
                "cannot prove the deviation is its own. No mutation attempted "
                "and the partition was NOT deleted — deleting it would strand "
                "whatever is assigned. Recover with "
                f"`{_change_io_slots_command(fixture, add=False)}`.",
            )
            return None
        st, data = await state.call(
            client,
            "hmc_run_command",
            cmd=_change_io_slots_command(fixture, add=False),
        )
        state.record(34, "chsyscfg io_slots- (cleanup)", st, data)
        if st != "PASS":
            state.record(
                34,
                "dedicated cleanup: slot removal failed",
                "FAIL",
                "MANUAL RECOVERY REQUIRED: slot removal failed, so the "
                "partition was not deleted — deleting it now would strand "
                f"slot {fixture.drc_index!r}. Run "
                f"`{_change_io_slots_command(fixture, add=False)}` then delete "
                f"{fixture.lpar_name!r}. Error: {str(data)[:400]}",
            )
            return None
        after = await _read_profile_io_slots(client, state, fixture)
        if after != fixture.baseline_io_slots:
            state.record(
                34,
                "dedicated cleanup: baseline not restored",
                "FAIL",
                "MANUAL RECOVERY REQUIRED: io_slots is "
                f"{after!r} after removal, not the captured baseline "
                f"{fixture.baseline_io_slots!r}; the partition was not deleted. "
                f"Reconcile {fixture.lpar_name!r} by hand.",
            )
            return None
        state.record(
            34,
            "dedicated cleanup: slot removed",
            "PASS",
            f"io_slots restored to the captured baseline {after!r}",
        )

    # Guard C — re-read identity once more, immediately before the delete.
    final = await _read_dedicated_state(client, state, fixture)
    if final.caller_token != fixture.run_marker or (
        fixture.lpar_uuid is not None and final.lpar_uuid != fixture.lpar_uuid
    ):
        state.record(
            34,
            "dedicated cleanup: identity changed before delete",
            "FAIL",
            "MANUAL RECOVERY REQUIRED: the fixture's identity changed between "
            "slot removal and deletion — read caller_token="
            f"{final.caller_token!r} uuid={final.lpar_uuid!r}, expected "
            f"{fixture.run_marker!r} / {fixture.lpar_uuid!r}. The partition was "
            "NOT deleted; remove it by hand after confirming what it is.",
        )
        return None
    # `_read_dedicated_state` already fetched the profile, so this clause is
    # free — and without it the "refuse the delete while the profile differs
    # from the baseline" rule is enforced only at Guard A's earlier read.
    # A slot added by a concurrent session after that read, or after Guard B's
    # confirming read, would otherwise be stranded by this delete. On the
    # never-assigned path Guard A's read is the only profile observation at
    # all, so this is the whole of the window.
    if (
        fixture.baseline_io_slots is not None
        and final.profile_io_slots != fixture.baseline_io_slots
    ):
        state.record(
            34,
            "dedicated cleanup: profile changed before delete",
            "FAIL",
            "MANUAL RECOVERY REQUIRED: the fixture profile's io_slots is "
            f"{final.profile_io_slots!r} immediately before the delete, not "
            f"the captured baseline {fixture.baseline_io_slots!r}; something "
            "changed it after this run's last check. The partition was NOT "
            "deleted, because deleting it would strand whatever is assigned.",
        )
        return None

    # Act on the identity Guard C just verified. `hmc_delete_lpar` accepts a
    # UUID or a name; deleting by name would re-resolve the name and reopen
    # the window the guard closed. `ownership_override` stays off: the fixture
    # is stamped by this run, so the tool's own description-token check passes
    # on every intended path, and it is the one operation-layer check that
    # survives the escape hatch.
    st, data = await state.call(
        client,
        "hmc_delete_lpar",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.lpar_uuid or fixture.lpar_name,
    )
    state.record(34, "hmc_delete_lpar (fixture cleanup)", st, data)
    if st != "PASS":
        state.record(
            34,
            "dedicated cleanup: fixture delete failed",
            "FAIL",
            f"MANUAL RECOVERY REQUIRED: fixture {fixture.lpar_name!r} on "
            f"{arm.system_name!r} still exists and must be deleted by hand. "
            f"Its slot assignment was already removed. Error: {str(data)[:400]}",
        )
    return st


async def exercise_dedicated_pcie_assignment(client: Client, state: RunState) -> None:
    """Orchestrate the dedicated-slot assign/verify/unassign/reassign/cleanup arm."""
    print("\n============================")
    print("=== Dedicated PCIe Live Test (issue #217) ===")
    print("============================")

    fixture = await capture_dedicated_baseline(client, state, inventory=True)
    if fixture is None:
        print("  Dedicated baseline SKIP — halting dedicated arm")
        return

    try:
        await _exercise_dedicated_steps(client, state, fixture)
    except Exception as exc:  # noqa: BLE001 — see below
        state.record(
            34,
            "dedicated arm raised before cleanup",
            "FAIL",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        # `created` covers the fixture; `probe_created` covers a create-time
        # probe partition that ST30 could not clean up, or that the arm raised
        # before cleaning up — it may hold the slot. A create that never happened has
        # nothing to clean up, and calling cleanup then would emit a
        # manual-recovery row for a partition that does not exist.
        deleted = None
        if fixture.created or fixture.probe_created:
            deleted = await cleanup_dedicated(client, state, fixture)
        clean = False
        if deleted is not None:
            absent = await name_absent(client, state, fixture)
            clean = absent and not fixture.probe_created
            _record_held(
                state,
                34,
                "hmc_delete_lpar (judged)",
                assertions=[
                    Assertion("delete-call-succeeded", deleted == "PASS"),
                    Assertion("lpar-name-absent", absent),
                ],
                cleanup="not-required",
                data=f"lpar={fixture.lpar_name!r} delete status {deleted}",
            )
        _record_dedicated_additions(state, fixture, clean)


def _record_dedicated_additions(
    state: RunState, fixture: _DedicatedFixture, clean: bool
) -> None:
    """Record each slot addition once the teardown has decided its cleanup.

    Cleanup is `passed` only when the fixture delete was confirmed by absence
    and no probe partition remains: an addition whose partition survives is not
    one this run undid.
    """
    cleanup = "passed" if clean else "failed"
    if (held := fixture.evidence.get("assign")) is not None:
        state.record_verified(
            31,
            "hmc_assign_dedicated_pcie_slot (verified)",
            operation="pcie.assign_dedicated_slot",
            scenario=_DEDICATED_SCENARIO,
            assertions=[
                Assertion("assign-call-succeeded", held[0]),
                Assertion("profile-lists-slot", held[1]),
            ],
            cleanup=cleanup,
            data=f"drc_index={fixture.drc_index!r}",
        )
    if (held := fixture.evidence.get("io-slots-add")) is not None:
        state.record_verified(
            36,
            "hmc_assign_dedicated_pcie_slot (io-slots)",
            operation="pcie.assign_dedicated_slot",
            scenario=_IO_SLOTS_SCENARIO,
            assertions=[
                Assertion("zero-suffix-add-accepted", held[0]),
                Assertion("added-slot-renders-none-pool", held[1]),
                Assertion("other-slots-stable-on-add", held[2]),
            ],
            cleanup=cleanup,
            data="third slot added through the operation (#912)",
        )
    if (held := fixture.evidence.get("reassign")) is not None:
        state.record_verified(
            33,
            "chsyscfg-io-slots-add",
            operation="command.run",
            scenario=_DEDICATED_SCENARIO,
            assertions=[
                Assertion("add-command-succeeded", held[0]),
                Assertion("profile-lists-slot", held[1]),
            ],
            cleanup=cleanup,
            data=f"drc_index={fixture.drc_index!r}",
        )


async def _exercise_dedicated_steps(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> None:
    """Run ST30–ST33; the caller owns cleanup on every exit."""
    if not await create_dedicated_fixture(client, state, fixture):
        return

    assign_ok = await assign_dedicated_slot(client, state, fixture)
    verify_ok = (
        await verify_dedicated_assigned(client, state, fixture) if assign_ok else False
    )
    if not assign_ok:
        state.skip(
            32,
            "dedicated post-assign verify",
            f"skipping verify: assign_ok={assign_ok}",
        )

    if assign_ok and verify_ok:
        unassign_ok = await unassign_dedicated_slot(client, state, fixture)
    else:
        state.skip(
            33,
            "chsyscfg io_slots- (unassign)",
            f"skipping unassign: assign_ok={assign_ok} verify_ok={verify_ok}",
        )
        unassign_ok = False

    if unassign_ok:
        if await reassign_dedicated_slot(client, state, fixture):
            await _io_slots_scenario(client, state, fixture)
    else:
        state.skip(
            33,
            "chsyscfg io_slots+ (reassign)",
            f"skipping reassign: unassign_ok={unassign_ok}",
        )


# ---------------------------------------------------------------------------
# io_slots scenario — ST36, inside the dedicated arm (#912)
# ---------------------------------------------------------------------------


def _io_slot_elements(io_slots: str) -> list[str]:
    """The admitted value's `drc/pool/is_required` elements, in their read order."""
    return [] if io_slots == "none" else io_slots.split(",")


def _listed_drcs(io_slots: str) -> list[str]:
    return [element.split("/", 1)[0] for element in _io_slot_elements(io_slots)]


async def _spare_slots(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> list[str] | None:
    """Unowned slots no profile lists, other than the arm's — the ST29 guards (#916).

    Read fresh rather than reused from ST29, whose selection is minutes old by
    now. None when either read fails, which rules every slot out.
    """
    arm = fixture.config
    st, data = await state.call(
        client, "hmc_list_dedicated_pcie_slots", system_name_or_uuid=arm.system_name
    )
    if st != "PASS" or not isinstance(data, dict):
        return None
    st, table = await state.call(
        client, "hmc_run_command", cmd=profile_io_slot_rows_command(arm.system_name)
    )
    if st != "PASS" or not isinstance(table, str):
        return None
    try:
        profile_rows = parse_profile_io_slot_rows(table)
    except HMCCLIError:
        return None
    return [
        str(row.get("drc_index"))
        for row in data.get("items") or []
        if isinstance(row, dict)
        and _slot_unowned(row)
        and row.get("drc_index") != fixture.drc_index
        and not _profile_lists_slot(profile_rows, str(row.get("drc_index")))
    ]


async def _io_slots_scenario(
    client: Client, state: RunState, fixture: _DedicatedFixture
) -> None:
    """Answer #912 on the fixture: `//0` add and remove, rendering, stability, `none`.

    Runs after the reassign, with the profile at exactly the arm's one slot A.
    Adds B with `is_required=1`, adds and removes C through the operations,
    removes B with `//0`, then A. A restore removes whatever of B and C is still
    listed; the dedicated cleanup handles A as it always does.
    """
    print("\n=== ST36: io_slots grammar (issue #912) ===")
    slot_a = str(fixture.drc_index)
    if fixture.config.drc_index is not None:
        state.skip(
            36,
            "io_slots scenario",
            "LIVE_TEST_DEDICATED_PCIE_DRC_INDEX pins the arm to one slot and the "
            "scenario needs two more; a pinned run mutates only the slot it names — "
            "SKIP io_slots scenario",
        )
        return
    if (
        fixture.baseline_io_slots != "none"
        or fixture.applied_io_slots != f"{slot_a}/none/0"
    ):
        state.skip(
            36,
            "io_slots scenario",
            f"the fixture profile is not the empty baseline plus {slot_a}/none/0 "
            f"(baseline={fixture.baseline_io_slots!r} applied="
            f"{fixture.applied_io_slots!r}) — SKIP io_slots scenario",
        )
        return
    spares = await _spare_slots(client, state, fixture)
    if spares is None or len(spares) < 2:
        state.skip(
            36,
            "io_slots scenario",
            "fewer than two further slots are unowned and listed by no profile "
            f"(found {spares!r}) — SKIP io_slots scenario",
        )
        return
    slot_b, slot_c = spares[:2]
    try:
        await _io_slots_steps(client, state, fixture, slot_b, slot_c)
    finally:
        await _restore_io_slots(
            client, state, fixture, ((slot_c, False), (slot_b, True))
        )


async def _io_slots_steps(
    client: Client,
    state: RunState,
    fixture: _DedicatedFixture,
    slot_b: str,
    slot_c: str,
) -> None:
    """The five #912 steps; any failed step returns and leaves the rest to the restore."""
    arm = fixture.config
    slot_a = str(fixture.drc_index)
    st, data = await state.call(
        client,
        "hmc_run_command",
        cmd=_change_io_slots_command(
            fixture, add=True, drc_index=slot_b, required=True
        ),
    )
    populated = await _read_profile_io_slots(client, state, fixture)
    setup_ok = (
        st == "PASS"
        and populated is not None
        and sorted(_io_slot_elements(populated))
        == sorted([f"{slot_a}/none/0", f"{slot_b}/none/1"])
    )
    state.record(
        36,
        "io_slots setup (is_required=1 element)",
        "PASS" if setup_ok else "FAIL",
        data if st != "PASS" else f"io_slots={populated!r}",
    )
    if not setup_ok or populated is None:
        return

    # A lost response has still written, so each readback decides.
    st, data = await state.call(
        client,
        "hmc_assign_dedicated_pcie_slot",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.lpar_name,
        profile_name=arm.profile_name,
        drc_index=slot_c,
    )
    state.record(36, "hmc_assign_dedicated_pcie_slot (io-slots call)", st, data)
    added = await _read_profile_io_slots(client, state, fixture)
    elements = _io_slot_elements(added) if added is not None else []
    rendered_c = [element for element in elements if element.split("/", 1)[0] == slot_c]
    held = (
        st == "PASS" and bool(rendered_c),
        rendered_c == [f"{slot_c}/none/0"],
        added is not None
        and [e for e in elements if e not in rendered_c]
        == _io_slot_elements(populated),
    )
    fixture.evidence["io-slots-add"] = held
    if not all(held):
        return

    st, data = await state.call(
        client,
        "hmc_unassign_dedicated_pcie_slot",
        system_name_or_uuid=arm.system_name,
        lpar_name_or_uuid=fixture.lpar_name,
        profile_name=arm.profile_name,
        drc_index=slot_c,
    )
    removed = await _read_profile_io_slots(client, state, fixture)
    c_removed = (
        st == "PASS" and removed is not None and slot_c not in _listed_drcs(removed)
    )
    state.record_verified(
        36,
        "hmc_unassign_dedicated_pcie_slot (io-slots)",
        operation="pcie.unassign_dedicated_slot",
        scenario=_IO_SLOTS_SCENARIO,
        assertions=[
            Assertion("zero-suffix-remove-accepted", c_removed),
            Assertion("other-slots-stable-on-remove", removed == populated),
        ],
        cleanup="not-required",
        data=data if st != "PASS" else f"io_slots={removed!r} before add={populated!r}",
    )
    if not c_removed or removed != populated:
        return

    st, data = await state.call(
        client,
        "hmc_run_command",
        cmd=_change_io_slots_command(fixture, add=False, drc_index=slot_b),
    )
    remaining = await _read_profile_io_slots(client, state, fixture)
    b_removed = (
        st == "PASS" and remaining is not None and slot_b not in _listed_drcs(remaining)
    )
    state.record_verified(
        36,
        "chsyscfg-io-slots-remove-required",
        operation="command.run",
        scenario=_IO_SLOTS_SCENARIO,
        assertions=[
            Assertion("required-slot-removed-by-zero-suffix", b_removed),
            Assertion("remaining-slot-stable", remaining == f"{slot_a}/none/0"),
        ],
        cleanup="not-required",
        data=data if st != "PASS" else f"io_slots={remaining!r}",
    )
    if not b_removed or remaining != f"{slot_a}/none/0":
        return

    st, data = await state.call(
        client, "hmc_run_command", cmd=_change_io_slots_command(fixture, add=False)
    )
    emptied = await _read_profile_io_slots(client, state, fixture)
    state.record_verified(
        36,
        "chsyscfg-io-slots-remove-last",
        operation="command.run",
        scenario=_IO_SLOTS_SCENARIO,
        assertions=[
            Assertion("remove-command-succeeded", st == "PASS"),
            Assertion("empty-profile-reads-none", emptied == "none"),
        ],
        cleanup="not-required",
        data=data if st != "PASS" else f"io_slots={emptied!r}",
    )


async def _restore_io_slots(
    client: Client,
    state: RunState,
    fixture: _DedicatedFixture,
    written: tuple[tuple[str, bool], ...],
) -> None:
    """Remove each scenario slot still listed, with the suffix its readback renders.

    Not `//0` throughout: whether `//0` removes an `is_required=1` element is
    the question step 4 asks, so the restore must not depend on its answer. An
    unreadable profile proves nothing, so every slot is removed with the suffix
    it was written with; removing an absent one only fails.
    """
    current = await _read_profile_io_slots(client, state, fixture)
    rendered = (
        {element.split("/", 1)[0]: element for element in _io_slot_elements(current)}
        if current is not None
        else None
    )
    for drc_index, required in written:
        if rendered is not None:
            if drc_index not in rendered:
                continue
            required = rendered[drc_index].endswith("/1")
        st, data = await state.call(
            client,
            "hmc_run_command",
            cmd=_change_io_slots_command(
                fixture, add=False, drc_index=drc_index, required=required
            ),
        )
        state.record(36, "io_slots restore (io_slots-)", st, data)
