"""Behavioural tests for the live-probe capture harness (issue #1161)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import asyncssh
import httpx
import pytest
from conftest import live_fixture
from defusedxml import ElementTree

LIVE_TEST_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(LIVE_TEST_ROOT))
from live_test import capture  # noqa: E402

from hmcpctl.client.core import HMCClient  # noqa: E402
from hmcpctl.jobs.requests import deploy_partition_template_job  # noqa: E402


class _Result:
    def __init__(self, exit_status: int, stdout: Any, stderr: Any) -> None:
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


def _fake_request(response: httpx.Response | BaseException):
    async def fake(self: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if isinstance(response, BaseException):
            raise response
        return response

    return fake


def _fake_run(result: _Result | BaseException):
    async def fake(self: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(result, BaseException):
            raise result
        return result

    return fake


def _install_rest(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    monkeypatch.setattr(HMCClient, "_request", _fake_request(response))


def _install_ssh(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    monkeypatch.setattr(asyncssh.SSHClientConnection, "run", _fake_run(result))


def _rest(
    method: str = "GET", path: str = "/rest/api/uom/x", **kwargs: Any
) -> httpx.Response:
    return asyncio.run(HMCClient._request(None, method, path, **kwargs))  # type: ignore[arg-type]


def _ssh(*args: Any, **kwargs: Any) -> Any:
    return asyncio.run(asyncssh.SSHClientConnection.run(None, *args, **kwargs))  # type: ignore[arg-type]


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    """An ignored destination outside any repository: always acceptable."""
    return tmp_path / "out.capture.jsonl"


def _git_repo(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("*.capture.jsonl\n")
    return root


def _response(text: str = "<ok/>", status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, text=text, headers=headers)


def test_rest_request_is_recorded(monkeypatch: pytest.MonkeyPatch, dest: Path) -> None:
    _install_rest(monkeypatch, _response("<feed/>", ETag="e1"))
    with capture.capture(dest) as cap:
        cap.step("list")
        resp = _rest(
            "POST",
            "/rest/api/uom/x",
            headers={
                "ACCEPT": "application/xml",
                "content-type": "application/vnd.ibm",
            },
            content=b"<body/>",
        )
    assert resp.text == "<feed/>"
    (rec,) = _records(dest)
    assert rec["kind"] == "rest"
    assert rec["step"] == "list"
    assert isinstance(rec["t"], float)
    assert rec["method"] == "POST"
    assert rec["path"] == "/rest/api/uom/x"
    assert rec["accept"] == "application/xml"
    assert rec["content_type"] == "application/vnd.ibm"
    assert rec["status"] == 200
    assert rec["response_headers"]["etag"] == "e1"
    assert rec["body"] == "<feed/>"
    assert rec["request_body"] == "<body/>"


def test_json_request_body_is_serialized(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(monkeypatch, _response())
    with capture.capture(dest):
        _rest("PUT", json={"a": 1})
    assert _records(dest)[0]["request_body"] == '{"a": 1}'


def test_steps_label_subsequent_records(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(monkeypatch, _response())
    with capture.capture(dest) as cap:
        _rest()
        cap.step("two")
        _rest()
    assert [r["step"] for r in _records(dest)] == [_records(dest)[0]["step"], "two"]
    assert _records(dest)[0]["step"] != "two"


def test_ssh_command_is_recorded(monkeypatch: pytest.MonkeyPatch, dest: Path) -> None:
    _install_ssh(monkeypatch, _Result(0, "out", b"err"))
    with capture.capture(dest) as cap:
        cap.step("ssh")
        _ssh("lssyscfg -r sys")
    (rec,) = _records(dest)
    assert rec["kind"] == "ssh"
    assert rec["step"] == "ssh"
    assert rec["command"] == "lssyscfg -r sys"
    assert rec["exit_status"] == 0
    assert rec["stdout"] == "out"
    assert rec["stderr"] == "err"


def test_ssh_command_keyword_argument(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_ssh(monkeypatch, _Result(0, None, None))
    with capture.capture(dest):
        _ssh(command="date", check=True)
    assert _records(dest)[0]["command"] == "date"


def test_failed_ssh_command_is_recorded_and_reraised(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    err = asyncssh.ProcessError(None, "x", None, 3, None, 3, b"bad out", b"bad err")
    _install_ssh(monkeypatch, err)
    with capture.capture(dest), pytest.raises(asyncssh.ProcessError) as raised:
        _ssh("false", check=True)
    assert raised.value is err
    (rec,) = _records(dest)
    assert (rec["exit_status"], rec["stdout"], rec["stderr"]) == (
        3,
        "bad out",
        "bad err",
    )


def test_transport_exception_is_recorded_and_reraised(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(monkeypatch, httpx.ConnectError("refused"))
    _install_ssh(monkeypatch, OSError("no route"))
    with capture.capture(dest):
        with pytest.raises(httpx.ConnectError):
            _rest()
        with pytest.raises(OSError, match="no route"):
            _ssh("date")
    rest, ssh = _records(dest)
    assert rest["exception"] == "ConnectError: refused"
    assert ssh["exception"] == "OSError: no route"


def test_cancellation_is_recorded_and_reraised(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(monkeypatch, asyncio.CancelledError("stop"))
    with capture.capture(dest), pytest.raises(asyncio.CancelledError):
        _rest()
    assert "CancelledError" in _records(dest)[0]["exception"]


def test_session_headers_are_dropped(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(
        monkeypatch,
        _response(
            **{
                "Set-Cookie": "s=1",
                "X-API-Session": "tok",
                "Authorization": "b",
                "X-Keep": "ok",
            }
        ),
    )
    with capture.capture(dest):
        _rest(
            headers={
                "x-api-session": "tok",
                "Cookie": "c",
                "Authorization": "a",
                "Accept": "x",
            }
        )
    (rec,) = _records(dest)
    assert rec["response_headers"]["x-keep"] == "ok"
    text = dest.read_text()
    for gone in ("tok", "s=1", "set-cookie", "authorization", "cookie"):
        assert gone not in text.lower()


def test_logon_bodies_are_redacted(monkeypatch: pytest.MonkeyPatch, dest: Path) -> None:
    _install_rest(monkeypatch, _response("<LogonResponse>SESSIONTOKEN</LogonResponse>"))
    with capture.capture(dest):
        _rest("PUT", "/rest/api/web/Logon", content="<user>u</user>")
    (rec,) = _records(dest)
    assert rec["body"] == capture.LOGON_REDACTED
    assert rec["request_body"] == capture.LOGON_REDACTED
    assert "SESSIONTOKEN" not in dest.read_text()


def test_logon_exception_text_is_redacted(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(monkeypatch, RuntimeError("bad user alice"))
    with capture.capture(dest), pytest.raises(RuntimeError):
        _rest("PUT", "/rest/api/web/Logon")
    assert _records(dest)[0]["exception"] == capture.LOGON_REDACTED


_TEMPLATE_BODY = deploy_partition_template_job(
    "u1", "u2", "tok-123"
)  # pragma: allowlist secret
_KEYWORDS = [
    "password",
    "PASSWD",
    "Passphrase",
    "SFTPKey",
    "SSHKey",
    "Private Key",
    "X_API_SESSION",
    "JSESSIONID: ",
]


@pytest.mark.parametrize("keyword", _KEYWORDS)
@pytest.mark.parametrize("field", ["request", "response", "exception"])
def test_secret_text_is_redacted_rest(
    monkeypatch: pytest.MonkeyPatch, dest: Path, field: str, keyword: str
) -> None:
    text = f"before {keyword}=hunter2 after"
    kwargs: dict[str, Any] = {}
    if field == "request":
        kwargs["content"] = text
        _install_rest(monkeypatch, _response())
    elif field == "response":
        _install_rest(monkeypatch, _response(text))
    else:
        _install_rest(monkeypatch, RuntimeError(text))
    with capture.capture(dest):
        try:
            _rest("PUT", **kwargs)
        except RuntimeError:
            pass
    assert "hunter2" not in dest.read_text()
    assert capture.SECRET_REDACTED in dest.read_text()


@pytest.mark.parametrize("keyword", _KEYWORDS)
@pytest.mark.parametrize("field", ["command", "stdout", "stderr"])
def test_secret_text_is_redacted_ssh(
    monkeypatch: pytest.MonkeyPatch, dest: Path, field: str, keyword: str
) -> None:
    text = f"x {keyword}=hunter2"
    parts = {"command": "date", "stdout": "o", "stderr": "e"}
    parts[field] = text
    _install_ssh(monkeypatch, _Result(0, parts["stdout"], parts["stderr"]))
    with capture.capture(dest):
        _ssh(parts["command"])
    assert "hunter2" not in dest.read_text()
    assert capture.SECRET_REDACTED in dest.read_text()


def _captured_error_body(session: str, cookie: str) -> str:
    """The captured 404 REST0005 body with a live-looking header echo put back."""
    body = live_fixture("rest-job-not-found")["body"]
    for redacted, value in (("SESSION", session), ("COOKIE", cookie)):
        token = next(
            form
            for form in (f"<REDACTED-{redacted}>", f"&lt;REDACTED-{redacted}&gt;")
            if form in body
        )
        body = body.replace(token, value)
    return body


def test_error_body_keeps_everything_but_the_echoed_session(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    body = _captured_error_body(
        "sess-0123abcd=", "JSESSIONID=js-4567:-1; CCFWSESSION=cc-89ef"
    )
    _install_rest(monkeypatch, _response(body, status=404))
    with capture.capture(dest):
        _rest()
    text = dest.read_text()
    for value in ("sess-0123abcd", "js-4567", "cc-89ef"):
        assert value not in text
    recorded = _records(dest)[0]["body"]
    assert "REST0005 No such Job" in recorded
    assert "cookie=redacted-session, " in recorded
    assert "x-api-session=redacted-session, " in recorded
    ElementTree.fromstring(recorded.strip())


@pytest.mark.parametrize("field", ["request", "response", "exception"])
@pytest.mark.parametrize(
    "secret",
    [
        "x-api-session=hunter2",
        "x-api-session=[hunter2, hunter2]",
        "cookie=JSESSIONID=hunter2; CCFWSESSION=hunter2",
        "cookie=JSESSIONID=hunter2; LtpaToken2=hunter2",
        "cookie=[JSESSIONID=hunter2, CCFWSESSION=hunter2]",
        "JSESSIONID=hunter2",
        "<X-API-Session>hunter2</X-API-Session>",
    ],
)
def test_session_values_are_redacted_and_the_rest_kept(
    monkeypatch: pytest.MonkeyPatch, dest: Path, field: str, secret: str
) -> None:
    text = f"{{before=1, {secret}, after=2}}"
    kwargs: dict[str, Any] = {}
    if field == "request":
        kwargs["content"] = text
        _install_rest(monkeypatch, _response())
    elif field == "response":
        _install_rest(monkeypatch, _response(text))
    else:
        _install_rest(monkeypatch, RuntimeError(text))
    with capture.capture(dest):
        try:
            _rest("PUT", **kwargs)
        except RuntimeError:
            pass
    written = dest.read_text()
    assert "hunter2" not in written
    assert capture.SECRET_REDACTED not in written
    assert "{before=1, " in written and ", after=2}" in written


@pytest.mark.parametrize(
    "text",
    [
        "X-API-Session: hunter2",
        "x-api-session=redacted-session X-API-Session hunter2",
        "x-api-session= hunter2",
        "x-api-session=abc hunter2",
        'x-api-session="abc hunter2"',
        "JSESSIONID= hunter2",
        "Cookie: LtpaToken2=hunter2",
        "cookie=JSESSIONID=abc\nhunter2",
        "cookie=JSESSIONID=abc<hunter2",
    ],
)
def test_a_session_keyword_outside_a_known_form_is_still_wholesale(
    monkeypatch: pytest.MonkeyPatch, dest: Path, text: str
) -> None:
    _install_rest(monkeypatch, _response(text))
    with capture.capture(dest):
        _rest()
    assert _records(dest)[0]["body"] == capture.SECRET_REDACTED


def test_template_deploy_memento_is_redacted(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(monkeypatch, _response())
    with capture.capture(dest):
        _rest("POST", "/rest/api/uom/job", content=_TEMPLATE_BODY)
    assert "tok-123" not in dest.read_text()
    assert _records(dest)[0]["request_body"] == capture.SECRET_REDACTED


def test_recorder_failure_does_not_change_the_result(
    monkeypatch: pytest.MonkeyPatch, dest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    response = _response()
    _install_rest(monkeypatch, response)
    _install_ssh(monkeypatch, _Result(0, "o", "e"))
    monkeypatch.setattr(capture, "_redact", _boom)
    with capture.capture(dest):
        assert _rest() is response
        assert _ssh("date").stdout == "o"
    assert dest.read_text() == ""
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1
    assert str(dest) in err[0]
    assert "recorder exploded" in err[0]


def _boom(*_a: Any, **_k: Any) -> Any:
    raise ValueError("recorder exploded")


def test_recorder_failure_still_reraises_original(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(monkeypatch, KeyError("orig"))
    monkeypatch.setattr(capture, "_redact", _boom)
    with capture.capture(dest), pytest.raises(KeyError, match="orig"):
        _rest()


def test_missing_parent_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = HMCClient._request
    target = tmp_path / "nope" / "a.capture.jsonl"
    with pytest.raises(ValueError, match="parent"), capture.capture(target):
        pass  # pragma: no cover
    assert not target.parent.exists()
    assert HMCClient._request is before


def test_unignored_destination_is_refused(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    target = repo / "plain.jsonl"
    before = (HMCClient._request, asyncssh.SSHClientConnection.run)
    with pytest.raises(ValueError, match="plain.jsonl"), capture.capture(target):
        pass  # pragma: no cover
    assert (HMCClient._request, asyncssh.SSHClientConnection.run) == before
    assert not target.exists()


def test_ignored_destination_in_repo_is_accepted(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    target = repo / "a.capture.jsonl"
    assert capture.destination_is_ignored(target)
    with capture.capture(target):
        pass


def test_destination_outside_a_repository_is_accepted(tmp_path: Path) -> None:
    assert capture.destination_is_ignored(tmp_path / "anything.jsonl")


def test_nested_entry_raises_and_outer_keeps_recording(
    monkeypatch: pytest.MonkeyPatch, dest: Path, tmp_path: Path
) -> None:
    _install_rest(monkeypatch, _response())
    with capture.capture(dest):
        inner = capture.capture(tmp_path / "inner.capture.jsonl")
        with pytest.raises(RuntimeError, match="already active"), inner:
            pass  # pragma: no cover
        _rest()
    assert len(_records(dest)) == 1
    assert not (tmp_path / "inner.capture.jsonl").exists()
    with capture.capture(dest):  # marker cleared on exit
        pass


@pytest.mark.parametrize("fail", [False, True])
def test_originals_are_restored_on_exit(
    monkeypatch: pytest.MonkeyPatch, dest: Path, fail: bool
) -> None:
    _install_rest(monkeypatch, _response())
    _install_ssh(monkeypatch, _Result(0, "", ""))
    before = (HMCClient._request, asyncssh.SSHClientConnection.run)
    try:
        with capture.capture(dest):
            assert HMCClient._request is not before[0]
            assert asyncssh.SSHClientConnection.run is not before[1]
            if fail:
                raise RuntimeError("probe failed")
    except RuntimeError:
        assert fail
    assert (HMCClient._request, asyncssh.SSHClientConnection.run) == before


def test_file_is_created_with_mode_0600(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    old = os.umask(0)
    try:
        with capture.capture(dest):
            pass
    finally:
        os.umask(old)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_existing_file_is_tightened_to_mode_0600(dest: Path) -> None:
    dest.write_text("")
    dest.chmod(0o644)
    with capture.capture(dest):
        pass
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_symlinked_destination_is_refused(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    tracked = repo / "notes.md"
    tracked.write_text("kept\n")
    link = repo / "probe.capture.jsonl"
    link.symlink_to(tracked)
    with pytest.raises(ValueError, match="symlink"), capture.capture(link):
        pass
    assert tracked.read_text() == "kept\n"
    assert not capture._ACTIVE


async def _chunks() -> Any:
    yield b"iso-bytes"


def test_streamed_request_body_is_a_placeholder_and_recording_continues(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(monkeypatch, _response())
    with capture.capture(dest):
        _rest("PUT", content=_chunks())
        _rest("GET")
    first, second = _records(dest)
    assert first["request_body"] == capture.STREAM_NOT_RECORDED
    assert second["kind"] == "rest"


def test_ssh_timeout_records_the_exception(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    err = asyncssh.TimeoutError(None, "sleep", None, None, None, None, b"part", b"")
    _install_ssh(monkeypatch, err)
    with capture.capture(dest), pytest.raises(asyncssh.TimeoutError):
        _ssh("sleep 99", timeout=1)
    (rec,) = _records(dest)
    assert rec["exception"].startswith("TimeoutError")
    assert rec["stdout"] == "part"
    assert rec["exit_status"] is None


def test_record_completing_after_exit_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, dest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario() -> None:
        gate = asyncio.Event()

        async def slow(self: Any, method: str, path: str, **kwargs: Any) -> Any:
            await gate.wait()
            return _response()

        monkeypatch.setattr(HMCClient, "_request", slow)
        with capture.capture(dest):
            task = asyncio.ensure_future(
                HMCClient._request(None, "GET", "/x")  # type: ignore[arg-type]
            )
            await asyncio.sleep(0)
        gate.set()
        await task

    asyncio.run(scenario())
    assert dest.read_text() == ""
    assert capsys.readouterr().err == ""  # no write attempted on the closed fd


# --- raw mode (#1202) ---------------------------------------------------------


@pytest.mark.parametrize("keyword", _KEYWORDS[:6])
def test_raw_mode_keeps_answers_that_name_a_secret_keyword(
    monkeypatch: pytest.MonkeyPatch, dest: Path, keyword: str
) -> None:
    body = f"<UserProfile><{keyword}Policy>on</{keyword}Policy></UserProfile>"
    _install_rest(monkeypatch, _response(body))
    with capture.capture(dest, raw=True):
        _rest("GET")
    assert _records(dest)[0]["body"] == body


def test_raw_mode_keeps_command_output_and_exceptions(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_ssh(monkeypatch, _Result(0, "passwd_policy=on", "SSHKey missing"))
    with capture.capture(dest, raw=True):
        _ssh("lshmcusr -F passwd_policy")
    (rec,) = _records(dest)
    assert (rec["stdout"], rec["stderr"]) == ("passwd_policy=on", "SSHKey missing")


def test_raw_mode_still_redacts_requests_logons_and_sessions(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(
        monkeypatch, _response("{cookie=JSESSIONID=abc123, x-api-session=tok}")
    )
    with capture.capture(dest, raw=True):
        _rest("PUT", "/rest/api/uom/x", content="password=hunter2")
        _rest("PUT", "/rest/api/web/Logon", content="<user>u</user>")
    first, logon = _records(dest)
    assert first["request_body"] == capture.SECRET_REDACTED
    assert "abc123" not in first["body"]
    assert "tok}" not in first["body"]
    assert logon["body"] == logon["request_body"] == capture.LOGON_REDACTED


def test_default_mode_is_still_wholesale(
    monkeypatch: pytest.MonkeyPatch, dest: Path
) -> None:
    _install_rest(monkeypatch, _response("<PasswordPolicy>on</PasswordPolicy>"))
    with capture.capture(dest):
        _rest("GET")
    assert _records(dest)[0]["body"] == capture.SECRET_REDACTED


def test_raw_mode_refuses_any_destination_in_a_work_tree(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    target = repo / "a.capture.jsonl"
    assert capture.destination_is_ignored(target)
    with (
        pytest.raises(ValueError, match="inside a git work tree"),
        capture.capture(target, raw=True),
    ):
        pass
    assert not target.exists()


def test_raw_mode_accepts_a_destination_outside_any_repository(dest: Path) -> None:
    with capture.capture(dest, raw=True):
        pass
    assert dest.exists()


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize(
    "redirect", ["missing-dir", "other-worktree", "bare-config", "ceiling"]
)
def test_git_environment_cannot_hide_capture_repository(
    tmp_path, monkeypatch, raw, redirect
):
    repo = _git_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = nested / ("a.capture.jsonl" if raw else "plain.jsonl")
    if redirect == "missing-dir":
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing.git"))
    elif redirect == "other-worktree":
        monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(outside))
    elif redirect == "bare-config":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    else:
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(nested))
    original = (HMCClient._request, asyncssh.SSHClientConnection.run)
    with pytest.raises(ValueError), capture.capture(target, raw=raw):
        pytest.fail("Git environment hid the physical repository")
    assert not target.exists()
    assert (HMCClient._request, asyncssh.SSHClientConnection.run) == original


@pytest.mark.parametrize("raw", [False, True])
def test_capture_refuses_unknown_git_discovery_failure(tmp_path, monkeypatch, raw):
    target = tmp_path / "a.capture.jsonl"
    failure = subprocess.CompletedProcess(
        [], 128, stdout="", stderr="fatal: unsafe repository"
    )
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **kw: failure)
    with pytest.raises(ValueError, match="Git"), capture.capture(target, raw=raw):
        pytest.fail("An unknown Git failure admitted capture")
    assert not target.exists()


def test_capture_refuses_git_ignore_query_failure(tmp_path, monkeypatch):
    target = tmp_path / "a.capture.jsonl"
    replies = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="true\n", stderr=""),
            subprocess.CompletedProcess(
                [], 128, stdout="", stderr="fatal: cannot read index"
            ),
        ]
    )
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **kw: next(replies))
    with pytest.raises(ValueError, match="Git"), capture.capture(target):
        pytest.fail("An unknown Git ignore-query failure admitted capture")
    assert not target.exists()
