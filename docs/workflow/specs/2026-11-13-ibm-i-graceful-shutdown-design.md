# IBM i graceful shutdown eligibility — design (#1814)

Decision: [ADR 0372](../../adr/0372-ibm-i-graceful-shutdown-without-rmc.md), amending ADR 0199
Decision 5.

## Problem

`_precheck` in `src/hmcpctl/operations/lpar/power.py` refuses a graceful stop or restart unless
RMC reads `active`. pypowervm and the HMC references exempt IBM i (`OS400`).

## Design

- `_precheck` requires RMC `active` for graceful only when `Resource.PartitionType != "OS400"`
  (exact string). The refusal text names the IBM i exception; nothing else in the body changes,
  so the job stays PowerOff `osshutdown`, `immediate=false`, `restart` per action.
- Tool description (`src/hmcpctl/server_tools/lpar/power.py`), `docs/mcp-server.md`,
  `docs/capabilities/baseline.md`, `docs/live-testing.md` and regenerated `docs/tools/` say
  "RMC must be active except on IBM i (OS400)".
- `scripts/live_test/arms/lpar_power.py` `GAPS` gains an IBM i graceful stop/restart row whose
  note starts `gap: needs` and names an owned scratch IBM i partition with an installed OS and a
  recovery/boot plan. No live run.

## Success

1. For type ∈ {`OS400`} × RMC ∈ {`active`, `inactive`, absent} × action ∈ {stop, restart},
   graceful submits exactly one `osshutdown` job with `immediate=false`.
2. For type ∈ {`AIX/Linux`, `Virtual IO Server`, absent, `os400`, `UNKNOWN`} × RMC ∈
   {`inactive`, absent} × {stop, restart}, graceful fails before any job with no effect; with RMC
   `active` it submits one `osshutdown` job.
3. No path in `_precheck` or `_submit` turns `graceful` into `shutdown`/`immediate=true`
   (asserted by criterion 1's job parameters).
4. The lpar-power arm emits a SKIP row for the IBM i gap and makes no call for it.

## Failure model

1. Actors and deployments: an MCP client agent calling `hmc_power_lpar` against an HMC the
   operator configured; the live arm run by the operator.
2. Invariants and assets: no graceful job without RMC on non-IBM i partitions; never an
   implicit immediate power-off; ADR 0011 ownership and ADR 0190 guard ordering unchanged.
3. Accepted failure classes: an IBM i OS that ignores `osshutdown` — bounded by ADR 0199's
   300 s job / 120 s settle bounds and `needs_attention`; IBM i path unevidenced live — ADR 0212.
4. Covered elsewhere: `hmc_power_off_lpar` semantics — its own contract (excluded); IBM i
   install/media — operator (excluded).

## Validation

Focused: `uv run --no-sync pytest tests/unit/test_lpar_power.py tests/test_live_lpar_power_arm.py -q`;
then `just tool-docs`, `just verify`, `uv run --no-sync prek run --all-files`.
