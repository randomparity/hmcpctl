# TLS trust documentation and tests (#1595)

## Problem

`HMC_VERIFY_SSL=true` tells operators to "install the HMC CA locally" in three places
and gives no procedure; `docs/environment-variables.md` admits the gap. Verified on the
pinned httpx 0.28.1 and the real CLI: the client trusts only `certifi` plus
`SSL_CERT_FILE` or `SSL_CERT_DIR`. The OS trust store and `REQUESTS_CA_BUNDLE` are not
read, so the issue's per-OS trust-store request would document instructions that fail.

## Scope

Docs and tests only; no `src/` change. Owners: OS-store support is #1602; other
exclusions are follow-up issues filed at the end of the quest.

- `docs/configuration.md`: new `## TLS trust` section covering the mechanisms below.
  - `SSL_CERT_FILE`/`SSL_CERT_DIR` (the directory needs hash-named entries); the OS store and
    `REQUESTS_CA_BUNDLE` are not read. A root-only bundle suffices when the HMC sends its chain.
  - An HMC may present a self-signed or an organizational-CA-signed certificate; trust
    the CA, or the certificate itself when self-signed.
  - PEM versus DER detection and conversion, and bundle assembly with a newline between
    files (unterminated concatenation fails to load).
  - Internal CAs appear only as `root-cert-1`, `intermediate-cert-2`; no hostname or URL.
- Link each "install the HMC CA locally" sentence (`README.md`, `docs/configuration.md`,
  `docs/environment-variables.md`) to the section; rewrite the gap-admission note. Keep the
  TLS bullet one `- **` bullet with its "where the setting came from" clause, which
  `tests/test_authorization_audit_doc.py` anchors on.
- `tests/app/test_cli_e2e.py`: four named tests using the `mock_hmc` fixture.

### Failure model

- Actors and deployments: an operator running the CLI or MCP server against an HMC.
- Invariants: instructions must work as written; docs publish no internal identifier.
- Accepted: `SSL_CERT_DIR` hashed-directory setup is documented from manual verification,
  not tested, since hashing needs an `openssl` binary CI does not guarantee.
- Covered elsewhere: OS-store trust (#1602); Windows steps and a CA-path setting (follow-ups).

## Success

1. The section states which variables are honoured and which are not.
2. The three sentences link to it; the gap note no longer says "no trust-store story".
3. The four tests pass and fail on the stated mutation.
4. The diff contains no hostname, URL or certificate name from an organizational CA.

## Validation

- Mechanism claims. focused-test: `test_ssl_cert_file_trust_verifies_hmc_e2e` (red:
  exit 1 without the variable), `test_verify_ssl_without_trusted_ca_fails_e2e`,
  `test_requests_ca_bundle_alone_does_not_trust_e2e`,
  `test_unterminated_pem_bundle_fails_to_load_e2e`; green: `pytest tests/app/test_cli_e2e.py -k "trust or verify_ssl or unterminated"`.
- Docs prose and links. task-test-not-applicable: prose is not machine-checkable; checked
  by manual run of each documented command and by `just doc-freshness`.
- Public safety. task-test-not-applicable: verified by scanning the diff for internal
  domain suffixes before commit.
