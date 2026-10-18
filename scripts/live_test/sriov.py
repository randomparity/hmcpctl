"""SR-IOV live scenarios on the configured existing partition (subtask 23).

Exercise reversible logical-port assignment, readback and guarded cleanup.
The configured partition must be Not Activated; missing hardware or a wrong
state produces SKIP. Cleanup failure retains manual-recovery evidence.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastmcp import Client

# Imported rather than restated so the arm's SKIP envelope cannot drift from the one
# the product's admission gates enforce (ADR 0166 decision 3); a copied predicate or
# literal would go stale silently the first time the admitted release moves.
from .observation import (
    Assertion,
)

if TYPE_CHECKING:
    from live_test_runner import RunState


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
