# Running a live test against real HMC hardware

Use the release-window procedure below for a release candidate. Named-arm details
remain the reference for inspecting an authorized attempt and its recovery.

A live run **creates, mutates and deletes real partitions and profiles on a
managed system**. Nothing here runs in CI, and no `just` recipe reaches an HMC:
every command below is one you run deliberately.

## Before you start

You need a host with network reach to the HMC, credentials for it, and this
repository checked out with `just setup` already run.

Credentials resolve in this order, highest first:

1. `HMC_*` environment variables already set
2. `config.toml` in the platform config directory — the documented profile
3. a local `.env` file

That directory is `~/.config/hmcpctl` on Linux (or `$XDG_CONFIG_HOME/hmcpctl`)
and `~/Library/Application Support/hmcpctl` on macOS. The runner resolves it
per platform; when it finds nothing it prints the path it looked in.

Scenario settings — every `LIVE_TEST_*` key — come **only** from `.env`. That
file is git-ignored and must stay that way; it names the managed system a run
will create and delete partitions on.
`.env.example` lists every required key. Size the scratch partition's
processing units, `LIVE_TEST_SCRATCH_CREATE_DESIRED_PROCS` and
`LIVE_TEST_SCRATCH_CREATE_MAX_PROCS`, for its vCPU counts on your platform:
the create sends them explicitly, and the HMC refuses too few units per
virtual processor.

round2's provisioning subtasks (13 and 14) need two settings a fresh lab cannot
supply from its own partitions:

- `LIVE_TEST_PROVISION_VLAN_ID` — the VLAN for the provisioned partition's client
  adapter. It must already have a virtual network on `LIVE_TEST_SYSTEM_NAME`; the
  runner never creates one. Subtask 14 lists the virtual networks and stops before
  deleting anything when the VLAN is not there.
- `LIVE_TEST_PROVISION_DISK_MIB` — the size of subtask 14's temporary test disk,
  in MiB and a multiple of 1024.

## 1. Ask whether the run would start

```sh
uv run --no-sync python scripts/live_test_preflight.py --group dedicated
```

`--no-sync` is required on every command in this document. A bare `uv run`
prunes the `app` extra and the runner stops importing.

Preflight calls the same validators the runner gates on, so its configuration
verdict is the runner's. Exit 0 means the runner would start.

It also prints what each arm will touch:

```
predicted arms — the run decides; a RUNNABLE arm may still SKIP
  dedicated  RUNNABLE configured
    will mutate: managed system sys-R1
    will mutate: partitions named live-pcie-* (created, then deleted)
    will mutate: profile default io_slots (assigned, then restored)
    will mutate: dedicated slot 21010020
    envelope:    admitted (ADR 0053 envelope)
```

**Read that list before continuing.** It is the last point at which nothing has
been changed.

Two things preflight reports but does not block on: an unreachable HMC, and a
managed system outside the ADR 0053 envelope. Both make the arm SKIP, which is
correct behaviour, not a failure. Add `--skip-hardware` to predict from
configuration alone and contact no HMC.

For round2 it also prints a `provision VLAN:` line. `no virtual network on VLAN
<id>` means subtasks 13 and 14 will fail: set `LIVE_TEST_PROVISION_VLAN_ID` to a
VLAN that has one before running. This line does not change the exit status
either.

Preflight predicts. Only the run decides — an arm can SKIP where preflight said
RUNNABLE, because it checks preconditions against hardware at dispatch.

## 2. Run the authorized window

Live verification is a release stage, not a requirement after development merges.
Ordinary changes do not require a live rerun or a rerun issue. The operator may
separately authorize an ad-hoc development window; its catalog evidence counts at
release time only if the candidate closure and age gate still match. Such evidence
does not change the packaged release snapshot between releases.

For a release, name and freeze the candidate as [Releasing](development.md#freeze)
describes. Create a separate non-default worktree at that commit and run `just setup`.
At its root, use the normal runner configuration above and a clean tracked, staged
and untracked tree. `origin/HEAD` must identify the default branch. Commit the release's
claims file under `docs/capabilities/releases/` first; its validated triples determine
the arm list. The following uses `vX.Y.Z.json` as a filename placeholder.

Choose a new `promotion/` branch name and an existing physical mode-0700 directory
outside the checkout for private output. Preserve prior selected-arm results and
sidecars, and any prior round2/vmedia outputs, outside this attempt: the command
refuses them rather than risk using stale runner state. Do not run concurrent arms.

```sh
uv run --no-sync python scripts/run_release_window.py \
  --claims docs/capabilities/releases/vX.Y.Z.json \
  --branch promotion/release-X-Y-Z --output-dir "$WINDOW_PRIVATE" --dry-run
```

Set `WINDOW_PRIVATE` to that private directory before the command. Dry-run requires
valid local configuration but contacts no HMC, launches no arm and creates no output.
Its JSON contains private mutation descriptions: retain it locally. Have the operator
review the arm details below and the exact candidate, arms and mutation boundary.
Save only its `authorization` object in a private JSON file, set `authorized` to
`true`, and explicitly set `vios_access` to `true` when the selection includes round2,
vmedia, sriov, profiles, vios-backup, network, storage or lpar-power; otherwise keep
it `false`. Keep the remaining fields unchanged. The file must match the derived
candidate and boundary exactly; a general approval or dry run is not authorization.
Set `WINDOW_AUTHORIZATION` to that file's path, then run once per authorized attempt:

```sh
uv run --no-sync python scripts/run_release_window.py \
  --claims docs/capabilities/releases/vX.Y.Z.json \
  --branch promotion/release-X-Y-Z --output-dir "$WINDOW_PRIVATE" \
  --authorization "$WINDOW_AUTHORIZATION"
```

The command captures independent before-baselines for the selected systems, including
all discovered VIOS partitions there when a VIOS-bearing arm is selected. It then runs
each selected arm in named-wrapper order: preflight → arm → recovery. After successful
arms and clean recovery it captures and compares after-baselines, generates evidence,
promotes onto the new local branch and reads the claimed-set gate. No push or tag occurs.

An attempted arm gets recovery even after failure or catchable interruption, once
its child has terminated. A failed phase or non-clean recovery stops later arms and
promotion; drift or an unreadable baseline is not waivable by prose. SIGKILL, host loss
or an unreaped child cannot guarantee recovery: inspect the retained artifacts and
perform section 4 before another attempt. There is no automatic resume or rollback.
A failed promotion or final gate may leave an unpushed promotion branch to inspect.

Logs, before/after snapshots, filtered matrices and `summary.json` go in a new private
`window-` subdirectory beneath `WINDOW_PRIVATE`; raw results and observation sidecars
remain ignored at checkout root. Exit 0 means the window passed, 1 means a phase failed,
and 2 means configuration/authorization, execution setup or summary handling failed.
Read the private phase summary and logs; never infer clean hardware from process exit alone.
Review the promotion diff and actual evidence before publishing its PR. See
[Recording an observation](capabilities/README.md#recording-an-observation) for the
allowlist and snapshot rules. The tag still requires its own gate read.

### Named arms

The window invokes these wrappers itself. For a separately authorized manual arm,
use section 1's preflight, the named command, then section 4's recovery before producing
evidence or proceeding to another arm. Capture and compare independent baselines as
below. The table does not grant hardware or VIOS access.

| Arm | Command | Covers |
|---|---|---|
| round2 | `uv run --no-sync python scripts/live_round2.py` | subtasks 0–15 except 11 |
| vmedia | `uv run --no-sync python scripts/live_vmedia.py` | subtasks 16–22 |
| sriov | `uv run --no-sync python scripts/live_sriov.py` | subtask 23 |
| dedicated | `uv run --no-sync python scripts/live_dedicated.py` | subtask 24 |
| bare-cec | `uv run --no-sync python scripts/live_bare_cec.py` | subtask 25 |
| profiles | `uv run --no-sync python scripts/live_profiles.py` | subtasks 0, 4, 10 and 15 |
| users | `uv run --no-sync python scripts/live_users.py` | subtask 11 |
| vios-backup | `uv run --no-sync python scripts/live_vios_backup.py` | subtask 37 |
| pcm | `uv run --no-sync python scripts/live_pcm.py` | subtask 38 |
| network | `uv run --no-sync python scripts/live_network.py` | subtasks 2 and 9 |
| lpar-config | `uv run --no-sync python scripts/live_lpar_config.py` | subtask 39 |
| storage | `uv run --no-sync python scripts/live_storage.py` | subtasks 0, 3, 40 and 42 |
| lpar-power | `uv run --no-sync python scripts/live_lpar_power.py` | subtask 41 |

Each writes `test-results-<arm>.json`. Run one arm at a time: they share a
managed system, and a concurrent run makes the recovery check in step 4
ambiguous about which run stranded what.

`scripts/live_test_runner.py` is there for anything these do not cover; see its
`--help`.

### Reading the output

Rows print as they complete. **Row subtask ids go up to 42, while the ids you
can dispatch are 0 to 25 and 37 to 42.** That is not a bug: subtask 24 dispatches the whole
dedicated arm, and the arm records its internal phases as rows 26 through 34,
plus its io_slots scenario as row 36. A row numbered 31 is part of the arm you
asked for. Subtask 25 dispatches the
bare-cec arm, which records its own steps as row 35. It reuses the dedicated
arm's baseline, fixture-create and cleanup steps, so rows 29, 30 and 34 appear
in a bare-cec run too, with their dedicated-arm wording. Subtask 37 is the
vios-backup arm, and its rows carry its own id. Subtask 38 is the
pcm arm's, and it SKIPs in any other selection. Subtask 39 is the lpar-config
arm's, and it SKIPs in any other selection too, as does subtask 41, the lpar-power
arm's.

A SKIP is a result, not a failure. An arm SKIPs when a precondition is absent —
an out-of-envelope system, no unassigned slot, a capability the HMC refuses —
and that is the arm working.

### The inventory reads

Before it selects a slot, the dedicated arm reads the system's I/O and SR-IOV
inventory and records each read as an observation, scenario
`st29-pcie-inventory` (#630). It reads the slot listing twice (all slots, then
the `eth` class), the SR-IOV adapters, and the physical and logical ports of the
first adapter in SR-IOV mode. It also reads the vNICs of the first partition the
system lists one for. Every read is read-only, and none of them decides what
the arm selects or mutates. An empty SR-IOV or vNIC listing records an
`(empty)` row and no observation. No adapter in SR-IOV mode SKIPs the port
reads. The bare-cec arm
reuses the dedicated baseline without these reads.

The release-window command owns its independent before/after baseline sequence.
For an authorized manual arm, capture outside the repository before mutation and
again after recovery, using the same system and VIOS selectors:

```sh
uv run --no-sync python scripts/live_test_baseline.py capture \
  --system sys-R1 --output "$WINDOW_PRIVATE/before.json" --dry-run
uv run --no-sync python scripts/live_test_baseline.py capture \
  --system sys-R1 --output "$WINDOW_PRIVATE/before.json"
# Run the authorized arm and its recovery check, then:
uv run --no-sync python scripts/live_test_baseline.py capture \
  --system sys-R1 --output "$WINDOW_PRIVATE/after.json"
uv run --no-sync python scripts/live_test_baseline.py compare \
  "$WINDOW_PRIVATE/before.json" "$WINDOW_PRIVATE/after.json"
```

`sys-R1` is an anonymous system selector; replace it with the authorized system.
Use an existing private directory and new filenames. Capture reads system identity,
partitions/states, slot owners, every profile's `io_slots`, and SR-IOV adapters and
physical/logical ports. Add the same repeated `--vios-id ID` arguments to both captures
for authorized VIOS partitions; without them manual capture does no VIOS-side reads.
VIOS capture includes identity, vSCSI mappings/backing resources, volume groups and
volume rows. `--profile NAME` uses normal connection configuration and precedence.
VIOS capture refuses surrounding system-name whitespace and UUID-shaped system or
partition names because the shared resolver cannot distinguish them from resource UUIDs.

Keep other changes quiescent during these sequential reads. Dry-run opens no connections.
Snapshots contain private identifiers, are created mode 0600, and stay local; inherited
transport diagnostics also need redaction before publication. Existing files and symlinks
are refused. Validation rejects incomplete/malformed evidence. Comparison ignores row and
profile-slot ordering but preserves structural identity, UUIDs and state. Exit 0 means
capture succeeded or snapshots match, 1 means drift, and 2 means unreadable/incomplete
evidence. Comparison prints category names only. Drift or unreadable evidence holds the
window for operator inspection and recovery; the baseline script never mutates hardware.

### The io_slots scenario

After its reassign, the dedicated arm answers the #912 profile grammar
questions on its own fixture: row 36. It adds a second slot with
`is_required=1`, adds and removes a third through the assign and unassign
operations, removes the `is_required=1` slot with `//0`, then removes the last
and expects the profile to read `none`.

- It takes **two more slots** than preflight names: the first two, other than
  the arm's own, that no partition owns and no partition profile lists. Only the
  fixture's profile lists them, and only while the scenario runs.
- It SKIPs when `LIVE_TEST_DEDICATED_PCIE_DRC_INDEX` is set, because a pinned
  run mutates only the slot it names, and when fewer than two such slots exist.
- A failed step ends the scenario. The arm then removes the slots it added, and
  its cleanup deletes the fixture only when the profile is back at the baseline.
  Otherwise it prints a manual-recovery row, as for any other drift.

### The profiles arm

The profiles arm verifies the partition-property, profile, memory-pool and
affinity operations (#627). Subtask 10 changes each property it tests and
restores the value it read just before:

- the test partition's description;
- the first VIOS's `msp` flag;
- the processor compatibility mode of the test partition's default profile,
  unless ST0 read its `sync_curr_profile` as 1: the HMC refuses a profile change
  then, so the arm records the round trip as SKIP (subtask 15 skips its restore
  of that mode in every arm on the same condition);
- its `sync_curr_profile` setting, which it touches only while the partition is
  `Not Activated`.

It is the only arm that runs the system-wide profile backup and type-3
merge-restore. It backs up to `hmcpctl-live-st10`, a file in the HMC's
`/var/hsc/profiles/<serial>/` directory. That file stays there and is
overwritten by the next successful backup. The restore runs only when this
run's backup succeeded and both of its `lssyscfg` reads before the restore
passed; otherwise the arm records it as skipped.

The restore resets a not-activated partition's `resource_config` from 1 to 0,
even though it merges a backup taken moments earlier, so its observation fails
on that side effect. The arm then re-applies each such partition's current
profile, which leaves it `Not Activated`. A partition it cannot re-apply (no
current profile, a refused apply, or no `resource_config` in the read after
the restore) is a FAIL row marked `MANUAL RECOVERY REQUIRED` that names the
partition and the `chsyscfg ... -o apply` command to run.

If a run stops partway, restore by hand what it may have left changed. Use
the values in the run's baseline, `artifacts.lp3_baseline` in its results
document, which an interrupted run still writes:

- `chsyscfg -r lpar -m <system> -i "name=<lpar>,description=<original>"`
- `chsyscfg -r lpar -m <system> -i "name=<vios>,msp=<0|1>"`
- `chsyscfg -r prof -m <system> -i "name=<profile>,lpar_name=<lpar>,lpar_proc_compat_mode=<mode>"`
- `chsyscfg -r lpar -m <system> -i "name=<lpar>,sync_curr_profile=<0|1|2>"`
- `chsyscfg -r lpar -m <system> -o apply -p <lpar> -n <profile>`, for a
  not-activated partition whose `resource_config` the restore left at 0

If the restore itself failed or was interrupted, review the profiles before
anything else, then restore them with
`rstprofdata -m <system> -l 1 -f hmcpctl-live-st10`. Do not run the arm
again until the profiles are confirmed: its next backup overwrites that file.

### The users arm

The users arm verifies the user, task-role, resource-role and remote-access
tools (#632). It is the only arm that creates an HMC user, and HMC users are
global to the console, not to a managed system.

- It reads the user list, the task and resource roles, and the console's
  LDAP/Kerberos settings. It never writes remote-access settings.
- It then creates one user named `hmcpctl-live-<8 hex>`, with the `hmcviewer`
  task role and web and SSH remote access disabled. The password is generated
  in the run and never printed or written. The arm reads the user, changes and
  then clears its description, and deletes it by UUID.
- It creates nothing while any `hmcpctl-live-` user already exists: run the
  recovery check below first.
- Keep its terminal output private. If the HMC refuses the create and echoes the
  request, the in-process server logs that error, password included, to stderr
  before the arm can scrub it.
- An interrupted run can leave that one user. The recovery check names it by its
  prefix; remove it with `rmhmcusr -u <name>`.

`LIVE_TEST_TEST_USER_NAME` is retired. A `.env` that still sets it loads with a
notice; delete the line.

`LIVE_TEST_NETWORK_TEST_LPAR_NAME` is retired too: subtask 9 no longer uses a
network-test partition (#1361). A `.env` that still sets it loads with a notice;
delete the line.

### The network arm

The network arm verifies the virtual-network, client-adapter and VIOS-label
operations (#629). Subtask 2 only reads. Subtask 9 runs only in this arm; round2
and `all` skip it. Each round trip reads its own baseline first, re-reads after
every change whatever the call returned, and reverses the difference:

- **Preconditions.** The test partition must read `Not Activated`, or nothing is
  changed. The adapter and label round trips also need exactly one VIOS with a
  vSCSI server adapter toward the test partition (the serving VIOS); otherwise
  only the VLAN round trip runs.
- **VLAN.** It creates `hmcpctl-live-vlan<id>-<8 hex>` on the first VLAN in
  `LIVE_TEST_VLAN_RANGE_START`–`END` that no network uses, tries a second network
  on the same VLAN (expected refused; deleted if not), adds a client network
  adapter on that VLAN to the test partition, deletes an unknown adapter UUID
  (expected refused), then removes the adapter and the run's own networks. Any
  other network on that VLAN is reported, never deleted.
- **vSCSI and vFC clients.** Each first tries an add on the virtual slot the
  test partition's own vSCSI client uses (expected refused), then adds a client
  adapter to the test partition paired to the lowest server slot of the serving
  VIOS assigned to the test partition (a slot open to any partition is never
  used), checks the pairing, and removes it. The VIOS's
  server adapters and the test partition's storage mappings must be unchanged
  afterwards. With no such slot, that round trip SKIPs. Each vFC add
  takes a WWPN pair from the system's pool.
- **Labels.** On the serving VIOS's first FC port it sets
  `hmcl-<8 hex>`, tries the same on port `fcs9999` (expected refused),
  removes the label and puts back the original. It creates a vFC group label
  `hmcl-<8 hex>` (the HMC caps a group label at 16 characters), tries to create it again (expected refused), renames
  it with `-r` and removes it. A label the tools cannot write back exactly, or a
  label read the HMC refuses, SKIPs that round trip.

A reversal that fails or cannot be confirmed is a FAIL row marked
`MANUAL RECOVERY REQUIRED`, and nothing after it runs. The arm ends with one
`network baseline compare` row per baseline it read.

The recovery check witnesses subtask 9 from the baselines the run recorded:
no network on the run's VLAN (`artifacts.test_vlan_id`), whatever its name; the
test partition's client network, vSCSI and vFC adapters as before; the serving
VIOS's FC-port labels as before; and no vFC group label named `hmcl-*`.
Subtask 2 only reads. After an interrupted run (exit 2), check the same by hand.

### The vmedia arm

The vmedia arm verifies the media-repository, optical-media and mapping
operations (#1347). A VIOS holds one media repository, and the arm works in it
whoever created it, removing only what this run created:

- **Repository.** Subtask 16 reads every volume group for the repository. When
  none holds one, it creates one in `LIVE_TEST_VDISK_VOLUME_GROUP_NAME`, and
  subtasks 17 and 22 delete it again. An existing repository is never resized
  or deleted; its create and delete are then gaps that need a VIOS with none.
- **Protected test partition.** When `LIVE_TEST_PROTECTED_LPAR_NAMES` lists the
  test partition, subtasks 19 and 20 SKIP before any HMC call, naming the gap; the
  reads and teardown still run.
- **Blank medium (subtask 19).** The test partition must read `Not Activated`
  and the repository must have 1 GiB free. The arm reads its baselines (the
  repository's media and size, every storage mapping on the VIOS, and the vSCSI
  adapter rows of the VIOS and the test partition), creates
  `hmcpctl_live_<8 hex>`, mounts it to the test partition (the HMC adds a vSCSI
  adapter pair), tries to delete it while mounted (expected refused), unmounts
  and deletes it, and compares each read with its baseline. The HMC media-name
  pattern admits no hyphen, hence the underscores.
- **ISO (subtasks 18 and 20).** Only when the file at `LIVE_TEST_ISO_PATH`
  exists; the arm never fetches media. It uploads it as
  `<LIVE_TEST_ISO_MEDIA_NAME stem>_<8 hex><suffix>`, checks a same-name upload is
  refused, deletes it, then uploads it again to boot the test partition from it
  and powers the partition off again. Without the file, both subtasks SKIP and
  the partition is never powered on.
- **Mappings (subtask 21)** are read on the VIOS and for the test partition.

An adapter or mapping that differs from its baseline after the mount or the
unmount, or an unmount the arm cannot confirm, is a FAIL row marked
`MANUAL RECOVERY REQUIRED` with the command that clears it. The arm never
removes an adapter. Subtask 22 unmounts and deletes any medium of this run's
that is left, and nothing else.

### The storage arm

The storage arm verifies the volume-group, virtual-disk, mapping and cluster
reads and the disk lifecycle (#1348). Subtask 0 resolves the VIOS and the test
partition; subtask 3 reads the inventory; subtasks 40 and 42 run only in this arm.

- **Inventory (subtask 3).** The volume-group listing is compared with the VIOS's
  own `lsvg`. Clusters and shared storage pools are promoted only when the HMC
  lists some; an empty feed stays a plain row. An absent pool UUID must come back
  empty or not found, and a listed pool is read back.
- **Preconditions (subtask 40).** The test partition must not be in
  `LIVE_TEST_PROTECTED_LPAR_NAMES` and must read `Not Activated`, and
  `LIVE_TEST_VDISK_VOLUME_GROUP_NAME` must have 1 GiB free. Each failed
  precondition is a SKIP naming why.
- **Lifecycle.** The arm reads its baselines: the group's free space, its logical
  volumes through `viosvrcmd … -c 'lsvg -lv <group>'`, every storage mapping on
  the VIOS, and the vSCSI adapter rows of the VIOS and the test partition. It
  creates a 1 GiB volume `hpctl<8 hex>` and maps it to the test partition (the
  HMC adds a vSCSI adapter pair). A delete while it is mapped is expected to be
  refused. It then detaches the mapping, checks the volume survived, deletes it,
  and compares each read with its baseline.
- **Attach.** Only when the lifecycle left everything at its baseline: the same
  through `hmc_attach_disk_to_lpar`, then detach and delete.

A mapping the arm cannot confirm gone, or mappings or adapters that differ from
the baseline, is a FAIL row marked `MANUAL RECOVERY REQUIRED` with the command
that clears it. The arm never removes an adapter. It deletes the volume only
after a listing shows no mapping backed by it, except for the one guarded delete
it expects to be refused.

**Scratch volume group (subtask 42, #1370).** This step is disabled unless both
optional `.env` settings are supplied: `LIVE_TEST_SCRATCH_PV_NAME=hdisk<n>` and
`LIVE_TEST_SCRATCH_VG_NAME=hpvg<8 lowercase hex>`. Use the one physical volume
explicitly released by the operator and an absent group name for this authorized
run. Preflight discloses these exact names; it saves no state. Never choose a disk
from inventory automatically or reuse an existing group.

Before create, the arm compares REST group names with VIOS `lsvg` and captures
`lspv -field pvname pvid vgname -fmt :`,
`lspv -free -field pvname pvid size -fmt :`, and `lsvg`. A successful free-listing
literal `":"` also means an empty free inventory. The explicit disk must appear free, in no
group, with the same identified PVID in both listings. The arm
records its pending group before one `hmc_create_volume_group` call and reads
back through REST and VIOS even after a refused or lost create response. It never
retries create (ADR 0136).

Cleanup uses `viosvrcmd -m <system> --id <VIOS id> -c 'reducevg <run-group> <explicit-PV>'`
only after `lsvg -pv <run-group> -field pvname -fmt :` proves exact single-PV
membership, `lsvg -lv <run-group>` proves no logical volumes, and the complete
PV inventory differs only by that disk's new group with its original PVID.
There is no force flag, root command, metadata repair or product delete-group API.
Plain `reducevg` removes the group when its last PV is removed. A passing command
is insufficient: REST/VIOS absence and exact before/after snapshots must match.

Unreadable state, unsafe membership, refused cleanup or snapshot drift fails with
`MANUAL RECOVERY REQUIRED` and retains the pending artifact. Recovery reports a
run-owned scratch group or unresolved restoration even when REST lists no group;
every invocation validates and restores its existing results before client setup.
Only selected runs borrow other-arm fallback context; rejected reports stay unchanged.
Compare durable run results and private before/after snapshots with independent reads before any
manual removal. Do not retry create or repair metadata automatically. Run recovery
after every attempt. A second create requires a separate operator allowance.

### The vios-backup arm

The vios-backup arm verifies the VIOS backup catalog, a `viosioconfig` backup
and its restore (#1349) on the VIOS serving `LIVE_TEST_LPAR_NAME`. It runs only
when dispatched as its own group; `all` and a bare run skip it.

Before it, an operator confirms with read-only commands where that VIOS's
management IP address sits relative to its Shared Ethernet Adapter, and rules on
whether to go ahead when it is on the SEA. The arm records the interfaces but
does not gate on them. Because a restore can rewrite that SEA, the way back is
the HMC console, so the arm guards on it instead.

- **Preconditions.** The test partition is `Not Activated`, it is the only
  non-VIOS partition on the system, and exactly one VIOS holds exactly one disk
  mapping toward it. That VIOS's RMC state reads `active`, and it has a virtual
  serial server adapter the HMC can open a console on. Otherwise the arm SKIPs
  and changes nothing.
- **What it changes.** It backs up the VIOS's I/O configuration to
  `hmcpctl-live-st37-<8 hex>`, removes the test partition's disk VTD (the backing
  logical volume stays), and restores the backup with `-r`, which lets the HMC
  restart the VIOS. Run it with `HMC_SSH_TIMEOUT=2400` exported: the restart
  happens inside one `rstviosbk` call. Preflight refuses a lower value.
- **What it checks.** The disk mapping is back on the same server adapter with
  the same backing device; the VIOS's mapping list and its `lsmap -all`,
  `lsmap -all -net`, `lsmap -all -npiv` and `lsdev -virtual` listings equal the
  baseline, line order aside. If the mapping is not back, it recreates it with
  `mkvdev` and records the restore as failed.
- **Cleanup.** hmcpctl has no backup-removal tool (#698). The arm removes its
  backup through `hmc_run_command` only when a final read equals the baseline:
  the REST mapping set, the four listings, and the VIOS's own
  `lsmap -vadapter`. After the restore it waits, up to 2400 s, for RMC to read
  `active` and the VIOS to answer `ioslevel`. When that never happens, the
  restore call ended without an exit status from the HMC (a timeout or a dropped
  session), or a read failed after a change, it recreates nothing and changes
  nothing more. In those cases, and whenever the final read is off the baseline,
  it keeps the backup: it is the way back. The recovery check then reports
  `VIOS off baseline, backup kept` (exit 1), never the backup's bare removal. Recover the VIOS
  through its HMC console first, then remove the backup by hand, as the recovery
  check prints:

  ```sh
  rmviosbk -t viosioconfig -m <system> -p <vios> -f hmcpctl-live-st37-<8 hex>.tar.gz
  ```

  Use the name `lsviosbk` lists: the HMC catalogs a backup made with
  `-f <name>` as `<name>.tar.gz`, and `rstviosbk` and `rmviosbk` refuse the bare
  name with `HSCLC455`.

  and recreate a missing mapping with
  `viosvrcmd -m <system> -p <vios> -c "mkvdev -vdev <backing> -vadapter <vhostN> -dev <vtd>"`.
  The names are in the results document's `artifacts.vios_backup_*` fields.

### The pcm arm

The pcm arm verifies `hmc_set_pcm_preferences` (#634). Subtask 38 changes the
managed system's PCM collection preferences, which every PCM consumer of that
system shares. It reads the five flags (`LongTermMonitorEnabled`,
`AggregationEnabled`, `ShortTermMonitorEnabled`, `ComputeLTMEnabled`,
`EnergyMonitorEnabled`) and records that read as the row
`hmc_get_pcm_preferences (snapshot)` before its first write. Then, for each flag,
it sets the opposite value, reads it back, and writes all five snapshot values
again: the HMC couples the flags, and enabling aggregation also enables
long-term monitoring and, where the system supports it, energy monitoring. It
reads the flags back after each restore. If the snapshot has aggregation off and
long-term or energy monitoring still reads on instead of its saved off value,
it makes one ordered restore attempt: turn aggregation off, read it back off,
then write all five saved flags and require an exact snapshot readback. A failed
read, refused restore write, or remaining mismatch stops further toggles and
requires manual recovery. It passes only when every restore succeeds and a final
read equals the snapshot. While
aggregation is on, the HMC holds long-term monitoring on, and energy monitoring on
when the system is capable, so the arm expects the HMC to accept those two
toggles and read them back unchanged. Any other flag that does not read back flipped fails its assertion.

Before the run, save a read of the flags outside the repository. A hang-up writes
no results document at all:

```sh
uv run --no-sync hmcpctl metrics prefs ManagedSystem <system> > ~/pcm-before.json
```

If the run fails or stops partway, restore the five values from that read, or
from the snapshot row in `test-results-pcm.json`, choosing each flag's on or off
form:

```sh
uv run --no-sync hmcpctl metrics set-prefs ManagedSystem <system> \
  --ltm|--no-ltm --aggregation|--no-aggregation --stm|--no-stm \
  --compute-ltm|--no-compute-ltm --energy|--no-energy --yes
```

If long-term or energy monitoring still reads back on after that restore while the
saved read has aggregation off, turn aggregation off first
(`hmcpctl metrics set-prefs ManagedSystem <system> --no-aggregation --yes`) and run the
five-flag restore again: aggregation holds both on.

Do not run the arm again until a fresh read matches the saved one. The next run
overwrites `test-results-pcm.json` and takes the flags as it finds them as its
snapshot.

### The lpar-config arm

The lpar-config arm verifies the LPAR configuration, DLPAR and boot-order
operations (#1345) on one partition it creates and deletes. It changes no other
partition: every mutating call names that partition's UUID.

- **Before.** It reads the system's partition names, free processing units and
  free memory (with the memory region size). It creates nothing while a
  partition named `hmcpctl-live-lpar-*` exists: that prefix is reserved for this
  arm, so run the recovery check first.
- **The partition.** `hmcpctl-live-lpar-<8 hex>`, ownership-stamped with caller
  token `lparcfg-<8 hex>` (the same hex): 1024/2048/4096 MiB, shared uncapped,
  0.1/0.5/1.0 processing units, 1/1/2 virtual processors.
- **Dedicated assignment (optional).** Existing `LIVE_TEST_DEDICATED_PCIE_*`
  settings must select this same system. Before creation, the arm reads the exact
  admitted release/model, all profile `io_slots` and dedicated inventory. It
  requires an unowned slot listed by no profile. An explicit DRC selects only
  that slot; otherwise it selects the first eligible slot. On the run's empty
  scratch profile it calls `hmc_modify_lpar` with `assignments.dedicated`, judges
  the completed delegate step and exact profile readback, then restores the
  profile before any SMS activation. It compares every other profile and the
  final all-profile/slot baseline. Failed or malformed reads remain FAIL;
  missing settings, no eligible slot or a valid unsupported environment are SKIP.
  SR-IOV/vNIC writes remain named authorization/hardware/RMC prerequisite gaps.
- **While Not Activated.** One `hmc_modify_lpar` (desired and maximum memory and
  desired units), then small, large, no-op and empty `hmc_dlpar_mem` and
  `hmc_dlpar_proc` requests, and one memory request above the maximum whose
  answer is recorded but never observed. It renames the partition to
  `<name>-rn` and back, sets a two-path pending boot order, and calls
  `hmc_clear_lpar_boot_order`, which must refuse (#1048).
- **Activated.** It activates the partition to SMS and makes one small memory and
  one small processor DLPAR request. These rows are never observations. The
  partition has no operating system, so it has no RMC connection: on V10R3 the HMC
  refused both with `HSCL7016` (the partition must be running), and the arm records
  each as a SKIP naming that gap; any other failure there stays a FAIL. The
  activation used the partition profile, which discarded the configuration changes
  made while it was Not Activated (#1170). DLPAR on a running operating system is
  therefore unverified. The resource observation covers only the resource path;
  the optional dedicated case emits its own observation after cleanup.
- **After.** It powers the partition off, deletes it by UUID only while its
  description still carries the run's caller token, and compares the system
  reads with the ones taken before (one re-read after 30 s on a difference).
  Observations are recorded with `cleanup` `passed` only when the delete is
  confirmed and the compare holds.

An uncertain dedicated restoration prevents subsequent cases, activation and
normal deletion. Recovery remains read-only: inspect the saved baseline, scratch
profile `io_slots` and dedicated ownership before removing the retained partition.
A teardown that cannot confirm the delete records a FAIL row marked
`MANUAL RECOVERY REQUIRED`. Check the partition's description for the caller
token, then run
`chsysstate -m <system> -r lpar -n <name> -o shutdown --immed` (when it is not
Not Activated) and `rmsyscfg -r lpar -m <system> -n <name>`.

### The lpar-power arm

The lpar-power arm verifies the LPAR power and lifecycle operations (#1346) on
partitions it creates and deletes, and one VIOS logical volume it creates and
deletes. It changes no other partition, volume or mapping: every mutating call
names a run partition (by UUID once it has one), the run's volume, or a mapping
backed by it. It SKIPs unless `HMC_AUTHORIZE_POWER_OPERATIONS=true`, so its power
evidence covers the ownership-guarded path, as bare-cec's does.

- **Before.** It reads partition names and states, free processing units and
  memory, I/O slot owners, and, on the one VIOS holding
  `LIVE_TEST_VDISK_VOLUME_GROUP_NAME`, its storage mappings, its vSCSI server
  adapters and the group's volume names. It creates nothing while a
  `hmcpctl-live-pwr-*` partition or an `lppwr*` volume exists: both prefixes are
  reserved for this arm.
- **Partition A** (`hmcpctl-live-pwr-<8 hex>`, caller token `lparpwr-<8 hex>`,
  the lpar-config arm's resources). Two creates hmcpctl refuses after its own
  reads and before any HMC write (processing units above the virtual processors;
  memory above the system's configurable memory; a create the guard lets through
  is a FAIL and is removed by the teardown's prefix sweep) and a duplicate-name
  create, then: activation to
  SMS through the partition profile, a second PowerOn while activated (it must
  report `already_running` and submit nothing), a delete while activated (refused),
  a 30 s console capture, a delayed power-off, activation of the current
  configuration to Open Firmware with an explicit operation type and keylock, an
  immediate power-off, `hmc_power_lpar` start, immediate restart, immediate stop,
  a repeated stop (already in state) and a replayed request id, and the delete.
- **Partition P** (`…-p`). Only when exactly one VIOS lists the configured group
  with 1 GiB free and `LIVE_TEST_PROVISION_VLAN_ID` has a virtual network. It
  first sends a dry-run provision carrying a dedicated slot, which must be refused
  before any HMC read (`dedicated-slot-refused`; #1390). It then creates the 1 GiB
  volume `lppwr<8 hex>` and provisions P on it (virtual Ethernet on the VLAN, vSCSI
  mapping, PowerOn) with no PCIe argument. It then proves the decommission dry run inventories the exact vSCSI
  mapping and client adapters without changing them. The real decommission removes
  its own mapping before deleting P; the arm proves P and its mapping absent, the
  backing volume retained, and foreign mappings and other volumes unchanged. Only
  after these complete reads does teardown delete the run volume.
- **Not run, recorded as SKIP rows naming what they need:** `hmc_dump_restart_lpar`,
  provision's SR-IOV and vNIC arguments, a graceful `hmc_power_lpar` stop or
  restart, and whole-system power.
- **After.** Teardown abandons any `hmc_power_lpar` operation left open. Any failed,
  cancelled, or unproven decommission attempt retains its remaining resources for
  manual recovery; teardown does not retry mapping or partition deletion. When no
  decommission attempt started, teardown detaches a
  run mapping while its partition exists, powers off and deletes each run
  partition only while it carries the run's caller token (the provisioned one is
  kept while its mapping could not be detached), then deletes the volume.
  The before reads are compared (one re-read after 30 s on a difference).
  Observations carry `cleanup` `passed` only when every run object is gone and the
  compare holds. A sequence that stops early (a power step that does not settle)
  still records each operation it entered, failed, with the assertions it never
  reached absent: re-run rather than copy those observations.

The optional `--detach-probe` mode (#1391) selects only a diagnostic comparison:
one ownership-stamped ST41 scratch partition and one run-owned 1024 MiB volume in
`LIVE_TEST_VDISK_VOLUME_GROUP_NAME`. Two previous attach attempts consumed two of
the three-cycle total ceiling. This invocation uses only the remaining cycle:
attach while Not Activated, activate only the owned scratch partition into Open
Firmware, then detach the same mapping ID. It changes no existing test partition
and runs no provision, decommission or dedicated-PCIe path.

After the orchestrator grants the exact pushed head, run:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-power --detach-probe
uv run --no-sync python scripts/live_lpar_power.py --detach-probe
uv run --no-sync python scripts/live_test_recovery.py --results test-results-lpar-power.json
uv run --no-sync python scripts/live_test_evidence.py test-results-lpar-power.json
```

Archive the previous private result file before this invocation; its failed
observations remain failed. The previous Open Firmware attach was refused with
HSCL7006 and did not exercise detach, so it establishes no detach cause.

Private result provenance records `detach_probe: true`. The remaining cycle captures
exact mapping and reciprocal adapter identities, both endpoint state/RMC, and the original
response/HTTP/REST/HSCL evidence. A failed response remains failed even when the
mapping disappeared. Activation requires a successful attach, exact protected
inventory and the owned reciprocal adapter pair. Detach requires the same proofs
after activation and both endpoint state/RMC contexts. Malformed/duplicate inventory,
unrelated adapter drift, failed attachment or uncertain cleanup stops without retries
and retains assets for manual
recovery; the existing ST41 recovery prefixes apply. Run recovery after interruption
too. Raw result JSON stays private; publish only filtered, commit-stamped evidence.
Successful cleanup requires successful responses and independent absence proofs;
a failed cleanup response stops subsequent writes even if its effect is visible.
One remaining cycle may not reproduce an intermittent cause. Product detach success
policy and any ADR 0136 amendment require a separate evidence-based operator decision.

Live on V10R3 (2026-10-07, POWER9) seven of its eight observations passed:
`lpar.create`, `lpar.power_on`, `lpar.capture_console`, `lpar.power_off`,
`lpar.power`, `lpar.delete` and `lpar.decommission`. `provision.lpar` with the
dedicated-slot argument failed and stays failed: the slot is written into the new
partition's profile, but the provision's PowerOn activates the current configuration,
so the running partition does not own the slot. Provision now refuses a dedicated
slot before any HMC call (#1390), and the arm expects that refusal instead of sending
the slot; a dedicated slot is assigned through the bare-CEC path
([recipe](recipes/bare-cec-lpar.md)). Profile-first provisioning is #1225.

`lpar.power_on` passed on a re-run after #1392. The first run's current-configuration
activation ended `FAILED_TO_START` with "Parameter 'OperationType' is not allowed for
this Job." (`INVALID_PARAMETER`), and the same request without `operation_type`
reached Open Firmware. The tool now accepts `operation_type="activate"` and sends no
`OperationType`, and the arm's one activation, still stating `activate`, reached Open
Firmware.

`hmc_detach_storage_mapping` also answered `REST0126` carrying `HSCL2957` (no RMC
connection to the VIOS) while the VIOS read `rmc_state` active, and the readback
showed the mapping removed. That historical run used a separate pre-detach; it
does not prove native mapped decommission. Native decommission preserves a
refused detach as an error and stops, even if diagnostic readback suggests an
effect. ST41 now requires the native product path and complete readback proof.

A teardown that cannot finish records a FAIL row marked `MANUAL RECOVERY
REQUIRED` naming the commands: for a partition, `chsysstate … -o shutdown --immed`
and `rmsyscfg`; for a mapping, `rmvdev -vtd <device>` on the VIOS before the volume's
`rmlv`. The arm never removes a VIOS adapter: a server adapter left after the detach
fails the compare.

### The bare-cec arm

The bare-cec arm is the release path end to end. It creates a partition, assigns
it a dedicated slot, powers it on once with no profile and records what that does,
and activates it to SMS. It then reads its reference codes and console and runs
the PowerOff variants, including an `osshutdown` it expects the HMC to refuse.
Last, it unassigns the slot and deletes the partition.

- It reads the same four `LIVE_TEST_DEDICATED_PCIE_*` keys as the dedicated arm.
  With no DRC index configured it takes the first free slot, whatever its kind.
- It SKIPs unless `HMC_AUTHORIZE_POWER_OPERATIONS=true`, so its evidence covers
  the ownership-guarded power path.
- `LIVE_TEST_ACCEPT_PLATFORM_DUMP=true` lets it run `dumprestart`, which crashes
  the partition and takes a platform dump. Unset or `false` skips that one step.
- Run it on its own. In an `all` run the dedicated arm runs first, and bare-cec
  SKIPs rather than record a second fixture over the one the recovery check reads.
- Its create, PowerOn, console, PowerOff and delete steps are judged rows, not
  observations: the lpar-power arm owns those operations' evidence (#1346).

### Observations

The SR-IOV and dedicated arms record their verified steps through the same
observation path as the bare-cec arm. Scenarios are `st23-sriov-logical-port`,
`st29-dedicated-pcie`, `st29-pcie-inventory` and `st36-io-slots`. A run from a clean committed tree,
with both `LIVE_TEST_ENV_*` keys set, writes them to
`test-results-<arm>-observations.json`. Promote them by hand
(`docs/capabilities/README.md`, "Recording an observation").

The dedicated arm's create and delete steps (rows 30 and 34) are judged rows,
not observations: the lpar-power arm owns those operations' evidence (#1389).

An SR-IOV call that changed nothing records no observation. From a profile
that reads `none`, only the assign is observed. To observe the unassign as
well, start with the profile already listing the test port; the baseline check
admits that state. The reassign is never observed: the profile-only unassign
leaves the effective port assigned, so the reassign finds it assigned as asked
and changes nothing.

## 3. Produce the evidence

The release-window command does this only after section 4's recovery checks and
independent after-baseline comparisons pass. For an authorized manual arm, complete
those checks first, then generate its matrix:

```sh
uv run --no-sync python scripts/live_test_evidence.py test-results-dedicated.json
```

This prints a Markdown matrix stamped with the commit the run executed on.
**Paste that, not a hand-copied table.** A transcribed matrix carries no commit,
so nothing marks it stale when the branch moves — which is how PR #869's matrix
survived two weeks and a merge that left the arm unable to import.

If it refuses with "cannot be attributed to a commit", the document has no
`run.tested_commit`. It cannot support a release claim. After recovery, obtain
authorization for a new attempt using the current runner before trying to replace that evidence.

If the header says **tree was dirty**, `src/` or `scripts/` had uncommitted
changes, so the SHA does not name the code that ran. Do not cite it as clean
candidate evidence. Recover first; a replacement run needs a clean committed candidate
and explicit operator authorization.

If the matrix opens with **PARTIAL run**, an exception or interrupt stopped the
run. The script still renders it, because the rows it recorded are attributable
to the commit, but the selection line reads `Subtasks selected (not all ran)`
and the time reads `Interrupted`. Cite it as an interrupted run, never as a
complete one, and keep the PARTIAL line when you paste it.

The matrix deliberately omits each row's `data` and `note` and the document's
`hmc` block. Those carry HMC-derived text — hostnames, account names, ISO
names, location codes — and are redacted only on FAIL rows. **Do not paste the
results JSON itself into a pull request, issue, or any public location.**

### CLI coverage

The bare-CEC arm also runs the installed CLI's `lpars state`, `lpars refcodes` and
`jobs show` paths against its existing fixture and activation job. Their asserted
`record_verified` observations remain separate `cli_observation` raw result rows;
they do not replace MCP catalog sidecars or acquire MCP closure currency. The private
window summary reports `exercised: bare-cec CLI at candidate` or an explicit CLI gap
for each claim. A dry run reports gaps because no wrapper has run yet. Remaining
wrappers are unverified; historical v0.1.0's 15 claims remain MCP-only. Cite the
actual candidate and state the CLI gaps in release evidence.

## 4. Confirm the system is clean

Run this after every attempted arm, including one that looked fine, before the next
arm or evidence promotion. The release-window command does so automatically. Pass the arm's
own results document:

```sh
uv run --no-sync python scripts/live_test_recovery.py --results test-results-dedicated.json
```

After the other arms pass `test-results-<arm>.json` the same way: `vmedia`,
`bare-cec`, `round2`, `sriov`, `profiles`, `users`, `vios-backup`, `pcm`,
`network`, `lpar-config`, `storage` or `lpar-power`.

The check reads the subtasks the run dispatched from the document, and witnesses
these sets of them:

| Subtasks | What it reads |
|---|---|
| 16–22 (vmedia) | the test partition left running, its pending boot string changed, a medium of the run's still mounted to it or still in the repository, a VIOS vSCSI server adapter toward it with no mapping, and the media repository the run created |
| 11 (users) | any HMC user named `hmcpctl-live-*`, whichever run's document you pass |
| 24–25 (dedicated, bare-cec) | a partition carrying this run's marker, its dedicated slot still owned, its profile's `io_slots` off the baseline |
| 37 (vios-backup) | the run's backup still in the VIOS catalog, the test partition's disk mapping missing, and a final read the run recorded as off its baseline |
| 39 (lpar-config) | any partition named `hmcpctl-live-lpar-*` on the run's system, whichever run left it |
| 0, 3, 40, 42 (storage) | a mapping (its virtual target device) backed by an `hpctl<8 hex>` volume, such a volume still in `LIVE_TEST_VDISK_VOLUME_GROUP_NAME`, and a VIOS vSCSI server adapter toward the test partition with no mapping; 0 and 3 only read; ST42 also reports `hpvg<8 hex>` groups or unresolved pending restoration |
| 41 (lpar-power) | any partition named `hmcpctl-live-pwr-*`, running or not; on each VIOS, any mapping backed by an `lppwr*` volume, any vSCSI adapter serving a `hmcpctl-live-pwr-*` partition, and any `lppwr*` volume left in the configured volume group (read with the one admitted `viosvrcmd … -c 'lsvg -lv <group>'` command) |
| 2, 9 (network) | a network on the run's test VLAN, the test partition's client adapters off the run's baseline, the serving VIOS's FC-port labels off their originals, and a vFC group label named `hmcl-*` |

A complete current round2 run carries a fresh producer baseline. Recovery validates
its scope against the run, independently recaptures system/VIOS tables and compares
the original partition's description and profile compatibility. ST14 now provisions
and removes a temporary owned partition/disk while retaining the configured originals;
its residue or identity drift fails recovery. Missing legacy or malformed producer
witnesses are unreadable, never clean. Other unsupported dispatched subtasks print
`NOT WITNESSED`.

| Exit | Meaning |
|---|---|
| 0 | every dispatched subtask is witnessed and nothing is left behind |
| 1 | something is stranded; the output names it and the command that clears it |
| 2 | some state could not be read, the run dispatched subtasks the check does not witness, or the run was interrupted (`run.partial`), even when something is also stranded — **this is not clean** |

Exit 2 remains expected after SR-IOV, profiles, pcm and `all` runs: they dispatch
subtasks the check does not witness. For those, check by hand:

- **SR-IOV**: the test logical port is no longer assigned to the test
  partition, and its profile no longer lists it.
- **profiles**: compare an independent `lssyscfg` read with one taken before
  the run. That read covers the test partition, every profile on the system
  (`lssyscfg -r prof -m <system>`) and the VIOS `msp` flag. It should match
  line for line as a set: the HMC reorders a partition's profiles after a
  restore. The backup file `hmcpctl-live-st10` is the one expected addition.
- **pcm**: `hmcpctl metrics prefs ManagedSystem <system>` matches the read saved
  before the run.

Manual inspection does not convert exit 2 into an automated release-window pass.
Keep the window held if its selected arm lacks a complete witness.

The check issues no mutating call. When it reports something stranded, run the
command it prints yourself, then run the check again.

Exit 2 still prints anything it had already confirmed, and every class it could
read, so treat its findings as real and each `NOT READ` line as unknown. A run
that ends on exit 2 has not been shown clean by anything — check the rest of the
system yourself.

The header names the run the document came from: its group, commit and finish
time. A run that an exception or interrupt stopped still writes its results
document, marked `"partial": true` in its `run` block. Its rows end where the
run stopped, so a call cut off mid-flight has no row. On such a document the
header starts with PARTIAL and says when the run was interrupted, the check
never prints CLEAN, and it exits 2 whatever it found. Check an interrupted run
by hand as well.

It identifies the PCIe arms' leftovers by the run marker recorded in the results
document. A partition sharing the fixture's name but carrying a different
marker is never attributed to your run, and never reported for you to delete.
Run the check from the run's tested commit. A partition that carries your marker
in an ownership stamp this checkout cannot parse, such as one written before the
`hmcpctl` rename, exits 2 rather than being reported clean.

The vmedia classes key on the configured test partition (`LIVE_TEST_LPAR_NAME`
as the run recorded it), not on a marker. A medium is the run's only when its
artifacts name it (`vmedia_blank_name`, `vmedia_iso_name`); the configured ISO
name alone never is. An unmapped server adapter toward that partition is
reported whoever left it. REST shows such an adapter only as a mapping holding just its
server adapter, with no partition link, so `storage list-mappings --lpar` never lists
it; the `lshwres` listing is the witness (#1250).

## If something goes wrong mid-run

Stop. The arm's cleanup refuses to mutate anything whose ownership it cannot
confirm, so an interrupted run leaves its traces in place rather than deleting
something it did not create. That is the safe outcome. A run that finishes,
even with failed rows, writes its results document, and step 4 is what tells you
what is there. A run you interrupt, or one an uncaught error ends, writes a
partial document (`"partial": true` in its `run` block) that holds the baseline
and run marker gathered before the stop. Its rows can miss the call that was in
flight, so also check what its arm changes by hand.

Never hand-delete a partition because its name looks like a fixture. Check the
marker first.

A bare-cec run interrupted after activation can leave its partition running.
The recovery check reports it, but deleting a running partition fails. Power it
off first with `chsysstate -m <system> -r lpar -n <partition> -o shutdown --immed`.
Then remove the slot and delete the partition with the commands the check prints.

## Recording the result

A live matrix is evidence **only for the commit it ran on**. When quoting one in
an ADR, a pull request, or an issue, quote the commit with it. Before trusting
an existing matrix, check whether the branch has moved since.

Redact before posting anywhere public: hostnames, IP addresses, serial numbers,
U-code location strings, usernames, internal domain suffixes. The evidence
script's output is already filtered for this; anything you add by hand is not.

## Capturing an HMC's vocabulary

This is a separate, read-only procedure. It records what an HMC answers to every
read hmcpctl makes, so `just live-vocabulary` can check `src/` and `tests/`
against real answers instead of guesses (#1202). It creates, changes and deletes
nothing: a guard refuses any REST method but `GET` (logon and logoff excepted)
and any command not starting with `ls` before it is sent.

The output is raw HMC data: hostnames, serial numbers, location codes, account
names, and answers that name a secret keyword, which the default capture would
have blanked. Write it to a private directory **outside every repository** (the
sweep refuses one inside a git work tree), and never commit or paste it.

1. Sweep one HMC profile. `--system`, `--lpar` and `--vios` are optional; without
   them the sweep picks the first operating system, a running partition on it and
   a VIOS on it.

   ```sh
   uv run --no-sync python scripts/live_capture_sweep.py \
     --out ~/hmc-live-evidence/<date>-<profile> --profile <profile>
   ```

   It writes `sweep.capture.jsonl` and `tools.capture.jsonl`. A tool whose required
   parameter the sweep cannot supply is logged as a `skip` naming the parameter.

2. Tokenize. Pass every lab name, host prefix and site word that could appear in
   the output as `--private`. Collected names are tokenized first, so a partition
   named after a lab system keeps its system token (`sys-R1-lp3`); a `--private`
   match elsewhere replaces the whole word it sits in. The export fails, writing
   nothing, when a match survives or a `<REDACTED-PRIVATE>` token is left joined to
   the rest of a word, and does the same for any collected name, URL host, IP
   address, location code, device id, session value or SSH key.

   ```sh
   uv run --no-sync python scripts/live_capture_export.py tokenize \
     ~/hmc-live-evidence/<date>-<profile>/*.capture.jsonl \
     --out ~/hmc-live-evidence/<date>-<profile>/corpus.json --private '<lab-pattern>'
   ```

   The corpus is still private: it keeps every response body.

3. Derive the committed files, one vocabulary and one enum list per HMC release
   and system, named for both (`v11r2-p11-9824-42a`). `enums` needs the corpus to
   hold the `Enumerations.xsd` read the sweep makes.

   ```sh
   C=~/hmc-live-evidence/<date>-<profile>/corpus.json
   V=tests/fixtures/live/vocabulary
   P=v11r2-p11-9824-42a
   uv run --no-sync python scripts/live_capture_export.py enums "$C" \
     --firmware V11R2 --out "$V/enums-$P.json"
   uv run --no-sync python scripts/live_capture_export.py vocabulary "$C" \
     --enums "$V/enums-$P.json" --firmware "$P" \
     --source '<date> read-only sweep: HMC <release> managing a <family> <model>' \
     --out "$V/$P.json" --private '<lab-pattern>'
   ```

   A corpus tokenized from several raw captures names each one with its own
   `--source`, in the order passed; each becomes one entry of the vocabulary's
   `sources`.

   Read the vocabulary diff before committing it: name-bearing elements and fields
   keep only their shape (`<text>`, `<int>`, `<uuid-upper>`), so a literal that
   looks like a name is a bug in the exporter, not data.

4. Run `just live-vocabulary`. A new capture can retire allowlist entries, which
   the gate then reports as stale; delete them, or regenerate the list with
   `uv run --no-sync python scripts/check_live_vocabulary.py --write-allowlist`,
   which keeps the reasons of entries that still apply. Give every new entry a
   reason that cites #1202. A test value that is deliberately not an HMC answer
   is not allowlisted: end its line with `# live-vocabulary: allow <reason>`.

### Adding a capture to an existing pair

A committed vocabulary is derived output, so a new capture of a pair that already
has one is added by re-deriving, never by merging its rows in by hand: rows
merged by hand are lost the next time anyone re-runs the derivation.

1. Keep the new raw capture beside the pair's other raw captures, in the private
   directory its export script reads. `tests/fixtures/live/README.md` names the
   pairs whose raw captures and export script are retained, and where; treat a
   pair it names none for as having none, and re-capture that pair in full
   instead.
2. Add the raw file to that script's `tokenize` inputs, and add a `--source`
   naming it to its `vocabulary` command.
3. Re-run the script, so every raw capture is tokenized in one pass into one
   corpus and the enum list and vocabulary are derived from it.
4. Read the diff and run `just live-vocabulary`, as in steps 3 and 4 of
   "Capturing an HMC's vocabulary". A re-derivation can drop rows only an input
   you no longer hold produced; list them in the pull request.
