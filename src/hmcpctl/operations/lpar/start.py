"""Contained power-controller admission and creation-only configuration observations."""

import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from ...client.core import HMCClient
from ...documents.lpar_start import LparStartSettings
from ...resource_identity import is_uuid
from ...xmlutil import leaf_text
from .ownership import authorize_lpar_ownership_description

PartitionKind = Literal["LogicalPartition", "VirtualIOServer"]


@dataclass(frozen=True)
class StartController:
    kind: PartitionKind
    uuid: str
    name: str
    partition_id: int


@dataclass(frozen=True)
class StartAdmission:
    system_uuid: str
    system_name: str
    settings: LparStartSettings
    controllers: tuple[StartController, ...]


def _controller(kind: PartitionKind, entry: Any) -> StartController:
    if not isinstance(entry, dict) or entry.get("ResourceType") != kind:
        raise ValueError("Power controller resource kind is unavailable")
    uuid, resource = entry.get("UUID"), entry.get("Resource")
    if not isinstance(uuid, str) or not is_uuid(uuid) or not isinstance(resource, dict):
        raise ValueError("Power controller identity is unavailable")
    name, native = (
        leaf_text(resource.get("PartitionName")),
        leaf_text(resource.get("PartitionID")),
    )
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(native, str)
        or not re.fullmatch(r"[1-9][0-9]*", native)
    ):
        raise ValueError("Power controller name/native identity is unavailable")
    return StartController(kind, uuid, name, int(native))


async def _inventory(hmc: HMCClient, system: str) -> tuple[StartController, ...]:
    targets = []
    for kind, rows in (
        ("LogicalPartition", await hmc.list_logical_partitions(system)),
        ("VirtualIOServer", await hmc.list_vios(system)),
    ):
        if not isinstance(rows, list):
            raise TypeError("Power controller contained inventory is unreadable")
        targets.extend(_controller(kind, row) for row in rows)
    for attr in ("uuid", "name", "partition_id"):
        values = [
            getattr(t, attr).casefold() if attr == "uuid" else getattr(t, attr)
            for t in targets
        ]
        if len(set(values)) != len(values):
            raise ValueError("Power controller contained inventory is ambiguous")
    return tuple(targets)


async def recheck_start_controllers(hmc: HMCClient, admission: StartAdmission) -> None:
    """Rebind and authorize selected controllers at the fresh effect/readback boundary."""
    if not admission.controllers:
        return
    inventory = await _inventory(hmc, admission.system_uuid)
    for target in admission.controllers:
        if target not in inventory:
            raise ValueError("Power controller left the contained inventory")
        entry = await hmc.get_uom(target.kind, target.uuid)
        if not isinstance(entry, dict) or _controller(target.kind, entry) != target:
            raise ValueError("Power controller identity changed")
        description = leaf_text(entry["Resource"].get("Description"))
        if not isinstance(description, str):
            raise TypeError("Power controller ownership description is unavailable")
        authorize_lpar_ownership_description(
            hmc,
            admission.system_name,
            target.name,
            description,
            operation="lpar-mutation",
        )


async def admit_start_creation(
    hmc: HMCClient, system_uuid: str, system_name: str, settings: LparStartSettings
) -> StartAdmission:
    if not isinstance(settings, LparStartSettings):
        raise TypeError("start_settings must be LparStartSettings")
    selectors = settings.power_ctrl_lpar_ids or settings.power_ctrl_lpar_names
    controllers = []
    if selectors:
        inventory = await _inventory(hmc, system_uuid)
        for selector in selectors:
            matches = [
                t
                for t in inventory
                if (t.partition_id if settings.power_ctrl_lpar_ids else t.name)
                == selector
            ]
            if len(matches) != 1:
                raise ValueError("Power controller selector is not uniquely contained")
            controllers.append(matches[0])
    resolved = replace(
        settings,
        power_ctrl_lpar_names=None,
        power_ctrl_lpar_ids=tuple(t.partition_id for t in controllers) or None,
    )
    admission = StartAdmission(system_uuid, system_name, resolved, tuple(controllers))
    await recheck_start_controllers(hmc, admission)
    return admission


async def confirm_start_created(
    hmc: HMCClient, admission: StartAdmission, entry: Any, name: str, environment: str
) -> dict[str, Any]:
    """Confirm a new resource before ownership stamping or any follow-up effect."""
    kind: PartitionKind = (
        "VirtualIOServer" if environment == "Virtual IO Server" else "LogicalPartition"
    )
    target = _controller(kind, entry)
    if target.name != name:
        raise ValueError("Created start identity differs")
    inventory = await _inventory(hmc, admission.system_uuid)
    if target not in inventory:
        raise ValueError("Created start identity is not contained")
    fresh = await hmc.get_uom(kind, target.uuid, group="Advanced")
    if (
        not isinstance(fresh, dict)
        or _controller(kind, fresh) != target
        or leaf_text(fresh["Resource"].get("PartitionType")) != environment
    ):
        raise ValueError("Created start identity changed")
    await recheck_start_controllers(hmc, admission)
    return fresh


def _observed_bool(value: object) -> bool:
    value = leaf_text(value)
    if value not in ("true", "false"):
        raise ValueError("Strict start Boolean observation unavailable")
    return value == "true"


def _power_set(
    hmc: HMCClient, value: object, admission: StartAdmission
) -> set[StartController]:
    from urllib.parse import urlsplit

    if value == "":
        return set()
    if (
        not isinstance(value, dict)
        or set(value) - {"link", "@attrs"}
        or "link" not in value
    ):
        raise ValueError("Recognized power-control collection unavailable")
    links = value["link"] if isinstance(value["link"], list) else [value["link"]]
    observed = set()
    for link in links:
        if not isinstance(link, dict):
            raise TypeError("Power-control link unavailable")
        href = link.get("href")
        if (
            not isinstance(href, str)
            or not href
            or not href.isprintable()
            or any(c.isspace() for c in href)
        ):
            raise TypeError("Power-control link unavailable")
        parsed = urlsplit(href)
        if (
            parsed.scheme != "https"
            or parsed.hostname != hmc.config.host
            or (parsed.port or 443) != hmc.config.port
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Power-control link is foreign or malformed")
        matches = [
            t
            for t in admission.controllers
            if parsed.path
            in {
                f"/rest/api/uom/{t.kind}/{t.uuid}",
                f"/rest/api/uom/ManagedSystem/{admission.system_uuid}/{t.kind}/{t.uuid}",
            }
        ]
        if len(matches) != 1 or matches[0] in observed:
            raise ValueError("Power-control link is ambiguous or not admitted")
        observed.add(matches[0])
    return observed


async def created_start_step(
    hmc: HMCClient,
    admission: StartAdmission,
    entry: Any,
    name: str,
    environment: str,
    *,
    controller_mode: bool = False,
):
    """Observe each requested field on exactly its approved required planes."""
    from ...errors import HMCError
    from .profiles import read_partition_profile
    from .workflow_contract import WorkflowStep

    observed: dict[str, object] = {}
    status = "unavailable"
    try:
        fresh = await confirm_start_created(hmc, admission, entry, name, environment)
        resource = fresh["Resource"]
        saved = None
        if not controller_mode:
            if leaf_text(resource.get("DefaultProfileName")) != "default_profile":
                raise ValueError("Initial start profile identity unavailable")
            profile = await read_partition_profile(
                hmc,
                admission.system_uuid,
                fresh["UUID"],
                "default_profile",
                partition_type="VirtualIOServer"
                if environment == "Virtual IO Server"
                else "LogicalPartition",
            )
            saved = profile.fields
        settings = admission.settings
        comparisons = []
        for native, saved_name, current_name, requested in (
            ("auto_start", "AutoStart", None, settings.auto_start),
            (
                "conn_monitoring",
                "ConnectionMonitoringEnabled",
                "IsConnectionMonitoringEnabled",
                settings.conn_monitoring,
            ),
            ("boot_mode", "BootMode", "BootMode", settings.boot_mode),
        ):
            if requested is None:
                continue
            planes = {"current": resource[current_name]} if current_name else {}
            if saved is not None:
                planes["saved"] = saved[saved_name]
            if not planes:
                raise ValueError("No established controller auto_start observer")
            for plane, value in planes.items():
                if native == "boot_mode":
                    assert isinstance(requested, str)
                    mapping = dict(
                        zip(
                            ("norm", "dd", "ds", "of", "sms"),
                            (
                                "Normal",
                                "Diagnostic_With_Default_Boot_List",
                                "Diagnostic_With_Stored_Boot_List",
                                "Open_Firmware",
                                "System_Management_Services",
                            ),
                            strict=True,
                        )
                    )
                    actual = leaf_text(value)
                    if actual not in mapping.values():
                        raise ValueError("Start boot mode observation unavailable")
                    equal = actual == mapping[requested]
                else:
                    actual = _observed_bool(value)
                    equal = actual == requested
                observed[f"{native}.{plane}"] = actual
                comparisons.append(equal)
        if admission.controllers:
            planes = {"current": resource["PowerControlPartitions"]}
            if saved is not None:
                planes["saved"] = saved["PowerControlPartitions"]
            for plane, value in planes.items():
                actual = _power_set(hmc, value, admission)
                observed[f"power_control.{plane}"] = sorted(
                    t.partition_id for t in actual
                )
                comparisons.append(actual == set(admission.controllers))
        status = "verified" if all(comparisons) else "mismatch"
    except (HMCError, ValueError, TypeError, KeyError, PermissionError, OSError):
        status = "unavailable"
    return WorkflowStep(
        "settings",
        "ok" if status == "verified" else "error",
        {
            "start_settings": {"status": status, "observed": observed},
        },
    )
