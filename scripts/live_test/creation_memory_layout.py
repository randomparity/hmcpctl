"""ST58: two fresh inactive fixtures with an independent literal profile oracle."""

from __future__ import annotations

import re
import shlex
import uuid
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.linux_roles import read_role_capabilities
from hmcpctl.resource_identity import (
    is_uuid,
    resolve_lpar_uuid,
    resolve_system_name,
    resolve_system_uuid,
    resolve_vios_uuid,
)
from hmcpctl.ssh.commands import build_filter, parse_hmc_result_rows

from . import evidence
from .config import _validate_creation_memory_layout
from .lpar_config import (
    NAME_PREFIX,
    RESOURCES,
    TOKEN_PREFIX,
    _compare,
    _pools_data,
    _read_pools,
    _Run,
    _teardown,
    _token_of,
    _uuid_of,
    scratch_partitions,
)
from .observation import Assertion, plain_data

if TYPE_CHECKING:
    from fastmcp import Client

    from .config import LiveTestConfig
    from .runner import RunState

SUBTASK = 58
GROUP = "creation-memory-layout"
LPAR_SCENARIO = "st58-lpar-memory-layout"
VIOS_SCENARIO = "st58-vios-page-table-ratios"
VIOS_PREFIX = "hmcpctl-live-c06-vios-"


def configured(config: LiveTestConfig) -> LparMemoryLayout:
    if config.creation_memory_layout_fresh_vios_delete_authorized is not True:
        raise ValueError("separate conditional fresh-VIOS deletion permission required")
    if (
        not config.creation_memory_layout_hpt_ratio
        or not config.creation_memory_layout_ppt_ratio
    ):
        raise ValueError("both selected-system advertised ratios must be explicit")
    return LparMemoryLayout(
        0,
        0,
        0,
        config.creation_memory_layout_hpt_ratio,
        config.creation_memory_layout_ppt_ratio,
        Decimal(0),
    )


def _candidate() -> str:
    root = evidence.repository_root()
    if root is None:
        raise ValueError("candidate repository unavailable")
    result = evidence._git(root, "rev-parse", "HEAD")
    if (
        result.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40}", result.stdout.strip()) is None
    ):
        raise ValueError("candidate commit unavailable")
    return result.stdout.strip()


def _checkpoint(state: RunState, phase: str) -> None:
    journal = state.artifacts.creation_memory_layout
    configured(state.config)
    _validate_creation_memory_layout(journal)
    from .runner import _hmc_identity

    if (
        state.checkpoint is None
        or state.hmc_config is None
        or journal["invocation_id"] != state.creation_memory_layout_invocation
        or journal["candidate_commit"] != _candidate()
        or journal["hmc_identity"] != _hmc_identity(state.hmc_config)
    ):
        raise ValueError("fresh invocation/checkpoint/candidate/HMC identity required")
    journal["phase"] = phase
    state.checkpoint()


async def _vios_baseline(hmc, system_uuid: str) -> list[dict]:
    rows = []
    for entry in await hmc.list_vios(system_uuid):
        identity, name = (
            _uuid_of(entry),
            (entry.get("Resource") or {}).get("PartitionName"),
        )
        if (
            not identity
            or not is_uuid(identity)
            or identity != identity.lower()
            or not isinstance(name, str)
            or not name
        ):
            raise ValueError("VIOS feed identity unavailable")
        rows.append({"uuid": identity, "name": name})
    if len({row["uuid"] for row in rows}) != len(rows) or len(
        {row["name"] for row in rows}
    ) != len(rows):
        raise ValueError("VIOS feed identity ambiguous")
    return sorted(rows, key=lambda row: row["uuid"])


async def _identity(
    hmc, system_uuid: str, identity: str, name: str, *, vios: bool
) -> None:
    resolver = resolve_vios_uuid if vios else resolve_lpar_uuid
    await resolver(hmc, identity, system_name_or_uuid=system_uuid)
    entry = (
        await hmc.get_vios(identity)
        if vios
        else await hmc.get_logical_partition(identity)
    )
    resource = (entry or {}).get("Resource") or {}
    if (
        _uuid_of(entry) != identity
        or resource.get("PartitionName") != name
        or resource.get("PartitionType")
        != ("Virtual IO Server" if vios else "AIX/Linux")
        or resource.get("PartitionState") != "not activated"
    ):
        raise ValueError("fresh contained inactive UUID/name/type identity unconfirmed")


async def _profile(
    client: Client, state: RunState, system: str, name: str, *, vios: bool
) -> dict:
    # This literal projection and expectations must remain independent of the product getter.
    columns = (
        "lpar_name",
        "name",
        "min_num_huge_pages",
        "desired_num_huge_pages",
        "max_num_huge_pages",
        "hpt_ratio",
        "ppt_ratio",
        "kvm_reserved_mem_percent",
    )
    filters = build_filter([("lpar_names", name), ("profile_names", "default_profile")])
    command = (
        f"lssyscfg -r prof -m {shlex.quote(system)} --filter {shlex.quote(filters)} "
        "-F lpar_name,name,min_num_huge_pages,desired_num_huge_pages,max_num_huge_pages,hpt_ratio,ppt_ratio,kvm_reserved_mem_percent --header"
    )
    status, data = await state.call(client, "hmc_run_command", cmd=command)
    if status != "PASS" or not isinstance(data, str):
        return {"status": "unavailable", "observed": {}}
    try:
        rows = parse_hmc_result_rows(data, columns, "ST58 independent profile oracle")
        if (
            len(rows) != 1
            or rows[0]["lpar_name"] != name
            or rows[0]["name"] != "default_profile"
        ):
            raise ValueError("independent profile identity/row count differs")
    except ValueError:
        return {"status": "unavailable", "observed": {}}
    row = rows[0]
    expected = {
        "hpt_ratio": state.config.creation_memory_layout_hpt_ratio,
        "ppt_ratio": state.config.creation_memory_layout_ppt_ratio,
    }
    if not vios:
        expected.update(
            {
                "min_num_huge_pages": 0,
                "desired_num_huge_pages": 0,
                "max_num_huge_pages": 0,
                "kvm_reserved_mem_percent": Decimal(0),
            }
        )
    missing, mismatch = False, False
    observed = {}
    for key, value in expected.items():
        text = row[key]
        if text.lower() in {"", "null", "none", "unknown"}:
            missing = True
            continue
        try:
            if key in {
                "min_num_huge_pages",
                "desired_num_huge_pages",
                "max_num_huge_pages",
            }:
                if re.fullmatch(r"[0-9]+", text) is None:
                    raise ValueError("non-ASCII count")
                actual = int(text)
            elif key == "kvm_reserved_mem_percent":
                actual = Decimal(text)
                if not actual.is_finite():
                    raise ValueError("nonfinite reservation")
            else:
                actual = text
            observed[key] = str(actual) if isinstance(actual, Decimal) else actual
            mismatch |= actual != value
        except (ValueError, InvalidOperation):
            missing = True
    return {
        "status": "mismatch" if mismatch else "unavailable" if missing else "verified",
        "observed": observed,
    }


async def _read_lpar(
    client: Client, state: RunState, run: _Run, system_uuid: str
) -> dict:
    assert state.hmc_config is not None and run.uuid is not None
    if await _token_of(client, state, run, run.uuid) != run.token:
        raise ValueError("current LPAR ownership token differs")
    async with HMCClient(state.hmc_config) as hmc:
        await _identity(hmc, system_uuid, run.uuid, run.name, vios=False)
        mode = await read_role_capabilities(
            hmc, system_uuid, need_kvm=False, need_management=False
        )
        if mode.controller_mode:
            return {"status": "unavailable", "observed": {}}
        observed = await _profile(client, state, run.system, run.name, vios=False)
        await _identity(hmc, system_uuid, run.uuid, run.name, vios=False)
        mode = await read_role_capabilities(
            hmc, system_uuid, need_kvm=False, need_management=False
        )
        if mode.controller_mode:
            return {"status": "unavailable", "observed": {}}
    if await _token_of(client, state, run, run.uuid) != run.token:
        raise ValueError("LPAR ownership changed during independent read")
    return observed


async def _read_vios(
    client: Client,
    state: RunState,
    system_uuid: str,
    returned_uuid: str,
    fresh_name: str,
) -> dict:
    assert state.hmc_config is not None
    async with HMCClient(state.hmc_config) as hmc:
        await _identity(hmc, system_uuid, returned_uuid, fresh_name, vios=True)
        mode = await read_role_capabilities(
            hmc, system_uuid, need_kvm=False, need_management=False
        )
        if mode.controller_mode:
            return {"status": "unavailable", "observed": {}}
        system = await resolve_system_name(hmc, system_uuid)
        observed = await _profile(client, state, system, fresh_name, vios=True)
        await _identity(hmc, system_uuid, returned_uuid, fresh_name, vios=True)
        mode = await read_role_capabilities(
            hmc, system_uuid, need_kvm=False, need_management=False
        )
        return (
            {"status": "unavailable", "observed": {}}
            if mode.controller_mode
            else observed
        )


async def _delete_fresh_vios(
    client: Client,
    state: RunState,
    system_uuid: str,
    returned_uuid: str,
    fresh_name: str,
) -> bool:
    journal = state.artifacts.creation_memory_layout
    _checkpoint(state, journal["phase"])
    if (
        state.config.creation_memory_layout_fresh_vios_delete_authorized is not True
        or not journal["preabsence"]["vios"]
        or not journal["create_returned"]["vios"]
        or journal["system_uuid"] != system_uuid
        or journal["vios_uuid"] != returned_uuid
        or journal["vios_name"] != fresh_name
        or not is_uuid(returned_uuid)
        or any(row["uuid"] == returned_uuid for row in journal["baseline"]["vios"])
        or journal["deletion"]["vios"] != "not-attempted"
    ):
        raise ValueError(
            "current conditional grant and fresh acknowledged VIOS UUID required"
        )
    assert state.hmc_config is not None
    async with HMCClient(state.hmc_config) as hmc:
        await _identity(hmc, system_uuid, returned_uuid, fresh_name, vios=True)
    journal["deletion"]["vios"] = "pending"
    _checkpoint(state, "vios-delete-pending")
    status, data = await state.call(
        client,
        "hmc_delete_vios",
        vios_name_or_uuid=returned_uuid,
        system_name_or_uuid=system_uuid,
    )
    state.record(SUBTASK, "hmc_delete_vios", status, data)
    journal["deletion"]["vios"] = "unknown"
    if status != "PASS":
        return False
    async with HMCClient(state.hmc_config) as hmc:
        rows = await _vios_baseline(hmc, system_uuid)
    if rows != journal["baseline"]["vios"]:
        return False
    journal["deletion"]["vios"] = "confirmed-absent"
    _checkpoint(state, "vios-deleted")
    return True


async def _lpar_fixture(
    client: Client,
    state: RunState,
    run: _Run,
    system_uuid: str,
    layout: LparMemoryLayout,
) -> bool:
    journal = state.artifacts.creation_memory_layout
    _checkpoint(state, "lpar-create-pending")
    run.create_attempted = True
    status, data = await state.call(
        client,
        "hmc_create_lpar",
        system_name_or_uuid=run.system,
        name=run.name,
        partition_type="AIX/Linux",
        resources=RESOURCES,
        caller_token=run.token,
        memory_layout=TypeAdapter(LparMemoryLayout).dump_python(
            layout, mode="json", exclude_none=True
        ),
        apply_partition_profile=False,
    )
    data = plain_data(data)
    state.record(SUBTASK, "hmc_create_lpar", status, data)
    journal["outcome"]["lpar"] = "unknown"
    if status != "PASS" or not isinstance(data, dict):
        if status == "FAIL" and re.search(r"\bHSCL[A-Z0-9]{4}\b", str(data)):
            current = await _read_pools(client, state, run.system)
            if current is not None and run.name not in current[0].names:
                journal["outcome"]["lpar"] = "rejected"
        return False
    identity = _uuid_of(data.get("lpar"))
    if not identity or not is_uuid(identity):
        return False
    run.uuid = identity
    journal["lpar_uuid"], journal["create_returned"]["lpar"] = identity, True
    journal["outcome"]["lpar"] = "partial"
    _checkpoint(state, "lpar-create-returned")
    if (
        data.get("workflow_completed") is not True
        or data.get("ownership_stamped") is not True
    ):
        return False
    run.created = True
    observed = await _read_lpar(client, state, run, system_uuid)
    journal["outcome"]["lpar"] = (
        "created-verified" if observed["status"] == "verified" else observed["status"]
    )
    # Reuse cleanup only after an acknowledged UUID and fresh independent identity/token.
    if await _token_of(client, state, run, run.uuid) != run.token:
        raise ValueError("LPAR cleanup token differs")
    assert state.hmc_config is not None
    async with HMCClient(state.hmc_config) as hmc:
        await _identity(hmc, system_uuid, run.uuid, run.name, vios=False)
    journal["deletion"]["lpar"] = "pending"
    _checkpoint(state, "lpar-delete-pending")
    await _teardown(client, state, run, inactive_only=True)
    journal["deletion"]["lpar"] = "confirmed-absent" if run.deleted else "unknown"
    _checkpoint(state, "lpar-deleted" if run.deleted else "stopped-uncertain")
    return observed["status"] == "verified" and run.deleted


async def _vios_fixture(client: Client, state: RunState, system_uuid: str) -> bool:
    journal = state.artifacts.creation_memory_layout
    # Fresh absence is rechecked after the LPAR's cleanup, immediately before VIOS intent.
    assert state.hmc_config is not None
    async with HMCClient(state.hmc_config) as hmc:
        if await _vios_baseline(hmc, system_uuid) != journal["baseline"]["vios"]:
            raise ValueError("VIOS baseline changed before create")
    ratios = LparMemoryLayout(
        hpt_ratio=state.config.creation_memory_layout_hpt_ratio,
        ppt_ratio=state.config.creation_memory_layout_ppt_ratio,
    )
    _checkpoint(state, "vios-create-pending")
    status, data = await state.call(
        client,
        "hmc_create_vios",
        system_name_or_uuid=system_uuid,
        name=journal["vios_name"],
        resources=RESOURCES,
        memory_layout=TypeAdapter(LparMemoryLayout).dump_python(
            ratios, mode="json", exclude_none=True
        ),
    )
    data = plain_data(data)
    state.record(SUBTASK, "hmc_create_vios", status, data)
    journal["outcome"]["vios"] = "unknown"
    identity = _uuid_of(data) if status == "PASS" and isinstance(data, dict) else None
    if (
        not identity
        or not is_uuid(identity)
        or any(row["uuid"] == identity for row in journal["baseline"]["vios"])
    ):
        return False
    journal["vios_uuid"], journal["create_returned"]["vios"] = identity, True
    journal["outcome"]["vios"] = "partial"
    _checkpoint(state, "vios-create-returned")
    observed = await _read_vios(
        client, state, system_uuid, identity, journal["vios_name"]
    )
    journal["outcome"]["vios"] = (
        "created-verified" if observed["status"] == "verified" else observed["status"]
    )
    deleted = await _delete_fresh_vios(
        client, state, system_uuid, identity, journal["vios_name"]
    )
    return observed["status"] == "verified" and deleted


async def exercise_creation_memory_layout(client: Client, state: RunState) -> None:
    if state.group != GROUP or state.hmc_config is None or state.checkpoint is None:
        state.skip(
            SUBTASK,
            GROUP,
            "requires named group, HMC configuration and durable checkpoint",
        )
        return
    if (
        state.artifacts.creation_memory_layout
        or state.creation_memory_layout_invocation is not None
    ):
        state.skip(SUBTASK, GROUP, "prior invocation/restored journal refused")
        return
    try:
        layout = configured(state.config)
        if (
            not {
                "hmc_run_command",
                "hmc_create_lpar",
                "hmc_create_vios",
                "hmc_delete_lpar",
                "hmc_delete_vios",
            }
            <= state.schemas.keys()
        ):
            raise ValueError(
                "served tools/policy must permit the finite create/read/delete sequence"
            )
        async with HMCClient(state.hmc_config) as hmc:
            system_uuid = await resolve_system_uuid(hmc, state.config.system_name)
            system = await resolve_system_name(hmc, system_uuid)
            vios = await _vios_baseline(hmc, system_uuid)
        baseline = await _read_pools(client, state, system)
        if (
            baseline is None
            or scratch_partitions(baseline[0].names)
            or any(row["name"].startswith(VIOS_PREFIX) for row in vios)
        ):
            raise ValueError(
                "baseline unavailable or stranded scratch; run read-only recovery"
            )
        suffix = uuid.uuid4().hex[:8]
        run = _Run(
            system,
            NAME_PREFIX + suffix,
            TOKEN_PREFIX + suffix,
            baseline[1],
            subtask=SUBTASK,
        )
        vname = VIOS_PREFIX + uuid.uuid4().hex[:8]
        if (
            run.name in baseline[0].names
            or vname in baseline[0].names
            or any(row["name"] == vname for row in vios)
        ):
            raise ValueError("fresh-name collision")
        from .runner import _hmc_identity

        invocation = uuid.uuid4().hex
        state.creation_memory_layout_invocation = invocation
        state.artifacts.creation_memory_layout = {
            "invocation_id": invocation,
            "candidate_commit": _candidate(),
            "system_uuid": system_uuid,
            "hmc_identity": _hmc_identity(state.hmc_config),
            "phase": "preflight",
            "lpar_name": run.name,
            "lpar_token": run.token,
            "lpar_uuid": None,
            "vios_name": vname,
            "vios_uuid": None,
            "preabsence": {"lpar": True, "vios": True},
            "create_returned": {"lpar": False, "vios": False},
            "fresh_vios_delete_authorized": True,
            "requested": {
                "lpar": {
                    "min_num_huge_pages": 0,
                    "desired_num_huge_pages": 0,
                    "max_num_huge_pages": 0,
                    "hpt_ratio": state.config.creation_memory_layout_hpt_ratio,
                    "ppt_ratio": state.config.creation_memory_layout_ppt_ratio,
                    "kvm_reserved_mem_percent": "0",
                },
                "vios": {
                    "hpt_ratio": state.config.creation_memory_layout_hpt_ratio,
                    "ppt_ratio": state.config.creation_memory_layout_ppt_ratio,
                },
            },
            "baseline": {"pools": _pools_data(baseline[0]), "vios": vios},
            "outcome": {"lpar": "not-attempted", "vios": "not-attempted"},
            "deletion": {"lpar": "not-attempted", "vios": "not-attempted"},
        }
        _checkpoint(state, "preflight")
        lpar_ok = await _lpar_fixture(client, state, run, system_uuid, layout)
        if not lpar_ok or not await _compare(client, state, run, baseline[0]):
            _checkpoint(state, "stopped-uncertain")
            return
        state.record_verified(
            SUBTASK,
            "hmc_create_lpar",
            operation="lpar.create",
            scenario=LPAR_SCENARIO,
            assertions=[Assertion("independent-memory-layout-readback", lpar_ok)],
            cleanup="passed",
            data={"configured-only": True, "activation": "not-run"},
        )
        vios_ok = await _vios_fixture(client, state, system_uuid)
        if not vios_ok or not await _compare(client, state, run, baseline[0]):
            _checkpoint(state, "stopped-uncertain")
            return
        state.record_verified(
            SUBTASK,
            "hmc_create_vios",
            operation="vios.create",
            scenario=VIOS_SCENARIO,
            assertions=[Assertion("independent-page-table-ratios", vios_ok)],
            cleanup="passed",
            data={"configured-only": True, "activation": "not-run"},
        )
        _checkpoint(state, "complete")
    except (HMCError, OSError, ValueError, TypeError) as error:
        state.record(SUBTASK, GROUP, "FAIL", type(error).__name__)
        # Never write again after a failed checkpoint or unknown outcome. The durable pending
        # phase is itself the recovery witness; a best-effort rewrite could hide that failure.
