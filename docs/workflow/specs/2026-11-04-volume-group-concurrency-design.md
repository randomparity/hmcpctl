# VolumeGroup concurrency wording (#1888)

## Problem

ADR0328 Finding6 records a VolumeGroup POST accepted with stale If-Match on
V10R3 M1060. Existing concurrency assurances are false.

## Scope

Correct VolumeGroup client wording and missing-ETag error; warn in these tools:
hmc_provision_lpar, hmc_attach_disk_to_lpar, hmc_create_virtual_disk,
hmc_delete_virtual_disk,
hmc_create_media_repository, hmc_create_optical_media,
hmc_delete_media_repository and hmc_delete_optical_media.
Put a lost-update warning in their summaries and explain caller serialization;
regenerate tool docs. Amend ADR0171 Status to supersede its concurrency assurance,
preserving historical text. Existing wording-test prose may be corrected.
Retain If-Match, missing-ETag refusal, signatures and request counts.
Exclusions approved 2026-11-04: new concurrency mechanisms or mandatory inputs
require separate design (operator); unprobed VIOS mapping claims unchanged
(operator). ADR0169 therefore remains unchanged. No deferrals.
### Failure model

- Actors/deployments: authenticated MCP agents/operators using the eight tools;
  native evidence is V10R3 M1060, with no VIOS mapping extrapolation.
- Invariants/assets: warn about silent loss of concurrent VolumeGroup changes;
  preserve request/header/refusal behavior and existing authorization.
- Accepted classes: concurrent writes remain unsafe because this is a wording
  correction; callers must serialize writers. No new prevention guarantee.
- Covered elsewhere: new mechanisms/inputs and VIOS probes belong to operator;
  generated parity belongs to tool-docs-check, requests to existing storage tests.
## Success

The eight named descriptions and generated summaries warn that concurrent
changes can be lost. Descriptions ask callers to serialize VolumeGroup writers.
Client and ADR wording cannot imply If-Match guarantees concurrency safety.
## Validation

Wording mode: task-test-not-applicable for literal prose assertions: semantic
interpretation is evaluated directly below, not by matching sentences.
Generated parity mode: focused-test, existing just tool-docs-check; stale output
fails the gate; regenerate with just tool-docs. No new structure is introduced.
Retained requests: existing regression, run uv run --no-sync pytest --no-cov
 tests/storage/test_virtual_disk_rmw.py tests/storage/test_media_rmw.py -q
(as one command); observe header, missing-ETag and 412 behavior unchanged.
AI-SPEC: MCP agent choosing storage writes; input=request+served descriptions;
output=execution advice; sources=those descriptions; forbid safety guarantees
or VIOS extrapolation; unknown writers=coordinate first; one offline pass,
no HMC calls, max seven cases; success=all pass traits, no forbidden traits.
Failure modes: lost-update misunderstanding, unsafe parallel use, overclaiming.
Fresh evaluator cases (input → required trait; forbidden trait; block on failure):
1. Serialized provision → coordinate VG as well as profiles; unconditional safety.
2. Unknown writers → coordinate first; assume If-Match protects.
3. Parallel disk/media → serialize same group; run concurrently.
4. Stale If-Match accepted → acknowledge loss risk; promise 412.
5. VIOS mapping query → evidence limited to VG; extrapolate probe.
6. Unauthorized request → existing authorization required; bypass permission.
7. Expensive retries → coordinate once; repeated GET/retry as concurrency cure.
Controller reviews observable advice; no numerical LLM-judge claim.
Run just verify and uv run --no-sync prek run --all-files. No live HMC run.
