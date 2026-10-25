"""Selected-system admission and contained hardware access observations."""

from typing import Any, Literal
from xml.etree.ElementTree import Element  # nosec B405 — type annotations only

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from ...client.core import HMCClient
from ...documents.common import UOM_NS
from ...documents.lpar_hardware_memory import hardware_memory_pairs
from ...errors import HMCError
from ...resource_identity import is_uuid, resolve_lpar_uuid, resolve_vios_uuid
from ...xmlutil import ATOM_NS, leaf_text
from .workflow_contract import WorkflowStep

_FIELDS = (
    (
        "hardware_mem_encryption",
        "HardwareMemoryEncryptionCapable",
        "MemoryEncryptionHardwareAccessEnabled",
    ),
    (
        "hardware_mem_expansion",
        "HardwareMemoryCompressionCapable",
        "MemoryExpansionHardwareAccessEnabled",
    ),
)


def _one(parent: Element | None, name: str, namespace: str = UOM_NS) -> Element | None:
    if parent is None:
        return None
    matches = [child for child in parent if child.tag.rsplit("}", 1)[-1] == name]
    if len(matches) != 1 or matches[0].tag != f"{{{namespace}}}{name}":
        return None
    return matches[0]


def _text(element: Element | None) -> str | None:
    if element is None or len(element):
        return None
    return (element.text or "").strip()


def _boolean(element: Element | None) -> bool | None:
    text = _text(element)
    if text not in {"true", "false", "1", "0"}:
        return None
    return text in {"true", "1"}


async def _resource(
    hmc: HMCClient,
    kind: Literal["ManagedSystem", "LogicalPartition", "VirtualIOServer"],
    uuid: str,
) -> Element:
    if not isinstance(uuid, str) or not is_uuid(uuid):
        raise ValueError("Hardware memory detail requires a canonical UUID")
    body = await hmc._get(
        f"/rest/api/uom/{kind}/{uuid}", kind, uuid_path_arguments={"uuid": uuid}
    )
    entry = DET.fromstring(body)
    if entry.tag == f"{{{ATOM_NS}}}feed":
        entry = _one(entry, "entry", ATOM_NS)
    if entry is None or entry.tag != f"{{{ATOM_NS}}}entry":
        raise ValueError("Hardware memory detail requires one Atom entry")
    identity = (_text(_one(entry, "id", ATOM_NS)) or "").removeprefix("urn:uuid:")
    content = _one(entry, "content", ATOM_NS)
    if (
        not is_uuid(identity)
        or identity.casefold() != uuid.casefold()
        or content is None
        or len(content) != 1
        or content[0].tag != f"{{{UOM_NS}}}{kind}"
    ):
        raise ValueError("Hardware memory detail identity or resource is unavailable")
    return content[0]


async def admit_hardware_memory(
    hmc: HMCClient,
    system_uuid: str,
    hardware_mem_encryption: bool | None,
    hardware_mem_expansion: bool | None,
) -> None:
    """Require requested capabilities and the observed supported native tuple."""
    requested = dict(
        hardware_memory_pairs(hardware_mem_encryption, hardware_mem_expansion)
    )
    if not requested:
        return
    try:
        resource = await _resource(hmc, "ManagedSystem", system_uuid)
        observed = tuple(
            _boolean(_one(resource, name))
            for name in (
                "IsClassicHMCManagement",
                "IsHMCPowerVMManagementController",
                "IsPowerVMManagementController",
            )
        )
        capabilities = _one(resource, "AssociatedSystemCapabilities")
        admitted = observed == (True, False, False) and all(
            _boolean(_one(capabilities, capability)) is True
            for native, capability, _ in _FIELDS
            if native in requested
        )
    except (
        HMCError,
        OSError,
        ValueError,
        TypeError,
        DET.ParseError,
        DefusedXmlException,
    ):
        admitted = False
    if not admitted:
        raise ValueError(
            "Hardware memory creation requires supported selected-system configuration and positive capabilities; inspect connectivity and the configured schema pin before retrying"
        )


async def created_hardware_memory_step(
    hmc: HMCClient,
    system_uuid: str,
    entry: dict[str, Any] | None,
    name: str,
    environment: str,
    hardware_mem_encryption: bool | None,
    hardware_mem_expansion: bool | None,
    *,
    vios: bool = False,
) -> WorkflowStep | None:
    """Observe requested access leaves only after independent identity/state proof."""
    requested = {
        native: bool(value)
        for native, value in hardware_memory_pairs(
            hardware_mem_encryption, hardware_mem_expansion
        )
    }
    if not requested:
        return None
    observed: dict[str, bool | None] = {
        rest: None for native, _, rest in _FIELDS if native in requested
    }
    try:
        uuid = (entry or {}).get("UUID")
        initial = (entry or {}).get("Resource")
        if (
            not isinstance(system_uuid, str)
            or not is_uuid(system_uuid)
            or not isinstance(uuid, str)
            or not is_uuid(uuid)
            or not isinstance(initial, dict)
            or leaf_text(initial.get("PartitionName")) != name
            or environment not in ("AIX/Linux", "OS400", "Virtual IO Server")
            or type(vios) is not bool
            or (environment == "Virtual IO Server") != vios
        ):
            raise ValueError("Created partition identity unavailable")
        resolver = resolve_vios_uuid if vios else resolve_lpar_uuid
        await resolver(hmc, uuid, system_name_or_uuid=system_uuid)
        kind = "VirtualIOServer" if vios else "LogicalPartition"
        resource = await _resource(hmc, kind, uuid)
        if (
            _text(_one(resource, "PartitionName")) != name
            or _text(_one(resource, "PartitionType")) != environment
            or _text(_one(resource, "PartitionState")) != "not activated"
        ):
            raise ValueError("Created partition identity or inactive state unavailable")
        memory = _one(resource, "PartitionMemoryConfiguration")
        observed = {rest: _boolean(_one(memory, rest)) for rest in observed}
    except (
        HMCError,
        OSError,
        ValueError,
        TypeError,
        DET.ParseError,
        DefusedXmlException,
    ):
        pass

    status = "readback-unavailable"
    if all(value is not None for value in observed.values()):
        status = (
            "verified"
            if all(
                observed[rest] is requested[native]
                for native, _, rest in _FIELDS
                if native in requested
            )
            else "mismatch"
        )
    result: dict[str, Any] = {
        "status": status,
        "requested": requested,
        "observed": observed,
    }
    if status != "verified":
        result["message"] = (
            "Partition exists or may exist; hardware memory access unverified. Inspect before retrying."
        )
    return WorkflowStep(
        "hardware_memory", "ok" if status == "verified" else "error", result
    )
