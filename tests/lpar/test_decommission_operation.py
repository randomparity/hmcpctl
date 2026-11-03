"""Journaled ``hmc_decommission_lpar``: body, classifiers, result and handler (ADR 0320)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from lpar.test_decommission_tool import (
    LPAR_UUID,
    SYSTEM_UUID,
    VIOS_UUID,
    _client,
    _complete_storage_detail,
    _lpar,
    _patch_common,
)

from hmcpctl.errors import HMCError
from hmcpctl.operations.logical import engine, store
from hmcpctl.operations.logical.engine import BodyResult, OperationRequest
from hmcpctl.operations.logical.models import EffectRecord, OperationRecord
from hmcpctl.operations.lpar import decommission as ops
from hmcpctl.operations.lpar.decommission import (
    EFFECT_KINDS,
    adapter_key,
    decommission_lpar,
    unmap_key,
)
from hmcpctl.operations.lpar.decommission_operation import (
    DEFAULT_ARGUMENTS,
    TOOL_NAME,
    decommission_body,
    decommission_classifiers,
    result_from_record,
)
from hmcpctl.server_tools.lpar.lifecycle import hmc_decommission_lpar

ARGUMENTS = {
    **DEFAULT_ARGUMENTS,
    "system_name_or_uuid": "system-a",
    "lpar_name_or_uuid": "aix-prod",
    "poll_interval": 1,
}
UNMAP = unmap_key(VIOS_UUID, "vhost0/vtscsi0")
ADAPTERS = [
    adapter_key(uuid)
    for uuid in (
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2",
        "cccccccc-cccc-cccc-cccc-ccccccccccc2",
        "dddddddd-dddd-dddd-dddd-ddddddddddd2",
    )
]


def _factory(hmc: Any):
    @asynccontextmanager
    async def opened(_connection: str):
        yield hmc

    return opened


def _submit(hmc: Any, continuation: str = "none", request_id: str = "r1"):
    request = OperationRequest(
        TOOL_NAME,
        "hmcpctl",
        "<default>",
        "hmc.test",
        request_id,
        ARGUMENTS if continuation == "none" else {},
    )
    return engine.submit(
        request,
        decommission_body(ARGUMENTS, _factory(hmc)),
        classifiers=decommission_classifiers(_factory(hmc)),
        continuation=continuation,
        wait_seconds=30,
    )


@pytest.fixture
def hmc(monkeypatch):
    calls: list[str] = []
    _patch_common(monkeypatch, calls)
    client = _client()
    client.calls = calls
    return client


def _statuses(record: OperationRecord) -> dict[str, str]:
    return {effect.key: effect.status for effect in record.effects}


def test_a_clean_run_journals_each_write_and_completes(hmc):
    record = _submit(hmc)

    assert (record.state, record.outcome) == ("terminal", "completed")
    assert list(_statuses(record)) == [
        "power_off",
        UNMAP,
        *ADAPTERS,
        "delete_lpar",
    ]
    assert set(_statuses(record).values()) == {"applied"}
    assert record.system_uuid == SYSTEM_UUID and record.partition_uuid == LPAR_UUID
    result = result_from_record(record)
    assert result.resource_deleted is True and result.workflow_completed is True
    assert result.operation == record
    assert [s.step for s in result.steps] == [
        "power_off",
        "detach_storage_mappings",
        "detach_adapters",
        "delete_lpar",
    ]


def test_a_repeat_returns_the_operation_and_writes_nothing(hmc):
    first = _submit(hmc)
    again = _submit(hmc)

    assert again.operation_id == first.operation_id
    assert hmc.submit_job.await_count == 1
    assert hmc.delete_logical_partition.await_count == 1


def test_a_failed_detach_pauses_and_resume_continues_without_repeating(hmc):
    hmc.delete_storage_mapping.side_effect = [HMCError("HTTP 500"), None]

    paused = _submit(hmc)
    assert (paused.state, paused.outcome) == ("paused", "needs_attention")
    assert _statuses(paused) == {"power_off": "applied", UNMAP: "uncertain"}
    result = result_from_record(paused)
    assert result.steps[1].step == "detach_storage_mappings"
    assert result.steps[1].status == "error"

    # The mapping is still listed, so the detach is classified not applied and rerun;
    # the partition now reads off because this operation powered it off.
    hmc.get_logical_partition.return_value = _lpar(state="not activated")
    resumed = _submit(hmc, "resume")
    assert resumed.outcome == "completed"
    assert hmc.submit_job.await_count == 1
    assert hmc.delete_storage_mapping.await_count == 2
    power = result_from_record(resumed).steps[0]
    assert (power.step, power.status) == ("power_off", "ok")
    assert power.result["already_off"] is False


def test_abandon_writes_nothing(hmc):
    hmc.delete_storage_mapping.side_effect = HMCError("HTTP 500")
    _submit(hmc)
    abandoned = _submit(hmc, "abandon")

    assert (abandoned.state, abandoned.outcome) == ("terminal", "abandoned")
    assert hmc.delete_storage_mapping.await_count == 1
    assert hmc.delete_logical_partition.await_count == 0


def test_a_resume_after_the_delete_landed_reads_nothing(hmc):
    hmc.delete_logical_partition.side_effect = HMCError("connection reset")
    paused = _submit(hmc)
    assert _statuses(paused)["delete_lpar"] == "uncertain"

    hmc.list_logical_partitions.return_value = []
    hmc.get_logical_partition.reset_mock()
    resumed = _submit(hmc, "resume")

    assert (resumed.state, resumed.outcome) == ("terminal", "completed")
    hmc.get_logical_partition.assert_not_awaited()
    result = result_from_record(resumed)
    assert result.resource_deleted is True and result.workflow_completed is True
    assert result.lpar_uuid == LPAR_UUID
    assert result.blast_radius["lpar_uuid"] == LPAR_UUID
    assert any("inventory is unavailable" in w for w in result.warnings)


def test_incomplete_vscsi_inventory_pauses_with_no_effect_but_holds_the_guard(
    monkeypatch,
):
    _patch_common(monkeypatch, [])
    hmc = _client(complete=False)

    record = _submit(hmc)

    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert record.effects == ()
    assert record.partition_uuid == LPAR_UUID
    assert result_from_record(record).steps[1].status == "error"


def test_a_partition_another_operation_holds_ends_failed(hmc):
    async def hold(ctx):
        ctx.guard(SYSTEM_UUID, LPAR_UUID)
        return BodyResult("needs_attention")

    request = OperationRequest("hmc_power_lpar", "hmcpctl", "<default>", "h", "p", {})
    engine.submit(request, hold, wait_seconds=10)

    record = _submit(hmc)

    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "partition_busy" in record.warnings[0]
    hmc.submit_job.assert_not_awaited()


def test_the_guard_takes_lower_cased_uuids(monkeypatch):
    _patch_common(monkeypatch, [])

    async def upper(_hmc, _value):
        return SYSTEM_UUID.replace("1", "A")

    monkeypatch.setattr(ops, "resolve_system_uuid", upper)
    guarded: list[tuple[str, str]] = []
    journal = SimpleNamespace(
        set_phase=lambda _name: None,
        guard=lambda system, lpar: guarded.append((system, lpar)),
        recorded=lambda _key: None,
    )

    async def effect(_key, _kind, _target, write):
        return await write()

    journal.effect = effect
    asyncio.run(
        decommission_lpar(
            _client(),
            "aix-prod",
            system_name_or_uuid="s",
            dry_run=True,
            journal=journal,
        )
    )
    assert guarded == [(SYSTEM_UUID.replace("1", "a"), LPAR_UUID)]


def test_a_resumed_ownership_refusal_names_no_selector(hmc, monkeypatch):
    hmc.delete_storage_mapping.side_effect = HMCError("HTTP 500")
    _submit(hmc)

    async def refuse(*_args, **_kwargs):
        raise PermissionError("LPAR 'aix-prod' is owned by another agent")

    monkeypatch.setattr(ops, "authorize_decommission_lpar_ownership_snapshot", refuse)
    resumed = _submit(hmc, "resume")

    assert (resumed.state, resumed.outcome) == ("paused", "needs_attention")
    assert "PermissionError" in resumed.warnings[0]
    assert "aix-prod" not in " ".join(resumed.warnings)


# --- classifiers -----------------------------------------------------------------


def _classify(hmc: Any, key: str, target: str):
    kind = EFFECT_KINDS[key]
    effect = EffectRecord(key, kind, target, "uncertain", None)
    classifier = decommission_classifiers(_factory(hmc))[kind]
    ctx = SimpleNamespace(connection="<default>", partition=(SYSTEM_UUID, LPAR_UUID))
    return asyncio.run(classifier(ctx, effect))


@pytest.mark.parametrize(
    ("state", "status"),
    [("not activated", "applied"), ("running", "needs_attention")],
)
def test_power_off_classifies_by_partition_state(state, status):
    hmc = AsyncMock()
    hmc.get_quick_property.return_value = state
    assert _classify(hmc, "power_off", f"lpar:{LPAR_UUID}").status == status


def test_unmap_classifies_by_the_vios_mappings():
    hmc = _client()
    target = f"vios:{VIOS_UUID}/mapping:vhost0/vtscsi0"
    assert _classify(hmc, "unmap", target).status == "not_applied"
    detail = _complete_storage_detail()
    detail["Resource"]["VirtualSCSIMappings"]["VirtualSCSIMapping"].pop(0)
    hmc.get_vios_storage_detail.return_value = detail
    verdict = _classify(hmc, "unmap", target)
    assert verdict.status == "applied"
    assert verdict.identity == {"vios_uuid": VIOS_UUID, "mapping_id": "vhost0/vtscsi0"}


def test_unmap_stays_open_on_an_incomplete_read():
    hmc = _client(complete=False)
    with pytest.raises(HMCError, match="incomplete"):
        _classify(hmc, "unmap", f"vios:{VIOS_UUID}/mapping:vhost0/vtscsi0")


def test_adapter_classifies_by_the_partition_inventory():
    hmc = _client()
    listed = (
        f"lpar:{LPAR_UUID}/ClientNetworkAdapter:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
    )
    gone = f"lpar:{LPAR_UUID}/ClientNetworkAdapter:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa9"
    assert _classify(hmc, "adapter", listed).status == "not_applied"
    assert _classify(hmc, "adapter", gone).status == "applied"


def test_delete_classifies_by_the_system_partition_list():
    hmc = _client()
    assert _classify(hmc, "delete_lpar", f"lpar:{LPAR_UUID}").status == "not_applied"
    hmc.list_logical_partitions.return_value = []
    assert _classify(hmc, "delete_lpar", f"lpar:{LPAR_UUID}").status == "applied"


# --- result rebuild ----------------------------------------------------------------


def _record(effects, *, state="paused", outcome="needs_attention", result=None):
    return OperationRecord(
        "0" * 32,
        "r",
        TOOL_NAME,
        "<default>",
        SYSTEM_UUID,
        LPAR_UUID,
        state,
        "tearing_down",
        outcome,
        tuple(effects),
        (),
        False,
        (),
        (),
        result,
        "t",
        "t",
    )


def test_a_running_record_rebuilds_a_valid_result_without_writes_in_flight():
    effects = [
        EffectRecord("power_off", "lpar.power_off", "lpar:x", "applied", {}),
        EffectRecord(UNMAP, "vios.storage_unmap", "vios:v", "intended", None),
    ]
    running = _record(effects, state="running", outcome=None)
    result = result_from_record(running)
    assert [(s.step, s.status) for s in result.steps] == [("power_off", "ok")]
    assert result.workflow_completed is False and result.resource_deleted is False
    paused = result_from_record(replace(running, state="paused"))
    assert [(s.step, s.status) for s in paused.steps] == [
        ("power_off", "ok"),
        ("detach_storage_mappings", "error"),
    ]


def test_a_record_paused_before_any_guard_rebuilds_an_empty_identity():
    record = replace(_record([]), system_uuid=None, partition_uuid=None)
    result = result_from_record(record)
    assert result.lpar_uuid == "" and result.steps == ()


# --- the MCP handler -------------------------------------------------------------


@pytest.fixture
def served(hmc, monkeypatch):
    @asynccontextmanager
    async def opened(_profile=None):
        yield hmc

    monkeypatch.setattr("hmcpctl._app.client_from_env", opened)
    monkeypatch.setattr("hmcpctl.operations.lpar.power.client_from_env", opened)
    return hmc


def _recorded(request_id: str):
    return engine.recorded_arguments("hmcpctl", request_id, TOOL_NAME)


def test_the_tool_requires_request_id():
    with pytest.raises(TypeError, match="request_id"):
        hmc_decommission_lpar(system_name_or_uuid="s", lpar_name_or_uuid="l")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="request_id"):
        hmc_decommission_lpar(request_id="", system_name_or_uuid="s")


def test_a_dry_run_records_nothing(served):
    result = hmc_decommission_lpar(
        request_id="d1",
        system_name_or_uuid="system-a",
        lpar_name_or_uuid="aix-prod",
        dry_run=True,
    )
    assert result.dry_run is True and result.operation is None
    assert _recorded("d1") is None
    served.submit_job.assert_not_awaited()


def test_a_dry_run_reads_no_operation_store(served, monkeypatch, tmp_path):
    # A store-id sentinel without its store is a lost store (ADR 0190 Decision 1).
    (tmp_path / "store-id").write_text("x")
    monkeypatch.setenv(store.STATE_DIR_ENV, str(tmp_path))
    result = hmc_decommission_lpar(
        request_id="d2",
        system_name_or_uuid="system-a",
        lpar_name_or_uuid="aix-prod",
        dry_run=True,
    )
    assert result.dry_run is True
    with pytest.raises(store.OperationRefused):
        hmc_decommission_lpar(
            request_id="d2", system_name_or_uuid="system-a", lpar_name_or_uuid="x"
        )


def test_a_refused_inventory_records_nothing(served):
    served.list_logical_partitions.return_value = []
    with pytest.raises(ValueError):
        hmc_decommission_lpar(
            request_id="n1", system_name_or_uuid="system-a", lpar_name_or_uuid="x"
        )
    assert _recorded("n1") is None


def test_a_new_request_needs_both_selectors(served):
    with pytest.raises(ValueError, match="lpar_name_or_uuid"):
        hmc_decommission_lpar(request_id="m1", system_name_or_uuid="system-a")


def test_a_continuation_without_a_record_is_not_found(served):
    with pytest.raises(store.OperationRefused, match="no operation"):
        hmc_decommission_lpar(request_id="none", continuation="resume")


def test_request_id_alone_continues_an_immediate_start(served):
    served.delete_storage_mapping.side_effect = HMCError("HTTP 500")
    first = hmc_decommission_lpar(
        request_id="i1",
        system_name_or_uuid="system-a",
        lpar_name_or_uuid="aix-prod",
        immediate=True,
        wait_seconds=30,
    )
    assert first.operation is not None and first.operation.outcome == "needs_attention"

    abandoned = hmc_decommission_lpar(request_id="i1", continuation="abandon")

    assert abandoned.operation is not None
    assert abandoned.operation.outcome == "abandoned"
    assert _recorded("i1")["immediate"] is True


def test_a_dry_run_cannot_continue(served):
    with pytest.raises(ValueError, match="dry_run"):
        hmc_decommission_lpar(request_id="c1", dry_run=True, continuation="resume")
