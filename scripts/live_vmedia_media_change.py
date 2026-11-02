"""Run the optical media change capture (#1284) against a real HMC.

Covers subtask 70 between ST16, which resolves the VIOS, the test partition and the
media repository, and ST22, which removes any medium ST70 left. ST70 creates two blank
media, mounts one to the test partition, sends two raw `ViosSCSIMapping` writes (one
expected refused), swaps media with `viosvrcmd` `unloadopt`/`loadopt`, and powers the
test partition on and off once, recording every HMC call to a git-ignored capture file
under `hmc-captures/`.

Usage:
    uv run --no-sync python scripts/live_vmedia_media_change.py

Equivalent to `live_test_runner.py --group vmedia-media-change`. Results go to
`test-results-vmedia-media-change.json`.

**This mutates a managed system.** Run `scripts/live_test_preflight.py
--group vmedia-media-change` first to see what it will touch.

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
GROUP = "vmedia-media-change"


def main(argv: list[str] | None = None) -> int:
    """Dispatch this arm through the runner's own argument entry point."""
    # Parsed even though there are no options: without this, `--help` would be
    # ignored and the wrapper would start mutating a managed system instead of
    # explaining itself.
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args(argv)
    return runner._run_from_arguments(["--group", GROUP])


if __name__ == "__main__":
    raise SystemExit(main())
