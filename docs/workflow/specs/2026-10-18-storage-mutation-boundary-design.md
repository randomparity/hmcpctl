# Normalize storage mutation results at the operation boundary

Scope: explicit user approval of all three next-boundary changes. Owner root.
Complexity M, denominator250, full-spec (pre-release model/interface boundary).

## Outcome and contracts
Remove transport envelope and job interpretation from CLI _print_mutation. The
existing storage operations module owns one normalized mutation representation
with raw payload and optional existing JobOutcome, preserving raw None/dict values
for details and MCP output. Preserve current outcome mapping and all job handles.
Normalize all CLI storage mutation paths consistently, including map results with
LPAR/change-location metadata; avoid a one-operation exception or adapter-side
normalization substitute. Inventory return models remain unchanged. Update direct
consumers; replace old internal return paths with no compatibility re-exports.
MCP adapters explicitly project the original raw payload, preserving schemas and
actual wire output. Existing metadata wrappers retain their wire projection.

Normalization reuses job_identifier/job_outcome and the exact existing resource/
Resource envelope behavior. Ordinary resource UUIDs alone must not become jobs.
CLI rendering retains operation fields, warnings, target, change location, details,
JSON/TSV/text, failed-job exit1 and request-succeeded verification wording.
No authorization, transport dispatch, validation, filesystem lifecycle or workflow
behavior changes. No new dependencies or persisted fields.

## Failure model
Resource vs job discrimination and incomplete/pending/error job representations
retain existing semantics. None and ordinary resource payloads remain usable.
Normalization never adds a poll/network request; operation/client errors propagate.
CLI failed jobs still render before exit1. MCP exposes original values unchanged.

## Validation inventory and execution
Read all mutation returns/callers. Test operation normalization using actual helper
logic and representative raw/enveloped/None/UUID/SELF job cases; confirm new
operation-result expectations fail before implementation. Retain meaningful CLI
summary and details tests and actual MCP raw payload tests. Migrate mocks at the
new operation boundary, without mocking the normalization under test. Run storage,
CLI storage/consent/terminal and operation selectors suites plus lint/types/format.
Review exact output/schema compatibility and commit one task. Full assembled
verification and pinned hooks remain separate.

## Implemented representation
`StorageMutationResult` retains the original details projection in `raw`, the
optional existing `JobOutcome` in `job`, and optional resolved LPAR and
change-location metadata. Map and mount retain their original three-field
metadata envelope in `raw`; detach and unmount retain the original location
projection; delete-repository retains its resolved VIOS string. MCP adapters
project their respective original values explicitly. Provisioning ignores the
create-disk operation result as before. The attach workflow retains its own
result and step semantics; its renderer supplies an uninterpreted details value.
