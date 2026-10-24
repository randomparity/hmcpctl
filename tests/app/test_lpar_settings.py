"""C01 presentation forwarding and truthful exits."""

import asyncio
import json
import shlex
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from test_app_lpar_security_policy import policy_app
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.cli import app
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparSettings
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.configuration import LparSettingsResult
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar.configuration import (
    hmc_configure_lpar,
    hmc_get_lpar_settings,
)
from hmcpctl.server_tools.lpar.lifecycle_create import hmc_create_lpar
from hmcpctl.xmlutil import parse_feed

SYSTEM = "0000000e-abcd-4ef0-8abc-00000000000e"
PARTITION = "0000000c-abcd-4ef0-8abc-00000000000c"


def test_mcp_settings_and_create_forward_false_zero(monkeypatch):
    from hmcpctl.server_tools.lpar import configuration, lifecycle_create

    settings = LparSettings(False, 0, False, False, False)
    result = LparSettingsResult("verified", settings, settings, ())
    configure = AsyncMock(return_value=result)
    read = AsyncMock(return_value=settings)
    create = AsyncMock(return_value="created")
    monkeypatch.setattr(configuration, "configure_lpar_settings", configure)
    monkeypatch.setattr(configuration, "read_lpar_settings", read)
    monkeypatch.setattr(
        configuration, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
    )
    monkeypatch.setattr(lifecycle_create, "create_lpar", create)
    monkeypatch.setattr(
        lifecycle_create, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
    )
    assert hmc_configure_lpar("s", "l", settings) == result
    assert configure.await_args.args[2] == settings
    assert hmc_get_lpar_settings("s", "l") == settings
    hmc_create_lpar("s", "l", settings=settings)
    assert create.await_args.args[2].settings == settings


def test_cli_settings_and_outcome_exits(monkeypatch):
    from hmcpctl.cli_commands.lpar import config

    settings = LparSettings(allow_perf_collection=False, lpar_avail_priority=0)
    result = LparSettingsResult("verified", settings, settings, ())
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(config, "configure_lpar_settings", call)
    monkeypatch.setattr(config, "with_client", lambda f: asyncio.run(f("hmc")))
    args = [
        "lpars",
        "configure",
        "l",
        "s",
        "--no-allow-perf-collection",
        "--lpar-avail-priority",
        "0",
    ]
    run = CliRunner().invoke(app, args)
    assert run.exit_code == 0, run.output
    assert call.await_args.args[2] == settings
    for status in ("mismatch", "readback-unavailable", "write-outcome-unknown"):
        call.return_value = replace(result, status=status)
        assert CliRunner().invoke(app, args).exit_code == 1


def test_cli_create_settings_forwarding(monkeypatch):
    from hmcpctl.cli_commands.lpar import create
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult

    call = AsyncMock(
        return_value=LparPcieWorkflowResult(True, True, {"UUID": "new"}, True, (), ())
    )
    monkeypatch.setattr(create, "create_lpar", call)
    monkeypatch.setattr(create, "with_client", lambda f: asyncio.run(f("hmc")))
    run = CliRunner().invoke(
        app,
        [
            "lpars",
            "create",
            "new",
            "--system",
            "s",
            "--yes",
            "--no-migration-disabled",
            "--lpar-avail-priority",
            "0",
        ],
    )
    assert run.exit_code == 0, run.output
    assert call.await_args.args[2].settings == LparSettings(
        lpar_avail_priority=0, migration_disabled=False
    )


@pytest.mark.parametrize("creation", [False, True])
@pytest.mark.parametrize("management", [None, False, True])
@pytest.mark.parametrize("kvm", [None, False, True])
def test_cli_role_settings_reuse(monkeypatch, creation, management, kvm):
    from hmcpctl.cli_commands.lpar import config, create
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult

    settings = LparSettings(powervm_mgmt_capable=management, kvm_capable=kvm)
    module = create if creation else config
    result = (
        LparPcieWorkflowResult(True, True, {"UUID": "new"}, True, (), ())
        if creation
        else LparSettingsResult("verified", settings, settings, ())
    )
    call = AsyncMock(return_value=result)
    monkeypatch.setattr(
        module, "create_lpar" if creation else "configure_lpar_settings", call
    )
    monkeypatch.setattr(module, "with_client", lambda f: asyncio.run(f("hmc")))
    args = (
        ["lpars", "create", "new", "--system", "sys-R1", "--yes"]
        if creation
        else ["lpars", "configure", "owned", "sys-R1"]
    )
    for field, value in (("powervm-mgmt-capable", management), ("kvm-capable", kvm)):
        if value is not None:
            args.append(("--" if value else "--no-") + field)
    run = CliRunner().invoke(app, args)
    assert run.exit_code == 0, run.output
    assert (
        call.await_args.args[2].settings if creation else call.await_args.args[2]
    ) == settings
    if creation:
        call.return_value = replace(result, workflow_completed=False)
        assert CliRunner().invoke(app, args).exit_code == 1
    else:
        for status in ("mismatch", "readback-unavailable", "write-outcome-unknown"):
            call.return_value = LparSettingsResult(status, settings, None, ())
            assert CliRunner().invoke(app, args).exit_code == 1


@pytest.mark.asyncio
async def test_mcp_role_settings_reuse():
    async with Client(policy_app("hmc_create_lpar", "hmc_configure_lpar")) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    for name in ("hmc_create_lpar", "hmc_configure_lpar"):
        properties = tools[name].input_schema["properties"]
        settings = properties["settings"]
        if "anyOf" in settings:
            settings = next(
                item for item in settings["anyOf"] if item.get("type") == "object"
            )
        assert settings["additionalProperties"] is False
        assert set(settings["properties"]) == set(asdict(LparSettings()))
        for field in ("powervm_mgmt_capable", "kvm_capable"):
            schema = settings["properties"][field]
            assert schema["default"] is None
            assert schema["anyOf"] == [{"type": "boolean"}, {"type": "null"}]
            assert schema["description"].strip()
        assert "intended_linux_guest" not in properties
        assert "lpar_proc_compat_mode" not in properties


@pytest.fixture
def role_app_boundary(monkeypatch):
    from hmcpctl.server_tools.lpar import configuration, lifecycle_create

    fixture_root = Path(__file__).parents[1] / "fixtures" / "live"

    def captured(name):
        return parse_feed(json.loads((fixture_root / name).read_text())["body"])[0]

    system = captured("rest-ms-entry-mtms-pair.json")
    system["Resource"]["SystemName"] = "sys-R1"
    entry = captured("rest-lpar-entry.json")
    entry["UUID"] = PARTITION
    entry["Resource"]["PartitionName"] = "owned"
    hmc = SimpleNamespace(
        config=HMCConfig.from_mapping({"host": "hmc.test"}),
        get_managed_system=AsyncMock(return_value=system),
        get_console_info=AsyncMock(
            return_value={
                "Resource": {
                    "VersionInfo": {
                        "Version": "10",
                        "Release": "3",
                        "ServicePackName": "1060",
                        "Maintenance": "0",
                        "Minor": "1",
                    }
                }
            }
        ),
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM, "SystemName": "sys-R1"}]
        ),
        list_logical_partitions=AsyncMock(return_value=[entry]),
        get_logical_partition=AsyncMock(return_value=entry),
        find_partition_by_name=AsyncMock(return_value=entry),
    )
    writes = []
    state = {"error": None, "description": ""}

    async def remote(config, command, **kwargs):
        if command.startswith(("chsyscfg ", "mksyscfg ")):
            writes.append(command)
            if state["error"] is not None:
                raise HMCError(state["error"])
            if command.startswith("chsyscfg "):
                record = shlex.split(command)[-1]
                fields = dict(item.split("=", 1) for item in record.split(","))
                for field, native in (
                    ("powervm_mgmt_capable", "PowerVMManagementCapable"),
                    ("kvm_capable", "KvmCapable"),
                ):
                    if field in fields:
                        value = "true" if fields[field] == "1" else "false"
                        current = entry["Resource"].get(native)
                        if isinstance(current, dict):
                            current = current.get("text")
                        if current != value:
                            entry["Resource"][native] = value
            return ""
        if command.endswith("-F description"):
            return state["description"]
        if command.endswith("-F lpar_proc_compat_modes"):
            return "POWER10"
        if command.endswith("-F uuid,name"):
            return f"{SYSTEM},sys-R1"
        if command.endswith("-F name"):
            return "sys-R1"
        if "-F lpar_env,allow_perf_collection," in command:
            if state.get("baseline_error") is not None:
                raise state["baseline_error"]
            return "aixlinux,0,0,0,0,0"
        raise AssertionError(f"Unexpected external SSH request: {command}")

    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", remote)
    monkeypatch.setattr("hmcpctl.ssh.profiles.run_hmc_command", remote)
    for module in (configuration, lifecycle_create):
        monkeypatch.setattr(module, "with_client", lambda f, **kw: asyncio.run(f(hmc)))
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    return SimpleNamespace(
        hmc=hmc,
        system=system["Resource"],
        resource=entry["Resource"],
        writes=writes,
        state=state,
    )


@pytest.mark.parametrize("creation", [False, True])
@pytest.mark.asyncio
async def test_mcp_role_invalid_values_before_remote(role_app_boundary, creation):
    b = role_app_boundary
    name = "hmc_create_lpar" if creation else "hmc_configure_lpar"
    async with Client(policy_app(name)) as client:
        for invalid in (0, 1, "false", "true", [], {}):
            for field in ("powervm_mgmt_capable", "kvm_capable"):
                args = {
                    "system_name_or_uuid": SYSTEM,
                    "name" if creation else "lpar_name_or_uuid": PARTITION,
                    "settings": {field: invalid},
                }
                result = await client.call_tool(name, args, raise_on_error=False)
                assert result.is_error
        result = await client.call_tool(
            name,
            {
                "system_name_or_uuid": SYSTEM,
                "name" if creation else "lpar_name_or_uuid": PARTITION,
                "settings": {"unexpected_role": False},
            },
            raise_on_error=False,
        )
        assert result.is_error
    b.hmc.get_managed_system.assert_not_awaited()
    b.hmc.get_logical_partition.assert_not_awaited()
    assert b.writes == []


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.asyncio
async def test_mcp_false_role_evidence_and_omission(role_app_boundary, wrapped):
    b = role_app_boundary
    if wrapped:
        for name in ("PowerVMManagementCapable", "KvmCapable"):
            b.resource[name] = {"@attrs": {"ksv": "V1_0"}, "text": "false"}
    async with Client(policy_app("hmc_configure_lpar")) as client:
        result = await client.call_tool(
            "hmc_configure_lpar",
            {
                "system_name_or_uuid": SYSTEM,
                "lpar_name_or_uuid": PARTITION,
                "settings": {"powervm_mgmt_capable": False},
            },
            raise_on_error=False,
        )
        assert not result.is_error, result
        data = result.structured_content
        assert data["status"] == "verified"
        assert data["requested"]["powervm_mgmt_capable"] is False
        assert data["requested"]["kvm_capable"] is None
        assert data["observed"]["powervm_mgmt_capable"] is False
        assert data["observed"]["kvm_capable"] is False
        before = b.hmc.get_managed_system.await_count
        omitted = await client.call_tool(
            "hmc_configure_lpar",
            {
                "system_name_or_uuid": SYSTEM,
                "lpar_name_or_uuid": PARTITION,
                "settings": {"time_ref": False},
            },
            raise_on_error=False,
        )
        assert not omitted.is_error, omitted
        assert omitted.structured_content["requested"]["powervm_mgmt_capable"] is None
        assert b.hmc.get_managed_system.await_count == before
    assert len(b.writes) == 2
    assert "powervm_mgmt_capable=0" in b.writes[0]
    assert "kvm_capable=" not in b.writes[0]
    assert "powervm_mgmt_capable=" not in b.writes[1]


@pytest.mark.parametrize("creation", [False, True])
@pytest.mark.asyncio
async def test_mcp_capability_false_denial_has_reason(role_app_boundary, creation):
    b = role_app_boundary
    b.resource["KvmCapable"] = "true"
    before = deepcopy(b.resource)
    if creation:
        b.hmc.find_partition_by_name.return_value = None
    name = "hmc_create_lpar" if creation else "hmc_configure_lpar"
    async with Client(policy_app(name)) as client:
        result = await client.call_tool(
            name,
            {
                "system_name_or_uuid": SYSTEM,
                "name" if creation else "lpar_name_or_uuid": "new"
                if creation
                else PARTITION,
                "settings": {"kvm_capable": False},
            },
            raise_on_error=False,
        )
    assert result.is_error
    assert (
        "KVM selection requires positive system capability and HMC >=10.3.1060.1"
        in str(result.content)
    )
    assert b.writes == []
    assert (await b.hmc.get_logical_partition(PARTITION))["Resource"] == before


@pytest.mark.parametrize("minor", ["0", "1", "3"])
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.asyncio
async def test_mcp_kvm_console_fix_level(role_app_boundary, minor, selected):
    b = role_app_boundary
    b.hmc.get_console_info.return_value["Resource"]["VersionInfo"]["Minor"] = minor
    # Synthetic eligibility isolates console release from the captured hardware.
    b.system["AssociatedSystemCapabilities"]["KvmOnPowerVMCapable"] = "true"
    b.system["SystemFirmware"] = "FW1060_FW1060.10 (10)"
    b.resource["PendingProcessorCompatibilityMode"] = "POWER10"
    b.resource["PartitionProcessorConfiguration"].update(
        HasDedicatedProcessors="true", SharingMode="keep idle procs"
    )
    before = deepcopy(b.resource)
    async with Client(policy_app("hmc_configure_lpar")) as client:
        result = await client.call_tool(
            "hmc_configure_lpar",
            {
                "system_name_or_uuid": SYSTEM,
                "lpar_name_or_uuid": PARTITION,
                "settings": {"kvm_capable": selected},
            },
            raise_on_error=False,
        )
    if minor == "0":
        assert result.is_error
        assert "HMC >=10.3.1060.1" in str(result.content)
        assert b.writes == [] and b.resource == before
    else:
        assert not result.is_error, result
        assert result.structured_content["status"] == "verified"
        assert result.structured_content["observed"]["kvm_capable"] is selected
        assert len(b.writes) == 1
        assert f"kvm_capable={int(selected)}" in b.writes[0]


@pytest.mark.parametrize("stage", ["write", "readback"])
@pytest.mark.parametrize("creation", [False, True])
@pytest.mark.asyncio
async def test_mcp_role_remote_diagnostic_privacy(
    role_app_boundary, creation, stage, caplog
):
    b = role_app_boundary
    marker = "PRIVATE-ROLE-MARKER\x1b[31m password=hidden"
    b.state["error"] = marker if stage == "write" else None
    if stage == "readback":
        if creation:
            b.hmc.find_partition_by_name.side_effect = [None, HMCError(marker)]
        else:
            entry = b.hmc.get_logical_partition.return_value

            async def read(uuid):
                if b.writes:
                    raise HMCError(marker)
                return entry

            b.hmc.get_logical_partition.side_effect = read
    name = "hmc_create_lpar" if creation else "hmc_configure_lpar"
    if creation:
        b.hmc.find_partition_by_name.return_value = None
    async with Client(policy_app(name)) as client:
        result = await client.call_tool(
            name,
            {
                "system_name_or_uuid": SYSTEM,
                "name" if creation else "lpar_name_or_uuid": "new"
                if creation
                else PARTITION,
                "settings": {"powervm_mgmt_capable": False},
            },
            raise_on_error=False,
        )
    assert marker not in str(result)
    assert "PRIVATE-ROLE-MARKER" not in str(result)
    assert "PRIVATE-ROLE-MARKER" not in caplog.text
    assert len(b.writes) == 1
    assert "powervm_mgmt_capable=0" in b.writes[0]
    if creation and stage == "write":
        assert result.is_error
        assert "Native role creation outcome is unknown" in caplog.text
        assert (
            "Native role creation outcome is unknown; the partition may exist"
            in str(result.content)
        )
    elif creation:
        assert not result.is_error, result
        assert result.structured_content["resource_created"] is True
        assert result.structured_content["workflow_completed"] is False
        assert result.structured_content["lpar"] is None
    else:
        assert not result.is_error, result
        assert result.structured_content["status"] == (
            "write-outcome-unknown" if stage == "write" else "readback-unavailable"
        )
        assert result.structured_content["observed"] is None


@pytest.mark.asyncio
async def test_mcp_role_ownership_denial_before_write(role_app_boundary):
    b = role_app_boundary
    b.state["description"] = "[hmcpctl owner:foreign created:2026-10-24]"
    async with Client(policy_app("hmc_configure_lpar")) as client:
        result = await client.call_tool(
            "hmc_configure_lpar",
            {
                "system_name_or_uuid": SYSTEM,
                "lpar_name_or_uuid": PARTITION,
                "settings": {"powervm_mgmt_capable": False},
            },
            raise_on_error=False,
        )
    assert result.is_error
    assert "is owned by 'foreign'" in str(result.content)
    b.hmc.get_managed_system.assert_not_awaited()
    assert b.writes == []


def test_role_capability_owner_bindings():
    root = Path(__file__).parents[2]
    ledger = root / "docs" / "capabilities"
    operations = json.loads((ledger / "operations.json").read_text())["operations"]
    maturity = json.loads((ledger / "maturity.json").read_text())["operations"]
    required_rows = {
        "rest:managed-system",
        "rest:managed-system/logical-partition",
        "rest:management-console",
        "cli:commands/lssyscfg",
    }
    for operation in ("lpar.create", "lpar.configure"):
        owner = next(row for row in operations if row["operation"] == operation)
        assert required_rows <= set(owner["row_ids"])
        assert "tests/lpar/test_linux_roles.py" in owner["tests"]
        assert "tests/app/test_lpar_settings.py" in owner["tests"]
        scopes = next(row for row in maturity if row["operation"] == operation)[
            "implementation"
        ]["implemented_scope"]
        role_scope = next(
            scope
            for scope in scopes
            if scope["variant"] == "c05-linux-management-roles"
        )
        assert {item["name"] for item in role_scope["parameters"]} == {
            "settings.powervm_mgmt_capable",
            "settings.kvm_capable",
        }


@pytest.mark.asyncio
async def test_mcp_role_target_denial_before_remote(role_app_boundary):
    b = role_app_boundary
    name = "hmc_configure_lpar"
    policy = compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": [name],
                            "connections": ["<default>"],
                            "targets": {"managed_system": [SYSTEM], "lpar": ["other"]},
                        }
                    ]
                }
            }
        },
        "test",
        TOOL_SECURITY,
        "test-policy.toml",
    )
    async with Client(create_mcp(policy)) as client:
        result = await client.call_tool(
            name,
            {
                "system_name_or_uuid": SYSTEM,
                "lpar_name_or_uuid": PARTITION,
                "settings": {"powervm_mgmt_capable": False},
            },
            raise_on_error=False,
        )
    assert result.is_error
    assert "is not permitted on" in str(result.content)
    b.hmc.list_logical_partitions.assert_not_awaited()
    b.hmc.get_logical_partition.assert_not_awaited()
    b.hmc.get_managed_system.assert_not_awaited()
    assert b.writes == []


@pytest.mark.parametrize("creation", [False, True])
@pytest.mark.parametrize(
    ("settings", "stage"),
    [
        ({"powervm_mgmt_capable": False}, "system"),
        ({"powervm_mgmt_capable": True}, "system"),
        ({"kvm_capable": False}, "system"),
        ({"kvm_capable": True}, "system"),
        ({"kvm_capable": False}, "console"),
        ({"kvm_capable": True}, "console"),
        ({"powervm_mgmt_capable": False, "kvm_capable": False}, "console"),
        ({"powervm_mgmt_capable": True, "kvm_capable": True}, "console"),
    ],
)
@pytest.mark.asyncio
async def test_mcp_role_preflight_remote_diagnostic_privacy(
    role_app_boundary, creation, settings, stage
):
    b = role_app_boundary
    marker = "PRIVATE-ROLE-PREFLIGHT-MARKER"
    failure = HMCError("Remote evidence failed", 503, f"<Message>{marker}</Message>")
    before = deepcopy(b.resource)
    getattr(
        b.hmc, "get_managed_system" if stage == "system" else "get_console_info"
    ).side_effect = failure
    if creation:
        b.hmc.find_partition_by_name.return_value = None
    name = "hmc_create_lpar" if creation else "hmc_configure_lpar"
    async with Client(policy_app(name)) as client:
        result = await client.call_tool(
            name,
            {
                "system_name_or_uuid": SYSTEM,
                "name" if creation else "lpar_name_or_uuid": "new"
                if creation
                else PARTITION,
                "settings": settings,
            },
            raise_on_error=False,
        )
    assert result.is_error
    assert marker not in str(result)
    assert "Linux role preflight" in str(result.content)
    assert "no changes were submitted" in str(result.content)
    assert "503" in str(result.content)
    assert b.writes == []
    assert b.resource == before
    assert marker in str(failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["partition", "baseline"])
@pytest.mark.parametrize(
    "settings",
    [
        {"powervm_mgmt_capable": False},
        {"powervm_mgmt_capable": True},
        {"kvm_capable": False},
        {"powervm_mgmt_capable": False, "kvm_capable": False},
    ],
)
async def test_mcp_configuration_preflight_read_privacy(
    role_app_boundary, stage, settings
):
    from hmcpctl.ssh.transport import HMCCLIError

    b = role_app_boundary
    b.system["AssociatedSystemCapabilities"]["KvmOnPowerVMCapable"] = "true"
    marker = "PRIVATE-CONFIG-PREFLIGHT-MARKER"
    kind = HMCError if stage == "partition" else HMCCLIError
    failure = kind("Remote evidence unavailable", 503, f"<Message>{marker}</Message>")
    before = deepcopy(b.resource)
    if stage == "partition":
        b.hmc.get_logical_partition.side_effect = failure
    else:
        b.state["baseline_error"] = failure
    async with Client(policy_app("hmc_configure_lpar")) as client:
        result = await client.call_tool(
            "hmc_configure_lpar",
            {
                "system_name_or_uuid": SYSTEM,
                "lpar_name_or_uuid": PARTITION,
                "settings": settings,
            },
            raise_on_error=False,
        )
    assert result.is_error and marker not in str(result)
    assert "Linux role preflight" in str(
        result.content
    ) and "no changes were submitted" in str(result.content)
    assert "503" in str(result.content)
    assert not b.writes and b.resource == before
    assert marker in str(failure)


@pytest.mark.parametrize("stage", ["partition", "baseline"])
@pytest.mark.parametrize("selected", [False, True])
def test_cli_configuration_preflight_read_privacy(
    role_app_boundary, monkeypatch, stage, selected
):
    from contextlib import asynccontextmanager

    from hmcpctl.cli_commands import runtime
    from hmcpctl.ssh.transport import HMCCLIError

    b = role_app_boundary
    marker = "PRIVATE-CLI-PREFLIGHT-MARKER"
    kind = HMCError if stage == "partition" else HMCCLIError
    failure = kind("Remote evidence unavailable", 503, f"<Message>{marker}</Message>")
    before = deepcopy(b.resource)
    if stage == "partition":
        b.hmc.get_logical_partition.side_effect = failure
    else:
        b.state["baseline_error"] = failure

    @asynccontextmanager
    async def client_boundary():
        yield b.hmc

    monkeypatch.setattr(runtime, "client", client_boundary)
    flag = "--powervm-mgmt-capable" if selected else "--no-powervm-mgmt-capable"
    result = CliRunner().invoke(app, ["lpars", "configure", PARTITION, SYSTEM, flag])
    assert result.exit_code == 1 and marker not in result.output
    output = " ".join(result.output.split())
    assert "Linux role preflight" in output and "no changes were submitted" in output
    assert "503" in result.output
    assert not b.writes and b.resource == before
