# 0320 — LPAR decommission is one journaled operation keyed by the resources it removes

## Status

Accepted (2026-11-03), issue #1847 (piece 1 of #1229, epic #1215). Implements the H1 spec's
`hmc_decommission_lpar` row with `storage_cleanup` absent (it is `retain` today) under ADRs
0190, 0195 and 0236, with ADR 0313 as the wiring precedent. ADR 0027's steps, ownership checks
and storage retention stand; only how the writes are recorded changes. #1848 adds
`delete_owned` (ADR 0192).

## Context

- `decommission_lpar` writes directly: a PowerOff job, one VIOS mapping detach per vSCSI
  mapping, one delete per client adapter, then the partition delete
  (`operations/lpar/decommission.py`, main `35174c09`). A repeat after an interruption starts
  over from a fresh inventory, and nothing records which writes landed.
- The engine replays a body against its journal: an `applied` key is skipped, an open one is
  classified by a live read before the body runs, and a kind without a classifier pauses (ADRs
  0195, 0236). Keys are a persisted contract.
- Each run re-reads the inventory, and what the HMC lists shrinks as writes land: a detached
  mapping and a deleted adapter are no longer listed, and after the delete the partition is not.
- `hmc_decommission_lpar` is itself the specialist the H1 action table delegates to for
  `retain`. Its security today is target kind `lpar`, exhaustive, with required
  `lpar_name_or_uuid` and `system_name_or_uuid` selectors, so a targets table can grant it.
- ADR 0190 Decision 3: a continuation needs only `request_id`; other inputs it gives must match.

## Decision

1. **Inputs.** `request_id` is required on the MCP tool and as the CLI's `--request-id`, with
   no compatibility path. Both add `continuation` (`none` / `resume` / `abandon`); the tool adds
   `wait_seconds`. ADR 0027's inputs stay and are recorded with the request. `dry_run` validates
   `request_id` and stops: no operation, lock or guard. `dry_run` with a continuation is refused.
2. **Authorization stays the tool's own.** The security keeps kind `lpar` and exhaustive
   targets; both selectors become optional so a continuation can omit them. Under `all-targets`
   a continuation needs only `request_id`. Under a targets table an omitted selector is denied
   (ADR 0039), so such a caller repeats both selectors, and the engine refuses any value that
   differs from the record. No delegated specialist is added.
3. **Preconditions refuse before recording.** A new request runs the dry-run inventory, ownership
   check included, and raises its error to the caller with nothing recorded. A repeat or a
   continuation does not.
4. **Effects**, keyed by the resource each removes, so a re-read inventory that no longer lists a
   removed resource cannot shift a later key:

   | Key | Kind | Target | Identity | Classifier |
   | --- | --- | --- | --- | --- |
   | `power_off` | `lpar.power_off` | `lpar:<uuid>` | `{job_id}` | `not activated` → applied; else attention (ADR 0199's rule) |
   | `unmap.<16 hex>` | `vios.storage_unmap` | `vios:<uuid>/mapping:<id>` | `{vios_uuid, mapping_id}` | the VIOS's vSCSI mappings for the partition, read completely: listed → not applied; absent → applied; incomplete → stays open |
   | `adapter.<uuid>` | `lpar.client_adapter_delete` | `lpar:<uuid>/<type>:<uuid>` | `{type, uuid}` | the partition's complete adapter inventory: listed → not applied; absent → applied |
   | `delete_lpar` | `lpar.delete` | `lpar:<uuid>` | `{lpar_uuid}` | the system's partition list: listed → not applied; absent → applied |

   The 16 hex digits are the first of the SHA-256 of `<vios uuid>|<mapping id>`, since a mapping
   id (`vhost0/vtscsi0`) is not a valid key. UUIDs are lower-cased. A partition already `not
   activated` with no `power_off` effect gets no effect, as today.
5. **Outcomes.** A step that ends in error leaves its write `uncertain` (raised, not returned),
   stops the run and ends `needs_attention`, holding the guard; `resume` classifies and
   continues from a fresh inventory, re-checking ownership. A run whose inventory is incomplete
   or whose state check fails ends `needs_attention` without an open effect. A clean run ends
   `completed`. Once `delete_lpar` is applied a run writes and reads nothing and ends
   `completed`. Phases are `validating` and `tearing_down`.
6. **Guard.** The body takes the ADR 0190 partition guard on the resolved system and partition
   UUIDs before its first write; a partition another operation holds ends `failed`.
7. **Result.** `DecommissionResult` keeps its fields and adds `operation`. The body's result is
   returned when the outcome is `completed`, or `needs_attention` with steps that agree with the
   journal; otherwise the steps are rebuilt from the effects (applied → `ok`, else `error`),
   `workflow_completed` is false and `resource_deleted` follows `delete_lpar`.

## Consequences

- Callers must send `request_id`; a repeat returns the same operation and never powers off,
  detaches or deletes twice. The CLI is refused while another process holds the execution lock.
- A failed PowerOff or detach now pauses for `resume` or `abandon` instead of returning; the
  partition stays guarded until then.
- After a resume, `blast_radius` describes what remained at that run, not the first inventory;
  the journal holds what was removed.
- A result larger than the store's 64 KiB bound pauses the operation for attention.
- Ledger entries are untouched; #1848 removes them with `delete_owned`.
- Effect keys and kinds are a persisted contract (ADR 0195). `decommission_lpar` keeps its
  direct path for Python callers (ADR 0029) and takes an optional journal.

## Considered & rejected

- **Do nothing.** judgment: fit. #1847 and the H1 spec require the journaled tool.
- **Index keys by inventory position (`unmap.0`).** judgment: fit. A resumed run re-reads an
  inventory without the removed mappings, so positions shift onto other resources.
- **Make the tool `console` and non-exhaustive, as `hmc_power_lpar`.** judgment: fit. A targets
  table could no longer grant decommission, which it can today (`tests/app/
  test_target_authorization.py::test_dry_run_changes_no_authorization_decision`, main
  `35174c09`), and `retain` names no specialist to carry the bound instead.
- **Delegate to `hmc_power_off_lpar`, `hmc_delete_adapter` and `hmc_delete_lpar`.** judgment:
  fit. The H1 action table gives `retain` the tool's existing set; a delegated set is a new
  authorization contract the issue does not ask for.
- **Return the bare operation record, as power does.** judgment: fit. The H1 row keeps
  `DecommissionResult` and adds the operation fields; the live arm reads its fields.
- **Persist the first inventory and replay it.** judgment: complexity. The engine has no store
  for a body's plan, and a fresh inventory re-checks ownership and what is still attached.
