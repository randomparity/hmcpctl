# ISO fixture startup resource cleanup

## Problem
The invocation-owned ISO HTTP fixture publishes its HTTPServer before constructing
and starting the worker. A synchronous thread-construction/start failure leaves an
open listening socket and a partially initialized fixture. Calling shutdown on a
server whose loop never started can block. An offline probe reproduced the open
socket after a RuntimeError from thread construction.

## Scope
Root owns this bounded bug repair under the requested code-quality work. Keep
IsoHttpServer signatures, configuration, allowlist behavior, daemon worker and
normal close semantics. Create a startup-decision event before acquiring the socket. Construct the server
locally and have its worker wait for that decision before entering serve_forever.
Construct/start the thread inside a cleanup boundary. On a synchronous startup
exception, release the gate with serving disallowed, close the socket and re-raise.
Only successful start publishes self._server, allows service and releases the gate. No new dependency, persistent data, public output,
HMC operation, thread registry or common framework. This does not add readiness
monitoring for exceptions raised asynchronously inside serve_forever.

## Failure model
The OS may refuse a socket or a thread; thread construction or startup may raise
an ordinary exception or interruption. Failed startup must release resources and
leave close safe and restart possible. A worker that starts before or after a synchronous startup failure must never
enter the serving loop. Its canceled wrapper may finish after socket closure.
Arbitrary interruption between successful handoff statements is outside this
bounded repair, as are asynchronous worker failures. Keep existing
close idempotence. Do not change the ownership rules for repository lifecycle.

## Validation
Use only loopback and temporary synthetic ISO bytes. Test serving the selected
directory, repeated start identity, close twice, released socket, and restart.
Fault-inject thread construction, start-before-running, and start-after-running and a delayed native bootstrap;
assert propagated failure, no published server, closed socket and safe close.
Observe failure before implementation. Test owned ST17 repository roundtrip and
failed final restoration separately. Run focused virtual-media tests, lint, types,
format and hooks, then the final guardrail suite. No real HMC or live arm runs.
