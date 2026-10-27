"""Independent saved affinity proof and bounded native observations after creation."""

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
    """Verify saved classic configuration and report native observation separately."""
    validate_affinity_group_id(affinity_group_id)
    if affinity_group_id is None:
        return None
    observed: dict[str, int | str | None] = {"native-partition": None}
    if not controller_mode:
        observed["native-profile"] = None
    identity_confirmed = False
    identity_conflict = False
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
        if (
            not controller_mode
            and leaf_text(resource.get("DefaultProfileName")) != DEFAULT_PROFILE_NAME
        ):
            raise ValueError("Created affinity default profile identity unavailable")
        system = await resolve_system_name(hmc, system_uuid)
        native_env = {
            "AIX/Linux": "aixlinux",
            "OS400": "os400",
            "Virtual IO Server": "vioserver",
        }[environment]
        identity_confirmed = True
    except (HMCError, OSError, ValueError, TypeError, PermissionError):
        pass
    if identity_confirmed:
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
            try:
                raw = await run_hmc_command(
                    hmc.config,
                    f"lssyscfg -r {'prof' if profile else 'lpar'} -m {shlex.quote(system)} "
                    f"--filter {shlex.quote(build_filter(filters))} -F {','.join(fields)} --header",
                )
                records = raw.splitlines()
                if not records or records[0] != ",".join(fields):
                    raise ValueError(
                        "Affinity observation requires one exact header and row"
                    )
                rows = parse_hmc_result_rows(raw, fields, "created affinity group")
                if len(rows) != 1 or any(
                    rows[0][field] != value for field, value in expected.items()
                ):
                    identity_conflict = True
                    raise ValueError("Affinity observation identity unavailable")
                if len(records) != 2:
                    raise ValueError(
                        "Affinity observation requires one exact header and row"
                    )
                value = rows[0]["affinity_group_id"]
                if value == "none":
                    observed[label] = value
                elif re.fullmatch(r"[1-9][0-9]{0,2}", value) and int(value) <= 255:
                    observed[label] = int(value)
                else:
                    raise ValueError("Affinity observation domain unavailable")
            except (HMCError, OSError, ValueError, TypeError, PermissionError):
                continue
    observation_status = {
        label: "readback-unavailable"
        if value is None
        else "matches-request"
        if value == affinity_group_id
        else "mismatch"
        for label, value in observed.items()
    }
    status = "readback-unavailable"
    if identity_confirmed and not identity_conflict and not controller_mode:
        saved_status = observation_status["native-profile"]
        if saved_status != "readback-unavailable":
            status = (
                "saved-configuration-verified"
                if saved_status == "matches-request"
                else "mismatch"
            )
    verified = status == "saved-configuration-verified"
    message = (
        "Saved affinity configuration verified; native partition observation is separate. "
        "Current/pending semantics and physical placement are unproved"
        if verified
        else "Partition exists or may exist; saved affinity configuration unverified. "
        "Inspect before retrying; no rollback performed."
    )
    if controller_mode:
        message += " Controller saved-proof source is unavailable."
    result: dict[str, Any] = {
        "status": status,
        "requested": affinity_group_id,
        "observed": observed,
        "observation_status": observation_status,
        "message": message,
    }
    return WorkflowStep("affinity_group", "ok" if verified else "error", result)
