# Separate LPAR activation and shutdown operations

Scope: user approved all three changes in response to the next-boundaries question.
Replace the pre-release combined power_lpar API and CLI boolean dispatcher.
Owner: root. Complexity M, design denominator250, full-spec (API/auth boundary).

## Outcome and contracts
Provide submit_power_on_lpar(hmc,lpar,*,system_name_or_uuid=None,...activation options)
and power_off_lpar(hmc,lpar,*,system_name_or_uuid=None,...shutdown options), both
returning the existing LparPowerResult. Preserve the existing power_on_lpar
wrapper and its LparPowerOnOutcome result, activation verification and optional
affinity assessment; it delegates to submit_power_on_lpar. Raw activation
callers retain their current result and wait behavior through submit_power_on_lpar.
Both retain wait/timeout/poll interval/ownership override; force is activation-only.
Activation retains boot mode, partition profile, operation type, keylock. Shutdown
retains immediate, restart, operation, allow-dump-restart. Reject cross-arm arguments
at the call boundary. Remove power_lpar rather than add compatibility aliases.

Preserve the existing validation-before-audited-override order, opt-in ownership
guard, scoped resolution, already-active behavior, partition profile containment
and warnings, shutdown refusal policy, submitted XML/path, waiting and result model.
Reuse existing focused helpers; a shared private target-resolution helper may hold
the identical authorize/resolve branch. Do not recreate a boolean-union dispatcher.
CLI commands retain all names/options/prompts/rendering and each calls its operation;
share confirmation/rendering only where it reduces duplicated policy.
MCP power-on/off handlers and provisioning/affinity callers migrate directly. The
separate durable hmc_power_lpar engine/tool is unchanged. Six-name facade unchanged.

## Failure model
Invalid activation or shutdown options fail before ownership reads/audits and jobs.
Unresolved/unauthorized targets submit nothing. Active-state shortcuts and failed
profile reads remain unchanged. Wait failure propagation and shutdown failures
retain existing behavior. No new persistence, remote calls or live testing.

## Validation inventory and execution
Migrate repository callers and meaningful tests, including dynamic on/off test
parameterization to explicit operation choices. Replace tests of ignored irrelevant
options with rejection tests; retain every policy/job/ownership assertion. Verify
call-boundary rejection is red before implementation. Preserve source citation and
generated help references to current operation names without rewriting historical
ADR decisions. Run focused lpar power/ownership/provision/affinity, CLI commands,
selector signatures, API/introspection, metadata/citation tests; lint/types/format.
Regenerate documents if their freshness gate requires it. Commit one logical task.
Full verification and pinned hooks run after assembled changes.

Excluded: CLI/MCP wire changes, authorization redesign, model/schema changes, live
HMC mutation. These exclusions preserve the user's approved behavior constraints.
