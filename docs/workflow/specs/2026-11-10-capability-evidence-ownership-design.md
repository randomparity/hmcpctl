# Capability evidence below the command

## Problem and decision
check_capability_inventory.py is both a catalog/corpus command and the shared implementation
of producer evidence rules. Four independent consumers import its schema, fingerprints and
freshness/report primitives, including private helpers. Extract existing responsibilities to
scripts/capability_evidence/ with a docstring-only initializer and four concrete owners:

| Owner | Existing responsibilities |
| --- | --- |
| schema.py | InventoryError, duplicate-key JSON loading, array/object/key validation, closed maturity/implementation/observation/confirmation schemas and their immutable grammars/version constants |
| closure.py | PACKAGE and the exact current readable-path/import-graph walk and length-prefixed path-ordered source fingerprint |
| registry.py | RegistryTool, lazy composed handler discovery, record_verified source sites, closed release routes and claims validation |
| verification.py | OperationState, age/closure freshness and gap confirmation, release snapshot loading/rendering, runtime projection rendering and existing atomic publisher; STALE_AFTER_DAYS lives here |

Expose the existing shared schema helpers under their meaningful unprefixed names where
multiple consumers actually use them; private per-owner helpers stay private. No aliases in the initializer or old CLI.
The command may import shared classes/functions only for real validation/type/catch uses.
Prefer module-qualified calls with distinct aliases so local registry/path/record variables
do not shadow owners. No new generic pipeline, injection hook, object model or framework.

## Consumers and retained command
Keep check_capability_inventory.py's ROOT/default paths, Report, corpus/source-unit extraction,
row/topic/corpus/operation catalog validation, projection/report orchestration, CLI parsing,
summary/exit behavior and existing invocation path. CLI delegates common rules to real owners.
Migrate live_test/evidence.py and runner.py, promote_live_observations.py and run_release_window.py
to those owners. Subprocess command strings still invoke the existing check command unchanged.
A product src/ module must not import this developer scripts package.
Keep FastMCP/server handler construction and live runner route/site imports inside their
existing function calls, not at new module import time. Shared schema/closure import standalone.

## Preserved contracts and failure model
Actors are offline inventory validation, live-run evidence emitters, local promotion and release
window tools. Their existing authorization/mutation boundaries remain; this refactor itself
runs no hardware, promotion or release. JSON shapes/version numbers/privacy grammars, duplicate
key refusal, strict boolean/int distinctions, byte bounds, timestamp/age checks, errors and
return/report ordering stay exact. Keep current source closure module-scope If/Try traversal,
TYPE_CHECKING coverage, package initializer inclusion, symlink exclusion, path/length hashing,
per-call fresh fingerprint cache and explicit shared _fingerprints option.
Release snapshots retain their existing no-age derivation and frozen runtime projection policy;
no observation becomes fresh or verified because a helper moved. The application source tree is
unchanged by this unit, so actual per-handler paths/fingerprints must agree before/after it.
Existing library-only module paths are pre-release internal seams; all in-repo direct callers
and patch sites migrate together without compatibility shims. Existing standalone CLI remains.

## Validation
focused-test: new behavior-grouped tests/scripts/test_capability_evidence.py imports each of the
four real consumers with a MetaPathFinder forbidding check_capability_inventory, in fresh
subprocesses. Old command coupling fails; shared imports pass with no hardware/config work.
Keep/migrate all existing schema, closure, registry, freshness, projection, producer, promotion
and release tests to their actual lookup owners. Preserve every assertion; do not patch backend
shapes or add producer getattr compatibility for old fakes.
Audit moved definition ASTs after only declared owner/name bindings; no strings/conditions/data
changes. Compare captured all207 registry/MCP schemas, all207 application closure paths/digests,
valid/invalid schema outcomes and generated runtime/release bytes. Run existing regenerators
only when their guards require them and explain any real output delta, never invent freshness.
Run tests/scripts plus all affected live runner/arm/evidence consumers; lint/types/format,
static and normal14commit hooks. Root owns final integrated full just verify and pinned prek.
