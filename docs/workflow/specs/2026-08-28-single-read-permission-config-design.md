# Single-Read Permission Configuration Design

## Scope

Issue #536 requires each `hmc_effective_permissions` invocation to read and parse `config.toml`
at most once, independent of connection count. Existing reported values, `HMC_HOST` env-only
behavior, environment/profile precedence, fresh-per-call behavior, and exception classification
must remain unchanged. No cache or migration is permitted.

This design follows [ADR 0108](../../adr/0108-invocation-scoped-config-documents.md).

## Architecture

Configuration resolution gains an internal immutable document snapshot containing the selected
path and parsed mapping. `build_config` accepts an optional snapshot and otherwise behaves exactly
as today. The permissions resolver creates one snapshot immediately before resolving its ordered
connection set and passes it to every `_power_guard` call.

Snapshot creation stays inside the invocation and begins with `resolve_config_path`, matching the
current `build_config` path so path-resolution exceptions keep their existing classification.
When `HMC_HOST` selects environment-only
construction, the resolver skips the document read and `build_config` ignores profile selection as
it does today. When reading or parsing fails, the resolver retains that exception for the loop;
each connection receives the same existing closed `unresolved` classification, and warning
reporting remains connection-scoped as it is today.

## Data flow and errors

1. Determine and order the policy's reachable connection tokens using the existing rules.
2. If no ambient `HMC_HOST` exists and at least one connection is present, call
   `resolve_config_path` and parse its document once. An absent path produces an empty snapshot;
   exceptions from path resolution remain generic exceptions rather than being normalized to
   `ConfigError` by another path-selection helper.
3. Pass the snapshot, or its captured creation exception, through `_power_guard`.
4. For a snapshot, `build_config` preserves explicit-host and ambient-host branching, profile
   selection, environment-over-TOML precedence, override merging, and `NoProfileSelectedError`
   fallback. For an exception, `_power_guard` uses the same `ConfigError`/other-exception
   classification already used for per-connection failures.

No exception text is exposed to callers, and no document or secret value leaves configuration
resolution.

## Testing

- Add an application regression test with multiple granted profiles that spies on the config
  document reader and asserts exactly one read while checking both reported values.
- Pin zero document reads when ambient `HMC_HOST` selects environment-only construction (including
  a case-variant spelling) and when the policy yields no reachable connections.
- Add a malformed-document test with multiple granted connections that asserts one read attempt,
  one `ConfigError` unresolved row and existing connection-scoped warning per connection.
- Add a path-resolution failure test proving its existing generic exception classification is not
  normalized into `ConfigError` by snapshot creation.
- Add focused configuration tests proving a supplied snapshot avoids filesystem resolution while
  preserving profile/environment precedence and proving separate calls obtain fresh snapshots.
- Retain the existing malformed-file, missing-profile, `HMC_HOST`, case-variant environment, and
  per-profile report tests as behavioral regression coverage.

## Global constraints

- Python versions and target architectures remain those declared by the repository and CI:
  Python 3.11 through 3.14 on amd64 and arm64 Ubuntu runners.
- Add no dependency, cache, public API, schema, migration, or external service.
- Run `just verify` and `UV_NO_SYNC=1 uv run prek run --all-files`; CI separately gates generated
  tool documentation and document freshness.
