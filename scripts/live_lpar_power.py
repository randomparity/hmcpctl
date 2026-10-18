"""Run the lpar-power arm of the live integration suite against a real HMC.

Covers subtask 41: it creates partitions named `hmcpctl-live-pwr-<8 hex>...`,
exercises create's refusals, power on and off, the composite power operation,
console capture and delete, then provisions and decommissions a second one with a
run-owned 1 GiB VIOS volume `lppwr<8 hex>`, and deletes that volume (issue #1346).
It touches no other partition.

Usage:
    uv run --no-sync python scripts/live_lpar_power.py

Equivalent to `live_test_runner.py --group lpar-power`, and named so that an
operator picking an arm from the runbook cannot mistype the group or land on a
different one. Results go to `test-results-lpar-power.json`.

**This mutates a managed system.** Run `scripts/live_test_preflight.py
--group lpar-power` first to see what it will touch.

Pass --detach-probe for the bounded mapping/RMC comparison on one owned scratch
partition and one 1 GiB volume; all other run inputs remain configuration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import runner

#: The one argument vector this wrapper dispatches.
GROUP = "lpar-power"


def main(argv: list[str] | None = None) -> int:
    """Dispatch this arm through the runner's own argument entry point.

    `_run_from_arguments`, not `main`: the credential bootstrap, the
    git-ignored-destination guard and the per-group results path all live in
    the former. Calling `main` directly would run uncredentialled and write
    every arm's results over `test-results-round2.json`.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--detach-probe",
        action="store_true",
        help="run the bounded mapping/RMC comparison only",
    )
    args = parser.parse_args(argv)
    dispatched = ["--group", GROUP]
    if args.detach_probe:
        dispatched.append("--detach-probe")
    return runner._run_from_arguments(dispatched)


if __name__ == "__main__":
    raise SystemExit(main())
