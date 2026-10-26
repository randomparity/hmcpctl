"""Run standalone ST63 only; no default runner or ST100 dispatch."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import lpar_serial_adapter


def main(argv: list[str] | None = None) -> int:
    return lpar_serial_adapter.main(["run", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
