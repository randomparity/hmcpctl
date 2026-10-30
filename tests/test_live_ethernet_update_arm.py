"""ST69 admission, physical bounds, retained evidence and read-only recovery."""

import copy
import hashlib
import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))


@pytest.fixture(autouse=True)
def require_arm():
    global arm
    assert (
        Path(__file__).parents[1] / "scripts/live_test/ethernet_update.py"
    ).exists(), "ST69 arm absent"
    arm = importlib.import_module("live_test.ethernet_update")


UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def packet():
    return {
        "schema_version": 1,
        "candidate": {"commit": "1" * 40, "tree": "2" * 40},
        "hmc_uuid": UUID,
        "system_uuid": UUID,
        "lpar_uuid": UUID,
        "adapter_uuid": UUID,
        "case": "qos-set",
        "expires_utc": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        "retained_change_authorized": True,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("case", "command"),
        ("hmc_uuid", "host"),
        ("system_uuid", "../"),
        ("retained_change_authorized", 1),
        ("expires_utc", "2020-01-01T00:00:00+00:00"),
        ("candidate", {"commit": "1" * 40}),
        ("unknown", True),
    ],
)
def test_bad_packet_refuses(field, value):
    value_packet = packet()
    value_packet[field] = value
    with pytest.raises((ValueError, TypeError)):
        arm.validate_packet(value_packet)


def test_packet_case_normalization_does_not_mutate_original():
    source = packet()
    source["hmc_uuid"] = UUID.upper()
    before = copy.deepcopy(source)
    normalized = arm.validate_packet(source)
    assert normalized["hmc_uuid"] == UUID
    assert source == before


def test_private_json_duplicate_and_bounds(tmp_path):
    tmp_path.chmod(0o700)
    source = tmp_path / "packet.json"
    source.write_text('{"case":"qos-set","case":"vsi-remove"}')
    source.chmod(0o600)
    with pytest.raises(ValueError, match="duplicate"):
        arm.read_private(source, arm.MIB)
    source.write_bytes(b" " * (arm.MIB + 1))
    with pytest.raises(ValueError):
        arm.read_private(source, arm.MIB)
    with pytest.raises(ValueError):
        arm.read_private(Path("relative.json"), arm.MIB)


def test_budget_reserves_logical_caps_and_original_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(arm.time, "monotonic", lambda: clock[0])
    budget = arm.Budget("run", packet()["expires_utc"])
    budget.dispatch(0, arm.REST_CAP + 7)
    assert budget.used == [1, 0, arm.REST_CAP + 7]
    clock[0] = budget.deadline - 1
    with pytest.raises(ValueError):
        budget.admit_effect()
    assert budget.used == [1, 0, arm.REST_CAP + 7]


@pytest.mark.parametrize("kind,size", [(0, 9 * 1024 * 1024), (1, 9 * 1024 * 1024)])
def test_budget_exhaustion_precedes_dispatch(kind, size):
    budget = arm.Budget("run", packet()["expires_utc"])
    with pytest.raises(ValueError):
        budget.dispatch(kind, size)
    assert budget.used == [0, 0, 0]


def test_packet_digest_and_paths_before_credentials(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    source = tmp_path / "packet.json"
    source.write_text(json.dumps(packet()))
    source.chmod(0o600)
    monkeypatch.setattr(
        arm, "load_profile", lambda *_: pytest.fail("credentials reached")
    )
    with pytest.raises(ValueError):
        arm.load_packet(source, "0" * 64)
    assert (
        arm.load_packet(source, hashlib.sha256(source.read_bytes()).hexdigest())["case"]
        == "qos-set"
    )


@pytest.fixture
def live_boundary(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import httpx
    from network.test_adapter_update import adapter

    from hmcpctl.client.core import HMCClient
    from hmcpctl.config import HMCConfig
    from hmcpctl.ssh import ethernet

    tmp_path.chmod(0o700)
    value_packet = packet()
    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "operator",
            "password": "private",  # pragma: allowlist secret — offline transport fixture only.
            "verify_ssl": True,
            "agent_id": "test-agent",
            "ssh_timeout": 10,
        }
    )
    partition = {
        "UUID": UUID,
        "ResourceType": "LogicalPartition",
        "Resource": {
            "PartitionName": "part-R1",
            "PartitionID": "7",
            "PartitionState": "running",
            "CurrentProfileSync": "On",
            "LastActivatedProfile": "default",
        },
    }
    rows = [adapter(UUID)]
    rows[0]["Resource"]["QualityOfServicePriority"] = "0"
    state = SimpleNamespace(
        writes=[], reads=[], rows=rows, packet=value_packet, root=tmp_path
    )
    monkeypatch.setattr(arm.files, "revision", lambda _: None)
    monkeypatch.setattr(arm, "load_profile", lambda _: config)

    async def transport(self, method, path, **kwargs):
        state.reads.append((method, path))
        body = (
            "<LogonResponse><X-API-Session>private-token</X-API-Session></LogonResponse>"
            if method == "PUT"
            else "<ok/>"
        )
        return httpx.Response(
            200, text=body, request=httpx.Request(method, "https://hmc.test" + path)
        )

    monkeypatch.setattr(HMCClient, "_request", transport)

    def api(name, value):
        async def read(self, *args, **kwargs):
            await self._request("GET", "/rest/api/uom/" + name)
            return copy.deepcopy(value() if callable(value) else value)

        return read

    monkeypatch.setattr(
        HMCClient,
        "list_uom",
        api(
            "ManagementConsole",
            [
                {
                    "UUID": UUID,
                    "Resource": {
                        "VersionInfo": {
                            "Version": "10",
                            "Release": "3",
                            "ServicePackName": "1060",
                        }
                    },
                }
            ],
        ),
    )
    monkeypatch.setattr(
        HMCClient,
        "list_system_summaries",
        api("ManagedSystem/quick/All", [{"UUID": UUID, "SystemName": "sys-R1"}]),
    )
    monkeypatch.setattr(
        HMCClient, "list_logical_partitions", api("LogicalPartition", [partition])
    )
    monkeypatch.setattr(
        HMCClient, "get_logical_partition", api("LogicalPartition/instance", partition)
    )
    monkeypatch.setattr(
        HMCClient,
        "_network_adapter_creation_feed",
        api("ClientNetworkAdapter", lambda: state.rows),
    )

    async def native(config, command, **kwargs):
        assert kwargs["max_output_bytes"] == arm.SSH_CAP
        if command.startswith("lssyscfg"):
            return "[hmcpctl owner:test-agent created:2026-10-30]"
        state.writes.append(command)
        state.rows[0]["Resource"]["QualityOfServicePriority"] = "3"
        return ""

    state.native = AsyncMock(side_effect=native)
    monkeypatch.setattr(ethernet, "run_hmc_command", state.native)
    return state


async def run_preflight(boundary):
    digest = "a" * 64
    destination = boundary.root / "preflight.json"
    document = await arm.preflight(boundary.packet, digest, destination)
    assert document["complete"], document
    return digest, destination, document


@pytest.mark.asyncio
async def test_real_operation_one_retained_write_and_readonly_recovery(live_boundary):
    digest, preflight_path, _ = await run_preflight(live_boundary)
    run_path = live_boundary.root / "run.json"
    document = await arm.execute(
        live_boundary.packet, digest, preflight_path, run_path, digest, True
    )
    assert document["complete"] and document["result"] == "retained_verified_change", (
        document
    )
    assert len(live_boundary.writes) == 1
    assert document["budget"]["used"][0] <= 24
    recovered = await arm.recover(run_path, live_boundary.root / "recovered.json")
    assert (
        recovered["complete"] and recovered["result"] == "retained_verified_change"
    ), recovered
    assert len(live_boundary.writes) == 1
    assert recovered["session"] == dict.fromkeys(arm.SESSION_KEYS, True)
    assert (
        recovered["original_results_sha256"]
        == hashlib.sha256(run_path.read_bytes()).hexdigest()
    )


@pytest.mark.asyncio
async def test_checkpoint_failure_stops_before_native_write(live_boundary, monkeypatch):
    digest, path, _ = await run_preflight(live_boundary)
    original = arm.checkpoint

    def checkpoint(destination, document, **kwargs):
        if document["intent"] is not None:
            raise OSError("checkpoint failure")
        return original(destination, document, **kwargs)

    monkeypatch.setattr(arm, "checkpoint", checkpoint)
    with pytest.raises(OSError):
        await arm.execute(
            live_boundary.packet,
            digest,
            path,
            live_boundary.root / "run.json",
            digest,
            True,
        )
    assert not live_boundary.writes
    # Intent was never durably armed, and own-token logout was still attempted.
    assert live_boundary.reads[-1] == ("DELETE", "/rest/api/web/Logon")


@pytest.mark.asyncio
@pytest.mark.parametrize("grant,exclusive", [("b" * 64, True), ("a" * 64, False)])
async def test_grant_admission_has_no_credentials(
    live_boundary, grant, exclusive, monkeypatch
):
    monkeypatch.setattr(
        arm, "load_profile", lambda _: pytest.fail("credentials reached")
    )
    with pytest.raises(ValueError):
        await arm.execute(
            live_boundary.packet,
            "a" * 64,
            live_boundary.root / "absent",
            live_boundary.root / "run",
            grant,
            exclusive,
        )
    assert not live_boundary.writes and not live_boundary.reads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "intent",
        "target",
        "binding",
        "snapshot",
        "submission",
        "session",
        "preflight_hash",
        "version",
        "complete",
    ],
)
async def test_corrupt_original_refuses_before_credentials(
    live_boundary, monkeypatch, corruption
):
    digest, path, _ = await run_preflight(live_boundary)
    run_path = live_boundary.root / "run.json"
    document = await arm.execute(
        live_boundary.packet, digest, path, run_path, digest, True
    )
    assert document["complete"]
    if corruption == "intent":
        document["intent"]["attributes"] = [["qos_priority", "7"]]
    elif corruption == "target":
        document["intent"]["adapter_uuid"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    elif corruption == "binding":
        document["binding"]["extra"] = "unexpected"
    elif corruption == "snapshot":
        document["baseline"]["slot"] = 9
    elif corruption == "submission":
        document["submission"]["submission"] = "invented"
    elif corruption == "session":
        document["session"]["logoff_acknowledged"] = False
    elif corruption == "preflight_hash":
        document["preflight_sha256"] = None
    elif corruption == "version":
        document["schema_version"] = True
    else:
        document["observations"]["postwrite"] = None
    arm.checkpoint(run_path, document)
    monkeypatch.setattr(
        arm, "load_profile", lambda _: pytest.fail("credentials reached")
    )
    with pytest.raises((ValueError, TypeError)):
        await arm.recover(run_path, live_boundary.root / "recovery.json")


@pytest.mark.asyncio
async def test_native_unknown_still_observes_and_never_retries(live_boundary):
    from hmcpctl.ssh.transport import HMCCLIError

    digest, path, _ = await run_preflight(live_boundary)
    native = live_boundary.native.side_effect

    async def uncertain(config, command, **kwargs):
        result = await native(config, command, **kwargs)
        if command.startswith("chhwres"):
            raise HMCCLIError("opaque timeout")
        return result

    live_boundary.native.side_effect = uncertain
    run_path = live_boundary.root / "run.json"
    document = await arm.execute(
        live_boundary.packet, digest, path, run_path, digest, True
    )
    assert document["submission"]["submission"] == "unknown"
    assert document["result"] == "unconfirmed"
    assert document["observations"]["postwrite"] is not None
    assert len(live_boundary.writes) == 1
    recovered = await arm.recover(run_path, live_boundary.root / "recovery.json")
    assert recovered["result"] == "unconfirmed"
    assert len(live_boundary.writes) == 1


@pytest.mark.asyncio
async def test_native_guard_rejects_different_or_second_write(live_boundary):
    from hmcpctl.config import HMCConfig

    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "u",
            "password": "p",
            "verify_ssl": True,
            "ssh_timeout": 10,
        }
    )
    hmc = arm.CountedHMC(
        config.model_copy(update={"max_response_bytes": arm.REST_CAP}),
        arm.Budget("run", packet()["expires_utc"]),
    )
    try:
        hmc.allowed_write = ["fixed", "command"]
        with pytest.raises(PermissionError):
            await hmc.native_write(hmc.config, "different command")
        await hmc.native_write(hmc.config, "fixed command")
        with pytest.raises(PermissionError):
            await hmc.native_write(hmc.config, "fixed command")
    finally:
        await hmc._http.aclose()


@pytest.mark.asyncio
async def test_logical_reservation_survives_real_rest_stream_overflow():
    import httpx
    from unit.test_response_limits import ObservedStream

    from hmcpctl.config import HMCConfig
    from hmcpctl.errors import HMCError

    stream = ObservedStream([b"x" * (arm.REST_CAP + 4096)])
    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "u",
            "password": "p",
            "verify_ssl": True,
            "max_response_bytes": arm.REST_CAP,
        }
    )
    budget = arm.Budget("preflight", packet()["expires_utc"])
    client = arm.CountedHMC(config, budget)
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="https://hmc.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    try:
        with pytest.raises(HMCError):
            await client._request("GET", "/rest/api/uom/ManagementConsole")
        assert budget.used == [1, 0, arm.REST_CAP]
        assert stream.yielded == 1 and stream.closed
        # The transport received a whole over-limit chunk; no cap+1 delivered-byte claim.
        assert len(stream.chunks[0]) > arm.REST_CAP + 1
    finally:
        await client._http.aclose()


@pytest.mark.asyncio
async def test_logical_reservation_survives_real_ssh_chunk_overflow(monkeypatch):
    from unit.test_ssh_output_limits import connection

    from hmcpctl.config import HMCConfig
    from hmcpctl.ssh.transport import HMCCLIError

    conn, process = connection(monkeypatch, b"x" * (arm.SSH_CAP + 4096))
    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "u",
            "password": "p",
            "verify_ssl": True,
            "ssh_timeout": 10,
        }
    )
    budget = arm.Budget("preflight", packet()["expires_utc"])
    hmc = arm.CountedHMC(
        config.model_copy(update={"max_response_bytes": arm.REST_CAP}), budget
    )
    hmc.allowed_read = ["fixed", "read"]
    try:
        with pytest.raises(HMCCLIError):
            await hmc.native_read(config, "fixed read")
        assert budget.used == [0, 1, arm.SSH_CAP + len("fixed read")]
        assert process.stdout.sizes[-1] == 4096
        conn.abort.assert_called_once()
    finally:
        await hmc._http.aclose()


@pytest.mark.parametrize("interrupt_spawn", [False, True])
def test_supervisor_kills_and_reaps_only_its_worker(monkeypatch, interrupt_spawn):
    import os
    import signal
    import subprocess
    import sys

    created = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        created.append(process)
        if interrupt_spawn:
            os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr(arm.subprocess, "Popen", spawn)
    assert arm.supervise([], 0.03) == 2
    assert len(created) == 1 and created[0].returncode is not None
    assert created[0].poll() is not None


@pytest.mark.asyncio
async def test_evidence_allowlist_and_exact_commit_binding(live_boundary):
    import live_test_evidence as evidence

    digest, path, _ = await run_preflight(live_boundary)
    run_path = live_boundary.root / "run.json"
    await arm.execute(live_boundary.packet, digest, path, run_path, digest, True)
    recovered = await arm.recover(run_path, live_boundary.root / "recovery.json")
    rendered = evidence.render(recovered)
    assert rendered is not None and "retained_verified_change" in rendered
    assert "1" * 40 in rendered and "qos-set" in rendered
    for value in (UUID, "sys-R1", "part-R1", "hmc.test", "operator", "020000000001"):
        assert value not in rendered
    assert "CLEAN" not in rendered
    broken = copy.deepcopy(recovered)
    broken["binding"]["candidate"]["commit"] = "not-a-commit"
    assert evidence.render(broken) is None
    broken = copy.deepcopy(recovered)
    broken["session"]["http_closed"] = False
    assert evidence.render(broken) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "tagging-enable",
        "tagging-disable",
        "vlan-replace",
        "vlan-add",
        "vlan-remove",
        "vlan-empty",
        "qos-set",
        "qos-disable",
        "vsi-set",
        "vsi-remove",
    ],
)
async def test_every_fixed_case_arms_only_its_record(live_boundary, case):
    live_boundary.packet["case"] = case
    if case == "tagging-disable":
        live_boundary.rows[0]["Resource"]["TaggedVLANIDs"] = ""
    digest, path, preflight = await run_preflight(live_boundary)
    run_path = live_boundary.root / "run.json"
    document = await arm.execute(
        live_boundary.packet, digest, path, run_path, digest, True
    )
    assert document["submission"] is not None, document
    _, expected = arm.configured(preflight["baseline"], live_boundary.packet)
    assert document["intent"]["attributes"] == [list(pair) for pair in expected]
    assert len(live_boundary.writes) == 1
    if case == "vsi-remove":
        assert document["result"] in ("unconfirmed", "observed_drift")
    assert "--force" not in live_boundary.writes[0]
    assert "--allownetdisrupt" not in live_boundary.writes[0]


@pytest.mark.asyncio
async def test_already_empty_case_has_no_write_and_no_live_change_claim(live_boundary):
    live_boundary.packet["case"] = "vlan-empty"
    live_boundary.rows[0]["Resource"]["TaggedVLANIDs"] = ""
    digest, path, _ = await run_preflight(live_boundary)
    run_path = live_boundary.root / "run.json"
    document = await arm.execute(
        live_boundary.packet, digest, path, run_path, digest, True
    )
    assert document["result"] == "no_write_verified"
    assert not live_boundary.writes
    recovered = await arm.recover(run_path, live_boundary.root / "recovery.json")
    assert recovered["result"] == "no_write_verified"


@pytest.mark.asyncio
async def test_slow_intent_checkpoint_rechecks_original_deadline(
    live_boundary, monkeypatch
):
    digest, path, _ = await run_preflight(live_boundary)
    original = arm.checkpoint
    clock = [arm.time.monotonic()]
    monkeypatch.setattr(arm.time, "monotonic", lambda: clock[0])

    def checkpoint(destination, document, **kwargs):
        original(destination, document, **kwargs)
        if document["intent"] is not None:
            clock[0] += 550

    monkeypatch.setattr(arm, "checkpoint", checkpoint)
    document = await arm.execute(
        live_boundary.packet,
        digest,
        path,
        live_boundary.root / "run.json",
        digest,
        True,
    )
    assert document["result"] == "unconfirmed"
    assert not live_boundary.writes


@pytest.mark.asyncio
async def test_postwrite_checkpoint_failure_retains_original_intent_and_logs_off(
    live_boundary, monkeypatch
):
    digest, path, _ = await run_preflight(live_boundary)
    original = arm.checkpoint

    def checkpoint(destination, document, **kwargs):
        if document["submission"] is not None:
            raise OSError("postwrite checkpoint failed")
        return original(destination, document, **kwargs)

    monkeypatch.setattr(arm, "checkpoint", checkpoint)
    run_path = live_boundary.root / "run.json"
    with pytest.raises(OSError):
        await arm.execute(live_boundary.packet, digest, path, run_path, digest, True)
    retained = arm.read_private(run_path)
    assert retained["intent"] is not None and retained["submission"] is None
    assert len(live_boundary.writes) == 1
    assert live_boundary.reads[-1] == ("DELETE", "/rest/api/web/Logon")


@pytest.mark.asyncio
async def test_missing_closeout_never_becomes_complete_or_retained(
    live_boundary, monkeypatch
):
    from hmcpctl.client.core import HMCClient
    from hmcpctl.errors import HMCTransportError

    digest, path, _ = await run_preflight(live_boundary)
    request = HMCClient._request

    async def fail_logout(self, method, route, **kwargs):
        if method == "DELETE":
            raise HMCTransportError("opaque")
        return await request(self, method, route, **kwargs)

    monkeypatch.setattr(HMCClient, "_request", fail_logout)
    document = await arm.execute(
        live_boundary.packet,
        digest,
        path,
        live_boundary.root / "run.json",
        digest,
        True,
    )
    assert document["complete"] is False
    assert document["session"]["logoff_acknowledged"] is False
    assert document["result"] != "retained_verified_change"


def test_packet_hash_binds_the_same_bytes_that_were_parsed(tmp_path, monkeypatch):
    from contextlib import contextmanager

    tmp_path.chmod(0o700)
    source = tmp_path / "packet.json"
    first = json.dumps(packet()).encode()
    second_packet = packet()
    second_packet["case"] = "qos-disable"
    second = json.dumps(second_packet).encode()
    source.write_bytes(first)
    source.chmod(0o600)
    original_open = Path.open
    swapped = []

    @contextmanager
    def changing_open(path, mode="r", *args, **kwargs):
        with original_open(path, mode, *args, **kwargs) as stream:
            yield stream
        if path == source and mode == "rb" and not swapped:
            swapped.append(True)
            with original_open(source, "wb") as stream:
                stream.write(second)

    monkeypatch.setattr(Path, "open", changing_open)
    with pytest.raises(ValueError, match="digest"):
        arm.load_packet(source, hashlib.sha256(second).hexdigest())


@pytest.mark.asyncio
async def test_interrupted_history_recovery_roundtrips_without_false_completion(
    live_boundary,
):
    digest, path, _ = await run_preflight(live_boundary)
    run_path = live_boundary.root / "run.json"
    original = await arm.execute(
        live_boundary.packet, digest, path, run_path, digest, True
    )
    original.update(submission=None, complete=False, result=None)
    original["observations"]["postwrite"] = None
    arm.checkpoint(run_path, original)
    recovered = await arm.recover(run_path, live_boundary.root / "recovery.json")
    assert recovered["result"] == "unconfirmed"
    assert recovered["complete"] is False
    assert (
        arm.validate_document(json.loads(json.dumps(recovered)), "recover") == recovered
    )
    assert len(live_boundary.writes) == 1


@pytest.mark.asyncio
async def test_recovery_known_drift_precedes_original_uncertainty(live_boundary):
    from hmcpctl.ssh.transport import HMCCLIError

    digest, path, _ = await run_preflight(live_boundary)
    native = live_boundary.native.side_effect

    async def uncertain(config, command, **kwargs):
        result = await native(config, command, **kwargs)
        if command.startswith("chhwres"):
            raise HMCCLIError("opaque timeout")
        return result

    live_boundary.native.side_effect = uncertain
    run_path = live_boundary.root / "run.json"
    original = await arm.execute(
        live_boundary.packet, digest, path, run_path, digest, True
    )
    assert original["result"] == "unconfirmed"
    live_boundary.rows[0]["Resource"]["MACAddress"] = "020000000099"
    recovered = await arm.recover(run_path, live_boundary.root / "recovery.json")
    assert recovered["result"] == "observed_drift"
    assert recovered["submission"]["submission"] == "unknown"
    assert len(live_boundary.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["system_name", "slot", "hmc_version"])
async def test_observed_identity_drift_roundtrips_through_recovery_and_evidence(
    live_boundary, monkeypatch, drift
):
    import live_test_evidence as evidence

    from hmcpctl.client.core import HMCClient

    original_summaries = HMCClient.list_system_summaries
    original_console = HMCClient.list_uom

    async def summaries(self):
        result = await original_summaries(self)
        if drift == "system_name" and live_boundary.writes:
            result[0]["SystemName"] = "sys-R2"
        return result

    async def console(self, *args):
        result = await original_console(self, *args)
        if drift == "hmc_version" and live_boundary.writes:
            result[0]["Resource"]["VersionInfo"]["ServicePackName"] = "1061"
        return result

    monkeypatch.setattr(HMCClient, "list_system_summaries", summaries)
    monkeypatch.setattr(HMCClient, "list_uom", console)
    native = live_boundary.native.side_effect

    async def changed(config, command, **kwargs):
        result = await native(config, command, **kwargs)
        if drift == "slot" and command.startswith("chhwres"):
            live_boundary.rows[0]["Resource"]["VirtualSlotNumber"] = "6"
        return result

    live_boundary.native.side_effect = changed
    digest, path, _ = await run_preflight(live_boundary)
    run_path = live_boundary.root / "run.json"
    document = await arm.execute(
        live_boundary.packet, digest, path, run_path, digest, True
    )
    assert document["complete"] and document["result"] == "observed_drift"
    recovered = await arm.recover(run_path, live_boundary.root / "recovery.json")
    assert recovered["result"] == "observed_drift"
    assert recovered["binding"] == document["binding"]
    assert (
        arm.validate_document(json.loads(json.dumps(recovered)), "recover") == recovered
    )
    rendered = evidence.render(recovered)
    assert rendered is not None and "observed_drift" in rendered
    assert "sys-R1" not in rendered and "sys-R2" not in rendered
    assert len(live_boundary.writes) == 1


async def healthy(boundary):
    digest, path, _ = await run_preflight(boundary)
    run_path = boundary.root / "run.json"
    run = await arm.execute(boundary.packet, digest, path, run_path, digest, True)
    recovery = await arm.recover(run_path, boundary.root / "recovery.json")
    assert run["complete"] and recovery["result"] == "retained_verified_change"
    return run_path, run, recovery


def corrupt(document, case):
    value = document["submission"]
    witness = value["verification"]
    if case == "nonempty_no_write":
        value.update(
            submission="not_dispatched",
            mutation_dispatched=False,
            reason="already_satisfied",
        )
        document["result"] = "no_write_verified"
    elif case == "expected":
        witness["expected"] = {"qos_priority": 7}
    elif case == "observed":
        witness["observed"] = {"qos_priority": 0}
    elif case == "mismatch_list":
        witness["mismatched_fields"] = ["qos_priority"]
    elif case == "unavailable_type":
        witness["unavailable_fields"] = "malformed"
    elif case == "preservation":
        value["preservation"]["changed_adapter_uuids"] = [
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        ]
    elif case == "boolean_number":
        witness["observed"] = {"qos_priority": True}
    else:
        value["errors"] = [17]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "nonempty_no_write",
        "expected",
        "observed",
        "mismatch_list",
        "unavailable_type",
        "preservation",
        "boolean_number",
        "error_type",
    ],
)
async def test_corrupt_history_refuses_in_both_readers(
    live_boundary, monkeypatch, case
):
    import live_test_evidence as evidence

    run_path, run, recovery = await healthy(live_boundary)
    corrupt(run, case)
    corrupt(recovery, case)
    arm.checkpoint(run_path, run)
    assert evidence.render(recovery) is None
    monkeypatch.setattr(
        arm, "load_profile", lambda _: pytest.fail("credentials reached")
    )
    with pytest.raises((ValueError, TypeError)):
        await arm.recover(run_path, live_boundary.root / "bad-recovery.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disappeared", "inventory_growth"])
async def test_positive_inventory_drift_is_retained(live_boundary, change):
    import live_test_evidence as evidence

    run_path, run, _ = await healthy(live_boundary)
    if change == "disappeared":
        live_boundary.rows.clear()
    else:
        for index in range(1, 10):
            row = copy.deepcopy(live_boundary.rows[0])
            row["UUID"] = f"{index:08x}-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            row["Resource"]["VirtualSlotNumber"] = str(4 + index)
            live_boundary.rows.append(row)
    recovery = await arm.recover(run_path, live_boundary.root / "changed-recovery.json")
    assert recovery["result"] == "observed_drift"
    assert recovery["observations"]["recovery"] is not None
    assert recovery["binding"] == run["binding"]
    assert (
        arm.validate_document(json.loads(json.dumps(recovery)), "recover") == recovery
    )
    if change == "disappeared":
        assert recovery["observations"]["recovery"]["slot"] is None
        assert recovery["binding"]["slot"] == 4
    assert "observed_drift" in evidence.render(recovery)
    assert len(live_boundary.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["baseline_absent", "prewrite_absent", "absent_nonnull", "present_null"]
)
async def test_absence_policy_does_not_weaken_admission_or_invent_slot(
    live_boundary, case
):
    _, _, document = await healthy(live_boundary)
    if case == "baseline_absent":
        selected = document["baseline"]
    elif case == "prewrite_absent":
        selected = document["observations"]["prewrite"]
    else:
        selected = document["observations"]["recovery"]
    if case != "present_null":
        selected["adapters"] = []
    if case != "absent_nonnull":
        selected["slot"] = None
    with pytest.raises((ValueError, TypeError)):
        arm.validate_document(document, "recover")
