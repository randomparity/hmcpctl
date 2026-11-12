"""Strict packet admission, source attestation, and durable private journals."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import selectors
import subprocess  # nosec B404 # Call sites document execution authority.
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import hmcpctl
from hmcpctl.documents.partition_profile import ATOM, UOM
from hmcpctl.resource_identity import is_uuid

MIB = 1024 * 1024
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
    # Fixed attestation queries override Git routing, fsmonitor and untracked cache.
    process = subprocess.Popen(  # nosec B603
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
