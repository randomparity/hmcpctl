# Locked pytest plugin warnings

## Problem

Locked pytest 9.2.0 emits two plugin deprecations: pytest-asyncio
1.4.1.post1 has no explicit fixture-loop default; AnyIO 4.14.2 imports
`CallSpec2` during pytest plugin autoload (#2019).

## Scope

The existing `pyproject.toml` pytest configuration remains the owner.
Set `asyncio_default_fixture_loop_scope` to `function`, matching the four
existing function-scoped async fixtures. Add `-p no:anyio` to `addopts`:
full collection has no AnyIO marker/backend consumers and the same 28,546
node IDs with that plugin disabled. Keep runtime dependencies installed.
No ownership transition, caller migration, or compatibility path is needed.

### Failure model

- Actors and deployments: local developers and Linux CI running locked pytest.
- Invariants and assets: existing async fixture setup/teardown loops, test
  selection, unrelated warning visibility, and the coverage floor remain intact.
- Accepted failure classes: none for the two reported warnings.
- Covered elsewhere: application behavior and exact coverage by `just verify`;
  Linux amd64 Python 3.11–3.15 portability by existing CI.

## Success

The configured pytest startup and collection emit neither reported deprecation.
Two async tests receive distinct function loops; their async fixture setup,
test body, and teardown use the same loop. An unrelated warning is visible
and still fails when promoted to an error. Dependency pins, runtime code,
and the coverage threshold remain unchanged.

## Validation

- `focused-test`: `tests/test_pytest_configuration.py` runs subprocess pytest
  using the repository configuration. Before the fix, startup/collection
  assertions fail on the two plugin deprecations; the green command is
  `uv run --no-sync pytest --no-cov -q tests/test_pytest_configuration.py`.
  The same probe verifies async fixture lifetime and unrelated warning
  capture/error behavior. Deliberate config and loop/warning mutations
  demonstrate the assertions fail, then restore the exact candidate.
- `task-test-not-applicable`: this prose specification has no executable
  consumer; source-text assertions would not prove its requirements.
- Run existing tests for the four async-fixture consumers and the canonical
  serial runner; exercise xdist subprocess mode for worker propagation.
  Run `just verify` and separate pinned prek before pushing; CI provides
  the canonical Linux worker path and declared Python-version matrix.
