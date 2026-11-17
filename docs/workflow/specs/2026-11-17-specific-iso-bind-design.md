# Limit live-test ISO serving to one local interface (#1899)

## Problem and scope

Installation media can carry a guest's network settings. The live runner accepts
a wildcard bind and starts its HTTP server on interfaces the operator did not
select. [ADR 0395](../../adr/0395-require-a-specific-live-iso-bind.md) governs
the local listener. The HMC-visible upload URL remains separately configurable.

## Success

1. Media preflight, CLI and direct dispatch identify ST18, ST20 and ST72 through
   the runner's existing selection table. Reject unsafe configuration with a
   nonzero result before hardware probes, client creation or media operations.
   Cover the default, all, vmedia, provision-boot, both native groups and direct
   numbers. A non-serving selection can still read an existing wildcard setting.
2. Use the listener's existing IPv4 family to resolve the configured address.
   Reject empty input, resolution failures and addresses equivalent to an
   all-interface bind. Bind the checked numeric result so a subsequent name
   lookup cannot substitute an unchecked address. Only publish the advertised
   allowlist entry after the bind succeeds.
3. A correctly configured synthetic loopback server serves its one ISO and
   releases its socket. Historical wildcard values continue to decode verbatim,
   and restore still compares the full configuration.
4. The example and dataclass use a specific loopback address. The guide tells
   operators how to select their local HMC-facing interface. File-based runs
   still require the existing setting.

## Global Constraints

Python >=3.11, standard library only. Verification uses mocked hardware and synthetic loopback media.

## Failure model

- **Actors and deployments.** Local operators, multihomed runners and the HMC
  as HTTP client. An unrelated host can reach an unintended runner interface.
- **Invariants and assets.** The listener binds only the selected local IPv4 address. An
  unsafe bind leaves the environment allowlist intact. Records retain the
  original configured value even if later name resolution changes.
- **Accepted failures.** A specific address may be unavailable or unreachable
  from the HMC. Startup reports local bind errors through the existing arm path.
- **Covered elsewhere.** Template-placeholder admission belongs to #1898.
  The operator owns separately authorized live HMC validation. The maintainer
  owns any separately requested wildcard override or listener access controls.

## Threat model

The operator controls the authoritative configuration file. Resolution bridges
the configured local name and the operating system's socket address. An untrusted
network peer can retrieve media from a reachable listener. No new boundary is
introduced or widened.

Run the same bind-address policy at selection and server startup. Refuse an
unsafe target before opening the listener or updating its allowlist. Use the
setting name and a corrective instruction in configuration diagnostics. The
existing one-file handler limits served paths. Hosts already able to reach the
chosen interface retain access during the serving window.
