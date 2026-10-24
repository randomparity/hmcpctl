"""Fixed vTPM advertisement and partition configuration commands (C03)."""

from __future__ import annotations

import csv
import io
import shlex

from ..config import HMCConfig
from ..documents.lpar_vtpm import LparVtpmSettings, VtpmState
from .commands import build_attribute_record, build_filter
from .transport import HMCCLIError, run_hmc_command


def _row(raw: str, width: int) -> list[str]:
    rows = list(csv.reader(io.StringIO(raw, newline=""), strict=True))
    if len(rows) != 1 or len(rows[0]) != width:
        raise ValueError(f"expected exactly one complete {width}-field CSV row")
    return rows[0]


def _advertised(value: str, name: str) -> tuple[str, ...]:
    if value == "null":
        return ()
    members = tuple(value.split(","))
    if not value or len(set(members)) != len(members):
        raise ValueError("advertisement must contain distinct nonempty members")
    for member in members:
        if name == "vtpm_version":
            LparVtpmSettings(vtpm_version=member)
        else:
            LparVtpmSettings(vtpm_encryption=member)
    return members


async def get_vtpm_advertisement(
    config: HMCConfig, system_name: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read the selected system's own version/level lists; null is unavailable."""
    raw = await run_hmc_command(
        config,
        f"lssyscfg -r sys -m {shlex.quote(system_name)} "
        "-F supported_vtpm_versions,supported_vtpm_encryptions",
    )
    try:
        versions, encryptions = _row(raw, 2)
        return _advertised(versions, "vtpm_version"), _advertised(
            encryptions, "vtpm_encryption"
        )
    except (ValueError, TypeError, csv.Error) as error:
        raise HMCCLIError(
            f"Cannot read vTPM advertisement: {error}; inspect the selected system before retrying"
        ) from error


async def get_lpar_vtpm_state(
    config: HMCConfig, system_name: str, name: str, *, vios: bool = False
) -> VtpmState:
    """Read one exact contained target; callers select the proper resource kind."""
    selector = build_filter([("lpar_names", name)])
    raw = await run_hmc_command(
        config,
        f"lssyscfg -r lpar -m {shlex.quote(system_name)} --filter {shlex.quote(selector)} "
        "-F name,lpar_env,state,vtpm_enabled,vtpm_version,vtpm_encryption",
    )
    try:
        observed_name, environment, state, enabled, version, encryption = _row(raw, 6)
        expected = "vioserver" if vios else "aixlinux"
        if observed_name != name or environment != expected:
            raise ValueError(
                f"expected the selected {expected} partition and unchanged name"
            )
        if not state or state != state.strip() or any(ord(c) < 32 for c in state):
            raise ValueError("partition state is missing or malformed")
        if enabled not in {"0", "1"}:
            raise ValueError("vtpm_enabled must read as 0 or 1")
        settings = LparVtpmSettings(
            enabled == "1",
            None if version == "null" else version,
            None if encryption == "null" else encryption,
        )
        return VtpmState(expected, state, settings)
    except (ValueError, TypeError, csv.Error) as error:
        raise HMCCLIError(
            f"Cannot read vTPM settings: {error}; inspect before retrying"
        ) from error


async def set_lpar_vtpm_settings(
    config: HMCConfig, system_name: str, name: str, settings: LparVtpmSettings
) -> None:
    """Submit one canonical record; admission/consent belong to the owning operation."""
    if not settings.pairs():
        raise ValueError("Supply at least one vTPM setting")
    record = build_attribute_record([("name", name), *settings.pairs()])
    await run_hmc_command(
        config,
        f"chsyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(record)}",
    )
