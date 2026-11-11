"""The host handoff behind hmc_prepare_host_handoff (#1852, ADR 0193, ADR 0329)."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.operations.logical import holds, store
from hmcpctl.operations.logical.store import OperationRefused
from hmcpctl.operations.lpar import handoff
from hmcpctl.operations.lpar.handoff import (
    UNFENCED,
    UNVERIFIED,
    check_inputs,
    prepare_handoff,
    release_handoff,
)

SYS = "0000000a-0000-4000-8000-000000000000"
LPAR = "0000000a-0000-4000-8000-0000000000aa"
VIOS = "0000000a-0000-4000-9000-000000000001"
KEY_BLOB = b"\x00\x00\x00\x0bssh-ed25519" + bytes(range(32))
KEY = "ssh-ed25519 " + base64.b64encode(KEY_BLOB).decode() + " lpar-a@sys-R1"
PRINT = "SHA256:" + base64.b64encode(hashlib.sha256(KEY_BLOB).digest()).decode().rstrip(
    "="
)
UNCHECKED = ("hmc_restore_lpar_profiles", "hmc_run_command")


def _partition() -> dict[str, Any]:
    return {
        "UUID": LPAR.upper(),
        "Resource": {
            "PartitionName": "lpar-a",
            "PartitionID": "7",
            "PartitionState": "running",
            "ResourceMonitoringControlState": "active",
            "Description": "owner=agent-z",
        },
    }


class _HMC:
    def __init__(self) -> None:
        self.config = object()
        self.adapters: list[dict[str, Any]] | Exception = [
            {
                "UUID": "AD-1",
                "Resource": {
                    "MACAddress": "AABBCCDDEEFF",
                    "PortVLANID": "1",
                    "VirtualSlotNumber": "4",
                },
            }
        ]

    async def list_logical_partitions(self, _uuid: str) -> list[dict[str, Any]]:
        return [_partition()]

    async def list_vios(self, _uuid: str) -> list[dict[str, Any]]:
        return [{"UUID": VIOS}]

    async def get_vios_storage_detail(self, _uuid: str) -> dict[str, Any]:
        return {"Resource": {}}

    async def list_adapters(
        self, _uuid: str, adapter_type: str, *, allow_empty_network: bool = False
    ) -> list[dict[str, Any]]:
        if isinstance(self.adapters, Exception):
            raise self.adapters
        return self.adapters


@pytest.fixture(autouse=True)
def _system(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve_system(_hmc: Any, selector: str) -> str:
        if selector not in ("sys-R1", SYS):
            raise ValueError(f"No managed system named {selector!r}")
        return SYS.upper()

    monkeypatch.setattr(handoff, "resolve_system_uuid", resolve_system)


class _Admit:
    def __init__(self, denied: frozenset[str] = frozenset()) -> None:
        self.denied = denied
        self.calls: list[tuple[str, dict[str, str | None]]] = []

    def __call__(self, name: str, by_kind: Mapping[str, str | None]) -> str | None:
        self.calls.append((name, dict(by_kind)))
        return f"{name} is withheld" if name in self.denied else None


def _prepare(agent: str = "agent-a", label: str | None = "kdive", **kw: Any):
    arguments: dict[str, Any] = {
        "connection": "<default>",
        "agent_id": agent,
        "admit": _Admit(),
        "system": "sys-R1",
        "lpar": "lpar-a",
        "consumer_label": label,
        "unchecked_tools": UNCHECKED,
    }
    arguments.update(kw)
    hmc = arguments.pop("hmc", _HMC())
    return asyncio.run(prepare_handoff(hmc, **arguments))


def _release(agent: str = "agent-a", hold_id: str | None = None):
    return asyncio.run(
        release_handoff(
            _HMC(), agent_id=agent, system="sys-R1", lpar="lpar-a", hold_id=hold_id
        )
    )


def _provision(arguments: dict[str, Any], *, pruned: bool = False) -> str:
    operation_id = "f" * 32
    with store.session() as conn, store.write_transaction(conn):
        store.insert_operation(
            conn,
            operation_id=operation_id,
            agent_id="agent-p",
            request_id="r1",
            tool="hmc_provision_lpar",
            connection="<default>",
            host="hmc.test",
            digest="d",
            request_json=json.dumps(
                {"tool": "hmc_provision_lpar", "arguments": arguments}
            ),
        )
        store.set_state(conn, operation_id, "terminal", "completed")
        store.add_ledger(
            conn,
            operation_id,
            key="partition",
            kind="partition",
            system_uuid=SYS,
            partition_uuid=LPAR.upper(),
            identity={"lpar_uuid": LPAR, "name": "lpar-a"},
        )
        if pruned:
            conn.execute(
                "DELETE FROM operations WHERE operation_id = ?", (operation_id,)
            )
    return operation_id


def test_prepare_with_hold_places_it_and_reveals_the_id_once():
    result = _prepare()
    placed = holds.find_hold(LPAR)
    assert placed is not None and result.action == "prepare"
    assert result.hold is not None and result.hold.hold_id == placed.hold_id
    assert result.document is not None and result.document.hold is not None
    assert result.document.hold.hold_id is None
    assert result.document.hold.consumer_label == "kdive"
    assert _prepare().hold.hold_id == placed.hold_id


def test_other_agent_sees_no_hold_id():
    _prepare()
    seen = _prepare(agent="agent-b", label=None)
    assert seen.hold is not None and seen.hold.agent_id == "agent-a"
    assert seen.hold.hold_id is None


def test_prepare_without_a_hold_places_nothing():
    result = _prepare(label=None)
    assert result.hold is None and result.document.hold is None
    assert holds.find_hold(LPAR) is None


def test_refusals_pass_through_and_place_nothing_on_failure():
    _prepare()
    with pytest.raises(OperationRefused) as held:
        _prepare(agent="agent-b")
    assert held.value.reason == "partition_held"
    holds.release_hold(SYS, LPAR, agent_id="agent-a")
    with store.session() as conn, store.write_transaction(conn):
        store.insert_operation(
            conn,
            operation_id="e" * 32,
            agent_id="agent-p",
            request_id="r2",
            tool="hmc_power_lpar",
            connection="<default>",
            host="hmc.test",
            digest="d",
            request_json="{}",
        )
        store.set_partition(conn, "e" * 32, SYS, LPAR)
    with pytest.raises(OperationRefused) as guarded:
        _prepare()
    assert guarded.value.reason == "partition_guarded"
    assert holds.find_hold(LPAR) is None
    with pytest.raises(ValueError, match="No managed system"):
        _prepare(system="sys-X")
    assert holds.find_hold(LPAR) is None


def test_document_groups_and_no_secrets():
    _provision(
        {
            "install": {
                "profile": "ubuntu-26.04.1",
                "network": {"address": "192.0.2.10/24", "routes": [], "dns": []},
                "media": {"mode": "built"},
                "ssh_authorized_keys": [KEY],
                "login_user": "ops",
            }
        }
    )
    document = _prepare().document
    assert [u.item for u in document.unverified] == list(UNVERIFIED)
    assert all("kdive" in u.statement for u in document.unverified)
    assert document.unfenced == list(UNFENCED)
    assert any("hmcpctl raw" in line for line in document.unfenced)
    assert document.unchecked_tools == list(UNCHECKED)
    assert (document.references.connection, document.references.agent_id) == (
        "<default>",
        "agent-a",
    )
    assert document.identity.partition_uuid == LPAR
    assert document.identity.system_uuid == SYS
    assert (document.identity.name, document.identity.partition_id) == ("lpar-a", 7)
    assert document.observed.state == "running"
    assert document.observed.rmc_state == "active"
    assert document.observed.resources.storage_source.status == "ok"
    text = json.dumps(dataclasses.asdict(document))
    assert KEY.split()[1] not in text and 'hold_id": "' not in text


_BUILT = {
    "profile": "ubuntu-26.04.1",
    "network": {
        "address": "192.0.2.10/24",
        "routes": [{"destination": "0.0.0.0/0", "gateway": "192.0.2.1"}],
        "dns": ["192.0.2.53"],
        "extra": "dropped",
    },
    "media": {"mode": "built"},
    "ssh_authorized_keys": [KEY, "not-a-key"],
    "login_user": "ops",
}


@pytest.mark.parametrize(
    ("arguments", "pruned", "expected"),
    [
        (
            {"install": _BUILT},
            False,
            {
                "source": "ok",
                "install_profile": "ubuntu-26.04.1",
                "login_user": "ops",
                "ssh_key_fingerprints": (PRINT,),
                "operation_state": "terminal",
                "operation_outcome": "completed",
            },
        ),
        (
            {"install": {**_BUILT, "media": {"mode": "prepared"}}},
            False,
            {"source": "ok", "login_user": None, "ssh_key_fingerprints": ()},
        ),
        ({"name": "lpar-a"}, False, {"source": "ok", "install_profile": None}),
        (None, False, {"source": "none", "operation_id": None}),
        ({"install": _BUILT}, True, {"source": "unavailable", "install_profile": None}),
    ],
    ids=["built", "prepared", "no-install", "no-entry", "pruned"],
)
def test_declared_from_the_provision_record(arguments, pruned, expected):
    if arguments is not None:
        _provision(arguments, pruned=pruned)
    declared = _prepare(label=None).document.declared
    for field, value in expected.items():
        assert getattr(declared, field) == value, field
    if arguments is not None and "install" not in arguments:
        assert declared.detail == "the provision installed no operating system"
    if arguments is not None and "install" in arguments and not pruned:
        assert declared.network == {
            "address": "192.0.2.10/24",
            "routes": [{"destination": "0.0.0.0/0", "gateway": "192.0.2.1"}],
            "dns": ["192.0.2.53"],
        }


def test_declared_reports_an_unreadable_store(monkeypatch):
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OperationRefused("store_corrupt", "bad")

    monkeypatch.setattr(store, "ledger_request", broken)
    _provision({"install": _BUILT})
    declared = _prepare(label=None).document.declared
    assert declared.source == "unavailable" and "bad" in (declared.detail or "")


def test_adapter_section_sources():
    admit = _Admit()
    adapters = _prepare(label=None, admit=admit).document.observed.adapters
    assert adapters.source.status == "ok"
    assert adapters.items == [
        {"mac": "aa:bb:cc:dd:ee:ff", "port_vlan_id": "1", "slot": "4"}
    ]
    assert ("hmc_list_adapters", {"lpar": "lpar-a", "managed_system": "sys-R1"}) in (
        admit.calls
    )
    denied = _prepare(
        label=None, admit=_Admit(frozenset({"hmc_list_adapters"}))
    ).document.observed.adapters
    assert (denied.source.status, denied.items) == ("denied", [])
    broken = _HMC()
    broken.adapters = HMCError("feed failed")
    failed = _prepare(label=None, hmc=broken).document.observed.adapters
    assert (
        failed.source.status == "unavailable" and failed.source.detail == "feed failed"
    )


def test_release_paths():
    placed = _prepare().hold
    other = _release(agent="agent-b", hold_id=placed.hold_id)
    assert other.action == "release" and other.document is None
    assert other.hold is not None and other.hold.hold_id is None
    assert holds.find_hold(LPAR) is None
    _prepare()
    with pytest.raises(OperationRefused) as mismatch:
        _release(agent="agent-b", hold_id="0" * 32)
    assert mismatch.value.reason == "hold_mismatch"
    assert _release().hold.consumer_label == "kdive"
    assert _release().hold is None


@pytest.mark.parametrize(
    ("action", "hold", "label", "hold_id", "message"),
    [
        ("release", True, None, None, "release takes no hold"),
        ("release", False, "kdive", None, "release takes no hold"),
        ("prepare", False, None, "x", "prepare is exempt"),
        ("prepare", True, None, None, "go together"),
        ("prepare", False, "kdive", None, "go together"),
    ],
)
def test_check_inputs_refuses_undefined_combinations(
    action, hold, label, hold_id, message
):
    with pytest.raises(ValueError, match=message):
        check_inputs(action, hold, label, hold_id)


def test_check_inputs_accepts_the_defined_combinations():
    check_inputs("prepare", True, "kdive", None)
    check_inputs("prepare", False, None, None)
    check_inputs("release", False, None, "x")


@pytest.mark.parametrize("stage", ["resources", "adapters"])
def test_pin_mismatch_prevents_handoff_hold(stage, monkeypatch):
    failure = HMCTLSFingerprintError("pin mismatch")
    hmc = _HMC()
    if stage == "resources":
        monkeypatch.setattr(hmc, "list_vios", AsyncMock(side_effect=failure))
    else:
        hmc.adapters = failure
    assert holds.find_hold(LPAR) is None
    with pytest.raises(HMCTLSFingerprintError) as caught:
        _prepare(hmc=hmc)
    assert caught.value is failure
    assert holds.find_hold(LPAR) is None
