# ADR 0249: Scenario definitions derived from the ledger

## Status
Accepted (2026-10-19), issue #1093.

## Context
ADR 0247 gave live scenarios a declared format and left
`scripts/live_test/scenario_definitions.json` empty. Most ledger operations need nothing
but a target to be exercised once, yet each would otherwise be hand-written by a #620
child. A definition commits literal argument values, but a lab's target identities may not
be committed, and a mutating definition claims `expected: success` for a state change.

## Decision
`scripts/gen_scenario_definitions.py` writes the registry from
`docs/capabilities/operations.json`, the served input schemas
(`runner.served_client`), and `TOOL_SECURITY`. For each ledger operation, in operation
order, it emits a definition only when:

- the operation's tool is registered and served;
- no argument the served schema requires is a nested selector's container, and every one
  is a top-level target selector, which the definition supplies as the ADR 0247 placeholder
  of that selector's kind; nothing else is supplied; and
- for a non-`read` effect, every other argument the schema declares is a top-level target
  selector, the connection argument, or `ownership_override`, whose default refuses a
  foreign-owned partition.

Every other operation is printed as `hand-written: <operation>: <reason>` and gets no
definition. A read-only definition leaves optional arguments at their defaults; a read
with defaults is still a read. A mutating operation with any other optional argument — a
change set, a mode, an `immediate` flag, a confirmation gate — is one whose defaulted call
may be a no-op or a refusal, so it fails closed to hand-written. That includes `wait`: it
defaults to `false` in every served schema declaring it, so a defaulted job call reports
submission, not the state change `expected: success` claims. The generator validates
its document with `definitions.load_definitions` before writing, and never runs one.

Freshness is a test in the suite comparing a fresh generation with the committed file;
`just scenario-definitions` regenerates it.

## Consequences
At 2e2ca227 this yields 70 definitions (65 read-only, 5 mutating) and reports 95
operations as hand-written, including `lpar.plan` and `provision.lpar` (the operation ADR
0247 calls `lpar.provision`). The 5 mutating definitions are deletes and a boot-order
clear; ADR 0247 already bars any executor from running one without a per-run operator
selection. A new optional parameter on a mutating tool
moves its operation to hand-written and reddens the freshness test until regenerated.
Definitions never count as exercised, so the gap report's coverage is unchanged. An
operation the ledger marks composite but that one served call reaches (`lpar.inspect`,
`inventory.logical`) is derived like any other; multi-call composite scenarios stay with
the #620 children.

## Considered & rejected
- **Leave the registry empty; children hand-write every definition.** judgment: issue
  #1093 asks for the derivable cases to be generated; the empty registry stays the rollback.
- **Derive mutating operations by the read-only rule.** verified: this generator's rule
  without its non-`read` clause, run over `operations.json` and
  `runner.served_schemas(runner.served_client())` at 2e2ca227, admits 27 mutating
  operations, among them `lpar.modify`, `system.modify` and `user.modify` with no change
  argument.
- **Admit `wait`, `poll_interval` and `timeout_seconds` as run controls.** verified: the same
  served schemas declare `wait` with default `false` on all 21 tools that have it, so six
  more definitions would claim success for a job submission.
- **Supply `"wait": true` on those six.** judgment: a chosen literal whose outcome still
  rides on a defaulted timeout; a hand-written scenario can choose both.
- **Read-only definitions only.** judgment: the operator chose to cover single-operation
  definitions in this change.
- **A hand-kept list of mutating operations to skip.** judgment: a new mutating tool would be
  derived by default; the chosen rule fails closed instead.
- **Placeholder or example literals for required non-target arguments.** judgment: a
  literal nobody chose makes `expected: success` unfounded.
- **A `static` recipe and prek hook for freshness.** judgment: the suite already runs in
  every CI leg; a gate, a hook and a `STATIC_GATES` entry add three places for one check.
- **Commit the hand-written list.** judgment: a second generated file to keep fresh; the gap
  report already counts defined operations.
