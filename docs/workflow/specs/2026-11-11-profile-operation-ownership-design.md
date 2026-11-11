# Profile operation ownership

## Problem

Profile synchronization and collective restore live in `lpar/configuration.py`,
although the existing profile package owns the related domain operations.
[ADR 0361](../../adr/0361-colocate-profile-synchronization-and-restore.md) records
this internal ownership correction under the authorized quality cleanup.

## Scope

One implementation unit moves `synchronize_lpar_profile` unchanged to existing
`profiles/sync.py` and `restore_system_lpar_profiles` to existing `profiles/artifacts.py`.
Remove the old definitions; migrate the MCP profile adapter and the direct imports,
patch targets and selector inventory in the four affected LPAR test modules.
Update ADR 0092's maintained symbol citations and its test's source inventory.
Classify the existing backup-artifact deletion as non-LPAR mutation: it deletes a
ManagedSystem backup artifact, not a live partition. This adds no authorization exemption.
No forwarding wrappers, new production modules, public signatures or policy changes.

### Failure model

- Actors/deployments: authenticated application callers and offline test callers.
- Invariants: mode validation precedes HMC access; ownership admission precedes SSH;
  preserve override, force, arguments, returns, exceptions and the six-name facade.
- Accepted classes: existing transport failures retain their current contracts;
  old internal import paths are replaced directly under repository conventions.
- Covered elsewhere: ownership helpers and MCP security policy retain their boundaries.
- Threat boundaries: none added or widened; caller-controlled selectors and restore
  paths retain the existing ownership guard, validation and SSH encoding owners.
- Untrusted actors: existing application callers; no additional trust is delegated.
- Out of scope: live hardware and hostile concurrent HMC changes retain existing owners.

## Success

The two moved functions have identical ASTs. The MCP adapter imports them from
their profile owners; the old definitions disappear. The demonstrated direct
test consumers and ADR 0092 inventory follow the new owners without weakening
behavior assertions. Fresh imports have no new runtime cycle.

## Validation

- Mode: task-test-not-applicable — pure relocation adds no behavioral contract
  that needs a manufactured failing test. Compare both function ASTs against
  `b32a5dfd`; run the affected suite with `uv run --no-sync pytest --no-cov -q` over
  `tests/lpar/test_lpar_profile.py`, `tests/lpar/test_restore_profile_ownership.py`,
  `tests/lpar/test_reconfiguration_ownership.py`, `tests/lpar/test_selector_signatures.py`,
  `tests/unit/test_adr_0092_citations.py`, `tests/unit/test_profile_sync.py`,
  `tests/lpar/test_profile_artifacts.py`, `tests/lpar/test_profile_artifact_tools.py`,
  `tests/scripts/test_live_profile_artifacts.py`, `tests/unit/test_public_api.py`.
- Mode: focused-test — the expanded source inventory must discover and classify
  backup deletion while preserving guards and maintained citations. Existing
  `tests/unit/test_adr_0092_citations.py` must fail if the new classification or
  moved citation is omitted; run that module with the same pytest options.
- Mode: focused-test — existing mode-refusal, ownership-denial, override and force
  tests must still pass at the new owners; controlled guard removal must fail.
- Mode: task-test-not-applicable — the new ADR/spec explain the ownership decision;
  their prose is not an executable contract. Run doc gates and pinned all-file hooks.
