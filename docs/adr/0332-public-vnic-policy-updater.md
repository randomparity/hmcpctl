# ADR 0332: Offer only natively qualified existing-vNIC policy updates

## Status

Proposed, 2026-11-04 (#1858). Native qualification cases U1–U7 in
`docs/live-testing.md` gate acceptance.

## Context

#1828 asks for a typed updater for five existing-vNIC adapter-policy fields:
`port_vlan_id`, `pvid_priority`, `auto_priority_failover`, `allowed_vlan_ids` and
`allowed_os_mac_addrs`. ADR 0312 records the native qualification that #1856 and
#1857 ran on HMC V10R3 M1060 through `chhwres -r virtualio --rsubtype vnic -o s`:

- `pvid_priority` and `auto_priority_failover` sets qualified;
- `port_vlan_id` 0→nonzero qualified while allowed VLAN IDs were `none`, and
  `HSCLAB12` refused a zero/nonzero change while allowed VLAN IDs were configured;
- `+=` on either finite list qualified; `-=`, a dropping `=` replacement and the
  `all`/`none` sentinels were refused;
- a multi-pair record is applied or refused as a whole;
- an omitted `auto_priority_failover` was reset to 1 by an update of another field.

#1858 originally expected an explicit `none` clear. That expectation predates the
#1856 evidence: `=none` on an existing vNIC was refused natively, so the issue's own
"offer only natively qualified modes" rule excludes it.

## Decision

Add `vnic.update_policy`: the `update_vnic_policy` operation, the
`hmc_update_vnic_policy` MCP tool and `hmcpctl network update-vnic-policy`. It
selects one vNIC by partition and slot and accepts exactly one of five inputs:
`port_vlan_id`, `pvid_priority`, `auto_priority_failover`, or add-only
`add_allowed_vlan_ids` / `add_allowed_os_mac_addrs`. There is no replace, remove,
clear or sentinel input, and no combined request.

The request is desired state. A scalar equal to its baseline, or an add whose
members are all present, returns without dispatch; members already present are
dropped. Otherwise it refuses before any effect when:

- `port_vlan_id` would leave a nonzero baseline, would become 0, or the baseline's
  allowed VLAN IDs are not `none`;
- a list add targets an `all`, `none` or unobservable baseline;
- the baseline `auto_priority_failover` is unobservable and must be preserved.

Every record carries `auto_priority_failover` explicitly: the requested value, or
else the observed baseline value. So exactly five record shapes exist: the changed
pair plus that one, or `auto_priority_failover` alone. A list pair goes last, as
ADR 0311 requires of a quoted pair, through the shared `build_attribute_record`.
The operation submits once and never retries or rolls back.

The result reports three independent axes: submission (`not_submitted`,
`completed`, `rejected`, `unknown`), selected-policy readback against the derived
expected values, and preservation of every other cell of the partition's vNIC rows.
Anything short of completed, verified and unchanged raises `VnicPartialError` with
the full result attached. After an `unknown` submission `changed` stays null even
when the readback shows no change, because the command may still apply later.

## Consequences

The updater is admitted only where `require_admitted_environment` admits SR-IOV
mutations, which is the environment the evidence came from. Every mode beyond
those listed stays unoffered until a native run qualifies it; widening one is a
code change, not a configuration choice. Pairing a change with the preserving
`auto_priority_failover`, and multi-member adds, were not in #1857's set; the #1858
native cases U1–U7 in `docs/live-testing.md` exercise each of the five shapes,
bare and quoted, before merge. A concurrent writer between the baseline read and the
submission can have its `auto_priority_failover` change overwritten; preservation
then reports drift only for cells the update did not target.

## Considered & rejected

- **Offer explicit `none` clear as #1858 first proposed.** verified:
  `docs/live-testing.md` "Recorded source outcomes (#1856)" records
  `allowed_vlan_ids=none` and `allowed_os_mac_addrs=none` on an existing vNIC as
  rejected with "An invalid attribute value was entered" on HMC V10R3 M1060.
- **Expose replace (`=`) and remove (`-=`) and let the HMC refuse.** verified: the
  #1857 table records `HSCLAB1F`/`HSCLAB20` for every removing form; offering a
  mode known to be refused contradicts the qualified-modes criterion.
- **Submit only the requested pairs.** verified: #1857 case W1 submitted
  `pvid_priority=5` alone and the readback showed `auto_priority_failover` 0→1.
- **Refuse a request whose scalar already matches, as the qualification harness
  does.** judgment: the harness refuses no-ops to tell an ignored update apart;
  a public caller retrying after an uncertain outcome needs the converged no-op.
- **Accept several inputs in one request.** verified: #1857 qualified only
  single-change records, and W10 (`docs/live-testing.md`) shows a multi-pair record
  refused as a whole; each combination would need its own native case.
- **Extend `hmc_add_vnic` with an update mode.** judgment: creation selects
  backings and capacity and has different sentinel semantics; one tool carrying
  both multiplies its schema and its failure model.
