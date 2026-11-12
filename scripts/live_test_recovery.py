"""Confirm from outside a run that it left nothing behind.

Arm cleanup guards refuse to mutate on a mismatch and emit a manual-recovery
row, which is correct. But the only witness that recovery happened is the same
run that failed to complete it. This script is the outside witness.

Usage:
    uv run --no-sync python scripts/live_test_recovery.py --results PATH

It witnesses the dedicated PCIe and bare-cec arms (subtasks 24-25) by their run
marker, the users arm (subtask 11) by any HMC user still carrying its reserved
`hmcpctl-live-` prefix, and the vMedia arm (subtasks 16-22) by what it can leave on the run's
configured test partition: a running partition, a changed pending boot string,
an optical mapping, a VIOS vSCSI server adapter with no mapping, a medium it
created, and the media repository it created. It witnesses the vios-backup arm (subtask 37) by the backup
and disk mapping that run recorded: a backup still in the catalog (remedy:
`rmviosbk`) and a disk mapping not put back (remedy: `mkvdev`). It witnesses
the network arm (subtask 9) by the baselines that run recorded: a network left on
its test VLAN, a client adapter on the test partition off its baseline, an FC-port
label off its original, and a vFC group label it named. Subtask 2 only reads, so
there is nothing for it to leave. It witnesses the lpar-config arm (subtask 39) by
any partition still carrying its reserved `hmcpctl-live-lpar-` prefix, and the
lpar-power arm (subtask 41) by a `hmcpctl-live-pwr-` partition, a VIOS mapping backed
by an `lppwr` volume, an `lppwr` volume left in the configured volume group, or a VIOS
vSCSI adapter serving a `hmcpctl-live-pwr-` partition. The lpar-reconfigure arm
(subtask 70) is witnessed the same way for its `lprcf` volume and the
`hmcpctl-live-lpar-` partition it served, beside lpar-config's prefix check. Complete round2 runs carry
an ST0 snapshot: recovery independently recaptures system and VIOS tables and
reads the original partition's description/profile compatibility. Missing witness
data or scope mismatches fail closed. Every other
subtask the run dispatched is listed as NOT WITNESSED, to be checked by hand
(docs/live-testing.md, step 4).

Exit 0 means every dispatched subtask was witnessed and nothing is stranded,
or a complete unarmed ST58 record was classified SKIPPED with no fixture work.
That SKIPPED disposition is not independent baseline or live verification.
Exit 1 means something is, and the output names it with the command that
clears it. Exit 2 means some state could not be read, or the run dispatched a
subtask this script does not witness, which is not the same as clean. A run
that an exception or interrupt stopped (`run.partial` present and not `false`)
also exits 2 whatever the checks found, under a PARTIAL header: the call in
flight at the stop may have no row, so complete execution is not attested.

**This never remediates.** It issues no mutating call: a remediator acting on
a partial read strands exactly what the arm's cleanup guards exist to refuse.
Every command it prints is for a human to run and check.

Its inputs come from the run's own results document: `run.subtasks`, the
`config` the run used, and its `artifacts` and result rows. The PCIe marker is
per-run random (`pcie-<8 hex>`), so a run's traces cannot be identified without
them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import config as live_config
from live_test import runner
from live_test.arms import (
    creation_memory,
    creation_memory_layout,
    logical_workflow,
    lpar_affinity_group,
    lpar_config,
    lpar_hardware_memory,
    lpar_linux_roles,
    lpar_physical_io,
    lpar_power,
    lpar_reconfigure,
    lpar_security_policy,
    lpar_vtpm,
    memory_quantities,
    network,
    processor_quantities,
    processor_sharing_weight,
    provision_boot,
    storage_lifecycle,
    storage_volume_group,
    vios_backup,
)
from live_test.recovery import procedures, reporting
from live_test.recovery.backup import (
    check_vios_backup,
    vios_backup_inputs_from_document,
)
from live_test.recovery.baseline import (
    check_profile_artifacts,
    check_round2,
    round2_inputs_from_document,
)
from live_test.recovery.common import (
    VOLUME_LISTING,
    Finding,
    StateUnreadable,
)
from live_test.recovery.memory import (
    check_creation_memory,
    check_creation_memory_layout,
    creation_memory_inputs_from_document,
    creation_memory_layout_inputs_from_document,
    creation_memory_layout_skip_reason,
    creation_memory_skip_reason,
)
from live_test.recovery.network import check_network, network_inputs_from_document
from live_test.recovery.partition import (
    check_lpar_config,
    check_lpar_power,
    check_lpar_reconfigure_storage,
    lpar_config_inputs_from_document,
    lpar_power_inputs_from_document,
    lpar_reconfigure_inputs_from_document,
)
from live_test.recovery.pcie import check, inputs_from_document
from live_test.recovery.storage import (
    MEDIA_SUBTASKS,
    check_provision_boot_media,
    check_test_partition,
    lpar_inputs_from_document,
    provision_boot_media_inputs_from_document,
)
from live_test.recovery.users import USERS_SUBTASK, check_users, users_witnessed

#: Every tool this script may call. Enforced on the call path rather than left
#: to review: the whole point of the script is that it cannot make things worse
#: while checking whether they are bad.
_READ_ONLY_TOOLS = frozenset(
    {
        "hmc_list_dedicated_pcie_slots",
        "hmc_list_backup_lpar_profiles",
        "hmc_get_lpar_description",
        "hmc_get_lpar_proc_compat",
        "hmc_get_lpar_state",
        "hmc_run_command",
        "hmc_read_lpar_boot_order",
        "hmc_list_optical_mappings",
        "hmc_list_storage_mappings",
        "hmc_list_volume_groups",
        "hmc_get_media_repository",
        "hmc_get_console_info",
        "hmc_list_users",
        "hmc_list_optical_media",
        "hmc_list_vios_backups",
        "hmc_list_vios",
        "hmc_list_lpars",
        "hmc_get_system",
        "hmc_list_virtual_networks",
        "hmc_list_adapters",
        "hmc_list_vios_fc_port_labels",
        "hmc_list_vios_vfc_group_labels",
    }
)

#: `hmc_run_command` is read-only only for the commands given. `lssyscfg` and
#: `lshwres` list; `chsyscfg` would mutate, and shares the tool.
_READ_ONLY_COMMAND_PREFIXES = ("lssyscfg ", "lshwres ")

#: A command built from a results document must not be able to chain a second
#: one behind an admitted prefix.
_SHELL_METACHARACTERS = frozenset(";|&$`<>()\n")
#: The storage arm (#1348): ST40's volume and mapping are read back by name prefix;
#: ST0 and ST3 only read.
_STORAGE_SUBTASKS = frozenset(
    {0, 3, storage_lifecycle.SUBTASK, storage_volume_group.SUBTASK}
)
_WITNESSED_SUBTASKS = (
    MEDIA_SUBTASKS
    | _STORAGE_SUBTASKS
    | {
        USERS_SUBTASK,
        43,
        24,
        25,
        vios_backup.SUBTASK,
        network.INVENTORY_SUBTASK,
        network.SUBTASK,
        lpar_config.SUBTASK,
        lpar_config.SETTINGS_SUBTASK,
        lpar_config.REPORTING_SUBTASK,
        lpar_physical_io.SUBTASK,
        lpar_vtpm.SUBTASK,
        lpar_reconfigure.SUBTASK,
        memory_quantities.SUBTASK,
        processor_quantities.SUBTASK,
        processor_sharing_weight.SUBTASK,
        lpar_security_policy.SUBTASK,
        lpar_hardware_memory.SUBTASK,
        creation_memory.SUBTASK,
        creation_memory_layout.SUBTASK,
        lpar_affinity_group.SUBTASK,
        lpar_linux_roles.SUBTASK,
        lpar_power.SUBTASK,
        provision_boot.SUBTASK,
        logical_workflow.SUBTASK,
    }
)


class MutatingCallRefused(RuntimeError):
    """Raised when a call would leave the read-only surface."""


def guard_read_only(tool: str, arguments: dict[str, Any]) -> None:
    """Refuse a call that could change the managed system."""
    if tool not in _READ_ONLY_TOOLS:
        raise MutatingCallRefused(f"{tool} is not on the read-only allowlist")
    if tool != "hmc_run_command":
        return
    command = str(arguments.get("cmd", "")).strip()
    if VOLUME_LISTING.fullmatch(command):
        return
    if not command.startswith(_READ_ONLY_COMMAND_PREFIXES):
        raise MutatingCallRefused(
            "hmc_run_command is read-only only for lssyscfg and lshwres; "
            f"refused: {command.split()[0] if command else '(empty)'}"
        )
    if _SHELL_METACHARACTERS & set(command):
        raise MutatingCallRefused("hmc_run_command refused a shell metacharacter")


# ---------------------------------------------------------------------------
# The configured test partition (vMedia arm, and round2's ST14 provision)
# ---------------------------------------------------------------------------


def dispatched_subtasks(document: Any) -> list[int] | None:
    """The subtasks the run dispatched, or `None` when the document does not say."""
    run = document.get("run") if isinstance(document, dict) else None
    subtasks = run.get("subtasks") if isinstance(run, dict) else None
    if not isinstance(subtasks, list) or any(type(n) is not int for n in subtasks):
        return None
    return subtasks


async def check_run(
    call,
    checks: tuple[tuple[Callable[..., Awaitable[list[Finding]]], object], ...],
    *,
    users: bool = False,
) -> list[Finding]:
    """Each arm's checks in turn, each read whatever the others found."""
    findings: list[Finding] = []
    unread: list[str] = []
    if users:
        try:
            findings += await check_users(call)
        except StateUnreadable as unreadable:
            unread.append(str(unreadable))
    for run_checks, inputs in checks:
        try:
            findings += await run_checks(call, inputs)
        except StateUnreadable as unreadable:
            findings += unreadable.findings
            unread.append(str(unreadable))
    if unread:
        raise StateUnreadable("; ".join(unread), findings)
    return findings


def _read_only_caller(client, state: runner.RunState):
    """A call path that refuses anything off the read-only surface."""

    async def call(tool: str, **arguments: Any) -> tuple[str, Any]:
        guard_read_only(tool, arguments)
        return await state.call(client, tool, **arguments)

    return call


async def _run_checks(
    checks: tuple[tuple[Callable[..., Awaitable[list[Finding]]], object], ...],
    *,
    users: bool = False,
) -> list[Finding]:
    state = runner.RunState(config=live_config.LiveTestConfig())
    # The checks need the live run's composition, not bare ``create_mcp``:
    # ``hmc_run_command`` is an opt-in escape hatch that only it registers. Without
    # it the profile-drift check was answered "Unknown tool", which read as "no
    # drift" before ``_profile_drift`` raised, so the script reported a system
    # clean it had never looked at. The 2026-09-21 live run showed review alone had
    # missed that, so a test asserts the read-only tools are registered here.
    async with runner.served_client() as client:
        return await check_run(
            _read_only_caller(client, state),
            checks,
            users=users,
        )


def _recovery_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=Path("test-results-dedicated.json"),
        help="the run's results document (default: %(default)s)",
    )
    source_packets = parser.add_mutually_exclusive_group()
    source_packets.add_argument(
        "--paging-switchover",
        action="store_true",
        help="ST66 bounded read-only original-baseline recovery",
    )
    source_packets.add_argument("--source-packet", type=Path)
    source_packets.add_argument("--activation-source-packet", type=Path)
    source_packets.add_argument("--serial-procedure", action="store_true")
    source_packets.add_argument("--boot-start-procedure", action="store_true")
    source_packets.add_argument("--ethernet-update-procedure", action="store_true")
    source_packets.add_argument(
        "--memory-statistics",
        action="store_true",
        help="ST67 bounded raw-record recovery; never restores counter history",
    )
    source_packets.add_argument("--vios-ame-source-packet", type=Path)
    source_packets.add_argument("--vios-boot-source-packet", type=Path)
    source_packets.add_argument("--vnic-creation-policy-packet", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--packet-sha256")
    return parser


def _recover_generic_document(document: dict[str, Any], results_path: Path) -> int:
    subtasks = dispatched_subtasks(document)
    if subtasks is None:
        print(
            f"ERROR: {results_path} records no run.subtasks, so what the run "
            "changed is unknown; check the system by hand",
            file=sys.stderr,
        )
        return 2
    unread: list[str] = []
    round2_inputs = None
    try:
        round2_inputs = round2_inputs_from_document(document, subtasks)
    except (ValueError, TypeError, KeyError) as error:
        if message := str(error):
            unread.append(message)
    memory_inputs = None
    try:
        skip_reason = creation_memory_skip_reason(document, subtasks)
        if skip_reason:
            print(f"SKIPPED  ST65 no fixture work: {skip_reason}")
            print(
                "Saved unarmed result only; not baseline, configured-value or runtime proof."
            )
            return 0
        memory_inputs = creation_memory_inputs_from_document(document, subtasks)
    except (ValueError, TypeError, KeyError) as error:
        if message := str(error):
            unread.append(message)
    creation_inputs = None
    try:
        skip_reason = creation_memory_layout_skip_reason(document, subtasks)
        if skip_reason:
            print(f"SKIPPED  ST58 no fixture work: {skip_reason}")
            print(
                "Saved unarmed result only; not baseline, configured-value or runtime proof."
            )
            return 0
        creation_inputs = creation_memory_layout_inputs_from_document(
            document, subtasks
        )
    except (ValueError, TypeError, KeyError) as error:
        if message := str(error):
            unread.append(message)
    witnessed = _WITNESSED_SUBTASKS | (
        set(runner.SUBTASK_GROUPS["round2"]) if round2_inputs else set()
    )
    unwitnessed = sorted(set(subtasks) - witnessed)
    pcie = inputs_from_document(document)
    partition = lpar_inputs_from_document(document, subtasks)
    vios = vios_backup_inputs_from_document(document, subtasks)
    network_inputs = network_inputs_from_document(document, subtasks)
    try:
        lpar_config_inputs = lpar_config_inputs_from_document(document, subtasks)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    lpar_power_inputs = lpar_power_inputs_from_document(document, subtasks)
    boot_media = provision_boot_media_inputs_from_document(document, subtasks)
    reconfigure_inputs = lpar_reconfigure_inputs_from_document(document, subtasks)

    artifact_system = None
    if 43 in subtasks:
        artifact_config = document.get("config")
        artifact_system = ""
        if isinstance(artifact_config, dict):
            artifact_system = artifact_config.get("system_name") or ""
    checks = tuple(
        (checker, inputs)
        for checker, inputs in (
            (check, pcie),
            (check_test_partition, partition),
            (check_vios_backup, vios),
            (check_network, network_inputs),
            (check_lpar_config, lpar_config_inputs),
            (check_lpar_power, lpar_power_inputs),
            (check_provision_boot_media, boot_media),
            (check_lpar_reconfigure_storage, reconfigure_inputs),
            (check_creation_memory, memory_inputs),
            (check_creation_memory_layout, creation_inputs),
            (check_round2, round2_inputs),
            (check_profile_artifacts, artifact_system),
        )
        if inputs is not None
    )

    findings: list[Finding] = []
    try:
        users = users_witnessed(document, subtasks)
    except ValueError as error:
        users = False
        unread.append(str(error))
    if users or checks:
        if not runner._bootstrap_config():
            print("ERROR: no HMC credentials; cannot check the system", file=sys.stderr)
            return 2
        try:
            findings = asyncio.run(_run_checks(checks, users=users))
        except MutatingCallRefused as refused:
            print(f"ERROR: refused a mutating call: {refused}", file=sys.stderr)
            return 2
        except StateUnreadable as unreadable:
            findings = unreadable.findings
            unread.append(str(unreadable))
        except Exception as error:  # noqa: BLE001 - an unreadable system is not a clean one
            print(f"ERROR: could not read the managed system: {error}", file=sys.stderr)
            return 2

    reporting.report(document, findings, unwitnessed, unread)
    if unread or unwitnessed or reporting.is_partial(document):
        print("The system was NOT confirmed clean.", file=sys.stderr)
        return 2
    return 1 if findings else 0


def main(argv: list[str] | None = None) -> int:
    parser = _recovery_parser()
    args = parser.parse_args(argv)
    for delegate in (
        procedures.delegate_procedure_recovery,
        procedures.delegate_packet_recovery,
    ):
        result = delegate(args, parser)
        if result is not None:
            return result
    try:
        document = json.loads(args.results.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"ERROR: cannot read {args.results}: {error}", file=sys.stderr)
        return 2
    if document.get("group") == "client-ethernet-update":
        parser.error(
            "ST69 requires --ethernet-update-procedure and --output; generic recovery cannot handle it"
        )
    result = procedures.recover_document_group(document)
    return (
        result
        if result is not None
        else _recover_generic_document(document, args.results)
    )


if __name__ == "__main__":
    raise SystemExit(main())
