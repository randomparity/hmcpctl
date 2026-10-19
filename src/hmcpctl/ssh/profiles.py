"""LPAR property and profile commands over the SSH transport."""

from __future__ import annotations

import csv
import re
import shlex
from dataclasses import dataclass
from typing import Literal

from ..config import HMCConfig
from .commands import build_attribute_record, build_filter, parse_hmc_result_rows
from .description_validation import validate_lpar_description
from .transport import HMCCLIError, run_hmc_command


async def get_lpar_description(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
) -> str:
    """Get the description field of *lpar_name* on *system_name* via SSH.

    Returns the raw CLI value, including an empty line when no description is
    set. CLI-name-keyed write flows use this transport; bulk ownership reads
    obtain the same field from the REST list feed.
    """
    cmd = (
        f"lssyscfg -r lpar -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))} -F description"
    )
    return await run_hmc_command(config, cmd)


async def set_lpar_description(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    description: str,
) -> str:
    """Set the description field of *lpar_name* via SSH.

    Runs ``chsyscfg -r lpar -m <system_name>
    -i "name=<lpar_name>,description=<description>"`` and returns the raw
    command output.

    Raises ``ValueError`` if *description* is not printable ASCII or carries a
    character the record treats as structure; see
    :func:`validate_lpar_description` for the constraint and error code.

    Raises :class:`HMCCLIError` if *lpar_name* contains a character that would
    corrupt the ``chsyscfg -i`` attribute record; see
    :func:`build_attribute_record`, which enforces the record grammar for both
    fields so the guard cannot be present at one and absent at its neighbour.
    Space and semicolon are ordinary value data, as confirmed by the live-HMC
    verification recorded in ADR 0045.
    """
    validate_lpar_description(description)
    record = build_attribute_record([("name", lpar_name), ("description", description)])
    cmd = f"chsyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(record)}"
    return await run_hmc_command(config, cmd)


async def get_lpar_msp(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
) -> bool:
    """Get the MSP (Migratable Service Partition) flag of *lpar_name* via SSH.

    Runs ``lssyscfg -r lpar -m <system_name> --filter lpar_names=<lpar_name>
    -F msp`` and returns ``True`` when the flag is ``1``, ``False`` when ``0``.
    """
    cmd = (
        f"lssyscfg -r lpar -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))} -F msp"
    )
    raw = await run_hmc_command(config, cmd)
    value = raw.strip()
    if value == "1":
        return True
    if value == "0":
        return False
    raise HMCCLIError(
        f"Unexpected MSP value {value!r} for LPAR {lpar_name!r} "
        f"on system {system_name!r}; expected '0' or '1'"
    )


async def set_lpar_msp(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    enabled: bool,
) -> str:
    """Set the MSP (Migratable Service Partition) flag of *lpar_name* via SSH.

    Checks that *lpar_name* is a VIOS partition (``lpar_env=vioserver``) before
    issuing the command.  The HMC rejects ``msp=...`` for AIX/Linux partitions
    with a confusing generic error; this guard surfaces a clear diagnostic
    before the SSH round-trip.

    Note: the ``lpar_env`` probe and the ``chsyscfg`` write are two separate
    SSH connections (each ``run_hmc_command`` call opens its own connection).
    The guard is not atomic with the write; the HMC itself enforces the
    VIOS-only invariant and returns an error if the race were to occur.

    Runs ``chsyscfg -r lpar -m <system_name> -i "name=<lpar_name>,msp=<0|1>"``
    and returns the raw command output.

    Raises:
        HMCCLIError: If the partition is not found on the system (the HMC's
            ``HSCL8012``), or if its ``lpar_env`` is not ``vioserver``.
    """
    env_cmd = (
        f"lssyscfg -r lpar -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))} -F lpar_env"
    )
    # An unknown partition exits 1 with HSCL8012, which run_hmc_command raises.
    lpar_env = (await run_hmc_command(config, env_cmd)).strip()
    if lpar_env != "vioserver":
        raise HMCCLIError(
            f"Cannot set MSP on '{lpar_name}': the msp attribute is only valid "
            f"for a VIOS partition (lpar_env=vioserver), but '{lpar_name}' has "
            f"lpar_env='{lpar_env}'. Use hmc_list_vios to confirm the partition type."
        )
    value = "1" if enabled else "0"
    record = build_attribute_record([("name", lpar_name), ("msp", value)])
    cmd = f"chsyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(record)}"
    return await run_hmc_command(config, cmd)


# Processor compatibility (lssyscfg / chsyscfg)


async def get_proc_compat_modes(
    config: HMCConfig,
    system_name: str,
) -> list[str]:
    """List processor compatibility modes supported by *system_name* via SSH.

    Runs ``lssyscfg -r sys -m <system_name> -F lpar_proc_compat_modes`` and
    returns the comma-separated modes as a list of stripped strings.
    """
    cmd = f"lssyscfg -r sys -m {shlex.quote(system_name)} -F lpar_proc_compat_modes"
    raw = await run_hmc_command(config, cmd)
    if not raw.strip():
        return []
    try:
        values = next(iter(csv.reader([raw.strip()], strict=True)))
    except csv.Error as error:
        raise HMCCLIError(
            f"malformed processor compatibility mode output: {error}"
        ) from error
    return [
        mode.strip() for value in values for mode in value.split(",") if mode.strip()
    ]


async def get_lpar_default_profile(
    config: HMCConfig, system_name: str, lpar_name: str
) -> str:
    """Return the name of *lpar_name*'s default partition profile.

    Runs ``lssyscfg -r lpar -m <system_name> --filter lpar_names=<lpar_name>
    -F default_profile``.

    Raises:
        HMCCLIError: If the HMC reports no default profile.
    """
    cmd = (
        f"lssyscfg -r lpar -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))} "
        "-F default_profile"
    )
    name = (await run_hmc_command(config, cmd)).strip()
    if not name:
        raise HMCCLIError(
            f"partition {lpar_name!r} reports no default profile; "
            "pass profile_name to choose one"
        )
    return name


async def get_lpar_proc_compat(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    profile_name: str | None = None,
) -> dict[str, str]:
    """Get the processor compatibility modes of an LPAR and one of its profiles.

    Runs ``lssyscfg -r lpar -m <system_name> --filter lpar_names=<lpar_name>
    -F desired_lpar_proc_compat_mode,curr_lpar_proc_compat_mode,default_profile``
    and then ``lssyscfg -r prof`` for *profile_name* (the default profile when
    omitted). Returns ``"desired"`` and ``"curr"`` (the partition), ``"profile"``
    (the profile examined) and ``"profile_mode"`` (its
    ``lpar_proc_compat_mode``). The partition's desired mode is not the profile's
    value: the HMC accepts ``lpar_proc_compat_mode`` only on a profile.

    Note: ``pend_lpar_proc_compat_mode`` is not a valid HMC CLI attribute;
    ``desired_lpar_proc_compat_mode`` is the correct field name.
    """
    cmd = (
        f"lssyscfg -r lpar -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(build_filter([('lpar_names', lpar_name)]))} "
        "-F desired_lpar_proc_compat_mode,curr_lpar_proc_compat_mode,default_profile"
    )
    raw = await run_hmc_command(config, cmd)
    parts = [part.strip() for part in raw.strip().split(",")]
    parts += [""] * (3 - len(parts))
    profile = profile_name or parts[2]
    if not profile:
        raise HMCCLIError(
            f"partition {lpar_name!r} reports no default profile; "
            "pass profile_name to choose one"
        )
    mode_cmd = (
        f"lssyscfg -r prof -m {shlex.quote(system_name)} --filter "
        f"{shlex.quote(build_filter([('lpar_names', lpar_name), ('profile_names', profile)]))} "
        "-F lpar_proc_compat_mode"
    )
    profile_mode = (await run_hmc_command(config, mode_cmd)).strip()
    return {
        "desired": parts[0],
        "curr": parts[1],
        "profile": profile,
        "profile_mode": profile_mode,
    }


async def set_lpar_proc_compat(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    mode: str,
    profile_name: str | None = None,
) -> str:
    """Set the processor compatibility mode on a profile of *lpar_name* via SSH.

    ``lpar_proc_compat_mode`` is a partition-profile attribute; ``chsyscfg -r
    lpar`` rejects it. Runs ``chsyscfg -r prof -m <system_name>
    -i "name=<profile>,lpar_name=<lpar_name>,lpar_proc_compat_mode=<mode>"``,
    where the profile is *profile_name* or, when omitted, the partition's default
    profile. Returns the name of the profile changed.

    Raises:
        HMCCLIError: If *lpar_name*, *profile_name* or *mode* contains a
            character the ``-i`` record's parser treats as structure, or the
            partition has no default profile.
    """
    profile = profile_name or await get_lpar_default_profile(
        config, system_name, lpar_name
    )
    record = build_attribute_record(
        [("name", profile), ("lpar_name", lpar_name), ("lpar_proc_compat_mode", mode)]
    )
    cmd = f"chsyscfg -r prof -m {shlex.quote(system_name)} -i {shlex.quote(record)}"
    await run_hmc_command(config, cmd)
    return profile


# SR-IOV adapter mode and vNICs (chhwres)


async def backup_lpar_profiles(
    config: HMCConfig,
    system_name: str,
    file_path: str,
    *,
    force: bool = False,
) -> str:
    """Backup all LPAR profiles on *system_name* to *file_path* via SSH.

    Runs ``bkprofdata -m <system_name> -f <file_path>`` and returns the raw
    command output. *file_path* is on the HMC filesystem, not the local
    machine; the backup file is created at that path on the HMC host.

    When *force* is ``True``, ``--force`` is appended to the command so that
    an existing file at *file_path* is overwritten instead of raising an error.
    """
    cmd = f"bkprofdata -m {shlex.quote(system_name)} -f {shlex.quote(file_path)}"
    if force:
        cmd += " --force"  # literal flag — not a user value, no quoting needed
    return await run_hmc_command(config, cmd)


# rstprofdata's mandatory ``-l`` (rstprofdata.md). Type 4 initializes the
# profile data, deleting every partition, and is not offered.
ProfileRestoreType = Literal[1, 2, 3]
_PROFILE_RESTORE_TYPES = (1, 2, 3)


async def restore_lpar_profiles(
    config: HMCConfig,
    system_name: str,
    file_path: str,
    restore_type: ProfileRestoreType,
    *,
    force_keystore: bool = False,
) -> str:
    """Restore LPAR profiles from *file_path* on *system_name* via SSH.

    Runs ``rstprofdata -m <system_name> -l <restore_type> -f <file_path>`` and
    returns the raw command output. *file_path* must already exist on the HMC
    filesystem. `force_keystore=True` appends `--forcekeystore`; successful stderr
    warnings are returned alongside stdout. *restore_type* is the mandatory type:

    - ``1``: full restore from the backup file.
    - ``2``: merge the current and backup profile data; on a conflict the
      backup data wins.
    - ``3``: merge the current and backup profile data; on a conflict the
      current data wins.

    Raises:
        ValueError: If *restore_type* is not 1, 2 or 3, before any command runs.
    """
    if isinstance(restore_type, bool) or restore_type not in _PROFILE_RESTORE_TYPES:
        raise ValueError(
            f"restore_type must be 1, 2 or 3, got {restore_type!r}: 1 restores the "
            "backup in full, 2 merges with the backup winning conflicts, 3 merges "
            "with the current data winning. Type 4 (initialize, which deletes "
            "every partition) is not offered."
        )
    if not isinstance(force_keystore, bool):
        raise TypeError("force_keystore must be a boolean")
    if not file_path or not file_path.strip():
        raise ValueError("file_path must not be empty")
    cmd = (
        f"rstprofdata -m {shlex.quote(system_name)} -l {restore_type} "
        f"-f {shlex.quote(file_path)}"
    )
    if force_keystore:
        cmd += " --forcekeystore"
    return await run_hmc_command(config, cmd, include_stderr=True)


# The ``sync_curr_profile`` partition setting's documented values (chsyscfg).
ProfileSyncMode = Literal["enable", "disable", "suspend"]
_PROFILE_SYNC_VALUES: dict[str, int] = {"enable": 1, "disable": 0, "suspend": 2}


def validate_profile_sync_mode(mode: object) -> None:
    """Refuse a mode other than ``enable``, ``disable`` or ``suspend``."""
    if mode not in _PROFILE_SYNC_VALUES:
        raise ValueError(f"mode must be enable, disable or suspend, got {mode!r}")


async def sync_lpar_profile(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    mode: ProfileSyncMode = "enable",
) -> str:
    """Set *lpar_name*'s ``sync_curr_profile`` partition setting (ADR 0201).

    Runs ``chsyscfg -r lpar -m <system_name>
    -i "name=<lpar_name>,sync_curr_profile=<1|0|2>"`` and returns the raw command
    output. ``enable`` (1) keeps the partition's current configuration
    synchronized with its active profile, ``disable`` (0) turns that off, and
    ``suspend`` (2) pauses it until the next activation or apply. The setting
    persists; the call itself writes no profile on a not-activated partition.

    Raises:
        ValueError: If *mode* is not ``enable``, ``disable`` or ``suspend``,
            before any command runs.
        HMCCLIError: If *lpar_name* contains a character the ``-i`` record's
            parser treats as structure.
    """
    validate_profile_sync_mode(mode)
    record = build_attribute_record(
        [("name", lpar_name), ("sync_curr_profile", _PROFILE_SYNC_VALUES[mode])]
    )
    cmd = f"chsyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(record)}"
    return await run_hmc_command(config, cmd)


async def assign_profile_io_slot(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    profile_name: str,
    drc_index: str,
) -> str:
    """Add a physical I/O slot DRC index to *profile_name* without force.

    Raises:
        HMCCLIError: If *profile_name*, *drc_index*, or *lpar_name* contains a
            character the ``-i`` record's parser treats as structure.  The
            ``//0`` suffix is record-safe, so validating the whole ``io_slots``
            value covers *drc_index*.
    """
    return await _change_profile_io_slot(
        config, system_name, lpar_name, profile_name, drc_index, add=True
    )


async def unassign_profile_io_slot(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    profile_name: str,
    drc_index: str,
) -> str:
    """Remove a physical I/O slot DRC index from a profile without force."""
    return await _change_profile_io_slot(
        config, system_name, lpar_name, profile_name, drc_index, add=False
    )


async def _change_profile_io_slot(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    profile_name: str,
    drc_index: str,
    *,
    add: bool,
) -> str:
    operator = "io_slots+" if add else "io_slots-"
    record = build_attribute_record(
        [
            ("name", profile_name),
            (operator, f"{drc_index}//0"),
            ("lpar_name", lpar_name),
        ]
    )
    command = f"chsyscfg -r prof -m {shlex.quote(system_name)} -i {shlex.quote(record)}"
    return await run_hmc_command(config, command)


PROFILE_IO_SLOT_FIELDS = ("lpar_name", "name", "io_slots")

#: The DRC-index form every captured and documented `io_slots` index takes (ADR 0166).
DRC_INDEX_PATTERN = re.compile(r"[0-9A-F]{8}")


@dataclass(frozen=True)
class ProfileIoSlot:
    """One `io_slots` entry in the ADR 0165-admitted read rendering."""

    drc_index: str
    pool_id: str | None
    is_required: bool


def profile_io_slot_rows_command(system_name: str) -> str:
    """Return the exact profile `io_slots` read ADR 0165 admits.

    The captured form is issued verbatim: all three fields, ``--header``, and no
    ``--filter``. ADR 0165 admits no narrower form.
    """
    return (
        f"lssyscfg -r prof -m {shlex.quote(system_name)} "
        f"-F {','.join(PROFILE_IO_SLOT_FIELDS)} --header"
    )


def parse_profile_io_slot_rows(output: str) -> list[dict[str, str]]:
    """Parse the admitted readback into one row per profile; the empty-result reply is none.

    Raises:
        HMCCLIError: If *output* is not the header-bearing three-field table.
    """
    return parse_hmc_result_rows(
        output, PROFILE_IO_SLOT_FIELDS, "profile io_slots readback"
    )


async def read_profile_io_slot_rows(
    config: HMCConfig, system_name: str
) -> list[dict[str, str]]:
    """Read every profile's `io_slots` with the exact command ADR 0165 admits."""
    output = await run_hmc_command(config, profile_io_slot_rows_command(system_name))
    return parse_profile_io_slot_rows(output)


def parse_profile_io_slots(value: str) -> tuple[ProfileIoSlot, ...]:
    """Parse an admitted `io_slots` value into `drc/pool/is_required` triples.

    The HMC renders an unset pool as the literal ``none`` where the documented input
    grammar leaves it empty, so an empty pool position is refused rather than read as
    unset. The whole value ``none`` is a profile with no slots.

    Raises:
        HMCCLIError: If any entry is not in the admitted rendering, or a DRC index
            appears twice.
    """
    if value == "none":
        return ()
    slots: list[ProfileIoSlot] = []
    for entry in value.split(","):
        parts = entry.split("/")
        if (
            len(parts) != 3
            or not DRC_INDEX_PATTERN.fullmatch(parts[0])
            or not parts[1].strip()
            or parts[2] not in {"0", "1"}
        ):
            raise HMCCLIError(f"unadmitted io_slots rendering: {entry!r}")
        drc_index, pool_id, is_required = parts
        slots.append(
            ProfileIoSlot(
                drc_index, None if pool_id == "none" else pool_id, is_required == "1"
            )
        )
    if len({slot.drc_index for slot in slots}) != len(slots):
        raise HMCCLIError(
            f"unadmitted io_slots rendering: repeated DRC index in {value!r}"
        )
    return tuple(slots)


async def read_lpar_profile_record(
    config: HMCConfig, system_name: str, lpar_name: str, profile_name: str
) -> str:
    """Read exactly one native LPAR profile attribute record."""
    filters = build_filter([("lpar_names", lpar_name), ("profile_names", profile_name)])
    command = (
        f"lssyscfg -r prof -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(filters)}"
    )
    output = await run_hmc_command(config, command)
    records = [line for line in output.splitlines() if line]
    if len(records) != 1:
        raise HMCCLIError(
            "lssyscfg profile capture expected exactly one record; "
            f"received {len(records)}"
        )
    return records[0]


async def list_system_profile_records(config: HMCConfig, system_cli_name: str) -> str:
    """Read the entire system-profile association inventory, retaining diagnostics."""
    return await run_hmc_command(
        config,
        f"lssyscfg -r sysprof -m {shlex.quote(system_cli_name)} "
        "-F name:lpar_ids:profile_names --header",
        include_stderr=True,
    )


async def delete_system_profile_record(
    config: HMCConfig, system_cli_name: str, name: str
) -> str:
    """Delete exactly one system profile through the selected documented route."""
    return await run_hmc_command(
        config,
        f"rmsyscfg -r sysprof -m {shlex.quote(system_cli_name)} -n {shlex.quote(name)}",
        include_stderr=True,
    )


async def validate_system_profile_record(
    config: HMCConfig,
    system_cli_name: str,
    name: str,
    *,
    activate: bool = False,
    continue_on_error: bool = False,
) -> str:
    """Execute one fixed validation or combined validation/activation command."""
    if not isinstance(activate, bool) or not isinstance(continue_on_error, bool):
        raise TypeError("activation and continuation must be boolean")
    if continue_on_error and not activate:
        raise ValueError("continue requires activation")
    command = f"chsysstate -m {shlex.quote(system_cli_name)} -r sysprof -n {shlex.quote(name)}"
    if activate:
        command += " -o on"
    command += " --test"
    if continue_on_error:
        command += " --continue"
    return await run_hmc_command(config, command, include_stderr=True)
