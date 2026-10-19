# Design: unscoped LPAR listing reads `LogicalPartition/quick/All`

**Issue:** #1731 · **ADR:** [0248](../../adr/0248-unscoped-lpar-listing-reads-quick-all.md) ·
**Branch:** `feat/bulk-lpar-listing-1731` · **BASE_BRANCH:** `main` ·
**Guardrails:** focused pytest, `just test`, `just verify`, `uv run --no-sync prek run --all-files`

## Problem

Unscoped `list_lpars` costs one `ManagedSystem/quick/All` read plus one Atom feed per operating
system: 16 requests and about 20 s on the #244 V10R3 HMC, against one
`LogicalPartition/quick/All` request in about 0.5 s through `HMCClient.list_quick_all`
(ADR 0245). The bulk item is a flat summary, not the Atom entry `FleetListing` carries today.

## Decisions (resolved for operator review; rationale in ADR 0248)

| Open decision | Choice |
|---|---|
| Entry shape | Existing envelope; `Resource` is the bulk item verbatim; `title`/`link` null (ADR 0248 d.3) |
| `power off` systems' partitions | In `entries`; `unreadable_systems` empty on the bulk path (d.4) |
| Before `No Connection` evidence | Proceed behind a state gate (`operating`, `power off`) and an any-`HMCError` / unreadable-item fallback to the ADR 0197 read; no new timeout (d.1, d.2) |
| Which caller | Unscoped `list_lpars` only (d.5) |
| `ManagedSystem.UUID` | Last `/ManagedSystem/` segment of `AssociatedManagedSystem`, must be a UUID; null, non-string or malformed falls back (d.2, d.3) |
| CHANGELOG | Breaking entry under **Changed** (d.6) |

## Scope

In: `src/hmcpctl/operations/systems/fleet.py` (new `read_fleet_quick_all`, a shared size
check), `src/hmcpctl/operations/lpar/inventory.py` (unscoped branch), the `hmc_list_lpars`
docstring in `src/hmcpctl/server_tools/systems/core.py`, regenerated `docs/tools/`, tests,
`CHANGELOG.md`, ADR 0248.

Out, with owners: VIOS and `list_lpar_ownership` migration (#1731 follow-up); raw quick/All
MCP/CLI exposure (#1732); other-firmware and `No Connection` captures (#1733, #1290); internal
`hmc.list_*` callers and `list_system_summaries` delegation (operator).

Ownership: `fleet.py` owns fleet-wide partition reads (`read_fleet`), so the bulk read extends it
there and keeps `read_fleet` as its fallback; `list_lpars` swaps one call. No caller moves, no
path becomes obsolete: `read_fleet` still serves VIOS, ownership and the fallback.

## Failure model

Actors and deployments:
- A local operator (CLI) or an MCP client through `hmc_list_lpars`, against a V10R3 HMC
  (probed) or an unprobed firmware (V11R2 is in the lab).

Invariants and assets at stake:
- Read-only GETs; no HMC state changes.
- `unreadable_systems` names exactly the systems whose partitions are absent from `entries`.
- An HMC error never becomes a partial success: the fallback raises its own errors.
- `FleetListing` type and generated output schema unchanged; `limit`, `state` filter and the
  100-system refusal unchanged.

Accepted failure classes:
- `Resource` keys and value types depend on which path answered (ADR 0248 Consequences);
  bounded by the docstring and CHANGELOG naming both.
- A bulk timeout costs one configured `HMC_TIMEOUT` before the fallback; the gate excludes
  the one state known to stall HMC-wide reads.
- A bulk item list that omits a partition the Atom feed has is not detected; the #244 probe
  found the sets identical (77/77).
- A system that changes state between the summary read and the bulk read is listed by
  whatever the bulk read returns.

Covered elsewhere:
- Path safety of the bulk request: `list_quick_all` (ADR 0245, ADR 0143).
- Other firmware and `No Connection` bulk captures: #1733, #1290.

## Success

1. With every summary `operating` or `power off` and a readable bulk answer, unscoped
   `list_lpars` makes exactly two GETs (`ManagedSystem/quick/All`,
   `LogicalPartition/quick/All`), returns one entry per item in the shape of ADR 0248 d.3, and
   `unreadable_systems == []`.
2. A summary in any other state, or without a UUID, makes the call read exactly as ADR 0197
   (no bulk GET); existing `hmc_list_lpars` tests with a `no connection` system pass unchanged.
3. A bulk `HMCError` of any status or transport kind, a non-dict item, an item whose `UUID` is
   not a UUID, and an `AssociatedManagedSystem` that is null, not a string, has no
   `/ManagedSystem/` segment or does not end in a UUID each produce the ADR 0197 result.
4. `ManagedSystem` uses the summary's `UUID` spelling and `SystemName` when the href matches a
   summary case-insensitively; otherwise the href UUID and the item's `SystemName`.
5. `state` filters bulk entries on `Resource.PartitionState`; more than 100 summaries is refused
   before any partition read; `limit` caps `entries` only.
6. Scoped `list_lpars` and `list_vios` send the same requests as before.
7. `hmc_list_lpars`' docstring states both entry shapes; `docs/tools/` regenerated; CHANGELOG
   entry; `just verify` and `prek run --all-files` pass.

## Validation

- Success 1, 4: focused-test. `tests/unit/test_fleet_listing.py`, new cases over
  `rest-lpar-quick-all.json` with an `AsyncMock` HMC; red `ImportError` on
  `read_fleet_quick_all`; green `uv run --no-sync pytest tests/unit/test_fleet_listing.py -q`.
- Success 2, 3: focused-test. Same file, one parametrized case per fallback trigger asserting
  `read_fleet`'s per-system reads ran and the bulk result was discarded.
- Success 5: focused-test. Same file (size refusal, no `list_quick_all` call) and
  `tests/system/test_system_tools.py` (respx, `hmc_list_lpars(state=...)` on the bulk path).
- Success 1 end to end, 6: focused-test. `tests/system/test_system_tools.py` respx case: an
  all-`operating` fleet routes `LogicalPartition/quick/All` and asserts no per-system feed is
  called; existing scoped and VIOS cases stay green.
  Green `uv run --no-sync pytest tests/system/test_system_tools.py -q`.
- Success 7 docstring/docs: focused-test via `just tool-docs-check`. CHANGELOG:
  task-test-not-applicable — prose whose only consumers are structural gates `just verify`
  already runs.
