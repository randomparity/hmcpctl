"""Independent admission and baseline witnesses for creation-memory journals."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass
from datetime import datetime

from hmcpctl.config import HMCConfig
from live_test import artifacts as live_artifacts
from live_test import config as live_config
from live_test import runner
from live_test.arms import creation_memory, creation_memory_layout, lpar_config

from .common import Finding, StateUnreadable


@dataclass(frozen=True)
class CreationMemoryInputs:
    system_name: str
    journal: dict


def creation_memory_skip_reason(document: dict, subtasks: list[int]) -> str | None:
    """Recognize only the runner's complete pre-effect configuration rejection."""
    if subtasks != [creation_memory.SUBTASK]:
        return None
    config = live_config.decode_saved_config(document.get("config"))
    artifacts = live_artifacts.decode_artifacts(document.get("artifacts"))
    run = document["run"]
    if (
        set(document) != {"run", "config", "hmc", "artifacts", "results"}
        or set(run)
        != {
            "group",
            "subtasks",
            "partial",
            "tested_commit",
            "tree_clean",
            "schema_version",
            "finished",
        }
        or run.get("group") != creation_memory.GROUP
        or run.get("partial") is not False
        or not isinstance(run.get("tested_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", run["tested_commit"]) is None
        or type(run.get("tree_clean")) is not bool
        or not isinstance(run.get("schema_version"), str)
        or document["artifacts"].get("creation_memory") != {}
        or artifacts != live_artifacts.LiveTestArtifacts()
        or document["artifacts"] != asdict(artifacts)
    ):
        return None
    hmc = document.get("hmc")
    expected_hmc = runner._hmc_identity(HMCConfig.from_mapping({}))
    if (
        not isinstance(hmc, dict)
        or set(hmc) != set(expected_hmc)
        or any(type(hmc[key]) is not type(value) for key, value in expected_hmc.items())
    ):
        return None
    rows = document.get("results")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if (
        set(row) != {"subtask", "tool", "status", "result", "timestamp", "note", "data"}
        or type(row["subtask"]) is not int
        or row["subtask"] != creation_memory.SUBTASK
        or row["tool"] != creation_memory.GROUP
        or row["status"] != "SKIP"
        or row["result"] != "skipped"
        # RunState.record serializes scalar None as this string, not JSON null.
        or row["data"] != "None"
    ):
        return None
    for value in (run.get("finished"), row["timestamp"]):
        if not isinstance(value, str):
            return None
        try:
            if datetime.fromisoformat(value).tzinfo is None:
                return None
        except ValueError:
            return None
    try:
        creation_memory.configured(config)
    except (ValueError, TypeError) as error:
        reason = str(error)
        return reason if reason and row["note"] == reason else None
    return None


def creation_memory_inputs_from_document(
    document: dict, subtasks: list[int]
) -> CreationMemoryInputs | None:
    if creation_memory.SUBTASK not in subtasks:
        return None
    config = live_config.decode_saved_config(document.get("config"))
    artifacts = live_artifacts.decode_artifacts(document.get("artifacts"))
    journal = artifacts.creation_memory
    if (
        not journal
        or document.get("hmc") != journal["hmc_identity"]
        or document.get("run", {}).get("tested_commit") != journal["candidate_commit"]
    ):
        raise ValueError("ST65 recovery lacks candidate/HMC-bound journal")
    kind, memory = creation_memory.configured(config)
    if (
        document["run"].get("group") != creation_memory.GROUP
        or document["run"].get("subtasks") != [creation_memory.SUBTASK]
        or kind != journal["kind"]
        or dict(memory.pairs()) != journal["requested"]
    ):
        raise ValueError("ST65 run/config/request binding differs")
    return CreationMemoryInputs(config.system_name, journal)


@dataclass(frozen=True)
class CreationMemoryLayoutInputs:
    system_name: str
    journal: dict


def creation_memory_layout_skip_reason(document, subtasks) -> str | None:
    """Recognize only the runner's complete pre-effect configuration rejection."""
    if subtasks != [creation_memory_layout.SUBTASK]:
        return None
    config = live_config.decode_saved_config(document.get("config"))
    artifacts = live_artifacts.decode_artifacts(document.get("artifacts"))
    run = document["run"]
    if (
        set(document) != {"run", "config", "hmc", "artifacts", "results"}
        or set(run)
        != {
            "group",
            "subtasks",
            "partial",
            "tested_commit",
            "tree_clean",
            "schema_version",
            "finished",
        }
        or run.get("group") != creation_memory_layout.GROUP
        or run.get("partial") is not False
        or not isinstance(run.get("tested_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", run["tested_commit"]) is None
        or type(run.get("tree_clean")) is not bool
        or not isinstance(run.get("schema_version"), str)
        or document["artifacts"].get("creation_memory_layout") != {}
        or artifacts != live_artifacts.LiveTestArtifacts()
        or document["artifacts"] != asdict(artifacts)
    ):
        return None
    hmc = document.get("hmc")
    expected_hmc = runner._hmc_identity(HMCConfig.from_mapping({}))
    if (
        not isinstance(hmc, dict)
        or set(hmc) != set(expected_hmc)
        or any(type(hmc[key]) is not type(value) for key, value in expected_hmc.items())
    ):
        return None
    rows = document.get("results")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if (
        set(row) != {"subtask", "tool", "status", "result", "timestamp", "note", "data"}
        or type(row["subtask"]) is not int
        or row["subtask"] != creation_memory_layout.SUBTASK
        or row["tool"] != creation_memory_layout.GROUP
        or row["status"] != "SKIP"
        or row["result"] != "skipped"
        # RunState.record serializes scalar None as this string, not JSON null.
        or row["data"] != "None"
    ):
        return None
    for value in (run.get("finished"), row["timestamp"]):
        if not isinstance(value, str):
            return None
        try:
            if datetime.fromisoformat(value).tzinfo is None:
                return None
        except ValueError:
            return None
    try:
        creation_memory_layout.configured(config)
    except (ValueError, TypeError) as error:
        reason = str(error)
        return reason if reason and row["note"] == reason else None
    return None


def creation_memory_layout_inputs_from_document(document, subtasks):
    if creation_memory_layout.SUBTASK not in subtasks:
        return None
    config = live_config.decode_saved_config(document.get("config"))
    artifacts = live_artifacts.decode_artifacts(document.get("artifacts"))
    journal = artifacts.creation_memory_layout
    if (
        not journal
        or document.get("hmc") != journal["hmc_identity"]
        or document.get("run", {}).get("tested_commit") != journal["candidate_commit"]
    ):
        raise ValueError("ST58 recovery lacks candidate/HMC-bound journal")
    return CreationMemoryLayoutInputs(config.system_name, journal)


async def check_creation_memory(call, inputs: CreationMemoryInputs) -> list[Finding]:
    """Read-only current reconciliation; retained grants never authorize cleanup."""
    j = inputs.journal
    if (
        runner._hmc_identity(HMCConfig()) != j["hmc_identity"]
        or creation_memory._candidate() != j["candidate_commit"]
    ):
        raise StateUnreadable("ST65 current candidate/HMC differs from journal")
    status, entry = await call("hmc_get_system", system_name_or_uuid=inputs.system_name)
    name = (
        (entry.get("Resource") or {}).get("SystemName")
        if isinstance(entry, dict)
        else None
    )
    if (
        status != "PASS"
        or creation_memory._canonical(lpar_config._uuid_of(entry)) != j["system_uuid"]
        or not isinstance(name, str)
        or not name
    ):
        raise StateUnreadable("ST65 selected-system identity unreadable")
    system = shlex.quote(name)
    current = {}
    for key, command in (
        ("partitions", f"lssyscfg -r lpar -m {system} -F name"),
        (
            "available_processing_units",
            f"lshwres -r proc -m {system} --level sys -F curr_avail_sys_proc_units",
        ),
        (
            "available_memory_mib",
            f"lshwres -r mem -m {system} --level sys -F curr_avail_sys_mem,mem_region_size",
        ),
    ):
        status, data = await call("hmc_run_command", cmd=command)
        if (
            status != "PASS"
            or not isinstance(data, str)
            or len(data.encode()) > creation_memory.LIMIT
        ):
            raise StateUnreadable("ST65 native baseline unreadable")
        if key == "partitions":
            names = data.splitlines()
            if len(set(names)) != len(names) or any(not n for n in names):
                raise StateUnreadable("ST65 duplicate/empty partition names")
            current[key] = sorted(names)
        else:
            text = data.strip().split(",")[0]
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text) is None:
                raise StateUnreadable("ST65 capacity baseline unreadable")
            current[key] = text
    inventories = {}
    for kind, tool in (
        ("LogicalPartition", "hmc_list_lpars"),
        ("VirtualIOServer", "hmc_list_vios"),
    ):
        status, data = await call(tool, system_name_or_uuid=j["system_uuid"])
        raw = data.get("entries") if isinstance(data, dict) else data
        if (
            status != "PASS"
            or not isinstance(raw, list)
            or len(json.dumps(data).encode()) > creation_memory.LIMIT
            or (isinstance(data, dict) and data.get("unreadable_systems"))
        ):
            raise StateUnreadable("ST65 complete partition inventory unreadable")
        rows = []
        for row in raw:
            uid = creation_memory._canonical(lpar_config._uuid_of(row))
            part_name = (
                (row.get("Resource") or {}).get("PartitionName")
                if isinstance(row, dict)
                else None
            )
            if not uid or not isinstance(part_name, str) or not part_name:
                raise StateUnreadable("ST65 partition identity unreadable")
            rows.append({"uuid": uid, "name": part_name})
        if len({r["uuid"] for r in rows}) != len(rows) or len(
            {r["name"] for r in rows}
        ) != len(rows):
            raise StateUnreadable("ST65 partition inventory ambiguous")
        inventories[kind] = sorted(rows, key=lambda r: r["uuid"])
    all_rows = inventories["LogicalPartition"] + inventories["VirtualIOServer"]
    if sorted(r["name"] for r in all_rows) != current["partitions"]:
        raise StateUnreadable("ST65 independent inventories disagree")
    findings = []
    residual = any(r["uuid"] == j["uuid"] or r["name"] == j["name"] for r in all_rows)
    if (
        residual
        or current != j["baseline"]["pools"]
        or inventories["VirtualIOServer"] != j["baseline"]["vios"]
    ):
        findings.append(
            Finding(
                "ST65 baseline/residual",
                "partition/pool/VIOS baseline differs or exact fixture remains",
                "root must inspect exact UUID and separately authorize any later cleanup; no replay",
            )
        )
    if (
        j["phase"] != "complete"
        or j["deletion"] != "confirmed-absent"
        or j["outcome"] != "created-verified"
        or not j["preabsence"]
        or not j["create_returned"]
        or not j["uuid"]
        or j["case"] == "controller-shared-lpar"
    ):
        findings.append(
            Finding(
                "ST65 interrupted/incomplete intent",
                f"phase {j['phase']}; saved grants are not current authority",
                "retain journal and independently reconcile without create/stamp/delete/resume",
            )
        )
    return findings


async def check_creation_memory_layout(
    call, inputs: CreationMemoryLayoutInputs
) -> list[Finding]:
    """Reconcile every phase read-only; intent and saved grants never authorize delete."""
    journal = inputs.journal
    if runner._hmc_identity(HMCConfig()) != journal["hmc_identity"]:
        raise StateUnreadable("ST58 current HMC identity differs from its journal")
    status, entry = await call(
        "hmc_get_system", system_name_or_uuid=journal["system_uuid"]
    )
    name = (
        (entry.get("Resource") or {}).get("SystemName")
        if isinstance(entry, dict)
        else None
    )
    if (
        status != "PASS"
        or creation_memory_layout._canonical_uuid(lpar_config._uuid_of(entry))
        != journal["system_uuid"]
        or not isinstance(name, str)
        or not name
    ):
        raise StateUnreadable("ST58 selected-system UUID/name unreadable")
    system = shlex.quote(name)
    current = {}
    for key, command in (
        ("partitions", f"lssyscfg -r lpar -m {system} -F name"),
        (
            "available_processing_units",
            f"lshwres -r proc -m {system} --level sys -F curr_avail_sys_proc_units",
        ),
        (
            "available_memory_mib",
            f"lshwres -r mem -m {system} --level sys -F curr_avail_sys_mem,mem_region_size",
        ),
    ):
        status, data = await call("hmc_run_command", cmd=command)
        if status != "PASS" or not isinstance(data, str):
            raise StateUnreadable("ST58 pool/partition baseline unreadable")
        current[key] = (
            sorted(data.splitlines())
            if key == "partitions"
            else data.strip().split(",")[0]
        )
    status, data = await call(
        "hmc_list_vios", system_name_or_uuid=journal["system_uuid"]
    )
    raw = data.get("entries") if isinstance(data, dict) else data
    if status != "PASS" or not isinstance(raw, list):
        raise StateUnreadable("ST58 VIOS feed unreadable")
    rows = []
    for row in raw:
        identity = creation_memory_layout._canonical_uuid(lpar_config._uuid_of(row))
        name = (
            (row.get("Resource") or {}).get("PartitionName")
            if isinstance(row, dict)
            else None
        )
        if not identity or not isinstance(name, str) or not name:
            raise StateUnreadable("ST58 VIOS identity unreadable")
        rows.append({"uuid": identity, "name": name})
    rows.sort(key=lambda row: row["uuid"])
    findings = []
    if current != journal["baseline"]["pools"] or rows != journal["baseline"]["vios"]:
        findings.append(
            Finding(
                "ST58 baseline/orphan",
                "partition/pool/VIOS baseline differs; diagnostic identities are not adopted",
                "root must independently reconcile the exact UUID and authorize any later cleanup",
            )
        )
    if journal["phase"] != "complete" or any(
        v != "confirmed-absent" for v in journal["deletion"].values()
    ):
        findings.append(
            Finding(
                "ST58 interrupted intent",
                f"phase {journal['phase']}; saved delete permission is not current authority",
                "retain the journal; root must reconcile each pending create/delete UUID without replay",
            )
        )
    return findings
