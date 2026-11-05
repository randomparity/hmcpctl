# Terminal decommission ledger reclamation

## Problem and authority

Issue #1890 identifies partition and retained-resource rows that consume capacity after
`delete_owned` has deleted their partition. ADR 0323 deliberately leaves those rows.
The operator approved the provenance-preserving rule on 2026-11-04, recorded in
[the approval](https://github.com/randomparity/hmcpctl/issues/1890#issuecomment-6647344871)
and frozen in [scope](https://github.com/randomparity/hmcpctl/issues/1890#issuecomment-6647359549).
[ADR 0341](../../adr/0341-terminal-decommission-ledger-reclamation.md) records the decision.

## Global Constraints

Python >=3.11; hosted verification targets amd64 on Python 3.11, 3.12, 3.13 and 3.14.
No new dependency, database schema, migration, public facade, or HMC operation.
Use the existing managed-system UUID plus partition UUID ownership namespace.
Retained physical storage deletion, retain-mode changes, old-history rewrite, and general
ledger garbage collection are outside this issue and operator-owned.
Abandonment, nonterminal states, retain mode, dry runs, unproven deletion, missing or
oversized fresh results, and historical terminal records keep existing semantics.
Existing 30-day operation-result retention remains; longer archival is operator-owned.

## Success

- C1: a fresh completed/failed `delete_owned` termination with matching applied partition
  deletion and complete fresh storage provenance removes its partition/retained-resource
  rows, freeing their contribution to capacity; no other system/partition rows change.
- C2: each unreadable retained entry preserves its original JSON identity losslessly in
  optional `ledger_identity`; readable entries retain their existing shape.
- C3: state/result/reclamation form one SQLite write transaction. A failing result/state
  write or ledger delete leaves the previous state/result/ledger together recoverable.
- C4: the Global Constraints' unchanged cases are not retroactive cleanup entry points.
- C5: persisted status and reconstructed decommission results retain storage provenance
  on completed, failed-after-delete, and repeat reads, including distinct unreadable rows.

## Architecture and data flow

Extend existing owners; no ownership move or new finalizer framework. `store.set_state`
continues to own persisted terminal transitions. `engine._end` already wraps it in
`store.write_transaction`; admission/operator abandonment use that transaction too.
`decommission_storage.owned_items` remains the owner of projected storage identities.
`decommission_operation` carries the cleanup pass result to the engine; its normal and
HTTP-412 failure paths already do so. No caller signature change is needed.

Add one private store helper called by `set_state` for an eligible transition, before
releasing the transaction. Read the previous operation row to exclude a terminal replay;
use the incoming `result_json`, never the COALESCE-preserved previous result, as proof of
freshness. Eligibility requires tool `hmc_decommission_lpar`, recorded request mode
`delete_owned`, nonempty guarded system/partition, requested state terminal and outcome
completed/failed, and that operation's `delete_lpar` effect with kind `lpar.delete`,
status applied, target `lpar:<guarded partition>` and identity `lpar_uuid` matching the
partition. UUID identity comparisons are case-insensitive. Open effects prohibit cleanup.

A complete fresh storage result is the successfully serialized cleanup pass result with
`storage` object, `deleted`, `retained`, and `pending` arrays, and empty `pending`.
The bounded internal producer is `decommission_body.run`: only after `clean_storage`
returns does it construct this result. Exceptions before that return supply no complete
new storage pass. The store must also verify remaining storage rows have retained
provenance: a readable row matches its existing canonical projected identity in retained;
an unreadable row matches a retained entry with the same kind, reason unreadable_record,
and exact `ledger_identity` JSON value. Duplicate readable rows may share one entry as
ADR0323 already deduplicates those resources; distinct unreadable identities must not be
merged. Missing coverage makes the transition ineligible for reclamation, not a reason
to drop evidence or invent a retention reason. Partition rows need the applied delete
proof, not a storage entry. Only existing kinds partition, virtual_disk and installer_media
are reclaimed; unknown kinds retain their prior behavior rather than becoming general GC.

Keep this comparison aligned with `owned_items` projection: disk identity is vios_uuid,
vg_uuid and disk_name; media identity is vios_uuid and media_name. UUIDs are normalized
as the producer already does; name comparison remains exact. Extra readable identity
fields are ignored under ADR0323, not newly exposed. No producer-operation join is used;
rows whose producing operation expired remain reclaimable in the same partition scope.
Use bound SQL parameters and delete only the selected kind/system/partition rows in the
same transaction as `set_state`. Preserve existing per-item removal after applied physical
storage deletes; it remains necessary during pauses and resumes before terminalization.

For unreadable rows, add optional `ledger_identity` containing the original identity
object, including unknown keys and non-string JSON values without coercion. Widen only
the retained-entry value annotation to admit that object and propagate it through cleanup,
body serialization and result reconstruction. Existing readable, deleted and pending shapes
and legacy results remain accepted. Preserve each unreadable occurrence and avoid normal
item-key deduplication/filtering of those entries. Readable reconstruction filtering in
`same_item` and its `_storage` consumer uses the complete canonical identity: disk
(kind, normalized vios_uuid, normalized vg_uuid, exact disk_name), media
(kind, normalized vios_uuid, exact media_name). A deleted disk in one volume group must
not hide a retained namesake in another group on the same VIOS. This corrects an existing
direct-consumer defect required by C5 without changing result fields or adding a second
identity convention. No raw identity is added to warnings.
The existing MAX_RESULT_BYTES check applies after this addition: no truncation or larger
bound. An oversized successful body result pauses; an oversized carried failure result
uses existing failed/no-new-result behavior. Neither path reclaims ledger rows.

## Alternatives

- Recommended: extend the existing terminal transaction with explicit proof and coverage
  checks. It preserves atomicity without another lifecycle or persisted marker.
- Reclaim in the cleanup body: rejected because its result has not yet been durably saved.
- Leave unreadable rows indefinitely: operator declined this limited closure in favor of
  preserving original available identity in the result; no exception is silently added.

## Failure model

Actors and deployments:
- Local CLI/MCP operation workers and status readers sharing the private SQLite store.
- Supported Python runtimes; deterministic tests mock the external HMC boundary only.
Invariants and assets at stake:
- Retained storage provenance, recoverable operation state, capacity, and other partitions.
- Fresh matching delete proof; lossless unreadable identities; atomic terminal visibility.
Accepted failure classes:
- Approved ineligible cases retain rows and their capacity cost; safety precedes reclamation.
- Unknown ledger kinds remain unchanged; they are not the existing resource kinds of #1890.
- Result expiry after 30 days is existing policy, not permanent archival.
Covered elsewhere:
- Physical cleanup and longer archival: operator. HMC write authorization: existing tools.
- Corrupt/unreadable private stores: existing store refusal and operator recovery, ADR0190.

## Threat model

Boundary inventory:
- Added: unreadable ledger identity appears inside an authenticated operation result.
- Existing: persisted operation/effect/result data controls local ledger deletion.
Actor model:
- Trust the local credential holder and existing journal producers, not HMC text as code.
- A local adversary with write access to the private store is outside ADR0190's trust model.
Control per boundary:
- Existing private-store checks, operation status authorization, bounded JSON serialization;
  identity stays structured JSON, with no evaluation, warning interpolation or public posting.
- Eligibility and row-coverage checks plus parameterized SQL bound the deletion target;
  BEGIN IMMEDIATE serializes writers. Existing execution ownership excludes competing workers.
Explicitly out of scope:
- New authorization, storage deletion, historical GC or protection from an owner editing SQLite.

## Validation

Use the transient plan's focused-test inventory for C1-C5. Cover real SQLite commit/rollback
and second-connection visibility with deterministic synchronization; no sleep-based races.
Inject a SQLite delete trigger failure and verify all three persisted surfaces roll back.
Prove tests bite by disabling the scoped delete and by dropping ledger_identity in controlled
faults, observing failures, then restoring source. Cover duplicate rows, other systems and
partitions, mixed-case UUIDs, shared retained resources, pruned producers and capacity reuse.
Exercise the real engine/decommission body with only HMC calls mocked, including HTTP412,
resume, oversize fresh results and repeat result reconstruction. Readable results stay stable.
Run focused tests, lint/type checks, generated-doc regeneration if its schema output changes,
then final `just verify` and `uv run --no-sync prek run --all-files`. Hosted matrix supplies
Python3.12-3.14 evidence. No live-system mutations are authorized or needed.
