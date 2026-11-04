# 0330 — A served application lists the primary catalog; harnesses opt into the registered listing

## Status

Accepted (2026-11-03), issue #1853. Carries out ADR 0189 Decision 4's listing switch; ADR 0189
otherwise stands.

## Context

ADR 0189 fixed an eleven-tool primary set (`PRIMARY_TOOLS`) and said a direct `tools/call` of
a permitted specialist keeps working once the default listing switches to it. Until now
`OperationMaturityMiddleware` only marked each listed tool `primary` or `secondary`; every
registered tool was listed (206 registered, 205 under the legacy-equivalent policy).

Two kinds of caller read the listing. MCP clients use it to choose tools. In-repo harnesses —
the live-test runner's `served_client`, `scripts/gen_tool_reference.py`, the scenario
generators, and about fifty test modules — read it as "every registered tool and its schema".
The switch has to serve the first without breaking the second.

## Decision

1. **The listing is `PRIMARY_TOOLS` intersected with what is registered.** The existing
   `OperationMaturityMiddleware.on_list_tools` drops every tool whose name is not primary.
   Registration already applies the policy ceiling (ADR 0038), so a primary tool the policy
   withholds is absent too. Server-side dispatch does not change: `tools/call` of a
   registered tool by name, `hmc_search_tools`, and `hmc_invoke_tool` reach the same tools
   under the same guards as before.
2. **`create_mcp(policy, *, catalog="primary")`.** The keyword takes `"primary"` (default) or
   `"registered"`. `"registered"` lists every registered tool with its tier metadata, which is
   the listing before this change. It exists for in-repo harnesses that need specialist
   schemas over a client session. No CLI flag, configuration key or environment variable
   reaches it: `hmcpctl serve` always composes `"primary"`.
3. **Tier metadata stays.** Every listed tool still carries
   `io.github.randomparity.hmcpctl/catalog-tier`; under `"primary"` it is always `primary`,
   under `"registered"` it still distinguishes the two tiers.
4. **The instructions name the primary tools.** `INSTRUCTIONS` describes the listed tools and
   says specialists are found with `hmc_search_tools` and called by name, or through
   `hmc_invoke_tool` where it is granted. The ADR 0048 qualification keeps naming withheld
   tools; it now says a withheld tool cannot be called by name or through invoke, and that
   `tools/list` is the primary subset of what the server exposes, not the whole of it.
5. **`hmc_run_command` is not listed, even when enabled.** It is not in the ADR 0189 set and
   `hmc_invoke_tool` refuses it, so with `--enable-arbitrary-command` and a grant it is
   reachable only by a direct `tools/call` of its name.

## Consequences

- A client that calls only listed tools reaches specialists through search and invoke, one
  extra call per specialist use; the scenario report under `docs/workflow/` records counts.
- `hmc_invoke_tool` is `destructive`, so an `effects = ["read"]` or read+mutate grant
  withholds it. Under such a policy the listing holds the granted read primaries (five under
  the documented read-only example) and a client reaches a specialist only by finding it with
  `hmc_search_tools` and calling it by name. A policy that withholds both gateway tools
  leaves its specialists reachable only by a caller that already knows their names; with no
  primary tool granted, the listing is empty.
- A client has no output schema for an unlisted tool, so it cannot validate a direct call's
  structured content; fastmcp's client logs that and returns the result untyped.
- `hmc_effective_permissions` is unlisted; it stays callable by name and through invoke.
- An operator who enabled the escape hatch for a listing-driven client loses it from that
  client (Decision 5). Widening the primary set is an ADR 0189 change, not this one.
- Tests and harnesses that read the listing as the registered set must compose with
  `catalog="registered"` or read the registry directly.

## Considered & rejected

- **Hide specialists with FastMCP's `disable`.** verified: on fastmcp 4.0.10,
  `m.disable(names={"b"})` removes `b` from `list_tools()` and `call_tool("b", {})` raises
  `ToolError: Unknown tool: 'b'`, which breaks ADR 0189 D4's direct call.
- **Do nothing (keep the tier metadata only).** judgment: fit. ADR 0189 D4 and #1853 ask for
  the switch.
- **An operator flag to restore the full listing.** judgment: fit. Nothing asks for it, and
  every permitted specialist stays callable by name; the policies that withhold invoke are a
  stated consequence above, not a reason to keep the old listing.
- **Keep `"registered"` as the default and pass `"primary"` only from `serve`.** judgment:
  fit. It saves test churn, but the default should be what clients see, so a new composer is
  safe without remembering the argument.
- **Harnesses read `list_tools(run_middleware=False)` instead of a keyword.** judgment: fit. The
  live runner deliberately reads schemas over a client session, which has no such switch, and
  the bypass skips the middleware that attaches maturity and tier metadata. In-process
  callers that need only names may still read the registry directly.
- **List `hmc_run_command` when it is enabled.** judgment: fit. It contradicts ADR 0189's
  exactly-eleven set, which this change implements rather than revisits.
