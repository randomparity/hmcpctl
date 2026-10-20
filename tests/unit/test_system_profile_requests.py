"""Fixed managed-system system-profile request contracts."""

import xml.etree.ElementTree as ET

import pytest

from hmcpctl.jobs import requests


def parameters(document):
    root = ET.fromstring(document)
    return {
        p.find("{*}ParameterName").text: p.find("{*}ParameterValue").text
        for p in root.findall(".//{*}JobParameter")
    }


def test_reference_requests():
    assert parameters(
        requests.create_system_profile_job("a&b", ("one",), ("base",))
    ) == {
        "systemProfileName": "a&b",
        "logicalPartitionNames": "one",
        "logicalPartitionProfileNames": "base",
    }
    assert parameters(requests.update_system_profile_job("old", "new")) == {
        "systemProfileName": "old",
        "systemProfileNewName": "new",
    }
    assert parameters(requests.activate_system_profile_job("x", True)) == {
        "systemProfile": "x",
        "operation": "on",
        "test": "false",
        "continue": "true",
    }


@pytest.mark.parametrize(
    "partitions,profiles", [((), ()), (("a",), ()), (None, ("b",))]
)
def test_reference_requests_refuse_bad_pairs(partitions, profiles):
    with pytest.raises(ValueError):
        requests.update_system_profile_job(
            "x", partition_names=partitions, profile_names=profiles
        )


@pytest.mark.parametrize("operation", ["create", "update"])
def test_paired_tuple_names_round_trip_xml_metacharacters(operation):
    payload = '<member attr="x">&</member>'
    if operation == "create":
        document = requests.create_system_profile_job("scratch", (payload,), (payload,))
    else:
        document = requests.update_system_profile_job(
            "scratch",
            partition_names=(payload,),
            profile_names=(payload,),
        )
    parsed = parameters(document)
    assert (
        parsed["logicalPartitionNames"]
        == parsed["logicalPartitionProfileNames"]
        == payload
    )
    assert not ET.fromstring(document).findall(".//member")
