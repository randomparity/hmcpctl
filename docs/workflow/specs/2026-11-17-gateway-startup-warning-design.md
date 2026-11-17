# Gateway startup reachability diagnostic

## Problem

Issue [#1921](https://github.com/randomparity/hmcpctl/issues/1921) identifies silent
gateway reachability gaps under the primary catalog. A policy can admit specialists
while withholding invocation, or both search and invocation.

## Scope

Extend `_startup_warnings` in `src/hmcpctl/server.py`, using `permits_tool` and the
gateway name constants. For a nonempty served registry with invocation withheld,
append one line naming the withheld gateway(s). If search is admitted, explain
that permitted specialists require direct calls by name; otherwise explain that
clients must already know permitted specialist names. Describe this as conditional
reachability, without claiming that a policy grants specialists.

Keep the existing empty-surface suppression, `_warn` sink, registration, listing,
and authorization behavior. Search withheld alone adds no diagnostic. Existing
startup ownership is appropriate; no ownership transition or caller migration is
needed. Document the read-only example's observed warning in `docs/mcp-server.md`.
No new interface, configuration, dependency, or architecture decision is needed.

### Failure model

- Actors and deployments: local operators starting stdio or HTTP MCP with compiled policies.
- Invariants and assets: accurate policy diagnostics; empty-surface suppression;
  bounded stderr output and untouched JSON-RPC stdout; existing policy ceiling.
- Accepted failures: diagnostic delivery can be dropped by the existing bounded sink
  to preserve startup availability (ADR 0043); no new delivery guarantee.
- Covered elsewhere: grants/authorization by access-policy contracts; listing by
  ADR 0330; arbitrary-command discovery/direct-name behavior by #1920 and ADR 0391.
- Threat boundary: operator-controlled policy name reaches the existing stderr sink;
  reuse its repr rendering and bounded writer. No boundary is added or widened.
- Actor trust: policy authors control local configuration; MCP callers do not control
  startup policy text. Existing authorization wrappers continue to govern calls.
- Controls: `permits_tool` supplies the ceiling; `_warn` owns diagnostic delivery.
- Out of scope: granting access, changing listings, and arbitrary-command discovery
  retain the owners above and the operator-approved campaign exclusions.

## Success

The invoke-withheld and both-withheld cases each produce exactly one gateway line
with the appropriate reachability explanation. Unrestricted and search-only-withheld
policies produce no gateway line. An empty surface retains its single no-tools line.
The documented read-only output matches the observed diagnostic.

## Validation

- `focused-test`: policy matrix in `tests/app/test_capability_ceiling.py`; red is a
  missing gateway line; green command: `uv run --no-sync pytest --no-cov -q
  tests/app/test_capability_ceiling.py tests/app/test_fail_closed_startup.py
  tests/app/test_authorization_audit.py`. Controlled missing-warning, wrong-search
  branch, and empty-surface mutations must fail relevant assertions.
- `task-test-not-applicable`: guide prose has no executable consumer; observe the
  read-only startup stderr through the existing serving entry point and compare it.
- Run `just verify` and `uv run --no-sync prek run --all-files`; CI supplies the
  declared Linux amd64 Python 3.11–3.15 coverage beyond local Darwin arm64 3.11.
