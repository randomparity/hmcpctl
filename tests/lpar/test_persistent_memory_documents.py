"""Persistent-memory volume parsing and request documents (#643, ADR 0373).

The fixtures under ``tests/fixtures/pmem/`` are tokenized HMC V10R3 responses
from the 2026-11-13 capture recorded in
``tests/fixtures/live/vocabulary/v10r3-8375-42a-pmem-source.json``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from defusedxml import ElementTree

from hmcpctl.documents.persistent_memory import (
    PersistentMemoryVolume,
    build_volume_document,
    parse_volume,
    validate_volume_name,
)
from hmcpctl.xmlutil import parse_feed

FIXTURES = Path(__file__).parents[1] / "fixtures" / "pmem"
NS = "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}"


def _entry(name: str = "volume-feed.xml") -> dict:
    return parse_feed((FIXTURES / name).read_text())[0]


def test_parse_volume_maps_captured_entry() -> None:
    assert parse_volume(_entry()) == PersistentMemoryVolume(
        uuid="00000004-ABCD-4EF0-8ABC-000000000004",
        name="example-vol",
        size_mib=256,
        current_size_mib=256,
        volume_id=1142931457,
        affinity=False,
        partition_id=1,
        partition_name="example-lpar",
        partition_uuid="00000005-ABCD-4EF0-8ABC-000000000005",
    )


def test_parse_volume_reads_create_response_entry() -> None:
    volume = parse_volume(_entry("volume-entry.xml"))
    assert volume.uuid == "00000051-ABCD-4EF0-8ABC-000000000051"
    assert volume.partition_id == 1


def test_parse_volume_leaves_unassigned_partition_fields_empty() -> None:
    entry = _entry()
    for field in (
        "AssociatedPartition",
        "AssociatedPartitionId",
        "AssociatedPartitionName",
    ):
        del entry["Resource"][field]
    volume = parse_volume(entry)
    assert (volume.partition_id, volume.partition_name, volume.partition_uuid) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize("field", ["Uuid", "Name", "Size"])
def test_parse_volume_rejects_missing_field(field: str) -> None:
    entry = _entry()
    del entry["Resource"][field]
    with pytest.raises(ValueError, match=field):
        parse_volume(entry)


@pytest.mark.parametrize(
    ("field", "value"),
    [("Size", "big"), ("Size", "0"), ("AssociatedPartitionId", "x"), ("Affinity", "1")],
)
def test_parse_volume_rejects_malformed_field(field: str, value: str) -> None:
    entry = _entry()
    entry["Resource"][field] = value
    with pytest.raises(ValueError, match=field):
        parse_volume(entry)


@pytest.mark.parametrize("name", ["example-vol", "a", "A_b.c-9", "x" * 64])
def test_validate_volume_name_accepts(name: str) -> None:
    assert validate_volume_name(name) == name


@pytest.mark.parametrize("name", ["", "x" * 65, "a b", "a,b", "a<b", "é"])
def test_validate_volume_name_rejects(name: str) -> None:
    with pytest.raises(ValueError, match="nothing was changed"):
        validate_volume_name(name)


def test_validate_volume_name_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        validate_volume_name(5)  # type: ignore[arg-type]


def test_build_volume_document_matches_capture() -> None:
    root = ElementTree.fromstring(
        build_volume_document("example-vol", 256, affinity=False, partition_id=1)
    )
    assert root.tag == f"{NS}VirtualPersistentMemoryVolume"
    children = [
        (child.tag.removeprefix(NS), child.text, child.get("ksv")) for child in root
    ]
    assert children == [
        ("Metadata", None, None),
        ("Name", "example-vol", "V1_10_0"),
        ("Size", "256", "V1_10_0"),
        ("Affinity", "false", "V1_10_0"),
        ("AssociatedPartitionId", "1", "V1_10_0"),
    ]


def test_build_volume_document_escapes_name() -> None:
    document = build_volume_document("a<b&c", 256, affinity=True, partition_id=7)
    root = ElementTree.fromstring(document)
    assert root.find(f"{NS}Name").text == "a<b&c"
    assert root.find(f"{NS}Affinity").text == "true"


def test_parse_volume_rejects_blank_field_and_missing_resource() -> None:
    entry = _entry()
    entry["Resource"]["Name"] = " "
    with pytest.raises(ValueError, match="Name is malformed"):
        parse_volume(entry)
    with pytest.raises(ValueError, match="has no Size"):
        parse_volume({"Resource": None})
