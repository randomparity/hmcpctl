# Put live runner implementation in its owning package

Scope: explicit user approval of all three next-boundary changes. Owner root.
Complexity L, denominator1000, full-spec (architecture/live mutation boundary).

## Outcome and contracts
Move scripts/live_test_runner.py implementation and RunState into existing package
scripts/live_test/runner.py. Preserve the documented executable at its original
path as a thin wrapper importing the owning runner and calling its argument entry.
Migrate scenario TYPE_CHECKING references, named arm wrappers, preflight, recovery,
evidence and repository tests to the owner. Do not keep broad compatibility
re-exports or sys.modules aliases. Keep executable documentation/usage truthful.

Move existing definitions unchanged wherever possible. Account for imports of
sibling scripts, module inspection/AST inventories and cloned fixture trees.
The owning module must preserve its own stable package import, and executable
wrappers must work with scripts on sys.path as documented. No sys.path addition
that changes dependency precedence beyond existing executable bootstrap.
Keep configuration precedence/default dotenv path, schema validation, redaction,
private result destinations, provenance, client lifecycle, dispatch, recovery,
result serialization and exit behavior unchanged. No new hardware operations.
The existing one-test-module-per-executable guard remains satisfied.

## Failure model
Malformed arguments/config, unsafe result paths and incomplete provenance retain
existing pre-bootstrap refusal. Exception and cancellation cleanup preserve the
same owners. Script-relative paths must continue resolving the same files after
the move; module discovery must not omit scenarios or duplicate runner dispatch.
Importing config/artifact helpers still requires neither runner nor MCP.

## Validation inventory and execution
Inventory all live_test_runner references and path-dependent operations first.
Preserve ASTs of moved definitions except explicitly required import/path changes.
Move implementation tests to import the owner; keep real executable --help/invalid
argument/unsafe-destination tests proving wrapper delegation before bootstrap.
Add only missing meaningful wrapper/owner import behavior tests, red before move.
Run all offline live runner/arm/preflight/recovery/evidence/release-window tests,
script layout/vocabulary/scenario guards, lint/types/format; update clone fixtures
with exact package dependencies. Commit one task. No actual live run is authorized
by this refactor; full local verification and pinned hooks run assembled afterward.
