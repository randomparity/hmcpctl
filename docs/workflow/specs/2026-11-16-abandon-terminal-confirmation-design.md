# Confirm abandoned LPAR operations before teardown

## Problem and scope

Issue [#1918](https://github.com/randomparity/hmcpctl/issues/1918) reports that
ST41/ST72 share a teardown which records abandon responses without proving that
execution has stopped. Extend the existing `_abandon` owner and its `_teardown`
caller; no owner transition, caller migration, or compatibility path is needed.

## Design

After each abandon, read `hmc_operation_status` with the same request ID.
Use the existing `_items` typed-result adapter and match `request_id`; only
`state == "terminal"` with a successful read proves completion. Record the read,
update the request's terminal flag, and aggregate the results without skipping
later abandon attempts. Failed, empty, mismatched, or nonterminal reads emit a
FAIL row with MANUAL RECOVERY REQUIRED and the request ID. The operator must
inspect that operation and confirm terminality before recovering retained resources.

Return the aggregate verdict to `_teardown`; an unproved operation sets
`run.clean = False` and stops resource deletion because execution may remain active.
Already-terminal requests need no abandon or read. An unsuccessful abandon response
still receives a status read; actual terminality governs cleanup safety.
See [ADR 0390](../../adr/0390-confirm-abandon-before-cleanup.md).

## Success

- C1: each nonterminal provision, decommission, and power abandon is followed by
  the matching status read, including when abandon itself fails.
- C2: unproved terminality fails cleanup, preserves resources, and names the request
  in a manual-recovery row; a terminal confirmation permits existing cleanup.
- C3: offline arm tests exercise the three operation kinds and both shared arm groups,
  terminal/nonterminal, unreadable/missing/mismatched status, and mixed requests.

## Global constraints

Python >=3.11; existing locked dependencies; no dependency or public API changes.
Hosted CI targets amd64; local verification on macOS arm64 is separate evidence.
No live HMC run is needed or authorized.

## Failure model

- Actors and deployments: authorized local live-test operator running ST41/ST72;
  offline CI uses the production RunState with a scripted MCP client.
- Invariants and assets at stake: a running operation can still mutate run-owned
  partitions and storage; cleanup cannot claim success while terminality is unproved.
- Accepted failure classes: terminal status is a point-in-time observation, not proof
  against an unrelated operator subsequently mutating resources; this change adds no
  polling or execution cancellation guarantee.
- Covered elsewhere: existing operation lifecycle tools own abandon semantics;
  existing teardown ownership and inventory guards own resource deletion safety.

## Threat model

- Boundary inventory: reuse the existing MCP status-result boundary; no entry point
  or permission is added or widened.
- Actor model: trust the configured HMC/server and authorized operator; malformed or
  absent status must not authorize destructive cleanup.
- Control per boundary: existing served-schema validation and `_items` adaptation;
  successful read plus matching request ID plus terminal state are required.
- Explicitly out of scope: authentication/transport trust belongs to existing MCP
  and HMC configuration, unchanged by this bounded cleanup correction.

## Validation

Use the plan's focused-test inventory and offline fake HMC. Run the affected arm
module, static guardrails, `just verify`, and pinned prek. No test snapshots prose.
