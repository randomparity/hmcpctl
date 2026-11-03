"""Run one native boot-started arm of the live integration suite against a real HMC.

Covers subtask 72 in the `native-ubuntu-boot-started` or `native-rocky-boot-started`
group (issue #1850, ADR 0328). It provisions one scratch partition named
`hmcpctl-live-pwr-<8 hex>-p` on a run-owned 1 GiB volume, boots the producer's
launcher for that profile with the configured MAC and the producer's network, and
reads refcodes and partition state every 15 s for at most 15 minutes from the
first provision call. It then powers the partition off, unmounts and deletes the
media, and deletes the partition and the volume. It touches no other partition.

Usage:
    uv run --no-sync python scripts/live_native_boot_started.py {ubuntu,rocky}

Results go to `test-results-native-<profile>-boot-started.json`.

**This mutates a managed system and puts a guest on the network.** Run
`scripts/live_test_preflight.py --group native-<profile>-boot-started` first to
see what it will touch.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import runner

#: The group each profile argument dispatches.
GROUPS = {
    "ubuntu": "native-ubuntu-boot-started",
    "rocky": "native-rocky-boot-started",
}


def main(argv: list[str] | None = None) -> int:
    """Dispatch one profile's group through the runner's own argument entry point.

    `_run_from_arguments`, not `main`: the credential bootstrap, the
    git-ignored-destination guard and the per-group results path all live there.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("profile", choices=sorted(GROUPS))
    args = parser.parse_args(argv)
    return runner._run_from_arguments(["--group", GROUPS[args.profile]])


if __name__ == "__main__":
    raise SystemExit(main())
