# Console capture interruption

Scope: #1692, q1692-bfa7309f; charter is the issue's WORK:SCOPE annotation.
Decision: [ADR 0235](../../adr/0235-console-interrupt-contract.md).

## Problem and success

C1: help, CLI guide, ISO recipe and ADR 0175 explain Ctrl-C, retained flushed partial
output and exit codes. C2: a real SIGINT after capture starts exits 130 only when
release is proven; otherwise 3. C3: retain raw flushed partial output, owner-only
creation and no overwrite. C4: prove this through an offline child process that
runs the CLI, runtime, selector/capture and existing release path, mocking only
REST resolution and SSH transport. Ordinary library/MCP cancellation still raises.

## Design

Keep ownership in place. Extend the internal bounded capture path with keyword-only
`stop_on_cancel: bool = False`, forwarded by the selector operation; only the CLI
passes True. Collection may return `stop_reason="interrupted"` with buffered bytes
on cancellation. The surrounding session context still closes and independently
proves release. Cancellation during acquisition or cleanup also produces an
interrupted result only on this opt-in path, after existing cleanup completes;
initialize empty data before opening so the outcome remains defined. Keep the
interrupted reason through lost-hold and late-contention postprocessing, retaining
existing error detail and release proof. Ordinary/default contention still raises.

CLI exit selection checks unproven release first, then interrupted=130. The normal
file close flushes and retains partial bytes. If cancellation occurs before the
capture can return (selector resolution or repeated synchronous interruption),
catch KeyboardInterrupt at the CLI boundary, close/retain the partial file and
exit 3 with honest release-unknown guidance. Other exceptional failures retain
existing discard behavior. Interrupted write/close failures retain whatever was
flushed and report the write error; release proof still decides 130 versus 3.
No new signal handler, session release implementation, dependency or stable facade.

## Failure model

Named deployment: CPython CLI on supported Linux amd64 Python 3.11–3.14.
Covered: SIGINT while collecting, acquiring, and releasing; release probe failure;
raw bytes and empty partial output; file close/write failure; ordinary library
cancellation; foreign-holder protection inherited and regression-tested.
SIGTERM/SIGHUP, process kill, power loss and live hardware verification are excluded
by the approved scope. Abrupt repeated interrupts may prevent proof and return 3;
no guarantee of release is inferred from cancellation alone.

## Validation

Focused unit tests establish opt-in versus default cancellation and precedence.
A bounded subprocess test waits for a ready handshake after flushed bytes, sends
SIGINT, drains both streams concurrently, reaps on every path, and asserts exit,
exact raw partial file/stdout and observed release/probe calls for proven and
unproven outcomes. It uses no HMC or network credentials. Existing capture tests
retain bounds, contention and foreign-session protection. Full final `just verify`
and pinned `uv run --no-sync prek run --all-files`, then CI across declared versions.
