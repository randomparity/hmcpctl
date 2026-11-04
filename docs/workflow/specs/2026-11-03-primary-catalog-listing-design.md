# Default listing switch to the primary catalog, with transport scenarios

**Issue:** #1853 (child of #1232; native proof is #1854)
**Decision:** [ADR 0330](../../adr/0330-default-listing-is-the-primary-catalog.md), carrying out
[ADR 0189](../../adr/0189-logical-lpar-catalog-and-delegated-authorization.md) Decision 4
**Branch:** `feat/primary-catalog-listing-1853` from `main`
**Guardrails:** focused pytest; `just verify`; `uv run --no-sync prek run --all-files`

## Problem

`tools/list` advertises every registered tool (205 under the legacy-equivalent policy).
`PRIMARY_TOOLS` is only a metadata label (`server_tools/catalog.py`, `operation_maturity_middleware.py`).
No test drives the logical workflows over an MCP client session using only what the listing
offers, and nothing measures what the switch saves.

## Scope

In:

1. **Listing.** `OperationMaturityMiddleware` drops non-primary tools when the application was
   composed with `catalog="primary"` (ADR 0330 D1-D3). `create_mcp` gains the keyword-only
   `catalog: Literal["primary", "registered"] = "primary"` and passes the filter choice to the
   middleware. Any other value raises `ValueError` naming the two accepted values.
2. **Instructions.** `INSTRUCTIONS` in `src/hmcpctl/_app.py` is rewritten around the eleven
   primary tools: what each is for, the provision → status → inspect → reconfigure → decommission
   flow, `request_id`/`continuation` resume, the `boot_started` boundary (ADR 0191), and
   "find specialists with `hmc_search_tools`, call them by name, or through `hmc_invoke_tool`
   where it is granted". It
   keeps the addressing, ownership and `HMC_AGENT_ID` paragraphs. Every `hmc_*` name it
   mentions must be registered (existing `_mentioned_tools` intersection), and the only
   non-primary names it may mention are named as invoke targets. `ceiling_aware_instructions`
   wording changes from "absent from `tools/list` and cannot be called" to "cannot be called,
   by name or through `hmc_invoke_tool`", and its closing sentence from "`tools/list` is the
   authoritative set of what this server exposes" to "`tools/list` lists this server's primary
   tools; `hmc_search_tools` finds the rest it exposes". The `_initialize` tests in
   `tests/app/test_capability_ceiling.py` are re-decided against this text, not recomposed as
   `registered`.
3. **Harness compositions.** Callers that need specialist schemas compose with
   `catalog="registered"`: `scripts/live_test/runner.py::served_client`,
   `scripts/gen_tool_reference.py::load_descriptions`. Their downstream readers
   (`served_schemas`, `gen_scenario_definitions.py`, `scenario_gap_report.py`,
   `live_capture_sweep.py`) then need no change. `scripts/smoke_mcp.py` keeps the default and so
   reports the default listing (11 tools).
4. **Tests.** Existing tests that read the listing as the registered set compose with
   `catalog="registered"` (Client-based) or read `application.local_provider.list_tools()` /
   `list_tools(run_middleware=False)` (in-process). Tests that assert the default listing are
   updated to the new truth, not weakened. The existing
   `test_listing_marks_primary_tools_without_changing_the_listing` is replaced by a test that
   the default listing equals `PRIMARY_TOOLS` and a registered-listing test.
5. **Transport scenarios.** New `tests/app/test_primary_catalog_scenarios.py`; every scenario
   composes with the default `catalog`, opens a `fastmcp.Client` session, and calls only names
   the session's `list_tools()` returned, except the D4 direct-call scenario. Each scenario counts
   its `tools/call` requests and asserts the exact count. HMC traffic goes to the existing fakes
   (`unit.test_lpar_power.FakeHMC`, `unit.test_lpar_reconfigure.FakeHMC`,
   `test_provision_boot.BootHMC`, the inspect/plan/decommission test doubles) via the same
   `monkeypatch.setattr(<operation module>, "open_client", ...)` seams those tests use.

   | Id | Scenario | Proves |
   | --- | --- | --- |
   | S1 | listing under legacy-equivalent policy | names == `PRIMARY_TOOLS`; every tier meta `primary` |
   | S1b | listing under the documented read-only policy (`effects = ["read"]`) and under a specialist-only grant (`tools = ["hmc_list_systems"]`) | read-only: the five granted read primaries, no `hmc_invoke_tool`; a read specialist found by `hmc_search_tools` succeeds by a direct call; specialist-only: empty listing, and the instructions' ceiling block names both gateway tools as withheld |
   | S2 | create: `hmc_provision_lpar` with prepared `install`, `boot=immediate`, `wait_seconds=30`, then `hmc_operation_status` | outcome `boot_started`; no `guest_ready` anywhere in the record |
   | S3 | reconfigure: `hmc_reconfigure_lpar` `patch.memory_mib` | terminal `completed`, one `changes[]` entry |
   | S4 | inspect: `hmc_inspect_lpar` `include=["rmc","refcodes"]` | both sections `ok` |
   | S5 | destroy: `hmc_decommission_lpar` `storage_cleanup=retain` | terminal `completed` |
   | S6 | search/invoke: `hmc_search_tools(query)` → `hmc_search_tools(name)` → `hmc_invoke_tool` of a read specialist | invoke's `result` equals a direct call's `structured_content` |
   | S7 | policy denial: grant without `hmc_power_on_lpar`; `hmc_power_lpar start` | `ToolError` naming `hmc_power_on_lpar`; no HMC write; withheld specialist absent from search, and invoke gives the unknown-name denial |
   | S8 | ambiguous target: `hmc_plan_lpar` placement where two VIOS/volume-group pairs qualify | blocker code `ambiguous`; no write |
   | S9 | missing prerequisite: `hmc_provision_lpar` with `install` and `exclusive_writer_window` omitted/false | refused naming the flag; no operation recorded |
   | S10 | resume from a new application and session in the same process: provision `boot=deferred` pauses `ready_to_boot`; a second, freshly composed application and new session calls `continuation="boot"` | outcome `boot_started`; one power-on |
   | S11 | direct call of an unlisted, permitted specialist | dispatches through its own guards (ADR 0189 D4); asserts on `structured_content` |

   Timing is fixed by construction: S2 and S10 pass `wait_seconds=30`, and the provision fake
   answers a `CA00E105` row on the first refcode read after power-on, so no 15-second poll
   sleeps and the call counts do not depend on the CI leg's speed. The provision tool builds
   its body with the default client factory bound at import
   (`server_tools/lpar/provision.py` calls `provision_body(...)` and `provision_classifiers(...)`
   without one), so the scenario patches those two names in that module with wrappers that
   bind `client_factory` to the fake. S10 proves a new session and application, not a process
   restart: engine workers and the store lock are process state, and process-restart recovery
   stays with the existing engine and store tests (`tests/unit/test_logical_engine.py`).

   Plus one payload test: the default listing's serialized size is smaller than the
   registered listing's, measured the same way the report measures it.
6. **Docs.** `docs/mcp-server.md` (tier paragraph at :200 becomes the listing contract;
   :27 and :99 stop equating the ceiling with `tools/list`); the `hmc_search_tools` docstring
   and the module docstring in `src/hmcpctl/server_tools/gateway.py` (search covers what the
   policy exposes, a superset of `tools/list`); `docs/authorization-audit.md:34` (re-run
   `tests/test_authorization_audit_doc.py`); `SUMMARY_NOTE` in `scripts/gen_tool_reference.py`
   and the index's "exposed by a default deployment" line, which becomes "callable" beside the
   new "listed" line. Before editing, `rg -n 'tools/list' src scripts docs` (excluding
   `docs/adr`, `docs/workflow`, `docs/tools`) and resolve every hit;
   `docs/recipes/lpar-iso-install.md` gains a short "From an MCP client" section naming the
   primary path and search/invoke for its specialists; `scripts/gen_tool_reference.py` adds an
   index line and section "Listed by default" naming the primary tools, then `just tool-docs`;
   `README.md` only where it states what the listing contains; `CHANGELOG.md`; ADR 0189 status
   pointer to 0330.
7. **Report.** `docs/workflow/primary-catalog-report.md`: commit, environment, payload bytes
   for the registered and primary listings (legacy-equivalent policy, `json.dumps` of
   `[t.model_dump(mode="json", exclude_none=True) for t in tools]` with `sort_keys=True` and
   `(",", ":")` separators), the per-scenario `tools/call` counts from S2-S11, and a "what ran
   where" line: local and CI only, no emulation or hardware (#1854). The command that produced
   the bytes is printed in the report. No percentage target.

Out: everything the WORK:SCOPE exclusions list, including any operator-facing full-listing
option, schema changes, and live runs.

## Failure model

1. **Actors and deployments:** MCP clients of `hmcpctl serve` (stdio/HTTP) under an operator's
   policy; in-repo harnesses and CI composing in-process.
2. **Invariants and assets at stake:** ADR 0189 D4 (direct calls and invoke unchanged, guards
   unchanged); the listing never shows a tool the policy withholds; instructions name no
   withheld or unregistered tool; generated docs match the registry.
3. **Accepted failure classes:** a listing-driven client cannot reach `hmc_run_command` (ADR 0330
   D5, follow-up candidate); under a policy that withholds `hmc_invoke_tool` (any grant without
   the `destructive` effect) a listing-driven client reaches specialists only by search plus a
   direct call by name, and under one withholding both gateway tools only by known name (ADR
   0330 Consequences; a startup warning for that case is a follow-up candidate); clients get no
   output schema for unlisted tools (ADR 0330 Consequences); scenario fakes are not native evidence (#1854); report numbers are
   point-in-time and drift with docstrings — they are dated and commit-stamped, not gated.
4. **Covered elsewhere:** authorization of each call — ADR 0038/0039 `authorized()`; search and
   invoke filtering — ADR 0189 D3 tests in `tests/app/test_tool_gateway.py`; hardware proof —
   #1854.

The change narrows what is advertised and adds no entry point, input, or authorization path,
so no threat model beyond the failure model is needed.

## Success

- `just smoke` prints `11 tools exposed`; S1, S1b and S2-S11 and the payload test pass on the
  four CI legs (3.11-3.14).
- `just tool-docs-check`, `just doc-freshness`, `just verify` and `prek run --all-files` pass.
- The report exists with measured values and the commit it measured.
