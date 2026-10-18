# Shared partition field normalization

## Problem
Inspection imports private parsing functions from the logical inventory workflow,
which already shares processor parsing with composite inventory.

## Scope
The operator approved consolidating these existing helpers in inventory/composite.py.
Reuse composite's identical _text_or_none as optional_text, removing logical's
duplicate _text. Move _int and _float unchanged as optional_int and optional_float. Rename composite.py's _container and
_processor_figures to resource_container and processor_figures. Migrate both
logical inventory and partition inspection to these five explicit shared names.
Remove the former definitions and imports; retain no compatibility aliases.
No output, numeric parsing, delegated authorization, or I/O behavior changes.
The stable facade is unchanged. The root agent owns this boundary-preserving scope.

### Failure model
Malformed or missing fields retain the existing None/empty-container behavior.
Processor mode still selects the same current and desired containers, including
unknown modes. Approved internal import names change; external outputs do not.
A missed consumer must fail import/type checking rather than use a second copy.

## Success
Inspection no longer imports logical inventory. All three consumers use one owner
for the five functions, with their former normalization semantics intact.

## Validation
focused-test: existing logical inventory, inspection, LPAR summary and composite
concurrency tests cover missing and malformed fields, dedicated/shared processors,
output construction and delegated authorization. Run those and their app adapters.
Compare helper ASTs modulo the approved names to prove unchanged algorithms.
task-test-not-applicable: no new behavior is introduced, so no synthetic assertions
of source spelling or duplicated parser tests. Run lint, format and type checking.
