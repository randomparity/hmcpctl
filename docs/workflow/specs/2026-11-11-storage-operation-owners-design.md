# Storage operation ownership

## Authority and problem

The operator approved the prepared storage module split, separately from the
completed presentation boundary. [ADR 0368](../../adr/0368-separate-storage-optical-and-iso-transfer-owners.md)
records this owner choice. A frozen 44-definition inventory bounds the move.

## Design and transition

Keep17definitions in storage.resources: disk/volume-group/mapping operations,
StorageMutationResult and shared provider projections. Move18definitions to
storage.optical for repository/media/native device workflows, including
StorageOpticalResult and OpticalMedia. Move9definitions to storage.iso_upload
for URL admission, HTTP download, file/chunk/hash and HMC web-file upload,
including StorageUploadResult. Move their configuration constants too.

Both new modules depend on core's existing result and volume-group resolver.
Optical reuses _resource, _required_text and _optional_number directly. Core
imports neither new owner; optical imports no ISO owner. No generic layer,
compatibility re-export or initializer import is added. Preserve the existing
ISO logger name as an explicit literal. Migrate seven production consumer files
and actual affected test imports/patch targets. Update only current source
citations in ADR0092's maintained inventory and its executable path list;
policy and historical Context remain intact.

## Success

The44definition bodies/signatures remain AST-identical. Three moved dataclass
module paths change as approved, while fields/serialized shapes and shared base
identity remain. The six-name facade,207MCPschemas and CLI output remain unchanged.
Moved functions preserve request/auth order, URL policy/bounds, TLS cutoffs,
errors, cleanup and no-replay behavior. The new dependency direction is enforced.

## Failure model

- Actors/deployments: configured Python, CLI and MCP callers using existing
  provider/HMC and HTTP responses on supported Python versions; offline fakes
  and the existing localhost download fixture exercise these boundaries.
- Assets/invariants: ownership admission, truthful raw/job/location facts,
  bounded allowlisted transfer and fatal identity cleanup behavior, exact output.
- Accepted: old pre-release module imports/monkeypatch paths are replaced without
  aliases. Existing ambiguous remote effects, shallow diagnostic payload aliasing
  and uncertain mount jobs retain current semantics; no rollback/replay promise.
- Covered elsewhere: client/provider validation, presentation serialization,
  transport identity policy and real hardware qualification keep their owners;
  affected tests verify this move does not change their crossings.

## Validation

A subprocess import blocker proves one-way ownership and shared result bases;
red before the move, green after. Existing transport-level storage, HTTP upload,
optical SSH, authorization, profile-sync, CLI/MCP and provisioning tests retain
behavior assertions with direct new patch owners. Compare44ASTs and207schemas.
Inject reverse-import and URL-admission/TLS-cleanup faults into actual new owners,
observe targeted failures, restore bytes and pass. Full final verification and
bounded whole-delta review remain caller-owned after this task.
