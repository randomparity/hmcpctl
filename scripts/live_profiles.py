"""Run the profiles arm of the live integration suite against a real HMC.

Covers subtasks 0, 4, 10 and 15: the baseline, the profile and affinity reads,
the property round trips, and the baseline restore (issue #627). It is the only
arm that runs the system-wide profile backup and merge-restore.

Usage:
    uv run --no-sync python scripts/live_profiles.py

Equivalent to `live_test_runner.py --group profiles`, and named so that an
operator picking an arm from the runbook cannot mistype the group or land on
a different one. Results go to `test-results-profiles.json`.

**This mutates a managed system.** Run `scripts/live_test_preflight.py
--group profiles` first to see what it will touch.

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
GROUP = "profiles"


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
