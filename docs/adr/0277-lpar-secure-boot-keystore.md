# ADR 0277: Bounded partition secure-boot and keystore policy

## Status

Proposed (2026-10-23) — issue #1765; frozen C04 scope and two operator decisions.

## Context

Five native partition fields have eighteen documented P10/P11 create/update
bindings. They can weaken boot verification or irreversibly enable signed updates.
The observed HMC rejects `secure_boot` as an `lssyscfg` read attribute, while
captured BasePartition schemas and contained feeds expose pending/current policy.

## Decision

Extend existing LPAR and VIOS owners with a strict five-field settings object,
one fixed native writer and contained REST readback. Compose requested creation
fields in the existing single native record, retaining each unchanged creation
route when policy is absent. Verify before explicit apply or dependent assignments.
Require positive runtime capability, source-backed applicability and transition
admission. Report pending configuration separately from current boot policy.
Use explicit settings plus existing CLI confirmation/`--yes` and warnings as
consent; add neither a security acknowledgement nor a Linux-intent parameter.
Selection of the Linux field declares intent but proves no installed guest OS.
Extend ADR0092's existing guarded VIOS mutation inventory only for this new setter.

## Consequences

Readback can prove configuration while current policy awaits a later explicit boot.
Absent policy values remain unknown. HMC decides store emptiness and whether a
shrink fits; configuration does not prove key survival or guest boot readiness.
A failed submission or verification requires inspection, with no replay or rollback.

## Considered & rejected

- **Read native field names with lssyscfg.** verified: the bounded read-only source
  pass on HMC 10.3.1060/build2408210051 returned invalid attribute `secure_boot`;
  retained BasePartition XSD and REST feeds provide the supported policy fields.
- **Write through a second REST mapping.** judgment: duplicates the existing native
  creation contract and its admission instead of extending one finite encoder.
- **Add separate consent and Linux-intent flags.** verified: the operator selected
  explicit settings plus `--yes`, and field selection as intent, for #1765.
- **Use a generic policy/command proxy.** judgment: expands authority beyond five
  fields and loses target-specific ownership and validation.
- **Leave the fields unsupported.** judgment: fails the approved C04 outcome.
