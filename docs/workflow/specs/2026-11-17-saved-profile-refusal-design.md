# Saved-profile refusal diagnostics

## Problem and authority

[#1917](https://github.com/randomparity/hmcpctl/issues/1917) permits either refusing
resume after a saved-profile apply refusal or explaining that resume retries it.
The campaign selected the diagnostic alternative. Frozen scope token:
`q1917-38b693c0`; approved exclusion/owner set: EMPTY.

## Design

Keep `_Run._write_apply` as the owner of provisioning's apply-specific diagnostic.
No ownership transition or caller migration is needed. Catch only `HMCCLIError`
around `apply_lpar_profile_via_cli`. With `remote_nonzero_completed=True`, prefix
its message: “HMC refused the saved-profile apply; resuming deliberately retries
the same apply if the partition remains Not Activated with a different
last-activated profile. Inspect the refusal before resuming.” Retain the original
remote error text after that prefix. Preserve exception identity, subtype,
metadata, cause, and traceback using `args` plus a bare re-raise. Without completed
remote evidence, bare re-raise unchanged. The existing guards and post-apply proof
remain outside the catch.

[ADR 0389](../../adr/0389-saved-profile-refusal-retry-diagnostic.md) records this
choice. No new interfaces, journal fields, dependencies, or effect-state changes.
The resume classifier continues to retry the same saved profile after observing
Not Activated with a different last-activated profile.

## Success

- C1: trace refusal, effect recording, and resume through the actual tool path.
- C2: both initial and resumed completed refusals explain refusal and deliberate
  retry in the caller-visible, bounded workflow step, including long remote text.
- C3: a refusing fake receives two calls for the same profile in one operation;
  previously applied effects are reconciled rather than recreated.
- C4: successful apply completes, and errors without completed-remote evidence
  retain their diagnostic and exception semantics.

## Failure model

- **Actors and deployments:** authenticated operators call provisioning locally
  or through MCP; offline CI exercises mocked HMC boundaries.
- **Invariants and assets:** saved profile identity, journal integrity, unchanged
  reconciliation, honest completion/refusal evidence, readable bounded diagnostic.
- **Accepted failure classes:** a deterministic refusal repeats after explicit
  resume (the selected diagnostic alternative); remote detail beyond the existing
  512-character workflow cap is truncated after the required retry guidance.
- **Covered elsewhere:** ADR 0331 and existing tests own apply readback and drift
  classification; SSH transport owns command completion evidence and redaction.

## Global Constraints

Python >=3.11. CI targets Linux amd64 with Python 3.11–3.15; local verification
uses Darwin arm64 with Python 3.11.15. No dependencies change. Use `uv run --no-sync`.
No live HMC access. Existing public facade and persisted operation models stay intact.

## Validation

One tightly coupled implementation unit: add refusal/resume regression and exception
preservation cases in `tests/lpar/test_provision_tool.py`, then narrow the apply catch.
The existing successful/no-apply and unproven-apply cases remain behavior checks.
Run focused red/green tests, deliberately remove the enrichment and observe the
regression fail, restore it, then run `just verify` and
`uv run --no-sync prek run --all-files` before push. CI supplies the other Python legs.
