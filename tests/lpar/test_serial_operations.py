"""Constructed REST/profile fixtures exercise the approved finite serial contracts."""

import csv
import io
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter as Adapter
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import serial
from hmcpctl.ssh import lpar_serial

SYS = "11111111-1111-4111-8111-111111111111"
PEER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NEW = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def entry(uuid=PEER, name="peer", native=7, kind="VirtualIOServer", **fields):
    resource = {
        "PartitionName": name,
        "PartitionID": str(native),
        "Description": "",
        "DefaultProfileName": "peer_saved",
        "PartitionIOConfiguration": {"MaximumVirtualIOSlots": "8"},
    }
    resource.update(fields)
    return {"UUID": uuid, "ResourceType": kind, "Resource": resource}


def record(name, native, profile, adapters, maximum=8, **extra):
    values = {
        "name": profile,
        "lpar_name": name,
        "lpar_id": str(native),
        "max_virtual_slots": str(maximum),
        "virtual_serial_adapters": adapters,
        **extra,
    }
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow(
        f"{k}={v}" for k, v in values.items()
    )
    return output.getvalue()


@pytest.fixture
def service(monkeypatch):
    peer = entry()
    local = entry(
        NEW, "new", 9, "LogicalPartition", DefaultProfileName="default_profile"
    )
    feeds = {"LogicalPartition": [], "VirtualIOServer": [peer]}
    resources = {PEER: peer, NEW: local}
    profiles = {
        "peer": record("peer", 7, "peer_saved", "3/server/0/9/new/2/0"),
        "new": record("new", 9, "default_profile", "2/client//7/peer/3/0"),
    }
    hmc = SimpleNamespace(config=HMCConfig.from_mapping({"agent_id": "agent"}))
    hmc.list_logical_partitions = AsyncMock(
        side_effect=lambda system: deepcopy(feeds["LogicalPartition"])
    )
    hmc.list_vios = AsyncMock(
        side_effect=lambda system: deepcopy(feeds["VirtualIOServer"])
    )
    hmc.get_uom = AsyncMock(
        side_effect=lambda kind, uuid: deepcopy(resources[uuid.casefold()])
    )
    monkeypatch.setattr(
        serial, "resolve_system_cli_name", AsyncMock(return_value="system")
    )

    async def read(config, system, name, profile, *, max_output_bytes):
        assert max_output_bytes == 1048576
        return profiles[name]

    reader = AsyncMock(side_effect=read)
    monkeypatch.setattr(lpar_serial, "read_lpar_profile_record", reader)
    return SimpleNamespace(
        hmc=hmc,
        feeds=feeds,
        resources=resources,
        profiles=profiles,
        local=local,
        reader=reader,
    )


async def admit(service, adapters=None, **changes):
    values = {
        "name": "new",
        "partition_id": 9,
        "maximum": 8,
        "kind": "LogicalPartition",
    }
    values.update(changes)
    if adapters is None:
        adapters = (Adapter(2, "client", False, remote_lpar_id=7, remote_slot=3),)
    return await serial.admit_serial_creation(
        service.hmc, SYS, adapters=adapters, **values
    )


def created(service):
    service.feeds["LogicalPartition"].append(service.local)
    return deepcopy(service.local)


@pytest.mark.asyncio
async def test_selected_peer_kind_identity_ownership_and_no_mutation(
    service, monkeypatch
):
    authorize = Mock(wraps=serial.authorize_lpar_ownership_description)
    monkeypatch.setattr(serial, "authorize_lpar_ownership_description", authorize)
    admission = await admit(service)
    peer = admission.peers[0]
    assert (
        peer.kind,
        peer.uuid,
        peer.name,
        peer.partition_id,
        peer.maximum,
        peer.profile_name,
    ) == ("VirtualIOServer", PEER, "peer", 7, 8, "peer_saved")
    assert admission.preexisting == frozenset({PEER})
    authorize.assert_called_once_with(
        service.hmc, "system", "peer", "", operation="lpar-mutation"
    )
    service.hmc.get_uom.assert_awaited_once_with("VirtualIOServer", PEER)
    service.hmc.list_logical_partitions.assert_awaited_once_with(SYS)
    service.hmc.list_vios.assert_awaited_once_with(SYS)
    assert service.resources[PEER] == entry()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "description,denial",
    [
        ("[hmcpctl owner:other created:2026-10-26]", "foreign-owner"),
        ("[hmcpctl broken]", "malformed-token"),
    ],
)
async def test_peer_denial_preserves_existing_audit(
    service, monkeypatch, description, denial
):
    from hmcpctl.operations.lpar import ownership

    audit = Mock()
    monkeypatch.setattr(ownership.audit, "record_ownership_denied", audit)
    service.resources[PEER]["Resource"]["Description"] = description
    with pytest.raises(PermissionError):
        await admit(service)
    assert audit.call_args.kwargs["operation"] == "lpar-mutation"
    assert audit.call_args.kwargs["denial"] == denial
    service.reader.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("UUID", OTHER),
        ("ResourceType", "LogicalPartition"),
        ("PartitionName", "stale"),
        ("PartitionID", "99"),
        ("PartitionIOConfiguration", {}),
        ("PartitionIOConfiguration", {"MaximumVirtualIOSlots": "bad"}),
        ("Description", []),
    ],
)
async def test_peer_resource_must_agree_with_contained_feed(service, field, value):
    service.resources[PEER] = deepcopy(service.resources[PEER])
    destination = (
        service.resources[PEER]
        if field in {"UUID", "ResourceType"}
        else service.resources[PEER]["Resource"]
    )
    destination[field] = value
    with pytest.raises(ValueError):
        await admit(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "uuid",
        "name",
        "native",
        "malformed",
        "oversized",
        "existing-name",
        "existing-id",
    ],
)
async def test_inventory_uniqueness_bounds_and_pending_conflicts(service, mode):
    if mode == "malformed":
        service.feeds["LogicalPartition"] = [{"UUID": OTHER}]
    elif mode == "oversized":
        service.feeds["LogicalPartition"] = [entry()] * 10001
    elif mode == "existing-name":
        service.feeds["LogicalPartition"] = [
            entry(OTHER, "new", 10, "LogicalPartition")
        ]
    elif mode == "existing-id":
        service.feeds["LogicalPartition"] = [
            entry(OTHER, "other", 9, "LogicalPartition")
        ]
    else:
        service.feeds["LogicalPartition"] = [
            entry(
                PEER.upper() if mode == "uuid" else OTHER,
                "peer" if mode == "name" else "other",
                7 if mode == "native" else 10,
                "LogicalPartition",
            )
        ]
    with pytest.raises(ValueError):
        await admit(service)
    service.hmc.get_uom.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selectors",
    [
        {"remote_lpar_name": "absent"},
        {"remote_lpar_id": 99},
        {"remote_lpar_name": "peer", "remote_lpar_id": 99},
        {"remote_lpar_name": "new", "remote_lpar_id": 7},
    ],
)
async def test_missing_and_conflicting_selectors(service, selectors):
    with pytest.raises(ValueError):
        await admit(service, (Adapter(2, "server", False, remote_slot=3, **selectors),))


@pytest.mark.asyncio
async def test_numeric_name_is_name_and_dual_selectors_require_agreement(service):
    service.feeds["VirtualIOServer"][0]["Resource"]["PartitionName"] = "007"
    admission = await admit(
        service,
        (Adapter(2, "server", False, remote_lpar_name="007", remote_lpar_id=7),),
    )
    assert admission.peers[0].name == "007"
    with pytest.raises(ValueError):
        await admit(service, (Adapter(2, "server", False, remote_lpar_name="7"),))


@pytest.mark.asyncio
async def test_pending_self_different_slot_unbound_and_ambiguous_id(service):
    for selectors in (
        {"remote_lpar_name": "new"},
        {"remote_lpar_id": 9},
        {"remote_lpar_name": "new", "remote_lpar_id": 9},
    ):
        admission = await admit(
            service, (Adapter(2, "server", False, remote_slot=3, **selectors),)
        )
        assert admission.peers == (admission.pending,)
        with pytest.raises(ValueError, match="same.*slot"):
            await admit(
                service, (Adapter(2, "server", False, remote_slot=2, **selectors),)
            )
    with pytest.raises(ValueError):
        await admit(
            service,
            (Adapter(2, "server", False, remote_lpar_name="new", remote_lpar_id=9),),
            partition_id=None,
        )
    admission = await admit(
        service, (Adapter(2, "server", False, remote_slot=900),), partition_id=None
    )
    assert admission.peers == (None,)
    assert admission.pending.partition_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_remote_slot_must_be_below_observed_or_explicit_maximum(service, pending):
    with pytest.raises(ValueError, match="maximum"):
        await admit(
            service,
            (
                Adapter(
                    2,
                    "client",
                    False,
                    remote_lpar_name="new" if pending else "peer",
                    remote_slot=8,
                ),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["old", "absent", "name", "id", "kind", "uuid", "feed-name", "read-name"]
)
async def test_created_identity_is_fresh_contained_and_independent(service, change):
    admission = await admit(service)
    candidate = created(service)
    if change == "old":
        candidate = entry()
    elif change == "absent":
        service.feeds["LogicalPartition"] = []
    elif change in {"name", "id"}:
        candidate["Resource"][
            "PartitionName" if change == "name" else "PartitionID"
        ] = "other" if change == "name" else "99"
    elif change in {"kind", "uuid"}:
        candidate["ResourceType" if change == "kind" else "UUID"] = (
            "VirtualIOServer" if change == "kind" else OTHER
        )
    elif change == "feed-name":
        service.feeds["LogicalPartition"][0]["Resource"]["PartitionName"] = "changed"
    else:
        service.resources[NEW] = entry(NEW, "changed", 9, "LogicalPartition")
    with pytest.raises(ValueError):
        await serial.confirm_serial_created(service.hmc, admission, candidate)


@pytest.mark.asyncio
async def test_created_uuid_casefold_preserves_feed_spelling_and_assigned_id(service):
    admission = await admit(service, partition_id=None)
    candidate = created(service)
    service.feeds["LogicalPartition"][0]["UUID"] = NEW.upper()
    confirmed = await serial.confirm_serial_created(service.hmc, admission, candidate)
    assert confirmed["UUID"].casefold() == NEW
    service.hmc.get_uom.assert_awaited_with("LogicalPartition", NEW.upper())


@pytest.mark.asyncio
async def test_saved_pair_is_independent_of_current_and_caches_unique_peer(service):
    adapters = (
        Adapter(2, "client", False, remote_lpar_id=7, remote_slot=3),
        Adapter(4, "client", True, remote_lpar_name="peer", remote_slot=5),
    )
    service.profiles["new"] = record(
        "new", 9, "default_profile", "2/client//7/peer/3/0,4/client//7/peer/5/1"
    )
    service.profiles["peer"] = record(
        "peer", 7, "peer_saved", "3/server/0/9/new/2/0,5/server/0/9/new/4/1"
    )
    admission = await admit(service, adapters)
    step = await serial.serial_creation_step(service.hmc, admission, created(service))
    assert step.status == "ok"
    assert step.result["configuration"] == "verified"
    assert step.result["plane"] == "saved-default-profile"
    assert step.result["current_configuration"] == "unavailable"
    assert step.result["active_traffic"] == "unproven"
    assert [p["status"] for p in step.result["pairs"]] == ["verified-saved-pair"] * 2
    assert service.reader.await_count == 2
    assert [call.args[3] for call in service.reader.await_args_list] == [
        "default_profile",
        "peer_saved",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "family",
    [
        "virtual_eth_adapters",
        "virtual_fc_adapters",
        "virtual_scsi_adapters",
        "vnic_adapters",
    ],
)
@pytest.mark.parametrize(
    "role,local,peer",
    [
        ("client", "2/client//7/peer/3/0", "3/server/0/9/new/2/0"),
        ("server", "2/server/0/7/peer/3/0", "3/client//9/new/2/0"),
    ],
)
async def test_saved_pair_preserves_unrelated_peer_families(
    service, family, role, local, peer
):
    adapter = Adapter(
        2, role, False, 0 if role == "server" else None, remote_lpar_id=7, remote_slot=3
    )
    service.profiles["new"] = record("new", 9, "default_profile", local)
    service.profiles["peer"] = record(
        "peer", 7, "peer_saved", peer, **{family: "opaque"}
    )
    original = service.profiles["peer"]
    admission = await admit(service, (adapter,))
    step = await serial.serial_creation_step(service.hmc, admission, created(service))
    assert step.status == "ok"
    assert step.result["configuration"] == "verified"
    assert step.result["pairs"] == [{"slot": 2, "status": "verified-saved-pair"}]
    assert service.profiles["peer"] == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counterpart",
    [
        "none",
        "3/client//9/new/2/0",
        "3/server/0/99/new/2/0",
        "3/server/0/9/wrong/2/0",
        "3/server/0/9/new/4/0",
        "0/server/1/any//any/1",
    ],
)
async def test_one_created_side_cannot_certify_pair(service, counterpart):
    admission = await admit(service)
    service.profiles["peer"] = record("peer", 7, "peer_saved", counterpart)
    step = await serial.serial_creation_step(service.hmc, admission, created(service))
    assert step.status == "error"
    assert step.result["configuration"] == "verified"
    assert step.result["pairs"][0]["status"] == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counterpart",
    ["3/server/0////0", "3/server/0/9///0", "3/server/0//new//0", "3/server/0///2/0"],
)
async def test_source_shaped_unbound_server_can_accept_selected_client(
    service, counterpart
):
    admission = await admit(service)
    service.profiles["peer"] = record("peer", 7, "peer_saved", counterpart)
    assert (
        await serial.serial_creation_step(service.hmc, admission, created(service))
    ).status == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("local", ["2/server/0////0", "2/server/0///3/0"])
async def test_no_selected_partition_is_configured_unbound(service, local):
    adapter = Adapter(2, "server", False, 0, remote_slot=3 if "3/0" in local else None)
    admission = await admit(service, (adapter,))
    service.profiles["new"] = record("new", 9, "default_profile", local)
    step = await serial.serial_creation_step(service.hmc, admission, created(service))
    assert step.status == "ok"
    assert step.result["pairs"] == [{"slot": 2, "status": "unbound"}]


@pytest.mark.asyncio
async def test_self_pair_uses_observed_local_profile_and_no_guessed_id(service):
    adapters = (
        Adapter(2, "client", False, remote_lpar_name="new", remote_slot=3),
        Adapter(3, "server", False, 0),
    )
    admission = await admit(service, adapters, partition_id=None)
    service.profiles["new"] = record(
        "new", 9, "default_profile", "2/client//9/new/3/0,3/server/0////0"
    )
    local = created(service)
    step = await serial.serial_creation_step(service.hmc, admission, local)
    assert step.status == "ok"
    assert step.result["pairs"][0]["status"] == "verified-saved-pair"
    service.reader.assert_awaited_once()
    service.profiles["new"] = record("new", 9, "default_profile", "2/client//9/new/3/0")
    assert (
        await serial.serial_creation_step(service.hmc, admission, local)
    ).status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observed",
    [
        "2/server//7/peer/3/0",
        "2/client//7/peer/3/1",
        "2/client//99/peer/3/0",
        "2/client//7/wrong/3/0",
        "2/client//7/peer/4/0",
        "none",
        "2/client//7/peer/3/0,4/server/0////0",
    ],
)
async def test_local_requested_fields_and_unexpected_records_are_independent(
    service, observed
):
    admission = await admit(service)
    service.profiles["new"] = record("new", 9, "default_profile", observed)
    step = await serial.serial_creation_step(service.hmc, admission, created(service))
    assert step.status == "error"
    assert step.result["configuration"] == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "controller",
        "mixed",
        "peer-missing-profile",
        "peer-moved",
        "peer-max",
        "max",
        "bad-profile",
        "transport",
        "missing-created",
    ],
)
async def test_unavailable_or_stale_observations_block_completion(service, mode):
    admission = await admit(service)
    local = created(service)
    if mode == "mixed":
        service.profiles["new"] = record(
            "new",
            9,
            "default_profile",
            "2/client//7/peer/3/0",
            virtual_scsi_adapters="opaque",
        )
    elif mode == "peer-missing-profile":
        service.resources[PEER]["Resource"].pop("DefaultProfileName")
    elif mode == "peer-moved":
        service.feeds["VirtualIOServer"] = []
    elif mode == "peer-max":
        service.resources[PEER]["Resource"]["PartitionIOConfiguration"][
            "MaximumVirtualIOSlots"
        ] = "4"
    elif mode == "max":
        service.profiles["new"] = record(
            "new", 9, "default_profile", "2/client//7/peer/3/0", maximum=16
        )
    elif mode == "bad-profile":
        service.profiles["peer"] = record(
            "wrong", 7, "peer_saved", "3/server/0/9/new/2/0"
        )
    elif mode == "transport":
        service.reader.side_effect = HMCError("sensitive external message")
    elif mode == "missing-created":
        local = None
    step = await serial.serial_creation_step(
        service.hmc, admission, local, controller_mode=mode == "controller"
    )
    assert step.status == "error"
    assert "sensitive external message" not in str(step.result)
    if mode == "controller":
        assert step.result["plane"] == "current-unavailable"
        service.reader.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("UUID", "invalid"),
        ("Resource", None),
        ("PartitionIOConfiguration", None),
        ("DefaultProfileName", []),
        ("PartitionID", ""),
    ],
)
async def test_malformed_feed_fields_refuse_before_peer_authorization(
    service, field, value
):
    item = service.feeds["VirtualIOServer"][0]
    destination = item if field in {"UUID", "Resource"} else item["Resource"]
    destination[field] = value
    with pytest.raises((ValueError, TypeError)):
        await admit(service)
    service.hmc.get_uom.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"kind": "wrong"}, {"partition_id": True}])
async def test_invalid_creation_identity_refuses_before_inventory(service, changes):
    with pytest.raises(ValueError):
        await admit(service, **changes)
    service.hmc.list_vios.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "supports,observed", [(0, "2/server/////0"), (None, "2/server/1////0")]
)
async def test_explicit_and_implicit_hmc_observations_are_not_interchangeable(
    service, supports, observed
):
    admission = await admit(service, (Adapter(2, "server", False, supports),))
    service.profiles["new"] = record("new", 9, "default_profile", observed)
    step = await serial.serial_creation_step(service.hmc, admission, created(service))
    assert step.status == "error"
    assert step.result["configuration"] == "unverified"


@pytest.mark.asyncio
async def test_missing_default_profile_is_unavailable_not_a_guessed_read(service):
    service.feeds["VirtualIOServer"][0]["Resource"].pop("DefaultProfileName")
    admission = await admit(service)
    step = await serial.serial_creation_step(service.hmc, admission, created(service))
    assert step.status == "error"
    assert step.result["configuration"] == "verified"
    assert step.result["pairs"][0]["status"] == "unverified"
    service.reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_independently_changed_created_maximum_does_not_verify(service):
    admission = await admit(service)
    service.local["Resource"]["PartitionIOConfiguration"]["MaximumVirtualIOSlots"] = (
        "16"
    )
    step = await serial.serial_creation_step(service.hmc, admission, created(service))
    assert step.status == "error"
    service.reader.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_slot", [None, 3])
async def test_selected_server_requires_independent_reciprocal_client(
    service, remote_slot
):
    admission = await admit(
        service,
        (
            Adapter(
                2, "server", True, 0, remote_lpar_name="peer", remote_slot=remote_slot
            ),
        ),
    )
    suffix = "" if remote_slot is None else "3"
    service.profiles["new"] = record(
        "new", 9, "default_profile", f"2/server/0/7/peer/{suffix}/1"
    )
    service.profiles["peer"] = record("peer", 7, "peer_saved", "3/client//9/new/2/0")
    local = created(service)
    assert (
        await serial.serial_creation_step(service.hmc, admission, local)
    ).status == "ok"
    service.profiles["peer"] = record("peer", 7, "peer_saved", "3/client//9/other/2/0")
    step = await serial.serial_creation_step(service.hmc, admission, local)
    assert step.status == "error"
    assert step.result["configuration"] == "verified"


@pytest.mark.asyncio
async def test_inventory_without_maximum_is_resolved_from_selected_resource(service):
    service.feeds["VirtualIOServer"][0]["Resource"].pop("PartitionIOConfiguration")
    service.resources[PEER] = entry()
    admission = await admit(service)
    assert admission.peers[0].maximum == 8
    service.resources[PEER]["Resource"].pop("PartitionIOConfiguration")
    with pytest.raises(ValueError, match="maximum"):
        await admit(service)


@pytest.mark.asyncio
async def test_vios_pending_uses_vios_resource_and_lpar_peer_uses_lpar_resource(
    service,
):
    service.feeds["LogicalPartition"] = [entry(kind="LogicalPartition")]
    service.feeds["VirtualIOServer"] = []
    service.resources[PEER] = entry(kind="LogicalPartition")
    service.local["ResourceType"] = "VirtualIOServer"
    admission = await admit(service, kind="VirtualIOServer")
    service.feeds["VirtualIOServer"].append(service.local)
    step = await serial.serial_creation_step(service.hmc, admission, service.local)
    assert step.status == "ok"
    assert admission.peers[0].kind == "LogicalPartition"
    assert {call.args for call in service.hmc.get_uom.await_args_list} == {
        ("VirtualIOServer", NEW),
        ("LogicalPartition", PEER),
    }


@pytest.mark.asyncio
async def test_request_validation_precedes_all_external_reads(service):
    for changes in ({"maximum": None}, {"maximum": 2}, {"partition_id": False}):
        with pytest.raises(ValueError):
            await admit(service, **changes)
    service.hmc.list_logical_partitions.assert_not_awaited()
    service.hmc.list_vios.assert_not_awaited()
    service.hmc.get_uom.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_peer_unavailable_is_not_authorized(service, monkeypatch):
    authorize = Mock()
    monkeypatch.setattr(serial, "authorize_lpar_ownership_description", authorize)
    service.hmc.get_uom.side_effect = None
    service.hmc.get_uom.return_value = None
    with pytest.raises(ValueError):
        await admit(service)
    service.hmc.get_uom.side_effect = HMCError("unavailable")
    with pytest.raises(HMCError):
        await admit(service)
    authorize.assert_not_called()
