# 0393 — CLI installer provisioning uses a typed file and the operation journal

## Status

Accepted for issue #1903.

## Context

Operators can already provision partitions from the CLI and MCP tool against the same
journal. Installer media has nested network and producer data. An installer boot can
take several minutes to produce the reference code described in ADR 0328.

## Decision

1. The CLI accepts a JSON file containing the shared install object, an optional MAC
   for prepared media, the boot mode, and the boot continuation. A fresh writer-window
   assertion accompanies a write. The journal owns the effective continuation request.
2. Use the operation engine's existing maximum wait for the worker's reference-code
   budget. The CLI waits for the worker's final record before exiting.
3. A deliberate deferred stop and an observed firmware handoff are successful CLI
   outcomes. The printed record distinguishes them. Guest installation and readiness
   remain outside the available evidence.

## Consequences

Operators can attach a console between staging and boot. File parsing and the operation
store have separate limits. A schema-valid install can exceed the journal's canonical
request cap in ADR 0190. Historical non-install requests retain their recorded shape.

## Considered & rejected

- **Keep installer provision MCP-only.** judgment: it leaves #1903 unresolved.
- **Flatten the install object into flags.** judgment: replicating nested media and
  network inputs would add a second interface to maintain.
- **Use only the existing worker join.** verified: at commit 157663607,
  `provisioning/operation.py` defaults the body wait to zero and computes the deadline
  inside the body. Waiting for the worker alone allows just the initial poll.
