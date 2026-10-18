"""Answer whether a live run would start, and what it would touch.

The runner already validates configuration and credentials before its first
dispatch. What it cannot do is answer the question without being the run. This
script asks the same validators the runner gates on, so there is one definition
of a valid configuration, and adds the facts the runner never establishes
before dispatch: what a selected arm will create, mutate and delete, whether the
managed system is inside the ADR 0053 admitted envelope, and whether round2's
provision VLAN has a virtual network.

Usage:
    uv run --no-sync python scripts/live_test_preflight.py [--group NAME]
    uv run --no-sync python scripts/live_test_preflight.py --skip-hardware

Exit 0 means the runner would start. Non-zero means it would not, for a reason
named in the output.

**Configuration is blocking; hardware is advisory.** An unreachable HMC or an
out-of-envelope managed system is reported as a predicted SKIP and does not
change the exit status: the arm already SKIPs correctly on both, and a blocking
check here would be a second copy of an admission rule ADR 0053 moves. A
provision VLAN with no virtual network is reported the same way: subtask 14
refuses it before deleting anything, and the HMC can gain the network between
preflight and the run.

The per-arm verdict is a prediction. An arm decides for itself at run time and
may SKIP where preflight said RUNNABLE.

No `HMC_*` value is ever printed — credentials report as present or absent.
Scenario settings are printed in full: naming the managed system a run will
create and delete partitions on is the point of the verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import (
    bare_cec,
    dedicated_pcie,
    lpar_config,
    lpar_power,
    runner,
    storage_lifecycle,
    storage_volume_group,
    users,
    vios_backup,
    vmedia,
)
from live_test import config as live_config

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig, env_var_value
from hmcpctl.operations.lpar.provision import check_vlan_exists
from hmcpctl.operations.virtualization.pcie import (
    require_admitted_environment,
)
from hmcpctl.resource_identity import resolve_system_uuid

#: The credential names the runner needs resolved before it dispatches. Reported
#: as present or absent and never by value: this output is meant to be pasted
#: into an issue when a run will not start.
_CREDENTIAL_KEYS = ("HMC_HOST", "HMC_USER", "HMC_PASSWORD")


@dataclass(frozen=True)
class ArmVerdict:
    """One arm's predicted runnability and the hardware it would mutate."""

    group: str
    runnable: bool
    reason: str
    mutates: tuple[str, ...] = ()
    #: The managed system whose envelope decides this arm, when it has one.
    system_name: str | None = None


#: Why either arm built on the dedicated configuration is predicted to SKIP.
_DEDICATED_CONFIG_REQUIRED = (
    "LIVE_TEST_DEDICATED_PCIE_SYSTEM_NAME and _LPAR_PREFIX must both be "
    "set, and no value may carry an HMC record delimiter"
)


def _dedicated_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Predict the dedicated PCIe arm from the predicate the arm itself uses.

    `dedicated_pcie._dedicated_config` is called rather than restated: it applies the
    delimiter rejection and the no-fallback rule that decide whether the arm
    runs at all, and a second copy here would drift from the one that governs.
    """
    resolved = dedicated_pcie._dedicated_config(config)
    if resolved is None:
        return ArmVerdict(
            "dedicated",
            False,
            _DEDICATED_CONFIG_REQUIRED,
        )
    return ArmVerdict(
        "dedicated",
        True,
        "configured",
        (
            f"managed system {resolved.system_name}",
            f"partitions named {resolved.lpar_prefix}* (created, then deleted)",
            f"profile {resolved.profile_name} io_slots (assigned, then restored)",
            f"dedicated slot {resolved.drc_index or dedicated_pcie.AUTO_SELECTED_SLOT}",
            _io_slots_scenario_line(resolved),
        ),
        resolved.system_name,
    )


def _io_slots_scenario_line(resolved: dedicated_pcie._DedicatedConfig) -> str:
    """Predict `dedicated_pcie._io_slots_scenario`'s (#985) effect on the dedicated arm.

    Mirrors the scenario's own gate (`fixture.config.drc_index is not None`):
    a pinned slot leaves no room for the two further slots the scenario needs,
    so it SKIPs; otherwise it takes two more unowned, profile-unlisted slots
    beyond the fixture's own.
    """
    if resolved.drc_index is not None:
        return (
            "io_slots scenario: SKIP (LIVE_TEST_DEDICATED_PCIE_DRC_INDEX pins one slot; "
            "the scenario needs two more)"
        )
    return f"io_slots scenario: two further spare slots {dedicated_pcie.AUTO_SELECTED_SLOT}"


def _bare_cec_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Predict the bare-cec arm from the predicates the arm itself admits on.

    It shares the dedicated arm's configuration, and adds its own two refusals:
    power operations must be ownership-guarded, and the platform-dump opt-in
    must be a value the arm recognises. Both are the arm's functions, called
    here after `_check_credentials` has loaded `.env`, as the runner has by then.
    """
    resolved = dedicated_pcie._dedicated_config(config)
    accept_dump = bare_cec._dump_opt_in(config)
    refusals = [
        reason
        for failed, reason in (
            (resolved is None, _DEDICATED_CONFIG_REQUIRED),
            (
                not bare_cec._power_operations_authorized(),
                "HMC_AUTHORIZE_POWER_OPERATIONS must be true",
            ),
            (
                accept_dump is None,
                "LIVE_TEST_ACCEPT_PLATFORM_DUMP must be true, false or unset",
            ),
        )
        if failed
    ]
    if refusals or resolved is None or accept_dump is None:
        return ArmVerdict("bare-cec", False, "; ".join(refusals))
    return ArmVerdict(
        "bare-cec",
        True,
        "configured",
        (
            f"managed system {resolved.system_name}",
            (
                f"partitions named {resolved.lpar_prefix}* (created, activated to "
                "SMS, powered off and restarted, then deleted)"
            ),
            f"profile {resolved.profile_name} io_slots (assigned, then restored)",
            f"dedicated slot {resolved.drc_index or dedicated_pcie.AUTO_SELECTED_SLOT}",
            "platform dump: "
            + ("taken (dumprestart opted in)" if accept_dump else "not taken"),
        ),
        resolved.system_name,
    )


def _profiles_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Name what the profiles arm (#627) changes, each restored by the arm itself."""
    return ArmVerdict(
        "profiles",
        True,
        "configuration validated",
        (
            f"managed system {config.system_name}",
            f"partition {config.lp3_name}: description (restored)",
            (
                f"partition {config.lp3_name}: sync_curr_profile (restored; only when "
                "ST0 reads it as 0, 1 or 2 and the partition as Not Activated)"
            ),
            (
                f"partition {config.lp3_name}: default-profile processor compatibility "
                "mode (restored; skipped when ST0 reads sync_curr_profile as 1)"
            ),
            "the first VIOS by name: msp (toggled, then restored)",
            (
                "every profile on the system: backed up to hmcpctl-live-st10, then "
                "type-3 merge-restored"
            ),
            "not-activated partitions the restore unconfigures: profile re-applied",
        ),
        config.system_name,
    )


def _vios_backup_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Name what the vios-backup arm (#1349) changes and removes again."""
    if HMCConfig().ssh_timeout < vios_backup.MIN_SSH_TIMEOUT:
        return ArmVerdict(
            "vios-backup",
            False,
            f"HMC_SSH_TIMEOUT must be at least {vios_backup.MIN_SSH_TIMEOUT}: the "
            "restore restarts the VIOS inside one rstviosbk call",
        )
    return ArmVerdict(
        "vios-backup",
        True,
        "configuration validated",
        (
            f"managed system {config.system_name}",
            (
                f"the VIOS serving {config.lp3_name}: I/O configuration backed up to "
                f"{vios_backup.BACKUP_PREFIX}<8 hex>"
            ),
            f"that VIOS: {config.lp3_name}'s disk VTD removed (the backing device kept)",
            (
                "that VIOS: viosioconfig restore with -r (the HMC may restart the "
                "VIOS); requires active RMC and an HMC console adapter first"
            ),
            (
                "that backup: removed with rmviosbk only when a final read equals "
                "the baseline; otherwise kept for the operator"
            ),
        ),
        config.system_name,
    )


def _pcm_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Name the system-wide PCM preferences the pcm arm (#634) toggles and restores."""
    return ArmVerdict(
        "pcm",
        True,
        "configuration validated",
        (
            f"managed system {config.system_name}",
            (
                "PCM collection preferences (system-wide): each of the five flags "
                "toggled, then all five restored to the pre-run read"
            ),
        ),
        config.system_name,
    )


def _lpar_config_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Name the one partition the lpar-config arm (#1345) creates and deletes again."""
    arm = dedicated_pcie._dedicated_config(config)
    dedicated = (
        f"scratch profile {arm.profile_name}: dedicated slot {arm.drc_index or dedicated_pcie.AUTO_SELECTED_SLOT}; "
        "requires unowned/no-profile baseline; assign through hmc_modify_lpar and restore "
        "before SMS, then compare all profiles and slot ownership"
        if arm is not None and arm.system_name == config.system_name
        else "dedicated assignment unavailable: requires dedicated settings for this same system"
    )
    return ArmVerdict(
        "lpar-config",
        True,
        "configuration validated",
        (
            f"managed system {config.system_name}",
            dedicated,
            (
                f"one partition {lpar_config.NAME_PREFIX}<8 hex> (created; memory, "
                "processors, name and pending boot order changed; activated to SMS "
                "and powered off; then deleted)"
            ),
            (
                "no other partition: the system's partition names and free "
                "processing units and memory are compared with the pre-run read"
            ),
        ),
        config.system_name,
    )


def _lpar_power_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Name the partitions and the VIOS volume the lpar-power arm (#1346) creates."""
    return ArmVerdict(
        "lpar-power",
        True,
        "configuration validated",
        (
            f"managed system {config.system_name}",
            (
                f"partitions {lpar_power.NAME_PREFIX}<8 hex>[-x|-p] (created; "
                "activated to SMS and Open Firmware, powered off and restarted, "
                "including through hmc_power_lpar; then deleted or decommissioned)"
            ),
            (
                f"one {lpar_power.VOLUME_MIB} MiB volume {lpar_power.VOLUME_PREFIX}"
                f"<8 hex> in volume group {config.vdisk_volume_group_name} on the "
                "VIOS holding it, mapped to the provisioned partition, detached and "
                "deleted"
            ),
            (
                "a dedicated PCIe slot only when the dedicated arm is configured "
                "for this system and the slot is unowned and listed by no profile"
            ),
            (
                "no other partition, volume or mapping: partition names and states, "
                "free processing units and memory, slot owners, VIOS mappings and "
                "server adapters are compared with the pre-run read"
            ),
        ),
        config.system_name,
    )


def _network_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Name what the network arm (#629) changes, each reversed by the arm itself."""
    return ArmVerdict(
        "network",
        True,
        "configuration validated",
        (
            f"managed system {config.system_name}",
            (
                "one virtual network on the first unused VLAN in "
                f"{config.vlan_range_start}-{config.vlan_range_end} (created, a "
                "duplicate attempted, then deleted); no existing network is changed"
            ),
            (
                f"partition {config.lp3_name}, only while Not Activated: a client "
                "network, a vSCSI and a vFC client adapter (each added, then removed; "
                "each vFC add uses a WWPN pair from the system's pool); each vSCSI "
                "and vFC add is first tried on the slot the partition's own vSCSI "
                "client uses, and an unknown adapter UUID is deleted (both expected "
                "refused; anything accepted is reversed or reported)"
            ),
            (
                f"the one VIOS serving {config.lp3_name}: one FC port's label (set, "
                "removed, restored; a set on absent port fcs9999 expected refused) "
                "and a vFC group label (created, a duplicate refused, renamed, "
                "removed)"
            ),
        ),
        config.system_name,
    )


def _users_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Name the HMC user the users arm (#632) creates and deletes again."""
    return ArmVerdict(
        "users",
        True,
        "configuration validated",
        (
            (
                f"the HMC: one user {users.SCRATCH_PREFIX}<8 hex> with the "
                f"{users.VIEWER_TASK_ROLE} task role, web and SSH remote access "
                "disabled (created, modified, then deleted by UUID)"
            ),
            "existing users, roles and LDAP/Kerberos settings: read only",
        ),
    )


def _vmedia_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Name what the vmedia arm (#1347) changes, each removed by the arm itself."""
    return ArmVerdict(
        "vmedia",
        True,
        "configuration validated",
        (
            f"managed system {config.system_name}",
            (
                "the VIOS's media repository: created, and deleted again, only when "
                "the VIOS has none; an existing one is never resized or deleted"
            ),
            (
                f"in that repository, one blank 1 GiB medium {vmedia.BLANK_PREFIX}"
                f"<8 hex>: created, mounted to {config.lp3_name} only while Not "
                "Activated (the HMC adds a vSCSI adapter pair), a delete while "
                "mounted expected refused, unmounted, deleted"
            ),
            (
                f"the ISO at {config.iso_path}, only when that file exists: uploaded "
                "under a per-run name and deleted; then mounted for a boot test that "
                f"powers {config.lp3_name} on and off and restores its pending boot "
                "order"
            ),
            "no other medium or mapping is unmounted or deleted",
        ),
        config.system_name,
    )


def _storage_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    """Name what the storage arm (#1348) changes, each removed by the arm itself."""
    volume = f"{storage_lifecycle.DISK_PREFIX}<8 hex>"
    return ArmVerdict(
        "storage",
        True,
        "configuration validated",
        (
            f"managed system {config.system_name}",
            (
                f"in volume group {config.vdisk_volume_group_name}, one 1 GiB logical "
                f"volume {volume} at a time, created twice: once mapped to "
                f"{config.lp3_name} only while Not Activated through a new virtual "
                "target device the HMC names (vtscsi<n>, with a new vSCSI adapter "
                "pair), a delete while mapped expected refused, detached and deleted; "
                "then created and mapped the same way by attach-disk, detached and "
                "deleted"
            ),
            (
                f"ST42 creates scratch group {config.scratch_vg_name} on explicit physical "
                f"volume {config.scratch_pv_name}, only after free/no-VG identity guards; "
                "cleanup uses empty-single-PV reducevg and exact snapshot restoration"
                if storage_volume_group.configured_scratch(
                    config.scratch_pv_name, config.scratch_vg_name
                )
                else "ST42 SKIPs: both valid LIVE_TEST_SCRATCH_PV_NAME and LIVE_TEST_SCRATCH_VG_NAME required"
            ),
        ),
        config.system_name,
    )


def _profile_artifacts_verdict(config: live_config.LiveTestConfig) -> ArmVerdict:
    return ArmVerdict(
        "profile-artifacts",
        True,
        "requires HMC 10.3.1061.0+ and a complete clean inventory before creation",
        (
            f"managed system {config.system_name}: one unpredictable hmcpctl-live-prof- backup created without overwrite, listed and deleted; no restore or partition mutation",
        ),
        config.system_name,
    )


_ARM_VERDICTS = {
    "dedicated": _dedicated_verdict,
    "bare-cec": _bare_cec_verdict,
    "profiles": _profiles_verdict,
    "profile-artifacts": _profile_artifacts_verdict,
    "pcm": _pcm_verdict,
    "vios-backup": _vios_backup_verdict,
    "network": _network_verdict,
    "users": _users_verdict,
    "lpar-config": _lpar_config_verdict,
    "lpar-power": _lpar_power_verdict,
    "vmedia": _vmedia_verdict,
    "storage": _storage_verdict,
}


def _generic_verdict(group: str, config: live_config.LiveTestConfig) -> ArmVerdict:
    """The arms whose settings `LiveTestConfig` supplies with defaults.

    These have no arm-specific admission predicate to import: `from_env_file`
    has already validated every key they read, so reaching here means they are
    configured. What they mutate is not enumerated, because the runner resolves
    it against the HMC at dispatch rather than from configuration.
    """
    return ArmVerdict(
        group,
        True,
        "configuration validated",
        (f"managed system {config.system_name}",),
        config.system_name,
    )


def arm_verdicts(
    config: live_config.LiveTestConfig, group: str | None
) -> tuple[ArmVerdict, ...]:
    """Predict each selected arm. `None` and `"all"` select every arm."""
    selected = tuple(runner.SUBTASK_GROUPS) if group in (None, "all") else (group,)
    return tuple(
        _ARM_VERDICTS[name](config)
        if name in _ARM_VERDICTS
        else _generic_verdict(name, config)
        for name in selected
        if name != "all"
    )


async def _probe_environment(system_name: str) -> str:
    """Report the ADR 0053 envelope for one managed system, as a display string.

    Delegates to `require_admitted_environment`, the predicate the operations
    layer enforces, so preflight cannot admit a system the operation refuses.
    Any failure below that — DNS, TLS, auth, an absent system — is reported as
    unknown rather than raised: hardware findings are advisory here.
    """
    try:
        await require_admitted_environment(HMCConfig(), system_name)
    except Exception as exc:  # noqa: BLE001 - every hardware failure is advisory
        return f"not admitted or unreachable ({type(exc).__name__})"
    return "admitted (ADR 0053 envelope)"


async def _probe_provision_vlan(system_name: str, vlan_id: int) -> str:
    """Report whether ST13/ST14's configured VLAN has a virtual network (#970).

    Asks `check_vlan_exists`, the predicate `hmc_provision_lpar` refuses on, so
    preflight cannot pass a VLAN the provision rejects. Advisory, like the envelope:
    any failure other than the predicate's own refusal is reported as unknown.
    """
    try:
        async with HMCClient(HMCConfig()) as hmc:
            system_uuid = await resolve_system_uuid(hmc, system_name)
            try:
                await check_vlan_exists(hmc, system_uuid, vlan_id)
            except ValueError:
                return (
                    f"no virtual network on VLAN {vlan_id} — ST13 and ST14 will fail; "
                    "set LIVE_TEST_PROVISION_VLAN_ID to a VLAN with a virtual network"
                )
    except Exception as exc:  # noqa: BLE001 - every hardware failure is advisory
        return f"unknown ({type(exc).__name__})"
    return f"{vlan_id} has a virtual network"


def _check_configuration() -> tuple[live_config.LiveTestConfig | None, str]:
    """Run the runner's own `.env` validator and report its verdict."""
    try:
        return live_config.LiveTestConfig.from_env_file(), ""
    except ValueError as exc:
        return None, str(exc)


def _check_credentials() -> tuple[bool, dict[str, bool]]:
    """Resolve credentials the way the runner does, reporting presence only.

    `_bootstrap_config` prints its own diagnostics and, on the TOML path,
    announces the profile it loaded. That output is captured and discarded
    rather than relayed: the caller gets a per-key present/absent map, which
    says what to fix without disclosing what resolved.
    """
    with contextlib.redirect_stdout(io.StringIO()):
        resolved = runner._bootstrap_config()
        # The same two calls in the same order as the runner's own startup
        # gate, and for the reason recorded there. Dropping one would make
        # preflight name a request environment the run will not use.
        runner._load_dotenv()
    return resolved, {key: env_var_value(key) is not None for key in _CREDENTIAL_KEYS}


def _print_arms(
    verdicts: tuple[ArmVerdict, ...],
    envelopes: dict[str, str],
    provision_vlan: str | None,
) -> None:
    print("\npredicted arms — the run decides; a RUNNABLE arm may still SKIP")
    for verdict in verdicts:
        state = "RUNNABLE" if verdict.runnable else "SKIP"
        print(f"  {verdict.group:<10} {state:<8} {verdict.reason}")
        for target in verdict.mutates:
            print(f"    will mutate: {target}")
        envelope = envelopes.get(verdict.system_name or "")
        if envelope is not None:
            print(f"    envelope:    {envelope}")
        if verdict.group == "round2" and provision_vlan is not None:
            print(f"    provision VLAN: {provision_vlan}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--group",
        choices=tuple(runner.SUBTASK_GROUPS),
        help="predict one arm instead of every arm",
    )
    parser.add_argument(
        "--skip-hardware",
        action="store_true",
        help="predict from configuration alone, contacting no HMC",
    )
    parser.add_argument(
        "--detach-probe",
        action="store_true",
        help="predict only the bounded ST41 mapping/RMC investigation",
    )
    args = parser.parse_args(argv)
    if args.detach_probe and args.group != "lpar-power":
        parser.error("--detach-probe requires --group lpar-power")

    config, config_error = _check_configuration()
    credentials_ok, present = _check_credentials()
    verdicts = arm_verdicts(config, args.group) if config is not None else ()
    if args.detach_probe and config is not None:
        verdicts = (
            ArmVerdict(
                "lpar-power",
                True,
                "bounded detach probe selected",
                (
                    f"managed system {config.system_name}",
                    "one ST41-prefix scratch partition, ownership-stamped; attach while Not Activated, activate only owned scratch Open Firmware, then detach; deleted only after safe reads",
                    f"one 1024 MiB ST41-prefix run volume in {config.vdisk_volume_group_name}; one remaining fresh attach/detach cycle, two previous attach attempts consumed, three total ceiling",
                    "other mappings/adapters compared exactly; ambiguity retains the partition/volume for manual recovery; existing test partition unchanged",
                ),
                config.system_name,
            ),
        )

    envelopes: dict[str, str] = {}
    provision_vlan: str | None = None
    if config is not None and credentials_ok and not args.skip_hardware:
        systems = {v.system_name for v in verdicts if v.system_name and v.runnable}
        for system in sorted(systems):
            envelopes[system] = asyncio.run(_probe_environment(system))
        if any(v.group == "round2" for v in verdicts):
            provision_vlan = asyncio.run(
                _probe_provision_vlan(config.system_name, config.provision_vlan_id)
            )

    print("live-test preflight")
    print("=" * 60)
    if config is None:
        print(f"configuration  FAIL  {config_error}")
    else:
        print("configuration  OK    .env validated by LiveTestConfig.from_env_file")
    detail = " ".join(f"{k}={'set' if v else 'MISSING'}" for k, v in present.items())
    print(f"credentials    {'OK  ' if credentials_ok else 'FAIL'}  {detail}")
    # Reported, never gated: the variable is opt-in and a run starts either way
    # (#875). It is on its own row because `MISSING` on the credentials row read
    # as a defect to fix, which is what made the runner demand it.
    presence = "set" if env_var_value("HMC_SCHEMA_VERSION") else "not set"
    print(
        f"schema version INFO  HMC_SCHEMA_VERSION={presence} — optional; recorded "
        "so a run's evidence names its request environment (docs/compatibility.md)"
    )
    if verdicts:
        _print_arms(verdicts, envelopes, provision_vlan)

    if config is None or not credentials_ok:
        print("\nthe runner would not start — fix the FAIL rows above")
        return 1
    print("\nthe runner would start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
