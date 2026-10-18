"""Capability-aware Power11 minimum-affinity policy reads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest
from conftest import live_fixture
from fastmcp import Client
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli import app
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.cli_commands.lpar import config as cli_lpars
from hmcpctl.config import HMCConfig
from hmcpctl.operations.affinity.ssh import (
    MinimumAffinityPolicyResult,
    get_minimum_affinity_policy,
    set_minimum_affinity_policy,
)
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import configuration as server_lpar_config
from hmcpctl.ssh.affinity import (
    MinimumAffinityPolicy,
    query_minimum_affinity_policy,
    set_minimum_affinity_policy_cli,
)
from hmcpctl.ssh.transport import HMCCLIError


def _config() -> HMCConfig:
    return HMCConfig(host="hmc.test", user="u", password="p")


def _hmc() -> SimpleNamespace:
    return SimpleNamespace(config=_config())


@pytest.mark.asyncio
async def test_policy_query_uses_compatibility_gate_and_exact_projection():
    runner = AsyncMock(
        side_effect=[
            "default,POWER10,POWER11\n",
            "min_affinity_score,min_affinity_score_action\n0,none\n",
        ]
    )
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        patch("hmcpctl.ssh.profiles.run_hmc_command", runner),
    ):
        result = await query_minimum_affinity_policy(_config(), "system", "lpar one")

    assert result.min_affinity_score == 0
    assert result.min_affinity_score_action == "none"
    assert result.unavailable_reason is None
    assert runner.await_args_list[0].args[1] == (
        "lssyscfg -r sys -m system -F lpar_proc_compat_modes"
    )
    assert runner.await_args_list[1].args[1] == (
        "lssyscfg -r lpar -m system --filter 'lpar_names=lpar one' "
        "-F min_affinity_score,min_affinity_score_action --header"
    )


@pytest.mark.asyncio
async def test_policy_query_accepts_quoted_compatibility_modes():
    runner = AsyncMock(
        side_effect=[
            '"default,POWER9,POWER9_base,POWER10,POWER11"\n',
            "min_affinity_score,min_affinity_score_action\n0,none\n",
        ]
    )
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        patch("hmcpctl.ssh.profiles.run_hmc_command", runner),
    ):
        result = await query_minimum_affinity_policy(_config(), "system", "lpar")

    assert result.min_affinity_score == 0
    assert result.min_affinity_score_action == "none"
    assert runner.await_count == 2


@pytest.mark.asyncio
async def test_policy_query_reads_captured_power11_answer():
    """V11R2 on POWER11: the modes advertise POWER11 and the policy row parses."""
    policy = live_fixture("cli-min-affinity-policy-p11")
    runner = AsyncMock(
        side_effect=[
            live_fixture("cli-proc-compat-modes-p11")["stdout"],
            policy["stdout"],
        ]
    )
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        patch("hmcpctl.ssh.profiles.run_hmc_command", runner),
    ):
        result = await query_minimum_affinity_policy(_config(), "sys-2", "lpar-5")

    assert runner.await_args_list[1].args[1] == policy["command"]
    assert (result.min_affinity_score, result.min_affinity_score_action) == (0, "none")
    assert result.unavailable_reason is None


@pytest.mark.asyncio
async def test_policy_query_returns_capability_absence_without_policy_command():
    # The captured POWER9 answer. Asking that system for min_affinity_score
    # exits 1 with "An invalid attribute was entered" (#1202), so the gate
    # must stop before the policy read.
    runner = AsyncMock(return_value=live_fixture("cli-proc-compat-modes")["stdout"])
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        patch("hmcpctl.ssh.profiles.run_hmc_command", runner),
    ):
        result = await query_minimum_affinity_policy(_config(), "system", "lpar")

    assert result.min_affinity_score is None
    assert result.min_affinity_score_action is None
    assert "POWER11" in result.unavailable_reason
    runner.assert_awaited_once()


@pytest.mark.parametrize(
    "output",
    [
        "min_affinity_score,min_affinity_score_action\n",
        "No results were found.\n",
        "min_affinity_score,min_affinity_score_action\n0,none\n1,warn\n",
        "min_affinity_score,min_affinity_score_action\n,none\n",
        "min_affinity_score,min_affinity_score_action\n101,warn\n",
        "min_affinity_score,min_affinity_score_action\n1.5,warn\n",
        "min_affinity_score,min_affinity_score_action\n50,ignore\n",
    ],
)
@pytest.mark.asyncio
async def test_policy_query_rejects_malformed_output(output):
    runner = AsyncMock(side_effect=["POWER11\n", output])
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        patch("hmcpctl.ssh.profiles.run_hmc_command", runner),
        pytest.raises(HMCCLIError, match="malformed lssyscfg minimum-affinity"),
    ):
        await query_minimum_affinity_policy(_config(), "system", "lpar")


@pytest.mark.parametrize("output", ["", "wrong,min_affinity_score_action\n0,none\n"])
@pytest.mark.asyncio
async def test_policy_query_rejects_an_unheaded_reply(output):
    runner = AsyncMock(side_effect=["POWER11\n", output])
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        patch("hmcpctl.ssh.profiles.run_hmc_command", runner),
        pytest.raises(
            HMCCLIError,
            match="lssyscfg minimum-affinity policy response did not match the "
            "expected min_affinity_score,min_affinity_score_action fields",
        ),
    ):
        await query_minimum_affinity_policy(_config(), "system", "lpar")


@pytest.mark.parametrize("score", [-1, 101, True])
@pytest.mark.asyncio
async def test_policy_setter_rejects_invalid_score_before_hmc_call(score):
    runner = AsyncMock()
    policy = MinimumAffinityPolicy(score, "warn")
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        patch("hmcpctl.ssh.profiles.run_hmc_command", runner),
        pytest.raises(ValueError, match="integer from 0 through 100"),
    ):
        await set_minimum_affinity_policy_cli(_config(), "system", "lpar", policy)
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_setter_rejects_unsupported_system_before_mutation():
    runner = AsyncMock(return_value="default,POWER10\n")
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        patch("hmcpctl.ssh.profiles.run_hmc_command", runner),
        pytest.raises(HMCCLIError, match="advertises POWER11"),
    ):
        await set_minimum_affinity_policy_cli(
            _config(), "system", "lpar", MinimumAffinityPolicy(80, "warn")
        )
    runner.assert_awaited_once()
    assert runner.await_args.args[1].startswith("lssyscfg -r sys")


@pytest.mark.asyncio
async def test_policy_setter_requires_deliberate_fail_and_quotes_command():
    runner = AsyncMock(side_effect=["POWER11\n", "changed\n"])
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        patch("hmcpctl.ssh.profiles.run_hmc_command", runner),
    ):
        result = await set_minimum_affinity_policy_cli(
            _config(), "system one", "lpar one", MinimumAffinityPolicy(90, "fail")
        )
    assert result == "changed\n"
    assert runner.await_args_list[1].args[1] == (
        "chsyscfg -r lpar -m 'system one' "
        "-i 'name=lpar one,min_affinity_score=90,min_affinity_score_action=fail'"
    )


@pytest.mark.asyncio
async def test_public_policy_setter_validates_before_resolution():
    hmc = AsyncMock()
    hmc.config = _config()
    resolver = AsyncMock()
    with (
        patch(
            "hmcpctl.operations.affinity.ssh.resolve_and_authorize_lpar_names",
            resolver,
        ),
        pytest.raises(ValueError, match="none, warn, or fail"),
    ):
        await set_minimum_affinity_policy(
            hmc,
            "lpar",
            MinimumAffinityPolicy(80, "invalid"),
            system_name_or_uuid="system",
        )
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_policy_setter_authorizes_before_mutation():
    hmc = AsyncMock()
    hmc.config = _config()
    events: list[str] = []
    authorize = AsyncMock(
        side_effect=lambda *args, **kwargs: (
            events.append("authorize") or ("system", "lpar")
        )
    )
    mutate = AsyncMock(side_effect=lambda *args: events.append("mutate") or "changed")
    with (
        patch(
            "hmcpctl.operations.affinity.ssh.resolve_and_authorize_lpar_names",
            authorize,
        ),
        patch(
            "hmcpctl.operations.affinity.ssh.set_minimum_affinity_policy_cli", mutate
        ),
    ):
        result = await set_minimum_affinity_policy(
            hmc, "lpar", MinimumAffinityPolicy(80, "warn"), system_name_or_uuid="system"
        )
    assert result == "changed"
    assert events == ["authorize", "mutate"]


@pytest.mark.asyncio
async def test_shared_policy_operation_resolves_names_and_wraps_result():
    query = AsyncMock(
        return_value=type(
            "Query",
            (),
            {
                "min_affinity_score": 80,
                "min_affinity_score_action": "fail",
                "unavailable_reason": None,
            },
        )()
    )
    with (
        patch(
            "hmcpctl.operations.affinity.ssh.resolve_ssh_names",
            AsyncMock(return_value=("resolved-system", "resolved-lpar")),
        ),
        patch("hmcpctl.operations.affinity.ssh.query_minimum_affinity_policy", query),
    ):
        result = await get_minimum_affinity_policy(
            _hmc(), "lpar", system_name_or_uuid="system"
        )

    assert result.capability == "available"
    assert result.system == "resolved-system"
    assert result.lpar == "resolved-lpar"
    assert result.min_affinity_score == 80
    assert result.min_affinity_score_action == "fail"
    assert result.unavailable_reason is None


def test_mcp_policy_adapter_delegates_to_shared_operation():
    expected = MinimumAffinityPolicyResult(
        "available", "system", "lpar", 75, "warn", None
    )
    operation = AsyncMock(return_value=expected)
    with (
        patch.object(
            server_lpar_config,
            "with_client",
            side_effect=lambda callback, **_: asyncio.run(callback(_hmc())),
        ),
        patch.object(server_lpar_config, "get_minimum_affinity_policy", operation),
    ):
        actual = server_lpar_config.hmc_get_minimum_affinity_policy("system", "lpar")

    assert actual == expected
    operation.assert_awaited_once_with(ANY, "lpar", system_name_or_uuid="system")


def test_mcp_registers_minimum_affinity_policy_as_lpar_read():
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))

    async def names():
        async with Client(create_mcp(policy)) as client:
            return {tool.name for tool in await client.list_tools()}

    assert "hmc_get_minimum_affinity_policy" in asyncio.run(names())
    security = TOOL_SECURITY["hmc_get_minimum_affinity_policy"]
    assert security.effect == "read"
    assert [(target.kind, target.argument) for target in security.targets] == [
        ("managed_system", "system_name_or_uuid"),
        ("lpar", "lpar_name_or_uuid"),
    ]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            MinimumAffinityPolicyResult(
                "available", "system", "lpar", 75, "warn", None
            ),
            "minimum affinity score: 75 (warn)",
        ),
        (
            MinimumAffinityPolicyResult(
                "capability-unavailable", "system", "lpar", None, None, "upgrade"
            ),
            "unavailable: upgrade",
        ),
    ],
)
def test_cli_policy_human_output(result, expected):
    with (
        patch.object(
            cli_lpars,
            "with_client",
            side_effect=lambda callback, **_: asyncio.run(callback(_hmc())),
        ),
        patch.object(
            cli_lpars, "get_minimum_affinity_policy", AsyncMock(return_value=result)
        ),
    ):
        invocation = CliRunner().invoke(
            app, ["lpars", "get-minimum-affinity-policy", "lpar", "system"]
        )
    assert invocation.exit_code == 0, invocation.output
    assert expected in invocation.output


def test_cli_policy_json_delegates():
    expected = MinimumAffinityPolicyResult(
        "available", "system", "lpar", 100, "fail", None
    )
    operation = AsyncMock(return_value=expected)
    with (
        patch.object(
            cli_lpars,
            "with_client",
            side_effect=lambda callback, **_: asyncio.run(callback(_hmc())),
        ),
        patch.object(cli_lpars, "get_minimum_affinity_policy", operation),
    ):
        invocation = CliRunner().invoke(
            app,
            [
                "lpars",
                "get-minimum-affinity-policy",
                "lpar",
                "system",
                "--json",
            ],
        )
    assert invocation.exit_code == 0, invocation.output
    assert '"min_affinity_score": 100' in invocation.output
    operation.assert_awaited_once_with(ANY, "lpar", system_name_or_uuid="system")
