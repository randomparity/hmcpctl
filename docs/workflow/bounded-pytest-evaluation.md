# Bounded pytest evaluation (#1434)

Decision: adopt bounded execution with the reviewed resource and lifecycle policy,
explicitly approved by the operator on 2026-10-09.

## Provenance and candidate

The operator invoked the #1430–#1435 campaign and approved this evaluation's
scope. [Issue #1434](https://github.com/randomparity/hmc-mcp/issues/1434) permits
adoption or a measured NO-GO; [the frozen charter](https://github.com/randomparity/hmc-mcp/issues/1434#issuecomment-6081889875)
retains its exclusions. The integrated base is
`64a2619c45801efd98b6c9618207a7652ae614c8`; the admitted timing/RSS comparisons below use
`7f0dfe0dcad9df1b11f06a2d42c82bd1547a0363`.

The candidate is pytest-xdist 3.8.0, exactly two workers,
`--dist=loadfile --max-worker-restart=0`. Both modes use the same locked app/dev
environment, including the xdist plugin. Pytest is 9.1.1, pytest-cov 7.1.0 and
coverage 7.15.4. The dependency was checked against its primary release source
before installation. Serial execution remains the comparison reference.

Every admitted timing/RSS comparison reports the same 8,708 passed test identities
and identical per-file coverage summaries: 17,079 statements, 4,540 branches and 4,152 covered
branches (91.45374449%). Combined statement-plus-branch coverage is 95.30968130%,
above the unchanged configured 90.5% combined floor. Those 8,708
include eight temporary observer tests. The six sharing-mode serialization cases
were preserved and sorted to make worker collection deterministic; no case was
removed. Independent complete collection under hash seeds 1 and 2 retained the
same identities before/after that correction and became identically ordered.

## Method

The temporary harness at the measured commit invokes the configured full pytest
suite through the installed interpreter. It records JUnit and coverage JSON,
monotonic command-through-cleanup wall time, and samples the sum of descendant
RSS every 100 ms. This sum includes shared pages once per process and can miss
short peaks; it is not unique physical memory or maximum single-process RSS.
The observer's own RSS is excluded. Cgroup accounting, where recorded, includes
all charged processes, tmpfs and kernel memory, so it answers a different question.

The harness runs through `uv run --no-sync`, preserving installed console-script
PATH and the locked app extras. It strips ambient pytest/coverage overrides.
The Linux observer adopts/reaps orphan descendants, and contains failures after
recording them. Its cleanup is not evidence that the production runner cleans
workers. The separate lifecycle probe observes survivors before containment.

Each sample runs `python -m pytest -q` with JUnit and coverage JSON outputs; the
parallel command adds exactly the three options above. The configured coverage
gate stays enabled. Wall includes observer teardown. All package/setup caches
are retained; these are warm development comparisons. Hosted jobs use their
normal setup/cache path. Native jobs run serial, parallel, parallel, serial on
one runner per architecture; no queue delay is included in sample wall time.

Local: x86_64 Fedora 44, Linux 7.2.7, Python 3.11.15, 48 affinity CPUs and
approximately 250.92 GiB physical RAM. Normal local cgroup limits are unlimited.
The constrained scope enforces one CPU quota, 2 GiB memory and zero swap for the
whole driver/observer/test process tree; affinity remains 48 and is not a CPU
quota. Local `/tmp` is tmpfs. Native: Ubuntu 24.04 on amd64 and arm64,
Python 3.11.16 and four affinity CPUs each. Their recorded immediate cgroup
limits are unlimited; physical RAM and ancestor limits were not captured.

## Eligible samples

RSS values are decimal MB (1 MB = 1,000,000 bytes).

| Profile / pair | Serial wall (s) | Two-worker wall (s) | Serial RSS (MB) | Two-worker RSS (MB) |
|---|---:|---:|---:|---:|
| Normal local, first eligible | 472.164 | 246.312 | 790.8 | 1,362.1 |
| Normal local, reversed confirmation | 469.769 | 243.986 | 793.2 | 1,339.9 |
| Native amd64, first | 409.411 | 215.557 | 810.1 | 1,378.4 |
| Native amd64, reversed | 388.810 | 214.137 | 806.1 | 1,381.9 |
| Native arm64, first | 558.248 | 282.042 | 810.7 | 1,346.6 |
| Native arm64, reversed | 533.661 | 278.412 | 802.7 | 1,344.6 |

The native pairs reduce wall time by 44.93–49.48% and increase sampled summed
RSS by 66.11–71.42%. The first eligible local pair reduces wall by 47.83% and
increases sampled summed RSS by 72.24%. The explicitly authorized final reversed
normal pair reduces wall by 48.06% and increases RSS by 68.91%. Serial walls differ
by 0.51% and parallel walls by 0.95% (ranges relative to their two-run means).
The initial invalid pair remains counted: three normal attempts produced two admitted pairs, with no
further normal repetition authorized. These are measured comparisons, not
promised multipliers or sums of earlier component savings.

[The native evaluation run](https://github.com/randomparity/hmc-mcp/actions/runs/37871628038)
completed all eight samples on the exact PR head. Its normalized artifacts hash
test/file identifiers and retain statuses/coverage without publishing raw logs.
[Ordinary CI at that head](https://github.com/randomparity/hmc-mcp/actions/runs/37871627994)
also passed eight native verification, eight wheel and two library legs plus the
report; one conditional support-drift job was skipped. This is experiment-head
verification, not final-branch proof.

## Failures and limits

The original normal attempt is rejected: serial 459.448 s / 793.7 MB returned
nonzero because a direct-interpreter driver omitted the console-script PATH and
the observer deferred adopted-zombie reaping. The original parallel command
returned a six-case collection-order mismatch after 14.987 s. Neither is admitted
as a successful performance result. Corrected invocation, active adopted-child
reaping and sorted parameter order were verified before the admitted samples.

The first native attempt was cancelled on both architectures. Its retained arm64
serial artifact (585.901 s / 837.4 MB, nonzero) also used a merge-preview tree
rather than the local candidate and had 8,811 identities. It is rejected; no
cross-tree comparison is made. The corrected temporary workflow explicitly
checks out the same immutable PR-head SHA for both native architectures.

The first constrained serial run passed in 530.629 s / 791.8 MB with the same
identities and coverage. The following parallel command was terminated by the
kernel memory controller around 45% progress. The scope returned 143 with
`Result=oom-kill`, exact kernel memory peak 2 GiB and zero swap. No final candidate
JUnit, coverage, elapsed-time or RSS report exists; none is invented.
At that event, kernel accounting showed about 809.6 MB anonymous memory,
1,252.9 MB tmpfs/shared-memory charge and 84.8 MB kernel memory. The preceding
serial fixture directory retained about 649 MB, and the failed parallel one
about 512 MB. Both commands shared one scope, so retained temporary files make
this an order-dependent resource result, not proof that parallel process RSS
alone exceeded 2 GiB. The authorized reversed confirmation used a fresh scope:
parallel passed in 504.502 s / 1,321.2 MB sampled RSS, with exact identities and
coverage. Its actual cgroup peak was 1,995,755,520 bytes with zero max/OOM events.
After pytest exited, 787,968,000 bytes remained charged, including 720,486,400
bytes shmem. The subsequent serial command then also OOM-stopped (143, no final
sample), with about 626.1 MB anonymous, 1,422.6 MB shmem and 98.5 MB kernel
memory at the event. The scope again peaked at exactly 2 GiB. This reversal
confirms that retained tmpfs can kill the second mode in either order. It does
not identify an intrinsic fresh parallel-only memory failure. No third constrained
pair ran. Successful fresh serial/parallel observations come from different
attempts; they are not presented as a complete successful paired repetition.

Separate baseline-runner timeout probes reproduced two surviving xdist workers
three seconds after runner exit, twice; the serial controls had none. That baseline runner
only terminates its direct child. Fixture containment subsequently removes the
survivors and receives no credit for production cleanup. Owned sessions can
address worker signalling, but nested-session descendants additionally require
the adopted ownership/reaping design.

## Supplemental memory accounting

Three memory-only observations establish actual kernel charged peaks for the
production runner. They do not enter the earlier timing/RSS comparison dataset.
The fresh constrained kernel peak above remains evidence for that distinct profile.

| Profile | Immutable source | Kernel memory.peak (bytes) | Execution cost (s) |
|---|---|---:|---:|
| Normal local | `ffc1f7740a9f4a243465fa9271e0546c2f275f9b` | 1,998,241,792 | 236.641 |
| Native amd64 | `12ce31955b1ef0a2ca470ae5ca34fac4c0ed5568` | 2,104,983,552 | 321.255 |
| Native arm64 | `12ce31955b1ef0a2ca470ae5ca34fac4c0ed5568` | 2,045,255,680 | 265.671 |

All three selected two workers through the production defaults and passed all
8,762 cases. Complete hashed collection identities, all 207 per-file coverage
summaries and every coverage total equal the production reference exactly.
The count is 8,708 minus eight removed observer tests plus 62 new runner tests;
all original cases remain. The coverage denominator and configured floor are unchanged.
The local source tree is `0be47f8b860831c0a9f5465d1218f5d52b0ebed4`; the native tree
is `f148c6dc43934d382b5c0df521e125709ffa92b2`. Runner and runner-test sources are
identical between those commits; the native tree includes the isolated capability
console fixture and temporary observer updates. These are separate source cohorts.

The observer reads memory.peak in a fresh owned cgroup after runner exit and an
empty descendant inventory, before removing the unit. This is charged cgroup
memory, including tmpfs, kernel charges and the small in-scope Python observer;
it is neither sampled RSS nor unique physical memory. Collection occurs outside
before the scope and coverage export outside afterwards. Existing warm-cache pages
charged elsewhere are not recharged merely because a test reads them. All memory
events were zero, no descendants remained and every owned unit was stopped/reset
and absent after cleanup. Cheap controls first proved counter growth, exit 0/7
propagation, timeout survivor removal, exact membership and state restoration.

The local profile retains the Fedora/Python/48-CPU facts above and unlimited
visible ancestor CPU/memory/swap limits. Both native observations use Ubuntu
24.04.5, Linux 6.17.0-1022-azure, Python 3.11.16 and four affinity CPUs. Physical
RAM is about 15.6 GiB; original MemAvailable was about 14.1/14.2 GiB respectively.
Every visible non-root original and observed ancestor has unlimited CPU, memory
and swap limits; root controls are legitimately absent. The system-manager scope
preserves affinity, original credentials and the runner-sanitized environment.
No controller, threshold or worker-selection override is used. This is a startup
admission test, not a memory reservation; concurrent outside allocations can
invalidate its headroom. The largest observed peak is about 1.96 GiB, below the
3 GiB acceleration threshold, without establishing a universal bound.

[The native memory run](https://github.com/randomparity/hmc-mcp/actions/runs/37938882776)
completed both allocated observations. The initial native attempts at ffc1f774
returned runner exit 0 but selected serial because fresh user scopes lacked
cpu.max. Their rejected serial peaks were 1,650,221,056/1,610,764,288 bytes and
costs 511.662/529.780 s. Neither supplies parallel evidence or admitted complete
identity/coverage proof. Both units were removed without descendants or memory
events. Both native system-manager preflights then passed at source 050f4107,
followed by exactly one additional full candidate per architecture. Thus three
admitted observations and two rejected native attempts remain recorded; no local
rerun, serial comparison, new pair, pooled speed result or further attempt occurred.

## Production verification

The production runner's real subprocess tests cover two-worker execution,
combined coverage, concurrent ports/files/environment, nonzero and signal status,
timeout, PID/group/repeated interrupts, nested sessions, launch/inventory failures
and subreaper restoration. Survivor assertions precede fixture containment.
Controlled faults made resource, coverage, lifecycle and portability tests fail.
The capability table test now uses an isolated fixed-width console: an import-time
80-column environment reproduced the native Python 3.12 failure, and the fixture
preserves every existing assertion and restores the original shared console.
Final delivery requires the unchanged eight native verification, eight wheel and
two library legs after removal of the temporary workflow.


## Current test and CI allowances (#1775)

The operator separately approved a 25-minute test-runner deadline and a 30-minute
budget for the named `ci` job. Their five-minute difference is allowance for the
surrounding job lifecycle. Other job budgets, the four-version matrix, worker
selection, coverage requirements and interrupt/termination graces are unchanged.
ADR 0129's readiness and interrupt-collection decisions, as subsequently qualified
by ADR 0130, remain separate from these overall test and job allowances.

This policy change does not establish the cause of the earlier 1,020-second
Python 3.12 timeout. A bounded observed run completed, but lacked a corresponding
per-node trace from the failed run; no performance fix is claimed. The historical
evaluation and measurements above retain their original scope and provenance.
