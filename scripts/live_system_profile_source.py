"""Run the exact-packet system-profile metadata-only source arm.

First run live_test_preflight.py --group system-profile-source with --packet and
--packet-sha256. Afterwards run live_test_recovery.py with --source-packet,
--packet-sha256 and --results. Capture requires a separate exact native grant.
"""

from live_test.system_profile_source import main

if __name__ == "__main__":
    raise SystemExit(main())
