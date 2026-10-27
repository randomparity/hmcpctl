"""ST65 fixed cases and single-use mutation boundaries."""

import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.xmlutil import ATOM_NS

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import config, runner


def armed(case="shared-lpar", **kwargs):
    return config.LiveTestConfig(
        creation_memory_case=case,
        creation_memory_primary_vios_name="paging-a" if "lpar" in case else "",
        creation_memory_fresh_delete_authorized=True,
        **kwargs,
    )


@pytest.mark.parametrize(
    "case,kind,pairs",
    [
        (
            "shared-lpar",
            "LogicalPartition",
            {
                "mem_mode": "shared",
                "desired_io_entitled_mem": "auto",
                "mem_weight": 0,
                "primary_paging_vios_name": "paging-a",
                "secondary_paging_vios_id": "none",
            },
        ),
        ("ame-zero-vios", "VirtualIOServer", {"mem_mode": "ded", "mem_expansion": "0"}),
        (
            "ame-positive-vios",
            "VirtualIOServer",
            {"mem_mode": "ded", "mem_expansion": "1.1"},
        ),
    ],
)
def test_fixed_cases(case, kind, pairs):
    from live_test import creation_memory as arm

    actual_kind, memory = arm.configured(armed(case))
    assert actual_kind == kind
    assert dict(memory.pairs()) == pairs


@pytest.mark.parametrize(
    "changes",
    [
        {"creation_memory_case": ""},
        {"creation_memory_case": "unknown"},
        {"creation_memory_primary_vios_name": ""},
        {"creation_memory_secondary_vios_name": "unexpected"},
        {"creation_memory_fresh_delete_authorized": False},
        {"creation_memory_case": "ame-zero-vios"},
        {
            "creation_memory_case": "redundant-lpar",
            "creation_memory_secondary_vios_name": "paging-a",
        },
    ],
)
def test_invalid_configuration_refuses(changes):
    from live_test import creation_memory as arm

    with pytest.raises((ValueError, TypeError)):
        arm.configured(replace(armed(), **changes))


def test_redundant_is_explicit_and_decimal_json_is_number():
    from live_test import creation_memory as arm

    _, memory = arm.configured(
        armed("redundant-lpar", creation_memory_secondary_vios_name="paging-b")
    )
    assert dict(memory.pairs())["secondary_paging_vios_name"] == "paging-b"
    assert "secondary_paging_vios_id" not in dict(memory.pairs())
    _, memory = arm.configured(armed("ame-positive-vios"))
    assert memory.mem_expansion == Decimal("1.1")
    assert arm._tool_memory(memory) == {"mem_mode": "ded", "mem_expansion": 1.1}


@pytest.mark.asyncio
async def test_unarmed_skip_before_effects():
    from live_test import creation_memory as arm

    from hmcpctl.config import HMCConfig

    state = runner.RunState(group=arm.GROUP, hmc_config=HMCConfig.from_mapping({}))
    state.checkpoint = lambda: pytest.fail("unarmed checkpoint")
    await arm.exercise_creation_memory(None, state)
    assert len(state.results) == 1
    assert state.results[0]["status"] == "SKIP"
    assert state.artifacts.creation_memory == {}


# Synthetic raw Atom/UOM bodies follow the retained schemas in ADR 0299's spec.
# No fixture is a copied native capture and no product verifier supplies the oracle.
SYSTEM = "11111111-1111-4111-8111-111111111111"
FRESH = "22222222-2222-4222-8222-222222222222"
PRIMARY = "33333333-3333-4333-8333-333333333333"
SECONDARY = "44444444-4444-4444-8444-444444444444"
PROFILE = "55555555-5555-4555-8555-555555555555"


def xml_entry(identity, name, kind, fields=""):
    environment = "AIX/Linux" if kind == "LogicalPartition" else "Virtual IO Server"
    return (
        f'<entry xmlns="{ATOM_NS}"><id>urn:uuid:{identity}</id><content>'
        f'<{kind} xmlns="{UOM_NS}"><PartitionName>{name}</PartitionName>'
        f"<PartitionID>7</PartitionID><PartitionType>{environment}</PartitionType>"
        f"<PartitionState>not activated</PartitionState>{fields}</{kind}>"
        "</content></entry>"
    )


def feed(rows):
    return f'<feed xmlns="{ATOM_NS}">' + "".join(rows) + "</feed>"


@pytest.fixture
def native_target(monkeypatch):
    from live_test import creation_memory as arm

    state = runner.RunState(
        group=arm.GROUP,
        config=replace(armed(), system_name=SYSTEM),
        hmc_config=HMCConfig.from_mapping({"host": "hmc.test", "user": "test"}),
    )
    state.schemas = {
        name: {}
        for name in (
            "hmc_run_command",
            "hmc_create_lpar",
            "hmc_create_vios",
            "hmc_delete_lpar",
            "hmc_delete_vios",
            "hmc_get_lpar_description",
        )
    }
    env = SimpleNamespace(
        state=state,
        arm=arm,
        fault="",
        calls=[],
        checkpoints=[],
        exists=False,
        name="",
        token="",
        mode_reads=0,
    )

    def row(identity, name, kind):
        xml = xml_entry(identity, name, kind)
        if identity == FRESH:
            if env.fault == "active":
                xml = xml.replace("not activated", "running")
            if env.fault == "wrong-name":
                xml = xml.replace(name, "foreign")
            if env.fault == "wrong-kind":
                xml = xml.replace("AIX/Linux", "OS400")
        return xml

    def vios_rows():
        rows = [
            row(PRIMARY, "paging-a", "VirtualIOServer"),
            row(SECONDARY, "paging-b", "VirtualIOServer"),
        ]
        if env.exists and "vios" in state.config.creation_memory_case:
            rows.append(row(FRESH, env.name, "VirtualIOServer"))
        return rows

    async def raw(path, **kwargs):
        assert kwargs["uuid_path_arguments"]
        if path.endswith("VirtualIOServer"):
            result = feed(vios_rows())
        elif path.endswith("LogicalPartition"):
            result = feed(
                [row(FRESH, env.name, "LogicalPartition")]
                if env.exists and "lpar" in state.config.creation_memory_case
                else []
            )
        elif path.endswith(PRIMARY):
            result = row(PRIMARY, "paging-a", "VirtualIOServer")
        elif path.endswith(SECONDARY):
            result = row(SECONDARY, "paging-b", "VirtualIOServer")
        else:
            assert path.endswith(FRESH)
            kind = (
                "VirtualIOServer"
                if "vios" in state.config.creation_memory_case
                else "LogicalPartition"
            )
            fields = "<PartitionMemoryConfiguration><SharedMemoryEnabled>true</SharedMemoryEnabled><AutoEntitledMemoryEnabled>true</AutoEntitledMemoryEnabled><CurrentMemoryWeight>0</CurrentMemoryWeight></PartitionMemoryConfiguration>"
            result = xml_entry(FRESH, env.name, kind, fields)
            if env.fault in {"active", "wrong-name", "wrong-kind"}:
                result = row(FRESH, env.name, kind)
        if env.fault == "continuation":
            result = result.replace(
                "</feed>", '<link rel="next" href="https://foreign.test/next"/></feed>'
            )
        if env.fault == "oversized-feed":
            result = "x" * (arm.LIMIT + 1)
        if (
            env.fault == "foreign-system"
            and env.exists
            and path.endswith("LogicalPartition")
        ):
            result = feed([])
        return result

    hmc = SimpleNamespace(
        config=state.hmc_config,
        _get=AsyncMock(side_effect=raw),
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM, "SystemName": "system-test"}]
        ),
        get_managed_system=AsyncMock(
            return_value={
                "UUID": SYSTEM,
                "Resource": {
                    "SystemName": "system-test",
                    "IsClassicHMCManagement": "true",
                    "IsHMCPowerVMManagementController": "false",
                    "IsPowerVMManagementController": "false",
                },
            }
        ),
    )
    env.hmc = hmc

    @asynccontextmanager
    async def session(config):
        assert config is state.hmc_config
        yield hmc

    monkeypatch.setattr(arm, "HMCClient", session)
    monkeypatch.setattr(arm, "_candidate", lambda: "b" * 40)

    async def mode(config, command):
        assert command == "lscomgmt -m system-test -F is_controller"
        env.mode_reads += 1
        if env.fault == "unknown-mode":
            return "unknown"
        if state.config.creation_memory_case == "controller-shared-lpar" or (
            env.fault == "mode-drift" and env.mode_reads > 1
        ):
            return "1"
        return "null"

    monkeypatch.setattr("hmcpctl.operations.lpar.linux_roles.run_hmc_command", mode)

    async def inventory(hmc, system, parent, *, partition_type):
        assert system == SYSTEM and parent == FRESH
        selected = SimpleNamespace(uuid=PROFILE, name="default_profile")
        return SimpleNamespace(
            system_uuid=system,
            partition_uuid=parent,
            partition_type=partition_type,
            profiles=(selected, selected)
            if env.fault == "ambiguous-profile"
            else (selected,),
        )

    monkeypatch.setattr(arm.profiles, "list_partition_profiles", inventory)

    async def native(hmc, kind, parent, profile, system):
        assert (parent, profile, system) == (FRESH, PROFILE, SYSTEM)
        request = state.artifacts.creation_memory["requested"]
        if "mem_expansion" in request:
            enabled = "false" if request["mem_expansion"] == "0" else "true"
            fields = f"<ActiveMemorySharingEnabled>false</ActiveMemorySharingEnabled><ActiveMemoryExpansionEnabled>{enabled}</ActiveMemoryExpansionEnabled><ExpansionFactor>1.100</ExpansionFactor>"
            if request["mem_expansion"] == "0":
                fields = fields.replace("1.100", "stale-ignored")
        else:
            fields = "<ActiveMemorySharingEnabled>true</ActiveMemorySharingEnabled><ManualEntitledModeEnabled>false</ManualEntitledModeEnabled><MemoryWeight>0</MemoryWeight>"
            fields += f'<PrimaryPagingServicePartition><link href="https://hmc.test/rest/api/uom/VirtualIOServer/{PRIMARY}"/></PrimaryPagingServicePartition>'
            fields += (
                f'<SecondaryPagingServicePartition><link href="https://hmc.test/rest/api/uom/ManagedSystem/{SYSTEM}/VirtualIOServer/{SECONDARY}"/></SecondaryPagingServicePartition>'
                if request.get("secondary_paging_vios_name")
                else "<SecondaryPagingServicePartition/>"
            )
        if env.fault == "scalar-mismatch":
            fields = fields.replace("<MemoryWeight>0", "<MemoryWeight>1")
        if env.fault == "missing-leaf":
            fields = fields.replace("<MemoryWeight>0</MemoryWeight>", "")
        if env.fault == "absent-secondary":
            fields = fields.replace("<SecondaryPagingServicePartition/>", "")
        if env.fault == "foreign-provider":
            fields = fields.replace("https://hmc.test", "https://foreign.test")
        if env.fault == "whitespace-provider":
            fields = fields.replace("https://hmc.test", " https://hmc.test")
        if env.fault == "direct-link":
            fields = fields.replace(
                f'<PrimaryPagingServicePartition><link href="https://hmc.test/rest/api/uom/VirtualIOServer/{PRIMARY}"/></PrimaryPagingServicePartition>',
                f'<PrimaryPagingServicePartition href="https://hmc.test/rest/api/uom/VirtualIOServer/{PRIMARY}"/>',
            )
        if env.fault == "duplicate-leaf":
            fields += "<MemoryWeight>0</MemoryWeight>"
        if env.fault == "ame-mismatch":
            fields = fields.replace("1.100", "1.2")
        if env.fault == "wrong-provider":
            fields = fields.replace(PRIMARY, SECONDARY)
        xml = f'<LogicalPartitionProfile xmlns="{UOM_NS}"><ProfileMemory>{fields}</ProfileMemory></LogicalPartitionProfile>'
        return SimpleNamespace(uuid=PROFILE, name="default_profile", raw=xml)

    monkeypatch.setattr(arm.client_profiles, "read_native_profile", native)

    def checkpoint():
        j = state.artifacts.creation_memory
        env.checkpoints.append(deepcopy(j))
        config.decode_artifacts(asdict(state.artifacts))
        if env.fault == "checkpoint" or env.fault == "checkpoint-" + j["phase"]:
            raise OSError("checkpoint fault")

    state.checkpoint = checkpoint

    async def call(client, tool, **args):
        env.calls.append((tool, deepcopy(args)))
        if tool == "hmc_run_command":
            names = ["paging-a", "paging-b"] + ([env.name] if env.exists else [])
            if args["cmd"].endswith("-F name"):
                return "PASS", "\n".join(names)
            if args["cmd"].endswith("curr_avail_sys_proc_units"):
                return "PASS", "10.0"
            return (
                "PASS",
                "4096,256"
                if env.fault == "baseline-drift" and env.name and not env.exists
                else "8192,256",
            )
        if tool.startswith("hmc_create_"):
            assert env.checkpoints[-1]["phase"] == "create-pending"
            env.name, env.token = args["name"], args.get("caller_token")
            env.exists = env.fault != "refusal"
            if env.fault in {"refusal", "timeout"}:
                return "FAIL", "unknown or rejected private diagnostic"
            if tool == "hmc_create_lpar":
                return "PASS", {
                    "lpar": {"UUID": FRESH} if env.fault != "no-uuid" else {},
                    "workflow_completed": env.fault != "incomplete",
                    "ownership_stamped": True,
                    "creation_memory_step": {
                        "status": "ok",
                        "result": {"fabricated": "not-an-oracle"},
                    },
                }
            return "PASS", {"UUID": FRESH}
        if tool == "hmc_get_lpar_description":
            token = "foreign" if env.fault == "foreign-token" else env.token
            return "PASS", f"[hmcpctl owner:test created:2026-01-01] [caller {token}]"
        if tool.startswith("hmc_delete_"):
            assert env.checkpoints[-1]["phase"] == "delete-pending"
            if env.fault == "delete-timeout":
                return "FAIL", "unknown"
            env.exists = env.fault == "residual"
            return "PASS", None
        pytest.fail(tool)

    state.call = call
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["shared-lpar", "redundant-lpar", "ame-zero-vios", "ame-positive-vios"]
)
async def test_one_fixed_case_independent_oracle_cleanup_and_no_runtime(
    native_target, case
):
    e = native_target
    e.state.config = replace(
        armed(
            case,
            **(
                {"creation_memory_secondary_vios_name": "paging-b"}
                if case == "redundant-lpar"
                else {}
            ),
        ),
        system_name=SYSTEM,
    )
    await e.arm.exercise_creation_memory(None, e.state)
    journal = e.state.artifacts.creation_memory
    assert journal["phase"] == "complete"
    assert journal["deletion"] == "confirmed-absent"
    creates = [(t, a) for t, a in e.calls if t.startswith("hmc_create_")]
    deletes = [(t, a) for t, a in e.calls if t.startswith("hmc_delete_")]
    assert len(creates) == len(deletes) == 1
    tool, args = creates[0]
    assert args["resources"] == e.arm.RESOURCES
    if tool == "hmc_create_lpar":
        assert args["apply_partition_profile"] is False
        assert args["caller_token"] == journal["token"]
        assert args["creation_memory"]["mem_weight"] == 0
        assert deletes[0][1]["lpar_name_or_uuid"] == FRESH
    else:
        assert "apply_partition_profile" not in args and "caller_token" not in args
        assert type(args["creation_memory"]["mem_expansion"]) in {int, float}
        assert deletes[0][1]["vios_name_or_uuid"] == FRESH
    assert len(e.state.observations) == 1
    assert e.state.observations[0]["observation"]["cleanup"] == "passed"
    assert all(
        c["phase"]
        in {
            "prepared",
            "create-pending",
            "create-returned",
            "delete-pending",
            "deleted",
            "complete",
        }
        for c in e.checkpoints
    )
    assert not any("apply" in t or "activate" in t or "stamp" in t for t, _ in e.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "refusal",
        "timeout",
        "no-uuid",
        "incomplete",
        "active",
        "wrong-name",
        "wrong-kind",
        "foreign-system",
        "foreign-token",
        "scalar-mismatch",
        "missing-leaf",
        "absent-secondary",
        "foreign-provider",
        "whitespace-provider",
        "wrong-provider",
        "duplicate-leaf",
        "ambiguous-profile",
        "mode-drift",
        "checkpoint-create-returned",
        "checkpoint-delete-pending",
    ],
)
async def test_incomplete_or_uncertain_creation_never_deletes_retries_or_promotes(
    native_target, fault
):
    e = native_target
    e.fault = fault
    await e.arm.exercise_creation_memory(None, e.state)
    assert len([t for t, _ in e.calls if t.startswith("hmc_create_")]) == 1
    assert not any(t.startswith("hmc_delete_") for t, _ in e.calls)
    assert not e.state.observations
    assert any(r["status"] == "FAIL" for r in e.state.results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["checkpoint", "unknown-mode", "continuation", "oversized-feed"]
)
async def test_pre_effect_failures_stop(native_target, fault):
    e = native_target
    e.fault = fault
    await e.arm.exercise_creation_memory(None, e.state)
    assert not any(t.startswith(("hmc_create_", "hmc_delete_")) for t, _ in e.calls)
    assert not e.state.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["delete-timeout", "residual", "baseline-drift"])
async def test_cleanup_or_baseline_uncertainty_never_promotes(native_target, fault):
    e = native_target
    e.fault = fault
    await e.arm.exercise_creation_memory(None, e.state)
    assert len([t for t, _ in e.calls if t.startswith("hmc_create_")]) == 1
    assert len([t for t, _ in e.calls if t.startswith("hmc_delete_")]) == 1
    assert e.state.artifacts.creation_memory["phase"] == "stopped-uncertain"
    assert not e.state.observations


@pytest.mark.asyncio
async def test_controller_retains_current_facts_without_designation_or_cleanup(
    native_target,
):
    e = native_target
    e.state.config = replace(armed("controller-shared-lpar"), system_name=SYSTEM)
    await e.arm.exercise_creation_memory(None, e.state)
    j = e.state.artifacts.creation_memory
    assert j["outcome"] == "unavailable" and j["phase"] == "stopped-uncertain"
    assert not any(t.startswith("hmc_delete_") for t, _ in e.calls)
    assert not e.state.observations
    observation = next(
        r["data"]
        for r in e.state.results
        if isinstance(r["data"], dict) and "observed" in r["data"]
    )
    assert observation["observed"]["mem_weight"] == "0"
    assert observation["unavailable"] == [
        "PrimaryPagingServicePartition",
        "SecondaryPagingServicePartition",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["candidate", "connection", "invocation", "grant", "request"]
)
async def test_mutation_checkpoint_binding_refuses_changed_context(
    native_target, fault
):
    e = native_target
    kind, memory = e.arm.configured(e.state.config)
    await e.arm._prepare(None, e.state, e.hmc, kind, memory)
    if fault == "candidate":
        e.state.artifacts.creation_memory["candidate_commit"] = "c" * 40
    elif fault == "connection":
        e.state.artifacts.creation_memory["hmc_identity"]["host"] = "foreign.test"
    elif fault == "invocation":
        e.state.creation_memory_invocation = "d" * 32
    elif fault == "grant":
        e.state.config = replace(
            e.state.config, creation_memory_fresh_delete_authorized=False
        )
    else:
        e.state.config = replace(
            e.state.config, creation_memory_primary_vios_name="changed"
        )
    with pytest.raises(ValueError):
        await e.arm._create(None, e.state, e.hmc, memory)
    assert not any(t.startswith("hmc_create_") for t, _ in e.calls)


@pytest.mark.asyncio
async def test_missing_current_policy_and_repeated_invocation_never_mutate(
    native_target,
):
    e = native_target
    e.state.schemas.pop("hmc_delete_lpar")
    await e.arm.exercise_creation_memory(None, e.state)
    assert not any(t.startswith("hmc_create_") for t, _ in e.calls)
    e.state.schemas["hmc_delete_lpar"] = {}
    await e.arm.exercise_creation_memory(None, e.state)
    before = deepcopy(e.calls)
    await e.arm.exercise_creation_memory(None, e.state)
    assert e.calls == before
    assert e.state.results[-1]["status"] == "SKIP"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["grant", "token", "system", "candidate", "invocation"]
)
async def test_fresh_delete_authority_rechecked_after_verified_oracle(
    native_target, fault
):
    e = native_target
    kind, memory = e.arm.configured(e.state.config)
    await e.arm._prepare(None, e.state, e.hmc, kind, memory)
    assert await e.arm._create(None, e.state, e.hmc, memory)
    j = e.state.artifacts.creation_memory
    j["outcome"] = "created-verified"
    if fault == "grant":
        e.state.config = replace(
            e.state.config, creation_memory_fresh_delete_authorized=False
        )
    elif fault == "token":
        e.fault = "foreign-token"
    elif fault == "system":
        e.state.config = replace(e.state.config, system_name=SECONDARY)
    elif fault == "candidate":
        j["candidate_commit"] = "f" * 40
    else:
        e.state.creation_memory_invocation = None
    with pytest.raises(ValueError):
        await e.arm._delete(None, e.state, e.hmc)
    assert not any(t.startswith("hmc_delete_") for t, _ in e.calls)


@pytest.mark.asyncio
async def test_ame_factor_mismatch_cannot_trust_product_success(native_target):
    e = native_target
    e.state.config = replace(armed("ame-positive-vios"), system_name=SYSTEM)
    e.fault = "ame-mismatch"
    await e.arm.exercise_creation_memory(None, e.state)
    assert e.state.artifacts.creation_memory["outcome"] == "mismatch"
    assert not e.state.observations
    assert not any(t.startswith("hmc_delete_") for t, _ in e.calls)


@pytest.mark.asyncio
async def test_incomplete_created_uuid_is_not_cleanup_authority(native_target):
    e = native_target
    kind, memory = e.arm.configured(e.state.config)
    await e.arm._prepare(None, e.state, e.hmc, kind, memory)
    assert await e.arm._create(None, e.state, e.hmc, memory)
    e.state.artifacts.creation_memory["outcome"] = "unavailable"
    with pytest.raises(ValueError, match="incomplete creation"):
        await e.arm._delete(None, e.state, e.hmc)
    assert not any(t.startswith("hmc_delete_") for t, _ in e.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "<feed/>",
        feed([xml_entry(PRIMARY, "paging-a", "VirtualIOServer")]),
        feed([xml_entry(FRESH, "duplicate", "LogicalPartition")] * 2),
        f'<entry xmlns="{ATOM_NS}"><content/></entry>',
    ],
)
async def test_raw_partition_inventory_requires_complete_unique_typed_atom(
    native_target, raw
):
    e = native_target
    e.hmc._get = AsyncMock(return_value=raw)
    with pytest.raises(ValueError):
        await e.arm._feed(e.hmc, SYSTEM, "LogicalPartition")


@pytest.mark.asyncio
async def test_direct_native_provider_href_is_independently_resolved(native_target):
    e = native_target
    e.fault = "direct-link"
    await e.arm.exercise_creation_memory(None, e.state)
    assert e.state.artifacts.creation_memory["phase"] == "complete"
    assert e.state.observations[0]["observation"]["result"] == "passed"
