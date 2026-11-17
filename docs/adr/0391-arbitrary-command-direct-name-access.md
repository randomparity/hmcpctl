# 0391 — Keep arbitrary-command invocation explicit by tool name

## Status

Accepted (2026-11-17), issue #1920. Clarifies ADR 0330 Decision 5; ADR 0189's
primary set and ADR 0330's listing decision remain in force.

## Context

An operator can enable and grant `hmc_run_command`, yet a client using only
`tools/list` and `hmc_invoke_tool` cannot run it. ADR 0330 Decision 5 records this
consequence; #1920 asks whether to change listing/invocation or document it explicitly.
The operator chose to keep direct-name access and document it explicitly.

## Decision

Keep `hmc_run_command` outside ADR 0189's eleven-tool primary catalog and keep
`hmc_invoke_tool`'s refusal of arbitrary-command tools. Invocation deliberately
requires a direct MCP `tools/call` with `name: "hmc_run_command"`.

Document that call in the existing MCP operator guide, including `cmd`, optional
`profile`, the `--enable-arbitrary-command` flag, an explicit named policy grant,
and `targets = "all-targets"`. The selected connection must also be granted.
Search may describe the enabled, permitted tool when `hmc_search_tools` is itself
granted; that discovery does not make it invokable through the gateway.

## Consequences

- Clients restricted to listed tools and gateway invocation cannot use this escape
  hatch. They need support for directly calling a known tool name.
- Enabling the flag alone grants no permission. A direct call retains the existing
  connection authorization and audit checks; this decision adds no bypass.
- The guide provides the invocation contract without widening discovery or adding
  startup warnings. Dedicated tools remain preferable when available.

## Considered & rejected

- **List the enabled escape hatch.** judgment: retain the focused eleven-tool
  catalog; an arbitrary-command exception would widen that accepted interface.
- **Allow it through `hmc_invoke_tool`.** judgment: retain the deliberate distinction
  between the arbitrary-command escape hatch and gateway delegation.
- **Explain access only in startup output.** judgment: the existing operator guide
  can carry a reusable protocol example and its policy prerequisites together.
