# Standalone LPAR post-activation affinity assessment

Issue: #319
Decision: [ADR 0090](../../adr/0090-standalone-lpar-affinity-assessment.md)

## Contract

`hmc_power_on_lpar` keeps one result object and its existing power fields. A new, always-present
`affinity_assessment` companion reports `measured`, `status`, `reason`, and the optional assessment
evidence. With no assessment request, it is `measured=false` and `status=skipped`; no additional
HMC or SSH traffic occurs.

The existing `ProvisionAffinityAssessment` request contract is shared with standalone activation.
It binds captured evidence to the requested managed-system and LPAR names and carries the explicit
`warn` or `fail` response. Validation happens before activation.

## Activation sequencing

Assessment runs only when this call submits PowerOn with `wait=true` and observes a successful
terminal job status. An already-running guard result is not an activation observed by this call,
so assessment is skipped. `force=true` still bypasses that guard. A non-waiting call submits and
returns without assessment. A timeout or unsuccessful terminal status returns the power result and
an unavailable assessment without measuring.

After successful activation, the operation reads current and predicted affinity scores and current
minimum-affinity policy, then applies the #317 assessment contract. Unsupported capability or
missing evidence produces `unavailable`. A clean classification produces `passed`. An adverse
classification produces `warned` for `response=warn` and `failed` for `response=fail`. Fail-closed
also maps measurement/read unavailability to `failed`; warning intent preserves `unavailable`.

## Error and compatibility behavior

Assessment is opt-in. Existing defaults, job payloads, already-running messages, timeout values,
polling, and force behavior remain unchanged. Assessment errors are represented in the companion
field instead of replacing or hiding the completed power result.

## Tests

Contract tests cover the unchanged default path and schema plus already-running, non-waiting,
timeout, unsupported, warning, and fail-closed outcomes. Focused operation tests verify that score
and policy reads occur only after an observed successful activation.
