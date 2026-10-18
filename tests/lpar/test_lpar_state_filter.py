"""Fleet-wide LPAR state filtering against what a V10R3 HMC answers (#1202).

The HMC's search parser refuses a PartitionState value with a space:
``LogicalPartition/search/(PartitionState==not%20activated)`` is a captured
``500 Unable to parse expression``. ``list_lpars`` therefore filters the
partition feeds itself; without a system it reads each operating system's
``ManagedSystem/{uuid}/LogicalPartition`` feed (ADR 0197).
"""

from __future__ import annotations

import httpx
import pytest
from conftest import live_fixture, live_response, make_config, mock_system_summaries

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.inventory import list_lpars
from hmcpctl.operations.partition_state import PARTITION_STATES

# The captured LPAR read (state `not activated`), and the same entry reading
# `running`, wrapped as the fleet feed the HMC returns for this path.
_ENTRY = live_fixture("rest-lpar-entry")["body"]
_RUNNING_ENTRY = _ENTRY.replace(">not activated<", ">running<")
_FEED = (
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    f"{_ENTRY.split('?>', 1)[-1]}{_RUNNING_ENTRY.split('?>', 1)[-1]}</feed>"
)
_SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["not activated", "running"])
async def test_fleet_wide_state_filter_reads_the_feed_not_the_search(
    mock_hmc, state
) -> None:
    assert _RUNNING_ENTRY != _ENTRY
    search_path, refused = live_response("rest-lpar-search-state-refused")
    search = mock_hmc.get(url__regex=r"/rest/api/uom/LogicalPartition/search/.*").mock(
        return_value=refused
    )
    mock_system_summaries(mock_hmc, (_SYSTEM_UUID, "sys-E2", "operating"))
    feed = mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{_SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(200, text=_FEED))
    fleet_wide = mock_hmc.get("/rest/api/uom/LogicalPartition").mock(
        return_value=httpx.Response(200, text=_FEED)
    )

    async with HMCClient(make_config()) as hmc:
        listing = await list_lpars(hmc, state=state)

    assert "PartitionState==not%20activated" in search_path
    assert not search.called
    assert not fleet_wide.called
    assert feed.call_count == 1
    assert [lpar["Resource"]["PartitionState"] for lpar in listing.entries] == [state]


def test_partition_states_are_the_hmc_schema_enumeration() -> None:
    """`LogicalPartitionState.Enum`, read from the V10R3 HMC's own schema.

    ``GET /rest/api/web/schema/inc/Enumerations.xsd`` captured 2026-09-30
    (capture-raw.jsonl#5). `Unknown` is capitalised there.
    """
    assert PARTITION_STATES == {
        "error",
        "not activated",
        "not available",
        "open firmware",
        "running",
        "shutting down",
        "starting",
        "migrating not active",
        "migrating running",
        "hardware discovery",
        "suspended",
        "suspending",
        "resuming",
        "Unknown",
    }
