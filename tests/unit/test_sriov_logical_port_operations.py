from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.operations.virtualization.pcie import (
    InventorySelector,
    SriovLogicalPortCapabilityError,
    SriovLogicalPortPartialError,
    assign_sriov_logical_port,
    unassign_sriov_logical_port,
)
from hmcpctl.ssh.transport import HMCCLIError


def _hmc() -> AsyncMock:
    hmc = AsyncMock()
    hmc.config = HMCConfig(host="h", user="u", password="p")
    return hmc


def _common(
    monkeypatch,
    *,
    state="Not Activated",
    rmc="inactive",
    configured=(),
    granularity=None,
):
    physical = {
        "adapter_id": "1",
        "phys_port_id": "0",
        "state": "1",
        "phys_port_loc": "U-P1-C4-T1",
    }
    if granularity is not None:
        physical["min_eth_capacity_granularity"] = granularity
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.resolve_and_authorize_lpar_names",
        AsyncMock(return_value=("sys", "lpar")),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.read_sriov_environment",
        AsyncMock(
            return_value=("Version: 10\nRelease: 3\nService Pack: 1060", "8375-42A")
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.list_sriov_adapter_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": "1",
                    "config_state": "sriov",
                    "functional_state": "1",
                    "sriov_status": "running",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.list_sriov_physical_port_rows",
        AsyncMock(return_value=[physical]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.list_sriov_configured_logical_port_rows",
        AsyncMock(return_value=list(configured)),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.list_sriov_unconfigured_logical_port_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": "1",
                    "logical_port_id": "3",
                    "logical_port_type": "unconfigured",
                    "location_code": "U-P1-C4-T1-S3",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.read_sriov_lpar_state",
        AsyncMock(
            return_value={
                "name": "lpar",
                "lpar_id": "2",
                "state": state,
                "rmc_state": rmc,
            }
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.read_sriov_profile_ports",
        AsyncMock(return_value={"name": "prof", "sriov_eth_logical_ports": "none"}),
    )


@pytest.mark.asyncio
async def test_assign_mutates_and_verifies_effective_readback(monkeypatch):
    _common(monkeypatch)
    mutate = AsyncMock(return_value="")
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.assign_sriov_logical_port_dynamic",
        mutate,
    )
    after = {
        "config_id": "0",
        "lpar_name": "lpar",
        "lpar_id": "2",
        "lpar_state": "Not Activated",
        "adapter_id": "1",
        "logical_port_id": "3",
        "phys_port_id": "0",
        "functional_state": "1",
        "capacity": "2.0",
        "max_capacity": "100.0",
    }
    rows = AsyncMock(side_effect=[[], [after]])
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.list_sriov_configured_logical_port_rows",
        rows,
    )

    result = await assign_sriov_logical_port(
        _hmc(),
        "lpar",
        InventorySelector("1", "0", "3"),
        Decimal("2.0"),
        profile_name="prof",
        system_name_or_uuid="sys",
    )

    assert result.changed is True
    assert result.effective_after.owner_lpar == "lpar"
    mutate.assert_awaited_once()


@pytest.mark.asyncio
async def test_assign_is_idempotent_and_refuses_foreign_owner(monkeypatch):
    owned = {
        "config_id": "0",
        "lpar_name": "lpar",
        "lpar_id": "2",
        "lpar_state": "Not Activated",
        "adapter_id": "1",
        "logical_port_id": "3",
        "phys_port_id": "0",
        "functional_state": "1",
        "capacity": "2.0",
        "max_capacity": "100.0",
    }
    _common(monkeypatch, configured=[owned])
    mutate = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.assign_sriov_logical_port_dynamic",
        mutate,
    )
    unchanged = await assign_sriov_logical_port(
        _hmc(),
        "lpar",
        InventorySelector("1", "0", "3"),
        Decimal("2.0"),
        profile_name="prof",
        system_name_or_uuid="sys",
    )
    assert unchanged.changed is False
    mutate.assert_not_awaited()

    owned["lpar_name"] = "other"
    with pytest.raises(PermissionError, match="already assigned"):
        await assign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            Decimal("2.0"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )


@pytest.mark.asyncio
async def test_assign_rejects_capacity_and_unsupported_running_state(monkeypatch):
    _common(monkeypatch, state="Running", rmc="inactive")
    with pytest.raises(SriovLogicalPortCapabilityError, match="active RMC"):
        await assign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            Decimal("2.0"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )
    with pytest.raises(ValueError, match="between 1 and 100"):
        await assign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            Decimal("0.5"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )


def _dynamic_mutation(monkeypatch) -> AsyncMock:
    mutate = AsyncMock(return_value="")
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.assign_sriov_logical_port_dynamic",
        mutate,
    )
    return mutate


async def _assign(capacity: str):
    return await assign_sriov_logical_port(
        _hmc(),
        "lpar",
        InventorySelector("1", "0", "3"),
        Decimal(capacity),
        profile_name="prof",
        system_name_or_uuid="sys",
    )


@pytest.mark.asyncio
async def test_assign_refuses_capacity_off_the_port_granularity_before_mutation(
    monkeypatch,
):
    _common(monkeypatch, granularity="1.0")
    mutate = _dynamic_mutation(monkeypatch)

    with pytest.raises(
        ValueError,
        match=r"capacity_percent 7\.5 is not a multiple of the physical port's "
        r"capacity granularity 1\.0%",
    ):
        await _assign("7.5")

    mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_sends_capacity_on_the_port_granularity(monkeypatch):
    _common(monkeypatch, granularity="2.0")
    mutate = _dynamic_mutation(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.list_sriov_configured_logical_port_rows",
        AsyncMock(side_effect=[[], []]),
    )

    with pytest.raises(SriovLogicalPortPartialError):
        await _assign("4")

    mutate.assert_awaited_once()
    assert mutate.await_args.args[-1] == "4"


@pytest.mark.asyncio
@pytest.mark.parametrize("granularity", [None, "", "null"])
async def test_assign_without_reported_granularity_leaves_the_hmc_to_judge(
    monkeypatch, granularity
):
    _common(monkeypatch, granularity=granularity)
    mutate = _dynamic_mutation(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.list_sriov_configured_logical_port_rows",
        AsyncMock(side_effect=[[], []]),
    )

    with pytest.raises(SriovLogicalPortPartialError):
        await _assign("7.5")

    mutate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "granularity", ["abc", "0", "-1", "NaN", "Infinity", "1E-27", "101"]
)
async def test_assign_rejects_malformed_port_granularity_before_mutation(
    monkeypatch, granularity
):
    _common(monkeypatch, granularity=granularity)
    mutate = _dynamic_mutation(monkeypatch)

    with pytest.raises(
        HMCCLIError, match="malformed physical-port capacity granularity"
    ):
        await _assign("2")

    mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_unassign_is_idempotent_and_verified(monkeypatch):
    _common(monkeypatch)
    mutate = AsyncMock(return_value="")
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.unassign_sriov_logical_port_profile",
        mutate,
    )
    unchanged = await unassign_sriov_logical_port(
        _hmc(),
        "lpar",
        InventorySelector("1", "0", "3"),
        profile_name="prof",
        system_name_or_uuid="sys",
    )
    assert unchanged.changed is False

    record = "0:1:0:3:0:0:0:all::all:0:0:2.0:100.0:none:0::::"
    reads = AsyncMock(
        side_effect=[
            {"name": "prof", "sriov_eth_logical_ports": record},
            {"name": "prof", "sriov_eth_logical_ports": "none"},
        ]
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.read_sriov_profile_ports", reads
    )
    changed = await unassign_sriov_logical_port(
        _hmc(),
        "lpar",
        InventorySelector("1", "0", "3"),
        profile_name="prof",
        system_name_or_uuid="sys",
    )
    assert changed.changed is True
    mutate.assert_awaited_once()


@pytest.mark.asyncio
async def test_unassign_rejects_multiple_profile_records_before_dispatch(monkeypatch):
    _common(monkeypatch)
    mutate = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.unassign_sriov_logical_port_profile",
        mutate,
    )
    record = "0:1:0:3:0:0:0:all::all:0:0:2.0:100.0,0:1:0:4:0:0:0:all::all:0:0:2.0:100.0"
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.read_sriov_profile_ports",
        AsyncMock(return_value={"name": "prof", "sriov_eth_logical_ports": record}),
    )
    with pytest.raises(ValueError, match="exactly the selected"):
        await unassign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )
    mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_wraps_post_dispatch_read_failure(monkeypatch):
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.assign_sriov_logical_port_dynamic",
        AsyncMock(return_value=""),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.list_sriov_configured_logical_port_rows",
        AsyncMock(side_effect=[[], RuntimeError("read failed")]),
    )
    with pytest.raises(SriovLogicalPortPartialError) as caught:
        await assign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            Decimal("2.0"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )
    assert caught.value.result.changed is True
    assert caught.value.result.effective_after is None
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "read failed"


@pytest.mark.asyncio
async def test_assign_reports_hmc_refusal_when_readback_is_unchanged(monkeypatch):
    _common(monkeypatch)
    refusal = RuntimeError(
        "HSCL127D ... HSCL1294 The capacity specified for the logical port with "
        "configuration ID 0 is 7.5. This is not a multiple of the capacity granularity "
        "of the specified protocol of this physical port which is 1.0."
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.assign_sriov_logical_port_dynamic",
        AsyncMock(side_effect=refusal),
    )
    with pytest.raises(SriovLogicalPortPartialError) as caught:
        await assign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            Decimal("2.0"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )
    message = str(caught.value)
    assert "could not be verified" not in message
    assert "refused by HMC" in message
    assert "HSCL1294" in message
    assert caught.value.__cause__ is refusal
    assert caught.value.result.effective_before is None
    assert caught.value.result.effective_after is None
    assert (
        caught.value.result.profile_before
        == caught.value.result.profile_after
        == "none"
    )


@pytest.mark.asyncio
async def test_assign_keeps_unverified_wording_when_readback_also_changed(monkeypatch):
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.assign_sriov_logical_port_dynamic",
        AsyncMock(side_effect=RuntimeError("HSCL0000E command failed unexpectedly")),
    )
    after = {
        "config_id": "0",
        "lpar_name": "lpar",
        "lpar_id": "2",
        "lpar_state": "Not Activated",
        "adapter_id": "1",
        "logical_port_id": "3",
        "phys_port_id": "0",
        "functional_state": "1",
        "capacity": "2.0",
        "max_capacity": "100.0",
    }
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.list_sriov_configured_logical_port_rows",
        AsyncMock(side_effect=[[], [after]]),
    )
    with pytest.raises(SriovLogicalPortPartialError) as caught:
        await assign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            Decimal("2.0"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )
    message = str(caught.value)
    assert "could not be verified" in message
    assert "refused by HMC" not in message
    assert caught.value.result.effective_after is not None


@pytest.mark.asyncio
async def test_unassign_reports_hmc_refusal_when_readback_is_unchanged(monkeypatch):
    _common(monkeypatch)
    record = "0:1:0:3:0:0:0:all::all:0:0:2.0:100.0:none:0::::"
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.read_sriov_profile_ports",
        AsyncMock(
            side_effect=[
                {"name": "prof", "sriov_eth_logical_ports": record},
                {"name": "prof", "sriov_eth_logical_ports": record},
            ]
        ),
    )
    refusal = RuntimeError("HSCL1500E the profile record is currently in use")
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.unassign_sriov_logical_port_profile",
        AsyncMock(side_effect=refusal),
    )
    with pytest.raises(SriovLogicalPortPartialError) as caught:
        await unassign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )
    message = str(caught.value)
    assert "could not be verified" not in message
    assert "refused by HMC" in message
    assert "HSCL1500E" in message
    assert caught.value.__cause__ is refusal
    assert (
        caught.value.result.profile_before
        == caught.value.result.profile_after
        == record
    )


@pytest.mark.asyncio
async def test_unassign_keeps_unverified_wording_when_readback_also_changed(
    monkeypatch,
):
    _common(monkeypatch)
    record = "0:1:0:3:0:0:0:all::all:0:0:2.0:100.0:none:0::::"
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.read_sriov_profile_ports",
        AsyncMock(
            side_effect=[
                {"name": "prof", "sriov_eth_logical_ports": record},
                {"name": "prof", "sriov_eth_logical_ports": "none"},
            ]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.unassign_sriov_logical_port_profile",
        AsyncMock(side_effect=RuntimeError("HSCL0000E command failed unexpectedly")),
    )
    with pytest.raises(SriovLogicalPortPartialError) as caught:
        await unassign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )
    message = str(caught.value)
    assert "could not be verified" in message
    assert "refused by HMC" not in message
    assert caught.value.result.profile_after == "none"


@pytest.mark.asyncio
async def test_unassign_wraps_post_dispatch_read_failure(monkeypatch):
    _common(monkeypatch)
    record = "0:1:0:3:0:0:0:all::all:0:0:2.0:100.0:none:0::::"
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.read_sriov_profile_ports",
        AsyncMock(
            side_effect=[
                {"name": "prof", "sriov_eth_logical_ports": record},
                RuntimeError("read failed"),
            ]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.unassign_sriov_logical_port_profile",
        AsyncMock(return_value=""),
    )
    with pytest.raises(SriovLogicalPortPartialError) as caught:
        await unassign_sriov_logical_port(
            _hmc(),
            "lpar",
            InventorySelector("1", "0", "3"),
            profile_name="prof",
            system_name_or_uuid="sys",
        )
    message = str(caught.value)
    assert "could not be verified" in message
    assert "refused by HMC" not in message
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "read failed"
    assert caught.value.result.profile_before == record
    assert caught.value.result.profile_after is None
