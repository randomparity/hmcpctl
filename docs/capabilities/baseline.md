# Existing-code verification baseline (#635)

This acceptance covers the 163 registered operations joined by [operations.json](operations.json),
not the 382 reference rows or full API completion. Every current operation has a schema4
[maturity record](maturity.json), a reference-row or explained composite disposition and nonempty
automated-test links. `just capability-inventory` rejects omissions and empty test lists.
Those links identify tests; the candidate's test run supplies the automated result.

No native run was authorized for this reconciliation. The 110 stored observations are unchanged,
including four failed observations. Eleven added records have empty evidence, so candidate
reporting calls them unevidenced. [ADR0212](../adr/0212-release-scoped-live-verification.md)
keeps installed verification tied to the unchanged last-release snapshot; implementation metadata
changes do not promote it. `just verification-report` separately derives candidate currency.
Historical passing results prove only their recorded scenario, assertions, environment and closure;
they do not prove untested variants or current fitness. Earlier domain-spec result tables describe
their dated runs; the catalog retains later observations where revalidation occurred.

## Procedure and obligation map

The finite set is the operation IDs in operations.json. The families below cover that set;
`lpar` is split by behavior. Each linked procedure owns its scenario steps, baseline,
postconditions and recovery; its **Live gaps** section is a durable linked obligation, even if
its originating issue is closed. Closure is not evidence. Missing hardware, authority, images,
licenses, topology or an asserted named arm is the reason those cases remain unrun. Operators
own native verification; later API children own missing implementation. No obligation is closed here.

For every native case, first follow [live-testing](../live-testing.md): match the deployed candidate,
run preflight, use the applicable named arm only under explicit effects/target authorization, and
run recovery plus independent baseline comparison afterwards. A transport/job success without
intended postconditions and cleanup cannot pass. If the named arm does not assert the case, the
obligation remains open; this document does not authorize an ad-hoc mutation invocation.

| Registered operations | Procedure and remaining prerequisites/obligations |
| --- | --- |
| `console`, `capacity`, `placement`, `inventory`, `health`; `system` reads; `lpar` list/get/summary/get_state/list_refcodes/list_ownership/inspect/plan | [System/LPAR reads](../workflow/specs/2026-10-06-system-lpar-read-contract-verification-design.md): ST1, known managed system and partition; non-operating-system branches, fleet authorization, paging beyond50 and schema-header variants need their declared environments. Planning is a prediction, not allocation proof. |
| `lpar` create/delete/power_on/power_off/power/dump_restart/capture_console/decommission; `system.power_on`, `system.power_off`; `provision` | [Lifecycle/power](../workflow/specs/2026-10-06-lpar-power-contract-verification-design.md): disposable partition/storage and window; whole-system power needs its own approval, graceful actions need active RMC, dump restart explicit dump authority. Native decommission after mapping-detach repair still needs first matching proof. Dedicated provisioning is refused, not verified (see below). |
| `lpar` modify/rename/dlpar_proc/dlpar_mem/set_minimum_affinity_policy; `system.modify` | [Configuration](../workflow/specs/2026-10-06-lpar-config-contract-verification-design.md): scratch partition, resource headroom and reversible baseline; running/DLPAR and capability-specific cases need the stated active partition and hardware. |
| `lpar` get/set_description, get/set_msp, get/set_proc_compat, get_minimum_affinity_policy, get_memopt_score, list_memopt_scores, plan_memopt_scores; `system` get_proc_compat_modes/get_memopt_score/plan_memopt_score; `lpar_profile`, `memory_pool`, `resource_group` | [Profiles/affinity](../workflow/specs/2026-10-05-profile-contract-verification-design.md): profile baseline, owned scratch partition/VIOS, system-wide restore authority where applicable; AMS pool and affinity-capable hardware for positive branches. Restore and MSP failures retain the dispositions below. |
| `boot_order` | [Boot-order reads/writes](../workflow/specs/2026-09-24-boot-order-read-modify-write-design.md) and [clear refusal](../workflow/specs/2026-09-24-clear-boot-order-refusal-design.md) and existing boot-order tests: known partition and device, current/pending order readback and recovery. `clear` is an authorized refusal; clearing pending order remains missing implementation. An activated last-booted-device read needs an activated partition. |
| `pcie`, `sriov`, `vnic`, `io_slot` | [PCIe/SR-IOV/vNIC](../workflow/specs/2026-10-06-pcie-sriov-vnic-evidence-design.md): admitted adapter envelope, unclaimed resources and authorized disposable targets. SR-IOV mode transitions, required/pooled dedicated slots, dynamic or multi-record unassign, failover/degraded vNIC variants retain catalog missing scope and their owners (#667/#669 and linked PCIe work). Current-mode confirmation is not a transition. A vNIC round trip lacks an asserted named step. |
| `network`, `adapter`, `vios_label` | [Network/adapters](../workflow/specs/2026-10-06-network-adapter-contract-verification-design.md): scratch VLAN/adapters, matched VIOS slots, FC hardware/second VIOS/fabric for the declared branches. Historical AssociatedSwitch refusal is not the later catalogued create result; neither establishes NPIV fabric login or running-partition DLPAR. |
| `storage`, `cluster` | [Storage/cluster](../workflow/specs/2026-10-06-storage-contract-verification-design.md): disposable backing storage; free physical volume for group/whole-volume cases, cluster-aware shared pool for positive cluster lifecycle, active partition for dynamic map/detach. Absent-pool proof is not positive-pool proof. |
| `media` | [Virtual media](../workflow/specs/2026-10-06-vmedia-contract-verification-design.md): disposable media/repository, ISO source, matching VIOS and baseline. Repository create/delete needs a VIOS without an operator repository and asserted lifecycle; upload/boot needs an ISO. Later optical round-trip evidence does not erase those gaps. |
| `vios.list`; `vios.create/delete/get/power_on/power_off` | Inventory uses ST1 in the read procedure. The five newly recorded operations have no catalogued live observation; their explicit procedure and obligations follow below. A storage-detail call inside a different scenario is not an asserted proof of `vios.get`. |
| `vios.list_backups/backup/restore/install` | [VIOS backup/install](../workflow/specs/2026-10-06-vios-backup-contract-verification-design.md): operator-approved VIOS access, scratch mapping baseline and backup destination; full-image/SSP needs its storage/topology, install needs image/NIM network and an install window. Submit-and-detach install does not report OS completion. |
| `update`, `upgrade` | [Update jobs](../workflow/specs/2026-10-06-update-job-contract-verification-design.md): approved maintenance window, applicable images/repository, spare disks and recovery plan; firmware submission needs supported HMC11.1.1111+/POWER11. Version-gate refusal is not an update pass; firmware partition-migration scope remains missing. |
| `user`, `task_role`, `resource_role`, `remote_access` | [Users/roles](../workflow/specs/2026-10-06-user-contract-verification-design.md): disposable user and cleanup authority; directory-backed variants need LDAP/Kerberos, remote-access writes an approved window. Source fixes to serializer/timeout models do not prove the failed user-modify live scenario. |
| `pcm`, `metrics`, `template` | [PCM/templates](../workflow/specs/2026-10-06-pcm-template-contract-verification-design.md): PCM-enabled system/metric history, real template and scratch target; #108/#111 retain applicable native positive-case obligations. Template job submission alone is not a deployed partition. |
| `lpar.migrate_validate/migrate/migrate_affinity/migrate_abort/migrate_recover/remote_restart` | [Migration obligations](../workflow/specs/2026-10-18-migration-verification-design.md) and [procedure](../live-testing.md#migration-and-remote-restart-verification-obligations-631): compatible approved source/destination pair, RMC/storage/network/VIOS prerequisites and an asserted named arm; all six are implemented/unevidenced, not native passes. |
| `snapshot` | Existing snapshot/affinity tests and [read procedure](../workflow/specs/2026-10-06-system-lpar-read-contract-verification-design.md): capture has its recorded observation; inspect/validate/assess_affinity have no live observation. Replay a captured system with known affinity/capacity differences and compare each report against independent input facts; no native write or cleanup. Applicable asserted named-arm coverage remains an operator obligation. |
| `job` | Existing job tests and [live-testing](../live-testing.md): a known attributable job, terminal success/error/timeout and response-shape checks; job status alone does not verify its operation's outcome. Preserve each job's declared cleanup policy. |
| `permissions`, `config`, `operation`, `tools` | Local/dispatch-only procedure below. No dedicated IBM capability is implied by these composites. |
| `command` | Generic opt-in SSH dispatch only. Existing authorization/transport tests cover it; a safe operator-approved read command can confirm exact stdout/exit/error forwarding with no mutation cleanup. There is no catalogued live observation or fixed IBM capability coverage. Native dispatch proof remains [this obligation](#local-and-dispatch-only-operations); arbitrary delegated commands are outside this baseline's coverage claim. |

## Five VIOS obligations

[These obligations](#five-vios-obligations) remain open: no dedicated lifecycle/power named arm
asserts them. Require a disposable VIOS with no production storage/network clients, explicit VIOS
create/delete/power authority, capacity and maintenance window, exact revision and independent
before/after partition, mapping and resource baselines. A created VIOS is an inactive partition,
not an installed/working VIOS OS. A power-on proof additionally needs an installed scratch OS.

- Create: call the existing create surface through an approved named arm; independently reread
  VirtualIOServer identity/type, name, resource values and inactive state. Delete only the run's
  new inactive VIOS, then require its absence and unchanged foreign/resource baseline.
- Delete: prove a running VIOS is refused without deletion, then delete an independently identified
  inactive disposable VIOS; require absence, retained foreign inventory and no mappings removed
  from another VIOS. Deletion is irreversible; the fixture's recreation is the recovery boundary.
- Get: compare both returned storage mapping groups with independent VIOS inventory; test duplicate
  name/system disambiguation and wrong-system refusal. Read-only; cleanup is not required.
- Power on/off: retain VIOS identity and baseline state, assert attributable terminal job plus actual
  running/inactive readback; distinguish wait=false submission from wait=true completion. Include
  graceful/immediate only as separately authorized. Restore initial state, compare client mappings
  and foreign inventory. A power-job response alone cannot satisfy either proof.

## Local and dispatch-only operations

The five local operations have empty live evidence; hardware verification is inapplicable to their
local result, not passed. Existing automated scenarios exercise config inventory without reading
secret values, effective permissions without opening HMC sessions, operation queries with agent/
connection/host isolation, bounded visible-tool search, and invocation re-entering actual target
schema and authorization. Reproduce these through the installed MCP client with temporary config/
state, known visible/withheld tools and foreign operation records; assert no local-query HMC traffic,
no secret content, correct pagination and denials. Remove only the fixture config/store afterwards.
`tools.invoke` requires a separately authorized target fixture; its dispatch proof does not promote
the target's hardware evidence. `command.run` similarly retains an open native dispatch obligation.

## Historical failed observations and current scope

| Operation | Disposition and remaining obligation |
| --- | --- |
| `lpar_profile.restore` | Failed `st10-profile-backup-restore` remains verbatim. Its unchanged-partition assertion conflicts with the already documented type3 `resource_config` reset, so it is not a promised unchanged-runtime contract. The tool requires system-wide acknowledgment/audited ownership override and documents manual current-profile reapplication. Still unverified: actual profile-content restoration, all three modes and independently confirmed reapply recovery. Use a scratch system/window, deliberately changed disposable profiles and independent before/after profile/partition reads; recovery failure is a failure, not an unavailable case. |
| `lpar.set_msp` | Failed `st10-msp-round-trip` remains. Current VIOS-aware resolver/ownership code and its name/UUID/foreign/non-VIOS tests address the historical lookup defect; repeat the real flag round trip and restore the VIOS baseline before claiming native success. |
| `user.modify` | Failed `st11-user-lifecycle` remains. Current code resends UserID/authentication type and empty password element, and accepts non-negative integer timeout minutes; automated serializer/postcondition tests cover it. Old missing-scope prose described the repaired rejection. Repeat disposable-user modification, independent timeout/unchanged-field reads and user cleanup; no source fix is a native pass. |
| `provision.lpar` | Failed `st41-lpar-power` remains. Dedicated slots are refused before provisioning; the catalog is partial and #1225 owns profile-first activation. SR-IOV/vNIC variants require their own admitted envelope and native proof. A later API implementation must remove the denial with real ownership-after-activation evidence; the baseline does not call this missing capability implemented. |

The other partial or refused variants remain explicit in maturity.json. They may satisfy this
verification baseline as honest restrictions, but continue to block full implementation of their
reference capability. The baseline accepts evidence accounting, not full-reference completion or
native fitness. No known failing required behavior is converted into an unavailable-environment gap.

## Backup artifact extension (#1726)

The four assigned REST backup/list/delete/restore rows retain all 78 source units.
Backup file/force and restore types 1–3 reuse the existing SSH implementation.
`forceKeyStore` maps to explicit `force_keystore` with stdout/stderr warnings
preserved. Initialization/type 4 remains refused; restore maturity is partial
with `initialize-type4-denied` recorded as missing native behavior, not dropped
from the denominator. Dedicated list/delete use the documented managed-system
jobs and exact HMC file names. `rmprofdata` stays allocated to #698. New artifact
operations have no native implementation observations; source-format capture
does not promote them. See ADR 0244 and the artifact-only live scenario.
