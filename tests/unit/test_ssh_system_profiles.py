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
