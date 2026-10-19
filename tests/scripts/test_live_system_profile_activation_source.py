"""Offline safety contracts for the new-owned-fixture calibration arm."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from live_test import system_profile_activation_source as arm

from hmcpctl.config import HMCConfig

S = "00000001-0000-4000-8000-000000000001"
L = "00000002-0000-4000-8000-000000000002"
P = "00000003-0000-4000-8000-000000000003"


@pytest.fixture
def packet(monkeypatch):
    binding = {
        "profile": "selected",
        "host": "hmc.test",
        "port": 443,
        "system_uuid": S,
        "selected_inventory": {
            "UUID": S,
            "Resource": {
                "SystemName": "isolated",
                "MachineTypeModelAndSerialNumber": {
                    "MachineType": "9009",
                    "Model": "22A",
                    "SerialNumber": "TEST000",
                },
            },
        },
    }
    raw = json.dumps(binding)
    monkeypatch.setattr(arm, "BINDING_SHA256", hashlib.sha256(raw.encode()).hexdigest())
    return {
        "binding_json": raw,
        "grant": "temporary-fixtures-20261019",
        "owner": "test-owner",
        "caller_token": "q1725-d647765d",
        "negative_cases": False,
        "tested_commit": "a" * 40,
        "tested_tree": "b" * 40,
        "baseline_sha256": None,
    }


def test_exact_binding_and_closed_packet(packet):
    assert arm.binding(packet)["system_uuid"] == S
    packet["binding_json"] += " "
    with pytest.raises(ValueError, match="binding"):
        arm.binding(packet)


@pytest.mark.parametrize(
    "field,value",
    [
        ("grant", "other"),
        ("negative_cases", 1),
        ("caller_token", "other"),
        ("owner", ""),
        ("tested_commit", "main"),
        ("extra", True),
    ],
)
def test_packet_rejects_unapproved_surface(packet, field, value):
    packet[field] = value
    with pytest.raises(ValueError):
        arm.binding(packet)


def test_context_is_checked_before_client_and_port_explicit(packet, monkeypatch):
    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "operator",
            "agent_id": "test-owner",
            "authorize_power_operations": True,
        }
    )
    monkeypatch.setattr(arm, "load_profile", lambda _: config)
    monkeypatch.setattr(
        arm,
        "load_config_document",
        lambda: SimpleNamespace(
            data={"profiles": {"selected": {"host": "hmc.test", "user": "operator"}}}
        ),
    )
    monkeypatch.delenv("HMC_HOST", raising=False)
    actual = arm.configured(packet)
    assert actual.port == 443 and "port" in actual.model_fields_set
    monkeypatch.setenv("HMC_HOST", "foreign.test")
    with pytest.raises(ValueError, match="ambient"):
        arm.configured(packet)


def test_ambient_port_mismatch_is_not_overwritten(packet, monkeypatch):
    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "operator",
            "port": 12443,
            "agent_id": "test-owner",
        }
    )
    monkeypatch.setattr(arm, "load_profile", lambda _: config)
    monkeypatch.setattr(
        arm,
        "load_config_document",
        lambda: SimpleNamespace(
            data={"profiles": {"selected": {"host": "hmc.test", "user": "operator"}}}
        ),
    )
    monkeypatch.delenv("HMC_HOST", raising=False)
    with pytest.raises(ValueError, match="context"):
        arm.configured(packet)


def test_fixture_configuration_has_hard_caps_and_no_attachments():
    r = arm.resources()
    assert (r.min_memory, r.desired_memory, r.max_memory) == (512, 512, 1024)
    assert (r.min_vcpus, r.desired_vcpus, r.max_vcpus) == (1, 1, 1)
    assert (r.min_procs, r.desired_procs, r.max_procs) == (0.05, 0.05, 0.05)
    assert r.dedicated is False and r.uncapped is False
    command = arm.profile_command("bound-system", 0)
    assert "mem_mode=ded" in command and "sharing_mode=cap" in command
    assert "all_resources=1" not in command
    assert all(
        term not in command
        for term in ["io_slots", "virtual_eth_adapters", "virtual_scsi_adapters"]
    )


def test_wrapper_calls_narrow_main(monkeypatch):
    path = (
        Path(__file__).parents[2] / "scripts/live_system_profile_activation_source.py"
    )
    spec = importlib.util.spec_from_file_location("activation_source_wrapper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main is arm.main


def profile(name, uuid, parent, desired=512):
    return {
        "UUID": uuid,
        "Resource": {
            "ProfileName": name,
            "AssociatedPartition": {"href": f"/rest/api/uom/LogicalPartition/{parent}"},
            "AssignAllResources": "false",
            "AutoStart": "false",
            "ProfileMemory": {
                "ActiveMemorySharingEnabled": "false",
                "ActiveMemoryExpansionEnabled": "false",
                "MinimumMemory": "512",
                "DesiredMemory": str(desired),
                "MaximumMemory": "1024",
            },
            "ProcessorAttributes": {
                "HasDedicatedProcessors": "false",
                "SharingMode": "capped",
                "SharedProcessorConfiguration": {
                    "MinimumProcessingUnits": "0.05",
                    "DesiredProcessingUnits": "0.05",
                    "MaximumProcessingUnits": "0.05",
                    "MinimumVirtualProcessors": "1",
                    "DesiredVirtualProcessors": "1",
                    "MaximumVirtualProcessors": "1",
                },
            },
            "IOConfigurationInstance": {
                "MaximumVirtualIOSlots": "2",
                "ProfileVirtualIOAdapters": {},
            },
        },
    }


@pytest.fixture
def hardware(packet, monkeypatch):
    import copy
    import shlex
    from xml.etree import ElementTree as ET

    state = {
        "members": {
            L: {
                "UUID": L,
                "Resource": {
                    "PartitionName": "preexisting",
                    "PartitionID": "1",
                    "PartitionType": "AIX/Linux",
                    "PartitionState": "not activated",
                    "Description": "unchanged",
                },
                "profiles": [profile("existing", P, L)],
            }
        },
        "sysprofiles": {},
        "jobs": {},
        "deleted_jobs": [],
        "commands": [],
        "next": 10,
    }
    h = AsyncMock()
    h.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "operator", "agent_id": "test-owner"}
    )
    system = json.loads(packet["binding_json"])["selected_inventory"]
    system["Resource"].update(
        State="operating",
        AssociatedSystemMemoryConfiguration={
            "CurrentAvailableSystemMemory": "4096",
            "CurrentLogicalMemoryBlockSize": "16",
        },
        AssociatedSystemProcessorConfiguration={
            "CurrentAvailableSystemProcessorUnits": "1",
            "MinimumProcessorUnitsPerVirtualProcessor": "0.05",
        },
    )
    h.get_uom.return_value = system

    async def partitions(_):
        return copy.deepcopy(list(state["members"].values()))

    async def profiles(_, uuid, __):
        return copy.deepcopy(state["members"][uuid]["profiles"])

    h.list_logical_partitions.side_effect = partitions
    h.list_child.side_effect = profiles

    def uid():
        state["next"] += 1
        return f"{state['next']:08x}-0000-4000-8000-{state['next']:012x}"

    async def create(config, system, name, **kwargs):
        assert (
            kwargs["resources"] == arm.resources() and kwargs["max_virtual_slots"] == 2
        )
        u = uid()
        p = profile(kwargs["profile_name"], uid(), u)
        state["members"][u] = {
            "UUID": u,
            "Resource": {
                "PartitionName": name,
                "PartitionID": str(state["next"]),
                "PartitionType": "AIX/Linux",
                "PartitionState": "not activated",
                "Description": "",
                "LastActivatedProfile": "",
                "PartitionMemoryConfiguration": {
                    "CurrentMemory": "512",
                    "RuntimeMemory": "0",
                },
            },
            "profiles": [p],
        }
        return ""

    async def stamp(config, system, name, **kwargs):
        e = next(
            e
            for e in state["members"].values()
            if e["Resource"]["PartitionName"] == name
        )
        desc = f"[hmcpctl owner:{kwargs['agent_id']} created:2026-10-19] [caller {kwargs['caller_token']}]"
        e["Resource"]["Description"] = desc
        return desc

    async def ssh(config, command):
        state["commands"].append(command)
        args = shlex.split(command)
        if args[:3] == ["lssyscfg", "-r", "sysprof"]:
            return (
                "name\n" + "".join(n + "\n" for n in sorted(state["sysprofiles"]))
                if state["sysprofiles"]
                else "No results were found."
            )
        if args[:3] == ["mksyscfg", "-r", "prof"]:
            v = dict(
                item.split("=", 1) for item in args[args.index("-i") + 1].split(",")
            )
            e = next(
                e
                for e in state["members"].values()
                if e["Resource"]["PartitionName"] == v["lpar_name"]
            )
            e["profiles"].append(
                profile(v["name"], uid(), e["UUID"], int(v["desired_mem"]))
            )
            return ""
        if args[:3] == ["rmsyscfg", "-r", "sysprof"]:
            del state["sysprofiles"][args[-1]]
            return ""
        assert args[:3] == ["lssyscfg", "-r", "lpar"]
        identifier = args[args.index("--filter") + 1].split("=", 1)[1]
        e = next(
            e
            for e in state["members"].values()
            if e["Resource"]["PartitionID"] == identifier
        )
        return f"name:lpar_id:curr_profile\n{e['Resource']['PartitionName']}:{identifier}:{e['Resource'].get('LastActivatedProfile', '')}\n"

    async def submit(path, xml):
        root = ET.fromstring(xml)

        def local(tag):
            return tag.rsplit("}", 1)[-1]

        op = next(e.text for e in root.iter() if local(e.tag) == "OperationName")
        params = {}
        for e in root.iter():
            if local(e.tag) == "JobParameter":
                vals = {local(c.tag): c.text for c in e}
                params[vals["ParameterName"]] = vals["ParameterValue"]
        identity = f"own-{len(state['jobs'])}"
        state["jobs"][identity] = {
            "operation": op,
            "parameters": params,
            "target": path.split("/")[-3],
        }
        return {"Resource": {"JobID": identity}}

    def apply(e, name):
        p = next(p for p in e["profiles"] if p["Resource"]["ProfileName"] == name)
        e["Resource"]["PartitionProcessorConfiguration"] = {
            "CurrentHasDedicatedProcessors": "false",
            "CurrentSharingMode": "capped",
            "CurrentSharedProcessorConfiguration": {
                "AllocatedVirtualProcessors": "1",
                "CurrentProcessingUnits": "0.05",
            },
        }
        e["Resource"].update(
            PartitionState="running",
            LastActivatedProfile=name,
            AssociatedPartitionProfile={
                "href": f"/rest/api/uom/LogicalPartition/{e['UUID']}/LogicalPartitionProfile/{p['UUID']}"
            },
            PartitionMemoryConfiguration={
                "CurrentMemory": p["Resource"]["ProfileMemory"]["DesiredMemory"],
                "RuntimeMemory": p["Resource"]["ProfileMemory"]["DesiredMemory"],
                "SharedMemoryEnabled": "false",
            },
        )

    async def wait(identity, **kwargs):
        job = state["jobs"][identity]
        op, p = job["operation"], job["parameters"]
        result = ""
        status = "COMPLETED_OK"
        code = "0"
        if op == "CreateSystemProfile":
            state["sysprofiles"][p["systemProfileName"]] = list(
                zip(
                    p["logicalPartitionNames"].split(","),
                    p["logicalPartitionProfileNames"].split(","),
                    strict=True,
                )
            )
        elif op == "GetSystemProfiles":
            rows = []
            for name, members in state["sysprofiles"].items():
                ids = [
                    next(
                        e["Resource"]["PartitionID"]
                        for e in state["members"].values()
                        if e["Resource"]["PartitionName"] == n
                    )
                    for n, _ in members
                ]
                rows.append(
                    {
                        "SystemProfileName": name,
                        "PartitionNames": ",".join(n for n, _ in members),
                        "PartitionIds": ",".join(ids),
                        "PartitionProfileNames": ",".join(n for _, n in members),
                    }
                )
            result = json.dumps(rows)
        elif op == "ActivateSystemProfile" and p.get("operation") == "on":
            for name, prof in state["sysprofiles"][p["systemProfile"]]:
                e = next(
                    e
                    for e in state["members"].values()
                    if e["Resource"]["PartitionName"] == name
                )
                # Constructed native failure model only; actual already-running behavior is UNKNOWN.
                if e["Resource"]["PartitionState"] == "running":
                    status, code = "FAILED_BEFORE_COMPLETION", "1"
                    if p["continue"] == "false":
                        break
                else:
                    apply(e, prof)
        elif op == "PowerOff":
            state["members"][job["target"]]["Resource"]["PartitionState"] = (
                "not activated"
            )
            state["members"][job["target"]]["Resource"]["PartitionMemoryConfiguration"][
                "RuntimeMemory"
            ] = "0"
        return {
            "Resource": {
                "JobID": identity,
                "Status": status,
                "Results": {
                    "JobParameter": [
                        {"ParameterName": "returnCode", "ParameterValue": code},
                        {"ParameterName": "result", "ParameterValue": result},
                    ]
                },
            }
        }

    async def delete_job(identity, **kwargs):
        state["deleted_jobs"].append(identity)

    async def delete_lpar(uuid):
        del state["members"][uuid]

    h.submit_job.side_effect = submit
    h.wait_for_job_entry.side_effect = wait
    h.delete_job.side_effect = delete_job
    h.delete_logical_partition.side_effect = delete_lpar
    monkeypatch.setattr(arm, "create_lpar_via_cli", AsyncMock(side_effect=create))
    monkeypatch.setattr(arm, "stamp_lpar_ownership", AsyncMock(side_effect=stamp))
    monkeypatch.setattr(arm, "run_hmc_command", AsyncMock(side_effect=ssh))
    monkeypatch.setattr(arm.source, "run_hmc_command", AsyncMock(side_effect=ssh))
    return h, state


@pytest.mark.asyncio
@pytest.mark.parametrize("negative", [False, True])
async def test_complete_fixture_calibration_and_independent_recovery(
    packet, hardware, tmp_path, negative
):
    h, state = hardware
    packet["negative_cases"] = negative
    packet["baseline_sha256"] = arm._baseline_digest(await arm.preflight(h, packet))
    directory = tmp_path / "capture"
    directory.mkdir()
    doc = await arm.capture(h, packet, directory)
    assert doc["calibration"]["consistent"] is True
    assert doc["calibration"]["application_semantics"].startswith("UNPROVEN")
    assert len(state["members"]) == 1 and not state["sysprofiles"]
    assert doc["run"]["partial"] is False
    await arm.recover(h, packet, doc)
    if negative:
        assert [c["continue"] for c in doc["negative_cases"]] == [False, True]
        assert all(
            c["partial_activation"].startswith("UNPROVEN")
            for c in doc["negative_cases"]
        )
        failed = [
            j
            for j in doc["jobs"]
            if j["terminal"]["Resource"]["Status"] == "FAILED_BEFORE_COMPLETION"
        ]
        assert len(failed) == 2 and all(
            j["id"] not in state["deleted_jobs"] for j in failed
        )


@pytest.mark.asyncio
async def test_unconfirmed_stamp_stops_without_more_effects(
    packet, hardware, tmp_path, monkeypatch
):
    h, state = hardware
    packet["baseline_sha256"] = arm._baseline_digest(await arm.preflight(h, packet))
    monkeypatch.setattr(arm, "stamp_lpar_ownership", AsyncMock(return_value=None))
    with pytest.raises(PermissionError, match="unconfirmed"):
        await capture_in(h, packet, tmp_path)
    assert arm.create_lpar_via_cli.await_count == 1
    h.submit_job.assert_not_awaited()
    h.delete_logical_partition.assert_not_awaited()
    assert not any(c.startswith("mksyscfg -r prof") for c in state["commands"])


@pytest.mark.asyncio
async def test_collision_and_unreadable_inventory_never_create(
    packet, hardware, tmp_path
):
    h, state = hardware
    state["sysprofiles"][arm.SYSTEM_PROFILES[0]] = []
    with pytest.raises(ValueError, match="collision"):
        await arm.preflight(h, packet)
    arm.create_lpar_via_cli.assert_not_awaited()
    state["sysprofiles"].clear()
    h.list_logical_partitions.side_effect = OSError("unreadable")
    with pytest.raises(OSError):
        await arm.preflight(h, packet)
    arm.create_lpar_via_cli.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    [
        "missing-job",
        "missing-effect",
        "wrong-cleanup-target",
        "forged-partial",
        "baseline",
        "terminal-id",
    ],
)
async def test_corrupted_journal_cannot_claim_clean(packet, hardware, tmp_path, tamper):
    h, _state = hardware
    packet["baseline_sha256"] = arm._baseline_digest(await arm.preflight(h, packet))
    doc = await capture_in(h, packet, tmp_path)
    if tamper == "missing-job":
        doc["jobs"].pop(0)
    elif tamper == "missing-effect":
        doc["effects"].pop(-1)
    elif tamper == "wrong-cleanup-target":
        doc["effects"][-1]["target"] = L
    elif tamper == "forged-partial":
        doc["run"]["partial"] = True
    elif tamper == "baseline":
        doc["before"]["partitions"] = []
    else:
        doc["jobs"][0]["terminal"]["Resource"]["JobID"] = "foreign"
    previous = h.get_uom.await_count
    with pytest.raises(ValueError):
        await arm.recover(h, packet, doc)
    assert (
        h.get_uom.await_count > previous
    )  # Independent inventory happens even on partial journals.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["code0-failure", "wrong-id", "wrong-self", "duplicate-handle", "timeout"]
)
async def test_job_ambiguity_never_cleans_unrelated_job(
    packet, hardware, tmp_path, bad
):
    h, _state = hardware
    doc = {
        "jobs": [],
        "effects": [],
        "pending": {"stage": "CreateSystemProfile", "target": S},
    }
    submission = {"Resource": {"JobID": "source-own"}}
    terminal = {
        "Resource": {
            "JobID": "source-own",
            "Status": "COMPLETED_OK",
            "Results": {
                "JobParameter": {"ParameterName": "returnCode", "ParameterValue": "0"}
            },
        }
    }
    if bad == "code0-failure":
        terminal["Resource"]["Status"] = "FAILED_TO_START"
    elif bad == "wrong-id":
        terminal["Resource"]["JobID"] = "foreign"
    elif bad == "wrong-self":
        submission["link"] = "/rest/api/uom/jobs/foreign"
    elif bad == "duplicate-handle":
        doc["jobs"] = [{"id": "source-own", "href": "/rest/api/uom/jobs/source-own"}]
    else:
        h.wait_for_job_entry.side_effect = TimeoutError("bounded")
    if bad != "timeout":
        h.wait_for_job_entry.side_effect = None
        h.wait_for_job_entry.return_value = terminal
    with pytest.raises((ValueError, TimeoutError)):
        await arm._finish_job(
            h, packet, doc, tmp_path, "CreateSystemProfile", submission
        )
    h.delete_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "CANCELED_BEFORE_START",
        "CANCELED_WHILE_RUNNING",
        "COMPLETED_WITH_WARNINGS",
        "FAILED_BEFORE_COMPLETION_RETRY",
    ],
)
async def test_uncertain_negative_terminal_stops_later_effects(
    packet, hardware, tmp_path, status
):
    h, _state = hardware
    h.wait_for_job_entry.side_effect = None
    h.wait_for_job_entry.return_value = {"Resource": {"JobID": "own", "Status": status}}
    doc = {"jobs": [], "effects": [], "pending": {"stage": "negative-false"}}
    with pytest.raises(ValueError, match="uncertain"):
        await arm._finish_job(
            h,
            packet,
            doc,
            tmp_path,
            "ActivateSystemProfile",
            {"Resource": {"JobID": "own"}},
            negative=True,
        )
    h.delete_job.assert_not_awaited()
    assert doc["pending"] == {"stage": "negative-false"}


@pytest.mark.asyncio
async def test_unchanged_mixed_and_stale_profile_never_application_proof(
    packet, hardware, tmp_path
):
    from copy import deepcopy

    h, _state = hardware
    packet["baseline_sha256"] = arm._baseline_digest(await arm.preflight(h, packet))
    document = await capture_in(h, packet, tmp_path)
    unchanged = deepcopy(document["calibration"]["after"])
    unchanged[1]["partition"]["Resource"]["PartitionState"] = "not activated"
    assert arm.calibrated(unchanged, deepcopy(unchanged)) is False


@pytest.mark.asyncio
async def test_capacity_and_existing_drift_stop_before_fixture_creation(
    packet, hardware, tmp_path
):
    h, state = hardware
    packet["baseline_sha256"] = arm._baseline_digest(await arm.preflight(h, packet))
    state["members"][L]["Resource"]["Description"] = "changed"
    with pytest.raises(ValueError, match="baseline"):
        await capture_in(h, packet, tmp_path)
    arm.create_lpar_via_cli.assert_not_awaited()
    packet["baseline_sha256"] = None
    h.get_uom.return_value["Resource"]["AssociatedSystemMemoryConfiguration"][
        "CurrentAvailableSystemMemory"
    ] = "1024"
    with pytest.raises(ValueError, match="capacity"):
        await arm.preflight(h, packet)


@pytest.mark.asyncio
async def test_initial_creation_ambiguity_keeps_intent_and_never_retries(
    packet, hardware, tmp_path, monkeypatch
):
    h, _state = hardware
    packet["baseline_sha256"] = arm._baseline_digest(await arm.preflight(h, packet))
    monkeypatch.setattr(
        arm,
        "create_lpar_via_cli",
        AsyncMock(side_effect=TimeoutError("unknown create")),
    )
    with pytest.raises(TimeoutError):
        await capture_in(h, packet, tmp_path)
    arm.create_lpar_via_cli.assert_awaited_once()
    arm.stamp_lpar_ownership.assert_not_awaited()
    doc = json.loads((tmp_path / "capture" / "results.json").read_text())
    assert doc["pending"]["stage"] == "create-fixture" and doc["run"]["partial"] is True
    h.delete_logical_partition.assert_not_awaited()


async def capture_in(hmc, packet, tmp_path):
    directory = tmp_path / "capture"
    directory.mkdir()
    return await arm.capture(hmc, packet, directory)


@pytest.mark.parametrize(
    "breach",
    ["parent", "runtime-memory", "runtime-cpu", "runtime-vcpu", "shared-memory", "io"],
)
def test_owned_resource_limits_and_containment_are_mandatory(packet, breach):
    from copy import deepcopy

    uuid = "00000004-0000-4000-8000-000000000004"
    desc = "[hmcpctl owner:test-owner created:2026-10-19] [caller q1725-d647765d]"
    e = {
        "UUID": uuid,
        "Resource": {
            "PartitionName": arm.NAMES[0],
            "PartitionID": "2",
            "PartitionType": "AIX/Linux",
            "Description": desc,
            "PartitionState": "running",
            "PartitionMemoryConfiguration": {
                "CurrentMemory": "512",
                "RuntimeMemory": "512",
                "SharedMemoryEnabled": "false",
            },
            "PartitionProcessorConfiguration": {
                "CurrentHasDedicatedProcessors": "false",
                "CurrentSharingMode": "capped",
                "CurrentSharedProcessorConfiguration": {
                    "AllocatedVirtualProcessors": "1",
                    "CurrentProcessingUnits": "0.05",
                },
            },
        },
        "profiles": [profile(arm.BASE[0], P, uuid)],
    }
    if breach == "parent":
        e["profiles"][0]["Resource"]["AssociatedPartition"]["href"] = (
            f"/rest/api/uom/LogicalPartition/{L}"
        )
    elif breach == "runtime-memory":
        e["Resource"]["PartitionMemoryConfiguration"]["CurrentMemory"] = "2048"
    elif breach == "runtime-cpu":
        e["Resource"]["PartitionProcessorConfiguration"][
            "CurrentSharedProcessorConfiguration"
        ]["CurrentProcessingUnits"] = "1"
    elif breach == "runtime-vcpu":
        e["Resource"]["PartitionProcessorConfiguration"][
            "CurrentSharedProcessorConfiguration"
        ]["AllocatedVirtualProcessors"] = "2"
    elif breach == "shared-memory":
        e["Resource"]["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = "true"
    else:
        e["profiles"][0]["Resource"]["IOConfigurationInstance"][
            "ProfileVirtualIOAdapters"
        ] = {"ClientNetworkAdapter": {"VirtualSlotNumber": "2"}}
    record = {
        "uuid": uuid,
        "name": arm.NAMES[0],
        "partition_id": "2",
        "description": desc,
        "profiles": deepcopy(e["profiles"]),
    }
    h = SimpleNamespace(
        config=HMCConfig.from_mapping({"host": "hmc.test", "agent_id": "test-owner"})
    )
    with pytest.raises(ValueError):
        arm._owned(h, packet, e, record)


def test_canonical_preflight_and_recovery_routes(monkeypatch, tmp_path):
    import live_test_preflight
    import live_test_recovery

    calls = []
    monkeypatch.setattr(arm, "main", lambda argv: calls.append(argv) or 17)
    assert (
        live_test_preflight.main(
            [
                "--group",
                arm.GROUP,
                "--packet",
                "packet",
                "--packet-sha256",
                "digest",
                "--output",
                "preflight",
            ]
        )
        == 17
    )
    assert calls[-1] == [
        "preflight",
        "--packet",
        "packet",
        "--packet-sha256",
        "digest",
        "--output",
        "preflight",
    ]
    assert (
        live_test_recovery.main(
            [
                "--activation-source-packet",
                "packet",
                "--packet-sha256",
                "digest",
                "--results",
                "journal",
                "--output",
                "recovery",
            ]
        )
        == 17
    )
    assert calls[-1] == [
        "recover",
        "--packet",
        "packet",
        "--packet-sha256",
        "digest",
        "--results",
        "journal",
        "--output",
        "recovery",
    ]
    with pytest.raises(SystemExit):
        live_test_preflight.main(["--group", arm.GROUP, "--skip-hardware"])
    with pytest.raises(SystemExit):
        live_test_recovery.main(
            ["--source-packet", "old", "--activation-source-packet", "new"]
        )
    from live_test import runner

    assert (
        arm.GROUP not in runner.SUBTASK_GROUPS
    )  # Never admitted by all/default/round2.


def test_cli_rejects_context_before_auth_and_sanitizes_error(
    packet, tmp_path, monkeypatch, capsys
):
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet))
    packet_path.chmod(0o600)
    digest = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        arm.source, "_revision", lambda: (packet["tested_commit"], True)
    )
    monkeypatch.setattr(
        arm.subprocess, "check_output", lambda *a, **k: packet["tested_tree"]
    )
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.setattr(
        arm,
        "load_profile",
        lambda _: HMCConfig.from_mapping(
            {
                "host": "private-canary.test",
                "user": "operator",
                "agent_id": "test-owner",
            }
        ),
    )
    monkeypatch.setattr(
        arm,
        "load_config_document",
        lambda: SimpleNamespace(
            data={"profiles": {"selected": {"host": "hmc.test", "user": "operator"}}}
        ),
    )
    constructor = AsyncMock()
    monkeypatch.setattr(arm, "HMCClient", constructor)
    output = tmp_path / "output"
    assert (
        arm.main(
            [
                "preflight",
                "--packet",
                str(packet_path),
                "--packet-sha256",
                digest,
                "--output",
                str(output),
            ]
        )
        == 2
    )
    constructor.assert_not_called()
    text = capsys.readouterr()
    assert "private-canary" not in text.err + text.out and "Traceback" not in text.err
    assert "CALIBRATION STOPPED" in text.err
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "results.json").stat().st_mode & 0o777 == 0o600


def test_public_evidence_filters_private_calibration_fields():
    import live_test_evidence

    document = {
        "run": {
            "tested_commit": "a" * 40,
            "tree_clean": True,
            "group": arm.GROUP,
            "partial": True,
        },
        "results": [
            {
                "subtask": "source-calibration",
                "tool": "live_system_profile_activation_source",
                "status": "FAIL",
                "data": "private-host.test",
                "note": "private-uuid",
            }
        ],
        "calibration": {"before": "private-before"},
        "before": "private-baseline",
        "fixtures": "private-members",
        "jobs": "private-terminal",
    }
    rendered = live_test_evidence.render(document)
    assert "private-" not in rendered
    assert "PARTIAL" in rendered and "FAIL 1" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [443, 12443])
async def test_real_logon_failure_never_falls_back_to_another_port(
    packet, monkeypatch, tmp_path, port
):
    import os
    import tomllib
    from functools import partial

    import httpx
    import respx

    from hmcpctl.client.core import HMCClient
    from hmcpctl.config import load_profile
    from hmcpctl.errors import HMCTransportError

    for key in list(os.environ):
        if key.upper().startswith("HMC_"):
            monkeypatch.delenv(key)
    raw_binding = json.loads(packet["binding_json"])
    raw_binding["port"] = port
    packet["binding_json"] = json.dumps(raw_binding)
    monkeypatch.setattr(
        arm,
        "BINDING_SHA256",
        hashlib.sha256(packet["binding_json"].encode()).hexdigest(),
    )
    configuration = tmp_path / "config.toml"
    configuration.write_text(
        '[profiles.selected]\nhost="hmc.test"\nuser="operator"\npassword="p"\nagent_id="test-owner"\nverify_ssl=true\n'
        + (f"port={port}\n" if port != 443 else "")
    )
    monkeypatch.setattr(
        arm, "load_profile", partial(load_profile, config_path=configuration)
    )
    monkeypatch.setattr(
        arm,
        "load_config_document",
        lambda: SimpleNamespace(data=tomllib.loads(configuration.read_text())),
    )
    config = arm.configured(packet)
    selected_url = (
        "https://hmc.test/rest/api/web/Logon"
        if port == 443
        else "https://hmc.test:12443/rest/api/web/Logon"
    )
    other_url = (
        "https://hmc.test:12443/rest/api/web/Logon"
        if port == 443
        else "https://hmc.test/rest/api/web/Logon"
    )
    with respx.mock(assert_all_called=False) as router:
        selected = router.put(selected_url).mock(
            side_effect=httpx.ConnectError("transport failed")
        )
        other = router.put(other_url).mock(
            side_effect=httpx.ConnectError("forbidden alternate port")
        )
        with pytest.raises(HMCTransportError):
            async with HMCClient(config):
                pytest.fail("no session should open")
    assert selected.call_count == 1 and other.call_count == 0


@pytest.mark.asyncio
async def test_unapproved_job_stage_has_no_effect(packet, hardware, tmp_path):
    h, _state = hardware
    with pytest.raises(ValueError, match="seven fixed"):
        await arm._job(h, packet, {}, tmp_path, "arbitrary-operation")
    h.get_uom.assert_not_awaited()
    h.submit_job.assert_not_awaited()


def test_recovery_observes_even_when_journal_is_unreadable(
    packet, hardware, tmp_path, monkeypatch
):
    h, _state = hardware
    h.__aenter__.return_value = h
    monkeypatch.setattr(arm, "HMCClient", lambda _: h)
    monkeypatch.setattr(arm, "load_profile", lambda _: h.config)
    monkeypatch.setattr(
        arm,
        "load_config_document",
        lambda: SimpleNamespace(
            data={"profiles": {"selected": {"host": "hmc.test", "user": "operator"}}}
        ),
    )
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.setattr(
        arm.source, "_revision", lambda: (packet["tested_commit"], True)
    )
    monkeypatch.setattr(
        arm.subprocess, "check_output", lambda *a, **k: packet["tested_tree"]
    )
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet))
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    output = tmp_path / "recovery"
    assert (
        arm.main(
            [
                "recover",
                "--packet",
                str(path),
                "--packet-sha256",
                digest,
                "--results",
                str(tmp_path / "missing.json"),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert h.get_uom.await_count == 1
    assert json.loads((output / "results.json").read_text())["read_only"] is True
    h.submit_job.assert_not_awaited()
    h.delete_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing", ["CurrentMemory", "RuntimeMemory", "contradictory-profile"]
)
async def test_incomplete_calibration_stops_before_any_later_effect(
    packet, hardware, tmp_path, missing
):
    h, state = hardware
    packet["baseline_sha256"] = arm._baseline_digest(await arm.preflight(h, packet))
    original_wait = h.wait_for_job_entry.side_effect

    async def wait(identity, **kwargs):
        result = await original_wait(identity, **kwargs)
        job = state["jobs"][identity]
        if (
            job["operation"] == "ActivateSystemProfile"
            and job["parameters"].get("operation") == "on"
        ):
            for entry in state["members"].values():
                if entry["Resource"]["PartitionName"] in arm.NAMES:
                    if missing == "contradictory-profile":
                        entry["Resource"]["LastActivatedProfile"] = "contradiction"
                    else:
                        entry["Resource"]["PartitionMemoryConfiguration"].pop(missing)
        return result

    h.wait_for_job_entry.side_effect = wait
    with pytest.raises(ValueError):
        await capture_in(h, packet, tmp_path)
    assert len(state["members"]) == 3 and len(state["sysprofiles"]) == 1
    assert not any(j["operation"] == "PowerOff" for j in state["jobs"].values())
    h.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper", ["create-failed", "validate-failed", "stop-failed", "deleted-negative"]
)
async def test_required_job_failure_or_deleted_negative_never_clean(
    packet, hardware, tmp_path, tamper
):
    h, _state = hardware
    packet["negative_cases"] = True
    packet["baseline_sha256"] = arm._baseline_digest(await arm.preflight(h, packet))
    doc = await capture_in(h, packet, tmp_path)
    if tamper == "deleted-negative":
        record = next(
            j
            for j in doc["jobs"]
            if j["terminal"]["Resource"]["Status"] == "FAILED_BEFORE_COMPLETION"
        )
        record["own_job_deleted"] = True
    else:
        if tamper == "create-failed":
            record = doc["jobs"][0]
        elif tamper == "stop-failed":
            record = next(j for j in doc["jobs"] if j["operation"] == "PowerOff")
        else:
            record = next(
                j for j in doc["jobs"] if j["operation"] == "ActivateSystemProfile"
            )
        record["terminal"]["Resource"]["Status"] = "FAILED_TO_START"
    with pytest.raises(ValueError):
        await arm.recover(h, packet, doc)


def test_inactive_fixture_can_omit_runtime_memory_before_activation():
    arm._runtime_limits(
        {"PartitionState": "not activated", "PartitionMemoryConfiguration": {}}
    )


@pytest.mark.asyncio
async def test_false_calibration_cannot_forge_completed_clean(
    packet, hardware, tmp_path
):
    h, _state = hardware
    packet["negative_cases"] = False
    packet["baseline_sha256"] = arm._baseline_digest(await arm.preflight(h, packet))
    doc = await capture_in(h, packet, tmp_path)
    doc["calibration"]["after"][0]["partition"]["Resource"]["LastActivatedProfile"] = (
        "contradiction"
    )
    doc["calibration"]["consistent"] = False
    doc["results"] = arm._result_rows(doc)
    with pytest.raises(ValueError):
        await arm.recover(h, packet, doc)
