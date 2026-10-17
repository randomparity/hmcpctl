# Scoped volume-group selectors (#1697)

## Contract and architecture
Source: #1697, parent #1695 and the approved WORK:SCOPE annotation on #1697.
[ADR 0227](../../adr/0227-scoped-volume-group-selectors.md) records the boundary.
C1: VIOS-centric storage commands keep VIOS positional. VG-bearing disk,
repository and media read/write/upload commands take `--vg NAME_OR_UUID`;
media object arguments and map `--disk` become `--name`. Mount/unmount take
`--lpar` and `--name`.
Upload retains positional ISO_SOURCE. Mapping IDs remain positional identities.
C2: attach-disk keeps positional LPAR with `--vios`/`--vg` names or UUIDs.
Its CLI adapter resolves VIOS with optional system containment, then VG, before
constructing the existing UUID-only ProvisionStorage and calling the workflow.
The provisioning VG preflight delegates to the same unique-identity resolver
after requiring a UUID, so direct attach/provision dry-runs retain that contract.
C3: storage/resources.py owns `resolve_volume_group_uuid(hmc, vios_uuid, value)`.
It reads only that VIOS's existing list_volume_groups feed. Canonical UUIDs match
case-insensitively; other values match GroupName exactly. Names require complete
name information to exclude hidden duplicates; matching rows require canonical
UUID identity. UUID selection requires exactly one matching complete UUID.
Errors identify the selected VIOS, provided selector, candidate names/UUIDs and
list-vgs recovery. Empty/duplicate/incomplete inventories refuse before mutation.
Do not parse capacity or free space to resolve identity. Propagate failed reads.
C4: Existing operations resolve VG before their client VG calls, including reads
and upload. Preserve upload URL validation before HMC access, guards and upload
handle cleanup. Low-level UUID/path validation, If-Match, mapped/mounted refusal,
LPAR ownership and optional system containment remain existing owners.
C5: CLI help, docs/cli.md old-to-new examples, affected recipes and behavioral
fixtures migrate together. No compatibility aliases or LPAR repository filtering.

## Global Constraints
Python 3.11 minimum; CI Python 3.11–3.14 on amd64. No new dependencies.
No live HMC operations. Keep client APIs and ProvisionStorage UUID-only.

## Failure model
- Actors and deployments: local authenticated CLI operators and MCP/library
  operation callers against HMC; offline mocked tests and hosted CI.
- Invariants and assets: wrong-VIOS writes can destroy storage; ambiguous names
  must not choose an arbitrary object. Existing guarded mutation paths remain.
- Accepted classes: concurrent inventory change between selection and write is
  bounded by existing client parent-scoped URLs and If-Match; no new transaction.
- Covered elsewhere: presentation #1699, consent #1703, terminal policy #1275,
  final journey #1704; repository filtering and client name APIs excluded.

## Threat model
- Boundaries added: operator VG names and HMC inventory identities enter resolution.
  Existing CLI selectors widen to exact names; transport does not widen.
- Actors/trust: operator input and HMC-returned identity fields need validation;
  configured HMC and existing ownership policy remain the authority.
- Controls: exact matching, complete identity, VIOS-scoped enumeration, canonical
  UUID check before passing onward; candidates use repr-safe text. Preserve client
  validation and ETag guards; lookup errors never include credentials.
- Out of scope: compromised HMC, new authorization policy, live hardware testing;
  existing TLS and client guards retain their established responsibilities.

## Validation
Focused resolver tests cover exact names, UUID case, absent/duplicate/malformed
identity, wrong-parent UUID, failed reads and no writes on rejection. Operation
integration covers disk/media/repository/upload routing; parser tests exercise
new syntax and reject old positional forms. Attach tests prove both selectors,
system containment and dry-run. Existing client RMW and operation refusal tests
prove guards remain. Parser-tested recipes cover executable documentation.
Run focused tests first, then just verify and pinned prek before push; CI separately.
