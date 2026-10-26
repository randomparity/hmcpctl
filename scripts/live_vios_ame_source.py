"""Run the separately registered, source-only VIOS AME capture."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import vios_ame_source


def main(argv: list[str] | None = None) -> int:
    return vios_ame_source.main(["capture", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
