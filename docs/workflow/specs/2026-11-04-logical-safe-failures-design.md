# Safe HMC failures in logical-operation warnings

## Problem and scope

Issue #1885 traces durable warning disclosure to `_failure` in the logical engine.
It formats exception text for body and reconciliation failures; HMCError text can
contain remote diagnostics. Retain this shared owner and its existing callers.
[ADR 0333](../../adr/0333-logical-operation-safe-hmc-failures.md) records the decision.

## Success

- C1: HMCError instances reaching `_failure`, including subclasses, produce their
  concrete exception type plus HTTP status, with an explicit unavailable status
  when absent. Their free message and body do not enter these warnings.
- C2: Curated OperationFailed text and existing non-HMC exception rendering remain
  unchanged. Warning bounds, effect status and operation outcomes remain unchanged.
- C3: Both body and reconciliation failures persist this safe warning, which a later
  operation-status read returns. Regression tests fail under the old renderer.

## Design

Import HMCError from the shared errors module and special-case it in `_failure`.
Use `<type>: HTTP <status>` or `<type>: HTTP status unavailable`; never invoke the
HMCError string renderer. No code extraction is needed: HMCError carries no
classified code field and the issue makes codes optional. `_warning` still owns
length bounds. Body/reconciliation call sites and store schemas remain unchanged.
No ownership transition or compatibility wrapper is needed.

## Failure model

- Actors and deployments: local operators and agents using logical tools and the
  durable operation store; unit tests and CI on supported Python versions.
- Invariants and assets: remote diagnostic confidentiality at the exception-to-warning
  boundary; preserved curated failure advice and durable operation state.
- Accepted failure classes: non-HMC exception text and classifier-returned reasons
  retain their existing producer responsibility; previously stored warnings remain.
- Covered elsewhere: transport body sanitization (#1884); broad exception taxonomy
  and historical-store migration (operator, outside #1885); store durability and
  authorization (existing engine/store tests).

## Threat model

- Boundary inventory: existing remote HMC diagnostic → engine warning → SQLite/status
  response; no new or widened entry point.
- Actor model: HMC diagnostics are untrusted for disclosure; local exception classes
  and integer/None status metadata are trusted application objects.
- Control per boundary: `_failure` selects type/status without reading text/body;
  existing `_warning` caps stored strings. Status read returns the persisted value.
- Out of scope: transport error text (#1884), non-HMC producers, existing stored rows,
  and store migration, as approved in WORK:SCOPE.

## Validation

Focused engine tests cover message/body sentinels in classifier/body HMCError,
subclass/no-status handling and curated OperationFailed preservation. Assert exact
warnings and a subsequent store.operation_status result. Run the tests first with
the old renderer to observe disclosure failures, then with the fix. Run focused
pytest without aggregate coverage, ruff and typecheck for iteration; final `just
verify` and pinned `prek run --all-files` cover the assembled branch. No live HMC.
