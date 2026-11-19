"""Owned shared processor pool round trip and independent recovery."""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from hmcpctl.operations.systems.processor_pools import SharedProcessorPool


def slot():
    return SharedProcessorPool(
        "00000000-0000-0000-0000-000000000002",
        2,
        "SharedPool02",
        False,
        0.0,
        0.0,
        0.0,
        0.0,
        None,
    )


def test_admission_is_positive_nondefault_zero_placeholder():
    from live_test.arms.processor_pool import eligible

    initial = slot()
    assert eligible(initial)
    for candidate in [
        replace(initial, pool_id=0),
        replace(initial, configured=True),
        replace(initial, name="other"),
        replace(initial, maximum_processing_units=None),
        replace(initial, pending_reserved_processing_units=1),
        replace(initial, assigned_partition_uuids=("member",)),
    ]:
        assert not eligible(candidate)


def test_recovery_requires_a_private_baseline_and_checks_current_independently():
    import asyncio
    from dataclasses import asdict
    from unittest.mock import AsyncMock

    import pytest
    from live_test.arms import processor_pool as arm
    from live_test.recovery.common import StateUnreadable

    with pytest.raises(ValueError, match="baseline"):
        arm.recovery_input({"results": []})
    pool = slot()
    system = "00000000-0000-0000-0000-000000000011"
    baseline = arm.baseline_data(system, pool)
    call = AsyncMock(
        return_value=("PASS", {"system_uuid": system, "pool": asdict(pool)})
    )
    assert asyncio.run(arm.recover(call, baseline)) == []
    changed = asdict(replace(pool, reserved_processing_units=0.05))
    call.return_value = ("PASS", {"system_uuid": system, "pool": changed})
    assert len(asyncio.run(arm.recover(call, baseline))) == 1
    call.return_value = ("FAIL", None)
    with pytest.raises(StateUnreadable):
        asyncio.run(arm.recover(call, baseline))


def test_round_trip_checkpoints_then_changes_and_restores(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from live_test.arms import processor_pool as arm

    original = slot()
    capacity = SimpleNamespace(member_count=0, available=1)
    state = SimpleNamespace(
        config=SimpleNamespace(system_name="system"), checkpoint=Mock(), record=Mock()
    )
    config = object()
    hmc = SimpleNamespace(config=config)
    listing = SimpleNamespace(system_uuid="sys", pools=(original,))
    monkeypatch.setattr(
        arm, "list_shared_processor_pools", AsyncMock(return_value=listing)
    )
    monkeypatch.setattr(arm, "capacity_evidence", AsyncMock(return_value=capacity))
    monkeypatch.setattr(arm, "resolve_system_name", AsyncMock(return_value="system"))
    monkeypatch.setattr(arm, "uuid4", lambda: SimpleNamespace(hex="abcd1234"))
    observed = original

    async def get(*args):
        return SimpleNamespace(pool=observed)

    monkeypatch.setattr(arm, "get_shared_processor_pool", get)

    async def submit(_config, _name, pool_id, change):
        nonlocal observed
        state.checkpoint.assert_called_once()
        assert pool_id == original.pool_id
        observed = replace(
            original,
            name=change.name,
            maximum_processing_units=float(change.maximum),
            reserved_processing_units=float(change.reserved),
            pending_reserved_processing_units=float(change.reserved),
        )

    monkeypatch.setattr(arm, "submit_native", AsyncMock(side_effect=submit))
    asyncio.run(arm._round_trip(hmc, state))
    assert observed == original
    assert arm.submit_native.await_count == 2
    assert state.record.call_args.args[2] == "PASS"
