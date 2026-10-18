# Existing-code verification baseline (#635)

## Problem

The 163-operation registry joins the reference ledger, but 11 operations have no
maturity record. Test links can be empty. Closed domain verification issues are
not evidence that every supported operation passed native postconditions.

## Scope and design

Freeze the supported set to the registered operation IDs represented by
`docs/capabilities/operations.json`, rechecked against the current registry. The
reference denominator remains `rows.json`; this baseline does not complete it.
Extend the existing inventory validator to reject an omitted maturity operation
and an empty automated-test list. Preserve the sparse-reader `unrecorded` state
for historical release snapshots and diagnostic derivation, while the current
checked-in catalog becomes complete. No runtime or observation format changes.

Reconcile the 11 missing records from current handlers: VIOS create, delete,
storage-detail read and power jobs; local permissions/configuration/operation
inspection; tool search/invocation; the generic command escape hatch. Empty
live evidence means unevidenced. Correct `config.list_hosts`'s unrelated HMC
reference links to an explained local composition. Add behavioral test links
where metadata/security tests alone fail to represent the operation. Review
current `user.modify` and provisioning implementation scope against their actual
fixes and denials without changing their historical failed observations.

A bounded `docs/capabilities/baseline.md` groups the current supported set by
existing domain procedure. It records native prerequisites, unavailable or
failed cases, and durable linked obligations. Local-only operations explain why
hardware verification is inapplicable; gateway/command tests prove only their
bounded dispatch behavior, not the HMC commands delegated. Reference and test
links remain owned by `operations.json`; implementation and observations remain
owned by schema4 `maturity.json`. Do not duplicate the live evidence or create
a second machine-readable evidence format or new gate.

Keep release-verification.json byte-identical. Regenerate packaged maturity and
tool documentation through existing recipes. Public facade exports, runtime
authorization/admission, dependencies, and native runner stay unchanged.

## Known failures and gaps

Preserve each failed observation: `lpar_profile.restore`, `lpar.set_msp`,
`user.modify`, and `provision.lpar`. Distinguish source fixes from live proof.
MSP now resolves a VIOS; user modification resends required profile fields and
an empty password element; provisioning explicitly refuses dedicated slots.
The latter is partial implementation, not implemented dedicated assignment.
Restore type3's resource_config reset is already an explicit tool warning with
manual recovery; its historical unchanged-partition assertion remains failed,
and no successful profile-content restoration is inferred from it.

Use the repository live-testing procedure for applicable native hardware proofs.
This issue grants no hardware authority. Missing environments leave durable
obligations, not passed/skipped observations. Existing historical evidence may
be stale under current closures and remains historical under ADR0212.

## Success

For the operation set joined by the current registry and operations.json:
each operation has a valid maturity record, a reference or composite disposition,
and nonempty existing behavioral/contract test links. The inventory check fails
for a missing maturity record or empty tests. The baseline document maps native
families to procedures/prerequisites and remaining obligations. Catalog evidence
and the frozen release snapshot are unchanged; implementation-only corrections
regenerate accurately. No full-reference-completion claim is made.

## Failure model

- Actors and deployments: repository maintainers and CI running the offline
  inventory check; installed CLI/MCP discovery reading generated metadata.
- Invariants and assets: no silent supported-operation omission, no false live
  promotion, no conflation of implemented scope with admitted or tested scope.
- Accepted failure classes: unavailable authorized native environments retain
  explicit procedure/prerequisite/reason/obligation links; historical observations
  can be stale without blocking development under ADR0212. Neither is success.
- Covered elsewhere: API expansion belongs to later #620 children, full reference
  completion to #698, migration implementation/verification procedure to #631,
  runtime safety/authorization to existing guards; live execution to operators.

## Validation

- `focused-test`: tests/scripts/test_check_capability_inventory.py rejects removal
  of a registered maturity record and an empty automated list. Assert diagnostics
  against constructed inventories; first observe red before implementing guards.
- `focused-test`: committed-catalog assertions cover the missing records, local
  composite binding, provisioning denial scope and preservation of failed evidence;
  perform controlled faults to prove negative cases bite.
- `focused-test`: run the relevant VIOS lifecycle/power, local gateway/status/
  configuration/permissions, user/profile/provision and inventory tests. These
  assertions establish automated behavior, not native proof.
- `focused-test`: existing capability-inventory and tool-docs-check compare generated
  metadata with the catalog after regeneration.
- `task-test-not-applicable`: baseline prose is an authored interpretation and
  operator procedure, with no executable consumer. Review source links and each
  named family; do not snapshot wording to claim native proof.
- Final candidate: `just verify` and `uv run --no-sync prek run --all-files`, then
  amd64 CI Python3.11–3.14 and dependent wheel checks. Local host is Darwin arm64;
  no project target is declared. No native test is authorized by this plan.
