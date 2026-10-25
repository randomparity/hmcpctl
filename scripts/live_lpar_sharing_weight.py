"""Run opt-in ST59 sharing/weight on two sequential owned inactive fixtures.

Run live_test_preflight.py --group processor-sharing-weight first and
live_test_recovery.py afterwards, including after apparent success or failure.
Use --results-file for an absolute path in a private external directory.
No profile apply or activation is performed. Separate operator authority is required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner
from live_test.processor_sharing_weight import GROUP


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
