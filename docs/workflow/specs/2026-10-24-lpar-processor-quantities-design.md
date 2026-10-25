# D05: Partition processor quantities

## Authority and source contract

Proposed design for #1778, native child of #638, frozen scope q1778-1db8f45e
in issue comment 6395185550. All seven Expected criteria remain binding; the
operator-approved exclusion/owner set is empty. [ADR 0296](../../adr/0296-lpar-processor-delta-operations.md)
records the proposed boundary. The operator approved the contract-plan format on 2026-10-25;
design approval and implementation remain separate gates. No live access is authorized here.

Source inspection at `40f9f85672dee57c6dcc03d6446534efab93e43e` established:

| Contract | Evidence |
| --- | --- |
| Native non-pool add/remove/set grammar | `docs/refs/hmc-commands-p10/commands/chhwres.md:142`; P11 equivalent `:133` |
| Whole dedicated CPUs or shared virtual processors; current-based arithmetic | P10 `chhwres.md:226`; P11 `:230` |
| Shared entitlement, at most two decimal places | P10 `chhwres.md:227`; P11 `:231` |
| Shutdown below-minimum removal couples CPU and memory reset | P10 `chhwres.md:206`; P11 `:208` |
| Explicit no-RMC force and deferred operating-system effect | P10 `chhwres.md:236`; P11 `:239` |
| Processor read, level, filter and field selection | P10 `lshwres.md:62,97,102,103`; P11 corresponding processor synopsis/options |
| Current/configured dedicated CPU and shared entitlement/vCPU REST paths | `tests/fixtures/partition_profile_contract/partition-resource.json:216`; `tests/fixtures/live/rest-lpar-entry.json`; `rest-vios-entry.json` |
| Native current/pending/runtime CPU fields | `tests/fixtures/live/vocabulary/v10r3-p9.json:4413`; P11 vocabulary `v11r2-p11-9242-21b.json:3990` |
| System capacity, per-partition limits, minimum units per vCPU | `tests/fixtures/live/rest-ms-entry-mtms-pair.json`; P9 vocabulary |

The local reference search found separate current, pending and runtime semantics,
not an alias for the existing REST Desired-field setter. References are cited;
licensed prose is not reproduced. Captures establish field shape and observed release
vocabulary, not successful mutation on an untested hardware/release combination.
No unretained SystemProcessorConfiguration schema or schema import is claimed as evidence.

## Boundary and exact public interfaces

Keep `set_lpar_processors(hmc, lpar_name_or_uuid, resources, *,
system_name_or_uuid=None, ownership_override=False)` unchanged, including its complete
partition If-Match update, optional system selector, `LparResources` units, capping and
sharing behavior. `resources.desired_procs` means entitlement when shared, whereas
native `--procs` means virtual processors. Native set targets CURRENT quantities;
REST writes Desired/Minimum/Maximum values. Thus the old resources mode reuses the
setter, while native add/remove/set cannot safely be reduced to it.

Add frozen, extra-forbidden `LparProcessorChange` in `documents/lpar_processors.py`:

- `operation: Literal["add", "remove", "set"]` is required.
- `processors: StrictInt | None = None`, nonnegative, is whole dedicated CPUs or
  shared virtual processors according to the observed CURRENT mode.
- `processing_units: Decimal | None = None`, nonnegative, is shared entitlement.
  Accept Decimal, decimal string, JSON number or integer, but reject booleans,
  nonfinite values and values not exactly representable in hundredths. Bound numeric
  representation before exact conversion/quantization; never round. Trailing zeros are harmless.
- `force: StrictBool = False`; at least one quantity must be supplied.

Keep Decimal internally and serialize entitlement evidence as canonical fixed-point
strings with two fractional digits, so no binary-float arithmetic reaches comparisons.
Add `async change_lpar_processors(hmc: HMCClient, lpar_name_or_uuid: str,
change: LparProcessorChange, *, system_name_or_uuid: str,
ownership_override: bool = False) -> dict[str, Any]` in `operations/lpar/processors.py`.

Append `change: LparProcessorChange | None = None` to `hmc_dlpar_proc`; preserve
its operation ID `lpar.dlpar_proc`, mutate classification, lpar target, previous positional
arguments/defaults and connection-profile behavior. Reject `change` plus nonempty resources
or absent/blank system before `with_client`. No new facade export or arbitrary command surface.
Register `lpars processors NAME --system SYSTEM --operation add|remove|set
[--procs INTEGER] [--procunits DECIMAL] [--force] [--yes] [--ownership-override]`.
CLI validates before connecting/confirming; confirmation names the CPU/memory shutdown
reset and force/restart consequences. Incomplete results exit 1; usage failures exit 2.

Reuse landed `memory._contained_partition` for LPAR/VIOS containment and
`memory._snapshot` for identity, memory envelope, current CPU vector and profile-sync
observation; leave those helpers and their existing callers unchanged. Their memory-specific
error messages remain accurate for that memory evidence. Processor admission, native CPU
snapshot and postconditions belong in processors.py. No new generic workflow framework,
no dependency on the unlanded memory-move implementation, and no ownership migration.

## Read shape and admission

Resolve selected system UUID, contained partition kind/UUID and unique CLI system name;
use the kind-specific REST reader. Confirm partition UUID/name/positive numeric ID,
then call existing `authorize_lpar_mutation(..., max_output_bytes=65536)` before processor
reads or mutations. Its required ownership-description SSH read uses the same aggregate cap;
overflow prevents an authorization decision and all processor commands. The audited override
keeps its existing no-read behavior; no caller-supplied description substitutes for the guard.
The full REST read supplies state, partition type, RMC, operation-in-progress, profile
sync, memory envelope, current mode and configured processor bounds. Require explicit
operation-in-progress and mode evidence rather than the memory helper's absent-field fallback.

Read native quantities with one fixed, quoted `lshwres -r proc -m SYSTEM --level lpar
--filter lpar_ids=ID -F FIELDS` command. FIELDS is the constant ordered vector:
`lpar_id,lpar_name,curr_proc_mode,curr_min_procs,curr_procs,curr_max_procs,
pend_proc_mode,pend_min_procs,pend_procs,pend_max_procs,run_procs,
curr_min_proc_units,curr_proc_units,curr_max_proc_units,
pend_min_proc_units,pend_proc_units,pend_max_proc_units,run_proc_units`.
Parse strict CSV with stdlib csv, exactly one record and exactly eighteen fields;
reject missing/extra/duplicate records and mismatched ID/name. Both new fixed processor
reads and mutations pass a 65536-byte combined stdout/stderr limit to the existing SSH
transport as specified below. Do not return raw SSH output; retained errors are at most
2048 characters. Projection bounds requested shape, not received bytes by itself.

Validate native mode (`ded`/`shared`) against REST CurrentHasDedicatedProcessors;
require pending/configured mode to agree before this finite operation. Integers must be
nonnegative and integral; shared unit fields must be finite nonnegative hundredths.
Dedicated rows may omit unit fields, which are inapplicable; shared rows require them.
Current and pending min/assigned/max envelopes are validated independently; pending
values may differ from current. Native current values must equal the REST mode-specific
current CPU vector; desired fields must equal pending values. A mismatch is stale or
unsupported evidence and refuses before mutation. Runtime values are separate observations.

Bound numeric input tokens and normalized JSON numeric values to B=65536//62=1057 bytes:
30 values per maximum shared snapshot, twice, plus two expected quantities. Include unit-string
quotes; whole decimal digits are at most B (or Python's smaller active integer serialization
limit), unit integer digits at most B-5. This covers requests, runtime/capacity and inherited
identity/memory readers before conversion/arithmetic/rendering. Finite/sign/zero and exponent
checks precede bounded exact quantization; local arithmetic precision 2*B+2 preserves hundredths.
Invalid preeffect evidence refuses; invalid post-read is unverified. This is a numeric-value
budget, not a hardware limit or a total JSON/RSS bound; source limits and failure model remain.

Admit only stable `running` and `not activated` states and supported AIX/Linux, VIOS or
OS400 partition types. Running AIX/Linux/VIOS requires active RMC and reported
DynamicLogicalPartitionProcessorCapable; explicit force admits its documented no-RMC
case with deferred effect. IBM i requires reported dynamic CPU capability when running;
force is invalid for IBM i. Inactive operations require no RMC and never activate/apply
a profile. Native firmware may reject inactive growth/set: submission remains a hypothesis,
and a refusal is represented honestly rather than counted as supported live behavior.
Unknown/missing applicable capability evidence refuses. P10/P11 command source plus V10R3
and V11R2 captures define the reference envelope; observed fields/capabilities gate admission,
not an invented minimum release number. Native unsupported-operation errors remain rejection.

Compute each supplied target from CURRENT, not pending or runtime. Omitted quantities
remain current; shared count and entitlement can be supplied together. Dedicated units
input refuses even if zero. Negative requests/subtraction beyond current refuse.
Zero add/remove components are unchanged; an all-zero delta becomes an authorized,
rechecked no-op without a mutation. Set equal to current is also an observed no-op
unless it requests the explicit shutdown zero/reset transition. Set zero is never silently
folded into a delta no-op.

Use current bounds when initialized; an inactive all-zero current envelope may use the
separately valid configured envelope. Positive growth must reach minimum and stay below
maximum. Running targets cannot fall below either applicable minimum. For a shutdown
reduction below an applicable minimum, or explicit set zero of a processor quantity,
expect the entire mode-specific current CPU minimum/assigned/maximum vector AND current
memory minimum/assigned/maximum to reset to zero. The reset takes precedence over normal
positive entitlement/count consistency; reject a simultaneous positive-growth component
combined with reset, because its requested end state contradicts complete deallocation.

For ordinary shared targets require `minimum_units_per_vcpu * target_vcpus <= target_units
<= target_vcpus`; take minimum_units_per_vcpu from the selected system's captured
MinimumProcessorUnitsPerVirtualProcessor, never a hard-coded 0.05/0.1. Check both the
current and configured envelopes for sensible ordering and positive nonzero targets.
System AssociatedSystemProcessorConfiguration supplies configurable/available units,
CurrentMaximumAllowedProcessorsPerPartition, MaximumAllowedVirtualProcessorsPerPartition
and the applicable current dedicated/shared maximum for AIX/Linux, IBM i or VIOS.
Require finite ordered capacity, valid positive applicable limits and system State operating.
Dedicated target/growth consumes whole units; shared entitlement target/growth consumes
decimal units, while vCPU growth is bounded by its count limit, not system free whole CPUs.
Reject known target/capacity/limit violations before effects. HMC retains final admission
for capacity races, processor licensing and shared-pool entitlement availability; no pool
lifecycle, membership or policy is mutated or inferred from system free capacity.

Immediately before submission, independently reread the contained identity, REST snapshot,
native CPU row and system capacity/limits. Require unchanged decision-relevant evidence,
then submit once with only `chhwres -r proc -m SYSTEM -o a|r|s --id ID`, requested
`--procs`, `--procunits`, and admitted `--force`. Quantities and IDs are typed; system
names are shell quoted. There is no `-a`, pool selector, target partition, automatic retry,
profile update, mode switch, guest activation or compensating mutation. Native deltas are
performed by HMC; a preflight read is not a cross-transport transaction or ETag guarantee.
Native CPU command budget is two pre-write reads, zero or one mutation, one post-write read;
add one ownership-description read unless the explicit audited override skips it.
REST calls are bounded by that single-pass sequence, without polling or discovery retries.

## Bounded SSH option

#638's output/transfer criterion requires streaming bounds; post-buffer validation is
insufficient. Extend only existing `ssh.transport.run_hmc_command(config, cmd, *,
include_stderr=False, secrets=(), max_output_bytes: int | None = None) -> str`.
None preserves its existing connection.run path; a positive exact integer selects bounded
output. Reject zero, negative, bool and noninteger limits before credentials/connect/exec.
Thread the optional keyword through ownership.authorize_lpar_mutation, its private
_read_lpar_description_for_authorization and ssh.profiles.get_lpar_description. Their None
defaults preserve existing callers and guard/audit policy; only this operation supplies65536.
No CLI/MCP limit parameter, dependency, arbitrary proxy or global caller policy is added.

Installed AsyncSSH 2.24.1 has no cumulative output cap: connection.py:4650 delegates run
to process.wait, and process.py:1451–1478 communicate disables backpressure before collecting.
Native create_process/create_session support binary streams, receive window and packet size;
stream.py:137,436–467,529 implements bounded reads and shared stream backpressure. Use
`create_process(cmd, encoding=None, window=4096, max_pktsize=4096)` and two concurrent
stdout/stderr readers, each requesting at most4096 bytes. Both streams consume one aggregate
budget even when include_stderr is false. Check/increment/append without an intervening await;
never retain a chunk which would cross the limit. Exact-limit output succeeds only after EOF.
Always drain both streams; no sequential-read deadlock or ignored-stderr escape is permitted.

The selected route never calls connection.run, process.wait, communicate, collect_output,
read(-1), unbounded readline or file redirection. Successful readers finish at EOF, then
process.wait_closed supplies exit metadata without output collection. Decode the bounded
streams as strict UTF-8, preserving existing successful stdout/[stderr] formatting. A complete
nonzero exit constructs ProcessError with bounded decoded stdout/stderr and native status/signal,
then uses existing error translation. Missing exit status or invalid UTF-8 is uncertain failure.

The configured timeout covers connect, process creation, draining and normal exit. Overflow
immediately aborts the connection and raises HMCCLIError with an explicit unknown-completion
message, no output fragment and no ProcessError cause. Timeout, reader error and cancellation
also abort before connection-context exit; close the process if obtained, cancel and await the
two owned reader tasks in finally. Cleanup awaits only cancelled local readers and aborted
connection/process closure, never remote exit/acknowledgment. External CancelledError propagates;
no detached task, shielding, background collection, compensation or replay is introduced.
Keep the existing secret/log-redaction wrapper around this path. With declared secrets it
continues removing exception chains; absent secrets, genuine ProcessError causes survive.

The cap bounds retained command payload bytes, not encrypted network traffic or total process
RSS. Two fixed reader chunks and AsyncSSH's4096-byte receive window/backpressure bound output
lookahead; the library rejects channel data exceeding its remaining window (channel.py:568–614).
Packet-size advertisement alone is not an enforced payload cap. Connection protocol/socket
buffers remain library-owned; no claim says at most65536 bytes can arrive on the wire.
Output overflow during mutation is uncertain even if exit information has arrived: independently
read back the target on the normal error path. Cancellation returns no successful result and
requires later independent reconciliation; aborting SSH cannot promise remote command rollback.

## Outcomes, warnings and concurrency

After native return or HMC/SSH failure, re-resolve containment and independently read the
kind-specific REST entry and native CPU row. Missing/malformed/changed identity, mode or
state yields unverified. Compare requested current values; ordinary changes must preserve
unrequested CPU quantities and memory assignments. For active nondeferred changes also
require run_procs and shared run_proc_units to match the whole resulting CPU target.
Reset success requires the complete CPU and memory current triplets to be zero; a partial
reset is never applied. Native and REST current values must still agree.

Return UUID, outcome (`applied`, `noop`, `rejected`, `partial`, `unverified`),
workflow_completed, before, after, expected, submission_error, change_location and warnings.
Before/after are typed-by-contract finite dictionaries: base memory snapshot plus processor
mode, current/pending/runtime count/unit envelopes; expected holds requested targets,
reset, deferred and noop. Decimal amounts become two-place strings at this boundary.
Only applied/noop set workflow_completed true. Matching independent state can establish
applied despite a retained submission error; no causal attribution to this request is claimed.
A completed positive native exit status without an exit signal plus unchanged relevant state
is rejected; signal termination, missing status, byte overflow and decode failure are uncertain.
Changed but incomplete requested/side-effect state is partial; unchanged state after uncertain transport or failed
readback is unverified. No replay follows any of those states.

Reuse change_location_of/ChangeLocation for CurrentProfileSync classification and warnings.
Inactive readback proves current configuration only; force/no-RMC adds an operating-system
restart warning. Coupled reset warns to inspect CPU and memory before activation. Preserve
existing authorization/audit interception and submission errors; do not claim success from
an exit code, command text or generated schema. No profile-sync mode is enabled implicitly.

## Failure model

1. Actors/deployments: authenticated local CLI and authorized MCP callers against configured
   HMC, with untrusted HMC responses; offline CI with captured-shape fixtures.
2. Invariants/assets: contained owned partition, exact CPU units/current baseline,
   no mode/pool/policy mutation, CPU/memory availability, old REST If-Match contract,
   truthful uncertain/partial results and preserved profile scope.
3. Accepted classes: another administrator or capacity change after the final check can
   cause rejection/partial effects; no atomic SSH/REST transaction exists. Runtime fields
   unavailable after an admitted mutation mean unverified. A bounded-output overflow/timeout
   may follow effects; abort stops local receiving, not remote execution. SSH protocol/socket
   buffers are outside the command-payload cap. Cancelled callers receive no completion claim
   and must reconcile before a new request. Live variants not exercised stay unevidenced.
4. Covered elsewhere: ownership/access/audit ADR0092/0040; pool lifecycle #642, processor
   moves #1779 and sharing/weight #1780/#1781; profile operations #1724; absolute resources
   setter's original contract and tests. These boundaries do not remove D05 criteria.

## Validation and live evidence

The plan's per-contract Verification inventories cover quantities, admission, readback,
transport and required ownership-read closure, CLI/MCP routing and the retained absolute setter.
Each new/suspect test must fail under its named controlled fault before restoration/green.
Fixtures distinguish captured shapes from constructed edge values; real local AsyncSSH peers
exercise flow control and cleanup without hardware. Overflow must prevent authorization and
processor commands; default callers, denial and override audit behavior stay covered.

The implementation PR adds change_lpar_processors as Reconfiguring in ADR0092§3.2, with
its real guard citation and #1778/ADR0296 provenance, as required by §6; policy is unchanged.
Implementation updates operations/maturity/reference bindings, generated tool docs/runtime
projection, CHANGELOG and a reproducible named arm56 (`lpar-processor-quantities`,
`st56-lpar-processor-quantities`) under existing preflight/wrapper/recovery architecture.
That initial arm uses one owned inactive shared AIX/Linux fixture, required stamp, no disks,
no activation, no force and no foreign-resource mutation. Existing guarded setup may apply
its own default profile once only when both CPU and memory current vectors are all zero;
contradictory/mixed setup refuses. Use minimum/desired/maximum entitlement 0.1/0.5/1.0 and
vCPUs 1/2/4 only if the selected system's minimum units/vCPU admits them, with 1024/2048/4096
MiB memory aligned to its region. The sequence attempts remove 0.1 units (target 0.4),
a zero delta, an over-maximum refusal, then explicit set to original 0.5 units, stopping
on the first unexpected outcome. Independent REST/native reads verify each step.
Inactive positive operations are hypotheses; native refusal fails the arm without replay.
Finally delete only the matching-token inactive fixture and compare names/free resources;
recovery runs even after apparent success. Output stays in a private external directory.

The procedure is preflight `--group lpar-processor-quantities`, named wrapper
`scripts/live_lpar_processor_quantities.py --results-file "$RUN_DIR/results.json"`,
recovery `--results "$RUN_DIR/results.json"`, then evidence generation from that file;
each runs via `uv run --no-sync python` at the deployed candidate commit. Explicit live-window
authorization and clean baseline precede it. Arm56 does not prove dedicated, vCPU-only,
active-RMC, force/restart, IBM i or coupled reset behavior; those remain explicit live gaps.
The complete product still implements and offline-tests those documented contexts.

Rollback is reverting the code/metadata change; it cannot undo external processor effects.
An operator reconciles partial/unknown state with fresh reads before any separate remedial
request. Temporary owned live fixtures are cleaned using existing token and inactive guards,
never blind delete or automatic inverse arithmetic. No full suite or hardware run is claimed
by this design; implementation later requires just verify and pinned prek, then CI.
