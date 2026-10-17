# CLI TLS diagnostics

## Scope and architecture

#1700, under #1695, owns concise TLS diagnostics with executable recovery.
[ADR 0233](../../adr/0233-cli-tls-diagnostic-presentation.md) selects scoped
presentation in `cli_commands.runtime.run_cli_coroutine` and `audit.sink`.
`client.core` retains warning emission, categories and per-host/source throttle;
only its invalid flag literal changes. Direct runtime callers migrate together
through the existing shared boundary. No alternate CLI transport is added.

## Success

The CLI renders distinct concise security and field-default migration warnings,
without Python locations/source lines. Explicit false omits migration; true
omits TLS warnings and TLS-disabled audit. Repeated clients preserve the
existing warning throttle and per-construction audit events. A following error
retains its normal exit code and stderr diagnostic. Suggested CLI flags parse.
TTY fallback audit is a compact lossless view of each TLS event; redirected
stderr retains original JSON. Existing handlers, logger configuration, non-TLS
warnings and non-TLS audit messages retain their behavior after the invocation.
Stdout data and server/library behavior remain unchanged.

## Global constraints

Python 3.11–3.14; amd64 CI; no new dependencies. Preserve ADR 0224 defaults,
pinning scope, library warning categories and audit distinction. No blanket
quiet switch or generic error/help redesign. No live HMC execution.

## Failure model

- Actors and deployments: local CLI operator with TTY or redirected stderr;
  sequential embedded CLI invocations; library callers with configured logging.
- Invariants and assets: warning visibility, per-host/source throttling, full
  audit event emission, collector JSON, stdout results, subsequent real errors.
- Accepted failure classes: concurrent embedded CLI invocation is outside the
  synchronous command model; broken or closed stderr retains the standard logging error behavior;
  presentation adds no queue or event-drop policy.
- Covered elsewhere: TLS default and pinning ADR 0224/#1614; generic errors
  #1702; logging destination retention remains deployment-owned ADR 0040.

## Threat model

- Boundaries added/widened: none; presentation changes existing warning and
  audit-to-stderr boundaries. Host/source values originate from configuration.
- Actors: local operator controls config; remote HMC text remains untrusted.
- Controls: retain JSON encoding for audit fields, no raw terminal controls;
  only the exact known TLS event shape is reformatted; original message is the
  fallback. No logging level/filter/propagation changes; configured sinks win.
- Out of scope: TLS trust-policy changes and new destinations, owned above.

## Validation

A focused CLI test module covers false/default/true, repeated hosts and source
changes, real failures, executable suggestions, formatter restoration and
unrelated warnings. Sink tests cover actual TTY versus redirected output,
configured handlers, unusual event shapes, controls and per-construction counts.
A real Python CLI subprocess uses a controlled transport boundary; no hardware.
Run affected client/audit/runtime tests and lint/type/format checks, then final
`just verify` and `uv run --no-sync prek run --all-files` before publication.
