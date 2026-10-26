# ADR 0298: LPAR sharing and weight current-configuration verification

## Status

Accepted 2026-10-25 for #1780, scope q1780-13ff64e1.

## Context

The existing LparResources → partition_updates → whole-partition If-Match owner
already writes six sharing modes but cannot set arbitrary uncapped weight.
Known successful HTTP2xx acknowledges submission even if parsing fails; its
body does not prove independent configured/Current* agreement.
The combined modifier can rename before discovering invalid resource context.

## Decision

Extend those owners with update-only strict integer uncapped_weight (0–255).
Retain the exact no-weight asymmetric alias resolution; reject contradictory
explicit selectors when weight is present. Independently enforce processor
context/subsets, including fresh ETag-bound revalidation after preflight.
Permit only the source-backed missing-weight insertion immediately after the
same configured shared container's SharedProcessorPoolID; retain namespace,
attributes, ordering and refusal of ambiguous structure.

Sharing/weight completion means independently verified current configuration:
requested configured fields match, configured and Current* mode agree, and
applicable configured/current weights agree (and equal an explicit target).
Runtime evidence is separate and cannot turn unavailable observation into guest
success. Propagate incomplete outcomes through combined modify, CLI and MCP.
No profile apply, activation, replay or automatic compensation is introduced.

## Consequences

A valid HTTP response can return an incomplete workflow; asynchronous convergence
may need later operator inspection. Runtime mismatch/unavailability is visible
beside configuration completion. Capping/uncapping side effects are observed,
not replaced with a guessed zero/default/previous weight. Shared creation users
reject the new update-only field before effects; planning rejects it before reads.
ST59 is a bounded inactive fixture procedure; its existence is not hardware evidence or a state matrix.

## Considered & rejected

- **Do nothing.** judgment: leaves the requested arbitrary weight gap unresolved.
- **Reject conflicting aliases even without weight.** verified: at base
  `git show 8167873b:tests/lpar/test_lpar_rmw.py` records uncapped=True
  overriding capped at319–336; `git show 8167873b:src/hmcpctl/documents/lpar.py`
  gives the reverse explicit-mode precedence at335–341.
- **Add a second native sharing setter.** verified: IBM immutable 8b10a3f
  hmc_rest_client.py:1758–1766 demonstrates the narrow current-document insertion
  path. Judgment: a parallel setter duplicates the existing owner.
- **Treat POST/configured echo as completion.** verified: captured processor
  subtree in tests/fixtures/live/rest-lpar-create-ok.json at base8167873b has configured/current
  uncapped/128 and runtime Unknown/0, so these observations are not equivalent.
- **Require universal runtime or RMC/state admission.** judgment: changes bounded
  configuration completion into a broader guest-effect contract and unowned policy.
