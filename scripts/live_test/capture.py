"""Record what hmcpctl sends to and receives from a live HMC.

A probe script wraps its calls in the context manager and labels phases::

    with capture(Path("hmc-captures/probe.capture.jsonl")) as cap:
        cap.step("create")
        ...  # drive hmcpctl against the HMC

Every REST request (`HMCClient._request`) and SSH command
(`asyncssh.SSHClientConnection.run`) becomes one JSON line. Both are patched on
the class, so modules that imported `run_hmc_command` by name are still seen.
Secrets are redacted before anything is written; see the issue #1161 design spec.

``capture(path, raw=True)`` keeps response bodies, command output and exception text
whole even when they name a secret keyword, for the read-only sweep of issue #1202,
whose exporter tokenizes them afterwards. Logon exchanges, request bodies, commands,
session headers and echoed session values are still redacted, and a raw destination must be outside every
git work tree.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, TypeVar, cast

import asyncssh
import httpx

from hmcpctl.client.core import HMCClient

_Callable = TypeVar("_Callable", bound=Callable[..., Any])

STREAM_NOT_RECORDED = "<stream: not recorded>"
LOGON_REDACTED = "<redacted: logon>"
SECRET_REDACTED = "<redacted: secret>"
#: Replaces a session value inside text that is otherwise kept. No angle brackets, so
#: an XML body stays well formed.
SESSION_REDACTED = "redacted-session"

_SECRET_KEYWORDS = (
    "password",
    "passwd",
    "passphrase",
    "sftpkey",
    "sshkey",
    "private key",
    "x-api-session",
    "x_api_session",
    "jsessionid",
    "ccfwsession",
    "cookie:",
)
# V10R3 echoes the request headers in every HttpErrorResponse body, as
# `{…, cookie=JSESSIONID=…; CCFWSESSION=…, …, x-api-session=…, …}` (#1161).
# These forms lose only their values. The text is still replaced wholesale when a
# redacted value is not followed by its form's delimiter, or when any other secret
# keyword is left in it.
_LIST_VALUE = r"\[[^\]]*\]"
_SESSION_VALUES = (
    re.compile(rf"(?i)\b(cookie=)(?:{_LIST_VALUE}|[^,}}\n<\[]+)"),
    re.compile(rf"(?i)\b(x-api-session=)(?:{_LIST_VALUE}|[^,}}\s\[]+)"),
    re.compile(r"(?i)\b((?:jsessionid|ccfwsession)=)[^;,}\s]+"),
    re.compile(r"(?i)(<x-api-session>)[^<]+"),
)
_UNDELIMITED_SESSION = re.compile(rf"{SESSION_REDACTED}(?![,;}}]|</|$)")
_REDACTED_SESSION_FORMS = re.compile(
    rf"(?i)\b(?:x-api-session|jsessionid|ccfwsession)={SESSION_REDACTED}"
    rf"|<x-api-session>{SESSION_REDACTED}</x-api-session>"
)
_DROPPED_HEADERS = frozenset({"x-api-session", "cookie", "set-cookie", "authorization"})

#: Set while a context is live. Module-level because the patch it guards is global.
_ACTIVE = False


def destination_is_ignored(path: Path) -> bool:
    """Report whether Git would let *path* be committed from where it is written.

    Same rule as `scripts/live_test_runner.py`: exit 1 from `git check-ignore -q`
    means an unignored path inside a repository; exit 0 (ignored) and anything
    else (no repository, or outside one) leave nothing committable.
    """
    target = path.parent.resolve() / path.name
    proc = subprocess.run(
        ["git", "-C", str(target.parent), "check-ignore", "-q", str(target)],
        capture_output=True,
        check=False,
    )
    return proc.returncode != 1


def inside_work_tree(path: Path) -> bool:
    """Report whether *path*'s directory lies inside any git work tree."""
    proc = subprocess.run(
        ["git", "-C", str(path.parent.resolve()), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _text(value: str | bytes | None) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _redact(text: str | bytes | None, *, logon: bool = False) -> str | None:
    """Redact session values in *text*; replace it wholesale when it still names a secret.

    A logon exchange is always replaced wholesale.
    """
    value = _text(text)
    if value is None:
        return None
    if logon:
        return LOGON_REDACTED
    for pattern in _SESSION_VALUES:
        value = pattern.sub(rf"\g<1>{SESSION_REDACTED}", value)
    if _UNDELIMITED_SESSION.search(value):
        return SECRET_REDACTED
    residue = _REDACTED_SESSION_FORMS.sub("", value).lower()
    if any(word in residue for word in _SECRET_KEYWORDS):
        return SECRET_REDACTED
    return value


def _safe_headers(headers: Any) -> dict[str, str]:
    return {
        k: v
        for k, v in httpx.Headers(headers).items()
        if k.lower() not in _DROPPED_HEADERS
    }


def _exception_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class Capture:
    """Writes records to one destination and labels them with the current step."""

    def __init__(self, path: Path, fd: int, *, raw: bool = False) -> None:
        self._path = path
        self._fd = fd
        self._raw = raw
        self._step = "init"
        self._enabled = True

    def step(self, name: str) -> None:
        """Label the records that follow."""
        self._step = name

    def _answer(self, text: str | bytes | None, *, logon: bool = False) -> str | None:
        """Redact what the HMC answered; raw keeps all of it but logons and sessions."""
        if not self._raw or logon:
            return _redact(text, logon=logon)
        value = _text(text)
        for pattern in _SESSION_VALUES:
            value = value and pattern.sub(rf"\g<1>{SESSION_REDACTED}", value)
        return value

    def _emit(self, build: Callable[..., dict[str, Any]], *args: Any) -> None:
        """Write one record; on any recorder failure, disable and say so once."""
        if not self._enabled:
            return
        try:
            record = {"step": self._step, "t": time.time(), **build(*args)}
            os.write(self._fd, (json.dumps(record, default=str) + "\n").encode())
        except Exception as exc:  # noqa: BLE001 - recording must never alter the call
            self._enabled = False
            print(f"capture: recording to {self._path} stopped: {exc}", file=sys.stderr)

    def _rest_record(
        self, method: str, path: str, kwargs: dict[str, Any], outcome: Any
    ) -> dict[str, Any]:
        logon = "Logon" in path
        headers = httpx.Headers(kwargs.get("headers"))
        body = (
            json.dumps(kwargs["json"])
            if kwargs.get("json") is not None
            else kwargs.get("content")
        )
        if body is not None and not isinstance(body, str | bytes):
            body = STREAM_NOT_RECORDED
        record: dict[str, Any] = {
            "kind": "rest",
            "method": method,
            "path": path,
            "accept": headers.get("accept"),
            "content_type": headers.get("content-type"),
            "request_body": _redact(body, logon=logon),
        }
        if isinstance(outcome, BaseException):
            record["exception"] = self._answer(_exception_text(outcome), logon=logon)
        else:
            record["status"] = outcome.status_code
            record["response_headers"] = _safe_headers(outcome.headers)
            record["body"] = self._answer(outcome.text, logon=logon)
        return record

    def _ssh_record(self, command: Any, outcome: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"kind": "ssh", "command": _redact(command)}
        # A ProcessError still carries the command's exit status and output.
        completed = not isinstance(outcome, BaseException) or isinstance(
            outcome, asyncssh.ProcessError
        )
        if not completed or isinstance(outcome, asyncssh.TimeoutError):
            record["exception"] = self._answer(_exception_text(outcome))
        if completed:
            record["exit_status"] = outcome.exit_status
            record["stdout"] = self._answer(outcome.stdout)
            record["stderr"] = self._answer(outcome.stderr)
        return record

    def wrap_request(self, original: _Callable) -> _Callable:
        @functools.wraps(original)
        async def spy(client: Any, method: str, path: str, **kwargs: Any) -> Any:
            try:
                response = await original(client, method, path, **kwargs)
            except BaseException as exc:
                self._emit(self._rest_record, method, path, kwargs, exc)
                raise
            self._emit(self._rest_record, method, path, kwargs, response)
            return response

        return cast(_Callable, spy)

    def wrap_run(self, original: _Callable) -> _Callable:
        @functools.wraps(original)
        async def spy(conn: Any, *args: Any, **kwargs: Any) -> Any:
            command = args[0] if args else kwargs.get("command")
            try:
                result = await original(conn, *args, **kwargs)
            except BaseException as exc:
                self._emit(self._ssh_record, command, exc)
                raise
            self._emit(self._ssh_record, command, result)
            return result

        return cast(_Callable, spy)


@contextlib.contextmanager
def capture(path: Path, *, raw: bool = False) -> Generator[Capture]:
    """Record every HMC REST call and SSH command made inside the block to *path*.

    *raw* keeps answers unredacted (see the module docstring) and refuses a
    destination inside any git work tree, ignored or not.
    """
    global _ACTIVE
    if _ACTIVE:
        raise RuntimeError(
            "a capture is already active; nested capture() is unsupported"
        )
    if not path.parent.is_dir():
        raise ValueError(
            f"capture destination parent {path.parent} does not exist; create it first"
        )
    if raw and inside_work_tree(path):
        raise ValueError(
            f"raw capture destination {path} is inside a git work tree; "
            "write raw captures to a private directory outside every repository"
        )
    if not destination_is_ignored(path):
        raise ValueError(
            f"capture destination {path} is not git-ignored; use a name ending in "
            ".capture.jsonl or a path under hmc-captures/"
        )
    # O_NOFOLLOW: git judged the link's own name, not its target, so a link to a
    # tracked file would pass the ignore check and land captures in that file.
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        if path.is_symlink():
            raise ValueError(
                f"capture destination {path} is a symlink; name a regular file"
            ) from exc
        raise
    os.fchmod(fd, 0o600)
    _ACTIVE = True
    original_request = HMCClient._request
    original_run = asyncssh.SSHClientConnection.run
    cap = Capture(path, fd, raw=raw)
    try:
        HMCClient._request = cap.wrap_request(original_request)  # type: ignore[method-assign]
        asyncssh.SSHClientConnection.run = cap.wrap_run(original_run)  # type: ignore[method-assign]
        yield cap
    finally:
        HMCClient._request = original_request  # type: ignore[method-assign]
        asyncssh.SSHClientConnection.run = original_run  # type: ignore[method-assign]
        cap._enabled = False  # a spy still in flight must not write to a reused fd
        os.close(fd)
        _ACTIVE = False
