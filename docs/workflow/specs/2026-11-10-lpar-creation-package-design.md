# Focused LPAR creation ownership

## Problem and decision
The flat LPAR directory mixes one creation cluster with runtime ownership/configuration APIs.
Create creation/ with docstring-only initializer. Move core creation declarations to creation/core.py,
workflows.py -> creation/workflow.py, physical_io.py -> creation/physical_io.py,
start.py -> creation/start.py, serial.py -> creation/serial.py. Serial is exclusively contained
creation admission/confirmation and saved observations, used by the same LPAR/VIOS coordinator.
Keep actual delete_lpar and rename_lpar implementations in existing lpar/core.py with their real
imports only; do not move those runtime operations into creation or leave compatibility exports.
Keep genuinely shared ownership, Linux-role, assignments, configuration and memory owners in place.
Preserve the invocation-local operations/partition_creation.py owner shared with VIOS; migrate its
actual creation/start/serial imports and TYPE_CHECKING LparCreation reference to real new owners.

## Consumer closure
Update real resolved imports in production/adapters/provisioning, scripts and tests, including
relative imports and TYPE_CHECKING edges. Old core consumers must be classified by symbol:
delete/rename remain oldcore; creation types/helpers/patch sites move to creation.core. A test
using both imports separate real modules rather than keeping an alias facade. Do not rewrite
client.core, server adapter module names, operation identifiers or arbitrary strings by substring.
Inventory dynamic imports, embedded subprocess Python and selected SOURCE_PATHS explicitly.
Selected witness paths migrate only where actually selected; closure follows real imports.
Historical ADR citations remain historical; current maintained specs must not point to removed owners.

## Failure model
Required: preserve all creation submission/stamping/apply/observation ordering, missing versus
unconfirmed identity diagnostics, TLS terminality, confirmed partial evidence, profile persistence
and authorization. Delete/rename state checks, RMW translation, containment and output stay exact.
Accepted: untracked private imports and old source-bound captures become stale; no compatibility
facade or fabricated refreshed evidence. All in-repo consumers migrate together.
The move itself supplies no new live hardware evidence or capability maturity.

## Validation
Focused-test: fresh-process import of lpar.core with MetaPathFinder rejecting partition_creation
must fail before the split and pass afterward: deletion/rename no longer require creation setup.
Retain actual existing lazy/import blockers and all behavioral assertions.
Task-test-not-applicable: all other moves are raw relocation, no independent behavior change;
normalized whole-module/moved-definition AST and exact non-import body/string equality plus
existing creation/lifecycle/provisioning/native/adapter/live offline tests prove preservation.
Capture complete207 actual MCPmodels using explicit legacy policy include_arbitrary_command=True
and registered catalog, compare schema/meta exactly. Fingerprints truthfully migrate to new owners.
Run all lpar/vios creation/native/provisioning, deletion/rename/RMW/ownership, direct adapter/CLI,
affected live arm/wrapper/recovery and metadata/sourcewitness tests. Full lint/type/format/smoke,
applicable doc/capability/scenario guards and normal14hooks precede conventional commit.
Root owns final integrated full verify/pinned hooks/wholebranchreview. No external/hardware run.
