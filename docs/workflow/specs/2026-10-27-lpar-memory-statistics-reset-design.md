# Entitled-memory statistics reset — full design for #1777

Proposed design; implementation and hardware remain held behind normal gates.
ADR0288; M250; EMPTY exclusions; parent #638 D04. Sourcebase2ff6c6f7;
read-only integration context616a7dbf. Operator decisions are recorded in the issue workflow.

## Problem and approved scope

Provide the documented one-partition chhwres mem rs path through a strict request,
domain operation, CLI and MCP, with truthful independently observed statistics.
Allocation quantity/move, paging/pools and saved-profile behavior keep their owners.
The four exact approved proposals govern observation, interfaces, bounded admission
and row representation. No additional OS/range/unit/reset oracle is invented.
This is one deliverable: each layer and named arm serves this same operation.

## Global Constraints

Python >=3.11; CI Python3.11,3.12,3.13,3.14 on Ubuntu24.04 amd64.
No dependency, CI action, interpreter-floor or stable hmcpctl.api export changes.
Use existing pydantic/asyncssh/typer/FastMCP and stdlib csv, re, shlex.
Keep allocation APIs/results, shared permissive parser, config defaults and existing permission semantics.
Required system/partition selectors; canonical UUID comparison ignores case, resource names do not.
Only one native reset attempt; no force/retry/reverse/activation/allocation/profile write.
Finite configured SSH timeout and64KiB combined command-output cap remain in force.
Native wait0 disables only the guest-command timer, not client timeout or server execution.
Public artifacts contain no lab identifiers; private live results stay outside checkout.
Live proof uses the matching deployed commit, preflight, named arm, recovery and generated evidence.
Never promote not-run/skipped/failed or a raw dedicated statistics read to live reset evidence.

## Requirements and Expected mapping

R1 Documented reset/typed CLI/MCP: strict dedicated request and one fixed mem rs command.
R2 Approved eligibility: unique contained authorized UUID/kind/positive ID/name; positive
current shared, Running and selected-system ActiveMemorySharingCapable; usable before.
Missing/malformed required facts or dedicated target refuse before submit. No release
floor or universal RMC/OS restriction; HMC validates undocumented final eligibility.
R3 Optional wait/detail only for exact reported PartitionType AIX/Linux; this does not
verify installed OS. Base reset can reach other contained environments satisfyingR2.
R4 Preserve ownership, operation audit and all-target MCP authorization; quote names/system
at destination, use resolved numeric ID, and recheck context/ownership before submit.
R5 Separate native completion from observations; no units/ranges/reset-cause guarantees.
R6 Strict row/data policy and one recognized numeric-text minimum, with retained extras/gaps.
R7 Independent after observation and identity/mode admission; native nonzero/unknown cannot
complete the workflow. No effect retry or counter-history restoration.
R8 Meaningful negative/reference/transport/presentation tests, generated registry/maturity,
ST67 reproducible procedure/read-only recovery and normal full shipping gates.

Original Expected1/4 oracle/per-counter completeness is explicitly amended by the
observation decision, not silently satisfied. Expected2 complete client eligibility is
explicitly amended by bounded-admission approval. Expected3/5/6 remain covered byR4–R8.
Parent638 finite row allocation, independent implementation/evidence, privacy and no
hardware-by-issue rules remain unchanged. Allocation rowcli:commands/chhwres is shared;
register new semantic operation without duplicating source units or changing denominator.

## Exact interfaces

Input owner documents/lpar_memory.py: LparMemoryStatisticsReset(BaseModel), frozen,
strict=True, extra=forbid; wait_minutes:StrictInt>=0|None=None;
detail_level:StrictInt between1and4|None=None. Empty request selects native defaults.
Domain operations/lpar/memory_statistics.py:
reset_lpar_memory_statistics(hmc:HMCClient,lpar_name_or_uuid:str,reset:LparMemoryStatisticsReset,
*,system_name_or_uuid:str)->dict[str,Any].
No companion public statistics-read operation. Private observation is an internal helper.
CLI lpars memory-statistics-reset NAME --system SYSTEM [--wait-minutes N]
[--detail-level N] [--yes] (detail N=1..4); negative/blank input before client.
MCP hmc_reset_lpar_memory_statistics(lpar_name_or_uuid:str,system_name_or_uuid:str,
reset:LparMemoryStatisticsReset|None=None,profile:str|None=None)->dict[str,Any]. Omitted reset becomes the strict empty request.
Tool metadata mutate/operationlpar.reset_memory_statistics/target_kindlpar/exhaustive_targets=False.
The selector can resolve either LPAR or VIOS; existing policy only labels raw selectors and cannot
bound that union with a specific lpar/vios targets table. Require existing all-targets grant
for this tool/connection, plus domain containment/ownership. No new resolver, flag or grant namespace.
No public ownership override; both ownership checks use existing override=False and64KiB bound.

Internal SSH owner ssh/memory.py adds read_lpar_memory_statistics(config:HMCConfig,
system_name:str,partition_id:int)->str and reset_lpar_memory_statistics_native(config,
system_name,partition_id,reset)->str. Both use max_output_bytes=65536 and finite SSH owner.
Read exact command: lshwres -r mem -m QSYS --level lpar --filter lpar_ids=ID --stat.
Write exact command: chhwres -r mem -m QSYS -o rs --id ID, then -w/-d only if non-None.
IDs must be positive strict integers; system must be nonblank and shlex.quote encoded.
The local strict parser belongs to new domain module, not shared commands.py.

## Statistics representation and parser

Each observation is {raw:str|None,entries:list[entry],missing:list[str]|None,usable:bool,error:str|None}.
Each entry is {key:str,value:str,kind:numeric|unavailable|opaque}; preserve order/spelling/text.
Known keys exactly min_io_entitled_mem,optimal_io_entitled_mem,max_io_entitled_mem_usage.
For valid rows missing lists absent known keys in that fixed order; invalid/unreadable rows use
missing=None and empty entries because no qualified field inventory exists. Raw/error remain. Numeric means unsigned ASCII digits
including0, stored as text; never integer conversion, unit assignment or numeric comparison.
Unknown bounded fields are opaque. Other known values stay visible unavailable, including
empty/null/N/A/negative/decimal/exponent. At least one numeric known entry makes a valid row usable.
Raw row remains available on representable parse failure; no default inferred from missing.

Decode strict text; total UTF8<=65536bytes. Exactly one nonempty physical CSV record with
one optional trailingLF orCRLF; no additional/blank records, NUL/controls, invalid text or banners.
CSV strict, at most256cells; split each on FIRST equals sign. Key must match
[A-Za-z_][A-Za-z0-9_]*, ASCII<=128bytes; value<=4096UTF8bytes; no duplicate exact keys,
empty/bare cells or malformed quoting. Quoted whole cells may contain opaque comma/equals values.
Preserve whitespace in values; key whitespace fails. No key alias/case folding.
These limits are application representation bounds, not HMC maxima. No all-three requirement.

## Result contract

Closed result keys: system_uuid,system_name,lpar_uuid,lpar_name,partition_id,partition_kind,
request,native_completion,native_output,submission_error,before_statistics,after_statistics,
after_error,identity_preserved,workflow_completed,reset_verification.
native_completion: not_submitted|completed|nonzero_exit|unknown.
Native stdout is bounded text when returned; errors are bounded diagnostic strings.
identity_preserved=None means not independently established, False means observed identity
mismatch, True means matching after-context; unavailable observation is not false identity.
A baseline observation failure yields not_submitted and incomplete with retained observation.
Pre-write request/identity/capability/ownership errors use existing actionable exceptions.
Submission happens once after admission. Zero transport return means completed; explicit
positive no-signal remote process exit means nonzero_exit; transport/timeout/signal/absentstatus/
output/decode failures remain unknown. Do not parse prose/refusal tokens to infer completion.
reset_verification is literal unavailable for results governed by this design.
workflow_completed iff native completed, usable before/after and matching admitted identity/mode.
Same/zero/lower/higher values or changed field sets do not change verification semantics.

## Algorithm and error ordering

1 Strict request/selectors before client dispatch; resolve system and unique contained
LPAR/VIOS, get direct identity/current mode/state, positive system AMS and system CLI name.
2 Authorize ownership. Read one-ID statistics, parse bounded row. Unusable baseline returns
not_submitted/incomplete; no reset call. Validate optional settings against reported AIX/Linux.
3 Independently re-read full containment/direct context and system capability; compare
UUID/kind/name/nativeID/shared/state/system identity with initial context, reauthorize ownership.
Any change/refusal prevents submit. Do not compare volatile statistics or infer installed OS.
4 Invoke native reset exactly once. Catch ordinary HMC/transport errors into native fact;
propagate cancellation/BaseException so the owning runtime closes; no replay/fallback.
5 Re-resolve the same UUID in the same system and validate context before the after query.
If it changed/unavailable, record after_error and incomplete; do not query another native ID.
Otherwise independently read/parse after stats, then revalidate identity/mode once more.
6 Return facts and exact workflow boolean. Nonzero/unknown stays incomplete. No mutation in
reconciliation; independent read failure preserves submission and before facts.

Existing HMCCLIError gains remote_nonzero_completed:bool=False, normalized by isTrue.
Only _process_error setsTrue for strict positive int exit_status with no exit_signal;
copy that boolean through declared-secret reconstruction without cause/context. All other
error builders keepFalse. Existing messages/refusal code behavior stays unchanged.
No general transport result abstraction or numeric-exit-code public promise.

## Named live procedure and read-only recovery

Root assigns ST67/group lpar-memory-statistics/scenario st67-lpar-memory-statistics-reset.
Use implemented LiveTestConfig.lp3_name via LIVE_TEST_LPAR_NAME; no new fixture config field.
Require an existing positively owned, non-protected Running shared fixture with positive AMS,
usable baseline and exact candidate/destination. No creation, activation, pool/provider or media
setup belongs to this issue. Missing fixture is a live prerequisite, not scope added to1777.
Arm calls MCP once with empty settings/no override and records durable baseline, reset intent,
returned result, independent final stats/context. Checkpoint baseline and intent before call.
Rows form the finite journal; no duplicate new generic ledger/artifact framework.
Independent final observation verifies current eligibility and environment allocation fields
(CurrentMemory,DesiredMemory,RuntimeMemory,CurrentMinimumMemory,CurrentMaximumMemory),
not counter equality. Missing allocation fields are unavailable preservation evidence, not0.
Context/allocation changes fail the live preservation assertion; no corrective write follows.

Recovery checks nonpartial candidate/destination/config, exact ordered baseline/intent/result/
final rows, exactly one submit and matching result shape plus fresh read-only identity/current
allocation observations. Partial/missing/malformed/uncertain histories refuse confirmation.
It does not repeat reset, recover counter history, establish reset causality or infer no effect
from nonzero. Use text OBSERVED: finite reset-attempt record and current environment confirmed;
reset verification unavailable; not CLEAN/restored counters. Return0 only for complete finite
zero-exit observation workflow plus fresh preserved environment; otherwise2/unconfirmed.
Live evidence assertion IDs: native-command-completed,usable-before,usable-independent-after,
contained-shared-identity,allocation-observed-unchanged,reset-verification-unavailable.
cleanup not-required means no fixture resource effect/cleanup obligations were introduced;
it is not history restoration. PASS promotes only this approved observation workflow.
Live-running-OS/options variants not exercised remain unevidenced; no dedicated read promotion.

## Failure model

1 Actors and deployments: authenticated CLI/MCP operators and library callers using an HMC;
CI tests mock external boundaries only; root-led live arms on separately authorized hardware.
2 Invariants/assets: ownership/containment, one irreversible counter-history effect, honest
native completion and provenance, bounded untrusted text, unchanged allocation APIs.
3 Accepted classes: undocumented native eligibility may refuse at HMC (approved); no causal
reset oracle or complete counter inventory (approved); external uncoordinated writer races
cannot be made atomic by native ID command, but observed mismatch stops/fails this workflow;
local timeout cannot guarantee server cancellation. No accepted class permits an automatic retry.
4 Covered elsewhere: transport auth/trust/config by existing HMCConfig/SSH owner; MCP connection
and target grants by tool registry; fixture provisioning and campaign hardware scheduling by root;
public evidence/privacy by existing exporter and project policy. No extra scope exclusion added.

## Threat model

Boundaries added: typed reset settings through CLI/MCP; raw SSH row/native errors into result;
finite live result rows into recovery/evidence. Existing ownership/target boundaries are reused.
Actors: authenticated clients can send malformed settings/selectors; HMC can return malformed,
oversize or misleading data; local result files may be incomplete/tampered; fixture writers may race.
Controls: strict request+nonblank selector before I/O, exact containment/system/ownership and
MCP all-targets policy; fixed commands/quoted system/numeric ID; output/time caps and strict parser;
explicit transport provenance rather than text matching; closed finite row/candidate/destination
validation and no recovery writes; terminal encoder and public filtered exporter.
Trust host/runtime and configured HMC authentication; compromised HMC/host or forged matching
complete evidence by a privileged operator is outside this feature's attestation capability.
No secret-bearing cause/context is retained by declared-secret error reconstruction.

## Ownership, alternatives and validation

Extend existing request/SSH/CLI/MCP owners; new cohesive domain module owns context/parser/flow,
new live module owns arm/journal validation/recovery. No migration/obsolete path or facade alias.
ADR0288 records alternatives: allocation rs, causal counter checks, global parser change and
public read tool rejected. Existing allocation and permissive parser regression suites remain.
Plan mapsR1–R8 to concrete tasks/cases; generated artifacts are regenerated before checks.
Full verify and separate pinned14hooks before push; CI3.11–3.14 is distinct evidence.
No unit test pretends to prove live reset, counter history or exact modern eligibility.
