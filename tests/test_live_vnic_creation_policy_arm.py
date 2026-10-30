"""Synthetic transport proofs for the one-create, guarded-cleanup vNIC arm."""

from __future__ import annotations

import csv
import io
import json
import shlex
import sys
import time
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.ssh import transport

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import vnic_creation_policy as arm
from test_live_vios_ame_source_arm import entry, feed, field

SYS = "00000000-0000-4000-8000-000000000001"
LPAR = "00000000-0000-4000-8000-000000000003"
OWNER = "[hmcpctl owner:test-agent created:2026-10-30] [caller test-owner]"


@pytest.fixture
def packet():
    return {
        "version": 1,
        "configured_profile": "test",
        "hmc_identity": {"host": "hmc.test", "port": 443},
        "expected_commit": "a" * 40,
        "system_uuid": SYS,
        "lpar_uuid": LPAR,
        "expected_lpar_id": 3,
        "owner_token": "test-owner",
        "selector": {
            "vios_name": "vios-a",
            "vios_lpar_id": "100",
            "adapter_id": "1",
            "physical_port_id": "1",
            "capacity_percent": "2",
        },
        "port_vlan_id": 0,
        "policy": {
            "pvid_priority": 0,
            "auto_priority_failover": False,
            "allowed_vlan_ids": [2, 3],
            "allowed_os_mac_addrs": ["001122334455", "aabbccddeeff"],
            "mac_addr": "aabbccddee01",
        },
        "baseline_sha256": None,
        "expires_at": int(time.time()) + 600,
        "exclusive_writer_window": True,
        "allow_one_create": True,
        "allow_captured_slot_cleanup": True,
    }


class Wire:
    def __init__(self, packet):
        self.packet = packet
        self.created = False
        self.commands = []
        self.requests = []
        self.fault = None
        self.description = OWNER
        self.slot = "2"
        self.vnic_reads = 0
        self.created_reads = 0

    def http(self, request):
        self.requests.append((request.method, request.url.path))
        if request.url.path.endswith("/Logon"):
            return httpx.Response(
                200,
                text="<LogonResponse><X-API-Session>test-session</X-API-Session></LogonResponse>",
            )
        if self.fault == "large_response":
            return httpx.Response(200, content=b"x" * (arm.MIB + 1))
        if request.url.path.endswith("/ManagedSystem/quick/All"):
            return httpx.Response(
                200,
                json=[{"UUID": SYS, "SystemName": "system-a", "State": "operating"}],
            )
        system = entry("ManagedSystem", SYS, field("SystemName", "system-a"))
        lpar = entry(
            "LogicalPartition",
            LPAR,
            field("PartitionName", "client-a")
            + field("PartitionID", 3)
            + field("Description", self.description),
        )
        path = request.url.path
        if path.endswith(f"/ManagedSystem/{SYS}"):
            body = system
        elif path.endswith(f"/LogicalPartition/{LPAR}"):
            body = lpar
        elif path.endswith(f"/ManagedSystem/{SYS}/LogicalPartition"):
            body = feed([lpar], path)
        else:
            raise AssertionError(path)
        return httpx.Response(200, text=body)

    async def ssh(
        self, config, command, include_stderr, *, shown, max_output_bytes=None
    ):
        assert max_output_bytes == arm.MIB
        self.commands.append(command)
        tokens = shlex.split(command)
        if tokens[0] == "chhwres":
            operation = tokens[tokens.index("-o") + 1]
            self.created = operation == "a"
            if self.fault == "unknown_create" and operation == "a":
                raise TimeoutError("synthetic ambiguous completion")
            return ""
        if tokens[0] == "lshmc":
            return (
                "Version: 10\nRelease: 3\nService Pack: 1060"
                if self.fault != "ineligible"
                else "V11R2 M1110"
            )
        if tokens[-1] == "type_model":
            return "8375-42A"
        if tokens[-1] == "description":
            return self.description
        fields = tokens[tokens.index("-F") + 1].split(",")
        rows = []
        if tokens[0] == "lssyscfg":
            rows = [{"name": "vios-a", "lpar_id": "100", "lpar_env": "vioserver"}]
        else:
            subtype = tokens[tokens.index("--rsubtype") + 1]
            if subtype == "adapter":
                rows = [
                    {
                        "adapter_id": "1",
                        "config_state": "sriov",
                        "functional_state": "1",
                    }
                ]
            elif subtype == "physport" and tokens[tokens.index("--level") + 1] == "eth":
                rows = [
                    {
                        "adapter_id": "1",
                        "phys_port_id": "1",
                        "state": "1",
                        "min_eth_capacity_granularity": "1",
                    }
                ]
            elif subtype == "vnic":
                self.vnic_reads += 1
                if self.created:
                    self.created_reads += 1
                    policy = {
                        "pvid_priority": "0",
                        "auto_priority_failover": "0",
                        "allowed_vlan_ids": "2,3",
                        "allowed_os_mac_addrs": "001122334455,aabbccddeeff",
                        "mac_addr": "aabbccddee01",
                    }
                    if self.fault == "policy_mismatch":
                        policy["pvid_priority"] = "7"
                    if self.fault == "slot_replaced" and self.created_reads >= 2:
                        self.slot = "9"
                    rows = [
                        {
                            "lpar_name": "client-a",
                            "lpar_id": "3",
                            "slot_num": self.slot,
                            "port_vlan_id": "0",
                            "desired_mode": "ded",
                            "curr_mode": "ded",
                            **policy,
                            "backing_devices": "sriov/vios-a/100/1/1/3/2/2/50/100/100",
                            "backing_device_states": "sriov/3/1/Operational",
                        }
                    ]
            elif subtype == "vnicbkdev" and self.created:
                rows = [
                    {
                        "lpar_name": "vios-a",
                        "lpar_id": "100",
                        "type": "sriov",
                        "adapter_id": "1",
                        "physical_port_id": "1",
                        "logical_port_id": "3",
                        "capacity": "2",
                        "desired_capacity": "2",
                        "max_capacity": "100",
                        "desired_max_capacity": "100",
                        "failover_priority": "50",
                        "is_active": "1",
                        "status": "Operational",
                    }
                ]
            elif subtype == "logport" and self.created:
                rows = [
                    {
                        "adapter_id": "1",
                        "phys_port_id": "1",
                        "logical_port_id": "3",
                        "capacity": "2",
                    }
                ]
        if not rows:
            return "No results were found."
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(fields)
        writer.writerows([row.get(name, "") for name in fields] for row in rows)
        return output.getvalue()

    def config(self):
        return HMCConfig.from_mapping(
            {
                "host": "hmc.test",
                "port": 443,
                "user": "tester",
                "password": "p",
                "agent_id": "test-agent",
                "verify_ssl": True,
                "max_response_bytes": arm.MIB,
            }
        )

    def client(self):
        return HMCClient(self.config())

    @property
    def writes(self):
        return [command for command in self.commands if command.startswith("chhwres ")]


@pytest.fixture
def wire(monkeypatch, packet):
    wire = Wire(packet)
    monkeypatch.setattr(transport, "_run_hmc_command", wire.ssh)
    monkeypatch.setattr(
        HMCClient,
        "_new_http_client",
        lambda self, port: httpx.AsyncClient(
            base_url="https://hmc.test:443", transport=httpx.MockTransport(wire.http)
        ),
    )
    monkeypatch.setattr(arm, "_revision", lambda: ("a" * 40, True))
    return wire


async def prepare(wire, packet, tmp_path):
    result = await arm.preflight(wire.client(), packet, tmp_path / "preflight")
    assert result["status"] == "READY", result
    packet["baseline_sha256"] = result["baseline_sha256"]
    wire.commands.clear()
    wire.requests.clear()


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("expected_lpar_id", True),
        ("port_vlan_id", False),
        ("expires_at", 0),
        ("allow_one_create", False),
        ("expected_commit", "main"),
        ("owner_token", ""),
        ("baseline_sha256", "wrong"),
        ("surprise", True),
    ],
)
def test_packet_rejection(packet, field, value):
    packet[field] = value
    with pytest.raises(ValueError):
        arm.validate_packet(packet)


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [True, False])
async def test_one_create_guarded_remove_and_independent_recovery(
    wire, packet, tmp_path, selection
):
    if not selection:
        packet["policy"] = {}
    await prepare(wire, packet, tmp_path)
    result = await arm.capture(wire.client(), packet, tmp_path / "capture")
    assert result["status"] == "CLEAN", result
    assert len(wire.writes) == 2
    assert " -o a " in wire.writes[0] and " -o r " in wire.writes[1]
    assert result["optional_policy_verified"] is selection
    assert result["counts"]["rest"] == len(wire.requests) <= 48
    assert result["counts"]["ssh"] == len(wire.commands) <= 48
    recovery = await arm.recover(wire.client(), packet, result)
    assert recovery["status"] == "CLEAN", recovery
    assert len(wire.writes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["unknown_create", "policy_mismatch", "slot_replaced"]
)
async def test_uncertainty_never_arms_cleanup(wire, packet, tmp_path, fault):
    await prepare(wire, packet, tmp_path)
    wire.fault = fault
    result = await arm.capture(wire.client(), packet, tmp_path / "capture")
    assert result["status"] == "MANUAL_RECOVERY", result
    assert len(wire.writes) == 1
    recovery = await arm.recover(wire.client(), packet, result)
    assert recovery["status"] == "MANUAL_RECOVERY"
    assert len(wire.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["identity", "destination", "baseline", "revision", "journal"]
)
async def test_prewrite_failures_have_no_mutation(
    wire, packet, tmp_path, monkeypatch, failure
):
    await prepare(wire, packet, tmp_path)
    if failure == "identity":
        wire.description = "malformed [hmcpctl owner:test-agent]"
    elif failure == "destination":
        packet["hmc_identity"]["port"] = 12443
    elif failure == "baseline":
        packet["baseline_sha256"] = "b" * 64
    elif failure == "revision":
        monkeypatch.setattr(arm, "_revision", lambda: ("b" * 40, True))
    else:
        monkeypatch.setattr(
            arm, "save", lambda *args: (_ for _ in ()).throw(OSError("full"))
        )
    try:
        result = await arm.capture(wire.client(), packet, tmp_path / "capture")
        assert result["status"] != "CLEAN"
    except (ValueError, OSError):
        pass
    assert not wire.writes


@pytest.mark.asyncio
async def test_ineligible_preflight_skip(wire, packet, tmp_path):
    wire.fault = "ineligible"
    result = await arm.preflight(wire.client(), packet, tmp_path / "preflight")
    assert result["status"] == "SKIP"
    assert not wire.writes


@pytest.mark.asyncio
async def test_incomplete_or_changed_recovery_cannot_be_clean(wire, packet, tmp_path):
    await prepare(wire, packet, tmp_path)
    result = await arm.capture(wire.client(), packet, tmp_path / "capture")
    for key in ("complete", "create", "remove"):
        incomplete = deepcopy(result)
        incomplete.pop(key)
        assert (await arm.recover(wire.client(), packet, incomplete))[
            "status"
        ] == "MANUAL_RECOVERY"
    wire.created = True
    assert (await arm.recover(wire.client(), packet, result))[
        "status"
    ] == "MANUAL_RECOVERY"


@pytest.mark.asyncio
async def test_response_bound_rejects_inventory(wire, packet, tmp_path):
    wire.fault = "large_response"
    result = await arm.preflight(wire.client(), packet, tmp_path / "preflight")
    assert result["status"] != "READY"
    assert not wire.writes


def test_duplicate_packet_keys_and_nonfinite_are_rejected(tmp_path):
    path = tmp_path / "packet.json"
    for data in ('{"version":1,"version":1}', '{"expires_at":NaN}'):
        path.write_text(data)
        path.chmod(0o600)
        with pytest.raises(ValueError):
            arm._read_private(path, arm.MIB)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rest", "ssh"])
async def test_physical_request_budget_refuses_next_call(wire, packet, kind):
    client = wire.client()
    budget = arm._Budget(client, packet, "preflight", {"intents": []}, None)
    with arm._bounded(budget):
        if kind == "rest":
            for _ in range(23):
                await client._request("GET", f"/rest/api/uom/ManagedSystem/{SYS}")
            with pytest.raises(ValueError, match="count or deadline"):
                await client._request("GET", f"/rest/api/uom/ManagedSystem/{SYS}")
            assert len(wire.requests) == 23
            await client._request("DELETE", "/rest/api/web/Logon")
            assert len(wire.requests) == 24
        else:
            for _ in range(24):
                await transport.run_hmc_command(client.config, "lshmc -V")
            with pytest.raises(ValueError, match="count or deadline"):
                await transport.run_hmc_command(client.config, "lshmc -V")
            assert len(wire.commands) == 24
    await client._http.aclose()


@pytest.mark.asyncio
async def test_deadline_expiry_prevents_transport(wire, packet):
    client = wire.client()
    budget = arm._Budget(client, packet, "preflight", {"intents": []}, None)
    budget.deadline = time.monotonic() - 1
    with arm._bounded(budget), pytest.raises(ValueError, match="count or deadline"):
        await transport.run_hmc_command(client.config, "lshmc -V")
    assert not wire.commands
    await client._http.aclose()


@pytest.mark.asyncio
async def test_journal_intent_failure_prevents_create(
    wire, packet, tmp_path, monkeypatch
):
    await prepare(wire, packet, tmp_path)
    save = arm.save

    def fail_on_intent(path, document):
        if document["intents"]:
            raise OSError("synthetic journal disk full")
        save(path, document)

    monkeypatch.setattr(arm, "save", fail_on_intent)
    with pytest.raises(OSError):
        await arm.capture(wire.client(), packet, tmp_path / "capture")
    assert not wire.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("alteration", ["outcome", "policy", "after", "destination"])
async def test_recovery_refuses_contradictory_complete_journal(
    wire, packet, tmp_path, alteration
):
    await prepare(wire, packet, tmp_path)
    result = await arm.capture(wire.client(), packet, tmp_path / "capture")
    assert result["status"] == "CLEAN"
    if alteration == "outcome":
        result["remove"]["changed"] = None
    elif alteration == "policy":
        result["create"]["policy_verification"]["status"] = "unavailable"
    elif alteration == "after":
        result["after"]["identity"]["id"] = 99
    else:
        result["packet_sha256"] = "b" * 64
    recovery = await arm.recover(wire.client(), packet, result)
    assert recovery["status"] == "MANUAL_RECOVERY"


def test_packet_digest_and_private_file_bound_are_checked_before_client(
    packet, tmp_path, monkeypatch
):
    from unittest.mock import Mock

    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet))
    path.chmod(0o600)
    load = Mock(side_effect=AssertionError("profile load entered"))
    monkeypatch.setattr(arm, "load_profile", load)
    assert (
        arm.main(
            [
                "preflight",
                "--packet",
                str(path),
                "--packet-sha256",
                "b" * 64,
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 2
    )
    load.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_system_response_identity_prevents_effect(
    wire, packet, tmp_path, monkeypatch
):
    original = wire.http

    def wrong_identity(request):
        response = original(request)
        if request.url.path.endswith(f"/ManagedSystem/{SYS}"):
            return httpx.Response(200, text=response.text.replace(SYS, LPAR))
        return response

    monkeypatch.setattr(wire, "http", wrong_identity)
    result = await arm.preflight(wire.client(), packet, tmp_path / "preflight")
    assert result["status"] != "READY"
    assert not wire.writes


@pytest.mark.asyncio
async def test_unknown_remove_does_not_become_clean_from_absence(
    wire, packet, tmp_path, monkeypatch
):
    await prepare(wire, packet, tmp_path)
    original = wire.ssh

    async def unknown_remove(config, cmd, include_stderr, **kwargs):
        result = await original(config, cmd, include_stderr, **kwargs)
        if cmd.startswith("chhwres ") and " -o r " in cmd:
            raise TimeoutError("synthetic remove completion uncertainty")
        return result

    monkeypatch.setattr(transport, "_run_hmc_command", unknown_remove)
    result = await arm.capture(wire.client(), packet, tmp_path / "capture")
    assert result["status"] == "MANUAL_RECOVERY"
    assert result["partial"]["changed"] is True
    assert len(wire.writes) == 2 and not wire.created
    assert (await arm.recover(wire.client(), packet, result))[
        "status"
    ] == "MANUAL_RECOVERY"


@pytest.mark.asyncio
async def test_foreign_baseline_drift_prevents_cleanup(
    wire, packet, tmp_path, monkeypatch
):
    await prepare(wire, packet, tmp_path)
    original = wire.ssh

    async def drift(config, cmd, include_stderr, **kwargs):
        raw = await original(config, cmd, include_stderr, **kwargs)
        if "--rsubtype logport" in cmd and wire.created_reads >= 2:
            rows = list(csv.reader(io.StringIO(raw)))
            foreign = dict(zip(rows[0], rows[1], strict=True))
            foreign["logical_port_id"] = "9"
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerows(rows)
            writer.writerow(foreign[name] for name in rows[0])
            return output.getvalue()
        return raw

    monkeypatch.setattr(transport, "_run_hmc_command", drift)
    result = await arm.capture(wire.client(), packet, tmp_path / "capture")
    assert result["status"] == "MANUAL_RECOVERY"
    assert len(wire.writes) == 1


def test_real_parser_preflight_uses_profile_and_private_output(
    wire, packet, tmp_path, monkeypatch
):
    import hashlib
    from unittest.mock import Mock

    path = tmp_path / "packet.json"
    raw = json.dumps(packet).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    profile = Mock(return_value=wire.config())
    monkeypatch.setattr(arm, "load_profile", profile)
    output = tmp_path / "preflight"
    assert (
        arm.main(
            [
                "preflight",
                "--packet",
                str(path),
                "--packet-sha256",
                hashlib.sha256(raw).hexdigest(),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    profile.assert_called_once_with("test")
    document = json.loads((output / "results.json").read_text())
    assert document["status"] == "READY" and document["complete"] is True
    assert (output.stat().st_mode & 0o777) == 0o700
    assert ((output / "results.json").stat().st_mode & 0o777) == 0o600
    assert not wire.writes


@pytest.mark.asyncio
async def test_exact_mutation_allowance_is_spent_once(wire, packet, tmp_path):
    client = wire.client()
    budget = arm._Budget(client, packet, "capture", {"intents": []}, tmp_path)
    command = "chhwres -r virtualio --rsubtype vnic -o a -m system-a -p client-a -a port_vlan_id=0"
    with arm._bounded(budget):
        with pytest.raises(ValueError, match="exact allowance"):
            await transport.run_hmc_command(client.config, command)
        budget.arm("create", command)
        with pytest.raises(ValueError, match="exact allowance"):
            await transport.run_hmc_command(client.config, command + "x")
        await transport.run_hmc_command(client.config, command)
        with pytest.raises(ValueError, match="already used"):
            budget.arm("create", command)
        with pytest.raises(ValueError, match="exact allowance"):
            await transport.run_hmc_command(client.config, command)
    assert len(wire.writes) == 1
    assert json.loads((tmp_path / "results.json").read_text())["intents"] == ["create"]
    await client._http.aclose()


@pytest.mark.asyncio
async def test_expired_window_prevents_armed_mutation(wire, packet, tmp_path):
    client = wire.client()
    budget = arm._Budget(client, packet, "capture", {"intents": []}, tmp_path)
    packet["expires_at"] = int(time.time()) - 1
    command = "chhwres -r virtualio --rsubtype vnic -o a -m system-a -p client-a -a port_vlan_id=0"
    budget.arm("create", command)
    with arm._bounded(budget), pytest.raises(ValueError, match="exact allowance"):
        await transport.run_hmc_command(client.config, command)
    assert not wire.writes
    await client._http.aclose()


@pytest.mark.parametrize("excess", [0, 1])
def test_packet_byte_boundary(wire, packet, tmp_path, monkeypatch, excess):
    import hashlib
    from unittest.mock import Mock

    raw = json.dumps(packet).encode()
    raw += b" " * (arm.MIB + excess - len(raw))
    path = tmp_path / "packet.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    profile = Mock(return_value=wire.config())
    monkeypatch.setattr(arm, "load_profile", profile)
    code = arm.main(
        [
            "preflight",
            "--packet",
            str(path),
            "--packet-sha256",
            hashlib.sha256(raw).hexdigest(),
            "--output",
            str(tmp_path / "out"),
        ]
    )
    assert code == (2 if excess else 0)
    assert profile.call_count == (0 if excess else 1)
    assert not wire.writes
