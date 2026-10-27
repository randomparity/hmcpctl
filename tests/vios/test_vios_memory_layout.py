"""VIOS ratios use the existing native owner and strict saved identity."""

import csv
import io
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.errors import HMCError
from hmcpctl.operations.vios import core
from hmcpctl.ssh.transport import HMCCLIError

SYSTEM = "00000001-abcd-4ef0-8abc-000000000001"
VIOS = "00000002-abcd-4ef0-8abc-000000000002"
RESOURCES = LparResources(desired_memory=1024, desired_procs=1, dedicated=True)
LAYOUT = LparMemoryLayout(hpt_ratio="1:128", ppt_ratio="1:256")


@pytest.fixture
def boundary(monkeypatch):
    entry = {"UUID": VIOS, "Resource": {"PartitionName": "vios-R1"}}
    hmc = SimpleNamespace(
        config=HMCConfig.from_mapping({}),
        find_vios_by_name=AsyncMock(side_effect=[None, entry]),
        create_vios=AsyncMock(return_value=entry),
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM, "SystemName": "sys-R1"}]
        ),
        get_managed_system=AsyncMock(
            return_value={
                "Resource": {
                    "SystemName": "sys-R1",
                    "AssociatedSystemMemoryConfiguration": {
                        "ConfigurableSystemMemory": "8192"
                    },
                }
            }
        ),
    )
    monkeypatch.setattr(core, "resolve_system_uuid", AsyncMock(return_value=SYSTEM))
    monkeypatch.setattr(
        core, "resolve_system_cli_name", AsyncMock(return_value="sys-R1"), raising=False
    )
    mode = AsyncMock(return_value=({}, False))
    monkeypatch.setattr(core, "read_creation_authority", mode, raising=False)
    write = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", write)
    state = SimpleNamespace(
        profile="default_profile", name="vios-R1", hpt="1:128", ppt="1:256"
    )

    def read(config, command):
        out = io.StringIO()
        writer = csv.writer(out)
        columns = shlex.split(command)[shlex.split(command).index("-F") + 1].split(",")
        writer.writerow(columns)
        writer.writerow(
            ["1:64,1:128", "1:128,1:256"]
            if command.startswith("lshwres")
            else [
                state.name,
                state.profile,
                "null",
                "null",
                "null",
                state.hpt,
                state.ppt,
                "null",
            ]
        )
        return out.getvalue()

    reader = AsyncMock(side_effect=read)
    monkeypatch.setattr("hmcpctl.ssh.lpar_memory_layout.run_hmc_command", reader)
    return SimpleNamespace(
        hmc=hmc, entry=entry, mode=mode, write=write, read=reader, state=state
    )


@pytest.mark.asyncio
async def test_ratios_force_existing_native_once_with_mode_only(boundary):
    b = boundary
    assert (
        await core.create_vios(
            b.hmc, "selector", "vios-R1", RESOURCES, memory_layout=LAYOUT
        )
        == b.entry
    )
    b.write.assert_awaited_once()
    record = shlex.split(b.write.await_args.args[1])[-1]
    assert "hpt_ratio=1:128,ppt_ratio=1:256" in record
    assert "lpar_env=vioserver" in record
    b.hmc.create_vios.assert_not_awaited()
    b.mode.assert_awaited_once_with(
        b.hmc, SYSTEM, need_management=False, context="Memory layout"
    )
    assert b.hmc.find_vios_by_name.await_args.kwargs == {"system_uuid": SYSTEM}


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [None, LparMemoryLayout()])
async def test_omitted_layout_preserves_rest_without_reads(boundary, layout):
    b = boundary
    assert (
        await core.create_vios(
            b.hmc, "selector", "vios-R1", RESOURCES, memory_layout=layout
        )
        == b.entry
    )
    b.hmc.create_vios.assert_awaited_once()
    b.mode.assert_not_awaited()
    b.read.assert_not_awaited()
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "layout",
    [
        LparMemoryLayout(min_num_huge_pages=0),
        LparMemoryLayout(kvm_reserved_mem_percent=0),
        object(),
    ],
)
async def test_vios_excluded_fields_refused_before_effects(boundary, layout):
    with pytest.raises((ValueError, TypeError)):
        await core.create_vios(
            boundary.hmc, "selector", "vios-R1", RESOURCES, memory_layout=layout
        )
    boundary.hmc.find_vios_by_name.assert_not_awaited()
    boundary.write.assert_not_awaited()
    boundary.read.assert_not_awaited()
    boundary.mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_requires_explicit_resources_before_effects(boundary):
    with pytest.raises(ValueError, match="explicit"):
        await core.create_vios(
            boundary.hmc, "selector", "vios-R1", LparResources(), memory_layout=LAYOUT
        )
    boundary.write.assert_not_awaited()
    boundary.mode.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "wrong-profile",
        "wrong-name",
        "mismatch",
        "missing",
        "identity",
        "lookup",
        "write",
        "controller",
    ],
)
async def test_layout_partial_or_unknown_never_replays_or_claims_verified(
    boundary, failure
):
    b = boundary
    if failure == "wrong-profile":
        b.state.profile = "other"
    if failure == "wrong-name":
        b.state.name = "other"
    if failure == "mismatch":
        b.state.hpt = "1:64"
    if failure == "missing":
        b.state.hpt = "null"
    if failure == "identity":
        b.entry["UUID"] = "not-a-uuid"
    if failure == "lookup":
        b.hmc.find_vios_by_name.side_effect = [
            None,
            RuntimeError("private-provider-marker"),
        ]
    if failure == "write":
        b.write.side_effect = HMCCLIError("private-provider-marker")
    if failure == "controller":
        b.mode.return_value = ({}, True)
    with pytest.raises(HMCError) as caught:
        await core.create_vios(
            b.hmc, "selector", "vios-R1", RESOURCES, memory_layout=LAYOUT
        )
    assert "private-provider-marker" not in str(caught.value)
    assert "VIOS exists or may exist" in str(caught.value)
    b.write.assert_awaited_once()
    b.hmc.create_vios.assert_not_awaited()
    if failure == "controller":
        assert "profile_name=" not in shlex.split(b.write.await_args.args[1])[-1]
        assert b.read.await_count == 1  # Advertisement only; never a profile query.


@pytest.mark.asyncio
async def test_vios_layout_canonical_system_failure_prevents_create(
    boundary, monkeypatch
):
    monkeypatch.setattr(
        core,
        "resolve_system_cli_name",
        AsyncMock(side_effect=HMCCLIError("lookup unavailable")),
    )
    with pytest.raises(HMCError):
        await core.create_vios(
            boundary.hmc, "selector", "vios-R1", RESOURCES, memory_layout=LAYOUT
        )
    boundary.write.assert_not_awaited()
    boundary.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [None, "vtpm", "policy"])
async def test_vios_ratios_compose_existing_security_and_vtpm(
    boundary, monkeypatch, failed
):
    from hmcpctl.documents.lpar_security_policy import (
        LparSecurityPolicy,
        SecurityPolicyState,
    )
    from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
    from hmcpctl.operations.lpar.security_policy import SecurityPolicyCapabilities
    from hmcpctl.operations.lpar.vtpm import VtpmCapabilities

    policy = LparSecurityPolicy(secure_boot=0)
    vtpm = LparVtpmSettings(vtpm_enabled=False)
    monkeypatch.setattr(
        core,
        "read_vtpm_capabilities",
        AsyncMock(return_value=VtpmCapabilities(("1.2",), (), False)),
    )
    monkeypatch.setattr(
        core,
        "read_security_policy_capabilities",
        AsyncMock(
            return_value=SecurityPolicyCapabilities(
                True, False, False, None, None, False, None, None
            )
        ),
    )
    vtpm_read = AsyncMock(return_value=SimpleNamespace(settings=vtpm))
    policy_read = AsyncMock(
        return_value=SecurityPolicyState(
            "Virtual IO Server", "not activated", policy, None
        )
    )
    if failed:
        (vtpm_read if failed == "vtpm" else policy_read).side_effect = HMCError(
            "private-provider-marker"
        )
    monkeypatch.setattr(core, "get_lpar_vtpm_state", vtpm_read)
    monkeypatch.setattr(core, "read_lpar_security_policy", policy_read)
    args = (boundary.hmc, "selector", "vios-R1", RESOURCES)
    if failed:
        with pytest.raises(HMCError) as caught:
            await core.create_vios(
                *args, memory_layout=LAYOUT, vtpm_settings=vtpm, security_policy=policy
            )
        assert "private-provider-marker" not in str(caught.value)
    else:
        assert (
            await core.create_vios(
                *args, memory_layout=LAYOUT, vtpm_settings=vtpm, security_policy=policy
            )
            == boundary.entry
        )
    boundary.write.assert_awaited_once()
    record = shlex.split(boundary.write.await_args.args[1])[-1]
    assert all(
        item in record
        for item in [
            "hpt_ratio=1:128",
            "ppt_ratio=1:256",
            "secure_boot=0",
            "vtpm_enabled=0",
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", ["false", "true"])
async def test_vios_ratios_preserve_independent_hardware_barrier(boundary, observed):
    from hmcpctl.documents.common import UOM_NS
    from hmcpctl.xmlutil import ATOM_NS

    b = boundary
    b.hmc.list_vios = AsyncMock(return_value=[b.entry])

    async def detail(path, kind, **kwargs):
        uuid = SYSTEM if kind == "ManagedSystem" else VIOS
        body = (
            "<IsClassicHMCManagement>true</IsClassicHMCManagement>"
            "<IsHMCPowerVMManagementController>false</IsHMCPowerVMManagementController>"
            "<IsPowerVMManagementController>false</IsPowerVMManagementController>"
            "<AssociatedSystemCapabilities><HardwareMemoryEncryptionCapable>true</HardwareMemoryEncryptionCapable></AssociatedSystemCapabilities>"
            if kind == "ManagedSystem"
            else f"<PartitionName>vios-R1</PartitionName><PartitionType>Virtual IO Server</PartitionType><PartitionState>not activated</PartitionState><PartitionMemoryConfiguration><MemoryEncryptionHardwareAccessEnabled>{observed}</MemoryEncryptionHardwareAccessEnabled></PartitionMemoryConfiguration>"
        )
        return f'<entry xmlns="{ATOM_NS}"><id>urn:uuid:{uuid}</id><content><{kind} xmlns="{UOM_NS}">{body}</{kind}></content></entry>'

    b.hmc._get = AsyncMock(side_effect=detail)
    if observed == "true":
        with pytest.raises(HMCError, match="hardware memory access mismatch"):
            await core.create_vios(
                b.hmc,
                "selector",
                "vios-R1",
                RESOURCES,
                memory_layout=LAYOUT,
                hardware_mem_encryption=False,
            )
    else:
        assert (
            await core.create_vios(
                b.hmc,
                "selector",
                "vios-R1",
                RESOURCES,
                memory_layout=LAYOUT,
                hardware_mem_encryption=False,
            )
            == b.entry
        )
    b.write.assert_awaited_once()
    assert "hardware_mem_encryption=0" in shlex.split(b.write.await_args.args[1])[-1]


@pytest.mark.asyncio
async def test_vios_unadvertised_ratio_refused_before_create(boundary):
    with pytest.raises(ValueError, match="not advertised"):
        await core.create_vios(
            boundary.hmc,
            "selector",
            "vios-R1",
            RESOURCES,
            memory_layout=LparMemoryLayout(hpt_ratio="1:512"),
        )
    boundary.write.assert_not_awaited()
    assert boundary.hmc.find_vios_by_name.await_count == 1
