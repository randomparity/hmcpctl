"""Run the users arm of the live integration suite against a real HMC.

Covers subtask 11: the user, task-role, resource-role and remote-access reads,
then one scratch user's create, read, modify, clear and delete (issue #632). It
is the only arm that creates an HMC user. The user is named
`hmcpctl-live-<8 hex>`, has the viewer task role and no remote access, and is
deleted by UUID in the same run.

Usage:
    uv run --no-sync python scripts/live_users.py

Equivalent to `live_test_runner.py --group users`, and named so that an
operator picking an arm from the runbook cannot mistype the group or land on
a different one. Results go to `test-results-users.json`.

**This mutates a managed system's HMC.** Run `scripts/live_test_preflight.py
--group users` first to see what it will touch.

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
GROUP = "users"


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
