"""Run the separately registered, bounded vNIC creation policy procedure."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import vnic_creation_policy


def main(argv: list[str] | None = None) -> int:
    return vnic_creation_policy.main(
        ["capture", *(sys.argv[1:] if argv is None else argv)]
    )


if __name__ == "__main__":
    raise SystemExit(main())
