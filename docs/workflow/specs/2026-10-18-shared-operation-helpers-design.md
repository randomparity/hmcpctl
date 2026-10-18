# Make shared operation helpers explicit interfaces

Charter: interactive; owner root; user explicitly approved all three corrections
in the final-contracts question. Scope is four helper renames in their existing
modules, with all consumers migrated and no aliases. S100/full-spec (module API).

Rename _volume_group -> volume_group, _storage_mapping -> storage_mapping,
_require_allowlisted_iso_url -> require_allowlisted_iso_url in storage.resources;
_check_vlan_exists -> check_vlan_exists in lpar.provision. Planning and live
consumers import these explicit names. Keep class/models, algorithms, module
ownership, URL allowlist policy, VLAN admission and error behavior unchanged.
No new abstraction, move, facade export, dependency or persisted-data change.

Failure model: malformed input, unauthorized URL and absent VLAN must retain the
same refusal before writes. Pure spelling migration cannot alter those branches.
Verification: existing storage/planning/provision/ISO/live consumers plus exact
renamed definition AST comparison (ignoring names and necessary same-call renames).
No new mirrored unit tests for a reversible naming-only change; existing real
behavior/guards and import/type checks prove migration. Run lint/types/format and
relevant focused tests --no-cov; one conventional commit with this spec.
