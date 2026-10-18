"""Tests for VIOS media repository and optical media inventory operations."""

import httpx
import pytest
from conftest import live_fixture, make_config, volume_group_with_repository

from hmcpctl.client import client_storage
from hmcpctl.client.core import HMCClient

VG_ENTRY_WITH_REPO = volume_group_with_repository()
VG_ENTRY_EMPTY_REPO = volume_group_with_repository(media=False)
# The captured clientvg1 entry: a volume group with no media repository.
VG_ENTRY_WITHOUT_REPO = live_fixture("rest-volume-group")["body"]


@pytest.mark.asyncio
async def test_get_media_repository(mock_hmc):
    """get_media_repository returns a VolumeGroup entry containing capacity and media."""
    route = mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222220001"
    ).mock(return_value=httpx.Response(200, text=VG_ENTRY_WITH_REPO))

    async with HMCClient(make_config()) as hmc:
        result = await hmc.get_media_repository(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222220001",
        )

    assert route.called
    assert result is not None
    repo = result["Resource"]["MediaRepositories"]["VirtualMediaRepository"]
    assert repo["RepositoryName"] == "VMLibrary"
    # RepositorySize is GiB (#963).
    assert repo["RepositorySize"] == "15"
    media = repo["OpticalMedia"]["VirtualOpticalMedia"]
    assert [item["MediaName"] for item in media] == ["media-1", "media-2"]


@pytest.mark.asyncio
async def test_get_media_repository_empty(mock_hmc):
    """get_media_repository handles a repository with no optical media."""
    route = mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222220002"
    ).mock(return_value=httpx.Response(200, text=VG_ENTRY_EMPTY_REPO))

    async with HMCClient(make_config()) as hmc:
        result = await hmc.get_media_repository(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222220002",
        )

    assert route.called
    assert result is not None
    repo = result["Resource"]["MediaRepositories"]["VirtualMediaRepository"]
    assert repo["RepositoryName"] == "VMLibrary"
    assert repo["RepositorySize"] == "15"
    assert "VirtualOpticalMedia" not in repo["OpticalMedia"]


@pytest.mark.asyncio
async def test_get_media_repository_not_found(mock_hmc):
    """get_media_repository returns None when repository doesn't exist."""
    route = mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/99999999-9999-9999-9999-999999999999"
    ).mock(return_value=httpx.Response(404, text=""))

    async with HMCClient(make_config()) as hmc:
        result = await hmc.get_media_repository(
            "11111111-1111-1111-1111-111111111111",
            "99999999-9999-9999-9999-999999999999",
        )

    assert route.called
    assert result is None


@pytest.mark.asyncio
async def test_get_media_repository_absent_from_existing_volume_group(mock_hmc):
    """An existing volume group without a repository is not a repository."""
    route = mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222220003"
    ).mock(return_value=httpx.Response(200, text=VG_ENTRY_WITHOUT_REPO))

    async with HMCClient(make_config()) as hmc:
        result = await hmc.get_media_repository(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222220003",
        )

    assert route.called
    assert result is None


@pytest.mark.asyncio
async def test_list_optical_media(mock_hmc):
    """list_optical_media extracts and returns optical media entries."""
    route = mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222220001"
    ).mock(return_value=httpx.Response(200, text=VG_ENTRY_WITH_REPO))

    async with HMCClient(make_config()) as hmc:
        media_list = await hmc.list_optical_media(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222220001",
        )

    assert route.called
    # The captured media carry MediaName, MediaUDID, MountType and Size (GiB);
    # no MediaType.
    assert [(m["MediaName"], m["Size"]) for m in media_list] == [
        ("media-1", "0.9063"),
        ("media-2", "0.9648"),
    ]
    assert all("MediaType" not in m for m in media_list)


@pytest.mark.asyncio
async def test_list_optical_media_empty(mock_hmc):
    """list_optical_media returns empty list when no media present."""
    route = mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222220002"
    ).mock(return_value=httpx.Response(200, text=VG_ENTRY_EMPTY_REPO))

    async with HMCClient(make_config()) as hmc:
        media_list = await hmc.list_optical_media(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222220002",
        )

    assert route.called
    assert media_list == []


@pytest.mark.asyncio
async def test_list_optical_media_discards_malformed_response_elements(
    mock_hmc, monkeypatch
):
    """Only mapping-shaped media entries cross the client boundary."""
    route = mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/22222222-2222-2222-2222-222222220001"
    ).mock(return_value=httpx.Response(200, text="<feed/>"))
    monkeypatch.setattr(
        client_storage,
        "_parse_feed",
        lambda _xml, _path: [
            {"Resource": "not-a-mapping"},
            {"Resource": {"MediaRepositories": []}},
            {
                "Resource": {
                    "MediaRepositories": {
                        "VirtualMediaRepository": {
                            "OpticalMedia": {
                                "VirtualOpticalMedia": [
                                    {"MediaName": "kept.iso"},
                                    "discarded",
                                    None,
                                ]
                            }
                        }
                    }
                }
            },
        ],
    )

    async with HMCClient(make_config()) as hmc:
        media_list = await hmc.list_optical_media(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222220001",
        )

    assert route.called
    assert media_list == [{"MediaName": "kept.iso"}]


@pytest.mark.asyncio
async def test_list_optical_media_not_found(mock_hmc):
    """list_optical_media returns empty list when VG doesn't exist."""
    route = mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup/99999999-9999-9999-9999-999999999999"
    ).mock(return_value=httpx.Response(404, text=""))

    async with HMCClient(make_config()) as hmc:
        media_list = await hmc.list_optical_media(
            "11111111-1111-1111-1111-111111111111",
            "99999999-9999-9999-9999-999999999999",
        )

    assert route.called
    assert media_list == []
