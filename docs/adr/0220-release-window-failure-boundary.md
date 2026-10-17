# ADR 0220: Bound release-window execution and failure

## Status

Accepted (2026-10-14), implementing #1513 under ADR 0212.

## Context

The release window composes existing commands that mutate hardware. Named arm wrappers
write fixed result paths and recovery consumes those results. Promotion requires all
arms completed and clean recovery, and already owns evidence validation and its commit.

## Decision

Use one local sequential orchestrator and an explicit candidate-bound authorization
file containing the derived arm list, configured mutation descriptions and conditional
VIOS grant. Dry-run prints the plan and an unapproved template without HMC access.
Keep named wrappers and their existing recovery interface. Refuse pre-existing outputs
that could be mistaken for this attempt or restored by the runner. Persist private logs,
baselines, evidence and a phase summary outside the checkout; raw runner files remain
ignored at their existing paths. No automatic attempt resume or rollback is introduced.

After attempting an arm, await its child termination before recovery, including on
catchable interruption. An unclean recovery or failed phase stops the window. Only a
complete successful window reaches the existing promoter and claimed-set gate. The
original branch is never committed, pushed or tagged by this command.

Round2 receives a fresh producer baseline and independent read-only recovery witness.
ST14 provisions and removes fresh owned temporary resources, preserving the configured
baseline partition and disk. The outer baseline comparison stays strict, including UUIDs.
This producer repair follows the approved scope expansion; historical claim routes remain.

## Consequences

Operators explicitly review each attempt's authorization and retain partial artifacts
for manual inspection after failure. Catchable signals get a recovery opportunity;
SIGKILL and host loss cannot promise cleanup. Promotion retains its own offline checks.

## Considered & rejected

- **Keep ST14 replacing the baseline partition.** verified: at ff9b020,
  scripts/live_test/provisioning.py deletes it; operations/lpar/provision.py creates a
  new UUID; scripts/live_test_baseline.py preserves mapping client identity. A new UUID
  cannot equal the original snapshot. Preserving the original avoids unsupported restore.
- **Drop identity from baseline comparison or narrow round2.** judgment: contradicts the
  approved strict comparison and complete retained claim behavior.
- **Do nothing.** verified: recovery's witnessed set at ff9b020 omits eleven actual round2
  tasks; its main returns 2 for their dispatch even with no residue findings.
- **Reimplement baseline or promotion logic.** judgment: duplicates existing owners
  and their validation contracts.
- **Retry or resume automatically.** judgment: ambiguous partial hardware mutations need
  operator inspection, not another dispatch inferred from an incomplete attempt.
- **Use an unqualified yes flag.** judgment: it does not name the required arm and
  mutation boundary, or bind that approval to the candidate being exercised.
