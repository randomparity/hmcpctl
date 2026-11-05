# 0339 — System-wide partition writes refuse while a system has holds

## Status

Proposed (2026-11-04), issue #1887. Supersedes ADR 0327 Decision 5 only for the
six system-wide tools enumerated in the accompanying design.

## Context

ADR 0327 leaves system-scoped writes outside the partition-resolution hook. A held
partition can therefore be stopped or reconfigured through its managed system. The
operator selected "Refuse while any system hold exists" and accepted releasing holds
before these writes, without an override or multi-token authorization.

## Decision

Mark `hmc_modify_system`, `hmc_power_on_system`, `hmc_power_off_system`,
`hmc_activate_system_profile`, `hmc_restore_lpar_profiles` and `hmc_update_firmware`
as system-wide partition writes in the registry. Their existing managed-system UUID
resolution checks the local hold store through the front-end-installed guard. One hold
on that system refuses the call with `system_held`, regardless of caller or hold ID;
unknown store state also refuses. Holds on another system do not block it.

MCP dispatch, including invoke, and existing CLI mirrors carry the marker centrally.
This is a whole-tool rule, including rename-only or validate-only argument variants.
No system hold token or override is introduced. Keep partition-token behavior unchanged.
Update handoff disclosure and consumer docs. Other ADR 0327 decisions remain in force.

## Consequences

Holders must release all holds on the system before these operations, accepting the
loss of their hold protection while doing so. Existing ownership/access-policy checks
still apply. The check has ADR 0193's resolver-time semantics: it does not lock the HMC
job against a hold placed after admission, and does not fence library/raw/other-host
writers. No store migration or dependency is needed. Resource-only and creation tools
remain outside the six-tool set; the design records the complete current inventory.

## Considered & rejected

- **Keep system writes unchecked.** judgment: fit. It leaves the held-partition hazard
  in issue #1887 despite the operator's chosen refusal policy.
- **Accept every hold ID or an override.** judgment: complexity and fit. It adds an
  authorization interface the operator expressly declined.
- **Check separately in each handler and CLI command.** judgment: complexity. One
  registry marker and shared resolver keep policy and mirror coverage in the existing owners.
- **Fence every non-read managed-system tool.** judgment: fit. That broadens the rule
  to new-partition creation and unrelated resource records beyond the chosen boundary.
- **Condition the check on individual arguments.** judgment: complexity. It creates
  exceptions within tools whose safe whole-tool refusal the operator accepted.
