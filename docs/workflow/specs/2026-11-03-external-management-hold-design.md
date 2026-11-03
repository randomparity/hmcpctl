# External-management hold store and partition resolver hook

**Issue:** #1851 (piece 1 of #1231, epic #1215) · **Decisions:**
[ADR 0193](../../adr/0193-host-handoff-external-management-hold.md),
[ADR 0327](../../adr/0327-hold-store-schema-and-hold-id-plumbing.md), on
[ADR 0190](../../adr/0190-durable-logical-operation-store.md) · **Contract:**
[H1 spec](2026-10-01-logical-lpar-workflows-design.md), *Host handoff* · **Branch:**
`feat/external-management-hold-1851` · **Guardrails:** `just static`, `just test-group <g>` for the
four groups, `just smoke`, `just build verify-artifacts`; `uv run --no-sync prek run --all-files`

## Problem

Nothing implements ADR 0193's hold: the store has no holds table, no partition resolution checks
for one, no tool or CLI command accepts `hold_id`, and an operator cannot list or release holds.

## Scope

In: the hold store, the call context, the resolver hook and its installation by the MCP server and
CLI, `hold_id`/`--hold-id` plumbing, the registry-driven coverage test, and `hmcpctl holds list` /
`hmcpctl holds release`. Out (#1852): `hmc_prepare_host_handoff`, its handoff document,
`docs/kdive-tier-a-contract.md` and the logical-path interlock tests. Out (kdive, ADR 0193 D6):
anything kdive-side, and fencing the library, other hosts, the GUI or direct HMC commands.

**Components.**

- **Store (`operations/logical/store.py`).** Schema `"2"` adds
  `holds (system_uuid TEXT, partition_uuid TEXT, hold_id TEXT NOT NULL, consumer_label TEXT NOT
  NULL, agent_id TEXT NOT NULL, connection TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY
  (system_uuid, partition_uuid))`. Every hold function lower-cases its UUID arguments on entry, so storage and lookup are
  case-insensitive. `_open_existing` upgrades a schema-1 store in place for both `session` and
  `read_session` (ADR 0327 D1), so the first hook read of an old store upgrades it.
  `insert_operation`'s `store_full` count adds holds.
- **Hold API (new `operations/logical/holds.py`).** `Hold` dataclass (`system_uuid`,
  `partition_uuid`, `hold_id`, `consumer_label`, `agent_id`, `connection`, `created_at` ISO-8601).
  - `place_hold(system_uuid, partition_uuid, *, consumer_label, agent_id, connection) -> Hold`:
    validates the label (1–64 characters, no control characters); refuses `partition_guarded`
    naming the `operation_id` while a non-terminal operation guards the partition (UUIDs compared
    with `lower()` on both sides, since `operations` rows keep the caller's case); returns the
    existing hold when the same agent id and label placed it; otherwise refuses `partition_held`
    naming label, agent id and creation time, never the `hold_id`; counts toward `store_full`;
    mints `hold_id` as 32 lower-case hex digits.
  - `release_hold(system_uuid, partition_uuid, *, agent_id, hold_id=None) -> Hold | None`:
    deletes the hold when `hold_id` matches or `agent_id` placed it; `None` when no hold exists;
    otherwise refuses `hold_mismatch` without naming the `hold_id`.
  - `find_hold(partition_uuid) -> Hold | None` (any system), `list_holds() -> tuple[Hold, ...]`,
    `operator_release(partition_uuid) -> tuple[Hold, ...]`. Reads use `read_session` and never
    create a store; any store failure raises the ADR 0190 `OperationRefused`.
  - `holds_present() -> bool`: whether any hold exists (no store → false).
  - `enforce_hold(partition_uuid)`: the hook body below.
  #1852 builds its tool on `place_hold`, `release_hold` and `find_hold` unchanged.
- **Call context (new `hmcpctl/call_context.py`).** `CallContext(tool: str | None, read: bool,
  hold_id: str | None)` in a `ContextVar`, `current_call() -> CallContext | None` and the context
  manager `calling(context)`, plus `HOLD_EXEMPT_TOOLS` (`hmc_capture_lpar_console`,
  `hmc_prepare_host_handoff`). No dependency on the registry or the store; `holds.py` and
  `tool_registry.py` both import it.
- **Hook (`resource_identity.py`).** `install_partition_guard(guard: Callable[[str], None] |
  None)` and `guard_partition(partition_uuid)`, which calls the installed guard and does nothing
  when none is installed (the library). `enforce_hold`:
  1. context present and `read` → pass; tool in `HOLD_EXEMPT_TOOLS` → pass;
  2. `find_hold` (a store failure propagates: Decision 5); no hold → pass;
  3. presented `hold_id` equal by `hmac.compare_digest` on UTF-8 bytes → pass;
  4. else `HoldRefused("partition_held", ...)` naming partition, label, agent id and creation
     time and how to proceed (present the `hold_id`; `hmcpctl holds release`). `HoldRefused` is
     not a `ValueError`, so domain code that rewraps a resolver's `ValueError` cannot hide it,
     and a logical operation body that raises it ends `failed` rather than pausing.
  No context means a mutation with no `hold_id`.
- **Hook sites.** `resolve_lpar_uuid` calls `guard_partition` on the UUID it returns, which covers
  `resolve_and_authorize_lpar_mutation`, `resolve_and_authorize_lpar_names` and
  `_resolve_contained_lpar_names`; decommission's `resolve_target_lpar` and the SSH selectors in
  `ssh/selectors.py` call it on the UUID they resolve. `_resolve_ssh_names` passes two names
  through unresolved, so before that early return it calls
  `guard_partition_name(config_or_hmc, system_selector, lpar_name)`. That function does nothing
  unless a guard is installed, the context is a non-exempt mutation, and `holds_present()`; then it
  resolves the name with `resolve_lpar_uuid` (opening an `HMCClient` from an `HMCConfig` when
  needed), which runs the guard. A name it cannot resolve — REST unreachable, not found or
  ambiguous — refuses `partition_hold_unverified` (fail closed; Failure model 3). Hosts without
  holds make no extra HMC read. The coverage test below is the closure: any other path it shows
  is hooked the same way. It found, and this change hooks, the partition matchers in
  `operations/lpar/power.py`, `memory.py` (shared by memory, processor and statistics tools),
  `accelerator.py`, `paging.py` and `profiles.py`.
- **Registry (`tool_registry.py`).** `RUNTIME_PARTITION_TOOLS = {"hmc_detach_storage_mapping"}`
  (tools whose partition is derived at run time, not selected) and
  `hold_guarded(name, security) -> bool` (ADR 0327 D2). One helper gives a hold-guarded handler's
  wrapper a `__signature__` and `__annotations__` with an extra keyword-only
  `hold_id: Annotated[str | None, Field(description=...)] = None`; both
  `ToolDefinition._schema_template` and `authorized()` use it, so the published schema and
  FastMCP's run-time argument validation (`FunctionTool.run` validates against `self.fn`'s
  signature) agree. Handlers that declare `hold_id` themselves get no injection. `authorized()`
  pops the injected `hold_id`, binds the remaining arguments to the handler signature for the
  policy check, and runs the handler inside `calling(...)` for every tool. The handler never sees
  an injected `hold_id`, so it never enters a logical tool's request arguments or digest.
- **Installation.** `server.create_mcp` and the CLI root callback call
  `install_partition_guard(enforce_hold)`; nothing in the library does.
- **Engine.** `_start_worker` runs `_work` in `contextvars.copy_context()` (ADR 0327 D4).
- **CLI.** New `cli_commands/tool_mirrors.py` holds `CLI_TOOL_MIRRORS: Mapping[str, str | None]`
  keyed by command path (`"lpars power-on"`). `CLICommand` adds a `--hold-id` option to commands
  whose mirror is hold-guarded, pops it in `invoke`, and runs the command inside `calling(...)`.
  New `cli_commands/holds.py`: `hmcpctl holds list [--json]` and
  `hmcpctl holds release PARTITION_UUID [--yes]`, both local-only with no HMC call.
- **Docs.** `docs/mcp-server.md` (holds under *Logical operation state*; the upgrade is
  one-way; restart every hmcpctl process after upgrading, and an old process reporting `unknown
  schema version` needs a restart, not a store deletion; restoring or deleting the store drops
  holds, so list them first and re-place them), `docs/cli.md` (`holds` group, `--hold-id`), regenerated `docs/tools/`, and
  `docs/capabilities/maturity.json` only where `just capability-inventory` requires a change: this
  issue adds no registry operation.

## Failure model

1. **Actors and deployments.** MCP clients on one server; stdio servers and CLI commands on one
   host sharing one state directory; the local operator at the CLI; library callers (kdive) in
   their own process, with no hook.
2. **Invariants and assets.** A held partition is not mutated without its `hold_id` through a
   hold-guarded tool or a CLI command mirroring one, on this host's MCP server or CLI; the
   `hold_id` is returned only by `place_hold`, to its caller, and never appears in a refusal, a
   log line or `holds list`; existing stores keep their operations, guards and ledger across the
   upgrade; an unusable store refuses guarded operations.
3. **Accepted failure classes.**
   - Library callers, other hosts, the GUI and direct HMC commands bypass holds (ADR 0193 D6).
   - Tools that never resolve a partition are not hold-checked: system-scoped mutations
     (`hmc_restore_lpar_profiles`, system power and system profile activation) and the escape
     hatches (`hmc_run_command`, `hmcpctl raw`). ADR 0193 D2 places the check in partition
     resolution (ADR 0327 D5); these need explicit grants.
   - A hold placed while a guarded call is already past its resolution does not stop that call;
     `place_hold` refuses only on an ADR 0190 partition guard, which covers logical operations.
   - Two partitions with the same UUID on different systems: `find_hold` matches by partition UUID
     alone and so refuses more, never less.
   - While any hold exists, a name-addressed SSH mutation whose name cannot be resolved over REST
     (REST down, not found, ambiguous) is refused `partition_hold_unverified`: fail closed.
   - Version skew: an hmcpctl older than this change refuses the upgraded store as
     `store_corrupt`; restart or upgrade it (ADR 0327 consequences, `docs/mcp-server.md`).
   - Restoring a backup or deleting the state directory drops holds placed since; the operator
     lists holds first and re-places them (`docs/mcp-server.md`).
   - A tool whose second partition selector (`target_lpar_name_or_uuid`) is held by a different
     hold cannot present both ids; it is refused.
4. **Covered elsewhere.** Prepare/release over MCP and the handoff document: #1852. Store loss of
   operations and ledger: ADR 0190.

## Threat model

- **Boundaries.** Added: presented `hold_id` → hold check. Widened: store file → authorization
  of guarded mutations (H1 threat model boundary 4).
- **Actors.** An MCP client with a narrower grant than the holder; a local user who can write the
  state directory.
- **Controls.** Constant-time comparison; refusals never echo the `hold_id`; the store keeps its
  ADR 0190 private-mode checks; a store that is unreadable, corrupt, not private or lost refuses.
- **Out of scope.** A local user who can write the state directory can delete holds — the same
  standing as `hmcpctl holds release` (ADR 0193 D7).

## Success

1. `place_hold`/`release_hold`/`find_hold`/`list_holds`/`operator_release` behave as above, with
   holds counted toward `store_full`.
2. A schema-1 store opens as schema 2 with its rows intact, through `session` and
   `read_session`, and two connections upgrading at once both succeed; an unknown version
   refuses.
3. Every hold-guarded tool in `TOOL_SECURITY` publishes `hold_id`, and, called on a held partition
   through `create_mcp` by UUID (upper- and lower-case) and by name, is refused with
   `partition_held` before any HMC write; with the matching `hold_id` the hook runs and passes,
   directly and through `hmc_invoke_tool`. Every non-read tool with a VIOS
   selector and no partition selector that reaches the guard is hold-guarded. Reads and the
   exempt tools pass. A logical tool repeated with a different `hold_id` is the same request.
4. A corrupt store refuses a guarded call; with no hook installed nothing is checked.
5. Every registered CLI command is in `CLI_TOOL_MIRRORS`, each named tool exists, `--hold-id`
   appears exactly on commands mirroring a hold-guarded tool, and a held CLI mutation is refused.
6. `hmcpctl holds list` and `holds release` list and release holds without an HMC.

## Validation

Plan: `docs/workflow/plans/2026-11-03-external-management-hold.md` (local, not committed).
Tests: `tests/unit/test_holds.py` (store, API, upgrade, hook), `tests/app/test_hold_coverage.py`
(registry-driven), `tests/app/test_cli_holds.py` (CLI table, `--hold-id`, operator commands).
