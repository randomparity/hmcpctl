# Storage presentation boundary

## Authority and outcome

The operator explicitly approved the complete storage boundary refactor on
2026-11-11. [ADR 0364](../../adr/0364-project-storage-details-at-presentation-boundaries.md)
records the selected owner boundary. One coupled Cast unit migrates four producers,
MCP map, the fourteen CLI printer callers and their direct fixtures together.

## Ownership and data flow

In operations/storage/resources.py, map_storage normalizes the actual provider
entry with typed lpar_uuid and ChangeLocation. mount_optical_media keeps its existing
entry["Resource"] extraction (including KeyError for malformed non-None entries),
then normalizes that body with the known identity/location. No application envelope
is assembled by these producers. Detach and unmount directly construct
StorageMutationResult(None, change_location=location); job/resource remain None.

StorageMutationResult.from_raw recognizes provider Resource wrappers, not lowercase
application resource envelopes. Preserve scalar/None behavior, ordinary-resource
UUID non-job discrimination, JobID/status/entry UUID and job_outcome behavior.
Optical load/unload/upload and other mutation raw contracts remain unchanged.

MCP map constructs lpar_uuid/resource/change_location, retaining provider entry
nesting under resource. MCP mount still flattens typed resource beside location;
MCP detach/unmount keep their current mapping/string. CLI map/mount build their
previous three-field envelopes using raw provider diagnostics. CLI detach/unmount
print asdict(location) in details mode. Four producers guarantee location, narrowed
by an internal assertion in these adapter paths, as already used by MCP consumers.

The existing CLI printer takes summary, selected, details, keyword-only
 details_payload, job=None and location=None. It preserves job handling and exit1
for failed jobs without accessing a mutation DTO. Migrate fourteen callers, passing
raw/job/location explicitly; attach supplies its existing asdict result without
fabricating a StorageMutationResult. Do not add a serializer, generic wrapper,
new result field, subclass, RPC, dependency or compatibility fallback.
Update the earlier storage mutation design's raw description to reference this
approved four-operation exception; the remaining diagnostic contracts stay intact.

## Success

For these four operations, no known identity/location is serialized for decoding.
Exact MCP values and schemas, CLI summaries/details/exit status, six-name facade,
authorization, request ordering/count and existing provider error behavior remain.
The lowercase field in an ordinary provider object is retained, not stripped.

## Failure model

- Actors/deployments: existing CLI, MCP and internal Python operation callers;
  supported provider replies and offline fake-HMC/PTY tests.
- Invariants/assets: typed location, truthful resource/job classification, exact
  adapter payload nesting, job outcome/error exits and effect/request ordering.
- Accepted classes: existing ambiguous provider outcomes and interrupted remote
  effects retain current behavior; this internal carrier refactor adds no retry,
  rollback or hardware maturity guarantee. Shallow diagnostic payload aliasing stays.
- Covered elsewhere: transport/TLS, ownership and provider document validation keep
  their existing owners; adapters do not admit new remote operations or inputs.

## Validation

The ignored Cast plan maps producer, normalizer and printer contracts to focused
red/green observations. Test ordinary/wrapped/None/job replies, location-only returns,
malformed mount entry, independent typed facts and exact MCP/CLI output. Migrate
in-process and PTY fakes to actual new raw meanings. Preserve job failure/timeout
exits and request-order assertions. Compare 207 actual MCP wire schemas and facade.
Run affected storage/CLI suites, static checks and pinned all-file/commit hooks.
Use controlled output/job routing faults to prove tests bite; restore exact bytes.
Final exact-HEAD whole-branch verification and bounded whole-delta review are separate.
