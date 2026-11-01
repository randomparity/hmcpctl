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
from types import SimpleNamespace

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


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["create", "remove"])
@pytest.mark.parametrize("clock", ["wall", "monotonic"])
async def test_intent_save_crossing_time_boundary_refuses_physical_write(
    wire, packet, tmp_path, monkeypatch, intent, clock
):
    await prepare(wire, packet, tmp_path)
    now = {"wall": time.time(), "monotonic": time.monotonic()}
    monkeypatch.setattr(
        arm,
        "time",
        SimpleNamespace(time=lambda: now["wall"], monotonic=lambda: now["monotonic"]),
    )
    original = arm.save

    def delayed_save(directory, document):
        original(directory, document)
        if document["intents"] and document["intents"][-1] == intent:
            if clock == "wall":
                now["wall"] = packet["expires_at"]
            else:
                now["monotonic"] += arm.LIMITS["capture"][2] + 1

    monkeypatch.setattr(arm, "save", delayed_save)
    result = await arm.capture(wire.client(), packet, tmp_path / "capture")
    assert result["status"] == "MANUAL_RECOVERY"
    assert result["complete"] is False
    assert len(wire.writes) == (0 if intent == "create" else 1)
    assert wire.created is (intent == "remove")
    journal = json.loads((tmp_path / "capture" / "results.json").read_text())
    assert journal["intents"][-1] == intent


@pytest.mark.asyncio
async def test_dispatch_timeout_uses_time_remaining_after_intent_save(
    wire, packet, tmp_path, monkeypatch
):
    await prepare(wire, packet, tmp_path)
    now = [1000.0]
    monkeypatch.setattr(
        arm, "time", SimpleNamespace(time=time.time, monotonic=lambda: now[0])
    )
    original_save, original_ssh, timeout = arm.save, wire.ssh, arm.asyncio.timeout
    allowances, write_allowances = [], []
    saved_intents = set()

    def delayed_save(directory, document):
        original_save(directory, document)
        for intent in set(document["intents"]) - saved_intents:
            now[0] += 10
            saved_intents.add(intent)

    def observed_timeout(seconds):
        allowances.append(seconds)
        return timeout(seconds)

    async def observed_ssh(config, cmd, include_stderr, **kwargs):
        if cmd.startswith("chhwres "):
            write_allowances.append(allowances[-1])
        return await original_ssh(config, cmd, include_stderr, **kwargs)

    monkeypatch.setattr(arm, "save", delayed_save)
    monkeypatch.setattr(arm, "asyncio", SimpleNamespace(timeout=observed_timeout))
    monkeypatch.setattr(transport, "_run_hmc_command", observed_ssh)
    result = await arm.capture(wire.client(), packet, tmp_path / "capture")
    assert result["status"] == "CLEAN"
    assert write_allowances == [290, 280]
    assert result["counts"] == {"rest": 20, "ssh": 45}


@pytest.fixture
def source_packet(packet):
    result = deepcopy(packet)
    result.update(
        version=2,
        purpose="update-mode-source",
        case="vlan-replace-none",
        expected_lpar_state="not activated",
        allow_one_update=True,
    )
    result["policy"].update(allowed_vlan_ids=[2], allowed_os_mac_addrs=["001122334455"])
    result["policy"].pop("mac_addr")
    return result


class SourceWire(Wire):
    def __init__(self, packet):
        super().__init__(packet)
        self.state = "not activated"
        self.updated = False
        self.source_fault = None
        self.effect_counts = []
        self.source_values = {
            "allowed_vlan_ids": "2",
            "allowed_os_mac_addrs": "001122334455",
        }

    def http(self, request):
        if (
            self.updated
            and self.source_fault == "late_state"
            and self.created_reads >= 3
        ):
            self.state = "running"
        response = super().http(request)
        text = response.text
        if "</LogicalPartition>" in text:
            text = text.replace(
                "</LogicalPartition>",
                field("PartitionState", self.state) + "</LogicalPartition>",
            )
            return httpx.Response(response.status_code, text=text)
        return response

    async def ssh(
        self, config, command, include_stderr, *, shown, max_output_bytes=None
    ):
        tokens = shlex.split(command)
        if tokens[0] == "chhwres":
            self.effect_counts.append(
                (
                    tokens[tokens.index("-o") + 1],
                    len(self.requests),
                    len(self.commands) + 1,
                )
            )
        if (
            self.updated
            and self.source_fault == "late_owner"
            and self.created_reads >= 3
            and tokens[-1] == "description"
        ):
            self.commands.append(command)
            return self.description.replace("test-owner", "other-owner")
        if tokens[0] == "chhwres" and tokens[tokens.index("-o") + 1] == "s":
            assert max_output_bytes == arm.MIB
            self.commands.append(command)
            self.updated = True
            record = tokens[tokens.index("-a") + 1]
            name, value = record.split("=", 1)
            if self.source_fault == "refused":
                raise transport.HMCCLIError(
                    "synthetic refusal",
                    remote_nonzero_completed=True,
                    remote_refusal_code="HSCL1234",
                )
            if self.source_fault == "unknown":
                raise TimeoutError("synthetic uncertain update")
            if self.source_fault == "generic":
                raise transport.HMCCLIError("synthetic unstructured nonzero")
            key = name.removesuffix("-")
            self.source_values[key] = "none" if name.endswith("-") else value
            if self.source_fault in ("blank", "malformed", "mismatch"):
                self.source_values[key] = {
                    "blank": "",
                    "malformed": "bad=value",
                    "mismatch": "all",
                }[self.source_fault]
            return ""
        if (
            self.updated
            and self.source_fault == "preservation_timeout"
            and "vnicbkdev" in tokens
        ):
            self.commands.append(command)
            raise TimeoutError("synthetic backing observation failure")
        output = await super().ssh(
            config,
            command,
            include_stderr,
            shown=shown,
            max_output_bytes=max_output_bytes,
        )
        if not output or output == "No results were found." or "-F" not in tokens:
            return output
        reader = csv.DictReader(io.StringIO(output))
        rows = list(reader)
        names = reader.fieldnames
        if names is None or "slot_num" not in names:
            if (
                self.updated
                and self.source_fault == "backing"
                and names
                and "desired_capacity" in names
            ):
                rows[0]["desired_capacity"] = "3"
            else:
                return output
        else:
            for row in rows:
                row.update(self.source_values)
                if self.updated and self.source_fault == "unselected":
                    row["pvid_priority"] = "7"
                if self.updated and self.source_fault == "mac":
                    row["mac_addr"] = "aabbccddee02"
                if self.updated and self.source_fault == "slot":
                    row["slot_num"] = "9"
                if (
                    self.updated
                    and self.source_fault == "late_mac"
                    and self.created_reads >= 4
                ):
                    row["mac_addr"] = "aabbccddee02"
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue()


@pytest.fixture
def source_wire(monkeypatch, source_packet):
    wire = SourceWire(source_packet)
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


@pytest.mark.parametrize(
    "case,record",
    [
        ("vlan-replace-all", "allowed_vlan_ids=all"),
        ("vlan-replace-none", "allowed_vlan_ids=none"),
        ("vlan-remove-last", "allowed_vlan_ids-=2"),
        ("mac-replace-all", "allowed_os_mac_addrs=all"),
        ("mac-replace-none", "allowed_os_mac_addrs=none"),
        ("mac-remove-last", "allowed_os_mac_addrs-=001122334455"),
    ],
)
def test_source_packet_and_case(source_packet, case, record):
    source_packet["case"] = case
    arm.validate_packet(source_packet)
    command = arm._source_command(
        source_packet, {"system": "system-a", "lpar": "client-a"}, "2"
    )
    tokens = shlex.split(command)
    assert tokens[tokens.index("-a") + 1] == record
    assert tokens[tokens.index("-s") + 1] == "2"
    assert "--force" not in tokens and "--allownetdisrupt" not in tokens


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", True),
        ("case", []),
        ("case", "other"),
        ("purpose", "creation"),
        ("allow_one_update", 1),
        ("expected_lpar_state", ""),
        ("command", "chhwres"),
        ("target_slot", "2"),
        ("policy", {}),
        ("expires_at", False),
    ],
)
def test_source_invalid_packet(source_packet, key, value):
    source_packet[key] = value
    with pytest.raises((ValueError, TypeError)):
        arm.validate_packet(source_packet)


@pytest.mark.parametrize(
    "name,value",
    [
        ("allowed_vlan_ids", []),
        ("allowed_vlan_ids", [True]),
        ("allowed_vlan_ids", [1, 2]),
        ("allowed_vlan_ids", "none"),
        ("allowed_os_mac_addrs", None),
        ("pvid_priority", None),
        ("auto_priority_failover", 0),
        ("mac_addr", "001122334455"),
    ],
)
def test_source_invalid_policy(source_packet, name, value):
    source_packet["policy"][name] = value
    with pytest.raises((ValueError, TypeError)):
        arm.validate_packet(source_packet)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["", "running"])
async def test_source_state_binding(source_wire, source_packet, tmp_path, state):
    source_wire.state = state
    result = await arm.preflight(
        source_wire.client(), source_packet, tmp_path / "preflight"
    )
    assert result["status"] == "MANUAL_RECOVERY" and not source_wire.writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "vlan-replace-all",
        "vlan-replace-none",
        "vlan-remove-last",
        "mac-replace-all",
        "mac-replace-none",
        "mac-remove-last",
    ],
)
async def test_source_case_lifecycle(source_wire, source_packet, tmp_path, case):
    source_packet["case"] = case
    await prepare(source_wire, source_packet, tmp_path)
    result = await arm.capture(
        source_wire.client(), source_packet, tmp_path / "capture"
    )
    assert result["status"] == "CLEAN", result
    assert [
        shlex.split(c)[shlex.split(c).index("-o") + 1] for c in source_wire.writes
    ] == ["a", "s", "r"]
    assert result["source"]["submission"] == "completed"
    assert result["source"]["qualification"] == "qualified"
    assert (
        result["evidence_class"] == "source-only"
        and "optional_policy_verified" not in result
    )
    assert result["counts"] == {"rest": 23, "ssh": 50}
    assert source_wire.effect_counts == [("a", 10, 27), ("s", 13, 34), ("r", 19, 44)]
    assert len(source_wire.requests) == 23 and len(source_wire.commands) == 50
    recovered = await arm.recover(source_wire.client(), source_packet, result)
    assert recovered["status"] == "CLEAN"
    assert recovered["counts"] == {"rest": 5, "ssh": 4}
    assert json.loads((tmp_path / "preflight" / "results.json").read_text())[
        "counts"
    ] == {"rest": 8, "ssh": 15}
    assert len(source_wire.writes) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,submission,qualification",
    [
        ("refused", "rejected", "refused"),
        ("unknown", "unknown", "unresolved"),
        ("generic", "unknown", "unresolved"),
        ("blank", "completed", "unresolved"),
        ("malformed", "completed", "unresolved"),
        ("mismatch", "completed", "unresolved"),
        ("unselected", "completed", "unresolved"),
        ("mac", "completed", "unresolved"),
        ("slot", "completed", "unresolved"),
        ("backing", "completed", "unresolved"),
    ],
)
async def test_source_holds_cleanup(
    source_wire, source_packet, tmp_path, fault, submission, qualification
):
    await prepare(source_wire, source_packet, tmp_path)
    source_wire.source_fault = fault
    result = await arm.capture(
        source_wire.client(), source_packet, tmp_path / "capture"
    )
    assert result["status"] == "MANUAL_RECOVERY"
    assert result["source"]["submission"] == submission
    assert result["source"]["qualification"] == qualification
    assert len(source_wire.writes) == 2
    source_wire.created = False
    recovered = await arm.recover(source_wire.client(), source_packet, result)
    assert recovered["status"] == "MANUAL_RECOVERY" and len(source_wire.writes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["late_mac", "late_state", "late_owner"])
async def test_source_holds_late_removal_drift(
    source_wire, source_packet, tmp_path, fault
):
    await prepare(source_wire, source_packet, tmp_path)
    source_wire.source_fault = fault
    result = await arm.capture(
        source_wire.client(), source_packet, tmp_path / "capture"
    )
    assert result["source"]["qualification"] == "qualified"
    assert result["status"] == "MANUAL_RECOVERY" and len(source_wire.writes) == 2
    assert "remove" not in result["intents"]


@pytest.mark.asyncio
async def test_source_holds_partial_preservation(source_wire, source_packet, tmp_path):
    await prepare(source_wire, source_packet, tmp_path)
    source_wire.source_fault = "preservation_timeout"
    result = await arm.capture(
        source_wire.client(), source_packet, tmp_path / "capture"
    )
    source = result["source"]
    assert source["policy_verification"] == "verified"
    assert source["selected_after"]["allowed_vlan_ids"] == "none"
    assert source["preservation"] == "unavailable" and source["after_update"] is None
    assert source["qualification"] == "unresolved" and len(source_wire.writes) == 2


@pytest.mark.asyncio
async def test_source_history_recomputed(source_wire, source_packet, tmp_path):
    await prepare(source_wire, source_packet, tmp_path)
    result = await arm.capture(
        source_wire.client(), source_packet, tmp_path / "capture"
    )
    assert result["status"] == "CLEAN"
    for key in (
        "source",
        "create",
        "remove",
        "complete",
        "session",
        "case",
        "intents",
        "effect_commands",
        "removal_observations",
        "packet_sha256",
    ):
        changed = deepcopy(result)
        changed.pop(key)
        assert (await arm.recover(source_wire.client(), source_packet, changed))[
            "status"
        ] == "MANUAL_RECOVERY"
    for key in (
        "captured_slot",
        "native_record",
        "expected",
        "submission",
        "qualification",
        "before_update",
        "after_update",
    ):
        changed = deepcopy(result)
        changed["source"][key] = None
        assert (await arm.recover(source_wire.client(), source_packet, changed))[
            "status"
        ] == "MANUAL_RECOVERY"
    changed = deepcopy(result)
    changed["remove"]["vnic_before"][0]["policy"]["pvid_priority"] = 7
    assert (await arm.recover(source_wire.client(), source_packet, changed))[
        "status"
    ] == "MANUAL_RECOVERY"
    changed = deepcopy(result)
    changed["remove"]["selector"]["capacity_percent"] = "3"
    assert (await arm.recover(source_wire.client(), source_packet, changed))[
        "status"
    ] == "MANUAL_RECOVERY"
    assert len(source_wire.writes) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "update", "remove"])
@pytest.mark.parametrize("fault", ["expiry", "deadline", "journal", "overflow"])
async def test_source_intent_latency(
    source_wire, source_packet, tmp_path, monkeypatch, kind, fault
):
    client = source_wire.client()
    document = {"intents": []}
    output = tmp_path / "journal"
    output.mkdir(mode=0o700)
    now = {"wall": 1000.0, "monotonic": 1000.0}
    monkeypatch.setattr(
        arm,
        "time",
        SimpleNamespace(time=lambda: now["wall"], monotonic=lambda: now["monotonic"]),
    )
    source_packet["expires_at"] = 1300
    budget = arm._Budget(client, source_packet, "capture", document, output)
    original = arm.save
    original(output, document)
    preceding = (output / "results.json").read_bytes()

    def delayed(path, value):
        if fault == "journal":
            raise OSError("synthetic full journal")
        if fault == "overflow":
            value["padding"] = "x" * (5 * arm.MIB)
        original(path, value)
        if fault == "expiry":
            now["wall"] = 1301
        else:
            now["monotonic"] = 1280

    monkeypatch.setattr(arm, "save", delayed)
    command = "chhwres -r virtualio --rsubtype vnic -o s -m system-a -p client-a -s 2 -a allowed_vlan_ids=none"
    budget.arm(kind, command)
    budget.remove_reads = {"vnics", "backings", "target", "members", "description"}
    with arm._bounded(budget), pytest.raises((ValueError, OSError)):
        await transport.run_hmc_command(client.config, command)
    assert not source_wire.writes and not budget.dispatched
    assert budget.spent == {kind}
    if fault in ("journal", "overflow"):
        assert (output / "results.json").read_bytes() == preceding
    with pytest.raises(ValueError):
        budget.arm(kind, command)
    await client._http.aclose()


@pytest.mark.asyncio
async def test_source_logout_reserve(source_wire, source_packet, monkeypatch):
    client = source_wire.client()
    now = [1000.0]
    monkeypatch.setattr(arm.time, "monotonic", lambda: now[0])
    budget = arm._Budget(client, source_packet, "capture", {"intents": []}, None)
    now[0] = 1286.0
    with arm._bounded(budget):
        with pytest.raises(ValueError):
            await client._request("GET", f"/rest/api/uom/ManagedSystem/{SYS}")
        await client._request("DELETE", "/rest/api/web/Logon")
    assert budget.logged_off and len(source_wire.requests) == 1
    await client._http.aclose()


@pytest.mark.asyncio
async def test_source_physical_bounds(source_wire, source_packet):
    client = source_wire.client()
    budget = arm._Budget(client, source_packet, "capture", {"intents": []}, None)
    with arm._bounded(budget):
        for _ in range(47):
            await client._request("GET", f"/rest/api/uom/ManagedSystem/{SYS}")
        with pytest.raises(ValueError):
            await client._request("GET", f"/rest/api/uom/ManagedSystem/{SYS}")
        await client._request("DELETE", "/rest/api/web/Logon")
        for _ in range(56):
            await transport.run_hmc_command(client.config, "lshmc -V")
        with pytest.raises(ValueError):
            await transport.run_hmc_command(client.config, "lshmc -V")
    assert len(source_wire.requests) == 48 and len(source_wire.commands) == 56
    await client._http.aclose()


@pytest.mark.asyncio
async def test_source_physical_null_fallback(
    source_wire, source_packet, tmp_path, monkeypatch
):
    original = source_wire.http

    def expanded(request):
        path = request.url.path
        if path.endswith(f"/ManagedSystem/{SYS}"):
            source_wire.requests.append((request.method, path))
            return httpx.Response(500, text="Nested path contains null property")
        if "/ManagedSystem/search/" in path:
            source_wire.requests.append((request.method, path))
            return httpx.Response(
                200,
                text=feed(
                    [entry("ManagedSystem", SYS, field("SystemName", "system-a"))], path
                ),
            )
        return original(request)

    monkeypatch.setattr(source_wire, "http", expanded)
    await prepare(source_wire, source_packet, tmp_path)
    result = await arm.capture(
        source_wire.client(), source_packet, tmp_path / "capture"
    )
    assert result["status"] == "CLEAN", result
    assert result["counts"] == {"rest": 31, "ssh": 50}
    assert source_wire.effect_counts == [("a", 12, 27), ("s", 17, 34), ("r", 25, 44)]
    assert len(source_wire.requests) == 31


@pytest.mark.asyncio
@pytest.mark.parametrize("null_fallback", [False, True])
async def test_source_physical_ownership_fallback(
    source_wire, source_packet, tmp_path, monkeypatch, null_fallback
):
    original_http, original_ssh = source_wire.http, source_wire.ssh

    def failed_summary(request):
        path = request.url.path
        if null_fallback and path.endswith(f"/ManagedSystem/{SYS}"):
            source_wire.requests.append((request.method, path))
            return httpx.Response(500, text="Nested path contains null property")
        if null_fallback and "/ManagedSystem/search/" in path:
            source_wire.requests.append((request.method, path))
            return httpx.Response(
                200,
                text=feed(
                    [entry("ManagedSystem", SYS, field("SystemName", "system-a"))], path
                ),
            )
        if path.endswith("/ManagedSystem/quick/All"):
            resolving_null = null_fallback and source_wire.requests[-1][1].endswith(
                f"/ManagedSystem/{SYS}"
            )
            if not resolving_null:
                source_wire.requests.append((request.method, path))
                return httpx.Response(500, text="synthetic summary unavailable")
        return original_http(request)

    async def native_name(
        config, command, include_stderr, *, shown, max_output_bytes=None
    ):
        if command == "lssyscfg -r sys -F uuid,name":
            assert max_output_bytes == arm.MIB
            source_wire.commands.append(command)
            return f"{SYS},system-a"
        return await original_ssh(
            config,
            command,
            include_stderr,
            shown=shown,
            max_output_bytes=max_output_bytes,
        )

    monkeypatch.setattr(source_wire, "http", failed_summary)
    monkeypatch.setattr(transport, "_run_hmc_command", native_name)
    await prepare(source_wire, source_packet, tmp_path)
    result = await arm.capture(
        source_wire.client(), source_packet, tmp_path / "capture"
    )
    assert result["status"] == "CLEAN", result
    assert result["counts"] == {"rest": 31 if null_fallback else 23, "ssh": 53}
    assert source_wire.effect_counts == (
        [("a", 12, 29), ("s", 17, 36), ("r", 25, 47)]
        if null_fallback
        else [("a", 10, 29), ("s", 13, 36), ("r", 19, 47)]
    )
    assert source_wire.commands.count("lssyscfg -r sys -F uuid,name") == 3
    assert len(source_wire.writes) == 3 and result["session"]["logged_off"]


@pytest.mark.asyncio
async def test_source_physical_empty_feed(
    source_wire, source_packet, tmp_path, monkeypatch
):
    original = source_wire.http

    def empty(request):
        path = request.url.path
        if path.endswith(f"/ManagedSystem/{SYS}/LogicalPartition"):
            source_wire.requests.append((request.method, path))
            return httpx.Response(200, text=feed([], path))
        return original(request)

    monkeypatch.setattr(source_wire, "http", empty)
    result = await arm.preflight(
        source_wire.client(), source_packet, tmp_path / "preflight"
    )
    assert result["status"] == "MANUAL_RECOVERY" and not source_wire.writes
    assert result["counts"] == {"rest": 6, "ssh": 0}
    assert source_wire.requests[-1][0] == "DELETE"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "update", "remove"])
@pytest.mark.parametrize("counter", ["rest", "ssh"])
async def test_source_physical_suffix(
    source_wire, source_packet, tmp_path, kind, counter
):
    client = source_wire.client()
    output = tmp_path / "journal"
    output.mkdir(mode=0o700)
    budget = arm._Budget(client, source_packet, "capture", {"intents": []}, output)
    required = dict(zip(("rest", "ssh"), arm.SOURCE_SUFFIX[kind], strict=True))
    limit = budget.rest_limit if counter == "rest" else budget.ssh_limit
    budget.counts[counter] = limit - required[counter] + (0 if counter == "ssh" else 1)
    command = "chhwres -r virtualio --rsubtype vnic -o s -m system-a -p client-a -s 2 -a allowed_vlan_ids=none"
    budget.arm(kind, command)
    with arm._bounded(budget), pytest.raises(ValueError, match="reserves"):
        await transport.run_hmc_command(client.config, command)
    assert not source_wire.writes
    await client._http.aclose()


@pytest.mark.asyncio
async def test_source_logout_failure(source_wire, source_packet, tmp_path, monkeypatch):
    await prepare(source_wire, source_packet, tmp_path)
    original = source_wire.http

    def broken_logout(request):
        if request.method == "DELETE":
            source_wire.requests.append((request.method, request.url.path))
            return httpx.Response(500, text="synthetic logout failure")
        return original(request)

    monkeypatch.setattr(source_wire, "http", broken_logout)
    result = await arm.capture(
        source_wire.client(), source_packet, tmp_path / "capture"
    )
    assert result["status"] == "MANUAL_RECOVERY"
    assert result["source"]["qualification"] == "qualified"
    assert result["session"]["logged_off"] is False
    monkeypatch.setattr(source_wire, "http", original)
    assert (await arm.recover(source_wire.client(), source_packet, result))[
        "status"
    ] == "MANUAL_RECOVERY"


@pytest.mark.asyncio
async def test_source_intent_dispatch_timeout(
    source_wire, source_packet, tmp_path, monkeypatch
):
    await prepare(source_wire, source_packet, tmp_path)
    now = [1000.0]
    monkeypatch.setattr(
        arm, "time", SimpleNamespace(time=time.time, monotonic=lambda: now[0])
    )
    original_save, original_ssh, timeout = (
        arm.save,
        source_wire.ssh,
        arm.asyncio.timeout,
    )
    allowances, write_allowances, saved = [], [], set()

    def delayed_save(directory, document):
        original_save(directory, document)
        for intent in set(document["intents"]) - saved:
            now[0] += 10
            saved.add(intent)

    def observed_timeout(seconds):
        allowances.append(seconds)
        return timeout(seconds)

    async def observed_ssh(config, cmd, include_stderr, **kwargs):
        if cmd.startswith("chhwres "):
            write_allowances.append(allowances[-1])
        return await original_ssh(config, cmd, include_stderr, **kwargs)

    monkeypatch.setattr(arm, "save", delayed_save)
    monkeypatch.setattr(arm, "asyncio", SimpleNamespace(timeout=observed_timeout))
    monkeypatch.setattr(transport, "_run_hmc_command", observed_ssh)
    result = await arm.capture(
        source_wire.client(), source_packet, tmp_path / "capture"
    )
    assert result["status"] == "CLEAN"
    assert write_allowances == [275, 265, 255]
    assert result["session"]["logged_off"]
