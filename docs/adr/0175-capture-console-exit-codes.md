# ADR 0175: Exit codes for `hmcpctl lpars capture-console`

## Status

Accepted (2026-09-23). Applies ADR 0072 and ADR 0170's capture outcome (`stop_reason`,
`released`) to the CLI command #959 adds. PR #777's review deferred this contract to #959.
Extended by ADR 0235 (2026-10-16) for SIGINT and partial output.

## Context

A bounded capture ends in one of three ways a script must tell apart: it finished and the
vterm is proven free; it failed, before or during the capture; or the release was not proven,
so the partition's single console slot may still be held and needs `rmvterm` from an operator.
The command's stdout is the raw console stream, so stdout cannot carry the outcome.
`hmcpctl` already uses `1` for runtime errors and `2` for usage errors (`cli_commands/output.py`).

## Decision

| Code | Meaning |
|---:|---|
| `0` | Capture stopped on `duration`, `max_bytes`, `idle` or `remote-close`; `released` is true. |
| `1` | Runtime failure: selector lookup, SSH or HMC failure, `ConsoleHeldError` contention, a capture whose `stop_reason` is `error`, or bytes that could not be written. |
| `2` | Usage error, found before any file or connection opens: a bound outside its limits, an existing `--output` file, or a terminal stdout without `--output`. |
| `3` | Release is false or unproven. Outranks `1` and `130`. |
| `130` | Ctrl-C (SIGINT) interrupted capture and release was proven. |

Bytes a returned capture holds are written before a non-zero exit. When the capture returns,
one stderr line reports stop reason, byte count, `released` and any error; otherwise stderr
carries the `Error:` line. Ctrl-C stops early and waits for release cleanup. Its
flushed partial `--output` file is retained, including an empty file or the bytes
flushed before a write error. A write error is still reported; release proof
selects 130 versus 3. Interrupting before a capture outcome exists returns 3
without asserting release. Ordinary noninterrupt file-failure behavior is unchanged.
SIGTERM and SIGHUP remain outside this table.

## Consequences

- `2` never succeeds on retry; `1` may, and its stderr line names the cause; `3` needs the
  console released before any further capture.
- Contention shares `1` with other runtime failures; a script that must tell it apart reads
  stderr.
- A later `--follow` mode (#957) must map its own ending onto this table or record a new one.

## Considered & rejected

- **Do nothing: always exit 0, outcome on stderr only.** judgment: a script would parse prose to
  learn that the console may still be held, the one outcome that needs action.
- **A distinct code for contention.** judgment: a caller waits and retries on contention as on
  a transient HMC failure; a code of its own adds a branch no caller needs today.
- **Exit 1 when the release is unproven.** judgment: it merges the one state that leaves shared
  HMC state held with failures a retry clears.
