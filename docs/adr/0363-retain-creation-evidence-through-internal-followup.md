# ADR 0363: Retain creation evidence through internal follow-up

## Status

Accepted implementation refinement of ADRs 0348, 0360 and 0362.

## Context

Creation completion projects observations into `LparCreationResult`, sanitizes its
fields, and reconstructs observations for verification. Workflow and provisioning
both consume that public checkpoint. The approved shared coordinator already permits
private carriers using existing models and assigns public adaptation to callers.

## Decision

Keep `_CreationObservations` canonical through completion, sanitization and internal
follow-up. A private creation path returns a pair of existing result metadata and
observations. The metadata value carries no populated feature-step fields.
Workflow and provisioning consume that pair directly. Do not add a third step schema
or cache a verification decision independently of its evidence.

Keep the existing public entry points. `create_and_stamp_lpar` projects the retained
observations once before returning its existing result. `creation_followup` adapts
an externally supplied public result once, then uses the same canonical follow-up
logic. Internal creation consumers do not call either public adaptation path.

## Consequences

Public models remain in LPAR core. Feature ordering, ownership and apply gates,
partial facts, warnings, diagnostics and TLS cutoff remain unchanged. Existing
shallow payload identity is intentional. No new transport, persistence or recovery
policy follows from this internal representation change.

## Considered & rejected

- Keep the public checkpoint internally: verified: at `c7b2faa1`, creation core
  uses `with_public_steps(vars(result))`, and workflow and provisioning both call
  `creation_followup` after `create_and_stamp_lpar`.
- Add another completion dataclass or cached verification flag: judgment: the
  existing metadata and observation types already carry the required facts.
- Move public result models into the workflow contract: judgment: unnecessary for
  the canonical handoff and contrary to ADR 0348's retained public model owner.
