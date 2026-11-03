"""The built-media build-entry runner (ADR 0324 D2, D3)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

from hmcpctl.operations.lpar.install_media import MAX_PRODUCER_RESULT_BYTES
from hmcpctl.operations.lpar.media_build import BuildFailed, run_build_entry

REQUEST = b'{"format": "iso-chain-target-v1"}'


def _entry(tmp_path: Path, body: str) -> str:
    """An executable entry running a Python body that sees ``argv`` and ``record``.

    A ``/bin/sh`` wrapper, because a shebang cannot name an interpreter whose path
    holds a space.
    """
    script = tmp_path / "entry.py"
    script.write_text(
        f"import json, os, sys\nrecord = {str(tmp_path / 'record.json')!r}\n{body}\n"
    )
    path = tmp_path / "build-entry"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    path.chmod(0o700)
    return str(path)


def _run(command: tuple[str, ...], timeout: float = 30.0) -> bytes:
    return asyncio.run(run_build_entry(command, REQUEST, timeout=timeout))


def test_stdout_is_returned(tmp_path):
    entry = _entry(tmp_path, "sys.stdout.write('{\"format\": 1}\\n')")
    assert _run((entry, "{target}")) == b'{"format": 1}\n'


def test_the_target_file_is_private_and_only_the_target_varies(tmp_path, monkeypatch):
    monkeypatch.setenv("HMC_PASSWORD", "not-for-the-builder")
    monkeypatch.setenv("BUILD_PROBE", "kept")
    entry = _entry(
        tmp_path,
        "path = sys.argv[3]\n"
        "json.dump({'argv': sys.argv[1:], 'mode': os.stat(path).st_mode,"
        " 'dir_mode': os.stat(os.path.dirname(path)).st_mode,"
        " 'content': open(path).read(), 'env': sorted(os.environ)},"
        " open(record, 'w'))",
    )
    _run((entry, "container-build", "--target", "{target}", "--flag"))
    seen = json.loads((tmp_path / "record.json").read_text())
    target = seen["argv"][2]
    assert seen["argv"] == ["container-build", "--target", target, "--flag"]
    assert stat.S_IMODE(seen["mode"]) == 0o600
    assert stat.S_IMODE(seen["dir_mode"]) == 0o700
    assert seen["content"] == REQUEST.decode()
    assert not Path(target).exists()
    assert "BUILD_PROBE" in seen["env"]
    assert not [name for name in seen["env"] if name.upper().startswith("HMC_")]


def test_a_non_zero_exit_fails_naming_the_status(tmp_path):
    entry = _entry(tmp_path, "print('{}'); sys.exit(3)")
    with pytest.raises(BuildFailed, match="status 3"):
        _run((entry, "{target}"))


def test_a_timeout_kills_the_build(tmp_path):
    entry = _entry(
        tmp_path,
        "open(record, 'w').write(str(os.getpid()))\nimport time\ntime.sleep(30)",
    )
    started = time.monotonic()
    with pytest.raises(BuildFailed, match="0.5 seconds"):
        _run((entry, "{target}"), timeout=0.5)
    assert time.monotonic() - started < 5
    pid = int((tmp_path / "record.json").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_output_past_the_bound_fails(tmp_path):
    size = MAX_PRODUCER_RESULT_BYTES + 1
    entry = _entry(tmp_path, f"sys.stdout.write('x' * {size})")
    with pytest.raises(BuildFailed, match="64 KiB"):
        _run((entry, "{target}"))


def test_a_missing_executable_fails(tmp_path):
    with pytest.raises(BuildFailed, match="could not start"):
        _run((str(tmp_path / "absent"), "{target}"))


def test_an_unwritable_request_file_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "missing"))
    entry = _entry(tmp_path, "pass")
    with pytest.raises(BuildFailed, match="request file"):
        _run((entry, "{target}"))
