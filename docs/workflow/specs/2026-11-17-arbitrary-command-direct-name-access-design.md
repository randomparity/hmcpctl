# Deliberate direct-name access for arbitrary HMC commands

Issue: #1920. Decision: [ADR 0391](../../adr/0391-arbitrary-command-direct-name-access.md).

## Problem

A client restricted to the primary listing and invoke gateway cannot execute an
operator-enabled `hmc_run_command`. The operator chose direct-name access with
explicit documentation. Search already describes this tool when enabled and granted.

## Scope and architecture

Extend the existing MCP operator guide's discovery section with an arbitrary-command
subsection. Show a direct MCP `tools/call` request using `cmd: "lshmc -V"`, explain
optional `profile` and the environment default, and link the generated command reference.
State the flag, explicit named tool grant, granted connection and all-targets requirements.
Qualify the guide's general specialist invocation wording for this exception.

ADR 0391 clarifies ADR 0330 Decision 5 while retaining ADR 0189's eleven-tool set.
Server registration, search and invocation own their current behavior; no ownership
transition, direct caller migration, compatibility path or executable removal is needed.
The guide owns operator instructions; the ADR owns the durable decision.

Exclusions and owners: General primary-catalog expansion — ADR 0189/future catalog
issue; generalized gateway effect redesign — separate authorization design;
gateway-withholding warnings — #1921.

## Success

- C1: ADR 0391 records direct-name invocation and its consequences, retaining the
  eleven-tool catalog and invoke refusal (issue #1920 and operator decision).
- C2: The guide gives the direct request, arguments and conjunctive prerequisites,
  and distinguishes search discovery from invoke access (operator decision and
  existing command/gateway authorization contract).
- C3: The diff changes only the guide, this specification and ADR 0391; existing
  listing, registration, authorization and invocation behavior is preserved.

## Global Constraints

Python support remains >=3.11. Hosted CI targets amd64 Python 3.11–3.15; local
verification uses macOS arm64 Python 3.11.15. No dependency or toolchain change.

## Failure model

- Actors and deployments: operators configuring `hmcpctl serve`; MCP clients able
  to send direct requests; clients restricted to listed tools or gateway invocation.
- Invariants and assets at stake: published access contract; arbitrary-command
  opt-in and named grants; primary catalog and invoke refusal remain intact.
- Accepted failure classes: listing/invoke-only clients cannot execute the escape
  hatch by deliberate operator choice; unavailable SSH/configuration remains an
  execution error covered by the existing command path, not a guide change.
- Covered elsewhere: primary-set expansion — ADR 0189/future catalog issue;
  generalized gateway authorization — separate design; startup warnings — #1921.

## Threat model

- Boundary inventory: no added or widened runtime boundary; the guide describes
  the existing MCP client-to-command and command-to-SSH boundaries.
- Actor model: operator controls policy/flag; an MCP client remains constrained by
  that policy. Arbitrary HMC command text is privileged input.
- Control per boundary: registration needs flag and named grant; dispatch enforces
  connection and all-targets authorization; the SSH transport executes `cmd`.
- Out of scope: new command restrictions, grants, gateway effects and SSH behavior;
  those runtime policies remain governed by their existing implementations.

## Validation

Human review traces each guide statement to command registration and gateway source;
existing focused gateway/command tests corroborate unchanged behavior. No new prose
snapshot test: human-readable guidance has no executable consumer and text matching
would measure wording rather than the decision's accuracy. Run documentation guards,
focused tests, the full `just verify` suite and the pinned all-files prek hooks. No HMC run.
