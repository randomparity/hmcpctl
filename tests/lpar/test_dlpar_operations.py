"""Async DLPAR processor and memory operations (issue #365, ADR 0094).

These exercise ``operations_lpar.set_lpar_processors`` /
``set_lpar_memory`` directly. Every test here is an ``async def`` running under
``pytest.mark.asyncio``, so the operation is called *from inside an already
running event loop* — the property issue #365 exists to establish, and the one
the pre-extraction ``asyncio.run`` tool bodies could not satisfy.

The field mapping and the whole-partition read-modify-write stay covered by
``test_lpar_rmw.py``; the MCP tool delegation is covered by ``test_power_tools.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from conftest import (
    LPAR_RESOURCE_CONFIG,
    live_response,
    make_config,
    mock_system_summaries,
    mock_system_summary,
)

from hmcpctl.audit import sink as audit_sink
from hmcpctl.client.client_resolution import MAX_PARENT_DISCOVERY_SYSTEMS
from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.dlpar import set_lpar_memory, set_lpar_processors

SYSTEM_UUID = "cccc0000-0000-0000-0000-000000000001"
OTHER_SYSTEM_UUID = "cccc0000-0000-0000-0000-000000000002"
LPAR_UUID = "aaaa0000-0000-0000-0000-000000000001"
COLLIDING_LPAR_UUID = "aaaa0000-0000-0000-0000-000000000002"
LPAR_NAME = "lpar1"
SYSTEM_NAME = "server1"

SYSTEM_ENTRY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{uuid}</id>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <SystemName>{name}</SystemName>
      <State>operating</State>
    </ManagedSystem>
  </content>
</entry>
"""

LPAR_ENTRY = (
    """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{uuid}</id>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <PartitionName>{name}</PartitionName>
"""
    + LPAR_RESOURCE_CONFIG
    + """\
      <PartitionState>running</PartitionState>
    </LogicalPartition>
  </content>
</entry>
"""
)


def _feed(*entries: str) -> str:
    """Wrap rendered Atom entries in the feed envelope ``parse_feed`` expects."""
    inner = "".join(
        entry.split("?>", 1)[1]
        .strip()
        .replace(' xmlns="http://www.w3.org/2005/Atom"', "", 1)
        for entry in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">' + inner + "</feed>"
    )


def _mock_lpar_detail(router, uuid: str = LPAR_UUID) -> None:
    """The ownership name read and the read-modify-write both GET this partition."""
    router.get(f"/rest/api/uom/LogicalPartition/{uuid}").mock(
        return_value=httpx.Response(
            200,
            text=LPAR_ENTRY.format(uuid=LPAR_UUID, name=LPAR_NAME),
            headers={"ETag": "etag-1"},
        )
    )


def _mock_system_detail(router) -> None:
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200, text=SYSTEM_ENTRY.format(uuid=SYSTEM_UUID, name=SYSTEM_NAME)
        )
    )
    mock_system_summary(router, SYSTEM_UUID, SYSTEM_NAME)
    _mock_partition_feed(router, SYSTEM_UUID, LPAR_UUID)


def _mock_partition_feed(router, system_uuid: str, *lpar_uuids: str) -> None:
    """The per-system feed both containment checks read."""
    router.get(f"/rest/api/uom/ManagedSystem/{system_uuid}/LogicalPartition").mock(
        return_value=httpx.Response(
            200,
            text=_feed(
                *(LPAR_ENTRY.format(uuid=uuid, name=LPAR_NAME) for uuid in lpar_uuids)
            ),
        )
    )


def _mock_fleet(router) -> None:
    """Bounded parent discovery: the fleet, then each system's partition feed."""
    _mock_empty_other_system(router)
    mock_system_summaries(
        router,
        (OTHER_SYSTEM_UUID, "server0", "operating"),
        (SYSTEM_UUID, SYSTEM_NAME, "operating"),
    )
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(
            200, text=_feed(LPAR_ENTRY.format(uuid=LPAR_UUID, name=LPAR_NAME))
        )
    )


def _mock_empty_other_system(router) -> None:
    """An operating system with no partitions: its empty feed reads its state."""
    router.get(
        f"/rest/api/uom/ManagedSystem/{OTHER_SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(200, text=_feed()))
    mock_system_summaries(router, (OTHER_SYSTEM_UUID, "server0", "operating"))


def _mock_modify(router, uuid: str = LPAR_UUID) -> httpx.Response:
    return router.post(f"/rest/api/uom/LogicalPartition/{uuid}").mock(
        return_value=httpx.Response(
            200, text=LPAR_ENTRY.format(uuid=LPAR_UUID, name=LPAR_NAME)
        )
    )


def _owned_by(agent_id: str) -> AsyncMock:
    return AsyncMock(return_value=f"[hmcpctl owner:{agent_id} created:2026-08-14]")


def _description(text: str) -> AsyncMock:
    return AsyncMock(return_value=text)


# ------------------------------------------------------------------ #
# Callable from inside a running event loop (the #365 acceptance criterion)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_set_lpar_processors_runs_inside_a_running_event_loop(mock_hmc):
    """The operation completes on a loop that is already running.

    ``asyncio.run`` raises ``RuntimeError`` here, so this passing at all is the
    proof that no nested ``asyncio.run`` remains on the path.
    """
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    route = _mock_modify(mock_hmc)

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=_owned_by("hmcpctl"),
    ):
        async with HMCClient(make_config()) as hmc:
            result = await set_lpar_processors(
                hmc,
                SYSTEM_UUID,
                LPAR_UUID,
                LparResources(desired_procs=1.5, desired_vcpus=3),
            )

    assert result["Resource"]["PartitionName"] == LPAR_NAME
    request = route.calls.last.request
    assert request.headers["If-Match"] == "etag-1"
    body = request.content.decode()
    assert "<DesiredProcessingUnits>1.5<" in body
    assert "<DesiredVirtualProcessors>3<" in body
    assert "<DesiredMemory>4096<" in body


@pytest.mark.asyncio
async def test_set_lpar_memory_runs_inside_a_running_event_loop(mock_hmc):
    """The memory operation completes on an already-running loop."""
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    route = _mock_modify(mock_hmc)

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=_owned_by("hmcpctl"),
    ):
        async with HMCClient(make_config()) as hmc:
            result = await set_lpar_memory(
                hmc,
                SYSTEM_UUID,
                LPAR_UUID,
                LparResources(desired_memory=8192, min_memory=1024, max_memory=16384),
            )

    assert result["Resource"]["PartitionName"] == LPAR_NAME
    request = route.calls.last.request
    assert request.headers["If-Match"] == "etag-1"
    body = request.content.decode()
    assert "<DesiredMemory>8192<" in body
    assert "<MinimumMemory>1024<" in body and "<MaximumMemory>16384<" in body
    assert "<DesiredProcessingUnits>0.5<" in body


# ------------------------------------------------------------------ #
# ADR 0092 §3.2: Reconfiguring — guarded unconditionally
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("operation", [set_lpar_processors, set_lpar_memory])
@pytest.mark.asyncio
async def test_foreign_owner_is_rejected_before_any_mutation(mock_hmc, operation):
    """A partition another agent owns is refused, and nothing is POSTed."""
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    route = _mock_modify(mock_hmc)

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description", new=_owned_by("bob")
    ):
        async with HMCClient(make_config(agent_id="alice")) as hmc:
            with pytest.raises(PermissionError, match="ownership_override=true"):
                await operation(
                    hmc,
                    SYSTEM_UUID,
                    LPAR_UUID,
                    LparResources(desired_procs=1.0, desired_memory=1024),
                )

    assert not route.called


@pytest.mark.parametrize("operation", [set_lpar_processors, set_lpar_memory])
@pytest.mark.asyncio
async def test_malformed_ownership_token_is_rejected(mock_hmc, operation):
    """A malformed hmcpctl stamp fails closed rather than reading as unowned."""
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    route = _mock_modify(mock_hmc)

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=_description("[hmcpctl owner:broken]"),
    ):
        async with HMCClient(make_config(agent_id="alice")) as hmc:
            with pytest.raises(PermissionError, match="ownership_override=true"):
                await operation(
                    hmc,
                    SYSTEM_UUID,
                    LPAR_UUID,
                    LparResources(desired_procs=1.0, desired_memory=1024),
                )

    assert not route.called


@pytest.mark.parametrize("operation", [set_lpar_processors, set_lpar_memory])
@pytest.mark.asyncio
async def test_ownership_override_bypasses_the_guard_without_reading(
    mock_hmc, operation
):
    """ADR 0092 §5: the per-call override skips the token read and mutates."""
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    route = _mock_modify(mock_hmc)
    read = _owned_by("bob")

    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        async with HMCClient(make_config(agent_id="alice")) as hmc:
            await operation(
                hmc,
                SYSTEM_UUID,
                LPAR_UUID,
                LparResources(desired_procs=1.0, desired_memory=1024),
                ownership_override=True,
            )

    read.assert_not_awaited()
    assert route.called


@pytest.mark.parametrize("operation", [set_lpar_processors, set_lpar_memory])
@pytest.mark.asyncio
async def test_an_unstamped_partition_is_allowed(mock_hmc, operation):
    """ADR 0011 is advisory: a partition with no token is not foreign-owned."""
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    route = _mock_modify(mock_hmc)

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=_description("legacy partition"),
    ):
        async with HMCClient(make_config(agent_id="alice")) as hmc:
            await operation(
                hmc,
                SYSTEM_UUID,
                LPAR_UUID,
                LparResources(desired_procs=1.0, desired_memory=1024),
            )

    assert route.called


# ------------------------------------------------------------------ #
# ADR 0094: the guard's system selector when the caller omits one
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_omitted_system_is_discovered_and_named_to_the_guard(mock_hmc):
    """With no selector the owning system is found and passed to the guard."""
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    _mock_fleet(mock_hmc)
    route = _mock_modify(mock_hmc)
    read = _owned_by("hmcpctl")

    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        async with HMCClient(make_config()) as hmc:
            await set_lpar_processors(
                hmc, None, LPAR_UUID, LparResources(desired_procs=1.0)
            )

    assert route.called
    # The guard reads the token by CLI names, so the discovered system must be
    # the one that actually contains the partition — not merely any system.
    assert read.await_args.args[1:] == (SYSTEM_NAME, LPAR_NAME)


@pytest.mark.asyncio
async def test_a_supplied_system_skips_fleet_discovery(mock_hmc):
    """The discovery cost lands only on the omitted-selector path."""
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    _mock_fleet(mock_hmc)
    _mock_modify(mock_hmc)

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=_owned_by("hmcpctl"),
    ):
        async with HMCClient(make_config()) as hmc:
            await set_lpar_processors(
                hmc,
                SYSTEM_UUID,
                LPAR_UUID,
                LparResources(desired_procs=1.0),
            )

    paths = [call.request.url.path for call in mock_hmc.calls]
    assert "/rest/api/uom/ManagedSystem" not in paths
    assert (
        f"/rest/api/uom/ManagedSystem/{OTHER_SYSTEM_UUID}/LogicalPartition" not in paths
    )


@pytest.mark.parametrize("operation", [set_lpar_processors, set_lpar_memory])
@pytest.mark.asyncio
async def test_an_override_without_a_selector_skips_discovery_entirely(
    mock_hmc, operation
):
    """ADR 0092 §4: the override is blocked by no part of the fleet walk.

    Discovery exists only to feed the guard, so an approved override must not
    depend on it. No fleet route is registered, so respx fails this test if the
    walk runs at all; the partition read that names the audit record and the
    read-modify-write's own read are the only GETs allowed.
    """
    _mock_lpar_detail(mock_hmc)
    route = _mock_modify(mock_hmc)
    read = _owned_by("bob")

    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        async with HMCClient(make_config(agent_id="alice")) as hmc:
            await operation(
                hmc,
                None,
                LPAR_UUID,
                LparResources(desired_procs=1.0, desired_memory=1024),
                ownership_override=True,
            )

    read.assert_not_awaited()
    assert route.called
    assert [
        call.request.url.path for call in mock_hmc.calls if call.request.method == "GET"
    ] == [f"/rest/api/uom/LogicalPartition/{LPAR_UUID}"] * 2


@pytest.mark.asyncio
async def test_an_override_audits_one_partition_and_the_caller_s_selector(
    mock_hmc, caplog
):
    """The bypass record names the partition as every other record does.

    A partition name keeps the ``lpar`` field in one vocabulary across
    operations; the empty ``system`` records that the caller named no system,
    which is what the operator actually asserted.
    """
    _mock_lpar_detail(mock_hmc)
    _mock_modify(mock_hmc)

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description", new=AsyncMock()
        ),
        caplog.at_level(logging.WARNING),
    ):
        async with HMCClient(make_config(agent_id="alice")) as hmc:
            await set_lpar_processors(
                hmc,
                None,
                LPAR_UUID,
                LparResources(desired_procs=1.0),
                ownership_override=True,
            )

    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == audit_sink.AUDIT_LOGGER_NAME
    ]
    assert len(records) == 1, (
        "an absence assertion over an empty capture proves nothing"
    )
    assert records[0]["event"] == "ownership-override"
    assert records[0]["lpar"] == LPAR_NAME
    assert records[0]["system"] == ""


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.asyncio
async def test_a_blank_system_selector_is_read_as_absent(mock_hmc, blank):
    """An MCP client that serialises an unset optional as "" keeps working.

    Before the extraction the tool passed the blank straight to
    ``resolve_lpar_uuid``, which ignored it for a partition given by UUID.
    Treating it as a real selector would resolve a managed system named ""
    and fail a call shape that worked.
    """
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    _mock_fleet(mock_hmc)
    route = _mock_modify(mock_hmc)
    read = _owned_by("hmcpctl")

    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        async with HMCClient(make_config()) as hmc:
            await set_lpar_processors(
                hmc,
                blank,
                LPAR_UUID,
                LparResources(desired_procs=1.0),
            )

    assert route.called
    assert read.await_args.args[1:] == (SYSTEM_NAME, LPAR_NAME)


@pytest.mark.asyncio
async def test_an_upper_case_partition_uuid_still_matches_the_hmc_feed(mock_hmc):
    """``is_uuid`` admits upper-case hex; the HMC renders UUIDs lower-case.

    Both containment checks compare a caller-supplied UUID against HMC output,
    so a case difference must not turn a working call into a failure whose only
    escape is bypassing the ownership control.
    """
    _mock_lpar_detail(mock_hmc, LPAR_UUID.upper())
    _mock_system_detail(mock_hmc)
    route = _mock_modify(mock_hmc, LPAR_UUID.upper())
    read = _owned_by("hmcpctl")

    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        async with HMCClient(make_config()) as hmc:
            await set_lpar_processors(
                hmc,
                SYSTEM_UUID,
                LPAR_UUID.upper(),
                LparResources(desired_procs=1.0),
            )

    assert route.called


@pytest.mark.asyncio
async def test_an_upper_case_partition_uuid_is_discoverable(mock_hmc):
    """The same normalisation holds on the fleet walk."""
    _mock_lpar_detail(mock_hmc, LPAR_UUID.upper())
    _mock_system_detail(mock_hmc)
    _mock_fleet(mock_hmc)
    route = _mock_modify(mock_hmc, LPAR_UUID.upper())
    read = _owned_by("hmcpctl")

    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        async with HMCClient(make_config()) as hmc:
            await set_lpar_processors(
                hmc, None, LPAR_UUID.upper(), LparResources(desired_procs=1.0)
            )

    assert route.called
    assert read.await_args.args[1:] == (SYSTEM_NAME, LPAR_NAME)


@pytest.mark.asyncio
async def test_discovery_reports_frames_it_could_not_read(mock_hmc):
    """A skipped frame is named in the failure, not silently absorbed.

    Skipping never widens what may be mutated — the walk only returns on a
    positive UUID match — but a degraded fleet must read as degraded rather
    than as an absent partition.
    """
    _mock_lpar_detail(mock_hmc)
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"SystemName": SYSTEM_NAME, "State": "operating"},
                {
                    "UUID": OTHER_SYSTEM_UUID,
                    "SystemName": "server0",
                    "State": "operating",
                },
            ],
        )
    )
    mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{OTHER_SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(500))
    route = _mock_modify(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="2 could not be read") as exc_info:
            await set_lpar_processors(
                hmc, None, LPAR_UUID, LparResources(desired_procs=1.0)
            )

    message = str(exc_info.value)
    assert "incomplete inventory metadata" in message
    assert OTHER_SYSTEM_UUID in message
    assert "supply managed-system scope" in message
    assert not route.called


@pytest.mark.asyncio
async def test_an_unhealthy_frame_does_not_block_a_healthy_one(mock_hmc):
    """A frame that errors ahead of the owner is skipped, not fatal.

    Before ADR 0094 the selector-less call touched no frame but the partition's
    own; a walk that aborted on the first unhealthy frame would make every
    DLPAR call hostage to an unrelated system's health, ordered by inventory.
    """
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    mock_system_summaries(
        mock_hmc,
        (OTHER_SYSTEM_UUID, "server0", "operating"),
        (SYSTEM_UUID, SYSTEM_NAME, "operating"),
    )
    other = mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{OTHER_SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(500))
    route = _mock_modify(mock_hmc)
    read = _owned_by("hmcpctl")

    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        async with HMCClient(make_config()) as hmc:
            await set_lpar_processors(
                hmc, None, LPAR_UUID, LparResources(desired_procs=1.0)
            )

    assert other.called
    assert route.called
    assert read.await_args.args[1:] == (SYSTEM_NAME, LPAR_NAME)


@pytest.mark.asyncio
async def test_discovery_rejects_an_oversized_fleet(mock_hmc):
    """The fan-out cap names the fleet size, not a partition-name ambiguity."""
    _mock_lpar_detail(mock_hmc)
    mock_system_summaries(
        mock_hmc,
        *(
            (f"cccc0000-0000-0000-0000-{index:012d}", f"server{index}", "operating")
            for index in range(MAX_PARENT_DISCOVERY_SYSTEMS + 1)
        ),
    )
    route = _mock_modify(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="discovery exceeds") as exc_info:
            await set_lpar_processors(
                hmc, None, LPAR_UUID, LparResources(desired_procs=1.0)
            )

    assert "ambiguous" not in str(exc_info.value)
    assert "supply managed-system scope" in str(exc_info.value)
    assert not route.called


@pytest.mark.asyncio
async def test_discovery_translates_its_own_timeout(mock_hmc, monkeypatch):
    """A slow fleet reports the timeout and the remedy, not a bare CancelledError.

    The delay sits on the HMC boundary rather than on a zero-second deadline,
    which would race the mock transport and flake either way.
    """
    _mock_lpar_detail(mock_hmc)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.PARENT_DISCOVERY_TIMEOUT_SECONDS", 0.01
    )

    async def _slow_fleet(request):
        await asyncio.sleep(1)
        raise AssertionError("the deadline should have fired first")

    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(side_effect=_slow_fleet)
    route = _mock_modify(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="parent discovery timed out"):
            await set_lpar_processors(
                hmc, None, LPAR_UUID, LparResources(desired_procs=1.0)
            )

    assert not route.called


@pytest.mark.parametrize("operation", [set_lpar_processors, set_lpar_memory])
@pytest.mark.asyncio
async def test_a_partition_uuid_off_the_selected_system_is_rejected(
    mock_hmc, operation
):
    """The guard never reads a token from a system the partition does not live on.

    ``resolve_lpar_uuid`` passes a canonical UUID straight through, so nothing
    upstream pairs the selector with the partition. Left unchecked, a
    cross-system partition-name collision would let the guard approve against a
    different partition's token.
    """
    _mock_lpar_detail(mock_hmc)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{OTHER_SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200, text=SYSTEM_ENTRY.format(uuid=OTHER_SYSTEM_UUID, name="server0")
        )
    )
    mock_system_summary(mock_hmc, OTHER_SYSTEM_UUID, "server0")
    # server0 hosts a *different* partition that happens to share lpar1's name.
    _mock_partition_feed(mock_hmc, OTHER_SYSTEM_UUID, COLLIDING_LPAR_UUID)
    route = _mock_modify(mock_hmc)
    read = _owned_by("hmcpctl")

    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        async with HMCClient(make_config()) as hmc:
            with pytest.raises(ValueError, match="does not belong to managed system"):
                await operation(
                    hmc,
                    OTHER_SYSTEM_UUID,
                    LPAR_UUID,
                    LparResources(desired_procs=1.0, desired_memory=1024),
                )

    read.assert_not_awaited()
    assert not route.called


@pytest.mark.asyncio
async def test_an_unknown_partition_uuid_never_reaches_the_fleet_walk(mock_hmc):
    """``is_uuid`` is a format check, so existence is confirmed before the walk.

    Without the precheck one caller-supplied string that names no partition
    drives up to a hundred full partition-feed reads before failing — cheap,
    caller-controlled amplification against a capacity-limited appliance.
    Registering no fleet route is the assertion.
    """
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=live_response("rest-lpar-not-found")[1]
    )
    route = _mock_modify(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as info:
            await set_lpar_processors(
                hmc, None, LPAR_UUID, LparResources(desired_procs=1.0)
            )

    # The 404 names the partition the caller got wrong. Accepting any of several
    # exception types here would keep passing if the precheck silently moved
    # back behind the walk, which is the regression this guards.
    assert info.value.status_code == 404
    assert LPAR_UUID in str(info.value)
    assert [
        call.request.url.path for call in mock_hmc.calls if call.request.method == "GET"
    ] == [f"/rest/api/uom/LogicalPartition/{LPAR_UUID}"]
    assert not route.called


@pytest.mark.asyncio
async def test_a_nameless_partition_is_rejected_before_the_fleet_walk(mock_hmc):
    """A resource that parses but names no partition also stops before the walk."""
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<entry xmlns="http://www.w3.org/2005/Atom">'
                f"<id>urn:uuid:{LPAR_UUID}</id>"
                '<content type="application/vnd.ibm.powervm.uom+xml">'
                '<LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/'
                'firmware/uom/mc/2012_10/"><PartitionState>running</PartitionState>'
                "</LogicalPartition></content></entry>"
            ),
        )
    )
    route = _mock_modify(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="No LPAR"):
            await set_lpar_processors(
                hmc, None, LPAR_UUID, LparResources(desired_procs=1.0)
            )

    assert not route.called


@pytest.mark.parametrize("operation", [set_lpar_processors, set_lpar_memory])
@pytest.mark.asyncio
async def test_a_partition_name_is_not_re_read_for_containment(mock_hmc, operation):
    """A name resolved against the system's feed needs no second read of it.

    ``find_partition_by_name`` scoped to the system *is* that feed, so the
    containment question is already answered; re-fetching the largest payload
    in the guarded chain would answer it twice.
    """
    _mock_lpar_detail(mock_hmc)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200, text=SYSTEM_ENTRY.format(uuid=SYSTEM_UUID, name=SYSTEM_NAME)
        )
    )
    mock_system_summary(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    feed = mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition"
    ).mock(
        return_value=httpx.Response(
            200, text=_feed(LPAR_ENTRY.format(uuid=LPAR_UUID, name=LPAR_NAME))
        )
    )
    route = _mock_modify(mock_hmc)

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=_owned_by("hmcpctl"),
    ):
        async with HMCClient(make_config()) as hmc:
            await operation(
                hmc,
                SYSTEM_UUID,
                LPAR_NAME,
                LparResources(desired_procs=1.0, desired_memory=1024),
            )

    assert route.called
    assert feed.call_count == 1


@pytest.mark.parametrize("operation", [set_lpar_processors, set_lpar_memory])
@pytest.mark.asyncio
async def test_an_unreadable_feed_carries_its_error(mock_hmc, operation):
    """An unreadable containment feed refuses with the feed error, not advice."""
    _mock_lpar_detail(mock_hmc)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200, text=SYSTEM_ENTRY.format(uuid=SYSTEM_UUID, name=SYSTEM_NAME)
        )
    )
    mock_system_summary(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(500)
    )
    route = _mock_modify(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="HTTP 500") as info:
            await operation(
                hmc,
                SYSTEM_UUID,
                LPAR_UUID,
                LparResources(desired_procs=1.0, desired_memory=1024),
            )

    assert info.value.status_code == 500
    assert "retry" not in str(info.value)
    assert not route.called


@pytest.mark.asyncio
async def test_an_unavailable_fleet_inventory_names_the_operator_remedy(mock_hmc):
    """A firmware-fragile inventory read still points at the selector.

    A selector-less DLPAR call reads the managed-system inventory where it
    previously read nothing — so the one degraded dependency the selector
    actually fixes must not be the one failure that never mentions it.
    """
    _mock_lpar_detail(mock_hmc)
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(503)
    )
    route = _mock_modify(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="supply managed-system scope") as info:
            await set_lpar_processors(
                hmc, None, LPAR_UUID, LparResources(desired_procs=1.0)
            )

    assert "managed-system inventory is unavailable" in str(info.value)
    assert isinstance(info.value.__cause__, HMCError)
    assert not route.called


@pytest.mark.asyncio
async def test_undiscoverable_system_names_the_operator_remedy(mock_hmc):
    """No owning system found: the error names the selector that fixes it."""
    _mock_lpar_detail(mock_hmc)
    mock_system_summaries(mock_hmc, (OTHER_SYSTEM_UUID, "server0", "operating"))
    _mock_empty_other_system(mock_hmc)
    route = _mock_modify(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="supply managed-system scope"):
            await set_lpar_processors(
                hmc, None, LPAR_UUID, LparResources(desired_procs=1.0)
            )

    assert not route.called


# ------------------------------------------------------------------ #
# Error translation
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("operation", [set_lpar_processors, set_lpar_memory])
@pytest.mark.asyncio
async def test_http_406_is_translated_to_an_actionable_error(mock_hmc, operation):
    """The extraction keeps the tool-body 406 hint on the operation."""
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(406)
    )

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=_owned_by("hmcpctl"),
    ):
        async with HMCClient(make_config()) as hmc:
            with pytest.raises(HMCError, match="X-HMC-Schema-Version") as exc_info:
                await operation(
                    hmc,
                    SYSTEM_UUID,
                    LPAR_UUID,
                    LparResources(desired_procs=1.0, desired_memory=1024),
                )

    assert "HMC_SCHEMA_VERSION" not in str(exc_info.value)


# ------------------------------------------------------------------ #
# Which object the write changed (#1170)
# ------------------------------------------------------------------ #


def _mock_modify_with_sync(router, sync: str) -> None:
    """The POST response as captured live: it reports the partition's sync setting."""
    entry = LPAR_ENTRY.format(uuid=LPAR_UUID, name=LPAR_NAME).replace(
        "<PartitionState>",
        f"<CurrentProfileSync>{sync}</CurrentProfileSync><PartitionState>",
    )
    router.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=entry)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "resources"),
    [
        (set_lpar_memory, LparResources(desired_memory=3072)),
        (set_lpar_processors, LparResources(desired_procs=0.3)),
    ],
)
async def test_a_disabled_sync_warns_that_the_profile_is_unchanged(
    mock_hmc, operation, resources
):
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    _mock_modify_with_sync(mock_hmc, "Disabled")

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=_owned_by("hmcpctl"),
    ):
        async with HMCClient(make_config()) as hmc:
            result = await operation(hmc, SYSTEM_UUID, LPAR_UUID, resources)

    assert result["change_location"]["lives_in"] == "current-configuration"
    assert result["change_location"]["current_profile_sync"] == "Disabled"
    (warning,) = result["warnings"]
    assert "only in the current configuration" in warning
    assert "discards it" in warning


@pytest.mark.asyncio
async def test_an_on_sync_carries_no_warning(mock_hmc):
    _mock_lpar_detail(mock_hmc)
    _mock_system_detail(mock_hmc)
    _mock_modify_with_sync(mock_hmc, "On")

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=_owned_by("hmcpctl"),
    ):
        async with HMCClient(make_config()) as hmc:
            result = await set_lpar_memory(
                hmc, SYSTEM_UUID, LPAR_UUID, LparResources(desired_memory=3072)
            )

    assert result["change_location"]["lives_in"] == "current-configuration-and-profile"
    assert "warnings" not in result
