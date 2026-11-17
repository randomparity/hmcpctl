# Configuration isolation guidance

## Problem

Issue #1910 requests a current field inventory, a valid environment probe, and
accurate fixture exceptions. The guide already avoids a field count and supplies
the ISO build pair, but still claims ambient exports reveal per-test leaks.
The suite's autouse fixture now clears config-field variables and `HMC_PROFILE`
before test bodies, so that interpretation is stale.

## Scope

One documentation unit updates the environment diagnostic in
`docs/workflow/agent-operations.md`. Keep `HMCConfig.model_fields` as the field
inventory, valid non-default inputs, and the coupled ISO build settings. Describe
case-insensitive clearing, TLS verification pinned true, the power guard unset,
and the additional agent/schema/ISO fixture clearings. Bound the ambient recipe
to existing config tests and explain the evidence it supplies.

The guide retains ownership; no caller migration or ownership transition is
needed. Configuration behavior and suite isolation fixtures remain excluded,
owned by the existing HMCConfig/test-isolation contracts. No deferrals.

### Failure model

- Actors and deployments: local maintainers running the documented offline
  diagnostic in a bootstrapped checkout on a supported Python runtime.
- Invariants and assets: diagnostic inputs construct valid settings; fixture
  protections stay intact; a green bounded probe is described within its limits.
- Accepted failure classes: unselected tests, collection-time environment reads,
  and future fields are not certified by this bounded run; maintainers consult
  `model_fields` and extend valid inputs when settings change.
- Covered elsewhere: config behavior and isolation by existing configuration
  tests/fixtures; branch readiness by canonical guardrails and hosted CI.

## Success

The guide explains why ambient exports do not reach suite test bodies, preserves
the valid ISO pair and fixture exceptions, avoids a hard-coded field count, and
provides an offline bounded command with an honest interpretation of red/green.
Run that command once and report its actual result; run required branch guards.

## Validation

- Contract: diagnostic explanation and recipe. Mode: task-test-not-applicable —
  prose correctness requires comparison with source; no executable consumer
  asserts its wording, and a prose snapshot would not establish truth.
- Compare inputs with `HMCConfig.model_fields` and fixture clearing; execute the
  documented config-test probe once with `--no-cov`, reporting failures or none.
- Run `just doc-links`, `just static`, then final-candidate `just verify` and
  `uv run --no-sync prek run --all-files`; hosted CI covers its declared matrix.
