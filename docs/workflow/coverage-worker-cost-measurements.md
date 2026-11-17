# Hosted coverage and worker observations (#2000)

The original three-version run **FAILED**: Python 3.12 sample 8 changed per-file coverage.
Its actual map/hash was lost, and the cause remains unknown under
[#2021](https://github.com/randomparity/hmcpctl/issues/2021).
The later diagnostic did not reproduce it and does not clear that failure.

[Approved scope](https://github.com/randomparity/hmcpctl/issues/2000#issuecomment-6950377766)
admits only the three complete cohorts below, separately. These are descriptive observations
within each version/job, not a successful uniform matrix or pooled benchmark.
Original Python 3.12's seven accepted rows contribute **no comparative estimate**.

## Protocol and shared checks

The pinned application selector was `tests/app`, `tests/unit/test_tool_registry.py` and
`tests/unit/test_tool_schema_templates.py`. Each job used this fixed order:
`2 covered, 2 no-cov, 3 covered, 4 covered, 4 covered, 3 covered, 2 no-cov, 2 covered`.
Each mode therefore has two ordered observations. Tables retain their slot numbers.
Seconds are rounded to three decimals; arithmetic means and ratios use the original JSON values.

Every admitted cohort recorded eight matching case/outcome records: 3,070 cases, 12 skipped.
Their common identity/outcome SHA-256 is
`821ead4c0bc4643a8e2e1f213174e73fc89175063b0b9a9733ab5d85388f88bb`.
All accepted covered records report per-file SHA-256
`880117adf223d30785b088ce1fd3714c1d3e9801fac1a5c30c7d10a150af1b2a`.
Original 3.11/3.13 raw JUnit/maps were not uploaded: their recorded hashes/counts can be checked,
but the underlying case and file maps cannot be independently reconstructed from those artifacts.
The diagnostic retains raw JUnit and six 305-file maps; their hashes were independently recomputed.

All jobs used ubuntu-24.04/amd64, CTracer, pytest 9.2.0, pytest-cov 7.1.0,
pytest-xdist 3.8.0 and coverage 7.15.4. Lock SHA-256:
`af14eb74bab0b24393285a5719bba5832b3a9afb53508c1d8aa9f58dd2e12de7`.
Fresh resource records reported four CPUs/affinity CPUs and at least 12 GiB host available memory.
Each sample had a fresh systemd cgroup with `memory.max=12884901888` (12 GiB).
Visible unit/parent CPU quotas were `max 100000`; parent memory was unlimited.
Mounted-root control files were unavailable, so visibility of every host limit is not established.

Wall time surrounds the service invocation. `memory.peak` is the kernel's charged cgroup peak,
including descendants, launcher and charged file cache; it is **not RSS** or memory per worker.
No shared setup-uv cache was enabled. Each fresh VM reused one locked venv and warming OS caches
across its ordered samples. No HMC/live arm ran.

## Python 3.11 — separate original cohort

Measured commit: `11b84927b713d52206d324c7f679ca1bd60859c3`.
[Source](https://github.com/randomparity/hmcpctl/commit/11b84927b713d52206d324c7f679ca1bd60859c3).
[Measured job](https://github.com/randomparity/hmcpctl/actions/runs/44051027558/job/133928577554).
Interpreter: `3.11.18 (main, Nov 11 2026, 00:16:37) [Clang 23.1.3 ]`.
Host: `Linux-7.0.0-1014-aws-x86_64-with-glibc2.39`.
Preflight host available: 15343587328 bytes.
Artifact 14314639836 ZIP SHA-256:
`160a7897ed239768ab7b0ec0e8a8ee096432dfaf6975e47bc92b24efface46df`.

| Mode | Slots | Raw wall seconds | Two-sample mean seconds | Raw cgroup peak bytes |
| --- | --- | --- | ---: | --- |
| 2 covered | 1, 8 | 321.759; 305.481 | 313.620 | 1819512832; 1735643136 |
| 2 no-cov | 2, 7 | 226.740; 207.382 | 217.061 | 1781264384; 1800986624 |
| 3 covered | 3, 6 | 274.104; 264.784 | 269.444 | 2060742656; 2073501696 |
| 4 covered | 4, 5 | 250.779; 246.985 | 248.882 | 2274893824; 2288304128 |

For this cohort only, covered-two/uncovered-two mean wall ratio was 1.445
(44.5% more observed wall time). Covered-three/two and covered-four/two ratios
were 0.859 and 0.794. These summarize these pairs, not stable performance estimates.

## Python 3.13 — separate original cohort

Measured commit: `11b84927b713d52206d324c7f679ca1bd60859c3`.
[Source](https://github.com/randomparity/hmcpctl/commit/11b84927b713d52206d324c7f679ca1bd60859c3).
[Measured job](https://github.com/randomparity/hmcpctl/actions/runs/44051027558/job/133928577498).
Interpreter: `3.13.18 (main, Nov 11 2026, 00:17:48) [Clang 23.1.3 ]`.
Host: `Linux-7.0.0-1017-azure-x86_64-with-glibc2.39`.
Preflight host available: 15646830592 bytes.
Artifact 14317105012 ZIP SHA-256:
`5d15718dd1f53fa4705808906f5bdb2be4758c873a44748534f421d1ed6b0580`.

| Mode | Slots | Raw wall seconds | Two-sample mean seconds | Raw cgroup peak bytes |
| --- | --- | --- | ---: | --- |
| 2 covered | 1, 8 | 333.814; 319.638 | 326.726 | 1859600384; 1803767808 |
| 2 no-cov | 2, 7 | 246.772; 239.445 | 243.108 | 1747116032; 1750663168 |
| 3 covered | 3, 6 | 281.611; 275.636 | 278.623 | 2077343744; 2061656064 |
| 4 covered | 4, 5 | 265.436; 264.344 | 264.890 | 2250055680; 2261143552 |

For this cohort only, covered-two/uncovered-two mean wall ratio was 1.344
(34.4% more observed wall time). Covered-three/two and covered-four/two ratios
were 0.853 and 0.811. These summarize these pairs, not stable performance estimates.

## Python 3.12 — separate diagnostic cohort

Measured commit: `09123446bca5822c874ca7ff65f1b1ca34952f42`.
[Source](https://github.com/randomparity/hmcpctl/commit/09123446bca5822c874ca7ff65f1b1ca34952f42).
[Measured job](https://github.com/randomparity/hmcpctl/actions/runs/44087569413/job/134053902902).
Interpreter: `3.12.3 (main, Aug 31 2026, 10:18:26) [GCC 13.3.0]`.
Host: `Linux-7.0.0-1014-aws-x86_64-with-glibc2.39`.
Preflight host available: 15339253760 bytes.
Artifact 14333440439 ZIP SHA-256:
`aefba0fb37121d08df68088a4f417da99eb8971ea6c78cee8d42cf9dcaa3d6b2`.

| Mode | Slots | Raw wall seconds | Two-sample mean seconds | Raw cgroup peak bytes |
| --- | --- | --- | ---: | --- |
| 2 covered | 1, 8 | 346.081; 328.272 | 337.176 | 1836105728; 1837244416 |
| 2 no-cov | 2, 7 | 216.498; 216.325 | 216.411 | 1765265408; 1771003904 |
| 3 covered | 3, 6 | 291.738; 290.317 | 291.028 | 2050277376; 2050826240 |
| 4 covered | 4, 5 | 276.890; 279.784 | 278.337 | 2263793664; 2264481792 |

For this cohort only, covered-two/uncovered-two mean wall ratio was 1.558
(55.8% more observed wall time). Covered-three/two and covered-four/two ratios
were 0.863 and 0.825. These summarize these pairs, not stable performance estimates.

## Failed original cohort and retained diagnostic

[Original run 44051027558](https://github.com/randomparity/hmcpctl/actions/runs/44051027558)
remains **FAILED**. Its Python 3.12 job rejected the last two-worker covered repeat after seven
accepted rows. Only its slot-8 resource record survived; actual coverage map/hash and elapsed
were not persisted. No estimate above uses that partial cohort or infers its missing observation.
The original 3.12 ZIP SHA-256 is
`0a39933ed5cacd3f85e711105493777a6a8bf1ab7147559a55f27f02cb6a88fb`.
[#2021](https://github.com/randomparity/hmcpctl/issues/2021) owns the unresolved investigation;
no normalization, guessed fix, or flaky-rerun clearance is claimed.

The later diagnostic retained 51 owned raw files: expected/actual observations, resources,
source/input fingerprints, JUnit, six full coverage maps and bounded logs. It passed all eight
unchanged identity/coverage guards, with no observation errors or omitted sample-log bytes.
Workflow artifacts have seven-day retention; the campaign also preserved exact archives separately.
This report retains the observations and checksums, including the original raw-proof limitations.
Its before/after 962-file workload inventory matches the frozen original source/selector/lock:
`d6f1f026bda8bb7ea3e01b3f55ee59187ad97a172348d67b3afca95ff229db50`.
The earlier jobs pin their checkout and lock but did not upload this full inventory.

The diagnostic records actual distribution `0.2.1.dev162+g09123446b`; original metadata omitted
that field (the original 3.12 setup log records `0.2.1.dev158+g11b84927b`). The diagnostic's named
Python-build/tool/lock/core digest matches the original 3.12 tuple, but excludes this project
version difference. Added fingerprint reads warm input pages, and bounded log draining changes
instrumentation. No before/after instrumentation or full runtime/cache equivalence is asserted.

## Interpretation limits

Each cohort covers one shard and two observations per mode in a fixed reverse-paired order.
Order/cache/runner noise remains; no stable median, population estimate or cross-version causal
claim follows. Coverage/no-cov wall differences include tracing, analysis/export/combine and
process overhead; they do not isolate tracer CPU cost. Worker ratios likewise describe only these
ordered runs. Host providers/kernel builds differ, and cgroup charges are not worker RSS.
No worker-cap or coverage-floor recommendation follows. Their policy owners remain maintainers
via separate ADRs; group membership belongs to #1998. This change alters none of those policies.

## Cost and publication control

| Run | Status | Aggregate job seconds | Push/queue-inclusive controller seconds |
| --- | --- | ---: | ---: |
| 44046232456: prerequisite probes only | passed | 62 | 29.691 |
| 44051027558: original three-version attempt | FAILED | 6671 | 2372.051 |
| 44087569413: one Python 3.12 diagnostic | passed | 2267 | 2311.109 |

Total targeted runner time: 9,000 seconds = 150.00 runner-minutes, without retries.
These are job-timestamp durations, not GitHub billed minutes. The total is below both the
original 180-minute allowance and the approved 172.22 runner-minute cumulative ceiling.
The first probes ran no application shard. Each measured workload had RuntimeMax 720 seconds
plus at most 15 seconds of stop/observation containment, including five-second service stop grace.
The additional diagnostic stayed within its 60-minute job/overall/new runner-minute ceilings;
its overall clock included push/queue. No further experiment or preflight is authorized here.

The measured diagnostic workflow SHA-256 was
`715055a88951fe5a1d606e9fd228d721c9006647e85b95d08fc87202fcb28193`.
The later publication-only control gates the whole job on its existing explicit admission
predicate before provisioning; unmarked report/final pushes skip bootstrap, preflight and samples.
It changes no sample command/resource/hash guard. Its workflow SHA-256 is
`0f3a4b5514930e2ba922d8bd38ba291e859bb5e234e2cf1e4805f4c22f03ffe3`.
The report/control change and any subsequent main integration are **UNMEASURED**; all numbers
above belong only to their immutable measurement commits, never to a later PR head.

[#2019](https://github.com/randomparity/hmcpctl/issues/2019) tracks the plugin deprecations;
locked benchmark/config bytes were preserved here. ADR reservation 0386 remains consumed and unused.
