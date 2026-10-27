"""C08 strict scalar, native record and reference bindings."""

import shlex
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from hmcpctl.config import HMCConfig
from hmcpctl.ssh.lpar import create_lpar_via_cli

VALID = (None, "none", 1, 255)
INVALID = (True, False, 0, 256, -1, 1.0, "1", "NONE", "", " none", [], {})


@pytest.mark.parametrize("value", VALID + INVALID)
def test_input_domain_and_record(value):
    from hmcpctl.documents.lpar_affinity_group import (
        AffinityGroupID,
        validate_affinity_group_id,
    )

    if any(type(value) is type(good) and value == good for good in VALID):
        assert validate_affinity_group_id(value) == value
        assert TypeAdapter(AffinityGroupID | None).validate_python(value) == value
    else:
        with pytest.raises((ValueError, TypeError), match="affinity_group_id"):
            validate_affinity_group_id(value)
        with pytest.raises(ValidationError):
            TypeAdapter(AffinityGroupID | None).validate_python(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", VALID)
async def test_native_record(value, monkeypatch):
    command = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", command)
    await create_lpar_via_cli(
        HMCConfig.from_mapping({}), "sys-R1", "part-R1", affinity_group_id=value
    )
    command.assert_awaited_once()
    record = shlex.split(command.await_args.args[1])[-1]
    assert "all_resources=1" in record
    assert ("affinity_group_id=" in record) is (value is not None)
    if value is not None:
        assert f"affinity_group_id={value}" in record


@pytest.mark.asyncio
@pytest.mark.parametrize("value", INVALID)
async def test_native_invalid_before_io(value, monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", command)
    with pytest.raises((TypeError, ValueError), match="affinity_group_id"):
        await create_lpar_via_cli(
            HMCConfig.from_mapping({}), "s", "p", affinity_group_id=value
        )
    command.assert_not_awaited()


@pytest.mark.parametrize("generation", ["p10", "p11"])
def test_reference_bindings(generation):
    from hmcpctl.documents.lpar_affinity_group import validate_affinity_group_id

    source = (
        Path(__file__).parents[2]
        / f"docs/refs/hmc-commands-{generation}/commands/mksyscfg.md"
    )
    if not source.exists():
        pytest.skip("Local reference corpus unavailable; no live evidence")
    section = (
        source.read_text().split("[**affinity_group_id**]", 1)[1].split("[**", 1)[0]
    )
    assert "**none**" in section and "**1** - **255**" in section
    assert validate_affinity_group_id("none") == "none"
    assert validate_affinity_group_id(255) == 255
