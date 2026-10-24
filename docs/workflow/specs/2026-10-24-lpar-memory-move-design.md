# D02: Native two-partition memory moves

## Problem and authority

Issue #1775 is D02 of #638. Scope q1775-ecd59f74 in issue comment 6385449022
owns the native non-pool memory move, with an explicitly empty exclusion-owner set.
The baseline is 40f9f85672dee57c6dcc03d6446534efab93e43e. The implementation is one
PR; design review and scope audit precede the campaign coordinator's build release.
[ADR 0286](../../adr/0286-lpar-memory-move.md) records the two-target boundary.

The existing D01 single-partition operation remains its own contract. A move is one
native `chhwres -r mem -o m` submission, not two add/remove requests. Both endpoints
must be visible in policy, ownership admission, readback and the result.

## Reference contract

- Local P10 `commands/chhwres.md:90–96,215–219,224–225,234–236` and P11
  `commands/chhwres.md:90–96,217–221,228–229,237–239` describe
  source `--id`, destination `--tid`, MiB quantity aligned to the system region,
  no shared-memory moves, and the AIX/Linux/VIOS no-RMC force path. The D02
  synopsis selector in each corpus has SHA256
  `aab720aa0d44c72aada0883254e2f510ab75ee346218ff8d1428ad07ec4dc53a`.
- The [published P10 command reference](https://www.ibm.com/docs/en/power10?topic=commands-chhwres)
  and [P11 reference](https://www.ibm.com/docs/en/power11/9824-42A?topic=commands-chhwres)
  corroborate the grammar. The quantity uses the existing D01 MiB representation;
  no decimal conversion or rounding is introduced.
- [IBM dedicated-memory guidance](https://www.ibm.com/docs/en/power10?topic=dynamically-managing-dedicated-memory)
  establishes the reservation/concurrency boundary: assigned min/max constrain
  movement; the native system protects IBM i base-pool minimum and can release
  fewer pages; simultaneous DLPAR can leave unexpected amounts. DPO must complete
  or be stopped by its operator before a live move. This operation does not stop DPO.
- Local `commands/lshwres.md:99–100` describes a firmware minimum calculation,
  not a guest QBASPOOL reader. The local QueryReservedMemoryRequiredForPartition
  job reference supplies a sample request but no complete response contract.
  Neither is treated as an independently proved guest reservation calculator.

C2 reservation validation combines client checks of reported assigned bounds with
native firmware/guest admission. It does not promise that every reservation is
precomputed before submission. The coordinator confirmed this interpretation from
C2 and the sources on 2026-10-24. Native reservation refusal or incomplete release
is preserved in the dual outcome; the implementation never guesses a guest floor.
The source files are referenced, not reproduced into tracked documentation.

## Architecture and interfaces

Add `LparMemoryMove(quantity_mib: StrictInt >= 0, force: StrictBool = False)` to
`documents/lpar_memory.py`, frozen and extra-forbidden. Endpoints are separate
arguments, not nested fields. This prevents hiding a second mutation target in a
model whose original MCP selector covers only one partition.

Add `move_lpar_memory(hmc, lpar_name_or_uuid, target_lpar_name_or_uuid, move, *,
system_name_or_uuid, ownership_override=False)` in `operations/lpar/memory_move.py`.
Reuse D01's existing internal snapshot, identity, state, postcondition and endpoint
result functions directly; no utility layer or responsibility migration is needed.
Do not reuse its growth-versus-free-system-memory check: a transfer releases the
source quantity, and requiring that quantity already be unassigned would reject
valid moves. Check the two partition envelopes and configurable system total;
native admission owns remaining transient capacity and reservation decisions.

Add a fixed `move_lpar_memory_quantity(config, system_name, source_id, target_id,
move)` encoder to `ssh/memory.py`. It submits exactly one `mem m` command, with
quoted system name, validated distinct positive numeric IDs, `-q` and optional
`--force`. No entitlement, arbitrary options, ETag, remove/add emulation or replay.

Expose `hmc_move_lpar_memory(lpar_name_or_uuid, target_lpar_name_or_uuid,
system_name_or_uuid, move, profile=None, ownership_override=False)` in the existing
lifecycle module. Its operation identity is `lpar.move_memory`, effect `mutate`;
explicit `extra_targets=(("lpar", "target_lpar_name_or_uuid"),)` joins automatic
source/system selectors. One existing grant must cover connection and both endpoints.
This clean extension avoids changing the existing memory tool's optional-target
policy contract. Nested target selectors already exist, but an optional destination
would require conditional selection semantics to preserve those existing calls.

Extend the existing CLI memory module with `lpars memory-move SOURCE DESTINATION
--system SYSTEM --quantity MIB [--force] [--ownership-override] [--yes]`.
Confirmation names both endpoints through terminal sanitization and discloses
partial/uncertain effects and force's restart requirement. A non-completed result
prints structured evidence and exits nonzero. No stable-facade export is added.

## Admission and execution

1. Require a typed request and nonblank selectors. Resolve one managed system,
   then source/destination independently within its LPAR and VIOS inventories.
   Refuse missing, ambiguous, foreign, malformed or identical UUIDs; also refuse
   equal numeric IDs. Check the fetched names still match name selectors.
2. Authorize ownership for both endpoints using the existing guard. The optional
   override is the existing explicit operator mechanism and applies to both;
   an ownership denial sends no move. MCP target authorization precedes connection.
3. Require the same reported dedicated-memory mode, valid independent current and
   configured envelopes, and supported partition types. Reuse D01 state admission:
   stable running or not activated; ordinary running AIX/Linux/VIOS needs active RMC
   and reported dynamic-memory capability; explicit force admits documented no-RMC
   deferred effects. Running IBM i uses reported capability, not an invented RMC
   requirement; force is refused if either endpoint is IBM i. Inactive acceptance
   remains native admission, not a blanket running-only restriction or a promise
   that an inactive destination accepts memory. Unknown/transitional states refuse.
4. Read the selected operating managed system and validate identity/name, positive
   MemoryRegionSize and ConfigurableSystemMemory. Quantity and both resulting
   current quantities must align. Source current minus quantity must remain at or
   above its current minimum; destination current plus quantity must fit its
   current maximum and system total. A positive move to an inactive destination
   with a zero current envelope refuses before submission: the published contract
   does not establish resulting bounds that this operation can verify. This is a
   client verification limit, not a claim that native HMC rejects the case. Zero
   requests and nonzero-envelope inactive destinations keep their existing admission.
   A source below its current minimum is not admitted as a coupled CPU reset:
   the reference's shutdown reset wording describes removal, not a proved move.
5. Before sending, recheck containment and snapshots for both endpoints, system
   identity/state/region/total, and both ownership decisions. Changed evidence
   refuses. These are observations, not an atomic lock on native administrators.
6. Zero quantity returns a dual verified no-op after the same admission/rechecks;
   otherwise issue the one fixed move. Catch the existing transport error, retain
   its bounded message and distinguish a normal positive native exit status without
   an exit signal from lost transport, missing status or signal termination. Do not
   automatically retry or compensate any submitted request.

DPO, guest reservation availability and other native concurrent operations can
still reject or partially apply after the final reads. No absence of a reported
partition operation is claimed to prove the absence of a system-wide DPO task.
HMC release support is the published P10/P11 command contract plus reported
partition/system capabilities, not a guessed version threshold. Unsupported or
unreadable evidence refuses locally or remains an honest native rejection.

## Dual observation and result

After a submitted command, attempt independent containment/readback of source and
destination even if the first fails. Retain each available snapshot and its
bounded read error. Reuse D01's per-endpoint expected current/runtime checks and
change-location reporting. For a normal running endpoint, runtime must converge;
for inactive or forced/no-RMC endpoints, report current-configuration effect and
its deferred guest effect separately. Both current min/max envelopes and processor
vectors must be valid numeric observations and remain unchanged; invalid baseline
CPU evidence refuses before submission and invalid postread CPU evidence is unverified.
A changed identity/state/mode or malformed snapshot cannot verify that endpoint.

The aggregate result contains `outcome`, `workflow_completed`, `quantity_mib`,
`submission_error`, `source`, and `destination`. Each endpoint uses the D01 result
shape: UUID, before, after, expected, outcome, workflow_completed, change_location
and warnings, plus an explicit move-only read_error. `applied` means both expected postconditions were independently
observed; `noop` means a zero request with both unchanged fresh snapshots. Those
are the only completed outcomes. An observed incomplete change is `partial` when
both endpoints can be evaluated. A definite native refusal with both unchanged is
`rejected`. Missing/failed/invalid observation of either endpoint, or unchanged
state after uncertain submission, is `unverified`; the other endpoint's evidence
is retained. A matching pair can establish applied state despite a retained
submission error. No result asserts causation against an unrelated writer.

There is no automatic profile write, activation, restart, rollback or reconciliation
loop. Per-endpoint CurrentProfileSync produces distinct change-location warnings.
Before a manual retry, an operator inspects both current/runtime states and native
completion, chooses a fresh intent, and obtains authorization for any reverse move.
A reverse move is another operation with its own admission and can fail.

## Global Constraints

Python >=3.11; hosted target amd64 Ubuntu 24.04 on Python 3.11, 3.12, 3.13 and 3.14.
Use installed dependencies and stdlib only. Run uv with --no-sync; bootstrap with
just setup. Never add a stable hmcpctl.api export or arbitrary command surface.
All public evidence uses anonymous identifiers; raw evidence stays outside repositories.
Generated documents are regenerated by their declared commands, not hand-edited.
No delegated hardware, login, deploy or source-probe call is authorized by this design.

## Failure model

1. Actors and deployments:
   - Authenticated CLI operators and policy-authorized MCP callers on configured HMCs.
   - Dedicated-memory AIX/Linux, VIOS and IBM i endpoints; offline CI boundaries.
   - Native administrators can modify resources concurrently; HMC data is untrusted.
2. Invariants and assets at stake:
   - Both contained targets are authorized before the single memory mutation.
   - Exact MiB intent, source floor, destination ceiling, preserved CPU assignments.
   - Availability and truthful incomplete/unknown effects, with both observations retained.
3. Accepted failure classes:
   - Native DPO/reservation/capacity/other-writer refusal or partial effect: the native
     contract owns final admission, and dual readback exposes observed effects.
   - Transport loss or unreadable postconditions: result is unverified without replay.
   - No cross-system atomic fence or causal attribution exists for native CLI commands.
   - Positive inactive/forced/IBM i/VIOS behavior remains unevidenced until exercised.
4. Covered elsewhere:
   - Existing connection, grant and ownership controls: ADRs 0036, 0039 and 0092.
   - Single-endpoint quantities #1774; pool #642; paging #1776; statistics #1777;
     profile CRUD #1724. None is an omitted D02 requirement or a new exclusion.

### Threat model

- Added boundaries: second target selector and typed move enter fixed SSH encoding;
  the new MCP operation exposes both endpoints to the existing grant conjunction.
- Widened boundaries: existing HMC snapshot parser is consumed for two targets;
  per-endpoint ownership and containment are evaluated independently.
- Untrusted actors: CLI/MCP callers and malformed HMC responses. Trust remains in
  configured credential/transport authentication and native HMC reservation admission.
- Controls: strict model, explicit scalar selectors, one-grant authorization, two
  ownership checks, same-system/distinct-ID checks, D01 numeric/state validation,
  shell quoting, bounded SSH timeout and retained submission/read-error diagnostics,
  independent readbacks, no automatic replay. No transport-output cap is provided.
- Out of scope threats: a compromised HMC or authorized native administrator can race
  these reads; transport authentication and existing audit own their respective layers.
  No client guarantee turns that race into a transaction or a false completed result.

## Validation and live procedure

The plan covers input/encoding, two-target authorization and containment, source and
capacity boundaries, state/type/force combinations, races, readback independence,
partial/rejected/unverified results, profile location, presentation and maturity.
Each executable contract receives focused red/green evidence; existing D01 tests
prove that its unchanged model, command and result contract remain intact.
Run the relevant static gates, then final just verify and pinned all-file hooks;
hosted Python legs remain separate evidence. Design-only work runs no full suite.

Add opt-in arm54/group `lpar-memory-move`, assigned by the coordinator for C6,
with named `scripts/live_lpar_memory_move.py`, preflight and read-only recovery.
Reuse existing `LiveTestConfig.lp3_name` and `scratch_name` as the explicitly
configured source and destination; neither is created or deleted. Require unique
UUIDs and non-protected resolved names at each admission (including UUID aliases),
owned AIX/Linux running guests, active RMC, dedicated memory, dynamic-memory
capability and profile sync Disabled/Suspended. These are the proof arm's positive
prerequisites, not restrictions on the full product. No force/activation/OS deployment
is used to manufacture eligibility. An unavailable prerequisite stops without
positive evidence. The operator must approve an exclusive no-DPO window.

Record both independent CLI memory baselines, resolved identity/type/state, ownership
and system free-resource/name baseline before effects. Checkpoint the baseline and
each finite intended operation through the existing private runner results sink.
Attempt one memory-region move. A separate native CLI oracle must confirm source
minus region and destination plus region, current/runtime convergence and unchanged
bounds. Before proceeding, the forward result must report PASS, workflow_completed true,
outcome applied and an explicitly present null submission_error; absent is not null.
Only after that submission-and-observation proof, recheck exact identity/ownership/state
and expected current/runtime quantities, then attempt one explicit reverse test operation.
Verify both original baselines and system free resources independently. The reverse
is the second finite case, never rollback after a failed or uncertain submission.
Stop writes on rejection, partial/unverified effect, drift, failed oracle or checkpoint.

The named recovery path reads both stored endpoints and original baselines without
writing, creating, stopping or deleting anything. CLEAN requires run.partial explicitly
false, the ordered baseline → forward intent → successful forward observation → reverse
intent → successful reverse observation → final original-baseline confirmation, and
fresh equality of both original endpoint and system baselines. Both phase results must
meet the same submission-and-observation predicate, including explicit null submission
errors. Intents, quantities, before-states and independent observations must match the
original fixtures and their one-region forward/reverse requests within original bounds;
missing, duplicate, reordered or mismatched stages cannot clear recovery. Fresh read-only
witnesses still run for readable valid baselines when journal completion cannot
authorize CLEAN.
Partial, incomplete, refused and pre-effect runs return nonzero/unconfirmed even when
current baselines match; the coordinator decides any later recovery. These rules do
not change native product outcome semantics. Positive arm proof covers ordinary running
AIX/Linux dedicated memory only; other product types, inactive and forced variants
remain unevidenced until separately exercised.

The procedure names preflight, wrapper, recovery and evidence commands, candidate
revision, external mode0700 evidence directory/mode0600 files, a two-move budget,
per-call SSH/HMC deadlines and the controller's bounded session/logout/reaping.
Actual execution stays with the root's separately approved candidate-bound window.
