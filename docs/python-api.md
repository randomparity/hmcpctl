# Python library

[Documentation index](index.md)

A project that depends on the library pins a release tag, in the form the
[README](../README.md#install) shows. To install from a local checkout into your
application's virtual environment instead:

```bash
uv pip install /path/to/hmcpctl
```

The bare package includes the connection boundary. The `app` extra is needed
only for the CLI and MCP server. Configure the
[HMC environment variables](environment-variables.md) before connecting.

## Stable connection API

`hmcpctl.api` has exactly six stable names: `HMCClient`, `HMCConfig`,
`ConfigError`, `HMCError`, `HMCTransportError`, and
`TLSVerificationDisabledWarning`. Entering the client’s async context authenticates
automatically, and exiting it logs off and closes the transport.

```python
import asyncio

from hmcpctl.api import HMCClient, HMCConfig


async def main() -> None:
    async with HMCClient(HMCConfig()) as hmc:
        assert hmc.is_logged_on


asyncio.run(main())
```

Only `HMCClient.__init__`, `__aenter__`, `__aexit__`, `is_logged_on`, `logon`,
and `logoff` are stable client lifecycle members. Inherited REST methods are
callable but unsupported. The distribution ships a PEP 561 `py.typed` marker,
so type checkers read this contract's annotations.

## Domain operations

Operations and operation-specific models live in their owning domain modules.
They remain available for pre-release consumers that explicitly choose those
imports, but they are not compatibility promises. For example:

```python
from hmcpctl.operations.inventory.capacity import fetch_capacity_report

report = await fetch_capacity_report(hmc)
```

This distinction keeps the stable facade small while allowing the operation
modules to evolve independently. See [ADR 0118](adr/0118-core-library-facade.md).
