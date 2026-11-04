# Bounded hypervisor memory for the retained vNIC fixture

Design for #1856, which is bound by `WORK:SCOPE` token `q1856-c0f14bc5`. It amends
the allocation check in the
[retained-fixture design](2026-11-01-vnic-retained-fixture-design.md) and the
refusal policy recorded in [ADR 0312](../../adr/0312-vnic-policy-updates.md).
The operator approved it on 2026-11-04 as a run-exposed harness defect.

## Problem

The fixture-prepare stage checks six managed-system allocation counters against
exact deltas: assigned memory +512 MiB, hypervisor memory +48 MiB, current and
pending available memory -560 MiB, and current and pending available processor
units -0.05. The 48 MiB figure was a single observation from 2026-11-02. On
2026-11-04 the same fixed tuple on the same system moved hypervisor memory by
32 MiB and available memory by -544 MiB. Assigned memory and processor units
matched. The harness then refused after the create and before ownership
stamping. That left an unstamped inactive partition, which the cleanup stage
cannot remove. So the exact-hypervisor rule refuses a correct allocation, and the
refusal has a real cost.

## Decision

Keep the exact rules where both observations agree. Bound only the hypervisor
memory, the one counter that is not reproducible, and tie available memory to it:

| Counter | Required change |
| --- | --- |
| CurrentAssignedMemoryToPartitions | exactly +512 MiB |
| CurrentAvailableSystemProcessorUnits | exactly -0.05 |
| PendingAvailableSystemProcessorUnits | exactly -0.05 |
| MemoryUsedByHypervisor | `h`: a non-negative multiple of the baseline `CurrentLogicalMemoryBlockSize`, 0 ≤ `h` ≤ 64 MiB |
| CurrentAvailableSystemMemory | exactly -(512 + `h`) MiB |
| PendingAvailableSystemMemory | exactly -(512 + `h`) MiB |

Both observations satisfy the rule: 48 and 32 MiB, each a multiple of the 16 MiB
block. Anything else still refuses, as before. That covers a hypervisor change
that is negative, above 64 MiB or not a block multiple; available memory that
does not equal -(512 + `h`); any other counter mismatch; and a change in leaf
shape or attributes. The block size comes from the baseline. A changed block
size is foreign drift, which the existing whole-remainder equality already
refuses. A nonpositive block size is already refused before create.

The 64 MiB bound is operator policy. It is twice the smallest observation, at
four 16 MiB blocks, and it is not a claimed HMC limit. The rule still adds up
the totals: the fixture's 512 MiB plus `h` must equal exactly what left
available memory.

Stamping ownership must not move any allocation counter. The managed-system
counters in the pre-stamp and retained snapshots must be identical, both in
capture and when a later stage re-validates the preparation journal. The old
exact rule made the two snapshots agree by construction. Without this check, the
bound would accept a different `h` in each.

Check the bound before the block-multiple test. `0 ≤ h` must be explicit, because
`-16 % 16` is zero.

Pre-create eligibility uses the worst case of the bound. Before any effect, every
counter's baseline value plus its required change must stay non-negative,
evaluated with `h` = 64. Existing checks stay unchanged: the 1024 MiB available
minimum, block alignment and the per-virtual-processor minimum.

## Unchanged

Nothing else changes in the fixture tuple, packet schema, caps, effects, stamping,
cleanup, recovery, journals or the source-version-2 cases. The comparison copy
still restores each proven counter to its baseline leaf and requires the entire
remainder to equal the original baseline. Cleanup still requires the complete
original raw baseline after delete. Counter lag remains MANUAL_RECOVERY. Failed
histories are not repaired.

## Verification

Offline tests in `tests/test_live_vnic_creation_policy_arm.py` drive the existing
fixture wire. They check that:

- preparation with a 16 MiB block and `h` of 0, 32, 48 or 64 reaches PREPARED,
  and independent recovery reaches RETAINED. The same holds with a 32 MiB block
  and `h` of 32 or 64;
- each of the following refuses after the create, before stamping: `h` = 80;
  `h` = 24 (not a block multiple); `h` = -16; and `h` = 48 with a 32 MiB block;
- with a legal `h`, available memory that differs from -(512 + `h`) on either
  counter, or on both, refuses;
- a hypervisor change during stamping refuses in capture and in recovery;
- every exact counter that drifts by one still refuses;
- a baseline pending available memory of 570 MiB refuses before any effect. The
  old -560 rule admitted that value, and the 1024 MiB minimum covers only current
  available memory.

A controlled fault proves the tests bite. Restoring the exact +48 rule must fail
the 32 MiB acceptance test. Removing the bound must fail the 80 MiB refusal.

Hardware proof is a separately granted fresh fixture-prepare window at the new
head commit, with a fresh packet and preflight. That window has one
prerequisite: the operator must first remove the unstamped partition left by the
2026-11-04 attempt, which preparation's absence check would otherwise refuse.
The live block size in both observations was 16 MiB.

The dated retained-fixture design keeps its original table as history. ADR 0312
and the operator procedure in `docs/live-testing.md` carry the amended rule.

## Failure model

The rule only accepts more outcomes under the bound and the exact sum. A
mis-accepted allocation therefore differs from a correct one only in how much
hypervisor memory it used, by at most 64 MiB on an inactive fixture. No foreign
partition, profile, backing or unrelated counter can change undetected, because
the whole-remainder equality is unchanged.
