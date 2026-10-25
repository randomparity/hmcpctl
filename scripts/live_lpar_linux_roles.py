"""Run opt-in arm 53: one inactive Linux-role fixture and guarded cleanup.

This mutates a managed system. Run live_test_preflight.py --group lpar-linux-roles
first and always run live_test_recovery.py afterwards, even after apparent success.
Classic creation explicitly applies its profile; controller creation never does.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import runner

GROUP = "lpar-linux-roles"


def main(argv: list[str] | None = None) -> int:
    """Parse options before any hardware dispatch."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return runner._run_from_arguments(["--group", GROUP])


if __name__ == "__main__":
    raise SystemExit(main())
