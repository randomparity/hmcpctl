# ADR 0135: Compose the LPAR Install Lifecycle from Thin CLI Adapters

## Status

> **Confirmation spelling superseded by [0231](0231-storage-explicit-consent.md)** (2026-10-16).

Accepted

## Context

Issue #776 requires an operator to complete an LPAR installation lifecycle using only the
installed `hmcpctl` CLI. Most steps already have commands, but optical-media mount and unmount
exist only as storage operations. Bounded LPAR console capture is `hmcpctl lpars capture-console`
(#959, ADR 0175).

The existing operations already own selector resolution, LPAR ownership checks, and optical mapping
semantics. The CLI must expose those contracts without creating a second implementation.

## Decision

Add `storage mount-optical-media` and `storage unmount-optical-media` as thin adapters over the
existing storage operations. Both accept VIOS, LPAR, and media selectors; both pass through the
optional managed-system selector and ownership override. Mount uses the storage group's ordinary
`--yes` confirmation. Unmount uses `--confirm` because it removes a mapping, while stating that the
backing ISO remains.

The recipe observes the boot with `lpars capture-console`, whose selector resolver maps a
partition UUID to its name with the SSH `lssyscfg` lookup. HMC CLI attributes are not REST element
names: the live run on 2026-09-23 showed that the lookup's `-F UUID,PartitionName` is rejected as
an invalid attribute. The SSH UUID-to-name lookups now send the CLI attributes `uuid,name`, which
the HMC CLI cheatsheet documents; that form has not yet run live.

Keep the lifecycle as documentation rather than a new composite command. Its steps create resources
whose identifiers and operator choices are visible between calls, and its failure recovery is to
inspect and resume from the first incomplete step.

## Consequences

- The installable CLI expresses every recipe step except three, which run as HMC CLI commands
  until their issues land: applying the new partition's profile (#939), writing REST-added
  adapters into that profile before a profile power-on (#981), and reading the partition
  description for ownership checks (#965). The 2026-09-23 live run found all three.
- Existing storage authorization and console capture safety remain the source of truth.
- The new commands are public CLI/help contracts and receive focused body, confirmation, output,
  selector, ownership, and help-shape tests.
- The recipe's console observation is non-interactive; it does not send installer input.
- The documented workflow is intentionally non-transactional. Completed resources remain after a
  later failure until the operator resumes or runs the labelled cleanup steps.

## Considered & rejected

- **Resolve a partition UUID through the REST partition read.** judgment: `main` already resolves it
  with the system-scoped `resolve_lpar_uuid` and the SSH lookup, so fixing that lookup's attributes
  repairs the one path instead of adding a second resolver.
- **Add one composite install command.** judgment: the requested copy-and-adapt recipe must expose
  identifiers, confirmation points, partial completion, and optional cleanup, which a new orchestration
  API would conceal while duplicating existing commands.
- **Document an out-of-band `mkvterm` command.** verified: issue #776 requires every published recipe
  command to exist in the installable CLI, and `hmcpctl lpars --help` lists `capture-console`.
