"""Run opt-in arm 55: inactive hardware memory access creation and cleanup.

This mutates a managed system. Run live_test_preflight.py --group lpar-hardware-memory
first and live_test_recovery.py afterwards, including after apparent success.
No activation or profile apply is performed; access availability is not runtime proof.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import runner

GROUP = "lpar-hardware-memory"


def main(argv: list[str] | None = None) -> int:
    """Parse options before dispatch so help can never start a hardware run."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return runner._run_from_arguments(["--group", GROUP])


if __name__ == "__main__":
    raise SystemExit(main())
