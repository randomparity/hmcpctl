# ADR 0255: LPAR IBM i and reporting configuration

## Status

Accepted for issue #1763 under the approved C02 scope.

## Context

The sixteen POWER10/POWER11 source bindings distinguish partition creation
from update, and IBM i-only fields from common reporting fields. C01 settings
already have an independent typed model and verified outcome handling.

## Decision

Keep C01 unchanged. Add a typed optional reporting-settings model at the same
creation/configuration boundaries and use fixed documented SSH records.
IPL source is update-only; redundant error-path reporting is creation-only.
Require IBM i for IPL, restricted I/O and electronic reporting. Work-group ID
accepts a strict integer or the documented `none` sentinel without an invented
numeric range; the HMC decides unsupported IDs. None means omission.

Creation uses mksyscfg for reporting settings. Reject an explicit keylock plus
reporting settings before effects: this command has no documented keylock field,
and a creation-only field cannot be repaired after REST creation. Preserve the
existing C01-only REST/keylock route. Read supplied creation settings separately;
update reads applicable baseline settings, authorizes, writes once and compares
readback. Unavailable readback and uncertain writes remain explicit outcomes.

## Consequences

Reported configuration is not proof of guest IPL, OS behavior or dynamic effect.
The cited sections provide no universal firmware floor or restart rule. HMC
admission remains final. Explicit keylock/reporting combinations are refused;
callers can omit keylock or reporting settings, never silently lose either.
Maturity remains unevidenced until commit-bound live execution.

## Considered & rejected

- **Merge into C01's model.** judgment: alters an established fixed five-field
  reader and makes IBM i applicability implicit in unrelated settings.
- **Guess REST mappings.** verified: the cited command sections establish CLI
  field/context grammar, not writable REST mappings.
- **Post-create every field.** verified: #1763 allocates redundant reporting
  only to creation, and IPL only to update.
- **Do nothing.** verified: `rg` over the five exact names at merged core
  1373d713 establishes no corresponding setters.
