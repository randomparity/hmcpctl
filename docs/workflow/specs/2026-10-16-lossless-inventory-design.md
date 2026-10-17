# Lossless CLI inventory output

## Problem and authority

Issue #1698, scope q1698-74f1a18e, requires terminal tables and lossless redirected
inventory, preserving JSON contracts. ADR [0228](../../adr/0228-lossless-cli-inventory-output.md)
records the format boundary. No ownership transition is needed: output.py retains
format policy, commands retain projections, read_fleet retains parent discovery.

## Scope and contract

Address systems list/show/summary/health/capacity/find-placement; lpars list/show/
summary/refcodes; vios list; storage list-vgs/get-media-repo/list-optical-media/
list-mappings. Mutations (#1699), decommission (#1701), terminal policy (#1275),
and final journey (#1704) remain excluded under the operator-approved charter.
Scalar lpars state and console/raw REST streams keep their existing contracts.

Each addressed command exposes --format table|tsv|json and --json/-j. Validate
selection before calling an operation: JSON alias plus non-JSON format is exit 2;
JSON alias plus JSON format agrees. Invalid formats are parser errors. Explicit
selection wins over stdout.isatty(); otherwise TTY means table, non-TTY means TSV.
Remove --no-json. Repeated --format follows the native parser's last-value behavior.

TSV is UTF-8 text, one header followed by zero or more rows, newline terminated,
without titles, ANSI rendering, ellipsis, or width-dependent folding. Column order
is fixed per command, matching existing table columns; fleet lists prepend System
and System UUID. Health has Category, Name, UUID, State, RMC, System, System UUID.
Object shows and summaries have Field, Value, one row per top-level mapping key in
insertion order; nested values are compact JSON, preserving their full structure.
Missing or None cells are the literal backslash-N token; empty strings are empty
cells; zero and false remain values. Escape backslash first, then tab, LF and CR
as doubled backslash, backslash-t, backslash-n, backslash-r. Decode the null token
before scanning escapes once left-to-right. Literal backslash-N is thus distinct.
TSV is a documented projection; JSON retains the full existing result.

Tables remain Rich VerbatimTable output with '-' for absent cells. Empty collections
produce a TSV header or empty table plus an informative stderr notice, exit 0.
JSON produces its existing empty list/object/envelope without prose in stdout.
Warnings remain on stderr (including partial-fleet and summary/health warnings).
A partial empty fleet retains its unreadable_systems JSON and emits its warning;
TSV has a header and stderr diagnostics, so consumers must retain stderr.

read_fleet adds a ManagedSystem object (UUID, SystemName) to copied entries using
its already-fetched quick/All parent summary. Existing Resource and envelope values
are preserved; no per-row call. This additive metadata reaches fleet-list JSON and
its direct owned-inventory consumer. Explicitly scoped listings keep their existing
entries; parent display cells are absent when this metadata is absent. JSON envelope
keys entries/unreadable_systems remain unchanged. Refcode JSON keeps raw rows; TSV
uses raw values with delimiter escaping; existing terminal sanitation remains on
the table path only.

## Success and validation

The named commands accept formats/alias, reject conflicts before backend calls,
and preserve parsed JSON values. Real subprocess stdout redirected to a file at
COLUMNS 40/80/200 round-trips long identifiers, Unicode, brackets, backslashes,
tabs/newlines and empty/absent cells. Test zero rows, partial fleets, missing
objects, TTY/default/override routing, scalar state and raw refcode JSON.
Fleet tests prove parent association, no input mutation and no extra calls.
Update impacted tests to select table/JSON where that is the contract they test.
Document exact column headers, migration and recipes in docs/cli.md.
Run focused pytest, lint/typecheck, then just verify and pinned prek before push;
CI's four Python legs are separate evidence. No live HMC proof is claimed.

## Failure model

- Actors/deployments: CLI users and pipe consumers on supported Python versions,
  with existing typed operation results and HMC-parsed resource dictionaries.
- Invariants/assets: exact projected identifiers and cell boundaries, original JSON
  documents/envelopes, parent association, no new HMC requests, stderr diagnostics.
- Accepted classes: explicit table format may fold at narrow widths because it is
  human presentation; TSV projects documented columns rather than every raw field;
  consumers ignoring stderr cannot detect partial fleet reads (keep existing exit 0).
- Other owners: terminal-control policy #1275, mutation output #1699, decommission
  presentation #1701, composed workflow proof #1704; no hardware changes here.
