# Primary catalog listing: payload and call-count report

**Issue:** #1853 · **Decision:** [ADR 0330](../adr/0330-default-listing-is-the-primary-catalog.md)
· **Spec:** [primary catalog listing](specs/2026-11-03-primary-catalog-listing-design.md)

Measured on 2026-11-03 at commit `c0f0499430152b0b9a54f5190f1103a84d833b97`, Python 3.11.15,
fastmcp 4.0.10, macOS arm64. These are point-in-time figures: any tool docstring or schema
change moves them, and no gate keeps them current. There is no target figure.

## What ran where

- **Locally and in CI:** the listing tests and the transport scenarios in
  `tests/app/test_primary_catalog_scenarios.py`, over an in-memory fastmcp client session
  against in-repo fakes.
- **Not run:** no emulation and no hardware. Native proof on a deployed commit is #1854.

## Listing payload

Legacy-equivalent policy (`compile_legacy_policy(TOOL_SECURITY, ("<default>",))`). Bytes are
the UTF-8 length of
`json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools], sort_keys=True, separators=(",", ":"))`
over one client session's `list_tools()`.

| Listing | Tools | Bytes |
| --- | --- | --- |
| `catalog="registered"` (the listing before this change) | 205 | 632,045 |
| `catalog="primary"` (the default now) | 11 | 109,723 |

The `initialize` instructions are 5,691 bytes, down from 6,422.

The figures were produced with:

```python
import asyncio, json
from fastmcp import Client
from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.server import TOOL_SECURITY, create_mcp

async def size(catalog):
    policy = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))
    app = create_mcp(policy, catalog=catalog)
    async with Client(app) as client:
        tools = await client.list_tools()
    body = json.dumps(
        [t.model_dump(mode="json", exclude_none=True) for t in tools],
        sort_keys=True, separators=(",", ":"),
    )
    return len(tools), len(body.encode()), len(app.instructions.encode())

for catalog in ("registered", "primary"):
    print(catalog, *asyncio.run(size(catalog)))
```

## Tool calls per task

Each count is the number of `tools/call` requests the scenario makes after its one
`tools/list`. The test asserts the exact count.

| Scenario | Task | Calls |
| --- | --- | --- |
| S2 | create and install to `boot_started`, then read the operation | 2 |
| S3 | reconfigure memory | 1 |
| S4 | inspect state, RMC and refcodes | 1 |
| S5 | decommission, retaining storage | 1 |
| S6 | find a specialist by query, read its schema, invoke it (plus one direct call for comparison) | 3 + 1 |
| S7 | denied power start, search, invoke a withheld and an unknown name | 4 |
| S8 | plan with an ambiguous placement | 1 |
| S9 | provision refused for a missing `exclusive_writer_window` | 1 |
| S10 | provision deferred, then boot from a new application and session | 1 + 1 |
| S11 | direct call of an unlisted specialist | 1 |
| S1b | read-only policy: find a specialist and call it by name | 2 |

Under the read-only policy `hmc_invoke_tool` is withheld, so a specialist costs a search and a
direct call by name (ADR 0330 Consequences).
