# 0347 — Live-test arms have a distinct package

## Status

Accepted (2026-11-10), operator approval of the explicit 51-module move map.

## Context

Runner, configuration, observation and evidence infrastructure share a directory with
51 scenario implementations. Their relative imports, source attestations and the scenario
scanner currently assume that flat layout.

## Decision

Move the mapped scenario implementations into scripts/live_test/arms/ with a docstring-only
initializer. Retain the 15 generic infrastructure modules, recovery/ and scenario data at
their current paths. Existing wrappers remain the operator entry points.
Migrate direct imports and source paths without compatibility re-exports or aliases.
Keep scenario IDs, arguments, physical limits, safety/cleanup and result behavior unchanged.
Teach the existing scanner to discover root and arm modules and resolve their real imports
to its existing basename graph identities. Do not add source exclusions.
Fresh attested captures bind the new owner paths and commit; stale captures remain refused.

## Consequences

Internal script-library module paths change; all repository consumers move together.
Infrastructure and arm responsibilities are visible in the directory tree.
Package discovery, attestation and embedded program fixtures must be validated as one change.
No new application feature, dependency, framework or hardware maturity is introduced.

## Considered & rejected

- Keep the flat mixed directory. judgment: hides ownership between infrastructure and arms.
- Preserve old forwarding modules. judgment: duplicates pre-release internal entry points
  without an established compatibility contract.
- Recursively scan every script directory. judgment: would broaden recovery/helper analysis
  beyond the existing scenario set rather than preserve its coverage.
