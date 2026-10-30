"""Contained running-adapter updates, independent evidence and one-attempt outcomes."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import ownership
from hmcpctl.operations.virtualization import adapters
from hmcpctl.ssh.transport import HMCCLIError

SYSTEM = "22222222-2222-4222-8222-222222222222"
LPAR = "11111111-1111-4111-8111-111111111111"
TARGET = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FOREIGN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def adapter(identity=TARGET, slot="4"):
    return {
        "UUID": identity,
        "ResourceType": "ClientNetworkAdapter",
        "Resource": {
            "VirtualSlotNumber": slot,
            "PortVLANID": "100",
            "MACAddress": "020000000001",
            "VirtualSwitchID": "0",
            "TaggedVLANSupported": "true",
            "TaggedVLANIDs": "101 102",
            "QualityOfServicePriority": "3",
            "QualityOfServicePriorityEnabled": "true",
            "VirtualStationInterfaceManagerID": "1",
            "VirtualStationInterfaceTypeID": "2",
            "VirtualStationInterfaceTypeVersion": "3",
            "UnknownPolicy": {"Members": ["a", "b"]},
            "Metadata": {"Atom": {"Observed": "baseline"}},
        },
    }


@pytest.fixture
def context(monkeypatch):
    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping({"agent_id": "test-agent"})
    partition = {
        "UUID": LPAR,
        "ResourceType": "LogicalPartition",
        "Resource": {
            "PartitionName": "part-R1",
            "PartitionID": "7",
            "PartitionState": "running",
            "CurrentProfileSync": "On",
            "LastActivatedProfile": "default",
        },
    }
    hmc.list_system_summaries.return_value = [{"UUID": SYSTEM, "SystemName": "sys-R1"}]
    hmc.list_logical_partitions.return_value = [partition]
    hmc.get_logical_partition.return_value = partition
    before = [adapter(), adapter(FOREIGN, "5")]
    after = deepcopy(before)
    after[0]["Resource"]["QualityOfServicePriority"] = "0"
    hmc._network_adapter_creation_feed.side_effect = [before, deepcopy(before), after]
    description = AsyncMock(
        return_value="[hmcpctl owner:test-agent created:2026-10-30]"
    )
    monkeypatch.setattr(ownership, "get_lpar_description", description)
    writer = AsyncMock(return_value="native text is not an observation")
    monkeypatch.setattr(adapters, "_set_ethernet_adapter", writer, raising=False)
    return hmc, partition, before, after, writer, description


async def update(context, **values):
    subject = getattr(adapters, "update_network_adapter", None)
    assert callable(subject), "typed running-adapter update is missing"
    return await subject(
        context[0],
        LPAR,
        TARGET,
        system_name_or_uuid=SYSTEM,
        exclusive_writer_window=True,
        **values,
    )


@pytest.mark.asyncio
async def test_selected_zero_is_verified_from_independent_readback(context):
    result = await update(context, qos_priority=0)
    assert result.submission == "completed"
    assert result.mutation_dispatched is True
    assert result.verification.status == "verified"
    assert result.verification.expected == {"qos_priority": 0}
    assert result.verification.observed == {"qos_priority": 0}
    assert result.preservation.status == "unchanged"
    assert result.change_location.current_profile_sync == "On"
    assert result.change_location.lives_in == "unknown"
    assert result.change_location.profile_name == "default"
    context[4].assert_awaited_once_with(
        context[0].config, "sys-R1", 7, 4, [("qos_priority", 0)]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionState", "not activated"),
        ("PartitionState", None),
        ("PartitionID", "0"),
        ("PartitionID", True),
        ("PartitionName", None),
    ],
)
async def test_unusable_partition_prevents_the_write(context, field, value):
    context[1]["Resource"][field] = value
    with pytest.raises((ValueError, HMCError)):
        await update(context, qos_priority=0)
    context[4].assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_owner_is_checked_by_the_actual_guard(context):
    context[5].return_value = "[hmcpctl owner:other-agent created:2026-10-30]"
    with pytest.raises(PermissionError):
        await update(context, qos_priority=0)
    context[4].assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_system_names_refuse_even_with_ownership_override(context):
    context[0].list_system_summaries.return_value.append(
        {"UUID": FOREIGN, "SystemName": "sys-R1"}
    )
    with pytest.raises(ValueError, match="[Aa]mbigu|unique|duplicat"):
        await update(context, qos_priority=0, ownership_override=True)
    context[4].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", "not-a-uuid"])
async def test_bad_adapter_selector_has_no_transport(context, bad):
    subject = getattr(adapters, "update_network_adapter", None)
    assert callable(subject)
    with pytest.raises(ValueError):
        await subject(
            context[0],
            LPAR,
            bad,
            system_name_or_uuid=SYSTEM,
            exclusive_writer_window=True,
            qos_priority=0,
        )
    context[0].list_system_summaries.assert_not_awaited()
    context[4].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [False, None, 1, "true"])
async def test_explicit_literal_writer_acknowledgement_is_required(context, ack):
    subject = getattr(adapters, "update_network_adapter", None)
    assert callable(subject)
    with pytest.raises(ValueError):
        await subject(
            context[0],
            LPAR,
            TARGET,
            system_name_or_uuid=SYSTEM,
            exclusive_writer_window=ack,
            qos_priority=0,
        )
    context[0].list_system_summaries.assert_not_awaited()
    context[4].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["uuid", "slot", "foreign-policy", "selected-value"])
async def test_fresh_configuration_drift_prevents_write(context, change):
    hmc, _, before, after, writer, _ = context
    fresh = deepcopy(before)
    if change == "uuid":
        fresh[0]["UUID"] = SYSTEM
    elif change == "slot":
        fresh[0]["Resource"]["VirtualSlotNumber"] = "6"
    elif change == "foreign-policy":
        fresh[1]["Resource"]["UnknownPolicy"] = "changed"
    else:
        fresh[0]["Resource"]["QualityOfServicePriority"] = "7"
    hmc._network_adapter_creation_feed.side_effect = [before, fresh, after]
    with pytest.raises((ValueError, HMCError)):
        await update(context, qos_priority=0)
    writer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,ids,expected,wire",
    [
        ("replace", [103], [103], ("addl_vlan_ids", "103")),
        ("add", [103], [101, 102, 103], ("addl_vlan_ids+", "103")),
        ("remove", [101], [102], ("addl_vlan_ids-", "101")),
        ("replace", [], [], ("addl_vlan_ids-", "101,102")),
    ],
)
async def test_vlan_actions_verify_the_resulting_set(
    context, mode, ids, expected, wire
):
    context[3][0]["Resource"]["TaggedVLANIDs"] = " ".join(map(str, reversed(expected)))
    result = await update(context, addl_vlan_ids=ids, vlan_action=mode)
    assert result.verification.status == "verified"
    assert result.verification.expected == {"addl_vlan_ids": expected}
    assert context[4].await_args.args[-1] == [wire]


@pytest.mark.asyncio
async def test_already_empty_only_request_is_positively_observed_no_write(context):
    before = deepcopy(context[2])
    before[0]["Resource"]["TaggedVLANIDs"] = ""
    context[0]._network_adapter_creation_feed.side_effect = [before, deepcopy(before)]
    result = await update(context, addl_vlan_ids=[])
    assert (result.submission, result.mutation_dispatched, result.reason) == (
        "not_dispatched",
        False,
        "already_satisfied",
    )
    assert result.verification.status == "verified"
    assert result.preservation.status == "unchanged"
    context[4].assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_can_repair_a_complete_baseline_above_twenty(context):
    before = deepcopy(context[2])
    before[0]["Resource"]["TaggedVLANIDs"] = " ".join(map(str, range(101, 122)))
    after = deepcopy(before)
    after[0]["Resource"]["TaggedVLANIDs"] = " ".join(map(str, range(101, 121)))
    context[0]._network_adapter_creation_feed.side_effect = [
        before,
        deepcopy(before),
        after,
    ]
    result = await update(context, addl_vlan_ids=[121], vlan_action="remove")
    assert result.verification.status == "verified"
    assert result.verification.expected["addl_vlan_ids"] == list(range(101, 121))


@pytest.mark.asyncio
async def test_disabling_tagging_requires_explicit_empty_result(context):
    with pytest.raises(ValueError):
        await update(context, tagged=False)
    context[4].assert_not_awaited()


@pytest.mark.asyncio
async def test_false_tagging_and_explicit_empty_replacement_are_retained(context):
    context[3][0]["Resource"].update(TaggedVLANSupported="false", TaggedVLANIDs="")
    result = await update(context, tagged=False, addl_vlan_ids=[])
    assert result.verification.status == "verified"
    assert context[4].await_args.args[-1] == [
        ("ieee_virtual_eth", 0),
        ("addl_vlan_ids-", "101,102"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status,code",
    [
        (HMCCLIError("opaque timeout HSCL9999"), "unknown", None),
        (HMCCLIError("remote failed", remote_nonzero_completed=True), "rejected", None),
        (
            HMCCLIError(
                "remote refused",
                remote_nonzero_completed=True,
                remote_refusal_code="HSCL1234",
            ),
            "rejected",
            "HSCL1234",
        ),
    ],
)
async def test_native_provenance_and_post_observation_remain_separate(
    context, error, status, code
):
    context[4].side_effect = error
    result = await update(context, qos_priority=0)
    assert result.submission == status and result.refusal_code == code
    assert result.verification.status == "verified"
    context[4].assert_awaited_once()
    context[0].delete_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_command_output_cannot_verify_missing_readback(context):
    context[0]._network_adapter_creation_feed.side_effect = [
        context[2],
        deepcopy(context[2]),
        HMCError("unavailable"),
    ]
    result = await update(context, qos_priority=0)
    assert result.submission == "completed"
    assert result.verification.status == "unavailable"
    assert result.preservation.status == "unavailable"


@pytest.mark.asyncio
async def test_known_mismatch_wins_over_another_unavailable_selected_field(context):
    context[3][0]["Resource"]["QualityOfServicePriority"] = "7"
    context[3][0]["Resource"].pop("VirtualStationInterfaceManagerID")
    result = await update(context, qos_priority=0, vsi_manager_id=0)
    assert result.verification.status == "mismatched"
    assert result.verification.mismatched_fields == ("qos_priority",)
    assert result.verification.unavailable_fields == ("vsi_manager_id",)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "0", "-1", "1"])
async def test_vsi_profile_removal_has_no_invented_positive_oracle(context, value):
    for key in [
        "VirtualStationInterfaceManagerID",
        "VirtualStationInterfaceTypeID",
        "VirtualStationInterfaceTypeVersion",
    ]:
        context[3][0]["Resource"][key] = value
    result = await update(context, remove_vsi_profile=True)
    assert result.verification.status == "unavailable"
    assert result.verification.unavailable_fields == ("remove_vsi_profile",)
    assert context[4].await_args.args[-1] == [("vsi_manager_id", "none")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["foreign", "unknown-order", "target-slot", "new", "missing"]
)
async def test_bounded_preservation_detects_independent_drift(context, change):
    if change == "foreign":
        context[3][1]["Resource"]["PortVLANID"] = "200"
    elif change == "unknown-order":
        context[3][0]["Resource"]["UnknownPolicy"]["Members"].reverse()
    elif change == "target-slot":
        context[3][0]["Resource"]["VirtualSlotNumber"] = "6"
    elif change == "new":
        context[3].append(adapter(SYSTEM, "6"))
    else:
        context[3].pop(0)
    result = await update(context, qos_priority=0)
    assert result.preservation.status == "drifted"
    assert result.preservation.changed_adapter_uuids


@pytest.mark.asyncio
async def test_known_drift_is_not_lost_when_another_resource_is_unavailable(context):
    context[3][0]["Resource"]["PortVLANID"] = "200"
    context[3][1]["Resource"] = None
    result = await update(context, qos_priority=0)
    assert result.preservation.status == "drifted"


@pytest.mark.asyncio
async def test_metadata_changes_do_not_claim_raw_xml_preservation(context):
    context[3][0]["Resource"]["Metadata"] = {"Atom": {"Observed": "later"}}
    result = await update(context, qos_priority=0)
    assert result.preservation.status == "unchanged"
    assert (
        result.preservation.scope
        == "parsed_adapter_resources_except_metadata_and_selected_fields"
    )


@pytest.mark.asyncio
async def test_uuid_case_is_not_identity_drift(context):
    context[3][0]["UUID"] = TARGET.upper()
    result = await update(context, qos_priority=0)
    assert result.verification.status == "verified"
    assert result.preservation.status == "unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["duplicate-uuid", "duplicate-slot", "wrong-type", "missing-slot"]
)
async def test_invalid_baseline_binding_refuses_before_write(context, change):
    before = deepcopy(context[2])
    if change == "duplicate-uuid":
        before.append(deepcopy(before[0]))
    elif change == "duplicate-slot":
        before[1]["Resource"]["VirtualSlotNumber"] = "4"
    elif change == "wrong-type":
        before[0]["ResourceType"] = "VirtualSCSIClientAdapter"
    else:
        before[0]["Resource"].pop("VirtualSlotNumber")
    context[0]._network_adapter_creation_feed.side_effect = [before]
    with pytest.raises((ValueError, HMCError)):
        await update(context, qos_priority=0)
    context[4].assert_not_awaited()


@pytest.mark.asyncio
async def test_qos_disable_uses_only_its_positive_enable_flag(context):
    context[3][0]["Resource"].update(
        QualityOfServicePriorityEnabled="false", QualityOfServicePriority="7"
    )
    result = await update(context, qos_priority="none")
    assert result.verification.status == "verified"
    assert result.preservation.status == "unchanged"
    assert result.verification.observed == {"qos_priority": "none"}


@pytest.mark.asyncio
async def test_missing_qos_enable_is_unavailable_not_zero(context):
    context[3][0]["Resource"].pop("QualityOfServicePriorityEnabled")
    result = await update(context, qos_priority=0)
    assert result.verification.status == "unavailable"
    assert result.verification.unavailable_fields == ("qos_priority",)


@pytest.mark.asyncio
async def test_numeric_vsi_fields_can_be_requested_independently(context):
    context[3][0]["Resource"]["VirtualStationInterfaceManagerID"] = "0"
    result = await update(context, vsi_manager_id=0)
    assert result.verification.status == "verified"
    assert result.verification.expected == {"vsi_manager_id": 0}
    assert context[4].await_args.args[-1] == [("vsi_manager_id", 0)]


@pytest.mark.asyncio
async def test_caller_list_is_copied_before_the_first_await(context):
    requested = [103]
    summaries = deepcopy(context[0].list_system_summaries.return_value)

    async def mutate_caller():
        requested[:] = [104]
        return summaries

    context[0].list_system_summaries.side_effect = mutate_caller
    context[3][0]["Resource"]["TaggedVLANIDs"] = "103"
    result = await update(context, addl_vlan_ids=requested)
    assert requested == [104]
    assert result.verification.expected == {"addl_vlan_ids": [103]}
    assert result.verification.status == "verified"


@pytest.mark.asyncio
async def test_native_error_details_are_not_persisted_in_result(context):
    context[4].side_effect = HMCCLIError("remote output password=private-sentinel")
    result = await update(context, qos_priority=0)
    assert result.submission == "unknown"
    assert "private-sentinel" not in repr(result)


@pytest.mark.asyncio
async def test_post_partition_identity_change_disqualifies_verification(context):
    async def change_identity(*args):
        context[1]["Resource"]["PartitionID"] = "8"
        return ""

    context[4].side_effect = change_identity
    result = await update(context, qos_priority=0)
    assert result.verification.status == "unavailable"
    assert "identity" in " ".join(result.errors).lower()


@pytest.mark.asyncio
async def test_missing_fresh_name_index_never_uses_a_native_selector_fallback(
    context, monkeypatch
):
    summaries = deepcopy(context[0].list_system_summaries.return_value)
    context[0].list_system_summaries.side_effect = [
        summaries,
        HMCError("name index unavailable"),
        summaries,
    ]
    fallback = AsyncMock(return_value="sys-R1")
    monkeypatch.setattr(ownership, "resolve_system_cli_name", fallback)
    with pytest.raises((ValueError, HMCError)):
        await update(context, qos_priority=0)
    fallback.assert_not_awaited()
    context[4].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["sys\nnext", "sys\x00tail", "sys\x7f"])
async def test_control_bearing_native_name_refuses_before_ownership_read(context, name):
    context[0].list_system_summaries.return_value[0]["SystemName"] = name
    with pytest.raises(ValueError):
        await update(context, qos_priority=0)
    context[5].assert_not_awaited()
    context[4].assert_not_awaited()
