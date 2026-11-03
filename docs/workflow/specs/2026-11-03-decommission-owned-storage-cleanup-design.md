# `delete_owned` storage cleanup for `hmc_decommission_lpar`

**Issue:** #1848 (piece 2 of #1229, epic #1215) · **Decisions:**
[ADR 0192](../../adr/0192-decommission-owned-storage-cleanup.md),
[ADR 0323](../../adr/0323-decommission-deletes-owned-storage-as-journaled-effects.md) ·
**Contract:** [H1 spec](2026-10-01-logical-lpar-workflows-design.md) · **Branch:**
`feat/delete-owned-storage-1848` · **Guardrails:** `just verify`;
`uv run --no-sync prek run --all-files`

## Problem

Decommission retains every disk and medium it finds. Storage a provision or reconfigure created
for the partition leaks, because nothing reads the ledger to decide ownership and nothing removes
an entry.

## Scope

In: ADR 0323 on the MCP tool and the CLI mirror, and the lpar-power live arm. Out: PVs,
repositories, shared or foreign media, name-prefix deletion, a policy for pre-store partitions
(#1229), the hold interlock (#1231).

**Components.**

- `operations/logical/store.py`: `remove_ledger(conn, kind, system_uuid, partition_uuid,
  match)` deletes the partition's entries of that kind whose identity holds every `match` field,
  whichever operation wrote them. `operations/logical/engine.py`:
  `OperationContext.ledger_remove(...)` wraps it in a write transaction; `engine.recorded_partition(agent_id, request_id, tool)` returns the record's
  `(system_uuid, partition_uuid)` or None, read-only.
- New `operations/lpar/decommission_storage.py` (ADR 0323 Decisions 1–4, 6): `OwnedItem`,
  `owned_items(system_uuid, partition_uuid)`, `assess(hmc, system_uuid, partition_uuid, items)`
  (the single pass; returns per-item `deletable` or a retained reason, plus the remaining
  mappings to this partition per item), `clean_storage(hmc, journal, …)` (the effects and ledger
  removal), `storage_classifiers(client_factory)` and the effect key helpers.
- `operations/lpar/decommission.py`: the `DecommissionStorage` type and
  `DecommissionResult.storage: DecommissionStorage | None`.
  `decommission_operation.py`: `DEFAULT_ARGUMENTS` gains `storage_cleanup="retain"`; the body,
  given `authorized_vios`, runs cleanup after teardown (and on the fast path where `delete_lpar`
  is already applied) when `storage_cleanup` is `delete_owned`; `preview` adds the dry-run
  storage plan; `decommission_classifiers` merges `storage_classifiers`; `result_from_record`
  carries `storage`.
- New `server_tools/lpar/decommission.py`: `DECOMMISSION_SECURITY` (equal to today's derived
  security), `decommission_handler(tool_security, permits, authorize)` and
  `register_decommission_tool`. The handler adds `storage_cleanup` and `exclusive_writer_window`,
  resolves the effective arguments first, then enforces the flag and authorizes the four
  specialists per owned VIOS against the effective (recorded) mode (Decision 5); every reader
  defaults a missing `storage_cleanup` to `retain`. Its docstring names the new mode. It is
  removed from `lifecycle.py` and registered in `server.py` and `catalog.py`.
- The rebuilt steps list only ADR 0320's four steps, so `disk.*` and `media.*` effects appear in
  `storage` only.
- CLI `hmcpctl lpars decommission`: `--storage-cleanup retain|delete_owned` and
  `--exclusive-writer-window`, with the same refusal read from the recorded mode on a
  continuation (which accepts `--exclusive-writer-window`); the confirmation prompts and the
  text renderer name the deletion and print `storage`. The CLI has no access policy, so no
  delegated check.
- Live arm `scripts/live_test/lpar_power.py`: partition P is provisioned with an owned disk
  (`storage.capacity_mib` in the arm's volume group) instead of mapping a pre-created volume, and
  decommissioned with `delete_owned`; the judged assertion `backing-volume-retained` becomes
  `owned-disk-deleted`, and the existing foreign-mapping comparison is joined by
  `shared-storage-retained`: every other volume `lsvg -lv` listed in the group, every physical
  volume `lspv` listed and the repository `lsrep` listed before are listed after. The arm then no
  longer exercises `retain` or provision mapping an existing volume natively; their recorded
  evidence (#1847's run) stays, and the CLI and unit tests keep covering both.
- Docs: `CHANGELOG.md`, `docs/tools/` (regenerated), `docs/mcp-server.md`,
  `docs/capabilities/maturity.json`; and, as unavoidable consequences outside the frozen surface
  list, `docs/capabilities/operations.json` (its gate reads the moved handler's path and
  signature), `docs/cli.md` (documents the command) and `docs/live-testing.md` (narrates the
  arm's retained volume).
- Cleanup reads owned items with the existing `store.ledger_entries`, filtered by partition, so
  its callers in flight elsewhere keep their tuple shape.
- `scripts/check_capability_inventory.py` builds the moved handler with the other factory-built
  ones; its gate reads every registered handler.

## Failure model

1. **Actors and deployments:** an MCP client over stdio under an access policy; an operator at the
   CLI; the live-test arm. One server process holds the execution lock (ADR 0190).
2. **Invariants and assets at stake:** storage not ledgered for this partition — PVs, repositories,
   foreign or shared disks and media, the test partition's own disk — is never deleted (ADR 0192
   D3); a ledgered item is deleted only after the same-pass check; provenance survives until its
   item is gone; persisted effect keys and kinds.
3. **Accepted failure classes:**
   - A writer outside the exclusive window maps an item between the check and the delete. The
     caller asserts the window; the delete client re-reads the group under `If-Match`.
   - A retained item whose partition is gone keeps an unclaimable ledger row (ADR 0323
     Consequences).
   - A result rebuilt after a pause takes `storage.retained` from the latest stored pass, less
     what the journal shows deleted, so it can be stale; the journal and ledger still hold every
     item.
   - A ledgered disk deleted out of band whose name is reused in its group is deleted as owned:
     ADR 0313 D6's identity is the name (ADR 0323 Consequences); follow-up candidate to add a
     device fingerprint to the ledger identity.
   - A 412 after the partition is deleted leaves that item for the specialists (ADR 0323
     Consequences); within an asserted window a 412 means the assertion was broken.
4. **Covered elsewhere:** store loss (ADR 0190, ADR 0192 D6); teardown order and retain behaviour
   (ADR 0320); the hold interlock (#1231).

## Threat model

1. **Boundary inventory:** widened — `hmc_decommission_lpar` gains a destructive storage path.
   Added — ledger rows written by earlier operations decide which resources are deleted.
2. **Actor model:** an MCP client bounded by the access policy. Trust rests on the store under the
   private state directory (ADR 0190) and on `HMC_AGENT_ID`; the ledger is not caller input.
3. **Control per boundary:** the four specialists are permitted and authorized against each owned
   VIOS before recording; the body refuses VIOSes outside that set; ownership is the ledger, never
   a name; the same-pass check and Decision 2's incompleteness rule bound each delete;
   `exclusive_writer_window` is required. Messages carry UUIDs and resource names only.
4. **Explicitly out of scope:** a local actor who can write the store (ADR 0190 accepts it);
   agents sharing `HMC_AGENT_ID`.

## Success

1. `retain` (the default) is unchanged and returns `storage: null`.
2. A new request or `resume` whose effective mode is `delete_owned`, without
   `exclusive_writer_window=true`, or with a specialist the policy does not permit or does not
   admit for an owned item's VIOS, is refused and records nothing.
3. A clean `delete_owned` run deletes each ledgered disk and medium of the partition referenced
   only by this partition, after `delete_lpar`, as `applied` effects of the ADR 0323 kinds, removes
   their ledger rows, and ends `completed` with them in `storage.deleted`.
4. An item referenced by another client, ledgered for another partition, on a VIOS outside the
   authorized set, absent from or duplicated in its group, or without one repository group is
   retained with that reason; its row stays.
5. Any unreadable VIOS or unclassifiable mapping retains every owned item, ends `needs_attention`,
   and `resume` deletes them once the read succeeds.
6. A delete that raises pauses `needs_attention` with the effect `uncertain`; `resume` classifies
   it by the group read and continues; a 412 retains that item (`precondition_failed`), the other
   items are still processed, and the run ends `failed` with the row kept.
6a. `storage.deleted` after a resume lists the items deleted by every run of the operation.
7. A resume after `delete_lpar` is applied authorizes and deletes against the recorded system,
   VIOS and group, with no partition lookup.
8. With no ledger rows (store loss, pre-store partition), `delete_owned` deletes nothing.
9. A dry run with `delete_owned` reports the plan in `storage`, warns when the flag is absent, and
   records nothing.
10. The live arm deletes P's owned disk and leaves every other volume in its group, every
    physical volume, the repository and every foreign mapping it listed before.

## Validation

| Contract | Mode | Evidence |
| --- | --- | --- |
| Store ledger query and removal | focused-test | `tests/unit/test_logical_store.py`, `tests/unit/test_logical_engine.py` |
| Success 3–8, classifiers, keys | focused-test | `tests/lpar/test_decommission_storage.py`, engine on a temporary state dir with a fake client |
| Success 1, 2, 9; factory security equals today's | focused-test | `tests/lpar/test_decommission_operation.py`, `tests/app/test_tool_security.py`, `tests/app/test_target_authorization.py` |
| Legacy record without `storage_cleanup` resumes as `retain` | focused-test | `tests/lpar/test_decommission_operation.py` |
| CLI options, refusal, prompt, rendering | focused-test | `tests/app/test_cli_commands.py` |
| Live arm assertions | focused-test | `tests/test_live_lpar_power_arm.py`, `tests/test_live_runner.py` |
| Tool schema docs, inventories | focused-test | `just tool-docs-check`, `just doc-freshness`, capability inventory tests |
| Success 10 on hardware | task-test-not-applicable | needs a granted live window; evidence through `scripts/live_test_evidence.py` |
