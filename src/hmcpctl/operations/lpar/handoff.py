"""The host handoff behind ``hmc_prepare_host_handoff`` (ADR 0193, ADR 0329).

``prepare`` resolves the partition, reads the document's sections, each reported on its
own, and only then places or finds the hold, so a call that fails earlier places nothing.
``release`` drops the hold. Neither writes to the HMC, and no field carries a key, URL or
producer result. A ``hold_id`` appears only in the result's ``hold``, and only for the agent
that placed the hold. The contract is ``docs/workflow/specs/2026-11-03-host-handoff-design.md``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from hmcpctl.operations.lpar.decommission.core import resolve_target_lpar
from hmcpctl.operations.lpar.workflow_contract import PARTITION_LEDGER

from ...errors import HMCError, HMCTLSFingerprintError
from ...resource_identity import resolve_system_uuid
from ..inventory.composite import optional_int, optional_text
from ..logical import holds, store
from ..logical.holds import Hold
from .inspect import Admit, Resources, SectionSource, read_resources
from .ownership import lpar_ownership_entry
from .reconfiguration.network import current_adapters

ADAPTERS_TOOL = "hmc_list_adapters"
_MAX_DETAIL = 500
_KDIVE = "not verified by hmcpctl; kdive's doctor/adopt checks it"
UNVERIFIED = (
    "os",
    "kernel",
    "bootloader",
    "kdump_fadump",
    "ssh_reachability",
    "host_identity",
)
# ADR 0193 Decision 6 and ADR 0339: what the partition/system hold checks do not fence.
UNFENCED = (
    "the hmcpctl library API, which kdive calls",
    "hmcpctl on other hosts",
    "the HMC GUI",
    "direct HMC commands",
    "hmcpctl raw",
    (
        "every non-read hmcpctl tool the hold hook does not check, listed in "
        "unchecked_tools (ADR 0327 Decision 5, ADR 0339); hmc_invoke_tool is checked as the tool "
        "it dispatches"
    ),
    (
        "hmc_capture_lpar_console, exempt because console leasing is kdive's "
        "(ADR 0193 Decision 3)"
    ),
)
DeclaredSource = Literal["ok", "none", "unavailable"]


@dataclass(frozen=True)
class HoldView:
    """A hold as a caller sees it; ``hold_id`` only for the agent that placed it."""

    consumer_label: str
    agent_id: str
    created_at: str
    hold_id: str | None


@dataclass(frozen=True)
class Identity:
    connection: str
    system_uuid: str
    partition_uuid: str
    name: str | None
    partition_id: int | None


@dataclass(frozen=True)
class References:
    """Connection and credential references; never a secret."""

    connection: str
    agent_id: str


@dataclass(frozen=True)
class Ownership:
    """The advisory ADR 0011 stamp from the partition read."""

    owned: bool
    owner: str | None
    unparsed: bool


@dataclass(frozen=True)
class Adapters:
    """The client network adapters: MAC, port VLAN and slot of each."""

    source: SectionSource
    items: list[dict[str, Any]]


@dataclass(frozen=True)
class Observed:
    """Facts from reads made during this call."""

    state: str | None
    rmc_state: str | None
    resources: Resources
    ownership: Ownership
    adapters: Adapters


@dataclass(frozen=True)
class Declared:
    """What this host's provision requested, not what it applied (ADR 0329 Decision 2)."""

    source: DeclaredSource
    detail: str | None
    operation_id: str | None = None
    operation_state: str | None = None
    operation_outcome: str | None = None
    install_profile: str | None = None
    network: dict[str, Any] | None = None
    login_user: str | None = None
    ssh_key_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unverified:
    item: str
    statement: str


@dataclass(frozen=True)
class HandoffDocument:
    identity: Identity
    references: References
    observed: Observed
    declared: Declared
    unverified: list[Unverified]
    hold: HoldView | None
    unfenced: list[str]
    unchecked_tools: list[str]


@dataclass(frozen=True)
class HandoffResult:
    """ADR 0193 Decision 1's result; ``document`` is null on ``release``."""

    action: Literal["prepare", "release"]
    hold: HoldView | None
    document: HandoffDocument | None


def check_inputs(
    action: str, hold: bool, consumer_label: str | None, hold_id: str | None
) -> None:
    """Refuse the argument combinations ADR 0193 Decision 1 does not define."""
    if action == "release":
        if hold or consumer_label is not None:
            raise ValueError("release takes no hold or consumer_label")
        return
    if hold_id is not None:
        raise ValueError("hold_id is for release; prepare is exempt from holds")
    if hold != (consumer_label is not None):
        raise ValueError("hold=true and consumer_label go together")


def _view(hold: Hold | None, agent_id: str, *, reveal: bool) -> HoldView | None:
    if hold is None:
        return None
    mine = reveal and hold.agent_id == agent_id
    return HoldView(
        hold.consumer_label,
        hold.agent_id,
        hold.created_at,
        hold.hold_id if mine else None,
    )


async def _resolve(hmc: Any, system: str, lpar: str) -> tuple[str, dict[str, Any]]:
    system_uuid = (await resolve_system_uuid(hmc, system)).lower()
    return system_uuid, await resolve_target_lpar(hmc, system_uuid, lpar)


def _failure(exc: Exception) -> str:
    return (str(exc) or type(exc).__name__)[:_MAX_DETAIL]


async def _adapters(
    hmc: Any, admit: Admit, system: str, lpar: str, lpar_uuid: str
) -> Adapters:
    """Admitted with the caller's selectors, as inspect's refcodes are; read by UUID."""
    denial = admit(ADAPTERS_TOOL, {"lpar": lpar, "managed_system": system})
    if denial is not None:
        return Adapters(
            SectionSource("denied", ADAPTERS_TOOL, denial[:_MAX_DETAIL]), []
        )
    try:
        found = await current_adapters(hmc, lpar_uuid)
    except HMCTLSFingerprintError:
        raise
    except (HMCError, ValueError) as exc:
        return Adapters(SectionSource("unavailable", ADAPTERS_TOOL, _failure(exc)), [])
    keep = ("mac", "port_vlan_id", "slot")
    return Adapters(
        SectionSource("ok", ADAPTERS_TOOL),
        [{key: adapter[key] for key in keep} for adapter in found],
    )


def _fingerprint(key: object) -> str | None:
    """OpenSSH's ``SHA256:`` fingerprint of a public key line; None when undecodable."""
    parts = key.split() if isinstance(key, str) else []
    if len(parts) < 2:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def _from_install(
    operation: tuple[str, str | None, str | None], install: object
) -> Declared:
    if not isinstance(install, dict):
        return Declared("ok", "the provision installed no operating system", *operation)
    network = install.get("network")
    media = install.get("media")
    built = isinstance(media, dict) and media.get("mode") == "built"
    keys = install.get("ssh_authorized_keys") if built else None
    prints = [_fingerprint(key) for key in keys] if isinstance(keys, list) else []
    return Declared(
        "ok",
        None,
        *operation,
        install_profile=optional_text(install.get("profile")),
        network=(
            {key: network.get(key) for key in ("address", "routes", "dns")}
            if isinstance(network, dict)
            else None
        ),
        login_user=optional_text(install.get("login_user")) if built else None,
        ssh_key_fingerprints=tuple(p for p in prints if p is not None),
    )


def read_declared(system_uuid: str, partition_uuid: str) -> Declared:
    """The install this host's provision recorded for the partition (ADR 0329 Decision 2)."""
    try:
        with store.read_session() as conn:
            found = (
                None
                if conn is None
                else store.ledger_request(
                    conn, PARTITION_LEDGER, system_uuid, partition_uuid
                )
            )
    except (ValueError, KeyError, TypeError) as exc:
        return Declared("unavailable", _failure(exc))
    if found is None:
        return Declared("none", "no provision on this host recorded this partition")
    operation_id, state, outcome, arguments = found
    if not isinstance(arguments, dict):
        return Declared(
            "unavailable",
            "the provision record was pruned after retention",
            operation_id,
        )
    return _from_install((operation_id, state, outcome), arguments.get("install"))


async def prepare_handoff(
    hmc: Any,
    *,
    connection: str,
    agent_id: str,
    admit: Admit,
    system: str,
    lpar: str,
    consumer_label: str | None,
    unchecked_tools: Sequence[str],
) -> HandoffResult:
    """Read the handoff document, then place (given *consumer_label*) or find the hold."""
    system_uuid, entry = await _resolve(hmc, system, lpar)
    uuid = str(entry["UUID"]).lower()
    resource = entry.get("Resource") or {}
    stamp = lpar_ownership_entry(entry)
    observed = Observed(
        state=optional_text(resource.get("PartitionState")),
        rmc_state=optional_text(resource.get("ResourceMonitoringControlState")),
        resources=await read_resources(hmc, admit, system, system_uuid, entry),
        ownership=Ownership(stamp["owned"], stamp["owner"], stamp["unparsed"]),
        adapters=await _adapters(hmc, admit, system, lpar, uuid),
    )
    declared = read_declared(system_uuid, uuid)
    held = (
        holds.find_hold(uuid)
        if consumer_label is None
        else holds.place_hold(
            system_uuid,
            uuid,
            consumer_label=consumer_label,
            agent_id=agent_id,
            connection=connection,
        )
    )
    document = HandoffDocument(
        identity=Identity(
            connection,
            system_uuid,
            uuid,
            optional_text(resource.get("PartitionName")),
            optional_int(resource.get("PartitionID")),
        ),
        references=References(connection, agent_id),
        observed=observed,
        declared=declared,
        unverified=[Unverified(item, _KDIVE) for item in UNVERIFIED],
        hold=_view(held, agent_id, reveal=False),
        unfenced=list(UNFENCED),
        unchecked_tools=list(unchecked_tools),
    )
    return HandoffResult("prepare", _view(held, agent_id, reveal=True), document)


async def release_handoff(
    hmc: Any, *, agent_id: str, system: str, lpar: str, hold_id: str | None
) -> HandoffResult:
    """Drop the partition's hold; writes nothing to the HMC (ADR 0193 Decision 1)."""
    system_uuid, entry = await _resolve(hmc, system, lpar)
    released = holds.release_hold(
        system_uuid, str(entry["UUID"]).lower(), agent_id=agent_id, hold_id=hold_id
    )
    return HandoffResult("release", _view(released, agent_id, reveal=False), None)
