"""LPAR creation, ownership, validation, and SSH name resolution."""

from __future__ import annotations

import csv
import re
import shlex
from dataclasses import fields, replace
from typing import TYPE_CHECKING

from ..documents.lpar_io import LparPhysicalIO
from .lpar_io import creation_io_record

if TYPE_CHECKING:
    from ..operations.lpar.assignments import SriovLogicalPortAssignment

from pydantic import TypeAdapter

from ..config import HMCConfig
from ..documents.lpar import (
    SHARING_MODES,
    LparReportingSettings,
    LparResources,
    LparSettings,
    shared_units_over_vcpus,
    shared_vcpu_defaults,
)
from .commands import build_attribute_record, build_filter
from .description_validation import validate_lpar_description
from .profiles import set_lpar_description
from .transport import HMCCLIError, run_hmc_command

# The one partition profile create_lpar_via_cli writes and the create path applies.
DEFAULT_PROFILE_NAME = "default_profile"

# mksyscfg's dedicated-processor sharing_mode values (hmc-commands-p10 mksyscfg).
_DEDICATED_SHARING_MODES = (
    "keep_idle_procs",
    "share_idle_procs",
    "share_idle_procs_active",
    "share_idle_procs_always",
)


def validate_caller_token(token: str) -> None:
    """Raise ``ValueError`` if *token* cannot be embedded as ``[caller <token>]``.

    The grammar is server-defined (ADR 0064): 1–64 printable ASCII characters,
    forbidding whitespace, control characters, non-ASCII, and the characters
    ``,` ``=```"``[``]`` and ``\\``.  Commas, equals signs, and quotes are the
    HMC CLI ``-i`` record structure (ADR 0045); brackets break the caller
    segment's own framing; the backslash is refused because ADR 0045 records
    its ``-i`` behaviour as unverified.  The empty string is a violation, not
    an omission, and a non-``str`` value is rejected before any character
    check because the second validation site serves callers that bypass MCP
    tool typing.
    """
    if not isinstance(token, str):
        raise ValueError(f"caller_token must be a string, got {type(token).__name__}")  # noqa: TRY004 - ValueError is what the best-effort boundary below catches, asserted in tests/unit/test_ownership.py
    if not token:
        raise ValueError("caller_token must not be empty")
    if len(token) > 64:
        raise ValueError(f"caller_token is {len(token)} characters; maximum is 64")
    if not token.isascii() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in token
    ):
        raise ValueError(
            "caller_token contains non-ASCII or non-printable characters; "
            "only printable ASCII is accepted"
        )
    if any(character.isspace() for character in token):
        raise ValueError("caller_token contains whitespace; it must be a single word")
    forbidden = {
        ",": "commas corrupt the HMC CLI -i parser",
        "=": "equals signs corrupt the HMC CLI -i parser",
        '"': "double quotes are the HMC CLI -i record escape",
        "[": "brackets break the [caller <token>] segment format",
        "]": "brackets break the [caller <token>] segment format",
        "\\": "backslash behaviour inside an HMC CLI -i record is unverified "
        "(ADR 0045)",
    }
    for character, reason in forbidden.items():
        if character in token:
            raise ValueError(f"caller_token contains {character!r}; {reason}")


async def stamp_lpar_ownership(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    *,
    agent_id: str | None = None,
    caller_token: str | None = None,
) -> str | None:
    """Write an ownership token, plus an optional caller token, to *lpar_name*.

    Builds ``[hmcpctl owner:<agent_id> created:<YYYY-MM-DD>]`` and, when
    *caller_token* is given, appends `` [caller <token>]`` (ADR 0064), then
    writes the combined description with :func:`set_lpar_description` over SSH
    in one call.

    Returns the description on success; returns ``None`` (without raising) on
    SSH/network failure or a composed-description grammar failure — a
    best-effort post-create call that must not fail the LPAR creation itself.
    A malformed *caller_token* raises ``ValueError`` before any SSH traffic
    instead of being swallowed, so it can never discard the ownership stamp.

    *agent_id* defaults to ``"hmcpctl"`` when ``None`` or empty.
    """
    import datetime

    if caller_token is not None:
        validate_caller_token(caller_token)
    effective_id = agent_id if agent_id else "hmcpctl"
    today = datetime.date.today().isoformat()  # noqa: DTZ011 - the ADR 0011 ownership stamp records the operator's local calendar date and is persisted on the HMC; moving it to UTC is not this issue's to make
    description = f"[hmcpctl owner:{effective_id} created:{today}]"
    if caller_token is not None:
        description = f"{description} [caller {caller_token}]"
    try:
        # The composed description still gets the HMC's own grammar check
        # here, inside the best-effort boundary, as a defensive last resort:
        # validate_agent_id now refuses every character the description field
        # would reject ('"' and '\\' included; ADR 0065), so a ValueError
        # raised below the pre-flight check has no expected config-driven case
        # left — it still degrades to a skipped stamp rather than failing
        # the owning create after the LPAR already exists.  A malformed
        # caller_token cannot reach this catch: validate_caller_token above
        # the try raises before any SSH traffic (ADR 0064).
        validate_lpar_description(description)
        await set_lpar_description(config, system_name, lpar_name, description)
        return description
    except (HMCCLIError, OSError, ValueError):
        # Transport, network, and composed-description grammar failures are
        # best-effort here: none of these should fail the owning create call.
        return None


async def create_lpar_via_cli(
    config: HMCConfig,
    system_name: str,
    name: str,
    partition_type: str = "AIX/Linux",
    resources: LparResources = LparResources(),
    max_virtual_slots: int | None = None,
    profile_name: str = DEFAULT_PROFILE_NAME,
    *,
    partition_id: int | None = None,
    settings: LparSettings | None = None,
    reporting_settings: LparReportingSettings | None = None,
    physical_io: LparPhysicalIO | None = None,
    creation_sriov: tuple[SriovLogicalPortAssignment, ...] = (),
) -> str:
    """Create an LPAR via ``mksyscfg`` over SSH.

    The create path's fallback when the REST ``PUT ManagedSystem/{uuid}/LogicalPartition``
    is refused with HTTP 406 or a 400 ``REST0001`` payload rejection (ADR 0178), and its
    only path for a create with no resource values.  This is the same approach used by
    the IBM ansible-power-hmc collection and IBM internal provisioning toolkits.

    When no explicit resource values (memory/proc/vcpu) are provided, the
    ``all_resources=1`` flag is used, which allocates all available system
    resources and skips the need for exact proc/memory configuration.  This is
    the most reliable approach for HMC firmware that enforces strict resource
    accounting.  Pass explicit values to override individual resources.

    Returns the raw ``mksyscfg`` stdout (typically empty on success).
    Raises :class:`HMCCLIError` on non-zero exit, and before any command when
    more than one virtual processor is requested without processing units,
    when the guessed ``max_proc_units`` default would exceed the requested
    max vCPUs, when explicit shared processing units exceed their level's
    vCPUs, or a request carries a fractional dedicated count or the other
    processor mode's ``sharing_mode``. Omitted values take the defaults of
    :func:`complete_create_resources`, which the REST create uses too.
    """
    if reporting_settings is not None:
        reporting_settings.validate_context("create", partition_type)
    config_pairs: list[tuple[str, object]] = [
        ("name", name),
        ("lpar_env", _lpar_environment(partition_type)),
        ("profile_name", profile_name),
    ]
    config_pairs.extend(_lpar_resource_pairs(resources, max_virtual_slots))
    if partition_id is not None:
        config_pairs.append(("lpar_id", partition_id))
    if settings is not None:
        config_pairs.extend(settings.pairs())
    if reporting_settings is not None:
        config_pairs.extend(reporting_settings.pairs())

    # Two guards at two layers, neither substituting for the other:
    # build_attribute_record keeps the record's own ',' and '=' delimiters
    # meaningful to the HMC's parser, which splits the record itself; shlex.quote
    # keeps the whole record one word for the remote shell, which runs first and
    # strips the quotes before the HMC ever sees the text.
    config_str = build_attribute_record(config_pairs)
    extra = creation_io_record(physical_io or LparPhysicalIO(), creation_sriov)
    if extra:
        config_str += "," + extra
    cmd = f"mksyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(config_str)}"
    try:
        return await run_hmc_command(config, cmd)
    except HMCCLIError as error:
        raise HMCCLIError(
            f"Creation outcome unknown for {name!r}: {error}. Inspect whether the partition exists before retrying; ownership stamping has not run."
        ) from error


async def apply_lpar_profile_via_cli(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    profile_name: str = DEFAULT_PROFILE_NAME,
) -> str:
    """Apply *profile_name* to *lpar_name* without powering it on (#939).

    ``mksyscfg`` writes only a partition profile; until it is applied or the
    partition is activated, the partition has no current configuration and
    REST adapter writes fail (``REST0269``). ``-p`` names the partition and
    ``-n`` the profile; ``-f`` would name a file. Raises :class:`HMCCLIError`
    on a non-zero exit or a transport failure.
    """
    cmd = (
        f"chsyscfg -r lpar -m {shlex.quote(system_name)} -o apply "
        f"-p {shlex.quote(lpar_name)} -n {shlex.quote(profile_name)}"
    )
    return await run_hmc_command(config, cmd)


def _lpar_environment(partition_type: str) -> str:
    """Return the HMC environment identifier for a requested partition type."""
    normalized = partition_type.lower()
    if "ios" in normalized or "vios" in normalized:
        return "vioserver"
    if "os400" in normalized or "ibmi" in normalized or normalized == "i":
        return "os400"
    return "aixlinux"


def complete_create_resources(resources: LparResources) -> LparResources | None:
    """Complete a create's resources with the defaults both create paths use (#1164).

    Returns ``None`` when no memory or processor value is given: only
    ``mksyscfg``'s ``all_resources=1`` expresses that. Otherwise every memory
    and processor field the V10R3 create requires is filled -- its REST create
    refuses a document without them (``REST0126``), and ``mksyscfg`` has no
    default for a dedicated ``sharing_mode``. Raises :class:`HMCCLIError` for a
    fractional dedicated count, a sharing mode of the other processor mode, or
    processing units -- defaulted or explicit -- the requested virtual processors
    cannot use.
    """
    mode = resources.sharing_mode
    if mode is not None and (not isinstance(mode, str) or mode not in SHARING_MODES):
        raise ValueError(
            f"sharing_mode must be one of: {', '.join(sorted(SHARING_MODES))}"
        )
    if all(getattr(resources, name) is None for name in _RESOURCE_FIELDS):
        return None
    processors = (
        _dedicated_processors(resources)
        if resources.dedicated
        else _shared_processors(resources)
    )
    des_mem = resources.desired_memory or 4096
    return replace(
        processors,
        min_memory=resources.min_memory or 256,
        desired_memory=des_mem,
        max_memory=resources.max_memory or max(des_mem, 8192),
    )


_RESOURCE_FIELDS = (
    "min_memory",
    "desired_memory",
    "max_memory",
    "min_procs",
    "desired_procs",
    "max_procs",
    "min_vcpus",
    "desired_vcpus",
    "max_vcpus",
)


def _lpar_resource_pairs(
    resources: LparResources, max_virtual_slots: int | None
) -> list[tuple[str, object]]:
    """Build the complete HMC resource record or its all-resources variant."""
    complete = complete_create_resources(resources)
    if complete is None:
        all_pairs: list[tuple[str, object]] = [("all_resources", 1)]
        if max_virtual_slots is not None:
            all_pairs.append(("max_virtual_slots", max_virtual_slots))
        return all_pairs
    pairs: list[tuple[str, object]] = [
        ("min_mem", complete.min_memory),
        ("desired_mem", complete.desired_memory),
        ("max_mem", complete.max_memory),
    ]
    if complete.dedicated:
        pairs.extend(
            (
                ("proc_mode", "ded"),
                ("min_procs", complete.min_procs),
                ("desired_procs", complete.desired_procs),
                ("max_procs", complete.max_procs),
                ("sharing_mode", complete.sharing_mode),
            )
        )
    else:
        pairs.extend(
            (
                ("proc_mode", "shared"),
                ("sharing_mode", "uncap" if complete.uncapped else "cap"),
                ("min_proc_units", complete.min_procs),
                ("desired_proc_units", complete.desired_procs),
                ("max_proc_units", complete.max_procs),
                ("min_procs", complete.min_vcpus),
                ("desired_procs", complete.desired_vcpus),
                ("max_procs", complete.max_vcpus),
            )
        )
    if max_virtual_slots is not None:
        pairs.append(("max_virtual_slots", max_virtual_slots))
    return pairs


def _dedicated_processors(resources: LparResources) -> LparResources:
    """Whole-CPU fields of a dedicated create, with its sharing-mode default.

    Virtual-processor fields are shared-mode only and are dropped. An explicit
    ``sharing_mode`` must be one of the dedicated ``*_idle_procs`` values.
    """
    _min = _whole_processors(resources.min_procs, "min_procs", "--min-procs") or 1
    _des = _whole_processors(resources.desired_procs, "desired_procs", "--procs") or 1
    _max = _whole_processors(resources.max_procs, "max_procs", "--max-procs") or max(
        _des, 2
    )
    mode = resources.sharing_mode or "keep_idle_procs"
    if mode not in _DEDICATED_SHARING_MODES:
        raise HMCCLIError(
            f"sharing_mode={mode!r} applies to shared processors only; with "
            f"dedicated processors use one of: {', '.join(_DEDICATED_SHARING_MODES)}."
        )
    return LparResources(
        dedicated=True,
        min_procs=_min,
        desired_procs=_des,
        max_procs=_max,
        sharing_mode=mode,
    )


def _whole_processors(value: float | None, field: str, option: str) -> int | None:
    """Return a dedicated processor count as an ``int``, refusing a fraction."""
    if value is None:
        return None
    if not float(value).is_integer():
        raise HMCCLIError(
            f"dedicated processors are whole CPUs: {field}={value} is fractional "
            f"(pass a whole number to {option} on the CLI)."
        )
    return int(value)


def _shared_processors(resources: LparResources) -> LparResources:
    """Processing-unit and virtual-processor fields of a shared create.

    Uncapped unless ``uncapped=False`` or ``sharing_mode='capped'`` asks otherwise.
    """
    _min_pu = resources.min_procs or 0.1
    _des_pu = resources.desired_procs or 0.1
    _max_pu = resources.max_procs or max(_des_pu, 2.0)
    _min_vp, _des_vp, _max_vp = shared_vcpu_defaults(resources)
    _require_units_for_vcpus(resources.min_procs, _min_vp, "min_procs", "--min-procs")
    _require_units_for_vcpus(
        resources.desired_procs, _des_vp, "desired_procs", "--procs"
    )
    _require_max_units_fit_vcpus(resources.max_procs, _max_pu, _max_vp)
    if refusal := shared_units_over_vcpus(resources):
        raise HMCCLIError(refusal)
    if resources.sharing_mode in _DEDICATED_SHARING_MODES:
        raise HMCCLIError(
            f"sharing_mode={resources.sharing_mode!r} applies to dedicated processors "
            "only; with shared processors use capped or uncapped."
        )
    capped = resources.uncapped is False or (
        resources.uncapped is None and resources.sharing_mode == "capped"
    )
    return LparResources(
        dedicated=False,
        min_procs=_min_pu,
        desired_procs=_des_pu,
        max_procs=_max_pu,
        min_vcpus=_min_vp,
        desired_vcpus=_des_vp,
        max_vcpus=_max_vp,
        sharing_mode="capped" if capped else "uncapped",
        uncapped=not capped,
    )


def _require_units_for_vcpus(
    units: float | None, vcpus: int, field: str, option: str
) -> None:
    """Refuse the 0.1 processing-unit default for more than one virtual processor.

    The HMC rejects 0.1 units spread over several virtual processors
    (HSCL0622, #938). The per-processor minimum is platform-specific and not
    recorded in this repository, so no default is derived from the count.
    """
    if not units and vcpus > 1:
        raise HMCCLIError(
            f"{vcpus} virtual processors need explicit processing units: pass "
            f"{field} ({option} on the CLI). The 0.1-unit default covers one "
            "virtual processor only."
        )


def _require_max_units_fit_vcpus(
    max_units: float | None, computed_max: float, max_vcpus: int
) -> None:
    """Refuse a guessed ``max_proc_units`` default a virtual processor cannot use.

    A virtual processor can use at most 1.0 processing unit, so
    ``max(desired, 2.0)`` is only a safe guess when ``max_vcpus`` covers it.
    Mirrors :func:`_require_units_for_vcpus`'s refuse-rather-than-guess stance
    for #938, applied to the opposite (upper) bound (#949). A large
    ``max_vcpus`` is not refused here: the per-processor *minimum* ratio is
    platform-specific and not recorded in this repository (#938), so no
    default is derived from it.
    """
    if max_units is None and computed_max > max_vcpus:
        raise HMCCLIError(
            f"{max_vcpus} maximum virtual processors cannot use "
            f"{computed_max} maximum processing units: pass max_procs "
            "(--max-procs on the CLI). A virtual processor uses at most 1.0 "
            "processing unit."
        )


# UUID -> CLI-name lookup (SSH fallback for the REST-based resolvers)


async def resolve_system_cli_name(config: HMCConfig, system_uuid: str) -> str:
    """Look up a managed-system UUID's CLI name over SSH.

    Runs ``lssyscfg -r sys -F uuid,name`` and returns the row whose
    UUID column matches. Used as the fallback by the REST-based system-name
    resolver in :mod:`hmcpctl._app` when the REST API is unreachable.

    Raises:
        HMCCLIError: If no row matches *system_uuid* in the command output.
    """
    raw = await run_hmc_command(config, "lssyscfg -r sys -F uuid,name")
    return _match_uuid_name(raw, system_uuid, "system")


async def resolve_lpar_cli_name(
    config: HMCConfig,
    lpar_uuid: str,
    system_name: str | None = None,
) -> str:
    """Look up an LPAR UUID's CLI name over SSH.

    Runs ``lssyscfg -r lpar -m <system> -F uuid,name`` for *system_name*, or,
    when it is omitted, for each system ``lssyscfg -r sys -F name`` lists:
    ``lssyscfg -r lpar`` requires ``-m`` and exits 1 without it. A listed
    system whose partition listing fails is skipped, so one unreachable
    system does not end the search. Used as the fallback by the REST-based
    LPAR-name resolver in :mod:`hmcpctl._app` when the REST API is unreachable.

    Raises:
        HMCCLIError: If no row matches *lpar_uuid*; the message names each
            skipped system and its failure. A failure listing *system_name*,
            when given, is raised as it is.
    """
    if system_name:
        return _match_uuid_name(
            await _lpar_uuid_names(config, system_name), lpar_uuid, "LPAR"
        )
    listing = await run_hmc_command(config, "lssyscfg -r sys -F name")
    outputs: list[str] = []
    skipped: list[str] = []
    for system in (line.strip() for line in listing.splitlines()):
        if not system:
            continue
        try:
            outputs.append(await _lpar_uuid_names(config, system))
        except HMCCLIError as error:
            skipped.append(f"{system!r} ({error})")
    try:
        return _match_uuid_name("\n".join(outputs), lpar_uuid, "LPAR")
    except HMCCLIError as error:
        if not skipped:
            raise
        raise HMCCLIError(
            f"{error} These systems could not be searched: {'; '.join(skipped)}"
        ) from error


async def _lpar_uuid_names(config: HMCConfig, system_name: str) -> str:
    return await run_hmc_command(
        config, f"lssyscfg -r lpar -m {shlex.quote(system_name)} -F uuid,name"
    )


def _match_uuid_name(raw: str, uuid: str, what: str) -> str:
    """Return the name on the ``uuid,name`` line matching *uuid*.

    Non-matching lines are skipped; a matching line with an empty name column
    (malformed row) is not returned. UUIDs compare case-insensitively: V10R3
    prints LPAR UUIDs in upper case and system UUIDs in lower case.
    """
    for line in raw.splitlines():
        row_uuid, _, name = line.partition(",")
        if row_uuid.strip().lower() == uuid.lower():
            name = name.strip()
            if name:
                return name
    raise HMCCLIError(
        f"Could not resolve {what} UUID {uuid!r} to a CLI name over SSH. "
        "No matching row in the lssyscfg uuid,name output."
    )


async def get_lpar_settings(
    config: HMCConfig, system_name: str, lpar_name: str
) -> LparSettings:
    """Read one AIX/Linux or IBM i partition's five C01 settings, strictly."""
    names = tuple(item.name for item in fields(LparSettings))
    selector = build_filter([("lpar_names", lpar_name)])
    raw = await run_hmc_command(
        config,
        f"lssyscfg -r lpar -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(selector)} -F lpar_env,{','.join(names)}",
    )
    try:
        rows = list(csv.reader(raw.strip().splitlines(), strict=True))
        if len(rows) != 1 or len(rows[0]) != 6:
            raise ValueError("expected exactly one six-field partition row")
        environment, *values = rows[0]
        if environment not in {"aixlinux", "os400"}:
            raise ValueError("C01 settings require an AIX/Linux or IBM i partition")
        for name, value in zip(names, values, strict=True):
            if name == "lpar_avail_priority":
                if re.fullmatch(r"[0-9]+", value) is None:
                    raise ValueError("priority must be a decimal integer")
            else:
                if value not in {"0", "1"}:
                    raise ValueError(f"{name} must read as 0 or 1")
        return LparSettings(
            values[0] == "1",
            int(values[1]),
            values[2] == "1",
            values[3] == "1",
            values[4] == "1",
        )
    except (ValueError, csv.Error) as error:
        raise HMCCLIError(
            f"Cannot read partition settings: {error}; inspect lssyscfg output before retrying"
        ) from error


async def set_lpar_settings(
    config: HMCConfig, system_name: str, lpar_name: str, settings: LparSettings
) -> None:
    """Write only the supplied canonical C01 fields on one named partition."""
    pairs = settings.pairs()
    if not pairs:
        raise ValueError("Supply at least one partition setting")
    record = build_attribute_record([("name", lpar_name), *pairs])
    await run_hmc_command(
        config,
        f"chsyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(record)}",
    )


async def get_lpar_reporting_settings(
    config: HMCConfig,
    system_name: str,
    lpar_name: str,
    *,
    creation: bool = False,
    require_ibmi: bool = False,
) -> LparReportingSettings:
    """Strict applicable partition readback; creation includes creation-only fields."""
    selector = build_filter([("lpar_names", lpar_name)])
    command = (
        f"lssyscfg -r lpar -m {shlex.quote(system_name)} "
        f"--filter {shlex.quote(selector)} -F "
    )
    environment = (await run_hmc_command(config, command + "lpar_env")).strip()
    if environment not in {"aixlinux", "os400"} or (
        require_ibmi and environment != "os400"
    ):
        raise HMCCLIError(
            "Cannot read reporting settings: expected one applicable partition; IBM i is required for IPL, restricted I/O and electronic reporting"
        )
    names = [
        item.name
        for item in fields(LparReportingSettings)
        if item.name != ("ipl_source" if creation else "redundant_err_path_reporting")
        and (
            environment == "os400"
            or item.name in {"work_group_id", "redundant_err_path_reporting"}
        )
    ]
    raw = await run_hmc_command(config, command + "lpar_env," + ",".join(names))
    try:
        rows = list(csv.reader(raw.strip().splitlines(), strict=True))
        if (
            len(rows) != 1
            or len(rows[0]) != len(names) + 1
            or rows[0][0] != environment
        ):
            raise ValueError(
                "expected one complete row with unchanged partition environment"
            )
        values: dict[str, object] = {}
        for name, value in zip(names, rows[0][1:], strict=True):
            if name == "work_group_id":
                if value != "none" and re.fullmatch(r"-?[0-9]+", value) is None:
                    raise ValueError("work_group_id must read as an integer or none")
                values[name] = value if value == "none" else int(value)
            elif name == "ipl_source":
                values[name] = value
            else:
                if value not in {"0", "1"}:
                    raise ValueError(f"{name} must read as 0 or 1")
                values[name] = value == "1"
        return TypeAdapter(LparReportingSettings).validate_python(values)
    except (ValueError, TypeError, csv.Error) as error:
        raise HMCCLIError(
            f"Cannot read reporting settings: {error}; inspect before retrying"
        ) from error


async def set_lpar_reporting_settings(
    config: HMCConfig, system_name: str, lpar_name: str, settings: LparReportingSettings
) -> None:
    """Write one fixed C02 partition-update record; owning operation admits type."""
    settings.validate_context("update")
    if not settings.pairs():
        raise ValueError("Supply at least one reporting setting")
    record = build_attribute_record([("name", lpar_name), *settings.pairs()])
    await run_hmc_command(
        config,
        f"chsyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(record)}",
    )
