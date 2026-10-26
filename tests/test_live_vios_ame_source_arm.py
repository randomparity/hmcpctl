from __future__ import annotations

import json
import shlex
import sys
import time
from copy import deepcopy
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio

from hmcpctl.client.core import HMCError, HMCTransportError
from hmcpctl.config import HMCConfig
from hmcpctl.documents.partition_profile import UOM
from hmcpctl.ssh.transport import HMCCLIError

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import vios_ame_source as source

SYS = "00000000-0000-4000-8000-000000000001"
V0 = "00000000-0000-4000-8000-000000000002"
BASE = "https://hmc.test:443/rest/api/uom"


def packet(tmp_path):
    window = tmp_path / "window"
    window.mkdir(mode=0o700)
    now = int(time.time())
    p = {
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
        "imported_source_sha256": {name: "c" * 64 for name in source.SOURCE_PATHS},
        "cases": [],
        "resources": deepcopy(source.RESOURCES),
        "baseline_sha256": None,
        "window": {
            "directory": str(window),
            "not_before": now - 1,
            "expires_at": now + 3598,
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
            "created_date": "2026-10-26",
            "grant_id": "test-window-1",
        },
    }
    for nonce, factor in (("1" * 32, "0"), ("2" * 32, "1.10")):
        p["cases"].append(
            {
                "name": "c07v-" + nonce[:24],
                "caller_nonce": nonce,
                "description": f"[hmcpctl owner:source-test created:2026-10-26] [caller {nonce}]",
                "mem_expansion": factor,
            }
        )
    return p


def entry(kind, identity, content):
    return (
        f'<entry xmlns="{source.ATOM}"><id>urn:uuid:{identity}</id>'
        f'<content type="application/vnd.ibm.powervm.uom+xml; type={kind}"><{kind} xmlns="{UOM}">{content}</{kind}></content></entry>'
    )


def feed(entries, path):
    return (
        f'<feed xmlns="{source.ATOM}"><link rel="SELF" href="https://hmc.test:443{path}"/>'
        + "".join(entries)
        + "</feed>"
    )


def field(name, value):
    return f'<{name} kb="CUD" kxe="false">{escape(str(value))}</{name}>'


class Wire:
    def __init__(self, p):
        self.p = p
        self.nodes = {
            V0: {"name": "service-vios", "description": "foreign", "factor": "0"}
        }
        self.http = []
        self.commands = []
        self.creates = 0
        self.deletes = []
        self.fault = None
        self.profile_count = 1
        self.envelope_fault = None
        self.collection_hits = {}
        self.injected = 0

    def profile_ids(self, identity):
        return [
            f"00000000-0000-4000-8000-{int(identity[-12:], 16) * 100 + i:012x}"
            for i in range(
                1
                if self.nodes[identity]["name"].startswith("c07v-")
                else self.profile_count
            )
        ]

    def profile(self, identity, pid):
        node = self.nodes[identity]
        enabled = "false" if node["factor"] == "0" else "true"
        factor = "1.25" if self.fault == "factor" and identity != V0 else node["factor"]
        profile_name = (
            "default_profile"
            if pid == self.profile_ids(identity)[0]
            else "profile-" + pid[-4:]
        )
        content = (
            "<Metadata><Atom>"
            + field("AtomID", pid)
            + field("AtomCreated", "fixed")
            + "</Atom></Metadata>"
            + field("ProfileName", profile_name)
            + field("SettingID", pid)
            + f'<AssociatedPartition href="{BASE}/VirtualIOServer/{identity}"/>'
            + "<ProfileMemory><ActiveMemorySharingEnabled>false</ActiveMemorySharingEnabled>"
            + field("ActiveMemoryExpansionEnabled", enabled)
            + field("ExpansionFactor", factor)
            + "</ProfileMemory>"
        )
        if self.fault == "missing-ame" and identity != V0:
            content = content.replace(
                field("ActiveMemoryExpansionEnabled", enabled), ""
            )
        return entry("LogicalPartitionProfile", pid, content)

    def vios(self, identity):
        node = self.nodes[identity]
        state = (
            "running" if self.fault == "running" and identity != V0 else "not activated"
        )
        description = (
            "wrong"
            if self.fault == "rest-marker" and identity != V0
            else node["description"]
        )
        body = (
            field("PartitionName", node["name"])
            + field("PartitionID", int(identity[-12:], 16))
            + field("PartitionType", "Virtual IO Server")
            + field("PartitionState", state)
            + field("Description", description)
            + "<DefaultProfileName>default_profile</DefaultProfileName>"
            + "<PartitionMemoryConfiguration><DesiredMemory>2048</DesiredMemory></PartitionMemoryConfiguration>"
            + "<PartitionProcessorConfiguration><DesiredVirtualProcessors>1</DesiredVirtualProcessors></PartitionProcessorConfiguration>"
            + "<PartitionIOConfiguration><MaximumVirtualIOSlots>20</MaximumVirtualIOSlots></PartitionIOConfiguration>"
            + "<VirtualSCSIMappings/><VirtualFibreChannelMappings/><PartitionProfiles>"
            + "".join(
                f'<link href="{BASE}/VirtualIOServer/{identity}/LogicalPartitionProfile/{pid}"/>'
                for pid in self.profile_ids(identity)
            )
            + "</PartitionProfiles>"
        )
        if self.fault == "foreign-drift" and self.creates and identity == V0:
            body = body.replace("<DesiredMemory>2048", "<DesiredMemory>4096")
        return entry("VirtualIOServer", identity, body)

    def system(self):
        body = (
            "<SystemName>sys-R1</SystemName><State>operating</State><SystemFirmware>test</SystemFirmware>"
            "<IsClassicHMCManagement>true</IsClassicHMCManagement>"
            "<IsHMCPowerVMManagementController>false</IsHMCPowerVMManagementController>"
            "<IsPowerVMManagementController>false</IsPowerVMManagementController>"
            "<AssociatedSystemCapabilities><VirtualIOServerCapable>true</VirtualIOServerCapable>"
            "<ActiveMemoryExpansionCapable>true</ActiveMemoryExpansionCapable></AssociatedSystemCapabilities>"
            "<AssociatedSystemMemoryConfiguration><MemoryRegionSize>256</MemoryRegionSize>"
            "<ConfigurableSystemMemory>65536</ConfigurableSystemMemory>"
            "<CurrentAvailableSystemMemory>32768</CurrentAvailableSystemMemory></AssociatedSystemMemoryConfiguration>"
            "<AssociatedSystemProcessorConfiguration><ConfigurableSystemProcessorUnits>8</ConfigurableSystemProcessorUnits>"
            "<CurrentAvailableSystemProcessorUnits>4</CurrentAvailableSystemProcessorUnits>"
            "<MinimumProcessorUnitsPerVirtualProcessor>0.05</MinimumProcessorUnitsPerVirtualProcessor>"
            "</AssociatedSystemProcessorConfiguration>"
        )
        return entry("ManagedSystem", SYS, body)

    def request(self, request):
        self.http.append((request.method, str(request.url)))
        path = request.url.path
        if path == "/rest/api/web/Logon":
            if request.method == "PUT":
                return httpx.Response(
                    200,
                    text="<LogonResponse><X-API-Session>test-token</X-API-Session></LogonResponse>",
                )
            assert request.method == "DELETE"
            if self.fault == "logout-unknown":
                raise httpx.ReadTimeout("synthetic unknown logout", request=request)
            return httpx.Response(204)
        if request.method == "DELETE":
            identity = path.rsplit("/", 1)[-1]
            assert identity != V0 and identity in self.nodes
            self.deletes.append(identity)
            del self.nodes[identity]
            if self.fault == "delete-unknown":
                raise httpx.ReadTimeout("synthetic unknown delete", request=request)
            return httpx.Response(204)
        assert request.method == "GET"
        if path.endswith("/ManagementConsole"):
            body = feed(
                [
                    entry(
                        "ManagementConsole",
                        SYS,
                        "<VersionInfo><Version>10</Version></VersionInfo>",
                    )
                ],
                path,
            )
        elif path == f"/rest/api/uom/ManagedSystem/{SYS}":
            body = self.system()
        elif path.endswith("/SharedMemoryPool"):
            return self.corrupt(
                request, httpx.Response(204, headers={"Content-Type": "text/plain"})
            )
        elif path.endswith("/LogicalPartition"):
            body = feed([], path)
        elif path.endswith("/VirtualIOServer"):
            body = feed([self.vios(key) for key in self.nodes], path)
        elif "/quick/PartitionState" in path:
            return httpx.Response(200, text="not activated")
        else:
            bits = path.split("/")
            identity = bits[5]
            if len(bits) == 6:
                body = self.vios(identity)
            elif len(bits) == 7:
                body = feed(
                    [self.profile(identity, pid) for pid in self.profile_ids(identity)],
                    path,
                )
            else:
                body = self.profile(identity, bits[7])
        kind = (
            "LogicalPartitionProfile"
            if "/LogicalPartitionProfile" in path
            else "LogicalPartition"
            if path.endswith("/LogicalPartition")
            else "ManagementConsole"
            if path.endswith("/ManagementConsole")
            else "ManagedSystem"
            if path.endswith(SYS)
            else "VirtualIOServer"
        )
        return self.corrupt(
            request,
            httpx.Response(
                200,
                content=body.encode(),
                headers={
                    "ETag": "fixed",
                    "Content-Type": "application/vnd.ibm.powervm.uom+xml; type=" + kind,
                },
            ),
        )

    def corrupt(self, request, response):
        path = request.url.path
        self.collection_hits[path] = self.collection_hits.get(path, 0) + 1
        if (
            self.envelope_fault is None
            or (path, self.collection_hits[path]) != self.envelope_fault[:2]
        ):
            return response
        self.injected += 1
        fault, kind = self.envelope_fault[2], path.rsplit("/", 1)[-1]
        body = response.text or feed([], path)
        headers = {"Content-Type": "application/vnd.ibm.powervm.uom+xml; type=" + kind}
        status = 200
        if fault == "empty200":
            body = ""
        elif fault == "whitespace":
            body = " \n "
        elif fault == "next":
            body = body.replace(
                "</feed>",
                '<link rel="NEXT" href="https://hmc.test:443'
                + path
                + '?page=2"/></feed>',
            )
        elif fault == "extension":
            body = body.replace(
                "</feed>",
                '<totalResults xmlns="http://a9.com/-/spec/opensearch/1.1/">99</totalResults></feed>',
            )
        elif fault == "truncated":
            body = body.replace("<feed ", '<feed truncated="true" ', 1)
        elif fault == "http-link":
            headers["Link"] = "<https://hmc.test:443" + path + '?page=2>; rel="next"'
        elif fault == "range":
            headers["Content-Range"] = "items 0-0/2"
        elif fault == "length":
            headers["Content-Length"] = str(len(body.encode()) + 1)
        elif fault == "root":
            body = entry(kind, V0, "")
        elif fault == "xml":
            body = '<feed xmlns="' + source.ATOM + '">'
        elif fault == "type":
            headers["Content-Type"] = (
                "application/vnd.ibm.powervm.uom+xml; type=Unallocated"
            )
        elif fault == "path":
            body = body.replace(
                'href="https://hmc.test:443' + path,
                'href="https://hmc.test:443/wrong',
                1,
            )
        elif fault == "authority":
            body = body.replace("https://hmc.test:443", "https://other.test:443", 1)
        elif fault == "namespace":
            body = body.replace(source.ATOM, "urn:unsupported-atom")
        elif fault == "member":
            body = feed([entry("Unallocated", V0, "")], path)
        elif fault == "content":
            body = feed(
                [
                    entry(kind, V0, "").replace(
                        "</content>", f'<{kind} xmlns="{UOM}"/></content>'
                    )
                ],
                path,
            )
        elif fault == "identity":
            body = feed(
                [entry(kind, V0, "").replace("</id>", "</id><id>" + V0 + "</id>")], path
            )
        elif fault == "self":
            body = body.replace('rel="SELF"', 'rel="NEXT"', 1)
        elif fault == "204":
            status, body, headers = 204, "", {"Content-Type": "text/plain"}
        elif fault == "206":
            status = 206
        elif fault == "204-body":
            status, headers = 204, {"Content-Type": "text/plain"}
        else:
            raise AssertionError("unknown synthetic envelope fault")
        return httpx.Response(status, content=body.encode(), headers=headers)

    async def ssh(self, config, command, **kwargs):
        assert kwargs["max_output_bytes"] <= 65536
        self.commands.append(command)
        words = shlex.split(command)
        if words[0] == "lscomgmt":
            return "null\n"
        if words[0] == "mksyscfg":
            self.creates += 1
            assert self.creates <= 2
            assert len(self.nodes) == 1
            pairs = dict(pair.split("=", 1) for pair in words[-1].split(","))
            assert pairs["mem_mode"] == "ded" and pairs["lpar_env"] == "vioserver"
            if self.fault == "refusal":
                raise HMCCLIError("native refusal", remote_refusal_code="HSCL1234")
            identity = f"00000000-0000-4000-8000-{self.creates + 2:012x}"
            self.nodes[identity] = {
                "name": pairs["name"],
                "description": pairs["description"],
                "factor": pairs["mem_expansion"],
            }
            if self.fault == "create-unknown":
                raise HMCCLIError("completion unknown")
            return ""
        assert words[0] == "lssyscfg"
        if words[-1] == "description":
            return (
                "wrong\n"
                if self.fault == "native-marker"
                else next(
                    n["description"] + "\n"
                    for key, n in self.nodes.items()
                    if key != V0
                )
            )
        return "native-profile-source\n"


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    p = packet(tmp_path)
    wire = Wire(p)
    config = HMCConfig.from_mapping(
        {
            **p["hmc_identity"],
            "password": "test",  # pragma: allowlist secret -- mocked credential
            "agent_id": "source-test",
        }
    )
    monkeypatch.setattr(source, "run_hmc_command", wire.ssh)
    monkeypatch.setattr(source, "_attest", lambda p: None)
    monkeypatch.setattr(
        source.HMCClient,
        "_new_http_client",
        lambda self, port: httpx.AsyncClient(
            base_url="https://hmc.test:443", transport=httpx.MockTransport(wire.request)
        ),
    )
    hmc = source.CountedHMC(config, source.Budget("capture", p))
    hmc.packet_sha = "d" * 64
    before = await source.snapshot(hmc, p)
    p["baseline_sha256"] = source.digest(before["stable"])
    hmc.budget = source.Budget("capture", p)
    wire.http.clear()
    wire.commands.clear()
    wire.collection_hits.clear()
    output = tmp_path / "capture"
    output.mkdir(mode=0o700)
    try:
        yield p, wire, hmc, output, before
    finally:
        await hmc._http.aclose()


async def run_capture(rig):
    p, _wire, hmc, output, _before = rig
    return await source.capture(
        hmc,
        p,
        output,
        grant_packet_sha256=hmc.packet_sha,
        exclusive_writer_window=True,
        fresh_vios_delete_approved=True,
    )


@pytest.mark.parametrize(
    "fault", ["extra", "factor", "marker", "resource", "nonce", "budget"]
)
def test_packet_refuses_mutation_expansion(tmp_path, fault):
    p = packet(tmp_path)
    source.validate_packet(p)
    if fault == "extra":
        p["command"] = "arbitrary"
    elif fault == "factor":
        p["cases"][1]["mem_expansion"] = "2"
    elif fault == "marker":
        p["cases"][0]["description"] += "=bad"
    elif fault == "resource":
        p["resources"]["desired_memory"] = 8192
    elif fault == "nonce":
        p["cases"][1] = deepcopy(p["cases"][0])
    else:
        p["budgets"]["stages"]["capture"][0] += 1
    with pytest.raises(ValueError):
        source.validate_packet(p)


def test_record_has_one_atomic_marker_and_exact_finite_pairs(tmp_path):
    p = packet(tmp_path)
    source.validate_packet(p)
    for case in p["cases"]:
        record = source._record(case)
        pairs = [part.split("=", 1) for part in record.split(",")]
        assert len({key for key, value in pairs}) == len(pairs)
        values = dict(pairs)
        assert values["description"] == case["description"]
        assert values["mem_mode"] == "ded"
        assert values["mem_expansion"] == case["mem_expansion"]
        assert not any("paging" in key for key in values)
        assert "all_resources" not in values
        assert {
            k: values[k]
            for k in (
                "min_mem",
                "desired_mem",
                "max_mem",
                "proc_mode",
                "sharing_mode",
                "min_proc_units",
                "desired_proc_units",
                "max_proc_units",
                "min_procs",
                "desired_procs",
                "max_procs",
            )
        } == {
            "min_mem": "1024",
            "desired_mem": "2048",
            "max_mem": "4096",
            "proc_mode": "shared",
            "sharing_mode": "uncap",
            "min_proc_units": "0.1",
            "desired_proc_units": "0.5",
            "max_proc_units": "1.0",
            "min_procs": "1",
            "desired_procs": "1",
            "max_procs": "2",
        }


@pytest.mark.parametrize("stage", list(source.LIMITS))
def test_receipt_is_once_only(tmp_path, stage):
    p = packet(tmp_path.resolve())
    window = Path(p["window"]["directory"])
    assert window.is_absolute() and source._root() not in window.parents
    output = window / stage
    source._receipt(p, stage, "d" * 64, output)
    assert output.stat().st_mode & 0o077 == 0
    claim = window / (stage + ".claim")
    assert claim.read_text() == "d" * 64 and claim.stat().st_mode & 0o077 == 0
    with pytest.raises((ValueError, OSError)):
        source._receipt(p, stage, "d" * 64, output)


def test_relative_checkout_window_refused_before_receipt_or_transport(
    tmp_path, monkeypatch, request
):
    p = packet(tmp_path.resolve())
    root = source._root()
    monkeypatch.chdir(root)
    p["imported_source_sha256"] = {
        name: source.hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in source.SOURCE_PATHS
    }
    monkeypatch.setattr(source.sys, "version_info", (3, 11, 15))
    monkeypatch.setattr(
        source,
        "_git_read",
        lambda root, args: {
            ("rev-parse", "HEAD"): p["tested_commit"],
            ("rev-parse", "HEAD^{tree}"): p["tested_tree"],
            (
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignore-submodules=all",
            ): "",
        }[args],
    )
    config = HMCConfig.from_mapping(
        {
            **p["hmc_identity"],
            "password": "test",  # pragma: allowlist secret -- mocked credential
            "agent_id": "source-test",
        }
    )
    monkeypatch.setattr(source, "load_profile", lambda name: config)
    transport = Mock(
        side_effect=AssertionError("transport entered before path refusal")
    )
    monkeypatch.setattr(source.HMCClient, "_new_http_client", transport)
    ignored = root / "docs/workflow/plans"
    if not ignored.exists():
        ignored.mkdir()
        request.addfinalizer(ignored.rmdir)
    with TemporaryDirectory(prefix="private-window-", dir=ignored) as name:
        window = Path(name)
        p["window"]["directory"] = str(window.relative_to(root))
        packet_path = tmp_path / "packet.json"
        packet_path.write_bytes(source.encode(p))
        packet_path.chmod(0o600)
        packet_sha = source.hashlib.sha256(packet_path.read_bytes()).hexdigest()
        with pytest.raises(source.Held, match="canonical absolute"):
            source._main(
                [
                    "preflight",
                    "--packet",
                    str(packet_path),
                    "--packet-sha256",
                    packet_sha,
                    "--output",
                    str(Path(p["window"]["directory"]) / "preflight-initial"),
                ],
                time.monotonic(),
            )
        assert list(window.iterdir()) == []
        transport.assert_not_called()


def test_budget_refuses_dispatch_and_keeps_failed_payload_charge(tmp_path):
    b = source.Budget("capture", packet(tmp_path))
    with b.section("one", 1, 0, 8, 20):
        b.dispatch(0, "GET")
        b.charge(8)
        with pytest.raises(ValueError):
            b.dispatch(0, "GET")
        assert b.used == [1, 0, 8]
    b.deadline = time.monotonic() - 1
    with b.section("expired", 1, 0, 8, 20), pytest.raises(ValueError):
        b.timeout(20)


@pytest.mark.asyncio
async def test_two_cases_one_alive_atomic_marker_and_cleanup(rig):
    p, wire, hmc, _output, _before = rig
    result = await run_capture(rig)
    assert wire.creates == 2 and len(wire.deletes) == 2 and list(wire.nodes) == [V0]
    assert [c["outcome"] for c in result["cases"]] == ["observed-accepted"] * 2
    assert result["run"]["partial"] is False
    assert source._history_complete(p, result, hmc.packet_sha)
    assert hmc.budget.used[:2] == [76, 11]
    assert len(wire.http) == 76 and len(wire.commands) == 11
    assert not any(c.startswith("chsyscfg") for c in wire.commands)
    assert all(
        source._record(c) in shlex.split(command)
        for c, command in zip(
            p["cases"],
            [c for c in wire.commands if c.startswith("mksyscfg")],
            strict=True,
        )
    )


@pytest.mark.parametrize(
    "fault",
    ["create-unknown", "native-marker", "rest-marker", "running", "foreign-drift"],
)
@pytest.mark.asyncio
async def test_uncertain_or_unowned_never_deletes_or_replays(rig, fault):
    _p, wire, _hmc, output, _before = rig
    wire.fault = fault
    with pytest.raises(ValueError):
        await run_capture(rig)
    assert wire.creates == 1 and wire.deletes == []
    document = json.loads((output / "results.json").read_text())
    assert document["phase"] == "held-residual" and document["run"]["partial"] is True


@pytest.mark.asyncio
async def test_native_refusal_is_observed_without_adoption(rig):
    p, wire, hmc, _output, _before = rig
    wire.fault = "refusal"
    result = await run_capture(rig)
    assert wire.creates == 2 and wire.deletes == []
    assert [c["outcome"] for c in result["cases"]] == ["observed-rejected"] * 2
    assert source._history_complete(p, result, hmc.packet_sha)


@pytest.mark.asyncio
async def test_missing_ame_allows_proven_cleanup_then_stops(rig):
    _p, wire, _hmc, _output, _before = rig
    wire.fault = "missing-ame"
    with pytest.raises(ValueError):
        await run_capture(rig)
    assert wire.creates == 1 and len(wire.deletes) == 1
    assert list(wire.nodes) == [V0]


@pytest.mark.asyncio
async def test_unknown_delete_never_replays(rig):
    _p, wire, _hmc, output, _before = rig
    wire.fault = "delete-unknown"
    with pytest.raises(HMCTransportError):
        await run_capture(rig)
    assert wire.creates == 1 and len(wire.deletes) == 1
    document = json.loads((output / "results.json").read_text())
    assert document["cases"][0]["deletion"] == "pending"
    assert document["phase"] == "held-residual"


@pytest.mark.parametrize("phase", ["create-pending", "delete-pending"])
@pytest.mark.asyncio
async def test_checkpoint_failure_leaves_intent_without_next_effect(
    rig, monkeypatch, phase
):
    _p, wire, _hmc, _output, _before = rig
    save = source._save

    def fail(directory, document, name_out="results.json"):
        if document.get("phase") == phase:
            raise source.CheckpointError("synthetic disk full")
        save(directory, document, name_out)

    monkeypatch.setattr(source, "_save", fail)
    with pytest.raises(source.CheckpointError):
        await run_capture(rig)
    assert wire.creates == (0 if phase == "create-pending" else 1)
    assert wire.deletes == []


@pytest.mark.parametrize(
    "phase",
    [
        "admitted",
        "create-pending",
        "create-returned",
        "delete-pending",
        "held-residual",
    ],
)
@pytest.mark.asyncio
async def test_all_phase_recovery_observes_but_never_cleans_history(rig, phase):
    p, wire, hmc, output, _before = rig
    hmc.budget = source.Budget("recover", p)
    document = source._new_document(p, hmc.packet_sha, "capture")
    document["phase"] = phase
    result = await source.recover(hmc, p, document, output)
    assert result["baseline_agrees_now"] is True
    assert result["history_complete"] is False and result["clean"] is False
    assert wire.creates == 0 and wire.deletes == []
    assert all(method == "GET" for method, url in wire.http)
    assert all(command.startswith("lscomgmt") for command in wire.commands)


@pytest.mark.asyncio
async def test_foreign_mapping_metadata_and_unknown_profile_fields_are_protected(rig):
    _p, wire, _hmc, _output, before = rig
    changed = deepcopy(before["stable"])
    changed["vios"][V0]["fields"]["grouped_mappings"]["VirtualSCSIMappings"][
        "value"
    ] = {"Metadata": {"x": "new"}}
    assert source.digest(changed) != source.digest(before["stable"])
    raw = wire.profile(V0, wire.profile_ids(V0)[0])
    extra = raw.replace(
        "</ProfileMemory>", "</ProfileMemory><FutureField>one</FutureField>"
    )
    assert source._profile_semantic(raw) != source._profile_semantic(extra)
    with pytest.raises(ValueError):
        source._profile_semantic(
            raw.replace("</ProfileMemory>", "</ProfileMemory><ProfileMemory/>")
        )


@pytest.mark.asyncio
async def test_physical_counter_blocks_unallocated_write_before_transport(rig):
    _p, wire, hmc, _output, _before = rig
    with hmc.budget.section("blocked", 1, 0, source.MIB, 20):
        with pytest.raises(ValueError):
            await hmc._request("DELETE", f"/rest/api/uom/VirtualIOServer/{V0}")
        with pytest.raises(ValueError):
            await hmc._request("PUT", f"/rest/api/uom/ManagedSystem/{SYS}")
    assert wire.http == []


@pytest.mark.asyncio
async def test_final_grant_is_checked_before_native_effect(rig):
    p, wire, hmc, output, _before = rig
    with pytest.raises(ValueError):
        await source.capture(
            hmc,
            p,
            output,
            grant_packet_sha256="wrong",
            exclusive_writer_window=True,
            fresh_vios_delete_approved=True,
        )
    assert wire.commands == [] and wire.http == []


@pytest.mark.asyncio
async def test_snapshot_maximum_is_exactly_68_reads_and_one_ssh(rig):
    p, wire, hmc, _output, _before = rig
    for n in range(3, 10):
        identity = f"00000000-0000-4000-8000-{n:012x}"
        wire.nodes[identity] = {
            "name": f"service-{n}",
            "description": "foreign",
            "factor": "0",
        }
    wire.profile_count = 4
    identity = "00000000-0000-4000-8000-000000000010"
    wire.nodes[identity] = {
        "name": p["cases"][0]["name"],
        "description": p["cases"][0]["description"],
        "factor": "0",
    }
    observed = await source.snapshot(hmc, p)
    assert len(observed["stable"]["vios"]) == 9
    assert sum(len(v["profiles"]) for v in observed["stable"]["vios"].values()) == 33
    assert len(wire.http) == 68 and len(wire.commands) == 1
    assert hmc.budget.used[:2] == [68, 1]
    with pytest.raises(ValueError):
        source._admission(observed, p, require_digest=False)


@pytest.mark.asyncio
async def test_oversized_inventory_stops_without_creation(rig):
    p, wire, hmc, _output, _before = rig
    for n in range(3, 12):
        wire.nodes[f"00000000-0000-4000-8000-{n:012x}"] = {
            "name": f"service-{n}",
            "description": "foreign",
            "factor": "0",
        }
    with pytest.raises(ValueError):
        await source.snapshot(hmc, p)
    assert wire.creates == 0 and wire.deletes == []


@pytest.mark.asyncio
async def test_positive_factor_mismatch_cleans_owned_fixture_and_holds(rig):
    _p, wire, _hmc, output, _before = rig
    wire.fault = "factor"
    with pytest.raises(ValueError):
        await run_capture(rig)
    assert wire.creates == 2 and len(wire.deletes) == 2
    document = json.loads((output / "results.json").read_text())
    assert document["cases"][1]["outcome"] == "mismatch"
    assert document["run"]["partial"] is True


@pytest.mark.asyncio
async def test_session_unknown_is_not_success_and_closes_local_transport(
    rig, monkeypatch
):
    from types import SimpleNamespace

    p, _wire, hmc, output, _before = rig
    events = []

    async def logon(self):
        events.append("logon")
        raise TimeoutError("private target must not print")

    async def close(self):
        events.append("close")

    monkeypatch.setattr(source.CountedHMC, "logon", logon)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", close)
    monkeypatch.setattr(source, "load_profile", lambda name: hmc.config)
    args = SimpleNamespace(packet_sha256=hmc.packet_sha, output=output)
    assert await source._run_stage(p, args, "preflight-final", time.monotonic()) == 2
    result = json.loads((output / "session.json").read_text())
    assert result["logon"] == "unknown" and result["closed"] is False
    assert events == ["logon", "close"]


_CLOSEOUT_FAULTS = (
    [("valid", True)]
    + [
        (key, None)
        for key in ("source_only", "logon", "logoff", "http", "closed", "packet_sha256")
    ]
    + [
        ("source_only", 1),
        ("logon", "unknown"),
        ("logoff", "unknown"),
        ("http", "open"),
        ("closed", 1),
        ("closed", False),
        ("packet_sha256", "e" * 64),
        ("packet_sha256", "g" * 64),
        ("packet_sha256", 1),
        ("both-digests", None),
        ("both-malformed", "g" * 64),
    ]
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["preflight-final", "capture"])
@pytest.mark.parametrize(
    "key,value",
    _CLOSEOUT_FAULTS
    + [("binding", None)]
    + [
        ("schema_version", version)
        for version in (None, True, False, 2, 1.0, "1", {}, [])
    ]
    + [("results_source_only", marker) for marker in (None, False, 1)],
)
async def test_predecessor_requires_bound_clean_session(
    rig, monkeypatch, stage, key, value
):
    from types import SimpleNamespace

    p, wire, hmc, output, _before = rig
    baseline = p["baseline_sha256"]
    previous = "preflight-initial" if stage == "preflight-final" else "preflight-final"
    directory = Path(p["window"]["directory"]) / previous
    directory.mkdir(mode=0o700)
    if stage == "preflight-final":
        p["baseline_sha256"] = None
    old_sha = source.hashlib.sha256(source.encode(p)).hexdigest()
    monkeypatch.setattr(source, "load_profile", lambda name: hmc.config)
    args = SimpleNamespace(packet_sha256=old_sha, output=directory)
    assert await source._run_stage(p, args, previous, time.monotonic()) == 0
    document = json.loads((directory / "results.json").read_text())
    closeout = json.loads((directory / "session.json").read_text())
    if key == "binding":
        p["system"]["name"] = "different"
    elif key == "schema_version":
        document.pop(key) if value is None else document.update({key: value})
    elif key == "results_source_only":
        document.pop("source_only") if value is None else document.update(
            source_only=value
        )
    elif key == "both-digests":
        document.pop("packet_sha256")
        closeout.pop("packet_sha256")
    elif key == "both-malformed":
        document["packet_sha256"] = closeout["packet_sha256"] = value
    elif key != "valid":
        closeout.pop(key) if value is None else closeout.update({key: value})
    source._save(directory, document)
    source._save(directory, closeout, "session.json")
    p["baseline_sha256"] = baseline
    packet_path = output / "packet.json"
    packet_path.write_bytes(source.encode(p))
    packet_path.chmod(0o600)
    current_sha = source.hashlib.sha256(packet_path.read_bytes()).hexdigest()
    assert (old_sha != current_sha) is (stage == "preflight-final" or key == "binding")
    wire.http.clear()
    wire.commands.clear()
    receipt = Mock(side_effect=AssertionError("next stage admitted"))
    monkeypatch.setattr(source, "_receipt", receipt)
    if key == "valid":
        source._predecessor(p, stage, current_sha)
    else:
        argv = [
            "preflight" if stage == "preflight-final" else "capture",
            "--packet",
            str(packet_path),
            "--packet-sha256",
            current_sha,
            "--output",
            str(Path(p["window"]["directory"]) / stage),
        ]
        if stage == "capture":
            argv += [
                "--grant-packet-sha256",
                current_sha,
                "--exclusive-writer-window",
                "--fresh-vios-delete-approved",
            ]
        with pytest.raises(source.Held, match="bound predecessor"):
            source._main(argv, time.monotonic())
        assert not (Path(p["window"]["directory"]) / (stage + ".claim")).exists()
    receipt.assert_not_called()
    assert wire.http == [] and wire.commands == []
    assert wire.creates == 0 and wire.deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["marker", "snapshot", "record", "saved-ame"])
async def test_history_rederives_proof_instead_of_trusting_success_labels(rig, fault):
    p, _wire, hmc, _output, _before = rig
    document = await run_capture(rig)
    if fault == "marker":
        document["cases"][0]["initial_marker_readback"] = "wrong"
    elif fault == "snapshot":
        document["snapshots"].pop()
    elif fault == "record":
        document["cases"][0]["record_sha256"] = "0" * 64
    else:
        own = document["cases"][0]["uuid"]
        raw = document["snapshots"][1]["profiles"][own]
        pid = next(iter(raw))
        raw[pid] = raw[pid].replace(
            "ActiveMemoryExpansionEnabled", "UnknownMemoryExpansionEnabled"
        )
    assert source._history_complete(p, document, hmc.packet_sha) is False


@pytest.mark.asyncio
async def test_successful_observation_with_unknown_logout_is_held(rig, monkeypatch):
    from types import SimpleNamespace

    p, wire, hmc, output, _before = rig
    wire.fault = "logout-unknown"
    monkeypatch.setattr(source, "load_profile", lambda name: hmc.config)
    args = SimpleNamespace(packet_sha256=hmc.packet_sha, output=output)
    assert await source._run_stage(p, args, "preflight-final", time.monotonic()) == 2
    document = json.loads((output / "results.json").read_text())
    closeout = json.loads((output / "session.json").read_text())
    assert document["phase"] == "preflight-complete"
    assert closeout["logoff"] == "unknown" and closeout["closed"] is False
    assert closeout["http"] == "closed"
    assert sum(method == "PUT" for method, url in wire.http) == 1
    assert sum(method == "DELETE" for method, url in wire.http) == 1


@pytest.mark.parametrize("fault", ["success", "overflow", "exit", "timeout"])
def test_source_attestation_process_is_bounded_and_reaped(tmp_path, monkeypatch, fault):
    original_popen = source.subprocess.Popen
    scripts = {
        "success": "print('ok')",
        "overflow": "print('x' * 70000)",
        "exit": "raise SystemExit(3)",
        "timeout": "import time; time.sleep(1)",
    }
    children = []

    def launch(command, **kwargs):
        assert command[:2] == ["git", "--no-pager"]
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        child = original_popen([sys.executable, "-c", scripts[fault]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(source.subprocess, "Popen", launch)
    if fault == "timeout":

        class NoReadyBytes:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def register(self, *args):
                pass

            def select(self, timeout):
                return []

        monkeypatch.setattr(source.selectors, "DefaultSelector", NoReadyBytes)
    if fault == "success":
        assert source._git_read(tmp_path, ("rev-parse", "HEAD")) == "ok"
    else:
        with pytest.raises(ValueError):
            source._git_read(tmp_path, ("rev-parse", "HEAD"))
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("key,value", _CLOSEOUT_FAULTS)
@pytest.mark.parametrize("version", [1, None, 2, True, 1.0, "1", "missing"])
async def test_recovery_requires_supported_history_and_closed_session(
    rig, closed, version, key, value
):
    p, wire, hmc, output, _before = rig
    document = await run_capture(rig)
    document.pop("schema_version")
    if version != "missing":
        document["schema_version"] = version
    supported = type(version) is int and version == 1
    capture_dir = Path(p["window"]["directory"]) / "capture"
    capture_dir.mkdir(mode=0o700)
    closeout = {
        "source_only": True,
        "closed": closed,
        "packet_sha256": hmc.packet_sha,
        "logon": "acknowledged",
        "logoff": "acknowledged",
        "http": "closed",
    }
    if key == "both-digests":
        document.pop("packet_sha256")
        closeout.pop("packet_sha256")
    elif key == "both-malformed":
        document["packet_sha256"] = closeout["packet_sha256"] = value
    elif key != "valid":
        closeout.pop(key) if value is None else closeout.update({key: value})
    source._save(capture_dir, closeout, "session.json")
    capture_closed = closed and key == "valid"
    if key in {"both-digests", "both-malformed"}:
        supported = False
    wire.http.clear()
    wire.commands.clear()
    hmc.budget = source.Budget("recover", p)
    result = await source.recover(hmc, p, document, output)
    assert result["baseline_agrees_now"] is True
    assert result["history_complete"] is supported
    assert source._history_complete(p, document, hmc.packet_sha) is supported
    assert result["capture_session_closed"] is capture_closed
    assert result["clean"] is (capture_closed and supported)
    assert result["run"]["partial"] is (not (capture_closed and supported))
    assert all(method == "GET" for method, url in wire.http)
    assert len(wire.http) == 12  # One snapshot: eight bookend and four contained reads.
    assert len(wire.commands) == 1 and wire.commands[0].startswith("lscomgmt")
    assert wire.creates == 2 and len(wire.deletes) == 2


_COLLECTION_SLOTS = [
    ("LogicalPartition", 0),
    ("VirtualIOServer", 0),
    ("SharedMemoryPool", 0),
    ("LogicalPartitionProfile", 0),
    ("LogicalPartition", 1),
    ("VirtualIOServer", 1),
    ("SharedMemoryPool", 1),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,closing", _COLLECTION_SLOTS)
@pytest.mark.parametrize("after_create", [False, True])
@pytest.mark.parametrize("fault", ["empty200", "next"])
async def test_incomplete_inventory_holds_at_every_preservation_boundary(
    rig, kind, closing, after_create, fault
):
    _p, wire, _hmc, output, _before = rig
    parent = (
        f"VirtualIOServer/{V0}"
        if kind == "LogicalPartitionProfile"
        else f"ManagedSystem/{SYS}"
    )
    path = "/rest/api/uom/" + parent + "/" + kind
    hit = (
        1 + int(after_create)
        if kind == "LogicalPartitionProfile"
        else 1 + closing + 2 * int(after_create)
    )
    wire.envelope_fault = (path, hit, fault)
    with pytest.raises(source.Held):
        await run_capture(rig)
    assert wire.injected == 1
    assert wire.creates == int(after_create) and wire.deletes == []
    document = json.loads((output / "results.json").read_text())
    assert document["phase"] == "held-residual" and document["run"]["partial"] is True
    assert not any("?page=" in url for method, url in wire.http)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "whitespace",
        "extension",
        "truncated",
        "http-link",
        "range",
        "length",
        "root",
        "xml",
        "type",
        "path",
        "authority",
        "namespace",
        "member",
        "content",
        "identity",
        "self",
        "204",
        "206",
    ],
)
async def test_collection_envelope_cannot_be_flattened_into_an_empty_witness(
    rig, fault
):
    _p, wire, _hmc, _output, _before = rig
    wire.envelope_fault = (
        f"/rest/api/uom/ManagedSystem/{SYS}/LogicalPartition",
        1,
        fault,
    )
    with pytest.raises(source.Held):
        await run_capture(rig)
    assert wire.injected == 1 and wire.creates == 0 and wire.deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["empty200", "next"])
async def test_owned_profile_inventory_is_validated_before_delete(rig, fault):
    _p, wire, _hmc, _output, _before = rig
    own = "00000000-0000-4000-8000-000000000003"
    wire.envelope_fault = (
        f"/rest/api/uom/VirtualIOServer/{own}/LogicalPartitionProfile",
        1,
        fault,
    )
    with pytest.raises(source.Held):
        await run_capture(rig)
    assert wire.injected == 1 and wire.creates == 1 and wire.deletes == []


@pytest.mark.asyncio
async def test_deleter_containment_read_cannot_ignore_continuation(rig):
    _p, wire, _hmc, _output, _before = rig
    wire.envelope_fault = (
        f"/rest/api/uom/ManagedSystem/{SYS}/VirtualIOServer",
        5,
        "next",
    )
    with pytest.raises(source.Held):
        await run_capture(rig)
    assert wire.injected == 1 and wire.creates == 1 and wire.deletes == []


@pytest.mark.asyncio
async def test_pool_204_requires_a_bodyless_response(rig):
    _p, wire, _hmc, _output, _before = rig
    wire.envelope_fault = (
        f"/rest/api/uom/ManagedSystem/{SYS}/SharedMemoryPool",
        1,
        "204-body",
    )
    with pytest.raises(source.Held):
        await run_capture(rig)
    assert wire.injected == 1 and wire.creates == 0 and wire.deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["VirtualIOServer", "LogicalPartitionProfile"])
async def test_other_204_collections_remain_unavailable(rig, kind):
    _p, wire, _hmc, _output, _before = rig
    parent = (
        f"VirtualIOServer/{V0}"
        if kind == "LogicalPartitionProfile"
        else f"ManagedSystem/{SYS}"
    )
    wire.envelope_fault = ("/rest/api/uom/" + parent + "/" + kind, 1, "204")
    with pytest.raises(source.Held):
        await run_capture(rig)
    assert wire.injected == 1 and wire.creates == 0 and wire.deletes == []


@pytest.mark.parametrize(
    "fault", ["commit", "tree", "dirty", "runtime", "import", "hash"]
)
def test_source_binding_refuses_changed_execution_inputs(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace

    p = packet(tmp_path)
    checkout = tmp_path / "checkout"
    for name in source.SOURCE_PATHS:
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"source-bound-input")
        p["imported_source_sha256"][name] = source.hashlib.sha256(
            target.read_bytes()
        ).hexdigest()
    monkeypatch.setattr(source, "_root", lambda: checkout)
    monkeypatch.setattr(
        source,
        "hmcpctl",
        SimpleNamespace(__file__=str(checkout / "src/hmcpctl/__init__.py")),
    )
    monkeypatch.setattr(source.sys, "version_info", (3, 11, 15))
    calls = []

    def git(root, args):
        assert root == checkout
        calls.append(args)
        values = {
            ("rev-parse", "HEAD"): p["tested_commit"],
            ("rev-parse", "HEAD^{tree}"): p["tested_tree"],
            (
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignore-submodules=all",
            ): "",
        }
        return values[args]

    monkeypatch.setattr(source, "_git_read", git)
    source._attest(p)
    if fault in {"commit", "tree", "dirty"}:
        original = git
        index = {"commit": 0, "tree": 1, "dirty": 2}[fault]
        target_args = [
            ("rev-parse", "HEAD"),
            ("rev-parse", "HEAD^{tree}"),
            (
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignore-submodules=all",
            ),
        ][index]
        monkeypatch.setattr(
            source,
            "_git_read",
            lambda root, args: (
                "different" if args == target_args else original(root, args)
            ),
        )
    elif fault == "runtime":
        monkeypatch.setattr(source.sys, "version_info", (3, 11, 14))
    elif fault == "import":
        monkeypatch.setattr(
            source,
            "hmcpctl",
            SimpleNamespace(__file__=str(tmp_path / "foreign/__init__.py")),
        )
    else:
        (checkout / source.SOURCE_PATHS[0]).write_bytes(b"changed")
    with pytest.raises(source.Held):
        source._attest(p)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["expired", "future", "host", "agent", "exclusive", "delete"]
)
async def test_current_authority_is_required_before_any_transport(rig, fault):
    p, wire, hmc, output, _before = rig
    exclusive, deletion = True, True
    if fault == "expired":
        p["window"]["not_before"] -= 7200
        p["window"]["expires_at"] -= 7200
    elif fault == "future":
        p["window"]["not_before"] += 7200
        p["window"]["expires_at"] += 7200
    elif fault == "host":
        hmc.config = hmc.config.model_copy(update={"host": "foreign.test"})
    elif fault == "agent":
        hmc.config = hmc.config.model_copy(update={"agent_id": "foreign"})
    elif fault == "exclusive":
        exclusive = False
    else:
        deletion = False
    with pytest.raises(source.Held):
        await source.capture(
            hmc,
            p,
            output,
            grant_packet_sha256=hmc.packet_sha,
            exclusive_writer_window=exclusive,
            fresh_vios_delete_approved=deletion,
        )
    assert wire.http == [] and wire.commands == []


@pytest.mark.asyncio
async def test_native_create_arm_is_consumed_before_unknown_completion(rig):
    p, wire, hmc, _output, _before = rig
    wire.fault = "create-unknown"
    case = p["cases"][0]
    with hmc.budget.section("create", 0, 2, 131072, 60):
        hmc.create_case = case["name"]
        with pytest.raises(HMCCLIError):
            await source._ssh(hmc, p, "create", case)
        hmc.create_case = case["name"]
        with pytest.raises(source.Held, match="repeated"):
            await source._ssh(hmc, p, "create", case)
    assert wire.creates == 1 and len(wire.commands) == 1


@pytest.mark.asyncio
async def test_third_resource_delete_is_refused_before_transport(rig):
    _p, wire, hmc, _output, _before = rig
    wire.nodes["00000000-0000-4000-8000-000000000003"] = {
        "name": "own",
        "description": "own",
        "factor": "0",
    }
    hmc.delete_count = 2
    hmc.delete_uuid = "00000000-0000-4000-8000-000000000003"
    with (
        hmc.budget.section("cleanup", 1, 0, source.MIB, 20),
        pytest.raises(source.Held, match="once-only"),
    ):
        await hmc._request("DELETE", "/rest/api/uom/VirtualIOServer/" + hmc.delete_uuid)
    assert wire.http == []


@pytest.mark.parametrize("bound", ["rest", "ssh", "bytes", "time"])
def test_creation_requires_full_remaining_cleanup_and_uncertainty_reserve(
    tmp_path, bound
):
    p = packet(tmp_path)
    budget = source.Budget("capture", p)
    budget.reserve(2)
    if bound == "time":
        budget.deadline = time.monotonic() + 989
    else:
        index, needed = {
            "rest": (0, 349),
            "ssh": (1, 11),
            "bytes": (2, 17 * source.MIB),
        }[bound]
        budget.used[index] = budget.maximum[index] - needed + 1
    with pytest.raises(source.Held, match="reserve unavailable"):
        budget.reserve(2)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["attestation", "checkpoint"])
async def test_create_rechecks_reserve_after_local_latency(
    tmp_path, monkeypatch, boundary
):
    import stat
    from types import SimpleNamespace

    p = packet(tmp_path)
    wire = Wire(p)
    checkout = tmp_path / "checkout"
    for name in source.SOURCE_PATHS:
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"source-bound-input")
        p["imported_source_sha256"][name] = source.hashlib.sha256(
            target.read_bytes()
        ).hexdigest()
    monkeypatch.setattr(source, "_root", lambda: checkout)
    monkeypatch.setattr(source.sys, "version_info", (3, 11, 15))
    monkeypatch.setattr(
        source,
        "hmcpctl",
        SimpleNamespace(__file__=str(checkout / "src/hmcpctl/__init__.py")),
    )
    elapsed = [0]
    delay_hits = []
    git_calls = []

    def git(root, args):
        assert root == checkout
        git_calls.append(args)
        if boundary == "attestation" and len(git_calls) == 4:
            elapsed[0] += 6
            delay_hits.append(boundary)
        return {
            ("rev-parse", "HEAD"): p["tested_commit"],
            ("rev-parse", "HEAD^{tree}"): p["tested_tree"],
            (
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignore-submodules=all",
            ): "",
        }[args]

    fsync = source.os.fsync

    def delayed_fsync(fd):
        if (
            boundary == "checkpoint"
            and not delay_hits
            and stat.S_ISREG(source.os.fstat(fd).st_mode)
        ):
            document = json.loads(source.os.pread(fd, source.MIB, 0))
            if document["phase"] == "create-pending":
                elapsed[0] += 6
                delay_hits.append(boundary)
        fsync(fd)

    monkeypatch.setattr(source, "_git_read", git)
    monkeypatch.setattr(source.os, "fsync", delayed_fsync)
    monkeypatch.setattr(source, "run_hmc_command", wire.ssh)
    monkeypatch.setattr(
        source.HMCClient,
        "_new_http_client",
        lambda self, port: httpx.AsyncClient(
            base_url="https://hmc.test:443", transport=httpx.MockTransport(wire.request)
        ),
    )
    config = HMCConfig.from_mapping(
        {
            **p["hmc_identity"],
            "password": "test",  # pragma: allowlist secret -- mocked credential
            "agent_id": "source-test",
        }
    )
    hmc = source.CountedHMC(config, source.Budget("capture", p))
    hmc.packet_sha = "d" * 64
    output = tmp_path / "capture"
    output.mkdir(mode=0o700)
    monotonic, wall = time.monotonic, time.time
    monkeypatch.setattr(source.time, "monotonic", lambda: monotonic() + elapsed[0])
    monkeypatch.setattr(source.time, "time", lambda: wall() + elapsed[0])
    try:
        before = await source.snapshot(hmc, p)
        p["baseline_sha256"] = source.digest(before["stable"])
        hmc.budget = source.Budget("capture", p)
        hmc.budget.deadline = time.monotonic() + 991
        with pytest.raises(source.Held, match="time reserve unavailable"):
            await source.capture(
                hmc,
                p,
                output,
                grant_packet_sha256=hmc.packet_sha,
                exclusive_writer_window=True,
                fresh_vios_delete_approved=True,
            )
        assert delay_hits == [boundary] and len(git_calls) == 6
        assert wire.creates == 0 and wire.deletes == [] and set(wire.nodes) == {V0}
        assert all(command.startswith("lscomgmt ") for command in wire.commands)
        assert hmc.create_case is None and hmc.created_names == set()
        document = json.loads((output / "results.json").read_text())
        assert document["phase"] == "held-residual" and document["run"]["partial"]
        assert (
            len(document["cases"]) == 1 and document["cases"][0]["native"] == "pending"
        )
        assert not source._history_complete(p, document, hmc.packet_sha)
    finally:
        await hmc._http.aclose()


@pytest.mark.asyncio
async def test_native_timeout_after_submission_leaves_owned_candidate_held(
    rig, monkeypatch
):
    _p, wire, _hmc, output, _before = rig
    original = wire.ssh

    async def timeout(config, command, **kwargs):
        result = await original(config, command, **kwargs)
        if command.startswith("mksyscfg"):
            raise TimeoutError("synthetic completion uncertainty")
        return result

    monkeypatch.setattr(source, "run_hmc_command", timeout)
    with pytest.raises(TimeoutError):
        await run_capture(rig)
    assert wire.creates == 1 and wire.deletes == [] and len(wire.nodes) == 2
    document = json.loads((output / "results.json").read_text())
    assert document["phase"] == "held-residual"
    assert document["cases"][0]["native"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture", ["rest-ms-vios-feed-media.json", "rest-lpar-feed-per-system.json"]
)
async def test_captured_collection_shapes_pass_the_physical_boundary(
    rig, monkeypatch, fixture
):
    from urllib.parse import urlsplit

    _p, _wire, hmc, _output, _before = rig
    captured = json.loads(
        (Path(__file__).parent / "fixtures/live" / fixture).read_text()
    )
    root = source.DET.fromstring(captured["body"])
    own = next(
        node.get("href")
        for node in root
        if node.tag == "{" + source.ATOM + "}link" and node.get("rel").upper() == "SELF"
    )
    url = urlsplit(own)
    hmc.config = hmc.config.model_copy(
        update={"host": url.hostname, "port": url.port or 443}
    )
    await hmc._http.aclose()
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            captured["status"],
            text=captured["body"],
            headers={"Content-Type": captured["content_type"]},
        )

    hmc._http = httpx.AsyncClient(
        base_url=f"https://{url.hostname}:{url.port or 443}",
        transport=httpx.MockTransport(transport),
    )
    with hmc.budget.section("captured", 1, 0, source.MIB, 20):
        response = await hmc._request("GET", captured["path"])
    assert response.status_code == 200 and len(requests) == 1
    assert hmc.budget.used[0] == 1


@pytest.mark.asyncio
async def test_oversized_response_keeps_its_charge_and_closes_own_stream(rig):
    _p, _wire, hmc, _output, _before = rig
    closed = []

    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (source.MIB + 1)

        async def aclose(self):
            closed.append(True)

    await hmc._http.aclose()
    hmc._http = httpx.AsyncClient(
        base_url="https://hmc.test:443",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Oversized())
        ),
    )
    original = hmc.config
    with (
        hmc.budget.section("oversized", 1, 0, source.MIB, 20),
        pytest.raises(HMCError, match="observed size"),
    ):
        await hmc._request("GET", f"/rest/api/uom/ManagedSystem/{SYS}")
    assert hmc.budget.used == [1, 0, source.MIB]
    assert closed == [True] and hmc.config is original
