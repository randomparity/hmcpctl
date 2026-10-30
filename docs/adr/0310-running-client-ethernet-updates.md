# ADR 0310: Update running client Ethernet through one native command

## Status

Proposed, 2026-10-30. Private #638/N2 design; not implementation authority.
0310 is privately reserved after checking 398 distinct visible heads; no collision.
The only open PR head was included. Recheck the reservation before publication.

## Context

The six N2 fields describe one existing client Ethernet adapter on a running LPAR.
Creation belongs to #1821; vNIC backing/failover and system defaults retain their owners.
Native documentation covers list actions, QoS disable and whole VSI-profile removal.
Retained REST evidence does not establish all corresponding clear/action encodings.
The operator approved native writes, typed updates, separate results, coordinated writers,
bounded VLAN rules, HMC-final eligibility and explicit empty-list/no-write behavior.

## Decision

Extend the existing adapter operation owner with one UUID-targeted typed update.
Resolve the selected system and contained LPAR, authorize ownership, prove the adapter's
current UUID/slot mapping, then attempt at most one `chhwres --rsubtype eth -o s`.
Require `exclusive_writer_window=True`; it asserts coordination, not an HMC lock.
Reuse `build_attribute_record`, including its existing trailing quoted-list rule.
Use documented native operators; empty replacement removes the complete baseline set.
An already-empty, otherwise empty request returns explicit `not_dispatched` evidence.
Separate native submission from selected-field verification and preservation observations.
Readback is independent of command output; unavailable evidence never establishes success.
Do not retry, activate, force disruption, or compensate automatically after uncertainty.
Keep the standard audited ownership override; it bypasses ownership only.
Classify the operation as Reconfiguring in ADR 0092 and MCP `mutate`.

## Consequences

SSH is required. Native writes have no ETag compare-and-set guarantee.
VLAN policy is explicitly 1–4094, unique IDs, at most twenty resulting members.
HMC validates undocumented eligibility/semantic limits after established local checks.
VSI removal remains observation-unavailable without a positive source-backed oracle.
Preservation describes a bounded parsed-resource comparison, not raw XML or live traffic.
Existing creation APIs/results and the six-name stable facade remain unchanged.
The future live arm retains its one requested change; read-only recovery does not undo it.

## Considered & rejected

- REST-only update: verified: retained schema matrix `network.json` at base `669ebc0e`
  establishes numeric fields but no REST VSI-removal sentinel or native list-action equivalence.
- Parallel native and REST writers: judgment: duplicate mutation and uncertainty contracts.
- Trust command completion: judgment: completion cannot replace independent stored-state evidence.
- Add a generic mutation framework: judgment: one operation does not justify new orchestration.
- Implicit empty native value: verified: `docs/refs/hmc-commands-p11/commands/chhwres.md:241`
  documents list subtraction but establishes no empty-list replacement encoding.
