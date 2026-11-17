# ADR 0115: Configure the live-test environment

## Status

Accepted. [ADR 0395](0395-require-a-specific-live-iso-bind.md) supersedes the wildcard-listener allowance.

## Context

The live-test runner contains identifiers and hardware-specific values that only
apply to one environment. Reusing it requires editing tracked source and risks
publishing those identifiers. Issue #609 requires every environment-specific
value to be configured, with a documented `.env.example`. The operator also
requires missing or malformed live-test configuration to stop before a live
action, and sample values must be fictional.

## Decision

Use a strict `LIVE_TEST_*` namespace in the local `.env` file. The runner reads
that file directly and treats it as authoritative for this namespace; ambient
`LIVE_TEST_*` exports do not select live-test targets. It validates every
required value into `LiveTestContext` before it creates the MCP client. Scenario
modules consume the context rather than their own environment-specific module
constants. `.env.example` lists every setting, states its format and purpose,
and uses mutually consistent fictional values.

`HMC_*` connection settings retain their existing configuration precedence and
semantics. The live runner reports all missing or invalid `LIVE_TEST_*`
settings together and returns without opening a client. Virtual media uses
separate local-bind and HMC-advertised hosts so a wildcard listener can
advertise a routable address.

One shared pre-bootstrap entry validates the local live-test file for both CLI
and direct invocation. Duplicate `LIVE_TEST_*` keys are invalid, so the
operator-reviewed file has one unambiguous value for every target.

## Consequences

Operators copy and edit `.env.example` before a live run. Adding a new
environment-specific scenario input requires a corresponding `LIVE_TEST_*`
field, validation, example entry, and test. Runtime state such as discovered
UUIDs remains outside the static configuration.

## Considered & rejected

- **Keep source defaults.** judgment: defaults make an incomplete local setup
  capable of targeting an unintended environment.
- **Use `HMC_*` names for scenario inputs.** verified: `HMCConfig` owns the
  `HMC_*` namespace (`src/hmc_mcp/config.py`); mixing scenario inputs with
  connection settings would create an unsupported configuration surface.
- **Add a configuration dependency.** verified: the runner already parses its
  local `.env` file in `scripts/live_test_runner.py`; a small typed parser uses
  the existing standard-library path.
- **Let ambient `LIVE_TEST_*` exports override `.env`.** judgment: an operator
  must be able to review one local file and know which destructive targets run.
