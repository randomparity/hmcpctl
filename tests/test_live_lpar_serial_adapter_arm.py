"""ST63 finite procedure: independent observations and effects bounded by current authority."""

import csv
import hashlib
import io
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import lpar_serial_adapter as arm

SYSTEM = "11111111-1111-4111-8111-111111111111"
LOCAL = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def packet():
    return {
        "candidate": {
            "commit": "a" * 40,
            "tree": "b" * 40,
            "hmc_identity": {
                "host": "hmc.test",
                "port": 443,
                "user": "operator",
                "verify_ssl": True,
                "agent_id": "test",
            },
        },
        "expires_utc": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "system_uuid": SYSTEM,
        "partition_type": "AIX/Linux",
        "name": "hmcpctl-live-serial-12345678",
        "caller_token": "q1772-12345678",
        "cleanup_authorized": True,
    }


def csv_pair(name, native="9", **changes):
    fields = {
        "lpar_name": name,
        "lpar_id": native,
        "name": "default_profile",
        "max_virtual_slots": "8",
        "virtual_serial_adapters": f"2/server/0/{native}/{name}/3/0,3/client//{native}/{name}/2/0",
        "virtual_eth_adapters": "",
        "virtual_scsi_adapters": "",
        "virtual_fc_adapters": "",
        "vnic_adapters": "",
    } | changes
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(fields)
    writer.writerow(fields.values())
    return stream.getvalue()


def test_literal_pair_oracle():
    p = packet()
    assert arm.pair_oracle(csv_pair(p["name"]), p["name"], 9) is None
    for bad in (
        csv_pair(
            p["name"], virtual_serial_adapters="2/server/0/9//4/0,3/client//9//2/0"
        ),
        csv_pair(p["name"], virtual_serial_adapters="2/server/0/9//3/0"),
        csv_pair(p["name"], max_virtual_slots="7"),
        csv_pair(p["name"], virtual_eth_adapters="4/0/1//0/0"),
        csv_pair("other"),
        csv_pair(p["name"]) + csv_pair(p["name"]),
    ):
        with pytest.raises(ValueError):
            arm.pair_oracle(bad, p["name"], 9)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(extra=True),
        lambda p: p["candidate"].update(extra=True),
        lambda p: p["candidate"]["hmc_identity"].update(extra=True),
        lambda p: p["candidate"]["hmc_identity"].update(port=True),
        lambda p: p["candidate"]["hmc_identity"].update(verify_ssl="true"),
        lambda p: p["candidate"]["hmc_identity"].update(user=""),
        lambda p: p.update(cleanup_authorized=1),
        lambda p: p.update(partition_type="other"),
        lambda p: p.update(name="existing/peer"),
        lambda p: p.update(caller_token="bad token"),
    ],
)
def test_strict_packet(mutation):
    p = packet()
    mutation(p)
    with pytest.raises((ValueError, TypeError)):
        arm.validate_packet(p)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from hmcpctl.config import HMCConfig
    from hmcpctl.operations.lpar import serial

    p = packet()
    config = HMCConfig.from_mapping(p["candidate"]["hmc_identity"])
    monkeypatch.setattr(arm, "revision", lambda _: None)
    monkeypatch.setattr(
        serial, "resolve_system_cli_name", AsyncMock(return_value="9009-22A*TEST")
    )
    monkeypatch.setattr(
        arm,
        "read_role_capabilities",
        AsyncMock(return_value=SimpleNamespace(controller_mode=False)),
    )
    hmc = SimpleNamespace(config=config)
    r = SimpleNamespace(
        packet=p,
        hmc=hmc,
        effects=[],
        live=False,
        description="",
        fault=None,
        output=tmp_path / "result.json",
    )
    tmp_path.chmod(0o700)

    def entry():
        return {
            "UUID": LOCAL,
            "ResourceType": arm.kind(p),
            "Resource": {
                "PartitionName": p["name"],
                "PartitionID": "9",
                "PartitionType": p["partition_type"],
                "PartitionState": "not activated",
                "Description": r.description,
                "DefaultProfileName": "default_profile",
                "PartitionIOConfiguration": {"MaximumVirtualIOSlots": "8"},
                "PartitionMemoryConfiguration": {
                    k: "1024"
                    for k in ("MinimumMemory", "DesiredMemory", "MaximumMemory")
                },
                "PartitionProcessorConfiguration": {
                    "HasDedicatedProcessors": "true",
                    "DedicatedProcessorConfiguration": {
                        k: "1"
                        for k in (
                            "MinimumProcessors",
                            "DesiredProcessors",
                            "MaximumProcessors",
                        )
                    },
                },
            },
        }

    r.entry = entry

    async def logical(_):
        return [entry()] if r.live and arm.kind(p) == "LogicalPartition" else []

    async def vios(_):
        return [entry()] if r.live and arm.kind(p) == "VirtualIOServer" else []

    async def system(_):
        return {
            "UUID": SYSTEM,
            "Resource": {
                "State": "operating",
                "MachineTypeModelAndSerialNumber": {
                    "MachineType": "9009",
                    "Model": "22A",
                    "SerialNumber": "TEST",
                },
                "AssociatedSystemMemoryConfiguration": {
                    "CurrentAvailableSystemMemory": "3072" if r.live else "4096"
                },
                "AssociatedSystemProcessorConfiguration": {
                    "CurrentAvailableSystemProcessorUnits": "3" if r.live else "4"
                },
            },
        }

    async def get(kind, identity):
        assert kind == arm.kind(p) and identity == LOCAL
        return entry()

    async def command(config, text, **kwargs):
        assert text.startswith("lssyscfg -r prof ")
        if " --header" in text:
            if r.fault == "oracle":
                return csv_pair(p["name"], max_virtual_slots="7")
            return csv_pair(p["name"])
        if not r.live:
            return "No results were found."
        stream = io.StringIO()
        csv.writer(stream, lineterminator="\n").writerow(
            [
                "name=default_profile",
                f"lpar_name={p['name']}",
                "lpar_id=9",
                "max_virtual_slots=8",
                "virtual_serial_adapters=2/server/0/9//3/0,3/client//9//2/0",
            ]
        )
        return stream.getvalue()

    async def create(*args, **kwargs):
        r.effects.append("create")
        assert args[3] == p["partition_type"]
        assert args[4] == arm.RESOURCES
        assert kwargs["max_virtual_slots"] == 8
        selected = kwargs["virtual_serial_adapters"]
        assert [
            (a.local_slot, a.role, a.remote_slot, a.remote_lpar_name) for a in selected
        ] == [
            (2, "server", 3, p["name"]),
            (3, "client", 2, p["name"]),
        ]
        r.live = True
        if r.fault == "unknown":
            raise OSError("lost create response")
        return ""

    async def stamp(*args, **kwargs):
        r.effects.append("stamp")
        r.description = (
            f"[hmcpctl owner:test created:2026-10-26] [caller {p['caller_token']}]"
        )
        return None if r.fault == "stamp" else r.description

    async def delete(identity):
        assert identity == LOCAL
        r.effects.append("delete")
        if r.fault == "delete":
            raise OSError("lost delete response")
        r.live = False

    hmc.get_managed_system = system
    hmc.list_logical_partitions = logical
    hmc.list_vios = vios
    hmc.get_uom = get
    hmc.delete_logical_partition = delete
    hmc.delete_vios = delete
    monkeypatch.setattr(arm, "run_hmc_command", command)
    monkeypatch.setattr(arm, "create_lpar_via_cli", create)
    monkeypatch.setattr(arm, "stamp_lpar_ownership", stamp)
    r.window = lambda: arm.Window(p, "digest", "digest", True)

    async def execute(window=None):
        baseline = await arm.snapshot(hmc, p)
        preflight = {
            "baseline": baseline,
            "packet_sha256": "digest",
            "candidate": deepcopy(p["candidate"]),
            "captured_utc": datetime.now(UTC).isoformat(),
        }
        return await arm.execute(
            hmc,
            p,
            "digest",
            preflight,
            r.output,
            window or r.window(),
            tmp_path / "packet.json",
        )

    r.execute = execute
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ("AIX/Linux", "OS400", "Virtual IO Server"))
async def test_finite_three_environment_procedure_and_read_only_recovery(
    rig, environment
):
    rig.packet["partition_type"] = environment
    result = await rig.execute()
    assert result["complete"] is True
    assert result["phase"] == "complete"
    assert rig.effects == (
        ["create", "delete"]
        if environment == "Virtual IO Server"
        else ["create", "stamp", "delete"]
    )
    original = rig.output.read_bytes()
    effects = rig.effects.copy()
    await arm.recover(rig.hmc, rig.packet, result)
    assert rig.effects == effects
    assert rig.output.read_bytes() == original
    assert result["subtask"] == 63
    assert "verification" not in result and "evidence" not in result
    assert arm.read(rig.output) == result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,expected",
    [
        ("unknown", ["create"]),
        ("stamp", ["create", "stamp"]),
        ("oracle", ["create", "stamp"]),
        ("delete", ["create", "stamp", "delete"]),
    ],
)
async def test_uncertainty_stops_no_replay_or_compensation_and_partial_recovery(
    rig, fault, expected
):
    rig.fault = fault
    with pytest.raises((ValueError, OSError)):
        await rig.execute()
    assert rig.effects == expected
    before = rig.output.read_bytes()
    # Absence now cannot upgrade an incomplete intent or authorize cleanup.
    rig.live = False
    with pytest.raises(ValueError, match="incomplete"):
        await arm.recover(rig.hmc, rig.packet, arm.read(rig.output))
    assert rig.effects == expected
    assert rig.output.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing-grant",
        "wrong-grant",
        "exclusive",
        "cleanup",
        "expiry",
        "candidate",
        "identity",
    ],
)
async def test_admission_refuses_before_intent_and_effects(rig, monkeypatch, fault):
    window = rig.window()
    if fault == "missing-grant":
        window.grant = None
    elif fault == "wrong-grant":
        window.grant = "other"
    elif fault == "exclusive":
        window.exclusive = False
    elif fault == "cleanup":
        rig.packet["cleanup_authorized"] = False
    elif fault == "expiry":
        rig.packet["expires_utc"] = "2000-01-01T00:00:00+00:00"
    elif fault == "candidate":
        monkeypatch.setattr(
            arm, "revision", Mock(side_effect=ValueError("candidate drift"))
        )
    else:
        rig.hmc.config = rig.hmc.config.model_copy(update={"user": "different"})
    with pytest.raises((ValueError, PermissionError)):
        await rig.execute(window)
    assert rig.effects == []
    assert not rig.output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stamp-pending", "delete-pending"])
@pytest.mark.parametrize("fault", ["expiry", "candidate", "identity", "invocation"])
async def test_drift_before_next_effect_stops_all_subsequent_writes(
    rig, monkeypatch, phase, fault
):
    original = arm.write

    def write(path, document, **kwargs):
        original(path, document, **kwargs)
        if document["phase"] == phase:
            if fault == "expiry":
                rig.packet["expires_utc"] = "2000-01-01T00:00:00Z"
            elif fault == "candidate":
                monkeypatch.setattr(
                    arm, "revision", Mock(side_effect=ValueError("candidate drift"))
                )
            elif fault == "identity":
                rig.hmc.config = rig.hmc.config.model_copy(
                    update={"host": "changed.test"}
                )
            else:
                document["invocation"] = "restored"

    monkeypatch.setattr(arm, "write", write)
    with pytest.raises((ValueError, PermissionError)):
        await rig.execute()
    assert rig.effects == (
        ["create"] if phase == "stamp-pending" else ["create", "stamp"]
    )


@pytest.mark.asyncio
async def test_fresh_output_preserves_prior_intent(rig):
    rig.output.write_text("old intent")
    rig.output.chmod(0o600)
    with pytest.raises(ValueError, match="fresh"):
        await rig.execute()
    assert rig.output.read_text() == "old intent"
    assert rig.effects == []


@pytest.mark.parametrize(
    "fault", ["owner", "public-file", "public-parent", "symlink", "checkout"]
)
def test_private_input_refusal_preserves_bytes(tmp_path, monkeypatch, fault):
    tmp_path.chmod(0o700)
    path = tmp_path / "packet.json"
    path.write_text("preserve")
    path.chmod(0o600)
    if fault == "owner":
        monkeypatch.setattr(arm.os, "getuid", lambda: tmp_path.stat().st_uid + 1)
    elif fault == "public-file":
        path.chmod(0o644)
    elif fault == "public-parent":
        tmp_path.chmod(0o755)
    elif fault == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(path)
        path = alias
    else:
        monkeypatch.setattr(arm, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        arm.private_path(path)
    assert path.read_text() == "preserve"


@pytest.mark.parametrize(
    "fault",
    [
        "expiry",
        "identity",
        "candidate",
        "missing-grant",
        "wrong-grant",
        "exclusive",
        "cleanup",
    ],
)
def test_cli_pre_authentication_refusal(rig, monkeypatch, fault):
    p = rig.packet
    if fault == "expiry":
        p["expires_utc"] = "2000-01-01T00:00:00Z"
    elif fault == "identity":
        p["candidate"]["hmc_identity"]["host"] = "wrong.test"
    elif fault == "candidate":
        monkeypatch.setattr(
            arm, "revision", Mock(side_effect=ValueError("candidate drift"))
        )
    elif fault == "cleanup":
        p["cleanup_authorized"] = False
    path = rig.output.parent / "packet.json"
    path.write_text(json.dumps(p))
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(arm, "load_profile", lambda: rig.hmc.config)
    client = Mock(side_effect=AssertionError("must not authenticate"))
    monkeypatch.setattr(arm, "HMCClient", client)
    args = [
        "run",
        "--packet",
        str(path),
        "--packet-sha256",
        digest,
        "--preflight",
        str(path),
        "--results-file",
        str(rig.output),
    ]
    if fault != "missing-grant":
        args += ["--grant-packet-sha256", "wrong" if fault == "wrong-grant" else digest]
    if fault != "exclusive":
        args += ["--exclusive-writer-window"]
    assert arm.main(args) == 2
    client.assert_not_called()
    assert rig.effects == []


def test_full_saved_profiles_are_retained_and_incomplete_feeds_refused():
    records = arm.saved_profiles(
        "name=p,lpar_name=a,description=original\n", [{"name": "a"}]
    )
    assert records == [{"name": "p", "lpar_name": "a", "description": "original"}]
    for raw in ("", "name=p,lpar_name=a,name=q\n", "name=p,lpar_name=other\n"):
        with pytest.raises(ValueError):
            arm.saved_profiles(raw, [{"name": "a"}])


def test_cli_false_cleanup_allows_only_preflight(rig, monkeypatch):
    from contextlib import asynccontextmanager

    rig.packet["cleanup_authorized"] = False
    path = rig.output.parent / "packet.json"
    path.write_text(json.dumps(rig.packet))
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    @asynccontextmanager
    async def client(_):
        yield rig.hmc

    monkeypatch.setattr(arm, "load_profile", lambda: rig.hmc.config)
    monkeypatch.setattr(arm, "HMCClient", client)
    assert (
        arm.main(
            [
                "preflight",
                "--packet",
                str(path),
                "--packet-sha256",
                digest,
                "--output",
                str(rig.output),
            ]
        )
        == 0
    )
    assert rig.effects == []
    assert arm.read(rig.output)["packet_sha256"] == digest


@pytest.mark.parametrize("fault", ["head", "tree", "dirty", "source"])
def test_actual_candidate_provenance_guards(monkeypatch, fault, tmp_path):
    p = packet()
    root = tmp_path / "checkout"
    monkeypatch.setattr(arm, "ROOT", root)

    def command(args, **kwargs):
        assert kwargs["cwd"] == root
        if "HEAD" in args:
            return ("c" * 40 if fault == "head" else p["candidate"]["commit"]) + "\n"
        if "HEAD^{tree}" in args:
            return ("c" * 40 if fault == "tree" else p["candidate"]["tree"]) + "\n"
        return " M source.py\n" if fault == "dirty" else ""

    monkeypatch.setattr(arm.subprocess, "check_output", command)
    monkeypatch.setattr(
        arm.inspect,
        "getfile",
        lambda _: str(
            root
            / ("elsewhere.py" if fault == "source" else "src/hmcpctl/client/core.py")
        ),
    )
    with pytest.raises(ValueError, match="candidate"):
        arm.revision(p["candidate"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["controller", "capacity", "profile", "duplicate", "foreign"]
)
async def test_preflight_refuses_unavailable_or_changed_baselines(
    rig, monkeypatch, fault
):
    if fault == "controller":
        monkeypatch.setattr(
            arm,
            "read_role_capabilities",
            AsyncMock(return_value=SimpleNamespace(controller_mode=True)),
        )
    elif fault == "profile":
        rig.live = True
        monkeypatch.setattr(arm, "_command", AsyncMock(return_value=""))
    elif fault == "capacity":
        original = rig.hmc.get_managed_system

        async def system(identity):
            entry = await original(identity)
            entry["Resource"]["AssociatedSystemMemoryConfiguration"][
                "CurrentAvailableSystemMemory"
            ] = "512"
            return entry

        rig.hmc.get_managed_system = system
    elif fault == "duplicate":
        rig.live = True
        original = rig.hmc.list_logical_partitions

        async def duplicate(identity):
            rows = await original(identity)
            return rows + rows

        rig.hmc.list_logical_partitions = duplicate
    else:
        original = rig.hmc.get_managed_system

        async def system(identity):
            entry = await original(identity)
            entry["UUID"] = LOCAL
            return entry

        rig.hmc.get_managed_system = system
    with pytest.raises(ValueError):
        snapshot = await arm.snapshot(rig.hmc, rig.packet)
        arm.admission(snapshot, rig.packet)
    assert rig.effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["age", "digest", "candidate", "baseline"])
async def test_changed_or_stale_preflight_prevents_create(rig, fault):
    baseline = await arm.snapshot(rig.hmc, rig.packet)
    preflight = {
        "baseline": deepcopy(baseline),
        "packet_sha256": "digest",
        "candidate": deepcopy(rig.packet["candidate"]),
        "captured_utc": datetime.now(UTC).isoformat(),
    }
    if fault == "age":
        preflight["captured_utc"] = "2000-01-01T00:00:00+00:00"
    elif fault == "digest":
        preflight["packet_sha256"] = "different"
    elif fault == "candidate":
        preflight["candidate"]["commit"] = "c" * 40
    else:
        preflight["baseline"]["profiles"] = [{"name": "unrelated"}]
    with pytest.raises(ValueError):
        await arm.execute(
            rig.hmc,
            rig.packet,
            "digest",
            preflight,
            rig.output,
            rig.window(),
            rig.output.parent / "packet.json",
        )
    assert rig.effects == []
    assert not rig.output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["uuid", "state", "owner", "caller", "memory"])
async def test_observed_fixture_drift_never_authorizes_cleanup(rig, fault):
    original = rig.hmc.get_uom

    async def get(kind, identity):
        entry = await original(kind, identity)
        if rig.description:
            if fault == "uuid":
                entry["UUID"] = SYSTEM
            elif fault == "state":
                entry["Resource"]["PartitionState"] = "running"
            elif fault == "owner":
                entry["Resource"]["Description"] = (
                    "[hmcpctl owner:foreign created:2026-10-26]"
                )
            elif fault == "caller":
                entry["Resource"]["Description"] = (
                    "[hmcpctl owner:test created:2026-10-26] [caller other]"
                )
            else:
                entry["Resource"]["PartitionMemoryConfiguration"]["DesiredMemory"] = (
                    "2048"
                )
        return entry

    rig.hmc.get_uom = get
    with pytest.raises(ValueError):
        await rig.execute()
    assert rig.effects == ["create", "stamp"]
    assert arm.read(rig.output)["complete"] is False
