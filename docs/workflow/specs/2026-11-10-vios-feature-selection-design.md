# VIOS feature selection from the shared coordinator

## Problem
VIOS keeps eight requested-feature flags independently from the coordinator,
then repeats subtly different combinations for routing and error labels.

## Scope
Keep local type checks, explicit start/layout/policy validation, shared-memory
resource checks, update-weight/serial/affinity validation and hardware boolean
validation at their existing points. Request-pair enumeration on the validated
frozen document models is selection, not input validation.
Construct the existing coordinator after local validation and before route
selection, with the selector as provisional system UUID/name as LPAR already
does. Bind completed resources and resolved UUID before any coordinator I/O.
Read feature selection from its existing properties for route, options and
submission classification; remove the separately computed boolean inventory.
Name strict CLI-name selection (layout/serial/memory/start) and native resource
completion (that subset plus hardware/policy). vTPM also requires completed
resources but retains its legacy label. Affinity requires strict identity, not
resource completion or strict CLI name by itself. Policy requires strict identity.
Keep original request/confirmation/admission order, REST fallback, owner lookup,
arguments, readback and the existing try/except/else exception-chain policy.
No coordinator field, public contract or model change; only VIOS core and this spec.

### Failure model
- Actors and deployments: existing CLI/MCP/domain VIOS callers and offline CI.
- Invariants and assets: one creation attempt, exact resource/identity gates,
  fatal TLS behavior, original native versus legacy diagnostics and partial facts.
- Accepted failure classes: no new hardware proof for an offline refactor.
- Covered elsewhere: frozen document validation and coordinator admission remain
  existing owners; no auth or external I/O boundary is widened.

## Success
One feature-selection owner; exact old routing truth table and diagnostics for
supported typed VIOS inputs, including explicit false/zero and omitted values.
The coordinator sees the same bound resources/system before first coordinator I/O.

## Validation
- task-test-not-applicable: no new behavior or independent structure contract;
  existing real workflow regressions protect admitted/refused routes and outcomes.
- Compare old/new predicates over all256 feature combinations; compare original
  fallback, validation order and native submission traces across typed selections
  and representative multi-invalid inputs using existing external mocks.
- Run affected VIOS, creation-memory, serial/start/hardware/affinity and CLI/MCP
  tests, lint/types/format and normal hooks; final assembled verification remains.
