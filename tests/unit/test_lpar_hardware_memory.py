"""Fixed create flags and contained, availability-only memory observations."""

import itertools
import json
import shlex
from pathlib import Path
from unittest.mock import AsyncMock
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest
from conftest import make_config
from defusedxml import ElementTree as DET
from pydantic import TypeAdapter, ValidationError

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.lpar_hardware_memory import (
    HardwareMemoryFlag,
    hardware_memory_pairs,
)
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.hardware_memory import (
    admit_hardware_memory,
    created_hardware_memory_step,
)
from hmcpctl.ssh.lpar import create_lpar_via_cli
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import ATOM_NS, parse_feed

SYSTEM = "00000001-abcd-4ef0-8abc-000000000001"
PARTITION = "00000002-abcd-4ef0-8abc-000000000002"
OTHER = "00000003-abcd-4ef0-8abc-000000000003"
ENCRYPTION = "MemoryEncryptionHardwareAccessEnabled"
EXPANSION = "MemoryExpansionHardwareAccessEnabled"
TUPLE_FIELDS = (
    "IsClassicHMCManagement",
    "IsHMCPowerVMManagementController",
    "IsPowerVMManagementController",
)
PRIVATE = "PRIVATE-MARKER-" * 1000
BAD_LEAVES = [
    None,
    "",
    "yes",
    True,
    "TRUE",
    ["true", "true"],
    {"child": "true"},
    {"text": "true", "child": "false"},
]


def xml_entry(kind, entry):
    if entry is None:
        return ""
    root = Element(f"{{{ATOM_NS}}}entry")
    SubElement(root, f"{{{ATOM_NS}}}id").text = entry.get("UUID", "")
    content = SubElement(root, f"{{{ATOM_NS}}}content")
    resource = SubElement(content, f"{{{UOM_NS}}}{kind}")

    def add(parent, name, value):
        if isinstance(value, list):
            for item in value:
                add(parent, name, item)
            return
        child = SubElement(parent, f"{{{UOM_NS}}}{name}")
        if isinstance(value, dict):
            for key, item in value.items():
                add(child, key, item)
        elif value is not None:
            child.text = str(value)

    for name, value in (entry.get("Resource") or {}).items():
        add(resource, name, value)
    return tostring(root, encoding="unicode")


def captured_fields():
    return json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures/live/hardware-memory-schema-fields.json"
        ).read_text()
    )


@pytest.fixture
def target(monkeypatch):
    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping({"host": "hmc.test", "user": "test"})
    entry = {
        "UUID": PARTITION,
        "Resource": {
            "PartitionName": "part-R1",
            "PartitionType": "AIX/Linux",
            "PartitionState": "not activated",
            "PartitionMemoryConfiguration": {ENCRYPTION: "true", EXPANSION: "false"},
        },
    }
    hmc.list_logical_partitions.return_value = [entry]
    hmc.list_vios.return_value = [entry]
    hmc.partition_entry = entry
    hmc.system_entry = {
        "UUID": SYSTEM,
        "Resource": {
            **dict(zip(TUPLE_FIELDS, ("true", "false", "false"), strict=True)),
            "AssociatedSystemCapabilities": {
                "HardwareMemoryEncryptionCapable": "true",
                "HardwareMemoryCompressionCapable": "true",
            },
        },
    }

    async def read(path, kind, *, uuid_path_arguments):
        assert path == f"/rest/api/uom/{kind}/{uuid_path_arguments['uuid']}"
        return xml_entry(
            kind, hmc.system_entry if kind == "ManagedSystem" else hmc.partition_entry
        )

    hmc._get.side_effect = read
    command = AsyncMock(return_value="native-output")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", command)
    return hmc, entry, command


@pytest.mark.parametrize(
    "encryption,expansion", tuple(itertools.product((None, False, True), repeat=2))
)
@pytest.mark.asyncio
async def test_fixed_native_record_preserves_omission_and_false(
    target, encryption, expansion
):
    hmc, _, command = target
    expected = tuple(
        (key, int(value))
        for key, value in (
            ("hardware_mem_encryption", encryption),
            ("hardware_mem_expansion", expansion),
        )
        if value is not None
    )
    assert hardware_memory_pairs(encryption, expansion) == expected
    assert (
        await create_lpar_via_cli(
            hmc.config,
            "sys R1",
            "part-R1",
            hardware_mem_encryption=encryption,
            hardware_mem_expansion=expansion,
        )
        == "native-output"
    )
    command.assert_awaited_once()
    words = shlex.split(command.call_args.args[1])
    record = dict(
        pair.split("=", 1) for pair in words[words.index("-i") + 1].split(",")
    )
    assert {
        key: value for key, value in record.items() if key.startswith("hardware_")
    } == {key: str(value) for key, value in expected}
    assert record["profile_name"] == "default_profile"
    assert words[words.index("-m") + 1] == "sys R1"


@pytest.mark.parametrize(
    "value", [0, 1, "true", "false", "1", "0", "", PRIVATE, {}, [], object()]
)
def test_strict_private_scalar(value):
    adapter = TypeAdapter(HardwareMemoryFlag | None)
    with pytest.raises(ValidationError) as caught:
        adapter.validate_python(value)
    assert PRIVATE not in str(caught.value)
    assert all(detail["input"] is None for detail in caught.value.errors())
    for pair in ((value, None), (None, value)):
        with pytest.raises(TypeError) as direct:
            hardware_memory_pairs(*pair)
        assert PRIVATE not in str(direct.value)
    for valid in (None, False, True):
        assert adapter.validate_python(valid) is valid


@pytest.mark.asyncio
async def test_native_boundary_rejects_invalid_and_sanitizes_uncertainty(target):
    hmc, _, command = target
    for field in ("hardware_mem_encryption", "hardware_mem_expansion"):
        with pytest.raises(TypeError):
            await create_lpar_via_cli(
                hmc.config, "sys-R1", "part-R1", **{field: PRIVATE}
            )
    command.assert_not_awaited()
    command.side_effect = HMCCLIError(PRIVATE)
    with pytest.raises(HMCCLIError) as caught:
        await create_lpar_via_cli(
            hmc.config, "sys-R1", PRIVATE, hardware_mem_encryption=False
        )
    command.assert_awaited_once()
    assert PRIVATE not in str(caught.value)
    assert "unknown" in str(caught.value).lower()
    assert "inspect" in str(caught.value).lower()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "field", ["HardwareMemoryEncryptionCapable", "HardwareMemoryCompressionCapable"]
)
@pytest.mark.parametrize("value", ["false", "0", *BAD_LEAVES])
@pytest.mark.asyncio
async def test_unknown_or_negative_capability_refuses_even_false(target, field, value):
    hmc, _, command = target
    hmc.system_entry["Resource"]["AssociatedSystemCapabilities"][field] = value
    flags = (
        (False, None) if field == "HardwareMemoryEncryptionCapable" else (None, False)
    )
    with pytest.raises(ValueError):
        await admit_hardware_memory(hmc, SYSTEM, *flags)
    hmc._get.assert_awaited_once_with(
        f"/rest/api/uom/ManagedSystem/{SYSTEM}",
        "ManagedSystem",
        uuid_path_arguments={"uuid": SYSTEM},
    )
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_omission_does_not_read_capability_or_partition(target):
    hmc, _, command = target
    await admit_hardware_memory(hmc, SYSTEM, None, None)
    assert (
        await created_hardware_memory_step(
            hmc, SYSTEM, None, "part-R1", "AIX/Linux", None, None
        )
        is None
    )
    assert not hmc.mock_calls
    command.assert_not_awaited()


@pytest.mark.parametrize("values", tuple(itertools.product((False, True), repeat=3)))
@pytest.mark.parametrize("numeric", [False, True])
@pytest.mark.asyncio
async def test_hardware_native_tuple_admission(target, values, numeric):
    hmc, _, _ = target
    resource = hmc.system_entry["Resource"]
    resource.update(
        {
            key: str(int(value)) if numeric else str(value).lower()
            for key, value in zip(TUPLE_FIELDS, values, strict=True)
        }
    )
    if numeric:
        resource["AssociatedSystemCapabilities"] = {
            "HardwareMemoryEncryptionCapable": "1",
            "HardwareMemoryCompressionCapable": "1",
        }
    if values == (True, False, False):
        await admit_hardware_memory(hmc, SYSTEM, False, False)
    else:
        with pytest.raises(ValueError):
            await admit_hardware_memory(hmc, SYSTEM, False, False)
    hmc._get.assert_awaited_once_with(
        f"/rest/api/uom/ManagedSystem/{SYSTEM}",
        "ManagedSystem",
        uuid_path_arguments={"uuid": SYSTEM},
    )


@pytest.mark.parametrize("field", TUPLE_FIELDS)
@pytest.mark.parametrize("value", BAD_LEAVES)
@pytest.mark.asyncio
async def test_hardware_native_tuple_malformed_is_unavailable(target, field, value):
    hmc, _, _ = target
    hmc.system_entry["Resource"][field] = value
    with pytest.raises(ValueError):
        await admit_hardware_memory(hmc, SYSTEM, True, True)


@pytest.mark.asyncio
async def test_admission_reads_only_requested_capability_and_bounds_failures(target):
    hmc, _, _ = target
    resource = hmc.system_entry["Resource"]
    resource["AssociatedSystemCapabilities"]["HardwareMemoryCompressionCapable"] = (
        PRIVATE
    )
    await admit_hardware_memory(hmc, SYSTEM, False, None)
    hmc.reset_mock()
    with pytest.raises(TypeError):
        await admit_hardware_memory(hmc, SYSTEM, "true", None)
    hmc._get.assert_not_awaited()
    for entry in (None, {"UUID": SYSTEM, "Resource": {}}):
        hmc.system_entry = entry
        with pytest.raises(ValueError):
            await admit_hardware_memory(hmc, SYSTEM, True, None)
    hmc._get.side_effect = HMCError(PRIVATE)
    with pytest.raises(ValueError) as caught:
        await admit_hardware_memory(hmc, SYSTEM, True, None)
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("encoding", [("true", "false"), ("1", "0")])
@pytest.mark.asyncio
async def test_real_parser_uses_captured_capability_and_memory_shapes(target, encoding):
    hmc, _, _ = target
    fixture = captured_fields()
    hmc._get.side_effect = None
    hmc._get.return_value = fixture["system_body"]
    await admit_hardware_memory(hmc, SYSTEM, True, True)
    body = (
        fixture["partition_body"]
        .replace(
            ">true</MemoryEncryptionHardwareAccessEnabled>",
            f">{encoding[0]}</MemoryEncryptionHardwareAccessEnabled>",
        )
        .replace(
            ">true</MemoryExpansionHardwareAccessEnabled>",
            f">{encoding[1]}</MemoryExpansionHardwareAccessEnabled>",
        )
    )
    entry = parse_feed(body)[0]
    hmc.list_logical_partitions.return_value = [entry]
    hmc._get.return_value = body
    step = await created_hardware_memory_step(
        hmc, SYSTEM, entry, "part-R1", "AIX/Linux", True, False
    )
    assert step.status == "ok"
    assert step.result == {
        "status": "verified",
        "requested": {
            "hardware_mem_encryption": True,
            "hardware_mem_expansion": False,
        },
        "observed": {ENCRYPTION: True, EXPANSION: False},
    }


@pytest.mark.parametrize(
    "environment,vios",
    [("AIX/Linux", False), ("OS400", False), ("Virtual IO Server", True)],
)
@pytest.mark.asyncio
async def test_contained_inactive_requested_only_observation(target, environment, vios):
    hmc, entry, _ = target
    entry["Resource"]["PartitionType"] = environment
    entry["Resource"]["PartitionMemoryConfiguration"][EXPANSION] = {"text": "true"}
    step = await created_hardware_memory_step(
        hmc, SYSTEM, entry, "part-R1", environment, True, None, vios=vios
    )
    assert step.status == "ok"
    assert step.result == {
        "status": "verified",
        "requested": {"hardware_mem_encryption": True},
        "observed": {ENCRYPTION: True},
    }
    if vios:
        hmc.list_vios.assert_awaited_once_with(SYSTEM)
        kind = "VirtualIOServer"
    else:
        hmc.list_logical_partitions.assert_awaited_once_with(SYSTEM)
        kind = "LogicalPartition"
    hmc._get.assert_awaited_once_with(
        f"/rest/api/uom/{kind}/{PARTITION}",
        kind,
        uuid_path_arguments={"uuid": PARTITION},
    )


@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "foreign",
        "uuid",
        "name",
        "environment",
        "running",
        "starting",
        "missing",
        "alias",
        "repeated",
        "structured",
        "memory",
        "transport",
        "none",
        "input-uuid",
        "input-name",
        "mismatch",
    ],
)
@pytest.mark.asyncio
async def test_failed_observation_is_bounded_and_never_verified(target, vios, failure):
    from copy import deepcopy

    hmc, entry, _ = target
    environment = "Virtual IO Server" if vios else "AIX/Linux"
    entry["Resource"]["PartitionType"] = environment
    initial = deepcopy(entry)
    resource = entry["Resource"]
    memory = resource["PartitionMemoryConfiguration"]
    if failure == "foreign":
        hmc.list_logical_partitions.return_value = []
        hmc.list_vios.return_value = []
    elif failure == "uuid":
        entry["UUID"] = OTHER
        hmc.list_logical_partitions.return_value = [initial]
        hmc.list_vios.return_value = [initial]
    elif failure == "name":
        resource["PartitionName"] = PRIVATE
    elif failure == "environment":
        resource["PartitionType"] = "OS400"
    elif failure in ("running", "starting"):
        resource["PartitionState"] = failure
    elif failure == "missing":
        del memory[EXPANSION]
    elif failure == "alias":
        memory["MemoryCompressionHardwareAccessEnabled"] = memory.pop(EXPANSION)
    elif failure == "repeated":
        memory[EXPANSION] = ["false", "false"]
    elif failure == "structured":
        memory[EXPANSION] = {"text": "false", "child": "true"}
    elif failure == "memory":
        resource["PartitionMemoryConfiguration"] = []
    elif failure == "transport":
        hmc._get.side_effect = HMCError(PRIVATE)
    elif failure == "none":
        hmc.partition_entry = None
    elif failure == "input-uuid":
        initial["UUID"] = PRIVATE
    elif failure == "input-name":
        initial["Resource"]["PartitionName"] = PRIVATE
    elif failure == "mismatch":
        memory[EXPANSION] = "true"
    step = await created_hardware_memory_step(
        hmc, SYSTEM, initial, "part-R1", environment, True, False, vios=vios
    )
    assert step.status == "error"
    assert step.result["status"] == (
        "mismatch" if failure == "mismatch" else "readback-unavailable"
    )
    assert step.result["requested"] == {
        "hardware_mem_encryption": True,
        "hardware_mem_expansion": False,
    }
    assert set(step.result["observed"]) == {ENCRYPTION, EXPANSION}
    assert all(
        value is None or type(value) is bool
        for value in step.result["observed"].values()
    )
    if failure in ("missing", "alias", "repeated", "structured"):
        assert step.result["observed"] == {ENCRYPTION: True, EXPANSION: None}
    assert "inspect" in step.result["message"].lower()
    assert PRIVATE not in repr(step)
    assert len(repr(step)) < 800
    if failure == "foreign":
        hmc._get.assert_not_awaited()


@pytest.mark.parametrize(
    "encryption,expansion", tuple(itertools.product((None, False, True), repeat=2))
)
@pytest.mark.asyncio
async def test_requested_readbacks_preserve_all_flag_combinations(
    target, encryption, expansion
):
    hmc, entry, _ = target
    memory = entry["Resource"]["PartitionMemoryConfiguration"]
    memory[ENCRYPTION] = str(encryption).lower() if encryption is not None else PRIVATE
    memory[EXPANSION] = str(expansion).lower() if expansion is not None else PRIVATE
    step = await created_hardware_memory_step(
        hmc, SYSTEM, entry, "part-R1", "AIX/Linux", encryption, expansion
    )
    if encryption is None and expansion is None:
        assert step is None
        assert not hmc.mock_calls
    else:
        assert step.status == "ok"
        assert step.result["status"] == "verified"
        assert step.result["observed"] == {
            rest: value
            for rest, value in ((ENCRYPTION, encryption), (EXPANSION, expansion))
            if value is not None
        }


@pytest.mark.parametrize("capabilities", [None, [], {}])
@pytest.mark.asyncio
async def test_missing_capability_container_refuses(target, capabilities):
    hmc, _, _ = target
    hmc.system_entry["Resource"]["AssociatedSystemCapabilities"] = capabilities
    with pytest.raises(ValueError, match="capabil"):
        await admit_hardware_memory(hmc, SYSTEM, False, None)


def malformed_detail(body, kind, mutation):
    if mutation == "empty":
        return ""
    if mutation == "malformed":
        return "<entry>" + PRIVATE
    if mutation == "entity":
        return (
            '<!DOCTYPE entry [<!ENTITY secret "private-value">]><entry>&secret;</entry>'
        )
    root = DET.fromstring(body)
    identity = root.find(f"{{{ATOM_NS}}}id")
    content = root.find(f"{{{ATOM_NS}}}content")
    resource = content[0]
    container_name, leaf_name = (
        ("AssociatedSystemCapabilities", "HardwareMemoryEncryptionCapable")
        if kind == "ManagedSystem"
        else ("PartitionMemoryConfiguration", ENCRYPTION)
    )
    container = resource.find(f"{{{UOM_NS}}}{container_name}")
    leaf = container.find(f"{{{UOM_NS}}}{leaf_name}")
    if mutation in ("child-text", "attributed-child-text", "mixed-content"):
        leaf.clear()
        if mutation == "attributed-child-text":
            leaf.set("ksv", "V1_0")
        if mutation == "mixed-content":
            leaf.text = "true"
        SubElement(leaf, f"{{{UOM_NS}}}text").text = "true"
    elif mutation == "duplicate-leaf":
        container.append(DET.fromstring(tostring(leaf)))
    elif mutation == "duplicate-container":
        resource.append(DET.fromstring(tostring(container)))
    elif mutation == "foreign-leaf":
        leaf.tag = "{urn:wrong}" + leaf_name
    elif mutation == "foreign-leaf-duplicate":
        duplicate = DET.fromstring(tostring(leaf))
        duplicate.tag = "{urn:wrong}" + leaf_name
        container.append(duplicate)
    elif mutation == "foreign-container":
        container.tag = "{urn:wrong}" + container_name
    elif mutation == "duplicate-id":
        root.append(DET.fromstring(tostring(identity)))
    elif mutation == "foreign-id-duplicate":
        SubElement(root, "{urn:wrong}id").text = identity.text
    elif mutation == "missing-id":
        root.remove(identity)
    elif mutation == "nested-id":
        SubElement(identity, "text").text = identity.text
    elif mutation == "wrong-id":
        identity.text = OTHER
    elif mutation == "invalid-id":
        identity.text = PRIVATE
    elif mutation == "duplicate-content":
        root.append(DET.fromstring(tostring(content)))
    elif mutation == "missing-content":
        root.remove(content)
    elif mutation == "foreign-content":
        content.tag = "{urn:wrong}content"
    elif mutation == "duplicate-resource":
        content.append(DET.fromstring(tostring(resource)))
    elif mutation == "wrong-kind":
        resource.tag = f"{{{UOM_NS}}}VirtualSwitch"
    elif mutation == "wrong-resource-namespace":
        resource.tag = "{urn:wrong}" + kind
    elif mutation == "wrong-entry-namespace":
        root.tag = "{urn:wrong}entry"
    elif mutation in ("multi-entry", "foreign-entry-duplicate"):
        feed = Element(f"{{{ATOM_NS}}}feed")
        feed.append(root)
        duplicate = DET.fromstring(tostring(root))
        if mutation == "foreign-entry-duplicate":
            duplicate.tag = "{urn:wrong}entry"
        feed.append(duplicate)
        root = feed
    elif mutation == "bare-resource":
        root = resource
    return tostring(root, encoding="unicode")


@pytest.mark.parametrize(
    "kind", ["ManagedSystem", "LogicalPartition", "VirtualIOServer"]
)
@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "malformed",
        "entity",
        "child-text",
        "attributed-child-text",
        "mixed-content",
        "duplicate-leaf",
        "duplicate-container",
        "foreign-leaf",
        "foreign-leaf-duplicate",
        "foreign-container",
        "duplicate-id",
        "foreign-id-duplicate",
        "missing-id",
        "nested-id",
        "wrong-id",
        "invalid-id",
        "duplicate-content",
        "missing-content",
        "foreign-content",
        "duplicate-resource",
        "wrong-kind",
        "wrong-resource-namespace",
        "wrong-entry-namespace",
        "multi-entry",
        "foreign-entry-duplicate",
        "bare-resource",
    ],
)
@pytest.mark.asyncio
async def test_raw_structure_refuses_ambiguous_evidence(target, kind, mutation):
    hmc, entry, _ = target
    vios = kind == "VirtualIOServer"
    environment = "Virtual IO Server" if vios else "AIX/Linux"
    entry["Resource"]["PartitionType"] = environment
    fixture = captured_fields()
    body = fixture["system_body" if kind == "ManagedSystem" else "partition_body"]
    body = body.replace("LogicalPartition", kind).replace("AIX/Linux", environment)
    hmc._get.side_effect = None
    hmc._get.return_value = malformed_detail(body, kind, mutation)
    if kind == "ManagedSystem":
        with pytest.raises(ValueError) as caught:
            await admit_hardware_memory(hmc, SYSTEM, True, None)
        assert PRIVATE not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
    else:
        step = await created_hardware_memory_step(
            hmc, SYSTEM, entry, "part-R1", environment, True, None, vios=vios
        )
        assert step.status == "error"
        assert step.result["status"] == "readback-unavailable"
        assert step.result["observed"] == {ENCRYPTION: None}
        assert PRIVATE not in repr(step)
    hmc._get.assert_awaited_once()


@pytest.mark.parametrize("schema", [None, "V1_0"])
@pytest.mark.parametrize("vios", [False, True])
@pytest.mark.asyncio
async def test_actual_transport_preserves_headers_containment_and_counts(
    mock_hmc, schema, vios
):
    fixture = captured_fields()
    kind = "VirtualIOServer" if vios else "LogicalPartition"
    environment = "Virtual IO Server" if vios else "AIX/Linux"
    body = (
        fixture["partition_body"]
        .replace("LogicalPartition", kind)
        .replace("AIX/Linux", environment)
    )
    entry = parse_feed(body)[0]
    system = mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM}").respond(
        200, text=fixture["system_body"]
    )
    containment = mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM}/{kind}").respond(
        200, text=body
    )
    detail = mock_hmc.get(f"/rest/api/uom/{kind}/{PARTITION}").respond(200, text=body)
    config = make_config(port=443, schema_version=schema or "")
    async with HMCClient(config) as hmc:
        await admit_hardware_memory(hmc, SYSTEM, None, None)
        assert (
            await created_hardware_memory_step(
                hmc, SYSTEM, None, "part-R1", environment, None, None, vios=vios
            )
            is None
        )
        assert not [call for call in mock_hmc.calls if call.request.method == "GET"]
        await admit_hardware_memory(hmc, SYSTEM, True, True)
        step = await created_hardware_memory_step(
            hmc, SYSTEM, entry, "part-R1", environment, True, True, vios=vios
        )
    assert step.status == "ok"
    assert len([call for call in mock_hmc.calls if call.request.method == "GET"]) == 3
    for route, resource_kind in (
        (system, "ManagedSystem"),
        (containment, kind),
        (detail, kind),
    ):
        assert route.call_count == 1
        headers = route.calls.last.request.headers
        assert (
            headers["Accept"]
            == f"application/vnd.ibm.powervm.uom+xml; type={resource_kind}"
        )
        assert headers.get("X-HMC-Schema-Version") == schema


@pytest.mark.parametrize(
    "invalid",
    ["name-R1", "../escape", "", PRIVATE],
    ids=["name", "traversal", "empty", "private"],
)
@pytest.mark.asyncio
async def test_actual_transport_rejects_invalid_uuid_before_io(mock_hmc, invalid):
    config = make_config(port=443, schema_version="")
    async with HMCClient(config) as hmc:
        with pytest.raises(ValueError) as caught:
            await admit_hardware_memory(hmc, invalid, True, None)
        assert PRIVATE not in str(caught.value)
        entry = {"UUID": PARTITION, "Resource": {"PartitionName": "part-R1"}}
        step = await created_hardware_memory_step(
            hmc, invalid, entry, "part-R1", "AIX/Linux", True, None
        )
        assert step.status == "error"
        entry["UUID"] = invalid
        step = await created_hardware_memory_step(
            hmc, SYSTEM, entry, "part-R1", "AIX/Linux", True, None
        )
        assert step.status == "error"
    assert not [call for call in mock_hmc.calls if call.request.method == "GET"]


@pytest.mark.parametrize("status", [204, 500])
@pytest.mark.asyncio
async def test_actual_transport_unavailable_detail_never_falls_back(mock_hmc, status):
    route = mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM}").respond(
        status, text=PRIVATE if status == 500 else ""
    )
    config = make_config(port=443, schema_version="V1_0")
    async with HMCClient(config) as hmc:
        with pytest.raises(ValueError) as caught:
            await admit_hardware_memory(hmc, SYSTEM, False, None)
    assert PRIVATE not in str(caught.value)
    assert caught.value.__context__ is None
    assert route.call_count == 1
    assert len([call for call in mock_hmc.calls if call.request.method == "GET"]) == 1


@pytest.mark.asyncio
async def test_single_entry_feed_urn_identity_and_attributed_scalar_are_valid(target):
    hmc, entry, _ = target
    fixture = captured_fields()
    hmc._get.side_effect = None
    hmc._get.return_value = (
        f'<feed xmlns="{ATOM_NS}">'
        + fixture["system_body"].replace(SYSTEM, "urn:uuid:" + SYSTEM.upper())
        + "</feed>"
    )
    await admit_hardware_memory(hmc, SYSTEM, True, True)
    hmc._get.return_value = (
        f'<feed xmlns="{ATOM_NS}">'
        + fixture["partition_body"].replace(PARTITION, "urn:uuid:" + PARTITION.upper())
        + "</feed>"
    )
    step = await created_hardware_memory_step(
        hmc, SYSTEM, entry, "part-R1", "AIX/Linux", True, True
    )
    assert step.status == "ok"
