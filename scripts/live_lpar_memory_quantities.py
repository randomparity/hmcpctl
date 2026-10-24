"""Run opt-in arm 50: owned inactive memory quantities and mandatory cleanup.

This mutates a managed system. Run live_test_preflight.py with the
lpar-memory-quantities group first and live_test_recovery.py afterwards.
Run from the repository and use --results-file for a private external path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import runner
from live_test.memory_quantities import GROUP


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-file")
    args = parser.parse_args(argv)
    forwarded = ["--group", GROUP]
    if args.results_file is not None:
        forwarded.extend(["--results-file", args.results_file])
    return runner._run_from_arguments(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
