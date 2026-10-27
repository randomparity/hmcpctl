"""Boundary proofs for the fixed source-only diagnostic; native data is synthetic."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shlex
import sys
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio

from hmcpctl.config import HMCConfig
from hmcpctl.ssh.transport import HMCCLIError

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import vios_ame_source as ame
from live_test import vios_boot_source as source
from test_live_vios_ame_source_arm import SYS, V0, Wire

VALUES = [
    "c09-plain",
    "c09,comma",
    "c09=equals",
    "c09'single",
    'c09"double',
    "c09\\slash",
    "c09 space",
    "c09,='\"\\ combined",
]
GOLDEN = [
    "lpar_id=3,boot_string=c09-plain",
    'lpar_id=3,"boot_string=c09,comma"',
    "lpar_id=3,boot_string=c09=equals",
    "lpar_id=3,boot_string=c09'single",
    'lpar_id=3,"boot_string=c09""double"',
    "lpar_id=3,boot_string=c09\\slash",
    "lpar_id=3,boot_string=c09 space",
    'lpar_id=3,"boot_string=c09,=\'""\\ combined"',
]
OWN = "00000000-0000-4000-8000-000000000003"


def packet(tmp_path):
    directory = tmp_path / "window"
    directory.mkdir(mode=0o700)
    nonce = "1" * 32
    return {
        "schema_version": 1,
        "configured_profile": "test",
        "hmc_identity": {
            "host": "hmc.test",
            "port": 443,
            "user": "tester",
            "verify_ssl": True,
        },
        "system": {"uuid": SYS, "name": "sys-R1"},
        "tested_commit": "a" * 40,
        "tested_tree": "b" * 40,
        "imported_source_sha256": {p: "c" * 64 for p in source.SOURCE_PATHS},
        "fixture": {
            "name": "c09v-" + nonce[:24],
            "caller_nonce": nonce,
            "description": f"[hmcpctl owner:source-test created:2026-10-27] [caller {nonce}]",
        },
        "resources": deepcopy(ame.RESOURCES),
        "encoding_id": "csv-minimal-two-attribute-v1",
        "values": VALUES.copy(),
        "baseline_sha256": None,
        "window": {
            "directory": str(directory),
            "not_before": int(time.time()) - 1,
            "expires_at": int(time.time()) + 7198,
        },
        "budgets": {
            "stages": {k: list(v) for k, v in source.LIMITS.items()},
            "rows": {
                "regular": 128,
                "vios": 8,
                "profiles": 32,
                "pools": 4,
                "with_own_vios": 9,
                "with_own_profiles": 33,
            },
        },
        "grant_binding": {
            "agent_id": "source-test",
            "created_date": "2026-10-27",
            "grant_id": "test-window-1",
        },
    }


def identity(p):
    return {
        "system_uuid": SYS,
        "vios_uuid": OWN,
        "name": p["fixture"]["name"],
        "partition_id": 3,
        "description": p["fixture"]["description"],
    }


def native(p, pending="", **changes):
    cells = {
        "name": p["fixture"]["name"],
        "lpar_id": "3",
        "lpar_env": "vioserver",
        "uuid": OWN,
        "description": p["fixture"]["description"],
        "pend_boot_string": pending,
        "state": "Not Activated",
        "uptime": "0",
        "boot_string": "",
    }
    cells.update(changes)
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(cells)
    writer.writerow(cells.values())
    return out.getvalue()


def test_packet_closed_and_strict(tmp_path):
    p = packet(tmp_path)
    source.validate_packet(p)
    for key, value in [
        ("extra", True),
        ("schema_version", True),
        ("values", VALUES[::-1]),
        ("encoding_id", "other"),
        ("resources", {**p["resources"], "min_memory": True}),
    ]:
        q = deepcopy(p)
        q[key] = value
        with pytest.raises(ValueError):
            source.validate_packet(q)
    for key in ["port", "verify_ssl"]:
        q = deepcopy(p)
        q["hmc_identity"][key] = 1 if key == "verify_ssl" else True
        with pytest.raises(ValueError):
            source.validate_packet(q)
    q = deepcopy(p)
    q["budgets"]["rows"]["vios"] = True
    with pytest.raises(ValueError):
        source.validate_packet(q)
    q = deepcopy(p)
    q["imported_source_sha256"].pop(next(iter(q["imported_source_sha256"])))
    with pytest.raises(ValueError):
        source.validate_packet(q)
    with pytest.raises(ValueError):
        source._json(b'{"x":1,"x":2}')
    with pytest.raises(ValueError):
        source._json(b'{"x":NaN}')


@pytest.mark.parametrize("i", range(8))
def test_candidate_golden_shell_and_csv(i):
    record = source.encode_boot_record(3, i)
    assert record == GOLDEN[i]
    words = shlex.split("chsyscfg -r lpar -m sys-R1 -i " + shlex.quote(record))
    assert words == ["chsyscfg", "-r", "lpar", "-m", "sys-R1", "-i", GOLDEN[i]]
    assert next(csv.reader([words[-1]], strict=True)) == [
        "lpar_id=3",
        "boot_string=" + VALUES[i],
    ]


@pytest.mark.parametrize(
    "pid,index",
    [(True, 0), (0, 0), (-1, 0), ("3", 0), (3, True), (3, -1), (3, 8), (3, "x")],
)
def test_hostile_candidate(pid, index):
    with pytest.raises(ValueError):
        source.encode_boot_record(pid, index)


@pytest.mark.parametrize("pending", VALUES + [""])
def test_header_exact_literals(tmp_path, pending):
    p = packet(tmp_path)
    assert (
        source.parse_native_header(native(p, pending), identity(p))["pend_boot_string"]
        == pending
    )


@pytest.mark.parametrize("variant", [str.lower, str.upper, str.swapcase])
def test_header_uuid_case_preserves_native_text(tmp_path, variant):
    p = packet(tmp_path)
    ident = identity(p)
    ident["vios_uuid"] = "abcdef01-2345-6789-abcd-0123456789ab"
    observed = variant("aBcDeF01-2345-6789-AbCd-0123456789aB")
    assert (
        source.parse_native_header(native(p, uuid=observed), ident)["uuid"] == observed
    )


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "ragged",
        "extra",
        "missing",
        "control",
        "stderr",
        "replacement",
        "identity",
        "uuid_shape",
        "name_case",
        "quote",
    ],
)
def test_header_rejects_malformed(tmp_path, change):
    p = packet(tmp_path)
    raw = native(p)
    if change == "duplicate":
        raw = raw.replace("uptime", "name", 1)
    if change == "ragged":
        raw = raw.rstrip("\n") + ",extra\n"
    if change == "extra":
        raw += raw.splitlines()[1] + "\n"
    if change == "missing":
        raw = raw.replace("pend_boot_string", "unknown")
    if change == "control":
        raw = raw.replace("Not Activated", "Not\x00Activated")
    if change == "stderr":
        raw += "\n[stderr]\nwarning"
    if change == "replacement":
        raw = raw.replace("Not Activated", "\ufffd")
    if change == "identity":
        raw = native(p, uuid=V0)
    if change == "uuid_shape":
        raw = native(p, uuid="z" * 36)
    if change == "name_case":
        raw = native(p, name=identity(p)["name"].upper())
    if change == "quote":
        raw = raw.replace("Not Activated", '"unclosed')
    with pytest.raises(ValueError):
        source.parse_native_header(raw, identity(p))


def test_private_paths_receipt_and_input(tmp_path):
    p = packet(tmp_path)
    directory = Path(p["window"]["directory"])
    output = directory / "preflight-initial"
    ame._receipt(p, "preflight-initial", "a" * 64, output)
    with pytest.raises(ValueError):
        ame._receipt(p, "preflight-initial", "a" * 64, output)
    f = directory / "packet"
    f.write_text("{}")
    f.chmod(0o644)
    with pytest.raises(ValueError):
        source._read_private(f, 65536)
    f.chmod(0o600)
    link = directory / "link"
    link.symlink_to(f)
    with pytest.raises(ValueError):
        source._read_private(link, 65536)
    f.write_text(" " * 65537)
    with pytest.raises(ValueError):
        source._read_private(f, 65536)


def test_filesystem_session_bound_and_failed_checkpoint(tmp_path, monkeypatch):
    with pytest.raises(ame.CheckpointError):
        source._save(tmp_path, {"raw": "x" * (1024 * 1024)}, "session.json")
    (tmp_path / "results.json").write_text("old intent")
    monkeypatch.setattr(ame.os, "fsync", Mock(side_effect=OSError("synthetic fsync")))
    with pytest.raises(ame.CheckpointError):
        source._save(tmp_path, {"phase": "new"})
    assert (tmp_path / "results.json").read_text() == "old intent"


def test_attestation_exact_selected_closure(tmp_path, monkeypatch):
    p = packet(tmp_path)
    assert len(source.SOURCE_PATHS) == 20
    assert set(source.SOURCE_PATHS) == (
        set(ame.SOURCE_PATHS) - {"scripts/live_vios_ame_source.py"}
        | {"scripts/live_test/vios_boot_source.py", "scripts/live_vios_boot_source.py"}
    )
    monkeypatch.setattr(
        ame,
        "_git_read",
        lambda root, args: "changed" if args == ("rev-parse", "HEAD") else "",
    )
    with pytest.raises(ValueError):
        source._attest(p)


def config(p):
    return HMCConfig.from_mapping(
        {
            **p["hmc_identity"],
            "password": "test",  # pragma: allowlist secret -- synthetic
            "agent_id": "source-test",
        }
    )


def test_caps_precharge_errors_and_nested(tmp_path):
    p = packet(tmp_path)
    b = source.BootSourceBudget("capture", p)
    assert b.maximum == (830, 40, 42 * 1024 * 1024, 2600)
    with b.section("test", 1, 0, 1024, 20):
        b.dispatch(0, "GET")
        b.charge(1024)
        with pytest.raises(ValueError):
            b.dispatch(0, "GET")
        with pytest.raises(ValueError), b.section("nested", 1, 0, 1024, 20):
            pass
    assert b.used == [1, 0, 1024]


@pytest.mark.parametrize(
    "cursor,index",
    [
        ("before-create", 0),
        ("before-boot-observations", 0),
        ("before-boot-write", 7),
        ("before-cleanup-observations", 0),
        ("before-delete", 0),
    ],
)
def test_reserve_suffix_and_expiry(tmp_path, cursor, index):
    p = packet(tmp_path)
    b = source.BootSourceBudget("capture", p)
    b.reserve(cursor, index)
    for dimension in range(3):
        b.used = [0, 0, 0]
        b.used[dimension] = b.maximum[dimension]
        with pytest.raises(ValueError):
            b.reserve(cursor, index)
    b.used = [0, 0, 0]
    b.deadline = time.monotonic()
    with pytest.raises(ValueError):
        b.reserve(cursor, index)


@pytest.mark.parametrize(
    "cursor,index",
    [
        ("other", 0),
        ("before-create", 1),
        ("before-boot-write", True),
        ("before-boot-write", 8),
    ],
)
def test_reserve_closed_cursor(tmp_path, cursor, index):
    with pytest.raises(ValueError):
        source.BootSourceBudget("capture", packet(tmp_path)).reserve(cursor, index)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", list(source.LIMITS))
async def test_cleanup_physical_guard_before_parent(tmp_path, monkeypatch, stage):
    p = packet(tmp_path)
    h = source.BootSourceHMC(config(p), source.BootSourceBudget(stage, p))
    sends = []

    async def request(self, method, path, **kwargs):
        sends.append(path)
        return httpx.Response(204)

    monkeypatch.setattr(ame.HMCClient, "_request", request)
    h.delete_uuid = OWN
    try:
        with (
            h.budget.section("delete", 1, 0, 1024 * 1024, 20),
            pytest.raises(ValueError),
        ):
            await h._request("DELETE", "/rest/api/uom/VirtualIOServer/" + OWN)
        assert sends == [] and h.delete_count == 0
    finally:
        await h._http.aclose()


def test_reuse_globals_unchanged():
    assert ame.LIMITS["capture"] == (418, 12, 20 * 1024 * 1024, 1130)
    assert issubclass(source.BootSourceBudget, ame.Budget)
    assert issubclass(source.BootSourceHMC, ame.CountedHMC)
    assert source.BootSourceBudget.reserve is not ame.Budget.reserve


class BootWire(Wire):
    """Synthetic HTTP and SSH only; shared snapshot and real scoped deleter run unchanged."""

    def __init__(self, p):
        super().__init__(p)
        self.partition_id = 3
        self.pending = ""
        self.boots = []
        self.events = []
        self.boot_fault = None
        self.drift = None

    def vios(self, ident):
        raw = super().vios(ident)
        raw = raw.replace(
            "<PartitionMemoryConfiguration><DesiredMemory>2048</DesiredMemory></PartitionMemoryConfiguration>",
            "<PartitionMemoryConfiguration><MinimumMemory>1024</MinimumMemory><DesiredMemory>2048</DesiredMemory><MaximumMemory>4096</MaximumMemory></PartitionMemoryConfiguration>",
        )
        raw = raw.replace(
            "<PartitionProcessorConfiguration><DesiredVirtualProcessors>1</DesiredVirtualProcessors></PartitionProcessorConfiguration>",
            "<PartitionProcessorConfiguration><HasDedicatedProcessors>false</HasDedicatedProcessors><SharingMode>uncapped</SharingMode><SharedProcessorConfiguration><MinimumProcessingUnits>0.1</MinimumProcessingUnits><DesiredProcessingUnits>0.5</DesiredProcessingUnits><MaximumProcessingUnits>1.0</MaximumProcessingUnits><MinimumVirtualProcessors>1</MinimumVirtualProcessors><DesiredVirtualProcessors>1</DesiredVirtualProcessors><MaximumVirtualProcessors>2</MaximumVirtualProcessors></SharedProcessorConfiguration></PartitionProcessorConfiguration>",
        )
        if ident == OWN:
            raw = raw.replace(">3</PartitionID>", f">{self.partition_id}</PartitionID>")
        if ident == OWN and self.drift == "resource":
            raw = raw.replace("<MaximumMemory>4096", "<MaximumMemory>8192")
        return raw

    def system(self):
        raw = super().system()
        if OWN in self.nodes:
            raw = raw.replace("32768", "30720").replace(
                "<CurrentAvailableSystemProcessorUnits>4",
                "<CurrentAvailableSystemProcessorUnits>3.5",
            )
        if self.boots and self.drift == "capacity":
            raw = raw.replace("30720", "30464")
        return raw

    def request(self, request):
        response = super().request(request)
        self.events.append(("REST", request.method, request.url.path))
        if (
            self.boots
            and self.drift == "etag"
            and "/LogicalPartitionProfile/" in request.url.path
        ):
            response.headers["ETag"] = "changed"
        return response

    async def ssh(self, config, command, **kwargs):
        words = shlex.split(command)
        self.events.append(("SSH", words[0]))
        if words[0] == "chsyscfg":
            self.commands.append(command)
            pairs = dict(
                c.split("=", 1) for c in next(csv.reader([words[-1]], strict=True))
            )
            assert pairs["lpar_id"] == str(self.partition_id)
            index = len(self.boots)
            self.boots.append(pairs["boot_string"])
            if self.boot_fault == ("refusal", index):
                raise HMCCLIError("synthetic refusal", remote_refusal_code="HSCL1234")
            if self.boot_fault == ("unknown-before", index):
                raise HMCCLIError("synthetic unknown before apply")
            self.pending = pairs["boot_string"]
            if self.boot_fault == ("unknown", index):
                raise HMCCLIError("synthetic unknown")
            if self.boot_fault == ("warning", index):
                return "synthetic warning"
            if self.boot_fault == ("mismatch", index):
                self.pending = "different"
            return ""
        if words[0] == "lssyscfg" and words[-1] == "--header":
            self.commands.append(command)
            assert words[-2] == "-F" and words[-3] == f"lpar_ids={self.partition_id}"
            pending = (
                "different"
                if self.drift == "pending-pre" and self.boots
                else self.pending
            )
            return native(
                self.p,
                pending,
                lpar_id=str(self.partition_id),
                uptime="1" if self.boots and self.drift == "uptime" else "0",
            )
        return await super().ssh(config, command, **kwargs)


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    p = packet(tmp_path)
    wire = BootWire(p)
    monkeypatch.setattr(source, "run_hmc_command", wire.ssh)
    monkeypatch.setattr(ame, "run_hmc_command", wire.ssh)
    monkeypatch.setattr(source, "_attest", lambda p: None)
    monkeypatch.setattr(
        ame.HMCClient,
        "_new_http_client",
        lambda self, port: httpx.AsyncClient(
            base_url="https://hmc.test:443", transport=httpx.MockTransport(wire.request)
        ),
    )
    h = source.BootSourceHMC(config(p), source.BootSourceBudget("capture", p))
    h.packet_sha = "d" * 64
    before = await ame.snapshot(h, p)
    p["baseline_sha256"] = ame.digest(before["stable"])
    h.budget = source.BootSourceBudget("capture", p)
    h.packet = p
    h.grant_sha = h.packet_sha
    h.exclusive = True
    h.deletion_approved = True
    output = Path(p["window"]["directory"]) / "capture"
    output.mkdir(mode=0o700)
    # Production predecessor is tested independently; lifecycle starts after that boundary.
    monkeypatch.setattr(source, "_predecessor", lambda *args: None, raising=False)
    wire.http.clear()
    wire.commands.clear()
    wire.events.clear()
    wire.collection_hits.clear()
    try:
        yield p, wire, h, output, before
    finally:
        await h._http.aclose()


async def run_capture(rig):
    p, _wire, h, output, _before = rig
    with h.budget.section("logon", 1, 0, source.MIB, 20):
        await h.logon()
    return await source.capture(
        h,
        p,
        output,
        grant_packet_sha256=h.packet_sha,
        exclusive_writer_window=True,
        fresh_vios_delete_approved=True,
    )


@pytest.mark.asyncio
async def test_ordered_complete_capture_snapshot_and_preservation(rig):
    _p, w, h, out, before = rig
    result = await run_capture(rig)
    assert result["phase"] == "deleted-observed" and result["run"]["partial"] is False
    assert w.creates == 1 and w.boots == VALUES and w.deletes == [OWN]
    assert len(result["snapshots"]) == 11
    assert (
        result["snapshots"][0]["stable"]
        == before["stable"]
        == result["snapshots"][-1]["stable"]
    )
    b1 = result["snapshots"][1]["stable"]
    assert all(s["stable"] == b1 for s in result["snapshots"][2:-1])
    assert [c["record"] for c in result["cases"]] == GOLDEN
    assert all(
        c["observation"] == "exact" and c["execution"] == "acknowledged"
        for c in result["cases"]
    )
    assert h.delete_count == 1 and h.boot_sent == 8 and h.create_sent
    assert json.loads((out / "results.json").read_text()) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["etag", "uptime", "capacity", "pending-pre"])
async def test_preservation_drift_holds_all_effects(rig, drift):
    _p, w, _h, _out, _ = rig
    w.drift = drift
    result = await run_capture(rig)
    assert result["run"]["partial"] is True and result["phase"] == "held"
    assert len(w.boots) <= 1 and not w.deletes and OWN in w.nodes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["resource", "running", "rest-marker", "foreign-drift", "missing-ame"]
)
async def test_resource_unproved_fixture_prevents_boot(rig, fault):
    _p, w, _h, _out, _ = rig
    if fault == "resource":
        w.drift = fault
    else:
        w.fault = fault
    result = await run_capture(rig)
    assert result["run"]["partial"] is True and not w.boots and not w.deletes


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["create-unknown", "delete-unknown"])
async def test_uncertainty_resource_effect_not_replayed(rig, fault):
    _p, w, _h, _out, _ = rig
    w.fault = fault
    result = await run_capture(rig)
    assert result["phase"] == "held" and result["run"]["partial"] is True
    assert w.creates == 1 and len(w.deletes) <= 1
    if fault == "create-unknown":
        assert not w.boots and not w.deletes
    else:
        assert result["deletion"]["execution"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(8))
@pytest.mark.parametrize("kind", ["unknown", "unknown-before", "mismatch"])
async def test_uncertainty_each_boot_boundary(rig, kind, index):
    _p, w, _h, _out, _ = rig
    w.boot_fault = (kind, index)
    result = await run_capture(rig)
    assert result["phase"] == "held" and result["run"]["partial"] is True
    assert len(w.boots) == index + 1 and w.deletes == [] and OWN in w.nodes
    assert result["cases"][-1]["execution"] == (
        "unknown" if kind.startswith("unknown") else "acknowledged"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(8))
async def test_refusal_boot_stops_and_conditional_cleanup(rig, index):
    _p, w, _h, _out, _ = rig
    w.boot_fault = ("refusal", index)
    result = await run_capture(rig)
    assert result["run"]["partial"] is True and len(w.boots) == index + 1
    assert result["cases"][-1]["execution"] == "rejected"
    assert (
        w.deletes == [OWN] and result["deletion"]["observation"]["outcome"] == "exact"
    )


@pytest.mark.asyncio
async def test_refusal_create_has_no_cleanup_right(rig):
    _p, w, _h, _out, _ = rig
    w.fault = "refusal"
    result = await run_capture(rig)
    assert result["create"]["execution"] == "rejected" and not w.boots and not w.deletes
    assert result["snapshots"][-1]["stable"] == result["snapshots"][0]["stable"]


@pytest.mark.asyncio
@pytest.mark.parametrize("count,profiles,expected", [(8, 4, 64), (9, 0, 68)])
async def test_maxima_real_snapshot_counts(rig, count, profiles, expected):
    p, w, h, _out, _ = rig
    w.nodes = {
        f"00000000-0000-4000-8000-{i + 2:012x}": {
            "name": f"foreign-{i}",
            "description": "foreign",
            "factor": "0",
        }
        for i in range(count)
    }
    w.profile_count = 4
    if count == 9:
        original = w.profile_ids
        w.profile_ids = lambda ident: (
            original(ident)[:1] if ident.endswith("00000000000a") else original(ident)
        )
    h.budget = source.BootSourceBudget("capture", p)
    await ame.snapshot(h, p)
    assert h.budget.used[:2] == [expected, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["next", "extension", "truncated", "http-link", "range", "type"]
)
async def test_snapshot_complete_collection_guards(rig, fault):
    p, w, h, _out, _ = rig
    path = f"/rest/api/uom/ManagedSystem/{SYS}/VirtualIOServer"
    w.envelope_fault = (path, 1, fault)
    with pytest.raises(ValueError):
        await ame.snapshot(h, p)
    assert w.injected == 1 and not w.boots and not w.deletes


def session(p, stage="capture", document=None):
    close = {
        "schema_version": 1,
        "source_only": True,
        "packet_sha256": "d" * 64,
        "stage": stage,
        "logon": "acknowledged",
        "logoff": "acknowledged",
        "http": "closed",
        "own_process_pid": None,
        "transport": {
            "used": [2, 0, 0],
            "calls": [
                {"kind": "REST", "label": "PUT /rest/api/web/Logon"},
                {"kind": "REST", "label": "DELETE /rest/api/web/Logon"},
            ],
        },
        "closed": True,
    }
    if document is not None:
        close["transport"] = deepcopy(document["transport"])
        close["transport"]["calls"].append(
            {"kind": "REST", "label": "DELETE /rest/api/web/Logon"}
        )
        close["transport"]["used"][0] += 1
    return close


@pytest.mark.asyncio
async def test_history_rederives_every_witness(rig):
    p, _w, h, _out, _ = rig
    document = await run_capture(rig)
    assert source._history_complete(p, document, h.packet_sha)
    edits = [
        lambda d: d["cases"].reverse(),
        lambda d: d["cases"].pop(),
        lambda d: d["cases"][0].update(record="changed"),
        lambda d: d["cases"][0]["native_readback"]["after"]["cells"].update(
            pend_boot_string="changed"
        ),
        lambda d: d["snapshots"][2]["stable"]["vios"][OWN]["profiles"][
            next(iter(d["snapshots"][2]["stable"]["vios"][OWN]["profiles"]))
        ].update(etag="changed"),
        lambda d: d["create"].update(execution="unknown"),
        lambda d: d["deletion"].update(execution="unknown"),
        lambda d: d["deletion"]["observation"].update(predelete_native=None),
        lambda d: d["fixture"].update(vios_uuid=V0),
        lambda d: d["run"].update(tree_clean=False),
    ]
    for edit in edits:
        changed = deepcopy(document)
        edit(changed)
        assert not source._history_complete(p, changed, h.packet_sha)
    assert not source._history_complete(p, {}, h.packet_sha)


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["create", "boot", "deletion", "companion"])
async def test_recovery_present_absence_cannot_repair_history(rig, unknown):
    p, w, h, out, _ = rig
    document = await run_capture(rig)
    if unknown == "boot":
        document["cases"][4]["execution"] = "unknown"
    elif unknown != "companion":
        document[unknown]["execution"] = "unknown"
    if unknown != "companion":
        source._save(out, session(p, document=document), "session.json")
    h.budget = source.BootSourceBudget("recover", p)
    recovery = out.parent / "recover"
    recovery.mkdir(mode=0o700)
    before = (w.creates, len(w.boots), len(w.deletes))
    result = await source.recover(h, p, document, recovery)
    assert result["recovery"]["baseline_agrees_now"]
    assert result["recovery"]["fixture_absent_now"]
    assert not result["recovery"]["clean"] and result["run"]["partial"] is True
    assert (w.creates, len(w.boots), len(w.deletes)) == before


@pytest.mark.asyncio
async def test_recovery_complete_history_and_current_baseline(rig):
    p, _w, h, out, _ = rig
    document = await run_capture(rig)
    source._save(out, session(p, document=document), "session.json")
    h.budget = source.BootSourceBudget("recover", p)
    recovery = out.parent / "recover"
    recovery.mkdir(mode=0o700)
    result = await source.recover(h, p, document, recovery)
    assert result["recovery"]["clean"] and not result["run"]["partial"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("logon", "unknown"),
        ("logoff", "unknown"),
        ("http", "unknown"),
        ("own_process_pid", 123),
        ("closed", False),
        ("stage", "recover"),
        ("schema_version", True),
    ],
)
def test_session_exact_closeout(tmp_path, field, value):
    p = packet(tmp_path)
    close = session(p)
    assert source._session_closed(close, "d" * 64, "capture")
    close[field] = value
    assert not source._session_closed(close, "d" * 64, "capture")


def test_session_predecessor_recomputes_baseline_and_binding(tmp_path):
    p = packet(tmp_path)
    p["baseline_sha256"] = ame.digest({"synthetic": "baseline"})
    initial = Path(p["window"]["directory"]) / "preflight-initial"
    initial.mkdir(mode=0o700)
    d = source._new_document(p, "d" * 64)
    d["phase"] = "baseline-observed"
    d["run"].update(partial=False, finished=int(time.time()))
    d["snapshots"] = [{"stable": {"synthetic": "baseline"}}]
    d["observed_baseline_sha256"] = p["baseline_sha256"]
    source._save(initial, d)
    source._save(initial, session(p, "preflight-initial"), "session.json")
    source._predecessor(p, "preflight-final", "e" * 64)
    d["snapshots"][0]["stable"]["synthetic"] = "changed"
    source._save(initial, d)
    with pytest.raises(ValueError):
        source._predecessor(p, "preflight-final", "e" * 64)


@pytest.mark.asyncio
@pytest.mark.parametrize("event", range(31))
@pytest.mark.parametrize("edge", ["before", "after"])
async def test_ordered_intent_checkpoint_crashes_stop_effects(
    rig, monkeypatch, event, edge
):
    _p, w, h, out, _ = rig
    save = source._save
    reached = 0
    effects_at_failure = None

    def fault(directory, d, name_out="results.json"):
        nonlocal reached, effects_at_failure
        if name_out == "results.json":
            current = reached
            reached += 1
            if current == event:
                effects_at_failure = (w.creates, len(w.boots), len(w.deletes))
                if edge == "after":
                    save(directory, d, name_out)
                raise ame.CheckpointError("synthetic checkpoint loss")
        save(directory, d, name_out)

    monkeypatch.setattr(source, "_save", fault)
    with pytest.raises(ame.CheckpointError):
        await run_capture(rig)
    assert h.held and reached == event + 1
    assert (w.creates, len(w.boots), len(w.deletes)) == effects_at_failure
    # Even a complete journal written just before a crash cannot prove session closure.
    assert not (out / "session.json").exists()
    if event > 0 or edge == "after":
        durable = json.loads((out / "results.json").read_text())
        assert durable["create"]["execution"] in {"intent", "acknowledged"}


@pytest.mark.asyncio
async def test_cleanup_reuse_once_only_even_if_rearmed(rig, monkeypatch):
    _p, w, h, _out, _ = rig
    d = await run_capture(rig)
    assert h.delete_count == 1
    h.held = False
    h.delete_uuid = OWN
    h.delete_sent = False
    h.document = d
    d["phase"] = "delete-intent"
    sent = len(w.http)
    with (
        h.budget.section("attack-delete", 1, 0, 1024 * 1024, 20),
        pytest.raises(ValueError),
    ):
        await h._request("DELETE", "/rest/api/uom/VirtualIOServer/" + OWN)
    assert len(w.http) == sent and h.delete_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "grant", "deadline", "route"])
async def test_cleanup_rechecks_after_scoped_deleter_reads(rig, monkeypatch, change):
    _p, w, h, _out, _ = rig
    request = w.request

    def boundary(req):
        response = request(req)
        if "/quick/PartitionState" in req.url.path:
            if change == "source":
                monkeypatch.setattr(
                    source,
                    "_attest",
                    Mock(side_effect=ame.Held("synthetic source drift")),
                )
            elif change == "grant":
                h.grant_sha = "e" * 64
            elif change == "deadline":
                h.budget.deadline = time.monotonic()
            else:
                h.delete_uuid = V0
        return response

    # The mock transport dynamically calls the request wrapper at each physical boundary.
    h._http._transport = httpx.MockTransport(boundary)
    result = await run_capture(rig)
    assert result["phase"] == "held" and w.deletes == [] and OWN in w.nodes
    assert h.delete_count == 0


@pytest.mark.asyncio
async def test_caps_unknown_response_keeps_precharge_and_session_once(rig, monkeypatch):
    _p, _w, h, _out, _ = rig

    async def unknown(self, method, path, **kwargs):
        raise TimeoutError("synthetic unknown")

    monkeypatch.setattr(ame.HMCClient, "_request", unknown)
    h.budget = source.BootSourceBudget("capture", rig[0])
    with h.budget.section("logon", 1, 0, 1024 * 1024, 20), pytest.raises(TimeoutError):
        await h._request("PUT", "/rest/api/web/Logon")
    assert h.budget.used == [1, 0, 1024 * 1024]
    with (
        h.budget.section("second-logon", 1, 0, 1024 * 1024, 20),
        pytest.raises(ValueError),
    ):
        await h._request("PUT", "/rest/api/web/Logon")
    assert h.budget.used == [1, 0, 1024 * 1024]


def write_packet(p, tmp_path):
    path = tmp_path / "packet.json"
    raw = ame.encode(p)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path, hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "kind", ["linked-file", "repository-dir", "ancestor", "symlink"]
)
def test_private_all_source_checkout_destinations_rejected(tmp_path, monkeypatch, kind):
    p = packet(tmp_path)
    window = Path(p["window"]["directory"])
    if kind == "linked-file":
        (window / ".git").write_text("gitdir: synthetic")
    elif kind == "repository-dir":
        (window / ".git").mkdir()
    elif kind == "ancestor":
        (window.parent / ".git").write_text("gitdir: synthetic")
    else:
        link = tmp_path / "link"
        link.symlink_to(window)
        p["window"]["directory"] = str(link)
    path, sha = write_packet(p, tmp_path)
    calls = Mock(side_effect=AssertionError("no transport allowed"))
    monkeypatch.setattr(source, "load_profile", calls)
    monkeypatch.setattr(source, "_attest", lambda p: None)
    assert (
        source.main(
            [
                "preflight",
                "--packet",
                str(path),
                "--packet-sha256",
                sha,
                "--output",
                str(window / "preflight-initial"),
            ]
        )
        == 2
    )
    calls.assert_not_called()
    assert not (window / "preflight-initial.claim").exists()


@pytest.mark.parametrize("fault", ["stale", "digest", "read-only-grant"])
def test_private_invalid_invocation_never_opens_transport(tmp_path, monkeypatch, fault):
    p = packet(tmp_path)
    if fault == "stale":
        p["window"]["not_before"] -= 8000
        p["window"]["expires_at"] -= 8000
    path, sha = write_packet(p, tmp_path)
    if fault == "digest":
        sha = "f" * 64
    calls = Mock()
    monkeypatch.setattr(source, "load_profile", calls)
    args = [
        "preflight",
        "--packet",
        str(path),
        "--packet-sha256",
        sha,
        "--output",
        str(Path(p["window"]["directory"]) / "preflight-initial"),
    ]
    if fault == "read-only-grant":
        args += ["--exclusive-writer-window"]
    assert source.main(args) == 2
    calls.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "logout", "logon", "expired-setup"])
async def test_session_real_closeout_and_initial_deadline(rig, monkeypatch, fault):
    from types import SimpleNamespace

    p, w, _h, out, _ = rig
    p["baseline_sha256"] = None
    stage = "preflight-initial"
    output = out.parent / stage
    output.mkdir(mode=0o700)
    monkeypatch.setattr(source, "load_profile", lambda profile: config(p))
    if fault == "logout":
        w.fault = "logout-unknown"
    if fault == "logon":
        original = w.request

        def request(req):
            if req.method == "PUT":
                raise httpx.ReadTimeout("synthetic unknown logon", request=req)
            return original(req)

        monkeypatch.setattr(
            ame.HMCClient,
            "_new_http_client",
            lambda self, port: httpx.AsyncClient(
                base_url="https://hmc.test:443", transport=httpx.MockTransport(request)
            ),
        )
    args = SimpleNamespace(packet_sha256="d" * 64, output=output)
    started = time.monotonic() - (201 if fault == "expired-setup" else 0)
    code = await source._run_stage(p, args, stage, started)
    close = json.loads((output / "session.json").read_text())
    assert code == (0 if fault == "none" else 2)
    assert close["closed"] is (fault == "none")
    assert close["own_process_pid"] is None
    if fault == "logon":
        assert close["logon"] == "unknown" and close["logoff"] == "no-known-token"
    if fault == "logout":
        assert close["logoff"] == "unknown"
    if fault == "expired-setup":
        assert not w.http


@pytest.mark.asyncio
async def test_session_owned_unreaped_attestation_never_clean(rig, monkeypatch):
    _p, w, h, _out, _ = rig

    def held(p):
        error = ame.Held("synthetic unreaped own source process")
        error.own_process_pid = 123
        raise error

    monkeypatch.setattr(source, "_attest", held)
    result = await run_capture(rig)
    assert h.own_process_pid == 123 and result["phase"] == "held" and not w.creates


@pytest.mark.parametrize(
    "fault", ["head", "tree", "dirty", "runtime", "import", "bytes", "git-timeout"]
)
def test_attestation_every_bound_input(tmp_path, monkeypatch, fault):
    p = packet(tmp_path)
    root = tmp_path / "source"
    root.mkdir()
    package = root / "src" / "hmcpctl"
    package.mkdir(parents=True)
    for name in source.SOURCE_PATHS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic boundary source")
        p["imported_source_sha256"][name] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    monkeypatch.setattr(ame, "_root", lambda: root)
    monkeypatch.setattr(ame.hmcpctl, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(ame.sys, "version_info", (3, 11, 15))
    calls = []

    def git(root, args):
        calls.append(args)
        if fault == "git-timeout":
            raise ame.Held("source attestation process deadline expired")
        if args == ("rev-parse", "HEAD"):
            return "f" * 40 if fault == "head" else p["tested_commit"]
        if args == ("rev-parse", "HEAD^{tree}"):
            return "f" * 40 if fault == "tree" else p["tested_tree"]
        return "dirty" if fault == "dirty" else ""

    monkeypatch.setattr(ame, "_git_read", git)
    if fault == "runtime":
        monkeypatch.setattr(ame.sys, "version_info", (3, 11, 14))
    if fault == "import":
        monkeypatch.setattr(ame.hmcpctl, "__file__", str(tmp_path / "different.py"))
    if fault == "bytes":
        (root / source.SOURCE_PATHS[-1]).write_text("changed")
    with pytest.raises(ValueError):
        source._attest(p)
    assert 1 <= len(calls) <= 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["detail", "header"])
async def test_uncertainty_identity_mismatch_skips_native_probe(rig, monkeypatch, kind):
    _p, w, h, _out, _ = rig
    if kind == "header":
        original = w.ssh

        async def ssh(config, command, **kwargs):
            raw = await original(config, command, **kwargs)
            if w.boots and shlex.split(command)[0] == "lssyscfg":
                return raw.replace(OWN, V0)
            return raw

        monkeypatch.setattr(source, "run_hmc_command", ssh)
    else:
        original = w.request

        def request(req):
            response = original(req)
            if w.boots and req.url.path.endswith(OWN):
                return httpx.Response(
                    200,
                    content=response.content.replace(
                        b'<PartitionID kb="CUD" kxe="false">3',
                        b'<PartitionID kb="CUD" kxe="false">99',
                    ),
                    headers=response.headers,
                )
            return response

        h._http._transport = httpx.MockTransport(request)
    result = await run_capture(rig)
    assert result["phase"] == "held" and not w.deletes
    assert "uncertainty" not in h.native_consumed


@pytest.mark.asyncio
async def test_ordered_every_effect_has_durable_exact_intent(rig, monkeypatch):
    _p, w, h, out, _ = rig
    ssh = w.ssh
    request = w.request
    seen = []
    delete = source.delete_vios

    async def checked_delete(client, selector, *, system_name_or_uuid):
        assert selector == OWN and system_name_or_uuid == rig[0]["system"]["uuid"]
        return await delete(client, selector, system_name_or_uuid=system_name_or_uuid)

    monkeypatch.setattr(source, "delete_vios", checked_delete)

    async def checked(config, command, **kwargs):
        words = shlex.split(command)
        if words[0] in {"mksyscfg", "chsyscfg"}:
            d = json.loads((out / "results.json").read_text())
            if words[0] == "mksyscfg":
                assert (
                    d["phase"] == "create-intent"
                    and d["create"]["execution"] == "intent"
                    and h.create_sent
                )
                assert (
                    d["create"]["record_sha256"]
                    == hashlib.sha256(words[-1].encode()).hexdigest()
                )
            else:
                assert (
                    d["phase"] == "boot-intent"
                    and d["cases"][-1]["execution"] == "intent"
                )
                assert d["cases"][-1]["index"] == h.boot_sent - 1 == len(w.boots)
                assert d["cases"][-1]["record"] == words[-1]
            seen.append(words[0])
        return await ssh(config, command, **kwargs)

    def checked_request(req):
        if req.method == "DELETE":
            d = json.loads((out / "results.json").read_text())
            assert (
                d["phase"] == "delete-intent" and d["deletion"]["execution"] == "intent"
            )
            assert (
                d["deletion"]["identity"]["vios_uuid"]
                == req.url.path.rsplit("/", 1)[-1]
                == OWN
            )
            assert h.delete_count == 1 and h.delete_sent
            seen.append("DELETE")
        return request(req)

    monkeypatch.setattr(source, "run_hmc_command", checked)
    h._http._transport = httpx.MockTransport(checked_request)
    d = await run_capture(rig)
    assert not d["run"]["partial"]
    assert seen == ["mksyscfg"] + ["chsyscfg"] * 8 + ["DELETE"]


@pytest.mark.asyncio
async def test_recovery_renamed_owned_uuid_remains_residue(rig):
    p, w, h, out, _ = rig
    d = await run_capture(rig)
    source._save(out, session(p, document=d), "session.json")
    w.nodes[OWN] = {
        "name": "renamed-owned-fixture",
        "description": p["fixture"]["description"],
        "factor": "0",
    }
    h.budget = source.BootSourceBudget("recover", p)
    output = out.parent / "recover"
    output.mkdir(mode=0o700)
    result = await source.recover(h, p, d, output)
    assert result["recovery"]["history_complete"]
    assert not result["recovery"]["fixture_absent_now"]
    assert (
        not result["recovery"]["baseline_agrees_now"]
        and not result["recovery"]["clean"]
    )


def test_private_wrong_owner_is_rejected(tmp_path, monkeypatch):
    p = tmp_path / "packet"
    p.write_text("{}")
    p.chmod(0o600)
    uid = p.stat().st_uid
    monkeypatch.setattr(ame.os, "getuid", lambda: uid + 1)
    with pytest.raises(ValueError):
        source._read_private(p, 65536)


@pytest.mark.asyncio
async def test_history_native_output_observed_empty_success(rig):
    p, _w, h, _out, _ = rig
    d = await run_capture(rig)
    assert d["create"]["native_output"] == ""
    assert all(c["native_output"] == "" for c in d["cases"])
    assert "native_output" not in d["deletion"]
    assert source._history_complete(p, d, h.packet_sha)
    for slot in ["create", 0, 7]:
        changed = deepcopy(d)
        record = changed["create"] if slot == "create" else changed["cases"][slot]
        for value in [None, "synthetic warning", "x" * 65537, True, {}]:
            record["native_output"] = value
            assert not source._history_complete(p, changed, h.packet_sha)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(8))
async def test_uncertainty_native_output_warning_retained_before_hold(rig, index):
    _p, w, _h, out, _ = rig
    w.boot_fault = ("warning", index)
    d = await run_capture(rig)
    assert d["phase"] == "held" and d["run"]["partial"] is True
    assert d["cases"][-1]["native_output"] == "synthetic warning"
    assert d["cases"][-1]["execution"] == "acknowledged"
    assert len(w.boots) == index + 1 and not w.deletes
    stored = json.loads((out / "results.json").read_text())
    assert stored["cases"][-1]["native_output"] == "synthetic warning"
    assert d["cases"][-1]["native_readback"]["after"] is None


@pytest.mark.asyncio
async def test_uncertainty_native_output_create_warning_no_adoption(rig, monkeypatch):
    _p, w, _h, out, _ = rig
    original = w.ssh

    async def ssh(config, command, **kwargs):
        result = await original(config, command, **kwargs)
        return (
            "synthetic create warning"
            if shlex.split(command)[0] == "mksyscfg"
            else result
        )

    monkeypatch.setattr(source, "run_hmc_command", ssh)
    d = await run_capture(rig)
    assert d["create"]["native_output"] == "synthetic create warning"
    assert (
        d["create"]["execution"] == "acknowledged" and d["create"]["identity"] is None
    )
    assert d["fixture"]["vios_uuid"] is None and d["phase"] == "held"
    assert w.creates == 1 and not w.boots and not w.deletes
    assert (
        json.loads((out / "results.json").read_text())["create"]["native_output"]
        == "synthetic create warning"
    )


@pytest.mark.asyncio
async def test_uncertainty_exception_message_is_never_native_output(rig):
    _p, w, _h, _out, _ = rig
    w.boot_fault = ("unknown-before", 0)
    d = await run_capture(rig)
    assert d["cases"][-1]["native_output"] is None
    assert "synthetic unknown before apply" not in json.dumps(d)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "boot"])
async def test_filesystem_native_output_durable_before_hold(rig, monkeypatch, kind):
    _p, w, h, _out, _ = rig
    original = w.ssh

    async def ssh(config, command, **kwargs):
        result = await original(config, command, **kwargs)
        target = "mksyscfg" if kind == "create" else "chsyscfg"
        return (
            "private synthetic warning" if shlex.split(command)[0] == target else result
        )

    monkeypatch.setattr(source, "run_hmc_command", ssh)
    save = source._save
    checkpoints = []

    def checkpoint(directory, d, name_out="results.json"):
        effect = (
            d["create"] if kind == "create" else d["cases"][-1] if d["cases"] else {}
        )
        checkpoints.append((h.held, effect.get("native_output")))
        save(directory, d, name_out)

    monkeypatch.setattr(source, "_save", checkpoint)
    await run_capture(rig)
    assert (False, "private synthetic warning") in checkpoints
    assert checkpoints.index((False, "private synthetic warning")) < checkpoints.index(
        (True, "private synthetic warning")
    )


@pytest.mark.asyncio
async def test_caps_native_output_utf8_bound(rig, monkeypatch):
    _p, w, _h, _out, _ = rig
    original = w.ssh

    async def ssh(config, command, **kwargs):
        result = await original(config, command, **kwargs)
        return "é" * 32769 if shlex.split(command)[0] == "mksyscfg" else result

    monkeypatch.setattr(source, "run_hmc_command", ssh)
    d = await run_capture(rig)
    assert d["create"]["native_output"] is None and d["phase"] == "held"
    assert not w.boots and not w.deletes


@pytest.mark.asyncio
async def test_history_strict_observed_identity_types(rig):
    p, w, h, _out, _ = rig
    w.partition_id = 1
    d = await run_capture(rig)
    assert source._history_complete(p, d, h.packet_sha)
    for slot in ["fixture", "create", "deletion"]:
        changed = json.loads(json.dumps(d))
        target = changed[slot] if slot == "fixture" else changed[slot]["identity"]
        target["partition_id"] = True
        assert not source._history_complete(p, changed, h.packet_sha)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "missing-create",
        "duplicate-create",
        "ninth-boot",
        "duplicate-delete",
        "foreign-delete",
        "foreign-put",
        "unknown-ssh",
        "boot-before-create",
        "early-delete",
    ],
)
async def test_history_rejects_contradictory_physical_trace(rig, fault):
    p, _w, h, out, _ = rig
    document = await run_capture(rig)
    assert source._history_complete(p, document, h.packet_sha)
    trace = document["transport"]
    if fault == "empty":
        trace.update(used=[0, 0, 0], calls=[])
    elif fault == "missing-create":
        trace["calls"].remove({"kind": "SSH", "label": "create"})
    elif fault in {"boot-before-create", "early-delete"}:
        moved = (
            {"kind": "SSH", "label": "boot"}
            if fault == "boot-before-create"
            else {
                "kind": "REST",
                "label": "DELETE /rest/api/uom/VirtualIOServer/" + OWN,
            }
        )
        trace["calls"].remove(moved)
        trace["calls"].insert(1, moved)
    else:
        kind, label = {
            "duplicate-create": ("SSH", "create"),
            "ninth-boot": ("SSH", "boot"),
            "duplicate-delete": ("REST", "DELETE /rest/api/uom/VirtualIOServer/" + OWN),
            "foreign-delete": ("REST", "DELETE /rest/api/uom/VirtualIOServer/" + V0),
            "foreign-put": ("REST", "PUT /rest/api/uom/VirtualIOServer/" + OWN),
            "unknown-ssh": ("SSH", "power"),
        }[fault]
        trace["calls"].append({"kind": kind, "label": label})
    trace["used"][:2] = [
        sum(c["kind"] == k for c in trace["calls"]) for k in ["REST", "SSH"]
    ]
    source._save(out, session(p, document=document), "session.json")
    h.budget = source.BootSourceBudget("recover", p)
    output = out.parent / "recover"
    output.mkdir(mode=0o700)
    result = await source.recover(h, p, document, output)
    assert (
        result["recovery"]["baseline_agrees_now"]
        and result["recovery"]["fixture_absent_now"]
    )
    assert (
        not result["recovery"]["history_complete"] and not result["recovery"]["clean"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unrelated", "extra-delete", "counter"])
async def test_recovery_session_trace_must_extend_capture(rig, fault):
    p, _w, h, out, _ = rig
    document = await run_capture(rig)
    close = session(p, document=document)
    if fault == "unrelated":
        close = session(p)
    elif fault == "extra-delete":
        close["transport"]["calls"].insert(
            -1, {"kind": "REST", "label": "DELETE /rest/api/uom/VirtualIOServer/" + OWN}
        )
        close["transport"]["used"][0] += 1
    else:
        close["transport"]["used"][2] += source.MIB + 1
    source._save(out, close, "session.json")
    h.budget = source.BootSourceBudget("recover", p)
    output = out.parent / "recover"
    output.mkdir(mode=0o700)
    result = await source.recover(h, p, document, output)
    assert result["recovery"]["history_complete"]
    assert (
        not result["recovery"]["capture_session_closed"]
        and not result["recovery"]["clean"]
    )


@pytest.mark.asyncio
async def test_uncertainty_completed_snapshot_survives_native_failure(rig, monkeypatch):
    _p, w, h, out, _ = rig
    w.boot_fault = ("unknown", 0)
    original = w.ssh

    async def ssh(config, command, **kwargs):
        raw = await original(config, command, **kwargs)
        return (
            "malformed uncertainty header"
            if "uncertainty" in h.native_consumed
            else raw
        )

    monkeypatch.setattr(source, "run_hmc_command", ssh)
    result = await run_capture(rig)
    stored = json.loads((out / "results.json").read_text())
    assert len(result["snapshots"]) == 3 and stored["snapshots"] == result["snapshots"]
    assert stored["phase"] == "held" and stored["cases"][0]["execution"] == "unknown"
    assert w.creates == 1 and len(w.boots) == 1 and not w.deletes


@pytest.mark.asyncio
async def test_uncertainty_snapshot_checkpoint_failure_stops_native_read(
    rig, monkeypatch
):
    _p, w, h, out, _ = rig
    w.boot_fault = ("unknown", 0)
    save = source._save

    def failed(directory, document, name_out="results.json"):
        if len(document.get("snapshots", [])) == 3:
            raise source.CheckpointError("synthetic uncertainty checkpoint failure")
        save(directory, document, name_out)

    monkeypatch.setattr(source, "_save", failed)
    with pytest.raises(source.CheckpointError):
        await run_capture(rig)
    assert h.held and "uncertainty" not in h.native_consumed and not w.deletes
    stored = json.loads((out / "results.json").read_text())
    assert len(stored["snapshots"]) == 2 and stored["phase"] == "held"
