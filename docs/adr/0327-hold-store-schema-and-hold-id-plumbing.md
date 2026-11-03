# 0327 — Holds upgrade the store to schema 2 in place, and the front ends carry `hold_id` centrally

## Status

Accepted (2026-11-03), issue #1851 (piece 1 of #1231). Implements ADR 0193 Decisions 1, 2, 5
and 7 on the ADR 0190 store; ADR 0193's prepare/release tool is #1852.

## Context

- ADR 0190 creates the store at schema `"1"` with no holds table and refuses any other
  `schema_version` as corrupt. Stores already exist on operator hosts and carry live operations,
  partition guards and the resource ledger. ADR 0190 says nothing about changing the schema.
- ADR 0193 Decision 2 needs `hold_id` on every partition-mutation tool and `--hold-id` on every
  partition-mutation CLI command. On `main` at `41744bd2`, 55 registered non-`read` tools take a
  partition selector, and about 70 CLI commands mirror them.
- The `authorized()` wrapper and `ToolDefinition` already see every registered tool's
  `ToolSecurity`, and every CLI command passes through one `CLICommand` class in
  `cli.py`'s `_register_commands`.

## Decision

1. **Schema 2, upgraded in place.** `SCHEMA_VERSION` becomes `"2"`; schema 2 is schema 1 plus a
   `holds` table keyed by (system UUID, partition UUID). Opening a schema-1 store upgrades it in
   one `BEGIN IMMEDIATE` transaction that re-reads the version, creates the table and sets
   `schema_version` to `"2"`, so two processes upgrading at once leave one result. Any version
   other than `"1"` or `"2"` stays corrupt (ADR 0190 Decision 9). An older hmcpctl reading a
   schema-2 store therefore refuses it, and with it every logical mutation and ADR 0193 guarded
   operation, rather than ignoring holds it cannot see.
2. **The registry injects `hold_id`.** A tool is *hold-guarded* when its effect is not `read`,
   it is not exempt (`hmc_capture_lpar_console`, `hmc_prepare_host_handoff`), and one of its
   target selectors, nested ones included, is of kind `lpar`. `ToolDefinition` publishes a
   hold-guarded tool with one more optional argument, `hold_id`, unless the handler declares
   `hold_id` itself. The `authorized()` wrapper removes the injected argument before calling the
   handler and, for every tool, records the tool name, whether it is a read, and any `hold_id`
   in a call context the hook reads. Handlers do not change.
3. **The CLI names its mirrored tool in one table.** A table maps every CLI command path to the
   registry tool it mirrors, or to none. `CLICommand` sets the same call context from that
   table and adds `--hold-id` to every command whose mirrored tool is hold-guarded. A command
   mirroring no tool is treated as a mutation, as ADR 0193 treats a resolution with no context.
4. **The worker inherits the call.** A logical operation's worker thread runs in a copy of the
   submitting call's context, so a resolution inside the operation body sees the call that
   started or continued it.

## Consequences

- Upgrading is one-way: rolling hmcpctl back after any schema-2 process opened the store needs
  the store deleted or restored from a schema-1 backup. `docs/mcp-server.md` says so.
- Every hold-guarded tool's schema gains `hold_id`, so `docs/tools/` regenerates and a tool
  added later gains it with no handler edit. A new tool with a partition selector and a
  hand-written `hold_id` keeps its own.
- A new CLI command fails the mirror-table exhaustiveness test until it is listed.

## Considered & rejected

- **Keep schema 1 and create the table with `IF NOT EXISTS`.** judgment: fit. An older hmcpctl
  would open the store, see no reason to refuse, and run guarded mutations past holds it does
  not read — ADR 0193 Decision 5 turned into a silent bypass.
- **Refuse a schema-1 store and require the operator to delete it.** judgment: cost. It discards
  live operations, guards and ledger entries to add an empty table.
- **Add `hold_id` to each handler and CLI command by hand.** judgment: complexity. About 120
  edits whose omission is invisible until the coverage test, and every future tool repeats it.
- **One root `hmcpctl --hold-id` option.** judgment: fit. ADR 0193 names `--hold-id` on the
  partition commands, and a root option would also appear on commands that cannot use it.
- **Skip the worker context and re-check holds only at admission.** judgment: fit. A
  continuation's resolutions run in the worker, where a missing context reads as a mutation
  without the caller's `hold_id` and refuses the holder itself.
