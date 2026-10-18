# Reuse XML local-name matching

## Problem
Storage insertion repeats qualified-tag equality plus local-name comparison,
although xmlutil.localname is already imported and used in this module.
Full-spec S denominator100: bounded behavior-preserving cleanup; hazards none.

## Scope
User-authorized code-quality cleanup, specifically three reviewed redundant
predicates in client/client_storage.py. Replace them with existing localname;
remove the now-unused qualified constants and nested _local helper only.
No ownership transition, helper, dependency, validation or namespace-policy change.
### Failure model
Qualified, unqualified and foreign-namespace same-local-name anchors must match
exactly as before. Missing anchors retain fallback. Only normal XML string tags
are in the parsed protocol surface; no new non-element node policy is introduced.

## Success
C1: MediaRepositories remains immediately before first PhysicalVolumes;
if absent, after first MaximumLogicalVolumes; otherwise append. Child order stays.
C2: newly created OpticalMedia remains BEFORE first RepositoryName, otherwise
appends; existing OpticalMedia lookup and all RMW behavior remain unchanged.
Use localname(child.tag) == exact existing anchor name for three predicates.
Historical comments and unrelated protocol shaping remain outside this cleanup.

## Validation
C1/C2 Mode: focused-test, actual insertion ordering in tests/unit/test_client_storage_helpers.py
and tests/storage/test_media.py across qualified/unqualified/foreign namespaces,
first-anchor selection, missing primary/fallback and no-anchor append as applicable.
This preserves behavior, so new tests can pass before refactor; prove they bite via
controlled wrong insertion index or anchor-name fault, observe red then restore.
Run uv run --no-sync pytest tests/unit/test_client_storage_helpers.py
tests/storage/test_media.py tests/storage/test_media_rmw.py -q --no-cov.
Run formatter/lint/type/all14 actual hooks. No full suite or live HMC; root final review.
