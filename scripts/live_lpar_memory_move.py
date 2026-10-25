"""Run opt-in memory-move arm54 after separately authorized preflight."""

import argparse
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-file", required=True)
    args = parser.parse_args(argv)
    path = Path(args.results_file).resolve()
    parent = path.parent
    if (
        path.exists()
        or runner.evidence.observations_path(str(path)).exists()
        or not parent.is_dir()
        or stat.S_IMODE(parent.stat().st_mode) != 0o700
        or any((p / ".git").exists() for p in (parent, *parent.parents))
    ):
        parser.error(
            "Use a new results file in a private mode0700 directory outside repositories"
        )
    import os

    old_mask = os.umask(0o077)
    try:
        return runner._run_from_arguments(
            ["--group", "lpar-memory-move", "--results-file", str(path)]
        )
    finally:
        os.umask(old_mask)


if __name__ == "__main__":
    raise SystemExit(main())
