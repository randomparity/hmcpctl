# ADR 0239: Normalized MCP job inspection output

## Status
Accepted (2026-10-18), operator-approved output contract correction.
Refines ADR0093 amendment474's hmc_get_job presentation carve-out only;
its polling, handle persistence and authorization rules remain unchanged.

## Context
The operation retires a stale polling link, but hmc_get_job returns only its raw
job field. The corrected handle is lost while the raw mapping may echo the stalelink.

## Decision
Return existing JobOutcome from hmc_get_job, retaining raw data in job and exposing
the operation's corrected job_href. Missing reads use foundfalse/jobnull.
Migrate repository consumers and current schemas/docs directly, no outputaliases.

## Consequences
MCP callers of the pre-release rawdict/null shape must migrate. Addressing,
disappearance confirmation, URI/authorization policy and rawentry data do not change.

## Considered & rejected
- **Retain raw-only output.** judgment: forces MCP consumers to reconstruct a
  polling handle whose corrected version already exists in the operation.
- **Edit the raw entry's link.** judgment: destroys original HMC evidence instead
  of preserving normalized metadata alongside it.
