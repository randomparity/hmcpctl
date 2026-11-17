# Test instructions

- Test behavior, including errors, with isolated fixtures. For configuration that
  must ignore the workstation environment, use `HMCConfig.from_mapping({...})`
  (ADR 0096). Omitted fields use defaults; environment and dotenv are not read.
- `_env_file=None` does not isolate `os.environ`; do not add it or remove
  `monkeypatch.delenv` because it is present. When environment precedence is the
  behavior under test, use the normal constructor with explicit
  `monkeypatch.setenv`/`delenv`, not `from_mapping`.
- `HMC_PROFILE` selects profiles independently of config fields. `HMC_HOST` also
  reroutes profile and authorization decisions. Unset it before diagnosing an
  authorization assertion; do not change access-policy expectations to match
  workstation credentials.
- Read the [environment diagnostic](../docs/workflow/agent-operations.md#pre-existing-test-failures)
  before diagnosing ambient configuration. Keep its non-default probe values
  current when fields change. Diagnostic failures expose isolation gaps; they
  are not a baseline suite result. Verify fixes under the triggering environment.
- Check renamed imports when collection fails; `just smoke` can reveal server
  import errors. Fix genuine pre-existing guardrail failures in the same PR.
- Script test placement follows [scripts guidance](../scripts/AGENTS.md).
  Public facade exports are asserted in `unit/test_public_api.py`.
