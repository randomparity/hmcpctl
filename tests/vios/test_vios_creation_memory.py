"""VIOS C07 saved-only proof composes the landed C11 verification member."""

from decimal import Decimal

import pytest
from lpar.test_creation_memory_workflow import (
    PRIVATE,
    RESOURCES,
    SHARED,
    Memory,
)
from lpar.test_creation_memory_workflow import (
    memory_target as memory_target,  # noqa: PLC0414 — register the shared pytest fixture.
)

from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import serial
from hmcpctl.operations.vios.core import create_vios

pytestmark = pytest.mark.asyncio


def vios(b):
    b.local["ResourceType"] = "VirtualIOServer"
    b.local["Resource"]["PartitionType"] = "Virtual IO Server"


@pytest.mark.parametrize("factor", [0, Decimal("1.10")])
async def test_vios_ame_configured_only(memory_target, factor):
    b = memory_target
    vios(b)
    b.saved.update(
        ActiveMemorySharingEnabled="false",
        ActiveMemoryExpansionEnabled="true" if factor else "false",
        ExpansionFactor="1.1" if factor else "unused",
    )
    result = await create_vios(
        b.hmc,
        "system",
        "new",
        LparResources(desired_memory=1024),
        creation_memory=Memory(mem_mode="ded", mem_expansion=factor),
    )
    proof = result["creation_memory_verification"]
    assert proof.saved.status == "verified" and proof.current is None
    assert (
        "stamp" not in b.events
        and "apply" not in b.events
        and b.events.count("create") == 1
    )


async def test_vios_shared_can_exceed_physical_memory(memory_target):
    b = memory_target
    vios(b)
    result = await create_vios(
        b.hmc, "system", "new", RESOURCES, creation_memory=SHARED
    )
    assert result["creation_memory_verification"].saved.status == "verified"
    assert "stamp" not in b.events and "apply" not in b.events


async def test_vios_dedicated_retains_bound(memory_target):
    b = memory_target
    vios(b)
    with pytest.raises(ValueError, match="memory|Memory"):
        await create_vios(
            b.hmc, "system", "new", RESOURCES, creation_memory=Memory(mem_mode="ded")
        )
    assert not b.created


@pytest.mark.parametrize("missing", ["memory", "serial"])
async def test_vios_c11_c07_neither_returns_over_missing_other(memory_target, missing):
    b = memory_target
    vios(b)
    if missing == "memory":
        del b.saved["MemoryWeight"]
    else:
        serial.read_serial_profile.side_effect = ValueError(PRIVATE)
    with pytest.raises(HMCError) as error:
        await create_vios(
            b.hmc,
            "system",
            "new",
            RESOURCES,
            creation_memory=SHARED,
            max_virtual_slots=8,
            virtual_serial_adapters=(VirtualSerialAdapter(2, "server", False),),
        )
    assert PRIVATE not in str(error.value) and error.value.__cause__ is None
    assert (
        b.events.count("create") == 1
        and "stamp" not in b.events
        and "apply" not in b.events
    )


async def test_vios_c11_c07_metadata_compose(memory_target):
    b = memory_target
    vios(b)
    result = await create_vios(
        b.hmc,
        "system",
        "new",
        RESOURCES,
        creation_memory=SHARED,
        max_virtual_slots=8,
        virtual_serial_adapters=(VirtualSerialAdapter(2, "server", False),),
    )
    assert result["creation_memory_verification"].saved.status == "verified"
    assert result["serial_verification"]["configuration"] == "verified"


@pytest.mark.parametrize(
    "fault,classification", [("refusal", "rejected"), ("timeout", "partial")]
)
async def test_vios_uncertainty_reconciles_without_replay(
    memory_target, fault, classification
):
    b = memory_target
    vios(b)
    b.fault = fault
    with pytest.raises(HMCError, match=classification) as error:
        await create_vios(b.hmc, "system", "new", RESOURCES, creation_memory=SHARED)
    assert PRIVATE not in str(error.value) and b.events.count("create") == 1
    assert "stamp" not in b.events and "apply" not in b.events
