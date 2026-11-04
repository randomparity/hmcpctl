# REST keep-alive expiry below the HMC idle close (#1926)

## Problem

`HMCClient` pools HTTP/1.1 connections with httpx's default `keepalive_expiry` of 5.0 s.
Read-only probes on 2026-11-04 measured four HMCs closing an idle connection 4.85–5.01 s after
their last response. The server's idle clock starts first, so a request after about 4.8 s idle
can go out on a connection the HMC is closing. On the test HMC, 2 of 18 requests after
4.0–4.99 s gaps failed: `Server disconnected without sending a response`. `_request` sends once, so the caller gets `HMCTransportError`.

## Scope

`_new_http_client` passes `httpx.Limits` with httpx's default caps (100 connections, 20
idle) and `keepalive_expiry=1.0` from a named constant, for the initial and legacy-port
clients. A connection idle for 1 s is discarded, 4 s inside the measured close; requests under
1 s apart still reuse it; a longer gap costs one TLS handshake. `CHANGELOG.md` gets a Fixed
entry. Nothing is retried, so request counts and the `HMCClient._request` live-test wrappers
stay unchanged.

Not taken:

- Disable pooling. Every back-to-back read would pay a handshake for no added margin.
- Retry an idempotent request once on `RemoteProtocolError`. It adds requests the budget
  wrappers cannot see and leaves logoff `DELETE` uncovered; retry redesign is excluded.

### Failure model

1. **Actors and deployments:** operators and the MCP server calling an HMC over REST; HMCs
   whose idle close is above 1 s (all four measured close at about 5 s).
2. **Invariants and assets:** no mutation is sent twice; logoff reaches the HMC; request-budget
   counts stay exact.
3. **Accepted failure classes:** an HMC that closes idle connections in under about 1 s still
   races. No measured HMC does, and retry is excluded. Other server-side closes (restart,
   network drop) still surface as `HMCTransportError`, as they do today.
4. **Covered elsewhere:** retry/backoff policy is out of scope by operator decision. #1857 owns
   its harness cases.

## Success

1. A request sent after an idle gap longer than the client expiry and shorter than the HMC's
   idle close opens a fresh connection and succeeds.
2. Requests sent under 1 s apart reuse one connection.
3. The client sends each `_request` call exactly once.

## Validation

- Stale-connection reuse (Success 1): focused-test. `tests/unit/test_client_keepalive.py` runs
  a local TLS fake HMC that closes a connection without responding when a request arrives
  after 1.1 s idle, with no earlier FIN. The client waits 1.3 s between two `_request` calls.
  Red: without `limits=`, the second call raises `HMCTransportError` (`Server disconnected`).
  Green: `uv run --no-sync pytest tests/unit/test_client_keepalive.py -q`.
- Back-to-back reuse (Success 2): focused-test, same module. Two requests 0.1 s apart arrive on
  one server-side connection. Red: `max_keepalive_connections=0` fails it.
- Single send (Success 3): focused-test, same module. The fake HMC counts one request per call.
- Pool caps unchanged: focused-test, same module asserts 100/20 on the built pool.
- Live: an operator-authorized read-only private probe, 24 requests after 1.5–6 s gaps. Pass:
  new code opens a new connection for each and none fails; old code reuses one under 5 s.
