# Recorded decommission target authorization (#1886)

## Problem and authority

Request-ID-only decommission resume/abandon is documented but denied before its handler
under target-limited grants. The reported bypass was disproven. The operator approved
“Approve bounded integration” on 2026-11-04; scope token `q1886-dd62cc06` binds the charter.
[ADR 0334](../../adr/0334-authorize-recorded-decommission-targets.md) records the decision.

## Global Constraints

Python >=3.11; CI Python 3.11–3.14 on amd64. No new dependency, public tool parameter,
alias policy or store schema. Provision redesign and logical-store schema changes remain
excluded, owned by the operator. Exact original selector strings govern authorization.

## Architecture and behavior

`dispatch_authorizer(policy, *, resolve_targets=None)` accepts a callback of signature
`(str, Mapping[str, Any]) -> Mapping[str, Any] | None`. `server._gates` supplies
`decommission_authorization_targets`; it returns None for other tools and new calls.
After raw connection resolution, the existing sole grant loop calls it once upon the
first connection match. Its mapping replaces target extraction only: the selected
connection and actual handler arguments are unchanged. Subsequent grants reuse the same
extraction. Final target and connection permission must hold within one current grant.

The callback reads `engine.recorded_arguments(agent_id, request_id, TOOL_NAME)` using
local configuration. It requires both original selectors to be nonblank strings and
never substitutes supplied arguments or canonical UUIDs. Unknown/wrong-tool records,
malformed recorded selectors, and typed config/store/JSON read errors become a fixed
`TargetScopeError`, without chained private diagnostics. Dispatcher records that refusal
once as `deny/target-selector-unreadable` with null targets. A resolved but ungranted
record gets one ordinary target-denial audit with a fixed public error containing no
recovered values. Ordinary raw-call errors and decisions remain unchanged.

Matching supplied selectors and partial omissions work. Before overlay, the existing
`effective_arguments` continuation path rejects conflicting supplied selectors with
`OperationRefused(request_conflict)` and fixed wording. The CLI keeps its separate recorded-only continuation path.
The engine retains full argument, connection, host and tool binding. Authorization can
allow a recorded target before operation validation rejects a conflicting request; this
is a completed authorization, not a preliminary allow. Owned-storage specialist checks
remain intact. A missing canonical partition may reach existing fallback preview only
after original-selector authorization; missing original selectors cannot reach preview.

## Success and validation

C1–C5 from WORK:SCOPE are tested through the actual registered wrapper and composed
server with real policy/dispatcher, temporary records and sealed HMC boundaries: own and
other targets; resume/abandon; retain/delete_owned; matching/partial/conflicting selectors;
name/UUID exactness; split grants; denied tool/connection before record reads; unknown,
wrong-tool and malformed records; host/profile binding; specialist denials; single truthful
audit and safe errors. Run focused tests before/after and controlled faults for the new
boundaries, then `just verify` and pinned `uv run --no-sync prek run --all-files`.
C6 is the inspected provision finding in ADR 0334 and the PR; no new prose test is useful.

## Failure model

- Actors and deployments: MCP callers under configured access policies; local CLI users;
  one operator-controlled durable store; local verification and amd64 hosted CI.
- Invariants and assets: original-selector exactness, one-grant conjunction, protected
  record contents, truthful authorization audit, partition/storage integrity.
- Accepted failure classes: concurrent trusted administrative replacement of records is
  outside this repair; operation request identity is immutable through supported APIs.
- Covered elsewhere: engine admission owns atomic binding/state transitions; storage
  specialists own storage grants; store validation owns persistence integrity; audit sink
  owns bounded delivery failures (ADR 0040). No live HMC change is required by this issue.

## Threat model

- Added boundary: local recorded arguments become authorization input. Widened boundary:
  a scoped MCP caller may now continue a covered operation without repeating selectors.
- Actor model: MCP callers control request IDs, selectors and profile tokens; they do not
  control trusted policy/configuration/store files. The local operator controls those files.
- Controls: tool/connection eligibility precedes lookup; original-selector validation and
  same-grant checks precede handler; generic denial text conceals stored values; early
  selector conflict check and existing engine binding prevent redirection.
- Out of scope: local operator compromise and provisioning redesign; existing engine,
  persistence and specialist controls remain responsible for their established contracts.
