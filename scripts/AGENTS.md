# Script instructions

- Every top-level `scripts/<name>.py` entry point needs
  `tests/scripts/test_<name>.py`; `just test-layout` enforces this.
- Existing exceptions are `check_env_vars.py` → `tests/test_env_var_guard.py`
  and `live_test_runner.py` → `tests/test_live_runner.py`. Do not copy the
  exceptions for new scripts. Nested `live_test/` library modules use
  behavior-grouped tests.
- Read [test instructions](../tests/AGENTS.md) before changing tests.
- Live operations must follow [live testing](../docs/live-testing.md), including
  preflight, named arm dispatch, recovery, and commit-stamped filtered evidence.
  Never add HMC access to CI or `just` recipes, or publish raw result JSON.
- Keep guardrail recipes and prek hooks paired; read
  [CI instructions](../.github/AGENTS.md) when changing their execution graph.
