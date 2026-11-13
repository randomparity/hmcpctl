# Actionable job-status classification design

Issue: #408  
Decision: [ADR 0081](../../adr/0081-classify-actionable-job-outcomes.md)

## Outcome

Classify every accepted terminal job status as either successful or actionable. Canceled and
warning jobs must have a non-empty `JobOutcome.error` and must appear in fleet health's existing
`failed_jobs` snapshot.

## Design

`TERMINAL_JOB_STATUSES` remains the polling boundary and retains the established undocumented
compatibility values. `SUCCESSFUL_JOB_STATUSES` contains only `COMPLETED` and `COMPLETED_OK`.
`FAILED_JOB_STATUSES` is derived as the terminal-set difference so no terminal status can fall
through both sets. This existing name and the existing fleet-health bucket are retained to avoid
changing public shapes; their meaning is terminal outcomes that require attention.

`job_outcome` continues to prefer HMC detail extracted by `_job_error`. If an actionable status
has no detail, it returns a deterministic status-specific fallback instead of `None`. Unknown and
non-terminal statuses remain timed out and error-free.

No retryable field is added. `FAILED_BEFORE_COMPLETION_RETRY` remains distinguishable through its
status, which is already public.

## Compatibility

`COMPLETED`, `EXCEPTION`, and `FAILED` remain tolerated because repository history, existing tests,
and accepted ADR 0018 demonstrate compatibility use. No new status spelling is accepted.

## Acceptance tests

- The terminal set is exactly and disjointly partitioned by successful and actionable sets.
- Every actionable status produces `timed_out=False` and a non-empty error without HMC detail.
- HMC-provided detail still wins over the fallback.
- Successful, running, and unknown statuses remain non-errors with their existing timeout rules.
- Fleet health reports every actionable status and excludes success, running, and unknown status.

