# 0392 — Authorize installer-media detach on the provisioned VIOS

## Status

Accepted (2026-11-17), issue #1904. Partially supersedes Decision 6 of
[ADR 0321](0321-reconfigure-detaches-ledgered-installer-media.md).

## Context

A detach request names the managed system and LPAR. Provision recorded the VIOS UUID
and medium under the owning partition's operation. Previously the unmount specialist
received no VIOS selector, so only an all-targets grant could authorize this patch.

## Decision

1. After the specialist finds a connection-eligible grant, resolve new targets on the admitted connection and host. Require exactly one provision
   owner and one usable installer-media identity. Pass the original system and LPAR
   selector strings and the ledger VIOS UUID to the existing unmount authorization gate.
   A single matching grant on that connection must cover the three selectors.
2. Record the admitted partition and medium as an applied `admitted_media` journal effect
   in the same transaction as new operation admission. Carry it into planning, reconciliation
   and execution. A recorded plan binds subsequent calls to its guarded physical target,
   including when a mixed patch has renamed the partition. Refuse conflicting evidence
   before classifications or writes. Other delegated tools keep their own checks.
3. Existing requests can return their journal or abandon without HMC access. Authorize
   using compatible local evidence from the admission effect, guard, plan and ledger.
   Conflicting evidence refuses, including on replay and abandon. When historical records
   lack VIOS evidence, an explicit all-targets unmount grant can authorize this path.
   Admission must bind it to the same operation, connection and host and preclude a worker.
   Resume that can execute still requires the provision ledger and physical binding.

## Consequences

An operator can grant detach for one VIOS UUID. A new detach now requires the partition
and ownership ledger to resolve before submission, including under an all-targets grant.
The exclusive-writer window and the optical mapping's existing safety checks still apply.

## Considered & rejected

- Retain all-targets-only detach. verified: Decision 6 of ADR 0321 establishes that this
  refuses the scoped grant required by #1904.
- Add a caller-supplied VIOS. judgment: the authority source is the provision ledger.
- Authorize a VIOS at admission and resolve the media independently in the worker.
  verified: at 157663607, reconfiguration/core.py reuses a recorded plan on resume;
  logical/engine.py reconciles open effects before calling the body.
