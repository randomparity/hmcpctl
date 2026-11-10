# Bounded LPAR reference-code read

**Issue:** #874 (epic #871) · **Branch:** `feat/lpar-refcode-read-874` from `main`

## Problem

The only boot-progress signal the package offers is state polling or holding the virtual terminal.
`lsrefcode -r lpar` needs no vterm; epic #871 reports that it returns a partition's current SRC at
the same poll that sees `Running`, which is not confirmed here and is #879's to observe. An empty
read is not distinguishable from a cleared SRC. `rg -n lsrefcode src/` returns nothing: no wrapper
exists.

## Scope

One read-only SSH function in a new `src/hmc_mcp/ssh/refcodes.py`, one MCP tool in
`server_tools/lpar/lifecycle_boot.py`, one `lpars refcodes` subcommand in
`cli_commands/lpar/inventory.py`, one `lpar.list_refcodes` record in
`docs/capabilities/operations.json`. A new focused `ssh/` module matches the existing
one-topic-per-module layout (`affinity`, `console`, `memory`, `sriov`); neither `io_inventory`
("physical I/O, Fibre Channel, SEA") nor `lpar` ("creation, ownership, validation, name
resolution") owns reference codes. No ownership transition and no caller migration: nothing calls
this code today. No new quoting, parsing or empty-result primitive is written: `build_filter`
(`documents/native_records.py`) emits `lpar_names=<n>` and refuses a value carrying `,`,
`=`, `"` or a control character; `parse_hmc_delimited_rows` (`ssh/commands.py`) parses
`-F --header` output, refusing a
mismatched header; and the empty-result guard is the shape `ssh/sriov.py:28-34` already uses.

**The parsed field set** is `-F lpar_name,time_stamp,refcode --header`. Those three names are
IBM's own documented attribute list for `lsrefcode -r lpar`, identical on both corpus pages at
`docs/refs/hmc-commands-p11/commands/lsrefcode.md:54-55`; that example carries no `--header`,
`-n` or `--filter`, and `--header` is documented at `:35` as `-F`'s companion, so the composed
invocation is assembled rather than transcribed. Bare `-F` with no attribute names "displays
values for all of the reference code attributes" (`:34`), an unenumerated, firmware-dependent
set, so the stable-fields criterion rules it out. `parse_hmc_delimited_rows` fails closed on a
header mismatch; #879 confirms the set against hardware.

**The bound** is `count: int = 1`: `ValueError` outside `1..MAX_REFCODE_COUNT` (100) and
`TypeError` for a non-`int` — `bool` included, since `True` would otherwise become `-n 1` — both
before any interpolation. Both selectors are refused there too when empty, blank, or
control-bearing. The default matches the HMC's own — `-n` omitted lists only the current code —
while the command always passes `-n` so its shape is fixed.

### Failure model

- **Actors and deployments** — a local operator at the `hmc-mcp` CLI; an MCP client the operator
  has granted this tool. No anonymous or multi-tenant deployment.
- **Invariants at stake** — the read stays read-only and never holds a virtual terminal; a
  caller-supplied selector must not alter the command's structure; returned rows must not silently
  shift columns; a new public tool and CLI contract.
- **Accepted failure classes** — an HMC naming these three attributes differently raises
  `HMCCLIError` rather than returning wrong rows (fails closed; no live evidence yet). An empty
  read arrives as the exit-0 literal `No results were found.` (`docs/HMC_HINTS.md:170-181`) or as
  blank stdout, and both return `[]`. An unknown partition is **not** that empty path. Observed live (#893,
  2026-10-12, an HMC at V10R3 SP1060 managing a POWER8 8247-22L; fixture `tests/fixtures/live/cli-refcodes-unknown-lpar.json`,
  driven by `tests/unit/test_ssh_refcodes.py`): the reader's own command against a partition name
  the HMC does not know exits 1 and prints `HSCL8012 The partition named <name> was not found.` on
  stdout with stderr empty, so the reader raises `HMCCLIError` rather than returning `[]`. The same
  command with an unknown `lpar_ids` value or an unknown `-m` system was also refused with
  exit 1 (`HSCL8011`, `HSCL8018`), seen only in the private capture, not fixture-backed; the
  reader selects by name only. A known partition answers exit 0 (`cli-refcodes.json`).
  Observed on that one HMC release and system family only. `-n` above 100 is refused rather than
  streamed.
- **Covered elsewhere** — `lsrefcode -r sys`, `-s p|s`, FRU and LED inventory: #691.
  Multi-partition `lpar_names=p1,p2`: `build_filter` refuses an embedded comma; follow-up if #876
  needs it. Live observation and promotion to `current` maturity: #879. SSH transport, timeout and
  credential handling: `ssh/transport.py`, unchanged here.

### Threat model

- **Boundary added** — one: caller-supplied `system_name`, `lpar_name` and `count` reaching a
  remote shell command string. None is widened; `run_hmc_command` is unchanged.
- **Actor model** — the untrusted input is the selector text, not the caller, who is already
  authorized to run HMC commands. Trust sits in `ssh/transport.py` and the HMC's own
  authorization, as in every sibling SSH read.
- **Control per boundary** — both selectors: `validate_hmc_name` (`ssh/install.py:109`) plus a
  blank check, so neither is empty, blank, or control-bearing; then `shlex.quote`, which makes each
  one shell word but does not stop a leading-dash value reaching `lsrefcode` in option position —
  bounded here because `lsrefcode` is read-only, and refusing it belongs in the shared name guard
  rather than in this one caller. `lpar_name` additionally passes `build_filter`'s record-grammar
  check — two parsers, two guards, neither substituting for the other. `count`: type and range
  check before interpolation, so no non-numeric text reaches the string. The HMC's own narrowing is
  verified rather than trusted: a row naming another partition raises. Refusals name the argument
  and the offending value and carry no credential.
- **Out of scope** — HMC-side authorization, which is what bounds what the configured SSH
  account may do; SSH host-key policy. Not "anything the `arbitrary-command` tool permits": that
  tool is withheld by default (`tests/app/test_capabilities.py:175`), so it is not a bound either
  named actor has.

## Success

1. `hmc_read_lpar_refcodes(system_name_or_uuid, lpar_name_or_uuid, count=1)` issues `lsrefcode -r
   lpar -m <sys> --filter lpar_names=<n> -n <N> -F lpar_name,time_stamp,refcode --header` and
   returns one dict per row.
2. `hmc-mcp lpars refcodes <system> <lpar> [--count N] [--json]` mirrors it.
3. `lpar.list_refcodes` is recorded in `docs/capabilities/operations.json`; `just tool-docs-check`,
   `just doc-freshness` and `just capability-inventory` pass. It stays `unrecorded` in maturity.
4. `just verify` and `uv run --no-sync prek run --all-files` are green.

## Validation

Every entry is `Mode: focused-test`. `tests/unit/test_ssh_refcodes.py` holds the SSH-layer and
tool-body contracts: `count` outside `1..100` raises `ValueError` and a non-`int` raises
`TypeError`; an empty, blank, or control-bearing selector raises `ValueError`; all before any SSH
traffic. The command string is exactly Success 1's with every interpolated value quoted; shell
metacharacters in a selector stay inside one quoted word; a comma-, `=`- or quote-bearing selector
raises `HMCCLIError`; blank stdout, header-only stdout and the `No results were found.` sentinel
each return `[]`; a header mismatch and a short row each raise `HMCCLIError` carrying the cause; a
row naming another partition raises; a transport failure propagates unchanged; and the tool body
forwards the resolved names and `count` in order. `tests/app/test_cli_commands.py` drives the CLI
subcommand end to end, asserting the composed command and both output paths. Three more pin
registration: `tests/unit/test_server_module_boundaries.py` on the handler's module, `just
capability-inventory` on the ledger record, `tests/app/test_application_boundaries.py` on the
default deployment exposing one more tool.
