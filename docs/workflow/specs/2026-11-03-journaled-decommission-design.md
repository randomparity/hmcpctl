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
  today. `_Inventory` gains `system_uuid`. `DecommissionResult` gains
  `operation: OperationRecord | None = None`.
- New `operations/lpar/decommission_operation.py`: `EFFECT_KINDS`, `effect keys` helpers,
  `decommission_body(arguments, client_factory)`, `decommission_classifiers(client_factory)`
  and `result_from_record(record)`, following `provision_operation.py`.
- `server_tools/lpar/lifecycle.py`'s `hmc_decommission_lpar` keeps its `@tool` registration
  (kind `lpar`, exhaustive). `request_id` is its first parameter; the two selectors default to
  `None`; it adds `continuation` and `wait_seconds`. The handler validates `request_id`,
  refuses `dry_run` with a continuation, merges given arguments over the recorded ones on a
  continuation (refusing `not_found` when none exist), requires both selectors for a new one,
  runs the dry-run inventory for `dry_run` or a new request (raising its error before recording),
  then `engine.submit`s and returns `result_from_record`.
- CLI `hmcpctl lpars decommission`: required `--request-id`, `--continuation`; the partition
  argument and `--system` become optional for a continuation; waits for the worker
  (`engine.join(..., None)`), renders as today plus the operation id/state/outcome, and exits 1
  unless `completed` (or `abandoned` after `abandon`).
- Live scripts (`scripts/live_test/lpar_power.py`, `scripts/live_test/provisioning.py`) pass a
  run-unique `request_id` and `wait_seconds=600`; `lpar_power` registers its decommission
  request with the requests its teardown abandons.
- Docs: `CHANGELOG.md` (breaking entry), `docs/tools/` (regenerated), `docs/mcp-server.md`,
  `docs/capabilities/maturity.json` (a `journaled-operation` variant), ADR 0320. The kdive
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
   - A PowerOff job queued but not run when the process died pauses on every resume until the
     partition reads `not activated` (ADR 0199's rule; `abandon` is the exit).
   - A mapping re-created with the same device names after its detach was applied is not
     detached again by that key; the adapter refresh and the partition delete still run their own
     checks (not reachable without an out-of-band writer).
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
2. A new request whose inventory or ownership check refuses records no operation.
3. A clean run ends `completed` with the effects `power_off` (when the partition was active),
   one `unmap.*` per vSCSI mapping, one `adapter.*` per client adapter, then `delete_lpar`, all
   `applied`; the result carries `resource_deleted` true and `operation`.
4. A repeat with the same arguments returns the same operation and makes no HMC write.
5. A write that raises leaves its effect `uncertain` and the operation `paused/needs_attention`;
   `resume` classifies it with the ADR 0320 classifier and continues without repeating an
   `applied` write; `abandon` ends `abandoned` with no write.
6. With `delete_lpar` applied, a resume ends `completed` with no HMC read or write.
7. `dry_run` returns the inventory with `operation` null and records nothing.
8. A table grant naming the system and partition still admits a full call and denies one that
   omits a selector.

## Validation

| Contract | Mode | Evidence |
| --- | --- | --- |
| Success 1, 2, 7, 8 | focused-test | `tests/lpar/test_decommission_operation.py` (tool handler), `tests/app/test_target_authorization.py` |
| Success 3–6 and each classifier | focused-test | `tests/lpar/test_decommission_operation.py` with the engine on a temporary state dir and a fake client |
| Direct path unchanged | focused-test | existing `tests/lpar/test_decommission_tool.py` stays green |
| CLI | focused-test | `tests/app/test_cli_commands.py` decommission cases |
| Live arm request ids | focused-test | `tests/test_live_lpar_power_arm.py`, `tests/test_live_round2_recovery.py` |
| Tool schema docs | focused-test | `just tool-docs-check` after `just tool-docs` |
| maturity variant | focused-test | `just test` capability inventory tests |
| ADR/spec prose | task-test-not-applicable | prose; `just adr-numbering` checks only the number |
