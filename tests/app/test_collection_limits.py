"""Contract tests for client-side collection payload limits."""

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hmcpctl import _app as app_runtime
from hmcpctl._app import run_limited_collection
from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.operations.lpar import inventory as lpar_inventory
from hmcpctl.operations.systems.fleet import FleetListing, UnreadableSystem
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.storage import resources as server_storage
from hmcpctl.server_tools.systems import core as server_systems
from hmcpctl.server_tools.virtualization import adapters as server_adapters
from hmcpctl.server_tools.virtualization import network as server_network

# Composed here rather than imported: ADR 0041 removed the module-level application, so
# every consumer builds its own. The legacy-equivalent policy registers exactly the
# surface the unpolicied composition used to (pinned by G2 in
# tests/app/test_fail_closed_startup.py), and the dispatch wrapper is schema-transparent,
# so every assertion below reads the same registry it always did.
mcp = create_mcp(compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,)))


GUIDE = Path(__file__).parents[2] / "docs/operations.md"
COLLECTION_LIMIT_HEADING = "### Collection limits"
COLLECTION_LIMIT_NEXT = "### Public parameter units and selectors"
COLLECTION_LIMIT_CLAIMS = ("client-side", "complete HMC feed", "transferred and parsed")


EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"/>
"""


COLLECTION_TOOLS = {
    "hmc_list_systems": (server_systems, (), ["state", "profile", "limit"]),
    "hmc_list_lpars": (
        server_systems,
        (),
        ["system_name_or_uuid", "state", "profile", "limit"],
    ),
    "hmc_list_vios": (
        server_systems,
        (),
        ["system_name_or_uuid", "state", "profile", "limit"],
    ),
    "hmc_list_resources": (
        server_systems,
        ("ManagedSystem",),
        ["resource_type", "profile", "limit"],
    ),
    "hmc_list_adapters": (
        server_adapters,
        ("lpar-1",),
        [
            "lpar_name_or_uuid",
            "adapter_type",
            "profile",
            "limit",
            "system_name_or_uuid",
        ],
    ),
    "hmc_list_virtual_switches": (
        server_network,
        ("system-1",),
        ["system_name_or_uuid", "profile", "limit"],
    ),
    "hmc_list_virtual_networks": (
        server_network,
        ("system-1",),
        ["system_name_or_uuid", "profile", "limit"],
    ),
    "hmc_list_network_bridges": (
        server_network,
        ("system-1",),
        ["system_name_or_uuid", "profile", "limit"],
    ),
    "hmc_list_volume_groups": (
        server_storage,
        ("vios-1",),
        ["vios_name_or_uuid", "profile", "limit", "system_name_or_uuid"],
    ),
    "hmc_list_clusters": (server_storage, (), ["profile", "limit"]),
    "hmc_list_shared_storage_pools": (
        server_storage,
        (),
        ["profile", "limit"],
    ),
}


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (None, [{"id": 1}, {"id": 2}, {"id": 3}]),
        (2, [{"id": 1}, {"id": 2}]),
        (0, []),
    ],
)
def test_run_limited_collection_caps_after_operation(limit, expected):
    entries = [{"id": 1}, {"id": 2}, {"id": 3}]
    operation = AsyncMock(return_value=entries)
    client = MagicMock()

    with patch.object(
        app_runtime, "client_from_env", return_value=_client_context(client)
    ):
        result = run_limited_collection(operation, limit)

    assert result == expected
    operation.assert_awaited_once_with(client)


def test_run_limited_collection_rejects_negative_limit_before_operation():
    operation = AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="^limit must be greater than or equal to 0$"):
        run_limited_collection(operation, -1)

    operation.assert_not_called()


@pytest.mark.parametrize(("tool_name", "entry"), COLLECTION_TOOLS.items())
def test_collection_tool_signatures_and_limit_schema(tool_name, entry):
    module, _args, expected_parameters = entry
    function = getattr(module, tool_name)
    parameters = inspect.signature(function).parameters
    assert list(parameters) == expected_parameters

    expected_default = None
    assert parameters["limit"].default == expected_default

    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    limit_schema = tools[tool_name].parameters["properties"]["limit"]
    assert limit_schema["default"] == expected_default
    description = limit_schema["description"]
    assert "client-side" in description
    assert "complete HMC feed" in description
    assert "parsed" in description


# These return a FleetListing (ADR 0197): ``limit`` caps its entries only.
LISTING_TOOLS = {"hmc_list_lpars", "hmc_list_vios"}


@pytest.mark.parametrize("limit", [None, 2, 0, -1])
@pytest.mark.parametrize(
    ("tool_name", "entry"),
    [item for item in COLLECTION_TOOLS.items() if item[0] not in LISTING_TOOLS],
)
def test_collection_tools_delegate_limit_to_shared_helper(tool_name, entry, limit):
    module, args, _expected_parameters = entry
    function = getattr(module, tool_name)

    with patch.object(
        module, "run_limited_collection", return_value=[{"id": 1}]
    ) as run:
        result = function(*args, limit=limit)

    assert result == [{"id": 1}]
    assert run.call_args.args[1] == limit


@pytest.mark.parametrize("limit", [None, 1, 0])
@pytest.mark.parametrize("tool_name", sorted(LISTING_TOOLS))
def test_listing_limit_caps_entries_and_keeps_every_skipped_system(tool_name, limit):
    skipped = [UnreadableSystem("sys-R1", "r1", "No Connection", None)] * 2
    listing = FleetListing([{"id": 1}, {"id": 2}], skipped)

    with patch.object(server_systems, "with_client", return_value=listing):
        result = getattr(server_systems, tool_name)(limit=limit)

    assert result.entries == listing.entries[:limit]
    assert result.unreadable_systems == skipped


@pytest.mark.parametrize("tool_name", sorted(LISTING_TOOLS))
def test_listing_rejects_negative_limit_before_any_request(tool_name):
    with (
        patch.object(server_systems, "with_client") as run,
        pytest.raises(ValueError, match="^limit must be greater than or equal to 0$"),
    ):
        getattr(server_systems, tool_name)(limit=-1)

    run.assert_not_called()


def test_limit_is_not_sent_on_root_child_search_or_job_requests(monkeypatch, mock_hmc):
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")
    quick_all = mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(200, json=[])
    )
    cases = [
        (quick_all, lambda: server_systems.hmc_list_systems(limit=1)),
        (
            mock_hmc.get(
                "/rest/api/uom/LogicalPartition/"
                "00000000-0000-0000-0000-000000000002/ClientNetworkAdapter"
            ).mock(return_value=httpx.Response(200, text=EMPTY_FEED)),
            lambda: server_adapters.hmc_list_adapters(
                "00000000-0000-0000-0000-000000000002", limit=1
            ),
        ),
        (
            quick_all,
            lambda: server_systems.hmc_list_systems(state="operating", limit=1),
        ),
    ]

    forbidden = {"limit", "_limit", "maxCount", "count"}
    for route, invoke in cases:
        before = route.call_count
        assert invoke() == []
        assert route.call_count > before
        assert forbidden.isdisjoint(route.calls[-1].request.url.params.keys())


def _client_context(client):
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def test_system_state_selector_runs_before_results_are_capped():
    summaries = [
        {"UUID": f"00000000-0000-4000-8000-00000000000{index}", "State": state}
        for index, state in enumerate(("operating", "standby", "standby", "standby"))
    ]
    client = MagicMock()
    client.list_system_summaries = AsyncMock(return_value=summaries)
    client.get_managed_system = AsyncMock()
    with patch.object(
        app_runtime, "client_from_env", return_value=_client_context(client)
    ):
        result = server_systems.hmc_list_systems(state="standby", limit=2)

    assert [entry["UUID"] for entry in result] == [
        summary["UUID"] for summary in summaries[1:3]
    ]
    client.get_managed_system.assert_not_awaited()


def test_lpar_parent_selector_runs_before_results_are_capped():
    entries = [{"id": 1}, {"id": 2}, {"id": 3}]
    client = MagicMock()
    client.list_logical_partitions = AsyncMock(return_value=entries)
    with (
        patch.object(
            app_runtime, "client_from_env", return_value=_client_context(client)
        ),
        patch.object(
            lpar_inventory,
            "resolve_system_uuid",
            new=AsyncMock(return_value="system-uuid"),
        ) as resolve,
    ):
        result = server_systems.hmc_list_lpars("system-name", limit=2)

    assert result == FleetListing(entries[:2], [])
    resolve.assert_awaited_once_with(client, "system-name")
    client.list_logical_partitions.assert_awaited_once_with("system-uuid")


def test_adapter_type_selector_runs_before_results_are_capped():
    entries = [{"id": 1}, {"id": 2}, {"id": 3}]
    client = MagicMock()
    with (
        patch.object(
            app_runtime, "client_from_env", return_value=_client_context(client)
        ),
        patch.object(
            server_adapters,
            "list_adapters",
            new=AsyncMock(return_value=entries),
        ) as list_selected,
    ):
        result = server_adapters.hmc_list_adapters(
            "lpar-name", "VirtualSCSIClientAdapter", limit=2
        )

    assert result == entries[:2]
    list_selected.assert_awaited_once_with(
        client, None, "lpar-name", "VirtualSCSIClientAdapter"
    )


def test_arbitrary_resource_type_runs_before_results_are_capped():
    entries = [{"id": 1}, {"id": 2}, {"id": 3}]
    client = MagicMock()
    client.list_uom = AsyncMock(return_value=entries)
    with patch.object(
        app_runtime, "client_from_env", return_value=_client_context(client)
    ):
        result = server_systems.hmc_list_resources("Cluster", limit=2)

    assert result == entries[:2]
    client.list_uom.assert_awaited_once_with("Cluster")


def _collection_limit_section(readme: str) -> str:
    """Return the body of operation guide's '### Collection limits' section.

    Heading *order* is asserted first: without it a renamed or reordered heading makes
    the second split return the rest of the file, and the slice silently stops being a
    section.
    """
    for heading in (COLLECTION_LIMIT_HEADING, COLLECTION_LIMIT_NEXT):
        assert heading in readme, f"operation guide has no '{heading}' heading"
    assert readme.index(COLLECTION_LIMIT_HEADING) < readme.index(
        COLLECTION_LIMIT_NEXT
    ), (
        f"operation guide must keep '{COLLECTION_LIMIT_HEADING}' before '{COLLECTION_LIMIT_NEXT}'"
    )
    return readme.split(COLLECTION_LIMIT_HEADING, 1)[1].split(COLLECTION_LIMIT_NEXT, 1)[
        0
    ]


def _relocated(readme: str) -> str:
    """Move the collection-limit disclosure into an unrelated operation guide section."""
    body = _collection_limit_section(readme)
    return readme.replace(body, "\n\n", 1).replace(
        "# Operation details\n", f"# Operation details\n{body}", 1
    )


def test_operations_guide_discloses_collection_limit_costs():
    section = _collection_limit_section(GUIDE.read_text(encoding="utf-8"))

    for claim in COLLECTION_LIMIT_CLAIMS:
        assert claim in section, f"'{COLLECTION_LIMIT_HEADING}' must state {claim!r}"


def test_collection_limit_disclosure_relocated_out_of_its_section_is_caught():
    """The negative variant a whole-file substring check cannot see.

    'client-side' also appears under '### Public parameter units and selectors', so a
    bare ``in readme`` check certifies presence, not placement. Relocation keeps every
    phrase in the file and must still fail.
    """
    relocated = _relocated(GUIDE.read_text(encoding="utf-8"))

    assert all(claim in relocated for claim in COLLECTION_LIMIT_CLAIMS)
    moved = _collection_limit_section(relocated)
    assert [claim for claim in COLLECTION_LIMIT_CLAIMS if claim in moved] == []
