# Reconfiguration workflow package

## Problem and decision
Three modules implement one journaled reconfiguration workflow but sit among 33 LPAR siblings.
Move existing owners without wrappers: reconfigure.py -> reconfiguration/core.py,
reconfigure_media.py -> reconfiguration/media.py, reconfigure_network.py -> reconfiguration/network.py.
The initializer contains only a package docstring. Do not split or redesign the 1804-line core.
Adjust relative imports for one extra package level; core imports actual sibling media/network.
Move all existing declarations/constants verbatim, preserving comments and literal strings.
Migrate server_tools/lpar/reconfigure.py and operations/lpar/handoff.py and all direct test
imports/patch lookup sites. The external operation identifier lpar.reconfigure stays unchanged.
Old internal modules disappear. No compatibility re-export/alias or product schema addition.

## Failure model
Offline tests exercise a deterministic internal ownership move, not new hardware behavior.
Required retained protections: binding/authorization, profile-save uncertainty/no blind replay,
confirmed partial effects, ledger/effect identity, disk/network/media ordering, continuation,
strict parsing, client lifecycle, privacy diagnostics and TLS fatality.
Accepted class: old untracked private import paths and old source-bound captures become stale;
all in-repo consumers migrate and the existing fingerprint algorithm follows new imports.
This move does not establish live hardware proof or freshen persisted observations.

## Validation
Task-test-not-applicable: pure raw-file relocation with import binding adjustments adds no
independent behavior; existing tests, normalized whole-module AST equality and actual tool
schema/call metadata equality prove retained contracts. Do not invent text/path snapshot tests.
Exercise existing lazy-import boundary test with its actual updated module path; no weakened gate.
Capture complete 207-tool models and source closure before/after; schema/meta must equal while
closure paths/digests change honestly to the new owner. Existing literal old selected-source paths
must migrate only if genuinely present; no arbitrary expansion of finite source witnesses.
Run all reconfigure unit/app/native ownership/provision-install/handoff and corresponding
live logical/reconfigure arm/wrapper/recovery evidence cases; smoke, lint/type/format/diff,
capability/doc/scenario guards and all 14 normal commit hooks. No hardware run.
The orchestrator retains final integrated `just verify`, pinned all-file prek and whole-branch review.

The private schema capture must compile the legacy policy with
`compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,), include_arbitrary_command=True)`
and build `create_mcp(policy, catalog="registered")`. Enable the arbitrary-command tool with
`configure_arbitrary_command_tool(True, mcp, permits=policy.permits_tool,
authorize=dispatch_authorizer(policy))`, then use a FastMCP `Client(mcp)` to call
`list_tools()`. Compare each tool's complete `model_dump(mode="json")`, keyed by name;
canonicalize JSON object keys and sort JSON Schema `required` arrays. This captures 207
registered tools; the default legacy policy omits arbitrary commands and captures 206,
while the primary wire catalog exposes 11. Keep the captures private and compare the same
explicit catalog and policy on both sides of the move.
