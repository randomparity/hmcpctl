# 0189 — A logical LPAR catalog whose actions are authorized by the specialist tools they use

## Status

Accepted (2026-10-01), issue #1216. Partially supersedes ADR 0012 in three places. First, a
primary logical tool may carry an action discriminator whose variants differ in effect class
(Decision 2). Second, `hmc_invoke_tool`'s result is the invoked tool's own result, wrapped as
`{name, result}` (Decision 3). Third, the primary tools' names, which epic #1215 fixes, are
exempt from ADR 0012's `hmc_list_<resource>` / `hmc_get_<resource>` grammar (Decision 1). ADR
0012 otherwise stands, including the one-shape rule and the naming grammar for every other tool.

> **Partially superseded by [0196](0196-logical-inventory-authorizes-each-source.md)** (2026-10-02)
> **Partially superseded by [0198](0198-lpar-plan-delegates-to-the-reads-it-makes.md)** (2026-10-02)
> **Partially superseded by [0200](0200-lpar-inspect-reports-each-section-on-its-own.md)** (2026-10-03)
> **Decision 4 carried out by [0330](0330-default-listing-is-the-primary-catalog.md)** (2026-11-03)

## Context

The registry serves 157 tools; a permitting policy advertises 156 (`tool_registry.py`,
`server_tools/catalog.py`). Epic #1215 wants nine logical tools plus `hmc_search_tools` and
`hmc_invoke_tool` advertised, with the specialists still reachable.

A served policy binds two things only: a tool's name and its effect class. `_resolve_tools`
admits a tool a grant names or whose `effect` it lists, and nothing reads
`ToolSecurity.operation` (ADR 0188 Context). Connection and target scope are checked per call
by `authorized()` → `dispatch_authorizer` (ADRs 0038, 0039). So a logical tool registered with
one effect cannot by itself express "may start, may not stop".

## Decision

1. **The primary set is exactly eleven tools:** `hmc_inventory`, `hmc_plan_lpar`,
   `hmc_provision_lpar`, `hmc_reconfigure_lpar`, `hmc_decommission_lpar`, `hmc_power_lpar`,
   `hmc_inspect_lpar`, `hmc_prepare_host_handoff`, `hmc_operation_status`,
   `hmc_search_tools`, `hmc_invoke_tool`. `hmc_provision_lpar` and `hmc_decommission_lpar`
   extend the existing composites under their existing names; no competing tool is added.
   The names are the epic's, so `hmc_inventory`, `hmc_inspect_lpar`, `hmc_operation_status` and
   `hmc_search_tools` do not take the ADR 0012 list/get grammar; new specialists still do.
2. **Delegated authorization.** Each logical action names the specialist tools whose
   operations it performs (the spec's action table). The action runs only when the served
   policy permits every delegated tool *and* `dispatch_authorizer` admits the call, as that
   tool, for every resolved target, including nested VIOS and volume-group targets. The
   logical tool's own grant is necessary, never sufficient. The logical tool registers with
   the most severe effect any of its actions can have, so client approval prompts stay
   conservative; that is the ADR 0012 relaxation.
3. **Search and invoke.** `hmc_search_tools` (effect `read`) returns at most 20 entries, and at most
   one full input schema when an exact name is given, drawn only from tools the policy permits.
   `hmc_invoke_tool` (effect `destructive`) dispatches one permitted, registered tool through that
   tool's own `authorized()` wrapper, so validation, target scope, ownership, the ADR 0193 hold
   check, maturity metadata and the ADR 0040 authorization record all apply as for a direct call.
   Its result is `{name, result}`. It refuses the two gateway names and any tool whose effect is
   `arbitrary-command`. An unknown name and a withheld name return the same denial.
4. **Advertisement is a listing choice, not an authorization one.** When #1232 switches the
   default listing to the primary set, a direct `tools/call` of a permitted specialist name
   keeps working. Until #1232, the primary tools carry metadata marking them primary and the
   listing is unchanged.
5. **`hmc_inspect_lpar` is `read`.** Console capture stays the specialist
   `hmc_capture_lpar_console`; inspection returns a next-action reference to it, not a
   capture.

## Consequences

- A policy that grants a logical tool but withholds, say, `hmc_power_off_lpar` gets a working
  `start` and a denied `stop`, with the delegated tool named in the denial.
- Logical tools need each delegated specialist to stay registered and permitted. Renaming a
  specialist changes the logical tool's effective authority and needs a test that maps every
  action to registered names.
- A read-only deployment that wants invoke must name `hmc_invoke_tool` in a grant explicitly;
  `effects = ["read"]` does not admit it. Invoke still reaches only `read` tools there.
- Policies keep binding name and effect only; no operation or action key is added.

## Considered & rejected

- **Register one logical tool per action (`hmc_start_lpar`, `hmc_stop_lpar`, …).** judgment:
  fit. It rebuilds the flat catalog the epic exists to shrink, and the epic names
  `hmc_power_lpar`.
- **Add an `actions` key to grants.** verified: `rg -n operation
  src/hmcpctl/authorization/access_policy.py` returns nothing; grants resolve by `tools` and
  `effects` only (main `f1b302db`). judgment: a policy-model change wider than delegation, which
  already expresses the split.
- **Authorize logical tools only by their own grant.** judgment: fit. A grant for
  `hmc_power_lpar` would silently confer stop and restart authority the operator withheld
  from the specialists.
- **Filter `tools/list` only, with no gateway.** judgment: fit. Clients that call only listed
  tools would lose the specialist catalog.
- **Let invoke reach `hmc_run_command`.** judgment: fit. It would turn the gateway into an
  arbitrary-command path behind a `destructive` annotation.
