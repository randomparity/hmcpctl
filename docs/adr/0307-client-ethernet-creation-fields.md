# ADR 0307: Extend client Ethernet creation with selected-field observation

## Status
Accepted, 2026-10-29. Independent design review and scope audit approved the contract.
Authority: #1821 and its amended WORK:SCOPE annotation.

## Context
Existing creation already owns target resolution, ownership, PUT construction and results.
Six missing inputs need truthful post-write observation without breaking top-level identity.

## Decision
Extend the existing interface and builder with the six approved optional fields.
Keep existing omission behavior and add separate verification metadata only for selected inputs.
Use contained before/after identity plus selected-field observations; preserve uncertainty.
Apply approved VLAN policy and bounded QoS/VSI HMC-final-validation policy.

## Consequences
Adds bounded reads to selected-input requests and can report acknowledged creation unverified.
Existing create-with-no-new-input calls and storage adapter results retain their contracts.
Stored configuration does not prove runtime network behavior or external VSI registration.

## Considered & rejected
- New settings object: judgment: extra surface for six fields on one existing operation.
- Native parallel creator: judgment: redundant transport and identity/recovery policy.
- Trust PUT response alone: verified: core.py:1575 create_child returns the first parsed PUT
  entry without an independent read at baseline b305024c; it cannot prove C4.
- Wrap existing identity keys: verified: profile_sync.py:55 documents live callers reading
  top-level identity; preserve that contract instead.

## Specification

See [the creation contract](../workflow/specs/2026-10-29-client-ethernet-creation-design.md).
ST9 exercises one fixed six-field request with verified-UUID-only cleanup. No hardware
qualification is claimed; uncertainty stops subsequent harness effects.
