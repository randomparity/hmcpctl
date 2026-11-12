# System-wide writes respect external-management holds

Issue #1887; scope token `q1887-92586ba1`; full-spec, M/250, iterating.
Decision: [ADR 0343](../../adr/0343-system-write-hold-fence.md).
Source baseline: `0dcb93630a66efcae7aed85d3265ba85f8289c58`.

## Problem and scope

The partition hook cannot see writes that resolve only a managed system. The operator chose
"Refuse while any system hold exists": holders release first, without an override or a set
of tokens. This extends the existing front-end resolver-time check to the six tools below.
It does not turn the local store into a distributed lock or fence library/raw HMC writers.

### Inventory and affected callers

The non-read `TOOL_SECURITY` records with `target_kind == "managed_system"` contain 23 tools.
Names in this table have the `hmc_` prefix. The checked set is exactly these six:

| Tool | Existing operation owner and effect | CLI mirror |
| --- | --- | --- |
| modify_system | operations/systems/core.py: modify_system writes platform configuration, including LPAR start policy | none |
| power_on_system | operations/systems/core.py: power_system may auto-start partitions | systems power-on |
| power_off_system | operations/systems/core.py: power_system stops the platform and its partitions | systems power-off |
| activate_system_profile | operations/systems/profiles.py: activate_system_profile starts contained members | systems profile-activate |
| restore_lpar_profiles | operations/lpar/configuration.py: restore_system_lpar_profiles rewrites partition profiles | none |
| update_firmware | operations/updates/service.py: update_firmware submits platform/VIOS/I/O update work | none |

These operation paths call `resource_identity.resolve_system_uuid` before their first
mutation. Restore reaches it through `operations/lpar/ownership.py` before ownership override.
Firmware's version read precedes it; profile activation's argument validation precedes it.
The marker applies to the whole tool: even rename-only modify and validate-only activation
refuse on a held system. This deliberately avoids argument-dependent exceptions.

The other 17 records are classified by the resources their current code writes:

- create_system_profile, update_system_profile, delete_system_profile change system-profile
  container membership/metadata, not the contained partition profiles or running state
  (`operations/systems/profiles.py`, `_metadata`).
- create_lpar, create_vios, deploy_partition_template create new partitions; their creation
  paths do not rewrite an existing held partition (`operations/lpar/creation/workflow.py`,
  `operations/vios/core.py`, `operations/templates/core.py`). Resource competition remains outside
  the requested existing-partition fence.
- set_vios_fc_port_label, remove_vios_fc_port_label, create_vios_vfc_group_label,
  update_vios_vfc_group_label, remove_vios_vfc_group_label edit label resources, not partition
  configuration (`server_tools/vios/labels.py`).
- create_virtual_network, delete_virtual_network change virtual-network resources
  (`server_tools/virtualization/network.py`); remove_memory_pool removes an unused pool only
  after dedicated-memory checks (`ssh/memory.py`). Shared-resource protection is not added.
- set_sriov_adapter_mode currently verifies already-selected mode and refuses transitions
  (`server_tools/virtualization/pcie.py`); it submits no admitted mode change.
- backup_lpar_profiles and delete_backup_lpar_profile change backup artifacts, not current
  partition profiles (`server_tools/lpar/profiles.py`).

The existing partition fence remains unchanged. Direct HMC/raw commands and library fencing
are excluded under ADR 0193 D6; protection beyond system tools affecting partitions belongs
to the operator. The inventory explains the boundary instead of calling all system tools safe.

## Architecture and behavior

`ToolSecurity.system_partition_write: bool = False` records this classification explicitly
in the decorator. Reject a true marker on a read tool, a non-managed-system target, or a
record lacking the required managed-system selector. Add the marker to the six declarations.
Keep `hold_guarded` as the partition-token predicate: the system marker does not inject
`hold_id` into schemas or `--hold-id` into CLI help.

Copy the marker into `CallContext` in `authorized()` and `CLICommand.invoke`, using the existing
mirror table. Nested `hmc_invoke_tool` dispatch gets the selected tool's marker. Existing
context restoration and worker propagation continue to apply. No handler signature changes.

Extend the installed `PartitionGuard` with an optional system-check callback; the existing
server/CLI `HOLD_GUARD` supplies it and library code installs none. After resolving a name or
accepting a UUID, `resolve_system_uuid` calls that callback before returning. There is one
check path for name, lowercase UUID and uppercase UUID. Existing two-argument guard fixtures
remain valid because the new callback defaults to absent.

The callback returns immediately unless the current call carries the system marker. For a
marked call it verifies canonical UUID shape, lowercases the lookup key, and uses the existing
non-creating `store.read_session` lifecycle to select one hold on that system. Holds on other
systems and an absent store permit the call. An unreadable/corrupt/lost store propagates its
existing refusal; it never becomes an empty hold result. No schema or state migration is needed.

A matching hold raises `HoldRefused` with reason `system_held`, naming the system and one held
partition, advising `hmcpctl holds list` and release before retry. It includes no hold ID.
The placing agent, matching partition token, ownership override and power-operation setting
cannot waive this refusal. Hold release remains the existing explicit operation.

`server_tools/lpar/handoff.unchecked_tools` excludes both partition-guarded tools and marked
system tools. Update `operations/lpar/handoff.UNFENCED`, MCP docs and the kdive contract to
name the system rule and retain the library/raw/other-host exclusions. No handoff field is
added or removed. ADR 0327 gets only a Status banner identifying D5's partial supersession.

## Success

- C1: the six-tool checked set and the other 17 system records are classified above; ADR 0343
  records the policy and tradeoffs and ADR 0327's body remains unchanged.
- C2: each of the six tools refuses `system_held` before its HMC mutation when its system has
  at least one hold at the resolver check, including a hold placed by the calling agent.
  The same tools can pass this check for an unheld system; all holds must be released first.
- C3: direct MCP and invoke dispatch and the three existing CLI mirrors agree; no system
  `hold_id` interface appears; handoff disclosure removes exactly the newly checked tools.
- C4: unknown local store state fails closed for the checked set, reads and library calls keep
  their prior behavior, and focused tests plus final guardrails pass without live HMC access.

## Failure model

1. **Actors and deployments**
   - MCP callers and CLI operators on one host sharing its configured ADR 0190 store.
   - Concurrent local processes may read/place/release holds; existing resolver-time admission.
2. **Invariants and assets at stake**
   - A hold observed on the selected system blocks the six declared system tools before write.
   - Other systems, reads, library callers and existing partition-token semantics retain behavior.
   - Unknown store state cannot authorize a marked write; refusal text reveals no hold ID.
3. **Accepted failure classes**
   - A hold placed after the resolver snapshot does not cancel an already-admitted operation;
     the existing ADR 0193 hook is a pre-write check, not a lock spanning an HMC job. Operators
     coordinate handoff with in-flight system operations; no new transaction/job lock is added.
   - Conservative lockout of non-mutating argument variants within the six marked tools is
     bounded to those tools and ends after explicit hold release, matching the operator choice.
4. **Covered elsewhere**
   - Library, other hosts, GUI and raw HMC writers: ADR 0193 D6, consumer/operator coordination.
   - Shared-resource competition and tools outside the six-tool set: operator responsibility.
   - Store permissions/corruption/schema handling: ADR 0190; partition hold IDs: ADR 0193/0327.

## Threat model

- **Boundary inventory:** existing MCP/CLI arguments reach registry context, then resolved
  system UUID reaches the local hold store. No new remote input, credential or database schema.
- **Actors:** callers can choose selectors, tokens and ownership flags; they cannot set internal
  registry markers through public arguments. Local credential holders control store/release.
- **Controls:** registry shape validation and six-tool coverage test; existing access-policy
  authorization before handler; authoritative UUID resolution and canonical shape checking;
  parameterized system lookup and existing store validation; no token override or token leakage.
- **Out of scope:** hostile local store owner and independent HMC writers already control the
  excluded surfaces. This does not provide protection against them or span HMC job execution.

## Global constraints

Python 3.11 through 3.14; CI target amd64; local host x86_64. No new dependency or facade export.
Use `just setup`; never bare `uv sync` or `uv run`. Use private TMPDIR outside the worktree.
No live HMC access. Final candidate requires `just verify` and `uv run --no-sync prek run --all-files`.

## Validation

Use real temporary hold stores and mocked HMC/SSH boundaries. Add registry-driven composed
MCP tests for the six tools by name/lowercase UUID/uppercase UUID, directly and via invoke;
assert `system_held` and zero HMC writes/SSH mutations. Exercise the three CLI mirrors.
Store tests cover multiple holds, release of one versus last, cross-system isolation, matching
hold ID/agent non-bypass, missing store, corrupt/lost store and malformed resolved UUID.
Keep positive paths for reads, unheld systems, library with no installed guard, and unchanged
partition-token schema behavior. Test marker validation and handoff registry coverage.
No synthetic prose tests: doc changes are reviewed; regenerate generated tool docs if needed.
