"""Run the vg-if-match arm of the live integration suite against a real HMC.

Covers subtasks 0 (read-only baseline) and 42 (issue #1850, ADR 0328). ST42 creates
one scratch volume group named by `LIVE_TEST_SCRATCH_VG_NAME` (`hpvg<8 hex>`) on the
one free physical volume `LIVE_TEST_SCRATCH_PV_NAME`, only after its free/no-VG
identity guards. Between create and cleanup it POSTs that group's own body back under
an `If-Match` no ETag ever had, and records the HTTP status and, from an error body,
only its message and codes. Cleanup removes the group with ST42's guarded `reducevg`
and compares the physical-volume and group snapshots with the pre-run ones.

Usage:
    uv run --no-sync python scripts/live_vg_if_match.py

Equivalent to `live_test_runner.py --group vg-if-match`. Results go to
`test-results-vg-if-match.json`.

**This mutates a managed system.** Run `scripts/live_test_preflight.py
--group vg-if-match` first to see what it will touch.

Takes no options: everything else about a run is configuration, and the
runner itself is there for an invocation this does not cover.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import runner

#: The one argument vector this wrapper dispatches.
GROUP = "vg-if-match"


def main(argv: list[str] | None = None) -> int:
    """Dispatch this arm through the runner's own argument entry point.

    `_run_from_arguments`, not `main`: the credential bootstrap, the
    git-ignored-destination guard and the per-group results path all live in
    the former. Calling `main` directly would run uncredentialled and write
    every arm's results over `test-results-round2.json`.
    """
    # Parsed even though there are no options: without this, `--help` would be
    # ignored and the wrapper would start mutating a managed system instead of
    # explaining itself.
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args(argv)
    return runner._run_from_arguments(["--group", GROUP])


if __name__ == "__main__":
    raise SystemExit(main())
