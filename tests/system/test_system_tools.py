"""Tool-layer tests for the consolidated read-only inventory MCP tools.

The client methods are covered in the domain test dirs; these tests call the
actual ``@mcp.tool`` functions in ``server_tools.systems`` against the respx
``mock_hmc`` router so the argument->URL mapping in the tool bodies is
exercised. hmc_get_job is covered in ``tests/app/test_server_tools.py``;
hmc_run_command (SSH) is covered there too.
"""

import httpx
import pytest
from conftest import (
    captured_lpar_entry,
    captured_system_entry,
    mock_system_summaries,
)

from hmcpctl.errors import HMCError
from hmcpctl.operations.systems.fleet import FleetListing, UnreadableSystem
from hmcpctl.server_tools.inventory.capacity import (
    hmc_capacity_report,
    hmc_find_placement,
)
from hmcpctl.server_tools.systems.core import (
    hmc_get_console_info,
    hmc_get_lpar,
    hmc_get_lpar_state,
    hmc_get_system,
    hmc_get_vios_storage_detail,
    hmc_list_lpars,
    hmc_list_resources,
    hmc_list_systems,
    hmc_list_vios,
)

SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"
LPAR_UUID = "00000000-0000-0000-0000-000000000002"
VIOS_UUID = "00000000-0000-0000-0000-000000000003"


def _hmc_env(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def _feed(uuid: str, rtype: str, **fields: str) -> str:
    """A single-resource Atom feed; {fields} render as resource elements."""
    body = "\n".join(
        f'        <{name} xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">{value}</{name}>'
        for name, value in fields.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{uuid}</id>
    <title>{rtype}:{uuid}</title>
    <link rel="SELF" href="https://hmc.test:12443/rest/api/uom/{rtype}/{uuid}"/>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <{rtype} xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
{body}
      </{rtype}>
    </content>
  </entry>
</feed>
"""


OFFLINE_UUID = "00000000-0000-0000-0000-000000000009"


def _fleet(mock_hmc) -> list:
    """sys-R1 in No Connection and sys-E2 operating; returns the HMC-wide routes.

    Each returned route must stay uncalled: the HMC-wide feeds time out while
    any one system has no connection (#1293), and so does the full
    ManagedSystem feed (#1309).
    """
    mock_system_summaries(
        mock_hmc,
        (OFFLINE_UUID, "sys-R1", "no connection"),
        (SYSTEM_UUID, "sys-E2", "operating"),
    )
    return [
        mock_hmc.get("/rest/api/uom/ManagedSystem").mock(
            return_value=httpx.Response(504)
        ),
        mock_hmc.get("/rest/api/uom/LogicalPartition").mock(
            return_value=httpx.Response(504)
        ),
        mock_hmc.get("/rest/api/uom/VirtualIOServer").mock(
            return_value=httpx.Response(504)
        ),
        mock_hmc.get(url__regex=r"/rest/api/uom/VirtualIOServer/search/.*").mock(
            return_value=httpx.Response(504)
        ),
    ]


SKIPPED = [UnreadableSystem("sys-R1", OFFLINE_UUID, "no connection", None)]


LPAR_SEARCH_FEED = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{uuid}</id>
    <title>LogicalPartition:{name}</title>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>{name}</PartitionName>
      </LogicalPartition>
    </content>
  </entry>
</feed>
"""

EMPTY_FEED = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom"/>
"""


# ---------------------------------------------------------------------- #
# Console
# ---------------------------------------------------------------------- #

# Reconstructed, not captured. #880 quotes the *rendered* error for this
# failure -- `GET ... failed (HTTP 500): ` plus the detail -- which is what
# HMCError builds (errors.py), not what the HMC put on the wire. Two
# transformations produced the string below: the client-added prefix was
# removed, and the issue's three wrapped lines were joined with single
# spaces. The raw response body has never been captured; the live-run record
# for #880 is where a captured one belongs.
NULL_PROPERTY_500_BODY = (
    "Nested path contains null property , currentProperty=SessionId "
    "nestedPath=Session/SessionId/Value"
)

# Also constructed, and hypothetical: no ManagementConsole 500 body has been
# observed in this shape, and the vendored reference corpus names neither
# HttpErrorResponse nor the marker. errors.py states that HMC error bodies
# are XML, and a body of this shape renders as
# `... (HTTP 500): Internal Server Error` -- the guard sees the
# null-property text only by reading the body itself.
NULL_PROPERTY_500_XML_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<HttpErrorResponse>"
    "<Message>Internal Server Error</Message>"
    f"<Detail>{NULL_PROPERTY_500_BODY}</Detail>"
    "</HttpErrorResponse>"
)


def test_console_info_returns_management_console(monkeypatch, mock_hmc):
    """hmc_console_info GETs the ManagementConsole collection."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(
            200,
            text=_feed("mc-uuid-1", "ManagementConsole", Version="V10R1M1040"),
        )
    )
    result = hmc_get_console_info()
    assert result["UUID"] == "mc-uuid-1"
    assert result["Resource"]["Version"] == "V10R1M1040"


@pytest.mark.parametrize("body", [NULL_PROPERTY_500_BODY, NULL_PROPERTY_500_XML_BODY])
def test_console_info_translates_known_null_property_500(monkeypatch, mock_hmc, body):
    """A null-property 500 becomes the actionable schema-pin error.

    Asserting the produced message is what makes this test bite: the
    untranslated transport error carries the same status code and the same
    body, so only the message distinguishes a guard that fired from one that
    did not. The XML case is what makes the guard read `exc.body` rather than
    the rendered detail, which would show only `Internal Server Error`.
    """
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(500, text=body)
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_get_console_info()
    message = str(exc_info.value)
    assert "Management-console inventory is unavailable" in message
    assert "could not serialize a null property" in message
    assert "X-HMC-Schema-Version pin (HMC_SCHEMA_VERSION)" in message
    assert "unset HMC_SCHEMA_VERSION" in message
    assert "firmware" not in message
    assert exc_info.value.status_code == 500
    assert exc_info.value.body == body
    assert isinstance(exc_info.value.__cause__, HMCError)


@pytest.mark.parametrize("body", ["forbidden", NULL_PROPERTY_500_BODY])
def test_console_info_propagates_unrelated_hmc_error(monkeypatch, mock_hmc, body):
    """A non-500 stays untranslated even when it carries the null-property text.

    The second case pins the status half of the guard: without it, deleting
    `exc.status_code == 500` leaves every console test green.
    """
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(403, text=body)
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_get_console_info()
    assert exc_info.value.status_code == 403
    assert "Management-console inventory is unavailable" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_console_info_propagates_unrelated_http_500(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(500, text="database unavailable")
    )
    with pytest.raises(HMCError, match="database unavailable") as exc_info:
        hmc_get_console_info()
    assert exc_info.value.status_code == 500
    assert "Management-console inventory is unavailable" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def _system_entry(mock_hmc, name: str, state: str = "operating"):
    """quick/All lists one system; its own entry answers when it is operating."""
    mock_system_summaries(mock_hmc, (SYSTEM_UUID, name, state))
    return mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200, text=_feed(SYSTEM_UUID, "ManagedSystem", SystemName=name, State=state)
        )
    )


# ---------------------------------------------------------------------- #
# hmc_list_systems
# ---------------------------------------------------------------------- #


def test_systems_no_arg_lists_all(monkeypatch, mock_hmc):
    """hmc_list_systems() lists every system quick/All reports, with its full entry."""
    _hmc_env(monkeypatch)
    _system_entry(mock_hmc, "s824-01")
    result = hmc_list_systems()
    assert result[0]["UUID"] == SYSTEM_UUID
    assert result[0]["Resource"]["SystemName"] == "s824-01"
    assert result[0]["ResourceType"] == "ManagedSystem"


def test_find_system_gets_one_by_name(monkeypatch, mock_hmc):
    """hmc_get_system returns one system dict by exact name."""
    _hmc_env(monkeypatch)
    _system_entry(mock_hmc, "p10-e1080")
    result = hmc_get_system("p10-e1080")
    assert result["Resource"]["State"] == "operating"


def test_get_system_gets_one_by_uuid(monkeypatch, mock_hmc):
    """hmc_get_system resolves a UUID through quick/All, then the direct endpoint."""
    _hmc_env(monkeypatch)
    entry = _system_entry(mock_hmc, "p10-e1080")

    result = hmc_get_system(SYSTEM_UUID)

    assert result is not None
    assert result["UUID"] == SYSTEM_UUID
    assert entry.called


def test_find_system_list_error_propagates(monkeypatch, mock_hmc):
    """An inventory error during a system lookup preserves its status code."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(404, text="<error>not found</error>")
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_get_system("p10-e1080")
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------- #
# hmc_list_lpars
# ---------------------------------------------------------------------- #


def test_lpars_no_arg_reads_each_operating_system(monkeypatch, mock_hmc):
    """hmc_list_lpars() reads operating systems' feeds and names the rest."""
    _hmc_env(monkeypatch)
    fleet_wide = _fleet(mock_hmc)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(
            200, text=_feed(LPAR_UUID, "LogicalPartition", PartitionName="aix1")
        )
    )
    result = hmc_list_lpars()
    assert [e["Resource"]["PartitionName"] for e in result.entries] == ["aix1"]
    assert result.unreadable_systems == SKIPPED
    assert not any(route.called for route in fleet_wide)


def test_lpars_system_uuid_scopes(monkeypatch, mock_hmc):
    """hmc_list_lpars(system_uuid=...) uses the system-scoped child feed URL."""
    _hmc_env(monkeypatch)
    route = mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition"
    ).mock(
        return_value=httpx.Response(
            200, text=_feed(LPAR_UUID, "LogicalPartition", PartitionName="aix1")
        )
    )
    hmc_list_lpars(system_name_or_uuid=SYSTEM_UUID)
    assert route.called


def test_lpars_lpar_uuid_gets_one(monkeypatch, mock_hmc):
    """hmc_list_lpars(lpar_uuid=...) GETs one LPAR by UUID."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(
            200, text=_feed(LPAR_UUID, "LogicalPartition", PartitionState="running")
        )
    )
    result = hmc_get_lpar(LPAR_UUID)
    assert result["Resource"]["PartitionState"] == "running"


def test_lpars_name_finds_by_name(monkeypatch, mock_hmc):
    """hmc_list_lpars resolves a non-UUID selector as a PartitionName."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/LogicalPartition/search/(PartitionName==aixprod)").mock(
        return_value=httpx.Response(
            200, text=LPAR_SEARCH_FEED.format(uuid=LPAR_UUID, name="aixprod")
        )
    )
    result = hmc_get_lpar("aixprod")
    assert result["UUID"] == LPAR_UUID
    assert result["Resource"]["PartitionName"] == "aixprod"


def test_lpars_name_not_found_returns_none(monkeypatch, mock_hmc):
    """hmc_get_lpar returns None when a name matches nothing and every system operates."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/LogicalPartition/search/(PartitionName==ghost)").mock(
        return_value=httpx.Response(200, text=EMPTY_FEED)
    )
    mock_system_summaries(mock_hmc, (SYSTEM_UUID, "sys-E2", "operating"))
    assert hmc_get_lpar("ghost") is None


def test_get_lpar_state_returns_string(monkeypatch, mock_hmc):
    """hmc_get_lpar_state uses the cheap quick-property endpoint."""
    _hmc_env(monkeypatch)
    route = mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/quick/PartitionState"
    ).mock(return_value=httpx.Response(200, text='"running"'))
    result = hmc_get_lpar_state(LPAR_UUID)
    assert route.called
    assert result == "running"


# ---------------------------------------------------------------------- #
# hmc_list_vios
# ---------------------------------------------------------------------- #


def test_vios_no_arg_reads_each_operating_system(monkeypatch, mock_hmc):
    """hmc_list_vios() reads operating systems' feeds and names the rest."""
    _hmc_env(monkeypatch)
    fleet_wide = _fleet(mock_hmc)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/VirtualIOServer").mock(
        return_value=httpx.Response(
            200, text=_feed(VIOS_UUID, "VirtualIOServer", PartitionName="vios1")
        )
    )
    result = hmc_list_vios()
    assert [e["Resource"]["PartitionName"] for e in result.entries] == ["vios1"]
    assert result.unreadable_systems == SKIPPED
    assert not any(route.called for route in fleet_wide)


def test_vios_with_uuid_returns_storage_detail(monkeypatch, mock_hmc):
    """hmc_list_vios(vios_uuid=...) GETs both documented mapping groups."""
    _hmc_env(monkeypatch)
    route = mock_hmc.get(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}?group=ViosSCSIMapping,ViosFCMapping"
    ).mock(
        return_value=httpx.Response(
            200, text=_feed(VIOS_UUID, "VirtualIOServer", PartitionName="vios1")
        )
    )
    result = hmc_get_vios_storage_detail(VIOS_UUID)
    assert route.called
    assert result["UUID"] == VIOS_UUID


# ---------------------------------------------------------------------- #
# hmc_get_system
# ---------------------------------------------------------------------- #


def test_find_system_found(monkeypatch, mock_hmc):
    """hmc_get_system returns the matching system entry when found."""
    _hmc_env(monkeypatch)
    _system_entry(mock_hmc, "p9-01")
    result = hmc_get_system("p9-01")
    assert result is not None
    assert result["UUID"] == SYSTEM_UUID
    assert result["Resource"]["SystemName"] == "p9-01"


def test_find_system_not_found(monkeypatch, mock_hmc):
    """hmc_get_system returns None when no system matches the name."""
    _hmc_env(monkeypatch)
    mock_system_summaries(mock_hmc, (SYSTEM_UUID, "p9-01", "operating"))
    result = hmc_get_system("ghost-sys")
    assert result is None


# ---------------------------------------------------------------------- #
# hmc_list_resources (unchanged)
# ---------------------------------------------------------------------- #


def test_list_resources(monkeypatch, mock_hmc):
    """hmc_list_resources GETs the requested resource type collection."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/Cluster").mock(
        return_value=httpx.Response(200, text=_feed("cluster-uuid-1", "Cluster"))
    )
    result = hmc_list_resources("Cluster")
    assert result[0]["UUID"] == "cluster-uuid-1"


# ---------------------------------------------------------------------- #
# hmc_capacity_report + hmc_find_placement
# ---------------------------------------------------------------------- #

SYS_UUID_A = "00000000-0000-0000-0000-00000000000a"
SYS_UUID_B = "00000000-0000-0000-0000-00000000000b"


def _mock_operating_systems(router, *systems: tuple[str, str]) -> None:
    """Answer the quick/All read an empty partition feed triggers (#1301, #1500)."""
    mock_system_summaries(
        router, *((uuid, name, "operating") for uuid, name in systems)
    )


def _mock_system_entries(router, *systems: tuple[str, str, dict[str, str]]) -> None:
    """quick/All lists each system as operating; its own entry carries the figures."""
    for uuid, name, figures in systems:
        router.get(f"/rest/api/uom/ManagedSystem/{uuid}").mock(
            return_value=httpx.Response(
                200, text=captured_system_entry(uuid, name, **figures)
            )
        )
    _mock_operating_systems(router, *((uuid, name) for uuid, name, _ in systems))


def _sys_feed(*entries: str) -> str:
    """Wrap one or more entry XML strings in an Atom feed."""
    joined = "\n".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
{joined}
</feed>
"""


def test_capacity_report_computes_per_system(monkeypatch, mock_hmc):
    """hmc_capacity_report reports each system's own configurable and free figures."""
    _hmc_env(monkeypatch)
    _mock_system_entries(
        mock_hmc,
        (SYS_UUID_A, "p9-01", {}),
        (
            SYS_UUID_B,
            "p9-02",
            {
                "configurable_mem": "65536",
                "available_mem": "61440",
                "configurable_proc": "8",
                "available_proc": "7.5",
            },
        ),
    )
    # Inactive partitions read 0 everywhere, so they must not shape capacity.
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYS_UUID_A}/LogicalPartition").mock(
        return_value=httpx.Response(
            200,
            text=_sys_feed(
                captured_lpar_entry("lp-a1", "aix1", state="running"),
                captured_lpar_entry("lp-a2", "aix2"),
            ),
        )
    )
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYS_UUID_B}/LogicalPartition").mock(
        return_value=httpx.Response(
            200, text=_sys_feed(captured_lpar_entry("lp-b1", "linux1"))
        )
    )

    result = hmc_capacity_report()

    assert len(result) == 2
    by_name = {item.system_name: item for item in result}

    a = by_name["p9-01"]
    assert a.total_memory_mib == 131072
    assert a.free_memory_mib == 112448
    assert a.assigned_memory_mib == 131072 - 112448
    assert a.total_proc_units == 20.0
    assert a.free_proc_units == 18.0
    assert a.assigned_proc_units == 2.0
    assert a.total_lpars == 2
    assert a.running_lpars == 1  # only "running" counts

    b = by_name["p9-02"]
    assert b.assigned_memory_mib == 4096
    assert b.free_memory_mib == 61440
    assert b.assigned_proc_units == 0.5


def test_capacity_report_empty_lpar_list(monkeypatch, mock_hmc):
    """hmc_capacity_report handles a system with no LPARs."""
    _hmc_env(monkeypatch)
    _mock_system_entries(mock_hmc, (SYS_UUID_A, "empty-sys", {}))
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYS_UUID_A}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=EMPTY_FEED)
    )

    result = hmc_capacity_report()
    assert result[0].free_memory_mib == 112448
    assert result[0].running_lpars == 0
    assert result[0].total_lpars == 0


def test_find_placement_returns_candidates(monkeypatch, mock_hmc):
    """hmc_find_placement returns systems that can host the requested LPAR."""
    _hmc_env(monkeypatch)
    _mock_system_entries(
        mock_hmc,
        (SYS_UUID_A, "big-sys", {}),
        (SYS_UUID_B, "small-sys", {"available_mem": "2048", "available_proc": "0.5"}),
    )
    for uuid in (SYS_UUID_A, SYS_UUID_B):
        mock_hmc.get(f"/rest/api/uom/ManagedSystem/{uuid}/LogicalPartition").mock(
            return_value=httpx.Response(200, text=EMPTY_FEED)
        )

    # Request 4096 MiB and 0.5 procs → only big-sys qualifies (small-sys has 2048 MiB free)
    result = hmc_find_placement(desired_memory_mib=4096, desired_proc_units=0.5)
    assert len(result) == 1
    assert result[0].system_name == "big-sys"
    assert result[0].free_memory_mib == 112448


def test_find_placement_no_candidates(monkeypatch, mock_hmc):
    """hmc_find_placement returns empty list when no system has enough free resources."""
    _hmc_env(monkeypatch)
    _mock_system_entries(
        mock_hmc,
        (SYS_UUID_A, "full-sys", {"available_mem": "0", "available_proc": "0"}),
    )
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYS_UUID_A}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=EMPTY_FEED)
    )

    result = hmc_find_placement(desired_memory_mib=512)
    assert result == []


# ---------------------------------------------------------------------- #
# State filter tests — hmc_list_systems, hmc_list_lpars, hmc_list_vios
# ---------------------------------------------------------------------- #


def test_systems_state_filter_reads_quick_all_not_the_search(monkeypatch, mock_hmc):
    """hmc_list_systems(state=...) filters quick/All locally; the search can stall."""
    _hmc_env(monkeypatch)
    other = "00000000-0000-0000-0000-0000000000ff"
    mock_system_summaries(
        mock_hmc,
        (SYSTEM_UUID, "s824-01", "operating"),
        (other, "s824-02", "no connection"),
    )
    entry = mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200,
            text=_feed(
                SYSTEM_UUID, "ManagedSystem", SystemName="s824-01", State="operating"
            ),
        )
    )
    search = mock_hmc.get(url__regex=r"/ManagedSystem/search/.*").mock(
        side_effect=httpx.ReadTimeout("stalled")
    )
    result = hmc_list_systems(state="operating")
    assert entry.called and not search.called
    assert len(result) == 1
    assert result[0]["Resource"]["State"] == "operating"


def test_systems_state_filter_empty_returns_empty_list(monkeypatch, mock_hmc):
    """hmc_list_systems(state='no-match') returns [] when no system has that State."""
    _hmc_env(monkeypatch)
    mock_system_summaries(mock_hmc, (SYSTEM_UUID, "s824-01", "operating"))
    result = hmc_list_systems(state="no-match")
    assert result == []


def test_lpars_state_filter_reads_the_partition_feed(monkeypatch, mock_hmc):
    """hmc_list_lpars(state='running') filters the feed; V10R3 cannot search it (#1202)."""
    _hmc_env(monkeypatch)
    fleet_wide = _fleet(mock_hmc)
    route = mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition"
    ).mock(
        return_value=httpx.Response(
            200,
            text=_feed(
                LPAR_UUID,
                "LogicalPartition",
                PartitionName="aix1",
                PartitionState="running",
            ),
        )
    )
    result = hmc_list_lpars(state="running")
    assert route.called
    assert not any(route.called for route in fleet_wide)
    assert len(result.entries) == 1
    assert result.entries[0]["Resource"]["PartitionState"] == "running"


def test_lpars_state_filter_empty_returns_empty_list(monkeypatch, mock_hmc):
    """hmc_list_lpars(state='not activated') returns no entries when none match."""
    _hmc_env(monkeypatch)
    _fleet(mock_hmc)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(
            200,
            text=_feed(
                LPAR_UUID,
                "LogicalPartition",
                PartitionName="aix1",
                PartitionState="running",
            ),
        )
    )
    result = hmc_list_lpars(state="not activated")
    assert result == FleetListing([], SKIPPED)


def test_vios_state_filter_reads_each_operating_system(monkeypatch, mock_hmc):
    """hmc_list_vios(state='running') filters scoped feeds, never the HMC-wide search."""
    _hmc_env(monkeypatch)
    fleet_wide = _fleet(mock_hmc)
    route = mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/VirtualIOServer"
    ).mock(
        return_value=httpx.Response(
            200,
            text=_feed(
                VIOS_UUID,
                "VirtualIOServer",
                PartitionName="vios1",
                PartitionState="running",
            ),
        )
    )
    result = hmc_list_vios(state="running")
    assert route.called
    assert not any(route.called for route in fleet_wide)
    assert [e["Resource"]["PartitionState"] for e in result.entries] == ["running"]
    assert result.unreadable_systems == SKIPPED


def test_vios_state_filter_rejects_unknown_state(monkeypatch, mock_hmc):
    """The shared inventory boundary rejects values outside PartitionState."""
    _hmc_env(monkeypatch)
    route = mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/search/(PartitionState==no-match)"
    )

    with pytest.raises(ValueError, match="state must be one of"):
        hmc_list_vios(state="no-match")

    assert not route.called


def test_vios_system_and_state_filters_compose(monkeypatch, mock_hmc):
    """A system-scoped feed can also be filtered by PartitionState."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/VirtualIOServer").mock(
        return_value=httpx.Response(
            200,
            text=_feed(
                VIOS_UUID,
                "VirtualIOServer",
                PartitionName="vios1",
                PartitionState="not activated",
            ),
        )
    )

    result = hmc_list_vios(system_name_or_uuid=SYSTEM_UUID, state="running")

    assert result == FleetListing([], [])
