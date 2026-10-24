# ADR 0279: LPAR creation memory layout

## Status

Proposed for #1767; implementation held for shared creation-interface reconciliation.

## Context

Six P10/P11 mksyscfg CREATE fields describe huge-page counts, HPT/PPT ratios and
KVM reservation. Ordinary `LparResources` is consumed by both creation and update.
Profile CRUD already owns persistence operations. The native create boundary
supports a single configuration record and subsequent independent profile readback.

## Decision

Carry one optional creation-only `LparMemoryLayout` through the existing LPAR and
VIOS creation (ratios only)
boundaries and use native creation whenever supplied. Source-unrestricted HPT/PPT
remain available to OS400 and VIOS; only huge-page counts and KVM selection enforce
their respective source-specific AIX/Linux and Linux restrictions. Preserve omission/zero, strict
count ordering and advertised ratio membership. Huge pages come from their separate
system pool; shortage is an activation failure (`docs/refs/p10jau.md:2486`), not a
source-established creation reservation or ordinary-memory arithmetic check.

KVM reservation selection declares Linux intent, including zero. Enforce finite
numbers and at most two decimal places; HMC decides undocumented numeric limits,
as directly approved by the operator on 2026-10-24. Infer no 0..100 interval or
extra KVM-enable prerequisite. Verify saved effective fields independently and
stop downstream assignments on mismatch or unavailable readback. Controller mode
creates current configuration without a named profile; retain its admitted fields
and report unavailable verification where exact current getter semantics are not
established. Never query default_profile or claim complete controller readback there.

## Consequences

Native creation cannot express explicit keylock, so the combination fails before
effects. A successfully created partition remains after later verification failure;
report its identity and incomplete workflow without retry or automatic deletion.
A profile does not reserve or prove sufficient activation capacity. Saved configuration
is not proof of guest runtime, and positive KVM behavior remains unevidenced until
an explicitly authorized suitable target runs it.

## Considered & rejected

- **Extend ordinary resources.** verified: at base `edcf06bd`, reading
  `documents/lpar.py:partition_updates` and `operations/lpar/dlpar.py` shows shared
  update consumption; judgment: a creation-only model keeps this scope explicit.
- **Use REST creation for these fields.** judgment: it would add a second transport
  contract while the approved twelve bindings describe native mksyscfg creation.
- **Fix KVM bounds at 0..100.** verified: P10 mksyscfg:41 and P11 mksyscfg:49 specify
  Linux and precision but no interval; captured KVMReservedMemory uses an unfaceted
  float. The operator approved HMC final validation rather than invented bounds.
- **Do nothing.** judgment: leaves the six requested CREATE fields unsupported.
