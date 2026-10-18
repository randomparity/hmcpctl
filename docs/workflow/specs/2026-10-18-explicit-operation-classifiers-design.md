# Explicit logical-operation classifiers

## Problem
Importing LPAR power mutates the engine's global classifier registry; reloading
that provider raises a duplicate-registration error. Reconciliation depends on
which providers were imported. The operator approved explicit classifier injection.

## Scope
Add keyword-only classifiers: Mapping[str, Classifier] | None = None to engine.submit.
Snapshot the supplied mapping into a new dict once for each submission and pass it
through worker start, worker entry, async execution, and OperationContext.load to
that context. None supplies no classifiers. Context reconciliation looks up its
own mapping. Remove engine.CLASSIFIERS and register_classifier entirely.
LPAR power exposes POWER_CLASSIFIERS containing its existing start/stop checks.
The MCP power adapter and direct callers submit that map alongside power_body.
Restart still has no classifier, so uncertain restarts require attention.
No compatibility registry or automatic provider import remains. Preserve the
classifier implementations, request binding, thread lifecycle, journal, warning
bounds, authorization checks, accepted continuations and persisted schemas.
Root owns the migration; no dependencies, live writes or stable facade changes.

## Success
Provider import/reload has no registry side effects. Two calls can use distinct
checks for the same effect kind; later caller-map mutation cannot change a running
context. Missing or failed classifiers retain current needs_attention behavior.
All engine and power behavior, including resume without duplicate writes, remains.

## Failure model
Actors: MCP callers, library callers, worker threads and durable SQLite recovery.
Assets: no unproven effect replay, correct partition guards and private warnings.
Mappings belong to a submission, including resumes; they are not serialized.
Only the existing authorized tool chooses its providers. An absent provider fails
closed through the existing missing-live-check result. Accepted failure: approved
internal registration API is removed without compatibility exports.

## Validation
focused-test: provider reload in a fresh subprocess fails before this change.
New engine tests provide different maps for the same kind and mutate the caller's
map while reconciliation is blocked; preserve no-rewrite and snapshot behavior.
Prove tests red before implementation; existing engine/power and MCP power tests
cover crash windows, refused authorization, restart uncertainty and warning bounds.
Migrate test setup from the removed registry to explicit per-call mappings.
Run focused pytest, lint, format, type checking, tool-docs-check and MCP smoke.
