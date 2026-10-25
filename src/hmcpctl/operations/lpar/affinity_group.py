"""Contained, separately labelled native affinity observations after creation."""

import re
import shlex
from typing import Any

from ...client.core import HMCClient
from ...documents.lpar_affinity_group import AffinityGroupID, validate_affinity_group_id
from ...errors import HMCError
from ...resource_identity import (
    is_uuid,
    resolve_lpar_uuid,
    resolve_system_name,
    resolve_vios_uuid,
)
from ...ssh.commands import build_filter, parse_hmc_result_rows
from ...ssh.lpar import DEFAULT_PROFILE_NAME
from ...ssh.transport import run_hmc_command
from ...xmlutil import leaf_text
from .workflow_contract import WorkflowStep


async def created_affinity_group_step(
    hmc: HMCClient,
    system_uuid: str,
    entry: dict[str, Any] | None,
    name: str,
    environment: str,
    affinity_group_id: AffinityGroupID | None,
    *,
    controller_mode: bool = False,
    vios: bool = False,
) -> WorkflowStep | None:
    """Observe the selected partition and, separately, its classic named profile."""
    validate_affinity_group_id(affinity_group_id)
    if affinity_group_id is None:
        return None
    observed: dict[str, int | str | None] = {"native-partition": None}
    if not controller_mode:
        observed["native-profile"] = None
    try:
        uuid = (entry or {}).get("UUID")
        initial = (entry or {}).get("Resource")
        if (
            not isinstance(uuid, str)
            or not is_uuid(uuid)
            or not isinstance(initial, dict)
            or leaf_text(initial.get("PartitionName")) != name
            or environment not in ("AIX/Linux", "OS400", "Virtual IO Server")
            or (environment == "Virtual IO Server") != vios
        ):
            raise ValueError("Created affinity identity unavailable")
        resolver = resolve_vios_uuid if vios else resolve_lpar_uuid
        await resolver(hmc, uuid, system_name_or_uuid=system_uuid)
        fresh = (
            await hmc.get_vios(uuid) if vios else await hmc.get_logical_partition(uuid)
        )
        resource = (fresh or {}).get("Resource")
        if (
            not isinstance(resource, dict)
            or str((fresh or {}).get("UUID", "")).casefold() != uuid.casefold()
            or leaf_text(resource.get("PartitionName")) != name
            or leaf_text(resource.get("PartitionType")) != environment
        ):
            raise ValueError("Created affinity identity changed")
        partition_id = leaf_text(resource.get("PartitionID"))
        if (
            not isinstance(partition_id, str)
            or re.fullmatch(r"[1-9][0-9]*", partition_id) is None
        ):
            raise ValueError("Created affinity numeric identity unavailable")
        system = await resolve_system_name(hmc, system_uuid)
        native_env = {
            "AIX/Linux": "aixlinux",
            "OS400": "os400",
            "Virtual IO Server": "vioserver",
        }[environment]
        for label in observed:
            profile = label == "native-profile"
            filters: list[tuple[str, object]] = [("lpar_ids", partition_id)]
            expected = {"lpar_id": partition_id, "name": name, "lpar_env": native_env}
            if profile:
                filters.append(("profile_names", DEFAULT_PROFILE_NAME))
                expected = {
                    "lpar_id": partition_id,
                    "lpar_name": name,
                    "name": DEFAULT_PROFILE_NAME,
                    "lpar_env": native_env,
                }
            fields = (*expected, "affinity_group_id")
            raw = await run_hmc_command(
                hmc.config,
                f"lssyscfg -r {'prof' if profile else 'lpar'} -m {shlex.quote(system)} "
                f"--filter {shlex.quote(build_filter(filters))} -F {','.join(fields)} --header",
            )
            records = raw.splitlines()
            if len(records) != 2 or records[0] != ",".join(fields):
                raise ValueError(
                    "Affinity observation requires one exact header and row"
                )
            rows = parse_hmc_result_rows(raw, fields, "created affinity group")
            if len(rows) != 1 or any(
                rows[0][field] != value for field, value in expected.items()
            ):
                raise ValueError("Affinity observation identity unavailable")
            value = rows[0]["affinity_group_id"]
            if value == "none":
                observed[label] = value
            elif re.fullmatch(r"[1-9][0-9]{0,2}", value) and int(value) <= 255:
                observed[label] = int(value)
            else:
                raise ValueError("Affinity observation domain unavailable")
    except (HMCError, OSError, ValueError, TypeError, PermissionError):
        pass
    status = "readback-unavailable"
    if all(value is not None for value in observed.values()):
        status = (
            "verified"
            if all(value == affinity_group_id for value in observed.values())
            else "mismatch"
        )
    result: dict[str, Any] = {
        "status": status,
        "requested": affinity_group_id,
        "observed": observed,
    }
    if status != "verified":
        result["message"] = (
            "Partition exists or may exist; affinity group unverified. Inspect before retrying; no rollback performed."
        )
    return WorkflowStep(
        "affinity_group", "ok" if status == "verified" else "error", result
    )
