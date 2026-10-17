# Decommission mapping evidence

## Problem and scope
#1701, under #1695, requires actionable unresolved evidence and truthful partial outcomes.
`collect_storage_records` discards unresolved records, retaining a count; the CLI
stringifies nested step results. Extend these existing owners, with no ownership move.
C1–C7 refer to the frozen WORK:SCOPE for #1701, token q1701-87fcab9c.
Decision: [ADR 0232](../../adr/0232-unresolved-decommission-evidence.md).

## Global Constraints
Python 3.11–3.14; declared CI target amd64. No new dependencies. Preserve existing
JSON envelopes, mutation authorization, detach ordering and retained backing storage.

## Design
Add optional additive `mapping_evidence` to the blast-radius object, retaining existing
`storage_mappings` and both counts unchanged. Each scalar-string record contains known
VIOS UUID/name, mapping kind, observed UUID, supported mapping ID, backing device,
missing identity description, target relationship, disposition and reason. Unknown
values are explicit; no synthesized mapping identity. Use case-insensitive UUID identity for additive evidence, independently of legacy
matching; include every authoritative detach target and unresolved mapping; unrelated mappings remain excluded, unless existing vSCSI validation cannot
prove their client identity, in which case report unproven evidence without widening guards.
Unavailable source evidence retains known VIOS identity and reason, with unknown mapping
kind/identity; unavailable fleet discovery also gets an explicit source record.

Existing matching and `_vscsi_detach_inventory` remain mutation authority. Evidence
is observational only. Missing exact vSCSI ID or valid client UUID is unproven.
A fully proven dry-run target is planned detach only when inventory has no blocking
errors; blocked planned targets are retained. vFC mappings are retained by this workflow.
In actual results, successful detach entries become detached; an explicit attempted
failed mapping becomes failed (request outcome uncertain), and unattempted known targets
are retained. Unresolved relationship remains unproven. Backing storage stays retained
regardless of mapping disposition. No failed request is proof of absence.

CLI adds existing --format table|tsv|json and --json/-j selection before I/O.
JSON remains the dataclass envelope with additive evidence. Human/TSV rendering uses
existing print_inventory/VerbatimTable owners with scalar rows: Section, Item, Field, Value. Enumerated
paths flatten step dictionaries/sequences and evidence without container repr cells;
empty containers have explicit empty rows. Tables use one folding field/value column to prevent narrow-column ellipsis. Warnings and summary
go to stderr, including confirmation through existing confirm_on_stderr; TSV stdout is one header and escaped scalar rows. No output.py redesign.

For known vSCSI VIOS UUID, emit `hmcpctl storage list-mappings VIOS --system SYSTEM
--format tsv`, shell-quoting each argument, without --lpar. vFC inspection uses the
existing `hmcpctl raw get` path scoped to the observed VIOS UUID with the combined
ViosSCSIMapping,ViosFCMapping group query. Missing VIOS UUID falls back to scoped
`hmcpctl vios list --system SYSTEM --format tsv`, to discover identity before inspecting.
Explain that uncertainty proves neither another tenant nor permission to delete.
The CLI uses its supplied system selector and actual known VIOS UUID only. JSON
keeps these presentation-only commands on stderr beside warnings.

## Success and validation
C1/C2/C5: operation tests assert retained evidence, unchanged legacy inventories/counts,
no extra mutation calls, and planned/detached/failed/retained/unproven outcomes,
including uppercase linked UUIDs with lowercase target selectors.
C3/C4/C6: render dry-run and actual results for missing client, missing ID, unrelated
mapping, unreadable VIOS and partial detach. Parse inspection argv with the real CLI
at a stubbed transport boundary. Exercise narrow redirected TSV and explicit JSON/table;
full identifiers and scalar values survive, JSON has no preamble (also after accepted
confirmation), declined confirmation performs no I/O, ANSI help is unstyled.
C7: docs/cli.md and affected recipes explain format migration and explicit backing cleanup.
Offline fixtures prove this slice; no live HMC evidence is claimed.

## Failure model
- Actors and deployments: local terminal operators and redirected CLI automation;
  library/MCP consumers of the existing decommission result; HMC-provided mapping data.
- Invariants and assets: ownership and detach gates, retained backing data, truthful
  partial outcomes, unchanged established JSON keys/values, copyable known identities.
- Accepted failure classes: unavailable mapping data cannot be reconstructed; retain
  explicit unknowns and inspection guidance. Concurrent HMC changes remain governed by
  existing detach revalidation; evidence describes observed workflow, not future state.
- Covered elsewhere: common formats #1698; selectors #1696/#1697; final journey #1704;
  automatic backing cleanup and new detach authority excluded by #1695/#1701.

## Threat model
- Boundaries: existing HMC response to evidence to terminal/TSV/JSON; added displayed
  shell inspection argv assembled from known identifiers. No new execution boundary.
- Actors: HMC resource authors may supply hostile names/control characters; operator
  chooses connection and scope. Trust existing fetch and mutation guards only.
- Controls: reuse terminal-safe output and reversible TSV escaping; shlex.join for
  displayed argv; UUID paths only from validated known VIOS identifiers; never execute
  suggested commands. Preserve raw JSON data and existing authorization unchanged.
- Out of scope: transport authentication and mapping mutation races remain governed by
  existing clients; no new cleanup or permission grants.
