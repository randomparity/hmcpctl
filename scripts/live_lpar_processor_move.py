"""Run opt-in processor-move arm61 after separately authorized preflight."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner


def main(argv=None):
    return runner._run_private_group("lpar-processor-move", __doc__, argv)


if __name__ == "__main__":
    raise SystemExit(main())
