# ADR 0395: Require a specific local interface for live-test ISO serving

## Status

Accepted. Partially supersedes [ADR 0115](0115-configure-live-test-environment.md).

## Context

A live-test runner serves installation media over HTTP during an HMC upload.
Native launcher media can contain guest network settings. A wildcard bind makes
the file available on interfaces the operator did not select.

## Decision

Require a specific local bind address for selections that serve media. Keep
`LIVE_TEST_ISO_BIND_HOST` in the operator's local configuration. Resolve supported
hostnames for the listener's address family and bind the validated numeric address.
Validate at selection time and again when starting the server.

## Consequences

Operators with wildcard settings must select an interface before running these
arms. The address in the HMC-visible URL remains independently configurable.
Saved configurations retain their original bind value for recovery and comparison.

## Considered & rejected

- **Use the advertised hostname for binding.** judgment: the local interface and
  the endpoint the HMC reaches can require distinct operator choices.
- **Allow an explicit wildcard override.** judgment: a wildcard grants access
  through interfaces outside the selection the operator approved for media serving.
