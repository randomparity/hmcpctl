"""Run only the product system-profiles arm with a private recovery destination."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-file", required=True, type=Path)
    args = parser.parse_args(argv)
    path = args.results_file.resolve()
    root = Path(__file__).resolve().parent.parent
    if not path.parent.is_dir():
        parser.error("--results-file parent must be an existing private directory")
    parent = path.parent.stat()
    if (
        path == root
        or root in path.parents
        or path.exists()
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o077
    ):
        parser.error(
            "--results-file must be a fresh private path outside this checkout"
        )
    return runner._run_from_arguments(
        ["--group", "system-profiles", "--results-file", str(path)]
    )


if __name__ == "__main__":
    raise SystemExit(main())
