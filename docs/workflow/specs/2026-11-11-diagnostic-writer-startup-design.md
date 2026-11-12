# Diagnostic writer startup failure

## Problem and authority

The final review round reproduced submit raising when Thread.start fails, then
close raising while joining the installed unstarted thread. The user authorizes
fixing this round and stopping. ADR0043 already selects bounded, counted loss
instead of diagnostics changing operation behavior; no new interface is needed.

## Design

While holding the existing condition, catch ordinary Exception only around
_start_writer in submit. Count the current line as dropped and return before
queue insertion/pending increments. Construct the writer locally and publish
_writer only after start succeeds. Existing locking serializes start, submit
and close; no additional lock, retry timer, logging path or abstraction.

## Success

Thread construction/start failures at the named boundary do not escape submit
or write_diagnostic, poison the writer field, or create pending work. Close is
safe after that failure. A later submission may create a writer and reports the
existing drop marker before the retained line. Existing bounded queue/drain,
logger behavior and process-control BaseException propagation are preserved.

## Failure model

- Actors/deployments: configured Python/CLI/MCP callers submitting valid strings
  to the existing sink, where local thread resources can temporarily fail.
- Assets/invariants: caller outcome, counted loss, one published writer, bounded
  queue and existing shutdown; the current condition owns publication ordering.
- Accepted: persistent resource failure loses diagnostic lines; the counter can
  be reported only if a later writer/destination succeeds (ADR0043). Process
  interruption and catastrophic failure outside writer startup are unchanged.
- Covered elsewhere: sink allocation, queue/destination behavior and audit record
  formatting retain their existing owners and tests; no global delivery promise.

## Validation

Eight focused cases use the real sink, Thread construction/start fault injection,
public/direct submission, then recovery or close. Observe pre-fix failures,
including unstarted-thread close; after repair assert counted drop/no pending
work and exact marker/retained-line behavior. Existing audit, authorization and
served-connection suites protect downstream behavior. Final branch gates and
whole-delta review remain caller-owned closure work, not another cleanup round.
