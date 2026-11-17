# Hosted coverage and worker cost (#2000)

## Hosted prerequisite evidence

[Preflight run 44046232456](https://github.com/randomparity/hmcpctl/actions/runs/44046232456) tested
`983bcc1b062084d64a104f891349593ce735d6a3` on 2026-11-17. No application shard ran in this preflight.
All three jobs passed on ubuntu-24.04/x86_64, kernel 7.0.0-1014-aws, glibc 2.39.

| Python | Branch-coverage probe core | CPUs/affinity | Host available bytes | Probe seconds |
| --- | --- | --- | --- | --- |
| 3.11.18 | CTracer | 4/4 | 15351676928 | 0.214 |
| 3.12.3 | CTracer | 4/4 | 15362879488 | 0.214 |
| 3.13.18 | CTracer | 4/4 | 15337631744 | 0.264 |

Each same-user systemd probe resolved this virtualenv's `hmcpctl`, read fresh cgroup-v2
`memory.peak`, and observed `memory.max=12884901888` (12 GiB). Visible unit and parent
CPU limits were `max 100000`; the parent memory limit was unlimited. Root controller files
were unavailable and are reported as such; affinity and visible limits are the CPU evidence.
The four locked tools were pytest 9.2.0, pytest-cov 7.1.0, pytest-xdist 3.8.0 and coverage 7.15.4.
Lock SHA-256: `af14eb74bab0b24393285a5719bba5832b3a9afb53508c1d8aa9f58dd2e12de7`.
The queue/push-inclusive controller observed completion after 29.691 s;
job runtimes were 21/19/22 s, 62 aggregate seconds (not billed minutes).
This stayed below the separate 10-minute overall/5-minute job/15 runner-minute ceilings.

## Fixed measurement protocol

The [reviewed design](specs/2026-11-17-coverage-worker-cost-design.md) selects the complete
application group. Each Python runs 2 covered, 2 uncovered, 3 covered, 4 covered, then reverse.
Only the diagnostic's coverage switch/worker count changes; production policy remains intact.
Application measurements have not run yet. The observer rejects changed case/outcome hashes
and changed per-file coverage before recording comparisons; a failed sample is retained as
partial evidence without an automatic retry. The overall controller includes queue time.

The approved maximum is 24 samples, 12 minutes/sample, 60 minutes/version job,
180 aggregate runner-minutes and 60 minutes overall. A fresh same-service prerequisite
probe runs before samples. setup-uv cache is disabled; one locked venv and OS caches are reused
within each interpreter job. Two ordered observations per mode do not establish significance.
Covered/uncovered deltas include coverage analysis/export/combine and process overhead,
so they do not isolate tracer CPU cost. cgroup peaks include charged cache and are not RSS.
These results cover one shard; #1998 owns future grouping work. No coverage-floor or cap change.
