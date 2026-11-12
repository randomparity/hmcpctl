# Live-test arm ownership migration

Status: the operator approved the exact 51-module move map on 2026-11-10.
Decision record: ADR 0347, live-test-arms-have-a-distinct-package.
The explicit approved move map is recorded below; no compatibility path remains.

## Problem and success
Shared runner/config/observation infrastructure and dozens of real mutation arms share
one directory. Give existing arms a distinct owner without changing an arm's behavior.
Every mapped old path is removed, every direct consumer uses its real new owner, and
offline safety/functional tests and source-discovery coverage retain their meaning.

## Ownership
Move the 51 approved modules into scripts/live_test/arms/ with a docstring-only __init__.py.
Keep __init__, baseline, capture, cli, config, definitions, evidence, http_fixture, inventory,
observation, packets, redaction, results, runner and scenario_validation at current paths.
Keep recovery/ and scenario_definitions.json at their current paths. lpar_config is a
scenario and arm-fixture owner, so travels with its mapped peers; generic infrastructure
does not gain a dependency through a forwarding root facade.

## Migration sequence
First move the seven memory-related modules: creation_memory, creation_memory_layout,
lpar_hardware_memory, memory_move, memory_quantities, memory_statistics, paging_switchover.
Validate and commit that coherent transition, then move the other 44 and validate again.
Each milestone has live direct consumers; no scaffolding or empty compatibility package.
Use the same final map to resolve imports based on each phase's actual physical owners.

## Import and fixture rules
- Resolve relative imports against the old containing package, map the actual moved owner,
  and emit the correct relative import for the destination. Imports of retained root
  infrastructure gain a parent level; moved siblings retain a sibling level.
- Split mixed from-imports by real owner while preserving aliases, meaningful comments and
  justified suppression scope. Absolute imports and direct qualified attribute references
  use the new path. Existing public application/library imports remain unchanged.
- Never unparse whole functions/calls or reserialize Python string literals. Embedded
  subprocess/f-string programs require targeted text edits preserving braces and escapes.
  Inspect dynamic import templates and fictional parser fixtures rather than guessing.
- Update wrappers, pytest patches, sys.modules keys and module-name expectations to actual
  owners. No getattr compatibility shims, forwarding modules or sys.modules aliases.

## Paths and source trust
Eight own-file Path(__file__).resolve().parents[2] anchors become parents[3].
Do not change product.__file__, hmcpctl.__file__ identity checks, or own-file hash reads.
Update every affected source-file path and attested closed set, including the new package
initializer where an arm binds its executable source bundle. Keep it docstring-only.
Fresh source packets naturally bind the new commit/tree/paths; stale packets stay refused.
Preserve packet field names, versions, directory/privacy checks and all REST/SSH/time/byte
limits. Source bundles retain the separately extracted partition_creation owner where used.

## Scenario discovery and graph analysis
scenario_gap_report currently globs immediate root files and recognizes only flat imports.
Enumerate immediate root Python files plus immediate arms Python files, in deterministic
order. Do not inadvertently scan recovery/ or the whole repository.
Keep basename graph IDs, because mapped module basenames are unique and runner roots use
the final module component. Normalize root and arms absolute imports to those IDs.
Resolve relative ImportFrom nodes against an explicit containing-package argument, using
stdlib import resolution; retain existing scan_source callers with a default root package.
Handle aliased and directly qualified nested imports. Existing outside-package references
remain outside. Preserve dispatch/record associations, delegated operation coverage, unknown
and mismatch detection; no new exclusions or manufactured verification rows.
Add behavioral scanner cases for nested discovery and each relative/absolute import form
before changing discovery, then compare the real report to its current 463 dispatch baseline.

## Failure model
- Actors/deployments: existing local/CI offline tests and separately authorized operators
  running existing wrapper scripts against managed systems. No hardware execution here.
- Invariants/assets: no skipped arm discovery, no changed mutation/cleanup/refusal behavior,
  correct checkout root and installed-source identity, private evidence and exact ownership.
- Accepted classes: old internal module imports stop resolving; approved direct migration
  replaces them. Old source packets require fresh commit-bound capture, as already required.
- Covered elsewhere: actual HMC admission/cleanup by current arm tests and live-testing
  policy; stable facade by its existing test; packet parsing/privacy by existing guards.

## Validation and rollback
focused-test: real nested scenario scanning discovers an unregistered arm and links helper
dispatches for root/arms absolute imports, sibling/parent relative imports and direct aliases.
focused-test: all affected offline arm/runner/wrapper, preflight/recovery/evidence, vocabulary
and scenario/capability tests pass; source bundle tampering and privacy refusal tests remain.
Audit moved bodies after normalizing only imports, declared path/module identities and
the eight root-depth changes. Inspect every remaining old-name reference, including strings.
Regenerate generated metadata through existing owning recipes; do not hand-edit outputs.
Compare actual MCP schemas and scenario report operation/dispatch associations. Finish lint,
types, format, all-file hooks and root-owned full just verify after creation integration.
task-test-not-applicable: architecture prose has no executable body consumer; numbering,
factual review and current paths are its gate, not text snapshots.
Rollback must reverse paths and consumers together and use fresh source-bound captures.
Do not retain both module trees or raise budgets to conceal a migration failure.

## Approved owner map

| Current owner | Intended owner |
| --- | --- |
| `scripts/live_test/accelerator_qos.py` | `scripts/live_test/arms/accelerator_qos.py` |
| `scripts/live_test/bare_cec.py` | `scripts/live_test/arms/bare_cec.py` |
| `scripts/live_test/connectivity.py` | `scripts/live_test/arms/connectivity.py` |
| `scripts/live_test/creation_memory.py` | `scripts/live_test/arms/creation_memory.py` |
| `scripts/live_test/creation_memory_layout.py` | `scripts/live_test/arms/creation_memory_layout.py` |
| `scripts/live_test/dedicated_pcie.py` | `scripts/live_test/arms/dedicated_pcie.py` |
| `scripts/live_test/detach_probe.py` | `scripts/live_test/arms/detach_probe.py` |
| `scripts/live_test/escape_hatch.py` | `scripts/live_test/arms/escape_hatch.py` |
| `scripts/live_test/ethernet_update.py` | `scripts/live_test/arms/ethernet_update.py` |
| `scripts/live_test/logical_workflow.py` | `scripts/live_test/arms/logical_workflow.py` |
| `scripts/live_test/lpar.py` | `scripts/live_test/arms/lpar.py` |
| `scripts/live_test/lpar_affinity_group.py` | `scripts/live_test/arms/lpar_affinity_group.py` |
| `scripts/live_test/lpar_boot_start.py` | `scripts/live_test/arms/lpar_boot_start.py` |
| `scripts/live_test/lpar_config.py` | `scripts/live_test/arms/lpar_config.py` |
| `scripts/live_test/lpar_hardware_memory.py` | `scripts/live_test/arms/lpar_hardware_memory.py` |
| `scripts/live_test/lpar_linux_roles.py` | `scripts/live_test/arms/lpar_linux_roles.py` |
| `scripts/live_test/lpar_physical_io.py` | `scripts/live_test/arms/lpar_physical_io.py` |
| `scripts/live_test/lpar_power.py` | `scripts/live_test/arms/lpar_power.py` |
| `scripts/live_test/lpar_reconfigure.py` | `scripts/live_test/arms/lpar_reconfigure.py` |
| `scripts/live_test/lpar_security_policy.py` | `scripts/live_test/arms/lpar_security_policy.py` |
| `scripts/live_test/lpar_serial_adapter.py` | `scripts/live_test/arms/lpar_serial_adapter.py` |
| `scripts/live_test/lpar_vtpm.py` | `scripts/live_test/arms/lpar_vtpm.py` |
| `scripts/live_test/memory_move.py` | `scripts/live_test/arms/memory_move.py` |
| `scripts/live_test/memory_quantities.py` | `scripts/live_test/arms/memory_quantities.py` |
| `scripts/live_test/memory_statistics.py` | `scripts/live_test/arms/memory_statistics.py` |
| `scripts/live_test/metrics.py` | `scripts/live_test/arms/metrics.py` |
| `scripts/live_test/network.py` | `scripts/live_test/arms/network.py` |
| `scripts/live_test/paging_switchover.py` | `scripts/live_test/arms/paging_switchover.py` |
| `scripts/live_test/partition_profile_contract.py` | `scripts/live_test/arms/partition_profile_contract.py` |
| `scripts/live_test/partition_profiles.py` | `scripts/live_test/arms/partition_profiles.py` |
| `scripts/live_test/processor_move.py` | `scripts/live_test/arms/processor_move.py` |
| `scripts/live_test/processor_quantities.py` | `scripts/live_test/arms/processor_quantities.py` |
| `scripts/live_test/processor_sharing_weight.py` | `scripts/live_test/arms/processor_sharing_weight.py` |
| `scripts/live_test/profile_contract_run.py` | `scripts/live_test/arms/profile_contract_run.py` |
| `scripts/live_test/profiles.py` | `scripts/live_test/arms/profiles.py` |
| `scripts/live_test/provision_boot.py` | `scripts/live_test/arms/provision_boot.py` |
| `scripts/live_test/provisioning.py` | `scripts/live_test/arms/provisioning.py` |
| `scripts/live_test/sriov.py` | `scripts/live_test/arms/sriov.py` |
| `scripts/live_test/storage.py` | `scripts/live_test/arms/storage.py` |
| `scripts/live_test/storage_lifecycle.py` | `scripts/live_test/arms/storage_lifecycle.py` |
| `scripts/live_test/storage_volume_group.py` | `scripts/live_test/arms/storage_volume_group.py` |
| `scripts/live_test/system_profile_activation_source.py` | `scripts/live_test/arms/system_profile_activation_source.py` |
| `scripts/live_test/system_profile_source.py` | `scripts/live_test/arms/system_profile_source.py` |
| `scripts/live_test/system_profiles.py` | `scripts/live_test/arms/system_profiles.py` |
| `scripts/live_test/users.py` | `scripts/live_test/arms/users.py` |
| `scripts/live_test/vios_ame_source.py` | `scripts/live_test/arms/vios_ame_source.py` |
| `scripts/live_test/vios_backup.py` | `scripts/live_test/arms/vios_backup.py` |
| `scripts/live_test/vios_boot_source.py` | `scripts/live_test/arms/vios_boot_source.py` |
| `scripts/live_test/vmedia.py` | `scripts/live_test/arms/vmedia.py` |
| `scripts/live_test/vmedia_media_change.py` | `scripts/live_test/arms/vmedia_media_change.py` |
| `scripts/live_test/vnic_creation_policy.py` | `scripts/live_test/arms/vnic_creation_policy.py` |

Retained root modules: `__init__`, `baseline`, `capture`, `cli`, `config`, `definitions`, `evidence`, `http_fixture`, `inventory`, `observation`, `packets`, `redaction`, `results`, `runner`, and `scenario_validation`. The `recovery/` package and `scenario_definitions.json` also retain their owners.
