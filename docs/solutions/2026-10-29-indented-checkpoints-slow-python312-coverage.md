---
title: Indented checkpoint JSON makes Python 3.12 source-arm tests slow under coverage
date: 2026-10-29
tags: [performance, checkpoint, json, python, python312, pytest, coverage, cprofile]
components: [scripts/live_test/system_profile_source.py, scripts/live_test/system_profile_activation_source.py, tests/scripts/test_live_system_profile_source.py]
---

## Problem

Python 3.12 CI repeatedly ran slowly, and a full job exceeded its deadline.
Increasing a timeout did not establish a cause. Local per-test timing records
identified 167 tests in `test_live_system_profile_activation_source.py` as a
CPU hotspot. The same file was slow in focused hosted tests on both system
Python 3.12.3 and managed Python 3.12.14.

The [focused hosted observation](https://github.com/randomparity/hmcpctl/actions/runs/40923468965)
retained complete per-case phase/CPU records and profiles. Summed serial test-phase
times with coverage were 52.1 seconds on managed 3.11.16, 143.9 on system 3.12.3,
111.4 on managed 3.12.14 and 14.3 on managed 3.13.15. These were separate hosted
runners, not a pure interpreter-speed comparison. Updating 3.12 alone left the
hotspot present.

## Root cause

`system_profile_source.save` used `json.dumps(document, indent=2)` for every
atomic checkpoint. A profiled activation-source case saved 81 checkpoints;
checkpoint saves consumed about 95% of its covered test-call time, mostly in
Python JSON encoder traversal.

In the inspected CPython 3.12.13 `JSONEncoder.iterencode`, the C encoder is selected
only when `_one_shot` is true, `c_make_encoder` exists and `self.indent is None`.
Indentation selects recursive Python encoding. Coverage magnifies that work.
The journal consumers parse JSON; their contract does not require indentation.

This establishes a checkpoint-serialization hotspot. It does not establish that
this file alone caused the earlier whole-suite timeout or job cancellation.

## Solution

Remove `indent=2` in `scripts/live_test/system_profile_source.py:210`.
Keep the ordinary JSON defaults, encoded five-MiB ceiling, private temporary
file, flush/fsync, atomic replacement and failed-write cleanup unchanged.
Canonical digests still encode parsed values separately; old pretty journals
remain readable. No journal fields or recovery authority change.

On the same local macOS/arm64 host, Python 3.12.13 and locked environment,
the same 167 case identities took 84.036 seconds before and 4.604 after under
coverage, an 18.3-fold reduction in summed test-phase time. Process CPU over
those cases fell from 83.881 to 4.531 seconds. This was one ordered sample per
variant, not hosted confirmation of the fix or a full-suite qualification.

## Prevention

The source-arm tests require single-line JSON while preserving values, Unicode
escaping, key order and canonical digests. They cover old-journal recovery,
the exact encoded-size boundary, and retention of prior private evidence after
serialization, fsync or replacement failure. A controlled one-byte limit
relaxation made the size-boundary regression fail before restoration.

Run the two affected modules with `--no-cov` for functional checks. To inspect
the coverage-sensitive hotspot, run:

```sh
uv run --no-sync pytest tests/scripts/test_live_system_profile_activation_source.py -q --durations=30
```

That focused covered run normally fails the unchanged package-wide coverage
floor even when its tests pass. Preserve that nonzero status and report it
separately; only the normal full guardrail run qualifies the coverage gate.
Compare matching cases at the same source, lock, interpreter, execution mode
and resource context. Capture CPU and individual phases before increasing
timeouts or blaming a single old interpreter build.
