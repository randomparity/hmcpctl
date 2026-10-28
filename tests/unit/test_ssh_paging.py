"""Fixed paging commands, independently bounded before entering SSH."""

from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.ssh import memory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,command",
    [
        ("read_lpar_paging_memory", "lshwres -r mem -m 'system one' --level lpar"),
        (
            "read_lpar_paging_devices",
            "lshwres -r mempool -m 'system one' --rsubtype pgdev",
        ),
    ],
)
async def test_fixed_readers(monkeypatch, name, command):
    entry = getattr(memory, name, None)
    assert entry is not None, "documented fixed reader is missing"
    transport = AsyncMock(return_value="observation")
    monkeypatch.setattr(memory, "run_hmc_command", transport)
    config = HMCConfig.from_mapping({})
    assert await entry(config, "system one") == "observation"
    transport.assert_awaited_once_with(
        config, command, max_output_bytes=5 * 1024 * 1024
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [float("inf"), float("nan"), 0, -1])
@pytest.mark.parametrize(
    "name", ["read_lpar_paging_memory", "read_lpar_paging_devices"]
)
async def test_invalid_timeout_zero_io(monkeypatch, timeout, name):
    entry = getattr(memory, name, None)
    assert entry is not None
    transport = AsyncMock()
    monkeypatch.setattr(memory, "run_hmc_command", transport)
    with pytest.raises(ValueError, match="finite positive"):
        await entry(HMCConfig.from_mapping({"ssh_timeout": timeout}), "system")
    transport.assert_not_called()
