"""Refuse partition memory the managed system cannot supply (issue #1166)."""

from __future__ import annotations

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources
from hmcpctl.operations.systems.fleet import read_system_entry
from hmcpctl.resource_identity import optional_system_selector, resolve_system_uuid

_MEMORY_CONTAINER = "AssociatedSystemMemoryConfiguration"
_CONFIGURABLE_MEMORY = "ConfigurableSystemMemory"


def _configurable_memory_mib(system: dict[str, object] | None) -> int | None:
    """Read ``ConfigurableSystemMemory`` from a ManagedSystem entry, or None.

    The element is nested under ``AssociatedSystemMemoryConfiguration`` and is
    in MiB. An element carrying a ``ksv`` attribute parses as ``{"@attrs": ...,
    "text": ...}`` rather than a bare string, so both forms are read.
    """
    resource = (system or {}).get("Resource")
    container = resource.get(_MEMORY_CONTAINER) if isinstance(resource, dict) else None
    value = container.get(_CONFIGURABLE_MEMORY) if isinstance(container, dict) else None
    if isinstance(value, dict):
        value = value.get("text")
    try:
        return int(str(value).strip())
    except ValueError:
        return None


async def require_memory_within_system(
    hmc: HMCClient, system_uuid: str, resources: LparResources
) -> None:
    """Raise ``ValueError`` when desired memory exceeds the system's configurable memory.

    The bound is the system's own reported ``ConfigurableSystemMemory``;
    hypervisor overhead is not estimated. ``mksyscfg`` stores an oversize value
    in the profile without complaint and the failure surfaces only at
    activation, so this refuses before any write. Nothing is read when no
    desired memory is requested, and a system entry that does not report the
    figure leaves the request unchecked: a system that is not operating has only
    its sparse entry (``read_system_entry``, #1501).
    """
    if resources.desired_memory is None:
        return
    configurable = _configurable_memory_mib(await read_system_entry(hmc, system_uuid))
    if configurable is not None and resources.desired_memory > configurable:
        raise ValueError(
            f"desired_memory {resources.desired_memory} MiB exceeds the managed "
            f"system's configurable memory of {configurable} MiB; nothing was "
            "written. Request no more than the system's configurable memory."
        )


async def require_memory_within_selected_system(
    hmc: HMCClient, system_name_or_uuid: str | None, resources: LparResources
) -> None:
    """:func:`require_memory_within_system` for a modify whose selector is optional.

    With no managed-system selector the owning system is not resolved on this
    path, so the request is left unchecked.
    """
    selector = optional_system_selector(system_name_or_uuid)
    if selector is None or resources.desired_memory is None:
        return
    await require_memory_within_system(
        hmc, await resolve_system_uuid(hmc, selector), resources
    )
