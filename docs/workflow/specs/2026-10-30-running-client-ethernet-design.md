# Running client Ethernet updates: #638/N2

Proposed private design at `669ebc0e9243a401b826ca24d0c6904f85fb89ba`.
Authority is the frozen eight-field charter in `N2-scope.json`, including its exact approvals.
Classification: full-spec, M, fixed denominator 250; selected-scope exclusions EMPTY.
Filing, implementation, hardware and shipping remain held.
Decision: [ADR 0310](../../adr/0310-running-client-ethernet-updates.md).

## Problem and bounded ownership

Expose the six running-client fields `ieee_virtual_eth`, `addl_vlan_ids`, `qos_priority`,
`vsi_manager_id`, `vsi_type_id`, `vsi_type_version` through one contained adapter update.
Creation stays with #1821. Adapter-level vNIC policy stays N4, backing/failover #669,
multi-backing creation N5, system defaults #680/A9, and boot/console their existing owners.
These boundaries do not exclude any of the six selected N2 fields or their approved actions.
Extend `operations/virtualization/adapters.py`; do not export new stable-facade names.
Add only a thin native Ethernet command module, matching the existing SSH domain pattern.
No migration, persistence store, dependency, general transaction engine or REST writer.

## Interfaces

Python operation, in `operations/virtualization/adapters.py`:

```python
async def update_network_adapter(
    hmc: HMCClient, lpar_name_or_uuid: str, adapter_uuid: str, *,
    system_name_or_uuid: str, exclusive_writer_window: bool,
    tagged: bool | None = None,
    addl_vlan_ids: list[int] | None = None,
    vlan_action: Literal["replace", "add", "remove"] = "replace",
    qos_priority: int | Literal["none"] | None = None,
    vsi_manager_id: int | None = None,
    vsi_type_id: int | None = None,
    vsi_type_version: int | None = None,
    remove_vsi_profile: bool = False,
    ownership_override: bool = False,
) -> NetworkUpdateResult: ...
```

CLI: `adapters update-network LPAR --uuid UUID --system SYSTEM
--exclusive-writer-window`, optional `--tagged/--untagged`, `--addl-vlan-ids JSON`,
`--vlan-action replace|add|remove`, `--qos-priority 0..7|none`, numeric `--vsi-manager-id`,
`--vsi-type-id`, `--vsi-type-version`, `--remove-vsi-profile`, `--ownership-override`, `--yes`.
Confirmation uses terminal encoding. Print the separate result as JSON and a truthful summary.
Exit zero only for completed submission, verified selected evidence and unchanged bounded
preservation, or the
explicit independently confirmed already-satisfied no-write case; otherwise exit one.

MCP: `hmc_update_network_adapter` with the Python arguments except `hmc`, plus
`profile: str | None = None`; keyword-only mutation settings mirror Python.
Use `StrictBool` and `StrictInt` to prevent coercion, including list members.
Decorator: effect `mutate`, operation `adapter.update_network`, target_kind `lpar`.
System and LPAR selectors are exhaustive; adapter UUID is a contained child, not a new target kind.
Use existing policy dispatch and `with_client`, never call native transport from a tool body.

`NetworkUpdateResult` is a frozen dataclass with: `system_uuid: str`, `lpar_uuid: str`,
`adapter_uuid: str`, `slot_number: int`, `mutation_dispatched: bool`,
`submission: Literal["not_dispatched", "completed", "rejected", "unknown"]`,
`reason: Literal["already_satisfied"] | None`, `refusal_code: str | None`,
`verification: NetworkUpdateVerification`, `preservation: NetworkUpdatePreservation`,
`change_location: ChangeLocation`, `errors: tuple[str, ...]`.
The two new observation dataclasses use status literals below and ordinary JSON-safe values.
Verification: `status: Literal["verified", "mismatched", "unavailable"]`,
`expected: dict[str, object]`, `observed: dict[str, object]`,
`mismatched_fields: tuple[str, ...]`, `unavailable_fields: tuple[str, ...]`.
Preservation: `status: Literal["unchanged", "drifted", "unavailable"]`, `scope: str`,
`changed_adapter_uuids: tuple[str, ...]`.
Errors are locally constructed actionable summaries, not remote exception/body text.
Do not return credentials, transport bodies or a command-output success oracle.

## Validation and explicit intent

Omission means unchanged. Preserve `False` and integer zero; reject bool-as-int.
Reject malformed/non-UUID adapter selectors and blank system/LPAR selectors before transport.
Require a literal true writer acknowledgement and boolean override/removal values.
Reject no selected setting; reject non-replace action without a VLAN list.
VLAN lists contain unique strict integers 1–4094; add/remove lists must be nonempty.
Validate a complete baseline and at most twenty resulting members for add/remove.
Nonempty replacement needs at most twenty members; empty replacement requires a complete baseline.
Compute add/remove expectations as sets; VLAN list order carries no placement meaning.
An empty replacement sends `addl_vlan_ids-=<baseline>`; never emit an empty native value.
If already empty, omit that native attribute. With no other setting, return `not_dispatched`,
`mutation_dispatched=false`, `reason=already_satisfied` after fresh identity/empty evidence.
Do not generalize this no-write exception to other equal-value requests.
Require known enabled tagging for a nonempty resulting VLAN set, either selected true or
positively observed true. Disabling tagging requires a positively empty resulting VLAN set.
Do not infer that selecting false clears VLANs; require an explicit accompanying action.
QoS accepts strict 0–7 or `none`. VSI numbers use signed 32-bit representation; HMC validates
undocumented semantic limits. Removal is profile-wide and conflicts with any numeric VSI input.
Do not accept a free-form native field/operator, force flag or disruption override.

## Execution and evidence

1. Validate local request and finite positive SSH timeout before target transport.
2. Resolve system with `resolve_system_uuid`; positively obtain its canonical native name.
   Validate a fresh summary index: selected UUID occurs once and its SystemName is unique
   among its valid named rows. Missing/ambiguous index data refuses before native dispatch;
   explain that duplicate names require the documented type/model/serial native form, which
   this interface does not select. Do not mistake a UUID-to-name lookup for uniqueness proof.
   Resolve and authorize the LPAR with explicit system scope; UUID case is insensitive.
   Independently read its identity, positive native partition ID and exact `running` state.
3. Read sync policy before the write. Retain observed sync/profile names but return
   `ChangeLocation(sync, "unknown", profile_name)` for this native route: retained evidence
   does not establish native propagation equivalence. Never claim saved-profile equality.
   Obtain the complete contained ClientNetworkAdapter feed with the existing strict reader.
   Reject duplicate/invalid UUIDs, ambiguous resources or repeated slot mappings.
4. Find exactly the selected UUID and prove its unsigned-short virtual slot. Retain a deep
   baseline of parsed adapter resources and immutable UUID/slot/PVID/MAC/switch bindings.
   Validate baseline-dependent VLAN rules and derive expected selected values.
5. Recheck contained partition identity/running state and adapter UUID/slot immediately before
   dispatch. Compare the complete parsed adapter resources, excluding Metadata only; selected
   fields are not excluded from this freshness comparison. Refuse drift before writing; no retry.
6. Build scalar attributes followed by the single VLAN attribute. Use the existing
   `build_attribute_record` with the exact operator-bearing VLAN name marked quoted.
   Numeric inputs become canonical decimal text; tagging becomes 0/1; QoS disable is `none`;
   profile removal uses the documented `vsi_manager_id=none` representation once.
7. Attempt once: `chhwres -r virtualio --rsubtype eth -o s -m NAME --id ID -s SLOT -a RECORD`.
   Shell-quote names/record and use the bounded existing SSH transport (64 KiB output).
   Never append `--force` or `--allownetdisrupt`; HMC validates undocumented eligibility.
8. Independently reread contained partition identity and the complete adapter feed, even after
   a completed remote nonzero result. A failed read yields unavailable evidence, not no effect.
   Cancellation/process death can prevent a result; the caller must inspect before retrying.

Native `completed` means successful exit, not atomicity or verified effect.
`rejected` requires `HMCCLIError.remote_nonzero_completed`; a partial change remains possible.
Only `.remote_refusal_code` supplies a refusal code. Opaque transport failure is `unknown`.
Do not classify by searching exception text, echoed selectors or command output.
`not_dispatched` is reserved for the approved already-empty-only case.

Verification compares the requested field closure in the same contained existing UUID.
Malformed or missing fields remain unavailable; known mismatches take precedence over unavailable.
Tagging reads explicit `TaggedVLANSupported` true/false or 1/0; QoS disable reads explicit
`QualityOfServicePriorityEnabled=false`; enabled QoS additionally requires its priority.
VLANs require a present strict list leaf, unique bounded IDs, and set equality.
A present empty list is empty; an absent element is unavailable, never an inferred default.
Numeric VSI observations use the existing strict signed-integer reader.
VSI removal has no positive readback oracle in retained sources: report unavailable, retain
any observed numeric values as observations, and never equate omission/zero/-1 with removal.

Preservation status is `unchanged`, `drifted` or `unavailable`.
Compare the complete UUID-keyed adapter inventory's parsed Resource values, excluding only
target selected-field closures and each Resource's `Metadata` bookkeeping subtree.
Closures: tagging→TaggedVLANSupported; VLAN→TaggedVLANIDs; QoS→priority+enabled; each numeric
VSI input→its element; profile removal→all three VSI elements. Compare other parsed fields,
unknown non-Metadata fields and list ordering exactly. Preserve UUID/slot binding independently.
Report scope `parsed_adapter_resources_except_metadata_and_selected_fields`; this does not
cover raw XML namespaces, parser-discarded attributes, Metadata, profiles or runtime traffic.
Known inventory/field drift wins over incomplete evidence; an unchanged readable subset cannot
establish complete preservation. Non-target addition/removal or target disappearance is drift.
Observed partition identity drift prevents selected-field verification and is reported explicitly.

## Failure model and threat boundaries

- Prevent: input/record/shell injection, wrong-system/partition/slot targeting, foreign ownership,
  coercion of false/zero, omitted-value clearing, duplicate inventories and writes after stale reads.
- Detect: known selected mismatch, target/foreign-adapter drift and partial/unavailable evidence.
- Contain: exactly one command attempt, no retry/activation/automatic compensation, strict
  authenticated REST observations and bounded SSH output using existing sanitization.
- Accept explicitly: coordinated writers are advisory; no atomicity, complete unpublished
  eligibility validation, positive VSI-clear oracle, saved-profile equality or runtime guarantee.
  Process termination can leave incomplete evidence; it cannot establish no effects.

## Future live procedure

Prepare ST69 `client-ethernet-update`, dispatched through a new named script only after preflight.
Use one existing owned, running, explicitly expendable client adapter; create/activate/delete none.
One current identity-bound packet selects one fixed case: tagging enable/disable, VLAN
replace/add/remove/empty, QoS set/disable, all-three numeric VSI set, or VSI profile removal.
No caller-supplied command or arbitrary settings record. Values/cases are enumerated in the plan.
Require separate matching packet grant, writer acknowledgement and retained-change permission.
Retain the requested change; no restoration or cleanup writer is authorized by this procedure.
Always run read-only recovery independently, including after apparent success or a held run.
Recovery classifies retained verified change, observed drift or unconfirmed state; never CLEAN.
Proposed budgets: 72 REST + 8 SSH transports, 16 MiB logical payload allowances, 1,200 seconds;
logical allowances cover configured retained response/output caps plus requests/commands, not
bytes delivered/allocated by underlying streams. Oversized chunks can exceed a cap before refusal;
per-phase allocations and session closeout proof are exact in the plan. No hardware run granted.

## Success and validation

C1–C6 correspond in order to the frozen charter's six completion criteria.
The plan maps validation, request grammar, containment, one-attempt outcomes, observations,
presentations and ST69 contracts to focused negative/positive tests and fault proofs.
Run focused tests per task, then required independent design/audit/product/security gates.
Before a future push run final `just verify` and the pinned all-files hooks in the worktree.
Read every Python-version CI leg; local Python 3.11 does not qualify 3.12–3.14.
No offline proof, retained V9 schema or command completion promotes live-support maturity.
