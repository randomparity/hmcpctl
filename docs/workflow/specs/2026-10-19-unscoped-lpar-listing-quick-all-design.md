# Design: unscoped LPAR listing reads `LogicalPartition/quick/All`

**Issue:** #1731 · **ADR:** [0248](../../adr/0248-unscoped-lpar-listing-reads-quick-all.md) ·
**Branch:** `feat/bulk-lpar-listing-1731` · **BASE_BRANCH:** `main` ·
**Guardrails:** focused pytest, `just test`, `just verify`, `uv run --no-sync prek run --all-files`

## Problem

Unscoped `list_lpars` costs one `ManagedSystem/quick/All` read plus one Atom feed per operating
system: 16 requests and about 20 s on the #244 V10R3 HMC, against one
`LogicalPartition/quick/All` request in about 0.5 s through `HMCClient.list_quick_all`
(ADR 0245). The bulk item is a flat summary, not the Atom entry `FleetListing` carries today.

## Decisions (for operator review; rationale in ADR 0248)

| Open decision | Choice |
|---|---|
| Entry shape | Existing envelope; `Resource` is the bulk item verbatim; `title`/`link` null marks it (d.3, d.6) |
| `power off` systems' partitions | In `entries`; `unreadable_systems` empty on the bulk path (d.4) |
| Before `No Connection` evidence | Proceed behind a state gate (`operating`, `power off`) and a logged fallback to the ADR 0197 read on any `HMCError` or unplaceable item; no new timeout (d.1, d.2) |
| Which caller | Unscoped `list_lpars` only (d.5) |
| `ManagedSystem.UUID` | Last `/ManagedSystem/` segment of `AssociatedManagedSystem`, must be a UUID; null, non-string or malformed is unplaceable (d.3) |
| CHANGELOG | Breaking entry under **Changed** (d.7) |

## Scope

In: `operations/systems/fleet.py` (new `read_fleet_quick_all`, a shared size check),
`operations/lpar/inventory.py` (unscoped branch), the `hmc_list_lpars` docstring, the
`lpar list` help text, the unscoped-entry paragraph of `docs/cli.md` (operator-approved
2026-10-19), regenerated `docs/tools/`, tests, `CHANGELOG.md`, ADR 0248.

Out, with owners: VIOS and `list_lpar_ownership` migration (#1731 follow-up); raw quick/All
MCP/CLI exposure (#1732); other-firmware and `No Connection` captures (#1733, #1290); internal
`hmc.list_*` callers and `list_system_summaries` delegation (operator).

Ownership: `fleet.py` owns fleet-wide partition reads, so the bulk read extends it there with
`read_fleet` as its fallback; `list_lpars` swaps one call. No caller moves and no path becomes
obsolete: `read_fleet` still serves VIOS, ownership and the fallback.

## Failure model

Actors and deployments:
- A local operator (CLI) or an MCP client through `hmc_list_lpars`, against a V10R3 HMC
  (probed) or an unprobed firmware (V11R2 is in the lab).

Invariants and assets at stake:
- Read-only GETs; no HMC state changes.
- On the ADR 0197 path, `unreadable_systems` names exactly the systems whose partitions are
  absent; on the bulk path it is empty.
- An HMC error never becomes a partial success: the fallback raises its own errors.
- `FleetListing` and the generated output schema unchanged; `limit`, `state` filter and the
  100-system refusal unchanged.

Accepted failure classes (ADR 0248 Consequences): `Resource` keys and types depend on the path,
marked per entry by a null `title`; one `HMC_TIMEOUT` before the fallback; a placeable bulk
answer that differs from the Atom read (omission, `PartitionState` spelling, lag after a
create or delete) on unprobed firmware; a system changing state between the two reads.

Covered elsewhere: path safety of the bulk request (`list_quick_all`, ADR 0245, ADR 0143);
other firmware and `No Connection` bulk captures (#1733, #1290).

## Success

1. With every summary `operating` or `power off` and a placeable bulk answer, unscoped
   `list_lpars` reads summaries once and the bulk listing once, returns one entry per item in
   the shape of ADR 0248 d.3, and `unreadable_systems == []`.
2. A summary in any other state, or without a UUID, makes the call read exactly as ADR 0197
   with no bulk request; existing tests with a `no connection` system pass unchanged.
3. Each of: a bulk `HMCError` (HTTP 404, HTTP 500, `HMCTransportError`), a non-object item, an
   item `UUID` that is not a UUID, and an `AssociatedManagedSystem` that is null, not a
   string, lacks `/ManagedSystem/`, or does not end in a UUID, logs one warning and produces
   the ADR 0197 result.
4. `ManagedSystem` uses the summary's `UUID` spelling and `SystemName` when the href matches a
   summary case-insensitively; otherwise the href UUID and the item's `SystemName`.
5. `state` filters bulk entries on `Resource.PartitionState`; more than 100 summaries is refused
   before any bulk request.
6. Scoped `list_lpars` and `list_vios` send the same requests as before.
7. The `hmc_list_lpars` docstring, `lpar list --help` and `docs/cli.md` name both entry shapes and the null
   `title` marker; `docs/tools/` regenerated; CHANGELOG entry; `just verify` and prek pass.

## Validation

- Success 1–5: focused-test, `tests/unit/test_fleet_listing.py` over `rest-lpar-quick-all.json`
  with an `AsyncMock` HMC (red: `ImportError` on `read_fleet_quick_all`; green
  `uv run --no-sync pytest tests/unit/test_fleet_listing.py -q`); the warning via `caplog`.
- Success 1, 5, 6 end to end: focused-test, respx cases in `tests/system/test_system_tools.py`
  counting the summary route; `tests/lpar/test_lpar_state_filter.py` gains a 404 bulk route so
  it keeps covering the per-system feed; `tests/app/test_cli_e2e.py` `lpars list` cases fall
  back on its stub's 404. Green: `uv run --no-sync pytest tests/system/test_system_tools.py
  tests/lpar/test_lpar_state_filter.py tests/app/test_cli_e2e.py -q`.
- Success 7: focused-test via `just tool-docs-check`; help text and CHANGELOG:
  task-test-not-applicable — prose with no consumer beyond gates `just verify` already runs.
