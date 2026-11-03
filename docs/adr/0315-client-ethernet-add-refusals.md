# ADR 0315: Classify client Ethernet add refusals and keep ST9's minimal request

## Status
Accepted, 2026-11-03. Authority: #1864 and its WORK:SCOPE annotation.
Amends [ADR 0307](0307-client-ethernet-creation-fields.md) on one point: ST9 no longer sends the
six-field request.

## Context
On 2026-11-03 (V10R3 M1060) a live capture added a client Ethernet adapter to an inactive test
partition, one creation field at a time.

- Until one `chsyscfg -o apply`, every add was refused with REST0269, even though `curr_profile`
  already named the profile.
- After the apply:
  - each policy field (`addl_vlan_ids`, `allowed_os_mac_addrs`, `qos_priority`) was accepted;
  - each `vsi_*` field sent alone was refused with REST0126, which names the missing manager or
    type ID;
  - all six fields together were refused with HSCLB021 (RMC return code 83), naming the serving
    VIOS. So the HMC registers a complete VSI profile through the VIOS.

Callers received REST0269 and HSCLB021 as bare HTTP 500s with no next step.

## Decision
- The add operation turns a 500 whose body names HSCLB021 or REST0269 into a new `HMCError`
  with a next step. The new error keeps the status and body and chains the original. Any other
  error propagates as the same object.
- The classified messages say only that the HMC refused the add. They tell the caller to list
  the partition's adapters to confirm none was left; they never claim that nothing changed.
- ST9 sends no creation fields. This capture is the evidence for the policy fields. A VSI
  profile needs a VSI manager, which the test boundary does not have.
- VSI validation stays with the HMC, as ADR 0307 decided. The tool documents the VIOS route and
  the manager/type pairing.

## Consequences
- The classification depends on the HMC's code tokens. A release that words a refusal
  differently still gets the unclassified error.
- vSCSI and vFC adds on an unapplied partition still return REST0269 unclassified.
- ST9 no longer proves the creation fields on each run. No capture has seen a VSI profile
  accepted on add.

## Considered & rejected
- **Do nothing.** verified: in #1861's runs 1–3 the ST9 add failed with bare 500s, and the
  refusal itself never named the remedy that the capture proved works (apply the profile).
- **Refuse an incomplete VSI profile locally.** verified: in the capture, the HMC's REST0126
  for each lone field already named the missing field, and nothing reached the VIOS. A local
  copy of that rule would fix one HMC level's behaviour in code and break existing single-field
  tests.
- **Refuse VSI fields on an inactive partition.** verified: no capture added a VSI profile to an
  active partition, and the failure names the VIOS's RMC call, not the client's state.
- **Withdraw the VSI creation fields.** judgment: the capture shows an environment without a VSI
  manager, not a field that is unusable. ADR 0307's field set stays.
- **Classify in the client transport.** judgment: other adapter types are out of this change's
  scope. Residue differs by path, too: a REST0269 on a mapping create has left a VIOS server
  adapter (`client_storage.py`, #1085).
- **Restore ST9's six-field request.** judgment: VSI cannot pass without a VSI manager, and
  re-proving the policy fields on every run repeats this capture.
