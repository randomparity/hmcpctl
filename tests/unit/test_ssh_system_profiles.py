"""Fixed documented system-profile SSH routes, never arbitrary command inputs."""

import shlex
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.ssh import profiles


@pytest.mark.asyncio
async def test_inventory_argv(monkeypatch):
    command = AsyncMock(return_value="No results were found.")
    monkeypatch.setattr(profiles, "run_hmc_command", command)
    target = "9009-42A*TEST; echo unsafe"
    await profiles.list_system_profile_records(HMCConfig.from_mapping({}), target)
    assert shlex.split(command.await_args.args[1]) == [
        "lssyscfg",
        "-r",
        "sysprof",
        "-m",
        target,
        "-F",
        "name:lpar_ids:profile_names",
        "--header",
    ]
    assert command.await_args.kwargs == {"include_stderr": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activate,continuation", [(False, False), (True, False), (True, True)]
)
async def test_validation_argv(monkeypatch, activate, continuation):
    command = AsyncMock(return_value="")
    monkeypatch.setattr(profiles, "run_hmc_command", command)
    await profiles.validate_system_profile_record(
        HMCConfig.from_mapping({}),
        "system; x",
        "name; x",
        activate=activate,
        continue_on_error=continuation,
    )
    expected = ["chsysstate", "-m", "system; x", "-r", "sysprof", "-n", "name; x"]
    if activate:
        expected += ["-o", "on"]
    expected += ["--test"]
    if continuation:
        expected += ["--continue"]
    assert shlex.split(command.await_args.args[1]) == expected
    assert command.await_args.kwargs == {"include_stderr": True}


@pytest.mark.asyncio
async def test_delete_argv(monkeypatch):
    command = AsyncMock(return_value="")
    monkeypatch.setattr(profiles, "run_hmc_command", command)
    await profiles.delete_system_profile_record(
        HMCConfig.from_mapping({}), "system; x", "name; x"
    )
    assert shlex.split(command.await_args.args[1]) == [
        "rmsyscfg",
        "-r",
        "sysprof",
        "-m",
        "system; x",
        "-n",
        "name; x",
    ]
    assert command.await_args.kwargs == {"include_stderr": True}
