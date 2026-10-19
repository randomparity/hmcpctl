"""Finite product system-profile scenario and independent read-only baseline proof."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from copy import deepcopy
from dataclasses import asdict, fields
from typing import TYPE_CHECKING

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import authorize_lpar_ownership_description
from hmcpctl.operations.systems import profiles as domain
from hmcpctl.resource_identity import resolve_system_uuid

from .observation import Assertion, plain_data

if TYPE_CHECKING:
    from fastmcp import Client

    from .config import LiveTestConfig
    from .runner import RunState

SUBTASK = 44
GROUP = "system-profiles"
PREFIX = "hmcpctl-live-sysprof-"
SCENARIO = "st44-system-profiles"


def configured(config: LiveTestConfig) -> list[domain.SystemProfilePair]:
    raw = json.loads(config.system_profile_members_json)
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError(
            "system-profiles requires at least two explicit contained member pairs"
        )
    pairs = [domain.SystemProfilePair.model_validate(p) for p in raw]
    if len({p.partition for p in pairs}) != len(pairs):
        raise ValueError("system-profiles requires distinct partitions")
    if config.system_profile_writer_window is not True:
        raise ValueError(
            "system-profiles requires confirmed caller writer coordination"
        )
    if config.system_profile_activation_action not in {
        "none",
        "validate",
        "activate",
        "validate-and-activate",
    }:
        raise ValueError("system-profiles action is invalid")
    if (
        config.system_profile_continue_on_error
        and config.system_profile_activation_action in {"none", "validate"}
    ):
        raise ValueError("system-profiles continuation requires an activating action")
    if (
        type(config.system_profile_continue_on_error) is not bool
        or type(config.system_profile_ownership_override) is not bool
    ):
        raise TypeError("system-profiles flags must be booleans")
    return pairs


async def snapshot(hmc_config, config):
    """Fresh bounded transport observations, never saved success flags."""
    pairs = configured(config)
    pinned = hmc_config.model_copy(update={"port": hmc_config.port})
    async with asyncio.timeout(300), HMCClient(pinned) as hmc:
        system = await resolve_system_uuid(hmc, config.system_name)
        phase, records = await domain._inventory(hmc, system)
        selected = domain._resolve_pairs(phase, pairs)
        for member in selected:
            resource = phase.entries[member.partition_uuid]["entry"]["Resource"]
            if "Description" not in resource:
                raise ValueError("system-profile ownership evidence is missing")
            authorize_lpar_ownership_description(
                hmc,
                phase.cli_name,
                member.partition_name,
                domain._text(resource["Description"]),
                operation="lpar-mutation",
                ownership_override=config.system_profile_ownership_override,
            )
        result = {
            "system_uuid": system,
            "stable": domain._stable(phase),
            "records": [asdict(r) for r in sorted(records, key=lambda r: r.name)],
            "selected": [asdict(m) for m in selected],
        }
        encoded = json.dumps(result, sort_keys=True)
        if len(encoded.encode()) > domain.LIMIT:
            raise ValueError("product system-profile baseline exceeds bound")
        return json.loads(encoded)


def expected_snapshot(before, owned):
    expected = deepcopy(before)
    records = {r["name"]: r for r in before["records"]}
    records.update(
        {name: {"name": name, "members": members} for name, members in owned.items()}
    )
    expected["records"] = [records[name] for name in sorted(records)]
    expected["stable"][2] = sorted(records)
    return expected


def successful(outcome):
    if (
        not isinstance(outcome, dict)
        or set(outcome) != {f.name for f in fields(domain.SystemProfileOutcome)}
        or outcome.get("state") != "completed"
        or outcome.get("error") is not None
    ):
        return False
    action = outcome.get("action")
    if action not in {
        "create",
        "update",
        "delete",
        "validate",
        "activate",
        "validate-and-activate",
    }:
        return False
    if outcome.get("postcondition") != (
        "not-applicable" if action == "validate" else "verified"
    ):
        return False
    if outcome.get("backend") != (
        "ssh-command"
        if action in {"delete", "validate", "validate-and-activate"}
        else "rest-job"
    ):
        return False
    if outcome.get("activation_execution") != (
        "complete" if action in {"activate", "validate-and-activate"} else None
    ):
        return False
    if outcome.get("backend") == "ssh-command":
        return (
            outcome.get("job") is None
            and outcome.get("return_code") is None
            and outcome.get("command_status") == "completed"
        )
    job = outcome.get("job")
    return (
        outcome.get("backend") == "rest-job"
        and outcome.get("return_code") == "0"
        and isinstance(job, dict)
        and set(job) == {f.name for f in fields(domain.JobOutcome)}
        and outcome.get("command_status") is None
        and job.get("error") is None
        and job.get("job") is None
        and job.get("status") == "COMPLETED_OK"
        and job.get("timed_out") is False
        and job.get("found") is True
        and isinstance(job.get("job_id"), str)
        and re.fullmatch(r"[A-Za-z0-9_:-][A-Za-z0-9_.:-]{0,4095}", job["job_id"])
        is not None
        and job.get("job_href") == f"/rest/api/uom/jobs/{job['job_id']}"
    )


def stages(names, action):
    a, b, c, d = names
    result = [
        ("create-a", "create", a),
        ("create-b", "create", b),
        ("rename-b", "update", c),
        ("replace-c", "update", c),
        ("rename-replace-c", "update", d),
    ]
    if action != "none":
        result.append(("action-a", action, a))
    return result + [("delete-a", "delete", a), ("delete-d", "delete", d)]


def validate_finished(artifacts, config):
    names = artifacts.system_profile_names
    if (
        len(names) != 4
        or len(set(names)) != 4
        or any(not re.fullmatch(re.escape(PREFIX) + r"[0-9a-f]{16}", n) for n in names)
    ):
        raise ValueError("system-profile run identities are incomplete")
    expected = stages(names, config.system_profile_activation_action)
    rows = artifacts.system_profile_jobs
    phase = (
        "finished-activation"
        if config.system_profile_activation_action
        in {"activate", "validate-and-activate"}
        else "finished"
    )
    if artifacts.system_profile_phase != phase or len(rows) != len(expected):
        raise ValueError("system-profile finite terminal sequence is incomplete")
    before = artifacts.system_profile_baseline["before"]
    listing = artifacts.system_profile_baseline.get("listing")
    expected_records = expected_snapshot(
        before, {names[0]: before["selected"], names[1]: before["selected"][:1]}
    )["records"]
    if (
        not isinstance(listing, dict)
        or listing.get("source") != "ssh-command"
        or listing.get("system_uuid") != before["system_uuid"]
        or listing.get("job") is not None
        or listing.get("command_status") != "completed"
        or listing.get("error") is not None
        or listing.get("records") != expected_records
    ):
        raise ValueError(
            "system-profile whole-list reader proof is missing or contradictory"
        )
    job_ids = set()
    for row, (stage, action, name) in zip(rows, expected, strict=True):
        outcome = row.get("outcome")
        if (
            row.get("stage") != stage
            or not successful(outcome)
            or outcome.get("action") != action
            or outcome.get("name") != name
            or outcome.get("system_uuid")
            != artifacts.system_profile_baseline["before"]["system_uuid"]
        ):
            raise ValueError(
                "system-profile stage outcome is failed, unknown or contradictory"
            )
        job = outcome.get("job")
        if job:
            if job["job_id"] in job_ids:
                raise ValueError("system-profile job handle is repeated")
            job_ids.add(job["job_id"])


async def reconcile(hmc_config, config, artifacts):
    """Always read actual state, then distinguish terminal proof from mere absence."""
    current = await snapshot(hmc_config, config)
    before = artifacts.system_profile_baseline.get("before")
    expected = artifacts.system_profile_baseline.get("activation_expected", before)
    if not isinstance(before, dict) or current != expected:
        raise ValueError(
            "system-profile baseline differs or is unavailable; manual reconciliation required"
        )
    validate_finished(artifacts, config)
    if current != before or config.system_profile_activation_action in {
        "activate",
        "validate-and-activate",
    }:
        raise ValueError(
            "known activation observations differ from original baseline; operator reconciliation required"
        )
    return current


async def exercise_system_profiles(client: Client, state: RunState) -> None:
    """Only this named arm performs the finite metadata lifecycle; no power cleanup."""
    if state.group != GROUP:
        state.skip(
            SUBTASK, "hmc_create_system_profile", "requires named system-profiles arm"
        )
        return
    pairs = configured(state.config)
    if state.hmc_config is None or state.checkpoint is None:
        raise ValueError(
            "system-profiles requires configured transport and durable recovery checkpoint"
        )
    before = await snapshot(state.hmc_config, state.config)
    if any(r["name"].startswith(PREFIX) for r in before["records"]):
        raise ValueError("reserved system-profile namespace is not empty")
    names = [PREFIX + secrets.token_hex(8) for _ in range(4)]
    if len(set(names)) != 4:
        raise ValueError("system-profile generated-name collision")
    artifacts = state.artifacts
    artifacts.system_profile_names = names
    artifacts.system_profile_baseline = {"before": before}
    artifacts.system_profile_jobs = []
    owned = {}
    working = deepcopy(before)
    a, b, c, d = names
    selected = before["selected"]
    system = before["system_uuid"]
    action = state.config.system_profile_activation_action
    for stage, _action, name in stages(names, action):
        current = await snapshot(state.hmc_config, state.config)
        if current != expected_snapshot(working, owned):
            raise ValueError(
                "system-profile pre-effect baseline changed; no later writes"
            )
        artifacts.system_profile_phase = "pending:" + stage
        state.checkpoint()
        tool, status, data = await dispatch_stage(
            client, state, system, stage, names, pairs
        )
        outcome = plain_data(data)
        artifacts.system_profile_jobs.append({"stage": stage, "outcome": outcome})
        artifacts.system_profile_phase = "confirmed:" + stage
        state.checkpoint()
        good = status == "PASS" and successful(outcome)
        record_stage(state, tool, stage, good, data)
        if not good:
            return
        if stage == "create-a":
            owned[a] = selected
        elif stage == "create-b":
            owned[b] = selected[:1]
        elif stage == "rename-b":
            owned[c] = owned.pop(b)
        elif stage == "replace-c":
            owned[c] = selected
        elif stage == "rename-replace-c":
            owned.pop(c)
            owned[d] = selected[1:2]
        elif stage.startswith("delete-"):
            owned.pop(name)
        if stage == "create-b":
            status, listing = await state.call(
                client, "hmc_list_system_profiles", system_name_or_uuid=system
            )
            listing = plain_data(listing)
            artifacts.system_profile_baseline["listing"] = listing
            state.checkpoint()
            listed = (
                status == "PASS"
                and isinstance(listing, dict)
                and listing.get("command_status") == "completed"
                and listing.get("source") == "ssh-command"
                and listing.get("system_uuid") == system
                and listing.get("job") is None
                and listing.get("error") is None
                and listing.get("records")
                == expected_snapshot(working, owned)["records"]
            )
            state.record_verified(
                SUBTASK,
                "hmc_list_system_profiles",
                operation="system_profile.list",
                scenario=SCENARIO,
                assertions=[Assertion("two-scratch-profiles-bound", listed)],
                cleanup="not-required",
                data=listing,
            )
            if not listed:
                return
        if stage == "action-a" and action != "validate":
            current = await snapshot(state.hmc_config, state.config)
            working = activation_baseline(before, current, owned)
            artifacts.system_profile_baseline["activation_expected"] = working
            state.checkpoint()
    if await snapshot(state.hmc_config, state.config) != working:
        raise ValueError(
            "system-profile final baseline differs; caller reconciliation required"
        )
    artifacts.system_profile_phase = (
        "finished-activation"
        if action in {"activate", "validate-and-activate"}
        else "finished"
    )
    state.checkpoint()


async def preflight(hmc_config, config):
    current = await snapshot(hmc_config, config)
    if any(r["name"].startswith(PREFIX) for r in current["records"]):
        raise ValueError("reserved system-profile namespace is occupied")
    return current


async def recover_document(hmc_config, document):
    from .config import decode_artifacts, decode_saved_config
    from .runner import _hmc_identity

    if document.get("hmc") != _hmc_identity(hmc_config):
        raise ValueError(
            "system-profile recovery transport identity differs before authentication"
        )
    config = decode_saved_config(document["config"])
    artifacts = decode_artifacts(document["artifacts"])
    current = await reconcile(hmc_config, config, artifacts)
    run = document.get("run", {})
    if (
        run.get("group") != GROUP
        or run.get("subtasks") != [SUBTASK]
        or run.get("partial") is not False
    ):
        raise ValueError(
            "system-profile original run remains partial or has another scope"
        )
    return current


def activation_baseline(before, current, owned):
    """Keep approved member effects separate; protect every other baseline field."""
    expected = expected_snapshot(before, owned)
    a, z = deepcopy(expected), deepcopy(current)
    for item in (a, z):
        for member in before["selected"]:
            resource = item["stable"][3][member["partition_uuid"]][1]
            for key in (
                "PartitionState",
                "LastActivatedProfile",
                "AssociatedPartitionProfile",
                "PartitionMemoryConfiguration",
                "PartitionProcessorConfiguration",
            ):
                resource.pop(key, None)
    if a != z:
        raise ValueError(
            "protected system-profile baseline changed beyond selected activation facts"
        )
    working = deepcopy(before)
    working["stable"][3] = deepcopy(current["stable"][3])
    return working


async def dispatch_stage(client, state, system, stage, names, pairs):
    """Closed literal MCP calls, visible to the repository dispatch-shape gate."""
    a, b, c, d = names
    config = state.config
    if stage in {"create-a", "create-b"}:
        status, data = await state.call(
            client,
            "hmc_create_system_profile",
            system_name_or_uuid=system,
            name=a if stage == "create-a" else b,
            members=[
                p.model_dump() for p in (pairs if stage == "create-a" else pairs[:1])
            ],
            exclusive_writer_window=config.system_profile_writer_window,
            ownership_override=config.system_profile_ownership_override,
        )
        return "hmc_create_system_profile", status, data
    if stage in {"rename-b", "replace-c", "rename-replace-c"}:
        status, data = await state.call(
            client,
            "hmc_update_system_profile",
            system_name_or_uuid=system,
            name=b if stage == "rename-b" else c,
            new_name=c
            if stage == "rename-b"
            else d
            if stage == "rename-replace-c"
            else None,
            members=[
                p.model_dump() for p in (pairs if stage == "replace-c" else pairs[1:2])
            ]
            if stage != "rename-b"
            else None,
            exclusive_writer_window=config.system_profile_writer_window,
            ownership_override=config.system_profile_ownership_override,
        )
        return "hmc_update_system_profile", status, data
    if stage == "action-a":
        status, data = await state.call(
            client,
            "hmc_activate_system_profile",
            system_name_or_uuid=system,
            name=a,
            action=config.system_profile_activation_action,
            continue_on_error=config.system_profile_continue_on_error,
            exclusive_writer_window=config.system_profile_writer_window,
            ownership_override=config.system_profile_ownership_override,
        )
        return "hmc_activate_system_profile", status, data
    if stage in {"delete-a", "delete-d"}:
        status, data = await state.call(
            client,
            "hmc_delete_system_profile",
            system_name_or_uuid=system,
            name=a if stage == "delete-a" else d,
            exclusive_writer_window=config.system_profile_writer_window,
            ownership_override=config.system_profile_ownership_override,
        )
        return "hmc_delete_system_profile", status, data
    raise ValueError("unknown finite system-profile stage")


def record_stage(state, tool, stage, good, data):
    assertions = [Assertion(stage + "-known-success", good)]
    if tool == "hmc_create_system_profile":
        state.record_verified(
            SUBTASK,
            "hmc_create_system_profile",
            operation="system_profile.create",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="not-required",
            data=data,
        )
    elif tool == "hmc_update_system_profile":
        state.record_verified(
            SUBTASK,
            "hmc_update_system_profile",
            operation="system_profile.update",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="not-required",
            data=data,
        )
    elif tool == "hmc_delete_system_profile":
        state.record_verified(
            SUBTASK,
            "hmc_delete_system_profile",
            operation="system_profile.delete",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="not-required",
            data=data,
        )
    else:
        state.record_verified(
            SUBTASK,
            "hmc_activate_system_profile",
            operation="system_profile.activate",
            scenario=SCENARIO,
            assertions=assertions,
            cleanup="not-required",
            data=data,
        )
