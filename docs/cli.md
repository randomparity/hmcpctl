# CLI guide

[Documentation index](index.md) · [CLI quick start](../README.md#cli-quick-start)

Configure a connection first using the [configuration guide](configuration.md).
Use `hmcpctl --help` and `hmcpctl <group> --help` to discover commands and options.
The [HMC CLI cheatsheet](hmc-cli-cheatsheet.md) covers the underlying IBM commands
used over SSH; this guide covers the `hmcpctl` application.
The [bare-CEC LPAR recipe](recipes/bare-cec-lpar.md) walks a dedicated-I/O partition from
create to delete.

## Discover commands and repair input

LPAR and storage help starts with inventory, then separates lifecycle, console,
configuration, disks, optical media and mapping tasks into panels. Use group help
before choosing a command, then command help for its required inputs.

Connection options belong before the group, including `--profile`, `--host`,
`--user` and TLS options. For example:

```bash
hmcpctl --profile sample systems list
hmcpctl --profile sample lpars list --system system1
hmcpctl --profile sample vios list
hmcpctl --profile sample storage list-vgs vios1 --system system1
```

Move `hmcpctl lpars list --profile sample` to
`hmcpctl --profile sample lpars list`. Usage errors now print concise stderr
text, usage and a `--help` hint, retaining exit 2. Missing required inputs are
listed together when the parser can determine them without executing the command;
unknown options and invalid values retain their own diagnostics. Resource misses
suggest CLI inventory commands. Reuse your root connection options for those
commands; a failed inventory read remains an error, not an empty result.

## Terminal text and machine streams

Human terminal fields display nonprintable characters as visible escapes: an ESC becomes
`\x1b`, a field newline becomes `\n`, and a bidi override becomes `\u202e`.
Printable Unicode and literal brackets remain unchanged. Application layout and colors remain;
stdout and stderr use their own actual terminal state, independently of color preferences.
Explicit human tables use the same field encoding even when redirected.

Raw REST responses use this protection on a terminal; redirected raw responses retain their
original text and existing trailing newline. Captured console bytes remain unchanged. JSON
escapes terminal controls through JSON encoding, so decoded values are unchanged. Explicit
TSV retains its documented reversible delimiter encoding even on a terminal; choose a table
for human display. Raw/capture files and explicit machine streams can contain controls, so
consumers displaying those bytes later must apply their own display policy (ADR 0229).

## Storage consent migration

The existing confirmation boundaries in `storage` use `--yes`/`-y` consistently.
`unmount-optical-media` and `detach-mapping` no longer accept `--confirm`:

```bash
hmcpctl storage unmount-optical-media vios1 --lpar aix1 --name install.iso --system sys1 --yes
hmcpctl storage detach-mapping vios1 vhost0/vtscsi0 --system sys1 --yes
```

Without `--yes`, these commands and the other prompted storage mutations require
terminal stdin. Piping `y`, closed stdin, JSON and redirected output do not grant
consent: noninteractive stdin exits 2 before dispatch and prints a Bash/Zsh retry
command retaining the supplied scope and target. Reuse the same root connection
options before `storage` (for example, `hmcpctl --profile lab storage ...`);
the diagnostic deliberately does not echo connection credentials. Nonprintable
arguments use shell ANSI-C quoting so the retry selects the original value safely.

On terminal stdin, the prompt identifies the supplied scope and target on stderr;
decline or EOF exits 1 without dispatch. `--yes` skips only confirmation: ownership,
containment, mapped-disk and mounted-media guards still apply. `--ownership-override`
remains a separate option. `attach-disk --dry-run` validates without consent or
mutation. `upload-iso` retains its existing no-prompt contract.

## CLI usage

```bash
hmcpctl console info                 # connectivity check / HMC version
hmcpctl systems list                 # table of managed systems
hmcpctl systems show <uuid>
hmcpctl systems summary <uuid>       # one-call summary: state, MTMS, firmware, LPARs, free resources
hmcpctl systems health               # issue-only fleet health; add --json for automation
hmcpctl report utilization --csv fleet.csv   # CPU/memory/disk/adapter allocation, every profile
hmcpctl report utilization --html fleet.html # the same survey as one offline HTML page
hmcpctl lpars list                   # all LPARs
hmcpctl lpars list --system <uuid>   # LPARs of one system
hmcpctl lpars show mylpar --system <uuid> --json  # scoped name or UUID
hmcpctl lpars state mylpar --system <uuid> # just "running", "not activated", ...
hmcpctl lpars summary mylpar         # one-call summary: state, RMC, memory, CPU, OS, adapters
hmcpctl lpars get-minimum-affinity-policy mylpar sys1 --json
hmcpctl lpars system-memopt-score sys1
hmcpctl lpars plan-memopt-scores sys1 --prioritize-name web --exclude-name batch
hmcpctl lpars plan-system-memopt-score sys1 --prioritize-id 3 --exclude-id 9 --json
hmcpctl lpars create web01 --system <uuid> --mem 8192 --vcpus 2 --procs 0.2
hmcpctl lpars modify web01 --mem 16384 --procs 2.0   # assign resources
hmcpctl lpars delete web01 --system <uuid> # destroy (must be powered off)
hmcpctl lpars decommission web01 --system <uuid> --dry-run   # preview blast radius
hmcpctl lpars power-on mylpar        # submits a PowerOn job (asks first)
hmcpctl lpars power-off mylpar --immediate
hmcpctl adapters list mylpar                    # network adapters (default type)
hmcpctl adapters list mylpar --type VirtualSCSIClientAdapter
hmcpctl adapters add-network mylpar --vlan 100  # add a NIC on VLAN 100
hmcpctl adapters add-vscsi mylpar --vios-id 1 --vios-slot 5   # skip before storage map: it creates the pair
hmcpctl adapters add-vfc mylpar --vios-id 1 --vios-slot 6
hmcpctl adapters delete mylpar --type ClientNetworkAdapter --uuid <adapter-uuid>
hmcpctl vios list
hmcpctl jobs show <job-id>
hmcpctl raw get /rest/api/uom/VirtualSwitch   # escape hatch, prints XML
```

Add `--json` to inventory commands for their full JSON result. The inventory
commands below default to lossless TSV when redirected and tables on a terminal. Composite workflows
such as `hmcpctl lpars decommission` and `hmcpctl lpars provision` return a
stable envelope with `workflow_completed`, ordered `steps`, `warnings`, and the
blast-radius or provisioning summary, which is friendlier for automation than
raw HMC payloads. Every entry is the parsed uom resource:
`{UUID, title, link, ResourceType, Resource}` where `Resource` is the flattened
XML (namespace-stripped, HMC bookkeeping attributes removed).

The memory-affinity planning commands are read-only `lsmemopt` calculations.
Repeat `--prioritize-name`, `--prioritize-id`, `--exclude-name`, or `--exclude-id`
to describe a scenario, using names or IDs consistently. Calculated scores are
predictions, not guarantees of placement, and these commands never start optimization.

The minimum-affinity policy command is also read-only. It first checks whether the managed
system advertises `POWER11` processor compatibility, then explicitly requests
`min_affinity_score` and `min_affinity_score_action` through `lssyscfg`. The score is validated
as an integer from 0 through 100 and the action as `none`, `warn`, or `fail`. Systems without
that capability remain usable and return an actionable `capability-unavailable` reason. This
surface does not provide a setter; portable snapshots record the policy only when supported.

Portable snapshots capture one named LPAR profile together with source identity and separate
timestamped placement and affinity observations:

```bash
hmcpctl snapshot capture sys1 aix1 default --output aix1.snapshot.json
hmcpctl snapshot validate aix1.snapshot.json
hmcpctl snapshot inspect aix1.snapshot.json
```

Capture refuses to overwrite an existing local file. Validation and inspection are local and
perform no HMC I/O. Snapshots do not expose a replay command; observation data is diagnostic and
never part of the replayable profile configuration.

A REST create gives the partition a current configuration, so no apply is needed and the
`apply_profile` step reports `skipped`. When the HMC creates the partition through `mksyscfg` (its
REST create was refused with HTTP 406 or a 400 `REST0001` payload rejection, or no memory or
processor value was given), `lpars create` then applies the new `default_profile` with `chsyscfg -o apply`, without powering
the partition on, and reports an `apply_profile` step. Until a profile is applied or the partition
is activated, it has no current configuration and REST adapter writes fail. `--no-apply` skips
the apply. Adapter changes made through REST after the apply live only in the current
configuration unless the partition's `CurrentProfileSync` is `On`. The adapter and mapping
commands print which after their result. `lpars power-on --partition-profile` warns once for
each current virtual SCSI, Fibre Channel or Ethernet client adapter the profile lacks, because
activating that profile removes it; the job is still submitted (#981).

A bounded console capture reads an LPAR's virtual console without sending it input:

```bash
hmcpctl lpars capture-console aix1 --system sys1 --duration 30 --output aix1.console.log
```

It stops at `--duration` seconds (at most 3600), `--max-bytes` (at most 1048576), or
`--idle-timeout` seconds of silence, writes the raw bytes to `--output` or to a redirected stdout,
and refuses an existing file or a terminal stdout. One stderr line reports the stop reason, byte
count and `released`. Exit codes ([ADR 0175](adr/0175-capture-console-exit-codes.md)): `0` the
capture finished and the console was released; `1` a lookup, SSH or HMC failure, a console held
by another session, a capture stopped by an error, or bytes that could not be written; `2` a
usage error; `3` the release was not proven, so run `rmvterm` on the HMC before another capture,
unless the line says another client ended the hold (`ConsoleHoldLostError`): leave that session.

`hmcpctl lpars decommission` enforces the ADR 0011 ownership token even for
`--dry-run`; use `--ownership-override` only after explicit operator approval.

### Running as a Python module

`python -m hmcpctl` runs the same program as the `hmcpctl` command, wherever the `app` extra is
installed, and takes the same arguments. Its usage lines name `python -m hmcpctl` instead of
`hmcpctl`. One difference matters for security: `-m` puts the current working directory first
on `sys.path`, so a `hmcpctl/` package or a module such as `typer.py` in the launch directory is
imported before the installed package, in a process that holds profile passwords and, for
`serve`, a granted access policy. Nothing inside the package can guard against this. Prefer
`hmcpctl` for a served process, and when you launch the module from a directory you do not
control, use `python -P -m hmcpctl` or set `PYTHONSAFEPATH=1`
([ADR 0128](adr/0128-l5-module-entry-point-launch.md)).

### End-to-end: give an LPAR a bootable disk

For the complete ISO provisioning and optical-boot lifecycle, see the [LPAR ISO installation recipe](recipes/lpar-iso-install.md).

```bash
# 1. create the partition
hmcpctl lpars create web01 --system <sys-uuid> --mem 8192 --vcpus 2 --procs 0.2

# 2. carve a virtual disk out of a VIOS volume group
hmcpctl storage list-vgs <vios-uuid>                       # find the VG + free space
hmcpctl storage create-disk <vios-uuid> --vg <vg-uuid> --name web01_root --capacity-mib 51200

# 3. map the disk to the partition (the HMC creates the vSCSI adapter pair)
hmcpctl storage map <vios-uuid> --lpar web01 --name web01_root

# 4. (optionally) network + power on
hmcpctl adapters add-network web01 --vlan 100
hmcpctl lpars power-on web01
```

> **Note on the storage model**: an LPAR's vSCSI/vFC *adapter* is just
> plumbing — it pairs the partition with a VIOS server slot. `storage map`
> creates its own vSCSI client/server adapter pair, so do not run
> `adapters add-vscsi` first; an adapter added that way is left unpaired.
> `lpars provision` and `storage attach-disk` rely on that pair too. The
> actual *disk* lives on the VIOS or in a Shared
> Storage Pool: carve it out of a Volume Group (`storage create-disk`) or a
> Cluster/SSP (`cluster create-lu`), then connect it with a mapping
> (`storage map`). Both the VIOS **Volume Group / Virtual Disk** model and the
> **Cluster / SSP Logical Unit** model are wrapped. Once a disk is mapped,
> partitioning it into filesystems is the guest OS's job (NIM, cloud-init,
> `mkfs`), not the HMC's.

## Cleanup journey: inspect, decommission, then choose backing files

Install the `app` extra and expose `hmcpctl` using [README installation](../README.md#install).
Configure the intended connection as described in [configuration](configuration.md).
Put connection options before the group: `hmcpctl --profile NAME systems list`.
Use that same profile for every command below. Start with discovery:

```sh
hmcpctl --help
hmcpctl lpars --help
hmcpctl storage --help
hmcpctl systems list --json
```

Choose the intended system from that output and copy its full UUID into `SYSTEM`.
Then carry each observed identifier forward; these shell variables are deliberate
operator selections, not instructions to choose the first inventory row.

```sh
SYSTEM='<selected system UUID>'
hmcpctl lpars list --system "$SYSTEM" --json
hmcpctl vios list --system "$SYSTEM" --json
LPAR='<selected LPAR UUID>'
VIOS='<selected VIOS UUID>'
hmcpctl lpars show "$LPAR" --system "$SYSTEM" --json
hmcpctl storage list-vgs "$VIOS" --system "$SYSTEM" --json
VG='<selected VG name or UUID>'
hmcpctl storage list-optical-media "$VIOS" --system "$SYSTEM" --vg "$VG" --json
hmcpctl storage list-mappings "$VIOS" --system "$SYSTEM" --json
hmcpctl lpars decommission "$LPAR" --system "$SYSTEM" --dry-run --json
```

Inventory JSON for LPARs and VIOS has `entries` and `unreadable_systems`; do not
interpret an incomplete fleet read as proof of absence. Explicit system scope
constrains UUIDs as well as names. A duplicate name or a UUID outside the chosen
system is a refusal to investigate, not a reason to drop `--system`.

Read the dry-run's `blast_radius.mapping_evidence` and `steps`. Match the intended
disk and ISO names to the observed VIOS/VG and their LPAR relationship; repository
membership alone does not establish ownership. Save those exact names before
removing the LPAR. A `planned detach` is a plan, not a completed change. For
`unproven` evidence, run the emitted `Inspect:` command from stderr without adding
`--lpar`: the filter could hide the unresolved mapping. Missing client or mapping
identity is neither proof of another tenant nor permission to delete. Stop until
that evidence is resolved; `--yes` does not bypass containment or ownership checks.

After reviewing the plan and obtaining permission for the intended destruction:

```sh
hmcpctl lpars decommission "$LPAR" --system "$SYSTEM" --yes --json
```

Check the exit status and `workflow_completed`, `resource_deleted`, and each
step's status. Partial failure can leave earlier mapping detaches completed while
later steps are failed or skipped; inspect actual state before any retry. Do not
continue to backing-file deletion merely because some steps succeeded.
Decommission retains backing storage. Once the LPAR cleanup is complete, make
separate explicit decisions about the exact unused disk and ISO:

```sh
DISK='<verified intended disk name>'
ISO='<verified intended ISO name>'
hmcpctl storage list-mappings "$VIOS" --system "$SYSTEM" --json
hmcpctl storage delete-disk "$VIOS" --system "$SYSTEM" --vg "$VG" --name "$DISK" --yes
hmcpctl storage delete-media "$VIOS" --system "$SYSTEM" --vg "$VG" --name "$ISO" --yes
hmcpctl storage list-optical-media "$VIOS" --system "$SYSTEM" --vg "$VG" --json
```

Mapped disks and mounted media are refused even with `--yes`. Leave unrelated
files and the shared VG/repository in place. Storage mutation summaries report
request outcomes; they do not independently prove final hardware state. Add
`--json` to parse a mutation result; redirected inventory defaults to lossless TSV.
For non-TTY storage input, use explicit `--yes` after authorization: piping `y`
or selecting JSON is not consent.

Earlier commands used positional VG/media arguments and sometimes `--confirm`.
Use the exact replacements in [selector migration](#storage-selector-migration)
and [consent migration](#storage-consent-migration); `delete-media` is the ISO
command. Replace `--no-json` with `--format table` or `--format tsv` as explained
in [inventory migration](#inventory-output-and-migration). Decommission's flat
rows and retained JSON envelope are covered by [evidence migration](#decommission-evidence-and-output-migration).

The automated journey in `tests/app/test_cli_e2e.py` uses real subprocess I/O,
synthetic HTTPS responses, and a substituted SSH command transport. It proves
offline composition, including guard refusals and injected partial failures;
it is **not live HMC evidence**. Hardware verification remains outstanding until
a separately authorized window follows [live testing](live-testing.md), including
preflight, the named arm, recovery, and commit-stamped generated evidence.

## Inventory output and migration

These commands accept `--format table|tsv|json` and the JSON alias `--json`/`-j`:
`systems list`, `show`, `summary`, `health`, `capacity`, `find-placement`;
`lpars list`, `show`, `summary`, `refcodes`; `vios list`; and
`storage list-vgs`, `get-media-repo`, `list-optical-media`, `list-mappings`.
Selection defaults to `table` when stdout is a terminal, `tsv` otherwise.
An explicit format wins. `--json --format json` agrees; combining `--json` or
`-j` with `table` or `tsv` fails with exit 2 before connecting. As with other
single-value CLI options, repeating `--format` uses its last value.

```bash
hmcpctl lpars list > lpars.tsv                 # full, unwrapped identifiers
hmcpctl lpars list --format table             # explicitly formatted for people
hmcpctl lpars show mylpar --json > lpar.json   # full original document
hmcpctl storage list-mappings vios1 -j > mappings.json
```

`lpars show --no-json` has been removed: it previously had no effect. Replace it
with `--format table` or `--format tsv`. Scripts that relied on implicit JSON
from `lpars show` or `systems show` must add `--json`. Scripts scraping Rich
borders should consume TSV or explicitly request `--format table`; tables can
still fold identifiers at narrow widths. The JSON mode keeps existing documents,
typed projections, and `entries`/`unreadable_systems` envelopes; it does not add a
second compact JSON schema. Existing JSON capture recipes already select `--json`.

TSV uses the following headers in this order, with literal tabs separating columns:

| Command | Columns |
| --- | --- |
| `systems list` | Name, UUID, State, MTMS, IP Address |
| `systems capacity` | System, UUID, Total Mem (MiB), Assigned Mem (MiB), Free Mem (MiB), Total Procs, Assigned Procs, Free Procs, Running LPARs, Total LPARs |
| `systems find-placement` | System, UUID, Free Mem (MiB), Free Procs, Running LPARs |
| `systems health` | Category, Name, UUID, State, RMC, System, System UUID |
| `lpars list` | System, System UUID, Name, ID, UUID, State, Type, OS, RMC |
| `vios list` | System, System UUID, Name, ID, UUID, State, Version |
| `lpars refcodes` | Partition, Timestamp, Code, Definition |
| `storage list-vgs` | Name, UUID, Free (GiB), Capacity (GiB), Diagnostic |
| `storage list-optical-media` | Media Name, Size (MiB) |
| `storage list-mappings` | Mapping ID, Client LPAR, Backing Storage, Type |
| `systems show`, `systems summary`, `lpars show`, `lpars summary`, `storage get-media-repo` | Field, Value |

Single-object commands emit one row for each top-level field, in document order.
Nested values use compact JSON within the cell; decode TSV escaping before parsing
that JSON. `Resource.RepositorySize` in `get-media-repo` is measured in GiB;
summary field names identify memory units as MiB. Tables show the same projection.

TSV always emits its header, including for zero results, and terminates each record
with LF. It emits no titles or presentation styling, and does not wrap with terminal
width. Empty strings are empty cells; absent or null values are the exact token
`\N`. Literal backslashes are doubled, then tab, LF and CR become `\t`, `\n`
and `\r`. Thus a literal `\N` value is encoded as `\\N`, distinct from null.
Decode `\N` as null first, then scan other cells once left-to-right, replacing
`\\`, `\t`, `\n`, `\r` with their single-character values. Do not repeatedly
apply replacements: a literal backslash followed by `n` is not a newline.
Zero and false are values, not missing; Unicode and brackets are retained.

Empty collections exit 0, with an informative notice on stderr for table/TSV;
JSON retains its original empty collection or envelope. Missing named systems or
LPARs still exit 1. Partial fleet reads also exit 0 and warn on stderr. Retain
stderr with TSV to distinguish empty successful inventory from skipped systems;
JSON additionally retains `unreadable_systems`. Health and summary warnings go to
stderr even when selecting JSON.

Unscoped LPAR/VIOS results add `ManagedSystem: {"UUID": ..., "SystemName": ...}`
to each entry. This metadata comes from the parent inventory already fetched,
without an extra request per row, and does not alter `Resource`. State filtering
retains it. Scoped listings keep their prior JSON entries; their parent columns
are missing when the entry lacks this metadata. `systems health` uses categories
`systems`, `vios`, `lpars`; system rows have missing parent/RMC cells.

`lpars state` remains one scalar state line. Refcode JSON retains raw code and
definition data; its table path keeps terminal sanitation. Console capture and
raw REST streams keep their existing byte-stream contracts. Storage mutations and
decommission use the result conventions below.

## Decommission evidence and output migration

`lpars decommission` now uses a table on a terminal and lossless TSV when redirected.
Use `--format table|tsv|json` explicitly, or `--json/-j` for the established JSON
envelope. Conflicting format choices fail before I/O. Summaries, warnings and
confirmation go to stderr. TSV has `Section`, `Item`, `Field`, `Value` columns;
steps use their names, mapping items use zero-based display indexes (not mapping IDs),
and nested step fields use dotted keys and `[index]` paths. Empty collections read
`empty`; missing scalar values and escaping follow the inventory TSV convention.
Tables fold long scalar values rather than ellipsizing nested records.

JSON retains existing fields/counts and adds `blast_radius.mapping_evidence`.
Evidence includes known VIOS, mapping kind, observed UUID, supported mapping ID,
backing device, missing identity and disposition. `unknown` means unavailable evidence,
not an invented identifier. A vSCSI mapping ID uses the supported server/target-device
names; HMC mappings need not have UUIDs. `planned detach` is dry-run only; `detached`
means its detach returned successfully; `retained` means no detach was attempted;
`failed` means the request failed and its resulting mapping state remains unproven;
`unproven` identifies incomplete identity/inventory. Backing storage stays retained.

```sh
hmcpctl lpars decommission web01 --system frame-a --dry-run --format tsv
hmcpctl storage list-mappings <vios-uuid> --system frame-a --format tsv
hmcpctl lpars decommission web01 --system frame-a --yes --json
```

Use the emitted inspection command for the observed VIOS. Do not add `--lpar` when
investigating unresolved mappings: that filter can hide them. vFC/source inspection
uses a UUID-scoped `raw get` with both mapping groups; missing VIOS identity instead
requests scoped VIOS discovery. JSON emits these commands on stderr. Put connection
options such as `--profile NAME` before the group when selecting a nondefault HMC.
Uncertainty is neither proof of another tenant nor permission to delete. Resolve the
identity and guards before retrying; backing-disk/media cleanup remains a separate,
explicit decision, never an automatic consequence of a successful decommission.

## Storage mutation output migration

The thirteen mutations in `storage` (`create-vg`, `create-disk`, `delete-disk`,
`attach-disk`, `map`, `create-media-repo`, `create-media`, `delete-media-repo`,
`delete-media`, `mount-optical-media`, `unmount-optical-media`, `detach-mapping`,
`upload-iso`) use concise results: tables on a terminal, lossless `Field`/`Value`
TSV when redirected, or explicit `--format table|tsv|json`. `--json/-j` selects
JSON; conflicting format choices fail before contacting the HMC.

Results identify the operation, target and provided parent scope. Most storage
operations discard HTTP completion status, so `request_succeeded` means the
operation returned without error, **not that final hardware state was verified**.
This includes accepted requests and idempotent no-ops; inspect inventory to verify
final state. A returned job reports `submitted`, `completed`, or `failed` from
its status, retaining its identifier/status/error. Failed jobs exit nonzero.
`upload-iso` reports `uploaded` only after its existing repository visibility check.
`attach-disk` distinguishes validation-only `dry_run` and `incomplete` workflows;
incomplete attachment remains exit 1 and shows step/error evidence.

Mapping results retain change-location information: a successful request does not
mean its changes reached a profile. Unmount/detach explicitly retain backing
storage. Existing confirmations write their prompt to stderr; the confirmation
options and safety rules are unchanged. Backend exceptions retain stderr and a
nonzero exit, rather than introducing a new JSON error envelope.

Old implicit raw VG/VIOS documents are no longer printed. Use `--details` for the
original operation value as one JSON document, including null or scalar returns;
it conflicts with table/TSV. **Detailed output may contain hardware identifiers.**
`attach-disk --json` and `upload-iso --json` deliberately retain their established
full result envelopes and fields (also via `--format json`), which may likewise
include hardware identifiers. Their human/TSV forms omit successful raw resources.

```bash
# Earlier: prose followed by a whole VG document. Now: one concise JSON result.
hmcpctl storage delete-disk vios-a --vg datavg --name disk01 --yes --json > result.json
# Explicit original result, only when required for diagnosis or an existing reader.
hmcpctl storage create-disk vios-a --vg datavg --name disk02 --capacity-mib 1024 --yes --details > details.json
# Preserve the established attach workflow envelope, including partial failures.
hmcpctl storage attach-disk web01 --vios vios-a --vg datavg --name disk03 --capacity-mib 1024 --yes --json
```

## Storage selector migration

VIOS-centric commands take one positional VIOS name or UUID. Use `--system` to
constrain that VIOS to a managed system, `--vg NAME_OR_UUID` for its volume group,
and `--name` for the disk or optical image. VG names match exactly within that
VIOS; missing, ambiguous or incomplete identities fail with candidate details.
UUIDs also require membership in the selected VIOS before a write.

These pre-release forms replace the earlier syntax (prefix each with `hmcpctl storage`):

| Earlier | Current |
| --- | --- |
| `get-media-repo VIOS VG` | `get-media-repo VIOS --vg VG` |
| `list-optical-media VIOS VG` | `list-optical-media VIOS --vg VG` |
| `create-media-repo VIOS VG --size-mib 20480` | `create-media-repo VIOS --vg VG --size-mib 20480` |
| `delete-media-repo VIOS VG` | `delete-media-repo VIOS --vg VG` |
| `create-media VIOS VG --name install.iso --size-mib 1024` | `create-media VIOS --vg VG --name install.iso --size-mib 1024` |
| `delete-media VIOS VG install.iso` | `delete-media VIOS --vg VG --name install.iso` |
| `upload-iso VIOS VG install.iso URL` | `upload-iso VIOS --vg VG --name install.iso URL` |
| `mount-optical-media VIOS LPAR install.iso` | `mount-optical-media VIOS --lpar LPAR --name install.iso` |
| `unmount-optical-media VIOS LPAR install.iso` | `unmount-optical-media VIOS --lpar LPAR --name install.iso` |
| `map VIOS --lpar LPAR --disk disk01` | `map VIOS --lpar LPAR --name disk01` |

`create-disk` and `delete-disk` already use `--vg` and `--name`; their `--vg`
now accepts an exact VG name as well as a UUID. Confirmation options are unchanged.
For example, `hmcpctl storage delete-disk vios-a --vg datavg --name disk01 --yes`
still refuses to delete a mapped disk. Mounted-media and If-Match guards also remain.

`attach-disk` is the target-oriented exception: its positional parent is the LPAR,
with both `--vios NAME_OR_UUID` and `--vg NAME_OR_UUID`, for example:

```bash
hmcpctl storage attach-disk web01 --vios vios-a --vg datavg --name disk01 --capacity-mib 1024 --dry-run
```

The media repository belongs to its VIOS/VG, so it has no LPAR filter. Inspect LPAR
relationships with `hmcpctl storage list-mappings vios-a --lpar web01`.

## Fleet utilization report

`hmcpctl report utilization --csv PATH` reads every profile in `config.toml`, or only those
named with repeated `--profile NAME`, and writes one CSV of CPU, memory, VIOS disk and adapter
allocation. It only reads; it changes nothing on any HMC. The accounting follows
[ADR 0184](adr/0184-fleet-utilization-accounting-model.md) and, for disk and adapters,
[ADR 0185](adr/0185-fleet-utilization-disk-and-adapter-accounting.md).

> **Both reports hold internal hostnames, system names and serial numbers. Never commit them or
> post them in a public place.** They are written with owner-only permissions.

`--html PATH` writes the same survey as one self-contained HTML page, instead of or beside the
CSV: give `--csv PATH`, `--html PATH`, or both, and both are written from one survey. The page
is stamped with the UTC time the survey finished and names every profile surveyed. It shows
fleet tiles for CPU, memory, disk, I/O slots, SR-IOV logical ports and idle (`not activated`)
partitions, the fleet notes, a per-HMC table, a per-system table sorted by clicking a column
heading, and every failed profile with its reason. Memory and disk show in GiB; the CSV keeps
exact MiB. A figure the survey could not read shows `unknown`, never 0. The page loads nothing
from the network: its style and sort script are inline and its content security policy blocks
any other script and every fetch, so it reads offline and prints.

The first column, `row_type`, says what each row is:

- `system`: one managed system. A system two HMCs manage appears once, with both profiles in
  `profiles`, and its most complete reading.
- `hmc`: the totals for one profile's HMC.
- `fleet`: the totals over the `system` rows.
- `failure`: a profile that could not be surveyed, with the reason in `notes`.

Each CPU (processor units) and memory (MiB) figure means:

- `installed`, `configurable`: the system's own totals.
- `vios`: the current configuration of the system's VIOS partitions.
- `client_active`: the current configuration of client partitions in any state except
  `not activated`.
- `idle_reserved`: the current configuration of `not activated` partitions. The hypervisor
  keeps it reserved, so it is allocated capacity that nothing is running on.
- `hypervisor` (memory only): memory the hypervisor itself uses.
- `other_reserved`: what remains of configurable capacity after free, the hypervisor and the
  partitions.
- `free`: what the system reports available.
- `allocated` and `util_pct`: configurable minus free, and that as a share of configurable.
- `dedicated`, `shared`, `shared_pools`: units held by dedicated and shared-processor
  partitions, and the shared pools in use.
- `profile_claims`, `profile_claim_mem_mib`, `profile_claim_cpu`: partitions that have never had
  a profile applied, and what their profiles would claim. The hypervisor reserves nothing for
  them, so these are not counted as allocated.

Disk and adapter columns follow `notes`:

- `disk_internal_*_mib` and `disk_san_*_mib`: the capacity of the physical volumes the system's
  VIOS report, as `total`, `assigned` and `free`. SAN means Fibre Channel- or iSCSI-backed. A
  volume two VIOS of one system both see counts once; it is assigned when either VIOS reports it
  unavailable for use (mapped, in a volume group, or otherwise in use), and free otherwise. Only
  VIOS-reported volumes count: NPIV-mapped LUNs and disks behind adapters a client partition
  owns are absent, and a system without VIOS reads 0. A LUN that VIOS of two systems see counts
  in both systems. `disk_util_pct` is assigned over total.
- `slots_assigned`, `slots_unassigned`, `slots_sriov`, `slots_empty`: physical I/O slots owned
  by a partition, occupied but unowned, holding an adapter in SR-IOV shared mode, and empty. An
  empty slot counts as empty even when a partition owns it. `slots_util_pct` is assigned plus
  SR-IOV over occupied slots.
- `sriov_adapters`, `sriov_logical_ports`, `sriov_logical_ports_free`: adapters in SR-IOV shared
  mode, the logical ports they support, and those not yet configured. `sriov_util_pct` is
  configured over supported ports.

An absent `IsISCSIBacked` (schema versions before V1_8_0) reads as not iSCSI-backed. A
`*_util_pct` is also `unknown` when its capacity is 0.

A VIOS that is not running reports no storage, so its system's disk columns are `unknown`, and
so are every system's when the HMC cannot read VIOS storage at all.

`unknown` means the HMC did not report a figure or a read failed; it is never written as 0. A
system row's `notes` names the failed read. A roll-up figure sums the systems that reported it,
and the roll-up row's `notes` names every column some of its systems lack, for example
`mem_vios_mib: 1 of 4 systems unknown`. A roll-up's percentage uses only the systems that
reported both configurable and free capacity.

The command surveys 4 profiles at a time (`--concurrency`) and gives each one 300 seconds
(`--hmc-timeout`) for logon and every read. A profile that runs out of time becomes a `failure`
row and keeps none of its readings. Ending its HMC session afterwards is bounded by the client's
own per-request `HMC_TIMEOUT` settings, not by `--hmc-timeout`. A profile whose reads all
finished keeps them even if ending its session fails; stderr carries a warning instead. A
system the HMC cannot list is absent from the report, with only a warning on stderr. The CSV
replaces `PATH` only once it is complete. A run killed by a signal other than Ctrl-C can leave
an empty, owner-only `.<name>.*.tmp` file beside `PATH`; delete it.

The command refuses to run when `HMC_HOST`, `HMC_USER`, `HMC_PASSWORD`, `HMC_PORT` or
`HMC_VERIFY_SSL` is exported, even as an empty value, or when a global connection option
(`--host`, `--user`, `--password`, `--verify-ssl`, `--profile`) is given. An exported value
overrides every profile's own, so one HMC's host, credentials or TLS setting would reach all of
them; the global options would be ignored. `HMC_TIMEOUT` and `HMC_SCHEMA_VERSION` still apply
to every profile.

When profiles fail, the `fleet` row's `notes` says how many and which: a system that only a
failed profile manages is missing from the fleet totals. It also says how many systems
reported no machine type-model-serial: those cannot be deduplicated, so one managed by two
HMCs counts twice. A roll-up over no systems reads 0. A text cell that would start with
`=`, `+`, `-` or `@` is written with a leading `'` so a spreadsheet shows it rather than
evaluating it.


### Managed-system scope for inspection

`lpars show` and `lpars state` now accept `--system NAME_OR_UUID` (or `-s`),
so inspection can carry the same scope as deletion. Without scope, ambiguous
names still fail. With scope, both names and UUIDs must belong to that system;
a UUID previously accepted with an unrelated system is now rejected. Correct
the system selector instead of retrying the mismatched target. This also applies
to shared LPAR/VIOS selectors used by lifecycle and storage commands.

`lpars delete` and `lpars decommission` require `--system` for scoped ownership
and cleanup checks. `lpars capture-console` requires it to select the console
host. A UUID or `--ownership-override` does not remove those requirements.
An unreadable system is reported as unavailable, rather than as an empty system.


### Stop a console capture early

Press Ctrl-C (SIGINT) during `hmcpctl lpars capture-console LPAR --system SYSTEM
--output FILE` and wait for cleanup. The command retains the flushed partial file;
it still refuses to overwrite an existing path. Exit 130 means the interrupted
capture's console release was proven. Exit 3 means release was not proven and
outranks 130 and write errors: inspect stderr before attempting another capture.
A reported write failure means the retained file is incomplete. Bytes remain raw,
including terminal escapes; outcome diagnostics go to stderr. If another client
ended the hold, leave that client's session alone. SIGTERM/SIGHUP and a standalone
release command are outside this contract. See [ADR 0235](adr/0235-console-interrupt-contract.md).
