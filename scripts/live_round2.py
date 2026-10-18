"""Run the round2 arm of the live integration suite against a real HMC.

Covers subtasks 0-15: connectivity, inventory, LPAR lifecycle, networking, storage, users, metrics and the CLI escape hatch.

Usage:
    uv run --no-sync python scripts/live_round2.py

Equivalent to `live_test_runner.py --group round2`, and named so that an
operator picking an arm from the runbook cannot mistype the group or land on
a different one. Results go to `test-results-round2.json`.

**This mutates a managed system.** Run `scripts/live_test_preflight.py
--group round2` first to see what it will touch.

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
GROUP = "round2"


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
