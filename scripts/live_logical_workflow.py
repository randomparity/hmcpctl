"""Run opt-in arm 73: the logical plan, create, reconfigure and destroy workflow (#1854).

This mutates a managed system: it plans and provisions one scratch partition on a new
1 GiB volume in the configured volume group, lists it in the logical inventory,
reconfigures its memory and processors, inspects it, holds it for a hand-off and
releases it, starts it once and stops it immediately, then decommissions it and the
volume, all through the primary catalog's workflow tools. Run
live_test_preflight.py --group logical-workflow first and live_test_recovery.py
afterwards, including after apparent success.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import runner

GROUP = "logical-workflow"


def main(argv: list[str] | None = None) -> int:
    """Parse options before dispatch so help can never start a hardware run."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return runner._run_from_arguments(["--group", GROUP])


if __name__ == "__main__":
    raise SystemExit(main())
