# ADR 0390: Confirm abandoned operations before LPAR cleanup

## Status

Accepted (2026-11-16).

## Context

Issue [#1918](https://github.com/randomparity/hmcpctl/issues/1918) identifies an
abandon-versus-execution race in the shared ST41/ST72 teardown. An abandon answer
alone does not establish that the operation stopped.

## Decision

Read the matching operation after each abandon. Only a successful terminal
confirmation permits resource cleanup. Otherwise fail cleanup, retain the run's
resources, and emit manual recovery evidence naming the request ID.

## Consequences

Unreadable status requires operator recovery even if abandon actually completed.
One immediate read keeps teardown bounded; the operation tool remains the owner
of cancellation behavior. Remaining tracked requests still get abandon attempts.

## Considered & rejected

- **Trust the abandon response.** judgment: it cannot resolve the execution race
  identified by the issue's completion criterion.
- **Continue deleting while marking cleanup failed.** judgment: mutating resources
  while operation execution may continue creates avoidable recovery races.
- **Poll until terminal.** judgment: an added retry policy is unnecessary to meet
  the requested status check and manual-recovery behavior.
