# ADR 0315: Require a complete VSI profile and classify client Ethernet add refusals

## Status
Accepted, 2026-11-03. Authority: #1864 and its WORK:SCOPE annotation.
Amends [ADR 0307](0307-client-ethernet-creation-fields.md) in two places: its bounded VSI
policy, which left all VSI validation to the HMC, and its ST9 six-field request.

## Context
A live capture on 2026-11-03 (V10R3 M1060) added a client Ethernet adapter to an inactive test
partition, one creation field at a time.
- Each policy field (`addl_vlan_ids`, `allowed_os_mac_addrs`, `qos_priority`) was accepted.
- Each `vsi_*` field sent alone was refused with REST0126, naming the missing manager or type ID.
- All six fields together were refused with HSCLB021 (RMC return code 83). That error names the
  serving VIOS, so the HMC registers a complete VSI profile through the VIOS.
- Before one `chsyscfg -o apply`, every add was refused with REST0269, although `curr_profile`
  already named the profile.

Callers received these refusals as bare HTTP 500s.

## Decision
- Refuse any selected VSI field unless `vsi_manager_id` and `vsi_type_id` are both selected.
  `vsi_type_version` stays optional, and the HMC still validates values finally.
- In the add operation, re-raise a 500 whose body names HSCLB021 or REST0269 as a new
  `HMCError` with an actionable message. The new error keeps the status and body and chains the
  original. Any other error propagates as the same object.
- ST9 sends no creation fields. The policy-field evidence is this capture. VSI needs a VSI
  manager, which is outside the test boundary.

## Consequences
- A call that sent one VSI field now fails locally instead of at the HMC.
- The classified messages depend on the HMC's code tokens. An HMC that words a refusal
  differently gets today's unclassified error.
- ST9 no longer proves the six creation fields on each run.

## Considered & rejected
- **Leave all VSI validation to the HMC (ADR 0307 as written).** verified: the 2026-11-03
  capture shows each single VSI field refused with REST0126, so the HMC requires the pair. A
  local check reports this before any read or write.
- **Require all three VSI fields.** verified: the capture never sent a manager-and-type pair
  without a version, so requiring the version has no evidence behind it.
- **Refuse VSI fields on an inactive partition.** verified: no capture added a VSI profile to an
  active partition. The failure names the VIOS's RMC call, not the client's state. A state-based
  refusal would block a request that might succeed.
- **Classify in the client transport.** judgment: the add operation is the single owner that
  the MCP and CLI paths share. Other PUTs carry these codes with different meanings.
- **Restore ST9's six-field request.** judgment: VSI cannot pass without a VSI manager, and
  re-proving the policy fields on every run duplicates this capture.
