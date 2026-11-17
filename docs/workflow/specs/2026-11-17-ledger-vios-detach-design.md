# Installer-media detach with a VIOS-scoped grant

Issue #1904 changes the grant accepted by `hmc_reconfigure_lpar` for
`installer_media: detach`. Its operator contract is recorded in
[ADR 0392](../../adr/0392-authorize-detach-on-the-provisioned-vios.md).

## Admission

The handler applies the writer window and delegated-tool availability checks. For a new
request the unmount dispatch gate runs target resolution only upon reaching its first
connection-eligible grant. The worker's existing ownership-aware resolution identifies the physical system
and LPAR on the admitted connection and host. A read of the provision ledger requires
exactly one partition owner and one media row with a valid VIOS UUID and usable medium
name. Authorization submits the caller's original system/LPAR selectors and the ledger
VIOS spelling to the existing specialist gate. A single grant must cover the whole tuple.
Mixed patches authorize each other specialist with its own existing targets.

For a new detach, write its authorized system UUID, partition UUID, VIOS UUID and medium
name as an already-applied admission effect in the transaction that inserts the operation.
The existing effect journal retains this evidence through an interruption before the
worker starts and through plans that have no media write. The admission effect has the
stable key `admitted_media` and kind `lpar.installer_media_binding`. It makes no HMC call.

For a matching existing request, check the tool, host, connection and original arguments.
Use its guarded UUIDs once assigned, including after a rename. For resume, check current
ownership and the provision ledger. Compare any applied plan's partition and planned
medium with this binding before starting a worker. A plan recording an already-unmapped
medium has no detach step. When the worker has yet to assign its guard, use the admitted UUIDs. For historical
records without the admission effect, executable resume requires both a guarded partition
and an applied plan recording the medium. A mismatch or missing original binding refuses
execution. When journal-only authorization succeeds, the operator can abandon and submit
a fresh request. Conflicting local identities also prevent abandon.

## Execution and recovery

Pass the admitted physical tuple and medium to the worker. Validate a fresh plan against
that binding before recording intent for any patch mutation. A recorded plan must agree
with the guard and the authorized medium. Check the binding before resume reconciliation,
including classifiers for other fields in the mixed patch. Retain the physical target when
a rename in the same operation has changed its original name. Reconcile using the same
host-bound client and preserve the existing journal and writer semantics.

Replay of an existing request and abandon are journal-only. A recorded plan can supply
the VIOS for a scoped grant without opening an HMC client. Compare all available local
evidence from the admission effect, guard, applied plan and ledger. A matching ledger
can supply an absent medium when the original guard exists. A missing ledger may leave
a valid recorded binding as the sole source for these journal-only paths. Only an
all-targets unmount grant covers a missing VIOS selector when the original evidence is
absent. Conflicting or malformed evidence refuses. In the admission transaction bind
the exact observed operation ID and the existing request checks. For journal-only
authority, refuse a missing or replaced record or any state transition that starts a
worker. Terminal resume is a journal return under the same checks.

## Failure model

- Actors and deployments — Operators call the composed MCP server with a configured HMC
  connection and the local SQLite operation store. A second caller can repeat or resume.
- Invariants and assets — Partition settings, the VIOS optical mapping, the medium and the
  journal stay within the admitted target. No mixed-patch mutation precedes validation.
- Accepted failures — Unavailable HMC or ledger blocks executable detach. An absent mapping
  can be a successful no-op under ADR 0321. Other writers require the existing writer window.
- Covered elsewhere — Native optical cleanup and evidence collection belong to separately
  authorized live work. Provision preview and CLI parity belong to #1902 and #1903.

## Threat model

- Added boundary — The handler reads physical target and local media identity before
  admission. Use the admitted connection and host, validate the record and redact
  pre-authorization failures that could disclose another target's identity.
- Widened boundary — A target-restricted caller can detach the provisioned medium if the
  existing single grant covers the original selectors and VIOS UUID on that connection.
  Callers control selectors, patch, request ID and continuation. They cannot supply the
  ledger identity or join grants. Trust the configured HMC, local store and access policy.
- Worker boundary — Reconcile and write only after checking the admitted binding. Local
  journal-only access cannot schedule an effect. A replaced operation cannot inherit it.
- Out of scope — Additional VIOS alias grants and compromise of the trusted HMC or
  operation-store owner. Existing policy and filesystem controls own those boundaries.

## Validation

Use a real composed MCP application with synthetic HMC and SQLite fixtures. Verify exact
and all-target grants, wrong and split grants, mixed patches, missing or divergent identity,
changed connection/host, interruption before first plan, journal replay/abandon and stale
record IDs. Denied connections perform no media lookup. Pre-authorization refusals omit
physical identifiers and ledger counts. Exercise journal-only disagreement between two usable
local sources as well as genuinely absent evidence.
Observe the journal and fake writes as well as the returned decision. Run the offline
repository verification and hooks. Live evidence is outside this issue's approval.
