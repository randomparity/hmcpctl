# Dump-restart as its own operation — design (#896)

Decision record: [ADR 0188](../../adr/0188-dump-restart-is-its-own-operation.md).

## Problem

A served grant that admits `hmc_power_off_lpar` can also crash the partition and take a
platform dump with `operation=dumprestart` (ADR 0164). A policy can bind to only a tool's name
or its effect class, so no grant can admit the stop and withhold the crash.

## Scope

- `src/hmcpctl/server_tools/lpar/lifecycle.py` gains `hmc_dump_restart_lpar`
  (`lpar.dump_restart`, `destructive`, `lpar`). Its parameters are `lpar_name_or_uuid`,
  `allow_dump_restart=False`, `wait`, `timeout_seconds`, `poll_interval`, `profile`,
  `system_name_or_uuid` and `ownership_override`. It calls `power_lpar(power_on=False,
  operation="dumprestart", immediate=False, restart=False, ...)`.
- `hmc_power_off_lpar`'s `operation` is typed by a new module-local
  `PowerOffToolOperation = Literal["shutdown", "osshutdown"]`, and its `allow_dump_restart`
  parameter is removed.
- Regenerated or updated artifacts: `docs/capabilities/operations.json` (new row; the power-off
  signature), `docs/capabilities/maturity.json` (the `dump-restart` variant moves to
  `lpar.dump_restart`, with no evidence), `src/hmcpctl/_operation_maturity.json`
  (`just capability-metadata`), and `docs/tools/` (`just tool-docs`).
- `scripts/live_test/arms/bare_cec.py`: the opt-in dump step dispatches `hmc_dump_restart_lpar`.
  Its row label becomes `hmc_dump_restart_lpar`. No other step changes.
- Prose: `docs/authorization-audit.md`, `docs/mcp-server.md`, `docs/kdive-tier-a-contract.md`,
  a Status banner on ADR 0164, and `CHANGELOG.md`.
- No ownership move. `power_lpar` remains the single owner of PowerOff validation, the
  ownership guard and the ADR 0180 audit record. Both tools delegate to it unchanged.

## Failure model

1. **Actors and deployments:** an MCP client (LLM agent) on `hmcpctl serve` under a named
   access policy; the operator who authors that policy; a local CLI or Python caller holding
   HMC credentials (not policy-governed).
2. **Invariants and assets:** a partition crash with a platform dump is irreversible. A grant
   that names `hmc_power_off_lpar` and not `hmc_dump_restart_lpar` must not reach a
   `dumprestart` job.
3. **Accepted failure classes:**
   - An `effects = ["destructive"]` grant reaches the crash. This is effect-class semantics,
     documented in `docs/mcp-server.md`.
   - The CLI and Python API still crash on request. Credential holders are outside policy
     (ADR 0188 Consequences).
   - Existing policies lose the crash silently until they name the new tool. This is the
     accepted contract change, recorded in the CHANGELOG.
4. **Covered elsewhere:** the crash audit trail (ADR 0180); `dumpretry` (unowned exclusion);
   dispatch-time target scope (ADR 0039, unchanged).

### Threat model

- **Boundaries:** one is added, the new tool's served entry point. One is narrowed,
  `hmc_power_off_lpar`'s `operation` input.
- **Actor:** an MCP client holding a policy grant. Trust sits with the operator-authored policy.
- **Controls:**
  - Capability ceiling: an ungranted tool is not served.
  - Target scope: both selectors are checked by ADR 0039.
  - The served JSON schema refuses `dumprestart` for `hmc_power_off_lpar`. That tool has no way
    to pass `allow_dump_restart`, so `power_lpar` refuses the value even when the schema is
    bypassed.
  - The `allow_dump_restart` confirmation stays on the new tool.
- **Out of scope:** callers holding HMC credentials directly.

## Success

1. Under a policy that grants only `hmc_power_off_lpar`, `hmc_dump_restart_lpar` is not served.
2. `hmc_power_off_lpar`'s served schema admits exactly `shutdown` and `osshutdown` and has no
   `allow_dump_restart`. A direct call with `operation="dumprestart"` submits no job.
3. `hmc_dump_restart_lpar(..., allow_dump_restart=True)` PUTs a PowerOff job carrying
   `operation=dumprestart`. Without the flag it is refused and submits nothing.
4. `TOOL_SECURITY["hmc_dump_restart_lpar"]` is `destructive`, `lpar.dump_restart`, `lpar`,
   `exhaustive_targets=True`.
5. The bare-CEC dump step dispatches the new tool, and `just scenario-gap` passes.
6. `just verify` and `prek run --all-files` pass.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| S1 ceiling | focused-test | `tests/app/test_capability_ceiling.py::test_a_power_off_grant_does_not_serve_dump_restart` |
| S2 schema and refusal | focused-test | `tests/app/test_capability_ceiling.py::test_served_power_off_admits_no_dump_restart`; `tests/app/test_server_tools.py::test_power_off_lpar_tool_cannot_send_dumprestart` |
| S3 job and gate | focused-test | `tests/app/test_server_tools.py::test_dump_restart_lpar_tool_submits_dumprestart`, `::test_dump_restart_lpar_tool_refuses_without_opt_in` |
| S4 classification | focused-test | `tests/app/test_tool_security.py` (`LEGACY_DESTRUCTIVE` membership and the selector-required table) |
| S5 live arm | focused-test | `tests/scripts/test_live_bare_cec.py::test_platform_dump_runs_only_on_opt_in` against the served schemas |
| Artifacts | focused-test | `just capability-inventory`, `just tool-docs-check`, `tests/app/test_kdive_tier_a_contract.py` |
| ADR/CHANGELOG/doc prose | task-test-not-applicable | No executable consumer reads the prose beyond the link and token checks above |
