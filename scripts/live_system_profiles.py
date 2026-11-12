"""Run only the product system-profiles arm with a private recovery destination."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner


def main(argv: list[str] | None = None) -> int:
    return runner._run_private_group("system-profiles", __doc__, argv)


if __name__ == "__main__":
    raise SystemExit(main())
