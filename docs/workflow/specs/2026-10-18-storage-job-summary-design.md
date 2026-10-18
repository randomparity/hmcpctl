# Preserve storage mutation job handles

## Problem
Storage CLI summaries require Resource.JobID and rebuild a partial entry, losing
UUID/SELF-link fallback identities and the polling link already normalized by jobs.

## Scope
The operator approved complete-entry normalization and adding job_href to mutation
summaries on 2026-10-18. Change _print_mutation in cli_commands/storage/resources.py.
Unwrap the existing resource envelope, retain entry metadata, and use job_identifier
and job_outcome for jobs. A bare JobID/Status payload remains supported by wrapping
it as Resource; a storage resource with only UUID must not become a submitted job.
Identify job-shaped resources by JobID or Status before applying fallback identifiers.
No operation submissions, normalizer semantics, raw --details output or exit rules change.

## Success
JobID, entry UUID and SELF-link job identities use the existing normalizer. Complete
entries retain canonical job_href in JSON and TSV summaries. Ordinary UUID-bearing
storage resources still report request_succeeded with no invented polling handle.
Existing completed/failed/submitted classifications and error exit status are preserved.

## Failure model
- Actors/deployments: local storage CLI users, JSON and TSV consumers, Python3.11–3.14.
- Invariants/assets: trustworthy job identity/status, diagnostic errors, unchanged writes.
- Accepted failures: no new job inference without JobID or Status; no new interpretation
  of malformed remote entries. Existing normalizer and request-path guards remain owners.
- Guardrails: parameterized CLI boundary tests for fallback identities, envelope forms,
  canonical links and ordinary UUID resources; existing status and details tests.

## Validation
Focused-test: tests/app/test_cli_storage_results.py covers JobID/UUID/link-only entry
identities and JSON/TSV. New tests initially fail missing job_href/fallback summaries.
Run that module plus tests/unit/test_job_lifecycle.py --no-cov, lint, format and typecheck.
