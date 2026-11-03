"""Journaled ``hmc_decommission_lpar``: body, classifiers, result and handler (ADR 0320)."""

from __future__ import annotations

import asyncio
import json
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
    deletes_owned,
    preview,
    result_from_record,
)
from hmcpctl.server_tools.catalog import TOOL_SECURITY
from hmcpctl.server_tools.lpar.decommission import decommission_handler

hmc_decommission_lpar = decommission_handler(
    TOOL_SECURITY, lambda _name: True, lambda *_args: None
)
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


def _submit(
    hmc: Any,
    continuation: str = "none",
    request_id: str = "r1",
    arguments: dict[str, Any] = ARGUMENTS,
):
    request = OperationRequest(
        TOOL_NAME,
        "hmcpctl",
        "<default>",
        "hmc.test",
        request_id,
        arguments if continuation == "none" else {},
    )
    return engine.submit(
        request,
        decommission_body(
            arguments, _factory(hmc), authorized_vios=frozenset({VIOS_UUID})
        ),
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


OWNED = {**ARGUMENTS, "storage_cleanup": "delete_owned"}
VG_UUID = "66666666-6666-6666-6666-666666666666"


def _owned_disks(hmc: Any, *names: str) -> None:
    """Ledger the partition's disks, as provision does, and list them in their group."""

    async def seed(ctx):
        for index, name in enumerate(names):
            identity = {"vios_uuid": VIOS_UUID, "vg_uuid": VG_UUID, "disk_name": name}
            ctx.ledger_add(
                f"d{index}", "virtual_disk", SYSTEM_UUID, LPAR_UUID, identity
            )
        return BodyResult("completed")

    request = OperationRequest("seed", "hmcpctl", "<default>", "hmc.test", "seed", {})
    engine.submit(request, seed, wait_seconds=30)
    hmc.list_vios.return_value = [{"UUID": VIOS_UUID}]
    hmc.get_vios_storage_detail.return_value = _complete_storage_detail()
    hmc.get_volume_group.return_value = {
        "Resource": {"VirtualDisks": {"VirtualDisk": [{"DiskName": n} for n in names]}}
    }


def test_retain_reads_no_ledger_and_returns_no_storage(hmc):
    _owned_disks(hmc, "lpar-a-root")

    record = _submit(hmc)

    assert record.outcome == "completed"
    hmc.get_volume_group.assert_not_awaited()
    hmc.delete_virtual_disk.assert_not_awaited()
    assert result_from_record(record).storage is None
    assert deletes_owned({}) is False


def test_delete_owned_deletes_the_owned_disk_after_the_partition(hmc):
    _owned_disks(hmc, "lpar-a-root")

    record = _submit(hmc, arguments=OWNED)

    assert (record.state, record.outcome, record.phase) == (
        "terminal",
        "completed",
        "cleaning_storage",
    )
    keys = list(_statuses(record))
    assert keys.index("delete_lpar") < len(keys) - 1
    assert keys[-1].startswith("disk.")
    hmc.delete_virtual_disk.assert_awaited_once_with(VIOS_UUID, VG_UUID, "lpar-a-root")
    result = result_from_record(record, delete_owned=True)
    assert [s.step for s in result.steps] == [
        "power_off",
        "detach_storage_mappings",
        "detach_adapters",
        "delete_lpar",
    ]
    assert result.storage == {
        "deleted": (
            {
                "kind": "virtual_disk",
                "vios_uuid": VIOS_UUID,
                "vg_uuid": VG_UUID,
                "disk_name": "lpar-a-root",
            },
        ),
        "retained": (),
        "pending": (),
    }


def test_an_unreadable_group_pauses_and_resume_cleans_without_the_partition(hmc):
    _owned_disks(hmc, "lpar-a-root")
    group = hmc.get_volume_group.return_value
    hmc.get_volume_group.side_effect = [HMCError("HTTP 500"), group]

    paused = _submit(hmc, arguments=OWNED)

    assert (paused.state, paused.outcome) == ("paused", "needs_attention")
    assert _statuses(paused)["delete_lpar"] == "applied"
    retained = result_from_record(paused, delete_owned=True).storage["retained"]
    assert [item["reason"] for item in retained] == ["inventory_incomplete"]
    lookups = hmc.list_logical_partitions.await_count

    resumed = _submit(hmc, "resume", arguments=OWNED)

    assert (resumed.state, resumed.outcome) == ("terminal", "completed")
    assert hmc.list_logical_partitions.await_count == lookups
    hmc.delete_virtual_disk.assert_awaited_once()
    result = result_from_record(resumed, delete_owned=True)
    assert result.resource_deleted is True
    assert len(result.storage["deleted"]) == 1


def test_deleted_spans_every_run_of_the_operation(hmc):
    _owned_disks(hmc, "disk-a", "disk-b")
    hmc.delete_virtual_disk.side_effect = [None, HMCError("connection reset")]

    paused = _submit(hmc, arguments=OWNED)

    assert paused.outcome == "needs_attention"
    pending = result_from_record(paused, delete_owned=True).storage["pending"]
    assert len(pending) == 1
    hmc.get_volume_group.return_value = {"Resource": {"VirtualDisks": ""}}

    resumed = _submit(hmc, "resume", arguments=OWNED)

    assert resumed.outcome == "completed"
    storage = result_from_record(resumed, delete_owned=True).storage
    assert sorted(item["disk_name"] for item in storage["deleted"]) == [
        "disk-a",
        "disk-b",
    ]
    assert storage["pending"] == ()


def test_the_dry_run_plans_owned_storage(hmc):
    _owned_disks(hmc, "lpar-a-root")

    result = asyncio.run(preview(hmc, OWNED))

    assert result.dry_run is True
    assert result.storage["pending"] == (
        {
            "kind": "virtual_disk",
            "vios_uuid": VIOS_UUID,
            "vg_uuid": VG_UUID,
            "disk_name": "lpar-a-root",
        },
    )
    hmc.delete_virtual_disk.assert_not_awaited()


def _gated(permitted=lambda _name: True, denied: str | None = None):
    """The handler under gates that record each delegated check."""
    checks: list[tuple[str, dict[str, Any]]] = []

    def authorize(name, _security, arguments):
        checks.append((name, dict(arguments)))
        if name == denied:
            raise PermissionError(f"{name} denied")

    return decommission_handler(TOOL_SECURITY, permitted, authorize), checks


def _owned_call(handler, **extra):
    return handler(
        request_id="o1",
        system_name_or_uuid="system-a",
        lpar_name_or_uuid="aix-prod",
        storage_cleanup="delete_owned",
        poll_interval=1,
        wait_seconds=30,
        **extra,
    )


def test_delete_owned_without_the_window_is_refused_and_records_nothing(served):
    with pytest.raises(ValueError, match="exclusive_writer_window"):
        _owned_call(hmc_decommission_lpar)
    assert _recorded("o1") is None


def test_an_unpermitted_specialist_refuses_delete_owned(served):
    _owned_disks(served, "lpar-a-root")
    handler, _ = _gated(permitted=lambda name: name != "hmc_delete_virtual_disk")

    with pytest.raises(PermissionError, match="hmc_delete_virtual_disk"):
        _owned_call(handler, exclusive_writer_window=True)
    assert _recorded("o1") is None
    served.submit_job.assert_not_awaited()


def test_a_denied_vios_refuses_delete_owned(served):
    _owned_disks(served, "lpar-a-root")
    handler, _ = _gated(denied="hmc_detach_storage_mapping")

    with pytest.raises(PermissionError):
        _owned_call(handler, exclusive_writer_window=True)
    assert _recorded("o1") is None


def test_delete_owned_authorizes_each_specialist_on_the_owning_vios(served):
    _owned_disks(served, "lpar-a-root")
    handler, checks = _gated()

    result = _owned_call(handler, exclusive_writer_window=True)

    assert result.operation.outcome == "completed"
    delegated = [(name, args) for name, args in checks if name != TOOL_NAME]
    assert [name for name, _ in delegated] == [
        "hmc_detach_storage_mapping",
        "hmc_unmount_optical_media",
        "hmc_delete_optical_media",
        "hmc_delete_virtual_disk",
    ]
    assert {args["vios_name_or_uuid"] for _, args in delegated} == {VIOS_UUID}
    assert {args["system_name_or_uuid"] for _, args in delegated} == {"system-a"}
    assert len(result.storage["deleted"]) == 1


def test_a_bare_resume_of_delete_owned_needs_the_window(served):
    _owned_disks(served, "lpar-a-root")
    served.delete_storage_mapping.side_effect = HMCError("HTTP 500")
    paused = _owned_call(hmc_decommission_lpar, exclusive_writer_window=True)
    assert paused.operation.outcome == "needs_attention"

    with pytest.raises(ValueError, match="exclusive_writer_window"):
        hmc_decommission_lpar(request_id="o1", continuation="resume")


def test_a_resume_after_the_delete_authorizes_against_the_recorded_targets(served):
    _owned_disks(served, "lpar-a-root")
    group = served.get_volume_group.return_value
    # The first read is the new request's preview; the run's own read fails.
    served.get_volume_group.side_effect = [group, HMCError("HTTP 500"), group]
    paused = _owned_call(hmc_decommission_lpar, exclusive_writer_window=True)
    assert paused.resource_deleted is True
    assert paused.operation.outcome == "needs_attention"
    served.list_logical_partitions.return_value = []
    handler, checks = _gated()

    resumed = handler(
        request_id="o1",
        continuation="resume",
        exclusive_writer_window=True,
        wait_seconds=30,
    )

    assert resumed.operation.outcome == "completed"
    assert len(resumed.storage["deleted"]) == 1
    vioses = {
        args.get("vios_name_or_uuid") for name, args in checks if name != TOOL_NAME
    }
    assert vioses == {VIOS_UUID}


def test_a_legacy_record_without_storage_cleanup_resumes_as_retain(served):
    _owned_disks(served, "lpar-a-root")
    served.delete_storage_mapping.side_effect = [HMCError("HTTP 500"), None]
    hmc_decommission_lpar(
        request_id="L1",
        system_name_or_uuid="system-a",
        lpar_name_or_uuid="aix-prod",
        poll_interval=1,
        wait_seconds=30,
    )
    with store.session() as conn, store.write_transaction(conn):
        row = conn.execute(
            "SELECT request_json FROM operations WHERE request_id = 'L1'"
        ).fetchone()
        request = json.loads(row["request_json"])
        del request["arguments"]["storage_cleanup"]
        conn.execute(
            "UPDATE operations SET request_json = ? WHERE request_id = 'L1'",
            (json.dumps(request),),
        )

    resumed = hmc_decommission_lpar(
        request_id="L1", continuation="resume", wait_seconds=30
    )

    assert resumed.operation.outcome == "completed"
    assert resumed.storage is None
    served.delete_virtual_disk.assert_not_awaited()


def test_a_delete_owned_dry_run_warns_without_the_window(served):
    _owned_disks(served, "lpar-a-root")

    result = hmc_decommission_lpar(
        request_id="d1",
        system_name_or_uuid="system-a",
        lpar_name_or_uuid="aix-prod",
        storage_cleanup="delete_owned",
        dry_run=True,
    )

    assert result.operation is None
    assert len(result.storage["pending"]) == 1
    assert any("exclusive_writer_window" in w for w in result.warnings)
    assert _recorded("d1") is None


def test_a_failed_resume_reports_earlier_retentions_it_deleted_only_as_deleted(hmc):
    _owned_disks(hmc, "disk-a", "disk-b")
    group = hmc.get_volume_group.return_value
    hmc.get_volume_group.side_effect = [HMCError("HTTP 500"), group, group]
    paused = _submit(hmc, arguments=OWNED)
    retained = result_from_record(paused, delete_owned=True).storage["retained"]
    assert [item["reason"] for item in retained] == ["inventory_incomplete"] * 2
    hmc.delete_virtual_disk.side_effect = [None, HMCError("changed", 412)]

    failed = _submit(hmc, "resume", arguments=OWNED)

    assert (failed.state, failed.outcome) == ("terminal", "failed")
    storage = result_from_record(failed, delete_owned=True).storage
    assert [item["disk_name"] for item in storage["deleted"]] == ["disk-a"]
    assert storage["retained"] == (
        {
            "kind": "virtual_disk",
            "vios_uuid": VIOS_UUID,
            "vg_uuid": VG_UUID,
            "disk_name": "disk-b",
            "reason": "precondition_failed",
        },
    )


def test_a_failed_run_still_reports_its_other_retained_items(hmc):
    _owned_disks(hmc, "disk-a", "disk-b")
    hmc.get_volume_group.return_value = {
        "Resource": {"VirtualDisks": {"VirtualDisk": [{"DiskName": "disk-b"}]}}
    }
    hmc.delete_virtual_disk.side_effect = HMCError("changed", 412)

    failed = _submit(hmc, arguments=OWNED)

    assert (failed.state, failed.outcome) == ("terminal", "failed")
    result = result_from_record(failed, delete_owned=True)
    assert result.resource_deleted is True
    assert result.blast_radius["lpar_name"] == "aix-prod"
    reasons = {item["disk_name"]: item["reason"] for item in result.storage["retained"]}
    assert reasons == {"disk-a": "absent", "disk-b": "precondition_failed"}
    assert "disk-b" in failed.warnings[0]


def test_a_resume_after_the_delete_keeps_the_first_runs_inventory(hmc):
    _owned_disks(hmc, "lpar-a-root")
    group = hmc.get_volume_group.return_value
    hmc.get_volume_group.side_effect = [HMCError("HTTP 500"), group]
    _submit(hmc, arguments=OWNED)

    resumed = _submit(hmc, "resume", arguments=OWNED)

    result = result_from_record(resumed, delete_owned=True)
    assert resumed.outcome == "completed"
    assert result.blast_radius["lpar_name"] == "aix-prod"
    assert not any("inventory is unavailable" in w for w in result.warnings)
    assert len(result.storage["deleted"]) == 1
