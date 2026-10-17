"""Tool-layer tests for media repository and optical media tools."""

from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import volume_group_with_repository

from hmcpctl.client.core import HMCClient

VIOS_UUID = "00000000-0000-0000-0000-000000000003"
VG_UUID = "22222222-2222-2222-2222-222222220001"


def _hmc_env(monkeypatch) -> None:
    """Set env vars so HMCConfig() succeeds inside the tool."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def _feed(uuid: str, rtype: str, **fields: str) -> str:
    """A single-resource Atom feed; {fields} render as resource elements."""
    body = "\n".join(
        f'        <{k} xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">{v}</{k}>'
        for k, v in fields.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{uuid}</id>
    <title>{rtype}:{uuid}</title>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <{rtype} xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
{body}
      </{rtype}>
    </content>
  </entry>
</feed>
"""


VG_FEED_WITH_REPO = volume_group_with_repository()


def test_get_media_repository(monkeypatch, mock_hmc):
    """hmc_get_media_repository GETs the VolumeGroup and returns the repository."""
    _hmc_env(monkeypatch)

    route = mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    ).mock(return_value=httpx.Response(200, text=VG_FEED_WITH_REPO))

    from hmcpctl.server_tools.storage.resources import hmc_get_media_repository

    result = hmc_get_media_repository(VIOS_UUID, VG_UUID)

    assert route.called
    assert result is not None
    assert result["UUID"] == "00000050-abcd-4ef0-8abc-000000000050"


def test_list_optical_media(monkeypatch, mock_hmc):
    """hmc_list_optical_media GETs the VolumeGroup and extracts optical media."""
    _hmc_env(monkeypatch)

    route = mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    ).mock(return_value=httpx.Response(200, text=VG_FEED_WITH_REPO))

    from hmcpctl.server_tools.storage.resources import hmc_list_optical_media

    media_list = hmc_list_optical_media(VIOS_UUID, VG_UUID)

    assert route.called
    assert len(media_list) == 2
    assert media_list[0]["name"] == "media-1"
    assert media_list[1]["name"] == "media-2"
    # The HMC's Size is GiB; the tool reports it in MiB (#963).
    assert media_list[0]["size_mib"] == 928.0512
    assert media_list[1]["size_mib"] == 987.9552
    # No captured or documented medium carries MediaType, so none is reported (#1202).
    assert set(media_list[0]) == {"name", "size_mib"}


def test_get_media_repository_not_found(monkeypatch, mock_hmc):
    """hmc_get_media_repository returns None when VG not found."""
    _hmc_env(monkeypatch)

    route = mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    ).mock(return_value=httpx.Response(404, text=""))

    from hmcpctl.server_tools.storage.resources import hmc_get_media_repository

    result = hmc_get_media_repository(VIOS_UUID, VG_UUID)

    assert route.called
    assert result is None


def test_list_optical_media_empty(monkeypatch, mock_hmc):
    """hmc_list_optical_media returns empty list when no media."""
    _hmc_env(monkeypatch)

    route = mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/VolumeGroup/{VG_UUID}"
    ).mock(
        return_value=httpx.Response(200, text=volume_group_with_repository(media=False))
    )

    from hmcpctl.server_tools.storage.resources import hmc_list_optical_media

    media_list = hmc_list_optical_media(VIOS_UUID, VG_UUID)

    assert route.called
    assert media_list == []


@pytest.fixture(autouse=True)
def scoped_volume_group_inventory(monkeypatch):
    """Supply membership at the HMC boundary for these media/guard tests."""
    lookup = AsyncMock(
        return_value=[{"UUID": VG_UUID, "Resource": {"GroupName": "rootvg"}}]
    )
    monkeypatch.setattr(HMCClient, "list_volume_groups", lookup)
    return lookup
