"""Run the finite opt-in ST64 accelerator QoS procedure in its authorized window."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner


def main(argv: list[str] | None = None) -> int:
    return runner._run_private_group("lpar-accelerator-qos", __doc__, argv)


if __name__ == "__main__":
    raise SystemExit(main())
