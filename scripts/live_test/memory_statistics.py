"""ST67 finite reset-observation record and read-only environment confirmation."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from hmcpctl.client.core import HMCClient
from hmcpctl.config import env_var_value
from hmcpctl.documents.lpar_memory import LparMemoryStatisticsReset
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.memory_statistics import (
    _admit,
    _context,
    _identity,
    _statistics,
    parse_statistics,
)
from hmcpctl.operations.lpar.ownership import authorize_lpar_ownership_description
from hmcpctl.resource_identity import is_uuid, resolve_system_uuid
from hmcpctl.ssh.profiles import get_lpar_description

from . import evidence
from .config import LiveTestConfig, decode_saved_config
from .observation import Assertion

SUBTASK = 67
GROUP = "lpar-memory-statistics"
SCENARIO = "st67-lpar-memory-statistics-reset"
TOOL = "hmc_reset_lpar_memory_statistics"
OPERATION = "lpar.reset_memory_statistics"
PHASES = (
    "memory-statistics baseline",
    "memory-statistics reset intent",
    "memory-statistics reset observed",
    "memory-statistics final observation",
)
_CONTEXT_KEYS = {
    "system_uuid",
    "system_name",
    "uuid",
    "name",
    "native_id",
    "kind",
    "state",
    "shared",
    "ams",
    "partition_type",
    "allocation",
}
_ALLOCATION = {
    "CurrentMemory",
    "DesiredMemory",
    "RuntimeMemory",
    "CurrentMinimumMemory",
    "CurrentMaximumMemory",
}
_BASELINE_KEYS = {
    "schema_version",
    "candidate_commit",
    "system_uuid",
    "system_name",
    "uuid",
    "name",
    "nativeID",
    "kind",
    "context",
    "allocation",
    "statistics",
    "request",
    "destination",
    "fixtureselector",
}
_RESULT_KEYS = {
    "system_uuid",
    "system_name",
    "lpar_uuid",
    "lpar_name",
    "partition_id",
    "partition_kind",
    "request",
    "native_completion",
    "native_output",
    "submission_error",
    "before_statistics",
    "after_statistics",
    "after_error",
    "identity_preserved",
    "workflow_completed",
    "reset_verification",
}


def configured(config) -> str:
    name = config.lp3_name
    if (
        not isinstance(name, str)
        or not name.strip()
        or name.startswith("example-")
        or name in config.protected_lpar_names
    ):
        raise ValueError("Configure an existing owned non-protected statistics fixture")
    return name


def private_destination(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError("Use a canonical absolute private results path")
    observations = evidence.observations_path(str(path))
    if (
        path.exists()
        or observations.exists()
        or observations.is_symlink()
        or not path.parent.is_dir()
        or stat.S_IMODE(path.parent.stat().st_mode) != 0o700
        or any(
            (parent / ".git").exists() for parent in (path.parent, *path.parent.parents)
        )
    ):
        raise ValueError(
            "Use new results/observations in a mode0700 directory outside repositories"
        )
    return path


def _text(value) -> bool:
    try:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value.encode()) <= 4096
            and value.isprintable()
        )
    except UnicodeError:
        return False


def _qualified_context(context) -> None:
    if not isinstance(context, dict) or set(context) != _CONTEXT_KEYS:
        raise ValueError("Statistics fixture context has missing or unknown fields")
    if any(
        not _text(context[key])
        for key in ("system_uuid", "system_name", "uuid", "name", "kind", "state")
    ):
        raise ValueError("Statistics fixture identity is malformed")
    if not all(is_uuid(context[key]) for key in ("system_uuid", "uuid")):
        raise ValueError("Statistics fixture UUID is malformed")
    if (
        type(context["native_id"]) is not int
        or context["native_id"] < 1
        or context["kind"] not in {"LogicalPartition", "VirtualIOServer"}
    ):
        raise ValueError("Statistics fixture kind or native ID is malformed")
    if (
        context["shared"] is not True
        or context["ams"] is not True
        or context["state"] != "running"
    ):
        raise ValueError("Statistics fixture is not positively Running/shared/AMS")
    if context["partition_type"] is not None and not _text(context["partition_type"]):
        raise ValueError("Statistics fixture PartitionType is malformed")
    allocation = context["allocation"]
    if (
        not isinstance(allocation, dict)
        or set(allocation) != _ALLOCATION
        or any(
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or len(value) > 4096
            for value in allocation.values()
        )
    ):
        raise ValueError("Statistics fixture allocation is unavailable or malformed")


def _valid_statistics(value) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"raw", "entries", "missing", "usable", "error"}
        and value["usable"] is True
        and isinstance(value["raw"], str)
        and parse_statistics(value["raw"]) == value
    )


def _environment(context):
    return _identity(context), context["partition_type"], context["allocation"]


def _destination(config):
    return {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "schema_version": config.schema_version,
    }


def _result_ok(result, context) -> bool:
    if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
        return False
    expected = {
        "system_uuid": context["system_uuid"],
        "system_name": context["system_name"],
        "lpar_uuid": context["uuid"],
        "lpar_name": context["name"],
        "partition_id": context["native_id"],
        "partition_kind": context["kind"],
    }
    for key, value in expected.items():
        observed = result[key]
        if type(observed) is not type(value):
            return False
        if (
            (observed.lower() != value.lower())
            if key.endswith("uuid")
            else (observed != value)
        ):
            return False
    try:
        output_ok = (
            isinstance(result["native_output"], str)
            and len(result["native_output"].encode()) <= 65536
        )
    except UnicodeError:
        return False
    return (
        output_ok
        and result["request"] == LparMemoryStatisticsReset().model_dump()
        and result["native_completion"] == "completed"
        and result["submission_error"] is None
        and result["after_error"] is None
        and result["identity_preserved"] is True
        and result["workflow_completed"] is True
        and result["reset_verification"] == "unavailable"
        and _valid_statistics(result["before_statistics"])
        and _valid_statistics(result["after_statistics"])
    )


async def _capture(hmc, system_uuid, selector, config):
    context = await _context(hmc, system_uuid, selector)
    _admit(context, LparMemoryStatisticsReset())
    _qualified_context(context)
    if context["name"] in config.protected_lpar_names:
        raise ValueError("Statistics fixture resolves to a protected partition")
    statistics = await _statistics(hmc, context)
    if not _valid_statistics(statistics):
        raise ValueError("Statistics fixture has no usable independent observation")
    description = await get_lpar_description(
        hmc.config, context["system_name"], context["name"], max_output_bytes=65536
    )
    owner = authorize_lpar_ownership_description(
        hmc,
        context["system_name"],
        context["name"],
        description,
        operation="lpar-mutation",
    )
    if owner != (hmc.config.agent_id or "hmcpctl"):
        raise ValueError(
            "Statistics fixture requires a positive matching ownership stamp"
        )
    return {
        "context": context,
        "allocation": context["allocation"],
        "statistics": statistics,
    }


def _project_document(state, rows):
    """Measure the existing runner envelope; this projection is never persisted."""
    document = {
        "run": evidence.run_provenance(
            [SUBTASK],
            GROUP,
            evidence.repository_root(),
            env_var_value("HMC_SCHEMA_VERSION") or "(not set)",
            False,
        ),
        "config": asdict(state.config),
        "hmc": {
            "host": state.hmc_config.host,
            "port": state.hmc_config.port,
            "user": state.hmc_config.user,
            "verify_ssl": state.hmc_config.verify_ssl,
        },
        "artifacts": asdict(state.artifacts),
        "results": rows,
    }
    return json.loads(json.dumps(document, allow_nan=False))


def _record_phase(state, label, status, data, candidate):
    row = {
        "subtask": SUBTASK,
        "tool": label,
        "status": status,
        "result": "observed" if status == "PASS" else "failed",
        "timestamp": datetime.now(UTC).isoformat(timespec="microseconds"),
        "note": "",
        "data": data,
    }
    projection = _project_document(state, [*state.results, row])
    if (
        projection["run"]["tree_clean"] is not True
        or projection["run"]["tested_commit"] != candidate
    ):
        raise ValueError("Statistics source candidate changed before checkpoint")
    # Leave space for bounded failure/evidence rows if a later capture cannot fit.
    if (
        len(json.dumps(projection, allow_nan=False, indent=2).encode())
        > 1024 * 1024 - 4096
    ):
        raise ValueError(
            "Statistics record has insufficient space within its 1 MiB bound"
        )
    state.record(SUBTASK, label, status, data)
    state.checkpoint()


def _baseline_record(state, provenance, initial, selector):
    context = initial["context"]
    destination = _destination(state.hmc_config)
    if not all(_text(destination[key]) for key in ("host", "user")) or not isinstance(
        destination["schema_version"], str
    ):
        raise ValueError("Statistics arm destination identity is incomplete")
    baseline = {
        "schema_version": 1,
        "candidate_commit": provenance["tested_commit"],
        "system_uuid": context["system_uuid"],
        "system_name": context["system_name"],
        "uuid": context["uuid"],
        "name": context["name"],
        "nativeID": context["native_id"],
        "kind": context["kind"],
        **initial,
        "request": {},
        "destination": destination,
        "fixtureselector": selector,
    }
    return baseline


async def exercise_memory_statistics(client, state):
    if state.group != GROUP or state.hmc_config is None:
        state.skip(
            SUBTASK, GROUP, "requires the named opt-in group and configured credentials"
        )
        return
    complete = False
    try:
        selector = configured(state.config)
        if state.checkpoint is None:
            raise ValueError("Private durable checkpoint is required before effects")
        provenance = evidence.run_provenance(
            [SUBTASK],
            GROUP,
            evidence.repository_root(),
            state.hmc_config.schema_version,
            False,
        )
        if provenance["tree_clean"] is not True or not re.fullmatch(
            r"[0-9a-f]{40}", provenance["tested_commit"] or ""
        ):
            raise ValueError("Statistics arm requires a clean immutable candidate")
        async with HMCClient(state.hmc_config) as hmc:
            system_uuid = await resolve_system_uuid(hmc, state.config.system_name)
            initial = await _capture(hmc, system_uuid, selector, state.config)
            context = initial["context"]
            baseline = _baseline_record(state, provenance, initial, selector)
            _record_phase(
                state, PHASES[0], "PASS", baseline, baseline["candidate_commit"]
            )
            _record_phase(
                state,
                PHASES[1],
                "PASS",
                {
                    "baseline_identity": list(_identity(context)),
                    "request": {},
                    "operation": OPERATION,
                    "attempt": 1,
                },
                baseline["candidate_commit"],
            )
            status, result = await state.call(
                client,
                "hmc_reset_lpar_memory_statistics",
                lpar_name_or_uuid=context["uuid"],
                system_name_or_uuid=context["system_uuid"],
                reset={},
                ownership_override=False,
            )
            success = status == "PASS" and _result_ok(result, context)
            _record_phase(
                state,
                PHASES[2],
                "PASS" if success else "FAIL",
                {
                    "status": status,
                    "result": result if isinstance(result, dict) else None,
                },
                baseline["candidate_commit"],
            )
            final = await _capture(hmc, system_uuid, context["uuid"], state.config)
            preserved = _environment(final["context"]) == _environment(context)
            _record_phase(
                state,
                PHASES[3],
                "PASS" if preserved else "FAIL",
                final,
                baseline["candidate_commit"],
            )
            complete = success and preserved
            if complete:
                row = {
                    "subtask": SUBTASK,
                    "tool": TOOL,
                    "status": "PASS",
                    "result": "passed",
                    "timestamp": datetime.now(UTC).isoformat(timespec="microseconds"),
                    "note": "",
                    "data": {"reset_verification": "unavailable"},
                }
                validate_finished(
                    _project_document(state, [*state.results, row]), baseline
                )
    except (HMCError, ValueError, TypeError, PermissionError, OSError) as error:
        complete = False
        detail = (
            str(error)[:256]
            if isinstance(error, (ValueError, TypeError, PermissionError))
            else type(error).__name__
        )
        state.record(
            SUBTASK,
            GROUP,
            "FAIL",
            f"Statistics arm incomplete: {detail}; read-only recovery required",
        )
    state.record_verified(
        SUBTASK,
        "hmc_reset_lpar_memory_statistics",
        operation="lpar.reset_memory_statistics",
        scenario=SCENARIO,
        assertions=[
            Assertion("native-command-completed", complete),
            Assertion("usable-before", complete),
            Assertion("usable-independent-after", complete),
            Assertion("contained-shared-identity", complete),
            Assertion("allocation-observed-unchanged", complete),
            Assertion("reset-verification-unavailable", complete),
        ],
        cleanup="not-required",
        data={"reset_verification": "unavailable"},
    )


def _timestamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("Statistics record timestamp is malformed")


def _validate_envelope(document):
    if len(json.dumps(document, allow_nan=False, indent=2).encode()) > 1024 * 1024:
        raise ValueError("Statistics finite record exceeds 1 MiB")
    if not isinstance(document, dict) or set(document) != {
        "run",
        "config",
        "hmc",
        "artifacts",
        "results",
    }:
        raise ValueError("Statistics document fields differ")
    run = document["run"]
    if not isinstance(run, dict) or set(run) != {
        "tested_commit",
        "tree_clean",
        "group",
        "subtasks",
        "schema_version",
        "finished",
        "partial",
    }:
        raise ValueError("Statistics run provenance fields differ")
    if (
        run["partial"] is not False
        or run["tree_clean"] is not True
        or run["group"] != GROUP
        or run["subtasks"] != [SUBTASK]
    ):
        raise ValueError("Statistics requires a complete exact clean named run")
    if (
        not isinstance(run["schema_version"], str)
        or type(run["subtasks"]) is not list
        or len(run["subtasks"]) != 1
        or type(run["subtasks"][0]) is not int
    ):
        raise ValueError("Statistics run schema or subtask has an invalid type")
    _timestamp(run["finished"])
    return run


def _validate_baseline(document, baseline, run):
    if (
        not isinstance(baseline, dict)
        or set(baseline) != _BASELINE_KEYS
        or type(baseline["schema_version"]) is not int
        or baseline["schema_version"] != 1
    ):
        raise ValueError("Statistics baseline schema is missing, unknown or malformed")
    if (
        not isinstance(run["tested_commit"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", run["tested_commit"])
        or baseline["candidate_commit"] != run["tested_commit"]
    ):
        raise ValueError("Statistics candidate identity differs")
    context = baseline["context"]
    _qualified_context(context)
    if (
        type(baseline["nativeID"]) is not int
        or baseline["nativeID"] != context["native_id"]
    ):
        raise ValueError("Statistics baseline native ID differs")
    for key in ("system_uuid", "system_name", "uuid", "name", "kind"):
        value = baseline[key]
        matches = isinstance(value, str) and (
            value.lower() == context[key].lower()
            if key.endswith("uuid")
            else value == context[key]
        )
        if not matches:
            raise ValueError("Statistics baseline identity fields disagree")
    hmc = document["hmc"]
    if (
        not isinstance(hmc, dict)
        or set(hmc) != {"host", "port", "user", "verify_ssl"}
        or type(hmc["verify_ssl"]) is not bool
        or type(hmc["port"]) is not int
        or hmc["port"] < 1
        or not all(_text(hmc[key]) for key in ("host", "user"))
    ):
        raise ValueError("Statistics HMC identity is malformed")
    destination = baseline["destination"]
    if (
        not isinstance(destination, dict)
        or set(destination) != {"host", "port", "user", "schema_version"}
        or type(destination["port"]) is not int
        or destination["port"] < 1
        or not isinstance(destination["schema_version"], str)
        or not all(_text(destination[key]) for key in ("host", "user"))
    ):
        raise ValueError("Statistics baseline destination is malformed")
    if not isinstance(document["config"], dict) or set(document["config"]) != set(
        asdict(LiveTestConfig())
    ):
        raise ValueError("Statistics requires the complete current configuration shape")
    saved_config = decode_saved_config(document["config"])
    if (
        configured(saved_config) != baseline["fixtureselector"]
        or baseline["request"] != {}
        or baseline["allocation"] != context["allocation"]
        or not _valid_statistics(baseline["statistics"])
    ):
        raise ValueError("Statistics baseline configuration or observation differs")
    return context


def validate_finished(document, baseline):
    run = _validate_envelope(document)
    context = _validate_baseline(document, baseline, run)
    rows = document["results"]
    if not isinstance(rows, list) or len(rows) != 5:
        raise ValueError("Statistics finite sequence is incomplete or duplicated")
    for index, (row, name) in enumerate(zip(rows, (*PHASES, TOOL), strict=True)):
        if (
            not isinstance(row, dict)
            or set(row)
            != {"subtask", "tool", "status", "result", "timestamp", "note", "data"}
            or type(row["subtask"]) is not int
            or row["subtask"] != SUBTASK
            or row["tool"] != name
            or row["status"] != "PASS"
            or row["note"] != ""
            or row["result"] != ("passed" if index == 4 else "observed")
        ):
            raise ValueError("Statistics finite row shape or sequence differs")
        _timestamp(row["timestamp"])
    intent = rows[1]["data"]
    expected = {
        "baseline_identity": list(_identity(context)),
        "request": {},
        "operation": OPERATION,
        "attempt": 1,
    }
    if (
        rows[0]["data"] != baseline
        or not isinstance(intent, dict)
        or set(intent) != set(expected)
        or type(intent.get("attempt")) is not int
    ):
        raise ValueError("Statistics original baseline or one-attempt intent differs")
    identity = intent["baseline_identity"]
    if (
        not isinstance(identity, list)
        or len(identity) != len(expected["baseline_identity"])
        or any(
            type(value) is not type(wanted)
            for value, wanted in zip(
                identity, expected["baseline_identity"], strict=True
            )
        )
    ):
        raise ValueError("Statistics intent identity has malformed scalar types")
    identity = list(identity)
    for index in (0, 2):
        identity[index] = identity[index].lower()
    if {**intent, "baseline_identity": identity} != expected:
        raise ValueError("Statistics one-attempt intent differs")
    observed, final = rows[2]["data"], rows[3]["data"]
    if (
        not isinstance(observed, dict)
        or set(observed) != {"status", "result"}
        or observed["status"] != "PASS"
        or not _result_ok(observed["result"], context)
    ):
        raise ValueError("Statistics reset lacks completed independent result evidence")
    if not isinstance(final, dict) or set(final) != {
        "context",
        "allocation",
        "statistics",
    }:
        raise ValueError("Statistics final observation fields differ")
    _qualified_context(final["context"])
    if (
        _environment(final["context"]) != _environment(context)
        or final["allocation"] != baseline["allocation"]
        or not _valid_statistics(final["statistics"])
    ):
        raise ValueError("Statistics final environment is unconfirmed")
    if rows[4]["data"] != {"reset_verification": "unavailable"}:
        raise ValueError("Statistics evidence claims an unsupported result")
    return {"baseline": baseline, "result": observed["result"], "final": final}


async def recover_document(hmc_config, document):
    rows = document.get("results", []) if isinstance(document, dict) else []
    baseline = rows[0].get("data") if rows and isinstance(rows[0], dict) else None
    baseline = validate_finished(document, baseline)["baseline"]
    current = evidence.run_provenance(
        [SUBTASK], GROUP, evidence.repository_root(), hmc_config.schema_version, False
    )
    if (
        current["tree_clean"] is not True
        or current["tested_commit"] != baseline["candidate_commit"]
    ):
        raise ValueError("Statistics recovery requires the same clean candidate")
    expected_hmc = {
        "host": hmc_config.host,
        "port": hmc_config.port,
        "user": hmc_config.user,
        "verify_ssl": hmc_config.verify_ssl,
    }
    if document["hmc"] != expected_hmc or baseline["destination"] != _destination(
        hmc_config
    ):
        raise ValueError("Statistics recovery destination or schema differs")
    config = decode_saved_config(document["config"])
    async with HMCClient(hmc_config) as hmc:
        system_uuid = await resolve_system_uuid(hmc, config.system_name)
        observed = await _capture(hmc, system_uuid, baseline["uuid"], config)
    if _environment(observed["context"]) != _environment(baseline["context"]):
        raise ValueError("Statistics recovery environment differs; no write attempted")
