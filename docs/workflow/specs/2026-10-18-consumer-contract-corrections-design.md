# Approved consumer contract corrections

## Authority and scope
The operator approved all five queued changes on 2026-10-18. Implement them on
refactor/mid-october in separate conventional commits. Preserve the six-name
stable facade, authorization, valid persisted data, schema, resource ownership,
terminal destination policy, console streaming and cancellation limits.
Root owns execution. This is a full-spec lane: complexity M, denominator250 per
bounded task, with interface/persistence hazards. No new dependency, compatibility
re-export, database migration or hardware operation is required.

## Changes and acceptance
1. Remove only cli.console and cli.GlobalOpts re-export import blocks. Definitions
   remain in output/runtime; repository consumers already import those owners.
   CLI composition, help and documented internal seams continue loading.
2. Name the existing phase/intent/outcome Literal as EventKind; use it in the
   model and store writer. Reject invalid kinds before sequence/insertion/pruning
   using existing vocabulary validation. Validate returned persisted events before
   construction, raising existing store_corrupt guidance. Schema, ordering,
   retention, truncation and valid event serialization remain unchanged.
3. Keep capture_lpar_console_by_selector(hmc, lpar, *, system_name_or_uuid=...)
   target-first and migrate every caller. System remains required. Preserve
   selector resolution, scope, SSH names and all capture options.
4. Render exception notes in insertion order after the main CLI error using
   terminal_field(err=True) and literal Rich Text. Preserve exit codes, guidance,
   resource-not-found rewriting and stderr destination; no notes means no change.
5. After rendering provisioning results, exit1 when the invocation is not a dry
   run and workflow_completed is false. Keep partial results in text/JSON, and
   successful workflows and successful validation-only dry runs exit0.

## Verification inventory
- Exports: low-impact unused import deletion; developer guidance rules out a new
  implementation-mirroring test. Existing CLI helper tests and all16 help groups
  prove composition still works; one exhaustive consumer search verifies removal.
- Event kinds: focused-test in tests/unit/test_logical_store.py. Invalid writer
  values fail before SQL mutations; corrupt returned rows raise store_corrupt;
  phase/intent/outcome round-trip and existing truncation tests remain green.
- Console: focused-test in tests/unit/test_console_capture.py rejects positional
  system and accepts the required keyword. Real selector calls through MCP and
  CLI retain distinct system/partition identity; run console CLI/interrupt tests.
- Notes: focused-test in CLI output tests drives actual command failure, ordered
  notes, markup literals, terminal controls and unchanged no-note behavior.
- Provision: focused-test in tests/app/test_cli_commands.py covers text/JSON,
  failure before/after creation, completion and dry run, retaining rendered data.
Run red before implementation, focused regressions and lint/types/format before
each commit; preserve all hooks. Independently review the assembled delta, then
run full just verify and pinned all-files prek. No live-HMC/CI claims.
