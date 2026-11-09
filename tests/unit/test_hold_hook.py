"""ADR 0193 hold hook at the partition-resolution sites (#1851, ADR 0327)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any, Self

import pytest

from hmcpctl import resource_identity
from hmcpctl.call_context import CallContext, calling, current_call
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCTransportError
from hmcpctl.operations.logical import engine, holds
from hmcpctl.operations.logical.engine import BodyResult, OperationRequest
from hmcpctl.operations.logical.holds import HoldRefused
from hmcpctl.operations.lpar.decommission import core as decommission
from hmcpctl.resource_identity import PartitionGuard, install_partition_guard
from hmcpctl.ssh import selectors

SYS = "0b3f9a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
LPAR = "7d1e2f3a-4b5c-4d6e-8f70-819a2b3c4d5e"
NAME = "lpar-a"
MUTATION = CallContext("hmc_modify_lpar", read=False, hold_id=None)


def test_ssh_selectors_do_not_import_the_logical_store():
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import hmcpctl.ssh.selectors; "
                "assert 'hmcpctl.operations.logical.holds' not in sys.modules; "
                "assert 'hmcpctl.operations.logical.store' not in sys.modules"
            ),
        ],
        check=True,
        timeout=20,
    )


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


def test_composing_application_preserves_explicit_library_guard(spy):
    from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
    from hmcpctl.server import TOOL_SECURITY, create_mcp

    policy = compile_legacy_policy(
        TOOL_SECURITY, ("<default>",), include_arbitrary_command=False
    )
    create_mcp(policy)
    resource_identity.guard_partition(LPAR)
    assert spy == [LPAR]


def test_cli_invocation_preserves_explicit_library_guard(spy):
    from typer.testing import CliRunner

    from hmcpctl.cli import app

    result = CliRunner().invoke(app, ["lpars", "--help"])
    assert result.exit_code == 0, result.output
    resource_identity.guard_partition(LPAR)
    assert spy == [LPAR]


def test_partition_guard_scope_restores_after_failure(spy):
    inner = []
    with (
        pytest.raises(RuntimeError, match="scope failed"),
        resource_identity.guarding_partitions(
            PartitionGuard(inner.append, lambda: False)
        ),
    ):
        resource_identity.guard_partition(LPAR)
        raise RuntimeError("scope failed")
    resource_identity.guard_partition(LPAR)
    assert inner == [LPAR]
    assert spy == [LPAR]


def test_concurrent_partition_guard_scopes_are_independent(spy):
    left, right = [], []

    async def run():
        entered = [asyncio.Event(), asyncio.Event()]

        async def check(index, seen):
            with resource_identity.guarding_partitions(
                PartitionGuard(seen.append, lambda: False)
            ):
                entered[index].set()
                await entered[1 - index].wait()
                resource_identity.guard_partition(LPAR)

        await asyncio.gather(check(0, left), check(1, right))
        resource_identity.guard_partition(LPAR)

    asyncio.run(run())
    assert left == right == spy == [LPAR]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", ["authorization", "handler"])
def test_authorized_call_scopes_guard_through_authorization_and_failure(
    spy, monkeypatch, asynchronous, failure
):
    from hmcpctl.tool_registry import ToolSecurity, authorized

    front = []
    monkeypatch.setattr(
        holds, "HOLD_GUARD", PartitionGuard(front.append, lambda: False)
    )

    def authorize(*args):
        resource_identity.guard_partition(LPAR)
        if failure == "authorization":
            raise PermissionError("authorization failed")

    def handler(lpar_name_or_uuid: str):
        resource_identity.guard_partition(lpar_name_or_uuid)
        raise RuntimeError("handler failed")

    async def async_handler(lpar_name_or_uuid: str):
        handler(lpar_name_or_uuid)

    wrapped = authorized(
        "hmc_test",
        ToolSecurity(effect="mutate", operation="lpar.modify", target_kind="lpar"),
        async_handler if asynchronous else handler,
        authorize,
    )

    async def run():
        error = PermissionError if failure == "authorization" else RuntimeError
        with pytest.raises(error, match=f"{failure} failed"):
            if asynchronous:
                await wrapped(LPAR)
            else:
                wrapped(LPAR)
        resource_identity.guard_partition(LPAR)
        assert current_call() is None

    asyncio.run(run())
    assert front == [LPAR] * (1 if failure == "authorization" else 2)
    assert spy == [LPAR]


def test_unresolved_guard_callback_cannot_allow_an_unidentified_name():
    seen = []
    guard = PartitionGuard(
        lambda _uuid: None,
        lambda: True,
        unresolved_name=lambda name, error: seen.append((name, error)),
    )
    install_partition_guard(guard)
    try:
        with pytest.raises(resource_identity.ResourceNotFoundError) as info:
            asyncio.run(selectors.resolve_ssh_names(FakeHMC(), "sys-R1", "unknown"))
        assert seen == [("unknown", info.value)]
    finally:
        install_partition_guard(None)


def test_unresolved_hold_guard_preserves_store_refusals(hold_guard, monkeypatch):
    from hmcpctl.operations.logical.store import OperationRefused

    _held()
    refusal = OperationRefused("store_unavailable", "inspect the operation store")

    async def resolve(*args, **kwargs):
        raise refusal

    monkeypatch.setattr(selectors, "resolve_lpar_uuid", resolve)
    with calling(MUTATION), pytest.raises(OperationRefused) as info:
        asyncio.run(selectors.resolve_ssh_names(FakeHMC(), "sys-R1", NAME))
    assert info.value is refusal


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


def test_a_handler_declaring_hold_id_keeps_it_and_sets_the_call_context():
    from hmcpctl.tool_registry import TargetSelector, ToolSecurity, authorized

    security = ToolSecurity(
        effect="mutate",
        operation="lpar.handoff",
        target_kind="lpar",
        targets=(TargetSelector("lpar", "lpar_name_or_uuid", True),),
    )

    def handler(
        lpar_name_or_uuid: str, hold_id: str | None = None, profile: str | None = None
    ):
        return hold_id, current_call()

    wrapped = authorized("hmc_test_handoff", security, handler, lambda *_: None)
    received, call = wrapped(LPAR, hold_id="h1")
    assert received == "h1"
    assert call == CallContext("hmc_test_handoff", read=False, hold_id="h1")
