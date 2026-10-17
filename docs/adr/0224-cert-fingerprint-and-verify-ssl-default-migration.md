# 0224 — SHA-256 certificate fingerprint, per-host pinning scope, and HMC_VERIFY_SSL default migration

## Status

Accepted (2026-10-15). Resolves #1613. Implementation: #1614.

## Context

`HMC_VERIFY_SSL` defaults to `false` (`src/hmcpctl/config.py:170–172`).
The `docs/environment-variables.md:64–79` policy statement explains the
rationale: HMCs ship self-signed certificates, and flipping the default would
break every existing operator's working configuration on upgrade with no
migration path.

ADR 0222 (PR #1617) added `HMC_TRUST_SYSTEM_STORE`, letting operators whose
HMC certificate is signed by an organisational CA already in their OS trust
store enable verification without exporting a bundle. ADR 0223 (PR #1631)
added `HMC_CA_BUNDLE`, a first-class TOML/CLI/env-var setting that names a
specific PEM file. Together, these two options cover HMCs with CA-signed
certificates.

The remaining blocking case is an HMC with a **self-signed certificate** — the
most common factory default — where the operator cannot or does not want to
manage a CA bundle. To enable verification for that case, `hmcpctl` needs a
way to trust a specific certificate by its content rather than by its CA
chain. The two candidate mechanisms are:

- **SHA-256 fingerprint verification:** the operator records the SHA-256 hash
  of the leaf certificate. On each connection, `hmcpctl` completes the TLS
  handshake and then compares the peer's presented certificate hash against the
  recorded value. A mismatch aborts the connection.
- **Per-host CA-bundle map:** a TOML structure that maps hostnames to CA-bundle
  paths, allowing a different bundle per HMC within one profile.

The `docs/environment-variables.md:72–74` policy also states that the `false`
default "stays until 1.0", without spelling out what must land for 1.0 to
be the right release for the flip.

## Decisions

### Decision 1 — Add SHA-256 fingerprint verification

Add `cert_fingerprint: str | None = None` to `HMCConfig`, exposed as:

- `HMC_CERT_FINGERPRINT` (environment variable)
- `cert_fingerprint` (TOML key under `[hmc]`)
- `--cert-fingerprint` (CLI flag)

The value is the colon-separated hexadecimal SHA-256 digest of the DER-encoded
leaf certificate, matching the format `openssl x509 -fingerprint -sha256`
prints (e.g. `AA:BB:CC:...`). Case-insensitive comparison; colons required in
the stored value for legibility but stripped before comparison.

**How it works.** `truststore` does not support fingerprint pinning, so
fingerprint verification requires a custom SSL context. `_new_http_client`
builds an `ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` with verification
**disabled** at the TLS layer, connects, retrieves the peer's DER-encoded
certificate from the connected socket, computes its SHA-256 digest, and
compares it against the configured value. When the digests match, the
connection proceeds; when they do not, `_new_http_client` raises
`HMCTLSFingerprintError` and closes the connection.

**Precedence.** `cert_fingerprint` slots into the existing four-way trust
chain:

```
HMC_CA_BUNDLE set            → verify=ca_bundle (ADR 0223)
HMC_CERT_FINGERPRINT set     → custom fingerprint context (this ADR)
SSL_CERT_FILE/SSL_CERT_DIR   → env-bundle precedence (existing)
HMC_TRUST_SYSTEM_STORE=True  → truststore.SSLContext (ADR 0222)
else                         → True (certifi roots)
```

`cert_fingerprint` takes precedence over the SSL env vars and the system
store, but defers to an explicit `HMC_CA_BUNDLE`: if both are set, the CA
bundle wins and the fingerprint is ignored (an operator who supplies a CA
bundle has asserted CA-level trust; fingerprint pinning is a weaker override
for the no-CA case only).

**Security properties.** Fingerprint pinning provides authentication against
MitM, subject to the operator having obtained the fingerprint through a
trusted channel (e.g. direct HMC console access, SSH, or a previous trusted
connection). It does not validate the certificate's hostname, validity period,
or extended key usage. The `tls-verification-disabled` audit record is **not**
emitted when `cert_fingerprint` is set and matches; the fingerprint constitutes
explicit operator-acknowledged trust for that specific certificate. A new audit
record type (`tls-fingerprint-verified`) is recorded instead, naming the host
and the matched fingerprint.

**`cert_fingerprint` is ignored when `verify_ssl=False`**, consistent with
all other trust-source settings.

Implementation is deferred to issue #1614.

### Decision 2 — No per-host CA-bundle map in this release

A per-host CA-bundle map (a structure like `[hmc.ca_bundles]` mapping hostnames
to PEM paths) is **not added** in this release cycle.

The primary use case — an operator with multiple HMCs signed by different CAs
in one configuration — is already addressed by the multi-profile mechanism:
each HMC gets its own TOML profile with its own `HMC_CA_BUNDLE`. The
within-profile per-host map adds implementation complexity
(custom TOML parsing, cert-selection logic, documentation surface) with value
only for operators who deliberately use a single profile for heterogeneous HMC
fleets, which is not the typical deployment pattern.

This decision does not foreclose a per-host map in a future release if
operators request it. When that work starts, ADR 0223's `ca_bundle` field is
the natural extension point.

### Decision 3 — Migration path for HMC_VERIFY_SSL default flip

The `HMC_VERIFY_SSL` default changes from `false` to `true` in the **1.0**
release, under the following conditions:

1. **Gate: fingerprint option must land.** The 1.0 flip requires issue #1614
   (implementation of this ADR's Decision 1) to have merged. Without
   `HMC_CERT_FINGERPRINT`, operators with self-signed HMC certificates have no
   single-setting path to re-enable their connection after the flip.

2. **Gate: documentation of migration steps.** Before the flip, the upgrade
   notes must list the available trust-source options in priority order
   (`HMC_CA_BUNDLE` → `HMC_CERT_FINGERPRINT` → `HMC_TRUST_SYSTEM_STORE` →
   `HMC_VERIFY_SSL=false` as a temporary escape hatch) and link to
   `docs/configuration.md`.

3. **Deprecation warning in the pre-1.0 release.** In the release immediately
   preceding the 1.0 flip, emit a one-time startup warning when `verify_ssl`
   equals `false` and its source is `field-default` (i.e. the operator has
   not set `HMC_VERIFY_SSL` explicitly). The warning names the forthcoming
   change and the available migration options. An operator who has set
   `HMC_VERIFY_SSL=false` explicitly already has a deliberate choice recorded
   and does not receive the warning.

4. **`HMC_VERIFY_SSL=false` remains valid indefinitely.** Operators who need
   to keep verification off for a specific HMC can set it explicitly; the
   default flip only changes the behaviour for operators who have never touched
   the setting.

The `tls-verification-disabled` audit record continues to be emitted whenever
`verify_ssl=false`, regardless of whether that came from the field default or
an explicit operator setting. The source field (`field-default`,
`environment:HMC_VERIFY_SSL`, or `explicit-argument`) allows tooling to
distinguish intentional from default-inherited off states after the flip.

## Considered and rejected

**Fingerprint without a custom SSL context (truststore-based).** The
`truststore` library injects the OS CA store but does not expose certificate
fingerprints. A post-handshake fingerprint check is therefore necessary
regardless. Rejected: no simpler implementation path exists.

**Embedding the fingerprint in the URL (à la `curl --pinnedpubkey`).** Curl's
`sha256//` prefix syntax is familiar but mixes connection target and trust
policy, makes TOML profile files harder to read, and cannot be set as a
standalone environment variable. Rejected in favour of a separate
`HMC_CERT_FINGERPRINT` field.

**SPKI hash instead of certificate hash.** Pinning the Subject Public Key
Info (SPKI) hash survives certificate renewal when the same key pair is
reused. HMC self-signed certificates are typically re-generated on firmware
update, which replaces both the key and the certificate. SPKI pinning provides
no practical advantage over certificate pinning for the HMC use case, and
certificate hashes are easier for operators to obtain (`openssl s_client` and
`openssl x509` are standard tools). Rejected.

**Per-host CA-bundle map.** Discussed in Decision 2 above. Rejected for this
release.

**Default flip immediately (without fingerprint landing).** Would break every
deployment using a self-signed HMC certificate. Rejected; the gate in
Decision 3 prevents this.

**Default flip deferred past 1.0.** The `docs/environment-variables.md`
policy already commits to 1.0. Deferring further would invalidate that
commitment and leave the insecure-by-default state beyond the boundary the
project has publicly named. Rejected unless the fingerprint implementation
gates are not met by the 1.0 milestone.

## Consequences

- A new `HMC_CERT_FINGERPRINT` / `--cert-fingerprint` / TOML `cert_fingerprint`
  setting will be added in #1614, enabling verification for self-signed HMC
  certificates without requiring a CA bundle.
- The five-way trust precedence
  (`ca_bundle` → `cert_fingerprint` → `SSL_CERT_FILE` → system store → certifi)
  will be complete after #1614 lands.
- The `HMC_VERIFY_SSL` default will flip to `true` in 1.0, gated on #1614
  merging and documentation of migration steps.
- No code changes in this PR; `docs/adr/0224-*.md` is the sole deliverable.
