"""Run opt-in arm 70: hmc_reconfigure_lpar's persistence across profile activation.

This mutates a managed system: it creates one scratch partition, changes its processors,
memory and name, activates its profile to SMS twice, then deletes it. Run
live_test_preflight.py --group lpar-reconfigure first and live_test_recovery.py
afterwards, including after apparent success. Live DLPAR is not exercised.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import runner

GROUP = "lpar-reconfigure"


def main(argv: list[str] | None = None) -> int:
    """Parse options before dispatch so help can never start a hardware run."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return runner._run_from_arguments(["--group", GROUP])


if __name__ == "__main__":
    raise SystemExit(main())
