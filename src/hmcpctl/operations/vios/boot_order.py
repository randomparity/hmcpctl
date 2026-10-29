"""VIOS pending boot text through a fixed CSV writer and independent native reader."""

import csv
import io
import re
import shlex

from ...client.core import HMCClient
from ...documents.boot import select_boot_string
from ...errors import HMCError
from ...resource_identity import (
    is_uuid,
    resolve_system_name,
    resolve_system_uuid,
    resolve_vios_uuid,
)
from ...ssh.commands import build_filter
from ...ssh.transport import run_hmc_command
from ...xmlutil import leaf_text
from ..lpar.ownership import authorize_lpar_ownership_description


async def _target(hmc, selector, system, *, authorize=False, ownership_override=False):
    system_uuid = await resolve_system_uuid(hmc, system)
    uuid = await resolve_vios_uuid(hmc, selector, system_name_or_uuid=system_uuid)
    entry = await hmc.get_vios(uuid)
    resource = (entry or {}).get("Resource")
    if (
        not isinstance(resource, dict)
        or (entry or {}).get("ResourceType") != "VirtualIOServer"
        or not is_uuid(uuid)
        or str((entry or {}).get("UUID", "")).casefold() != uuid.casefold()
    ):
        raise HMCError("Contained VIOS boot identity unavailable")
    name = leaf_text(resource.get("PartitionName"))
    native = leaf_text(resource.get("PartitionID"))
    description = leaf_text(resource.get("Description"))
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(native, str)
        or not re.fullmatch(r"[1-9][0-9]*", native)
        or not isinstance(description, str)
        or leaf_text(resource.get("PartitionType")) != "Virtual IO Server"
    ):
        raise HMCError("Contained VIOS boot identity unavailable")
    uuid = entry["UUID"]
    system_name = await resolve_system_name(hmc, system_uuid)
    if authorize:
        authorize_lpar_ownership_description(
            hmc,
            system_name,
            name,
            description,
            operation="lpar-mutation",
            ownership_override=ownership_override,
        )
    return system_name, {
        "uuid": uuid,
        "name": name,
        "lpar_id": native,
        "description": description,
    }


def _parse_header(raw, identity):
    if (
        not isinstance(raw, str)
        or len(raw.encode()) > 65536
        or "\ufffd" in raw
        or "[stderr]" in raw
        or any(not c.isprintable() and c not in "\r\n" for c in raw)
    ):
        raise ValueError("Invalid native boot observation")
    rows = list(csv.reader(io.StringIO(raw, newline=""), strict=True))
    required = {
        "name",
        "lpar_id",
        "lpar_env",
        "uuid",
        "description",
        "pend_boot_string",
    }
    if (
        len(rows) != 2
        or not rows[0]
        or any(not key for key in rows[0])
        or len(set(rows[0])) != len(rows[0])
        or len(rows[0]) != len(rows[1])
        or not required.issubset(rows[0])
    ):
        raise ValueError("Complete unique native header and one row required")
    if any(not c.isprintable() for row in rows for cell in row for c in cell):
        raise ValueError("Native boot cells contain controls")
    cells = dict(zip(*rows, strict=True))
    if (
        not is_uuid(cells["uuid"])
        or cells["uuid"].casefold() != identity["uuid"].casefold()
        or any(cells[k] != identity[k] for k in ("name", "lpar_id", "description"))
        or cells["lpar_env"] != "vioserver"
    ):
        raise ValueError("Native boot identity differs")
    return cells["pend_boot_string"] or None


async def _read(hmc, system, identity):
    query = build_filter([("lpar_ids", identity["lpar_id"])])
    command = f"lssyscfg -r lpar -m {shlex.quote(system)} --filter {shlex.quote(query)} -F --header"
    try:
        raw = await run_hmc_command(
            hmc.config, command, include_stderr=True, max_output_bytes=65536
        )
        return _parse_header(raw, identity)
    except (HMCError, OSError, ValueError, TypeError, csv.Error, UnicodeError):
        raise HMCError(
            "VIOS pending boot observation unavailable; inspect before retrying"
        ) from None


async def read_vios_boot_order(
    hmc: HMCClient, vios_name_or_uuid: str, *, system_name_or_uuid: str
) -> dict:
    system, identity = await _target(hmc, vios_name_or_uuid, system_name_or_uuid)
    text = await _read(hmc, system, identity)
    return {
        "vios_uuid": identity["uuid"],
        "vios_name": identity["name"],
        "pending_boot_string": text,
    }


def _encode_boot_record(partition_id: int, text: str) -> str:
    """One fixed native dialect; raw input's application limit belongs to its caller."""
    if type(partition_id) is not int or partition_id <= 0:
        raise ValueError("Positive contained partition ID required")
    if (
        not isinstance(text, str)
        or not text
        or not text.isprintable()
        or not text.strip()
    ):
        raise ValueError("Printable nonempty native boot text required")
    text.encode("utf-8")
    out = io.StringIO(newline="")
    csv.writer(
        out,
        delimiter=",",
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
        doublequote=True,
        escapechar=None,
        lineterminator="\n",
    ).writerow([f"lpar_id={partition_id}", f"boot_string={text}"])
    return out.getvalue()[:-1]


async def set_vios_boot_order(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    devices: list[str] | None = None,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
    boot_string: str | None = None,
) -> dict:
    requested = select_boot_string(devices, boot_string)
    system, identity = await _target(
        hmc,
        vios_name_or_uuid,
        system_name_or_uuid,
        authorize=True,
        ownership_override=ownership_override,
    )
    record = _encode_boot_record(int(identity["lpar_id"]), requested)
    # Identity and ownership are rechecked after composition, immediately before submission.
    current = await _target(
        hmc,
        identity["uuid"],
        system_name_or_uuid,
        authorize=True,
        ownership_override=ownership_override,
    )
    if current != (system, identity):
        raise HMCError("VIOS boot identity changed; nothing was submitted")
    try:
        raw = await run_hmc_command(
            hmc.config,
            f"chsyscfg -r lpar -m {shlex.quote(system)} -i {shlex.quote(record)}",
            include_stderr=True,
            secrets=(requested, record),
            max_output_bytes=65536,
        )
        if not isinstance(raw, str) or len(raw.encode()) > 65536 or "[stderr]" in raw:
            raise ValueError("Native acknowledgment unavailable")
    except (HMCError, OSError, ValueError, TypeError, UnicodeError):
        raise HMCError(
            "VIOS pending boot write unverified; inspect before retrying. No retry or rollback performed"
        ) from None
    current = await _target(hmc, identity["uuid"], system_name_or_uuid)
    if current != (system, identity):
        raise HMCError(
            "VIOS pending boot verification identity changed; inspect before retrying"
        )
    observed = await _read(hmc, system, identity)
    if observed != requested:
        raise HMCError(
            "VIOS pending boot verification mismatch; inspect before retrying"
        )
    return {
        "vios_uuid": identity["uuid"],
        "vios_name": identity["name"],
        "pending_boot_string": observed,
    }
