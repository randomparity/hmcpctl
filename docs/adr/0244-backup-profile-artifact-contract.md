# ADR 0244: Backup profile artifact contract

## Status

Accepted (2026-10-18), operator-approved #1726 contract; implementation pending.

## Context

#1726 owns four REST backup/list/delete/restore profile rows (78 source units).
Existing SSH backup overwrite and restore types 1–3 already cover their semantics.
A V11R2 SP1120 read-only ListBackupProfileData source job returned a lowercase
`result` JSON array containing `BackupFileName`/`BackupTime` strings and completed
successfully; its job was cleaned up. This is source evidence, not native proof.

## Decision

Parse bounded complete artifact inventories and require an exact name in a clean
pre-list before the documented delete job. Successful deletion requires fresh
same-system exact-name absence; expose present/unknown/not-checked distinctly.
Keep deletion destructive and all-targets: a basename is not a new containment
proof. Do not retry mutations automatically or promise atomicity against HMC writers.
Reuse existing backup/restore transports and audited restore admission. Add the
explicit default-false keystore flag while preserving both SSH output channels;
keep initialization/type 4 denied and account for it as restricted behavior.

## Consequences

CLI/MCP gain dedicated artifact list/delete discovery. Inventory overflow or
ambiguous identity blocks deletion rather than truncating its admission evidence.
Recovery artifacts remain the operator's responsibility; no automatic cleanup or
restore follows uncertainty. Native maturity does not advance from source capture.

## Considered & rejected

- **Duplicate backup/restore with REST wrappers.** judgment: existing equivalent
  behavior meets #1726; a second transport adds maintenance without capability.
- **Parse a guessed delimiter format.** verified: both P10/P11 list references
  specify only a Result list; V11R2 source capture instead returned a JSON array.
- **Infer narrow deletion containment from the filename.** verified: ADR 0044
  explains why an HMC path can escape a system selector; keeping all-targets
  avoids adding a containment guarantee that the available source does not prove.
- **Return opaque inventory or omit deletion readback.** judgment: weakens the
  operator-approved exact identity and postcondition contract.
