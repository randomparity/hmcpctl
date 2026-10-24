"""Strict Linux-role values share the finite native partition record."""

import shlex
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources, LparSettings
from hmcpctl.ssh.lpar import (
    DEFAULT_PROFILE_NAME,
    create_lpar_via_cli,
    get_lpar_settings,
    set_lpar_settings,
)
from hmcpctl.ssh.transport import HMCCLIError

CONFIG = HMCConfig.from_mapping({})


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True])
async def test_role_record_is_fixed_and_encoded(monkeypatch, value):
    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    await set_lpar_settings(
        CONFIG,
        "system;quoted",
        "owned;name",
        LparSettings(powervm_mgmt_capable=value, kvm_capable=not value),
    )
    assert shlex.split(run.await_args.args[1]) == [
        "chsyscfg",
        "-r",
        "lpar",
        "-m",
        "system;quoted",
        "-i",
        f"name=owned;name,powervm_mgmt_capable={int(value)},kvm_capable={int(not value)}",
    ]
    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["bad,name", "bad=name", 'bad"name', "bad\nname"])
@pytest.mark.parametrize("operation", ["create", "update", "read"])
async def test_role_record_refuses_selector_dispatch(monkeypatch, name, operation):
    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    with pytest.raises(HMCCLIError):
        if operation == "create":
            await create_lpar_via_cli(
                CONFIG, "system", name, settings=LparSettings(kvm_capable=True)
            )
        elif operation == "update":
            await set_lpar_settings(
                CONFIG, "system", name, LparSettings(kvm_capable=False)
            )
        else:
            await get_lpar_settings(CONFIG, "system", name)
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, DEFAULT_PROFILE_NAME, "custom;profile"])
@pytest.mark.parametrize("role", [None, "empty", "management", False, True])
async def test_native_same_record_and_profile(monkeypatch, profile, role):
    run = AsyncMock(return_value="created")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    settings = None
    if role == "empty":
        settings = LparSettings()
    elif role == "management":
        settings = LparSettings(powervm_mgmt_capable=True)
    elif role is not None:
        settings = LparSettings(powervm_mgmt_capable=False, kvm_capable=role)
    resources = LparResources(
        min_memory=1024,
        desired_memory=2048,
        max_memory=4096,
        min_procs=0.2,
        desired_procs=0.5,
        max_procs=1.0,
        min_vcpus=1,
        desired_vcpus=2,
        max_vcpus=4,
        uncapped=False,
    )
    result = await create_lpar_via_cli(
        CONFIG,
        "system;quoted",
        "owned;name",
        resources=resources,
        profile_name=profile,
        partition_id=7,
        max_virtual_slots=32,
        settings=settings,
    )
    assert result == "created"
    run.assert_awaited_once()
    words = shlex.split(run.await_args.args[1])
    assert words[:-1] == ["mksyscfg", "-r", "lpar", "-m", "system;quoted", "-i"]
    pairs = [pair.split("=", 1) for pair in words[-1].split(",")]
    record = dict(pairs)
    assert len(pairs) == len(record)
    expected = {
        "name": "owned;name",
        "lpar_env": "aixlinux",
        "lpar_id": "7",
        "max_virtual_slots": "32",
        "min_mem": "1024",
        "desired_mem": "2048",
        "max_mem": "4096",
        "proc_mode": "shared",
        "sharing_mode": "cap",
        "min_proc_units": "0.2",
        "desired_proc_units": "0.5",
        "max_proc_units": "1.0",
        "min_procs": "1",
        "desired_procs": "2",
        "max_procs": "4",
    }
    if profile is not None:
        expected["profile_name"] = profile
    if role == "management":
        expected["powervm_mgmt_capable"] = "1"
    elif role is False or role is True:
        expected |= {"powervm_mgmt_capable": "0", "kvm_capable": str(int(role))}
    if role is True:
        expected["lpar_proc_compat_mode"] = "POWER10"
    assert record == expected


@pytest.mark.asyncio
async def test_native_default_profile_and_all_resources(monkeypatch):
    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    await create_lpar_via_cli(
        CONFIG, "system", "owned", settings=LparSettings(kvm_capable=True)
    )
    run.assert_awaited_once()
    record = shlex.split(run.await_args.args[1])[-1]
    assert dict(pair.split("=", 1) for pair in record.split(",")) == {
        "name": "owned",
        "lpar_env": "aixlinux",
        "profile_name": DEFAULT_PROFILE_NAME,
        "all_resources": "1",
        "kvm_capable": "1",
        "lpar_proc_compat_mode": "POWER10",
    }
