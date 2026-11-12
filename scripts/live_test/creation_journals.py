"""Validate saved creation-memory intent and recovery evidence."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter

from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.resource_identity import is_uuid

from .config import LiveTestConfig

_CREATION_MEMORY_LAYOUT_PHASES = frozenset(
    {
        "preflight",
        "lpar-create-pending",
        "lpar-create-returned",
        "lpar-delete-pending",
        "lpar-deleted",
        "vios-create-pending",
        "vios-create-returned",
        "vios-delete-pending",
        "vios-deleted",
        "complete",
        "stopped-uncertain",
    }
)


def _validate_creation_memory_layout(value: Any) -> None:
    """A closed intent record; persisted permission never supplies current authority."""
    if not isinstance(value, dict):
        raise TypeError("creation memory layout journal must be an object")
    if not value:
        return
    keys = {
        "invocation_id",
        "candidate_commit",
        "system_uuid",
        "hmc_identity",
        "phase",
        "lpar_name",
        "lpar_token",
        "lpar_uuid",
        "vios_name",
        "vios_uuid",
        "preabsence",
        "create_returned",
        "fresh_vios_delete_authorized",
        "requested",
        "baseline",
        "outcome",
        "deletion",
    }
    if set(value) != keys or value["phase"] not in _CREATION_MEMORY_LAYOUT_PHASES:
        raise ValueError("creation memory layout journal fields/phase are invalid")
    for key, pattern in (
        ("invocation_id", r"[0-9a-f]{32}"),
        ("candidate_commit", r"[0-9a-f]{40}"),
        ("lpar_name", r"hmcpctl-live-lpar-[0-9a-f]{8}"),
        ("lpar_token", r"lparcfg-[0-9a-f]{8}"),
        ("vios_name", r"hmcpctl-live-c06-vios-[0-9a-f]{8}"),
    ):
        if not isinstance(value[key], str) or re.fullmatch(pattern, value[key]) is None:
            raise ValueError(f"creation memory layout {key} is invalid")
    if value["lpar_name"].removeprefix("hmcpctl-live-lpar-") != value[
        "lpar_token"
    ].removeprefix("lparcfg-"):
        raise ValueError("creation memory layout name/token disagree")
    for key in ("system_uuid", "lpar_uuid", "vios_uuid"):
        identity = value[key]
        if identity is None and key != "system_uuid":
            continue
        if (
            not isinstance(identity, str)
            or not is_uuid(identity)
            or identity != identity.lower()
        ):
            raise ValueError(f"creation memory layout {key} must be a canonical UUID")
    hmc = value["hmc_identity"]
    if (
        not isinstance(hmc, dict)
        or set(hmc) != {"host", "port", "user", "verify_ssl"}
        or any(
            type(hmc[k]) is not t
            for k, t in (
                ("host", str),
                ("port", int),
                ("user", str),
                ("verify_ssl", bool),
            )
        )
    ):
        raise TypeError("creation memory layout HMC identity is invalid")
    for key in ("preabsence", "create_returned"):
        if (
            not isinstance(value[key], dict)
            or set(value[key]) != {"lpar", "vios"}
            or any(type(v) is not bool for v in value[key].values())
        ):
            raise TypeError(f"creation memory layout {key} must contain two booleans")
    if type(value["fresh_vios_delete_authorized"]) is not bool:
        raise TypeError("creation memory layout saved authority must be boolean")
    for key, choices in (
        (
            "outcome",
            {
                "not-attempted",
                "rejected",
                "created-verified",
                "partial",
                "unknown",
                "unavailable",
                "mismatch",
            },
        ),
        (
            "deletion",
            {"not-attempted", "pending", "confirmed-absent", "unknown", "refused"},
        ),
    ):
        if (
            not isinstance(value[key], dict)
            or set(value[key]) != {"lpar", "vios"}
            or any(
                not isinstance(v, str) or v not in choices for v in value[key].values()
            )
        ):
            raise ValueError(f"creation memory layout {key} is invalid")
    requested = value["requested"]
    if not isinstance(requested, dict) or set(requested) != {"lpar", "vios"}:
        raise ValueError("creation memory layout requests are invalid")
    for kind, names in (
        (
            "lpar",
            {
                "min_num_huge_pages",
                "desired_num_huge_pages",
                "max_num_huge_pages",
                "hpt_ratio",
                "ppt_ratio",
                "kvm_reserved_mem_percent",
            },
        ),
        ("vios", {"hpt_ratio", "ppt_ratio"}),
    ):
        if not isinstance(requested[kind], dict) or set(requested[kind]) != names:
            raise ValueError("creation memory layout request fields are invalid")
        TypeAdapter(LparMemoryLayout).validate_python(
            requested[kind]
        ).validate_creation("AIX/Linux" if kind == "lpar" else "Virtual IO Server")
    baseline = value["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != {"pools", "vios"}:
        raise ValueError("creation memory layout baseline is invalid")
    pools = baseline["pools"]
    if (
        not isinstance(pools, dict)
        or set(pools)
        != {"partitions", "available_processing_units", "available_memory_mib"}
        or (
            not isinstance(pools["partitions"], list)
            or any(not isinstance(n, str) for n in pools["partitions"])
            or any(
                not isinstance(pools[k], str)
                for k in ("available_processing_units", "available_memory_mib")
            )
        )
    ):
        raise TypeError("creation memory layout pool baseline is invalid")
    rows = baseline["vios"]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or set(row) != {"uuid", "name"}
        or not isinstance(row["uuid"], str)
        or not is_uuid(row["uuid"])
        or row["uuid"] != row["uuid"].lower()
        or not isinstance(row["name"], str)
        or not row["name"]
        for row in rows
    ):
        raise ValueError("creation memory layout VIOS baseline is invalid")
    if (
        rows != sorted(rows, key=lambda row: row["uuid"])
        or len({r["uuid"] for r in rows}) != len(rows)
        or len({r["name"] for r in rows}) != len(rows)
    ):
        raise ValueError("creation memory layout VIOS baseline is ambiguous")


def _validate_creation_memory(value: Any) -> None:
    """Closed ST65 intent; saved grants are evidence and never cleanup authority."""
    if not isinstance(value, dict):
        raise TypeError("creation memory journal must be an object")
    if len(json.dumps(value).encode()) > 5 * 1024 * 1024:
        raise ValueError("creation memory journal exceeds 5 MiB")
    if not value:
        return
    keys = {
        "invocation_id",
        "candidate_commit",
        "system_uuid",
        "hmc_identity",
        "case",
        "kind",
        "name",
        "token",
        "uuid",
        "phase",
        "preabsence",
        "create_returned",
        "fresh_delete_authorized",
        "requested",
        "baseline",
        "outcome",
        "deletion",
    }
    if set(value) != keys:
        raise ValueError("creation memory journal fields are invalid")
    for key, pattern in (
        ("invocation_id", r"[0-9a-f]{32}"),
        ("candidate_commit", r"[0-9a-f]{40}"),
    ):
        if not isinstance(value[key], str) or re.fullmatch(pattern, value[key]) is None:
            raise ValueError(f"creation memory {key} is invalid")
    for key in ("system_uuid", "uuid"):
        item = value[key]
        if item is None and key == "uuid":
            continue
        if not isinstance(item, str) or not is_uuid(item) or item != item.lower():
            raise ValueError("creation memory UUID is not canonical")
    for key in ("preabsence", "create_returned", "fresh_delete_authorized"):
        if type(value[key]) is not bool:
            raise TypeError("creation memory flags must be boolean")
    hmc = value["hmc_identity"]
    if (
        not isinstance(hmc, dict)
        or set(hmc) != {"host", "port", "user", "verify_ssl"}
        or any(
            type(hmc[k]) is not t
            for k, t in (
                ("host", str),
                ("port", int),
                ("user", str),
                ("verify_ssl", bool),
            )
        )
        or not hmc["host"]
        or not hmc["user"]
        or not 1 <= hmc["port"] <= 65535
    ):
        raise ValueError("creation memory HMC identity is invalid")
    for key, choices in (
        (
            "phase",
            {
                "prepared",
                "create-pending",
                "create-returned",
                "delete-pending",
                "deleted",
                "complete",
                "stopped-uncertain",
            },
        ),
        (
            "outcome",
            {
                "not-attempted",
                "rejected",
                "partial",
                "unknown",
                "created-verified",
                "unavailable",
                "mismatch",
            },
        ),
        (
            "deletion",
            {"not-attempted", "pending", "confirmed-absent", "unknown", "refused"},
        ),
    ):
        if not isinstance(value[key], str) or value[key] not in choices:
            raise ValueError(f"creation memory {key} is invalid")
    request = value["requested"]
    if not isinstance(request, dict):
        raise TypeError("creation memory requested must be an object")
    # Reconstruct only the known saved decimal. Public numeric strings remain forbidden.
    from .arms.creation_memory import configured

    primary = request.get("primary_paging_vios_name", "")
    secondary = request.get("secondary_paging_vios_name", "")
    kind, model = configured(
        LiveTestConfig(
            creation_memory_case=value["case"],
            creation_memory_primary_vios_name=primary,
            creation_memory_secondary_vios_name=secondary,
            creation_memory_fresh_delete_authorized=value["fresh_delete_authorized"],
        )
    )
    normalized = dict(request)
    if "mem_expansion" in normalized:
        if normalized["mem_expansion"] not in ("0", "1.1"):
            raise ValueError("creation memory saved decimal is invalid")
        normalized["mem_expansion"] = Decimal(normalized["mem_expansion"])
    from hmcpctl.documents.lpar_creation_memory import LparCreationMemory

    reconstructed = LparCreationMemory(**normalized)
    if (
        value["kind"] != kind
        or reconstructed != model
        or request != dict(model.pairs())
    ):
        raise ValueError("creation memory case/request/kind disagree")
    prefix = (
        "hmcpctl-live-lpar-" if kind == "LogicalPartition" else "hmcpctl-live-c07-vios-"
    )
    if (
        not isinstance(value["name"], str)
        or re.fullmatch(prefix + r"[0-9a-f]{8}", value["name"]) is None
    ):
        raise ValueError("creation memory name is invalid")
    token = "lparcfg-" + value["name"][-8:] if kind == "LogicalPartition" else None
    if value["token"] != token:
        raise ValueError("creation memory name/token disagree")
    baseline = value["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != {"pools", "vios"}:
        raise ValueError("creation memory baseline is invalid")
    pools = baseline["pools"]
    if (
        not isinstance(pools, dict)
        or set(pools)
        != {"partitions", "available_processing_units", "available_memory_mib"}
        or not isinstance(pools["partitions"], list)
        or any(not isinstance(n, str) or not n for n in pools["partitions"])
        or pools["partitions"] != sorted(set(pools["partitions"]))
    ):
        raise ValueError("creation memory partition baseline is invalid")
    for key in ("available_processing_units", "available_memory_mib"):
        text = pools[key]
        if (
            not isinstance(text, str)
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text) is None
        ):
            raise ValueError("creation memory capacity baseline is invalid")
    rows = baseline["vios"]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or set(row) != {"uuid", "name"}
        or not isinstance(row["uuid"], str)
        or not is_uuid(row["uuid"])
        or row["uuid"] != row["uuid"].lower()
        or not isinstance(row["name"], str)
        or not row["name"]
        for row in rows
    ):
        raise ValueError("creation memory VIOS baseline is invalid")
    if (
        rows != sorted(rows, key=lambda r: r["uuid"])
        or len({r["uuid"] for r in rows}) != len(rows)
        or len({r["name"] for r in rows}) != len(rows)
    ):
        raise ValueError("creation memory VIOS baseline is ambiguous")
