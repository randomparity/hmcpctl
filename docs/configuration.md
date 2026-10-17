# Configuration

[Documentation index](index.md) · [Quick start](../README.md#configure)

## Configure

Configuration priority (highest to lowest): **CLI flags > `HMC_*` env vars > TOML profile**.
An explicit `--host` or `HMC_HOST` skips the TOML profile entirely, `--profile` and
`HMC_PROFILE` included: the connection is built from flags and environment variables alone
([precedence](environment-variables.md#precedence)).

## Clean cutover from the former name

Before 0.1.0 the project renamed its distribution, command, Python package, and configuration
directory from `hmc-mcp`/`hmc_mcp` to `hmcpctl`. An installation from before that rename needs
this one-time cutover. There is no compatibility alias or
configuration fallback. Stop every old CLI or MCP server process before moving the directory.
Each transaction below refuses to merge into an existing destination. The shell snippets run in
a subshell, so a refusal stops the snippet without closing your terminal.

The cutover also drops the old names from data the former tool wrote (ADR 0167):

- **Ownership stamps.** A partition whose description begins `[hmc-mcp owner:` reads as
  unowned, so ownership no longer protects it from another agent. Before upgrading, find these
  partitions with `hmc_list_lpar_ownership` (they appear as `owned: false, unparsed: true`)
  and delete them or restamp them by hand.
- **Snapshots.** A saved snapshot of format `hmc-mcp.lpar-snapshot` is refused. Recapture it
  with `hmc_snapshot_capture`.
- **Audit records.** `X-Audit-Memento` changes from `hmc-mcp:<agent_id>` to
  `hmcpctl:<agent_id>`; an HMC-side audit query spanning the upgrade must match both prefixes.

On Linux (including a custom `XDG_CONFIG_HOME`):

```bash
(
old_dir="${XDG_CONFIG_HOME:-$HOME/.config}/hmc-mcp"
new_dir="${XDG_CONFIG_HOME:-$HOME/.config}/hmcpctl"
if [ -e "$new_dir" ]; then echo "refusing existing destination: $new_dir" >&2; exit 1; fi
if [ -d "$old_dir" ]; then mv -- "$old_dir" "$new_dir"; fi
)
```

On macOS:

```bash
(
old_dir="$HOME/Library/Application Support/hmc-mcp"
new_dir="$HOME/Library/Application Support/hmcpctl"
if [ -e "$new_dir" ]; then echo "refusing existing destination: $new_dir" >&2; exit 1; fi
if [ -d "$old_dir" ]; then mv -- "$old_dir" "$new_dir"; fi
)
```

On Windows PowerShell:

```powershell
$oldDir = Join-Path $env:APPDATA 'hmc-mcp'
$newDir = Join-Path $env:APPDATA 'hmcpctl'
if (Test-Path -LiteralPath $newDir) { throw "Refusing existing destination: $newDir" }
if (Test-Path -LiteralPath $oldDir -PathType Container) {
    Move-Item -LiteralPath $oldDir -Destination $newDir
}
```

Then remove the former tool and install this checkout explicitly:

```bash
(
uv tool uninstall hmc-mcp
cd /absolute/path/to/hmcpctl-checkout
uv tool install --python 3.11 '.[app]'
hmcpctl --help
hmcpctl config show
test -f "${XDG_CONFIG_HOME:-$HOME/.config}/hmcpctl/access-policy.toml"  # Linux
if command -v hmc-mcp >/dev/null 2>&1; then echo "old command remains on PATH" >&2; exit 1; fi
)
```

For macOS, check `$HOME/Library/Application Support/hmcpctl/access-policy.toml` instead.
For PowerShell, run the equivalent verification:

```powershell
uv tool uninstall hmc-mcp
Set-Location -LiteralPath 'C:\absolute\path\to\hmcpctl-checkout'
uv tool install --python 3.11 '.[app]'
hmcpctl --help
hmcpctl config show
if (-not (Test-Path -LiteralPath (Join-Path $newDir 'access-policy.toml'))) {
    throw 'access-policy.toml was not moved'
}
if (Get-Command hmc-mcp -ErrorAction SilentlyContinue) { throw 'old command remains on PATH' }
```

Update every MCP client entry from command `hmc-mcp` to `hmcpctl`, preserving its arguments and
environment, and only then restart the client or server.

### TOML profile (recommended for multi-HMC setups)

Create `~/.config/hmcpctl/config.toml` (Linux / macOS `~/Library/Application Support/hmcpctl/config.toml` / Windows `%APPDATA%/hmcpctl/config.toml`):

```toml
default_profile = "prod"

[profiles.prod]
host = "hmc.example.com"
port = 443                         # optional; omit to allow legacy fallback
user = "admin"
password_env = "HMC_PROD_PASSWORD"   # resolved from the environment at runtime  # pragma: allowlist secret

[profiles.dev]
host = "hmc-dev.example.com"
user = "devadmin"
password = "devpassword"              # or store inline for non-production  # pragma: allowlist secret
```

Select a profile with `--profile <name>` or `HMC_PROFILE=<name>`.
`password_env` keeps secrets out of the file; `password` is accepted for convenience.

#### Profile nicknames (friendly names)

When you run more than one HMC, remember the exact `[profiles]` key for every
`--profile` / `HMC_PROFILE` call. A top-level `nicknames` table maps a friendly
name to an existing profile key, so a memorable name resolves to a profile:

```toml
[nicknames]
big-iron = "prod"
staging  = "stg-hmc-03"
```

`big-iron` and `staging` now work anywhere a profile name does — `--profile
big-iron`, `HMC_PROFILE=staging`, and even `default_profile = "big-iron"` —
because resolution is a single name-selection step inside the profile loader, so
the CLI and every MCP tool inherit it with no per-tool change.

Rules:

- **One level deep.** A nickname resolves to a *profile key*; it never resolves
  to another nickname (no chains, no cycles).
- **Case-sensitive.** `big-iron` does not match `BIG-IRON`.
- **Profile key wins on collision.** A name that is both a profile key and a
  nickname key selects the profile.
- **Clear failures.** A nickname whose target is not a profile, an unknown name,
  or a malformed `nicknames` table raises a `ConfigError` naming the available
  profiles and nicknames.

Nicknames are *surfaced, not hidden*. `config list` prints each nickname as
`nick -> target` (flagging a dangling target), `config show --profile <nick>` shows the
resolved profile with a `resolved_from` field naming the nickname, and
`hmc_list_configured_hosts` reports each nickname and its target-existence — none
of which resolves a secret. `config init` scaffolds a commented `nicknames`
example. A guardrail (`just nicknames`, in `just verify`) validates a committed
fixture: every nickname target exists, no nickname collides with a profile key,
and no target is itself a nickname.

### Environment variables (single-HMC / MCP server)

| Setting           | Env var              | CLI flag          | Default   |
|-------------------|----------------------|-------------------|-----------|
| Profile           | `HMC_PROFILE`        | `--profile`       | —         |
| HMC host / IP     | `HMC_HOST`           | `--host`          | —         |
| REST port         | `HMC_PORT`           | —                 | `443`     |
| User              | `HMC_USER`           | `--user, -u`      | —         |
| Password          | `HMC_PASSWORD`       | `--password, -p`  | —         |
| Verify TLS        | `HMC_VERIFY_SSL`     | `--verify-ssl`    | `false`   |
| HTTP timeout (s)  | `HMC_TIMEOUT`        | —                 | `180.0`   |
| SSH timeout (s)   | `HMC_SSH_TIMEOUT`    | —                 | `300.0`   |
| SSH key file      | `HMC_SSH_KEY_FILE`   | —                 | —         |
| Audit memento     | `HMC_AUDIT_MEMENTO`  | —                 | `hmcpctl` |
| Schema version    | `HMC_SCHEMA_VERSION` | —                 | _(unset)_ |

When the REST port is omitted, hmcpctl tries port 443 and retries logon once on
legacy port 12443 only if the first attempt fails at the transport layer. Setting
`port` in TOML or `HMC_PORT` selects that port explicitly: a connection failure
is returned immediately and never falls back. On an older HMC, leaving the port
unset can therefore add the duration of the failed 443 attempt. `HMC_TIMEOUT`
applies to each HTTP timeout phase rather than to the combined two-attempt wall
clock time. If a 443 logon response is lost, that unreachable attempt may leave
a server-side session until the HMC expires it.

See [Environment variables](environment-variables.md) for the
full reference, including descriptions and usage notes.

SSH-backed operations use port 22 and either the configured password or
`HMC_SSH_KEY_FILE`. They verify the HMC's host key against the process user's
`~/.ssh/known_hosts` by default. Provision independently verified keys before
running these operations; see [SSH trust setup](HMC_HINTS.md#ssh-host-key-trust).

HMCs ship self-signed certificates, so TLS verification is off by default and
`hmcpctl` emits `TLSVerificationDisabledWarning` once per HMC host and
`verify_ssl` setting source per process while it stays off. Reusable Python
consumers can import that category from `hmcpctl.api` and filter it without
suppressing unrelated `UserWarning`s. To verify the HMC certificate, trust its
CA through `HMC_CA_BUNDLE` or `SSL_CERT_FILE` (see [TLS trust](#tls-trust)) and set
`HMC_VERIFY_SSL=true` (`--verify-ssl`) — otherwise the HMC credentials are at risk of
man-in-the-middle interception.

The CLI displays these warnings without Python source locations. When verification
comes from the field default, a separate notice explains the planned 1.0 change.
Use `hmcpctl --no-verify-ssl systems list` to explicitly retain verification off,
or configure certificate trust and use `hmcpctl --verify-ssl systems list`.

TLS-disabled audit events remain separate, once per client construction. With no
configured logging handler, terminal stderr displays their timestamp, host and
setting source in a compact line; redirected stderr retains JSON for collectors.
Configured audit handlers receive the original records. See
[Audit transport](authorization-audit.md#routing-levels-and-silencing) for delivery details.

## TLS trust

With `HMC_VERIFY_SSL=true` the client determines what to trust from three sources, in this order:

1. **`HMC_CA_BUNDLE`** (TOML key `ca_bundle`, CLI `--ca-bundle`): when set, the named PEM file
   is used exclusively. Takes precedence over `SSL_CERT_FILE`, `SSL_CERT_DIR`, and
   `HMC_TRUST_SYSTEM_STORE` (ADR 0223).
2. **`SSL_CERT_FILE`** (or `SSL_CERT_DIR`): when set and `HMC_CA_BUNDLE` is not set, the named
   bundle is used exclusively. `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` are ignored.
3. **`HMC_TRUST_SYSTEM_STORE=true`** (the default): when neither `HMC_CA_BUNDLE` nor an explicit
   `SSL_CERT_FILE`/`SSL_CERT_DIR` is set, the operating-system trust store (macOS Keychain,
   Windows Credential Store, the Linux CA bundle rebuilt by `update-ca-certificates` /
   `update-ca-trust`) is consulted via the `truststore` library (ADR 0222).
4. **Bundled `certifi` roots**: when `HMC_TRUST_SYSTEM_STORE=false` and none of the above are
   set, only the CA roots bundled with the HTTP library are trusted.

Set these variables in the environment of the process that runs `hmcpctl`, including the
launcher of an MCP server. They need no root access and apply to every HMC that process
connects to. An unreadable or malformed bundle file is an error; it does not fall back to
other sources.

### Choose what to trust

An HMC presents either a certificate signed by an organizational CA or its own
self-signed certificate.

- **CA-signed.** Trust the CA. The root alone is enough when the HMC sends its
  intermediate certificates, which `openssl s_client -connect HMC:443 -showcerts` shows.
  Otherwise add the intermediate too. Your organization's certificate service
  publishes them; the examples call them `root-cert-1.cer` and `intermediate-cert-2.cer`.
- **Self-signed.** Trust the HMC's certificate itself. Fetch it, then compare its
  fingerprint with one you obtained by another route (for example from the HMC
  administrator) before relying on it:

  ```bash
  openssl s_client -connect HMC:443 -servername HMC </dev/null 2>/dev/null \
    | openssl x509 -out hmc-cert.pem
  openssl x509 -in hmc-cert.pem -noout -fingerprint -sha256
  ```

The certificate's names must still match the host you connect to; trusting it does not
disable hostname checking.

### Build a PEM bundle

The client reads PEM, which starts with `-----BEGIN CERTIFICATE-----`. A downloaded `.cer`
file may be PEM or DER; a PEM file prints as text and a DER file does not:

```bash
head -c 27 root-cert-1.cer      # PEM prints "-----BEGIN CERTIFICATE-----"
openssl x509 -inform DER -in root-cert-1.cer -out root-cert-1.pem   # convert DER to PEM
```

Put the certificates in one file, with a newline after each. Some downloaded files end
without a trailing newline, so a plain `cat a.pem b.pem` can join one `END` line to the
next `BEGIN` line, and the file then fails to load with `[X509] PEM lib`:

```bash
for f in root-cert-1.pem intermediate-cert-2.pem; do cat "$f"; echo; done > hmc-ca-bundle.pem
export HMC_CA_BUNDLE="$PWD/hmc-ca-bundle.pem"
export HMC_VERIFY_SSL=true
hmcpctl console info
```

`HMC_CA_BUNDLE` replaces the bundled roots rather than adding to them. To trust a public
CA as well, include it in the file. If you prefer not to use `HMC_CA_BUNDLE`, you can also
set `SSL_CERT_FILE="$PWD/hmc-ca-bundle.pem"` (an environment-only alternative; `HMC_CA_BUNDLE`
takes precedence when both are set).

### Reuse the system trust store

The easiest way to trust what the operating system already trusts is to leave
`HMC_TRUST_SYSTEM_STORE` at its default (`true`) and set `HMC_VERIFY_SSL=true`:

```bash
export HMC_VERIFY_SSL=true
# HMC_TRUST_SYSTEM_STORE defaults to true — the OS store is consulted automatically
hmcpctl console info
```

This works on macOS (Keychain), Windows (Certificate Store), and Linux without any
additional setup.

If you need to export a static bundle — for example when `HMC_TRUST_SYSTEM_STORE=false`,
or when you want a snapshot that is immune to OS store changes — export it once and point
`SSL_CERT_FILE` at the copy. Re-export it when the system store changes.

#### macOS

```bash
security find-certificate -a -p /Library/Keychains/System.keychain \
  /System/Library/Keychains/SystemRootCertificates.keychain > system-ca-bundle.pem
export SSL_CERT_FILE="$PWD/system-ca-bundle.pem"
```

#### Linux

Linux distributions maintain a system bundle that `update-ca-certificates` (Debian/Ubuntu)
or `update-ca-trust` (RHEL/Fedora) rebuilds when you add a CA. Point `SSL_CERT_FILE` at
the existing bundle — no regeneration is needed to read the current store:

```bash
# Debian / Ubuntu
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# RHEL / Fedora (RHEL ≤ 9 / Fedora ≤ 42)
export SSL_CERT_FILE=/etc/pki/tls/cert.pem

# RHEL / Fedora (RHEL 11+ / Fedora 43+)
export SSL_CERT_FILE=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
```

> **Note:** These paths and commands were verified against distribution documentation and
> the `truststore` library's source; they were not run on a live Linux host.

To add a CA to the system bundle and have `SSL_CERT_FILE` reflect it automatically,
copy the PEM file to `/usr/local/share/ca-certificates/` and run
`sudo update-ca-certificates` (Debian/Ubuntu), or copy it to
`/etc/pki/ca-trust/source/anchors/` and run `sudo update-ca-trust` (RHEL/Fedora).

#### Windows

Windows users with `HMC_TRUST_SYSTEM_STORE=true` (the default) get the Windows Certificate
Store automatically via `truststore` — no export is needed.

If you need a static PEM bundle (for example when `HMC_TRUST_SYSTEM_STORE=false`), export
the Trusted Root CAs from the Windows Certificate Store with PowerShell:

```powershell
# Run in PowerShell (5 or 7). -Encoding UTF8 is required; the default
# in Windows PowerShell 5 is UTF-16 LE, which OpenSSL cannot read.
$certs = Get-ChildItem -Path Cert:\LocalMachine\Root
foreach ($cert in $certs) {
    $pem = [Convert]::ToBase64String($cert.Export('Cert'), [Base64FormattingOptions]::InsertLineBreaks)
    "-----BEGIN CERTIFICATE-----`n$pem`n-----END CERTIFICATE-----" | Out-File -Append -Encoding UTF8 system-ca-bundle.pem
}
$env:SSL_CERT_FILE = "$PWD\system-ca-bundle.pem"
```

Alternatively, use `certutil` to export individual certificates and convert them with
OpenSSL as described in [Build a PEM bundle](#build-a-pem-bundle).

### Use a directory instead of a file

`SSL_CERT_DIR` names a directory of certificates, each reachable through a hash-named
entry. Create the entry with the subject hash:

```bash
mkdir -p ca-dir && cp root-cert-1.pem ca-dir/
ln -s root-cert-1.pem "ca-dir/$(openssl x509 -in root-cert-1.pem -noout -subject_hash).0"
export SSL_CERT_DIR="$PWD/ca-dir"
```

> **Windows:** `ln -s` and `openssl` are not available in PowerShell or Command Prompt.
> Use `SSL_CERT_FILE` instead — see the [Windows export steps](#windows) in the section
> above for how to build a PEM bundle and set `SSL_CERT_FILE`.

A directory holding only unhashed `.pem` files trusts nothing. When both variables are
set, `SSL_CERT_FILE` is used.
