"""``delete_owned`` cleanup: ownership, the same-pass check, effects and ledger (ADR 0323)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.operations.logical import engine, store
from hmcpctl.operations.logical.engine import BodyResult, OperationRequest
from hmcpctl.operations.logical.models import EffectRecord, OperationRecord
from hmcpctl.operations.lpar import decommission_storage as cleanup
from hmcpctl.operations.lpar.decommission import EFFECT_KINDS, unmap_key
from hmcpctl.operations.lpar.decommission_storage import (
    OwnedItem,
    clean_storage,
    delete_key,
    plan_storage,
    storage_classifiers,
)

SYSTEM = "11111111-1111-1111-1111-111111111111"
LPAR = "22222222-2222-2222-2222-222222222222"
OTHER_LPAR = "33333333-3333-3333-3333-333333333333"
VIOS = "44444444-4444-4444-4444-444444444444"
VIOS_B = "55555555-5555-5555-5555-555555555555"
VG = "66666666-6666-6666-6666-666666666666"
REPO_VG = "77777777-7777-7777-7777-777777777777"
DISK = {"vios_uuid": VIOS, "vg_uuid": VG, "disk_name": "lpar-a-root"}
MEDIA = {"vios_uuid": VIOS, "media_name": "hmcpctl_0123456789ab.iso"}
DISK_ITEM = OwnedItem("virtual_disk", VIOS, "lpar-a-root", VG)
MEDIA_ITEM = OwnedItem("installer_media", VIOS, "hmcpctl_0123456789ab.iso")


def _factory(hmc: Any):
    @asynccontextmanager
    async def opened(_connection: str):
        yield hmc

    return opened


def _mapping(client: str, storage: Any, target: str = "vtscsi0") -> dict[str, Any]:
    return {
        "AssociatedLogicalPartition": {
            "href": f"/rest/api/uom/LogicalPartition/{client.upper()}"
        },
        "ServerAdapter": {"AdapterName": "vhost0"},
        "TargetDevice": {"VirtualSCSITargetDevice": {"TargetName": target}},
        "Storage": storage,
    }


def _detail(*mappings: dict[str, Any]) -> dict[str, Any]:
    block = {"VirtualSCSIMapping": list(mappings)} if mappings else ""
    return {"Resource": {"VirtualSCSIMappings": block}}


def _disk_ref(name: str = "lpar-a-root") -> dict[str, Any]:
    return {"VirtualDisk": {"DiskName": name}}


def _group(*names: str) -> dict[str, Any]:
    return {
        "Resource": {"VirtualDisks": {"VirtualDisk": [{"DiskName": n} for n in names]}}
    }


@pytest.fixture
def hmc() -> AsyncMock:
    client = AsyncMock()
    client.list_vios.return_value = [{"UUID": VIOS.upper()}]
    client.get_vios_storage_detail.return_value = _detail()
    client.get_volume_group.return_value = _group("lpar-a-root", "lpar-a-root-x")
    client.list_volume_groups.return_value = [
        {"UUID": VG, "Resource": {}},
        {
            "UUID": REPO_VG,
            "Resource": {"MediaRepositories": {"VirtualMediaRepository": {}}},
        },
    ]
    client.list_optical_media.return_value = [{"MediaName": MEDIA["media_name"]}]
    return client


def _seed(*rows: tuple[str, str, str, dict[str, Any]], request_id: str = "seed") -> str:
    """Ledger rows written by an earlier operation, as provision or reconfigure does."""

    async def body(ctx):
        for key, kind, lpar, identity in rows:
            ctx.ledger_add(key, kind, SYSTEM, lpar, identity)
        return BodyResult("completed")

    request = OperationRequest(
        "seed_tool", "agent", "<default>", "hmc.test", request_id, {}
    )
    return engine.submit(request, body, wait_seconds=30).operation_id


def _run(
    hmc: AsyncMock,
    *,
    authorized: frozenset[str] = frozenset({VIOS, VIOS_B}),
    continuation: str = "none",
) -> OperationRecord:
    async def body(ctx):
        ctx.guard(SYSTEM, LPAR)
        retained, failure = await clean_storage(
            hmc, ctx, SYSTEM, LPAR, authorized_vios=authorized
        )
        result = {"retained": [dict(r) for r in retained]}
        if failure is not None:
            raise engine.OperationFailed(failure, result=result)
        return BodyResult("completed", result)

    classifiers = {
        **storage_classifiers(_factory(hmc)),
        EFFECT_KINDS["unmap"]: _unmapped,
    }
    request = OperationRequest(
        "cleanup_tool", "agent", "<default>", "hmc.test", "d1", {}
    )
    return engine.submit(
        request,
        body,
        classifiers=classifiers,
        continuation=continuation,
        wait_seconds=30,
    )


async def _unmapped(_ctx, effect):
    return engine.Classification("applied", {"target": effect.target})


def _rows() -> list[tuple[str | None, dict[str, Any]]]:
    with store.session() as conn:
        return [
            (lpar, identity)
            for kind in ("virtual_disk", "installer_media")
            for _, lpar, identity in store.ledger_entries(conn, kind, SYSTEM)
        ]


def _reasons(record: OperationRecord) -> list[str]:
    return [entry["reason"] for entry in record.result["retained"]]


def test_only_a_ledgered_disk_is_deleted_and_its_entry_removed(hmc):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))

    record = _run(hmc)

    assert (record.state, record.outcome) == ("terminal", "completed")
    hmc.delete_virtual_disk.assert_awaited_once_with(VIOS, VG, "lpar-a-root")
    assert [e.key for e in record.effects] == [delete_key(DISK_ITEM)]
    assert record.effects[0].kind == "vios.virtual_disk_delete"
    assert record.effects[0].target == f"vios:{VIOS}/vg:{VG}/disk:lpar-a-root"
    assert record.effects[0].identity == DISK
    assert record.result == {"retained": []}
    assert _rows() == []


def test_without_a_store_nothing_is_owned(hmc):
    assert cleanup.owned_items(SYSTEM, LPAR) == ((), ())


def test_a_disk_mapped_to_another_client_is_retained_with_its_entry(hmc):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))
    hmc.get_vios_storage_detail.return_value = _detail(
        _mapping(OTHER_LPAR, _disk_ref())
    )

    record = _run(hmc)

    hmc.delete_virtual_disk.assert_not_awaited()
    assert _reasons(record) == ["referenced"]
    assert _rows() == [(LPAR, DISK)]


def test_a_mapping_naming_another_group_does_not_reference_the_disk(hmc):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))
    other = {
        "VirtualDisk": {
            "DiskName": "lpar-a-root",
            "VolumeGroup": {"href": f"/VirtualIOServer/{VIOS}/VolumeGroup/{REPO_VG}"},
        }
    }
    hmc.get_vios_storage_detail.return_value = _detail(_mapping(OTHER_LPAR, other))

    record = _run(hmc)

    assert record.result == {"retained": []}
    hmc.delete_virtual_disk.assert_awaited_once()


def test_an_identity_ledgered_for_another_partition_is_retained(hmc):
    _seed(
        ("root_disk", "virtual_disk", LPAR, DISK),
        ("added_disk:0", "virtual_disk", OTHER_LPAR, DISK),
    )

    record = _run(hmc)

    hmc.delete_virtual_disk.assert_not_awaited()
    assert _reasons(record) == ["ledgered_elsewhere"]


@pytest.mark.parametrize(
    "breakage",
    [
        "unreadable_vios",
        "vios_without_uuid",
        "missing_collection",
        "unverified_client",
        "unnamed_storage",
        "mapping_without_id",
        "owner_vios_unlisted",
    ],
)
def test_incomplete_inventory_retains_every_owned_item(hmc, breakage):
    _seed(
        ("root_disk", "virtual_disk", LPAR, DISK),
        ("installer_media", "installer_media", LPAR, MEDIA),
    )
    hmc.list_vios.return_value = [{"UUID": VIOS}, {"UUID": VIOS_B}]
    details = {VIOS: _detail(), VIOS_B: _detail()}
    if breakage == "unreadable_vios":
        details[VIOS_B] = HMCError("boom")
    elif breakage == "vios_without_uuid":
        hmc.list_vios.return_value = [{"UUID": VIOS}, {"UUID": ""}]
    elif breakage == "missing_collection":
        details[VIOS_B] = {"Resource": {}}
    elif breakage == "unverified_client":
        mapping = _mapping(OTHER_LPAR, {"PhysicalVolume": {"VolumeName": "hdisk1"}})
        del mapping["AssociatedLogicalPartition"]
        details[VIOS_B] = _detail(mapping)
    elif breakage == "unnamed_storage":
        details[VIOS_B] = _detail(_mapping(OTHER_LPAR, {"VirtualDisk": {}}))
    elif breakage == "mapping_without_id":
        mapping = _mapping(LPAR, _disk_ref())
        del mapping["ServerAdapter"]
        details[VIOS] = _detail(mapping)
    else:
        hmc.list_vios.return_value = [{"UUID": VIOS_B}]

    async def detail(uuid):
        value = details[uuid]
        if isinstance(value, Exception):
            raise value
        return value

    hmc.get_vios_storage_detail.side_effect = detail

    record = _run(hmc)

    hmc.delete_virtual_disk.assert_not_awaited()
    hmc.delete_optical_media.assert_not_awaited()
    assert _reasons(record) == ["inventory_incomplete"] * 2
    assert len(_rows()) == 2


def test_a_remaining_mapping_to_this_partition_is_unmapped_before_the_delete(hmc):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))
    hmc.get_vios_storage_detail.return_value = _detail(_mapping(LPAR, _disk_ref()))
    order: list[str] = []
    hmc.delete_storage_mapping.side_effect = lambda *a: order.append("unmap")
    hmc.delete_virtual_disk.side_effect = lambda *a: order.append("delete")

    record = _run(hmc)

    assert order == ["unmap", "delete"]
    hmc.delete_storage_mapping.assert_awaited_once_with(VIOS, "vhost0/vtscsi0", LPAR)
    assert [e.key for e in record.effects] == [
        unmap_key(VIOS, "vhost0/vtscsi0"),
        delete_key(DISK_ITEM),
    ]


def test_media_is_deleted_from_the_one_repository_group(hmc):
    _seed(("installer_media", "installer_media", LPAR, MEDIA))

    record = _run(hmc)

    hmc.delete_optical_media.assert_awaited_once_with(
        VIOS, REPO_VG, MEDIA["media_name"]
    )
    effect = record.effects[0]
    assert (effect.key, effect.kind) == (
        delete_key(MEDIA_ITEM),
        "vios.optical_media_delete",
    )
    assert effect.identity == {**MEDIA, "vg_uuid": REPO_VG}
    assert _rows() == []


@pytest.mark.parametrize("repositories", [0, 2])
def test_media_without_one_repository_group_is_retained(hmc, repositories):
    _seed(("installer_media", "installer_media", LPAR, MEDIA))
    repository = {"MediaRepositories": {"VirtualMediaRepository": {}}}
    hmc.list_volume_groups.return_value = [
        {"UUID": f"{index}", "Resource": repository} for index in range(repositories)
    ]

    record = _run(hmc)

    hmc.delete_optical_media.assert_not_awaited()
    assert _reasons(record) == ["repository_unresolved"]


@pytest.mark.parametrize(
    ("names", "reason"), [((), "absent"), (("lpar-a-root",) * 2, "ambiguous")]
)
def test_an_absent_or_duplicated_disk_is_retained_with_its_entry(hmc, names, reason):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))
    hmc.get_volume_group.return_value = _group(*names)

    record = _run(hmc)

    hmc.delete_virtual_disk.assert_not_awaited()
    assert _reasons(record) == [reason]
    assert _rows() == [(LPAR, DISK)]


def test_an_item_on_an_unauthorized_vios_is_retained(hmc):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))

    record = _run(hmc, authorized=frozenset({VIOS_B}))

    hmc.delete_virtual_disk.assert_not_awaited()
    assert _reasons(record) == ["not_authorized"]


def test_rows_from_two_operations_for_one_disk_are_removed_together(hmc):
    _seed(("root_disk", "virtual_disk", LPAR, DISK), request_id="provision")
    _seed(
        ("added_disk:0", "virtual_disk", LPAR, {**DISK, "extra": "x"}),
        request_id="reconfigure",
    )

    _run(hmc)

    hmc.delete_virtual_disk.assert_awaited_once()
    assert _rows() == []


def test_an_uncertain_delete_pauses_and_resume_classifies_it(hmc):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))
    hmc.delete_virtual_disk.side_effect = HMCError("connection reset")

    paused = _run(hmc)

    assert (paused.state, paused.outcome) == ("paused", "needs_attention")
    assert paused.effects[0].status == "uncertain"
    assert _rows() == [(LPAR, DISK)]
    hmc.get_volume_group.return_value = _group("lpar-a-root-x")

    resumed = _run(hmc, continuation="resume")

    assert (resumed.state, resumed.outcome) == ("terminal", "completed")
    assert resumed.effects[0].status == "applied"
    assert resumed.effects[0].identity == DISK
    assert hmc.delete_virtual_disk.await_count == 1
    assert _rows() == []


def test_a_listed_disk_classifies_not_applied_and_is_deleted_again(hmc):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))
    hmc.delete_virtual_disk.side_effect = [HMCError("connection reset"), None]
    _run(hmc)

    resumed = _run(hmc, continuation="resume")

    assert resumed.outcome == "completed"
    assert hmc.delete_virtual_disk.await_count == 2


def test_a_duplicated_name_after_an_uncertain_delete_needs_attention(hmc):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))
    hmc.delete_virtual_disk.side_effect = HMCError("connection reset")
    _run(hmc)
    hmc.get_volume_group.return_value = _group("lpar-a-root", "lpar-a-root")

    resumed = _run(hmc, continuation="resume")

    assert (resumed.state, resumed.outcome) == ("paused", "needs_attention")
    assert "share the name" in resumed.warnings[0]


def test_a_412_retains_that_item_finishes_the_others_and_fails(hmc):
    second = {**DISK, "disk_name": "lpar-a-root-x"}
    _seed(
        ("root_disk", "virtual_disk", LPAR, DISK),
        ("added_disk:0", "virtual_disk", LPAR, second),
    )
    hmc.delete_virtual_disk.side_effect = [HMCError("changed", 412), None]

    record = _run(hmc)

    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "precondition_failed" in record.warnings[0]
    assert "hmc_delete_virtual_disk" in record.warnings[0]
    assert [e.status for e in record.effects] == ["not_applied", "applied"]
    assert _rows() == [(LPAR, DISK)]
    assert _reasons(record) == ["precondition_failed"]


def test_a_run_that_stopped_before_the_ledger_write_finishes_it(hmc, monkeypatch):
    _seed(("root_disk", "virtual_disk", LPAR, DISK))
    real = store.remove_ledger
    calls: list[int] = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("store write lost")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "remove_ledger", flaky)
    paused = _run(hmc)
    assert paused.outcome == "needs_attention"
    assert paused.effects[0].status == "applied"
    hmc.get_volume_group.return_value = _group("lpar-a-root-x")

    resumed = _run(hmc, continuation="resume")

    assert resumed.outcome == "completed"
    assert resumed.result == {"retained": []}
    assert hmc.delete_virtual_disk.await_count == 1
    assert _rows() == []


def test_the_dry_run_plan_lists_deletable_items_as_pending(hmc):
    _seed(
        ("root_disk", "virtual_disk", LPAR, DISK),
        ("installer_media", "installer_media", LPAR, MEDIA),
    )
    hmc.get_vios_storage_detail.return_value = _detail(
        _mapping(LPAR, _disk_ref()),
        _mapping(
            OTHER_LPAR, {"VirtualOpticalMedia": {"MediaName": MEDIA["media_name"]}}, "x"
        ),
    )

    plan = asyncio.run(plan_storage(hmc, SYSTEM, LPAR))

    assert plan == {
        "deleted": (),
        "retained": ({"kind": "installer_media", **MEDIA, "reason": "referenced"},),
        "pending": ({"kind": "virtual_disk", **DISK},),
    }
    hmc.delete_virtual_disk.assert_not_awaited()


def test_the_journal_reads_applied_deletes_as_deleted_and_open_ones_as_pending():
    applied = EffectRecord(
        "disk.0", "vios.virtual_disk_delete", "vios:v/vg:g/disk:d", "applied", DISK
    )
    open_ = EffectRecord(
        "media.0", "vios.optical_media_delete", "vios:v/vg:g/media:m", "uncertain", None
    )
    other = EffectRecord("delete_lpar", "lpar.delete", "lpar:x", "applied", {})

    effects = [applied, open_, other]

    assert cleanup.deleted_from_effects(effects) == ({"kind": "virtual_disk", **DISK},)
    assert cleanup.pending_from_effects(effects) == (
        {"kind": "installer_media", "target": "vios:v/vg:g/media:m"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,item",
    [
        ("list_vios", DISK_ITEM),
        ("get_vios_storage_detail", DISK_ITEM),
        ("get_volume_group", DISK_ITEM),
        ("list_volume_groups", MEDIA_ITEM),
        ("list_optical_media", MEDIA_ITEM),
    ],
)
async def test_assessment_preserves_fatal_tls_and_stops_reads(hmc, method, item):
    failure = HMCTLSFingerprintError("pin mismatch")
    getattr(hmc, method).side_effect = failure

    with pytest.raises(HMCTLSFingerprintError) as caught:
        await cleanup.assess(hmc, SYSTEM, LPAR, (item, DISK_ITEM, MEDIA_ITEM))

    assert caught.value is failure
    assert hmc.mock_calls[-1][0] == method
    hmc.delete_virtual_disk.assert_not_awaited()
    hmc.delete_optical_media.assert_not_awaited()
