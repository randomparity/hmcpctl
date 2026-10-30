"""Run standalone ST69; one explicitly retained client Ethernet change."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import ethernet_update


def main(argv: list[str] | None = None) -> int:
    return ethernet_update.main(["run", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
