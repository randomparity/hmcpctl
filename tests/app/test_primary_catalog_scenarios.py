"""Logical workflows over an MCP session that lists only the primary catalog (#1853).

Every scenario composes ``create_mcp(policy)`` with the default catalog, opens one
client session, and calls only names that session's ``tools/list`` returned; the
ADR 0189 D4 direct calls of unlisted specialists are marked ``direct``. Each scenario
asserts its exact number of ``tools/call`` requests. See the spec
docs/workflow/specs/2026-11-03-primary-catalog-listing-design.md, Scope 5.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest
from app.test_lpar_inspect_tool import _HMC as InspectHMC
from app.test_lpar_plan_tool import _HMC as PlanHMC
from app.test_lpar_plan_tool import VG, VIOS_A
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from lpar.test_decommission_tool import _client, _patch_common
from lpar.test_provision_install import (
    GROUP,
    INSTALL,
    MAC,
    SYSTEM,
    VIOS,
    FakeHMC,
    Writes,
)
from unit.test_lpar_power import FakeHMC as PowerHMC
from unit.test_lpar_reconfigure import hmc as reconfigure_hmc  # noqa: F401 - a fixture

from hmcpctl._app import CEILING_HEADING
from hmcpctl.authorization.access_policy import (
    DEFAULT_CONNECTION_TOKEN,
    AccessPolicy,
    compile_access_policy,
)
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.operations.logical import engine
from hmcpctl.operations.lpar import inspect as inspect_module
from hmcpctl.operations.lpar import power
from hmcpctl.operations.lpar.provisioning import operation as provision_operation
from hmcpctl.operations.lpar.provisioning.core import (
    ProvisionAdapters,
    ProvisionStorage,
)
from hmcpctl.operations.lpar.provisioning.operation import json_value
from hmcpctl.server import PRIMARY_TOOLS, TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import provision as provision_tool

TIER = "io.github.randomparity.hmcpctl/catalog-tier"
SEARCH, INVOKE, STATUS = "hmc_search_tools", "hmc_invoke_tool", "hmc_operation_status"
PROVISION, POWER = "hmc_provision_lpar", "hmc_power_lpar"
POWER_ON, POWER_OFF = "hmc_power_on_lpar", "hmc_power_off_lpar"
PERMISSIONS = "hmc_effective_permissions"
DEFAULT = DEFAULT_CONNECTION_TOKEN
AGENT = "hmcpctl"
OLD = {"lpar_name": "lpar-a", "time_stamp": "11/03/2026 10:00:00", "refcode": "OLD1"}
HANDOFF = {
    "lpar_name": "lpar-a",
    "time_stamp": "11/03/2026 10:00:09",
    "refcode": "CA00E105",
}
READ_PRIMARIES = {"hmc_inspect_lpar", "hmc_inventory", STATUS, "hmc_plan_lpar", SEARCH}


@pytest.fixture(autouse=True)
def environment_only_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep build_config off a developer's own TOML profiles (AGENTS.md, HMC_* leaks)."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.delenv("HMC_AGENT_ID", raising=False)


class Session:
    """One client session that counts its ``tools/call`` requests."""

    def __init__(self, client: Client, listed: list[Any]) -> None:
        self.client = client
        self.tools = listed
        self.listed = frozenset(tool.name for tool in listed)
        self.calls: list[str] = []

    async def call(self, name: str, arguments: dict, *, direct: bool = False) -> Any:
        """Call *name*; only an ADR 0189 D4 ``direct`` call may name an unlisted tool."""
        assert direct or name in self.listed, f"{name} is not in this session's listing"
        self.calls.append(name)
        return (await self.client.call_tool(name, arguments)).structured_content


@contextlib.asynccontextmanager
async def _session(app: FastMCP) -> AsyncIterator[Session]:
    async with Client(app) as client:
        yield Session(client, await client.list_tools())


def _legacy() -> AccessPolicy:
    return compile_legacy_policy(TOOL_SECURITY, (DEFAULT,))


def _policy(*grants: dict) -> AccessPolicy:
    return compile_access_policy(
        {"policies": {"test": {"grants": list(grants)}}},
        "test",
        TOOL_SECURITY,
        "test-primary-catalog-scenarios.toml",
    )


def _grant(*tools: str) -> dict:
    return {"tools": list(tools), "connections": [DEFAULT], "targets": "all-targets"}


def _opened(hmc: object) -> Any:
    @contextlib.asynccontextmanager
    async def opened(*_args: Any, **_kwargs: Any) -> AsyncIterator[object]:
        yield hmc

    return opened


def _serve(monkeypatch: pytest.MonkeyPatch, hmc: object) -> None:
    """Open *hmc* wherever a tool asks ``_app`` for a client."""
    monkeypatch.setattr("hmcpctl._app.client_from_env", _opened(hmc))


# --- provision fakes: the install boot of tests/lpar/test_provision_boot.py ----------


class BootHMC(FakeHMC):
    """A healthy partition, powered off, whose one VIOS maps its media and root disk."""

    def __init__(self) -> None:
        super().__init__()
        self.state = "not activated"

    async def get_logical_partition(self, _lpar: str) -> dict[str, Any]:
        return {"Resource": {"PartitionState": self.state}}

    async def list_vios(self, _system: str) -> list[dict[str, Any]]:
        own = [*await self.list_storage_mappings(VIOS), *self.optical]
        mappings = {"VirtualSCSIMappings": {"VirtualSCSIMapping": own}}
        return [{"UUID": VIOS.upper(), "Resource": mappings}]

    async def list_adapters(self, _lpar: str, _kind: str) -> list[dict[str, Any]]:
        resource = {"PortVLANID": "100", "MACAddress": MAC.replace(":", "")}
        return [{"UUID": "adapter-1", "Resource": resource}]


class Boot(Writes):
    """Provision's writes plus the boot: the handoff code is the first read after power-on."""

    def __init__(
        self, hmc: BootHMC, monkeypatch: pytest.MonkeyPatch, creation_mock
    ) -> None:
        super().__init__(hmc, monkeypatch, creation_mock)
        self.hmc: BootHMC = hmc
        patch = monkeypatch.setattr
        patch(provision_operation, "list_lpar_refcodes", self.read_refcodes)
        patch(provision_operation, "resolve_ssh_names", self.names)
        patch(provision_operation, "_power_on", self.power)
        patch(provision_operation, "read_lpar_boot_order", self.read_boot)

    async def names(self, _hmc: Any, _system: str, _lpar: str) -> tuple[str, str]:
        return "sys-R1", "lpar-a"

    async def read_refcodes(self, _config, _system, _lpar, count: int) -> list[dict]:
        self.calls.append("refcodes")
        rows = [HANDOFF, OLD] if "power_on" in self.calls else [OLD]
        return rows[:count]

    async def power(self, *_args: Any, **_kwargs: Any) -> dict:
        self.calls.append("power_on")
        self.hmc.state = "starting"
        return {"job_id": "job-1"}

    async def read_boot(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"pending_boot_string": " ".join(self.boot[-1]) if self.boot else ""}


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch, creation_mock) -> Boot:
    """Serve hmc_provision_lpar against a boot fake, bound where the tool builds its body."""
    monkeypatch.setenv("HMC_USER", "u")
    monkeypatch.setenv("HMC_PASSWORD", "p")
    hmc = BootHMC()
    writes = Boot(hmc, monkeypatch, creation_mock)
    body, classifiers = (
        provision_tool.provision_body,
        provision_tool.provision_classifiers,
    )

    def bound_body(system: str, request: Any, *, wait_seconds: int = 0) -> Any:
        return body(system, request, lambda _c, _host: hmc, wait_seconds=wait_seconds)

    def bound_classifiers(request: Any) -> Any:
        return classifiers(request, lambda _c, _host: hmc)

    monkeypatch.setattr(provision_tool, "provision_body", bound_body)
    monkeypatch.setattr(provision_tool, "provision_classifiers", bound_classifiers)
    monkeypatch.setattr(
        provision_tool, "preview_provision", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        provision_tool, "with_client", lambda fn, **_kw: asyncio.run(fn(hmc))
    )
    return writes


def _provision(request_id: str, boot_mode: str, **extra: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "system_name_or_uuid": SYSTEM,
        "name": "lpar-a",
        "adapters": json_value(ProvisionAdapters(100, MAC)),
        "storage": json_value(
            ProvisionStorage(VIOS, "lv-a", vg_uuid=GROUP, capacity_mib=1024)
        ),
        "install": json_value(INSTALL),
        "boot": boot_mode,
        "wait_seconds": 30,
        **extra,
    }


# --- S1, S1b: what the default listing shows ------------------------------------------


def test_s1_legacy_listing_is_the_primary_catalog():
    async def go() -> Session:
        async with _session(create_mcp(_legacy())) as session:
            return session

    session = asyncio.run(go())
    assert session.listed == PRIMARY_TOOLS
    assert {(tool.meta or {}).get(TIER) for tool in session.tools} == {"primary"}
    assert session.calls == []


def test_s1b_read_only_listing_and_a_found_specialist_called_directly():
    grant = {"effects": ["read"], "connections": [DEFAULT], "targets": "all-targets"}

    async def go() -> tuple[Session, dict, dict]:
        async with _session(create_mcp(_policy(grant))) as session:
            found = await session.call(SEARCH, {"name": PERMISSIONS})
            direct = await session.call(PERMISSIONS, {}, direct=True)
            return session, found, direct

    session, found, direct = asyncio.run(go())
    assert session.listed == READ_PRIMARIES
    assert INVOKE not in session.listed
    assert [entry["name"] for entry in found["tools"]] == [PERMISSIONS]
    assert PERMISSIONS in {tool["name"] for tool in direct["tools"]}
    assert session.calls == [SEARCH, PERMISSIONS]


def test_s1b_specialist_only_listing_is_empty_and_instructions_name_the_gateway():
    app = create_mcp(_policy(_grant("hmc_list_systems")))

    async def go() -> tuple[Session, str]:
        async with _session(app) as session:
            return session, session.client.instructions or ""

    session, instructions = asyncio.run(go())
    assert session.listed == frozenset()
    ceiling = instructions.split(CEILING_HEADING, 1)[1]
    withheld = ceiling.split("They cannot be called", 1)[0]
    assert SEARCH in withheld and INVOKE in withheld
    assert session.calls == []


# --- S2-S5: the lifecycle --------------------------------------------------------------


def test_s2_provision_boots_the_installer_and_status_reports_boot_started(boot: Boot):
    async def go() -> tuple[Session, dict, dict]:
        async with _session(create_mcp(_legacy())) as session:
            created = await session.call(
                PROVISION,
                _provision("s2", "immediate", exclusive_writer_window=True),
            )
            status = await session.call(STATUS, {"request_id": "s2"})
            return session, created, status

    session, created, status = asyncio.run(go())
    assert created["operation"]["outcome"] == "boot_started"
    (record,) = status["operations"]
    assert (record["state"], record["outcome"]) == ("terminal", "boot_started")
    assert "guest_ready" not in json.dumps(record)
    assert boot.calls.count("power_on") == 1
    assert session.calls == [PROVISION, STATUS]


@pytest.mark.usefixtures("reconfigure_hmc")
def test_s3_reconfigure_memory_completes_with_one_change():
    arguments = {
        "request_id": "s3",
        "lpar_name_or_uuid": "web1",
        "system_name_or_uuid": "sysA",
        "patch": {"memory_mib": 1024},
        "exclusive_writer_window": True,
        "wait_seconds": 10,
    }

    async def go() -> tuple[Session, dict]:
        async with _session(create_mcp(_legacy())) as session:
            return session, await session.call("hmc_reconfigure_lpar", arguments)

    session, record = asyncio.run(go())
    assert (record["state"], record["outcome"]) == ("terminal", "completed")
    assert len(record["result"]["changes"]) == 1
    assert session.calls == ["hmc_reconfigure_lpar"]


def test_s4_inspect_reads_rmc_and_refcodes(monkeypatch: pytest.MonkeyPatch):
    async def names(_config: Any, system: str, lpar: str) -> tuple[str, str]:
        return system, lpar

    async def refcodes(_config: Any, _system: str, lpar: str, _count: int) -> list:
        return [{"lpar_name": lpar, "time_stamp": "t", "refcode": "CA00E105"}]

    monkeypatch.setattr(inspect_module, "resolve_ssh_names", names)
    monkeypatch.setattr(inspect_module, "list_lpar_refcodes", refcodes)
    _serve(monkeypatch, InspectHMC())
    arguments = {
        "lpar_name_or_uuid": "web1",
        "system_name_or_uuid": "sysA",
        "include": ["rmc", "refcodes"],
    }

    async def go() -> tuple[Session, dict]:
        async with _session(create_mcp(_legacy())) as session:
            return session, await session.call("hmc_inspect_lpar", arguments)

    session, result = asyncio.run(go())
    assert result["rmc"]["source"]["status"] == "ok"
    assert result["refcodes"]["source"]["status"] == "ok"
    assert session.calls == ["hmc_inspect_lpar"]


def test_s5_decommission_retaining_storage_completes(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    _patch_common(monkeypatch, calls)
    hmc = _client()
    _serve(monkeypatch, hmc)
    monkeypatch.setattr(
        "hmcpctl.client.client_factory.HMCClient", lambda _config: _opened(hmc)()
    )
    arguments = {
        "request_id": "s5",
        "system_name_or_uuid": "system-a",
        "lpar_name_or_uuid": "aix-prod",
        "storage_cleanup": "retain",
        "poll_interval": 1,
        "wait_seconds": 30,
    }

    async def go() -> tuple[Session, dict]:
        async with _session(create_mcp(_legacy())) as session:
            return session, await session.call("hmc_decommission_lpar", arguments)

    session, result = asyncio.run(go())
    assert (result["operation"]["state"], result["operation"]["outcome"]) == (
        "terminal",
        "completed",
    )
    assert result["storage"] is None
    assert session.calls == ["hmc_decommission_lpar"]


# --- S6, S7: the gateway and policy -----------------------------------------------------


def test_s6_search_then_invoke_a_read_specialist():
    async def go() -> tuple[Session, dict, dict, dict, dict]:
        async with _session(create_mcp(_legacy())) as session:
            ranked = await session.call(SEARCH, {"query": "effective permissions"})
            named = await session.call(SEARCH, {"name": PERMISSIONS})
            invoked = await session.call(INVOKE, {"name": PERMISSIONS, "arguments": {}})
            direct = await session.call(PERMISSIONS, {}, direct=True)
            return session, ranked, named, invoked, direct

    session, ranked, named, invoked, direct = asyncio.run(go())
    assert PERMISSIONS in [entry["name"] for entry in ranked["tools"]]
    (entry,) = named["tools"]
    assert entry["name"] == PERMISSIONS and entry["input_schema"] is not None
    assert invoked == {"name": PERMISSIONS, "result": direct}
    assert session.calls == [SEARCH, SEARCH, INVOKE, PERMISSIONS]


def test_s7_a_withheld_specialist_denies_its_primary_and_is_invisible(
    monkeypatch: pytest.MonkeyPatch,
):
    hmc = PowerHMC()
    monkeypatch.setattr(power, "open_client", lambda _connection, _host: hmc)
    monkeypatch.setattr(power, "POLL_SECONDS", 0)
    app = create_mcp(_policy(_grant(SEARCH, INVOKE, POWER, POWER_OFF)))
    start = {
        "request_id": "s7",
        "lpar_name_or_uuid": "web1",
        "system_name_or_uuid": "sysA",
        "action": "start",
        "wait_seconds": 10,
    }

    async def refusal(session: Session, name: str, arguments: dict) -> str:
        with pytest.raises(ToolError) as error:
            await session.call(name, arguments)
        return str(error.value)

    async def go() -> tuple[Session, str, dict, str, str]:
        async with _session(app) as session:
            denied = await refusal(session, POWER, start)
            found = await session.call(SEARCH, {"name": POWER_ON})
            withheld = await refusal(
                session, INVOKE, {"name": POWER_ON, "arguments": {}}
            )
            unknown = await refusal(
                session, INVOKE, {"name": "hmc_no_such_tool", "arguments": {}}
            )
            return session, denied, found, withheld, unknown

    session, denied, found, withheld, unknown = asyncio.run(go())
    assert POWER_ON in denied
    assert hmc.submits == [] and hmc.listings == 0
    assert found["tools"] == []
    assert withheld == unknown
    assert session.calls == [POWER, SEARCH, INVOKE, INVOKE]


# --- S8, S9: refusals before any write ---------------------------------------------------


class TwoGroupsHMC(PlanHMC):
    """sysA's VIOS holds two volume groups that both fit the new disk."""

    async def list_volume_groups(self, vios_uuid: str) -> list[dict]:
        groups = await super().list_volume_groups(vios_uuid)
        if vios_uuid != VIOS_A:
            return groups
        second = {**groups[0], "UUID": VG.replace("a000", "a001")}
        return [*groups, second]


def test_s8_two_qualifying_pairs_are_an_ambiguous_blocker(
    monkeypatch: pytest.MonkeyPatch,
):
    hmc = TwoGroupsHMC()
    _serve(monkeypatch, hmc)
    arguments = {
        "name": "web1",
        "adapters": {"port_vlan_id": 100},
        "storage": {"storage_name": "web1_root", "capacity_mib": 20480},
        "system_name_or_uuid": "sysA",
    }

    async def go() -> tuple[Session, dict]:
        async with _session(create_mcp(_legacy())) as session:
            return session, await session.call("hmc_plan_lpar", arguments)

    session, plan = asyncio.run(go())
    codes = [blocker["code"] for blocker in plan["candidates"][0]["blockers"]]
    assert codes == ["ambiguous"]
    assert all(call.startswith(("list_", "get_")) for call in hmc.calls)
    assert session.calls == ["hmc_plan_lpar"]


@pytest.mark.parametrize("window", [None, False])
def test_s9_an_install_without_the_writer_window_records_nothing(
    boot: Boot, window: bool | None
):
    extra = {} if window is None else {"exclusive_writer_window": window}
    request_id = f"s9-{window}"

    async def go() -> tuple[Session, str]:
        async with _session(create_mcp(_legacy())) as session:
            with pytest.raises(ToolError) as error:
                await session.call(
                    PROVISION, _provision(request_id, "deferred", **extra)
                )
            return session, str(error.value)

    session, message = asyncio.run(go())
    assert "exclusive_writer_window" in message
    assert engine.recorded_arguments(AGENT, request_id, PROVISION) is None
    assert boot.calls == []
    assert session.calls == [PROVISION]


# --- S10, S11 ----------------------------------------------------------------------------


def test_s10_a_new_application_and_session_boots_a_deferred_provision(boot: Boot):
    async def first() -> tuple[Session, dict]:
        async with _session(create_mcp(_legacy())) as session:
            arguments = _provision("s10", "deferred", exclusive_writer_window=True)
            return session, await session.call(PROVISION, arguments)

    async def second() -> tuple[Session, dict]:
        async with _session(create_mcp(_legacy())) as session:
            arguments = {
                "request_id": "s10",
                "continuation": "boot",
                "exclusive_writer_window": True,
                "wait_seconds": 30,
            }
            return session, await session.call(PROVISION, arguments)

    created_session, created = asyncio.run(first())
    assert created["operation"]["outcome"] == "ready_to_boot"
    assert "power_on" not in boot.calls
    booted_session, booted = asyncio.run(second())
    assert booted["operation"]["outcome"] == "boot_started"
    assert boot.calls.count("power_on") == 1
    assert created_session.calls == [PROVISION]
    assert booted_session.calls == [PROVISION]


def test_s11_an_unlisted_permitted_specialist_runs_by_direct_call(
    monkeypatch: pytest.MonkeyPatch,
):
    hmc = PlanHMC()
    _serve(monkeypatch, hmc)

    async def go() -> tuple[Session, dict]:
        async with _session(create_mcp(_legacy())) as session:
            return session, await session.call("hmc_list_systems", {}, direct=True)

    session, result = asyncio.run(go())
    assert "hmc_list_systems" not in session.listed
    names = [entry["Resource"]["SystemName"] for entry in result["result"]]
    assert names == ["sysA", "sysB"]
    assert session.calls == ["hmc_list_systems"]


# --- payload -----------------------------------------------------------------------------


def _listing_bytes(catalog: Literal["primary", "registered"]) -> int:
    async def go() -> list[Any]:
        async with Client(create_mcp(_legacy(), catalog=catalog)) as client:
            return await client.list_tools()

    tools = asyncio.run(go())
    payload = json.dumps(
        [tool.model_dump(mode="json", exclude_none=True) for tool in tools],
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(payload.encode())


def test_the_primary_listing_payload_is_smaller_than_the_registered_one():
    assert _listing_bytes("primary") < _listing_bytes("registered")
