# 0288 — Observe entitled-memory statistics around one native reset

## Status

Proposed for #1777; adopted for design review, implementation held.

## Context

D04 of #638 covers documented chhwres mem rs, separately from allocation/paging.
P10/P11 references provide one-target native grammar but no complete reset eligibility
matrix or independent modern reset oracle. The retained read shows three name=value
fields; it establishes neither complete counter inventory nor units.
The operator approved four design policies: bounded observation acceptance,
separate typed operation/CLI/MCP, bounded admission and strict statistics rows.
These explicitly replace verified-reset/completeness and complete pre-write native
eligibility guarantees; they do not claim those original guarantees were proved.

## Decision

Add LparMemoryStatisticsReset and separate reset operation/CLI/MCP presentations.
Classify the MCP reset as destructive under ADR0035: it irreversibly discards existing
counter history. A mutate-only grant cannot authorize it; all-targets remains mandatory.
Require contained authorized stable identity/current shared/Running/system AMS and
one usable before observation. HMC validates undocumented OS/release/RMC eligibility;
wait/detail are allowed only for reported AIX/Linux. Submit native mem rs once.
Reject non-finite/non-positive config.ssh_timeout before any ownership/statistics SSH.
Both new SSH entries enforce the same precondition using the existing timeout value.
Finite client timeout remains even when native wait0 disables the guest timer.
Revalidate optional wait/detail against fresh reported PartitionType before submission.
Keep native completion, raw ordered before/after fields, errors and identity facts
separate. Reset verification is unavailable for each result produced by this design.
Usable means a valid bounded row with at least one recognized numeric-text field;
unknown/missing/unavailable values are retained. Do not infer units or reset causation.
This narrowly amends ADR0092 scope for this counter-history reset on LogicalPartition
and VirtualIOServer, despite its legacy resource-change-only and closed VIOS wording;
unrelated VIOS policy is unchanged.
Retain standard audited ownership_override:bool=False on the operation/CLI/MCP.
Explicit true bypasses only the existing ownership read/check through its audit;
containment, identity, shared/Running/AMS, usable statistics and MCP grants still apply.
Reuse current authorization, audit and bounded SSH; ST67 keeps override=False.
Require existing MCP all-targets grants for this LPAR/VIOS union; no selector-kind alias. Add only explicit
completed-nonzero provenance to the existing CLI error; preserve it through redaction.
ST67 uses an already owned Running shared fixture, one intent and read-only recovery.
Counter history cannot be restored; recovery checks finite records/current environment,
never equality of counters or whether a reset occurred.

## Consequences

A zero exit plus usable observations can complete the observation workflow but cannot
verify reset. An uncertain or nonzero submit remains incomplete even if stats are read.
No retry, force, activation, conversion, allocation change or automatic reverse occurs.
Missing eligible hardware leaves live evidence unevidenced and does not waive gates.
No new dependency, stable facade export or existing allocation/parser behavior changes.

## Considered & rejected

- Extend allocation requests with rs. verified: LparMemoryChange requires quantities
  at source616a7dbf documents/lpar_memory.py:17–48; quantities misstate this operation.
- Infer reset from zero/decrease/unchanged counters. verified: approved observation
  policy and retained REDP4470 high-water semantics provide no independent causal oracle.
- Require a complete vendor eligibility matrix before design. verified: operator
  approved bounded positive admission plus HMC final undocumented eligibility.
- Add a public arbitrary statistics reader. judgment: unnecessary surface for one reset.
- Change the shared permissive parser globally. verified: ssh/commands.py:278–313
  intentionally accepts bare continuations/duplicate overwrites; reset needs strict rows.
