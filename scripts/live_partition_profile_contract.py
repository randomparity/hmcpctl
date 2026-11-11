"""Run only the bounded disposable-profile contract experiment; never activates a partition."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner


def main(argv: list[str] | None = None) -> int:
    return runner._run_private_group("partition-profile-contract", __doc__, argv)


if __name__ == "__main__":
    raise SystemExit(main())
