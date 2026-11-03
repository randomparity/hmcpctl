"""ADR 0193 hold hook at the partition-resolution sites (#1851, ADR 0327)."""

from __future__ import annotations

import asyncio
from typing import Any, Self

import pytest

from hmcpctl import resource_identity
from hmcpctl.call_context import CallContext, calling, current_call
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCTransportError
from hmcpctl.operations.logical import engine, holds
from hmcpctl.operations.logical.engine import BodyResult, OperationRequest
from hmcpctl.operations.logical.holds import HoldRefused
from hmcpctl.operations.lpar import decommission
from hmcpctl.resource_identity import PartitionGuard, install_partition_guard
from hmcpctl.ssh import selectors

SYS = "0b3f9a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
LPAR = "7d1e2f3a-4b5c-4d6e-8f70-819a2b3c4d5e"
NAME = "lpar-a"
MUTATION = CallContext("hmc_modify_lpar", read=False, hold_id=None)


class FakeHMC:
    """Answers the partition reads the resolvers make and records them."""

    def __init__(self, *, transport_down: bool = False) -> None:
        self.reads: list[str] = []
        self.transport_down = transport_down
        self.config = HMCConfig.from_mapping({"host": "hmc.test", "user": "u"})

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def find_partition_by_name(
        self, name: str, *, system_uuid: str | None = None
    ) -> dict[str, Any] | None:
        self.reads.append(f"find:{name}")
        if self.transport_down:
            raise HMCTransportError("connection refused")
        return {"UUID": LPAR} if name == NAME else None

    async def list_logical_partitions(self, system_uuid: str) -> list[dict[str, Any]]:
        self.reads.append(f"list:{system_uuid}")
        return [{"UUID": LPAR, "Resource": {"PartitionName": NAME}}]

    async def list_system_summaries(self) -> list[dict[str, Any]]:
        return [{"UUID": SYS, "SystemName": "sys-R1"}]

    async def get_logical_partition(self, lpar_uuid: str) -> dict[str, Any]:
        return {"Resource": {"PartitionName": NAME}}


@pytest.fixture
def spy():
    seen: list[str] = []
    install_partition_guard(PartitionGuard(seen.append, lambda: True))
    yield seen
    install_partition_guard(None)


@pytest.fixture
def hold_guard():
    install_partition_guard(holds.HOLD_GUARD)
    yield
    install_partition_guard(None)


def _held() -> holds.Hold:
    return holds.place_hold(
        SYS, LPAR, consumer_label="kdive", agent_id="a", connection="c"
    )


@pytest.mark.parametrize("selector", [LPAR, NAME])
@pytest.mark.parametrize("system", [None, SYS])
def test_resolve_lpar_uuid_runs_the_guard_on_its_result(spy, selector, system):
    resolved = asyncio.run(
        resource_identity.resolve_lpar_uuid(
            FakeHMC(), selector, system_name_or_uuid=system
        )
    )
    assert resolved == LPAR
    assert spy == [LPAR]


def test_no_installed_guard_checks_nothing():
    _held()
    with calling(MUTATION):
        assert asyncio.run(resource_identity.resolve_lpar_uuid(FakeHMC(), LPAR)) == LPAR


def test_decommission_target_resolution_runs_the_guard(spy):
    child = asyncio.run(decommission.resolve_target_lpar(FakeHMC(), SYS, NAME))
    assert child["UUID"] == LPAR
    assert spy == [LPAR]


def test_ssh_uuid_selector_runs_the_guard_before_any_read(hold_guard):
    _held()
    hmc = FakeHMC()
    with calling(MUTATION), pytest.raises(HoldRefused) as info:
        asyncio.run(selectors.resolve_ssh_names(hmc, "sys-R1", LPAR))
    assert info.value.reason == "partition_held"
    assert hmc.reads == []


def test_ssh_name_selector_resolves_the_name_while_holds_exist(hold_guard, monkeypatch):
    _held()
    hmc = FakeHMC()
    monkeypatch.setattr(selectors, "HMCClient", lambda _config: hmc)
    config = HMCConfig.from_mapping({"host": "hmc.test", "user": "u"})
    with calling(MUTATION), pytest.raises(HoldRefused) as info:
        asyncio.run(selectors.resolve_ssh_names(config, "sys-R1", NAME))
    assert info.value.reason == "partition_held"
    assert f"find:{NAME}" in hmc.reads


def test_ssh_name_selector_refuses_when_rest_cannot_identify_it(
    hold_guard, monkeypatch
):
    _held()
    monkeypatch.setattr(selectors, "HMCClient", lambda _c: FakeHMC(transport_down=True))
    config = HMCConfig.from_mapping({"host": "hmc.test", "user": "u"})
    for name in (NAME, "lpar-unknown"):
        with calling(MUTATION), pytest.raises(HoldRefused) as info:
            asyncio.run(selectors.resolve_ssh_names(config, "sys-R1", name))
        assert info.value.reason == "partition_hold_unverified"


def test_ssh_name_selector_makes_no_read_without_holds(hold_guard):
    hmc = FakeHMC()
    with calling(MUTATION):
        names = asyncio.run(selectors.resolve_ssh_names(hmc, "sys-R1", NAME))
    assert names == ("sys-R1", NAME)
    assert hmc.reads == []


def test_ssh_name_selector_skips_reads_for_read_calls(hold_guard):
    _held()
    hmc = FakeHMC()
    with calling(CallContext("hmc_get_lpar_settings", read=True, hold_id=None)):
        asyncio.run(selectors.resolve_ssh_names(hmc, "sys-R1", NAME))
    assert hmc.reads == []


def test_matching_hold_id_passes_every_site(hold_guard):
    placed = _held()
    with calling(CallContext("hmc_modify_lpar", read=False, hold_id=placed.hold_id)):
        assert asyncio.run(resource_identity.resolve_lpar_uuid(FakeHMC(), NAME)) == LPAR
        asyncio.run(selectors.resolve_ssh_names(FakeHMC(), "sys-R1", NAME))


def test_logical_worker_runs_in_the_submitting_call():
    async def body(_ctx):
        call = current_call()
        return BodyResult("completed", {"tool": call.tool, "hold_id": call.hold_id})

    request = OperationRequest(
        "hmc_power_lpar", "agent-a", "<default>", "hmc.test", "r1", {}
    )
    with calling(CallContext("hmc_power_lpar", read=False, hold_id="h1")):
        record = engine.submit(request, body, wait_seconds=10)
    assert record.result == {"tool": "hmc_power_lpar", "hold_id": "h1"}
