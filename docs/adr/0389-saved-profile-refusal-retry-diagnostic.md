# ADR 0389: Explain saved-profile apply refusals before resume

## Status

Accepted — 2026-11-17. Implements [#1917](https://github.com/randomparity/hmcpctl/issues/1917).

## Context

[ADR 0331](0331-provision-applies-its-saved-profile-when-it-does-not-power-on.md)
applies the saved profile without activating the partition. A refused apply leaves
a paused operation. Its resume classifier can observe Not Activated with another
last-activated profile and deliberately retry the apply. The existing CLI error
names the failure but does not explain that retry.

## Decision

At provisioning's saved-profile apply boundary, enrich an `HMCCLIError` only when
its existing `remote_nonzero_completed` evidence proves a completed nonzero remote
command. Prefix its message with HMC refusal and explain that resuming deliberately
retries the same apply when the partition remains Not Activated with a different
last-activated profile. Ask the caller to inspect the refusal before resuming.

Keep the original exception instance, attributes, subtype, cause, and traceback;
change only its displayed message and re-raise. Put the guidance before remote
detail so the workflow's existing 512-character truncation retains it. Errors
without completed-remote evidence keep their existing diagnostic. Preserve ADR
0331's classifier and the operation journal schema.

## Consequences

A deterministic refusal can still repeat after an explicit resume, with the retry
explained in both calls. Transport failure is not misreported as HMC refusal.
No new refusal state or terminal transition is required.

## Considered & rejected

- **Persist refusal and forbid resume.** judgment: an extra journal contract and
  classification path are unnecessary for the diagnostic alternative #1917 permits.
- **Wrap every CLI failure as refusal.** verified: `src/hmcpctl/ssh/transport.py`
  at main `a6bd56d5` produces `HMCCLIError` for both transport failure and completed
  nonzero commands; `remote_nonzero_completed` distinguishes them.
- **Append retry advice after remote detail.** verified: `_Run._leg` at main
  `a6bd56d5` truncates error summaries to 512 characters, which can omit an appendix.
