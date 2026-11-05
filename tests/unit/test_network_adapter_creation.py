"""Selected client Ethernet creation and independent stored-field proof (#1821)."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from conftest import live_fixture

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError, HMCTransportError
from hmcpctl.operations.lpar import ownership
from hmcpctl.operations.virtualization.adapters import add_network_adapter

LPAR = "11111111-1111-4111-8111-111111111111"
SYSTEM = "22222222-2222-4222-8222-222222222222"
CREATED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OLD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
FIELDS = {
    "addl_vlan_ids": [2, 3],
    "allowed_os_mac_addrs": "all",
    "qos_priority": 0,
    "vsi_manager_id": 1,
    "vsi_type_id": 2,
    "vsi_type_version": 3,
}
STORED = {
    "TaggedVLANIDs": "2 3",
    "AllowedOperatingSystemMACAddresses": "ALL",
    "QualityOfServicePriority": "0",
    "QualityOfServicePriorityEnabled": "true",
    "VirtualStationInterfaceManagerID": "1",
    "VirtualStationInterfaceTypeID": "2",
    "VirtualStationInterfaceTypeVersion": "3",
}


def entry(identity=CREATED, values=None):
    return {
        "UUID": identity,
        "ResourceType": "ClientNetworkAdapter",
        "Resource": deepcopy(STORED if values is None else values),
    }


@pytest.fixture
def hmc(monkeypatch):
    client = AsyncMock(spec=HMCClient)
    client.config = HMCConfig.from_mapping({"agent_id": "test-agent"})
    partition = {
        "UUID": LPAR,
        "ResourceType": "LogicalPartition",
        "Resource": {
            "PartitionName": "part-R1",
            "CurrentProfileSync": "Disabled",
        },
    }
    client.list_system_summaries.return_value = [
        {"UUID": SYSTEM, "SystemName": "sys-R1"}
    ]
    client.list_logical_partitions.return_value = [partition]
    client.get_logical_partition.return_value = partition
    client.add_network_adapter.return_value = entry()
    client._network_adapter_creation_feed.side_effect = [[], [entry()]]
    monkeypatch.setattr(
        ownership,
        "get_lpar_description",
        AsyncMock(return_value="[hmcpctl owner:test-agent created:2026-10-29]"),
    )
    return client


async def create(hmc, **values):
    return await add_network_adapter(
        hmc, LPAR, 100, tagged=True, system_name_or_uuid=SYSTEM, **values
    )


@pytest.mark.asyncio
async def test_selected_fields_require_new_contained_identity_and_independent_values(
    hmc,
):
    result = await create(hmc, **FIELDS)
    assert result.resource == entry()
    assert result.change_location.lives_in == "current-configuration"
    assert result.creation_verification == {
        "status": "verified",
        "checked_fields": list(FIELDS),
        "mismatched_fields": [],
        "unavailable_fields": [],
    }
    assert hmc._network_adapter_creation_feed.await_count == 2
    hmc.add_network_adapter.assert_awaited_once()
    assert hmc.add_network_adapter.await_args.kwargs == {
        "slot_number": None,
        "virtual_switch_id": None,
        "tagged": True,
        "mac_address": None,
        **FIELDS,
    }


@pytest.mark.asyncio
async def test_omitted_new_fields_keep_legacy_reads_and_result(hmc):
    result = await create(hmc)
    assert result.creation_verification is None
    hmc._network_adapter_creation_feed.assert_not_awaited()
    assert not set(FIELDS) & hmc.add_network_adapter.await_args.kwargs.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        {"addl_vlan_ids": [True]},
        {"addl_vlan_ids": [None]},
        {"addl_vlan_ids": [4095]},
        {"allowed_os_mac_addrs": []},
        {"qos_priority": True},
        {"vsi_type_id": 2**31},
    ],
)
async def test_bad_selected_fields_refuse_before_identity_or_description_reads(
    hmc, values
):
    with pytest.raises(ValueError):
        await create(hmc, **values)
    hmc.list_system_summaries.assert_not_awaited()
    hmc.list_logical_partitions.assert_not_awaited()
    ownership.get_lpar_description.assert_not_awaited()
    hmc._network_adapter_creation_feed.assert_not_awaited()
    hmc.add_network_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_ownership_guard_blocks_foreign_partition_before_create(hmc):
    ownership.get_lpar_description.return_value = (
        "[hmcpctl owner:other-agent created:2026-10-29]"
    )
    with pytest.raises(PermissionError):
        await create(hmc, qos_priority=0)
    hmc._network_adapter_creation_feed.assert_not_awaited()
    hmc.add_network_adapter.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "baseline",
    [
        [entry(None)],
        [entry("not-a-uuid")],
        [entry(OLD), entry(OLD.upper())],
        [{"UUID": OLD, "ResourceType": "VirtualSCSIClientAdapter", "Resource": {}}],
    ],
)
async def test_unusable_baseline_blocks_the_only_write(hmc, baseline):
    hmc._network_adapter_creation_feed.side_effect = [baseline]
    with pytest.raises(HMCError):
        await create(hmc, qos_priority=0)
    hmc.add_network_adapter.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        entry(None),
        entry("not-a-uuid"),
        {"UUID": CREATED, "ResourceType": "VirtualSCSIClientAdapter", "Resource": {}},
    ],
)
async def test_unusable_create_identity_is_unavailable_not_inferred(hmc, response):
    hmc.add_network_adapter.return_value = response
    result = await create(hmc, qos_priority=0)
    assert result.creation_verification["status"] == "unavailable"
    assert result.creation_verification["checked_fields"] == ["qos_priority"]
    assert result.creation_verification["unavailable_fields"] == ["qos_priority"]
    hmc.add_network_adapter.assert_awaited_once()
    hmc.delete_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_preexisting_case_variant_is_not_verified_as_new(hmc):
    hmc._network_adapter_creation_feed.side_effect = [
        [entry(CREATED.upper())],
        [entry()],
    ]
    result = await create(hmc, qos_priority=0)
    assert result.creation_verification["status"] == "unavailable"
    hmc.delete_adapter.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after",
    [
        [],
        [entry(OLD)],
        [entry(), entry(CREATED.upper())],
        [entry(), entry(None)],
    ],
)
async def test_missing_foreign_or_ambiguous_post_identity_remains_unavailable(
    hmc, after
):
    hmc._network_adapter_creation_feed.side_effect = [[], after]
    result = await create(hmc, qos_priority=0)
    assert result.creation_verification["status"] == "unavailable"
    hmc.delete_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_uuid_case_and_attributed_fields_are_compared_semantically(hmc):
    values = {
        key: {"@attrs": {"ksv": "V1_0"}, "text": value} for key, value in STORED.items()
    }
    hmc._network_adapter_creation_feed.side_effect = [
        [],
        [entry(CREATED.upper(), values), entry(OLD)],
    ]
    result = await create(hmc, **FIELDS)
    assert result.creation_verification["status"] == "verified"


@pytest.mark.asyncio
async def test_known_mismatch_outranks_missing_field_and_keeps_both_inventories(hmc):
    values = {**STORED, "QualityOfServicePriority": "1"}
    del values["VirtualStationInterfaceTypeID"]
    hmc._network_adapter_creation_feed.side_effect = [[], [entry(values=values)]]
    result = await create(hmc, **FIELDS)
    assert result.creation_verification == {
        "status": "mismatched",
        "checked_fields": list(FIELDS),
        "mismatched_fields": ["qos_priority"],
        "unavailable_fields": ["vsi_type_id"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        None,
        ["2", "2"],
        "2.0",
        {"text": "2"},
        {"@attrs": {}, "text": "2", "Nested": "3"},
    ],
)
async def test_missing_repeated_or_malformed_required_leaf_is_unavailable(hmc, raw):
    hmc._network_adapter_creation_feed.side_effect = [
        [],
        [
            entry(
                values={
                    **STORED,
                    "VirtualStationInterfaceTypeID": raw,
                }
            )
        ],
    ]
    result = await create(hmc, vsi_type_id=2)
    assert result.creation_verification["status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "status", "mismatched"),
    [([], "verified", []), ([2], "mismatched", ["addl_vlan_ids"])],
)
async def test_absent_tagged_vlan_ids_means_empty(hmc, requested, status, mismatched):
    values = {key: value for key, value in STORED.items() if key != "TaggedVLANIDs"}
    hmc._network_adapter_creation_feed.side_effect = [[], [entry(values=values)]]
    result = await create(hmc, addl_vlan_ids=requested)
    assert result.creation_verification == {
        "status": status,
        "checked_fields": ["addl_vlan_ids"],
        "mismatched_fields": mismatched,
        "unavailable_fields": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, ["2"], {"text": "2"}, "2.0", "0", "4095"])
async def test_null_or_malformed_tagged_vlan_ids_stays_unavailable(hmc, raw):
    hmc._network_adapter_creation_feed.side_effect = [
        [],
        [entry(values={**STORED, "TaggedVLANIDs": raw})],
    ]
    result = await create(hmc, addl_vlan_ids=[])
    assert result.creation_verification["status"] == "unavailable"
    assert result.creation_verification["unavailable_fields"] == ["addl_vlan_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observed",
    [
        {"UUID": CREATED, "ResourceType": "ClientNetworkAdapter"},
        entry(values=[]),
        {**entry(), "ResourceType": "OtherAdapter"},
    ],
)
async def test_absent_vlans_does_not_verify_missing_or_wrong_resource(hmc, observed):
    hmc._network_adapter_creation_feed.side_effect = [[], [observed]]
    result = await create(hmc, addl_vlan_ids=[])
    assert result.creation_verification["status"] == "unavailable"
    assert result.creation_verification["unavailable_fields"] == ["addl_vlan_ids"]


@pytest.mark.asyncio
async def test_explicit_empty_mac_none_and_disabled_qos_are_not_omission(hmc):
    values = {
        **STORED,
        "TaggedVLANIDs": "",
        "AllowedOperatingSystemMACAddresses": "NONE",
        "QualityOfServicePriorityEnabled": "false",
        "QualityOfServicePriority": "malformed",
    }
    hmc._network_adapter_creation_feed.side_effect = [[], [entry(values=values)]]
    result = await create(
        hmc, addl_vlan_ids=[], allowed_os_mac_addrs="none", qos_priority="none"
    )
    assert result.creation_verification["status"] == "verified"
    assert result.creation_verification["checked_fields"] == [
        "addl_vlan_ids",
        "allowed_os_mac_addrs",
        "qos_priority",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [HMCError("rejected"), HMCTransportError("uncertain")]
)
async def test_native_submission_error_preserves_exception_and_does_not_retry(
    hmc, failure
):
    hmc.add_network_adapter.side_effect = failure
    with pytest.raises(type(failure)) as error:
        await create(hmc, qos_priority=0)
    assert error.value is failure
    hmc.add_network_adapter.assert_awaited_once()
    assert hmc._network_adapter_creation_feed.await_count == 1
    hmc.delete_adapter.assert_not_awaited()


def captured_refusal(name):
    """The native add's HMCError for a captured #1864 refusal (ADR 0315)."""
    capture = live_fixture(name)
    return HMCError(f"PUT {capture['path']} failed", capture["status"], capture["body"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "values", "guidance"),
    [
        (
            "rest-client-network-add-vsi-rmc-refused",
            FIELDS,
            ("HSCLB021", "RMC state", "serving VIOS", "vsi_manager_id"),
        ),
        (
            "rest-client-network-add-state-refused",
            {},
            ("resource_config", "chsyscfg -r lpar -m <system> -o apply"),
        ),
    ],
)
async def test_known_add_refusals_are_classified(hmc, name, values, guidance):
    original = captured_refusal(name)
    hmc.add_network_adapter.side_effect = original
    with pytest.raises(HMCError) as error:
        await create(hmc, **values)
    assert error.value is not original
    assert error.value.__cause__ is original
    assert error.value.status_code == 500
    assert error.value.body == original.body
    message = str(error.value)
    assert message.startswith("Client network adapter add refused:")
    assert all(
        text in message
        for text in (*guidance, "hmc_list_adapters", "hmcpctl adapters list")
    )
    hmc.add_network_adapter.assert_awaited_once()
    hmc.delete_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_rmc_refusal_without_vsi_fields_names_no_vsi_cause(hmc):
    original = captured_refusal("rest-client-network-add-vsi-rmc-refused")
    hmc.add_network_adapter.side_effect = original
    with pytest.raises(HMCError) as error:
        await create(hmc, qos_priority=0)
    message = str(error.value).split(" (HTTP 500)")[0]
    assert "HSCLB021" in message and "RMC state" in message
    assert "VSI" not in message and "vsi_" not in message
    assert error.value.__cause__ is original


@pytest.mark.asyncio
async def test_known_code_outside_a_500_passes_through(hmc):
    capture = live_fixture("rest-client-network-add-state-refused")
    original = HMCError("PUT failed", 400, capture["body"])
    hmc.add_network_adapter.side_effect = original
    with pytest.raises(HMCError) as error:
        await create(hmc)
    assert error.value is original


@pytest.mark.asyncio
async def test_lone_vsi_field_refusal_passes_through(hmc):
    """A regression guard: REST0126 alone is not one of the classified refusals."""
    original = captured_refusal("rest-client-network-add-vsi-incomplete")
    hmc.add_network_adapter.side_effect = original
    with pytest.raises(HMCError) as error:
        await create(hmc, vsi_manager_id=0)
    assert error.value is original


@pytest.mark.asyncio
async def test_post_read_error_is_unavailable_without_automatic_delete(hmc):
    hmc._network_adapter_creation_feed.side_effect = [[], HMCError("read unavailable")]
    result = await create(hmc, qos_priority=0)
    assert result.resource == entry()
    assert result.creation_verification["status"] == "unavailable"
    hmc.add_network_adapter.assert_awaited_once()
    hmc.delete_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_lists_are_snapshotted_before_the_first_remote_await(hmc):
    requested = [2, 3]
    calls = 0

    async def inventory(_partition):
        nonlocal calls
        calls += 1
        if calls == 1:
            requested[:] = [9]
            return []
        return [entry()]

    hmc._network_adapter_creation_feed.side_effect = inventory
    result = await create(hmc, addl_vlan_ids=requested)
    assert hmc.add_network_adapter.await_args.kwargs["addl_vlan_ids"] == [2, 3]
    assert result.creation_verification["status"] == "verified"


@pytest.mark.asyncio
async def test_mac_address_lists_compare_hex_case_without_mutating_caller_values(hmc):
    requested = ["0200000000ab"]
    hmc._network_adapter_creation_feed.side_effect = [
        [],
        [
            entry(
                values={
                    **STORED,
                    "AllowedOperatingSystemMACAddresses": "0200000000AB",
                }
            )
        ],
    ]
    result = await create(hmc, allowed_os_mac_addrs=requested)
    assert result.creation_verification["status"] == "verified"
    assert requested == ["0200000000ab"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, stored, expected",
    [
        ("addl_vlan_ids", {"TaggedVLANIDs": "3 2"}, "mismatched"),
        ("addl_vlan_ids", {"TaggedVLANIDs": "2,3"}, "unavailable"),
        (
            "allowed_os_mac_addrs",
            {"AllowedOperatingSystemMACAddresses": "NONE"},
            "mismatched",
        ),
        (
            "allowed_os_mac_addrs",
            {"AllowedOperatingSystemMACAddresses": "broken"},
            "unavailable",
        ),
        ("qos_priority", {"QualityOfServicePriorityEnabled": "false"}, "mismatched"),
        ("qos_priority", {"QualityOfServicePriorityEnabled": None}, "unavailable"),
        ("qos_priority", {"QualityOfServicePriority": "8"}, "unavailable"),
        ("vsi_type_id", {"VirtualStationInterfaceTypeID": "9" * 5000}, "unavailable"),
    ],
)
async def test_observed_policy_values_are_not_guessed_or_coerced(
    hmc, field, stored, expected
):
    hmc._network_adapter_creation_feed.side_effect = [
        [],
        [entry(values={**STORED, **stored})],
    ]
    result = await create(hmc, **{field: FIELDS[field]})
    assert result.creation_verification["status"] == expected


@pytest.mark.asyncio
async def test_missing_resource_body_cannot_verify_selected_fields(hmc):
    item = entry()
    item["Resource"] = None
    hmc._network_adapter_creation_feed.side_effect = [[], [item]]
    result = await create(hmc, qos_priority=0)
    assert result.creation_verification["status"] == "unavailable"


@pytest.mark.asyncio
async def test_baseline_read_failure_propagates_before_submission(hmc):
    failure = HMCError("baseline unavailable")
    hmc._network_adapter_creation_feed.side_effect = failure
    with pytest.raises(HMCError) as error:
        await create(hmc, qos_priority=0)
    assert error.value is failure
    hmc.add_network_adapter.assert_not_awaited()
