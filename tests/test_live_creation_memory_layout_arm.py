"""ST58 real verification/oracle and inactive fresh-resource boundaries."""

from __future__ import annotations

import json
import shlex
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.operations.lpar import core

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import config as settings
from live_test import lpar_config, runner

SYSTEM = "00000000-0000-0000-0000-000000000001"
LPAR = "00000000-0000-0000-0000-000000000002"
VIOS = "00000000-0000-0000-0000-000000000003"
OLD = "00000000-0000-0000-0000-000000000004"
HEADER = "lpar_name,name,min_num_huge_pages,desired_num_huge_pages,max_num_huge_pages,hpt_ratio,ppt_ratio,kvm_reserved_mem_percent"
SIX = {
    "min_num_huge_pages": 0,
    "desired_num_huge_pages": 0,
    "max_num_huge_pages": 0,
    "hpt_ratio": "1:128",
    "ppt_ratio": "1:256",
    "kvm_reserved_mem_percent": "0",
}


@pytest.fixture
def target(monkeypatch):
    from live_test import creation_memory_layout as arm

    env = SimpleNamespace(
        arm=arm,
        fault="",
        calls=[],
        lpar=None,
        vios=None,
        mode_reads=0,
        checkpoints=[],
        token="",
        name="",
        vname="",
        product_status=None,
    )
    config = settings.LiveTestConfig(
        system_name=SYSTEM,
        creation_memory_layout_hpt_ratio="1:128",
        creation_memory_layout_ppt_ratio="1:256",
        creation_memory_layout_fresh_vios_delete_authorized=True,
    )
    state = runner.RunState(
        config=config,
        group=arm.GROUP,
        hmc_config=HMCConfig.from_mapping({"host": "hmc.test", "user": "test"}),
    )
    env.state = state
    state.schemas = {
        name: {}
        for name in (
            "hmc_run_command",
            "hmc_create_lpar",
            "hmc_create_vios",
            "hmc_delete_lpar",
            "hmc_delete_vios",
            "hmc_get_lpar_description",
            "hmc_get_lpar_state",
        )
    }

    def entry(identity, name, kind):
        fault = env.fault
        prefix = "vios-" if kind == "Virtual IO Server" else ""
        return {
            "UUID": OLD if fault == prefix + "identity" else identity,
            "Resource": {
                "PartitionName": "foreign" if fault == prefix + "name" else name,
                "PartitionType": "OS400" if fault == prefix + "type" else kind,
                "PartitionState": "running"
                if fault == prefix + "active"
                else "not activated",
            },
        }

    async def partition(identity):
        assert identity == LPAR
        if env.fault == "unreadable":
            raise OSError("unavailable")
        return entry(LPAR, env.name, "AIX/Linux")

    async def vios_detail(identity):
        if identity == OLD:
            return entry(OLD, "existing", "Virtual IO Server")
        assert identity == VIOS
        return entry(VIOS, env.vname, "Virtual IO Server")

    async def list_vios(system):
        assert system == SYSTEM
        if env.fault == "vios-feed-unreadable":
            raise OSError("unavailable")
        rows = [{"UUID": OLD, "Resource": {"PartitionName": "existing"}}]
        if env.vios:
            rows.append(entry(VIOS, env.vname, "Virtual IO Server"))
        return rows

    hmc = SimpleNamespace(
        config=state.hmc_config,
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM, "SystemName": "canonical-system"}]
        ),
        get_managed_system=AsyncMock(
            return_value={
                "UUID": SYSTEM,
                "Resource": {
                    "SystemName": "canonical-system",
                    "IsClassicHMCManagement": "true",
                    "IsHMCPowerVMManagementController": "false",
                    "IsPowerVMManagementController": "false",
                },
            }
        ),
        list_logical_partitions=AsyncMock(
            side_effect=lambda system: (
                []
                if env.fault == "foreign-system"
                else [entry(LPAR, env.name, "AIX/Linux")]
                if env.lpar
                else []
            )
        ),
        get_logical_partition=AsyncMock(side_effect=partition),
        list_vios=AsyncMock(side_effect=list_vios),
        get_vios=AsyncMock(side_effect=vios_detail),
    )
    env.hmc = hmc

    @asynccontextmanager
    async def session(config):
        assert config is state.hmc_config
        yield hmc

    monkeypatch.setattr(arm, "HMCClient", session)
    monkeypatch.setattr(lpar_config.asyncio, "sleep", AsyncMock())

    async def mode(config, command):
        assert command == "lscomgmt -m canonical-system -F is_controller"
        env.mode_reads += 1
        if env.fault == "mode-unknown":
            return "unknown"
        return (
            "1"
            if env.fault in {"controller", "oracle-controller"}
            or (env.fault == "mode-drift" and env.mode_reads > 1)
            else "null"
        )

    monkeypatch.setattr("hmcpctl.operations.lpar.linux_roles.run_hmc_command", mode)

    def raw(name, product=False):
        values = [name, "default_profile", "0", "0", "0", "1:128", "1:256", "0.00"]
        fault = env.fault
        if (
            fault in {"raw-mismatch", "false-product"}
            or fault == "vios-raw-mismatch"
            and env.vios
        ) and not product:
            values[5] = "1:64"
        if fault == "product-mismatch" and product:
            values[5] = "1:64"
        if fault == "count-nonzero" and not product:
            values[2] = "1"
        if fault == "count-malformed" and not product:
            values[3] = "٠"
        if fault == "reservation-nonfinite" and not product:
            values[7] = "NaN"
        if fault == "raw-missing" and not product:
            values[7] = "null"
        if fault == "vios-raw-missing" and env.vios and not product:
            values[5] = "null"
        if fault == "raw-both" and not product:
            values[5], values[7] = "1:64", "null"
        if fault == "foreign-row" and not product:
            values[0] = "foreign"
        header = HEADER
        if fault == "header" and not product:
            header = header.replace("hpt_ratio,ppt_ratio", "ppt_ratio,hpt_ratio")
        if fault == "width" and not product:
            values.append("extra")
        text = header + "\n" + ",".join(values)
        return text + (
            "\n" + ",".join(values) if fault == "duplicate" and not product else ""
        )

    async def product_read(config, command):
        assert command.startswith("lssyscfg -r prof ")
        return raw(env.name, product=True)

    monkeypatch.setattr("hmcpctl.ssh.lpar_memory_layout.run_hmc_command", product_read)

    def checkpoint():
        journal = state.artifacts.creation_memory_layout
        env.checkpoints.append(deepcopy(journal))
        settings.decode_artifacts(asdict(state.artifacts))
        if (
            env.fault == "checkpoint"
            or (
                env.fault == "lpar-checkpoint"
                and journal["phase"] == "lpar-create-returned"
            )
            or (
                env.fault == "vios-checkpoint"
                and journal["phase"] == "vios-create-returned"
            )
        ):
            raise OSError("checkpoint unavailable")

    state.checkpoint = checkpoint

    async def call(client, tool, **args):
        env.calls.append((tool, deepcopy(args)))
        if tool == "hmc_run_command":
            command = args["cmd"]
            if command.startswith("lssyscfg -r prof"):
                parsed = shlex.split(command)
                assert parsed[parsed.index("-m") + 1] == "canonical-system"
                assert parsed[parsed.index("-F") + 1] == HEADER
                assert parsed[-1] == "--header"
                assert (
                    parsed[parsed.index("--filter") + 1]
                    == f"lpar_names={env.vname if env.vios else env.name},profile_names=default_profile"
                )
                return "PASS", raw(env.vname if env.vios else env.name)
            if command.endswith("-F name"):
                if env.fault == "lpar-absence" and env.name and env.lpar is None:
                    return "FAIL", "unknown absence"
                return "PASS", "existing" + ("\n" + env.name if env.lpar else "")
            if command.endswith("curr_avail_sys_proc_units"):
                return "PASS", "10.0"
            assert command.endswith("curr_avail_sys_mem,mem_region_size")
            return (
                "PASS",
                "4096,256"
                if env.fault == "pool-drift"
                and env.name
                or env.fault == "vios-pool-drift"
                and env.vname
                else "8192,256",
            )
        if tool == "hmc_create_lpar":
            assert env.checkpoints[-1]["phase"] == "lpar-create-pending"
            env.name, env.token = args["name"], args["caller_token"]
            if env.fault == "rejected":
                return "FAIL", "HSCLA4A0"
            env.lpar = LPAR
            if env.fault == "unknown":
                return "FAIL", "unknown response"
            layout = TypeAdapter(LparMemoryLayout).validate_python(
                json.loads(json.dumps(args["memory_layout"]))
            )
            creation = core.LparCreation(
                env.name, "AIX/Linux", LparResources(), memory_layout=layout
            )
            step = await core._created_memory_layout_step(
                hmc,
                "canonical-system",
                creation,
                controller_mode=env.fault == "controller",
            )
            env.product_status = step.result.status
            return "PASS", {
                "lpar": {} if env.fault == "no-uuid" else {"UUID": LPAR},
                "workflow_completed": step.status == "ok",
                "ownership_stamped": env.fault != "unstamped",
                "memory_layout_step": asdict(step),
            }
        if tool == "hmc_get_lpar_description":
            token = "foreign" if env.fault == "token" else env.token
            return "PASS", f"[hmcpctl owner:test created:2026-01-01] [caller {token}]"
        if tool == "hmc_get_lpar_state":
            return "PASS", "Running" if env.fault == "active" else "Not Activated"
        if tool == "hmc_delete_lpar":
            assert args == {
                "system_name_or_uuid": "canonical-system",
                "lpar_name_or_uuid": LPAR,
            }
            assert env.checkpoints[-1]["phase"] == "lpar-delete-pending"
            if env.fault == "lpar-delete":
                return "FAIL", "unknown"
            env.lpar = None
            return "PASS", {}
        if tool == "hmc_create_vios":
            assert env.checkpoints[-1]["phase"] == "vios-create-pending"
            env.vname, env.vios = args["name"], VIOS
            if env.fault in {"vios-unknown", "vios-incomplete"}:
                return (
                    ("FAIL", "unknown") if env.fault == "vios-unknown" else ("PASS", {})
                )
            return "PASS", entry(
                OLD if env.fault == "baseline-uuid" else VIOS,
                env.vname,
                "Virtual IO Server",
            )
        if tool == "hmc_delete_vios":
            assert env.checkpoints[-1]["phase"] == "vios-delete-pending"
            assert args == {"vios_name_or_uuid": VIOS, "system_name_or_uuid": SYSTEM}
            if env.fault == "vios-delete":
                return "FAIL", "unknown"
            if env.fault != "vios-absence":
                env.vios = None
            return "PASS", VIOS
        raise AssertionError(f"unexpected effect {tool}")

    state.call = AsyncMock(side_effect=call)
    env.raw = raw
    return env


async def run(target):
    await target.arm.exercise_creation_memory_layout(object(), target.state)


def effects(target):
    return [
        name
        for name, _ in target.calls
        if name
        in {"hmc_create_lpar", "hmc_delete_lpar", "hmc_create_vios", "hmc_delete_vios"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["group", "transport", "checkpoint"])
async def test_group_absence_or_missing_transport_skips_before_effects(target, fault):
    if fault == "group":
        target.state.group = "all"
    elif fault == "transport":
        target.state.hmc_config = None
    else:
        target.state.checkpoint = None
    await run(target)
    assert effects(target) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["hpt", "ppt", "bad", "bad-ppt", "grant", "create-only"]
)
async def test_unarmed_or_invalid_config_has_zero_mutations(target, fault):
    key, value = {
        "hpt": ("creation_memory_layout_hpt_ratio", ""),
        "ppt": ("creation_memory_layout_ppt_ratio", ""),
        "bad": ("creation_memory_layout_hpt_ratio", "bad token"),
        "bad-ppt": ("creation_memory_layout_ppt_ratio", "bad token"),
        "grant": ("creation_memory_layout_fresh_vios_delete_authorized", False),
        "create-only": ("creation_memory_layout_fresh_vios_delete_authorized", False),
    }[fault]
    target.state.config = replace(target.state.config, **{key: value})
    await run(target)
    assert effects(target) == []
    assert target.calls == []
    assert target.state.artifacts.creation_memory_layout == {}
    assert len(target.state.results) == 1
    row = target.state.results[0]
    assert row["status"] == "SKIP"
    assert row["note"]
    assert (
        "ratio" if fault in {"hpt", "ppt", "bad", "bad-ppt"} else "permission"
    ) in row["note"]


@pytest.mark.asyncio
async def test_six_fields_pass_exactly_without_role_or_hardware_admission(target):
    await run(target)
    assert all(row["status"] == "PASS" for row in target.state.results)
    arguments = dict(target.calls)["hmc_create_lpar"]
    assert arguments["memory_layout"] == SIX
    assert type(arguments["memory_layout"]["min_num_huge_pages"]) is int
    assert arguments["apply_partition_profile"] is False
    assert set(arguments) == {
        "system_name_or_uuid",
        "name",
        "partition_type",
        "resources",
        "caller_token",
        "memory_layout",
        "apply_partition_profile",
    }
    assert target.state.artifacts.creation_memory_layout["phase"] == "complete"
    assert target.product_status == "verified"
    assert [
        (o["operation"], o["observation"]["scenario"])
        for o in target.state.observations
    ] == [
        ("lpar.create", "st58-lpar-memory-layout"),
        ("vios.create", "st58-vios-page-table-ratios"),
    ]
    assert all(
        o["observation"]["result"] == "passed" for o in target.state.observations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "raw-mismatch",
        "raw-missing",
        "raw-both",
        "count-nonzero",
        "count-malformed",
        "reservation-nonfinite",
        "duplicate",
        "foreign-row",
        "mode-drift",
        "mode-unknown",
        "controller",
        "oracle-controller",
        "identity",
        "name",
        "foreign-system",
    ],
)
async def test_lpar_independent_readback_gates_vios(target, fault):
    target.fault = fault
    await run(target)
    assert any(row["status"] == "FAIL" for row in target.state.results)
    assert "hmc_create_vios" not in effects(target)
    assert not target.state.observations
    if fault in {"controller", "oracle-controller", "mode-unknown"}:
        assert not any("-r prof" in args.get("cmd", "") for _, args in target.calls)
    if fault == "raw-both":
        assert (
            target.state.artifacts.creation_memory_layout["outcome"]["lpar"]
            == "mismatch"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["rejected", "unknown", "no-uuid", "checkpoint", "lpar-checkpoint", "unstamped"],
)
async def test_lpar_uncertainty_stops_next_fixture(target, fault):
    target.fault = fault
    await run(target)
    assert any(row["status"] == "FAIL" for row in target.state.results)
    assert "hmc_create_vios" not in effects(target)
    assert "hmc_delete_lpar" not in effects(target)
    assert not target.state.observations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["active", "unreadable", "token", "lpar-delete", "lpar-absence", "pool-drift"],
)
async def test_lpar_cleanup_stays_inactive_and_owned(target, fault):
    target.fault = fault
    await run(target)
    assert any(row["status"] == "FAIL" for row in target.state.results)
    assert "hmc_create_vios" not in effects(target)
    assert not target.state.observations
    if fault in {"active", "unreadable", "token"}:
        assert "hmc_delete_lpar" not in effects(target)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["baseline-uuid", "vios-identity", "vios-name", "vios-type", "vios-active"]
)
async def test_vios_requires_fresh_delete_authority_and_returned_uuid(target, fault):
    target.fault = fault
    await run(target)
    assert any(row["status"] == "FAIL" for row in target.state.results)
    assert "hmc_delete_vios" not in effects(target)
    assert len(target.state.observations) == 1
    if fault == "baseline-uuid":
        assert target.state.artifacts.creation_memory_layout["vios_uuid"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["vios-unknown", "vios-incomplete", "vios-checkpoint"]
)
async def test_vios_partial_name_rediscovery_is_never_adopted(target, fault):
    target.fault = fault
    await run(target)
    assert any(row["status"] == "FAIL" for row in target.state.results)
    assert target.vios == VIOS
    assert "hmc_delete_vios" not in effects(target)
    journal = target.state.artifacts.creation_memory_layout
    if fault != "vios-checkpoint":
        assert journal["vios_uuid"] is None


@pytest.mark.asyncio
async def test_vios_delete_uses_supported_uuid_scoped_tool(target):
    await run(target)
    assert [args for tool, args in target.calls if tool == "hmc_delete_vios"] == [
        {"vios_name_or_uuid": VIOS, "system_name_or_uuid": SYSTEM}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["vios-delete", "vios-absence", "vios-active", "vios-name"]
)
async def test_vios_delete_never_retries_or_powers_off(target, fault):
    target.fault = fault
    await run(target)
    assert any(row["status"] == "FAIL" for row in target.state.results)
    assert effects(target).count("hmc_delete_vios") <= 1
    assert not any("power" in name for name, _ in target.calls)
    assert not any(o["operation"] == "vios.create" for o in target.state.observations)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["raw-mismatch", "raw-missing", "lpar-delete", "pool-drift"]
)
async def test_observation_requires_independent_values_and_cleanup(target, fault):
    target.fault = fault
    await run(target)
    assert not target.state.observations


@pytest.mark.asyncio
async def test_finite_matrix_has_no_unrelated_effects(target):
    await run(target)
    assert effects(target) == [
        "hmc_create_lpar",
        "hmc_delete_lpar",
        "hmc_create_vios",
        "hmc_delete_vios",
    ]
    assert {name for name, _ in target.calls} <= {
        "hmc_run_command",
        "hmc_create_lpar",
        "hmc_create_vios",
        "hmc_get_lpar_description",
        "hmc_get_lpar_state",
        "hmc_delete_lpar",
        "hmc_delete_vios",
    }
    assert dict(target.calls)["hmc_create_vios"]["memory_layout"] == {
        "hpt_ratio": "1:128",
        "ppt_ratio": "1:256",
    }
    assert dict(target.calls)["hmc_create_vios"]["resources"] == lpar_config.RESOURCES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["old", "invocation", "candidate", "hmc", "system", "grant"]
)
async def test_restored_authority_never_authorizes_current_delete(target, fault):
    await run(target)
    before = effects(target)
    journal = target.state.artifacts.creation_memory_layout
    journal["deletion"]["vios"] = "not-attempted"
    target.vios = VIOS
    if fault == "old":
        target.state.creation_memory_layout_invocation = None
    elif fault == "invocation":
        journal["invocation_id"] = "f" * 32
    elif fault == "candidate":
        journal["candidate_commit"] = "f" * 40
    elif fault == "hmc":
        journal["hmc_identity"]["host"] = "foreign.test"
    elif fault == "system":
        journal["system_uuid"] = OLD
    else:
        target.state.config = replace(
            target.state.config,
            creation_memory_layout_fresh_vios_delete_authorized=False,
        )
    with pytest.raises((ValueError, TypeError)):
        await target.arm._delete_fresh_vios(
            object(), target.state, SYSTEM, VIOS, target.vname
        )
    assert effects(target) == before


@pytest.mark.asyncio
async def test_product_getter_false_positive_disagrees_with_raw_oracle(
    target, monkeypatch
):
    target.fault = "false-product"
    getter = AsyncMock(
        return_value=LparMemoryLayout(0, 0, 0, "1:128", "1:256", Decimal(0))
    )
    monkeypatch.setattr(core, "get_lpar_memory_layout", getter)
    await run(target)
    assert any(row["status"] == "FAIL" for row in target.state.results)
    getter.assert_awaited_once()
    assert target.product_status == "verified"
    oracle_rows = [
        row
        for row in target.state.results
        if row["tool"] == "hmc_run_command (independent memory layout)"
    ]
    assert len(oracle_rows) == 1 and ",1:64,1:256," in oracle_rows[0]["data"]
    assert (
        target.state.artifacts.creation_memory_layout["outcome"]["lpar"] == "mismatch"
    )
    assert "hmc_create_vios" not in effects(target)
    assert not target.state.observations


@pytest.mark.asyncio
async def test_raw_oracle_match_never_overrides_failed_creation(target):
    target.fault = "product-mismatch"
    await run(target)
    assert any(row["status"] == "FAIL" for row in target.state.results)
    assert target.product_status == "mismatch"
    assert "hmc_create_vios" not in effects(target)
    assert not target.state.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["header", "width", "duplicate", "foreign-row"])
async def test_literal_raw_projection_and_headers_are_independent(target, fault):
    target.fault = fault
    await run(target)
    assert not target.state.observations
    assert "hmc_create_vios" not in effects(target)


@pytest.mark.asyncio
async def test_request_json_dump_uses_literal_shapes_and_exact_strings(target):
    await run(target)
    lpar = next(args for name, args in target.calls if name == "hmc_create_lpar")
    assert type(lpar["memory_layout"]["kvm_reserved_mem_percent"]) is str
    assert json.loads(json.dumps(lpar["memory_layout"])) == SIX
    huge = TypeAdapter(LparMemoryLayout).dump_json(
        LparMemoryLayout(kvm_reserved_mem_percent=Decimal("1" + "0" * 4094 + "1"))
    )
    assert json.loads(huge)["kvm_reserved_mem_percent"] == "1" + "0" * 4094 + "1"


@pytest.mark.asyncio
async def test_independent_read_refuses_changed_token_even_with_valid_uuid(target):
    await run(target)
    target.fault, target.lpar = "token", LPAR
    fixture = lpar_config._Run(
        "canonical-system", target.name, target.token, 256, uuid=LPAR, subtask=58
    )
    with pytest.raises(ValueError, match="ownership"):
        await target.arm._read_lpar(object(), target.state, fixture, SYSTEM)


@pytest.mark.asyncio
async def test_saved_journal_without_current_invocation_never_starts(target):
    await run(target)
    before = list(target.calls)
    target.state.creation_memory_layout_invocation = None
    await run(target)
    assert target.calls == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    [
        "hmc_run_command",
        "hmc_create_lpar",
        "hmc_create_vios",
        "hmc_delete_lpar",
        "hmc_delete_vios",
        "hmc_get_lpar_description",
        "hmc_get_lpar_state",
    ],
)
async def test_missing_served_tool_or_policy_stops_before_mutation(target, tool):
    del target.state.schemas[tool]
    await run(target)
    assert effects(target) == []


@pytest.mark.asyncio
async def test_unknown_vios_delete_never_reports_cleanup_success(target):
    await run(target)
    target.state.artifacts.creation_memory_layout["deletion"]["vios"] = "not-attempted"
    target.vios, target.fault = VIOS, "vios-delete"
    assert not await target.arm._delete_fresh_vios(
        object(), target.state, SYSTEM, VIOS, target.vname
    )
    assert (
        target.state.artifacts.creation_memory_layout["deletion"]["vios"] == "unknown"
    )


def test_existing_operation_test_bindings_include_the_isolated_arm():
    root = Path(__file__).parents[1]
    operations = json.loads((root / "docs/capabilities/operations.json").read_text())
    selected = [
        row
        for row in operations["operations"]
        if row["tool"] in {"hmc_create_lpar", "hmc_create_vios"}
    ]
    assert len(selected) == 2
    assert all(
        "tests/test_live_creation_memory_layout_arm.py" in row["tests"]
        for row in selected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "raw-mismatch",
        "raw-missing",
        "no-uuid",
        "unstamped",
        "pool-drift",
        "vios-incomplete",
        "vios-raw-mismatch",
        "vios-raw-missing",
        "vios-absence",
        "vios-pool-drift",
    ],
)
async def test_unsuccessful_fixture_exits_actual_runner_nonzero(
    target, monkeypatch, tmp_path, fault
):
    target.fault = fault
    fixture_checkpoint = target.state.checkpoint

    @asynccontextmanager
    async def served_client():
        runner_checkpoint = target.state.checkpoint

        def checkpoint():
            fixture_checkpoint()
            runner_checkpoint()

        target.state.checkpoint = checkpoint
        yield object()

    # Reuse the fixture's tool/HMC boundaries; the real runner owns dispatch, checkpoint,
    # result serialization, summary and exit status.
    monkeypatch.setattr(runner, "served_client", served_client)
    monkeypatch.setattr(
        runner, "served_schemas", AsyncMock(return_value=target.state.schemas)
    )
    monkeypatch.setattr(runner, "RunState", lambda **_kwargs: target.state)
    monkeypatch.setattr(runner.evidence, "emit_observations", lambda *_args: None)
    destination = tmp_path / "results.json"
    result = await runner.main(
        group=target.arm.GROUP,
        results_path=str(destination),
        config=target.state.config,
        hmc_config=target.state.hmc_config,
    )
    assert result == 1
    saved = json.loads(destination.read_text())
    assert any(row["status"] == "FAIL" for row in saved["results"])
    assert saved["artifacts"]["creation_memory_layout"]["phase"] == "stopped-uncertain"
    if fault.startswith("vios-"):
        assert [row["operation"] for row in target.state.observations] == [
            "lpar.create"
        ]
    else:
        assert "hmc_create_vios" not in effects(target)
        assert not target.state.observations
    assert effects(target).count("hmc_create_lpar") == 1
    assert effects(target).count("hmc_create_vios") <= 1
    assert effects(target).count("hmc_delete_vios") <= 1
