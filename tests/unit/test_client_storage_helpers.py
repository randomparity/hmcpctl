"""Contracts for storage-client response-shape normalization helpers."""

import xml.etree.ElementTree as ET

import pytest

from hmcpctl.client.client_storage import (
    StorageMixin,
    _extract_optical_media,
    _filter_optical_mappings,
)


def test_extract_optical_media_reads_only_the_documented_nesting():
    """V10R3 nests media as MediaRepositories/VirtualMediaRepository/OpticalMedia (#1202).

    No capture or reference shows a repository directly under the resource or media
    directly under the repository, so those shapes yield nothing.
    """
    entries = [
        {
            "Resource": {
                "MediaRepositories": {
                    "VirtualMediaRepository": {
                        "OpticalMedia": {
                            "VirtualOpticalMedia": [{"MediaName": "a.iso"}]
                        }
                    }
                }
            }
        },
        {
            "Resource": {
                "VirtualMediaRepository": {
                    "VirtualOpticalMedia": {"MediaName": "b.iso"}
                }
            }
        },
    ]

    assert _extract_optical_media(entries) == [{"MediaName": "a.iso"}]


def test_filter_optical_mappings_keeps_only_requested_lpar():
    mappings = [
        {
            "Storage": {"VirtualOpticalMedia": {}},
            "AssociatedLogicalPartition": {
                "href": "/rest/api/uom/LogicalPartition/lpar-1"
            },
        },
        {"Storage": {"VirtualDisk": {}}},
        {
            "Storage": {"VirtualOpticalMedia": {}},
            "AssociatedLogicalPartition": {
                "href": "/rest/api/uom/LogicalPartition/lpar-2"
            },
        },
    ]

    assert _filter_optical_mappings(mappings, "lpar-1") == [mappings[0]]


@pytest.mark.parametrize(
    "namespace",
    [
        "",
        "{urn:foreign}",
        "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}",
    ],
)
@pytest.mark.parametrize(
    ("names", "position"),
    [
        (
            [
                "Metadata",
                "MaximumLogicalVolumes",
                "Other",
                "PhysicalVolumes",
                "PhysicalVolumes",
            ],
            3,
        ),
        (["Metadata", "MaximumLogicalVolumes", "MaximumLogicalVolumes", "Tail"], 2),
        (["Metadata", "Tail"], 2),
        ([], 0),
    ],
)
def test_media_repositories_insertion_preserves_first_anchor_and_fallback(
    namespace, names, position
):
    group = ET.Element("VolumeGroup")
    original = [ET.SubElement(group, namespace + name) for name in names]
    repositories = ET.Element("MediaRepositories")

    StorageMixin()._insert_mr_at_correct_position(group, repositories)

    assert list(group) == original[:position] + [repositories] + original[position:]
