# Documentation

Start with the [README quick starts](../README.md) to install hmcpctl, connect to
an HMC, and use the CLI or an MCP client.

## Guides

| Guide | Contents |
|-------|----------|
| [Configuration](configuration.md) | TOML profiles, nicknames, environment precedence, TLS, and REST ports |
| [CLI](cli.md) | Inventory, LPAR lifecycle, storage, adapters, and snapshots |
| [LPAR ISO installation recipe](recipes/lpar-iso-install.md) | Resumable provisioning and optical-boot lifecycle |
| [MCP server](mcp-server.md) | Access policies, client setup, HTTP transport, migration, and diagnostics |
| [Python library](python-api.md) | Installation, example usage, typing, and the supported API contract |
| [kdive Tier A contract](kdive-tier-a-contract.md) | Pre-release LPAR imports, PowerAction job mapping, console capture, and ownership and host-key settings |
| [HMC compatibility](compatibility.md) | What 0.1.0 verifies live, HMC versions, and firmware write-path limitations |
| [Development](development.md) | Stack, setup, testing, source layout, and HMC REST API internals |
| [Read-only system inventory recipe](recipes/system-inventory.md) | Capture managed-system configuration before later operator decisions |
| [Bare-CEC LPAR recipe](recipes/bare-cec-lpar.md) | Bring up an LPAR on a dedicated PCIe slot, activate it to SMS, and tear it down |

## Reference

- [MCP tools](tools/index.md): generated tool signatures, parameters, and security metadata.
- [Environment variables](environment-variables.md): all supported settings and operation guards.
- [Operation details](operations.md): units, selectors, collection limits, and capability restrictions.
- [Authorization audit](authorization-audit.md): events, reason codes, and log delivery.
- [Progress-code definitions](progress-codes.md): offline POWER9 coverage, lookup and enriched outputs.
- [HMC CLI cheatsheet](hmc-cli-cheatsheet.md): underlying IBM commands used over SSH.
- [HMC hints](HMC_HINTS.md): SSH access, host-key trust, and command troubleshooting.
- [HMC API patterns](api-patterns.md): verified V10R3/POWER9 REST and CLI behaviour, with the code paths that conform.

## Project records

- [Architecture decisions](adr/): numbered decisions and compatibility contracts.
- [Workflow specifications](workflow/specs/): designs for individual changes.
- [Contributing](../CONTRIBUTING.md), [security policy](../SECURITY.md), and [license](../LICENSE).
