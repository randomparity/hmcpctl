"""Finite serial creation inputs and independent native profile observations."""

import csv
import io
from itertools import product
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter, validate_serial_input
from hmcpctl.ssh import lpar_serial, profiles


def profile_record(serial="14/server/0////0", **changes):
    values = {
        "name": "default_profile",
        "lpar_name": "new",
        "lpar_id": "7",
        "max_virtual_slots": "32",
        "virtual_serial_adapters": serial,
    }
    values.update(changes)
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow(
        f"{key}={value}" for key, value in values.items()
    )
    return output.getvalue()


@pytest.mark.parametrize(
    "hmc,native,name,slot",
    list(product([None, 0], [None, 7], [None, "peer"], [None, 3])),
)
def test_server_optional_components(hmc, native, name, slot):
    adapter = VirtualSerialAdapter(14, "server", False, hmc, native, name, slot)
    expected = [
        "14",
        "server",
        "" if hmc is None else "0",
        "" if native is None else "7",
        "" if name is None else "peer",
        "" if slot is None else "3",
        "0",
    ]
    assert adapter.native().split("/") == expected
    assert validate_serial_input((adapter,), 15) == "/".join(expected)


def test_client_selectors_and_explicit_zero_false():
    assert (
        VirtualSerialAdapter(
            3, "client", True, remote_lpar_id=0, remote_slot=0
        ).native()
        == "3/client//0//0/1"
    )
    assert (
        VirtualSerialAdapter(
            3, "client", False, remote_lpar_name="007", remote_slot=4
        ).native()
        == "3/client///007/4/0"
    )
    assert VirtualSerialAdapter(14, "server", False, 0).native() == "14/server/0////0"


@pytest.mark.parametrize(
    "changes",
    [
        {"local_slot": 0},
        {"local_slot": 1},
        {"local_slot": -1},
        {"local_slot": True},
        {"local_slot": "14"},
        {"local_slot": 14.0},
        {"role": "other"},
        {"role": []},
        {"is_required": 0},
        {"is_required": "false"},
        {"supports_hmc": False},
        {"supports_hmc": 1},
        {"supports_hmc": "0"},
        {"remote_slot": -1},
        {"remote_slot": True},
        {"remote_slot": "3"},
        {"remote_lpar_id": True},
        {"remote_lpar_id": -1},
        {"remote_lpar_id": "7"},
        {"remote_lpar_name": 7},
        {"remote_lpar_name": ""},
    ],
)
def test_strict_record_types(changes):
    values = {"local_slot": 14, "role": "server", "is_required": False}
    values.update(changes)
    with pytest.raises((ValueError, TypeError)):
        VirtualSerialAdapter(**values)
    with pytest.raises(ValidationError):
        TypeAdapter(VirtualSerialAdapter).validate_python(values)


@pytest.mark.parametrize(
    "delimiter", ["/", ",", "=", '"', "\\", "\n", "\r", "\x00", "\x1f", "\x7f"]
)
def test_names_cannot_change_native_record_structure(delimiter):
    with pytest.raises(ValueError):
        VirtualSerialAdapter(14, "server", False, remote_lpar_name="peer" + delimiter)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"remote_lpar_id": 7},
        {"remote_slot": 3},
        {"remote_lpar_id": 7, "remote_lpar_name": "peer", "remote_slot": 3},
        {"remote_lpar_id": 7, "remote_slot": 3, "supports_hmc": 0},
    ],
)
def test_client_requires_one_selector_slot_and_no_hmc(changes):
    with pytest.raises(ValueError):
        VirtualSerialAdapter(3, "client", False, **changes)


def test_decoded_schema_forbids_unknown_fields():
    adapter = TypeAdapter(tuple[VirtualSerialAdapter, ...])
    with pytest.raises(ValidationError):
        adapter.validate_json(
            '[{"local_slot":2,"role":"server","is_required":false,"extra":0}]'
        )
    decoded = adapter.validate_json(
        '[{"local_slot":2,"role":"server","is_required":false,"supports_hmc":0}]'
    )
    assert validate_serial_input(decoded, 3) == "2/server/0////0"


def test_collection_omission_and_slot_guards():
    assert validate_serial_input((), None) == ""
    record = VirtualSerialAdapter(14, "server", False, 0)
    assert validate_serial_input((record,), 15) == "14/server/0////0"
    for maximum in (None, False, 14, 0, -1, "15", 15.0):
        with pytest.raises((ValueError, TypeError)):
            validate_serial_input((record,), maximum)
    with pytest.raises(ValueError, match="unique"):
        validate_serial_input((record, record), 15)
    for records in ([record], (object(),), "", None):
        with pytest.raises(TypeError):
            validate_serial_input(records, 15)


def test_encoded_bound_is_not_a_hardware_numeric_cap():
    large = 10**5000
    record = VirtualSerialAdapter(
        2, "client", False, remote_lpar_id=large, remote_slot=3
    )
    assert validate_serial_input((record,), 4) == "2/client//1" + "0" * 5000 + "//3/0"
    with pytest.raises(ValueError):
        VirtualSerialAdapter(2, "server", False, remote_lpar_id=1 << (65536 * 4))
    suffix = len(b"2/server/////0")
    name = "a" * (65536 - suffix)
    record = VirtualSerialAdapter(2, "server", False, remote_lpar_name=name)
    assert len(validate_serial_input((record,), 4).encode()) == 65536
    for too_long in (name + "a", "é" * len(name)):
        with pytest.raises(ValueError):
            validate_serial_input(
                (VirtualSerialAdapter(2, "server", False, remote_lpar_name=too_long),),
                4,
            )
    with pytest.raises(ValueError):
        validate_serial_input((record, VirtualSerialAdapter(3, "server", False)), 4)


def test_implicit_defaults_are_observations_not_create_authority():
    raw = "0/server/1/any//any/1,1/server/1/any//any/1,14/server/0////0"
    parsed = lpar_serial.parse_serial_profile(
        profile_record(raw), "new", 7, "default_profile"
    )
    assert parsed.records[0] == ("0", "server", "1", "any", "", "any", "1")
    assert parsed.records[14] == ("14", "server", "0", "", "", "", "0")
    assert (parsed.name, parsed.partition_id, parsed.profile_name, parsed.maximum) == (
        "new",
        7,
        "default_profile",
        32,
    )
    assert parsed.other_virtual_configuration is False
    with pytest.raises(ValueError):
        VirtualSerialAdapter(2, "server", True, 1)
    with pytest.raises(ValueError):
        VirtualSerialAdapter(2, "server", True, remote_lpar_id="any")


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "other"},
        {"lpar_name": "other"},
        {"lpar_id": "8"},
        {"lpar_id": ""},
        {"lpar_id": "٧"},
        {"max_virtual_slots": "1"},
        {"max_virtual_slots": "14"},
        {"max_virtual_slots": "x"},
    ],
)
def test_profile_identity_and_maximum(changes):
    with pytest.raises(ValueError):
        lpar_serial.parse_serial_profile(
            profile_record(**changes), "new", 7, "default_profile"
        )


@pytest.mark.parametrize(
    "record",
    [
        "",
        "x",
        'name="default_profile"',
        '"unclosed',
        profile_record() + ",name=default_profile",
        profile_record().replace("lpar_id=7,", ""),
        profile_record().replace(",virtual_serial_adapters=14/server/0////0", ""),
        profile_record() + ",bad-key=x",
        profile_record(extra="line\nfeed"),
        profile_record() + "\n",
        profile_record(extra="x" * 1048576),
    ],
)
def test_malformed_profile_records(record):
    with pytest.raises(ValueError):
        lpar_serial.parse_serial_profile(record, "new", 7, "default_profile")


@pytest.mark.parametrize(
    "serial",
    [
        "14/server/0////0,14/server/0////0",
        "14/server/0///0",
        "14/server/0////0/extra",
        "32/server/0////0",
        "-1/server/0////0",
        "14/other/0////0",
        "14/server/2////0",
        "14/server/0/-1///0",
        "14/server/0///-1/0",
        "14/server/0////2",
        "0/server/0////0",
        "1/client//7//3/0",
    ],
)
def test_malformed_observed_adapters(serial):
    with pytest.raises(ValueError):
        lpar_serial.parse_serial_profile(
            profile_record(serial), "new", 7, "default_profile"
        )


@pytest.mark.parametrize(
    "family",
    [
        "virtual_eth_adapters",
        "virtual_fc_adapters",
        "virtual_scsi_adapters",
        "vnic_adapters",
    ],
)
def test_other_families_remain_distinct_observations(family):
    parsed = lpar_serial.parse_serial_profile(
        profile_record(**{family: "opaque"}), "new", 7, "default_profile"
    )
    assert parsed.other_virtual_configuration is True
    assert set(parsed.records) == {14}


@pytest.mark.parametrize("empty", ["", "none"])
def test_empty_observed_collection(empty):
    assert (
        lpar_serial.parse_serial_profile(
            profile_record(empty), "new", 7, "default_profile"
        ).records
        == {}
    )


@pytest.mark.asyncio
async def test_reader_uses_existing_profile_read_then_binds_identity(monkeypatch):
    reader = AsyncMock(return_value=profile_record())
    monkeypatch.setattr(lpar_serial, "read_lpar_profile_record", reader)
    config = HMCConfig.from_mapping({})
    result = await lpar_serial.read_serial_profile(
        config, "system", "new", 7, "default_profile"
    )
    reader.assert_awaited_once_with(
        config, "system", "new", "default_profile", max_output_bytes=1048576
    )
    assert result.records[14][1] == "server"
    reader.return_value = profile_record(lpar_name="other")
    with pytest.raises(ValueError):
        await lpar_serial.read_serial_profile(
            config, "system", "new", 7, "default_profile"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options", [{}, {"max_output_bytes": None}, {"max_output_bytes": 32}]
)
async def test_profile_reader_optional_collection_bound(monkeypatch, options):
    command = AsyncMock(return_value="name=default_profile\n")
    monkeypatch.setattr(profiles, "run_hmc_command", command)
    config = HMCConfig.from_mapping({})
    assert (
        await profiles.read_lpar_profile_record(
            config, "system", "new", "default_profile", **options
        )
        == "name=default_profile"
    )
    expected = {} if options.get("max_output_bytes") is None else options
    command.assert_awaited_once_with(
        config,
        "lssyscfg -r prof -m system --filter lpar_names=new,profile_names=default_profile",
        **expected,
    )
