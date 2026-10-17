# hmcpctl

Manage IBM Power systems from your terminal or an AI assistant. hmcpctl connects
to the IBM Hardware Management Console (HMC) to inventory systems, inspect LPARs
and VIOS partitions, manage resources, and run lifecycle operations.

Use it as a CLI, an MCP server, or a [Python library](https://github.com/randomparity/hmcpctl/blob/main/docs/python-api.md).
MCP access policies control which tools, connections, and targets an assistant can use.

## Install

Requires **Python ≥3.11** and [uv](https://docs.astral.sh/uv/).
The project supports every stable, non-EOL CPython release at or above that floor.

Install the CLI and MCP server from source:

```bash
git clone https://github.com/randomparity/hmcpctl.git
cd hmcpctl
uv tool install --python 3.11 '.[app]'
hmcpctl --help
```

If your shell cannot find `hmcpctl`, run `uv tool update-shell` and open a new
terminal. The `app` extra includes both the CLI and MCP server; for development,
use [the contributor setup](CONTRIBUTING.md).

The package is not published to an index. A project that depends on it as a
library pins a [release tag](https://github.com/randomparity/hmcpctl/releases), which
names the commit the release was verified at and carries the
[API compatibility contract](docs/python-api.md). For the first release, `v0.1.0`:

```toml
dependencies = ["hmcpctl @ git+https://github.com/randomparity/hmcpctl@v0.1.0"]
```

Write `hmcpctl[app] @ ...` for the CLI and MCP server. With uv, `uv add --raw
"hmcpctl @ git+https://github.com/randomparity/hmcpctl@v0.1.0"` writes that entry. Without
`--raw`, uv moves the Git source into `[tool.uv.sources]`, which stays out of your package's
published metadata: other installers then look for `hmcpctl` on PyPI, where this project
publishes nothing and has not reserved the name, so whoever registers it decides what installs.

Existing pre-release installations must use the one-time
[clean-cutover procedure](docs/configuration.md#clean-cutover-from-the-former-name).

You need network access to an HMC and an HMC account with permissions for your
intended operations. The project targets HMC V8–V11; individual operations have
[version and firmware limits](https://github.com/randomparity/hmcpctl/blob/main/docs/compatibility.md).
Release 0.1.0 claims live verification for 15 system, LPAR, job and dedicated-PCIe
operations on HMC V10R3; the
[compatibility page](https://github.com/randomparity/hmcpctl/blob/main/docs/compatibility.md#what-010-supports)
lists them and shows how to check any other operation's state.

## Configure

For one HMC, set the connection environment in your shell:

```bash
export HMC_HOST=hmc.example.com
export HMC_USER=admin
export HMC_VERIFY_SSL=true
```

Set `HMC_PASSWORD` through your shell or secret manager before running commands.
Replace the example host and user with your own. TLS verification requires the
HMC's CA certificate to be trusted through `SSL_CERT_FILE`; see
[TLS trust](docs/configuration.md#tls-trust). Verification is off by default if
`HMC_VERIFY_SSL` is omitted, which exposes credentials to interception.

Check the connection:

```bash
hmcpctl console info
```

This prints HMC information after a successful logon. Some operations also use
SSH; see [configuration](https://github.com/randomparity/hmcpctl/blob/main/docs/configuration.md)
for SSH credentials, TOML profiles, friendly nicknames, and connection precedence.
For multiple consoles, start with `hmcpctl config init` and edit the file it prints.

## CLI quick start

Explore your systems and partitions:

```bash
hmcpctl systems list
hmcpctl systems health --json
hmcpctl lpars list --system <system-uuid>
hmcpctl lpars show <lpar-uuid>
hmcpctl vios list
```

Use UUIDs from the inventory output to identify resources. To change an LPAR,
for example:

```bash
hmcpctl lpars modify <lpar-uuid> --mem 16384 --procs 2.0
hmcpctl lpars power-on <lpar-uuid>
```

Memory is in MiB; power-on asks for confirmation. HMC permissions and operation
guards still apply. MCP access policies apply only to the MCP server; shell
commands run under your HMC credentials.

Use `hmcpctl --help` or `hmcpctl lpars --help` to explore.
The [CLI guide](https://github.com/randomparity/hmcpctl/blob/main/docs/cli.md)
covers storage, adapters, snapshots, provisioning, and job output.

## MCP quick start

Create `access-policy.toml` in the platform's hmcpctl configuration directory:

- Linux: `~/.config/hmcpctl/` (or `$XDG_CONFIG_HOME/hmcpctl/`)
- macOS: `~/Library/Application Support/hmcpctl/`
- Windows: `%APPDATA%/hmcpctl/`

Create the directory if needed. For an initial read-only server, add this policy
to the file, preserving any policies already there:

```toml
[[policies.readonly.grants]]
effects = ["read"]
connections = ["<default>"]
targets = "all-targets"
```

This permits read tools across the default HMC. Read tools can also disclose
local profile and policy metadata; see the
[policy scope guide](https://github.com/randomparity/hmcpctl/blob/main/docs/mcp-server.md#what-the-policy-does-not-bound).

With the connection environment above available, start the server over stdio:

```bash
hmcpctl serve --access-policy readonly
```

An MCP client normally launches this process for you. For clients that use an
`mcpServers` configuration, add:

```json
{
  "mcpServers": {
    "hmc": {
      "command": "hmcpctl",
      "args": ["serve", "--access-policy", "readonly"],
      "env": {
        "HMC_HOST": "hmc.example.com",
        "HMC_USER": "admin",
        "HMC_PASSWORD": "<HMC-password>",
        "HMC_VERIFY_SSL": "true"
      }
    }
  }
}
```

Replace the placeholders and use your client's secret handling if available.
If it cannot find the executable, use its absolute path from `uv tool dir --bin`.
The client process must have access to the policy file, and `SSL_CERT_FILE` must be set in its
environment when verifying a private CA ([TLS trust](docs/configuration.md#tls-trust)).

Try asking: “List my managed Power systems” or “Show fleet health.”
Call `hmc_effective_permissions` to inspect the active policy.

An access policy is required. For broader permissions, migration from older
deployments, audit logging, and HTTP setup, see the
[MCP server guide](https://github.com/randomparity/hmcpctl/blob/main/docs/mcp-server.md).
HTTP is unauthenticated: keep it on loopback or put an authenticated proxy in front.

## Documentation

- [Documentation index](https://github.com/randomparity/hmcpctl/blob/main/docs/index.md) — guides and reference.
- [MCP tool reference](https://github.com/randomparity/hmcpctl/blob/main/docs/tools/index.md) — available tools, their effects, and one-line summaries.
- [Environment variables](https://github.com/randomparity/hmcpctl/blob/main/docs/environment-variables.md) — all connection and operation settings.
- [Operation details](https://github.com/randomparity/hmcpctl/blob/main/docs/operations.md) — selectors, units, limits, and firmware-specific behavior.
- [Development guide](https://github.com/randomparity/hmcpctl/blob/main/docs/development.md) — stack, testing, source layout, and HMC API internals.

## Contributing, security, and license

See [Contributing](CONTRIBUTING.md) for the development and pull-request path.
Report suspected vulnerabilities through the private channel in the
[Security policy](SECURITY.md). This project is available under the [License](LICENSE).
