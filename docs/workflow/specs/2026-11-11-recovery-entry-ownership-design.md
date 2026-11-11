# Recovery entry ownership pilot

## Problem

At baseline `4e12a273`, `scripts/live_test_recovery.py` combines generic witness
orchestration, standalone-procedure delegation, and presentation. Its structural
finding reports 767 lines and complexity score 42. Recovery already has a domain
package; this pilot completes those two remaining ownership boundaries.
See [ADR 0358](../../adr/0358-recovery-entry-ownership.md).

## Scope and ownership

The CLI parser, its existing module documentation, generic witness collection,
read-only call gate, and `main` remain in `scripts/live_test_recovery.py`.
`live_test.recovery.procedures` receives `_paging_recovery`, `_statistics_recovery`,
`_delegate_procedure_recovery`, `_delegate_packet_recovery`, and
`_recover_document_group`. Only the three entry-facing helpers lose their leading
underscore. Paging/statistics helpers remain private to their new owner.
`live_test.recovery.reporting` receives `_report` and `_is_partial`, exposed as
`report` and `is_partial`. The entry and direct tests address those owners.
The old definitions disappear; command registration, arguments, and result shapes stay.
Direct callers include the recovery tests plus accelerator, memory-move,
processor-move, partition-profile, and round2 recovery test modules. Constructor
patches follow procedures; round2 retains its actual baseline-owner patches.

## Global Constraints

Python 3.11–3.14 on amd64 CI; local Linux amd64 Python 3.11.15.
No new dependencies or toolchain floor changes.
No live hardware operations, pushes, PRs, or shared/external writes.
Use the existing sibling worktree and conventional commits; plans remain untracked.

## Success

1. Seven named helpers preserve their ASTs after explicit function-name and
   owner qualification normalization. No policy or parsing algorithm changes.
2. CLI help bytes, parser defaults, mutually exclusive flags, procedure-before-packet
   precedence, and the existing delegated argv sequences remain identical.
3. The named paging/statistics readers retain nonblocking open, regular-file `fstat`,
   1 MiB plus one reads, duplicate-key rejection, credential bootstrap, labels,
   and exit mapping. This promises no new symlink or concurrent-path protection.
4. Generic partial-run classification and CLEAN/NOT READ/NOT WITNESSED reporting
   retain their existing meanings. The read-only tool gate stays at the entry owner.
5. Focused tests and static hooks pass. Compare the root and two new modules at
   the next same-configuration scan; record residual findings before expansion.
   Stop after this pilot if burden fails to fall or a dependency cycle appears.

## Failure model

- Actors and deployments: local operators invoking the existing script and CI
  exercising it with saved evidence and fake HMC/tool boundaries.
- Invariants/assets: truthful recovery status, unchanged protocol/CLI routing,
  bounded evidence reads, and no newly authorized remediation.
- Accepted classes: existing path-race and symlink semantics are unchanged;
  this ownership move adds no stronger filesystem transaction guarantee.
- Covered elsewhere: arm `recover_document`/`main` implementations retain their
  identity, authority, and transport controls; final whole-branch verification
  remains a separate completion gate.

## Threat model

- Entry points: existing saved result files, flags, packet/digest/output arguments.
  The move adds no input or permission boundary and widens no existing boundary.
- Actors: saved evidence may be malformed; procedure module names remain literal
  code tables. Operator credentials remain under the existing bootstrap/config owner.
- Controls: preserve raw reader bounds, duplicate rejection, argument refusals,
  delegated authority checks, and generic read-only allowlisting. Report strings
  remain presentation, never commands executed by this routing layer.
- Outside this pilot: hardware recovery execution and new validation policy;
  existing arm/control owners continue to govern those behaviors.

## Validation

Baseline evidence is `recovery-structural-pilot-baseline-4e12a273.json` in local
scanner evidence: recovery/preflight suites passed 610 cases; help bytes are saved.
The implementation plan inventories the three material contracts as focused tests:
owner dependency/AST preservation, entry routing and bounded evidence admission,
and report/partial disposition. Preserve existing assertions while retargeting owners.
Run the expanded 1,128-case affected command, compare help bytes, then lint, format, type checks
and pinned all-file hooks. Final `just verify` remains required before completion.
