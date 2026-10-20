# Design: bulk `quick/All` client method

**Issue:** #244 · **ADR:** [0245](../../adr/0245-bulk-quick-all-client-method.md) ·
**Branch:** `feat/bulk-quick-all-244` · **BASE_BRANCH:** `main` · **Guardrail:** `just verify`

## Problem

Listing LPARs or VIOSes costs one Atom request per managed system. On a V10R3 HMC with 16
systems, 77 LPARs and 15 VIOSes a read-only probe measured 16 requests (~20 s) for LPARs
and 18 requests (~23 s) for VIOSes, against one request (~0.4–0.5 s) for
`GET /rest/api/uom/{Type}/quick/All`. The client has no method for that endpoint except the
`ManagedSystem` instance behind `list_system_summaries`.

## Scope

In: one `HMCClient` method, `list_quick_all`, with the signature and behaviour fixed by
ADR 0245; unit tests; sanitized fixtures; one `CHANGELOG.md` entry.

Out (approved exclusions, with owners):

- Migrating `hmc_list_lpars`, `hmc_list_vios` or any caller, and any change to `FleetListing`,
  ADR 0012 or ADR 0197 — #1731.
- MCP tool or CLI exposure — #1732.
- V11R2 and other firmware, empty-result and `No Connection` captures, adapter types —
  #1733.
- Changes to `get_quick_property`, `list_quick_properties`, `list_system_summaries` — no owner.

Ownership: `core.py` owns `list_quick_properties`, the sibling that takes the same
type/parent arguments, so the method extends it there; no caller moves and no path is
obsolete. No protocol entry is added: `client_contracts.py` lists only methods that mixins
call on `self`, and nothing calls this one.

### Failure model

Actors and deployments:
- A local operator or an MCP server process calling the Python client against a V10R3 HMC.
- No caller exists yet; the method is reachable only through `hmcpctl.client`.

Invariants and assets at stake:
- Read-only GET; no HMC state changes.
- The request path stays inside `/rest/api/uom/` for any `resource_type`, `parent_type` or
  `parent_uuid` (ADR 0143, ADR 0146).
- A non-list or unparsable body never reaches a caller as data.

Accepted failure classes:
- Items are unvalidated, so a caller can see a null or missing field; the probe showed null
  values in several fields. Each caller decides what an unreadable item means, as with
  `list_system_summaries`.
- Firmware that rejects a type answers non-200 and the HMC's status reaches the caller
  unchanged; only V10R3 was probed.
- A very large fleet yields a large body held in memory, as the Atom feed already does.

Covered elsewhere:
- Path-segment injection is held by `_reject_unknown_uom_type` and
  `_request_with_uuid_path_arguments` (ADR 0143).
- Other firmware, `No Connection` systems and empty-result captures — #1733.

## Success

1. `list_quick_all("LogicalPartition")` sends one GET to `/rest/api/uom/LogicalPartition/quick/All`
   with `Accept: */*` and returns the parsed array.
2. `list_quick_all("LogicalPartition", parent_type="ManagedSystem", parent_uuid=U)` sends one
   GET to `/rest/api/uom/ManagedSystem/U/LogicalPartition/quick/All`.
3. An empty array returns `[]`.
4. A non-200 status, invalid JSON, a too-deeply nested document, and a non-array document each
   raise `HMCError` naming the path. A non-200 carries the status code and body.
5. Exactly one of `parent_type`/`parent_uuid` raises `ValueError` before any request. A
   malformed type or parent type, or a non-UUID `parent_uuid`, is refused before any request.
6. Tests use sanitized fixtures from the probe, with every name, UUID, serial and host replaced
   by tokens; `just verify` and `uv run --no-sync prek run --all-files` pass.

## Validation

- Contract: path and `Accept` for the type-scoped form (Success 1). Mode: focused-test.
  `tests/unit/test_client.py`, a respx route with a header assertion; red: `AttributeError`;
  green: `uv run --no-sync pytest tests/unit/test_client.py -k list_quick_all -q`.
- Contract: child-form path (Success 2). Mode: focused-test. Same file; same command.
- Contract: empty array, non-200 for an unsupported type, malformed JSON, non-array JSON,
  deep nesting (Success 3, 4). Mode: focused-test. Same file; same command.
- Contract: parent-argument pairing and path-safety refusals (Success 5). Mode: focused-test.
  `tests/unit/test_request_path_safety.py`: add `list_quick_all` rows to
  `_TYPE_SEGMENT_CALLS` and a direct non-UUID `parent_uuid` test; a pairing test in
  `test_client.py`. Green: `uv run --no-sync pytest tests/unit/test_request_path_safety.py -q`.
- Contract: fixtures are sanitized and documented (Success 6). Mode: focused-test. The
  fixture-reading tests above fail if a fixture is absent or not parseable; a scripted grep of
  each fixture for `ibm.com`, `10.` addresses and 12-hex serials must print nothing.
  `tests/fixtures/live/README.md` gains the capture entry.
- Contract: CHANGELOG entry. Mode: task-test-not-applicable. It is prose in a file whose only
  consumers check structure the existing gates already run (`just doc-freshness`); no
  task-specific observation could fail on the entry's wording.
