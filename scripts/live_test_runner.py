"""Executable entry point for the live HMC integration runner.

Usage:
    uv run --no-sync python scripts/live_test_runner.py [SUBTASK] [options]
"""

from live_test import runner

if __name__ == "__main__":
    raise SystemExit(runner._run_from_arguments())
