"""Strict running-client request policy, distinct from creation defaults (#1824)."""

import pytest

from hmcpctl.documents import adapters


def validate(**values):
    subject = getattr(adapters, "validate_network_adapter_update_fields", None)
    assert callable(subject), "running-client update validator is missing"
    return subject(**values)


@pytest.mark.parametrize(
    "values",
    [
        {"tagged": False},
        {"tagged": True},
        {"addl_vlan_ids": []},
        {"addl_vlan_ids": [1, 4094]},
        {"addl_vlan_ids": [1], "vlan_action": "add"},
        {"addl_vlan_ids": list(range(1, 22)), "vlan_action": "remove"},
        {"qos_priority": 0},
        {"qos_priority": 7},
        {"qos_priority": "none"},
        {"vsi_manager_id": -(2**31)},
        {"vsi_type_id": 0},
        {"vsi_type_version": 2**31 - 1},
        {"remove_vsi_profile": True},
    ],
)
def test_valid_explicit_values_are_not_omitted(values):
    assert validate(**values) is None


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"tagged": None},
        {"tagged": 0},
        {"tagged": "false"},
        {"tagged": []},
        {"addl_vlan_ids": "1,2"},
        {"addl_vlan_ids": (1, 2)},
        {"addl_vlan_ids": [True]},
        {"addl_vlan_ids": [None]},
        {"addl_vlan_ids": [0]},
        {"addl_vlan_ids": [4095]},
        {"addl_vlan_ids": [1.0]},
        {"addl_vlan_ids": [1, 1]},
        {"addl_vlan_ids": list(range(1, 22))},
        {"addl_vlan_ids": [], "vlan_action": "add"},
        {"addl_vlan_ids": [], "vlan_action": "remove"},
        {"vlan_action": "add", "qos_priority": 0},
        {"vlan_action": "remove", "qos_priority": 0},
        {"vlan_action": "merge", "addl_vlan_ids": [1]},
        {"vlan_action": None, "addl_vlan_ids": [1]},
        {"qos_priority": True},
        {"qos_priority": False},
        {"qos_priority": "0"},
        {"qos_priority": -1},
        {"qos_priority": 8},
        {"qos_priority": 1.0},
        {"vsi_manager_id": True},
        {"vsi_type_id": "none"},
        {"vsi_type_version": 2**31},
        {"vsi_manager_id": -(2**31) - 1},
        {"remove_vsi_profile": 1},
        {"remove_vsi_profile": "true"},
        {"remove_vsi_profile": True, "vsi_manager_id": 0},
        {"remove_vsi_profile": True, "vsi_type_id": 0},
        {"remove_vsi_profile": True, "vsi_type_version": 0},
    ],
)
def test_invalid_update_requests_refuse(values):
    with pytest.raises(ValueError):
        validate(**values)


def test_validation_does_not_mutate_the_callers_vlan_list():
    values = [4094, 1]
    validate(addl_vlan_ids=values)
    assert values == [4094, 1]
