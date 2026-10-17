# 0223 — First-class CA-bundle setting for TLS verification

## Status

Accepted (2026-10-15). Resolves #1604.

## Context

With `HMC_VERIFY_SSL=true`, the only way to point `hmcpctl` at a private CA
is the process-wide `SSL_CERT_FILE` / `SSL_CERT_DIR` environment variables
documented in `docs/configuration.md`. These variables have three drawbacks:

1. **Process-wide scope.** They replace the CA roots for every TLS connection
   the process makes, not just the HMC connection.
2. **Cannot differ per HMC.** A deployment with two HMCs signed by different
   CAs cannot use `SSL_CERT_FILE` to select a different bundle per HMC.
3. **Invisible in `config.toml` and `hmcpctl` help.** There is no TOML key
   and no CLI flag. Operators discover the mechanism only from the docs.

ADR 0222 (PR #1617) introduced `HMC_TRUST_SYSTEM_STORE` and extended
`_new_http_client` with a three-way precedence:

```
SSL_CERT_FILE/SSL_CERT_DIR wins → trust_system_store SSLContext → True (certifi)
```

A first-class `HMC_CA_BUNDLE` setting that names a PEM file in the profile or
on the command line closes all three drawbacks without replacing the existing
mechanisms.

## Decision

Add `ca_bundle: str | None = None` (env var `HMC_CA_BUNDLE`, TOML key
`ca_bundle`, CLI flag `--ca-bundle`) to `HMCConfig`. Extend `_new_http_client`
to check it first:

```
HMC_CA_BUNDLE set → pass verify=ca_bundle (path string; httpx accepts it)
SSL_CERT_FILE/SSL_CERT_DIR set → trust_system_store is bypassed (existing)
trust_system_store=True and no env bundle → truststore.SSLContext (ADR 0222)
else → True (certifi roots)
```

**`ca_bundle` wins over all other trust sources when `verify_ssl=True`.** The
path is passed as the string `verify=` argument to `httpx.AsyncClient`. httpx
0.28.x accepts a PEM file path for `verify=`, so no new library dependency is
needed.

**`ca_bundle` does not change the `verify_ssl` audit record.** The
`tls-verification-disabled` record names where `verify_ssl` came from
(`explicit-argument`, `environment:HMC_VERIFY_SSL`, or `field-default`);
`ca_bundle` is a trust-source choice, not the enable/disable decision.

**`ca_bundle` is ignored when `verify_ssl=False`**, consistent with all other
trust-source settings.

## Alternatives considered

**Keep using `SSL_CERT_FILE`:** Simple, no code change. Rejected because the
variable is process-wide, cannot vary per HMC, and is invisible in the
`config.toml`/help surfaces where operators configure `hmcpctl`.

**Per-host bundle map:** Allow a different `ca_bundle` per HMC host. Deferred
to #1613 (fingerprint / per-host pinning). A global `ca_bundle` setting solves
the main use case without the added complexity of a map.

**Fingerprint-only verification:** Trust by certificate fingerprint instead of
by CA. Deferred to #1613.

## Interaction with HMC_TRUST_SYSTEM_STORE

When `ca_bundle` is set, the system-store path is skipped entirely: the client
passes the bundle path and does not create a `truststore.SSLContext`. This is
intentional — an operator who sets `HMC_CA_BUNDLE` has made an explicit trust
decision that should not be supplemented by the OS store.

When `ca_bundle` is unset, the existing ADR 0222 precedence applies unchanged.

## Interaction with SSL_CERT_FILE / SSL_CERT_DIR

`HMC_CA_BUNDLE` takes precedence: when it is set, `SSL_CERT_FILE` and
`SSL_CERT_DIR` are ignored for the HMC connection (though httpx reads them for
other connections the process may make). This is deliberate — `HMC_CA_BUNDLE`
is an explicit per-configuration choice that should not be silently overridden
by an ambient environment variable.

## Consequences

- Operators can configure the HMC CA bundle through a TOML profile key or a
  CLI flag without altering process-wide environment variables.
- The four-way precedence (`ca_bundle` → `SSL_CERT_FILE` → system store →
  certifi) is now complete and documented in `docs/configuration.md`.
- No new library dependency: httpx already accepts a path string for `verify=`.
- `just env-vars` requires `HMC_CA_BUNDLE` to appear in
  `docs/environment-variables.md`; the new row satisfies that gate.
