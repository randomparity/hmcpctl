# ADR 0306: Bound VIOS boot-source qualification to one fixed candidate

## Status

Accepted, 2026-10-27. Independent design review and scope audit approved the contract.
Design-only authority: call_dfc2b0181f834c4083aafd06c2a39556/0.

## Context

#1770 needs nonempty VIOS pending-boot source evidence. The retained full native header
has an empty pend_boot_string on a Running VIOS; it establishes neither an opaque grammar
nor punctuation transport or inactive preservation. Product completion remains separate.
Root approved one bounded diagnostic to qualify, reject or hold one fixed encoding on one
fresh inactive owned VIOS/release. The diagnostic has its own L/1000 full-spec charter.

## Decision

Register explicit vios-boot-source preflight, capture, read-only recovery and source-only
rendering. Permit one create, eight fixed ordered cases and one conditional owned delete.
Use csv.writer QUOTE_MINIMAL for the two complete attribute cells, then shlex.quote the
whole record. Read a complete independent native header after each attempt; require exact
pend_boot_string equality and fixed B1/N1 preservation, including profile ETags and native
nonpending cells. Preserve B0 for final absence/restoration; never refresh baselines.
Reuse the existing AME snapshot/physical accounting primitives via finite new subclasses,
without changing their owner or introducing a generic framework. Tighten deletion before
parent dispatch and attest at the final physical boundary. Unknown, malformed or changed
preserved evidence holds subsequent resource effects, including deletion.

## Consequences

A positive result is restricted to eight literal observations on the bound release/fixture.
An accepted pending value with ETag or uptime drift remains unqualified; that gate has no
hardware guarantee. Held executions may retain one fixture for separately authorized root
recovery. Ordinary runners, product interfaces, native grammar and maturity remain unchanged.
Selected source hashes bind private helper coupling; existing AME tests protect its policy.
The operator approved the diagnostic-only contract-plan format in place of complete code.
This record authorizes neither implementation nor hardware; all normal gates remain.

## Considered & rejected

- Wait for a complete published grammar. judgment: root selected an exact bounded source
  qualification prerequisite instead; waiting does not produce the requested finite evidence.
- Guess alternative encoders until one succeeds. judgment: conflicts with the approved single
  candidate/eight cases and once-only effect budget; rejection is a useful terminal result.
- Add pending boot XML to the existing snapshot. verified: at base056cdc8006eb04323323896d126ec6f4a9929cb0,
  scripts/live_test/vios_ame_source.py:1067–1206 contains no supported pending-boot leaf;
  the retained native header is the available source. No VIOS pending XML mapping is proved.
- Parameterize the whole AME lifecycle. judgment: widens an independent diagnostic's cases,
  grants and history policy; finite subclass reuse preserves its existing owner.
- Do nothing. judgment: retains the unresolved source gate and cannot provide the approved
  registered diagnostic or truthful bounded negative evidence.
