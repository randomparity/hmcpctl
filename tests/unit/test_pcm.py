"""Tests for Cluster/SSP job builders, PCM helpers, and metrics tools."""

from urllib.parse import urlsplit

import httpx
import pytest
from conftest import captured_lpar_entry, live_fixture, live_response, make_config
from defusedxml import ElementTree as ET

from hmcpctl.client.core import HMCClient
from hmcpctl.client.pcm_payloads import (
    metric_links,
    newest_metric_link,
    pcm_preferences_to_dict,
    pcm_preferences_update,
)
from hmcpctl.errors import HMCError
from hmcpctl.jobs import (
    DEVICE_TYPES,
    LU_TYPES,
    create_logical_unit_job,
    delete_logical_unit_job,
)
from hmcpctl.server_tools.metrics.pcm import (
    hmc_aggregated_metric_links,
    hmc_aggregated_metrics,
    hmc_get_pcm_preferences,
    hmc_processed_metric_links,
    hmc_processed_metrics,
    hmc_set_pcm_preferences,
)

PCM_PREFS_XML = """<?xml version="1.0"?>
<ManagementConsolePcmPreference xmlns="http://www.ibm.com/xmlns/systems/power/firmware/pcm/mc/2012_10/">
  <LongTermMonitorEnabled>true</LongTermMonitorEnabled>
  <AggregationEnabled>false</AggregationEnabled>
</ManagementConsolePcmPreference>
"""

PCM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>ManagedSystem ProcessedMetrics</title>
    <updated>2026-08-07T12:00:30Z</updated>
    <link rel="SELF" href="/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json" type="application/json"/>
  </entry>
</feed>
"""

# Dedicated aggregated-metrics feed with AggregatedMetrics hrefs so that
# aggregated fetch tests verify the correct URL path, not ProcessedMetrics ones.
AGGREGATED_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>LogicalPartition AggregatedMetrics</title>
    <updated>2026-08-07T12:00:30Z</updated>
    <link rel="SELF" href="/rest/api/pcm/AggregatedMetrics/LogicalPartition_lpar_2.json" type="application/json"/>
  </entry>
</feed>
"""


# V10R3 answers a PCM request whose Accept it cannot serve with an empty 406
# (`application/xml` on preferences), and a PCM read the account may not make
# with a 403 `HttpErrorResponse` (#1202).
def _not_acceptable() -> httpx.Response:
    capture = live_fixture("rest-pcm-preferences-406")  # no body, no content type
    return httpx.Response(capture["status"], text=capture["body"])


def _forbidden() -> httpx.Response:
    return live_response("rest-pcm-metrics-403")[1]


NOT_ACCEPTABLE = "(?i)refused the media type"

EMPTY_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
</feed>
"""

# Newest entry (_2.json, 12:00:30) listed FIRST — the HMC does not guarantee
# the feed is ordered by age, so selection must be by updated stamp, not row.
OUT_OF_ORDER_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>ManagedSystem ProcessedMetrics</title>
    <updated>2026-08-07T12:00:30Z</updated>
    <link rel="SELF" href="/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json" type="application/json"/>
  </entry>
  <entry>
    <title>ManagedSystem ProcessedMetrics</title>
    <updated>2026-08-07T12:00:00Z</updated>
    <link rel="SELF" href="/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_1.json" type="application/json"/>
  </entry>
</feed>
"""

METRICS_JSON = {
    "systemUtil": {"utilization": 0.5},
    "sampleTime": "2026-08-07T12:00:30Z",
}


def _hmc_env(monkeypatch):
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


@pytest.fixture
def scoped_lpar(mock_hmc):
    entry = captured_lpar_entry("00000000-0000-0000-0000-000000000002", "lpar1")
    mock_hmc.get(
        "/rest/api/uom/ManagedSystem/00000000-0000-0000-0000-000000000001/LogicalPartition"
    ).mock(
        return_value=httpx.Response(
            200, text=f'<feed xmlns="http://www.w3.org/2005/Atom">{entry}</feed>'
        )
    )


def _route_preferences_read(router):
    """The GET a preferences update reads before it posts (#634)."""
    router.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/preferences"
    ).mock(return_value=live_response("rest-pcm-preferences")[1])


def _route_metrics_feed(router, category, uuid, kind, text=PCM_FEED):
    router.get(f"/rest/api/pcm/{category}/{uuid}/{kind}").mock(
        return_value=httpx.Response(200, text=text)
    )


def test_create_logical_unit_job():
    xml = create_logical_unit_job("newLU", 18, "THIN", "VirtualIO_Disk")
    assert "CreateLogicalUnit" in xml
    assert "Cluster" in xml
    assert "<ParameterName" in xml and "LUName" in xml and "newLU" in xml
    assert "LUSize" in xml and "18" in xml
    assert "LUType" in xml and "THIN" in xml
    assert "DeviceType" in xml and "VirtualIO_Disk" in xml
    assert "ClonedFrom" not in xml  # omitted when not cloning


def test_create_logical_unit_job_clone():
    xml = create_logical_unit_job("cloneLU", 20, cloned_from="udid-src")
    assert "ClonedFrom" in xml and "udid-src" in xml


def test_all_logical_unit_types_serialize_unchanged():
    for lu_type in LU_TYPES:
        for device_type in DEVICE_TYPES:
            xml = create_logical_unit_job("newLU", 18, lu_type, device_type)
            assert f">{lu_type}</ParameterValue>" in xml
            assert f">{device_type}</ParameterValue>" in xml


@pytest.mark.parametrize(
    "lu_type,device_type,match",
    [
        ("SPARSE", "VirtualIO_Disk", "lu_type"),
        ("THIN", "PhysicalDisk", "device_type"),
    ],
)
def test_invalid_logical_unit_types_are_rejected(lu_type, device_type, match):
    with pytest.raises(ValueError, match=match):
        create_logical_unit_job("newLU", 18, lu_type, device_type)


def test_delete_logical_unit_job():
    xml = delete_logical_unit_job("udid-123")
    assert "DeleteLogicalUnit" in xml
    assert "LogicalUnitUDID" in xml and "udid-123" in xml


_PCM_NS = "{http://www.ibm.com/xmlns/systems/power/firmware/pcm/mc/2012_10/}"


def test_pcm_preferences_update_echoes_the_read_document():
    """V10R3 answers a hand-built partial document with HTTP 500 (#634).

    It accepts the preference element its own GET returns, with the flag values
    changed, so the update keeps every other element of that read.
    """
    read = live_fixture("rest-pcm-preferences")["body"]

    xml = pcm_preferences_update(
        read, LongTermMonitorEnabled=True, EnergyMonitorEnabled=True
    )

    root = ET.fromstring(xml)
    assert root.tag == f"{_PCM_NS}ManagedSystemPcmPreference"
    assert root.findtext(f"{_PCM_NS}LongTermMonitorEnabled") == "true"
    assert root.findtext(f"{_PCM_NS}EnergyMonitorEnabled") == "true"
    assert root.findtext(f"{_PCM_NS}AggregationEnabled") == "false"
    assert root.findtext(f"{_PCM_NS}SystemName") == "sys-R1"
    assert root.find(f"{_PCM_NS}MachineTypeModelSerialNumber") is not None
    assert root.find(f"{_PCM_NS}Metadata/{_PCM_NS}Atom/{_PCM_NS}AtomID") is not None
    assert "<feed" not in xml and "<entry" not in xml


def test_pcm_preferences_update_rejects_unsupported_fields_in_sorted_order():
    with pytest.raises(
        ValueError,
        match="Unsupported PCM preference fields: AlphaFlag, ZetaFlag",
    ):
        pcm_preferences_update(
            live_fixture("rest-pcm-preferences")["body"], ZetaFlag=True, AlphaFlag=False
        )


def test_pcm_preferences_update_refuses_a_slice_that_loses_its_namespace():
    """A prefix declared on the feed, not the element, would post unbound XML."""
    read = (
        '<feed xmlns:p="urn:p"><p:ManagedSystemPcmPreference>'
        "<LongTermMonitorEnabled>false</LongTermMonitorEnabled>"
        "</p:ManagedSystemPcmPreference></feed>"
    )
    with pytest.raises(ValueError, match="not well-formed on its own"):
        pcm_preferences_update(read, LongTermMonitorEnabled=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("read", [httpx.Response(204), httpx.Response(200, text="<x>")])
async def test_set_pcm_preferences_unusable_read_posts_nothing(mock_hmc, read):
    """An empty or malformed read is an HMCError naming the GET; nothing is posted."""
    path = (
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/preferences"
    )
    mock_hmc.get(path).mock(return_value=read)
    post_route = mock_hmc.post(path).mock(return_value=httpx.Response(200))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match=r"GET .*preferences.*nothing was changed"):
            await hmc.set_pcm_preferences(
                "ManagedSystem",
                "00000000-0000-0000-0000-000000000001",
                LongTermMonitorEnabled=True,
            )

    assert not post_route.called


def test_pcm_preferences_update_refuses_a_read_without_the_flag():
    read = "<ManagedSystemPcmPreference xmlns='urn:x'><SystemName>s</SystemName>"
    read += "</ManagedSystemPcmPreference>"
    with pytest.raises(ValueError, match="LongTermMonitorEnabled"):
        pcm_preferences_update(read, LongTermMonitorEnabled=True)


@pytest.mark.asyncio
async def test_pcm_client_rejects_unsupported_field_before_post(mock_hmc):
    post_route = mock_hmc.post("/rest/api/pcm/ManagedSystem/system-1/preferences").mock(
        return_value=httpx.Response(204)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            ValueError, match="Unsupported PCM preference fields: TypoFlag"
        ):
            await hmc.set_pcm_preferences("ManagedSystem", "system-1", TypoFlag=True)

    assert not post_route.called


def test_pcm_preferences_parse():
    xml = """<?xml version="1.0"?>
<ManagementConsolePcmPreference xmlns="http://www.ibm.com/xmlns/systems/power/firmware/pcm/mc/2012_10/">
  <LongTermMonitorEnabled>true</LongTermMonitorEnabled>
  <AggregationEnabled>false</AggregationEnabled>
  <EnergyMonitoringCapable>true</EnergyMonitoringCapable>
</ManagementConsolePcmPreference>
"""
    prefs = pcm_preferences_to_dict(xml)
    assert prefs["LongTermMonitorEnabled"] is True
    assert prefs["AggregationEnabled"] is False
    assert prefs["EnergyMonitoringCapable"] is True


def test_pcm_preferences_parse_malformed_raises():
    """Malformed XML propagates ParseError instead of silently returning {}."""
    with pytest.raises(ET.ParseError):
        pcm_preferences_to_dict("<ManagementConsolePcmPreference><unclosed>")


def test_metric_links():
    feed = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>ManagedSystem ProcessedMetrics</title>
    <updated>2026-08-07T12:00:00Z</updated>
    <link rel="SELF" href="/rest/api/pcm/ProcessedMetrics/ManagedSystem_abc_1.json" type="application/json"/>
  </entry>
  <entry>
    <title>ManagedSystem ProcessedMetrics</title>
    <updated>2026-08-07T12:00:30Z</updated>
    <link rel="SELF" href="/rest/api/pcm/ProcessedMetrics/ManagedSystem_abc_2.json" type="application/json"/>
  </entry>
</feed>
"""
    links = metric_links(feed)
    assert len(links) == 2
    assert links[0]["link"].endswith("_1.json")
    assert links[1]["updated"] == "2026-08-07T12:00:30Z"


def test_metric_links_parse_the_captured_system_feed():
    """A V10R3 system feed: one JSON document, then the partition's sub-feed (#1242).

    The sub-feed entry is `application/atom+xml` and stamped newer than the
    document; `newest_metric_link` must still select the document.
    """
    links = metric_links(live_fixture("rest-pcm-processed-metrics-feed")["body"])

    document = (
        "/rest/api/pcm/ProcessedMetrics/ManagedSystem_00000003-abcd-4ef0-8abc-"
        "000000000003_20261012T164746+0000_20261012T184700+0000_30.json"
    )
    sub_feed = (
        "/rest/api/pcm/ManagedSystem/00000003-abcd-4ef0-8abc-000000000003"
        "/LogicalPartition/00000004-ABCD-4EF0-8ABC-000000000004/ProcessedMetrics"
    )
    assert [urlsplit(link["link"]).path for link in links] == [document, sub_feed]
    assert links[1]["updated"] > links[0]["updated"]
    assert newest_metric_link(links) == links[0]


def test_metric_links_malformed_raises():
    """Malformed XML propagates ParseError instead of silently returning []."""
    with pytest.raises(ET.ParseError):
        metric_links("<feed><entry><unclosed>")


def test_newest_metric_link_selects_by_updated_not_position():
    """newest_metric_link returns the newest stamp regardless of feed order."""
    links = [
        {"link": "/old.json", "updated": "2026-08-07T12:00:00Z", "title": ""},
        {"link": "/new.json", "updated": "2026-08-07T12:00:30Z", "title": ""},
        {"link": "/mid.json", "updated": "2026-08-07T12:00:10Z", "title": ""},
    ]
    assert newest_metric_link(links)["link"] == "/new.json"


def test_newest_metric_link_unparseable_stamp_sorts_oldest():
    """A stamp that fails to parse never wins over a real timestamp."""
    links = [
        {"link": "/real.json", "updated": "2026-08-07T12:00:00Z", "title": ""},
        {"link": "/garbage.json", "updated": "not-a-date", "title": ""},
        {"link": "/missing.json", "updated": "", "title": ""},
    ]
    assert newest_metric_link(links)["link"] == "/real.json"


def test_newest_metric_link_skips_a_newer_sub_feed_entry():
    """A ManagedSystem feed also lists its partitions' feeds, stamped newest (#634).

    Captured on a V10R3 HMC: the aggregated feed's third entry links to
    `.../LogicalPartition/<uuid>/AggregatedMetrics?StartTS=...`, an Atom feed,
    with a later `updated` than either JSON document.
    """
    links = [
        {
            "link": "https://hmc.test/rest/api/pcm/AggregatedMetrics/ManagedSystem_a_b_c_300.json",
            "updated": "2026-10-06T16:44:30.000Z",
            "title": "",
        },
        {
            "link": "https://hmc.test/rest/api/pcm/ManagedSystem/a/LogicalPartition/b"
            "/AggregatedMetrics?StartTS=2026-10-06T14%3A49%3A26Z",
            "updated": "2026-10-06T16:49:10.351Z",
            "title": "",
        },
    ]

    assert newest_metric_link(links)["link"].endswith("_300.json")
    assert newest_metric_link(links[1:]) is None


def test_newest_metric_link_returns_none_for_an_empty_feed():
    """An empty metric feed has no link to select."""
    assert newest_metric_link([]) is None


# ---------------------------------------------------------------------- #
# Metrics MCP tools (split link-list vs fetch)
# ---------------------------------------------------------------------- #


SUB_FEED_ONLY = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <updated>2026-10-06T16:49:10.351Z</updated>
    <link href="/rest/api/pcm/ManagedSystem/a/LogicalPartition/b/AggregatedMetrics?StartTS=x"/>
  </entry>
</feed>
"""


def test_metrics_fetch_refuses_a_feed_with_no_document(monkeypatch, mock_hmc):
    """Entries but no `.json` document is not an aged-out sample (#634)."""
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "AggregatedMetrics",
        text=SUB_FEED_ONLY,
    )

    with pytest.raises(HMCError, match="none links a .json metrics document"):
        hmc_aggregated_metrics(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            "2026-08-07T11:00:00Z",
        )


def test_processed_metric_links(monkeypatch, mock_hmc):
    """hmc_processed_metric_links returns the parsed link list."""
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "ProcessedMetrics",
    )

    result = hmc_processed_metric_links(
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "2026-08-07T11:00:00Z",
        no_of_samples=5,
    )

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["link"].endswith("_2.json")


def test_processed_metrics_mode_fetch_fetches_latest(monkeypatch, mock_hmc):
    """hmc_processed_metrics with mode='fetch' downloads the most recent JSON."""
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "ProcessedMetrics",
    )
    document = mock_hmc.get(
        "/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json"
    ).mock(return_value=httpx.Response(200, json=METRICS_JSON))

    result = hmc_processed_metrics(
        "ManagedSystem", "00000000-0000-0000-0000-000000000001", "2026-08-07T11:00:00Z"
    )

    assert result == METRICS_JSON
    # V10R3 answers `application/json` with 406 and serves the document, typed
    # `application/vnd.ibm.powervm.pcm.json`, for `*/*` (#634).
    assert document.calls[0].request.headers["accept"] == "*/*"


def test_processed_metrics_fetches_the_captured_feed_document(monkeypatch, mock_hmc):
    """The newest JSON document is fetched, not the newer partition subfeed (#1242)."""
    _hmc_env(monkeypatch)
    path, feed = live_response("rest-pcm-processed-metrics-feed")
    mock_hmc.get(path.split("?")[0]).mock(return_value=feed)
    document = mock_hmc.get(
        "/rest/api/pcm/ProcessedMetrics/ManagedSystem_00000003-abcd-4ef0-8abc-"
        "000000000003_20261012T164746+0000_20261012T184700+0000_30.json"
    ).mock(return_value=httpx.Response(200, json=METRICS_JSON))

    result = hmc_processed_metrics(
        "ManagedSystem", "00000003-abcd-4ef0-8abc-000000000003", "2026-10-12T16:47:46Z"
    )

    assert result == METRICS_JSON
    assert document.call_count == 1


def test_processed_metrics_default_mode_is_fetch(monkeypatch, mock_hmc):
    """hmc_processed_metrics defaults to mode='fetch' when mode is omitted."""
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "ProcessedMetrics",
    )
    mock_hmc.get("/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json").mock(
        return_value=httpx.Response(200, json=METRICS_JSON)
    )

    result = hmc_processed_metrics(
        "ManagedSystem", "00000000-0000-0000-0000-000000000001", "2026-08-07T11:00:00Z"
    )

    assert result == METRICS_JSON


def test_processed_metrics_fetches_newest_not_last(monkeypatch, mock_hmc):
    """The newest document is selected by updated stamp, not feed position.

    The newest entry is listed first; the stale document (last row) returns a
    404. The tool must fetch the newest and return its JSON rather than
    surfacing no-data from the stale row.
    """
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "ProcessedMetrics",
        text=OUT_OF_ORDER_FEED,
    )
    mock_hmc.get("/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json").mock(
        return_value=httpx.Response(200, json=METRICS_JSON)
    )
    mock_hmc.get("/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_1.json").mock(
        return_value=httpx.Response(404, text="<error>expired</error>")
    )

    result = hmc_processed_metrics(
        "ManagedSystem", "00000000-0000-0000-0000-000000000001", "2026-08-07T11:00:00Z"
    )

    assert result == METRICS_JSON


def test_processed_metrics_empty_feed(monkeypatch, mock_hmc):
    """hmc_processed_metrics returns {} when no metrics are in range."""
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "ProcessedMetrics",
        text=EMPTY_FEED,
    )

    result = hmc_processed_metrics(
        "ManagedSystem", "00000000-0000-0000-0000-000000000001", "2026-08-07T11:00:00Z"
    )

    assert result == {}


def test_processed_metrics_expired_doc(monkeypatch, mock_hmc):
    """A 404 on the metrics document (aged out of retention) surfaces as {}."""
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "ProcessedMetrics",
    )
    mock_hmc.get("/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json").mock(
        return_value=httpx.Response(404, text="<error>expired</error>")
    )

    result = hmc_processed_metrics(
        "ManagedSystem", "00000000-0000-0000-0000-000000000001", "2026-08-07T11:00:00Z"
    )

    assert result == {}


def test_processed_metrics_non_404_error_propagates(monkeypatch, mock_hmc):
    """A non-404 HMCError from the document fetch is re-raised, not swallowed."""
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "ProcessedMetrics",
    )
    mock_hmc.get("/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json").mock(
        return_value=httpx.Response(500, text="<error>boom</error>")
    )

    with pytest.raises(HMCError):
        hmc_processed_metrics(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            "2026-08-07T11:00:00Z",
        )


def test_processed_metrics_doc_fetch_406_actionable(monkeypatch, mock_hmc):
    """A 406 on the metrics document fetch names the refused media type."""
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "ProcessedMetrics",
    )
    mock_hmc.get("/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json").mock(
        return_value=_not_acceptable()
    )

    with pytest.raises(HMCError, match=NOT_ACCEPTABLE):
        hmc_processed_metrics(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            "2026-08-07T11:00:00Z",
        )


def test_processed_metrics_doc_fetch_403_actionable(monkeypatch, mock_hmc):
    """A 403 on the metrics document fetch surfaces an actionable PCM authority message."""
    _hmc_env(monkeypatch)
    _route_metrics_feed(
        mock_hmc,
        "ManagedSystem",
        "00000000-0000-0000-0000-000000000001",
        "ProcessedMetrics",
    )
    mock_hmc.get("/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json").mock(
        return_value=_forbidden()
    )

    with pytest.raises(HMCError, match="(?i)does not have PCM authority"):
        hmc_processed_metrics(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            "2026-08-07T11:00:00Z",
        )


@pytest.mark.usefixtures("scoped_lpar")
def test_aggregated_metric_links(monkeypatch, mock_hmc):
    """hmc_aggregated_metric_links uses the AggregatedMetrics endpoint.

    Uses AGGREGATED_FEED so link hrefs carry AggregatedMetrics paths — the
    assertion confirms endpoint routing, not just that some href ends in _2.json.
    """
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/"
        "LogicalPartition/00000000-0000-0000-0000-000000000002/AggregatedMetrics"
    ).mock(return_value=httpx.Response(200, text=AGGREGATED_FEED))

    result = hmc_aggregated_metric_links(
        "LogicalPartition",
        "00000000-0000-0000-0000-000000000002",
        "2026-08-07T11:00:00Z",
        system_name_or_uuid="00000000-0000-0000-0000-000000000001",
    )

    assert len(result) == 1
    assert "AggregatedMetrics" in result[0]["link"]
    assert result[0]["link"].endswith("_2.json")


@pytest.mark.usefixtures("scoped_lpar")
def test_aggregated_metrics_mode_fetch_fetches_latest(monkeypatch, mock_hmc):
    """hmc_aggregated_metrics with mode='fetch' downloads from the AggregatedMetrics URL.

    Uses AGGREGATED_FEED so the document-fetch stub sits at an AggregatedMetrics
    path — a regression that accidentally fetches from ProcessedMetrics would miss
    the stub and raise an error rather than silently passing.
    """
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/"
        "LogicalPartition/00000000-0000-0000-0000-000000000002/AggregatedMetrics"
    ).mock(return_value=httpx.Response(200, text=AGGREGATED_FEED))
    mock_hmc.get("/rest/api/pcm/AggregatedMetrics/LogicalPartition_lpar_2.json").mock(
        return_value=httpx.Response(200, json=METRICS_JSON)
    )

    result = hmc_aggregated_metrics(
        "LogicalPartition",
        "00000000-0000-0000-0000-000000000002",
        "2026-08-07T11:00:00Z",
        system_name_or_uuid="00000000-0000-0000-0000-000000000001",
    )

    assert result == METRICS_JSON


def test_get_pcm_preferences(monkeypatch, mock_hmc):
    """hmc_get_pcm_preferences parses the captured V10R3 preferences feed."""
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_SCHEMA_VERSION", "V1_0")
    route = mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/preferences"
    ).mock(return_value=live_response("rest-pcm-preferences")[1])

    result = hmc_get_pcm_preferences(
        "ManagedSystem", "00000000-0000-0000-0000-000000000001"
    )

    assert result == {
        "EnergyMonitoringCapable": True,
        "LongTermMonitorEnabled": False,
        "AggregationEnabled": False,
        "ShortTermMonitorEnabled": False,
        "ComputeLTMEnabled": False,
        "EnergyMonitorEnabled": False,
    }
    # V10R3 answers `application/xml` and the uom media type with 406; `*/*` is
    # the Accept it serves (#1202).
    request = route.calls[0].request
    assert request.headers["accept"] == "*/*"
    assert "x-hmc-schema-version" not in request.headers


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        (hmc_processed_metric_links, "ProcessedMetrics"),
        (hmc_aggregated_metric_links, "AggregatedMetrics"),
    ],
)
def test_metric_feed_requests_accept_any(monkeypatch, mock_hmc, tool, kind):
    """A metric feed GET asks for `*/*`, as the preferences GET does (#634).

    V10R3 refuses the generic uom Accept on a metric feed with 406, the way it
    refuses it on the preferences endpoint beside it (#1202).
    """
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_SCHEMA_VERSION", "V1_0")
    route = mock_hmc.get(
        f"/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/{kind}"
    ).mock(return_value=httpx.Response(200, text=PCM_FEED))

    tool(
        "ManagedSystem", "00000000-0000-0000-0000-000000000001", "2026-08-07T11:00:00Z"
    )

    request = route.calls[0].request
    assert request.headers["accept"] == "*/*"
    assert "x-hmc-schema-version" not in request.headers


def test_set_pcm_preferences_returns_updated(monkeypatch, mock_hmc):
    """hmc_set_pcm_preferences returns the updated preferences dict."""
    _hmc_env(monkeypatch)
    _route_preferences_read(mock_hmc)
    mock_hmc.post(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/preferences"
    ).mock(return_value=httpx.Response(200, text=PCM_PREFS_XML))

    result = hmc_set_pcm_preferences(
        "ManagedSystem", "00000000-0000-0000-0000-000000000001", long_term_monitor=True
    )

    assert result["LongTermMonitorEnabled"] is True
    assert result["AggregationEnabled"] is False


def test_set_pcm_preferences_no_flags_raises(monkeypatch, mock_hmc):
    """hmc_set_pcm_preferences raises ValueError when no flags are supplied."""
    _hmc_env(monkeypatch)

    with pytest.raises(ValueError, match="No preference flags"):
        hmc_set_pcm_preferences("ManagedSystem", "00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------- #
# PCM 406 / 403 actionable error messages (issue #98)
# ---------------------------------------------------------------------- #


def test_get_pcm_preferences_406_actionable(monkeypatch, mock_hmc):
    """hmc_get_pcm_preferences on HTTP 406 names the refused media type."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/preferences"
    ).mock(return_value=_not_acceptable())

    with pytest.raises(HMCError, match=NOT_ACCEPTABLE):
        hmc_get_pcm_preferences("ManagedSystem", "00000000-0000-0000-0000-000000000001")


def test_get_pcm_preferences_403_actionable(monkeypatch, mock_hmc):
    """hmc_get_pcm_preferences on HTTP 403 raises HMCError mentioning PCM authority."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/preferences"
    ).mock(return_value=_forbidden())

    with pytest.raises(HMCError, match="(?i)does not have PCM authority"):
        hmc_get_pcm_preferences("ManagedSystem", "00000000-0000-0000-0000-000000000001")


def test_processed_metrics_406_actionable(monkeypatch, mock_hmc):
    """hmc_processed_metrics on HTTP 406 names the refused media type."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/ProcessedMetrics"
    ).mock(return_value=_not_acceptable())

    with pytest.raises(HMCError, match=NOT_ACCEPTABLE):
        hmc_processed_metrics(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            "2026-08-07T11:00:00Z",
        )


def test_processed_metrics_403_actionable(monkeypatch, mock_hmc):
    """hmc_processed_metrics on HTTP 403 raises HMCError mentioning PCM authority."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/ProcessedMetrics"
    ).mock(return_value=_forbidden())

    with pytest.raises(HMCError, match="(?i)does not have PCM authority"):
        hmc_processed_metrics(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            "2026-08-07T11:00:00Z",
        )


@pytest.mark.usefixtures("scoped_lpar")
def test_aggregated_metrics_406_actionable(monkeypatch, mock_hmc):
    """hmc_aggregated_metrics on HTTP 406 names the refused media type."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/LogicalPartition/00000000-0000-0000-0000-000000000002/AggregatedMetrics"
    ).mock(return_value=_not_acceptable())

    with pytest.raises(HMCError, match=NOT_ACCEPTABLE):
        hmc_aggregated_metrics(
            "LogicalPartition",
            "00000000-0000-0000-0000-000000000002",
            "2026-08-07T11:00:00Z",
            system_name_or_uuid="00000000-0000-0000-0000-000000000001",
        )


@pytest.mark.usefixtures("scoped_lpar")
def test_aggregated_metrics_403_actionable(monkeypatch, mock_hmc):
    """hmc_aggregated_metrics on HTTP 403 raises HMCError mentioning PCM authority."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/LogicalPartition/00000000-0000-0000-0000-000000000002/AggregatedMetrics"
    ).mock(return_value=_forbidden())

    with pytest.raises(HMCError, match="(?i)does not have PCM authority"):
        hmc_aggregated_metrics(
            "LogicalPartition",
            "00000000-0000-0000-0000-000000000002",
            "2026-08-07T11:00:00Z",
            system_name_or_uuid="00000000-0000-0000-0000-000000000001",
        )


def test_set_pcm_preferences_406_actionable(monkeypatch, mock_hmc):
    """hmc_set_pcm_preferences on HTTP 406 names the refused media type."""
    _hmc_env(monkeypatch)
    _route_preferences_read(mock_hmc)
    mock_hmc.post(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/preferences"
    ).mock(return_value=_not_acceptable())

    with pytest.raises(HMCError, match=NOT_ACCEPTABLE):
        hmc_set_pcm_preferences(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            long_term_monitor=True,
        )


def test_set_pcm_preferences_403_actionable(monkeypatch, mock_hmc):
    """hmc_set_pcm_preferences on HTTP 403 raises HMCError mentioning PCM authority."""
    _hmc_env(monkeypatch)
    _route_preferences_read(mock_hmc)
    mock_hmc.post(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/preferences"
    ).mock(return_value=_forbidden())

    with pytest.raises(HMCError, match="(?i)does not have PCM authority"):
        hmc_set_pcm_preferences(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            long_term_monitor=True,
        )


def test_check_pcm_error_preserves_hmc_body(monkeypatch, mock_hmc):
    """The translated HMCError retains the HMC diagnostic body and message."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/preferences"
    ).mock(return_value=_forbidden())

    with pytest.raises(HMCError) as exc_info:
        hmc_get_pcm_preferences("ManagedSystem", "00000000-0000-0000-0000-000000000001")

    assert exc_info.value.body == live_fixture("rest-pcm-metrics-403")["body"]
    assert "requested authority" in str(exc_info.value)


def test_processed_metric_links_406_actionable(monkeypatch, mock_hmc):
    """Metric discovery translates HTTP 406 to an actionable error."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/ProcessedMetrics"
    ).mock(return_value=_not_acceptable())

    with pytest.raises(HMCError, match=NOT_ACCEPTABLE):
        hmc_processed_metric_links(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            "2026-08-07T11:00:00Z",
        )


def test_processed_metric_links_403_actionable(monkeypatch, mock_hmc):
    """Metric discovery translates HTTP 403 to an actionable error."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/ProcessedMetrics"
    ).mock(return_value=_forbidden())

    with pytest.raises(HMCError, match="(?i)does not have PCM authority"):
        hmc_processed_metric_links(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            "2026-08-07T11:00:00Z",
        )


def test_resolution_403_not_wrapped_as_pcm_error(monkeypatch, mock_hmc):
    """A 403 from the UUID-resolution endpoint does NOT get a PCM-specific message.

    When a resource *name* (not UUID) is passed, _resolve_resource_uuid issues a
    GET to the UOM list endpoint.  A 403 on that endpoint is NOT PCM-specific —
    the charter exclusion says it should propagate as a raw HMCError.
    """
    _hmc_env(monkeypatch)
    # Stub the managed-system summary endpoint that resolves names to return 403.
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(403, text="<error>Forbidden</error>")
    )

    with pytest.raises(HMCError) as exc_info:
        hmc_get_pcm_preferences("ManagedSystem", "my-system-name")

    # The error must NOT contain the PCM-specific actionable messages.
    msg = str(exc_info.value)
    assert "media type" not in msg.lower()
    assert "does not have PCM authority" not in msg


def test_processed_metric_links_404_names_collection_preferences(monkeypatch, mock_hmc):
    """V11R2 answers a metric feed with 404 while every collection preference is off."""
    _hmc_env(monkeypatch)
    capture = live_fixture("rest-pcm-metrics-404-v11r2")
    mock_hmc.get(
        "/rest/api/pcm/ManagedSystem/00000000-0000-0000-0000-000000000001/ProcessedMetrics"
    ).mock(
        return_value=httpx.Response(
            capture["status"],
            text=capture["body"],
            headers={"Content-Type": capture["content_type"]},
        )
    )

    with pytest.raises(HMCError) as exc_info:
        hmc_processed_metric_links(
            "ManagedSystem",
            "00000000-0000-0000-0000-000000000001",
            "2026-08-07T11:00:00Z",
        )

    assert exc_info.value.status_code == 404
    message = str(exc_info.value)
    assert "no ProcessedMetrics feed" in message
    assert "hmc_get_pcm_preferences" in message
    assert exc_info.value.body == capture["body"]
