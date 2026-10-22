"""Finite saved-profile lifecycle and independent read-only recovery confirmation."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from hmcpctl.client.client_profiles import NativeProfile, require_fresh_profile
from hmcpctl.client.core import HMCClient
from hmcpctl.documents.partition_profile import (
    ProfilePatch,
    apply_profile_patch,
    compare_profiles,
    parse_profile,
)
from hmcpctl.operations.lpar import profiles as domain

from .config import decode_saved_config
from .observation import Assertion, plain_data

SUBTASK = 45
GROUP = "partition-profiles"
PREFIX = "hmcpctl-live-partprof-"
SCENARIO = "st45-partition-profile-lifecycle"
STAGES = (
    "create",
    "patch",
    "overwrite",
    "default-scratch",
    "default-original",
    "delete-scratch",
    "delete-recovery",
)


def configured(config):
    if (
        not isinstance(config.partition_profile_target, str)
        or not config.partition_profile_target.strip()
    ):
        raise ValueError("partition-profiles requires an explicit disposable partition")
    if config.partition_profile_kind not in {"LogicalPartition", "VirtualIOServer"}:
        raise ValueError("partition-profiles requires a recognized partition kind")
    if config.partition_profile_writer_window is not True:
        raise ValueError("partition-profiles requires coordinated exclusive writers")
    if (
        type(config.partition_profile_memory_mib) is not int
        or config.partition_profile_memory_mib <= 0
    ):
        raise ValueError("partition-profiles requires positive patch memory")


async def snapshot(hmc_config, config):
    configured(config)
    pinned = hmc_config.model_copy(update={"port": hmc_config.port})
    async with asyncio.timeout(300), HMCClient(pinned) as hmc:
        target = await domain._target(
            hmc,
            config.system_name,
            config.partition_profile_target,
            config.partition_profile_kind,
            True,
        )
        if (
            target.resource.get("PartitionState") != "Not Activated"
            or target.name in config.protected_lpar_names
        ):
            raise ValueError(
                "partition-profiles requires an inactive unprotected target"
            )
        if domain._text(target.resource.get("CurrentProfileSync")) not in {
            "Disabled",
            "Suspended",
        }:
            raise ValueError(
                "partition-profiles requires disabled profile synchronization"
            )
        await domain._empty_system_profiles(hmc, target)
        profiles = await domain._inventory(hmc, target)
        for name in ("DefaultProfileName", "LastActivatedProfile"):
            domain._choose(profiles, domain._text(target.resource.get(name)))
        if any(not p.etag for p in profiles):
            raise ValueError(
                "partition-profiles requires complete original profile ETags"
            )
        default = domain._choose(
            profiles, domain._text(target.resource.get("DefaultProfileName"))
        )
        apply_profile_patch(
            default.element,
            ProfilePatch(desired_memory=config.partition_profile_memory_mib),
        )
        result = {
            "system": target.system,
            "partition": target.uuid,
            "kind": target.kind,
            "resource": domain._partition_snapshot(target.resource),
            "profiles": [
                {
                    "uuid": p.uuid,
                    "name": p.name,
                    "setting_id": p.setting_id,
                    "etag": p.etag,
                    "raw": p.raw,
                }
                for p in profiles
            ],
        }
        if len(json.dumps(result).encode()) > domain.LIMIT:
            raise ValueError("partition-profile baseline exceeds bound")
        return result


def _native(row):
    return NativeProfile(
        row["uuid"],
        row["name"],
        row["setting_id"],
        row["etag"],
        parse_profile(row["raw"]),
        row["raw"],
        {},
    )


def _baseline(before, current, owned, default):
    expected = dict(before["resource"], DefaultProfileName=default)
    if (
        any(before[k] != current[k] for k in ("system", "partition", "kind"))
        or current["resource"] != expected
    ):
        raise ValueError(
            "partition original identity/current/default/sync baseline changed"
        )
    old = {p["uuid"]: p for p in before["profiles"]}
    new = {p["uuid"]: p for p in current["profiles"]}
    for uuid, row in old.items():
        if uuid not in new:
            raise ValueError("original profile missing")
        require_fresh_profile(_native(row), _native(new[uuid]))
    if {p["name"] for p in current["profiles"] if p["uuid"] not in old} != set(owned):
        raise ValueError("unexpected or missing scratch profile")


def successful(outcome):
    return (
        isinstance(outcome, dict)
        and outcome.get("execution") == "completed"
        and outcome.get("requested_change") == "verified"
        and outcome.get("preservation") in {"verified", "not-applicable"}
        and not outcome.get("error")
        and (
            outcome.get("preservation") != "verified"
            or outcome.get("comparison_complete") is True
        )
        and (
            outcome.get("recovery_profile") is None
            or outcome.get("recovery_verified") is True
        )
    )


def _request(config, journal, current, stage):
    suffix = (
        "vios_partition_profile"
        if config.partition_profile_kind == "VirtualIOServer"
        else "partition_profile"
    )
    args = {
        "system_name_or_uuid": current["system"],
        (
            "vios_name_or_uuid"
            if config.partition_profile_kind == "VirtualIOServer"
            else "lpar_name_or_uuid"
        ): current["partition"],
    }
    action = {
        "create": "save_current",
        "patch": "update",
        "overwrite": "save_current",
        "default-scratch": "set_default",
        "default-original": "set_default",
        "delete-scratch": "delete",
        "delete-recovery": "delete",
    }[stage]
    args["exclusive_writer_window"] = True
    name = journal["name"]
    if stage == "delete-recovery":
        name = journal["recovery"]["name"]
    if stage == "default-original":
        name = journal["before"]["resource"]["DefaultProfileName"]
    if action == "save_current":
        args["name"] = name
        if stage == "overwrite":
            args["overwrite"] = True
    else:
        args["profile_selector"] = name
    if stage in {"patch", "overwrite", "delete-scratch", "delete-recovery"}:
        args["expected_etag"] = next(
            p["etag"] for p in current["profiles"] if p["name"] == name
        )
    if stage == "patch":
        args["patch"] = {"desired_memory": config.partition_profile_memory_mib}
        source = parse_profile(
            next(p["raw"] for p in current["profiles"] if p["name"] == name)
        )
        if compare_profiles(
            source, apply_profile_patch(source, ProfilePatch(**args["patch"]))
        ).equal:
            raise ValueError("configured profile memory patch would be a no-op")
    return f"hmc_{action}_{suffix}", args


async def _reads(client, state, before):
    suffix = (
        "vios_partition_profile"
        if before["kind"] == "VirtualIOServer"
        else "partition_profile"
    )
    args = {
        "system_name_or_uuid": before["system"],
        (
            "vios_name_or_uuid"
            if before["kind"] == "VirtualIOServer"
            else "lpar_name_or_uuid"
        ): before["partition"],
    }
    values = []
    for action in ("list", "read"):
        tool = f"hmc_{action}_{suffix}" + ("s" if action == "list" else "")
        if action == "read":
            args["profile_selector"] = before["profiles"][0]["name"]
        status, data = await dispatch(client, state, tool, args)
        if status != "PASS":
            raise ValueError("initial product profile reads failed")
        data = plain_data(data)
        if not isinstance(data, dict):
            raise TypeError("profile read returned no structured identity")
        if action == "list":
            if (
                data.get("system_uuid") != before["system"]
                or data.get("partition_uuid") != before["partition"]
                or data.get("partition_type") != before["kind"]
                or {p["uuid"] for p in data.get("profiles", [])}
                != {p["uuid"] for p in before["profiles"]}
            ):
                raise ValueError("product profile list identity differs from baseline")
        elif data.get("uuid") != before["profiles"][0]["uuid"]:
            raise ValueError("product profile read identity differs from baseline")
        values.append((tool, action, data))
    return values


async def _exercise(client, state):
    before = await snapshot(state.hmc_config, state.config)
    if any(p["name"].startswith((PREFIX, "hmcprec-")) for p in before["profiles"]):
        raise ValueError("reserved scratch/recovery namespace is not empty")
    journal: dict[str, Any] = {
        "before": before,
        "name": PREFIX + secrets.token_hex(8),
        "steps": [],
        "phase": "prepared",
    }
    state.artifacts.partition_profile_lifecycle = journal
    state.checkpoint()
    evidence = await _reads(client, state, before)
    owned = set()
    default = before["resource"]["DefaultProfileName"]
    for stage in STAGES:
        current = await snapshot(state.hmc_config, state.config)
        _baseline(before, current, owned, default)
        tool, args = _request(state.config, journal, current, stage)
        if stage == "overwrite":
            journal["copy_source"] = next(
                p["raw"] for p in current["profiles"] if p["name"] == journal["name"]
            )
        journal["phase"] = "pending:" + stage
        state.checkpoint()
        status, data = await dispatch(client, state, tool, args)
        outcome = plain_data(data)
        journal["steps"].append({"stage": stage, "outcome": outcome})
        journal["phase"] = "confirmed:" + stage
        state.checkpoint()
        if status != "PASS" or not successful(outcome):
            return
        after = await snapshot(state.hmc_config, state.config)
        if stage == "create":
            owned.add(journal["name"])
        elif stage == "overwrite":
            journal["recovery"] = outcome["recovery_profile"]
            owned.add(journal["recovery"]["name"])
            journal["copy_witness"] = next(
                p["raw"]
                for p in after["profiles"]
                if p["name"] == journal["recovery"]["name"]
            )
            if not compare_profiles(
                parse_profile(journal["copy_source"]),
                parse_profile(journal["copy_witness"]),
                recovery=True,
            ).equal:
                raise ValueError("recovery witness equality failed")
        elif stage.startswith("default-"):
            default = (
                journal["name"]
                if stage == "default-scratch"
                else before["resource"]["DefaultProfileName"]
            )
        elif stage.startswith("delete-"):
            owned.remove(
                journal["name"]
                if stage == "delete-scratch"
                else journal["recovery"]["name"]
            )
        _baseline(before, after, owned, default)
        if stage in {"patch", "overwrite", "default-original", "delete-recovery"}:
            action = {
                "patch": "update",
                "overwrite": "save_current",
                "default-original": "set_default",
                "delete-recovery": "delete",
            }[stage]
            evidence.append((tool, action, outcome))
        state.checkpoint()
    journal["phase"] = "complete"
    state.checkpoint()
    for tool, action, data in evidence:
        if before["kind"] == "VirtualIOServer":
            record_vios(state, tool, action, data)
        else:
            record_lpar(state, tool, action, data)


async def exercise_partition_profiles(client, state):
    if state.group != GROUP:
        return
    configured(state.config)
    if state.hmc_config is None or state.checkpoint is None:
        raise ValueError(
            "partition-profiles requires transport and a durable checkpoint"
        )
    async with asyncio.timeout(600):
        await _exercise(client, state)


async def recover_document(hmc_config, document):
    from .runner import _hmc_identity

    if document.get("hmc") != _hmc_identity(hmc_config):
        raise ValueError("partition-profile recovery HMC identity differs")
    run = document.get("run", {})
    if (
        run.get("group") != GROUP
        or run.get("subtasks") != [SUBTASK]
        or run.get("partial") is not False
    ):
        raise ValueError(
            "partition-profile recovery requires complete named-arm evidence"
        )
    config = decode_saved_config(document.get("config"))
    configured(config)
    journal = document["artifacts"]["partition_profile_lifecycle"]
    steps = journal.get("steps", [])
    if (
        journal.get("phase") != "complete"
        or [s.get("stage") for s in steps] != list(STAGES)
        or not all(successful(s.get("outcome")) for s in steps)
    ):
        raise ValueError("partition-profile lifecycle is incomplete or uncertain")
    _baseline(
        journal["before"],
        await snapshot(hmc_config, config),
        set(),
        journal["before"]["resource"]["DefaultProfileName"],
    )
    comparison = compare_profiles(
        parse_profile(journal["copy_source"]),
        parse_profile(journal["copy_witness"]),
        recovery=True,
    )
    if not comparison.equal or not comparison.complete:
        raise ValueError("retained recovery witness does not match original")
    jobs = [s["outcome"]["job"]["job_id"] for s in steps if s["outcome"].get("job")]
    if len(jobs) != 4 or len(set(jobs)) != 4:
        raise ValueError("finite lifecycle job identities incomplete or repeated")
    pinned = hmc_config.model_copy(update={"port": hmc_config.port})
    async with asyncio.timeout(300), HMCClient(pinned) as hmc:
        for ident in jobs:
            entry = await hmc.get_job_entry(ident)
            domain._job_identity(hmc, entry, ident)
            values = domain._result_values(entry)
            if not isinstance(entry, dict):
                raise TypeError("native job readback missing")
            resource = entry["Resource"]
            if (
                resource.get("Status") != "COMPLETED_OK"
                or values["returnCode"] != "0"
                or values["detailedStatus"]
                or any(
                    values.get(k) or resource.get(k) for k in ("ErrorData", "errorData")
                )
            ):
                raise ValueError(
                    "native profile job is not independently confirmed complete"
                )


async def dispatch_lpar(client, state, tool, args):
    """Literal tool calls keep the served-schema dispatch gate effective."""
    if tool == "hmc_list_partition_profiles":
        return await state.call(
            client,
            "hmc_list_partition_profiles",
            system_name_or_uuid=args["system_name_or_uuid"],
            lpar_name_or_uuid=args["lpar_name_or_uuid"],
        )
    if tool == "hmc_read_partition_profile":
        return await state.call(
            client,
            "hmc_read_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            lpar_name_or_uuid=args["lpar_name_or_uuid"],
            profile_selector=args["profile_selector"],
        )
    if tool == "hmc_update_partition_profile":
        return await state.call(
            client,
            "hmc_update_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            lpar_name_or_uuid=args["lpar_name_or_uuid"],
            profile_selector=args["profile_selector"],
            expected_etag=args["expected_etag"],
            patch=args["patch"],
            exclusive_writer_window=args["exclusive_writer_window"],
        )
    if tool == "hmc_save_current_partition_profile":
        return await state.call(
            client,
            "hmc_save_current_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            lpar_name_or_uuid=args["lpar_name_or_uuid"],
            name=args["name"],
            overwrite=args.get("overwrite", False),
            expected_etag=args.get("expected_etag"),
            exclusive_writer_window=args["exclusive_writer_window"],
        )
    if tool == "hmc_set_default_partition_profile":
        return await state.call(
            client,
            "hmc_set_default_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            lpar_name_or_uuid=args["lpar_name_or_uuid"],
            profile_selector=args["profile_selector"],
            exclusive_writer_window=args["exclusive_writer_window"],
        )
    if tool == "hmc_delete_partition_profile":
        return await state.call(
            client,
            "hmc_delete_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            lpar_name_or_uuid=args["lpar_name_or_uuid"],
            profile_selector=args["profile_selector"],
            expected_etag=args["expected_etag"],
            exclusive_writer_window=args["exclusive_writer_window"],
        )
    raise ValueError("unknown finite profile tool")


def record_lpar(state, tool, action, data):
    assertions = [Assertion("original-baseline-restored", True)]
    if action == "list":
        state.record_verified(
            SUBTASK,
            tool,
            operation="lpar_profile.list",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "read":
        state.record_verified(
            SUBTASK,
            tool,
            operation="lpar_profile.read",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "update":
        state.record_verified(
            SUBTASK,
            tool,
            operation="lpar_profile.update",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "save_current":
        state.record_verified(
            SUBTASK,
            tool,
            operation="lpar_profile.save_current",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "set_default":
        state.record_verified(
            SUBTASK,
            tool,
            operation="lpar_profile.set_default",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "delete":
        state.record_verified(
            SUBTASK,
            tool,
            operation="lpar_profile.delete",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    raise ValueError("unknown finite profile observation")


async def dispatch_vios(client, state, tool, args):
    """Literal tool calls keep the served-schema dispatch gate effective."""
    if tool == "hmc_list_vios_partition_profiles":
        return await state.call(
            client,
            "hmc_list_vios_partition_profiles",
            system_name_or_uuid=args["system_name_or_uuid"],
            vios_name_or_uuid=args["vios_name_or_uuid"],
        )
    if tool == "hmc_read_vios_partition_profile":
        return await state.call(
            client,
            "hmc_read_vios_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            vios_name_or_uuid=args["vios_name_or_uuid"],
            profile_selector=args["profile_selector"],
        )
    if tool == "hmc_update_vios_partition_profile":
        return await state.call(
            client,
            "hmc_update_vios_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            vios_name_or_uuid=args["vios_name_or_uuid"],
            profile_selector=args["profile_selector"],
            expected_etag=args["expected_etag"],
            patch=args["patch"],
            exclusive_writer_window=args["exclusive_writer_window"],
        )
    if tool == "hmc_save_current_vios_partition_profile":
        return await state.call(
            client,
            "hmc_save_current_vios_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            vios_name_or_uuid=args["vios_name_or_uuid"],
            name=args["name"],
            overwrite=args.get("overwrite", False),
            expected_etag=args.get("expected_etag"),
            exclusive_writer_window=args["exclusive_writer_window"],
        )
    if tool == "hmc_set_default_vios_partition_profile":
        return await state.call(
            client,
            "hmc_set_default_vios_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            vios_name_or_uuid=args["vios_name_or_uuid"],
            profile_selector=args["profile_selector"],
            exclusive_writer_window=args["exclusive_writer_window"],
        )
    if tool == "hmc_delete_vios_partition_profile":
        return await state.call(
            client,
            "hmc_delete_vios_partition_profile",
            system_name_or_uuid=args["system_name_or_uuid"],
            vios_name_or_uuid=args["vios_name_or_uuid"],
            profile_selector=args["profile_selector"],
            expected_etag=args["expected_etag"],
            exclusive_writer_window=args["exclusive_writer_window"],
        )
    raise ValueError("unknown finite profile tool")


def record_vios(state, tool, action, data):
    assertions = [Assertion("original-baseline-restored", True)]
    if action == "list":
        state.record_verified(
            SUBTASK,
            tool,
            operation="vios_profile.list",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "read":
        state.record_verified(
            SUBTASK,
            tool,
            operation="vios_profile.read",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "update":
        state.record_verified(
            SUBTASK,
            tool,
            operation="vios_profile.update",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "save_current":
        state.record_verified(
            SUBTASK,
            tool,
            operation="vios_profile.save_current",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "set_default":
        state.record_verified(
            SUBTASK,
            tool,
            operation="vios_profile.set_default",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    if action == "delete":
        state.record_verified(
            SUBTASK,
            tool,
            operation="vios_profile.delete",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
        return
    raise ValueError("unknown finite profile observation")


async def dispatch(client, state, tool, args):
    if state.config.partition_profile_kind == "VirtualIOServer":
        return await dispatch_vios(client, state, tool, args)
    return await dispatch_lpar(client, state, tool, args)
