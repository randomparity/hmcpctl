# Power11 Minimum-Affinity Policy Write Design

Issue: #316  
Decision: [ADR 0087](../../adr/0087-power11-minimum-affinity-policy-write.md)

## Outcome and boundaries

Expose one explicitly authorized LPAR mutation that accepts a complete policy: score 0–100 and
action `none`, `warn`, or `fail`. Omitted provisioning policy preserves existing behavior. The
implementation uses the IBM-documented HMC CLI attributes only; it does not add REST fields,
profile mutation, or a new snapshot-application surface.

## Architecture and data flow

`MinimumAffinityPolicy` is the shared immutable vocabulary. Its validator runs before network
access and is reused by the SSH setter and provisioning. `set_minimum_affinity_policy` resolves
the REST target and ownership names, authorizes mutation with the existing ownership protocol,
then delegates to `set_minimum_affinity_policy_cli`. The CLI helper revalidates, probes advertised
processor compatibility, raises a capability error unless `POWER11` is present, and dispatches one
`chsyscfg -r lpar` record containing the name, score, and action.

The MCP adapter is `effect="mutate"`, targets an LPAR, and exposes `ownership_override` consistently
with other ownership-protected mutations. Provisioning accepts `minimum_affinity_policy=None`.
When present, it validates and probes capability before any create or adapter call; after a
successful create and ownership stamp it applies the policy before the remaining workflow steps.
The policy step participates in the existing partial-result model.

## Failure contract

Invalid score/action raises `ValueError` before any HMC call. Unsupported systems raise an
actionable capability error before mutation, including provisioning create. Ownership denial
occurs before the setter's `chsyscfg`. CLI transport and command failures propagate. During
provisioning, a post-create policy command failure records `minimum_affinity_policy=error`, skips
later steps, and retains the created-resource result without pretending rollback occurred.

## Threat model

- Added boundary: authenticated MCP caller supplies score, action, selectors, and override. Tool
  metadata provides dispatch authorization; typed/domain validation bounds policy values; existing
  target scoping bounds selectors; existing ownership authorization controls mutation.
- Widened boundary: validated values enter an HMC SSH command. Values are closed-domain integers
  and literals, selectors use existing quoting/filter builders, and the entire input record is
  shell-quoted.
- Actors: an authenticated but policy-constrained caller and an HMC returning malformed capability
  data. The design trusts the configured HMC credentials and existing dispatch policy.
- Out of scope: compromised HMC credentials, undocumented REST behavior, and automatic rollback
  of a successfully created partition.

## Acceptance tests

- Scores below 0 or above 100 and unknown actions fail before capability or mutation calls.
- A system without advertised `POWER11` fails before `chsyscfg`; provisioning fails before create.
- Ownership authorization precedes setter mutation, and denial prevents it.
- The generated command contains both documented fields and safely quoted selectors.
- `fail` is accepted only when explicitly supplied; no API default selects it.
- Omitting provisioning policy preserves the existing sequence and defaults.
- A supplied provisioning policy is prevalidated, capability-gated, and applied after create but
  before network/storage/power-on.

## Verification context

Host is x86_64. Declared targets are amd64, arm64, and ppc64le; the host is included. Required
guardrails are `just verify` and `prek run --all-files`.
