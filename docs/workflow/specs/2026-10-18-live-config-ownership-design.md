# Live-test configuration ownership

## Problem
LiveTestConfig, LiveTestArtifacts and saved-document decoding are defined by the
executable runner but consumed by independent preflight and recovery commands.
The operator approved moving these contracts into scripts/live_test/config.py.

## Scope
Move both dataclasses, their validation constants, and saved config/artifact
decoders to live_test/config.py. Name the shared decoders decode_saved_config
and decode_artifacts. Preserve fields, defaults, validation, exceptions, legacy
saved-document compatibility and copied mutable values exactly. Configuration
still reads the authoritative local .env, never ambient scenario exports. Move its
default path to config.ENV_FILE; runner bootstrap/provenance share that same
constant, and tests patch its new owner. Move ENVIRONMENT_PREFIX with parsing.
Runner, preflight, recovery, release-window orchestration, scenario annotations
and tests import these contracts from their new owner, without compatibility
reexports. Runner bootstrap and execution remain in place; independent commands
still use its actual execution composition where needed. Do not widen this move
to RunState, credentials, dispatch groups or server lifecycle.
No new dependency, live HMC call, schema migration or stable-facade change.

## Success
The configuration module imports and decodes with live_test_runner and FastMCP
blocked. Both model definitions and decoder ASTs match their predecessors modulo
approved names/constants. Current and legacy recovery documents still load;
malformed data, unknown fields and unsafe config retain their existing refusal.

## Failure model
Actors: offline config consumers and operator-run CLI entry points; Python3.11–3.14.
Assets: correct target selection and complete recovery state. Preserve field names,
no ambient redirection, mutation isolation, and fail-closed decoding. Accepted
failure: approved old internal decoder/import paths change without aliases.
An unresolved caller must fail import/type checks. No hardware proof is claimed.

## Validation
focused-test: add a fresh-process blocked-import regression for config models and
JSON roundtrips, prove it red before the move and green afterward. Existing runner,
preflight, recovery, release-window and live-arm offline tests cover .env parsing,
legacy documents, exact types, missing keys, target selection and mutable copies.
Compare class and decoder ASTs and dataclass field schemas. Run lint, format,
type checks, static live vocabulary/scenario checks and the relevant offline tests.
