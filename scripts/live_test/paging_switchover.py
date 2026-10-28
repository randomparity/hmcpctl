"""ST66 one forward attempt and immutable-baseline read-only confirmation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict
from pathlib import Path

from pydantic import TypeAdapter

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.ownership import authorize_lpar_ownership_description
from hmcpctl.operations.lpar.paging import (
    _require_complete,
    _resolve_target,
    compare_paging,
)
from hmcpctl.operations.lpar.paging_observation import PagingObservation, observe_paging
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.profiles import get_lpar_description

from . import evidence
from .config import decode_saved_config
from .observation import Assertion
from .results import field

SUBTASK = 66
GROUP = "lpar-paging-switchover"
SCENARIO = "st66-lpar-paging-switchover"
TOOL = "hmc_switch_lpar_paging_vios"
OPERATION = "lpar.switch_paging_vios"
PHASES = (
    "paging baseline",
    "paging forward intent",
    "paging forward observed",
    "paging recovery observation",
)
_HEX = re.compile(r"[0-9a-f]{40}")


def configured_paging_fixture(config) -> tuple[str, str]:
    target, expected = config.lp3_name, config.paging_expected_current_vios
    if (
        any(
            not isinstance(value, str)
            or not value.strip()
            or value.startswith("example-")
            for value in (target, expected)
        )
        or target in config.protected_lpar_names
        or config.paging_writer_window is not True
    ):
        raise ValueError(
            "Configure an existing non-protected paging fixture, expected current VIOS and explicit writer window"
        )
    return target, expected


def _destination(config) -> dict:
    return {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "verify_ssl": config.verify_ssl,
        "schema_version": config.schema_version,
    }


def _candidate(config) -> tuple[str, str]:
    root = evidence.repository_root()
    provenance = evidence.run_provenance(
        [SUBTASK], GROUP, root, config.schema_version, False
    )
    tree = evidence._git(root, "rev-parse", "HEAD^{tree}") if root else None
    # served_client composes the local application; bind its editable source to this checkout.
    import hmcpctl.operations.lpar.paging as product

    if (
        root is None
        or Path(product.__file__).resolve()
        != root / "src/hmcpctl/operations/lpar/paging.py"
        or provenance["tree_clean"] is not True
        or not _HEX.fullmatch(provenance["tested_commit"] or "")
        or tree is None
        or tree.returncode != 0
        or not _HEX.fullmatch(tree.stdout.strip())
    ):
        raise ValueError(
            "Paging arm requires a clean immutable locally deployed source commit/tree"
        )
    return provenance["tested_commit"], tree.stdout.strip()


async def _capture(hmc, config, selector):
    system, name, target = await _resolve_target(hmc, config.system_name, selector)
    observation = await observe_paging(hmc, system, name, target)
    return observation


async def _owned_fixture(hmc, config, before):
    _require_complete(before)
    for target in (before.target, *before.providers):
        if target.name in config.protected_lpar_names:
            raise ValueError("Paging fixture contains a protected partition")
        if not math.isfinite(hmc.config.ssh_timeout) or hmc.config.ssh_timeout <= 0:
            raise ValueError(
                "Paging ownership read requires a finite positive ssh_timeout"
            )
        description = await get_lpar_description(
            hmc.config,
            before.system_name,
            target.name,
            max_output_bytes=5 * 1024 * 1024,
        )
        owner = authorize_lpar_ownership_description(
            hmc, before.system_name, target.name, description, operation="lpar-mutation"
        )
        if owner != (hmc.config.agent_id or "hmcpctl"):
            raise ValueError(
                "Paging live fixture requires positive matching ownership on consumer and both providers"
            )


def _record(state, label, data, candidate):
    if _candidate(state.hmc_config) != candidate:
        raise ValueError("Paging deployed source changed before checkpoint")
    row = {
        "subtask": SUBTASK,
        "tool": label,
        "status": "PASS",
        "result": "observed",
        "data": data,
    }
    projection = {
        "run": evidence.run_provenance(
            [SUBTASK],
            GROUP,
            evidence.repository_root(),
            state.hmc_config.schema_version,
            True,
        ),
        "config": asdict(state.config),
        "hmc": _destination(state.hmc_config),
        "artifacts": asdict(state.artifacts),
        "results": [*state.results, row],
    }
    if (
        len(json.dumps(projection, allow_nan=False, indent=2).encode())
        > 1024 * 1024 - 4096
    ):
        raise ValueError("Paging checkpoint exceeds its bounded private record")
    state.record(SUBTASK, label, "PASS", data)
    state.checkpoint()


async def exercise_paging_switchover(client, state):
    if state.group != GROUP or state.hmc_config is None:
        state.skip(SUBTASK, GROUP, "requires named opt-in group and credentials")
        return
    completed = False
    try:
        selector, expected = configured_paging_fixture(state.config)
        if state.checkpoint is None or any(
            row.get("subtask") == SUBTASK for row in state.results
        ):
            raise ValueError(
                "A fresh private checkpoint is required before one paging attempt"
            )
        candidate = _candidate(state.hmc_config)
        async with HMCClient(state.hmc_config) as hmc:
            before = await _capture(hmc, state.config, selector)
            await _owned_fixture(hmc, state.config, before)
            current = before.current_native
            if current is None or (
                current.uuid != expected.lower()
                if is_uuid(expected)
                else current.name != expected
            ):
                raise ValueError("Paging live expected provider is stale")
            baseline = {
                "schema_version": 1,
                "candidate_commit": candidate[0],
                "deployed_tree": candidate[1],
                "destination": _destination(state.hmc_config),
                "fixtureselector": selector,
                "expected_current": expected,
                "before": asdict(before),
            }
            _record(state, PHASES[0], baseline, candidate)
            _record(
                state,
                PHASES[1],
                {
                    "operation": OPERATION,
                    "attempt": 1,
                    "expected_current": current.uuid,
                    "exclusive_writer_window": True,
                },
                candidate,
            )
            try:
                status, result = await state.call(
                    client,
                    "hmc_switch_lpar_paging_vios",
                    lpar_name_or_uuid=before.target.uuid,
                    system_name_or_uuid=before.system_uuid,
                    expected_current_paging_vios_name_or_uuid=current.uuid,
                    exclusive_writer_window=True,
                )
                completed = (
                    status == "PASS" and field(result, "workflow_completed") is True
                )
                data = {
                    key: field(result, key)
                    for key in (
                        "submission",
                        "observation",
                        "preservation",
                        "outcome",
                        "workflow_completed",
                        "remote_refusal_code",
                        "diagnostics",
                    )
                }
                _record(state, PHASES[2], {"call_status": status, **data}, candidate)
            finally:
                # No reverse path is imported: even an interrupted call gets a fresh read if this process survives.
                after = await _capture(hmc, state.config, before.target.uuid)
                status, diagnostics = _recovery_status(before, after, False)
                _record(
                    state,
                    PHASES[3],
                    {
                        "status": status,
                        "diagnostics": diagnostics,
                        "after": asdict(after),
                    },
                    candidate,
                )
    except (HMCError, ValueError, TypeError, PermissionError, OSError) as error:
        state.record(
            SUBTASK,
            GROUP,
            "FAIL",
            f"Paging arm incomplete: {type(error).__name__}; mandatory read-only recovery required",
        )
    state.record_verified(
        SUBTASK,
        TOOL,
        operation="lpar.switch_paging_vios",
        scenario=SCENARIO,
        assertions=[
            Assertion("forward-independently-completed", completed),
            Assertion("original-baseline-restored", False),
        ],
        cleanup="failed",
        data={"forward_completed": completed, "restoration": "held"},
    )


def _decode_before(value):
    observation = TypeAdapter(PagingObservation).validate_json(
        json.dumps(value), strict=True
    )
    if json.loads(json.dumps(asdict(observation))) != value:
        raise ValueError("Paging baseline has missing or unknown fields")
    _require_complete(observation)
    identities = (observation.target, *observation.providers)
    if (
        not is_uuid(observation.system_uuid)
        or observation.pool_uuid is None
        or not is_uuid(observation.pool_uuid)
        or any(
            not is_uuid(target.uuid)
            or target.partition_id < 1
            or not target.name.strip()
            for target in identities
        )
        or observation.pool_id is None
        or observation.pool_id < 0
        or observation.state != "running"
        or observation.shared is not True
        or observation.uom_redundant is not True
        or observation.native_redundant is not True
        or observation.provider_dedicated != (True, True)
        or len({provider.uuid for provider in observation.providers}) != 2
        or observation.configured is None
        or {observation.configured.primary, observation.configured.secondary}
        != set(observation.providers)
        or observation.current_native != observation.current_uom
        or observation.current_native not in observation.providers
        or {uuid for uuid, path in observation.paths if path}
        != {provider.uuid for provider in observation.providers}
    ):
        raise ValueError(
            "Paging baseline does not establish positive configured and runtime redundancy"
        )
    return observation


def _baseline(config, document):
    if not isinstance(document, dict):
        raise TypeError("Paging recovery requires a results object")
    run = document.get("run", {})
    if (
        not isinstance(run, dict)
        or run.get("group") != GROUP
        or run.get("subtasks") != [SUBTASK]
        or run.get("tree_clean") is not True
        or type(run.get("partial")) is not bool
    ):
        raise ValueError("Paging recovery requires its named, source-bound run")
    saved_config = decode_saved_config(document.get("config"))
    selector, expected = configured_paging_fixture(saved_config)
    rows = document.get("results")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Paging recovery results are malformed")
    baselines = [
        row
        for row in rows
        if row.get("subtask") == SUBTASK and row.get("tool") == PHASES[0]
    ]
    if (
        len(baselines) != 1
        or baselines[0].get("status") != "PASS"
        or rows[0] is not baselines[0]
    ):
        raise ValueError("Paging recovery requires exactly one original first baseline")
    baseline = baselines[0].get("data")
    if not isinstance(baseline, dict) or set(baseline) != {
        "schema_version",
        "candidate_commit",
        "deployed_tree",
        "destination",
        "fixtureselector",
        "expected_current",
        "before",
    }:
        raise ValueError("Paging baseline shape is incomplete")
    if (
        type(baseline["schema_version"]) is not int
        or baseline["schema_version"] != 1
        or not _HEX.fullmatch(baseline["candidate_commit"] or "")
        or not _HEX.fullmatch(baseline["deployed_tree"] or "")
        or baseline["candidate_commit"] != run.get("tested_commit")
    ):
        raise ValueError("Paging baseline commit/tree is unbound")
    destination = _destination(config)
    if (
        baseline["destination"] != destination
        or document.get("hmc")
        != {key: destination[key] for key in ("host", "port", "user", "verify_ssl")}
        or run.get("schema_version") != config.schema_version
    ):
        raise ValueError("Paging baseline destination is not the selected connection")
    before = _decode_before(baseline["before"])
    if (
        baseline["fixtureselector"] != selector
        or baseline["expected_current"] != expected
        or (
            before.target.uuid != selector.lower()
            if is_uuid(selector)
            else before.target.name != selector
        )
    ):
        raise ValueError("Paging baseline is not the configured fixture")
    if before.current_native is None or (
        before.current_native.uuid != expected.lower()
        if is_uuid(expected)
        else before.current_native.name != expected
    ):
        raise ValueError("Paging baseline expected provider is inconsistent")
    phases = [
        row["tool"]
        for row in rows
        if row.get("subtask") == SUBTASK and row.get("tool") in PHASES
    ]
    indices = [PHASES.index(phase) for phase in phases]
    if indices != sorted(set(indices)) or (len(indices) > 1 and 1 not in indices):
        raise ValueError("Paging phases are duplicated or reordered")
    intent = [row for row in rows if row.get("tool") == PHASES[1]]
    if intent and intent[0].get("data") != {
        "operation": OPERATION,
        "attempt": 1,
        "expected_current": before.current_native.uuid,
        "exclusive_writer_window": True,
    }:
        raise ValueError("Paging intent is not one explicit forward attempt")
    observed = [
        row
        for row in rows
        if row.get("subtask") == SUBTASK and row.get("tool") == PHASES[2]
    ]
    result = observed[0].get("data", {}) if observed else {}
    terminal = (
        not run["partial"]
        and phases == list(PHASES)
        and isinstance(result, dict)
        and result.get("call_status") == "PASS"
        and result.get("workflow_completed") is True
        and result.get("outcome") == "completed"
        and result.get("observation") == "expected"
        and result.get("preservation") == "verified"
        and result.get("submission") in {"acknowledged", "remote-error", "unknown"}
    )
    return saved_config, before, terminal


def _recovery_status(before, after, terminal):
    preservation, diagnostics = compare_paging(before, after)
    current_drift = (
        after.current_native is not None
        and after.current_native != before.current_native
        or after.current_uom is not None
        and after.current_uom != before.current_uom
    )
    if current_drift:
        diagnostics += ("current-provider",)
    status = (
        "drift"
        if current_drift or preservation == "drifted"
        else "clean"
        if preservation == "verified" and terminal
        else "unverified"
    )
    return status, diagnostics


async def recover_document(hmc_config, document) -> dict:
    config, before, terminal = _baseline(hmc_config, document)
    async with HMCClient(hmc_config) as hmc:
        after = await _capture(hmc, config, before.target.uuid)
    status, diagnostics = _recovery_status(before, after, terminal)
    return {"status": status, "diagnostics": diagnostics, "after": asdict(after)}
