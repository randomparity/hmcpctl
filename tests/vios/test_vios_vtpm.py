"""Existing VIOS creation owner optionally carries the three native vTPM fields."""

import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
from hmcpctl.errors import HMCError
from hmcpctl.operations.vios.core import create_vios
from hmcpctl.ssh.transport import HMCCLIError

SYSTEM = "00000001-abcd-4ef0-8abc-000000000001"
SETTINGS = LparVtpmSettings(True, "1.2")


@pytest.fixture
def boundary(monkeypatch):
    entry = {"UUID": "vios-created", "Resource": {"PartitionName": "vios-R1"}}
    hmc = SimpleNamespace(
        config=HMCConfig.from_mapping({}),
        get_managed_system=AsyncMock(
            return_value={
                "Resource": {
                    "SystemName": "sys-R1",
                    "AssociatedSystemCapabilities": {
                        "VirtualTrustedPlatformModuleCapable": "true"
                    },
                }
            }
        ),
        find_vios_by_name=AsyncMock(side_effect=[None, entry]),
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM, "SystemName": "sys-R1"}]
        ),
        create_vios=AsyncMock(return_value=entry),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.vios.core.resolve_system_uuid",
        AsyncMock(return_value=SYSTEM),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.vios.core.require_memory_within_system", AsyncMock()
    )
    calls = []
    state = SimpleNamespace(readback="ok", write_error=None)

    async def native(config, command):
        calls.append(command)
        if state.write_error:
            raise state.write_error
        return ""

    async def read(config, command):
        if "-r sys" in command:
            return "1.2,null\n"
        if state.readback == "unavailable":
            raise HMCCLIError("readback lost")
        return (
            "vios-R1,vioserver,Not Activated,0,null,null\n"
            if state.readback == "mismatch"
            else "vios-R1,vioserver,Not Activated,1,1.2,null\n"
        )

    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", native)
    monkeypatch.setattr("hmcpctl.ssh.lpar_vtpm.run_hmc_command", read)
    return SimpleNamespace(hmc=hmc, entry=entry, calls=calls, state=state)


@pytest.mark.asyncio
async def test_existing_rest_path_and_shape_remain_when_no_vtpm_requested(boundary):
    b = boundary
    assert (
        await create_vios(
            b.hmc, "sys-R1", "vios-R1", LparResources(desired_memory=1024)
        )
        == b.entry
    )
    b.hmc.create_vios.assert_awaited_once()
    b.hmc.find_vios_by_name.assert_not_awaited()
    assert b.calls == []


@pytest.mark.asyncio
async def test_native_vios_creation_keeps_its_owner_and_scoped_readback(boundary):
    b = boundary
    actual = await create_vios(
        b.hmc,
        "sys-R1",
        "vios-R1",
        LparResources(desired_memory=1024),
        vtpm_settings=SETTINGS,
    )
    assert actual == b.entry
    assert len(b.calls) == 1
    args = shlex.split(b.calls[0])
    assert args[:3] == ["mksyscfg", "-r", "lpar"]
    assert "lpar_env=vioserver" in args[-1]
    assert "vtpm_enabled=1,vtpm_version=1.2" in args[-1]
    b.hmc.create_vios.assert_not_awaited()
    assert b.hmc.find_vios_by_name.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["duplicate", "capacity", "unsupported", "wrong-model"]
)
async def test_vios_pre_effect_admission_refuses_before_native_creation(
    boundary, monkeypatch, mode
):
    b = boundary
    if mode == "duplicate":
        b.hmc.find_vios_by_name.side_effect = None
        b.hmc.find_vios_by_name.return_value = b.entry
    if mode == "capacity":
        monkeypatch.setattr(
            "hmcpctl.operations.vios.core.require_memory_within_system",
            AsyncMock(side_effect=ValueError("capacity")),
        )
    if mode == "unsupported":
        b.hmc.get_managed_system.return_value["Resource"][
            "AssociatedSystemCapabilities"
        ]["VirtualTrustedPlatformModuleCapable"] = "false"
    with pytest.raises((ValueError, TypeError)):
        await create_vios(
            b.hmc,
            "sys-R1",
            "vios-R1",
            LparResources(desired_memory=1024),
            vtpm_settings={} if mode == "wrong-model" else SETTINGS,
        )
    assert not b.calls
    b.hmc.create_vios.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["mismatch", "unavailable", "missing", "ambiguous", "unknown-write"]
)
async def test_vios_created_or_uncertain_result_never_replays_or_claims_verified(
    boundary, mode
):
    b = boundary
    if mode in {"mismatch", "unavailable"}:
        b.state.readback = mode
    if mode == "missing":
        b.hmc.find_vios_by_name.side_effect = [None, None]
    if mode == "ambiguous":
        b.hmc.find_vios_by_name.side_effect = [None, ValueError("ambiguous VIOS")]
    if mode == "unknown-write":
        b.state.write_error = HMCCLIError("connection lost")
    with pytest.raises(HMCError, match="[Ii]nspect|[Cc]reated|exists|unknown"):
        await create_vios(
            b.hmc,
            "sys-R1",
            "vios-R1",
            LparResources(desired_memory=1024),
            vtpm_settings=SETTINGS,
        )
    assert len(b.calls) == 1
    b.hmc.create_vios.assert_not_awaited()


@pytest.mark.asyncio
async def test_vtpm_native_creation_does_not_implicitly_allocate_all_resources(
    boundary,
):
    b = boundary
    with pytest.raises(ValueError, match="resource|all_resources"):
        await create_vios(
            b.hmc, "sys-R1", "vios-R1", LparResources(), vtpm_settings=SETTINGS
        )
    assert not b.calls


@pytest.mark.asyncio
async def test_vios_native_capacity_uses_completed_resource_values(
    boundary, monkeypatch
):
    from hmcpctl.operations.lpar.memory_bound import require_memory_within_system

    b = boundary
    b.hmc.list_system_summaries.return_value[0]["State"] = "operating"
    b.hmc.get_managed_system.return_value["Resource"][
        "AssociatedSystemMemoryConfiguration"
    ] = {"ConfigurableSystemMemory": "2048"}
    monkeypatch.setattr(
        "hmcpctl.operations.vios.core.require_memory_within_system",
        require_memory_within_system,
    )
    with pytest.raises(ValueError, match="4096.*2048"):
        await create_vios(
            b.hmc,
            "sys-R1",
            "vios-R1",
            LparResources(desired_procs=0.1),
            vtpm_settings=SETTINGS,
        )
    assert not b.calls
