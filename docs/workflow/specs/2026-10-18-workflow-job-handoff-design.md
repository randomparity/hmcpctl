# Carry workflow job outcomes at the operation boundary
Full-spec M denominator250; user explicit approval item5, including additive job
field in JSON/MCP. Root owner. No persisted schema, authorization, dependency,
extra polling, job XML or existing raw result changes. No serialization framework.

## Architecture and scope
WorkflowStep gains job:JobOutcome|None=None after result, importing existing job
model from jobs (no app/MCP dependency). Existing positional3arguments stay valid.
Populate at actual job-producing handoffs: provision._run_power_leg and
 decommission._power_off. Reuse existing job_identifier/job_outcome for raw PowerOn
job entries; retain already-normalized outcomes without reconstruction. Preserve
result shapes: raw power job without assessment, asdict(outcome) with assessment,
error strings and decommission summary metadata. Failed/timed-out steps with known
outcome retain it; already-off steps have no job. Ordinary resource UUIDs/status
fields must never become jobs. No generic automatic dictionary normalization or
WorkflowStep.__post_init__ inference. Assignments/network return resource values,
and storage create/map return updated VolumeGroup/VIOS documents rather than jobs;
leave these nonjob steps job=None. No artificial plumbing for unreachable jobs.

CLI attach-disk consumes step.job for job_id/job_status/error instead of inspecting
Resource.JobID or recomputing outcomes. Current attach-disk storage helpers return
metadata after discarding mutation values; their old CLI Resource branch is
unreachable for today's real producers. Do not fabricate jobs from that metadata
or rewrite raw results to make a test pass. Test renderer using legitimate typed
step outcomes, and separately test real power/decommission producer handoffs.

## Failure model
Missing/ordinary/nonjob values keep job=None. Known raw JobID, UUID+job-resource or
job SELF-link entries preserve handle/status/error via existing helpers. Empty job
submission remains no handle; no guessed polling. New field is additive; existing
result payloads, status progression, errors and completion flags unchanged.
Do not reject a successful submission merely because its job has not yet completed;
current assessment/decommission completion checks retain their owners.

## Validation inventory
Mode focused-test: actual power producer without/with assessment, failure/timeout,
and decommission outcomes expose typed job while oldcode lacks field; preserve old
result projection by comparing expected behavior. Name-only metadata/resource UUID
is notjob. Existing normalized instance identity retained (no reconstruction).
Mode focused-test: CLI consumes typed step.job independent of step.result envelope,
keeps existing fields/detail/exit behavior, and JSON/MCP asdict includes normalized
job/null. Exercise actual tool serialization, not only dataclass asdict; no imports
from app in domain. Existing workflow/CLI/provision/decommission/MCP suites pass
--no-cov plus lint/format/types and actual14hooks before commit. Root final fullverify.
