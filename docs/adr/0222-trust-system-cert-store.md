# 0222 — Trust the operating-system certificate store for TLS verification

## Status

Accepted (2026-10-15). Resolves #1602.

## Context

`hmcpctl` builds its `httpx.AsyncClient` with `verify=self.config.verify_ssl`
(`src/hmcpctl/client/core.py`). When `verify_ssl=True`, httpx 0.28.1 consults
only the CA roots bundled with `certifi`, plus whatever `SSL_CERT_FILE` or
`SSL_CERT_DIR` names. The operating-system trust store — the macOS Keychain,
the Windows Credential Store, the Linux CA bundle updated by
`update-ca-certificates` / `update-ca-trust` — is never read.

An HMC whose certificate is signed by an organisational CA already present in
the OS store still triggers `CERTIFICATE_VERIFY_FAILED: self-signed certificate
in certificate chain` under `HMC_VERIFY_SSL=true`. The same handshake succeeds
with `openssl s_client`, which does read the OS store. Operators must export a
CA bundle and point `SSL_CERT_FILE` at it, even when the OS already trusts the
CA — a friction that disables verification in practice.

`truststore` 0.10.4 is a Python package that injects the OS trust store into
an `ssl.SSLContext`; it was already present in the dev venv as a transitive
dependency of the `app` extra but was not a declared dependency of `hmcpctl`.

## Decision

Add an opt-in field `trust_system_store` (default `true`) to `HMCConfig`. When
both `verify_ssl` and `trust_system_store` are `true` **and** neither `SSL_CERT_FILE`
nor `SSL_CERT_DIR` is set, `_new_http_client` builds a
`truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` and passes it as `verify=` to
`httpx.AsyncClient` instead of passing `True`.

**`SSL_CERT_FILE` and `SSL_CERT_DIR` take precedence.** When the operator exports an
explicit bundle or directory, the client uses it as before. The system store is consulted
only when no explicit bundle overrides it, preserving backward compatibility for operators
who already export `SSL_CERT_FILE` or `SSL_CERT_DIR`.

**`trust_system_store` is opt-out, not opt-in.** The default is `true` because
the system store is the expected trust source on every OS. An operator who wants
only the explicit bundle or the `certifi` roots can set
`HMC_TRUST_SYSTEM_STORE=false` (`--no-trust-system-store`).

**`trust_system_store` is ignored when `verify_ssl=false`.** No SSL context is
constructed when verification is disabled; the field has no effect there.

`truststore` is declared as a runtime dependency with range `>=0.10.4,<1` and
added to `LIBRARY_DEPENDENCIES` in `tests/test_supply_chain.py` and to ADR 0068.

## Consequences

- An operator with `HMC_VERIFY_SSL=true` and a CA-signed HMC certificate whose
  CA is in the OS store can connect without exporting `SSL_CERT_FILE`.
- The `SSL_CERT_FILE` / `SSL_CERT_DIR` mechanism continues to work and takes
  precedence, so existing configured deployments are unaffected.
- `truststore` joins the runtime dependency set; consumers that install the
  library path (`pip install hmcpctl`) will resolve and install it.
- Tests that construct an `HMCClient` with `verify_ssl=True` and do not set
  `trust_system_store=False` now exercise the truststore path; existing tests
  that rely on respx mocking are unaffected because no real TCP connection is
  made and the SSL context is only inspected, not used.

## Considered and rejected

**Always use `truststore` when `verify_ssl=true`.** Making it always-on removes
the `trust_system_store` toggle but breaks operators who rely on `SSL_CERT_FILE`
alone and whose OS stores contain certificates that would also match. It also
removes the ability to test the non-truststore path in CI. The opt-out approach
is strictly more flexible.

**Doing nothing.** The current documentation already recommends exporting the
OS store to a file as a workaround (`docs/configuration.md § Reuse the system
trust store`). That workaround requires re-exporting whenever the store changes,
which is untenable for managed deployments. The cost of adding `truststore` is
one declared dependency; the benefit is removing a recurring operator burden.

**A `trust_system_store` opt-in (default `false`) instead of opt-out.** An
opt-in preserves the exact pre-1602 behaviour by default but means every
operator who installs the new version still has to change a setting before the
OS store is consulted. The OS store is the intuitive default on every platform;
defaulting to `true` makes the common case automatic and reserves the explicit
`false` for the narrow case of wanting only the explicit bundle or certifi roots.
