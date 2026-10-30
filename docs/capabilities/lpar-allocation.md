# LPAR configuration allocation

This map records the operator's ownership and child-filing approvals for
[#638](https://github.com/randomparity/hmcpctl/issues/638), their historical
allocation and subsequent implementation progress. Allocation and implementation
are separate from passing live verification and hardware authority.
The reviewed allocation was based on `b11d25c398bb29ca3fbf874b450bbb8e5ccdf929`;
implementation status below was reconciled on 2026-10-30 after PR #1825 merged.

## Whole-row transfers

The approved historical transfer changed only `disposition.issue` on these
22 rows. `rows.json` retained its schema and every reference binding; this
progress reconciliation makes no further ledger change:

| Previous owner | New owner | Scope | Rows | Source units |
| --- | --- | --- | ---: | ---: |
| #637 | #1724 | Original approved profile/default-selection allocation | 3 | 106 |
| #638 | #1724 | LPAR SaveCurrentConfig | 1 | 32 |
| #698 | #1724 | VIOS SaveCurrentConfig | 1 | 32 |
| #638 | #671 | Migration/remote restart | 12 | 713 |
| #638 | #671 | `rsthwres` resource recovery | 1 | 52 |
| #638 | #668 | SR-IOV Ethernet/FCoE logical-port ClearStatistics | 2 | 12 |
| #638 | #663 | vFC/vSCSI client adapters | 2 | 42 |

#638's original 39 rows / 2,132 units became 21 rows / 1,281 units after
transferring 18 rows / 851 units. The additional VIOS row came from #698.
#1724 became five rows / 170 units. Its original three rows / 106 units had
still been bound to parent #637 in the ledger; the transfer reconciled their
previously approved child assignment alongside the two SaveCurrentConfig transfers.
The global 382-row / 14,311-unit denominator is unchanged.

The migration group comprises `lslparmigr`, `lsrrstartlpar`, `migrlpar`,
`rrstartlpar`; LPAR Migrate, MigrateAbort, MigrateRecover and MigrateValidate;
ManagedSystem LogicalPartition_RemoteRestart, partition-migration-progress-status,
PartitionValidateMigrate_LogicalPartition and PartitionValidateMigrate_ManagedSystem.

## Shared-family responsibilities

One structured source unit can describe multiple resource types or attributes.
Semantic responsibility below does not split, duplicate or transfer a whole mixed
row. Its supporting units remain counted once under their existing row owner.
Each owner must verify exact behavior before claiming equivalence or implementation.

| Capability | Responsible work |
| --- | --- |
| Profile CRUD/copy/default selection and documented save/apply variants | #1724 |
| System-profile CLI variants | Reuse closed #1725; verify exact equivalence without reopening or duplicating it |
| `chsyscfg -r sys` and system-level memory/processor configuration | #680 |
| Shared-processor-pool configuration, capacity and assignment | #641 |
| Shared-memory/reserved-storage pools and paging-device membership | #642 |
| Persistent-memory volumes and memory devices | #643 |
| ResourceGroup lifecycle and membership | #639 |
| Virtual-switch mutations and VLAN/network lifecycle | #664 |
| SR-IOV adapter mode transitions and configuration replacement | #667 |
| SR-IOV physical/logical-port properties and statistics | #668 |
| vNIC backing-device update/failover capabilities matching its scope | #669 |
| Virtual serial-number management | #695 |
| Bounded `lpardbg` collection and `lslpardbg` inventory | #690 |
| Diagnostic export/send | #692 |

The 22-unit vNIC row remains wholly owned by #638 until every capability's fit is
proved; #669 receives no blanket adapter scope. Mixed Ethernet/vNIC selectors route
Ethernet to the bounded N1/N2 work described below; matching vNIC backing-device
update/failover behavior remains with #669, and creation/adapter-field gaps retain
their separate scopes. Diagnostic collection/transfer selectors similarly share
supporting evidence.

## Completed configuration and dynamic-resource children

All twenty original native children of #638 (#1762–1781) are closed with merged
implementations. They used the landed core in #1724, #1725 and #1726 and their
specific internal dependencies; the dependency notes in the table are historical.
#637 and #1724 are also closed. The children's bodies retain the exact reviewed
source selectors, applicable contexts and verification obligations. Their finite
scopes below do not establish completion of every retained family or passing live
evidence. Source occurrences and shared selectors remain single-counted ledger
support, not new exclusive units.

| Slice | Issue | Finite scope |
| --- | --- | --- |
| C01 | [#1762](https://github.com/randomparity/hmcpctl/issues/1762) | Performance, priority, time-reference and restart flags |
| C02 | [#1763](https://github.com/randomparity/hmcpctl/issues/1763) | IBM i reporting and restricted-I/O settings |
| C03 | [#1764](https://github.com/randomparity/hmcpctl/issues/1764) | vTPM configuration |
| C04 | [#1765](https://github.com/randomparity/hmcpctl/issues/1765) | Secure-boot and keystore policy |
| C05 | [#1766](https://github.com/randomparity/hmcpctl/issues/1766) | Management-role and KVM eligibility |
| C06 | [#1767](https://github.com/randomparity/hmcpctl/issues/1767) | Huge-page and page-table configuration |
| C07 | [#1768](https://github.com/randomparity/hmcpctl/issues/1768) | Shared-memory partition configuration |
| C08 | [#1769](https://github.com/randomparity/hmcpctl/issues/1769) | Conventional `affinity_group_id` at creation |
| C09 | [#1770](https://github.com/randomparity/hmcpctl/issues/1770) | Boot, start and power-control configuration; reuse supported paths |
| C10 | [#1771](https://github.com/randomparity/hmcpctl/issues/1771) | Hardware memory encryption/expansion |
| C11 | [#1772](https://github.com/randomparity/hmcpctl/issues/1772) | Virtual-serial adapter creation |
| C12 | [#1773](https://github.com/randomparity/hmcpctl/issues/1773) | Physical/tagged I/O; reuse PCIe assignments |
| D01 | [#1774](https://github.com/randomparity/hmcpctl/issues/1774) | Non-pool memory quantities, `a/r/s` |
| D02 | [#1775](https://github.com/randomparity/hmcpctl/issues/1775) | Two-partition memory moves, `m`; historical dependency #1774 |
| D03 | [#1776](https://github.com/randomparity/hmcpctl/issues/1776) | Redundant paging-VIOS switchover, `so`; historical dependency #1768 |
| D04 | [#1777](https://github.com/randomparity/hmcpctl/issues/1777) | Entitled-memory statistics reset, `rs` |
| D05 | [#1778](https://github.com/randomparity/hmcpctl/issues/1778) | Non-pool processor quantities, `a/r/s` |
| D06 | [#1779](https://github.com/randomparity/hmcpctl/issues/1779) | Two-partition processor moves, `m`; historical dependency #1778 |
| D07 | [#1780](https://github.com/randomparity/hmcpctl/issues/1780) | Sharing/uncapped weight; reuse supported sharing modes |
| D08 | [#1781](https://github.com/randomparity/hmcpctl/issues/1781) | Partition GZIP accelerator QoS, `s` |

Conventional affinity groups, ResourceGroup membership and minimum-affinity score
policy are distinct. Existing minimum-affinity validation/query/setter/provisioning
is reused; no duplicate minimum-policy child is filed. Profile-context capabilities
remain with #1724; C06/C07/C09/C11/C12 coordinate shared models rather than duplicate
profile lifecycle. Pool lifecycle remains separate from partition resource changes.

The operator-approved C12 boundary retains all eight fields/sixteen source
bindings. Physical/HMC and declared direct-SR-IOV creation selectors are implemented
in #1773. Positive virtual-slot forms for `load_source_slot`,
`alt_restart_device_slot` and `console_slot` without creation-time adapter linkage
remain rejected before effects. [#1830](https://github.com/randomparity/hmcpctl/issues/1830)
tracks the missing device-role and creation-time adapter-linkage prerequisites under
#638. Existing adapter implementation owners are unchanged. Closure of #1773 does
not complete these positive virtual-slot modes or establish passing live evidence.

The operator-approved C04 boundary refuses creation combining
`simplified_remote_restart_capable=True` with `keystore_kbytes>0` before effects.
The five security-policy fields remain separately available; this is no update
restriction. [#1829](https://github.com/randomparity/hmcpctl/issues/1829) tracks the
supported positive user-defined system-key classifier prerequisite. Closure of
#1765 does not complete this combination.

## Completed client Ethernet work

N1 creation fields landed through [#1821](https://github.com/randomparity/hmcpctl/issues/1821)
and merged [PR #1822](https://github.com/randomparity/hmcpctl/pull/1822).
[ADR 0307](../adr/0307-client-ethernet-creation-fields.md) records the six optional
fields and independent selected-field observation. N2 running-adapter updates landed
through [#1824](https://github.com/randomparity/hmcpctl/issues/1824) and merged
[PR #1825](https://github.com/randomparity/hmcpctl/pull/1825).
[ADR 0310](../adr/0310-running-client-ethernet-updates.md) records the typed native
update, writer-coordination requirement and bounded readback. These are implemented
client Ethernet scopes; stored-state observations do not prove live traffic or
external VSI registration, and hardware qualification remains separate.

## Retained reconciliation

#638 retains ancillary client-network reconciliation beyond N1/N2, including
trunk/VIOS-server contexts and N5 multi-backing creation. N4 vNIC creation and
adapter-field update gaps are tracked by
[#1827](https://github.com/randomparity/hmcpctl/issues/1827) and
[#1828](https://github.com/randomparity/hmcpctl/issues/1828); backing-device
update/failover remains with #669, and managed-system defaults remain with #680/A9.
Utilization configuration/read/delete, power/activation, network-boot and
reserved-memory-query reconciliation also remain under #638. Subtract existing
equivalent supported paths before proposing children for demonstrated residual gaps.
No capability is silently dropped or marked complete. #698 audits completion rather
than inheriting implementation gaps.

For the completed #1724 profile work,
[ADR 0242](../adr/0242-partition-profile-lifecycle-contract.md#bounded-projection-decision--2026-10-22)
replaced the complete-transitive-XSD prerequisite with bounded annotation projection
under the captured root/eight-adapter grammar. Complete clone readback equality
allows only four approved root scalar differences: `ProfileName`, `SettingID`,
`Metadata/Atom/AtomID` and `Metadata/Atom/AtomCreated`. In-place profile identity
remains unchanged. General SettingID regeneration semantics and universal populated
adapter-clone qualification are not established by this bounded contract.

#638 remains open. The closure of #637/#1724 and the implementation merges above
do not change maturity/runtime admission or establish passing live evidence for
unrun, skipped or failed cases. This page grants no live execution authority.
