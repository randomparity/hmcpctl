"""Strict C07 creation inputs, retained sources and independently decoded native records."""

import csv
import hashlib
import json
import shlex
import traceback
from dataclasses import FrozenInstanceError, fields
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter
from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
from hmcpctl.ssh import lpar as native
from hmcpctl.ssh.transport import HMCCLIError

FIELDS = (
    "mem_mode",
    "desired_io_entitled_mem",
    "mem_weight",
    "primary_paging_vios_name",
    "primary_paging_vios_id",
    "secondary_paging_vios_name",
    "secondary_paging_vios_id",
    "mem_expansion",
)
SHARED = dict(
    zip(FIELDS[1:7], ("auto", 0, "primary", 1, "secondary", "none"), strict=True)
)


def memory(**values):
    from hmcpctl.documents.lpar_creation_memory import LparCreationMemory

    return LparCreationMemory(**values)


@pytest.mark.parametrize("decoded", [False, True])
@pytest.mark.parametrize(
    "changes",
    [
        {"mem_mode": "other"},
        {"mem_mode": []},
        {"mem_mode": False},
        *(
            {name: bad}
            for name in (
                "desired_io_entitled_mem",
                "primary_paging_vios_id",
                "secondary_paging_vios_id",
            )
            for bad in (0, -1, True, "1", 1.5)
        ),
        {"primary_paging_vios_id": "none"},
        *({"mem_weight": bad} for bad in (-1, 256, True, "0", 0.5)),
        *(
            {name: bad}
            for name in ("primary_paging_vios_name", "secondary_paging_vios_name")
            for bad in (
                "",
                3,
                b"peer",
                "peer,other",
                "peer=x",
                'peer"',
                "peer\n",
                "peer\x00",
                "peer\x7f",
            )
        ),
        *(
            {"mem_expansion": bad}
            for bad in (
                True,
                "1.1",
                -1,
                0.99,
                10.01,
                float("inf"),
                float("nan"),
                Decimal("NaN"),
                Decimal("-Infinity"),
                Decimal("1e999999999"),
                Decimal("1e-999999999"),
            )
        ),
    ],
)
def test_strict_domains(changes, decoded):
    values = {"mem_mode": "shared", **changes}
    with pytest.raises((ValueError, TypeError)):
        if decoded:
            TypeAdapter(type(memory())).validate_python(values)
        else:
            memory(**values)


@pytest.mark.parametrize("mode", [None, "ded"])
@pytest.mark.parametrize("field,value", SHARED.items())
def test_shared_only_requires_explicit_mode(mode, field, value):
    with pytest.raises(ValueError, match="shared"):
        memory(mem_mode=mode, **{field: value})


@pytest.mark.parametrize("role", ["primary", "secondary"])
def test_same_role_selectors_conflict(role):
    with pytest.raises(ValueError, match="name.*ID"):
        memory(
            mem_mode="shared",
            **{f"{role}_paging_vios_name": "peer", f"{role}_paging_vios_id": 1},
        )


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (-0.0, "0"),
        (1, "1"),
        (10, "10"),
        (1.1, "1.1"),
        (Decimal("1.10000000000000000000001"), "1.10000000000000000000001"),
        (Decimal("10.000"), "10"),
        (Decimal("-0e999999999"), "0"),
    ],
)
def test_expansion_exact_canonical_values(value, expected):
    with localcontext() as context:
        context.prec = 2
        request = memory(mem_expansion=value)
        assert request.mem_expansion == Decimal(expected)
        assert request.pairs() == [("mem_expansion", expected)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("mem_mode", "ded"),
        ("desired_io_entitled_mem", 128),
        ("mem_weight", 255),
        ("primary_paging_vios_name", "péér/path"),
        ("primary_paging_vios_id", 1),
        ("secondary_paging_vios_name", "secondary"),
        ("secondary_paging_vios_id", 2),
    ],
)
def test_positive_domain_values(field, value):
    request = memory(
        **(
            {field: value}
            if field == "mem_mode"
            else {"mem_mode": "shared", field: value}
        )
    )
    assert dict(request.pairs())[field] == value


def test_expansion_huge_integer_refused_before_rendering():
    with pytest.raises(ValueError, match="mem_expansion"):
        memory(mem_expansion=10**5000)


def test_schema_omission_sentinels_and_immutability():
    request = memory(
        mem_mode="shared",
        desired_io_entitled_mem="auto",
        mem_weight=0,
        primary_paging_vios_name="007",
        secondary_paging_vios_id="none",
        mem_expansion=0,
    )
    assert memory().pairs() == []
    assert {item.name for item in fields(request)} == set(FIELDS)
    assert request.pairs() == [
        ("mem_mode", "shared"),
        ("desired_io_entitled_mem", "auto"),
        ("mem_weight", 0),
        ("primary_paging_vios_name", "007"),
        ("secondary_paging_vios_id", "none"),
        ("mem_expansion", "0"),
    ]
    adapter = TypeAdapter(type(request))
    assert adapter.validate_json('{"mem_expansion":1.1}').mem_expansion == Decimal(
        "1.1"
    )
    schema = adapter.json_schema(mode="validation")
    assert schema["additionalProperties"] is False
    assert {
        item["type"] for item in schema["properties"]["mem_expansion"]["anyOf"]
    } == {"integer", "number", "null"}
    with pytest.raises(ValidationError):
        adapter.validate_python({"unexpected": 1})
    with pytest.raises(FrozenInstanceError):
        request.mem_weight = 1


def test_bounded_integer_name_and_complete_record():
    pairs = dict(memory(mem_mode="shared", primary_paging_vios_id=10**4095).pairs())
    assert len(str(pairs["primary_paging_vios_id"])) == 4096
    with pytest.raises(ValueError, match="4096"):
        memory(mem_mode="shared", primary_paging_vios_id=10**4096)
    for name in ("a" * 65537, "é" * 32769):
        with pytest.raises(ValueError, match="65536"):
            memory(mem_mode="shared", primary_paging_vios_name=name)
    overhead = len(b"mem_mode=shared,primary_paging_vios_name=")
    assert memory(
        mem_mode="shared", primary_paging_vios_name="a" * (65536 - overhead)
    ).pairs()
    with pytest.raises(ValueError, match="65536"):
        memory(mem_mode="shared", primary_paging_vios_name="a" * (65537 - overhead))
    with pytest.raises(ValueError, match="65536"):
        memory(mem_expansion=Decimal("1." + "0" * 65534 + "1"))
    assert memory(mem_expansion=Decimal("1." + "0" * 65535)).pairs() == [
        ("mem_expansion", "1")
    ]


@pytest.mark.parametrize("generation,line", [("p10", 41), ("p11", 49)])
@pytest.mark.parametrize("field", FIELDS)
def test_sixteen_source_bindings(generation, line, field):
    root = Path(__file__).resolve().parents[2]
    source = root / "docs/refs" / f"hmc-commands-{generation}" / "commands/mksyscfg.md"
    if not source.is_file():
        pytest.skip(f"Local {generation} corpus absent; source binding unevidenced")
    inventory = json.loads((root / "docs/capabilities/corpora.json").read_text())
    topic_id = f"commands-{generation}:commands/mksyscfg"
    topic = next(item for item in inventory["topics"] if item["id"] == topic_id)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == topic["sha256"].replace(
        "-", ""
    )
    unit = next(
        item
        for item in inventory["source_units"]
        if item["id"] == f"{topic_id}:L{line:05}:command-option"
    )
    assert unit["accounting"] == {"id": "cli:commands/mksyscfg", "kind": "row"}
    text = source.read_text().splitlines()[line - 1]
    assert text.startswith("| **-f** |")
    assert (
        text.index("**min_mem**")
        < text.index(f"**{field}**")
        < text.index("**proc_mode**")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("controller", [False, True])
async def test_one_fixed_record_composes_all_families(monkeypatch, controller):
    command = AsyncMock(return_value="created")
    monkeypatch.setattr(native, "run_hmc_command", command)
    request = memory(
        mem_mode="shared",
        desired_io_entitled_mem="auto",
        mem_weight=0,
        primary_paging_vios_name="peer'; $(ignored)",
        secondary_paging_vios_id="none",
        mem_expansion=0,
    )
    result = await native.create_lpar_via_cli(
        HMCConfig.from_mapping({}),
        "system; $(ignored)",
        "new",
        profile_name=None if controller else native.DEFAULT_PROFILE_NAME,
        max_virtual_slots=32,
        vtpm_settings=LparVtpmSettings(vtpm_enabled=False),
        security_policy=LparSecurityPolicy(secure_boot=0),
        physical_io=LparPhysicalIO(lpar_io_pool_ids=("1",)),
        memory_layout=LparMemoryLayout(hpt_ratio="1:128"),
        hardware_mem_encryption=False,
        hardware_mem_expansion=False,
        virtual_serial_adapters=(VirtualSerialAdapter(14, "server", False, 0),),
        creation_memory=request,
    )
    assert result == "created"
    command.assert_awaited_once()
    words = shlex.split(command.await_args.args[1])
    assert words[:5] == ["mksyscfg", "-r", "lpar", "-m", "system; $(ignored)"]
    assert len(words) == 7 and words[5] == "-i"
    pairs = [pair.split("=", 1) for pair in next(csv.reader([words[6]], strict=True))]
    actual = dict(pairs)
    assert len(actual) == len(pairs)
    assert {key: actual[key] for key in FIELDS if key in actual} == {
        "mem_mode": "shared",
        "desired_io_entitled_mem": "auto",
        "mem_weight": "0",
        "primary_paging_vios_name": "peer'; $(ignored)",
        "secondary_paging_vios_id": "none",
        "mem_expansion": "0",
    }
    assert {
        "hpt_ratio": "1:128",
        "hardware_mem_encryption": "0",
        "hardware_mem_expansion": "0",
        "secure_boot": "0",
        "vtpm_enabled": "0",
        "lpar_io_pool_ids": "1",
        "virtual_serial_adapters": "14/server/0////0",
    }.items() <= actual.items()
    assert ("profile_name" in actual) is not controller


@pytest.mark.asyncio
async def test_omission_preserves_native_defaults(monkeypatch):
    command = AsyncMock(return_value="")
    monkeypatch.setattr(native, "run_hmc_command", command)
    for request in (None, memory()):
        await native.create_lpar_via_cli(
            HMCConfig.from_mapping({}), "system", "new", creation_memory=request
        )
    records = [shlex.split(call.args[1])[-1] for call in command.await_args_list]
    assert (
        records[0]
        == records[1]
        == "name=new,lpar_env=aixlinux,profile_name=default_profile,all_resources=1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        HMCCLIError(
            "private-submission", 500, "private-body", remote_refusal_code="HSCL1234"
        ),
        TimeoutError("private-submission"),
        ValueError("private-submission"),
    ],
)
async def test_submission_error_sanitizes_only_transport(monkeypatch, error):
    command = AsyncMock(side_effect=error)
    monkeypatch.setattr(native, "run_hmc_command", command)
    with pytest.raises(HMCCLIError) as caught:
        await native.create_lpar_via_cli(
            HMCConfig.from_mapping({}),
            "system",
            "new",
            memory_layout=LparMemoryLayout(hpt_ratio="1:128"),
            creation_memory=memory(mem_expansion=0),
        )
    command.assert_awaited_once()
    assert type(caught.value).__name__ == "_CreationMemorySubmissionError"
    assert caught.value.remote_refusal_code == getattr(
        error, "remote_refusal_code", None
    )
    assert (
        caught.value.body is None
        and caught.value.__cause__ is None
        and caught.value.__context__ is None
    )
    assert "private-" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_bad_direct_request_and_record_fail_before_submission(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(native, "run_hmc_command", command)
    with pytest.raises(TypeError, match="creation_memory"):
        await native.create_lpar_via_cli(
            HMCConfig.from_mapping({}),
            "system",
            "new",
            creation_memory={"mem_mode": "shared"},
        )
    with pytest.raises(HMCCLIError) as caught:
        await native.create_lpar_via_cli(
            HMCConfig.from_mapping({}),
            "system",
            "invalid,name",
            creation_memory=memory(mem_expansion=0),
        )
    assert type(caught.value) is HMCCLIError
    command.assert_not_awaited()
