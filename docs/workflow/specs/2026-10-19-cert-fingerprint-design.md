# SHA-256 certificate fingerprint trust source (#1614)

Implements [ADR 0224](../../adr/0224-cert-fingerprint-and-verify-ssl-default-migration.md)
Decision 1. ADR 0224 Decisions 2 and 3 are not implemented here (see Scope).

## Problem

An HMC with its factory self-signed certificate can only be reached with `HMC_VERIFY_SSL=false`
unless the operator builds and manages a CA bundle. ADR 0224 decides to trust such a host by the
SHA-256 digest of its leaf certificate. The 1.0 flip of the `HMC_VERIFY_SSL` default is gated on
this option existing.

## Scope

In scope:

1. **Setting.** `HMCConfig.cert_fingerprint: str | None = None`, read from `HMC_CERT_FINGERPRINT`,
   the profile TOML key `cert_fingerprint` (profiles already construct `HMCConfig` from their
   keys), and the root CLI option `--cert-fingerprint`. A field validator accepts exactly 32
   colon-separated pairs of hex digits, in either case (`openssl x509 -fingerprint -sha256`
   output after `=`). An empty string normalises to `None`, matching how `ca_bundle` treats an
   empty value. Any other value fails construction with a message that names the expected shape
   and the `openssl` command, and does not echo the rejected value. The value is validated even
   when `verify_ssl` is false, so a typo surfaces before the flip.
2. **Precedence.** With `verify_ssl` true, `_new_http_client` resolves trust in this order:
   `ca_bundle` → `cert_fingerprint` → `SSL_CERT_FILE`/`SSL_CERT_DIR` → `trust_system_store` →
   certifi. With `verify_ssl` false, `cert_fingerprint` is ignored and `verify=False` as today.
3. **Handshake-time check.** The fingerprint branch passes
   `_fingerprint_ssl_context(expected, host)`. That is a stdlib
   `ssl.SSLContext(PROTOCOL_TLS_CLIENT)` with `check_hostname=False` and `verify_mode=CERT_NONE`,
   whose `sslobject_class` is a subclass of `ssl.SSLObject`. The subclass's `do_handshake` runs
   the real handshake, then hashes `getpeercert(binary_form=True)` with SHA-256 and compares it
   against the expected digest (colons stripped, case-folded):
   - On mismatch it raises `_FingerprintMismatch(ssl.SSLCertVerificationError)`, carrying the
     presented fingerprint. The handshake raising means httpx/anyio never send a request byte,
     so the logon password is never sent. This was confirmed by a spike against httpx 0.28 /
     anyio: the server logged no request.
   - On match it calls `audit.record_tls_fingerprint_verified(host=..., fingerprint=...)` and
     returns.

   This is ADR 0224's "connect, read the peer certificate, compare, abort on mismatch", placed
   at the earliest point where the certificate exists.
4. **Error.** `HMCTLSFingerprintError(HMCTransportError)` in `hmcpctl.errors`. In
   `HMCClient._request`, the `httpx.TransportError` handler walks the exception's
   `__cause__`/`__context__` chain. If it finds a `_FingerprintMismatch`, it raises
   `HMCTLSFingerprintError` naming the host, the expected and presented fingerprints, that
   nothing was sent, and the fix: re-obtain the fingerprint over a trusted channel and update
   `HMC_CERT_FINGERPRINT`. Otherwise the existing `HMCTransportError` path is unchanged.
   `hmcpctl.api` is not extended (ADR 0118's six-name facade). `logon` re-raises
   `HMCTLSFingerprintError` without attempting the legacy port-12443 fallback. A retry on
   another port would replace the mismatch report with an unrelated connection error.
5. **Audit.** A new event, `tls-fingerprint-verified`, at INFO (`_ALLOW_LEVEL`), with `time`,
   `event`, `host` (`HMCConfig.host`) and `fingerprint` (normalised upper-case colon form). It is
   emitted once per verified TLS handshake: the host was authenticated by an operator-supplied
   digest rather than by a CA, and the stream records that per connection. It is added to the
   `Event` literal, to the hard-coded vocabulary in `tests/unit/test_audit.py`, and as a section
   with a JSON sample in `docs/authorization-audit.md` (held by
   `tests/test_authorization_audit_doc.py`).
6. **Docs and messages.**
   - The `docs/environment-variables.md` table row plus the TLS verification bullet.
   - The precedence list in `docs/configuration.md#tls-trust`, plus how to obtain a fingerprint.
   - A `CHANGELOG.md` entry under Added.
   - The `--cert-fingerprint` help text.
   - The pre-1.0 migration warning in `HMCClient.__init__` gains `HMC_CERT_FINGERPRINT` among
     the options it names. ADR 0224 Decision 3 has that warning name the available options, and
     this change adds one.

Excluded, with owners approved by the operator in `WORK:SCOPE`:

- The `HMC_VERIFY_SSL=true` default flip and the 1.0 upgrade notes belong to the 1.0 release
  under #1605.
- The per-host CA-bundle map is rejected by ADR 0224 Decision 2.

No new dependency: `ssl` and `hashlib` are stdlib. No ADR is added.

## Failure model

1. **Actors and deployments.**
   - A local operator running the CLI.
   - An MCP server launched by an operator, with configuration from the environment or a TOML
     profile.
   - A network attacker positioned between the client and the HMC.
2. **Invariants and assets at stake.**
   - The HMC credentials in the logon body must not be sent to a peer whose certificate digest
     differs from the configured one.
   - Configurations that leave `cert_fingerprint` unset keep their current trust behaviour.
   - The closed audit vocabulary stays in step with its document.
3. **Accepted failure classes.**
   - Hostname, validity period and EKU are not checked under a fingerprint. ADR 0224 accepts
     this: the digest pins one exact certificate.
   - Per-handshake audit volume is bounded by connection count. httpx pools connections per
     client, and each tool call builds one client.
   - The pin is enforced only on TLS objects created through `wrap_bio` (`sslobject_class`).
     `httpx.AsyncClient` on anyio and trio is the only HMC REST transport, and both wrap through
     `wrap_bio`. The e2e mismatch test fails if that ever stops holding.
   - When a pinned HMC's certificate is regenerated, its connections fail until the operator
     updates the value. That is the intended behaviour.
4. **Covered elsewhere.**
   - The SSH transport's host-key trust (`ssh_verify_host_key`).
   - The ISO download client in `operations/storage/resources.py`, which does not talk to the HMC.

### Threat model

- **Boundaries.**
  - Widened: the TLS peer certificate presented to the REST client, which is untrusted network
    input. The design hashes its DER bytes and parses nothing.
  - Added: the `HMC_CERT_FINGERPRINT` / TOML / CLI value, which is operator-trusted.
- **Actors.**
  - The untrusted actor is a network attacker who can present any certificate.
  - The operator is trusted to have obtained the digest over a trusted channel.
- **Controls.**
  - The digest is compared before any application data is sent.
  - A malformed setting is refused at construction, without echoing it.
  - On mismatch, the presented digest is shown. It is public certificate data, not a secret.
- **Out of scope.**
  - An operator who pins an attacker's certificate.
  - Downgrade to `HMC_VERIFY_SSL=false`, which is operator-controlled and already audited.

## Success

- With `verify_ssl` true and only `cert_fingerprint` among the trust settings, a correct digest
  connects to a self-signed HMC and emits one `tls-fingerprint-verified` record per handshake.
- A wrong digest raises `HMCTLSFingerprintError`, and the HMC receives zero HTTP requests.
- `ca_bundle` wins over `cert_fingerprint`. `cert_fingerprint` wins over `SSL_CERT_FILE`,
  `SSL_CERT_DIR` and `trust_system_store`. With `verify_ssl` false it is ignored.
- `just verify` and `prek run --all-files` pass, including `env-vars` and the audit document tests.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Field validation and normalisation | focused-test | `tests/unit/test_config.py`: valid upper/lower case accepted, empty → `None`; bare hex, wrong length, non-hex rejected without echoing the value |
| Precedence | focused-test | `tests/unit/test_client.py`: captured `verify=` is the fingerprint context over `SSL_CERT_FILE`, `SSL_CERT_DIR` and the system store; the path when `ca_bundle` is also set; `False` when `verify_ssl` is false |
| Handshake match and mismatch | focused-test | `tests/app/test_cli_e2e.py` against `mock_hmc`: the matching digest lists systems; a mismatch exits non-zero naming the fingerprint error, with `request_log` empty |
| Error mapping and no port fallback | focused-test | `tests/unit/test_client.py`: a transport error chained from `_FingerprintMismatch` becomes `HMCTLSFingerprintError`; logon on default port 443 does not rebuild for 12443 |
| Audit record | focused-test | `tests/unit/test_audit.py` vocabulary and record shape; `tests/test_authorization_audit_doc.py` passes with the new section |
| CLI option | focused-test | the e2e tests pass `--cert-fingerprint` |
| Env-var docs | focused-test | `just env-vars` |
| Migration warning text, configuration prose, CHANGELOG | task-test-not-applicable | human-readable prose with no executable consumer; asserting the wording would snapshot prose |
