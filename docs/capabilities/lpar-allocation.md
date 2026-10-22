# LPAR configuration allocation

This map applies the operator's individual ownership and child-filing approvals
for [#638](https://github.com/randomparity/hmcpctl/issues/638). It records planning
responsibility, not implemented coverage, passing verification or hardware authority.
The reviewed allocation was based on `b11d25c398bb29ca3fbf874b450bbb8e5ccdf929`.

## Whole-row transfers

`rows.json` retains its existing schema and every reference binding. Only the
`disposition.issue` of these 22 rows changes:

| Previous owner | New owner | Scope | Rows | Source units |
| --- | --- | --- | ---: | ---: |
| #637 | #1724 | Original approved profile/default-selection allocation | 3 | 106 |
| #638 | #1724 | LPAR SaveCurrentConfig | 1 | 32 |
| #698 | #1724 | VIOS SaveCurrentConfig | 1 | 32 |
| #638 | #671 | Migration/remote restart | 12 | 713 |
| #638 | #671 | `rsthwres` resource recovery | 1 | 52 |
| #638 | #668 | SR-IOV Ethernet/FCoE logical-port ClearStatistics | 2 | 12 |
| #638 | #663 | vFC/vSCSI client adapters | 2 | 42 |

#638's original 39 rows / 2,132 units become 21 rows / 1,281 units after
transferring 18 rows / 851 units. The additional VIOS row comes from #698.
#1724 becomes five rows / 170 units. Its original three rows / 106 units were
still bound to parent #637 in the ledger; this reconciles their previously approved
child assignment alongside the two new SaveCurrentConfig transfers.
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
Ethernet to retained client-network reconciliation and matching vNIC behavior to
#669. Diagnostic collection/transfer selectors similarly share supporting evidence.

## Filed configuration and dynamic-resource children

All twenty issues are native children of #638 and start against the merged core
in #1724, #1725 and #1726. #637 retains residual profile reconciliation and is
not a blanket prerequisite; each child retains its specific dependencies.
Their bodies bind the exact reviewed source selectors, applicable contexts and
verification obligations. Source occurrences and shared selectors are not new
exclusive ledger units. Full-spec design precedes implementation of each child.

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
| D02 | [#1775](https://github.com/randomparity/hmcpctl/issues/1775) | Two-partition memory moves, `m`; also blocked by #1774 |
| D03 | [#1776](https://github.com/randomparity/hmcpctl/issues/1776) | Redundant paging-VIOS switchover, `so`; also blocked by #1768 |
| D04 | [#1777](https://github.com/randomparity/hmcpctl/issues/1777) | Entitled-memory statistics reset, `rs` |
| D05 | [#1778](https://github.com/randomparity/hmcpctl/issues/1778) | Non-pool processor quantities, `a/r/s` |
| D06 | [#1779](https://github.com/randomparity/hmcpctl/issues/1779) | Two-partition processor moves, `m`; also blocked by #1778 |
| D07 | [#1780](https://github.com/randomparity/hmcpctl/issues/1780) | Sharing/uncapped weight; reuse supported sharing modes |
| D08 | [#1781](https://github.com/randomparity/hmcpctl/issues/1781) | Partition GZIP accelerator QoS, `s` |

Conventional affinity groups, ResourceGroup membership and minimum-affinity score
policy are distinct. Existing minimum-affinity validation/query/setter/provisioning
is reused; no duplicate minimum-policy child is filed. Profile-context capabilities
remain with #1724; C06/C07/C09/C11/C12 coordinate shared models rather than duplicate
profile lifecycle. Pool lifecycle remains separate from partition resource changes.

## Retained reconciliation

#638 retains bounded client-networking, utilization configuration/read/delete,
power/activation, network-boot and reserved-memory-query reconciliation. Subtract
existing equivalent supported paths first and propose additional implementation
children only for demonstrated residual gaps. No capability is silently dropped or
marked complete. #698 audits completion rather than inheriting implementation gaps.

This allocation does not close #638, #637 or #1724, change maturity/runtime admission,
or grant live execution. #1724's complete-schema and SettingID source requirements
remain unresolved; approved ownership is not feasibility evidence.
