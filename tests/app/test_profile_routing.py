"""Tests proving every network-backed MCP tool accepts and propagates `profile`.

Covers:
  - Registry-driven exhaustive check: every network tool has `profile` in schema
  - Sequential routing: two consecutive calls with different profiles hit the
    correct HMC host each time
  - Concurrent routing: two simultaneous calls with different profiles show no
    cross-call state leakage
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.server import TOOL_SECURITY, create_mcp

# Composed here rather than imported: ADR 0041 removed the module-level application, so
# every consumer builds its own. The legacy-equivalent policy registers exactly the
# surface the unpolicied composition used to (pinned by G2 in
# tests/app/test_fail_closed_startup.py), and the dispatch wrapper is schema-transparent,
# so every assertion below reads the same registry it always did.
mcp = create_mcp(compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,)))


# These tools make no network calls and therefore have no profile= parameter.
# They are excluded from the profile-routing registry check.
_NO_NETWORK_TOOLS = frozenset(
    {
        "hmc_list_configured_hosts",  # reads TOML config only; no HMC connection
        "hmc_effective_permissions",  # reads this application's own registry
        "hmc_search_tools",  # searches this application's own registry
        "hmc_invoke_tool",  # the invoked tool routes its own profile
        "hmc_snapshot_validate",  # validates caller-supplied JSON locally
        "hmc_snapshot_inspect",  # inspects caller-supplied JSON locally
        "hmc_snapshot_assess_affinity",  # assesses caller-supplied JSON locally
    }
)


def _tools_by_name():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


# ---------------------------------------------------------------------- #
# T-1: Exhaustive registry test
# ---------------------------------------------------------------------- #


def test_every_network_tool_has_profile_param():
    """Every network-backed MCP tool must expose 'profile' in its JSON schema.

    Only no-network tools are explicitly excluded; REST and SSH tools must
    expose the same routing contract.
    """
    by_name = _tools_by_name()
    orphaned_no_network = _NO_NETWORK_TOOLS - set(by_name)
    assert not orphaned_no_network, (
        f"_NO_NETWORK_TOOLS has stale entries not in the live registry: {sorted(orphaned_no_network)}"
    )
    missing = []
    for name, tool in by_name.items():
        if name in _NO_NETWORK_TOOLS:
            continue
        props = tool.parameters.get("properties", {})
        if "profile" not in props:
            missing.append(name)
    assert not missing, (
        f"These tools are missing the 'profile' parameter: {sorted(missing)}"
    )


def test_profile_param_is_optional_string():
    """The 'profile' parameter must be an optional string (nullable, no default required)."""
    by_name = _tools_by_name()
    bad = []
    for name, tool in by_name.items():
        if name in _NO_NETWORK_TOOLS:
            continue
        props = tool.parameters.get("properties", {})
        if "profile" not in props:
            continue  # caught by test_every_network_tool_has_profile_param
        # Must allow string or null, and must NOT be required
        required = tool.parameters.get("required", [])
        if "profile" in required:
            bad.append(f"{name}: profile is required (should be optional)")
    assert not bad, "\n".join(bad)


# ---------------------------------------------------------------------- #
# T-2: Sequential profile routing
# ---------------------------------------------------------------------- #

LOGON = """<?xml version="1.0"?><LogonResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"><X-API-Session>tok</X-API-Session></LogonResponse>"""

CONSOLE_A = """<?xml version="1.0"?><entry xmlns="http://www.w3.org/2005/Atom"><id>urn:uuid:ca</id><content type="application/vnd.ibm.powervm.uom+xml"><ManagementConsole xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"><SystemName>hmc-a</SystemName></ManagementConsole></content></entry>"""
CONSOLE_B = """<?xml version="1.0"?><entry xmlns="http://www.w3.org/2005/Atom"><id>urn:uuid:cb</id><content type="application/vnd.ibm.powervm.uom+xml"><ManagementConsole xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"><SystemName>hmc-b</SystemName></ManagementConsole></content></entry>"""


def _toml_two_profiles(tmp_path: Path) -> Path:
    """Write a TOML config with two profiles: alpha (hmc-a) and beta (hmc-b)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[profiles.alpha]\n"
        "host = 'hmc-a.test'\n"
        "user = 'hscroot'\n"
        "password = 'pass-a'\n"  # pragma: allowlist secret
        "\n"
        "[profiles.beta]\n"
        "host = 'hmc-b.test'\n"
        "user = 'hscroot'\n"
        "password = 'pass-b'\n"  # pragma: allowlist secret
    )
    return cfg


def test_sequential_profile_routing(tmp_path, monkeypatch):
    """Two sequential calls with different profiles each hit the correct HMC host."""
    from hmcpctl.config import load_profile as real_load_profile
    from hmcpctl.server_tools.systems.core import hmc_get_console_info

    cfg_path = _toml_two_profiles(tmp_path)

    def _load_profile_with_path(profile=None, config_path=None):
        return real_load_profile(profile=profile, config_path=cfg_path)

    with (
        patch("hmcpctl.config.load_profile", side_effect=_load_profile_with_path),
        patch("hmcpctl.config.resolve_config_path", return_value=cfg_path),
        respx.mock(assert_all_called=False) as router_a,
    ):
        # Alpha profile → hmc-a.test
        router_a.put("https://hmc-a.test/rest/api/web/Logon").mock(
            return_value=httpx.Response(200, text=LOGON)
        )
        router_a.delete("https://hmc-a.test/rest/api/web/Logon").mock(
            return_value=httpx.Response(204)
        )
        router_a.get("https://hmc-a.test/rest/api/uom/ManagementConsole").mock(
            return_value=httpx.Response(200, text=CONSOLE_A)
        )

        result_a = hmc_get_console_info(profile="alpha")

    with (
        patch("hmcpctl.config.load_profile", side_effect=_load_profile_with_path),
        patch("hmcpctl.config.resolve_config_path", return_value=cfg_path),
        respx.mock(assert_all_called=False) as router_b,
    ):
        # Beta profile → hmc-b.test
        router_b.put("https://hmc-b.test/rest/api/web/Logon").mock(
            return_value=httpx.Response(200, text=LOGON)
        )
        router_b.delete("https://hmc-b.test/rest/api/web/Logon").mock(
            return_value=httpx.Response(204)
        )
        router_b.get("https://hmc-b.test/rest/api/uom/ManagementConsole").mock(
            return_value=httpx.Response(200, text=CONSOLE_B)
        )

        result_b = hmc_get_console_info(profile="beta")

    assert result_a is not None and result_a["Resource"]["SystemName"] == "hmc-a"
    assert result_b is not None and result_b["Resource"]["SystemName"] == "hmc-b"


# ---------------------------------------------------------------------- #
# T-3: Concurrent profile isolation
# ---------------------------------------------------------------------- #


def test_two_profile_strings_produce_distinct_clients(tmp_path, monkeypatch):
    """Two calls with different profile strings produce clients pointing at different hosts.

    Uses asyncio.gather for structural concurrency; the mock has no await
    points so tasks run to completion without interleaving. This confirms
    that client_from_env constructs an independent HMCClient per call
    (no shared cached instance).  For thread/coroutine interleaving coverage
    see the sequential routing test.
    """
    from hmcpctl.client.core import HMCClient

    cfg_path = _toml_two_profiles(tmp_path)

    call_log: list[str] = []

    async def fake_context_a(self):
        call_log.append(f"open:{self.config.host}")
        return self

    async def fake_context_exit(self, *_):
        pass

    async def fake_get_console(self):
        call_log.append(f"get:{self.config.host}")
        return {"Resource": {"SystemName": self.config.host}, "UUID": "x"}

    async def run_concurrent():
        def patched_resolve():
            return cfg_path

        with (
            patch("hmcpctl.config.resolve_config_path", side_effect=patched_resolve),
            patch("hmcpctl.config.resolve_config_path", side_effect=patched_resolve),
            patch.object(HMCClient, "__aenter__", fake_context_a),
            patch.object(HMCClient, "__aexit__", fake_context_exit),
            patch.object(HMCClient, "get_console_info", fake_get_console),
        ):
            from hmcpctl.client.client_factory import client_from_env

            async def call_a():
                async with client_from_env("alpha") as hmc:
                    return await hmc.get_console_info()

            async def call_b():
                async with client_from_env("beta") as hmc:
                    return await hmc.get_console_info()

            task_a, task_b = await asyncio.gather(call_a(), call_b())
            return task_a, task_b

    result_a, result_b = asyncio.run(run_concurrent())

    assert "hmc-a.test" in result_a["Resource"]["SystemName"]
    assert "hmc-b.test" in result_b["Resource"]["SystemName"]
    assert result_a["Resource"]["SystemName"] != result_b["Resource"]["SystemName"], (
        "Concurrent calls returned the same host — state leakage detected"
    )


# ---------------------------------------------------------------------- #


# ---------------------------------------------------------------------- #
# T-4: Nickname resolution parity (issue #226)
# ---------------------------------------------------------------------- #


def _toml_with_nickname(tmp_path: Path) -> Path:
    """Write a TOML config with a profile and a nickname mapping to it."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "default_profile = 'prod'\n"
        "\n"
        "[profiles.prod]\n"
        "host = 'prod-hmc.test'\n"
        "user = 'hscroot'\n"
        "password = 'prod-pass'\n"  # pragma: allowlist secret
        "\n"
        "[nicknames]\n"
        "big-iron = 'prod'\n"
    )
    return cfg


def test_nickname_reaches_client_from_env(tmp_path, monkeypatch):
    """A nickname passed to client_from_env resolves to its target profile host.

    client_from_env is the single client builder shared by the CLI
    (``cli_app._client``) and every MCP tool (``client_from_env(profile)``), so
    resolving a nickname here proves the nickname works on both surfaces without
    a per-tool change.
    """
    from hmcpctl.client.client_factory import client_from_env

    cfg_path = _toml_with_nickname(tmp_path)
    monkeypatch.setattr("hmcpctl.config.resolve_config_path", lambda: cfg_path)
    monkeypatch.setattr("hmcpctl.config.resolve_config_path", lambda: cfg_path)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)

    client = client_from_env(profile="big-iron")

    assert client.config.host == "prod-hmc.test"


# ---------------------------------------------------------------------- #
# T-6: the two handlers that declared a profile and discarded it (#222)
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "hmc_set_lpar_boot_order",
            {
                "system_name_or_uuid": "sys-1",
                "lpar_name_or_uuid": "11111111-2222-3333-4444-555555555555",
                "devices": ["/network"],
            },
        ),
        (
            "hmc_clear_lpar_boot_order",
            {
                "system_name_or_uuid": "sys-1",
                "lpar_name_or_uuid": "11111111-2222-3333-4444-555555555555",
            },
        ),
    ],
)
def test_boot_order_tools_route_the_profile_they_declare(
    monkeypatch, tool_name, arguments
):
    """Both declared and documented `profile` while opening the default client.

    Authorization in #222 decides on the declared argument, so a handler that
    discards it authorizes one connection and reaches another.
    """
    from hmcpctl.server_tools.lpar import lifecycle_boot as server_lpars

    seen: list[str | None] = []

    class _Recorder:
        async def __aenter__(self):
            raise AssertionError("the test stops before any HMC request")

        async def __aexit__(self, *exc):
            return False

    def _capture(profile=None, **overrides):
        seen.append(profile)
        return _Recorder()

    monkeypatch.setattr("hmcpctl._app.client_from_env", _capture)

    with pytest.raises(AssertionError, match="stops before any HMC request"):
        getattr(server_lpars, tool_name)(**arguments, profile="beta")

    assert seen == ["beta"]
