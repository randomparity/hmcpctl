# Validate installer-media detach limits before HMC access

Issue #2028 covers the installer-media path of `hmc_reconfigure_lpar`. A fresh
detach can reach the HMC to resolve its target before logical submission checks
the wait and canonical request size. The caller can therefore receive a target
refusal for an input error.

## Validation and admission

The logical engine owns wait validation and canonical serialization. Factor a
side-effect-free function that checks the wait, serializes the request using the
existing byte bound, and returns the canonical text and digest. Submission uses
that result after its existing request ID and continuation checks.

The reconfigure handler invokes the same function inside the unmount specialist's
connection-eligible target callback, before the target resolution error handler.
This preserves the connection gate and allows both local limit errors to reach the
caller. The callback validates the original candidate arguments, including on a
retry. Submission independently enforces admission against its current journal
transaction and starts workers under its existing lock. These checks use the
existing request format, authorization policy and journal.

## Failure model

An authorized caller can supply an out-of-range wait or an oversized selector.
Neither input may open an HMC client. A target lookup can also fail, and a peer
can admit the same request during authorization. Valid inputs still follow the
existing redacted resolution error path and conditional admission retry.

## Threat model

The caller controls tool arguments. The specialist's connection gate separates
these arguments from HMC access. Limit errors may describe the caller's wait or
serialized byte count. The existing target-resolution boundary protects
recorded identities and remote details. This change introduces no state or I/O
and grants no additional target or worker authority.

## Verification

Exercise the composed MCP tool with authorized detach arguments, an invalid wait,
and a canonical request over the byte bound. Assert the specific local refusal
and zero calls to the HMC client factory for each case. Check that an invalid
wait on a denied specialist connection still returns the connection denial. Existing valid detach,
denied grant, replay, concurrency and worker tests cover downstream behavior.
