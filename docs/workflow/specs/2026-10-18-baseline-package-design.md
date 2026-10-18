# Baseline package ownership

Full-spec M denominator250; one ownership transition, no new behavior.
User approved proposed-final-boundaries.md scope3 with exact reply "approve".

## Scope and decision
Move implementation from scripts/live_test_baseline.py into
scripts/live_test/baseline.py, including main(), TABLES and private helpers.
Existing executable imports the baseline module and dispatches baseline.main()
under __main__, following live_test_runner.py. No helper re-exports.
ADR0237 records ownership. No dependency, schema, CLI, authorization or live change.

## Success
C1: package inventory/provisioning, recovery, release-window and tests import
live_test.baseline directly for implementation; executable command strings stay.
C2: definition bodies/constants and main CLI parsing, messages and exit values
remain unchanged except necessary relative import/module docstring adjustments.
C3: private snapshot schema, strict decoding, exclusive0600 publication,
symlink refusal and temporary cleanup stay unchanged. Dry-run loads no credentials.
C4: wrapper dispatch works; no implementation helpers remain on executable module.

## Failure model
Actors/deployments: existing offline unit/script tests and operators invoking
existing baseline command, package capture and recovery consumers.
Invariants/assets: saved private snapshots, selected connection/VIOS scope,
read-only capture behavior, privacy of diagnostics, matching/drift/error outcomes.
Accepted classes: unavailable live hardware proof; this move executes no HMC run.
Other owners: transport/auth/config validation retain existing implementation.

## Migration and validation
Audit exact live_test_baseline/live_test.baseline imports in scripts and tests.
Direct function tests patch the new owning module; wrapper tests exercise actual
entrypoint dispatch, not obsolete imported aliases. Release-window stub boundary
can import package main; subprocess path remains scripts/live_test_baseline.py.
Focused structural test: package import succeeds without executable import,
wrapper has no TABLES/capture/validate attributes; old tree fails. Controlled
restoration of executable-owned dependency must fail. Existing behavior tests
retain compare0/1/unreadable2, private publication, malformed input, dry-run guards.
Run baseline/recovery/release-window/round2-recovery plus affected live consumers,
--no-cov; lint/format/types/test-layout and actual14commit hooks. Root owns final
just verify and pinned all-files prek after the three completed units.
