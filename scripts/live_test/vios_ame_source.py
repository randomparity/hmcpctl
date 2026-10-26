"""Registered finite VIOS AME source probe; never product maturity evidence."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import selectors
import shlex
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

import hmcpctl
from hmcpctl.client.client_profiles import read_native_profile
from hmcpctl.client.core import HMCClient
from hmcpctl.config import load_profile, validate_agent_id
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.partition_profile import ATOM, UOM, _records, one, parse_profile
from hmcpctl.operations.vios.core import delete_vios
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.commands import build_attribute_record, build_filter
from hmcpctl.ssh.description_validation import validate_lpar_description
from hmcpctl.ssh.lpar import _lpar_resource_pairs, validate_caller_token
from hmcpctl.ssh.transport import HMCCLIError, run_hmc_command
from hmcpctl.xmlutil import leaf_text

GROUP = "vios-ame-source"
MIB = 1024 * 1024
SNAPSHOT_REST = 68
RESOURCES: dict[str, Any] = {
    "min_memory": 1024,
    "desired_memory": 2048,
    "max_memory": 4096,
    "dedicated": False,
    "min_procs": 0.1,
    "desired_procs": 0.5,
    "max_procs": 1.0,
    "min_vcpus": 1,
    "desired_vcpus": 1,
    "max_vcpus": 2,
    "uncapped": True,
}
LIMITS = {
    "preflight-initial": (70, 1, 4 * MIB, 170),
    "preflight-final": (70, 1, 4 * MIB, 170),
    "capture": (418, 12, 20 * MIB, 1130),
    "recover": (70, 1, 4 * MIB, 170),
}
BASE_FIELDS = (
    "PartitionID",
    "PartitionName",
    "PartitionType",
    "PartitionState",
    "Description",
    "DefaultProfileName",
    "LastActivatedProfile",
    "AssociatedPartitionProfile",
    "PartitionMemoryConfiguration",
    "PartitionProcessorConfiguration",
    "PartitionProfiles",
)
IO_FIELDS = (
    "PartitionIOConfiguration",
    "ClientNetworkAdapters",
    "HostEthernetAdapterLogicalPorts",
    "SRIOVEthernetLogicalPorts",
    "SRIOVRoCELogicalPorts",
    "SharedEthernetAdapters",
    "TrunkAdapters",
    "VirtualNICBackingDevices",
    "PhysicalVolumes",
    "StoragePools",
    "MediaRepositories",
)
MAPPINGS = ("VirtualSCSIMappings", "VirtualFibreChannelMappings")
SOURCE_PATHS = (
    "scripts/live_test/vios_ame_source.py",
    "scripts/live_vios_ame_source.py",
    "scripts/live_test_preflight.py",
    "scripts/live_test_recovery.py",
    "scripts/live_test_evidence.py",
    "src/hmcpctl/config.py",
    "src/hmcpctl/client/core.py",
    "src/hmcpctl/client/client_systems.py",
    "src/hmcpctl/client/client_profiles.py",
    "src/hmcpctl/client/client_resolution.py",
    "src/hmcpctl/documents/partition_profile.py",
    "src/hmcpctl/documents/lpar.py",
    "src/hmcpctl/operations/vios/core.py",
    "src/hmcpctl/resource_identity.py",
    "src/hmcpctl/ssh/lpar.py",
    "src/hmcpctl/ssh/commands.py",
    "src/hmcpctl/ssh/description_validation.py",
    "src/hmcpctl/ssh/transport.py",
    "src/hmcpctl/xmlutil.py",
)
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class Held(ValueError):
    """No later resource write is permitted in this invocation."""

    own_process_pid: int | None = None


class CheckpointError(Held):
    """Preserve the last durable intent; never retry a failed checkpoint."""


def encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def _pairs_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Held("duplicate JSON key")
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_pairs_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(Held("nonfinite JSON")),
    )


def _keys(value: object, expected: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise Held("closed object fields differ")
    return value


def _text(value: object, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > limit:
        raise Held("missing or oversized text")
    if not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Held("text contains controls or non-ASCII")
    return value


def _uuid(value: object) -> str:
    if not isinstance(value, str) or not is_uuid(value):
        raise Held("UUID required")
    return value.casefold()


def validate_packet(p: dict) -> None:
    _keys(
        p,
        {
            "schema_version",
            "configured_profile",
            "hmc_identity",
            "system",
            "tested_commit",
            "tested_tree",
            "imported_source_sha256",
            "cases",
            "resources",
            "baseline_sha256",
            "window",
            "budgets",
            "grant_binding",
        },
    )
    if type(p["schema_version"]) is not int or p["schema_version"] != 1:
        raise Held("unsupported source packet version")
    _text(p["configured_profile"], 128)
    h = _keys(p["hmc_identity"], {"host", "port", "user", "verify_ssl"})
    _text(h["host"])
    _text(h["user"])
    if type(h["port"]) is not int or not 1 <= h["port"] <= 65535:
        raise Held("invalid HMC port")
    if type(h["verify_ssl"]) is not bool:
        raise Held("TLS verification binding must be boolean")
    s = _keys(p["system"], {"uuid", "name"})
    if _uuid(s["uuid"]) != s["uuid"]:
        raise Held("packet system UUID must be canonical")
    _text(s["name"], 128)
    for key in ("tested_commit", "tested_tree"):
        if not isinstance(p[key], str) or not HEX40.fullmatch(p[key]):
            raise Held("exact clean Git identity required")
    hashes = _keys(p["imported_source_sha256"], set(SOURCE_PATHS))
    if any(not isinstance(v, str) or not HEX64.fullmatch(v) for v in hashes.values()):
        raise Held("source hash invalid")
    if p["baseline_sha256"] is not None and (
        not isinstance(p["baseline_sha256"], str)
        or not HEX64.fullmatch(p["baseline_sha256"])
    ):
        raise Held("baseline digest invalid")
    if encode(p["resources"]) != encode(RESOURCES):
        raise Held("resource tuple is not the fixed diagnostic tuple")
    g = _keys(p["grant_binding"], {"agent_id", "created_date", "grant_id"})
    validate_agent_id(_text(g["agent_id"], 64))
    if (
        date.fromisoformat(_text(g["created_date"], 10)).isoformat()
        != g["created_date"]
    ):
        raise Held("ownership date invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", _text(g["grant_id"], 64)):
        raise Held("grant identity invalid")
    if not isinstance(p["cases"], list) or len(p["cases"]) != 2:
        raise Held("exactly two fixed cases required")
    for c, factor in zip(p["cases"], ("0", "1.10"), strict=True):
        _keys(c, {"name", "caller_nonce", "description", "mem_expansion"})
        if not isinstance(c["caller_nonce"], str) or not HEX32.fullmatch(
            c["caller_nonce"]
        ):
            raise Held("128-bit caller nonce required")
        validate_caller_token(c["caller_nonce"])
        if (
            c["name"] != "c07v-" + c["caller_nonce"][:24]
            or c["mem_expansion"] != factor
        ):
            raise Held("case name or factor differs")
        marker = (
            f"[hmcpctl owner:{g['agent_id']} created:{g['created_date']}] "
            f"[caller {c['caller_nonce']}]"
        )
        if c["description"] != marker:
            raise Held("exact initial marker required")
        validate_lpar_description(marker)
    if len({c["name"] for c in p["cases"]}) != 2:
        raise Held("case names collide")
    w = _keys(p["window"], {"directory", "not_before", "expires_at"})
    _text(w["directory"], 4096)
    if any(type(w[k]) is not int for k in ("not_before", "expires_at")):
        raise Held("window timestamps must be integral UTC seconds")
    if not 1640 <= w["expires_at"] - w["not_before"] <= 3600:
        raise Held("window must fit the bound without being open-ended")
    expected = {
        "stages": {k: list(v) for k, v in LIMITS.items()},
        "rows": {
            "regular": 128,
            "vios": 8,
            "profiles": 32,
            "pools": 4,
            "with_own_vios": 9,
            "with_own_profiles": 33,
        },
    }
    if encode(p["budgets"]) != encode(expected):
        raise Held("reviewed finite stage budgets required")


def _current(p: dict) -> None:
    if not p["window"]["not_before"] <= time.time() < p["window"]["expires_at"]:
        raise Held("root window is not current")


def _private(path: Path, *, directory: bool) -> None:
    if path.is_symlink() or path.resolve() != path.absolute():
        raise Held("private path must have no symlink components")
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Held("private path owner/mode differs")
    expected_type = path.is_dir() if directory else path.is_file()
    if not expected_type:
        raise Held("private path has the wrong type")


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_read(root: Path, args: tuple[str, ...]) -> str:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    command = [
        "git",
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        *args,
    ]
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + 5
    try:
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as poller:
            poller.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not poller.select(remaining):
                    raise Held("source attestation process deadline expired")
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                if len(output) + len(chunk) > 65536:
                    raise Held("source attestation output exceeds64KiB")
                output.extend(chunk)
        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise Held("source attestation process failed")
        return output.decode("utf-8").strip()
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired as error:
            held = Held("own source-attestation process remains unreaped")
            held.own_process_pid = process.pid
            raise held from error


def _attest(p: dict) -> None:
    root = _root()

    def git(*args):
        return _git_read(root, args)

    if git("rev-parse", "HEAD") != p["tested_commit"]:
        raise Held("deployed commit differs")
    if git("rev-parse", "HEAD^{tree}") != p["tested_tree"]:
        raise Held("deployed tree differs")
    if git("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"):
        raise Held("deployed tree is not clean")
    if sys.version_info[:3] != (3, 11, 15):
        raise Held("live source runtime is not the bound Python3.11.15")
    if Path(hmcpctl.__file__).resolve().parent != root / "src" / "hmcpctl":
        raise Held("imported package is not the deployed source")
    for name, wanted in p["imported_source_sha256"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != wanted:
            raise Held("imported source closure changed")


def _save(directory: Path, document: dict, name_out: str = "results.json") -> None:
    if name_out not in {"results.json", "session.json"}:
        raise CheckpointError("unknown private journal destination")
    data = encode(document)
    if len(data) > 32 * MIB:
        raise CheckpointError("journal byte bound exceeded")
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix=".journal-", dir=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, directory / name_out)
        name = None
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as error:
        raise CheckpointError("durable checkpoint failed") from error
    finally:
        if name is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)


def _receipt(p: dict, stage: str, packet_sha: str, output: Path) -> None:
    window = Path(p["window"]["directory"])
    if window != window.resolve() or output != output.resolve():
        raise Held("canonical absolute window/output paths required")
    _private(window, directory=True)
    root = _root()
    if window == root or root in window.parents or output != window / stage:
        raise Held("exact external private stage destination required")
    if output.exists():
        raise Held("stage output already exists")
    fd = os.open(
        window / (stage + ".claim"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(packet_sha.encode())
        stream.flush()
        os.fsync(stream.fileno())
    output.mkdir(mode=0o700)
    fd = os.open(window, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Budget:
    def __init__(self, stage: str, p: dict):
        self.stage = stage
        self.maximum = LIMITS[stage]
        self.used = [0, 0, 0]
        self.started = time.monotonic()
        self.deadline = self.started + self.maximum[3]
        self.window = p["window"]["expires_at"]
        self.block: dict | None = None
        self.calls: list[dict] = []

    @contextmanager
    def section(self, name: str, rest: int, ssh: int, byte_count: int, seconds: int):
        if self.block is not None:
            raise Held("nested budget sections are invalid")
        self.block = {
            "name": name,
            "limit": [rest, ssh, byte_count],
            "used": [0, 0, 0],
            "deadline": min(self.deadline, time.monotonic() + seconds),
        }
        try:
            yield
        finally:
            self.block = None

    def remaining(self, kind: int) -> int:
        if self.block is None:
            raise Held("transport outside a declared stage block")
        return min(
            self.maximum[kind] - self.used[kind],
            self.block["limit"][kind] - self.block["used"][kind],
        )

    def timeout(self, maximum: int, *, session_close: bool = False) -> float:
        if self.block is None:
            raise Held("transport outside budget section")
        left = min(self.deadline, self.block["deadline"]) - time.monotonic()
        if not session_close:
            left = min(left, self.window - time.time())
        if left <= 0:
            raise Held("physical deadline expired")
        return min(float(maximum), left)

    def dispatch(self, kind: int, label: str) -> None:
        if self.remaining(kind) < 1 or self.remaining(2) < 1:
            raise Held("physical request or byte budget exhausted")
        self.used[kind] += 1
        assert self.block is not None
        self.block["used"][kind] += 1
        self.calls.append({"kind": "REST" if kind == 0 else "SSH", "label": label})

    def charge(self, size: int) -> None:
        if size > self.remaining(2):
            raise Held("response exceeds remaining retained-byte allowance")
        self.used[2] += size
        assert self.block is not None
        self.block["used"][2] += size

    def refund(self, size: int) -> None:
        if self.block is None or size < 0 or size > self.block["used"][2]:
            raise Held("invalid byte allowance refund")
        self.used[2] -= size
        self.block["used"][2] -= size

    def reserve(self, cases_left: int) -> None:
        # Normal remaining cases + one observational uncertainty snapshot + logout.
        wanted = (
            cases_left * 140 + 69,
            cases_left * 5 + 1,
            cases_left * 7 * MIB + 3 * MIB,
        )
        if any(self.maximum[i] - self.used[i] < wanted[i] for i in range(3)):
            raise Held("remaining normal/recovery/close transport reserve unavailable")
        if (
            min(self.deadline - time.monotonic(), self.window - time.time())
            < cases_left * 420 + 150
        ):
            raise Held("remaining normal/recovery/close time reserve unavailable")


def _collection_route(path: str) -> tuple[str, int] | None:
    bare = path.split("?", 1)[0].split("#", 1)[0]
    parts = bare.split("/")
    allowed = {
        ("ManagedSystem", "LogicalPartition"): 128,
        ("ManagedSystem", "VirtualIOServer"): 9,
        ("ManagedSystem", "SharedMemoryPool"): 4,
        ("VirtualIOServer", "LogicalPartitionProfile"): 33,
    }
    if bare == "/rest/api/uom/ManagementConsole":
        result = ("ManagementConsole", 1)
    elif (
        len(parts) == 7
        and parts[:4] == ["", "rest", "api", "uom"]
        and (parts[4], parts[6]) in allowed
    ):
        _uuid(parts[5])
        result = (parts[6], allowed[(parts[4], parts[6])])
    else:
        return None
    if path != bare:
        raise Held("collection queries/fragments are unsupported")
    return result


def _collection_media(value: str | None, kind: str) -> None:
    if value is None or [v.strip() for v in value.split(";")] != [
        "application/vnd.ibm.powervm.uom+xml",
        "type=" + kind,
    ]:
        raise Held("collection content type is unavailable or unsupported")


def _collection_link(
    node, config, expected_path: str, *, console: bool = False
) -> None:
    from urllib.parse import urlsplit

    if set(node.attrib) != {"rel", "href"} or len(node) or (node.text or "").strip():
        raise Held("collection link shape is unsupported")
    url = urlsplit(_text(node.get("href"), 4096))
    if (
        url.scheme != "https"
        or url.hostname != config.host
        or (url.port or 443) != config.port
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise Held("collection link authority or query differs")
    if console:
        bits = url.path.split("/")
        if len(bits) != 6 or bits[:5] != [
            "",
            "rest",
            "api",
            "uom",
            "ManagementConsole",
        ]:
            raise Held("management-console relation has an unsupported path")
        _uuid(bits[5])
    elif url.path != expected_path:
        raise Held("collection SELF path differs")


def _collection_entry(node, kind: str, config, path: str) -> None:
    atom, uom = "{" + ATOM + "}", "{" + UOM + "}"
    if node.attrib or (node.text or "").strip():
        raise Held("collection entry envelope is unsupported")
    ids, contents = node.findall(atom + "id"), node.findall(atom + "content")
    if len(ids) != 1 or len(contents) != 1 or ids[0].attrib or len(ids[0]):
        raise Held("collection entry identity/content is ambiguous")
    raw_id = _text(ids[0].text, 64)
    _uuid(raw_id.removeprefix("urn:uuid:"))
    content = contents[0]
    if (
        set(content.attrib) != {"type"}
        or len(content) != 1
        or (content.text or "").strip()
    ):
        raise Held("collection entry must carry one typed resource")
    _collection_media(content.get("type"), kind)
    if content[0].tag != uom + kind or (content[0].tail or "").strip():
        raise Held("collection member resource namespace/type differs")
    scalar = {atom + name for name in ("id", "title", "published", "updated")} | {
        uom + "etag"
    }
    seen = set()
    for child in node:
        if (child.tail or "").strip():
            raise Held("collection entry has mixed envelope text")
        if child.tag in scalar:
            if child.tag in seen or child.attrib or len(child):
                raise Held("collection entry scalar is ambiguous")
            seen.add(child.tag)
        elif child.tag == atom + "link":
            relation = child.get("rel", "").upper()
            if relation not in {"SELF", "MANAGEMENT_CONSOLE"}:
                raise Held("collection member continuation/unknown relation")
            _collection_link(
                child,
                config,
                path + "/" + raw_id.removeprefix("urn:uuid:"),
                console=relation == "MANAGEMENT_CONSOLE",
            )
        elif child.tag == atom + "author":
            if (
                child.attrib
                or (child.text or "").strip()
                or len(child) != 1
                or child[0].tag != atom + "name"
            ):
                raise Held("collection author envelope is unsupported")
            if child[0].attrib or len(child[0]) or (child[0].tail or "").strip():
                raise Held("collection author scalar is unsupported")
        elif child.tag != atom + "content":
            raise Held("collection entry extension is unsupported")


def _collection_response(path: str, response, config) -> None:
    route = _collection_route(path)
    if route is None:
        return
    kind, limit = route
    if "link" in response.headers or "content-range" in response.headers:
        raise Held("collection continuation/range headers are unsupported")
    length = response.headers.get("content-length")
    if length is not None and (
        not length.isascii()
        or not length.isdecimal()
        or int(length) != len(response.content)
    ):
        raise Held("collection declared length contradicts its complete body")
    if response.status_code == 204:
        if (
            kind != "SharedMemoryPool"
            or response.content
            or response.headers.get("content-type") not in {None, "text/plain"}
        ):
            raise Held("unestablished or contradictory empty-collection sentinel")
        return
    if response.status_code != 200 or not response.content.strip():
        raise Held("collection is unavailable or HTTP200 body is empty")
    _collection_media(response.headers.get("content-type"), kind)
    try:
        root = DET.fromstring(response.content)
    except (DET.ParseError, DefusedXmlException) as error:
        raise Held("collection XML is incomplete or unsafe") from error
    atom = "{" + ATOM + "}"
    if root.tag != atom + "feed" or root.attrib or (root.text or "").strip():
        raise Held("one complete Atom collection envelope required")
    entries = root.findall(atom + "entry")
    if len(entries) > limit:
        raise Held("physical collection row bound exceeded")
    scalars = {atom + name for name in ("id", "title", "updated", "generator")}
    seen, self_links = set(), 0
    for child in root:
        if (child.tail or "").strip():
            raise Held("collection has mixed envelope text")
        if child.tag == atom + "entry":
            _collection_entry(child, kind, config, path)
        elif child.tag in scalars:
            if child.tag in seen or child.attrib or len(child):
                raise Held("collection metadata scalar is ambiguous")
            seen.add(child.tag)
        elif child.tag == atom + "link":
            relation = child.get("rel", "").upper()
            if relation not in {"SELF", "MANAGEMENT_CONSOLE"}:
                raise Held("collection continuation/unknown relation")
            _collection_link(
                child, config, path, console=relation == "MANAGEMENT_CONSOLE"
            )
            self_links += relation == "SELF"
        else:
            raise Held("collection extension/completeness indicator is unsupported")
    if self_links != 1:
        raise Held("one exact collection SELF link required")


class CountedHMC(HMCClient):
    def __init__(self, config, budget: Budget):
        super().__init__(config)
        self.budget = budget
        self.delete_uuid: str | None = None
        self.delete_sent = False
        self.login_sent = False
        self.logout_sent = False
        self.create_case: str | None = None
        self.created_names: set[str] = set()
        self.delete_count = 0
        self.packet_sha = ""
        self.grant_sha = ""
        self.exclusive = False
        self.deletion_approved = False
        self.snapshot_responses: list[dict] | None = None
        self.failed_snapshot: list[dict] = []

    async def _request(self, method: str, path: str, **kwargs):
        close = method == "DELETE" and path == "/rest/api/web/Logon"
        if method == "PUT" and path == "/rest/api/web/Logon":
            if self.login_sent:
                raise Held("second logon refused")
            self.login_sent = True
        elif close:
            if self.logout_sent:
                raise Held("second logout refused")
            self.logout_sent = True
        elif method == "DELETE":
            if (
                self.delete_count >= 2
                or self.delete_sent
                or not self.delete_uuid
                or path != ("/rest/api/uom/VirtualIOServer/" + self.delete_uuid)
            ):
                raise Held("resource DELETE lacks once-only armed UUID")
            self.delete_sent = True
            self.delete_count += 1
        elif method != "GET":
            raise Held("unallocated REST mutation refused")
        seconds = self.budget.timeout(20, session_close=close)
        self.budget.dispatch(0, method + " " + path.split("?")[0])
        original = self.config
        cap = min(MIB, self.budget.remaining(2))
        self.budget.charge(cap)
        self.config = original.model_copy(
            update={"max_response_bytes": cap, "timeout": seconds}
        )
        try:
            async with asyncio.timeout(seconds):
                response = await super()._request(method, path, **kwargs)
            self.budget.refund(cap - len(response.content))
            if method == "GET" and self.snapshot_responses is not None:
                self.snapshot_responses.append(
                    {
                        "path": path,
                        "status": response.status_code,
                        "etag": response.headers.get("ETag"),
                        "body": response.content.decode("utf-8"),
                        "content_type": response.headers.get("content-type"),
                        "content_length": response.headers.get("content-length"),
                        "link": response.headers.get("link"),
                        "content_range": response.headers.get("content-range"),
                    }
                )
            if method == "GET":
                _collection_response(path, response, self.config)
            return response
        finally:
            self.config = original


async def _ssh(hmc: CountedHMC, p: dict, kind: str, case: dict | None = None) -> str:
    system = shlex.quote(p["system"]["name"])
    seconds = 20
    if kind == "mode":
        command = f"lscomgmt -m {system} -F is_controller"
    elif kind in {"profile", "description"} and case is not None and case in p["cases"]:
        pairs = [("lpar_names", case["name"])]
        if kind == "profile":
            pairs.append(("profile_names", "default_profile"))
        query = build_filter(pairs)
        level = "prof" if kind == "profile" else "lpar"
        suffix = "" if kind == "profile" else " -F description"
        command = (
            f"lssyscfg -r {level} -m {system} --filter {shlex.quote(query)}{suffix}"
        )
    elif kind == "create" and case is not None and case in p["cases"]:
        if (
            hmc.budget.stage != "capture"
            or hmc.create_case != case["name"]
            or case["name"] in hmc.created_names
            or len(hmc.created_names) >= 2
        ):
            raise Held("unarmed or repeated source creation")
        hmc.create_case = None
        hmc.created_names.add(case["name"])
        seconds = 60
        record = build_attribute_record(_record_pairs(case))
        command = f"mksyscfg -r lpar -m {system} -i {shlex.quote(record)}"
    else:
        raise Held("unknown finite source command")
    seconds = hmc.budget.timeout(seconds)
    cap = min(65536, hmc.budget.remaining(2))
    if cap <= 10:
        raise Held("native output allowance exhausted")
    hmc.budget.dispatch(1, kind)
    hmc.budget.charge(cap)
    async with asyncio.timeout(seconds):
        result = await run_hmc_command(
            hmc.config.model_copy(update={"ssh_timeout": seconds}),
            command,
            include_stderr=True,
            max_output_bytes=cap - 10,
        )
    hmc.budget.refund(cap - len(result.encode()))
    return result


def _record_pairs(case: dict) -> list[tuple[str, object]]:
    pairs = [
        ("name", case["name"]),
        ("lpar_env", "vioserver"),
        ("profile_name", "default_profile"),
    ]
    pairs.extend(_lpar_resource_pairs(LparResources(**RESOURCES), None))
    pairs.extend(
        [
            ("description", case["description"]),
            ("mem_mode", "ded"),
            ("mem_expansion", case["mem_expansion"]),
        ]
    )
    return pairs


def _record(case: dict) -> str:
    return build_attribute_record(_record_pairs(case))


def _projection(resource: dict, fields: tuple[str, ...]) -> dict:
    return {
        key: {"present": key in resource, "value": resource.get(key)} for key in fields
    }


def _entry(entry: object, kind: str) -> tuple[str, dict]:
    if not isinstance(entry, dict) or entry.get("ResourceType") != kind:
        raise Held("resource kind is unavailable or differs")
    identity = _uuid(entry.get("UUID"))
    resource = entry.get("Resource")
    if not isinstance(resource, dict):
        raise Held("resource body is unavailable")
    return identity, resource


def _inventory(feed: object, kind: str, limit: int) -> dict[str, dict]:
    if not isinstance(feed, list) or len(feed) > limit:
        raise Held("complete inventory exceeds its row bound")
    rows = {}
    names = set()
    ids = set()
    for entry in feed:
        identity, resource = _entry(entry, kind)
        name = _text(leaf_text(resource.get("PartitionName")), 128)
        partition_id = _text(leaf_text(resource.get("PartitionID")), 16)
        if not partition_id.isdecimal() or int(partition_id) < 1:
            raise Held("positive partition ID unavailable")
        _text(leaf_text(resource.get("PartitionState")), 128)
        partition_type = _text(leaf_text(resource.get("PartitionType")), 64)
        if kind == "VirtualIOServer" and partition_type != "Virtual IO Server":
            raise Held("VIOS type differs")
        if identity in rows or name in names or int(partition_id) in ids:
            raise Held("partition identity is ambiguous")
        rows[identity] = entry
        names.add(name)
        ids.add(int(partition_id))
    return rows


def _numbers(resource: dict, container: str, fields: tuple[str, ...]) -> dict:
    section = resource.get(container)
    if not isinstance(section, dict):
        raise Held("capacity container unavailable")
    result = {}
    for key in fields:
        raw = _text(leaf_text(section.get(key)), 64)
        try:
            number = Decimal(raw)
        except InvalidOperation as error:
            raise Held("capacity is not numeric") from error
        if not number.is_finite() or number < 0:
            raise Held("capacity is nonfinite or negative")
        result[key] = str(number)
    return result


def _system(entry: object, p: dict) -> dict:
    identity, resource = _entry(entry, "ManagedSystem")
    if (
        identity != p["system"]["uuid"]
        or leaf_text(resource.get("SystemName")) != p["system"]["name"]
    ):
        raise Held("selected system identity differs")
    if leaf_text(resource.get("State")) != "operating":
        raise Held("selected system is not operating")
    flags = tuple(
        leaf_text(resource.get(k))
        for k in (
            "IsClassicHMCManagement",
            "IsHMCPowerVMManagementController",
            "IsPowerVMManagementController",
        )
    )
    if flags != ("true", "false", "false"):
        raise Held("classic system authority unavailable")
    caps = resource.get("AssociatedSystemCapabilities")
    if not isinstance(caps, dict) or any(
        leaf_text(caps.get(k)) != "true"
        for k in (
            "VirtualIOServerCapable",
            "ActiveMemoryExpansionCapable",
        )
    ):
        raise Held("VIOS/AME capabilities unavailable")
    memory = _numbers(
        resource,
        "AssociatedSystemMemoryConfiguration",
        (
            "MemoryRegionSize",
            "ConfigurableSystemMemory",
            "CurrentAvailableSystemMemory",
        ),
    )
    cpu = _numbers(
        resource,
        "AssociatedSystemProcessorConfiguration",
        (
            "ConfigurableSystemProcessorUnits",
            "CurrentAvailableSystemProcessorUnits",
            "MinimumProcessorUnitsPerVirtualProcessor",
        ),
    )
    if Decimal(memory["CurrentAvailableSystemMemory"]) > Decimal(
        memory["ConfigurableSystemMemory"]
    ):
        raise Held("memory capacity contradicts total")
    if Decimal(cpu["CurrentAvailableSystemProcessorUnits"]) > Decimal(
        cpu["ConfigurableSystemProcessorUnits"]
    ):
        raise Held("CPU capacity contradicts total")
    return {
        "uuid": identity,
        "name": leaf_text(resource["SystemName"]),
        "mode": list(flags),
        "firmware": _text(leaf_text(resource.get("SystemFirmware")), 256),
        "capabilities": _projection(
            caps, ("VirtualIOServerCapable", "ActiveMemoryExpansionCapable")
        ),
        "memory": memory,
        "cpu": cpu,
    }


def _admit_capacity(system: dict) -> None:
    m, c = system["memory"], system["cpu"]
    region = Decimal(m["MemoryRegionSize"])
    if region <= 0 or region != region.to_integral_value():
        raise Held("memory region is not a positive integer")
    if any(Decimal(v) % region for v in (1024, 2048, 4096)):
        raise Held("fixed memory tuple is not region-aligned")
    if (
        Decimal(m["ConfigurableSystemMemory"]) < 4096
        or Decimal(m["CurrentAvailableSystemMemory"]) < 2048
    ):
        raise Held("fixed memory tuple exceeds observed capacity")
    if Decimal(c["ConfigurableSystemProcessorUnits"]) < 1 or Decimal(
        c["CurrentAvailableSystemProcessorUnits"]
    ) < Decimal("0.5"):
        raise Held("fixed processor tuple exceeds observed capacity")
    if not 0 < Decimal(c["MinimumProcessorUnitsPerVirtualProcessor"]) <= Decimal("0.1"):
        raise Held("fixed processor tuple is below the advertised minimum")


def _profile_semantic(raw: str) -> dict:
    # parse_profile applies existing XML identity/duplicate/depth safety first.
    parse_profile(raw)
    root = DET.fromstring(raw)
    if root.tag == "{" + ATOM + "}entry":
        publication = root.findall("{" + ATOM + "}published")
        if len(publication) > 1 or any(len(n) for n in publication):
            raise Held("outer publication scalar ambiguous")
        for node in publication:
            node.text = "<outer-publication>"
    return _records(root, False)


def _profile_links(resource: dict, hmc: CountedHMC, parent: str) -> set[str]:
    from urllib.parse import urlsplit

    if "PartitionProfiles" not in resource:
        raise Held("profile association inventory unavailable")
    section = resource["PartitionProfiles"]
    if section == "":
        return set()
    if not isinstance(section, dict):
        raise Held("profile association inventory malformed")
    links = section.get("link", [])
    if isinstance(links, dict):
        links = [links]
    if not isinstance(links, list) or len(links) > 33:
        raise Held("profile association bound exceeded")
    identities = set()
    for link in links:
        if not isinstance(link, dict) or not isinstance(link.get("href"), str):
            raise Held("profile association link malformed")
        u = urlsplit(link["href"])
        bits = u.path.split("/")
        if (
            u.scheme != "https"
            or u.hostname != hmc.config.host
            or (u.port or 443) != hmc.config.port
            or u.username
            or u.password
            or u.query
            or u.fragment
            or len(bits) != 8
            or bits[1:4] != ["rest", "api", "uom"]
            or bits[4] != "VirtualIOServer"
            or _uuid(bits[5]) != parent
            or bits[6] != "LogicalPartitionProfile"
        ):
            raise Held("foreign profile link")
        identity = _uuid(bits[7])
        if identity in identities:
            raise Held("duplicate profile association")
        identities.add(identity)
    return identities


def _pool_rows(feed: object) -> dict:
    if not isinstance(feed, list) or len(feed) > 4:
        raise Held("memory pool inventory bound exceeded")
    rows = {}
    ids = set()
    for entry in feed:
        identity, resource = _entry(entry, "SharedMemoryPool")
        pool_id = _text(leaf_text(resource.get("PoolID")), 16)
        if not pool_id.isdecimal() or int(pool_id) in ids or identity in rows:
            raise Held("pool identity ambiguous")
        if any(
            k not in resource
            for k in ("PagingServicePartitionOne", "PagingServicePartitionTwo")
        ):
            raise Held("existing pool provider associations unavailable")
        rows[identity] = resource
        ids.add(int(pool_id))
    return rows


def _closing(rows: dict) -> dict:
    return {
        key: _projection(
            entry["Resource"],
            (
                "PartitionID",
                "PartitionName",
                "PartitionType",
                "PartitionState",
                "PartitionProfiles",
            ),
        )
        for key, entry in rows.items()
    }


async def snapshot(hmc: CountedHMC, p: dict) -> dict:
    hmc.snapshot_responses = []
    try:
        return await _snapshot(hmc, p)
    except BaseException:
        hmc.failed_snapshot = list(hmc.snapshot_responses)
        raise
    finally:
        hmc.snapshot_responses = None


async def _snapshot(hmc: CountedHMC, p: dict) -> dict:
    system_uuid = p["system"]["uuid"]
    with hmc.budget.section("snapshot", 68, 1, 2 * MIB, 120):
        console = await hmc.get_console_info()
        _, console_resource = _entry(console, "ManagementConsole")
        version = console_resource.get("VersionInfo")
        if not isinstance(version, dict) or not version:
            raise Held("HMC version evidence unavailable")
        system = _system(await hmc.get_uom("ManagedSystem", system_uuid), p)
        if (await _ssh(hmc, p, "mode")).splitlines() != ["null"]:
            raise Held("native classic authority unavailable")
        regular = _inventory(
            await hmc.list_child("ManagedSystem", system_uuid, "LogicalPartition"),
            "LogicalPartition",
            128,
        )
        vios = _inventory(
            await hmc.list_child("ManagedSystem", system_uuid, "VirtualIOServer"),
            "VirtualIOServer",
            9,
        )
        if set(regular) & set(vios):
            raise Held("partition inventories overlap")
        names = [
            leaf_text(r["Resource"]["PartitionName"])
            for r in [*regular.values(), *vios.values()]
        ]
        ids = [
            int(_text(leaf_text(r["Resource"].get("PartitionID")), 16))
            for r in [*regular.values(), *vios.values()]
        ]
        if len(set(names)) != len(names) or len(set(ids)) != len(ids):
            raise Held("cross-inventory identity collision")
        pools = _pool_rows(
            await hmc.list_child("ManagedSystem", system_uuid, "SharedMemoryPool")
        )
        stable = {
            "system": system,
            "console": version,
            "regular": {},
            "vios": {},
            "pools": pools,
        }
        profiles = {}
        route_ids = {}
        for identity, entry in regular.items():
            for field in (
                "PartitionMemoryConfiguration",
                "PartitionProcessorConfiguration",
            ):
                if not isinstance(entry["Resource"].get(field), dict):
                    raise Held("regular partition resource baseline unavailable")
            stable["regular"][identity] = _projection(entry["Resource"], BASE_FIELDS)
        total_profiles = 0
        for identity, entry in vios.items():
            route = entry["UUID"]
            route_ids[identity] = route
            observed_id, resource = _entry(
                await hmc.get_uom("VirtualIOServer", route), "VirtualIOServer"
            )
            mapped_id, mapped = _entry(
                await hmc.get_vios_storage_detail(route), "VirtualIOServer"
            )
            if observed_id != identity or mapped_id != identity:
                raise Held("VIOS detail identity differs")
            if _closing({identity: {"Resource": resource}}) != _closing(
                {identity: entry}
            ):
                raise Held("VIOS feed/detail identity differs")
            for field in (
                "PartitionMemoryConfiguration",
                "PartitionProcessorConfiguration",
                "PartitionIOConfiguration",
            ):
                if not isinstance(resource.get(field), dict):
                    raise Held("VIOS resource baseline unavailable")
            if any(k not in mapped for k in MAPPINGS):
                raise Held("explicit mapping groups unavailable")
            fields = _projection(resource, BASE_FIELDS + IO_FIELDS)
            fields["grouped_mappings"] = _projection(mapped, MAPPINGS)
            feed = await hmc.list_child(
                "VirtualIOServer", route, "LogicalPartitionProfile"
            )
            if not isinstance(feed, list) or total_profiles + len(feed) > 33:
                raise Held("complete saved-profile bound exceeded")
            saved = {}
            raw = {}
            profile_names = set()
            for item in feed:
                profile_id, _ = _entry(item, "LogicalPartitionProfile")
                if profile_id in saved:
                    raise Held("duplicate profile identity")
                profile = await read_native_profile(
                    hmc, "VirtualIOServer", route, item["UUID"], system_uuid
                )
                if not profile.etag or profile.name in profile_names:
                    raise Held("profile ETag/name unavailable or ambiguous")
                saved[profile_id] = {
                    "name": profile.name,
                    "setting_id": profile.setting_id,
                    "etag": profile.etag,
                    "semantic": _profile_semantic(profile.raw),
                }
                raw[profile_id] = profile.raw
                profile_names.add(profile.name)
            if set(saved) != _profile_links(resource, hmc, identity):
                raise Held("contained profile inventory and associations differ")
            total_profiles += len(saved)
            stable["vios"][identity] = {"fields": fields, "profiles": saved}
            profiles[identity] = raw
        regular_end = _inventory(
            await hmc.list_child("ManagedSystem", system_uuid, "LogicalPartition"),
            "LogicalPartition",
            128,
        )
        vios_end = _inventory(
            await hmc.list_child("ManagedSystem", system_uuid, "VirtualIOServer"),
            "VirtualIOServer",
            9,
        )
        pools_end = _pool_rows(
            await hmc.list_child("ManagedSystem", system_uuid, "SharedMemoryPool")
        )
        if (
            _closing(regular_end) != _closing(regular)
            or _closing(vios_end) != _closing(vios)
            or pools_end != pools
        ):
            raise Held("inventory changed during snapshot")
        result = {
            "stable": stable,
            "profiles": profiles,
            "route_ids": route_ids,
            "raw_rest": list(hmc.snapshot_responses or []),
        }
        if len(encode(result)) > 2 * MIB:
            raise Held("protected snapshot serialization bound exceeded")
        return result


def _foreign(stable: dict, identity: str) -> dict:
    copy = _json(encode(stable))
    if identity not in copy["vios"]:
        raise Held("own UUID is not contained")
    del copy["vios"][identity]
    for key, field in (
        ("memory", "CurrentAvailableSystemMemory"),
        ("cpu", "CurrentAvailableSystemProcessorUnits"),
    ):
        del copy["system"][key][field]
    return copy


def _baseline_foreign(stable: dict) -> dict:
    copy = _json(encode(stable))
    for key, field in (
        ("memory", "CurrentAvailableSystemMemory"),
        ("cpu", "CurrentAvailableSystemProcessorUnits"),
    ):
        del copy["system"][key][field]
    return copy


def _admission(s: dict, p: dict, *, require_digest: bool) -> None:
    stable = s["stable"]
    if (
        len(stable["vios"]) > 8
        or sum(len(v["profiles"]) for v in stable["vios"].values()) > 32
    ):
        raise Held("baseline exceeds pre-existing VIOS/profile bound")
    names = [
        leaf_text(v["fields"]["PartitionName"]["value"])
        for v in stable["vios"].values()
    ]
    names.extend(
        leaf_text(v["PartitionName"]["value"]) for v in stable["regular"].values()
    )
    if any(_text(n, 128).startswith("c07v-") for n in names):
        raise Held("stranded diagnostic name requires read-only recovery")
    _admit_capacity(stable["system"])
    if require_digest and digest(stable) != p["baseline_sha256"]:
        raise Held("approved baseline changed")


def _candidate(s: dict, case: dict, before: dict) -> str:
    matches = [
        identity
        for identity, v in s["stable"]["vios"].items()
        if leaf_text(v["fields"]["PartitionName"]["value"]) == case["name"]
    ]
    if len(matches) != 1 or matches[0] in before["vios"]:
        raise Held("one new contained VIOS UUID required")
    identity = matches[0]
    fields = s["stable"]["vios"][identity]["fields"]
    if (
        leaf_text(fields["PartitionState"]["value"]) != "not activated"
        or leaf_text(fields["PartitionType"]["value"]) != "Virtual IO Server"
    ):
        raise Held("fresh VIOS is not inactive")
    if (
        fields["Description"]["present"]
        and leaf_text(fields["Description"]["value"]) != case["description"]
    ):
        raise Held("REST initial marker differs")
    return identity


def _observed(s: dict, identity: str, case: dict) -> dict:
    saved = s["stable"]["vios"][identity]["profiles"]
    if len(saved) != 1:
        return {
            "outcome": "unavailable",
            "reason": "unique default profile unavailable",
        }
    pid, profile = next(iter(saved.items()))
    if profile["name"] != "default_profile":
        return {"outcome": "unavailable", "reason": "default profile unavailable"}
    root = parse_profile(s["profiles"][identity][pid])
    try:

        def scalar(name):
            node = one(root, "ProfileMemory/" + name)
            if len(node) or node.text is None:
                raise ValueError("non-scalar saved value")
            return node.text.strip()

        sharing = scalar("ActiveMemorySharingEnabled")
        enabled = scalar("ActiveMemoryExpansionEnabled")
        if sharing not in {"true", "false"} or enabled not in {"true", "false"}:
            raise ValueError("non-boolean saved value")
        factor = None
        if case["mem_expansion"] != "0":
            factor = Decimal(scalar("ExpansionFactor"))
            if not factor.is_finite():
                raise ValueError("nonfinite saved factor")
        matches = sharing == "false" and enabled == (
            "false" if factor is None else "true"
        )
        if factor is not None:
            matches = matches and factor == Decimal("1.10")
        return {
            "outcome": "observed-accepted" if matches else "mismatch",
            "sharing": sharing,
            "enabled": enabled,
            "factor": str(factor) if factor is not None else None,
        }
    except (ValueError, InvalidOperation):
        return {"outcome": "unavailable", "reason": "saved AME/mode fields unavailable"}


def _grant(
    hmc: CountedHMC,
    p: dict,
    raw_sha: str,
    supplied_sha: str,
    exclusive: bool,
    deletion: bool,
) -> None:
    validate_packet(p)
    _current(p)
    if supplied_sha != raw_sha or exclusive is not True or deletion is not True:
        raise Held("current exact source/cleanup grant required")
    actual = {k: getattr(hmc.config, k) for k in ("host", "port", "user", "verify_ssl")}
    if (
        actual != p["hmc_identity"]
        or hmc.config.agent_id != p["grant_binding"]["agent_id"]
    ):
        raise Held("configured target/agent differs")
    _attest(p)


def _new_document(p: dict, raw_sha: str, stage: str) -> dict:
    return {
        "schema_version": 1,
        "source_only": True,
        "packet_sha256": raw_sha,
        "binding_sha256": _binding(p),
        "run": {
            "group": GROUP,
            "subtasks": [],
            "tested_commit": p["tested_commit"],
            "tested_tree": p["tested_tree"],
            "tree_clean": True,
            "partial": True,
            "stage": stage,
            "finished": None,
        },
        "phase": "admitted",
        "baseline_sha256": p["baseline_sha256"],
        "snapshots": [],
        "cases": [],
        "sessions": {"logon": "unknown", "logoff": "unknown"},
        "transport": {},
        "results": [],
    }


def _checkpoint(directory: Path, document: dict, phase: str, hmc: CountedHMC) -> None:
    document["phase"] = phase
    document["transport"] = {
        "used": list(hmc.budget.used),
        "calls": list(hmc.budget.calls),
    }
    _save(directory, document)


async def preflight(hmc: CountedHMC, p: dict, directory: Path) -> dict:
    document = _new_document(p, hmc.packet_sha, hmc.budget.stage)
    observed = await snapshot(hmc, p)
    _admission(observed, p, require_digest=p["baseline_sha256"] is not None)
    document["snapshots"].append(observed)
    document["observed_baseline_sha256"] = digest(observed["stable"])
    document["run"].update(partial=False, finished=int(time.time()))
    _checkpoint(directory, document, "preflight-complete", hmc)
    return document


async def _delete_owned(
    hmc: CountedHMC,
    p: dict,
    case: dict,
    record: dict,
    observed: dict,
    before: dict,
    directory: Path,
    document: dict,
) -> None:
    identity = _candidate(observed, case, before)
    if record["native"] != "acknowledged" or record["deletion"] != "not-attempted":
        raise Held("cleanup requires acknowledged once-only creation")
    if _foreign(observed["stable"], identity) != _baseline_foreign(before):
        raise Held("foreign protected configuration changed")
    marker = await _ssh(hmc, p, "description", case)
    if marker.splitlines() != [case["description"]]:
        raise Held("initial native marker unconfirmed")
    route = observed["route_ids"][identity]
    current_id, current = _entry(
        await hmc.get_uom("VirtualIOServer", route), "VirtualIOServer"
    )
    if (
        current_id != identity
        or leaf_text(current.get("PartitionName")) != case["name"]
        or leaf_text(current.get("PartitionType")) != "Virtual IO Server"
        or leaf_text(current.get("PartitionState")) != "not activated"
    ):
        raise Held("latest independent identity/state differs")
    if (
        "Description" in current
        and leaf_text(current["Description"]) != case["description"]
    ):
        raise Held("latest REST marker differs")
    _grant(hmc, p, hmc.packet_sha, hmc.grant_sha, hmc.exclusive, hmc.deletion_approved)
    record.update(
        uuid=identity,
        deletion="pending",
        marker_verified=True,
        initial_marker_readback=marker,
        identity_readback={
            "UUID": route,
            "ResourceType": "VirtualIOServer",
            "Resource": current,
        },
    )
    _checkpoint(directory, document, "delete-pending", hmc)
    hmc.delete_uuid = route
    hmc.delete_sent = False
    try:
        await delete_vios(hmc, route, system_name_or_uuid=p["system"]["uuid"])
    finally:
        hmc.delete_uuid = None
    record["deletion"] = "submitted"
    _checkpoint(directory, document, "delete-submitted", hmc)


async def capture(
    hmc: CountedHMC,
    p: dict,
    directory: Path,
    *,
    grant_packet_sha256: str,
    exclusive_writer_window: bool,
    fresh_vios_delete_approved: bool,
) -> dict:
    hmc.grant_sha = grant_packet_sha256
    hmc.exclusive = exclusive_writer_window
    hmc.deletion_approved = fresh_vios_delete_approved
    _grant(hmc, p, hmc.packet_sha, hmc.grant_sha, hmc.exclusive, hmc.deletion_approved)
    document = _new_document(p, hmc.packet_sha, "capture")
    _checkpoint(directory, document, "capture-start", hmc)
    recovery_used = False
    try:
        baseline = await snapshot(hmc, p)
        _admission(baseline, p, require_digest=True)
        before = baseline["stable"]
        document["snapshots"].append(baseline)
        for index, case in enumerate(p["cases"]):
            hmc.budget.reserve(2 - index)
            _admit_capacity(document["snapshots"][-1]["stable"]["system"])
            _grant(
                hmc,
                p,
                hmc.packet_sha,
                hmc.grant_sha,
                hmc.exclusive,
                hmc.deletion_approved,
            )
            record = {
                "name": case["name"],
                "record": _record(case),
                "record_sha256": hashlib.sha256(_record(case).encode()).hexdigest(),
                "native": "pending",
                "outcome": "unknown",
                "deletion": "not-attempted",
                "uuid": None,
                "marker_verified": False,
            }
            document["cases"].append(record)
            _checkpoint(directory, document, "create-pending", hmc)
            with hmc.budget.section("create", 0, 1, 65536, 60):
                hmc.budget.reserve(2 - index)
                hmc.create_case = case["name"]
                try:
                    record["native_output"] = await _ssh(hmc, p, "create", case)
                    record["native"] = "acknowledged"
                except HMCCLIError as error:
                    record["refusal_code"] = error.remote_refusal_code
                    record["native_error"] = str(error)[:16384]
                    record["native"] = (
                        "coded-refusal" if error.remote_refusal_code else "unknown"
                    )
            _checkpoint(directory, document, "create-returned", hmc)
            observed = await snapshot(hmc, p)
            document["snapshots"].append(observed)
            if record["native"] == "coded-refusal" and observed["stable"] == before:
                record["outcome"] = "observed-rejected"
                _checkpoint(directory, document, "case-reconciled", hmc)
                continue
            if record["native"] != "acknowledged":
                raise Held("native creation completion unknown; no adoption")
            identity = _candidate(observed, case, before)
            record["observed"] = _observed(observed, identity, case)
            record["outcome"] = record["observed"]["outcome"]
            with hmc.budget.section("observe-delete", 4, 2, 3 * MIB - 65536, 120):
                record["native_profile"] = await _ssh(hmc, p, "profile", case)
                await _delete_owned(
                    hmc, p, case, record, observed, before, directory, document
                )
            restored = await snapshot(hmc, p)
            document["snapshots"].append(restored)
            if restored["stable"] != before:
                raise Held("independent cleanup/baseline unconfirmed")
            record["deletion"] = "confirmed-absent"
            _checkpoint(directory, document, "case-reconciled", hmc)
            if record["outcome"] != "observed-accepted":
                raise Held("saved observation incomplete; no further case")
        document["run"].update(partial=False, finished=int(time.time()))
        _checkpoint(directory, document, "captured-and-reconciled", hmc)
        return document
    except CheckpointError:
        raise
    except BaseException as error:
        document["run"]["partial"] = True
        document["failure"] = type(error).__name__
        document["failed_snapshot"] = list(hmc.failed_snapshot)
        _checkpoint(directory, document, "held-residual", hmc)
        # One reserved read-only snapshot. This cannot enable cleanup or another case.
        if not recovery_used:
            recovery_used = True
            try:
                document["snapshots"].append(await snapshot(hmc, p))
            except (Exception, asyncio.CancelledError) as recovery_error:  # noqa: BLE001 — retain any failed reconciliation as held
                document["reconciliation_failure"] = type(recovery_error).__name__
            _checkpoint(directory, document, "held-residual", hmc)
        raise


def _history_complete(p: dict, document: dict, packet_sha: str) -> bool:
    try:
        return _history_matches(p, document, packet_sha)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False


def _history_matches(p: dict, document: dict, packet_sha: str) -> bool:
    if (
        type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("source_only") is not True
        or document.get("packet_sha256") != packet_sha
        or document.get("phase") != "captured-and-reconciled"
        or "failure" in document
        or document.get("baseline_sha256") != p["baseline_sha256"]
        or document.get("binding_sha256") != _binding(p)
    ):
        return False
    run = document.get("run")
    if (
        not isinstance(run, dict)
        or run.get("group") != GROUP
        or run.get("partial") is not False
        or run.get("tested_commit") != p["tested_commit"]
        or run.get("tested_tree") != p["tested_tree"]
        or run.get("tree_clean") is not True
        or run.get("subtasks") != []
    ):
        return False
    cases, snapshots = document.get("cases"), document.get("snapshots")
    if (
        not isinstance(cases, list)
        or len(cases) != 2
        or not isinstance(snapshots, list)
    ):
        return False
    if not 3 <= len(snapshots) <= 5:
        return False
    before = snapshots[0]["stable"]
    if digest(before) != p["baseline_sha256"]:
        return False
    cursor = 1
    for expected, actual in zip(p["cases"], cases, strict=True):
        if not isinstance(actual, dict) or actual.get("name") != expected["name"]:
            return False
        if (
            actual.get("record") != _record(expected)
            or actual.get("record_sha256")
            != hashlib.sha256(_record(expected).encode()).hexdigest()
        ):
            return False
        observed = snapshots[cursor]
        cursor += 1
        if actual.get("native") == "coded-refusal":
            if (
                actual.get("outcome") != "observed-rejected"
                or actual.get("deletion") != "not-attempted"
                or not re.fullmatch(
                    r"HSCL[A-Z0-9]{4}", str(actual.get("refusal_code", ""))
                )
                or observed["stable"] != before
            ):
                return False
            continue
        if (
            actual.get("native") != "acknowledged"
            or actual.get("outcome") != "observed-accepted"
            or actual.get("deletion") != "confirmed-absent"
            or actual.get("marker_verified") is not True
        ):
            return False
        identity = _candidate(observed, expected, before)
        if (
            actual.get("uuid") != identity
            or actual.get("initial_marker_readback", "").splitlines()
            != [expected["description"]]
            or _foreign(observed["stable"], identity) != _baseline_foreign(before)
            or _observed(observed, identity, expected).get("outcome")
            != "observed-accepted"
        ):
            return False
        current_id, current = _entry(actual.get("identity_readback"), "VirtualIOServer")
        if (
            current_id != identity
            or leaf_text(current.get("PartitionName")) != expected["name"]
            or leaf_text(current.get("PartitionState")) != "not activated"
            or leaf_text(current.get("PartitionType")) != "Virtual IO Server"
            or (
                "Description" in current
                and leaf_text(current["Description"]) != expected["description"]
            )
        ):
            return False
        if snapshots[cursor]["stable"] != before:
            return False
        cursor += 1
    return cursor == len(snapshots)


def _session_closed(closeout: dict, packet_sha: object) -> bool:
    return (
        isinstance(packet_sha, str)
        and HEX64.fullmatch(packet_sha) is not None
        and closeout.get("source_only") is True
        and closeout.get("packet_sha256") == packet_sha
        and closeout.get("closed") is True
        and closeout.get("logon") == "acknowledged"
        and closeout.get("logoff") == "acknowledged"
        and closeout.get("http") == "closed"
    )


async def recover(hmc: CountedHMC, p: dict, document: dict, directory: Path) -> dict:
    result = _new_document(p, hmc.packet_sha, "recover")
    current = await snapshot(hmc, p)
    result["snapshots"].append(current)
    result["baseline_agrees_now"] = digest(current["stable"]) == p["baseline_sha256"]
    result["history_complete"] = _history_complete(p, document, hmc.packet_sha)
    try:
        closeout = _read_private(
            Path(p["window"]["directory"]) / "capture" / "session.json", MIB
        )
    except (OSError, ValueError, TypeError):
        closeout = {}
    result["capture_session_closed"] = _session_closed(closeout, hmc.packet_sha)
    result["clean"] = (
        result["baseline_agrees_now"]
        and result["history_complete"]
        and result["capture_session_closed"]
    )
    result["run"].update(partial=not result["clean"], finished=int(time.time()))
    _checkpoint(directory, result, "recovery-observed", hmc)
    return result


def _binding(p: dict) -> str:
    return digest({k: v for k, v in p.items() if k != "baseline_sha256"})


def _read_private(path: Path, maximum: int) -> dict:
    _private(path, directory=False)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise Held("private input byte bound exceeded")
    result = _json(raw)
    if not isinstance(result, dict):
        raise Held("private input must be an object")
    return result


def _predecessor(p: dict, stage: str, packet_sha: str) -> None:
    previous = {
        "preflight-final": "preflight-initial",
        "capture": "preflight-final",
    }.get(stage)
    if previous is None:
        return
    directory = Path(p["window"]["directory"]) / previous
    document = _read_private(directory / "results.json", 32 * MIB)
    session = _read_private(directory / "session.json", MIB)
    if (
        document.get("binding_sha256") != _binding(p)
        or document.get("phase") != "preflight-complete"
        or document.get("observed_baseline_sha256") != p["baseline_sha256"]
        or document.get("run", {}).get("partial") is not False
        or not _session_closed(session, document.get("packet_sha256"))
    ):
        raise Held("successful bound predecessor is unavailable")
    if stage == "capture" and document.get("packet_sha256") != packet_sha:
        raise Held("final preflight used a different packet")


async def _run_stage(p: dict, args, stage: str, started: float) -> int:
    budget = Budget(stage, p)
    budget.started = started
    budget.deadline = started + LIMITS[stage][3]
    config = load_profile(p["configured_profile"])
    identity = {
        key: getattr(config, key) for key in ("host", "port", "user", "verify_ssl")
    }
    if (
        identity != p["hmc_identity"]
        or config.agent_id != p["grant_binding"]["agent_id"]
    ):
        raise Held("effective configured connection or agent differs")
    # Explicit port disables HMCClient's legacy retry. No alternate destination.
    config = config.model_copy(
        update={"port": config.port, "timeout": 20, "ssh_timeout": 20}
    )
    hmc = CountedHMC(config, budget)
    hmc.packet_sha = args.packet_sha256
    closeout = {
        "source_only": True,
        "packet_sha256": args.packet_sha256,
        "logon": "not-entered",
        "logoff": "not-entered",
        "http": "open",
        "closed": False,
    }
    code = 2
    try:
        _current(p)
        _attest(p)
        remaining = budget.deadline - time.monotonic() - 30
        if remaining <= 20:
            raise Held("stage body/close reserve unavailable")
        async with asyncio.timeout(remaining):
            closeout["logon"] = "unknown"
            with budget.section("logon", 1, 0, MIB, 20):
                await hmc.logon()
            closeout["logon"] = "acknowledged"
            if stage.startswith("preflight"):
                result = await preflight(hmc, p, args.output)
            elif stage == "capture":
                result = await capture(
                    hmc,
                    p,
                    args.output,
                    grant_packet_sha256=args.grant_packet_sha256,
                    exclusive_writer_window=args.exclusive_writer_window,
                    fresh_vios_delete_approved=args.fresh_vios_delete_approved,
                )
            else:
                try:
                    history = _read_private(args.results, 32 * MIB)
                except (OSError, ValueError, TypeError):
                    history = {}
                result = await recover(hmc, p, history, args.output)
            code = 0 if result["run"]["partial"] is False else 2
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as error:  # noqa: BLE001 — close own session after every body failure
        # Privacy boundary; preserve durable resource intent and failure category only.
        closeout["body_failure"] = type(error).__name__
        closeout["own_process_pid"] = getattr(error, "own_process_pid", None)
    finally:
        if hmc.is_logged_on:
            closeout["logoff"] = "unknown"
            try:
                with budget.section("logoff", 1, 0, MIB, 20):
                    await hmc.logoff()
                closeout["logoff"] = "acknowledged"
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — unknown closeout must remain durable
                closeout["logoff_failure"] = type(error).__name__
        else:
            closeout["logoff"] = "no-known-token"
        try:
            remaining = min(10, budget.deadline - time.monotonic())
            if remaining <= 0:
                raise Held("local close deadline exhausted")
            async with asyncio.timeout(remaining):
                await hmc._http.aclose()
            closeout["http"] = "closed"
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — unknown closeout must remain durable
            closeout["http"] = "unknown"
            closeout["close_failure"] = type(error).__name__
        closeout["closed"] = (
            closeout["logon"] == "acknowledged"
            and closeout["logoff"] == "acknowledged"
            and closeout["http"] == "closed"
        )
        closeout["transport"] = {"used": list(budget.used), "calls": list(budget.calls)}
        _save(args.output, closeout, "session.json")
    return code if closeout["closed"] else 2


def _main(argv: list[str] | None, started: float) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded source-only VIOS AME diagnostic"
    )
    parser.add_argument("mode", choices=("preflight", "capture", "recover"))
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--packet-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--grant-packet-sha256")
    parser.add_argument("--exclusive-writer-window", action="store_true")
    parser.add_argument("--fresh-vios-delete-approved", action="store_true")
    args = parser.parse_args(argv)
    _private(args.packet, directory=False)
    with args.packet.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != args.packet_sha256:
        raise Held("packet bytes exceed bound or digest changed")
    p = _json(raw)
    validate_packet(p)
    stage = args.mode
    if args.mode == "preflight":
        stage = (
            "preflight-initial" if p["baseline_sha256"] is None else "preflight-final"
        )
    elif p["baseline_sha256"] is None:
        raise Held("capture/recovery require the bound baseline digest")
    flags = (
        args.grant_packet_sha256,
        args.exclusive_writer_window,
        args.fresh_vios_delete_approved,
    )
    if args.mode == "capture":
        if flags != (args.packet_sha256, True, True) or args.results is not None:
            raise Held("capture requires exact current source and cleanup assertions")
    elif any(flags):
        raise Held("read-only stage refuses effect assertions")
    if args.mode == "recover":
        if args.results != Path(p["window"]["directory"]) / "capture" / "results.json":
            raise Held("recovery must read this window's exact capture journal")
    elif args.results is not None:
        raise Held("results input is recovery-only")
    _current(p)
    _attest(p)
    _predecessor(p, stage, args.packet_sha256)
    _receipt(p, stage, args.packet_sha256, args.output)
    # Library warnings may include connection identity; discard only console diagnostics.
    # The bounded source observations/errors remain in their private journals.
    with (
        open(os.devnull, "w") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        code = asyncio.run(_run_stage(p, args, stage, started))
    print(
        "SOURCE ONLY: complete observations"
        if code == 0
        else "SOURCE ONLY: held; inspect private evidence"
    )
    return code


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    try:
        return _main(argv, started)
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:  # noqa: BLE001 — redact all console failure details
        own_pid = getattr(error, "own_process_pid", None)
        suffix = f"; own child PID {own_pid} unreaped" if type(own_pid) is int else ""
        print(
            f"SOURCE ONLY: stopped ({type(error).__name__}){suffix}; private recovery required",
            file=sys.stderr,
        )
        return 2
