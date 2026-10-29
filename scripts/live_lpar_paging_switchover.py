"""Run the opt-in paging switchover arm after separately authorized preflight."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner
from live_test.memory_statistics import private_destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-file", required=True)
    args = parser.parse_args(argv)
    try:
        path = private_destination(args.results_file)
    except (ValueError, OSError, RuntimeError):
        parser.error(
            "Use new canonical absolute results in a private mode0700 directory outside repositories"
        )
    old_mask = os.umask(0o077)
    try:
        return runner._run_from_arguments(
            ["--group", "lpar-paging-switchover", "--results-file", str(path)]
        )
    finally:
        os.umask(old_mask)


if __name__ == "__main__":
    raise SystemExit(main())
