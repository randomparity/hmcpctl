"""ST74 vSCSI client opt-in arm wiring, with no hardware in unit tests.

The stub readbacks below are uncaptured: they exercise the arm's control flow only
and are not live evidence for issue #1980's live criterion.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test.arms import lpar_config
from live_test.arms import lpar_vscsi_client as arm

VIOS_LISTING = {
    "entries": [
        {
            "UUID": "vios",
            "Resource": {
                "PartitionName": "vios-R1",
                "PartitionID": "1",
                "PartitionIOConfiguration": {"MaximumVirtualIOSlots": "200"},
            },
        }
    ]
}


def created(status="ok", pairing="unproven"):
    return {
        "lpar": {"UUID": "new"},
        "ownership_stamped": True,
        "workflow_completed": status == "ok",
        "steps": [
            {
                "step": "virtual_scsi_adapters",
                "status": status,
                "result": {
                    "configuration": "verified" if status == "ok" else "unverified",
                    "vios_server_adapters": "not-created",
                    "vios_mappings": "not-created",
                    "pairing": pairing,
                    "records": [{"slot": 2, "status": "verified"}],
                },
            },
            {"step": "apply_profile", "status": "ok"},
        ],
    }


def make_state(monkeypatch, responses):
    state = SimpleNamespace(
        group=arm.GROUP,
        hmc_config=object(),
        config=SimpleNamespace(system_name="sys-R1"),
        skip=Mock(),
        record=Mock(),
        record_verified=Mock(),
    )
    state.call = AsyncMock(side_effect=responses)
    monkeypatch.setattr(
        lpar_config,
        "_read_pools",
        AsyncMock(return_value=(SimpleNamespace(names=[]), 256)),
    )

    async def teardown(client, state, run, **kwargs):
        run.deleted = True

    state.teardown = AsyncMock(side_effect=teardown)
    state.compare = AsyncMock(return_value=True)
    monkeypatch.setattr(arm, "_teardown", state.teardown)
    monkeypatch.setattr(arm, "_compare", state.compare)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "holds"),
    [
        (("PASS", created()), True),
        (("PASS", created(status="error")), False),
        (("PASS", created(pairing="verified")), False),
        (("FAIL", created()), False),
        (("PASS", {"lpar": {"UUID": "new"}, "steps": None}), False),
    ],
    ids=["verified", "mismatch", "pairing-claimed", "failed-call", "malformed"],
)
async def test_arm_asserts_comparison_and_always_cleans_up(
    monkeypatch, response, holds
):
    state = make_state(monkeypatch, [("PASS", VIOS_LISTING), response])
    await arm.exercise_lpar_vscsi_client(object(), state)
    state.teardown.assert_awaited_once()
    state.compare.assert_awaited_once()
    create = state.call.await_args_list[1].kwargs
    assert create["partition_type"] == "OS400"
    assert create["max_virtual_slots"] == arm.MAX_VIRTUAL_SLOTS
    assert create["virtual_scsi_adapters"] == [
        {"local_slot": 2, "remote_slot": 199, "is_required": False, "remote_lpar_id": 1}
    ]
    assert create["apply_partition_profile"] is True
    verified = state.record_verified.call_args.kwargs
    assert verified["assertions"][0].holds is holds
    assert verified["cleanup"] == "passed"
    assert verified["data"]["pairing"] == "unproven"


@pytest.mark.asyncio
async def test_arm_without_vios_never_creates(monkeypatch):
    state = make_state(monkeypatch, [("PASS", {"entries": []})])
    await arm.exercise_lpar_vscsi_client(object(), state)
    assert state.call.await_count == 1
    assert state.record_verified.call_args.kwargs["assertions"][0].holds is False
    assert state.record.call_args.args[2] == "FAIL"


@pytest.mark.asyncio
async def test_arm_requires_explicit_group():
    state = SimpleNamespace(group="round2", hmc_config=object(), skip=Mock())
    await arm.exercise_lpar_vscsi_client(object(), state)
    state.skip.assert_called_once()
