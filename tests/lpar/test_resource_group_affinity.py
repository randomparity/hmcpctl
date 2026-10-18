"""Capability-aware resource-group affinity queries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from conftest import live_fixture, live_process_error
from fastmcp import Client
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli import app
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.cli_commands.lpar import config as cli_lpars
from hmcpctl.config import HMCConfig
from hmcpctl.operations.affinity.ssh import (
    ResourceGroupAffinityResult,
    list_resource_group_memopt_scores,
)
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import configuration as server_lpar_config
from hmcpctl.ssh.affinity import (
    MemoptResourceGroupSelector,
    query_resource_group_memopt_scores,
)
from hmcpctl.ssh.transport import HMCCLIError


def _config() -> HMCConfig:
    return HMCConfig(host="hmc.test", user="u", password="p")


def _hmc() -> SimpleNamespace:
    return SimpleNamespace(config=_config())


V11 = "version= Version: 11\n Release: 2\n Service Pack: 1120\n"
CURRENT = (
    "resource_group_name,resource_group_id,curr_score\nDefault Resource Group,0,100\n"
)
CALCULATED = (
    "resource_group_name,resource_group_id,curr_score,predicted_score,"
    "requested_lpar_names,requested_lpar_ids,protected_lpar_names,protected_lpar_ids\n"
    "Default Resource Group,0,none,100,,none,,none\n"
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"names": ("",)},
        {"names": ("a", "a")},
        {"ids": (-1,)},
        {"ids": (1, 1)},
        {"names": ("a",), "ids": (0,)},
        {"names": ("a",), "all": True},
    ],
)
def test_resource_group_selector_rejects_invalid_modes(kwargs):
    with pytest.raises(ValueError):
        MemoptResourceGroupSelector(**kwargs)


def test_resource_group_selector_accepts_default_group_id_zero():
    assert MemoptResourceGroupSelector(ids=(0,)).ids == (0,)


@pytest.mark.parametrize(
    ("selector", "fragment"),
    [
        (MemoptResourceGroupSelector(all=True), "--gid all"),
        (MemoptResourceGroupSelector(ids=(0, 2)), "--gid 0,2"),
        (
            MemoptResourceGroupSelector(names=("Default Resource Group",)),
            "-g 'Default Resource Group'",
        ),
    ],
)
def test_current_query_uses_exact_projection_and_selector(selector, fragment):
    commands: list[str] = []

    async def run(_config, command):
        commands.append(command)
        return V11 if command == "lshmc -V" else CURRENT

    with patch("hmcpctl.ssh.affinity.run_hmc_command", run):
        result = asyncio.run(
            query_resource_group_memopt_scores(
                _config(), "system", selector, calculated=False
            )
        )

    assert result.unavailable_reason is None
    assert result.items == [
        {
            "resource_group_name": "Default Resource Group",
            "resource_group_id": "0",
            "curr_score": "100",
        }
    ]
    assert fragment in commands[1]
    assert "-F resource_group_name,resource_group_id,curr_score --header" in commands[1]


def test_calculated_query_preserves_sentinel_and_marks_prediction():
    runner = AsyncMock(side_effect=[V11, CALCULATED])
    with patch("hmcpctl.ssh.affinity.run_hmc_command", runner):
        result = asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "system",
                MemoptResourceGroupSelector(all=True),
                calculated=True,
            )
        )
    assert result.items[0]["curr_score"] == "none"
    assert result.items[0]["prediction_guaranteed"] is False


@pytest.mark.parametrize(
    "version",
    [
        "",
        "Version: eleven",
        "V10R3M1060",
        # The captured answer: labelled fields, fix lines and base_version.
        pytest.param(live_fixture("cli-lshmc-version")["stdout"], id="v10r3-capture"),
    ],
)
def test_unadmitted_hmc_returns_capability_without_score_query(version):
    runner = AsyncMock(return_value=version)
    with patch("hmcpctl.ssh.affinity.run_hmc_command", runner):
        result = asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "system",
                MemoptResourceGroupSelector(all=True),
                calculated=False,
            )
        )
    assert result.items == []
    assert "HMC V11R1M1110 or later" in result.unavailable_reason
    runner.assert_awaited_once_with(_config(), "lshmc -V")


def _captured_connection(*answers: str | BaseException) -> AsyncMock:
    connection = AsyncMock()
    connection.run = AsyncMock(
        side_effect=[
            answer if isinstance(answer, BaseException) else MagicMock(stdout=answer)
            for answer in answers
        ]
    )
    connection.__aenter__ = AsyncMock(return_value=connection)
    connection.__aexit__ = AsyncMock(return_value=False)
    return connection


def test_captured_v11r2_power11_current_scores():
    """V11R2 on POWER11: lshmc -V passes the gate and the group scores parse."""
    capture = live_fixture("cli-memopt-resgroup-current")
    connection = _captured_connection(
        live_fixture("cli-lshmc-version-v11r2")["stdout"], capture["stdout"]
    )
    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=connection):
        result = asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "sys-2",
                MemoptResourceGroupSelector(all=True),
                calculated=False,
            )
        )
    assert connection.run.await_args_list[1].args[0] == capture["command"]
    assert result.unavailable_reason is None
    assert result.items == [
        {
            "resource_group_name": "label-1",
            "resource_group_id": "0",
            "curr_score": "100",
        }
    ]


def test_captured_v11r2_power11_calculated_scores():
    """V11R2 on POWER11: a calculated row with no requested names still parses."""
    capture = live_fixture("cli-memopt-resgroup-calc")
    connection = _captured_connection(
        live_fixture("cli-lshmc-version-v11r2")["stdout"], capture["stdout"]
    )
    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=connection):
        result = asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "sys-2",
                MemoptResourceGroupSelector(all=True),
                calculated=True,
            )
        )
    assert connection.run.await_args_list[1].args[0] == capture["command"]
    assert result.unavailable_reason is None
    assert result.items == [
        {
            "resource_group_name": "label-1",
            "resource_group_id": "0",
            "curr_score": "100",
            "predicted_score": "100",
            "requested_lpar_names": "",
            "requested_lpar_ids": "none",
            "protected_lpar_names": "",
            "protected_lpar_ids": "none",
            "prediction_guaranteed": False,
        }
    ]


def test_captured_hsclca00_returns_managed_system_capability_result():
    """V11R2 on POWER9 refuses with HSCLCA00 on stdout, stderr empty."""
    connection = _captured_connection(
        live_fixture("cli-lshmc-version-v11r2")["stdout"],
        live_process_error("cli-memopt-resgroup-unsupported"),
    )
    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=connection):
        result = asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "sys-2",
                MemoptResourceGroupSelector(all=True),
                calculated=False,
            )
        )
    assert result.items == []
    assert "multiple resource groups" in result.unavailable_reason


def test_hsclca00_returns_managed_system_capability_result():
    runner = AsyncMock(side_effect=[V11, HMCCLIError("HSCLCA00 unsupported")])
    with patch("hmcpctl.ssh.affinity.run_hmc_command", runner):
        result = asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "system",
                MemoptResourceGroupSelector(all=True),
                calculated=False,
            )
        )
    assert result.items == []
    assert "managed system" in result.unavailable_reason


def test_noncapability_failure_propagates():
    runner = AsyncMock(side_effect=[V11, HMCCLIError("permission denied")])
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        pytest.raises(HMCCLIError, match="permission denied"),
    ):
        asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "system",
                MemoptResourceGroupSelector(all=True),
                calculated=False,
            )
        )


def test_error_that_only_mentions_hsclca00_propagates():
    runner = AsyncMock(
        side_effect=[V11, HMCCLIError("diagnostic says HSCLCA00 was not returned")]
    )
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        pytest.raises(HMCCLIError, match="was not returned"),
    ):
        asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "system",
                MemoptResourceGroupSelector(all=True),
                calculated=False,
            )
        )


@pytest.mark.parametrize(
    "output", ["", " \n", "resource_group_name,resource_group_id,curr_score\n"]
)
def test_blank_output_fails_but_header_only_is_empty(output):
    runner = AsyncMock(side_effect=[V11, output])
    with patch("hmcpctl.ssh.affinity.run_hmc_command", runner):
        call = query_resource_group_memopt_scores(
            _config(), "system", MemoptResourceGroupSelector(all=True), calculated=False
        )
        if output.strip():
            assert asyncio.run(call).items == []
        else:
            with pytest.raises(HMCCLIError, match="missing its header"):
                asyncio.run(call)


@pytest.mark.parametrize(
    "output",
    [
        "resource_group_id,resource_group_name,curr_score\n0,Default,100\n",
        "resource_group_name,resource_group_id,curr_score\nDefault,0\n",
    ],
)
def test_malformed_resource_group_output_is_actionable(output):
    runner = AsyncMock(side_effect=[V11, output])
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        pytest.raises(
            HMCCLIError,
            match="lsmemopt resource-group response did not match the expected "
            "resource_group_name,resource_group_id,curr_score fields",
        ),
    ):
        asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "system",
                MemoptResourceGroupSelector(all=True),
                calculated=False,
            )
        )


def test_empty_required_resource_group_field_is_actionable():
    output = "resource_group_name,resource_group_id,curr_score\nDefault,,100\n"
    runner = AsyncMock(side_effect=[V11, output])
    with (
        patch("hmcpctl.ssh.affinity.run_hmc_command", runner),
        pytest.raises(HMCCLIError, match="row 1 has empty required fields"),
    ):
        asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "system",
                MemoptResourceGroupSelector(all=True),
                calculated=False,
            )
        )


def test_empty_result_reply_is_no_resource_groups():
    runner = AsyncMock(side_effect=[V11, "No results were found.\n"])
    with patch("hmcpctl.ssh.affinity.run_hmc_command", runner):
        query = asyncio.run(
            query_resource_group_memopt_scores(
                _config(),
                "system",
                MemoptResourceGroupSelector(all=True),
                calculated=False,
            )
        )
    assert query.items == []


def test_shared_operation_resolves_system_and_defaults_to_all():
    hmc = _hmc()
    query = AsyncMock(
        return_value=type("Query", (), {"items": [], "unavailable_reason": None})()
    )
    with (
        patch(
            "hmcpctl.operations.affinity.ssh.resolve_ssh_names",
            AsyncMock(return_value=("resolved-system", None)),
        ) as resolve,
        patch(
            "hmcpctl.operations.affinity.ssh.query_resource_group_memopt_scores", query
        ),
    ):
        result = asyncio.run(list_resource_group_memopt_scores(hmc, "system-uuid"))
    assert result.capability == "available"
    assert result.selector == MemoptResourceGroupSelector(all=True)
    resolve.assert_awaited_once_with(hmc, "system-uuid", None)
    query.assert_awaited_once_with(
        _config(),
        "resolved-system",
        MemoptResourceGroupSelector(all=True),
        calculated=False,
    )


def test_mcp_adapter_delegates_to_shared_operation():
    expected = ResourceGroupAffinityResult(
        "capability-unavailable",
        "current",
        "system",
        MemoptResourceGroupSelector(all=True),
        [],
        "upgrade HMC",
    )
    operation = AsyncMock(return_value=expected)
    with (
        patch.object(
            server_lpar_config,
            "with_client",
            side_effect=lambda callback, **_: asyncio.run(callback(_hmc())),
        ),
        patch.object(
            server_lpar_config, "list_resource_group_memopt_scores", operation
        ),
    ):
        actual = server_lpar_config.hmc_list_resource_group_memopt_scores("system")
    assert actual == expected
    operation.assert_awaited_once_with(ANY, "system", None)


def test_mcp_registers_both_resource_group_affinity_tools():
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))

    async def names():
        async with Client(create_mcp(policy)) as client:
            return {tool.name for tool in await client.list_tools()}

    assert {
        "hmc_list_resource_group_memopt_scores",
        "hmc_plan_resource_group_memopt_scores",
    } <= asyncio.run(names())


def test_resource_group_cli_delegates_id_zero_and_prints_json():
    expected = ResourceGroupAffinityResult(
        "available",
        "current",
        "system",
        MemoptResourceGroupSelector(ids=(0,)),
        [],
        None,
    )
    operation = AsyncMock(return_value=expected)
    with (
        patch.object(
            cli_lpars,
            "with_client",
            side_effect=lambda callback, **_: asyncio.run(callback(_hmc())),
        ),
        patch.object(cli_lpars, "list_resource_group_memopt_scores", operation),
    ):
        result = CliRunner().invoke(
            app,
            [
                "lpars",
                "resource-group-memopt-scores",
                "system",
                "--resource-group-id",
                "0",
                "--json",
            ],
        )
    assert result.exit_code == 0, result.output
    assert '"capability": "available"' in result.output
    operation.assert_awaited_once_with(
        ANY, "system", MemoptResourceGroupSelector(ids=(0,))
    )


def test_resource_group_cli_rejects_mixed_selectors():
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "resource-group-memopt-scores",
            "system",
            "--resource-group-name",
            "Default Resource Group",
            "--all",
        ],
    )
    assert result.exit_code == 2
