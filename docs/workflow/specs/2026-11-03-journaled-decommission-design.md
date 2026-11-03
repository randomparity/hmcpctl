# Journaled `hmc_decommission_lpar`

**Issue:** #1847 (piece 1 of #1229, epic #1215) · **Decision:**
[ADR 0320](../../adr/0320-decommission-is-a-journaled-operation.md) · **Contract:**
[H1 spec](2026-10-01-logical-lpar-workflows-design.md) · **Branch:**
`feat/journaled-decommission-1847` · **Guardrails:** `just verify`;
`uv run --no-sync prek run --all-files`

## Problem

`decommission_lpar` writes directly to the HMC. A call interrupted after its power-off or a
detach cannot be reconciled, and a repeat starts over with nothing recording what landed.

## Scope

In: the MCP tool and the CLI mirror run the existing `retain` decommission through the logical
engine, as ADR 0320 decides. Out: `storage_cleanup`, the `storage` result block, ledger reads
and removal, and `exclusive_writer_window` (#1848).

**Components.**

- `operations/lpar/decommission.py` keeps the inventory, steps and `decommission_lpar`, which
  gains `journal: DecommissionJournal | None = None`. `DecommissionJournal` is a `Protocol` with
  `set_phase(name)`, `guard(system_uuid, partition_uuid)`, `recorded(key)` and
  `effect(key, kind, target, write)`; `OperationContext` satisfies it. Without a journal each
  write runs directly, as today. With one, every write goes through `journal.effect` with the
  ADR 0320 key, kind and target, and a write that ends in error raises inside the effect so it
  settles `uncertain`; the step function catches the raise and records the same error step as
  today. `_Inventory` gains `system_uuid`. With a journal, `journal.guard` takes the lower-cased
  system and partition UUIDs right after the inventory resolves them, before any early return.
  `DecommissionResult` gains `operation: OperationRecord | None = None`.
- New `operations/lpar/decommission_operation.py`: `EFFECT_KINDS`, `effect keys` helpers,
  `decommission_body(arguments, client_factory)`, `decommission_classifiers(client_factory)`
  and `result_from_record(record)`, following `provision_operation.py`.
- `server_tools/lpar/lifecycle.py`'s `hmc_decommission_lpar` keeps its `@tool` registration
  (kind `lpar`, exhaustive). `request_id` is its first parameter; the two selectors and
  `dry_run`, `ownership_override`, `immediate`, `timeout_seconds` and `poll_interval` default to
  `None` (a new request fills the old defaults from `DEFAULT_ARGUMENTS`); it adds `continuation`
  and `wait_seconds`. The handler validates `request_id`,
  refuses `dry_run` with a continuation, merges given arguments over the recorded ones on a
  continuation (refusing `not_found` when none exist), requires both selectors for a new one,
  runs the dry-run inventory for `dry_run` or a new request (raising its error before recording),
  then `engine.submit`s and returns `result_from_record`.
- CLI `hmcpctl lpars decommission`: required `--request-id`, `--continuation`; the partition
  argument and `--system` become optional for a continuation, which reads the recorded
  arguments and renders with the recorded selectors; waits for the worker
  (`engine.join(..., None)`), renders as today plus the operation id/state/outcome, and exits 1
  unless `completed` (or `abandoned` after `abandon`).
- Live scripts (`scripts/live_test/lpar_power.py`, `scripts/live_test/provisioning.py`) pass a
  run-unique `request_id` and `wait_seconds=600`, and when the returned operation is not terminal
  poll `hmc_operation_status` until it is (the arm's PowerOff timeout is 900 s). `lpar_power`
  registers its decommission request and its teardown abandons it with `hmc_decommission_lpar`.
- Docs: `CHANGELOG.md` (breaking entry), `docs/tools/` (regenerated), `docs/mcp-server.md`,
  `docs/capabilities/maturity.json` (a `journaled-operation` variant),
  `docs/capabilities/operations.json` (the tool's signature), `docs/cli.md`'s decommission examples
  (they gain `--request-id`; a necessary consequence of criterion 1), `docs/live-testing.md`'s arm
  narrative, ADR 0320. `docs/authorization-audit.md` is checked and changes only if it states the
  selectors are required. The kdive
  Tier A contract does not use decommission (`rg -n decommission docs/kdive-tier-a-contract.md`
  is empty), so it and its test are unchanged.

## Failure model

1. **Actors and deployments:** an MCP client over stdio holding a policy that grants
   `hmc_decommission_lpar`; an operator at the CLI; the live-test arm. One server process holds
   the execution lock.
2. **Invariants and assets at stake:** the partition and its client adapters (deleted
   irreversibly); backing storage and foreign mappings (must be retained, ADR 0027); the ADR
   0011 ownership check before each write phase; the persisted effect keys and kinds.
3. **Accepted failure classes:**
   - A result over 64 KiB pauses the operation (bounded; the journal still records the writes).
   - A PowerOff that failed, timed out or was orphaned with the partition not yet `not activated`
     pauses on every resume (ADR 0199's rule); the exit is `abandon` and a new `request_id`.
   - A mapping re-created with the same device names by an out-of-band writer after its detach was
     applied is not detached again; a stale VIOS mapping may remain, storage is retained, and it is
     recoverable by hand.
   - A resume after the delete returns a rebuilt result whose `blast_radius` may be minimal; the
     journal records what was removed.
4. **Covered elsewhere:** owned-storage deletion and ledger removal (#1848); the hold interlock
   (#1231); store loss and lock semantics (ADR 0190).

## Threat model

1. **Boundary inventory:** widened — the tool's selectors become optional, so a call can name no
   target. Added — a continuation re-runs writes against recorded targets.
2. **Actor model:** an MCP client bounded by the server's access policy; trust rests on the
   policy and on `HMC_AGENT_ID` isolation (ADR 0190).
3. **Control per boundary:** an omitted selector under a targets table is denied by the existing
   dispatch check (ADR 0039); under `all-targets` any target is already granted. A continuation's
   given selectors must equal the record (engine binding check). Ownership is re-checked from a
   fresh inventory on every run before writing.
4. **Explicitly out of scope:** an agent sharing another's `HMC_AGENT_ID` (ADR 0190 accepts it).

## Success

1. A call without `request_id` is a schema error on the tool and a usage error on the CLI.
2. A new request whose inventory or ownership check raises records no operation; an incomplete
   vSCSI inventory is recorded and ends `needs_attention` with no effect.
3. A clean run ends `completed` with the effects `power_off` (when the partition was active),
   one `unmap.*` per vSCSI mapping, one `adapter.*` per client adapter, then `delete_lpar`, all
   `applied`; the result carries `resource_deleted` true and `operation`.
4. A repeat with the same arguments returns the same operation and makes no HMC write.
5. A write that raises leaves its effect `uncertain` and the operation `paused/needs_attention`;
   `resume` classifies it with the ADR 0320 classifier and continues without repeating an
   `applied` write; `abandon` ends `abandoned` with no write.
6. With `delete_lpar` applied, a resume ends `completed` with no HMC read or write, and the tool
   returns the ADR 0320 Decision 7 rebuilt result.
7a. A continuation naming only `request_id` (after a start with `immediate=true`) is admitted.
7b. A paused `hmc_power_lpar` operation on the partition makes a new decommission end `failed`
   (`partition_busy`), whatever case the HMC returns its UUID in.
7c. A record returned while running, or paused before any body result, yields a valid
   `DecommissionResult` (Decision 7 rebuild).
7. `dry_run` returns the inventory with `operation` null and records nothing.
8. A table grant naming the system and partition still admits a full call and denies one that
   omits a selector.

## Validation

| Contract | Mode | Evidence |
| --- | --- | --- |
| Success 1, 2, 7, 8 | focused-test | `tests/lpar/test_decommission_operation.py` (tool handler), `tests/app/test_target_authorization.py` |
| Success 3–6 and each classifier | focused-test | `tests/lpar/test_decommission_operation.py` with the engine on a temporary state dir and a fake client |
| Direct path unchanged | focused-test | `tests/lpar/test_decommission_tool.py`'s `decommission_lpar` cases stay green; its MCP-handler cases gain `request_id` |
| Success 7a–7c | focused-test | `tests/lpar/test_decommission_operation.py` |
| Schema defaults and option set | focused-test | `tests/app/test_capabilities.py`, `tests/app/test_cli_commands.py` help test |
| operations.json signature | focused-test | `just capability-inventory` |
| CLI | focused-test | `tests/app/test_cli_commands.py` decommission cases |
| Live arm request ids | focused-test | `tests/test_live_lpar_power_arm.py`, `tests/test_live_round2_recovery.py` |
| Tool schema docs | focused-test | `just tool-docs-check` after `just tool-docs` |
| maturity variant | focused-test | `just test` capability inventory tests |
| ADR/spec prose | task-test-not-applicable | prose; `just adr-numbering` checks only the number |
