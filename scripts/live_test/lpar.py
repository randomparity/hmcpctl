"""LPAR lifecycle and property scenarios for the live HMC test harness."""

from __future__ import annotations

import csv
import shlex
from typing import TYPE_CHECKING, get_args

from fastmcp import Client

from hmcpctl.operations.lpar.core import ProcessorCompatibilityMode
from hmcpctl.ssh.commands import build_filter, parse_hmc_result_rows
from hmcpctl.ssh.lpar import validate_lpar_description
from hmcpctl.ssh.transport import HMCCLIError

from .inventory import read_sync_state
from .observation import (
    Assertion,
    CallFailure,
    judge_create_result,
    plain_data,
)
from .results import field

if TYPE_CHECKING:
    from live_test_runner import RunState


async def exercise_lpar_lifecycle(client: Client, state: RunState) -> None:
    print("\n=== ST8: LPAR Lifecycle ===")
    status, data = await state.call(
        client,
        "hmc_run_command",
        cmd=f"lssyscfg -r lpar -m {shlex.quote(state.config.system_name)} -F name --header",
    )
    try:
        names = (
            parse_hmc_result_rows(data, ("name",), "scratch admission")
            if status == "PASS" and isinstance(data, str)
            else None
        )
    except HMCCLIError:
        names = None
    if names is None or any(row["name"] == state.config.scratch_name for row in names):
        state.record(
            8,
            "scratch admission",
            "FAIL",
            "scratch name is present or inventory unreadable",
        )
        return
    await _discover_system_uuid(client, state)
    await _create_and_confirm_scratch_lpar(client, state)
    await _modify_and_summarize_scratch_lpar(client, state)
    await _power_off_and_delete_scratch_lpar(client, state)


async def _discover_system_uuid(client: Client, state: RunState) -> None:
    """Discover the managed-system UUID when prior inventory did not provide it."""
    config = state.config
    artifacts = state.artifacts
    if not artifacts.system_uuid:
        status, data = await state.call(
            client, "hmc_get_system", system_name_or_uuid=config.system_name
        )
        if status == "PASS" and isinstance(data, dict):
            artifacts.system_uuid = data.get("UUID") or data.get("uuid")


async def _create_and_confirm_scratch_lpar(client: Client, state: RunState) -> None:
    """Create the scratch LPAR and retain its identity from either response."""
    config = state.config
    artifacts = state.artifacts
    status, data = await state.call(
        client,
        "hmc_create_lpar",
        system_name_or_uuid=config.system_name,
        name=config.scratch_name,
        resources={
            "desired_memory": config.scratch_create_desired_memory_mib,
            "max_memory": config.scratch_create_max_memory_mib,
            "desired_vcpus": config.scratch_create_desired_vcpus,
            "max_vcpus": config.scratch_create_max_vcpus,
            # Explicit units: the SSH fallback's 0.1 default covers one virtual
            # processor only (#938).
            "desired_procs": config.scratch_create_desired_procs,
            "max_procs": config.scratch_create_max_procs,
        },
    )
    record_status, reason = judge_create_result(status, data)
    state.record(8, "hmc_create_lpar", record_status, data, reason)
    data = plain_data(data)
    if status == "PASS" and isinstance(data, dict):
        created = data.get("lpar")
        if isinstance(created, dict):
            artifacts.scratch_uuid = created.get("uuid") or created.get("UUID")

    status, data = await state.call(
        client, "hmc_get_lpar", lpar_name_or_uuid=config.scratch_name
    )
    state.record(8, "hmc_get_lpar (confirm created)", status, data)
    if status == "PASS" and isinstance(data, dict) and not artifacts.scratch_uuid:
        artifacts.scratch_uuid = data.get("uuid") or data.get("UUID")


async def _modify_and_summarize_scratch_lpar(client: Client, state: RunState) -> None:
    """Exercise the REST modification path and read its resulting summary.

    Recorded as is: the lpar-config arm (ST39) is where the modify contract is
    observed, and an HTTP 406 here is a FAIL row, never a declared gap.
    """
    config = state.config
    status, data = await state.call(
        client,
        "hmc_modify_lpar",
        lpar_name_or_uuid=config.scratch_name,
        resources={
            "desired_memory": config.scratch_modify_desired_memory_mib,
            "max_memory": config.scratch_modify_max_memory_mib,
        },
    )
    state.record(8, "hmc_modify_lpar", status, data)

    status, data = await state.call(
        client, "hmc_lpar_summary", lpar_name_or_uuid=config.scratch_name
    )
    state.record(8, "hmc_lpar_summary (post-modify)", status, data)


async def _power_off_and_delete_scratch_lpar(client: Client, state: RunState) -> None:
    """Exercise power operations, then delete and confirm the scratch LPAR is gone."""
    config = state.config
    artifacts = state.artifacts
    status, data = await state.call(
        client, "hmc_power_on_lpar", lpar_name_or_uuid=config.scratch_name, wait=True
    )
    state.record(
        8,
        "hmc_power_on_lpar",
        status,
        data,
        "boot failure expected — no OS installed",
    )
    if status == "PASS" and isinstance(data, dict):
        artifacts.job_uuid_sample = (
            data.get("job_uuid") or data.get("UUID") or artifacts.job_uuid_sample
        )

    status, data = await state.call(
        client,
        "hmc_power_off_lpar",
        lpar_name_or_uuid=config.scratch_name,
        immediate=True,
        wait=True,
    )
    state.record(8, "hmc_power_off_lpar", status, data)
    if status == "PASS" and isinstance(data, dict) and not artifacts.job_uuid_sample:
        artifacts.job_uuid_sample = data.get("job_uuid") or data.get("UUID")

    status, data = await state.call(
        client,
        "hmc_delete_lpar",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.scratch_name,
    )
    state.record(8, "hmc_delete_lpar", status, data)
    if status == "PASS":
        artifacts.scratch_uuid = None

    status, data = await state.call(client, "hmc_list_lpars")
    state.record(8, "hmc_list_lpars (confirm deleted)", status, data)


# ---------------------------------------------------------------------------
# ST10 — LPAR Properties Mutations (SSH/CLI)
# ---------------------------------------------------------------------------


def _unrestorable_description(text: str) -> str | None:
    """Return why *text* cannot be written back, or ``None`` when it can.

    Defers to the server's own validator rather than restating its rules, so a
    baseline description the CLI cannot round-trip (non-ASCII, a control
    character, or a character the HMC's ``-i`` attribute record treats as
    structure — ADR 0045) is refused before the restore call is attempted.
    """
    if not text:
        return None
    try:
        validate_lpar_description(text)
    except ValueError as exc:
        return str(exc)
    return None


def _baseline_description(state: RunState) -> str | None:
    """Return the captured description in the writable string form.

    ``None`` means ST0 never captured a baseline for this key — the read
    failed, or the key is absent from a resumed results file — distinct from
    a baseline that really was empty (#1038).
    """
    if "description" not in state.artifacts.lp3_baseline:
        return None
    description = state.artifacts.lp3_baseline["description"]
    if isinstance(description, dict):
        description = description.get("description") or ""
    return str(description) if description else ""


async def _restore_description(client: Client, state: RunState, scenario: int) -> bool:
    """Restore the captured description, or fail with a manual-recovery row.

    The baseline carries the partition's ownership stamp, so a description the
    CLI cannot write back is a FAIL, not a SKIP: every ownership-guarded command
    refuses the partition until someone restores it (#968). An absent baseline
    takes the same FAIL path rather than silently writing an empty description
    (#1038). Returns whether the description read back equals the baseline.
    """
    description = _baseline_description(state)
    config = state.config
    if description is None:
        state.record(
            scenario,
            "hmc_set_lpar_description (restore)",
            "FAIL",
            "MANUAL RECOVERY REQUIRED: no baseline description was captured for "
            "this run (the ST0 read failed, or the key is absent from a resumed "
            "results file), so the original value is not available from the "
            "results file or anywhere else this harness recorded. Recover it "
            "out of band (CMDB, HMC audit log, or the partition owner) and write "
            f"it back with chsyscfg -r lpar -m {shlex.quote(config.system_name)} "
            f'-i "name={config.lp3_name},description=<original>" or the HMC GUI '
            f"where the CLI record cannot carry it. ST{scenario} left its probe "
            "description in place.",
        )
        return False
    blocked = _unrestorable_description(description)
    if blocked:
        state.record(
            scenario,
            "hmc_set_lpar_description (restore)",
            "FAIL",
            f"MANUAL RECOVERY REQUIRED: chsyscfg -r lpar -m {shlex.quote(config.system_name)} "
            f'-i "name={config.lp3_name},description=<original>" (or the HMC GUI where '
            "the CLI record cannot carry it); the unredacted original is "
            "artifacts.lp3_baseline.description in the results file. "
            f"ST{scenario} left its probe description because the original cannot be "
            f"written back via CLI: {blocked}",
        )
        return False
    status, data = await state.call(
        client,
        "hmc_set_lpar_description",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        description=description,
    )
    state.record(scenario, "hmc_set_lpar_description (restore)", status, data)
    if status != "PASS":
        return False
    return (
        await _read_description(client, state, scenario, "verify restore")
        == description
    )


_PROBE_DESCRIPTION = "MCP live-test probe R2 safe to clear"
# A relative bkprofdata file lands in /var/hsc/profiles/<serial>/ on the HMC.
_PROFILE_BACKUP_FILE = "hmcpctl-live-st10"
# The modes hmc_set_lpar_proc_compat accepts. Both the CLI's `POWER9_base` and
# REST's `POWER9_Base` are accepted (#1319).
_SETTABLE_MODES = frozenset(get_args(ProcessorCompatibilityMode))
# sync_curr_profile values and the hmc_sync_lpar_profile mode that writes each (ADR 0201).
_SYNC_MODES = {"0": "disable", "1": "enable", "2": "suspend"}


def _description_text(data: object) -> str | None:
    """The CLI description value without its line terminator, or ``None``."""
    if isinstance(data, dict):
        data = data.get("description")
    if not isinstance(data, str):
        return None
    return data.removesuffix("\n").removesuffix("\r")


async def _read_description(
    client: Client, state: RunState, scenario: int, label: str
) -> str | None:
    config = state.config
    status, data = await state.call(
        client,
        "hmc_get_lpar_description",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record(scenario, f"hmc_get_lpar_description ({label})", status, data)
    return _description_text(data) if status == "PASS" else None


async def _exercise_description_round_trip(client: Client, state: RunState) -> None:
    """Set a probe description, read it back, and restore the ST0 baseline."""
    config = state.config
    status, data = await state.call(
        client,
        "hmc_set_lpar_description",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        description=_PROBE_DESCRIPTION,
    )
    probe = await _read_description(client, state, 10, "verify")
    restored = await _restore_description(client, state, 10)
    state.record_verified(
        10,
        "hmc_set_lpar_description",
        operation="lpar.set_description",
        scenario="st10-description-round-trip",
        assertions=[
            Assertion(
                "probe-description-read-back",
                status == "PASS"
                and probe is not None
                and probe.endswith(_PROBE_DESCRIPTION),
            ),
            Assertion("baseline-description-restored", restored),
        ],
        cleanup="passed" if restored else "failed",
        data=data,
    )


async def _partition_environments(client: Client, state: RunState) -> dict[str, str]:
    """Map each partition on the system to its ``lpar_env``."""
    config = state.config
    status, data = await state.call(
        client,
        "hmc_run_command",
        cmd=f"lssyscfg -r lpar -m {shlex.quote(config.system_name)} -F name,lpar_env",
    )
    state.record(10, "lssyscfg name,lpar_env", status, data)
    if status != "PASS" or not isinstance(data, str):
        return {}
    pairs = (line.rsplit(",", 1) for line in data.splitlines() if "," in line)
    return {name: environment.strip() for name, environment in pairs}


async def _check_non_vios_msp_refusal(client: Client, state: RunState) -> None:
    """A non-VIOS partition's MSP write is refused; a non-promoting check."""
    config = state.config
    status, data = await state.call(
        client,
        "hmc_set_lpar_msp",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        enabled=True,
    )
    refused = status == "FAIL" and "vioserver" in str(data).lower()
    state.record(
        10,
        "hmc_set_lpar_msp (non-VIOS refusal)",
        "PASS" if refused else "FAIL",
        data,
        "refused before chsyscfg" if refused else "expected a lpar_env refusal",
    )


async def _read_msp(client: Client, state: RunState, vios: str, label: str) -> object:
    config = state.config
    status, data = await state.call(
        client,
        "hmc_get_lpar_msp",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=vios,
    )
    state.record(10, f"hmc_get_lpar_msp ({label})", status, data)
    return data if status == "PASS" else None


async def _exercise_msp_behavior(client: Client, state: RunState) -> None:
    """Toggle the system's first VIOS's MSP flag and restore the value read first."""
    config = state.config
    environments = await _partition_environments(client, state)
    if environments.get(config.lp3_name) != "vioserver":
        await _check_non_vios_msp_refusal(client, state)
    if state.group != "profiles":
        state.skip(
            10, "hmc_set_lpar_msp (VIOS round trip)", "runs only in the profiles arm"
        )
        return
    vioses = sorted(name for name, env in environments.items() if env == "vioserver")
    if not vioses:
        state.skip(10, "hmc_set_lpar_msp (VIOS round trip)", "no VIOS on the system")
        return
    vios = vioses[0]
    original = await _read_msp(client, state, vios, "VIOS pre-read")
    if not isinstance(original, bool):
        state.skip(
            10,
            "hmc_set_lpar_msp (VIOS round trip)",
            f"the VIOS MSP pre-read gave {original!r}; nothing toggled",
        )
        return
    status, data = await state.call(
        client,
        "hmc_set_lpar_msp",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=vios,
        enabled=not original,
    )
    toggled = await _read_msp(client, state, vios, "verify toggle")
    restore_status, restore_data = await state.call(
        client,
        "hmc_set_lpar_msp",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=vios,
        enabled=original,
    )
    state.record(10, "hmc_set_lpar_msp (restore)", restore_status, restore_data)
    # Cleanup is judged by the state read back, not by whether the restore call ran.
    restored = (await _read_msp(client, state, vios, "verify restore")) is original
    state.record_verified(
        10,
        "hmc_set_lpar_msp",
        operation="lpar.set_msp",
        scenario="st10-msp-round-trip",
        assertions=[
            Assertion(
                "vios-msp-toggled", status == "PASS" and toggled is (not original)
            ),
            Assertion("vios-msp-restored", restored),
        ],
        cleanup="passed" if restored else "failed",
        data=data,
    )


async def _read_profile_mode(client: Client, state: RunState) -> tuple[str, str]:
    """Return the default profile's name and ``lpar_proc_compat_mode``, or blanks."""
    config = state.config
    status, data = await state.call(
        client,
        "hmc_get_lpar_proc_compat",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record(10, "hmc_get_lpar_proc_compat", status, data)
    if status != "PASS":
        return "", ""
    return str(field(data, "profile") or ""), str(field(data, "profile_mode") or "")


async def _set_profile_mode(
    client: Client, state: RunState, profile: str, mode: str
) -> tuple[str, object]:
    config = state.config
    return await state.call(
        client,
        "hmc_set_lpar_proc_compat",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        mode=mode,
        profile_name=profile,
    )


# The HMC refuses a profile change while the partition's profile is kept in sync (#1333).
_SYNCHRONIZED_PROFILE = (
    "ST0 sync_curr_profile is 1, so the HMC refuses a profile change and ST10 "
    "leaves the profile's lpar_proc_compat_mode as it is"
)


def _profile_synchronized(state: RunState) -> bool:
    return state.artifacts.lp3_baseline.get("sync_curr_profile") == "1"


async def _exercise_proc_compat(client: Client, state: RunState) -> None:
    """Set a supported mode other than the profile's own, then restore it."""
    config = state.config
    if _profile_synchronized(state):
        state.skip(10, "hmc_set_lpar_proc_compat (round trip)", _SYNCHRONIZED_PROFILE)
        return
    status, modes = await state.call(
        client, "hmc_get_proc_compat_modes", system_name_or_uuid=config.system_name
    )
    state.record(10, "hmc_get_proc_compat_modes", status, modes)
    profile, original = await _read_profile_mode(client, state)
    candidates = [
        mode
        for mode in (modes if status == "PASS" and isinstance(modes, list) else [])
        if mode in _SETTABLE_MODES and mode not in (original, "default")
    ]
    if not profile or original not in _SETTABLE_MODES or not candidates:
        state.skip(
            10,
            "hmc_set_lpar_proc_compat (round trip)",
            f"profile {profile!r} mode {original!r}: no settable probe mode, or the "
            "original cannot be written back through the tool",
        )
        return
    probe = candidates[-1]
    status, data = await _set_profile_mode(client, state, profile, probe)
    _, changed = await _read_profile_mode(client, state)
    restore_status, restore_data = await _set_profile_mode(
        client, state, profile, original
    )
    state.record(10, "hmc_set_lpar_proc_compat (restore)", restore_status, restore_data)
    _, final = await _read_profile_mode(client, state)
    restored = final == original
    state.record_verified(
        10,
        "hmc_set_lpar_proc_compat",
        operation="lpar.set_proc_compat",
        scenario="st10-proc-compat-round-trip",
        assertions=[
            Assertion("profile-mode-changed", status == "PASS" and changed == probe),
            Assertion("profile-mode-restored", restored),
        ],
        cleanup="passed" if restored else "failed",
        data=data,
    )


async def _exercise_sync_round_trip(client: Client, state: RunState) -> None:
    """Set a sync value other than ST0's on the not-activated partition, then restore it."""
    config = state.config
    baseline = state.artifacts.lp3_baseline
    original = baseline.get("sync_curr_profile")
    manual = (
        f"chsyscfg -r lpar -m {shlex.quote(config.system_name)} "
        f'-i "name={config.lp3_name},sync_curr_profile=<0|1|2>"'
    )
    if original not in _SYNC_MODES:
        state.skip(
            10,
            "hmc_sync_lpar_profile (round trip)",
            f"no valid ST0 sync_curr_profile ({original!r}); restore by hand if needed: {manual}",
        )
        return
    if baseline.get("state") != "Not Activated":
        state.skip(
            10,
            "hmc_sync_lpar_profile (round trip)",
            "the partition is activated; enabling sync there can overwrite its active "
            "profile and is a recorded live gap",
        )
        return
    # A probe equal to the baseline reads back as a pass with nothing changed (#1323).
    disabling = original == "1"
    probe = "0" if disabling else "1"
    status, data = await state.call(
        client,
        "hmc_sync_lpar_profile",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        mode=_SYNC_MODES[probe],
    )
    probed = await read_sync_state(client, state, 10)
    refused = isinstance(data, CallFailure) and data.exception_type != "InvalidDispatch"
    if disabling and refused and probed is not None and probed[0] == original:
        # No live capture of `disable` exists: a refusal that left the baseline in
        # place is an unproven mode, not a failed round trip, and needs no restore.
        state.record(
            10,
            "hmc_sync_lpar_profile (round trip)",
            "SKIP",
            data,
            "the disable probe was refused and sync_curr_profile still reads 1; "
            f"if a later read shows 0, restore by hand: {manual}",
        )
        return
    restore_status, restore_data = await state.call(
        client,
        "hmc_sync_lpar_profile",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        mode=_SYNC_MODES[original],
    )
    state.record(10, "hmc_sync_lpar_profile (restore)", restore_status, restore_data)
    final = await read_sync_state(client, state, 10)
    restored = final is not None and final[0] == original
    probe_read = status == "PASS" and probed is not None and probed[0] == probe
    state.record_verified(
        10,
        "hmc_sync_lpar_profile",
        operation="lpar_profile.sync",
        scenario="st10-sync-round-trip",
        assertions=[
            Assertion("sync-disable-read-0", probe_read)
            if disabling
            else Assertion("sync-enable-read-1", probe_read),
            Assertion("sync-restored-baseline", restored),
        ],
        cleanup="passed" if restored else "failed",
        data=data,
    )


def _records(text: object) -> list[dict[str, str]]:
    """Parse ``lssyscfg`` output into one attribute mapping per line."""
    if not isinstance(text, str):
        return []
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        pairs = (item.partition("=") for item in next(csv.reader([line])))
        records.append({key: value for key, _, value in pairs})
    return records


async def _read_system(
    client: Client, state: RunState, resource: str, label: str
) -> object:
    """Read every ``lpar`` or ``prof`` record on the system; ``None`` on failure."""
    config = state.config
    status, data = await state.call(
        client,
        "hmc_run_command",
        cmd=f"lssyscfg -r {resource} -m {shlex.quote(config.system_name)}",
    )
    state.record(10, f"lssyscfg -r {resource} ({label})", status, data)
    return data if status == "PASS" and isinstance(data, str) else None


def _same_lines(before: object, after: object) -> bool:
    """Equal line sets: the HMC reorders a partition's profiles after a restore."""
    return (
        isinstance(before, str)
        and isinstance(after, str)
        and sorted(before.splitlines()) == sorted(after.splitlines())
    )


async def _reapply_unconfigured(
    client: Client, state: RunState, before: object, after: object
) -> None:
    """Re-apply each not-activated partition whose resources the restore unconfigured.

    `rstprofdata -l 3` resets a not-activated partition's ``resource_config``
    from 1 to 0, even merging a backup taken moments earlier (#627, observed live).
    A partition this cannot re-apply, or whose state after the restore is
    unknown, is a FAIL naming the command to run by hand.
    """
    config = state.config
    system = shlex.quote(config.system_name)
    configured = {
        record["name"]: record.get("curr_profile", "")
        for record in _records(before)
        if record.get("resource_config") == "1"
        and record.get("state") == "Not Activated"
    }
    after_config = {
        record.get("name"): record.get("resource_config") for record in _records(after)
    }
    for name, profile in configured.items():
        current = after_config.get(name)
        if current not in (None, "0"):
            continue
        apply = (
            f"chsyscfg -r lpar -m {system} -o apply -p {shlex.quote(name)} "
            f"-n {shlex.quote(profile) if profile else '<profile>'}"
        )
        detail: object
        if current is None:
            detail = "the post-restore read did not report its resource_config"
        elif not profile:
            detail = "the restore left its resource_config at 0; it has no curr_profile"
            apply += f" (list profiles: lssyscfg -r prof -m {system} -F lpar_name,name)"
        else:
            status, detail = await state.call(client, "hmc_run_command", cmd=apply)
            if status == "PASS":
                state.record(
                    10, "chsyscfg -o apply (re-apply after restore)", status, detail
                )
                continue
        # The instruction rides in the note, which is never redacted: the FAIL
        # data's hostname redaction would mask a dotted partition or profile name.
        state.record(
            10,
            "chsyscfg -o apply (re-apply after restore)",
            "FAIL",
            detail,
            f"MANUAL RECOVERY REQUIRED: partition {name!r} — if its resource_config "
            f"is 0, run {apply}",
        )


async def _exercise_profile_backup_restore(client: Client, state: RunState) -> None:
    """Back up every profile, merge-restore that file, and compare the system.

    A type-3 merge from a backup taken moments earlier, current data winning,
    shows whether the restore is non-destructive; it cannot show that data was
    restored.
    """
    config = state.config
    profiles_before = await _read_system(client, state, "prof", "before")
    partitions_before = await _read_system(client, state, "lpar", "before")
    status, data = await state.call(
        client,
        "hmc_backup_lpar_profiles",
        system_name_or_uuid=config.system_name,
        file_path=_PROFILE_BACKUP_FILE,
        force=True,
    )
    if status != "PASS":
        reason = "the backup failed"
    elif profiles_before is None or partitions_before is None:
        reason = (
            "a pre-restore lssyscfg read failed, so the restore could be neither "
            "compared nor compensated"
        )
    else:
        reason = ""
    if reason:
        state.record_verified(
            10,
            "hmc_backup_lpar_profiles",
            operation="lpar_profile.backup",
            scenario="st10-profile-backup-restore",
            assertions=[Assertion("backup-accepted", status == "PASS")],
            cleanup="not-required",
            data=data,
        )
        state.skip(10, "hmc_restore_lpar_profiles", f"{reason}; not restoring")
        return
    restore_status, restore_data = await state.call(
        client,
        "hmc_restore_lpar_profiles",
        system_name_or_uuid=config.system_name,
        file_path=_PROFILE_BACKUP_FILE,
        restore_type=3,
        system_wide_restore_approved=True,
        ownership_override=True,
    )
    profiles_after = await _read_system(client, state, "prof", "after")
    partitions_after = await _read_system(client, state, "lpar", "after")
    await _reapply_unconfigured(client, state, partitions_before, partitions_after)
    partitions_final = await _read_system(client, state, "lpar", "final")
    state.record_verified(
        10,
        "hmc_backup_lpar_profiles",
        operation="lpar_profile.backup",
        scenario="st10-profile-backup-restore",
        assertions=[
            Assertion("backup-accepted", True),
            Assertion("backup-file-restorable", restore_status == "PASS"),
        ],
        cleanup="not-required",
        data=data,
    )
    state.record_verified(
        10,
        "hmc_restore_lpar_profiles",
        operation="lpar_profile.restore",
        scenario="st10-profile-backup-restore",
        assertions=[
            Assertion("merge-current-wins-accepted", restore_status == "PASS"),
            Assertion(
                "profiles-unchanged-after-merge-current-wins",
                _same_lines(profiles_before, profiles_after),
            ),
            Assertion(
                "partitions-unchanged-after-merge-current-wins",
                _same_lines(partitions_before, partitions_after),
            ),
        ],
        cleanup="passed"
        if _same_lines(partitions_before, partitions_final)
        else "failed",
        data=restore_data,
    )


async def _check_memory_pool_removal_refusal(client: Client, state: RunState) -> None:
    """Removing the pool of a system without one is refused before chhwres.

    The remove names no pool, so on a system that has one it would delete it:
    the probe is skipped there rather than risking a real pool.
    """
    config = state.config
    tool = "hmc_remove_memory_pool (no-pool refusal)"
    status, pools = await state.call(
        client, "hmc_list_memory_pools", system_name_or_uuid=config.system_name
    )
    if status != "PASS" or pools != []:
        note = (
            "the system lists a pool; not removing it"
            if status == "PASS"
            else "the pool list read failed; not removing"
        )
        state.record(10, tool, "SKIP", pools, note)
        return
    status, data = await state.call(
        client, "hmc_remove_memory_pool", system_name_or_uuid=config.system_name
    )
    refused = status == "FAIL" and "has no shared memory pool" in str(data)
    state.record(
        10,
        tool,
        "PASS" if refused else "FAIL",
        data,
        "refused before chhwres" if refused else "expected the no-pool refusal",
    )


async def mutate_lpar_properties(client: Client, state: RunState) -> None:
    """Run the ordered ST10 property round trips against the baseline LPAR."""
    print("\n=== ST10: LPAR Properties Mutations ===")
    await _exercise_description_round_trip(client, state)
    await _exercise_msp_behavior(client, state)
    if state.group == "profiles":
        await _exercise_proc_compat(client, state)
        await _exercise_sync_round_trip(client, state)
        await _exercise_profile_backup_restore(client, state)
    else:
        state.skip(
            10,
            "proc-compat, sync and profile backup/restore round trips",
            "they change the VIOS, the profile and every profile on the system, so "
            "they run only in the profiles arm",
        )
    await _check_memory_pool_removal_refusal(client, state)


# ---------------------------------------------------------------------------
# ST15 — Restore the baseline LPAR
# ---------------------------------------------------------------------------


async def _restore_baseline_profile_mode(client: Client, state: RunState) -> None:
    """Write ST0's profile ``lpar_proc_compat_mode`` back, or ask for it by hand."""
    config = state.config
    captured = state.artifacts.lp3_baseline.get("proc_compat")
    profile = field(captured, "profile")
    mode = field(captured, "profile_mode")
    if _profile_synchronized(state):
        state.skip(15, "hmc_set_lpar_proc_compat (restore)", _SYNCHRONIZED_PROFILE)
        return
    if not profile or not mode:
        state.record(
            15,
            "hmc_set_lpar_proc_compat (restore)",
            "FAIL",
            "MANUAL RECOVERY REQUIRED: no baseline profile mode was captured; confirm "
            f"chsyscfg -r prof -m {shlex.quote(config.system_name)} -i "
            f'"name=<profile>,lpar_name={config.lp3_name},lpar_proc_compat_mode=<mode>"',
        )
        return
    if mode not in _SETTABLE_MODES:
        state.skip(
            15,
            "hmc_set_lpar_proc_compat (restore)",
            f"baseline mode {mode!r} cannot be written through the tool; "
            "ST10 does not change it",
        )
        return
    status, data = await _set_profile_mode(client, state, str(profile), str(mode))
    state.record(15, "hmc_set_lpar_proc_compat (restore)", status, data)


async def restore_lpar_baseline(client: Client, state: RunState) -> None:
    config = state.config
    print("\n=== ST15: Restore baseline LPAR ===")

    st, data = await state.call(
        client, "hmc_lpar_summary", lpar_name_or_uuid=config.lp3_name
    )
    state.record(15, "hmc_lpar_summary (post-test)", st, data)

    await _restore_description(client, state, 15)
    await _restore_baseline_profile_mode(client, state)

    # Final adapter audit
    st, data = await state.call(
        client,
        "hmc_list_adapters",
        lpar_name_or_uuid=config.lp3_name,
        adapter_type="ClientNetworkAdapter",
    )
    state.record(15, "hmc_list_adapters (final audit)", st, data)

    # Final CLI dump
    st, data = await state.call(
        client,
        "hmc_run_command",
        cmd=f"lssyscfg -r lpar -m {shlex.quote(config.system_name)}"
        f" --filter {shlex.quote(build_filter([('lpar_names', config.lp3_name)]))}",
    )
    state.record(15, "hmc_run_command lssyscfg (final)", st, data)

    # Final summary
    st, data = await state.call(
        client, "hmc_lpar_summary", lpar_name_or_uuid=config.lp3_name
    )
    state.record(15, "hmc_lpar_summary (final confirm)", st, data)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ST16 — VG Free-Space Check + Repository Create
# ---------------------------------------------------------------------------
