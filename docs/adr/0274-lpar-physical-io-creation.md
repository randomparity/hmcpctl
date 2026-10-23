# ADR 0274: Typed physical and tagged I/O at partition creation

## Status

Accepted (2026-10-22), subject to implementation verification.

## Context

#1773 binds eight native creation attributes. Existing dedicated/SR-IOV workflows
assign after creation and cannot prove initial tagged relationships. The operator
approved retaining positive virtual-slot forms without adapter linkage for #638.

## Decision

Extend the existing creation owner with typed C12 fields and reuse direct-SR-IOV
assignment inputs for creation-time declarations. Select documented native creation
when requested, validate resource relationships before effects, and compare the
created profile afterwards through the existing contained REST profile reader,
using retained schema types rather than widening ADR 0165 CLI projections. Gate
profile application and provisioning on successful comparison. Keep configured-profile proof distinct from application,
activation and guest behavior. No implicit console default is supplied.

Partial or unknown creation leaves resources in place and stops later steps;
operators inspect and explicitly recover. No automatic rollback or retry. The three
unresolved virtual-slot forms fail before effects, with their #638 gap disclosed.

## Consequences

Creation may make additional scoped inventory reads. Contention cannot be locked
against external writers; post-read detects observable mismatches without claiming
transactionality. Hardware evidence remains independent from implementation.
The existing stable facade and profile lifecycle ownership do not change.

## Considered & rejected

- **Configure tags after creation.** verified: P10 mksyscfg:41 and P11:49 define
  initial tagged attributes; existing workflows.py creates before applying PCIe
  assignments. Post-create assignments cannot establish initial native records.
- **Pass arbitrary adapter records.** judgment: a new unbounded input surface would
  bypass typed relationships and increase command-injection risk.
- **Default IBM i console to hmc.** judgment: hides an operator device choice; the
  approved scope requires explicit console selection.
- **Do nothing.** verified: issue #1773 documents missing typed creation fields;
  source inspection at 1373d713 confirms native creation serializes none of them.
