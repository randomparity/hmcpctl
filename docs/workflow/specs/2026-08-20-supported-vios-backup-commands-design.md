# Supported VIOS backup command design

Issue: [#289](https://github.com/randomparity/hmc-mcp/issues/289)
Decision: [ADR 0060](../../adr/0060-use-supported-vios-backup-commands.md)

## Goal and constraints

Replace three command forms proven absent on HMC V10R3 SP1060 and V11R2 SP1120 with the supported
VIOS backup CLI. Python 3.11 remains the floor. Host verification is arm64; declared targets are
amd64, arm64, and ppc64le; the host is included. Add no dependency, compatibility shim, live-HMC
mutation, authorization change beyond backup's approved non-exhaustive/all-targets classification
with both selector inputs retained as metadata, or full-image restore workflow. `just verify` is
the final local guardrail.

The three VIOS backup tools require HMC V10 or newer. IBM's V9.1.940 command inventory does not
contain the replacement commands, so no V8/V9 fallback is claimed or implemented. General V8–V11
project support continues for tools whose HMC commands exist on those releases.

IBM's command references and the issue's live-HMC evidence govern the exact shapes:

- `lsviosbk --filter "vios_uuids=<uuid>" -F name,type --header`
- `mkviosbk -t <vios|viosioconfig|ssp> -m <system-name> --uuid <vios-uuid> -f <backup-name>`
- `rstviosbk -t <viosioconfig|ssp> -m <system-name> --uuid <vios-uuid> -f <backup-name> [-r]`

## Public interfaces

Keep `hmc_list_vios_backups(vios_name_or_uuid, profile=None)` unchanged.

> **Later change (#1947):** `hmc_list_vios_backups` has since gained an optional keyword-only
> `system_name_or_uuid`, declared as a `managed_system` target selector, so its signature is
> now `hmc_list_vios_backups(vios_name_or_uuid, profile=None, *, system_name_or_uuid=None)`.

Replace the other signatures with:

```python
def hmc_backup_vios(
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
    *,
    backup_name: str,
    backup_type: BackupType = "vios",
    profile: str | None = None,
) -> str: ...

def hmc_restore_vios(
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
    backup_name: str,
    *,
    backup_type: Literal["viosioconfig", "ssp"],
    restart_if_required: bool = False,
    profile: str | None = None,
) -> str: ...
```

The order is the user-approved system, VIOS, backup-name shape. `backup_name` is keyword-only for
backup, and `backup_type` is keyword-only for restore, so the old maximum-arity positional calls
cannot bind to different replacement parameters in direct Python calls. MCP calls are named JSON
objects rather than positional calls; their old payloads fail schema validation because system,
backup-name, or restore-type fields are newly required. Restore type is required because no safe
default is evidenced. Backup retains `vios` as its default because that is the existing public
default and `mkviosbk` supports it. `restart_if_required` maps only to `-r`; false emits no flag.

## Resolution and command construction

Listing retains the existing VIOS-name-to-UUID resolution. Its builder changes to the supported
command, quotes the complete `vios_uuids=<uuid>` filter value, and requests the explicit `name,type`
projection with `--header`. Replace the speculative fixed-width parser with `csv.DictReader` over
the comma-delimited projection. Empty stdout returns `[]`; otherwise the header must be exactly
`name,type`, each data row must contain exactly those two nonempty fields, and malformed, duplicate,
or extra columns raise an actionable `ValueError` rather than silently reporting false inventory.
The return remains `list[dict[str, str]]` with keys `name` and `type` supplied by the HMC header.
Parse the original stream without `splitlines()` so quoted fields preserve embedded CR/LF
characters exactly.

Backup and restore use one async helper that resolves only selectors which need REST. A direct
system name remains the CLI `-m` value, and a VIOS UUID remains the CLI `--uuid` value, so a call
whose selectors are already CLI-ready does not require a REST login before SSH. When REST is
needed, build the selected profile's configuration once, construct the REST client from that exact
object, and pass the same object to SSH after resolution. A system UUID is fetched
once and converted to its `MachineTypeModelSerialNumber`; nested machine-type/model/serial fields
serialize as `tttt-mmm*sssssss`. An already rendered value must parse into exactly three nonblank,
unpadded machine-type, model, and serial components separated by the first `-` and `*`, then
re-serialize byte-identically; an arbitrary nonblank string is not an MTMS. Missing or malformed
MTMS fails before SSH rather than degrading a unique UUID to a possibly duplicated name.
The helper resolves `vios_name_or_uuid` through
`resolve_vios_uuid(..., system_name_or_uuid=system_name_or_uuid)`. The system CLI identity, VIOS
UUID, type, and backup name are each shell-quoted where they enter the SSH string. REST resolution
completes before SSH runs. An unknown system UUID or a VIOS name absent from that system fails with
an actionable selector error.

The existing backup-name validator becomes command-neutral and runs for backup and restore. Its
error describes the syntactic catalog-name contract without telling backup creation callers to
select an already existing entry. It
rejects empty or padded names, `/`, `\\`, dots-only values, and a leading dash. It deliberately
does not add IBM's documented length or character grammar, preserving ADR 0044's narrow-refusal
decision. Backup type retains `vios`, `viosioconfig`, and `ssp`; restore type accepts only
`viosioconfig` and `ssp`. Invalid values fail before REST or SSH.

## Error behavior and compatibility

Type and selector validation errors name the relevant operation, rejected input, and permitted
values or repair. The shared catalog-name error remains operation-neutral while naming the rejected
input and its syntactic repair. REST and SSH failures retain existing exception behavior. Raw
successful HMC stdout remains the return value for backup and restore; listing retains
`list[dict[str, str]]`.

This is a replacement, not a migration. Keyword-only boundaries make the old maximum-arity Python
positional calls fail binding rather than being reinterpreted. Newly required schema fields make
old named MCP payloads fail validation. Generated MCP descriptions and schemas must show the new
requiredness and must contain no old command names. Existing Python exports remain under the same
function names. Rendered descriptions state the HMC V10+ floor before a caller chooses the tool; no
runtime version probe is added because no verified older fallback exists.

## Threat model

### Boundaries and actors

Authenticated MCP, CLI, and Python callers control system, VIOS, type, name, restart flag, and
profile. REST responses supply resolved HMC names and UUIDs. Those values cross into an SSH command
executed with configured HMC credentials. This design widens the existing command-building boundary
with system name, backup name on creation, restore type, and restart choice; it adds no new entry
point. The authenticated caller is untrusted for command text and target choice. The configured HMC
and its REST identity data are trusted peers; credentials are trusted configuration.

### Controls

- Exact `Literal`/set validation bounds both type arguments before external calls.
- The existing narrow catalog-name validator prevents option and path-shaped file values.
- Existing system/VIOS resolvers bind a VIOS name to the explicit managed-system scope.
- UUID-to-MTMS conversion preserves a unique managed-system selector when user-defined names
  collide and fails closed if the unique CLI identity cannot be obtained.
- `shlex.quote` encodes every caller-controlled or HMC-returned string as one remote-shell word.
- Existing tool metadata and dispatch authorization govern targets. Backup exposes required
  `managed_system` and `vios` selectors for extraction, diagnostics, and audit. Backup and restore
  are both non-exhaustive because `ssp` can affect a cluster beyond those selectors, so the current
  policy model authorizes either tool only through `targets = "all-targets"`; the grant may reach
  an operation by explicit tool name or effect class. Target tables cannot authorize them. Current
  operator guidance recommends explicit tool names for least privilege.
- Errors may disclose public selectors and HMC diagnostics but never credentials.

No authorization-model change beyond backup's approved non-exhaustive classification is claimed.
Races with
another HMC operator, rollback of backup or
restore, availability of catalog entries, HMC-side retention, and full-image restoration are out of
scope. Live mutation is excluded; mocked exact-command tests plus the recorded HMC help are the
available proof.

## Verification

Focused tests must first fail against the old commands and signatures, then pass after the change.
They cover exact list filtering, direct system-name and UUID-to-MTMS resolution, duplicate-name
safety, every missing and blank nested MTMS component plus malformed flattened MTMS failure,
system-scoped VIOS-UUID resolution, required selector metadata plus non-exhaustive all-targets
authorization and narrow-table rejection,
all valid backup types,
both restore types, required restore type, optional `-r`, invalid type/name refusal before external
calls, rejection of both legacy maximum-arity Python positional forms and both legacy named MCP
payloads before REST or SSH, shell quoting for every dynamic field including hostile direct system
names on both mutation commands, raw-output preservation, profile routing, destructive
scope forwarding, and rendered lifecycle/schema descriptions. Sweep all repository callers so no
old positional form or command spelling remains outside historical design records that explicitly
describe the defect. Pin the HMC V10+ floor in README, cheatsheet, and all three rendered tool
descriptions. Run `just test`, `just smoke`, and bare `just verify`.
