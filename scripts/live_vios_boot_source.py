"""Run the separately registered, source-only VIOS boot capture."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import vios_boot_source


def main(argv: list[str] | None = None) -> int:
    return vios_boot_source.main(["capture", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
