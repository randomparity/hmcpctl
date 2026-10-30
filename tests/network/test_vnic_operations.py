import json
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.operations.virtualization.vnic import (
    VnicBackingSelector,
    VnicCapabilityError,
    VnicChangeResult,
    VnicPartialError,
    _parse_vnic_snapshots,
    add_vnic,
    remove_vnic,
)
from hmcpctl.ssh.transport import HMCCLIError


def test_vnic_models_are_immutable_and_result_field_order_is_stable() -> None:
    selector = VnicBackingSelector("vios-a", "100", "1", "1", Decimal("2.0"))
    with pytest.raises(AttributeError):
        selector.adapter_id = "2"  # ty: ignore[invalid-assignment]
    assert [field.name for field in fields(VnicChangeResult)] == [
        "operation",
        "mutation_dispatched",
        "changed",
        "selector",
        "slot_num",
        "vnic_before",
        "backing_before",
        "vnic_after",
        "backing_after",
        "vnic_after_read_succeeded",
        "backing_after_read_succeeded",
        "output",
        "errors",
        "policy_verification",
    ]


_LIVE_VNIC_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "pcie" / "power9-v10r3m1060-live-vnic.json"
)


def _hmc() -> AsyncMock:
    hmc = AsyncMock()
    hmc.config = HMCConfig(
        host="h",
        user="u",
        password="p",
    )
    return hmc


def _vnic(
    slot: str = "2", vlan: str = "7", logical: str = "3", **changes: str
) -> dict[str, str]:
    backing = {
        "vios_name": "vios-a",
        "vios_lpar_id": "100",
        "adapter_id": "1",
        "physical_port_id": "1",
        "logical_port_id": logical,
        "capacity": "2.0",
        "desired_capacity": "2.0",
    }
    backing.update(changes)
    return {
        "lpar_name": "client-a",
        "lpar_id": "3",
        "slot_num": slot,
        "port_vlan_id": vlan,
        "backing_devices": (
            "sriov/{vios_name}/{vios_lpar_id}/{adapter_id}/{physical_port_id}/"
            "{logical_port_id}/{capacity}/{desired_capacity}/50/100/100"
        ).format_map(backing),
        "backing_device_states": f"sriov/{backing['logical_port_id']}/1/Operational",
    }


def _backing(logical: str = "3", **changes: str) -> dict[str, str]:
    row = {
        "lpar_name": "vios-a",
        "lpar_id": "100",
        "type": "sriov",
        "adapter_id": "1",
        "physical_port_id": "1",
        "logical_port_id": logical,
        "capacity": "2.0",
        "desired_capacity": "2.0",
        "max_capacity": "100",
        "desired_max_capacity": "100",
        "failover_priority": "50",
        "is_active": "1",
        "status": "Operational",
    }
    row.update(changes)
    return row


def _common(monkeypatch: pytest.MonkeyPatch) -> None:
    module = "hmcpctl.operations.virtualization.vnic"
    monkeypatch.setattr(
        f"{module}.resolve_and_authorize_lpar_names",
        AsyncMock(return_value=("system-a", "client-a")),
    )
    monkeypatch.setattr(f"{module}.require_admitted_environment", AsyncMock())
    monkeypatch.setattr(
        f"{module}.read_vios_identity",
        AsyncMock(
            return_value={"name": "vios-a", "lpar_id": "100", "lpar_env": "vioserver"}
        ),
    )
    monkeypatch.setattr(
        f"{module}.list_sriov_adapter_rows",
        AsyncMock(
            return_value=[
                {"adapter_id": "1", "config_state": "sriov", "functional_state": "1"}
            ]
        ),
    )
    monkeypatch.setattr(
        f"{module}.list_sriov_physical_port_rows",
        AsyncMock(
            return_value=[{"adapter_id": "1", "phys_port_id": "1", "state": "1"}]
        ),
    )
    monkeypatch.setattr(
        f"{module}.list_sriov_configured_logical_port_rows", AsyncMock(return_value=[])
    )


@pytest.mark.parametrize(
    "field", ["vios_name", "vios_lpar_id", "adapter_id", "physical_port_id"]
)
@pytest.mark.asyncio
async def test_add_rejects_blank_selector_fields(field: str) -> None:
    values = {
        "vios_name": "vios-a",
        "vios_lpar_id": "100",
        "adapter_id": "1",
        "physical_port_id": "1",
    }
    values[field] = " "
    selector = VnicBackingSelector(**values, capacity_percent=Decimal(2))
    with pytest.raises(ValueError, match="must not be blank"):
        await add_vnic(_hmc(), "client-a", selector, 7, system_name_or_uuid="system-a")


@pytest.mark.parametrize(
    "capacity", [Decimal(0), Decimal(101), Decimal("NaN"), Decimal("1.001")]
)
@pytest.mark.asyncio
async def test_add_rejects_invalid_capacity(capacity: Decimal) -> None:
    selector = VnicBackingSelector("vios-a", "100", "1", "1", capacity)
    with pytest.raises(ValueError):
        await add_vnic(_hmc(), "client-a", selector, 7, system_name_or_uuid="system-a")


@pytest.mark.parametrize("vlan", [-1, 4095])
@pytest.mark.asyncio
async def test_add_rejects_vlan_out_of_range(vlan: int) -> None:
    selector = VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2))
    with pytest.raises(ValueError, match="between 0 and 4094"):
        await add_vnic(
            _hmc(), "client-a", selector, vlan, system_name_or_uuid="system-a"
        )


@pytest.mark.parametrize("vlan", [7.5, True])
@pytest.mark.asyncio
async def test_add_rejects_non_integer_vlan_before_preflight(
    monkeypatch: pytest.MonkeyPatch, vlan: object
) -> None:
    selector = VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2))
    preflight = AsyncMock()
    mutation = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic._preflight_add", preflight
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutation
    )

    with pytest.raises(ValueError, match="integer between 0 and 4094"):
        await add_vnic(
            object(), "client-a", selector, vlan, system_name_or_uuid="system-a"
        )

    preflight.assert_not_awaited()
    mutation.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "character"),
    [
        (field, character)
        for field in ("vios_name", "vios_lpar_id", "adapter_id", "physical_port_id")
        for character in ("/", ",", "=", '"', "\n")
    ],
)
@pytest.mark.asyncio
async def test_add_rejects_each_delimiter_in_each_selector_field(
    field: str, character: str
) -> None:
    values = {
        "vios_name": "vios-a",
        "vios_lpar_id": "100",
        "adapter_id": "1",
        "physical_port_id": "1",
    }
    values[field] += character
    selector = VnicBackingSelector(**values, capacity_percent=Decimal(2))
    with pytest.raises(ValueError):
        await add_vnic(_hmc(), "client-a", selector, 7, system_name_or_uuid="system-a")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vios_name", "vios $(touch nope)"),
        ("vios_lpar_id", "100; whoami"),
        ("adapter_id", "1 & other"),
        ("physical_port_id", "1 ' port"),
    ],
)
@pytest.mark.asyncio
async def test_add_preserves_shell_metacharacters_as_quoted_payload_data(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    _common(monkeypatch)
    values = {
        "vios_name": "vios-a",
        "vios_lpar_id": "100",
        "adapter_id": "1",
        "physical_port_id": "1",
    }
    values[field] = value
    selector = VnicBackingSelector(**values, capacity_percent=Decimal(2))
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.read_vios_identity",
        AsyncMock(
            return_value={
                "name": values["vios_name"],
                "lpar_id": values["vios_lpar_id"],
                "lpar_env": "vioserver",
            }
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_sriov_adapter_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": values["adapter_id"],
                    "config_state": "sriov",
                    "functional_state": "1",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_sriov_physical_port_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": values["adapter_id"],
                    "phys_port_id": values["physical_port_id"],
                    "state": "1",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[], []]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[], []]),
    )
    mutate = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutate
    )

    with pytest.raises(VnicPartialError):
        await add_vnic(_hmc(), "client-a", selector, 7, system_name_or_uuid="system-a")

    mutate.assert_awaited_once()
    assert mutate.await_args_list[0].args[3] == "/".join(
        (
            "sriov",
            values["vios_name"],
            values["vios_lpar_id"],
            values["adapter_id"],
            values["physical_port_id"],
            "2",
        )
    )


@pytest.mark.parametrize(
    "identity",
    [
        {"name": "vios-b", "lpar_id": "100", "lpar_env": "vioserver"},
        {"name": "vios-a", "lpar_id": "101", "lpar_env": "vioserver"},
        {"name": "vios-a", "lpar_id": "100", "lpar_env": "aixlinux"},
    ],
)
@pytest.mark.asyncio
async def test_add_rejects_wrong_vios_identity_or_type(
    monkeypatch: pytest.MonkeyPatch, identity: dict[str, str]
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.read_vios_identity",
        AsyncMock(return_value=identity),
    )
    with pytest.raises(VnicCapabilityError, match="name, ID, or partition type"):
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
        )


@pytest.mark.parametrize(
    "inventory",
    [
        "missing-adapter",
        "wrong-adapter",
        "degraded-adapter",
        "missing-port",
        "wrong-port",
        "inactive-port",
    ],
)
@pytest.mark.asyncio
async def test_add_rejects_adapter_or_port_mismatch(
    monkeypatch: pytest.MonkeyPatch, inventory: str
) -> None:
    _common(monkeypatch)
    if "adapter" in inventory:
        rows = (
            []
            if inventory == "missing-adapter"
            else [
                {
                    "adapter_id": "2" if inventory == "wrong-adapter" else "1",
                    "config_state": "sriov",
                    "functional_state": "0" if inventory == "degraded-adapter" else "1",
                }
            ]
        )
        monkeypatch.setattr(
            "hmcpctl.operations.virtualization.vnic.list_sriov_adapter_rows",
            AsyncMock(return_value=rows),
        )
    else:
        rows = (
            []
            if inventory == "missing-port"
            else [
                {
                    "adapter_id": "1",
                    "phys_port_id": "2" if inventory == "wrong-port" else "1",
                    "state": "0" if inventory == "inactive-port" else "1",
                }
            ]
        )
        monkeypatch.setattr(
            "hmcpctl.operations.virtualization.vnic.list_sriov_physical_port_rows",
            AsyncMock(return_value=rows),
        )
    with pytest.raises(VnicCapabilityError):
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
        )


@pytest.mark.asyncio
async def test_add_rejects_exhausted_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_sriov_configured_logical_port_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": "1",
                    "logical_port_id": "9",
                    "phys_port_id": "1",
                    "capacity": "99",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=[]),
    )
    with pytest.raises(ValueError, match="capacity exhausted"):
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
        )


@pytest.mark.asyncio
async def test_add_verified_retry_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[_vnic()]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=[_backing()]),
    )
    mutate = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutate
    )
    result = await add_vnic(
        _hmc(),
        "client-a",
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
        7,
        system_name_or_uuid="system-a",
    )
    assert (result.changed, result.slot_num, result.mutation_dispatched) == (
        False,
        "2",
        False,
    )
    mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_verified_retry_resolves_before_new_allocation_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_sriov_configured_logical_port_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": "1",
                    "logical_port_id": "3",
                    "phys_port_id": "1",
                    "capacity": "60",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[_vnic(capacity="60", desired_capacity="60")]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=[_backing(capacity="60", desired_capacity="60")]),
    )
    mutate = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutate
    )

    result = await add_vnic(
        _hmc(),
        "client-a",
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(60)),
        7,
        system_name_or_uuid="system-a",
    )

    assert (result.changed, result.slot_num, result.mutation_dispatched) == (
        False,
        "2",
        False,
    )
    mutate.assert_not_awaited()


@pytest.mark.parametrize("projection", ["direct", "backing"])
@pytest.mark.asyncio
async def test_add_rejects_identical_duplicates_within_one_projection(
    monkeypatch: pytest.MonkeyPatch, projection: str
) -> None:
    _common(monkeypatch)
    direct_row = {
        "adapter_id": "1",
        "logical_port_id": "3",
        "phys_port_id": "1",
        "capacity": "2.0",
    }
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_sriov_configured_logical_port_rows",
        AsyncMock(
            return_value=[direct_row, direct_row] if projection == "direct" else []
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[]),
    )
    backing_rows = [_backing(), _backing()] if projection == "backing" else []
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=backing_rows),
    )
    mutate = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutate
    )

    with pytest.raises(ValueError, match=f"duplicate {projection}"):
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
        )

    mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_deduplicates_consistent_direct_and_backing_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_sriov_configured_logical_port_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": "1",
                    "logical_port_id": "3",
                    "phys_port_id": "1",
                    "capacity": "98.0",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=[_backing(capacity="50", desired_capacity="98.0")]),
    )
    mutate = AsyncMock(side_effect=RuntimeError("dispatched"))
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutate
    )

    with pytest.raises(VnicPartialError):
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
        )

    mutate.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_successfully_correlates_new_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[], [_vnic()]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[], [_backing()]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing",
        AsyncMock(return_value="created"),
    )
    result = await add_vnic(
        _hmc(),
        "client-a",
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
        7,
        system_name_or_uuid="system-a",
    )
    assert (result.changed, result.slot_num, result.output) == (True, "2", "created")


@pytest.mark.asyncio
async def test_add_ignores_unrelated_equal_selector_backing_for_target_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    unrelated = _backing(logical="9")
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[], [_vnic()]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[unrelated], [unrelated, _backing()]]),
    )
    mutation = AsyncMock(return_value="created")
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutation
    )

    result = await add_vnic(
        _hmc(),
        "client-a",
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
        7,
        system_name_or_uuid="system-a",
    )

    assert result.changed is True
    assert result.backing_before == ()
    assert tuple(item.logical_port_id for item in result.backing_after) == ("3",)
    mutation.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_before_state_after_dispatch_is_known_unchanged_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[], []]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[], []]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", AsyncMock()
    )

    with pytest.raises(VnicPartialError) as caught:
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
            pvid_priority=0,
        )

    result = caught.value.result
    assert result.mutation_dispatched
    assert result.changed is False
    assert result.slot_num is None
    assert result.vnic_after == ()
    assert result.backing_after == ()

    assert result.policy_verification.status == "unavailable"
    assert result.policy_verification.unavailable_fields == ("pvid_priority",)


@pytest.mark.parametrize(
    ("case", "vnic_after", "backing_after", "mutation_error", "expected_changed"),
    [
        ("final-success", [_vnic()], [_backing()], None, True),
        ("final-command-error", [_vnic()], [_backing()], RuntimeError("command"), True),
        ("before", [], [], None, False),
        ("contradictory", [_vnic()], [], None, None),
        ("one-read", [_vnic()], TimeoutError("backing timeout"), None, None),
        (
            "neither-read",
            TimeoutError("vnic timeout"),
            TimeoutError("backing timeout"),
            None,
            None,
        ),
    ],
)
@pytest.mark.asyncio
async def test_add_reconciliation_decision_table(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    vnic_after: list[dict[str, str]] | Exception,
    backing_after: list[dict[str, str]] | Exception,
    mutation_error: Exception | None,
    expected_changed: bool | None,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[], vnic_after]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[], backing_after]),
    )
    mutation = AsyncMock(return_value="created")
    if mutation_error is not None:
        mutation.side_effect = mutation_error
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutation
    )
    call = add_vnic(
        _hmc(),
        "client-a",
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
        7,
        system_name_or_uuid="system-a",
    )

    if case == "final-success":
        result = await call
    else:
        with pytest.raises(VnicPartialError) as caught:
            await call
        result = caught.value.result
        expected_cause = mutation_error
        if expected_cause is None and isinstance(vnic_after, Exception):
            expected_cause = vnic_after
        if expected_cause is None and isinstance(backing_after, Exception):
            expected_cause = backing_after
        assert caught.value.__cause__ is expected_cause

    assert result.changed is expected_changed
    assert result.vnic_after_read_succeeded is not isinstance(vnic_after, Exception)
    assert result.backing_after_read_succeeded is not isinstance(
        backing_after, Exception
    )


@pytest.mark.asyncio
async def test_add_retry_ignores_unrelated_selector_matching_degraded_backing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[_vnic()]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=[_backing(), _backing(logical="4", is_active="0")]),
    )
    mutate = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutate
    )

    result = await add_vnic(
        _hmc(),
        "client-a",
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
        7,
        system_name_or_uuid="system-a",
    )

    assert result.changed is False
    assert tuple(item.logical_port_id for item in result.backing_before) == ("3",)
    mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_final_ignores_unrelated_selector_matching_degraded_backing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[], [_vnic()]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(
            side_effect=[[], [_backing(), _backing(logical="4", status="Degraded")]]
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing",
        AsyncMock(return_value="created"),
    )

    result = await add_vnic(
        _hmc(),
        "client-a",
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
        7,
        system_name_or_uuid="system-a",
    )

    assert result.changed is True
    assert result.slot_num == "2"
    assert tuple(item.logical_port_id for item in result.backing_after) == ("3",)


@pytest.mark.asyncio
async def test_add_retry_refuses_degraded_correlated_target_backing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[_vnic()]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=[_backing(status="Degraded")]),
    )
    mutation = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutation
    )

    with pytest.raises(VnicCapabilityError, match="ambiguous or degraded"):
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
        )

    mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_rejects_two_new_matching_vnics_despite_one_operational_backing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[], [_vnic(), _vnic(slot="3", logical="4")]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[], [_backing()]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing",
        AsyncMock(return_value="created"),
    )

    with pytest.raises(VnicPartialError) as caught:
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
        )

    result = caught.value.result
    assert result.changed is None
    assert result.slot_num is None
    assert tuple(item.slot_num for item in result.vnic_after) == ("2", "3")
    assert result.vnic_after_read_succeeded
    assert result.backing_after_read_succeeded


@pytest.mark.asyncio
async def test_add_successful_reads_with_only_new_vnic_are_contradictory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[], [_vnic()]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[], []]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", AsyncMock()
    )

    with pytest.raises(VnicPartialError) as caught:
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
        )

    result = caught.value.result
    assert result.changed is None
    assert result.slot_num == "2"
    assert result.vnic_after_read_succeeded
    assert result.backing_after_read_succeeded
    assert len(result.vnic_after) == 1
    assert result.backing_after == ()


@pytest.mark.asyncio
async def test_add_command_and_both_read_failures_are_retained_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[], TimeoutError("vnic read")]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[], OSError("backing read")]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing",
        AsyncMock(side_effect=RuntimeError("HMC rejected VLAN")),
    )
    with pytest.raises(VnicPartialError) as caught:
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
            pvid_priority=0,
        )
    result = caught.value.result
    assert result.changed is None
    assert (
        not result.vnic_after_read_succeeded and not result.backing_after_read_succeeded
    )
    assert "HMC rejected VLAN" in result.errors[0]
    assert "vnic read" in result.errors[1]
    assert "backing read" in result.errors[2]

    assert result.policy_verification.status == "unavailable"
    assert result.policy_verification.unavailable_fields == ("pvid_priority",)


@pytest.mark.asyncio
async def test_remove_absent_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=[]),
    )
    result = await remove_vnic(_hmc(), "client-a", "2", system_name_or_uuid="system-a")
    assert (result.changed, result.selector, result.slot_num) == (False, None, "2")


@pytest.mark.asyncio
async def test_remove_success_preserves_captured_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[_vnic()], []]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[_backing()], []]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.remove_vnic_slot",
        AsyncMock(return_value="removed"),
    )
    result = await remove_vnic(_hmc(), "client-a", "2", system_name_or_uuid="system-a")
    assert result.changed is True
    assert result.selector == VnicBackingSelector(
        "vios-a", "100", "1", "1", Decimal("2.0")
    )


@pytest.mark.parametrize(
    ("case", "vnic_after", "backing_after", "mutation_error", "expected_changed"),
    [
        ("final-success", [], [], None, True),
        ("final-command-error", [], [], RuntimeError("command"), True),
        ("before", [_vnic()], [_backing()], None, False),
        ("contradictory", [], [_backing()], None, None),
        ("one-read", [], TimeoutError("backing timeout"), None, None),
        (
            "neither-read",
            TimeoutError("vnic timeout"),
            TimeoutError("backing timeout"),
            None,
            None,
        ),
    ],
)
@pytest.mark.asyncio
async def test_remove_reconciliation_decision_table(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    vnic_after: list[dict[str, str]] | Exception,
    backing_after: list[dict[str, str]] | Exception,
    mutation_error: Exception | None,
    expected_changed: bool | None,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[_vnic()], vnic_after]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[_backing()], backing_after]),
    )
    mutation = AsyncMock(return_value="removed")
    if mutation_error is not None:
        mutation.side_effect = mutation_error
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.remove_vnic_slot", mutation
    )
    call = remove_vnic(_hmc(), "client-a", "2", system_name_or_uuid="system-a")

    if case == "final-success":
        result = await call
    else:
        with pytest.raises(VnicPartialError) as caught:
            await call
        result = caught.value.result

    assert result.changed is expected_changed
    assert result.vnic_after_read_succeeded is not isinstance(vnic_after, Exception)
    assert result.backing_after_read_succeeded is not isinstance(
        backing_after, Exception
    )


@pytest.mark.parametrize(
    "rows", [[], [_backing(is_active="0")], [_backing(), _backing()]]
)
@pytest.mark.asyncio
async def test_remove_refuses_uncorrelated_or_degraded_backing(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, str]]
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[_vnic()]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=rows),
    )
    with pytest.raises(VnicCapabilityError):
        await remove_vnic(_hmc(), "client-a", "2", system_name_or_uuid="system-a")


@pytest.mark.parametrize("backing_devices", ["none", None, "extra"])
@pytest.mark.asyncio
async def test_remove_requires_exactly_one_embedded_backing_before_mutation(
    monkeypatch: pytest.MonkeyPatch, backing_devices: str | None
) -> None:
    _common(monkeypatch)
    row = _vnic()
    if backing_devices is None:
        row["backing_devices"] = ""
    elif backing_devices == "extra":
        second = _vnic(logical="4")
        row["backing_devices"] = f"{row['backing_devices']},{second['backing_devices']}"
        row["backing_device_states"] += f",{second['backing_device_states']}"
    else:
        row["backing_devices"] = backing_devices
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[row]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=[_backing()]),
    )
    mutate = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.remove_vnic_slot", mutate
    )

    with pytest.raises(VnicCapabilityError, match="exactly one embedded backing"):
        await remove_vnic(_hmc(), "client-a", "2", system_name_or_uuid="system-a")

    mutate.assert_not_awaited()


@pytest.mark.parametrize(
    ("vnic_changes", "backing_changes"),
    [
        ({}, {"lpar_name": "vios-b"}),
        ({}, {"lpar_id": "101"}),
        ({}, {"adapter_id": "2"}),
        ({}, {"physical_port_id": "2"}),
        ({}, {"logical_port_id": "4"}),
        ({}, {"capacity": "3.0"}),
        ({}, {"desired_capacity": "3.0"}),
    ],
)
@pytest.mark.asyncio
async def test_remove_requires_full_backing_correlation(
    monkeypatch: pytest.MonkeyPatch,
    vnic_changes: dict[str, str],
    backing_changes: dict[str, str],
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(return_value=[_vnic(**vnic_changes)]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(return_value=[_backing(**backing_changes)]),
    )

    with pytest.raises(VnicCapabilityError):
        await remove_vnic(_hmc(), "client-a", "2", system_name_or_uuid="system-a")


@pytest.mark.parametrize(
    "replacement",
    [
        {"lpar_name": "vios-b"},
        {"lpar_id": "101"},
        {"physical_port_id": "2"},
        {"capacity": "3.0"},
        {"desired_capacity": "3.0"},
    ],
)
@pytest.mark.asyncio
async def test_remove_ignores_distinct_backing_reusing_adapter_and_logical_port(
    monkeypatch: pytest.MonkeyPatch, replacement: dict[str, str]
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[_vnic()], []]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[_backing()], [_backing(**replacement)]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.remove_vnic_slot",
        AsyncMock(return_value="removed"),
    )

    result = await remove_vnic(_hmc(), "client-a", "2", system_name_or_uuid="system-a")

    assert result.changed is True
    assert result.backing_after == ()


@pytest.mark.asyncio
async def test_remove_successful_reads_with_changed_slot_are_contradictory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[_vnic()], [_vnic(vlan="8")]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(side_effect=[[_backing()], [_backing()]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.remove_vnic_slot", AsyncMock()
    )

    with pytest.raises(VnicPartialError) as caught:
        await remove_vnic(_hmc(), "client-a", "2", system_name_or_uuid="system-a")

    result = caught.value.result
    assert result.changed is None
    assert result.vnic_after_read_succeeded
    assert result.backing_after_read_succeeded
    assert result.vnic_after[0].port_vlan_id == 8
    assert len(result.backing_after) == 1


def _granularity_port(monkeypatch: pytest.MonkeyPatch, granularity: str) -> AsyncMock:
    """Wire an empty inventory whose physical port reports ``granularity``; return the mutation."""
    module = "hmcpctl.operations.virtualization.vnic"
    _common(monkeypatch)
    monkeypatch.setattr(
        f"{module}.list_sriov_physical_port_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": "1",
                    "phys_port_id": "1",
                    "state": "1",
                    "min_eth_capacity_granularity": granularity,
                }
            ]
        ),
    )
    monkeypatch.setattr(f"{module}.list_vnic_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(f"{module}.list_vnic_backing_rows", AsyncMock(return_value=[]))
    mutate = AsyncMock(side_effect=RuntimeError("dispatched"))
    monkeypatch.setattr(f"{module}.add_vnic_backing", mutate)
    return mutate


async def _add_at(capacity: str) -> VnicChangeResult:
    return await add_vnic(
        _hmc(),
        "client-a",
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(capacity)),
        7,
        system_name_or_uuid="system-a",
    )


@pytest.mark.asyncio
async def test_add_refuses_capacity_off_the_port_granularity_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutate = _granularity_port(monkeypatch, "1.0")
    with pytest.raises(
        ValueError,
        match=r"capacity_percent 7\.5 is not a multiple of the physical port's "
        r"capacity granularity 1\.0%",
    ):
        await _add_at("7.5")
    mutate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("granularity", "capacity"), [("2.0", "4"), ("", "7.5")])
async def test_add_dispatches_on_or_without_port_granularity(
    monkeypatch: pytest.MonkeyPatch, granularity: str, capacity: str
) -> None:
    mutate = _granularity_port(monkeypatch, granularity)
    with pytest.raises(VnicPartialError):
        await _add_at(capacity)
    mutate.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_rejects_malformed_port_granularity_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutate = _granularity_port(monkeypatch, "abc")
    with pytest.raises(
        HMCCLIError, match="malformed physical-port capacity granularity"
    ):
        await _add_at("2")
    mutate.assert_not_awaited()


_SENTINEL = "No results were found.\n"


def _csv(fields: tuple[str, ...], row: dict[str, str]) -> str:
    return ",".join(fields) + "\n" + ",".join(row.get(f, "") for f in fields) + "\n"


def _sentinel_hmc(
    monkeypatch: pytest.MonkeyPatch, after: dict[str, str] | None
) -> list[str]:
    """Stub the SSH transport: the HMC replies with its sentinel until a mutation."""
    from hmcpctl.ssh import vnic as ssh_vnic

    commands: list[str] = []

    async def fake_run(_config: object, command: str) -> str:
        commands.append(command)
        if command.startswith("chhwres"):
            return ""
        mutated = any(c.startswith("chhwres") for c in commands)
        if after is None or not mutated:
            return _SENTINEL
        if "vnicbkdev" in command:
            return _csv(ssh_vnic._VNIC_BACKING_FIELDS, _backing())
        return _csv(ssh_vnic._VNIC_FIELDS, after)

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    return commands


@pytest.mark.asyncio
async def test_add_on_partition_with_no_vnics_reads_sentinel_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    commands = _sentinel_hmc(monkeypatch, _vnic())
    result = await add_vnic(
        _hmc(),
        "client-a",
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
        7,
        system_name_or_uuid="system-a",
    )
    assert (result.changed, result.slot_num, result.vnic_before) == (True, "2", ())
    assert sum(c.startswith("chhwres") for c in commands) == 1


@pytest.mark.asyncio
async def test_remove_on_partition_with_no_vnics_reads_sentinel_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _common(monkeypatch)
    commands = _sentinel_hmc(monkeypatch, None)
    result = await remove_vnic(_hmc(), "client-a", "2", system_name_or_uuid="system-a")
    assert (result.changed, result.selector, result.mutation_dispatched) == (
        False,
        None,
        False,
    )
    assert not any(c.startswith("chhwres") for c in commands)


def test_snapshot_embedded_backing_reports_hmc_state() -> None:
    """Recorded live shape: state is joined to the backing by logical port ID."""
    fixture = json.loads(_LIVE_VNIC_FIXTURE.read_text())
    stdout = fixture["probes"][0]["stdout"]
    header, values = (line.split(",") for line in stdout.splitlines())
    row = dict(zip(header, values, strict=True))

    (snapshot,) = _parse_vnic_snapshots([row])

    assert snapshot.backing_devices[0].is_active is True
    assert snapshot.backing_devices[0].status == "Operational"


def test_snapshot_embedded_backing_reports_inactive_state() -> None:
    row = _vnic(logical="3")
    row["backing_device_states"] = "sriov/3/0/Degraded"

    (snapshot,) = _parse_vnic_snapshots([row])

    assert snapshot.backing_devices[0].is_active is False
    assert snapshot.backing_devices[0].status == "Degraded"


@pytest.mark.parametrize(
    "states",
    [
        "",
        "none",
        "sriov/3/1",
        "sriov/3/2/Operational",
        "sriov/4/1/Operational",
        "sriov/3/1/Operational,sriov/3/0/Operational",
        "sriov/3/1/Operational,sriov/4/1/Operational",
    ],
)
def test_snapshot_rejects_uncorrelatable_backing_states(states: str) -> None:
    row = _vnic(logical="3")
    row["backing_device_states"] = states

    with pytest.raises(ValueError, match="backing_device_states"):
        _parse_vnic_snapshots([row])


_POLICY = {
    "pvid_priority": 0,
    "allowed_vlan_ids": [2, 4094],
    "allowed_os_mac_addrs": ["AABBCCDDEEFF", "001122334455"],
    "auto_priority_failover": False,
    "mac_addr": "AABBCCDDEE01",
}
_NATIVE_POLICY = {
    "pvid_priority": "0",
    "allowed_vlan_ids": "4094,2",
    "allowed_os_mac_addrs": "aabbccddeeff,001122334455",
    "auto_priority_failover": "0",
    "mac_addr": "aabbccddee01",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"pvid_priority": True},
        {"pvid_priority": 8},
        {"pvid_priority": -1},
        {"auto_priority_failover": 0},
        {"mac_addr": "aa:bb:cc:dd:ee:ff"},
        {"mac_addr": "aabbccddee=f"},
        {"allowed_vlan_ids": []},
        {"allowed_vlan_ids": [0]},
        {"allowed_vlan_ids": [4095]},
        {"allowed_vlan_ids": [True]},
        {"allowed_vlan_ids": [2, 2]},
        {"allowed_vlan_ids": "2,3"},
        {"allowed_os_mac_addrs": []},
        {"allowed_os_mac_addrs": ["AABBCCDDEEFF", "aabbccddeeff"]},
        {"allowed_os_mac_addrs": ['aabbccddee"f']},
    ],
)
async def test_policy_invalid_before_preflight(monkeypatch, bad):
    preflight = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic._preflight_add", preflight
    )
    with pytest.raises(ValueError):
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
            **bad,
        )
    preflight.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("defect", [None, "missing", "mismatch", "malformed"])
async def test_selected_policy_reconciliation(monkeypatch, existing, defect):
    _common(monkeypatch)
    row = {**_vnic(), **_NATIVE_POLICY}
    if defect == "missing":
        del row["pvid_priority"]
    elif defect == "mismatch":
        row["pvid_priority"] = "7"
    elif defect == "malformed":
        row["pvid_priority"] = "garbage"
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_rows",
        AsyncMock(side_effect=[[row], [row]] if existing else [[], [row]]),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.list_vnic_backing_rows",
        AsyncMock(
            side_effect=[[_backing()], [_backing()]] if existing else [[], [_backing()]]
        ),
    )
    mutate = AsyncMock(return_value="ok")
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic.add_vnic_backing", mutate
    )

    async def run():
        return await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
            **_POLICY,
        )

    if existing and defect:
        with pytest.raises(VnicCapabilityError, match="selected policies"):
            await run()
        mutate.assert_not_awaited()
        return
    if defect:
        with pytest.raises(VnicPartialError) as caught:
            await run()
        result = caught.value.result
        assert result.changed is True
        verification = result.policy_verification
        assert verification.status == (
            "mismatched" if defect == "mismatch" else "unavailable"
        )
        assert (
            verification.mismatched_fields
            if defect == "mismatch"
            else verification.unavailable_fields
        ) == ("pvid_priority",)
    else:
        result = await run()
        assert result.changed is not existing
        assert result.policy_verification.status == "verified"
    assert set(result.policy_verification.checked_fields) == set(_POLICY)
    assert result.vnic_after[0].policy.auto_priority_failover is False
    assert result.vnic_after[0].policy.allowed_vlan_ids == (2, 4094)
    assert mutate.await_count == (0 if existing else 1)
    if not existing:
        assert mutate.await_args.kwargs["policy"] == {
            "pvid_priority": "0",
            "allowed_vlan_ids": "2,4094",
            "allowed_os_mac_addrs": "001122334455,aabbccddeeff",
            "auto_priority_failover": "0",
            "mac_addr": "aabbccddee01",
        }


def test_policy_observations_are_independent_and_do_not_change_topology_equality():
    first = _parse_vnic_snapshots([{**_vnic(), **_NATIVE_POLICY}])[0]
    second = _parse_vnic_snapshots(
        [
            {
                **_vnic(),
                **_NATIVE_POLICY,
                "allowed_vlan_ids": "2,,3",
                "auto_priority_failover": "false",
            }
        ]
    )[0]
    assert first == second
    assert second.policy.allowed_vlan_ids is None
    assert second.policy.auto_priority_failover is None
    assert second.policy.pvid_priority == 0
    assert second.policy.mac_addr == "aabbccddee01"


@pytest.mark.parametrize("sentinel", ["all", "none"])
def test_policy_sentinels_and_omitted_selection(sentinel):
    from hmcpctl.operations.virtualization.vnic_policy import (
        creation_policy,
        observed_policy,
        policy_attributes,
        verify_policy,
    )

    selected = creation_policy(allowed_vlan_ids=sentinel, allowed_os_mac_addrs=sentinel)
    assert policy_attributes(selected) == {
        "allowed_vlan_ids": sentinel,
        "allowed_os_mac_addrs": sentinel,
    }
    assert (
        verify_policy(
            selected,
            observed_policy(
                {"allowed_vlan_ids": sentinel, "allowed_os_mac_addrs": sentinel}
            ),
        ).status
        == "verified"
    )
    assert verify_policy(creation_policy(), None).status == "not_requested"
    assert verify_policy(selected, None).unavailable_fields == (
        "allowed_vlan_ids",
        "allowed_os_mac_addrs",
    )


def test_no_unproven_hardware_count_caps():
    from hmcpctl.operations.virtualization.vnic_policy import creation_policy

    policy = creation_policy(
        allowed_vlan_ids=list(range(1, 22)),
        allowed_os_mac_addrs=[f"{n:012x}" for n in range(5)],
    )
    assert len(policy.allowed_vlan_ids) == 21
    assert len(policy.allowed_os_mac_addrs) == 5


@pytest.mark.asyncio
async def test_complete_policy_record_is_bounded_before_preflight(monkeypatch):
    preflight = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.vnic._preflight_add", preflight
    )
    with pytest.raises(ValueError, match="65536-byte"):
        await add_vnic(
            _hmc(),
            "client-a",
            VnicBackingSelector("vios-a", "100", "1", "1", Decimal(2)),
            7,
            system_name_or_uuid="system-a",
            allowed_os_mac_addrs=[f"{n:012x}" for n in range(5100)],
        )
    preflight.assert_not_awaited()


@pytest.mark.parametrize(
    "name,raw",
    [
        ("pvid_priority", "8"),
        ("allowed_vlan_ids", "1,1"),
        ("allowed_os_mac_addrs", "not-a-mac"),
        ("auto_priority_failover", "true"),
        ("mac_addr", "123"),
    ],
)
def test_each_malformed_policy_observation_remains_independent(name, raw):
    from hmcpctl.operations.virtualization.vnic_policy import (
        creation_policy,
        observed_policy,
        verify_policy,
    )

    observation = observed_policy({**_NATIVE_POLICY, name: raw})
    verification = verify_policy(creation_policy(**_POLICY), observation)
    assert verification.status == "unavailable"
    assert verification.unavailable_fields == (name,)
    assert verification.mismatched_fields == ()
