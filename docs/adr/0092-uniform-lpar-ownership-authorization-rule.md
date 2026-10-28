# ADR 0092: Uniform ownership authorization rule for LPAR-mutating operations

## Status

Accepted (2026-08-25)

> Amended by [ADR 0276](0276-lpar-virtual-tpm-configuration.md) for the new VIOS vTPM update branch only; unrelated VIOS policy is unchanged.

## Context

ADR 0011 established the advisory ownership protocol: a token
(`[hmc-mcp owner:<agent_id> created:<date>]`) stamped into the partition
description, and `authorize_lpar_mutation` (`src/hmc_mcp/operations/lpar/ownership.py:152`)
to reject a mutation of a partition another agent owns. ADR 0011 named the tools
that *stamp* and *read* the token. It never said which mutations must *check* it.

The result is coverage set by whoever wrote the operation. Inside one module,
`delete_lpar` (`operations/lpar/core.py:680`, guard at `:807`), `rename_lpar` (`:904`,
guard at `:920`) and the boot-order operations (guards at `:1422`, `:1476`) call the
guard; `power_lpar` (`:827`) — exported from the same facade — guards only
when the operator opts in (§4). Across
modules the split is wider still: the PCIe, SR-IOV, vNIC and minimum-affinity
operations guard; the adapter, storage, provisioning, DLPAR and LPM operations do
not. Nothing records why, so a maintainer adding a new mutating operation has no
rule to consult.

This matters most for a library consumer. #218's server access policy — now
implemented — governs MCP tool dispatch, and its non-goals exclude the CLI and the
supported Python API by design. For a consumer of `hmc_mcp.api`, ADR 0011
ownership is therefore the *only* authorization boundary that applies — and §3
below shows it reaching well under half the LPAR-mutating operations that exist,
with two of the guarded ones currently unable to mutate anything at all.

The guard is not free. `authorize_lpar_mutation` reads the token through
`ssh_commands.get_lpar_description` (`src/hmc_mcp/ssh_commands.py:1577`), which
calls `ssh.run_hmc_command` (`src/hmc_mcp/ssh.py:34`), which opens a **fresh
`asyncssh.connect`** inside the call (`src/hmc_mcp/ssh.py:38`). There is no
connection pool and no reuse: every guarded call pays one full SSH login and
teardown. ADR 0071 added a bulk ownership read over REST but deliberately left the
single-LPAR authorization path on SSH, recording that move as a separate decision.

A uniform "guard everything" rule would therefore charge an SSH login to the
highest-frequency call in the package. A uniform "guard nothing new" rule leaves
the facade consumer unprotected. The rule has to distinguish classes of mutation.

## Decision

### 1. What this ADR governs

An operation is **LPAR-mutating** when both hold:

1. it names an **existing** partition whose resource type is `LogicalPartition`,
   and
2. it changes that partition's existence, identity, configuration, resource
   shape, virtual-device attachments, placement, or run state.

The guard belongs in the **operations layer**, not in a tool body or CLI command
(ADR 0013), so that every entry path — MCP tool, `hmc` CLI, and `hmc_mcp.api` —
crosses the same check.

The test in clause 1 is the **resource type**, not the mere presence of a
selector. `VirtualIOServer` partitions are out of scope: ADR 0011 never stamps
them, so there is no token to authorize against. That excludes the VIOS mutations —
`power_vios`, `hmc_delete_vios`, `hmc_install_vios`, `hmc_restore_vios`,
`hmc_backup_vios`, `hmc_vios_update` — and also `hmc_install_vios_by_lpar_selector`, despite its
name, because `installios` requires its `-p` partition to be of type Virtual I/O
Server. #366 moved that determination's subject out of the tool body into
`operations/install.py`, where §3.4a classifies the install operation. ADR 0203 later
retired the LPAR-selector tool and its operation.

**One VIOS-targeting operation is governed (#1318).** `configure_lpar_msp` sets `msp`,
which the HMC accepts only on a Virtual I/O Server, so its target can never be a
`LogicalPartition`. It resolves the target through the selected managed system's
`VirtualIOServer` feed — a name by exact `PartitionName`, a UUID by case-insensitive
match — which establishes containment as ADR 0094 does for LPARs, and refuses a selector
outside that feed before any SSH command. The resolved VIOS then crosses §2's rule
through `authorize_lpar_mutation` unchanged: a foreign or malformed stamp is refused, an
unstamped description proceeds (ADR 0011 never stamps a VIOS, so this is the common
case), and the §5 override skips the read and is audited. The tool still declares
`target_kind="lpar"`, so a #218 access-policy grant names the VIOS under `lpar` targets;
a `vios` grant does not authorize it. The other VIOS mutations above stay out of scope;
this paragraph does not extend to them.

Also out of scope: read operations; managed-system-, user- and cluster-scoped
mutations that name no partition (`create_volume_group`, `create_media_repository`,
`power_system`, `set_sriov_adapter_mode`, `set_pcm_preferences` — which rejects
every category but `ManagedSystem` at `operations/metrics/pcm.py:52` — and the user and
cluster tools); and operations that *create* a partition, which have no prior
owner to check and stamp instead (ADR 0011, ADR 0014).

**This section supersedes #369 on `upload_iso`.** #369 lists it as unguarded;
`upload_iso` (`operations/storage.py:445`) names a VIOS, a volume group and a
media name, and no partition at all. It is out of scope here, and that is a
decision, not an omission.

### 2. The three classes and their guard rules

**Destructive** — irreversible, or destroys state a consumer cannot reconstruct
from what remains.
→ **Guard unconditionally.** No configuration switch, no opt-out. The only bypass
is the per-call operator override in §5.

**Reconfiguring** — changes the partition's resource shape, device attachments or
placement. Reversible in principle, but reverting requires knowing the prior state,
which the caller may not have recorded.
→ **Guard unconditionally.** These are low-frequency operations; the guard's cost
(§4) is noise against the cost of the mutation itself.

**Operational** — changes run state only. Reversible by the inverse call with no
knowledge of prior configuration, and the highest-frequency calls a non-interactive
orchestrator makes.
→ **Decide explicitly per operation, with the cost stated.** §4 records the one
decision in this class.

### 3. Classification

Exhaustive as of `b41e658`, and extended in place by §6 as later PRs add operations —
each such row names the issue that added it. ADR 0119's executable inventory in
`tests/unit/test_adr_0092_citations.py` now enforces the extension: every discovered mutator in
the governed operation modules must be classified, and guarded entries must reach an existing
ownership helper. Guard-call sites are `authorize_lpar_mutation` unless
noted. "Unguarded" is a defect against this ADR, not a standing exemption; standing
exemptions are §3.4 only. The **Tracking** column names the issue that closes each
defect; a row with no issue must read `none yet` rather than be left blank, so the
gap is visible. No row reads `none yet`, and #369 must not close while one does.

The same obligation covers the citations: a PR that moves a definition cited in
§3, §4 or §5 re-verifies the cited file and symbol in the same change. §Context above is
exempt — it is the survey as it stood at `b41e658`, and its citations are read
against that commit rather than maintained forward.

#### 3.1 Destructive — guard unconditionally

| Operation | Location | Status | Tracking |
|---|---|---|---|
| reset_lpar_memory_statistics (planned) | Planned by ADR 0288; implementation pending | Planned guarded LPAR/VIOS counter-history reset; ADR0288 narrowly supersedes the legacy scope for this operation only; standard audited ownership_override=False and MCP all-targets remain required. Add verified source citations with implementation. | #1777 / ADR 0288 |
| `configure_lpar_security_policy` | `operations/lpar/security_policy.py::configure_lpar_security_policy` | guarded (`operations/lpar/security_policy.py::configure_lpar_security_policy`, via contained LPAR/VIOS authorization helpers); explicit settings plus existing operator confirmation declare consent | #1765 / ADR 0277 |
| `configure_lpar_vtpm` | `operations/lpar/vtpm.py::configure_lpar_vtpm` | guarded (`operations/lpar/vtpm.py::configure_lpar_vtpm`, via contained LPAR/VIOS authorization helpers); destructive enabled disable additionally requires independent consent | #1764 / ADR 0276 |
| `delete_lpar` | `operations/lpar/core.py::delete_lpar` | guarded (`operations/lpar/core.py::delete_lpar`) | — |
| `decommission_lpar` | `operations/lpar/decommission.py::decommission_lpar` | guarded (`operations/lpar/decommission.py::decommission_lpar`, via `authorize_decommission_lpar_ownership_snapshot`) | — |
| `rename_lpar` | `operations/lpar/core.py::rename_lpar` | guarded (`operations/lpar/core.py::rename_lpar`) | — |
| `set_lpar_ownership_description` | `operations/lpar/ownership.py::set_lpar_ownership_description` | guarded (`operations/lpar/ownership.py::set_lpar_ownership_description`) | — |
| `synchronize_lpar_profile` | `operations/lpar/configuration.py::synchronize_lpar_profile` | guarded (`operations/lpar/configuration.py::synchronize_lpar_profile`) | — |
| `restore_system_lpar_profiles` | `operations/lpar/configuration.py::restore_system_lpar_profiles` | guarded before SSH by `_authorize_system_lpar_profile_restore` (`operations/lpar/ownership.py::_authorize_system_lpar_profile_restore`) | #449 |

`rename_lpar` is Destructive rather than Reconfiguring because the partition name
is the identity every consumer addresses, and the ownership token itself is keyed
by name — a rename silently detaches both. `set_lpar_ownership_description`
overwrites the token, so it is destructive of the protocol's one artifact.
`hmc_sync_lpar_profile` overwrites a named profile with the running configuration;
the previous profile definition is gone.

`hmc_sync_lpar_profile` delegates to the guarded operation above; the SSH helper is
only the transport boundary. `hmc_restore_lpar_profiles` delegates its mutation to
`restore_system_lpar_profiles`, which checks every current partition reported by the
selected system's ADR 0071 bulk ownership feed before opening the restore SSH path.

That feed cannot reveal a partition definition present only in the opaque HMC-side
backup. The explicit `system_wide_restore_approved=true` acknowledgement remains
accident prevention only; it does not authorize that unknowable remainder. A call
without `ownership_override=true` therefore fails closed after a clean current-feed
check, explaining that only the audited override can cover backup-only definitions.

The bulk guard takes `ownership_override: bool = False`. Without an override, an
unreadable feed or a row missing a partition name or string-or-absent description
fails closed before SSH. Foreign owners and malformed `[hmc-mcp` tokens are all
handled by the existing partition-specific denial and audit at the first blocker. An
absent token or unrelated free text retains ADR 0011's unowned semantics but does not
remove the opaque-backup refusal. An approved override skips the feed, emits one
existing `ownership-override` event for the system with `lpar="*"`, and proceeds; the
wildcard records the operation's actual scope.

#### 3.2 Reconfiguring — guard unconditionally

| Operation | Location | Status | Tracking |
|---|---|---|---|
| `configure_lpar_reporting_settings` | `operations/lpar/configuration.py::configure_lpar_reporting_settings` | guarded (`operations/lpar/configuration.py::configure_lpar_reporting_settings`, via `resolve_and_authorize_lpar_names`) | — |
| `configure_lpar_settings` | `operations/lpar/configuration.py::configure_lpar_settings` | guarded (`operations/lpar/configuration.py::configure_lpar_settings`, via `resolve_and_authorize_lpar_names`) | — |
| `set_lpar_accelerator_qos` | `operations/lpar/accelerator.py::set_lpar_accelerator_qos` | guarded (`operations/lpar/accelerator.py::set_lpar_accelerator_qos`, via `authorize_lpar_mutation` after selected-system containment) | #1781 / ADR 0305 |
| `set_lpar_boot_order` | `operations/lpar/boot_order.py::set_lpar_boot_order` | guarded (`operations/lpar/boot_order.py::set_lpar_boot_order`) | — |
| `clear_lpar_boot_order` | `operations/lpar/boot_order.py::clear_lpar_boot_order` | guarded (`operations/lpar/boot_order.py::clear_lpar_boot_order`) | — |
| `assign_dedicated_pcie_slot` | `operations/virtualization/pcie.py::assign_dedicated_pcie_slot` | guarded (`operations/virtualization/pcie.py::assign_dedicated_pcie_slot`, via `_authorize_pcie_profile_request`) | — |
| `unassign_dedicated_pcie_slot` | `operations/virtualization/pcie.py::unassign_dedicated_pcie_slot` | guarded (`operations/virtualization/pcie.py::unassign_dedicated_pcie_slot`) | — |
| `assign_sriov_logical_port` | `operations/virtualization/pcie.py::assign_sriov_logical_port` | guarded (`operations/virtualization/pcie.py::assign_sriov_logical_port`, via `resolve_and_authorize_lpar_names`) | — |
| `unassign_sriov_logical_port` | `operations/virtualization/pcie.py::unassign_sriov_logical_port` | guarded (`operations/virtualization/pcie.py::unassign_sriov_logical_port`, via `resolve_and_authorize_lpar_names`) | — |
| `add_vnic` | `operations/virtualization/vnic.py::add_vnic` | guarded (`operations/virtualization/vnic.py::add_vnic`, via `_preflight_add` → `resolve_and_authorize_lpar_names`) | — |
| `remove_vnic` | `operations/virtualization/vnic.py::remove_vnic` | guarded (`operations/virtualization/vnic.py::remove_vnic`) | — |
| `set_minimum_affinity_policy` | `operations/affinity/ssh.py::set_minimum_affinity_policy` | guarded (`operations/affinity/ssh.py::set_minimum_affinity_policy`) | — |
| `set_lpar_processors` | `operations/lpar/dlpar.py::set_lpar_processors` | guarded (`operations/lpar/dlpar.py::set_lpar_processors`, via `_apply_dlpar_document` → `_resolve_and_authorize_lpar`) | — |
| `set_lpar_memory` | `operations/lpar/dlpar.py::set_lpar_memory` | guarded (`operations/lpar/dlpar.py::set_lpar_memory`, via `_apply_dlpar_document`) | — |
| `change_lpar_processors` | `operations/lpar/processors.py::change_lpar_processors` | guarded (`operations/lpar/processors.py::change_lpar_processors`, via `authorize_lpar_mutation` after selected-system containment) | #1778 / ADR 0296 |
| `change_lpar_memory` | `operations/lpar/memory.py::change_lpar_memory` | guarded (`operations/lpar/memory.py::change_lpar_memory`, via `authorize_lpar_mutation` after selected-system containment) | #1774 / ADR 0285 |
| `move_lpar_memory` | `operations/lpar/memory_move.py::move_lpar_memory` | guarded (`operations/lpar/memory_move.py::move_lpar_memory`, via `authorize_lpar_mutation` for both contained endpoints) | #1775 / ADR 0286 |
| `apply_lpar_pcie_assignments` | `operations/lpar/assignments.py::apply_lpar_pcie_assignments` | guarded via `apply_validated_lpar_pcie_assignments` delegates (`operations/virtualization/pcie.py::assign_dedicated_pcie_slot`, `operations/virtualization/pcie.py::assign_sriov_logical_port`, `operations/virtualization/vnic.py::add_vnic`) | — |
| `add_network_adapter` | `operations/virtualization/adapters.py::add_network_adapter` | guarded (`operations/virtualization/adapters.py::add_network_adapter`) | #372 |
| `add_vscsi_adapter` | `operations/virtualization/adapters.py::add_vscsi_adapter` | guarded (`operations/virtualization/adapters.py::add_vscsi_adapter`) | #372 |
| `add_vfc_adapter` | `operations/virtualization/adapters.py::add_vfc_adapter` | guarded (`operations/virtualization/adapters.py::add_vfc_adapter`) | #372 |
| `delete_adapter` | `operations/virtualization/adapters.py::delete_adapter` | guarded (`operations/virtualization/adapters.py::delete_adapter`) | #372 |
| `map_storage` | `operations/storage/resources.py::map_storage` | guarded (`operations/storage/resources.py::map_storage`) | #372 |
| `attach_disk_to_lpar` | `operations/lpar/provision.py::attach_disk_to_lpar` | guarded (`operations/lpar/provision.py::attach_disk_to_lpar`) | #372 |
| `mount_optical_media` | `operations/storage/resources.py::mount_optical_media` | guarded (`operations/storage/resources.py::mount_optical_media`) | — |
| `unmount_optical_media` | `operations/storage/resources.py::unmount_optical_media` | guarded (`operations/storage/resources.py::unmount_optical_media`) | — |
| `migrate_lpar` | `operations/lpar/migration.py::migrate_lpar` | guarded after optional validation and before migration submission (`operations/lpar/migration.py::migrate_lpar`) | #373 |
| `migrate_lpar_with_affinity_preflight` | `operations/lpar/migration.py::migrate_lpar_with_affinity_preflight` | guarded (`operations/lpar/migration.py::migrate_lpar_with_affinity_preflight`) | #373 |
| `abort_lpar_migration` | `operations/lpar/migration.py::abort_lpar_migration` | guarded (`operations/lpar/migration.py::abort_lpar_migration`) | #373 |
| `recover_lpar_migration` | `operations/lpar/migration.py::recover_lpar_migration` | guarded (`operations/lpar/migration.py::recover_lpar_migration`) | #373 |
| `remote_restart_lpar` | `operations/lpar/migration.py::remote_restart_lpar` | guarded (`operations/lpar/migration.py::remote_restart_lpar`) | #373 |

`mount_optical_media` and `unmount_optical_media` became facade exports in #363,
so they are Domain A callables (§5) as well as MCP tools — the guard is the only
authorization a `hmc_mcp.api` caller of either would cross. `detach_optical_mapping`
is absent from this table because #362 removed it: it was a duplicate alias of
`unmount_optical_media`, and one operation is now one row.

`migrate_lpar` is one function with a `validate: bool = False` keyword. Only the
`validate=False` branch migrates; `validate=True` submits an LPM validation job that
changes nothing. #373 guards the migrating branch. The validating branch is not
separately exempt — the function is classified here, once, as Reconfiguring.

`assign_dedicated_pcie_slot` / `unassign_dedicated_pcie_slot` are guarded but
currently inert: `_authorize_pcie_profile_request` raises
`PcieAssignmentUnavailableError` unconditionally at `operations/virtualization/pcie.py::_authorize_pcie_profile_request`, right
after the guard, so neither can mutate anything at this commit. They count as
correctly-shaped coverage, not as protection of a live mutation.

The remaining direct entry points and their guard state are:

| Entry point | Location | Status | Tracking |
|---|---|---|---|
| `configure_lpar_msp` | `operations/lpar/configuration.py::configure_lpar_msp` | guarded (`operations/lpar/configuration.py::configure_lpar_msp`, via `operations/lpar/configuration.py::_resolve_and_authorize_vios_names`); targets a VIOS (§1) | #1318 |
| `configure_lpar_processor_compatibility` | `operations/lpar/configuration.py::configure_lpar_processor_compatibility` | guarded (`operations/lpar/configuration.py::configure_lpar_processor_compatibility`) | — |
| `hmc_modify_lpar` | `server_tools/lpar/lifecycle.py::hmc_modify_lpar` | guarded by `operations/lpar/dlpar.py::modify_lpar` before any write | — |
| `hmc lpar modify` (CLI) | `cli_commands/lpar/modify.py::lpars_modify` | guarded by `operations/lpar/dlpar.py::modify_lpar` before any write | — |
| `detach_storage_mapping` | `operations/storage/resources.py::detach_storage_mapping` | resolves the mapping's client LPAR and guards it before deletion (`operations/storage/resources.py::detach_storage_mapping`) | #448 |

`hmc_dlpar_proc` and `hmc_dlpar_mem` were rows in this table at `b41e658`. #365
extracted `set_lpar_processors` and `set_lpar_memory` from those tool bodies and
guarded both, so the pair left Domain B for the Domain A table above — the §6
transition this table exists to drive. The tools still exist and still carry the
same names and behaviour; they delegate. ADR 0094 records how the guard obtains a
managed-system name when the caller omits the optional selector.

`modify_lpar` closes the sharpest gap this ADR identified. The public operation
resolves and authorizes the partition once before its ordered rename, resource, and
assignment workflow (`operations/lpar/dlpar.py::modify_lpar`). Both the MCP tool and CLI command
delegate their complete workflow to it, so an adapter cannot accidentally place one
kind of modification on the other side of the authorization boundary. Focused tests
prove that a foreign-owned resources-only modify is rejected before the HMC write
through both entry points, and that an approved override skips the ownership read.

#### 3.3 Operational — decide explicitly

| Operation | Location | Status | Tracking |
|---|---|---|---|
| `submit_power_on_lpar` | `operations/lpar/activation.py::submit_power_on_lpar` | guarded when opted in (`operations/lpar/activation.py::_resolve_power_target`, via `resolve_and_authorize_lpar_mutation`); §4 | #371 |
| `power_off_lpar` | `operations/lpar/activation.py::power_off_lpar` | guarded when opted in (`operations/lpar/activation.py::_resolve_power_target`, via `resolve_and_authorize_lpar_mutation`); §4 | #371 |

The activation and shutdown operations are the whole class. Both `hmc_power_on_lpar`
(`server_tools/lpar/lifecycle.py::hmc_power_on_lpar`) and `hmc_power_off_lpar`
(`server_tools/lpar/lifecycle.py::hmc_power_off_lpar`) delegate to them, and so does the
CLI, so one decision covers every entry path.

#### 3.4 Standing exemptions

Operations registered `mutate` or `destructive` that ship without a guard call.
Together with §3.1–§3.3 this is the list the #369 enforcement test reads (§5). The
remaining rows are outside §1's definition; issue #449 removed the final §3.4b
LPAR-mutating exemption.

**3.4a — outside §1's definition** (no ownership decision to make)

| Operation | Reason |
|---|---|
| `create_and_stamp_lpar` (`operations/lpar/core.py::create_and_stamp_lpar`) | Creates the partition. No prior owner exists to authorize against; it stamps the token instead (ADR 0011). |
| `provision_lpar` (`operations/lpar/provision.py::provision_lpar`) | Composite create-and-stamp. Its post-create legs act on the partition it just created and owns, inside one workflow. |
| `deploy_partition_template` (`operations/templates/core.py::deploy_partition_template`) | Creates the partition and stamps it per ADR 0014. |
| `hmc_capture_lpar_console` (`server_tools/console.py::hmc_capture_lpar_console`) | Holds a console session and releases it. Changes no partition existence, configuration or run state. |
| `hmc_backup_lpar_profiles` (`server_tools/lpar/profiles.py::hmc_backup_lpar_profiles`) | Reads every profile and writes an HMC-side backup file; it does not mutate a partition or profile. |
| `hmc_migrate_validate_lpar` (`server_tools/lpar/migration.py::hmc_migrate_validate_lpar`) | Calls `validate_lpar_migration`, which submits an LPM validation job and changes nothing. The mutating migration operation has its own guard. |
| `install_vios` (`operations/vios/install.py::install_vios`) | Added by #366. `installios` requires its `-p` partition to be a Virtual I/O Server, which ADR 0011 never stamps, so there is no ownership token to authorize against. After resolving through the `VirtualIOServer` feed, both name and UUID selectors are checked through the resolved `VirtualIOServer` resource for Virtual I/O Server type and `not activated` state before submission. |

**3.4b — no LPAR-mutating standing exemptions remain.** Issue #449 moved
`restore_system_lpar_profiles` to §3.1. Because a backup file does not reveal which
profiles it contains, the guard checks every partition currently present on the
selected system rather than accepting a caller-supplied subset. Only the explicit
audited ownership override covers definitions present only in the backup. The
system-wide acknowledgement remains accident prevention, and the managed-system grant
remains an additional MCP boundary; neither substitutes for the ownership check on
direct CLI or Python entry paths.

**Disposition of #366.** #366 shipped as a layering extraction only — it moved the
tool bodies into `operations.vios.install` unchanged and added no LPAR-capable install
path. Its `LogicalPartition`-selector operation could be handed a `LogicalPartition`
selector but could never submit, and [ADR 0203](0203-retire-install-vios-by-lpar-selector.md)
retired it (#1371). The remaining install operation, `install_vios`, resolves only
`VirtualIOServer`-feed targets, so no mutation of a `LogicalPartition` is reachable
through it whatever `installios` accepts, and ADR 0070's assumption 5 no longer bears on
this classification. §6's recording obligation is discharged in §3.4a. The condition
reopens only for an install path that can target a `LogicalPartition`: that path is
Destructive under §2, and §6 requires it to be classified and guarded in the PR that
introduces it.

### 4. The `power_lpar` decision

**`power_lpar` is guarded only when the operator opts in. The default is off.**

The setting is a new `HMCConfig` field, `authorize_power_operations` — environment
variable `HMC_AUTHORIZE_POWER_OPERATIONS`, TOML profile key
`authorize_power_operations` — defaulting to `false`. It governs `power_lpar` and
nothing else: it is not a switch for the Operational *class*. A second Operational
operation, if one is ever added, gets its own §3.3 row and its own decision under
§2, and may or may not share this flag.

When true, `power_lpar` calls `authorize_lpar_mutation` with the same
`ownership_override` semantics as its siblings in §3.1 and §3.2. When false it does
not, and its docstring carries the ADR 0011 advisory language telling the caller to
read the description first.

**The selector stays optional.** `power_lpar` is one of the operations ADR 0063
decided **"Optional, not required"** for, and this flag does not change that. An
earlier revision of #371 made the selector required when the flag was on, because
the ownership token is read per managed system and nothing then derived a
partition's owning system. ADR 0094 (#365) removed that constraint: its
`_resolve_and_authorize_lpar` derives the owning system by the same bounded parent
discovery `find_partition_by_name` already applies to a fleet-ambiguous name — the
100-system cap, the timeout, and the "supply managed-system scope" remedy. #371
calls that shared chain rather than adding a second convention in the same module,
so ADR 0063 is left intact and no caller that omits the selector breaks when the
flag goes on.

Supplying the selector is still worth it: it replaces the fleet walk with one
managed-system read. `hmc_power_on_lpar` / `hmc_power_off_lpar` already accepted
it, and `hmc-mcp lpars power-on` / `power-off` gained `--system` so the remedy the
discovery failure message names is reachable from the CLI too.

That shared chain also closes the gap where a partition UUID paired with a
mismatched selector would read the token off a system the partition does not live
on: `_verify_partition_on_system` rejects the pairing, and the discovery branch
matches by UUID. PR #558 applied this containment to both the optional-selector
and required-selector guarded chains; issue #462's duplicate-name regression
coverage records the required-selector boundary.

**Deriving the system from the partition was surveyed and is available.** Whether
a `LogicalPartition` names its parent managed system was the open question in #466.
A live survey answered it: in the single-entry read, the per-system feed and the
fleet-wide feed, every partition on V10R3 and V11R2 — `not activated` and
`open firmware` ones included — carries an `AssociatedManagedSystem` `href` that is
the bare `ManagedSystem/<uuid>` of its parent (the base `V1_0` schema; a migrating
partition was not sampled). The evidence is
`docs/workflow/lpar-parent-system-survey-466.md`. It rules out "the HMC does not say",
not the discovery walk: replacing the walk with the link is a code change behind
`_discover_owning_system` that this ADR does not make, and it is recorded as its
own decision when taken.

**The cost, stated.** Guarding `power_lpar` costs **one SSH login plus two REST
GETs** on every call that does not carry `ownership_override=True`.

The SSH login is the chain `authorize_lpar_mutation` (`operations/lpar/ownership.py::authorize_lpar_mutation`) →
`ssh.profiles.get_lpar_description` (`ssh/profiles.py::get_lpar_description`) →
`ssh.transport.run_hmc_command` (`ssh/transport.py::run_hmc_command`) → a fresh `asyncssh.connect` (`ssh/transport.py::run_hmc_command`) per
invocation. `run_hmc_command` opens and closes its connection inside the call; the
only long-lived SSH connection in the package is the console path (`ssh/transport.py::open_hmc_connection`),
which commands do not share. There is no pool and no reuse. (With
`ownership_override=True` the guard returns at `operations/lpar/ownership.py::authorize_lpar_mutation` after
auditing, before the read — so **`authorize_lpar_mutation` itself** pays nothing.
A caller that resolves the ownership names first still pays the two REST GETs
below, because the audit record for an approved override names the system and the
partition. #371 corrected an earlier reading of this parenthetical that took the
override path to be free end to end; ADR 0094's `_resolve_and_authorize_lpar`
narrows it further — it skips the fleet walk on an override and pays one name read.)

> **Amended by #1135** (2026-09-29): the function this section calls
> `_resolve_and_authorize_lpar` (here and in the §3 table) is now
> `resolve_and_authorize_lpar_mutation`
> (`operations/lpar/ownership.py::resolve_and_authorize_lpar_mutation`). On an override
> it returns through `_authorize_override`
> (`operations/lpar/ownership.py::_authorize_override`), which resolves the partition and
> reads its name once, and never reaches `resolve_lpar_ownership_names`. So the optional-
> selector chain on the power path pays one partition-name GET, no SSH command and no
> managed-system name read. The two REST GETs quoted above are paid on an override only by
> callers of `resolve_and_authorize_lpar_names`
> (`operations/lpar/ownership.py::resolve_and_authorize_lpar_names`), which the vNIC, PCIe
> and affinity operations use.

> **Amended by #1144** (2026-09-29): the #1135 note's list of callers of
> `resolve_and_authorize_lpar_names` is incomplete. It is also called by the LPAR
> configuration operations (`operations/lpar/configuration.py`) and by
> `set_lpar_ownership_description`
> (`operations/lpar/ownership.py::set_lpar_ownership_description`), so those paths pay the
> same two REST GETs on an override.

The two REST GETs come from `resolve_lpar_ownership_names`
(`operations/lpar/ownership.py::resolve_lpar_ownership_names`), which the guard needs to turn UUIDs into the CLI names
the SSH command takes. It calls `_resolve_system_name` (`operations/lpar/ownership.py::_resolve_system_name`) → `hmc.get_managed_system`
(`operations/lpar/ownership.py::_resolve_system_name`) and `hmc.get_logical_partition` (`operations/lpar/ownership.py::resolve_lpar_ownership_names`) **unconditionally** — supplying
`system_name_or_uuid` does not avoid either, as `rename_lpar` (`operations/lpar/core.py::rename_lpar`) and
`_authorize_pcie_profile_request` (`operations/virtualization/pcie.py::_authorize_pcie_profile_request`) already demonstrate.

The two REST reads are the same order of work `power_lpar` already does
(`resolve_lpar_uuid` and `get_quick_property` inside `operations/lpar/activation.py::submit_power_on_lpar`).
**The SSH login is the outlier**, and it is the part of the cost this decision turns
on.

**Why off.** A power-cycling orchestrator is the highest-frequency caller of
`power_lpar` in the package, and it is the caller that would pay this cost on every
call. Defaulting on would make the package materially slower for its main
non-interactive consumer in exchange for protecting the one class that is trivially
reversible by the inverse call, with no prior state to reconstruct. Defaulting off
with an explicit opt-in lets an operator on a shared HMC turn it on and accept the
cost knowingly.

**Reconsider when the SSH login is gone.** The checkable condition is: *guarding
`power_lpar` no longer opens an SSH connection*. Two concrete triggers, either of
which satisfies it:

1. `authorize_lpar_mutation` moves its description read onto the ADR 0071 REST
   path. ADR 0071 established that the description is fully inlined in the bulk
   `LogicalPartition` list feed and explicitly deferred moving the authorization
   read as "a separate decision".
2. The SSH transport gains connection reuse, so `run_hmc_command` no longer opens a
   connection per call.

Either trigger leaves the two REST reads in place. That is deliberate: they are
comparable to work `power_lpar` already performs, so once the SSH login is gone the
argument for defaulting off no longer holds and the default should flip to on. That
revisit needs no new investigation — this paragraph is the finding. Anyone landing
either change should amend this section in the same PR.

### 5. The exemption mechanism

Two distinct mechanisms, deliberately not interchangeable:

- **Per-call operator override.** Every guarded operation takes
  `ownership_override: bool = False`. When true the guard is bypassed for that one
  call and the bypass is audited by `_audit_lpar_ownership_override`
  (`operations/lpar/ownership.py::_audit_lpar_ownership_override`). This is an operator-approved exception to a *single*
  mutation. It is not an exemption from this ADR, and an operation that accepts it
  is still classified and still guarded.
- **Standing exemption.** A row in §3.4b with a recorded reason. This is the only
  way an operation that *is* LPAR-mutating may ship without a guard call. (§3.4a
  rows are not LPAR-mutating at all; they are listed so the inventory stays
  complete, and a test may read the two sub-tables as one allowlist.)

#### The predicate, stated so a test can implement it

An LPAR-mutating operation satisfies this ADR if and only if it **reaches a guard**
or **appears in §3.4**. Every part of that — which operations are in scope, what a
guard is, and what "reaches" means — needs definition, or the test cannot be
written.

**Enumeration domain.** Three domains, checked separately because they are reachable by
different means.

- *Domain A — the facade.* Every **function** exported from `hmc_mcp.api.__all__`
  whose definition lives in `src/hmc_mcp/operations_*.py` — the `inspect.isfunction`
  filter `tests/unit/test_public_api.py::test_public_api_does_not_reexport_domain_operations` already applies, not "every callable",
  which would drag in every exported dataclass and error type. This is the domain
  #369's acceptance criterion names, and the one that matters most, because a
  `hmc_mcp.api` consumer crosses no other authorization boundary (§7).
- *Domain B — entry points with no operation.* Every function registered by `@tool`
  with effect `mutate` or `destructive` that meets §1 and whose mutation does not
  route through a Domain A callable. These are §3.1's and §3.2's tool rows plus the
  CLI row. They cannot be fixed by adding a guard call: §6 requires an operation
  first, at which point they become Domain A and leave Domain B. **Domain B being
  empty is the end state**; until then each row needs a Tracking issue.
- *Domain C — managed-system-wide LPAR mutations.* The explicit pair
  `hmc_restore_lpar_profiles` → `restore_system_lpar_profiles`. Its selector is a
  managed system because every current partition and opaque backup-only definition is
  in scope, so `target_kind="lpar"` cannot enumerate it. The focused #449 tests execute
  the tool-to-operation delegation and the operation-to-guard denial path. While the
  §3 row is present, the citation guard checks that it names the real operation; #369
  owns exhaustive row-presence enforcement. A future managed-system-wide LPAR mutation
  must add its own row and focused reachability proof.

**Domain A is enumerated, then partitioned — not filtered by judgement.** "Meets §1"
is a semantic call no test can compute, so the test must not try. Instead every
Domain A callable must fall into exactly one of three sets, and their union must
equal the enumeration:

1. classified in §3.1, §3.2 or §3.3;
2. exempt in §3.4;
3. **not LPAR-mutating** — a set the test maintains explicitly, alongside the frozen
   public-API digest this repo already keeps in `tests/unit/test_public_api.py`.

Set 3 is where `capacity_report`, `list_adapters`, `read_lpar_boot_order` and every
other non-mutating export live. Membership is a deliberate, reviewed act: a new
facade export lands in none of the three sets, so the test fails until someone
classifies it. That is what makes the check non-circular — a filter on "meets §1"
could only ever re-check operations §3 already names, and by construction could never
catch the new unclassified operation §6 exists to prevent.

Domain B needs no such partition: `@tool(effect=…, target_kind="lpar")` is
machine-readable and exhaustively enforced by the existing registry test, so the
domain enumerates itself. Domain C is deliberately explicit because managed-system
target metadata cannot distinguish an LPAR-profile restore from an ordinary
managed-system mutation.

**What counts as a guard.** Exactly three callables:
`authorize_lpar_mutation` (`operations/lpar/ownership.py::authorize_lpar_mutation`) and
`authorize_decommission_lpar_ownership_snapshot` (`operations/lpar/ownership.py::authorize_decommission_lpar_ownership_snapshot`), plus
the system-wide `_authorize_system_lpar_profile_restore` (`operations/lpar/ownership.py::_authorize_system_lpar_profile_restore`).
Nothing else, and no new one without amending this list. The system-wide guard must
reach `authorize_lpar_ownership_description` for every complete current-feed row;
calling that parser on an arbitrary value does not independently count as a guard.

**"Reaches" means call-graph reachability, not a direct call.** Six operations in §3
guard through a private helper — `_authorize_pcie_profile_request`, `_resolve_lpar`,
`_resolve`/`_preflight_add`, and `apply_lpar_pcie_assignments`'s delegation to the
PCIe, SR-IOV and vNIC operations. A test that looks for a direct call in the
function body fails all of them and would drive maintainers to inline the guard for
the test's benefit. The check is reachability from the operation to a guard callable
within `src/hmc_mcp/`.

**Two assertions, not one.** The partition and the predicate answer different
questions, and the test needs both:

- **(a) The partition is total.** Every Domain A function is in exactly one of the
  three sets. This catches a new or reclassified export, and it can be green today.
- **(b) Every row whose Status reads *guarded* still reaches a guard.** This catches
  regression on the coverage that exists. Rows whose Status reads **unguarded** are
  expected-unguarded — they are the defects §3 records — so (b) skips them until
  their Tracking issue closes and the row flips, at which point (b) starts holding
  them.

Asserting the predicate against every classified row instead would fail fourteen
Domain A rows at this commit, because a classified-but-unguarded row satisfies (a)
and not the predicate. That is not a contradiction: §3 already calls those rows
defects and tracks each one. Asserting only (a) would let a guarded row silently
lose its guard, since (a) cannot tell `delete_lpar` from `map_storage` — both are
merely *classified*. Only the pair covers both failure modes, and the Status column
is what makes (b) mechanical.

The failure message should name this section and the row that is missing or that
lost its guard.

### 6. Rule for future mutating operations

A change that adds an operation meeting §1's definition must, **in the same PR**:

1. add a row to the §3.1, §3.2 or §3.3 table naming the operation and its class, or
   to §3.4a / §3.4b with a reason; and
2. satisfy that class's guard rule — for Destructive and Reconfiguring, a guard
   call reachable from an **operations-layer** function, not from a tool body or a
   CLI command. A new mutation that lives only in `server_*.py`, `cli_*.py` or
   `ssh_commands.py` enters §5's Domain B and is a defect on arrival.

A PR that adds an LPAR-mutating operation without a classification row is
incomplete. "It is new, so nothing owns it yet" is not a reason: the operation acts
on a partition that may already be owned.

A change that adds any new **facade export** defined in `operations_*.py` — mutating
or not — must also place it in one of §5's three sets. Doing nothing is not an
option the test permits, which is the point: it forces the §1 judgement to be made
once, in review, rather than silently deferred.

### 7. Relationship to #218's server access policy

The two boundaries are complementary and neither substitutes for the other. Both
must pass for an MCP call; only this one applies to a CLI or `hmc_mcp.api` call.

**What #218 gives this ADR.** The server access policy is an operator-configured,
startup-immutable allowlist evaluated at MCP dispatch, before any handler opens a
client: an effect-class ceiling, allowed connection profiles, and exact target
selectors. It decides *whether this caller may dispatch this tool against this
connection and target*. It never inspects an ownership token, never resolves who
created a partition, and is static — an operator writes it ahead of time. A policy
granting `lpar.power_off` over the `all-targets` sentinel says nothing about who
owns the partition being powered off. So #218 does not make this rule redundant for
any operation, on any entry path.

**What this ADR gives #218.** This guard is a resource-level check inside the
operation, evaluated on every entry path. It decides *whether this agent may mutate
this particular partition*, using state written at creation time by whichever agent
created it. It never widens what an MCP caller may dispatch: a denial by either
boundary denies the call, and this guard runs after the policy check, so a
policy-denied call never reaches it. #218's non-goals exclude the CLI and the
supported Python API from the policy boundary; that exclusion is deliberate and
correct, and it is precisely why this rule must live in the operations layer.

**Ordering and cost.** Access policy first: it performs no HMC request, so a denial
is free. Ownership second: it must read the partition's current description, and the
name resolution that read needs, so it costs outbound calls by construction (§4).
That asymmetry is why §4 exists at all.

## Consequences

- Every row marked **unguarded** in §3.1–§3.3 is now a recorded defect against an
  accepted ADR rather than an undocumented inconsistency, and every one carries a
  Tracking issue: #371 implements §4, #372 and #373 are #369's existing sub-issues,
  #365 covers DLPAR, and #440, #441, #448 and #449 were filed for the rows —
  §3.4b's included — that no #369 sub-issue reached. #369 must not close while any
  Tracking cell reads `none yet`.
- The #369 enforcement test has a concrete predicate (§5) — enumeration domains,
  guard set, reachability rule, and the three-set partition of the facade — so
  neither existing coverage nor the classification itself can silently regress: a
  new facade export belongs to no set and fails the test until someone classifies
  it. The cost is a maintained not-LPAR-mutating set in the test, which is the
  price of not making the check circular. The test also has to assert Domain B,
  which is not a facade check; the two arms are separate.
- `authorize_power_operations` adds a configuration field to the supported surface
  when #371 lands. That PR owns the CHANGELOG entry and the release-classification
  call under ADR 0029; this ADR changes no code and no signature.
- The setting's name and key are a reversible naming choice. If #371 renames them
  it must amend §4 in the same PR.
- Guarding the §3.2 operations adds one SSH login and two REST GETs per call to
  each (§4). They are low-frequency by construction, and the alternative is a facade
  consumer with no authorization boundary at all on adapter, storage and LPM
  mutations.
- `modify_lpar` guards its complete workflow (§3.2), so resource-only calls now pay
  the ownership check's SSH and REST cost. It is the one Reconfiguring operation a
  caller might invoke in a loop.
- `provision_lpar` calls `power_lpar` for its activation leg
  (`operations/lpar/provision.py::provision_lpar`). With the setting on, that leg would authorize a
  partition the same workflow just created and stamped — the check passes but costs
  an SSH login for nothing. #371 owns the case, and the resolution stays inside §5's
  two mechanisms: the internal call passes `ownership_override=True`, which is
  audited, rather than a third call-site-conditional guard.
- Issue #449 removes the last §3.4b standing exemption. A non-override restore now
  pays one bulk REST ownership read, reports the first current foreign or
  malformed-token blocker, and then fails closed when a clean feed cannot establish
  the backup's opaque affected set. An
  approved override skips that dependency and emits one `ownership-override` event
  with `lpar="*"` before SSH.
- This ADR touches only `docs/adr/`. ADR 0011 gets no back-reference to 0092 here;
  adding one is a one-line edit that belongs to whichever PR next amends 0011.

## Considered & rejected

**Guard every LPAR-mutating operation unconditionally, including `power_lpar`.**
The simplest rule, and it needs no configuration field. Rejected on the cost in §4:
one SSH login on every power call, paid by the package's highest-frequency
non-interactive consumer, to protect the one operation class whose inverse is a
single call with no prior state to recover.

**Leave ADR 0011 purely advisory and guard nothing further.** Rejected: #218's
policy boundary does not reach the CLI or `hmc_mcp.api` by design, so a facade
consumer would have no enforced authorization on any mutation. That is the gap this
epic exists to close.

**Let a clean current-partition feed authorize a system-wide profile restore.**
verified: IBM's `rstprofdata` command contract defines a full restore from the backup
file, while ADR 0036's HMC-side path contract exposes no supported backup-content
reader. The current feed therefore cannot prove the complete affected set; only the
existing audited override may cover it.

**Treat `system_wide_restore_approved` as authorization for backup-only data.**
judgment: that flag prevents accidental invocation and emits no ownership-override
audit. Reusing it would create a second, unaudited bypass beside §5's one mechanism.

**Cache the ownership token per `(system, lpar)` for a TTL, then guard everything.**
Removes the cost objection without a configuration field. Rejected: a token can
change out of band — another agent restamps it, an operator edits the description
in the HMC GUI — so a cache turns a stale read into an authorization decision, and
the failure mode is *allowing* a foreign mutation. Reconsider only with an
invalidation story, and prefer the ADR 0071 REST read (§4) which is cheap without
being stale.

**Rely on #218's target allowlist instead of ownership.** Rejected twice over: it
does not apply to the CLI or the supported Python API, and it is operator-static —
it cannot express "the partition another agent created five minutes ago", which is
the entire case ADR 0011 addresses.

**Default the power guard on with an opt-out.** Same cost, and it makes the slow
path the default for the consumer that motivates this ADR. An operator who wants it
sets one flag; the alternative is every other operator paying for a check they did
not ask for.

**Classify by the existing MCP `effect` metadata (`read`/`mutate`/`destructive`)
rather than a new taxonomy.** Attractive because the metadata is already exhaustive
and test-enforced. Rejected: `effect` is a client-facing blast-radius hint, not a
reversibility judgement, and on the cases that matter it points the wrong way —
`hmc_power_off_lpar` is `destructive` while `hmc_dlpar_mem` is `mutate`, the exact
inverse of the guard priority this rule needs. `effect` also stops at the MCP
boundary, and this rule has to hold for the CLI and the facade.
