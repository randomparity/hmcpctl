"""ST65: one opt-in inactive creation with an independent saved XML oracle."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError  # nosec B405 — exception class only

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from hmcpctl.client import client_profiles
from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar_creation_memory import LparCreationMemory
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import profiles
from hmcpctl.operations.lpar.linux_roles import read_role_capabilities
from hmcpctl.operations.lpar.ownership import parse_lpar_ownership_caller_token
from hmcpctl.resource_identity import is_uuid, resolve_system_name, resolve_system_uuid
from hmcpctl.xmlutil import parse_feed

from .config import _validate_creation_memory
from .creation_memory_layout import _candidate
from .lpar_config import (
    NAME_PREFIX,
    RESOURCES,
    TOKEN_PREFIX,
    _pools_data,
    _read_pools,
    _uuid_of,
    scratch_partitions,
)
from .observation import Assertion, judge_create_result, plain_data
from .results import field as result_field

if TYPE_CHECKING:
    from fastmcp import Client

    from hmcpctl.client.client_profiles import PartitionKind

    from .config import LiveTestConfig
    from .runner import RunState

SUBTASK = 65
GROUP = "creation-memory"
SHARED_SCENARIO = "st65-shared-lpar"
REDUNDANT_SCENARIO = "st65-redundant-lpar"
AME_ZERO_SCENARIO = "st65-ame-zero-vios"
AME_POSITIVE_SCENARIO = "st65-ame-positive-vios"
CONTROLLER_SCENARIO = "st65-controller-shared-lpar"
VIOS_PREFIX = "hmcpctl-live-c07-vios-"
LIMIT = 5 * 1024 * 1024
A = "{http://www.w3.org/2005/Atom}"
N = "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}"


def configured(config: LiveTestConfig) -> tuple[PartitionKind, LparCreationMemory]:
    case = config.creation_memory_case
    if case not in {
        "shared-lpar",
        "redundant-lpar",
        "ame-zero-vios",
        "ame-positive-vios",
        "controller-shared-lpar",
    }:
        raise ValueError("select exactly one known creation-memory case")
    if config.creation_memory_fresh_delete_authorized is not True:
        raise ValueError("current conditional fresh-UUID deletion permission required")
    primary, secondary = (
        config.creation_memory_primary_vios_name,
        config.creation_memory_secondary_vios_name,
    )
    if case in {"ame-zero-vios", "ame-positive-vios"}:
        if primary or secondary:
            raise ValueError("AME VIOS cases forbid paging selectors")
        return "VirtualIOServer", LparCreationMemory(
            mem_mode="ded",
            mem_expansion=Decimal(0) if case == "ame-zero-vios" else Decimal("1.10"),
        )
    if not primary or (
        case == "redundant-lpar" and (not secondary or primary == secondary)
    ):
        raise ValueError("explicit distinct paging provider names required")
    if case != "redundant-lpar" and secondary:
        raise ValueError("single-provider case forbids a secondary name")
    return "LogicalPartition", LparCreationMemory(
        mem_mode="shared",
        desired_io_entitled_mem="auto",
        mem_weight=0,
        primary_paging_vios_name=primary,
        secondary_paging_vios_name=secondary or None,
        secondary_paging_vios_id=None if secondary else "none",
    )


def _tool_memory(memory: LparCreationMemory) -> dict:
    result = dict(memory.pairs())
    if memory.mem_expansion is not None:
        result["mem_expansion"] = 0 if memory.mem_expansion == 0 else 1.1
    return result


def _canonical(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and is_uuid(value) else None


def _xml(raw: str):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > LIMIT:
        raise ValueError("native evidence exceeds 5 MiB or is not text")
    return DET.fromstring(raw)


async def _feed(
    hmc, system: str, kind: PartitionKind, identity: str | None = None
) -> list[dict]:
    path = f"/rest/api/uom/ManagedSystem/{system}/{kind}"
    arguments = {"system_uuid": system}
    if identity is not None:
        path = f"/rest/api/uom/{kind}/{identity}"
        arguments = {"partition_uuid": identity}
    raw = await hmc._get(path, uuid_path_arguments=arguments)
    root = _xml(raw)
    if root.tag not in {A + "feed", A + "entry"} or any(
        e.get("rel") == "next" for e in root.iter(A + "link")
    ):
        raise ValueError("complete Atom partition evidence required")
    entries = list(root.findall(A + "entry")) if root.tag == A + "feed" else [root]
    for entry in entries:
        contents, ids = entry.findall(A + "content"), entry.findall(A + "id")
        if (
            len(contents) != 1
            or len(contents[0]) != 1
            or contents[0][0].tag != N + kind
            or len(ids) != 1
            or len(ids[0])
            or _canonical((ids[0].text or "").removeprefix("urn:uuid:")) is None
        ):
            raise ValueError("Atom resource kind/identity is malformed")
    rows = parse_feed(raw)
    if len(rows) != len(entries):
        raise ValueError("Atom partition inventory is incomplete")
    if identity is not None and (
        len(rows) != 1 or _canonical(_uuid_of(rows[0])) != identity
    ):
        raise ValueError("fixed detail identity differs")
    identities, names = [], []
    for row in rows:
        resource = row.get("Resource")
        if not isinstance(resource, dict):
            raise ValueError("partition resource unavailable")  # noqa: TRY004 — malformed remote evidence
        uid, name = _canonical(_uuid_of(row)), resource.get("PartitionName")
        if uid is None or not isinstance(name, str) or not name:
            raise ValueError("partition inventory identity unavailable")
        identities.append(uid)
        names.append(name)
    if len(set(identities)) != len(rows) or len(set(names)) != len(rows):
        raise ValueError("partition inventory is ambiguous")
    return rows


async def _vios(hmc, system: str) -> list[dict]:
    return sorted(
        [
            {
                "uuid": _canonical(_uuid_of(row)),
                "name": row["Resource"]["PartitionName"],
            }
            for row in await _feed(hmc, system, "VirtualIOServer")
        ],
        key=lambda row: row["uuid"],
    )


async def _baseline(
    client: Client, state: RunState, hmc, system: str, name: str
) -> dict:
    pools = await _read_pools(client, state, name)
    vios = await _vios(hmc, system)
    lpars = await _feed(hmc, system, "LogicalPartition")
    if pools is None:
        raise ValueError("independent pool baseline unavailable")
    data = _pools_data(pools[0])
    names = [row["Resource"]["PartitionName"] for row in lpars] + [
        row["name"] for row in vios
    ]
    if len(set(names)) != len(names) or sorted(names) != data["partitions"]:
        raise ValueError("native and contained partition inventories disagree")
    return {"pools": data, "vios": vios}


def _checkpoint(state: RunState, phase: str) -> None:
    journal = state.artifacts.creation_memory
    kind, memory = configured(state.config)
    from .runner import _hmc_identity

    if (
        state.checkpoint is None
        or state.hmc_config is None
        or journal["invocation_id"] != state.creation_memory_invocation
        or journal["candidate_commit"] != _candidate()
        or journal["hmc_identity"] != _hmc_identity(state.hmc_config)
        or journal["kind"] != kind
        or journal["requested"] != dict(memory.pairs())
    ):
        raise ValueError("fresh invocation/candidate/HMC/checkpoint binding required")
    journal["phase"] = phase
    _validate_creation_memory(journal)
    state.checkpoint()


async def _identity(client: Client, state: RunState, hmc) -> None:
    j = state.artifacts.creation_memory
    if not j["create_returned"] or not j["preabsence"] or not j["uuid"]:
        raise ValueError("successful fresh returned UUID required")
    contained = await _feed(hmc, j["system_uuid"], j["kind"])
    matching = [r for r in contained if _canonical(_uuid_of(r)) == j["uuid"]]
    if len(matching) != 1:
        raise ValueError("created UUID is not contained")
    row = (await _feed(hmc, j["system_uuid"], j["kind"], j["uuid"]))[0]
    resource = row.get("Resource") or {}
    if (
        resource.get("PartitionName") != j["name"]
        or resource.get("PartitionType")
        != ("AIX/Linux" if j["kind"] == "LogicalPartition" else "Virtual IO Server")
        or resource.get("PartitionState") != "not activated"
        or matching[0]["Resource"].get("PartitionName") != j["name"]
    ):
        raise ValueError("fresh UUID/name/kind/inactive identity differs")
    if j["name"] in j["baseline"]["pools"]["partitions"] or any(
        r["uuid"] == j["uuid"] for r in j["baseline"]["vios"]
    ):
        raise ValueError("created identity overlaps baseline")
    if j["kind"] == "LogicalPartition":
        status, description = await state.call(
            client,
            "hmc_get_lpar_description",
            system_name_or_uuid=j["system_uuid"],
            lpar_name_or_uuid=j["uuid"],
        )
        if (
            status != "PASS"
            or not isinstance(description, str)
            or parse_lpar_ownership_caller_token(description) != j["token"]
        ):
            raise ValueError("fresh LPAR caller token differs")


def _one(parent, leaf: str):
    found = parent.findall(N + leaf)
    if len(found) != 1:
        raise ValueError("missing or ambiguous native leaf")
    return found[0]


def _scalar(parent, leaf: str) -> str:
    element = _one(parent, leaf)
    if len(element) or element.text is None:
        raise ValueError("native scalar unavailable")
    return element.text.strip()


async def _provider(hmc, system: str, element, expected: str | None) -> object:
    links = element.findall(N + "link")
    if expected is None:
        if (
            len(element)
            or (element.text or "").strip()
            or set(element.attrib) - {"kb", "kxe", "kbo", "schemaVersion"}
        ):
            raise ValueError("explicit empty secondary evidence unavailable")
        return "none"
    if (element.text or "").strip():
        raise ValueError("designated provider link unavailable")
    if len(element):
        if len(links) != 1 or len(element) != 1:
            raise ValueError("designated provider link ambiguous")
        href = links[0].get("href", "")
    else:
        href = element.get("href", "")
    if not href or not href.isprintable() or any(c.isspace() for c in href):
        raise ValueError("designated provider link malformed")
    url = urlsplit(href)
    identity = _canonical(url.path.rsplit("/", 1)[-1])
    if (
        not identity
        or url.scheme != "https"
        or url.hostname != hmc.config.host
        or (url.port if url.port is not None else 443) != hmc.config.port
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path
        not in {
            f"/rest/api/uom/VirtualIOServer/{identity}",
            f"/rest/api/uom/ManagedSystem/{system}/VirtualIOServer/{identity}",
        }
        or element.get("href") not in {None, href}
    ):
        raise ValueError("provider link is foreign or malformed")
    inventory = await _feed(hmc, system, "VirtualIOServer")
    selected = [r for r in inventory if _canonical(_uuid_of(r)) == identity]
    detail = (await _feed(hmc, system, "VirtualIOServer", identity))[0]
    resource = detail["Resource"]
    pid = resource.get("PartitionID")
    if (
        len(selected) != 1
        or resource.get("PartitionName") != selected[0]["Resource"]["PartitionName"]
        or resource.get("PartitionType") != "Virtual IO Server"
        or not isinstance(pid, str)
        or re.fullmatch(r"[1-9][0-9]*", pid) is None
    ):
        raise ValueError("designated provider identity unavailable")
    return {
        "uuid": identity,
        "name": resource["PartitionName"],
        "partition_id": int(pid),
    }


async def _oracle(hmc, journal: dict, *, controller: bool) -> dict:
    """Literal XML expectations: never consumes the product verification payload."""
    kind, system, identity = journal["kind"], journal["system_uuid"], journal["uuid"]
    if controller:
        raw = await hmc._get(
            f"/rest/api/uom/{kind}/{identity}",
            uuid_path_arguments={"partition_uuid": identity},
        )
        root = _xml(raw)
        entries = parse_feed(raw)
        if (
            len(entries) != 1
            or _canonical(_uuid_of(entries[0])) != identity
            or entries[0].get("ResourceType") != kind
        ):
            raise ValueError("controller current identity differs")
        containers = root.findall(".//" + N + "PartitionMemoryConfiguration")
    else:
        inventory = await profiles.list_partition_profiles(
            hmc, system, identity, partition_type=kind
        )
        selected = [p for p in inventory.profiles if p.name == "default_profile"]
        if (
            len(selected) != 1
            or inventory.partition_uuid != identity
            or inventory.system_uuid != system
            or inventory.partition_type != kind
        ):
            raise ValueError("independent default profile identity unavailable")
        native = await client_profiles.read_native_profile(
            hmc, kind, identity, selected[0].uuid, system
        )
        if native.uuid != selected[0].uuid or native.name != "default_profile":
            raise ValueError("independent native profile selection differs")
        root = _xml(native.raw)
        containers = root.findall(".//" + N + "ProfileMemory")
    if len(containers) != 1:
        return {"status": "unavailable", "observed": {}, "unavailable": ["memory"]}
    fields, requested = containers[0], journal["requested"]
    checks = {
        "mem_mode": (
            "SharedMemoryEnabled" if controller else "ActiveMemorySharingEnabled",
            "true" if requested["mem_mode"] == "shared" else "false",
        ),
    }
    if "mem_weight" in requested:
        checks["mem_weight"] = (
            "CurrentMemoryWeight" if controller else "MemoryWeight",
            "0",
        )
        checks["desired_io_entitled_mem"] = (
            "AutoEntitledMemoryEnabled" if controller else "ManualEntitledModeEnabled",
            "true" if controller else "false",
        )
    if "mem_expansion" in requested:
        checks["mem_expansion"] = (
            "ActiveMemoryExpansionEnabled",
            "false" if requested["mem_expansion"] == "0" else "true",
        )
    observed, missing, mismatch = {}, [], False
    for key, (leaf, expected) in checks.items():
        try:
            actual = _scalar(fields, leaf)
            if key == "mem_weight":
                if re.fullmatch(r"[0-9]+", actual) is None:
                    raise ValueError("weight unavailable")
                holds = int(actual) == 0
                observed[key] = int(actual)
            else:
                if actual not in {"true", "false"}:
                    raise ValueError("boolean unavailable")
                holds = actual == expected
                observed[key] = actual
            mismatch |= not holds
        except ValueError:
            missing.append(key)
    if requested.get("mem_expansion") == "1.1":
        try:
            factor = Decimal(_scalar(fields, "ExpansionFactor"))
            if not factor.is_finite():
                raise ValueError("factor unavailable")
            observed["ExpansionFactor"] = str(factor)
            mismatch |= factor != Decimal("1.1")
        except (ValueError, InvalidOperation):
            missing.append("ExpansionFactor")
    if requested["mem_mode"] == "shared":
        for role, leaf in (
            ("primary", "PrimaryPagingServicePartition"),
            ("secondary", "SecondaryPagingServicePartition"),
        ):
            try:
                if controller:
                    raise ValueError("controller designated proof unavailable")
                expected = requested.get(role + "_paging_vios_name")
                observed[leaf] = await _provider(
                    hmc, system, _one(fields, leaf), expected
                )
                mismatch |= (
                    observed[leaf] != "none"
                    if expected is None
                    else observed[leaf]["name"] != expected
                )
            except ValueError:
                missing.append(leaf)
    return {
        "status": "mismatch" if mismatch else "unavailable" if missing else "verified",
        "observed": observed,
        "unavailable": missing,
    }


async def _prepare(
    client: Client,
    state: RunState,
    hmc,
    kind: PartitionKind,
    memory: LparCreationMemory,
) -> tuple[str, bool]:
    assert state.hmc_config is not None
    system = _canonical(await resolve_system_uuid(hmc, state.config.system_name))
    if system is None:
        raise ValueError("selected-system UUID unavailable")
    name = await resolve_system_name(hmc, system)
    mode = await read_role_capabilities(
        hmc, system, need_kvm=False, need_management=False
    )
    controller = state.config.creation_memory_case == "controller-shared-lpar"
    if mode.controller_mode != controller:
        raise ValueError("independently observed classic/controller mode differs")
    baseline = await _baseline(client, state, hmc, system, name)
    if scratch_partitions(baseline["pools"]["partitions"]) or any(
        r["name"].startswith(VIOS_PREFIX) for r in baseline["vios"]
    ):
        raise ValueError("stranded scratch requires read-only recovery")
    suffix, invocation = uuid.uuid4().hex[:8], uuid.uuid4().hex
    fixture = (NAME_PREFIX if kind == "LogicalPartition" else VIOS_PREFIX) + suffix
    if fixture in baseline["pools"]["partitions"]:
        raise ValueError("fresh name collision")
    from .runner import _hmc_identity

    state.creation_memory_invocation = invocation
    state.artifacts.creation_memory = {
        "invocation_id": invocation,
        "candidate_commit": _candidate(),
        "system_uuid": system,
        "hmc_identity": _hmc_identity(state.hmc_config),
        "case": state.config.creation_memory_case,
        "kind": kind,
        "name": fixture,
        "token": TOKEN_PREFIX + suffix if kind == "LogicalPartition" else None,
        "uuid": None,
        "phase": "prepared",
        "preabsence": True,
        "create_returned": False,
        "fresh_delete_authorized": True,
        "requested": dict(memory.pairs()),
        "baseline": baseline,
        "outcome": "not-attempted",
        "deletion": "not-attempted",
    }
    _checkpoint(state, "prepared")
    return name, controller


async def _current_system(state: RunState, hmc) -> None:
    actual = _canonical(await resolve_system_uuid(hmc, state.config.system_name))
    if actual != state.artifacts.creation_memory["system_uuid"]:
        raise ValueError("current selected system differs from intent")


async def _create(
    client: Client, state: RunState, hmc, memory: LparCreationMemory
) -> bool:
    j = state.artifacts.creation_memory
    _checkpoint(state, "create-pending")
    await _current_system(state, hmc)
    if j["kind"] == "LogicalPartition":
        status, data = await state.call(
            client,
            "hmc_create_lpar",
            system_name_or_uuid=j["system_uuid"],
            name=j["name"],
            caller_token=j["token"],
            resources=RESOURCES,
            creation_memory=_tool_memory(memory),
            apply_partition_profile=False,
        )
        result_status, _ = judge_create_result(status, data)
        entry = result_field(data, "lpar") if result_status == "PASS" else None
        if j["case"] == "controller-shared-lpar":
            result_status, entry = "FAIL", None
            # This acknowledged partial result permits only independent readback.
            match plain_data(data):
                case {
                    "resource_created": True,
                    "workflow_completed": False,
                    "lpar": returned,
                    "steps": [
                        {"step": "create", "status": "ok", "result": created},
                        {
                            "step": "creation_memory",
                            "status": "error",
                            "result": {
                                "saved": None,
                                "current": {
                                    "status": "unavailable",
                                    "unavailable": [
                                        "primary_paging_vios_name",
                                        "secondary_paging_vios_id",
                                    ],
                                },
                            },
                        },
                        {"step": "apply_profile", "status": "skipped"},
                    ],
                } if (
                    status == "PASS"
                    and _canonical(_uuid_of(returned)) is not None
                    and _canonical(_uuid_of(returned)) == _canonical(_uuid_of(created))
                ):
                    result_status, entry = "PASS", returned
    else:
        status, data = await state.call(
            client,
            "hmc_create_vios",
            system_name_or_uuid=j["system_uuid"],
            name=j["name"],
            resources=RESOURCES,
            creation_memory=_tool_memory(memory),
        )
        result_status, entry = status, plain_data(data)
    identity = _canonical(_uuid_of(entry)) if result_status == "PASS" else None
    # Report only bounded classification; raw product errors can contain private remote text.
    state.record(
        SUBTASK,
        "creation submission",
        "PASS" if identity else "FAIL",
        {"returned-uuid": identity},
    )
    if identity is None:
        j["outcome"] = "unknown"
        _checkpoint(state, "stopped-uncertain")
        return False
    j["uuid"], j["create_returned"], j["outcome"] = identity, True, "partial"
    _checkpoint(state, "create-returned")
    return True


async def _delete(client: Client, state: RunState, hmc) -> bool:
    j = state.artifacts.creation_memory
    configured(state.config)
    if j["outcome"] != "created-verified":
        raise ValueError("incomplete creation forbids automatic cleanup")
    await _identity(client, state, hmc)
    await _current_system(state, hmc)
    j["deletion"] = "pending"
    _checkpoint(state, "delete-pending")
    if j["kind"] == "LogicalPartition":
        status, _ = await state.call(
            client,
            "hmc_delete_lpar",
            system_name_or_uuid=j["system_uuid"],
            lpar_name_or_uuid=j["uuid"],
        )
    else:
        status, _ = await state.call(
            client,
            "hmc_delete_vios",
            system_name_or_uuid=j["system_uuid"],
            vios_name_or_uuid=j["uuid"],
        )
    rows = await _feed(hmc, j["system_uuid"], j["kind"]) if status == "PASS" else None
    gone = rows is not None and all(
        _canonical(_uuid_of(r)) != j["uuid"]
        and r["Resource"]["PartitionName"] != j["name"]
        for r in rows
    )
    j["deletion"] = "confirmed-absent" if gone else "unknown"
    _checkpoint(state, "deleted" if gone else "stopped-uncertain")
    return gone


def _verified(state: RunState) -> None:
    data = {"configured-only": True, "activation": "not-run"}
    assertions = [Assertion("independent-creation-memory-readback", True)]
    case = state.config.creation_memory_case
    if case == "shared-lpar":
        state.record_verified(
            SUBTASK,
            "hmc_create_lpar",
            operation="lpar.create",
            scenario=SHARED_SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
    elif case == "redundant-lpar":
        state.record_verified(
            6501,
            "hmc_create_lpar",
            operation="lpar.create",
            scenario=REDUNDANT_SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
    elif case == "ame-zero-vios":
        state.record_verified(
            SUBTASK,
            "hmc_create_vios",
            operation="vios.create",
            scenario=AME_ZERO_SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )
    elif case == "ame-positive-vios":
        state.record_verified(
            6502,
            "hmc_create_vios",
            operation="vios.create",
            scenario=AME_POSITIVE_SCENARIO,
            assertions=assertions,
            cleanup="passed",
            data=data,
        )


async def exercise_creation_memory(client: Client, state: RunState) -> None:
    if state.group != GROUP or state.hmc_config is None or state.checkpoint is None:
        state.skip(
            SUBTASK,
            GROUP,
            "requires named group, HMC configuration and durable checkpoint",
        )
        return
    if state.artifacts.creation_memory or state.creation_memory_invocation is not None:
        state.skip(SUBTASK, GROUP, "prior invocation/restored journal refused")
        return
    try:
        kind, memory = configured(state.config)
    except (ValueError, TypeError) as error:
        state.skip(SUBTASK, GROUP, str(error))
        return
    try:
        tools = (
            {
                "hmc_run_command",
                "hmc_create_lpar",
                "hmc_delete_lpar",
                "hmc_get_lpar_description",
            }
            if kind == "LogicalPartition"
            else {"hmc_run_command", "hmc_create_vios", "hmc_delete_vios"}
        )
        if not tools <= state.schemas.keys():
            raise ValueError(
                "current served policy lacks required create/read/delete tools"
            )
        async with HMCClient(state.hmc_config) as hmc:
            name, controller = await _prepare(client, state, hmc, kind, memory)
            if not await _create(client, state, hmc, memory):
                return
            await _identity(client, state, hmc)
            mode = await read_role_capabilities(
                hmc,
                state.artifacts.creation_memory["system_uuid"],
                need_kvm=False,
                need_management=False,
            )
            if mode.controller_mode != controller:
                raise ValueError("management mode changed after creation")
            observed = await _oracle(
                hmc, state.artifacts.creation_memory, controller=controller
            )
            j = state.artifacts.creation_memory
            j["outcome"] = (
                "created-verified"
                if observed["status"] == "verified" and not controller
                else observed["status"]
            )
            state.record(
                SUBTASK,
                GROUP,
                "PASS" if j["outcome"] == "created-verified" else "FAIL",
                {
                    "configured-only": True,
                    "activation": "not-run",
                    "scenario": CONTROLLER_SCENARIO
                    if controller
                    else state.config.creation_memory_case,
                    **observed,
                },
            )
            if j["outcome"] != "created-verified":
                _checkpoint(state, "stopped-uncertain")
                return
            if not await _delete(client, state, hmc):
                return
            if (
                await _baseline(client, state, hmc, j["system_uuid"], name)
                != j["baseline"]
            ):
                state.record(
                    SUBTASK, GROUP, "FAIL", "independent baseline differs after cleanup"
                )
                _checkpoint(state, "stopped-uncertain")
                return
            _checkpoint(state, "complete")
            _verified(state)
    except (
        HMCError,
        OSError,
        ValueError,
        TypeError,
        ParseError,
        DefusedXmlException,
    ) as error:
        state.record(SUBTASK, GROUP, "FAIL", type(error).__name__)
        # A failed/uncertain checkpoint or call forbids follow-up effects and best-effort replay.
