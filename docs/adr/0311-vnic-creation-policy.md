# ADR 0311: Optional single-backing vNIC creation policy

## Status

Accepted 2026-10-30 under the operator-approved #1827 scope, subject to normal review.
Amends ADR0061 only for the typed vNIC creation caller's explicit opt-in.

## Context

The existing add route omits five documented policies and drops their observations.
Two list policies require nontrailing quoted pairs, which ADR0061's default rejects.
The admitted-release [V10R3 M1060 manual](https://www.ibm.com/support/pages/system/files/inline-files/hmc_man_10.3.1060.0.pdf),
CHHWRES printed page 25, documents a nontrailing quoted VLAN-list example. It is source
support, not evidence of a successful optional-policy mutation on the target hardware.

## Decision

Extend the existing ensure-one owner with five optional typed creation fields and independent
selected-policy verification. Omission retains old behavior; zero/false remain selected.
Use unique VLAN members 1..4094 and unique lowercase 12-hex MACs, all/none sentinels, and a
65,536-byte UTF-8 attribute-record cap as approved application policy, with HMC eligibility
still decisive. No native list-count cap is inferred from older REST types.
Keep one encoder: build_attribute_record gains default-off allow_nontrailing_quoted, used
only by typed vNIC creation. Preserve its remaining grammar guards and complete shell quoting.
Conflicting/missing selected policy on an existing match refuses without another add;
post-write uncertainty raises the existing structured partial error without retry/rollback.

## Consequences

Snapshots/results gain independent policy evidence; topology outcomes remain separate.
All five policies and both lists can share one creation attempt. Native optional modes,
traffic/failover and other environments remain unevidenced; admission is unchanged.

## Considered & rejected

- Omit policy verification: verified: current vnic.py drops five requested inventory fields;
  #1827 requires selected-field observation before no-write success.
- Reject simultaneous multi-member lists: judgment: narrows the requested combination when
  documented record grammar can preserve it with an explicitly approved bounded exception.
- Split creation and policy update: judgment: violates one-attempt creation and crosses #1828.
- Change the global default grammar: judgment: widens unrelated callers beyond this decision.
