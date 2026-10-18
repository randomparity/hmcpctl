"""Logical-partition PCM path and unsupported-endpoint contracts."""

import inspect
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.metrics.pcm import (
    fetch_metric_links,
    get_pcm_preferences,
    resolve_pcm_resource,
    set_pcm_preferences,
)
from hmcpctl.server_tools.metrics import pcm as server_metrics
from hmcpctl.server_tools.metrics.pcm import (
    hmc_aggregated_metric_links,
    hmc_aggregated_metrics,
    hmc_processed_metric_links,
    hmc_processed_metrics,
)

SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"
LPAR_UUID = "00000000-0000-0000-0000-000000000002"
FEED = """<feed xmlns="http://www.w3.org/2005/Atom"></feed>"""


@pytest.mark.asyncio
async def test_processed_lpar_path_is_nested_under_owning_system(mock_hmc):
    route = mock_hmc.get(
        f"/rest/api/pcm/ManagedSystem/{SYSTEM_UUID}/"
        f"LogicalPartition/{LPAR_UUID}/ProcessedMetrics"
    ).mock(return_value=httpx.Response(200, text=FEED))

    async with HMCClient(make_config()) as hmc:
        assert (
            await hmc.get_processed_metric_links(
                "LogicalPartition",
                LPAR_UUID,
                "2026-08-07T11:00:00Z",
                system_uuid=SYSTEM_UUID,
            )
            == []
        )

    assert route.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get_processed_metric_links", "ProcessedMetrics"),
        ("get_aggregated_metric_links", "AggregatedMetrics"),
        ("get_ltm_metric_links", "RawMetrics/LongTermMonitor"),
    ],
)
async def test_direct_metric_client_preserves_requested_time_window(
    mock_hmc, method, suffix
):
    start = "2026-08-07T11:00:00Z"
    end = "2026-08-07T12:00:00Z"
    route = mock_hmc.get(f"/rest/api/pcm/ManagedSystem/{SYSTEM_UUID}/{suffix}").mock(
        return_value=httpx.Response(200, text=FEED)
    )
    options = {} if method == "get_ltm_metric_links" else {"no_of_samples": 5}
    async with HMCClient(make_config()) as hmc:
        assert (
            await getattr(hmc, method)(
                "ManagedSystem", SYSTEM_UUID, start, end, **options
            )
            == []
        )
    assert route.call_count == 1
    expected = {"StartTS": start, "EndTS": end}
    if options:
        expected["NoOfSamples"] = "5"
    assert dict(route.calls.last.request.url.params) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["get_processed_metric_links", "get_aggregated_metric_links"]
)
@pytest.mark.parametrize(
    ("category", "owner", "message"),
    [
        ("LogicalPartition", None, "require the owning"),
        ("ManagedSystem", SYSTEM_UUID, "valid only for LogicalPartition"),
    ],
)
async def test_direct_metric_client_rejects_inconsistent_owner_before_transport(
    mock_hmc,
    method,
    category,
    owner,
    message,
):
    async with HMCClient(make_config()) as hmc:
        before = len(mock_hmc.calls)
        with pytest.raises(ValueError, match=message):
            await getattr(hmc, method)(
                category, LPAR_UUID, "2026-08-07T11:00:00Z", system_uuid=owner
            )
        assert len(mock_hmc.calls) == before


@pytest.mark.asyncio
async def test_resolve_pcm_lpar_scopes_name_to_owning_system():
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": "server1"}
    ]
    hmc.find_partition_by_name.return_value = {"UUID": LPAR_UUID}

    target = await resolve_pcm_resource(
        hmc, "LogicalPartition", "aix1", system_name_or_uuid="server1"
    )

    assert target.resource_uuid == LPAR_UUID
    assert target.system_uuid == SYSTEM_UUID
    hmc.find_partition_by_name.assert_awaited_once_with("aix1", system_uuid=SYSTEM_UUID)


@pytest.mark.asyncio
async def test_lpar_metrics_require_owning_system():
    with pytest.raises(ValueError, match="system_name_or_uuid"):
        await resolve_pcm_resource(AsyncMock(), "LogicalPartition", LPAR_UUID)


@pytest.mark.asyncio
async def test_pcm_rejects_unknown_category_before_resolution():
    hmc = AsyncMock()

    with pytest.raises(ValueError, match="category must be one of"):
        await resolve_pcm_resource(hmc, "TypoCategory", LPAR_UUID)

    hmc.list_system_summaries.assert_not_awaited()
    hmc.find_partition_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_pcm_rejects_unknown_metric_kind_before_resolution():
    hmc = AsyncMock()

    with pytest.raises(ValueError, match="kind must be either"):
        await fetch_metric_links(
            hmc,
            "ManagedSystem",
            SYSTEM_UUID,
            kind="typo",
            start_ts="2026-08-07T11:00:00Z",
            end_ts=None,
            no_of_samples=None,
        )

    hmc.list_system_summaries.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [get_pcm_preferences, set_pcm_preferences])
async def test_lpar_preferences_are_rejected_before_client_call(operation):
    hmc = AsyncMock()
    args = (hmc, "LogicalPartition", LPAR_UUID)
    if operation is set_pcm_preferences:
        args += ({"LongTermMonitorEnabled": True},)

    with pytest.raises(ValueError, match="ManagedSystem"):
        await operation(*args)

    hmc.get_pcm_preferences.assert_not_awaited()
    hmc.set_pcm_preferences.assert_not_awaited()


@pytest.mark.asyncio
async def test_ltm_rejects_lpar_before_request():
    hmc = AsyncMock()

    with pytest.raises(ValueError, match="ManagedSystem"):
        await HMCClient.get_ltm_metric_links(
            hmc, "LogicalPartition", LPAR_UUID, "2026-08-07T11:00:00Z"
        )

    hmc._metrics_links.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_pcm_preferences", "set_pcm_preferences"])
async def test_direct_client_rejects_lpar_preferences_before_request(method):
    hmc = AsyncMock()
    args = ("LogicalPartition", LPAR_UUID)
    kwargs = {"LongTermMonitorEnabled": True} if method.startswith("set") else {}

    with pytest.raises(ValueError, match="ManagedSystem"):
        await getattr(HMCClient, method)(hmc, *args, **kwargs)

    hmc._get.assert_not_awaited()
    hmc._post_pcm.assert_not_awaited()


@pytest.mark.parametrize(
    "tool",
    [
        hmc_processed_metrics,
        hmc_processed_metric_links,
        hmc_aggregated_metrics,
        hmc_aggregated_metric_links,
    ],
)
def test_metric_tools_preserve_positional_profile_slot(tool):
    parameters = list(inspect.signature(tool).parameters)

    assert parameters.index("profile") < parameters.index("system_name_or_uuid")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (
            hmc_processed_metrics,
            ("LogicalPartition", LPAR_UUID, "2026-08-07T11:00:00Z"),
        ),
        (
            hmc_processed_metric_links,
            ("LogicalPartition", LPAR_UUID, "2026-08-07T11:00:00Z"),
        ),
    ],
)
def test_metric_tools_reject_missing_lpar_owner_before_client(monkeypatch, tool, args):
    def fail_client_entry(_profile):
        pytest.fail("invalid metric target entered the HMC client")

    monkeypatch.setattr("hmcpctl._app.client_from_env", fail_client_entry)

    with pytest.raises(ValueError, match="system_name_or_uuid"):
        tool(*args)


def test_preferences_tool_rejects_lpar_before_client(monkeypatch):
    def fail_client_entry(_profile):
        pytest.fail("unsupported preference target entered the HMC client")

    monkeypatch.setattr("hmcpctl._app.client_from_env", fail_client_entry)

    with pytest.raises(ValueError, match="ManagedSystem"):
        server_metrics.hmc_get_pcm_preferences("LogicalPartition", LPAR_UUID)
