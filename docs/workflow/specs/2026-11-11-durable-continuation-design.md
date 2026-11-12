# Durable adapter continuation arguments

## Problem and ownership

Power, reconfiguration and provisioning repeat the same continuation record refusal
and supplied-over-recorded merge. Own that pure operation in
`server_tools/lpar/continuation.py`, imported directly by all three adapters.
Each adapter retains its store lookup, filtering, serialization, required fields,
defaults, existence flag and authorization. See ADR 0359.

## Global Constraints

Python 3.11–3.14 on amd64 CI; local Linux amd64 Python 3.11.15.
No new dependencies or toolchain floor changes.
No live hardware operations, pushes, PRs, or shared/external writes.
Use the existing sibling worktree and conventional commits; plans remain untracked.

## Success

1. `continuation_arguments(recorded, given, request_id)` raises the existing
   `OperationRefused` code/message when recorded is None. An empty mapping is present.
   Otherwise return a new dictionary with supplied fields overriding recorded fields;
   preserve retained value identity and never mutate either input.
2. All three continuation branches call it after their current store lookup.
   Power/reconfigure still avoid that lookup for new requests; provision still reads
   once for new requests to compute existence. Provision alone keeps JSON conversion.
3. Defaults, required fields, writer-window checks, delegated authorization, effective
   authorization arguments and supplied-only conflict checking remain unchanged.
   No engine change, handler framework, compatibility export or package relocation.
4. Affected behavioral tests and hooks pass. All 207 tool schemas and the six-name
   facade remain unchanged. Resolve only the demonstrated repeated continuation seam.

## Failure model

- Actors/deployment: authenticated tool callers and offline application test clients.
- Assets/invariants: recorded targets, delegated authority and refusal before submission.
- Accepted failures: existing store-read errors propagate; no retry or fallback added.
- Covered elsewhere: engine lookup validates request ids and agent/tool binding;
  adapter authorization and engine conflict checks retain their current responsibilities.

## Threat model

No boundary is added or widened. Supplied arguments remain untrusted. The helper
cannot read a store or submit work; callers retain scoped lookup. A missing record
must still refuse before authorization/submission. No new permission, persisted
format, credential handling or concurrency guarantee is introduced.

## Validation

Focused tests distinguish None/empty records, preserve overlay/identity/nonmutation,
and assert exact refusal bytes. Existing application tests exercise recorded-target
reauthorization and conflict handling through the real engine/store with fake HMC IO.
Check each caller's lookup timing, defaults and provisioning serialization.
Run affected tests, lint/format/type checks and pinned hooks before committing.
Final whole-branch `just verify` and pinned all-file hooks remain completion gates.
