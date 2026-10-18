"""Run the bare-CEC arm of the live integration suite against a real HMC.

Covers subtask 25: create a partition, give it a dedicated slot, activate it to
SMS, observe it, exercise the power-off variants, and delete it (issue #876).

Usage:
    uv run --no-sync python scripts/live_bare_cec.py

Equivalent to `live_test_runner.py --group bare-cec`, and named so that an
operator picking an arm from the runbook cannot mistype the group or land on
a different one. Results go to `test-results-bare-cec.json`.

**This mutates a managed system**: it creates, powers on and deletes a
partition. Run `scripts/live_test_preflight.py --group bare-cec` first to see
what it will touch.

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
GROUP = "bare-cec"


def main(argv: list[str] | None = None) -> int:
    """Dispatch this arm through the runner's own argument entry point.

    `_run_from_arguments`, not `main`: the credential bootstrap, the
    git-ignored-destination guard and the per-group results path all live in
    the former.
    """
    # Parsed even though there are no options, so `--help` explains instead of
    # starting a run that mutates a managed system.
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args(argv)
    return runner._run_from_arguments(["--group", GROUP])


if __name__ == "__main__":
    raise SystemExit(main())
