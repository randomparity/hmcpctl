# ADR 0276: Advertised partition vTPM configuration

## Status

Accepted (2026-10-23) — issue #1764; operator-approved interfaces, bounded design review and scope audit complete.

## Context

Three documented partition fields apply to AIX/Linux and VIOS. Supported versions
and encryption levels vary by system; an encryption replacement can destroy TPM
objects. LogicalPartition and VirtualIOServer have different existing creation and
authorization owners. Profile changes belong to #1724.

## Decision

Use a strict three-field object, runtime system advertisements and fixed SSH
records through those existing owners. Use separate target-typed presentations.
Extend ADR0092 ownership guarding to the new VIOS vTPM update branch, using the
existing contained-VIOS authorization helper; unrelated VIOS operations retain
their current rule.
Require inactive updates and explicit data-loss acknowledgement before disabling
an enabled instance. Refuse version/encryption replacement while enabled; never
synthesize disable/configure/re-enable, power, profile or trusted-key operations.
Read back once; retain omission preservation and uncertain outcome distinctions.

## Consequences

Callers control destructive transitions explicitly. Systems with missing or
unrecognized advertisement/compatibility fail before mutation. Configuration proof
is not guest Trusted Boot, TPM-object preservation or migration proof. A native
creation can succeed while verification fails; callers inspect instead of retrying.

## Considered & rejected

- **Hardcode versions by POWER generation.** verified: local P10/P11 `mksyscfg`
  and `chsyscfg` references direct consumers to the selected system's advertised
  lists; read-only HMC 10.3.1060 source capture at `3ad18376` returned `1.2,null`.
- **Automatically reset the device to change encryption.** verified: IBM PowerVM's
  published encryption-level explanation documents powered-off replacement and
  possible key/data loss; the operator approved explicit separate transitions.
- **One generic LPAR/VIOS presentation.** judgment: separate existing target owners
  and grants are clearer than changing the meaning of a partition selector.
- **Leave vTPM unsupported.** judgment: fails the approved three-field contract.
