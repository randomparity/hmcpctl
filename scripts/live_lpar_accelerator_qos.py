"""Run the finite opt-in ST64 accelerator QoS procedure in its authorized window."""

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-file", required=True)
    args = parser.parse_args(argv)
    supplied = Path(args.results_file).absolute()
    path = supplied.resolve()
    parent = path.parent
    if (
        supplied.exists()
        or supplied.is_symlink()
        or runner.evidence.observations_path(str(path)).exists()
        or runner.evidence.observations_path(str(path)).is_symlink()
        or not parent.is_dir()
        or stat.S_IMODE(parent.stat().st_mode) != 0o700
        or any((p / ".git").exists() for p in (parent, *parent.parents))
    ):
        parser.error(
            "Use a new results file in a private mode0700 directory outside repositories"
        )
    previous = os.umask(0o077)
    try:
        return runner._run_from_arguments(
            ["--group", "lpar-accelerator-qos", "--results-file", str(path)]
        )
    finally:
        os.umask(previous)


if __name__ == "__main__":
    raise SystemExit(main())
