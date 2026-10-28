"""The live-testing runbook must stay executable.

Its whole purpose is to be followed by an agent that will not read the source.
A command in it that no longer exists is a defect, not a stale doc, so every
script path and flag it names is checked against the tree.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "live-testing.md"
TEXT = RUNBOOK.read_text(encoding="utf-8")

#: Every `scripts/...py` the runbook names, brace-expanded.
_SCRIPT_RE = re.compile(r"scripts/[A-Za-z0-9_{},]+\.py")


def _named_scripts() -> set[str]:
    names = set()
    for match in _SCRIPT_RE.findall(TEXT):
        brace = re.search(r"\{([^}]+)\}", match)
        if brace is None:
            names.add(match)
            continue
        names.update(
            match.replace(brace.group(0), option)
            for option in brace.group(1).split(",")
        )
    return names


def test_the_runbook_names_at_least_the_scripts_it_documents():
    """Guards the check below: an empty scan would pass vacuously."""
    assert len(_named_scripts()) >= 7


@pytest.mark.parametrize("script", sorted(_named_scripts()))
def test_every_script_the_runbook_names_exists(script):
    assert (REPO_ROOT / script).is_file()


@pytest.mark.parametrize(
    "script",
    [
        "scripts/live_test_preflight.py",
        "scripts/live_test_evidence.py",
        "scripts/live_test_recovery.py",
        "scripts/live_round2.py",
        "scripts/live_vmedia.py",
        "scripts/live_sriov.py",
        "scripts/live_dedicated.py",
    ],
)
def test_every_documented_script_responds_to_help(script):
    """A script the runbook sends an operator to must explain itself."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / script), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize(
    "flag",
    ["--skip-hardware", "--group dedicated", "--results test-results-dedicated.json"],
)
def test_every_flag_the_runbook_shows_is_one_the_script_accepts(flag):
    option = flag.split()[0]
    script = {
        "--skip-hardware": "scripts/live_test_preflight.py",
        "--group": "scripts/live_test_preflight.py",
        "--results": "scripts/live_test_recovery.py",
    }[option]
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / script), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )

    assert option in result.stdout


def test_the_runbook_prescribes_no_sync_on_every_uv_invocation():
    """A bare `uv run` prunes the app extra and the runner stops importing."""
    assert not re.search(r"uv run (?!--no-sync)", TEXT)
    assert "uv run --no-sync" in TEXT


def test_the_runbook_states_that_result_ids_exceed_dispatch_ids():
    """Rows reach 34 while the dispatchable range stops at 24 (Success 1)."""
    assert "34" in TEXT
    assert "24" in TEXT


def test_the_runbook_names_the_config_directory_of_every_platform_it_runs_on():
    """A Linux-only path sent the first macOS operator to a directory macOS
    never reads. The runner resolves the directory per platform; the runbook
    has to say so, because an operator reads the runbook, not `config.py`."""
    assert "Library/Application Support/hmcpctl" in TEXT
    assert ".config/hmcpctl" in TEXT


def test_agents_md_points_at_the_runbook():
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "docs/live-testing.md" in agents
    assert "evidence only for the commit it ran on" in agents


@pytest.mark.parametrize(
    "script,flag",
    [
        ("scripts/live_vios_boot_source.py", "--grant-packet-sha256"),
        ("scripts/live_test_preflight.py", "vios-boot-source"),
        ("scripts/live_test_recovery.py", "--vios-boot-source-packet"),
    ],
)
def test_boot_source_runbook_entrypoints(script, flag):
    # The runbook's existing path checks enforce installed script references;
    # these are executable parser contracts, not prose assertions.
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / script), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0 and flag in result.stdout


def test_boot_source_no_default_dispatch():
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from live_test import runner

    assert "vios-boot-source" not in runner.SUBTASK_GROUPS
