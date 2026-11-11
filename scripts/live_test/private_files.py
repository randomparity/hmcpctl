"""Private evidence files and physical candidate checks for standalone live arms."""

from __future__ import annotations

import inspect
import json
import os
import stat
import subprocess  # nosec B404 # Call sites document execution authority.
import tempfile
from pathlib import Path

from hmcpctl.client.core import HMCClient

ROOT = Path(__file__).resolve().parents[2]
LIMIT = 1024 * 1024


def connection(config):
    return {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "verify_ssl": config.verify_ssl,
        "agent_id": config.agent_id or "hmcpctl",
    }


def revision(candidate):
    def git(*args):
        # Candidate hashes are compared with fixed Git queries, never executed.
        output = subprocess.check_output(  # nosec B603, B607
            ["git", "--no-pager", *args],
            cwd=ROOT,
            text=True,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
        )
        return output.strip()

    if (
        git("rev-parse", "HEAD") != candidate["commit"]
        or git("rev-parse", "HEAD^{tree}") != candidate["tree"]
        or git("status", "--porcelain", "--untracked-files=all")
        or Path(inspect.getfile(HMCClient)).resolve()
        != ROOT / "src/hmcpctl/client/core.py"
    ):
        raise ValueError("ST63 clean deployed candidate/import differs")


def private_path(path, *, fresh=False, maximum=LIMIT):
    if type(maximum) is not int or maximum not in (LIMIT, 4 * LIMIT, 8 * LIMIT):
        raise ValueError("fixed 1/4/8MiB private-file ceiling required")
    path = Path(path).absolute()
    resolved = path.resolve()
    parent = path.parent.lstat()
    if (
        path.is_symlink()
        or path.parent.is_symlink()
        or resolved == ROOT
        or ROOT in resolved.parents
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o077
    ):
        raise ValueError("ST63 private operator-owned path outside checkout required")
    if fresh:
        if path.exists():
            raise ValueError("ST63 fresh output cannot replace prior intent")
    else:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise ValueError("ST63 bounded private regular file required")
    return path


def read(path, *, maximum=LIMIT):
    with private_path(path, maximum=maximum).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("private input exceeds fixed byte bound")
    return json.loads(raw)


def write(path, document, *, fresh=False, maximum=LIMIT):
    raw = json.dumps(document, sort_keys=True, indent=2).encode()
    if type(maximum) is not int or maximum not in (LIMIT, 4 * LIMIT, 8 * LIMIT):
        raise ValueError("fixed 1/4/8MiB private-file ceiling required")
    if len(raw) > maximum:
        raise ValueError("ST63 retained evidence exceeds bound")
    path = private_path(path, fresh=fresh, maximum=maximum)
    if fresh:
        with path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".st63-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
