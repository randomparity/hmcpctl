"""Run the focused profile-artifacts arm (creates one scratch backup, then deletes it).

Run live_test_preflight.py --group profile-artifacts first, then independent
live_test_recovery.py --results test-results-profile-artifacts.json afterwards.
No profile restore, initialization, partition mutation or existing-file overwrite.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test import runner

GROUP = "profile-artifacts"


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return runner._run_from_arguments(["--group", GROUP])


if __name__ == "__main__":
    raise SystemExit(main())
