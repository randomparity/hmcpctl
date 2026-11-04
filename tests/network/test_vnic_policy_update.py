from __future__ import annotations

from dataclasses import fields
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.operations.virtualization.vnic import (
    VnicCapabilityError,
    VnicPartialError,
    VnicPolicyUpdateResult,
    update_vnic_policy,
)
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.ssh.vnic import build_vnic_update_attributes

_MODULE = "hmcpctl.operations.virtualization.vnic"
_MAC_1 = "0a0b0c0d0e01"
_MAC_2 = "0a0b0c0d0e02"
_MAC_3 = "0a0b0c0d0e03"


def _hmc() -> AsyncMock:
    hmc = AsyncMock()
    hmc.config = HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"})
    return hmc


def _row(slot: str = "4", **changes: str) -> dict[str, str]:
    row = {
        "lpar_name": "client-a",
        "lpar_id": "3",
        "slot_num": slot,
        "desired_mode": "ded",
        "curr_mode": "ded",
        "auto_priority_failover": "0",
        "port_vlan_id": "0",
        "pvid_priority": "0",
        "allowed_vlan_ids": "10",
        "mac_addr": "0a0b0c0d0eff",
        "allowed_os_mac_addrs": _MAC_1,
        "backing_devices": "sriov/vios-a/100/1/1/3/2.0/2.0/50/100/100",
        "backing_device_states": "sriov/3/1/Operational",
    }
    row.update(changes)
    return row


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    before: list[dict[str, str]],
    after: list[dict[str, str]] | Exception | None = None,
    submit: object = "",
) -> tuple[AsyncMock, AsyncMock]:
    monkeypatch.setattr(
        f"{_MODULE}.resolve_and_authorize_lpar_names",
        AsyncMock(return_value=("system-a", "client-a")),
    )
    monkeypatch.setattr(f"{_MODULE}.require_admitted_environment", AsyncMock())
    reads = AsyncMock(side_effect=[before, before if after is None else after])
    monkeypatch.setattr(f"{_MODULE}.list_vnic_rows", reads)
    mutation = AsyncMock(
        side_effect=submit if isinstance(submit, Exception) else None,
        return_value=submit,
    )
    monkeypatch.setattr(f"{_MODULE}.update_vnic_slot_policy", mutation)
    return reads, mutation


async def _update(**inputs: object) -> VnicPolicyUpdateResult:
    return await update_vnic_policy(
        _hmc(), "client-a", "4", system_name_or_uuid="system-a", **inputs
    )


def test_result_field_order_is_stable() -> None:
    assert [item.name for item in fields(VnicPolicyUpdateResult)] == [
        "operation",
        "slot_num",
        "mutation_dispatched",
        "changed",
        "record",
        "submission",
        "refusal_code",
        "vnic_before",
        "vnic_after",
        "readback",
        "preservation",
        "drifted_fields",
        "output",
        "errors",
    ]


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({}, "exactly one"),
        ({"pvid_priority": 1, "auto_priority_failover": True}, "exactly one"),
        ({"add_allowed_vlan_ids": [20], "add_allowed_os_mac_addrs": [_MAC_2]}, "one"),
        ({"pvid_priority": 8}, "pvid_priority"),
        ({"pvid_priority": True}, "pvid_priority"),
        ({"auto_priority_failover": 1}, "boolean"),
        ({"port_vlan_id": 0}, "port_vlan_id"),
        ({"port_vlan_id": 4095}, "port_vlan_id"),
        ({"port_vlan_id": "7"}, "port_vlan_id"),
        ({"add_allowed_vlan_ids": []}, "nonempty"),
        ({"add_allowed_vlan_ids": "none"}, "nonempty"),
        ({"add_allowed_vlan_ids": [20, 20]}, "duplicate"),
        ({"add_allowed_vlan_ids": [0]}, "between 1 and 4094"),
        ({"add_allowed_os_mac_addrs": ["all"]}, "12 hexadecimal"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_requests_are_refused_before_any_hmc_call(
    monkeypatch, inputs, message
) -> None:
    reads, mutation = _setup(monkeypatch, [_row()])
    with pytest.raises(ValueError, match=message):
        await _update(**inputs)
    reads.assert_not_awaited()
    mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsafe_slot_is_refused_before_any_hmc_call(monkeypatch) -> None:
    reads, mutation = _setup(monkeypatch, [_row()])
    with pytest.raises(ValueError, match="slot_num"):
        await update_vnic_policy(
            _hmc(), "client-a", "4,x", system_name_or_uuid="s", pvid_priority=1
        )
    reads.assert_not_awaited()
    mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_ownership_refusal_precedes_every_read(monkeypatch) -> None:
    reads, mutation = _setup(monkeypatch, [_row()])
    monkeypatch.setattr(
        f"{_MODULE}.resolve_and_authorize_lpar_names",
        AsyncMock(side_effect=PermissionError("not owned")),
    )
    with pytest.raises(PermissionError):
        await _update(pvid_priority=1)
    reads.assert_not_awaited()
    mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_slot_is_a_capability_error(monkeypatch) -> None:
    _, mutation = _setup(monkeypatch, [_row(slot="5")])
    with pytest.raises(VnicCapabilityError, match="slot"):
        await _update(pvid_priority=1)
    mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_slot_rows_fail_parsing(monkeypatch) -> None:
    _, mutation = _setup(monkeypatch, [_row(), _row()])
    with pytest.raises(ValueError, match="duplicate"):
        await _update(pvid_priority=1)
    mutation.assert_not_awaited()


@pytest.mark.parametrize(
    ("baseline", "inputs", "message"),
    [
        ({"port_vlan_id": "7", "allowed_vlan_ids": "none"}, {"port_vlan_id": 9}, "0"),
        ({"allowed_vlan_ids": "10"}, {"port_vlan_id": 9}, "HSCLAB12"),
        ({"allowed_vlan_ids": "none"}, {"add_allowed_vlan_ids": [20]}, "finite"),
        ({"allowed_vlan_ids": "all"}, {"add_allowed_vlan_ids": [20]}, "finite"),
        (
            {"allowed_os_mac_addrs": "x"},
            {"add_allowed_os_mac_addrs": [_MAC_2]},
            "finite",
        ),
        ({"auto_priority_failover": ""}, {"pvid_priority": 3}, "unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_unqualified_modes_are_refused_before_any_effect(
    monkeypatch, baseline, inputs, message
) -> None:
    _, mutation = _setup(monkeypatch, [_row(**baseline)])
    with pytest.raises(VnicCapabilityError, match=message):
        await _update(**inputs)
    mutation.assert_not_awaited()


@pytest.mark.parametrize(
    "inputs",
    [
        {"pvid_priority": 0},
        {"auto_priority_failover": False},
        {"port_vlan_id": 7},
        {"add_allowed_vlan_ids": [10]},
        {"add_allowed_os_mac_addrs": [_MAC_1.upper()]},
    ],
)
@pytest.mark.asyncio
async def test_a_satisfied_request_returns_without_dispatch(
    monkeypatch, inputs
) -> None:
    reads, mutation = _setup(
        monkeypatch, [_row(port_vlan_id="7" if "port_vlan_id" in inputs else "0")]
    )
    result = await _update(**inputs)
    mutation.assert_not_awaited()
    assert reads.await_count == 1
    assert (result.mutation_dispatched, result.changed, result.submission) == (
        False,
        False,
        "not_submitted",
    )
    assert (result.record, result.preservation) == ("", "not_applicable")
    assert result.readback.status == "verified"


@pytest.mark.parametrize(
    ("baseline", "inputs", "record", "after"),
    [
        (
            {},
            {"pvid_priority": 5},
            "pvid_priority=5,auto_priority_failover=0",
            {"pvid_priority": "5"},
        ),
        (
            {},
            {"auto_priority_failover": True},
            "auto_priority_failover=1",
            {"auto_priority_failover": "1"},
        ),
        (
            {"allowed_vlan_ids": "none", "auto_priority_failover": "1"},
            {"port_vlan_id": 7},
            "port_vlan_id=7,auto_priority_failover=1",
            {"port_vlan_id": "7"},
        ),
        (
            {},
            {"add_allowed_vlan_ids": [30, 10, 20]},
            'auto_priority_failover=0,"allowed_vlan_ids+=20,30"',
            {"allowed_vlan_ids": "30,10,20"},
        ),
        (
            {},
            {"add_allowed_vlan_ids": [20]},
            "auto_priority_failover=0,allowed_vlan_ids+=20",
            {"allowed_vlan_ids": "10,20"},
        ),
        (
            {},
            {"add_allowed_os_mac_addrs": [_MAC_3.upper(), _MAC_2]},
            f'auto_priority_failover=0,"allowed_os_mac_addrs+={_MAC_2},{_MAC_3}"',
            {"allowed_os_mac_addrs": f"{_MAC_1},{_MAC_2},{_MAC_3.upper()}"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_record_shape_completes_verified_and_unchanged(
    monkeypatch, baseline, inputs, record, after
) -> None:
    other = _row(slot="9", pvid_priority="3")
    before = [_row(**baseline), other]
    _, mutation = _setup(
        monkeypatch, before, [_row(**{**baseline, **after}), other], "ok"
    )
    result = await _update(**inputs)
    mutation.assert_awaited_once()
    assert mutation.await_args.args[1:4] == ("system-a", "client-a", "4")
    assert build_vnic_update_attributes(mutation.await_args.args[4]) == record
    assert (result.submission, result.readback.status, result.preservation) == (
        "completed",
        "verified",
        "unchanged",
    )
    assert (result.mutation_dispatched, result.changed, result.record) == (
        True,
        True,
        record,
    )
    assert result.output == "ok"
    assert result.errors == ()
    assert result.vnic_after is not None


@pytest.mark.parametrize(
    ("error", "submission", "code", "changed"),
    [
        (
            HMCCLIError("HSCLAB12 refused", remote_refusal_code="HSCLAB12"),
            "rejected",
            "HSCLAB12",
            False,
        ),
        (
            HMCCLIError("invalid attribute value", remote_nonzero_completed=True),
            "rejected",
            None,
            False,
        ),
        (HMCCLIError("SSH timed out"), "unknown", None, None),
        (OSError("connection reset"), "unknown", None, None),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_submission_is_read_back_once_and_raised(
    monkeypatch, error, submission, code, changed
) -> None:
    reads, mutation = _setup(monkeypatch, [_row()], [_row()], error)
    with pytest.raises(VnicPartialError) as caught:
        await _update(pvid_priority=5)
    result = caught.value.result
    assert isinstance(result, VnicPolicyUpdateResult)
    assert caught.value.__cause__ is error
    mutation.assert_awaited_once()
    assert reads.await_count == 2
    assert (result.submission, result.refusal_code) == (submission, code)
    assert result.readback.mismatched_fields == ("pvid_priority",)
    assert (result.preservation, result.changed) == ("unchanged", changed)


@pytest.mark.asyncio
async def test_unknown_submission_whose_change_landed_is_still_partial(
    monkeypatch,
) -> None:
    _setup(monkeypatch, [_row()], [_row(pvid_priority="5")], TimeoutError())
    with pytest.raises(VnicPartialError) as caught:
        await _update(pvid_priority=5)
    result = caught.value.result
    assert (result.submission, result.readback.status) == ("unknown", "verified")
    assert result.changed is None


@pytest.mark.parametrize(
    "after", [HMCCLIError("read failed"), [_row(port_vlan_id="x")], [_row(), _row()]]
)
@pytest.mark.asyncio
async def test_an_unreadable_after_state_is_unavailable(monkeypatch, after) -> None:
    _setup(monkeypatch, [_row()], after, "ok")
    with pytest.raises(VnicPartialError) as caught:
        await _update(pvid_priority=5)
    result = caught.value.result
    assert (result.submission, result.readback.status) == ("completed", "unavailable")
    assert result.readback.unavailable_fields == (
        "pvid_priority",
        "auto_priority_failover",
    )
    assert (result.preservation, result.vnic_after, result.changed) == (
        "unavailable",
        None,
        None,
    )


@pytest.mark.asyncio
async def test_a_vanished_slot_reads_unavailable_and_drifted(monkeypatch) -> None:
    _setup(monkeypatch, [_row()], [], "ok")
    with pytest.raises(VnicPartialError) as caught:
        await _update(pvid_priority=5)
    result = caught.value.result
    assert result.readback.status == "unavailable"
    assert (result.preservation, result.drifted_fields) == ("drifted", ("4:*",))


@pytest.mark.asyncio
async def test_a_mismatched_readback_raises(monkeypatch) -> None:
    _setup(monkeypatch, [_row()], [_row(pvid_priority="4")], "ok")
    with pytest.raises(VnicPartialError) as caught:
        await _update(pvid_priority=5)
    result = caught.value.result
    assert result.readback.mismatched_fields == ("pvid_priority",)
    assert result.changed is None


@pytest.mark.parametrize(
    ("after", "drifted"),
    [
        ([_row(pvid_priority="5", allowed_vlan_ids="10,11")], ("4:allowed_vlan_ids",)),
        ([_row(pvid_priority="5", mac_addr="0a0b0c0d0e99")], ("4:mac_addr",)),
        (
            [_row(pvid_priority="5"), _row(slot="9", pvid_priority="2")],
            ("9:*",),
        ),
        (
            [_row(pvid_priority="5", backing_device_states="sriov/3/0/Failed")],
            ("4:backing_device_states",),
        ),
    ],
)
@pytest.mark.asyncio
async def test_untargeted_changes_read_as_drift(monkeypatch, after, drifted) -> None:
    _setup(monkeypatch, [_row()], after, "ok")
    with pytest.raises(VnicPartialError) as caught:
        await _update(pvid_priority=5)
    result = caught.value.result
    assert (result.readback.status, result.preservation) == ("verified", "drifted")
    assert result.drifted_fields == drifted
    assert result.changed is None


@pytest.mark.asyncio
async def test_a_reset_preserving_failover_is_a_readback_mismatch(monkeypatch) -> None:
    _setup(
        monkeypatch,
        [_row()],
        [_row(pvid_priority="5", auto_priority_failover="1")],
        "ok",
    )
    with pytest.raises(VnicPartialError) as caught:
        await _update(pvid_priority=5)
    assert caught.value.result.readback.mismatched_fields == ("auto_priority_failover",)
