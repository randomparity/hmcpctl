# System-profile REST TLS observation cutoff

## Problem

The private `_rest` helper converts `HMCTLSFingerprintError` into ordinary
uncertainty. Its create/update/activate callers then reconcile through more
REST and SSH reads. An offline public-create probe observed eleven such calls.
ADR 0224 requires a pin mismatch to abort; the existing phase contract retains
known post-attempt job facts and returns uncertainty rather than replaying.

## Scope and design

Keep the existing `_RestInterrupted` abort channel and its cancellation default.
Allow it to carry the existing safe `TLS_OBSERVATION_ABORTED` diagnostic. Catch
fingerprint failure before `_rest`'s generic HMC failure handler and raise this
carrier with the current job and return-code facts. The two outer coordinators
already stop reconciliation when that carrier escapes. No public signature,
result field, backend route, retry policy or other module changes.

## Success

- The three REST mutation APIs stop operation-level reconciliation after TLS
  failure at submission or job wait, with no second submission.
- Submission-time failure retains no invented job; wait-time failure retains
  the known job identity. Both return uncertain/unverified with the safe TLS note.
- Existing deadline/cancellation behavior and ordinary failure classification
  remain intact. Pre-attempt errors still propagate.

## Failure model

- Actors/deployments: configured local/MCP callers and a remote HMC whose REST
  identity may fail during an admitted job; offline boundary fakes prove flow.
- Assets/invariants: pinned identity, known partial effects/job facts and no
  operation reconciliation after the named fatal mismatch.
- Accepted classes: a previously submitted job may continue remotely; operators
  reconcile it. No live qualification or automatic rollback/replay is promised.
- Covered elsewhere: client TLS detection/cleanup by ADR 0224 and client tests;
  admission/ownership and full API outcomes by the existing system-profile suite.

## Threat model

- Existing boundary: configured pin versus REST peer identity; no new boundary.
- Untrusted party: a peer presenting a mismatching certificate; operator trust
  configuration and existing SSH identity policy stay unchanged.
- Control: preserve the detected exception as a terminal internal abort, retain
  known facts and emit the existing safe diagnostic without provider details.
- Outside this fix: new certificate policy, transport cleanup and cross-invocation
  circuit breaking remain existing owners; no global network-silence claim.

## Validation

The single plan unit expands the existing public interruption regression to
six TLS cases and six deadline/cancellation controls, with counters over the
fixture's REST/SSH boundaries and known-job assertions. Existing profile,
MCP/CLI and request tests verify compatibility; required hooks stay mandatory.
