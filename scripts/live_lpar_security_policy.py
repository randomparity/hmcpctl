"""Run opt-in arm 52: inactive secure-boot create, log-policy update and cleanup.

This mutates a managed system. Run live_test_preflight.py --group lpar-security-policy
first and live_test_recovery.py afterwards, including after apparent success.
No activation, profile apply or key-management operations are performed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import runner

GROUP = "lpar-security-policy"


def main(argv: list[str] | None = None) -> int:
    """Parse options before dispatch so help can never start a hardware run."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return runner._run_from_arguments(["--group", GROUP])


if __name__ == "__main__":
    raise SystemExit(main())
