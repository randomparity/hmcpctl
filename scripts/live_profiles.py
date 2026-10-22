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

Use --partition-lifecycle for the separately authorized saved-profile lifecycle
on one explicitly configured inactive disposable partition. Preflight that arm
with --group partition-profiles; its results are test-results-partition-profiles.json.
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
    # Parse before dispatch so help and invalid options never start a live run.
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--partition-lifecycle", action="store_true")
    args = parser.parse_args(argv)
    return runner._run_from_arguments(
        ["--group", "partition-profiles" if args.partition_lifecycle else GROUP]
    )


if __name__ == "__main__":
    raise SystemExit(main())
