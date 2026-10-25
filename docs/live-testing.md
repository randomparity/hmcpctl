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
| profile-artifacts | `uv run --no-sync python scripts/live_profile_artifacts.py` | subtask 43; one disposable backup, no restore |
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
  report `already_activated` and submit nothing), a delete while activated (refused),
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

## Migration and remote-restart verification obligations (#631)

The six existing operation variants below have offline contract coverage, but no
native migration/restart observation. Their catalog evidence arrays remain empty.
Implementation refers to these current controls, not the complete IBM command/API
families; missing API options remain with #638/#698. No HMC release, POWER family,
firmware, license, topology or cross-HMC combination is verified by these tests.

| Operation | Existing scope to exercise | Required postcondition |
| --- | --- | --- |
| `lpar.migrate_validate` | Destination selector, optional target profile and wait time; optional job wait | Correct source partition and destination; terminal validation outcome; partition stays on source |
| `lpar.migrate` | Same destination controls; validation first by default, explicit bypass available | Validation precedes submission unless bypassed; successful partition is on destination and absent from source |
| `lpar.migrate_affinity` | Caller-supplied source/destination scores, capability and explicit warn/fail; canonical validation first | Rejected preflight submits no validation/migration; admitted preflight retains its outcome beside the migration job |
| `lpar.migrate_abort` | Owned source partition; optional job wait | Abort job belongs to the selected partition; reconcile actual partition location and migration state |
| `lpar.migrate_recover` | Owned failed-migration partition; optional job wait | Recovery job belongs to the selected partition; reconcile actual location, state and connectivity |
| `lpar.remote_restart` | Explicit validate/recover/restart/cleanup/cancel, source system, target name or UUID, restart-only current data and cleanup-only retained devices | Exact source/target/partition identity in the request; mode-specific terminal outcome and independently checked location/state |

These bind the exact reference rows in `docs/capabilities/operations.json` for
`lpar.migrate_validate`, `lpar.migrate`, `lpar.migrate_affinity`,
`lpar.migrate_abort`, `lpar.migrate_recover` and `lpar.remote_restart`.
Both retained REST corpora document partition-scoped Migrate, MigrateValidate,
MigrateAbort, MigrateRecover and RemoteRestart jobs. The RemoteRestart page lives
under `managedsystem-jobs`; that directory does not require a managed-system URL.
Availability still depends on actual HMC/platform capabilities; the references'
POWER10/POWER11 presence is not a tested version/transport matrix.

A reproducible native attempt requires this ordered procedure:

1. Obtain explicit approval for a source/destination managed-system pair, owned
   disposable partition, operation/modes, maintenance window and recovery plan.
   Record HMC builds, firmware, POWER family, mobility/restart licensing, VIOS/RMC,
   storage/network mappings, ownership token and authorized target scope privately.
   Cross-HMC transport/authentication must be approved separately when applicable.
2. Freeze the candidate SHA and confirm the installed/deployed build matches it.
   Capture independent source/destination partition, state and mapping baselines.
   Use UUIDs for both systems and the partition so duplicate names cannot select
   another resource. Keep raw identifiers and results private.
3. Follow this document's preflight → named arm → recovery sequence, with the
   applicable arm's configuration and exact mutation boundary approved first.
   **Current gap:** no registered migration/remote-restart arm dispatches this
   matrix. Stop at that prerequisite; do not substitute an unrelated arm or an
   ad-hoc mutating command. This procedure grants no hardware authorization.
4. An applicable authorized arm must call the existing MCP operations named in
   the table, retain submitted job IDs/hrefs and assert the listed postconditions
   using independent reads. Exercise validation before migration and preserve the
   affinity outcome. Attempt abort only during an observed in-progress migration;
   a migration that wins the race does not prove abort. Exercise recovery and the
   restart modes only for the partition states covered by the approved plan.
5. After success, failure or cancellation, run recovery and compare both endpoint
   baselines before another attempt. A terminal job is not proof of partition
   health or successful cleanup. Preserve partial/canceled outcomes and operator
   recovery requirements; do not infer rollback or automatically resubmit.
6. Generate filtered evidence with `scripts/live_test_evidence.py` only when an
   applicable arm supplies asserted observations and recovery proof. Keep the
   native pair/arm, version/topology and mode obligations open on #631 until then;
   unrun, skipped or failed outcomes cannot become passing live evidence.

Offline tests simulate unreachable destination resolution, cancellation and partial
completion without disrupting hardware. They preserve source/job attribution and
propagate failure details; they do not prove native recovery or destination health.

## Focused backup-artifact proof (#1726)

This arm requires separate operator authorization for one managed system and
HMC 10.3.1061.0 or later with the supported complete inventory shape. Confirm
the deployed revision equals the candidate HEAD; retain that revision and the
private baseline. Do not substitute the broad `profiles` arm: it performs a
system-wide restore. The focused arm performs no restore or initialization.

```sh
uv run --no-sync python scripts/live_test_preflight.py --group profile-artifacts
uv run --no-sync python scripts/live_profile_artifacts.py
uv run --no-sync python scripts/live_test_recovery.py --results test-results-profile-artifacts.json
uv run --no-sync python scripts/live_test_evidence.py test-results-profile-artifacts.json
```

Use the configured explicit system and connection in the normal validated live
configuration. First retain a complete baseline of the selected system's backup
names and identify required recovery artifacts. The arm refuses before creation
if inventory is unavailable or any `hmcpctl-live-prof-` scratch artifact already
exists. It creates one `hmcpctl-live-prof-<16 hex>` backup with overwrite disabled,
asserts its exact listed name and asks the dedicated delete operation to remove
only that name. The operation checks a complete pre-list and successful-job fresh
absence readback. The scratch file is disposable and is never used for restore.

The optional `.env` setting `LIVE_TEST_PROFILE_ARTIFACT_SUFFIX_HEX_LENGTH` accepts
only `12` or `16`; it defaults to `16` and ignores exported values. The default
name has 34 characters. An explicitly approved diagnostic run may select `12`
for a 30-character name with the same prefix and character classes. Preflight
prints the selected shape and length; saved results record the integer setting.
Older saved results without the setting retain the default of 16. Both lengths
use the same collision refusal, exact-name REST deletion and prefix recovery.

A V11R2 SP1120 attempt at `cfbdb5aee9f67132050f4b2aaca05e9d4aceb326`
created and listed its 34-character scratch backup, but the REST delete job
rejected its exact unpadded `file` value with `REST019C`. Separately authorized
CLI cleanup restored the original inventory. The rejection's cause remains
unestablished; the shorter option tests a hypothesis and is not a filename-limit
fix or evidence of a universal maximum. Each further attempt requires its own
exact-candidate grant. No SSH deletion fallback or cleanup retry is added.

An uncertain create, failed list or inconclusive deletion is a failed attempt,
not permission to retry or remove another file. Run independent recovery after
success, failure or interruption. It re-lists the selected system and reports
reserved-prefix residue without deleting or restoring anything. Unreadable
inventory or missing system identity cannot report clean. Inspect any retained
backup and the private run before separately authorizing exact-name manual cleanup;
keep required recovery artifacts. Offline tests cover collision, invalid names,
failed restore/cleanup and ambiguous outcomes; they are not passing native arms.

The evidence generator stamps the actual tested commit and filters identifiers.
Keep raw results private. Not-run/skipped/failed is not passing live evidence,
and no artifact operation is promoted merely because its source-format read
was captured. No native run is authorized by this procedure's presence.


## Metadata-only system-profile source capture

This narrowly selected source arm captures the unknown GetSystemProfiles association response.
It is not the system-profile product lifecycle or passing activation evidence. It references
exactly two existing regular AIX/Linux partitions and their differently named contained
profiles without changing their profiles, power, descriptions, ownership or defaults. Only
one unique scratch **system profile** is created, read and deleted. There is no Update,
activation, validation, restore, arbitrary command or generic job parameter surface.

A private immutable candidate packet binds the configured connection, host, managed system,
two partition/profile identities, names, power and ownership observations, and reserved scratch
name. Its SHA-256 is pinned by the operator. Existing unowned reference targets need explicit
source-only approval; the arm does not stamp them or bypass product ownership policy.
The flags below are operator assertions, not credentials or proof of permission. Obtain and
record the exact native grant before capture; interface approval alone is insufficient.

On the isolated, exact deployed source-probe revision, use private paths outside the checkout:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group system-profile-source \
  --packet "$PACKET" --packet-sha256 "$PACKET_SHA256"
```

Preflight resolves and checks the effective pinned host/port before client construction or
authentication, then only reads the exact contained references and name-only namespace.
It rejects changed bindings, profile association, power/ownership observations or scratch-name
collision. It does not load a fallback default scenario or infer an execution grant.
Only after the operator approves the exact targets/effects, source revision and cleanup:

```sh
uv run --no-sync python scripts/live_system_profile_source.py capture \
  --packet "$PACKET" --packet-sha256 "$PACKET_SHA256" --output "$PRIVATE_NEW_DIRECTORY" \
  --grant-packet-sha256 "$PACKET_SHA256" --references-approved --exclusive-writer-window
```

The coordinated writer interval begins before preflight and continues through terminal job and
cleanup reconciliation. The arm checks complete member-profile/name/power/ownership baselines
and scratch absence again immediately before Create. A bounded private atomic journal records
partial execution before submission, submission identity and terminal results separately.
Failed/warning/nonterminal status, nonzero return code, interruption, ambiguity or drift stops
further domain writes; there is no replay or automatic rollback. Retain the journal and job
handles for manual reconciliation. A timeout does not cancel the job or release coordination.
Only definite scratch creation followed by unchanged member baselines and exactly the expected
namespace permits one Delete. Get's raw unknown membership grammar is never cleanup authority.
The stable submission JobID, canonical UOM SELF path and terminal JobID/path must agree;
contradictions or reused effective handles stop before polling/cleanup. The shared generic
client compatibility policy is unchanged. Own successful jobs are removed only after saving
their identity-consistent terminal results. Files have private permissions;
raw captures must remain private and outside the checkout.

After every attempt, including an apparently successful one, run the independent read-only witness:

```sh
uv run --no-sync python scripts/live_test_recovery.py \
  --source-packet "$PACKET" --packet-sha256 "$PACKET_SHA256" \
  --results "$PRIVATE_NEW_DIRECTORY/results.json"
uv run --no-sync python scripts/live_test_evidence.py "$PRIVATE_NEW_DIRECTORY/results.json"
```

Recovery requires a coherent completed group/phase/timestamp and exactly three ordered,
successful, identity-consistent, cleaned own-job records; a false partial flag alone proves
nothing. Scratch must be absent in both the purported original and independently read namespace.
Recovery submits or deletes no job and changes no hardware. It independently compares the
pinned namespace and complete member-profile baselines, and refuses partial runs or changed
packet bindings. A failed/unreadable witness is not clean. The source journal records the exact
revision anchored to the source checkout, the renderer-compatible `tree_clean` flag, packet
identities and harness digest. CLI failures return a sanitized summary without a native
traceback; raw failure details are retained only in an owned private capture journal. The evidence renderer's
allowlist excludes raw native fields; source capture does not create maturity observations or
substitute for a final deployed product test. No native run is authorized by these instructions.

## New-owned-fixture system-profile activation calibration

The `system-profile-activation-source` arm is a bounded source investigation, separate from
metadata-only capture and the product lifecycle. It is never included in `all`, `round2` or
any default arm. It creates exactly two new owned regular AIX/Linux fixtures, four contained
profiles (one baseline and one witness per fixture), and at most two scratch system profiles.
It attaches no disks, network, storage or physical IO. Each fixture has dedicated memory,
shared **capped** CPU, one virtual CPU, 0.05 processing units and at most 1024 MiB memory.
The aggregate ceilings are two virtual CPUs, 0.10 processing units and 2048 MiB memory.

Current investigation authority and the immutable target binding are private. All future campaign
hardware tests are restricted to the operator-selected isolated test context; previous-context
packets are historical evidence and cannot authorize another run. A separate explicit fixture
lifecycle/activation/stop/cleanup grant is required; source flags, documentation and successful
preflight do not provide permission. The existing reference-only grant cannot be reused.
No existing partition, profile, description, power state, ownership, storage or network is changed.
Failed jobs from previous investigations remain retained. The earlier metadata source capture
failed at its documented REST Delete request; separate cleanup restored its baseline. That failed
capture is not a passing native procedure or activation proof, and its consumed grant cannot be replayed.

The private mode 0600 effect packet has exactly `binding_json`, `grant`, `owner`, `caller_token`,
`negative_cases`, `tested_commit`, `tested_tree` and `baseline_sha256`. `binding_json` contains the
byte-exact separately reviewed restricted-target binding, whose digest is pinned in this source
arm. The configured profile/effective endpoint/port/user/owner must agree before authentication;
ambient host overrides are refused and the already validated port is made explicit to prevent
legacy port fallback. `tested_commit` and `tested_tree` must name the exact clean deployed checkout.
`owner` is its existing configured agent identity, never an ownership override.

Prepare the packet outside the checkout with `baseline_sha256: null` for the first read-only
preflight. It returns `results.json` containing `observed`. Bind the canonical SHA-256 of its
`partitions`, `namespace` and `system_auto_off` fields (using the repository source `digest` convention) into the
final packet's `baseline_sha256`, and independently review its exact bytes/digest/effects.
Re-run the same preflight with that final packet; changed baseline or capacity refuses effects.
Fresh private directories must not already exist:

```sh
uv run --no-sync python scripts/live_test_preflight.py \
  --group system-profile-activation-source --packet "$PACKET" \
  --packet-sha256 "$PACKET_SHA256" --output "$PRIVATE/preflight"
```

Preflight reads only the bound target's identity/state/capacity, partition/profile inventory and
system-profile namespace. It checks documented resource shape, available capacity, unique identities
and absent reserved names. Invalid, ambiguous or unreadable data refuses admission. It neither
creates a job nor infers that native role permissions allow execution.

Every mutation-facing observation requires explicit native
`AssociatedIPLConfiguration.PowerOffWhenLastLogicalPartitionIsShutdown=false`. True, missing or
invalid policy refuses effects: stopping the final fixture must not power off the managed system.
The arm never changes that system policy. A separately authorized operator policy decision and
source-backed preparation are required if the restricted target currently enables automatic power-off.
The admitted writer interval includes preserving this one system policy and the new fixture/profile/
scratch namespaces; it does not claim exclusivity over unrelated existing partitions or actors.

Only after focused offline faults, exact-candidate full verification and pinned hooks, independent
source/security review, isolated deployment and actual preflight have passed, and the orchestrator
has verified the concrete effect packet against the explicit grant:

```sh
uv run --no-sync python scripts/live_system_profile_activation_source.py capture \
  --packet "$PACKET" --packet-sha256 "$PACKET_SHA256" --output "$PRIVATE/capture" \
  --grant-packet-sha256 "$PACKET_SHA256" --exclusive-writer-window
```

Creation uses one preselected existing CLI transport with every resource field explicit; no
`all_resources=1`, retry or backend fallback. The initial ownership stamp applies only to the
just-created UUID. A missing/mismatched stamp stops all later writes. Every profile/power/delete
operation freshly verifies the same UUID/name/owner/caller, contained profile fingerprints,
resource ceilings and unrelated baseline through the existing ownership authorizer, unconditionally.
The source arm does not turn an ownership-enforcement setting into an execution grant.

Both complete-path members must be shut down before validation/activation. Memory changes from
512MiB baseline to 1024 MiB witness profiles; saved desired memory is never substituted for independent
current/runtime observations. Job terminal facts, `LastActivatedProfile`, associated-profile UUID,
CLI `curr_profile`, actual memory/CPU and power facts are captured separately. Consistent calibration
is **not** universal proof of requested-profile application or complete activation. Unknown or
contradictory facts stop all later effects, including teardown, and retain manual reconciliation;
no power-only classifier exists. Running fixtures require both actual memory observations; inactive
pre-activation fixtures may legitimately omit runtime memory.

`negative_cases: true` selects two closed optional source experiments: fixture B remains running,
fixture A is stopped, and an ordered B/A record requests their distinct baseline profiles. This
intentionally violates the normal shutdown prerequisite. The once-only no-continue case may fail,
skip or succeed; no outcome is predeclared partial activation. The distinct once-only continue case
runs only if independent observations show the first case left the exact original mixed baseline.
It is not an automatic retry. Both cases preserve raw diagnostics and leave partial-effect attribution
unproven pending analysis. Their actual native failed terminal statuses remain FAIL evidence rows
and a nonzero capture exit, even after independently proven baseline restoration. No resource exhaustion, shared IO or
missing-profile fault is introduced.

This source arm always reads system-profile membership through the existing audited fixed CLI
`lssyscfg -r sysprof -m <bound-system> -F name:lpar_ids:profile_names --header`.
At most four whole-inventory reads replace GetSystemProfiles jobs: after each scratch create and
before each scratch delete. Unique complete rows, ordered ID/profile pairs and current owned
UUID/name/ID/contained-profile bindings must agree with fresh observations. Unknown encodings,
extra diagnostics, stale IDs or incomplete associations stop later effects. Power/resource checks
remain phase appropriate; running and open firmware are distinct known powered states, with the
same actual memory/CPU checks. Starting and unknown/transitional states stop later writes.
Mixed/teardown reads and normal immediate-stop requests use that same stable subset; raw firmware
state is never rewritten as running or treated as guest-OS boot proof.

The finite journal retains each bounded raw read, its effect position and complete fresh observation;
recovery verifies the exact read sequence and associations. The maximum domain-job count is eight,
with no GetSystemProfiles or validation REST submissions or automatic fallback. Historical REST Get evidence with
ambiguous ErrorData and its retained job remain unresolved; this CLI experiment does not prove
product REST Get support or requested-profile activation. No product maturity follows from it.

Required source validation uses exactly one fixed CLI command:
`chsysstate -m <bound-system> -r sysprof -n <exact-scratch> --test` (no `-o` or continuation).
The existing transport must return native exit0 and no stderr marker; bounded stdout is retained
without an English success classifier. Fresh normal guards and independent facts must be unchanged
before/after. A single fixed-position command proof stores raw output, exact command and complete
phase observations/facts; recovery rechecks normal ownership/current identities/contained profiles,
original baseline, namespace, safe policy, inactive state and unique current IDs. Missing, duplicate,
reordered or contradictory proofs refuse CLEAN. Historical REST validation warnings and their retained
job remain unresolved; this source-only backend does not change product job status semantics.

Optional N2 requires N1 to leave the same known mixed baseline. Only a newly created fixture's
powered `Uptime.text` may increase: bounded nonnegative native long, unchanged attributes and every
other fact exact. Preserve raw observations and store N2's fresh admission facts; recovery checks
both monotonic links from N1 before to after to N2 before. A running/open-firmware state change
skips N2, while missing/malformed/decreased counters or unknown facts refuse progression. Original
member baselines and inactive validation proofs remain exact, including uptime. Saved powered
facts retain the same normal ownership/profile/resource checks as live observations.

A private atomic journal records intent before effects, created identities, complete finite request/
effect/job sequence and raw observations. Reused or inconsistent job handles, nonterminal outcome,
interruption, drift, unconfirmed ownership or unknown power stops writes and retains coordination.
Own successful jobs are removed only after saving strict identity-consistent terminal/code success;
failed jobs are permitted only at the exact optional negative stages and remain undeleted.
Every required job stage must have confirmed strict success and own-job removal. Teardown is limited to known-owned new fixtures after known outcomes:
freshly verify scratch associations before fixed CLI sysprof deletion, stop only the new fixtures,
then delete only their same authorized UUIDs. There is no exception-driven rollback or name-only
cleanup. A timeout is not cancellation or permission to release coordination.

Run independent **read-only** recovery on every outcome, including interruption or apparent success:

```sh
uv run --no-sync python scripts/live_test_recovery.py \
  --activation-source-packet "$PACKET" --packet-sha256 "$PACKET_SHA256" \
  --results "$PRIVATE/capture/results.json" --output "$PRIVATE/recovery"
uv run --no-sync python scripts/live_test_evidence.py "$PRIVATE/capture/results.json"
```

Recovery records fresh inventory even for an incomplete journal. CLEAN requires the packet-bound
original namespace/member baseline, absence of all fixtures and scratch names, and a coherent
completed finite effect/job/read-proof record; a forged partial flag cannot establish completion. It submits
or deletes no job and cannot upgrade a failed/unproven capture. It records full fresh inventory and
explicit policy safety even when current policy is unsafe; unsafe policy refuses CLEAN after the read. Raw packet/journals/schema and
member identifiers remain private; the standard evidence renderer stamps the tested commit and
exports only allowlisted structural fields. No native evidence or maturity promotion follows from
mocked-boundary tests, preparation or this document. Product scope-audit activation finding remains
open until source-backed analysis resolves it.

### Product system-profile lifecycle arm

The named `system-profiles` arm exercises the product CLI/MCP domain contract. It is
separate from the source-calibration arms and does not inherit their target or fixture
permissions. Use the repository live-testing procedure for applicable native hardware proofs.
A new scoped operator grant must name the selected system, every existing member/profile,
any ownership override, and any activation/resource effects before execution. Configuration
flags are assertions, not an HMC grant or a fence against independent writers.

Add these optional settings to the existing complete private live-test configuration:

```dotenv
LIVE_TEST_SYSTEM_PROFILE_MEMBERS_JSON=[{"partition":"<member-A>","profile":"<contained-profile-A>"},{"partition":"<member-B>","profile":"<contained-profile-B>"}]
LIVE_TEST_SYSTEM_PROFILE_WRITER_WINDOW=true
LIVE_TEST_SYSTEM_PROFILE_ACTIVATION_ACTION=none
LIVE_TEST_SYSTEM_PROFILE_CONTINUE_ON_ERROR=false
LIVE_TEST_SYSTEM_PROFILE_OWNERSHIP_OVERRIDE=false
```

The default metadata scenario creates two unpredictable system profiles, lists both through
the product reader, performs rename-only, replacement-only and combined updates, then deletes
only its two current names. Four candidate names must be absent before the first effect.
Every member follows normal ownership policy; missing evidence, collision, drift or an unknown
outcome stops later writes. The arm persists the exact pending stage before each effect through
the runner's existing private recovery sidecar. An unwritable checkpoint prevents dispatch.
It creates no partition, storage, network, ownership stamp or automatic power-off operation.

`validate` selects the fixed CLI test route. `activate` selects REST activation-only;
`validate-and-activate` selects one fixed combined CLI command. Activating variants require a
separate explicit power/resource grant and caller coordination through known terminal
reconciliation. A complete witness needs inactive members with absent/different prior profiles,
new matching CLI/last-activated/contained-profile identities, and coherent actual resources.
An already-matching baseline or an uncalibrated resource shape remains unknown. No universal
saved-field application or guest-boot proof is claimed. Unknown/failed activation leaves its
names and handles for manual reconciliation; there is no automatic retry, fallback or rollback.
Known activation effects remain separate from the immutable original baseline; recovery cannot
call a changed power/resource baseline clean. Native jobs are retained, including failures.

After exact deployed HEAD/import/locked-input checks and a new scoped grant, use a fresh private
results path outside the checkout:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group system-profiles
uv run --no-sync python scripts/live_system_profiles.py --results-file "$PRIVATE/test-results-system-profiles.json"
uv run --no-sync python scripts/live_test_recovery.py --results "$PRIVATE/test-results-system-profiles.json"
uv run --no-sync python scripts/live_test_evidence.py "$PRIVATE/test-results-system-profiles.json"
```

Preflight performs read-only complete inventory/member/profile/ownership admission. Its
`--skip-hardware` variant establishes configuration only. Independent recovery always rereads
current state and requires both original-baseline equality and the complete finite terminal
sequence. Missing stages, retained residue, partial execution or unreadable evidence are not
CLEAN. Run it after every outcome, including interruption. Preserve raw results privately;
only the standard evidence renderer filters them for public use. Product native proof has not
run merely because the offline arm tests or historical source calibration passed.

### Saved partition-profile lifecycle (#1724)

The separately named `partition-profiles` arm uses one explicitly prepared,
inactive, normally owned disposable partition. It never creates or activates a
partition. Configure an empty system-profile inventory, disabled profile sync,
and a memory value within the default profile's minimum/maximum range. Coordinate
partition, saved-profile and system-profile writers for the entire run. Protected
partitions and an occupied scratch/recovery namespace are refused.

```dotenv
LIVE_TEST_PARTITION_PROFILE_TARGET=<disposable-partition-name-or-uuid>
LIVE_TEST_PARTITION_PROFILE_KIND=LogicalPartition
LIVE_TEST_PARTITION_PROFILE_WRITER_WINDOW=true
LIVE_TEST_PARTITION_PROFILE_MEMORY_MIB=512
```

Use `VirtualIOServer` only for an explicitly authorized inactive disposable VIOS.
Confirm the deployed server matches the branch commit and the configured target
before requesting run authority. Configuration and preflight grant no hardware
permission. After authorization, use these exact named entry points:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group partition-profiles
uv run --no-sync python scripts/live_profiles.py --partition-lifecycle
uv run --no-sync python scripts/live_test_recovery.py --results test-results-partition-profiles.json
uv run --no-sync python scripts/live_test_evidence.py test-results-partition-profiles.json
```

The finite sequence lists and reads profiles, saves current configuration into a
new random scratch profile, patches its desired memory, makes a retained recovery
copy and force-saves current configuration, selects the scratch default, restores
the original default, then deletes the two unprotected scratch profiles. This is
seven product mutations and eight native writes. A durable private checkpoint
precedes every product mutation. The independent recovery entry point performs
only reads: it rechecks original profile identities, ETags and complete documents,
current/default/sync identity, scratch absence and all four terminal native jobs.

Any uncertainty stops later effects and suppresses successful observations. The
results retain known scratch/recovery identities and pending intent; no automatic
retry, rollback or uncertain cleanup occurs. An operator must reconcile retained
profiles and default selection before using guarded product deletion. Independent
recovery refuses `CLEAN` for a partial sequence even if profiles appear absent.
Keep raw results private and generate public evidence through the evidence script.
This implementation has no new native execution evidence yet.

## Disposable saved-profile contract experiment (#1724)

`partition-profile-contract` is an opt-in diagnostic arm (subtask 45), excluded from
`all` and `round2`. It creates one inactive AIX/Linux partition, stamps normal ownership,
updates its base saved profile from 512 to 768 MiB, makes two successive REST profile
copies, updates the second copy to 1024 MiB, and deletes the known owned inactive
partition with its profiles. It never activates a partition, changes a default profile,
invokes force-save, or writes VIOS/storage/network/system-profile configuration.

This arm tests a finite sparse shape: 512/512/1024 MiB, one capped shared virtual CPU,
0.05 minimum/desired/maximum processing units, two virtual slots, no adapters, and at
most three profiles. It refuses a different release than V10R3 SP1060 build2408210051,
insufficient capacity, an unaligned memory block, a system-profile entry, an unsafe
stay-on policy, or existing residue under its reserved `hmcpctl-live-prof-` prefix.
The original partition/profile/policy baseline must remain equal; only the already
observed monotonic top-level runtime Uptime advance is exempted.

A private, operator-reviewed packet binds the exact target/account/owner and deployed
revision. Store it as a mode-0600 regular file in an owned mode-0700 directory outside
the checkout. The same directory, named by `window_dir`, holds once-only admission,
intent, raw non-authentication responses and recovery receipts. Error bodies are not
retained because the HMC can echo authentication headers in them. The packet has exactly
these fields; substitute the real authorized values privately:

```json
{
  "authorized": true,
  "host": "hmc.example.test",
  "port": 12443,
  "user": "example-account",
  "agent_id": "example-agent",
  "system_uuid": "11111111-1111-4111-8111-111111111111",
  "system_name": "sys-R1",
  "mtms": "9009-42A*REDACTED-SERIAL",
  "expected_build": "2408210051",
  "tested_commit": "<full verified git HEAD>",
  "tested_tree": "<full verified git HEAD^{tree}>",
  "fixture": "hmcpctl-live-prof-a1b2c3d4",
  "base": "q1724-base",
  "copies": ["q1724-copy-a", "q1724-copy-b"],
  "caller": "q1724-a1b2c3d4",
  "window_dir": "/private/authorized-profile-window"
}
```

The effective HMC configuration must match the packet before authentication. Imported
package source, clean deployed HEAD and tree must match the reviewed candidate. A packet
is an execution binding for an already granted window; setting `authorized` is not a
substitute for operator approval. No packet, target or candidate may be substituted
within an attempt. Set these optional keys in the ignored `.env` beside the runner's
normal required settings:

```dotenv
LIVE_TEST_PARTITION_PROFILE_CONTRACT_PACKET=/private/authorized-profile-window/packet.json
LIVE_TEST_PARTITION_PROFILE_CONTRACT_SHA256=<SHA256 of the exact packet bytes>
```

Run from that verified checkout, with `PROFILE_WINDOW` naming the same existing private
directory and `PROFILE_RESULTS` a fresh path there. The native admission receipt is
once-only and the arm must start within 60 seconds of its beginning:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group partition-profile-contract
uv run --no-sync python scripts/live_partition_profile_contract.py --results-file "$PROFILE_RESULTS"
uv run --no-sync python scripts/live_test_recovery.py --results "$PROFILE_RESULTS"
uv run --no-sync python scripts/live_test_evidence.py "$PROFILE_RESULTS"
```

An external operator/controller must run recovery after the arm has terminated even
when its command failed or timed out; do not condition recovery on a zero arm exit.
The ten-minute total window allocates at most seven minutes to admission plus the arm,
two minutes to independent recovery, and one minute to orchestration. The transport
upper bound is 192, including authentication and SSH: admission plus the arm share 160,
with 32 reserved for recovery. Namespace-read reservations can overcount a failed call,
never undercount it. Logout slots are reserved. There is no retry, automatic resume,
exception rollback or profile-capacity fallback. Phase deadlines use the same host's monotonic
clock and reserve ten seconds for own-session logout. Expiry closes local resources without
starting another HMC request and reports session cleanup unconfirmed.

The runner persists its partial recovery dispatch document before the arm can write. An
external termination can therefore leave a readable packet/journal recovery route; pending
mutation intent still prevents CLEAN even when an independent read sees no fixture.

Each attempted write gets a durable intent first. Lost replies, missing identities,
stale trees/ETags, foreign ownership or uncertain membership retain the fixture and
handles. A captured known configuration difference stops the copy chain; cleanup may
run only with settled effects, fresh exact fixture state, normal ownership and the
unchanged original baseline. Independent recovery always rereads the original baseline
and fixture absence, and cannot turn a lost mutation reply into a known success.

The first memory patch records HMC-added optional fields explicitly before freezing
its returned state as the copy experiment's baseline. Absent, zero and false are never
made equivalent. Read-only `ManualEntitledModeEnabled` is preserved in comparison.
Results distinguish strict whole-UOM equality from a diagnostic configuration comparison
that separately lists root name, SettingID and metadata deltas. Complete raw structural
differences remain private. Neither diagnostic equality nor clean cleanup admits a
production recovery copy, establishes VIOS/adapter/force-save behavior, or promotes a
capability's maturity. Use the filtered evidence generator for public summaries.


## IBM i partition reporting (C02, opt-in subtask 48)

The `lpar-reporting` arm explicitly requests `LparPhysicalIO(console_slot="hmc")`
through the landed typed creation prerequisite and creates one owned, inactive
OS400 scratch partition. It reads its four creation reporting fields, performs eight fixed update calls,
and deletes it without activation, power changes or profile mutation. It uses
2 GiB desired memory and 0.5 shared processing units (the lpar-config resource
fixture). IBM i support/eligibility and available resources must be established
by the operator on the selected system before the exclusive bounded window;
configuration preflight alone does not prove IBM i eligibility. Do not substitute
another partition if creation or readback is refused.

Deploy the exact candidate into a separate checkout, run `just setup`, compare
`git rev-parse HEAD` with the candidate SHA and confirm `hmcpctl.__file__` resolves
inside that checkout. Keep credentials private and do not write through a shared
`.env` symlink. Then execute only this named arm:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-reporting
uv run --no-sync python scripts/live_test_runner.py --group lpar-reporting
uv run --no-sync python scripts/live_test_recovery.py --results test-results-lpar-reporting.json
```

Recovery is required after success or failure. The arm independently compares
partition names and free memory/processing units before and after; teardown
requires its caller token and inactive state and refuses an active/foreign
fixture without a power call. Maximum effects are one create, eight update
attempts and one delete, with existing finite SSH/RPC/job timeouts and no write
retry. IPL a/b/a, restricted-I/O true/false, electronic-reporting true/false and
work-group `none` are configuration probes, not guest-runtime or IPL evidence.
No fabricated numeric EWLM ID is used. Record firmware/state rejections and
unavailable readback honestly. Generate public evidence using
`scripts/live_test_evidence.py`; raw result documents remain private. POWER9
execution does not establish POWER10/POWER11 hardware coverage.

## Inactive vTPM configuration (arm 51)

The opt-in `lpar-vtpm` arm creates one inactive AIX/Linux scratch partition with
confirmed ownership stamping and vTPM explicitly disabled. It reads the selected
system's advertised versions/encryption levels, preferring 1.2; otherwise it uses
2.0 with an advertised encryption level. Missing or unsupported advertisements
skip before creation. The creation owner also checks the system's positive
capability and compatibility before submitting a write.

Successful creation identity, ownership and independent disabled readback are
prerequisites for one explicit enable with the advertised version/encryption.
The update must report `verified`; another independent read must equal its full
observed settings before the arm records a passing update. Any uncertain or
mismatched outcome fails that assertion and stops further work. There is no
activation, profile application, key management, automatic retry or corrective
reset. Guarded inactive-only deletion and an independent resource comparison
follow even after failure.

Disabling an enabled instance still requires separate data-loss acknowledgement;
it can destroy keys and stored TPM data. An observed 1.2 disable at commit
`449f3fea09f32fb989f926787581ce1ae0634865` applied the requested false value but
cleared the omitted version. The API reported preservation `mismatch`, and that
run's two FAIL rows remain failed evidence with clean recovery. Acknowledgement
does not waive preservation equality. The positive arm starts disabled so that
the version/encryption added by enablement are explicit requested fields.

Use an explicitly authorized, exclusive hardware window and deploy its exact
candidate commit before these commands. The runner configuration and scratch
resource requirements above apply. A stranded shared scratch prefix blocks the
arm until recovery is resolved.

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-vtpm
uv run --no-sync python scripts/live_lpar_vtpm.py
uv run --no-sync python scripts/live_test_recovery.py --results test-results-lpar-vtpm.json
```

Always run recovery, including after apparent success. Generate public evidence
with `scripts/live_test_evidence.py`; keep raw results private. The selected
version/encryption is the only variant exercised. VIOS creation/update, other
versions/encryption levels, guest boot, TPM objects and migration remain unrun;
a passing configuration readback does not establish those behaviors. Registration
of the arm adds no packaged maturity credit.


## Native partition memory quantities (D01, opt-in subtask 50)

The `lpar-memory-quantities` arm creates one inactive AIX/Linux fixture with
1024/2048/4096 MiB minimum/current-target/maximum memory and 0.1/0.5/1.0 shared
processing units. Creation requires its unique ownership token and suppresses
profile application. If independent current bounds are all zero, the arm checks
contained UUID, name, type, inactive state and token, authorizes the owned target,
rechecks that evidence and applies its default profile once using the existing
transport. Positive current bounds need no apply; mixed, missing or malformed
bounds stop. A post-apply read must establish positive initialized current bounds.
This is fixture setup, not evidence that desired/profile memory equals current memory.

The sequence attempts removal of one system region while remaining at or above the
current minimum, verifies a zero-add no-op and refuses an over-maximum add. Each result
requires separate current min/current/max readback. The reference explicitly describes
shutdown removal and its below-minimum coupled reset. Ordinary removal above minimum
is a test hypothesis for this environment, not an acceptance guarantee: native refusal
fails the arm and stops the sequence. There is one possible native quantity write and
no replay, force, below-minimum/full removal, activation or guest operation.
The caller-token check and normal owning operation guard every quantity call; cleanup
only deletes the matching-token inactive fixture and independently checks absence and
restored names/free resources. These reads cannot atomically fence another administrator.
Use an exclusive approved window and stop on uncertainty.

Run from the deployed repository at the exact candidate commit. Set `RUN_DIR` to an
already-created private directory **outside every repository**, mode 0700, and use
`umask 077`. The named wrapper forwards the runner's existing output option so raw
results and observations remain external while Git provenance resolves from repo CWD:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-memory-quantities
uv run --no-sync python scripts/live_lpar_memory_quantities.py --results-file "$RUN_DIR/results.json"
uv run --no-sync python scripts/live_test_recovery.py --results "$RUN_DIR/results.json"
uv run --no-sync python scripts/live_test_evidence.py "$RUN_DIR/results.json"
```

Redirect each command's stdout/stderr into that private directory in the window
controller. Run recovery **even when the arm fails or appears successful**, and keep
independent before/after baseline evidence. The authorized controller must bound
requests, SSH commands, deadlines and its own logout/reaping; preflight output alone
is not hardware authorization. Generate public matrices only through the evidence
script, scan the complete output for private identifiers, and retain its exact commit.
Do not hand-stamp missing provenance or publish raw results. Arm50 never proves AMS,
automatic/numeric entitlement, active RMC, IBM i or forced restart; those remain unrun
unless separately authorized and exercised. At candidate `5f1317cb`, the first native
positive inactive set returned `HSCL7016 The partition must be running to perform this
operation.` Current memory remained unchanged; the arm failed, owned cleanup passed,
and independent before/after baselines matched. That attempt did not exercise add,
remove, no-op or over-maximum refusal. It is not a passing quantity matrix.

### Positive add/set validation — unrun

The inactive wrapper does not dispatch positive add/set. Those operation contexts remain
implemented and require their own approved candidate-bound window on an eligible owned
running AIX/Linux/VIOS partition with active RMC and reported dynamic-memory capability.
A separately authorized forced/no-RMC path may use explicit `--force`; a successful
current readback then does not establish guest effect, and the documented operating-system
restart remains necessary. Do not activate a fixture, supply force, switch targets or
replace a failed attempt automatically to satisfy this procedure.

For an authorized running fixture, independently record current min/current/max,
runtime memory and region size, and require at least two regions of maximum/capacity
headroom. Through the existing `lpars memory` command or `hmc_dlpar_mem` change input,
set the total to current plus one region, add one region, then remove two regions.
Independently verify each expected current value and, for an ordinary running operation,
runtime convergence before advancing. Preserve ownership/containment checks, no replay,
per-call/window bounds and independent before/after evidence. Any partial, rejected or
unverified outcome stops the sequence for the approved recovery procedure. This is a
validation procedure, not a second implemented live wrapper or an authorization to run it.

## Native two-partition memory move (D02, opt-in subtask 54)

The `lpar-memory-move` arm uses two preexisting owned AIX/Linux partitions that
are running with active RMC and dedicated memory. The authorized controller selects
the exact candidate commit and fixtures, assigns `LIVE_TEST_LPAR_NAME` and
`LIVE_TEST_SCRATCH_LPAR_NAME`, and approves an exclusive window without DPO and
its deadline. Both fixtures must report dynamic-memory capability, disabled or
suspended profile sync, matching current/runtime memory, and one region of source
minimum and destination maximum headroom. No setup creates or activates guests.

The bounded sequence moves one region forward and independently verifies both peers.
The forward result must also report PASS, workflow_completed true, outcome applied
and an explicitly present null submission_error. Only then does the arm freshly check
identity, ownership, state and quantities before the separate reverse operation.
Before either effect, the arm durably checkpoints its private intent and observations.
There are at most two effects. Any partial, unknown or changed baseline stops writes
and leaves both fixtures in place for the controller's
separate recovery decision. Neither fixture is deleted, and recovery reads only.

Verify deployed HEAD equals the selected candidate. Set `RUN_DIR` to an existing
mode-0700 directory outside every repository, use `umask 077`, and capture each
command's stdout/stderr there. The named wrapper rejects reused output and exposed
or repository destinations. Run with bounded controller-session and transport
deadlines and mandatory logout/reaping. Run recovery after every attempted arm,
including a failed or apparently successful run:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-memory-move
uv run --no-sync python scripts/live_lpar_memory_move.py --results-file "$RUN_DIR/results.json"
uv run --no-sync python scripts/live_test_recovery.py --results "$RUN_DIR/results.json"
uv run --no-sync python scripts/live_test_evidence.py "$RUN_DIR/results.json"
```

Read-only recovery requires the same clean candidate revision and HMC destination.
CLEAN requires run.partial explicitly false and the ordered original baseline,
forward intent and successful observation, reverse intent and successful observation,
and final original-baseline confirmation. Both results must contain explicit null
submission_error, workflow_completed true and outcome applied. Directions, one-region
quantities, before-states and independent observations must agree with the original
fixtures and remain within their original bounds. Fresh reads must also confirm both
original memory baselines and system resources. Matching current baselines alone
cannot clear a partial, incomplete,
refused or pre-effect run: recovery exits 2/unconfirmed, including for missing,
duplicate, reordered or mismatched proof. Readable valid baselines still receive fresh
read-only witnesses. No recovery write, retry or replay is attempted. Force, inactive,
IBM i and VIOS variants remain unrun; this proof arm does not narrow their product support.
Generate public evidence only through the evidence script, redact the complete
matrix and retain its commit binding. Raw results remain private. A skipped or
failed arm supplies no positive evidence and makes no maturity promotion.

## Inactive partition security policy (arm 52)

The opt-in `lpar-security-policy` arm requires a positive selected-system secure
boot capability and an independent resource baseline with no stranded shared
scratch prefix. It creates one owned inactive AIX/Linux fixture with explicit
`secure_boot=0`, using 2 GiB desired memory and 0.5 shared processing units.
Confirmed creation identity, ownership stamp, successful policy workflow step
and independent inactive readback precede one explicit `secure_boot=1` update.
The update must verify pending 1, preserve separately observed current secure
boot and every observable omitted setting, and match another independent read.
Missing current readback, uncertain writes and mismatches remain failures; there
is no retry or corrective write.

Use a separately authorized exclusive window and deploy the exact reviewed
candidate, verifying both HEAD and imported package source before these commands:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-security-policy
uv run --no-sync python scripts/live_lpar_security_policy.py
uv run --no-sync python scripts/live_test_recovery.py --results test-results-lpar-security-policy.json
```

Always run recovery after success or failure. Maximum effects are one creation,
one policy update attempt and one guarded inactive-only deletion. Existing
containment, identity, authorization, ownership and capability reads remain
required; the operation's two policy snapshots are not a total transport cap.
A concrete execution packet must count all physical requests, including session
establishment/logout, independent reads, cleanup and recovery, and retain the
existing finite transport/job timeouts. Cleanup uses the exact fixture identity
and caller token; an active or foreign fixture is retained without a power call.
Independent before/after partition and free-resource comparison and recovery
absence confirmation are required.

There are no keystore, dynamic-key, bypass, activation, profile or key-management
effects. Configuration readback proves no guest boot or runtime enforcement.
Capability-false, other secure-boot values, VIOS, OS400/Power11, irreversible
enables and keystore variants remain unrun. Mocks and scenario registration add
no maturity evidence. Generate public summaries only with
`scripts/live_test_evidence.py`; raw result documents remain private.


## Hardware memory access at creation (C10, opt-in arm 55)

LPAR and VIOS creation expose optional strict booleans `hardware_mem_encryption`
and `hardware_mem_expansion` through their existing CLI/MCP adapters. CLI options
are `--hardware-mem-encryption` / `--no-hardware-mem-encryption` and
`--hardware-mem-expansion` / `--no-hardware-mem-expansion`. Omission preserves
existing defaults; explicit false selects native 0. These are fixed creation-only
attributes, with no update operation or implicit expansion-factor change.

Each supplied field requires its positive selected-system capability advertisement.
Admission also requires observed `IsClassicHMCManagement=true`,
`IsHMCPowerVMManagementController=false` and `IsPowerVMManagementController=false`
on that same system. This tuple bounds admission to an observed native creation
configuration; it is not a general controller-mode classifier or a release floor.
Unknown or other tuples refuse before submission. Existing authorization, ownership,
resource and partition-environment restrictions still apply.

The named `lpar-hardware-memory` arm creates exactly one inactive owned AIX/Linux
fixture with both flags true and profile application disabled. Independent contained
identity, caller-token and inactive-state checks precede comparison of
`PartitionMemoryConfiguration/MemoryEncryptionHardwareAccessEnabled` and
`MemoryExpansionHardwareAccessEnabled` with the requested values. Missing, malformed,
mismatched or unavailable readback fails the proof. These optional read-only REST
fields establish access availability only: they do not identify desired/current
settings, guest encryption, physical-memory protection or active memory expansion.

Use a separately authorized exclusive window and deploy its exact candidate commit.
Verify deployed HEAD and imported package source before running these commands as
separate invocations; neither configuration nor preflight authorizes hardware writes:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-hardware-memory
uv run --no-sync python scripts/live_lpar_hardware_memory.py
uv run --no-sync python scripts/live_test_recovery.py --results test-results-lpar-hardware-memory.json
```

The wrapper accepts no `--results-file` option. Keep its default run files private
and run recovery after every attempt, including failure or apparent success. A
failed returned ownership-stamp flag blocks access proof; existing shared cleanup
may still delete only after its own fresh matching-token and inactive-state proof.
Unknown, foreign or active residue stays for manual inspection. There is no
activation, profile application, VIOS mutation, second create or automatic retry.
Require independent before/after partition and free-resource equality plus recovery
absence confirmation before treating cleanup as successful.

Scenario `st55-lpar-hardware-memory-creation` is evidence only after an actual
authorized candidate-bound run. Generate public evidence through
`scripts/live_test_evidence.py`, retain its exact commit and scan the complete
filtered output before publication; never publish raw results. Implementation,
offline tests, source observations and scenario registration add no positive native
proof. False/mixed combinations, VIOS and other environments remain unrun until
separately authorized windows; their product acceptance criteria remain in scope.

### Linux management roles (C05, arm 53)

The opt-in `lpar-linux-roles` arm creates one owned, inactive AIX/Linux fixture
with explicit dedicated never-share CPUs (minimum/desired/maximum one processor)
and memory (1024/2048/4096 MiB). When KVM prerequisites are admitted, creation
selects management false and KVM true with POWER10 in the same native record.
Both variants select the management field, including false, and require native
`lscomgmt -m <selected system> -F is_controller` value `1`. Controller mode passes
explicit apply false and omits the profile; classic profile support does not
authorize management-role selection. The full variant independently reads roles,
compatibility and resources, disables KVM, then changes management false→true→false
while KVM remains false. It does not activate a guest, install software, manage
keys, change the controller or rewrite an existing partition's resources.

Unknown management authority stops before fixture creation. If KVM evidence
is unavailable or ineligible but management-only admission succeeds, the same
arm omits KVM and POWER10 selection, verifies management false, and performs only
the two management updates. Independent reads must still expose both role values;
the known KVM value, compatibility and resources are preserved. Cleanup and
baseline comparison remain mandatory. This subset records explicitly labelled
management-only PASS/FAIL rows and emits no catalog observation or maturity
promotion. Generated matrices require context stating that KVM was not run.
The full KVM branch and its three catalog observations remain separate.

The committed captured system reports KVM capability false and FW950; it cannot
establish an eligible positive KVM run. Mocked eligible variants are synthetic.
A retained native management-false creation attempt on a null-controller target
was refused before any fixture was confirmed; recovery was clean and independent
baselines matched. This is refusal evidence, not a positive lifecycle claim.

A later, separately authorized finite execution packet must bind the exact
candidate HEAD and imported source provenance, target, explicit resources,
native controller authority and profileless effects, transport deadlines, cleanup/recovery
reserves and baseline facts before dispatch. Follow the shared preflight and
source-provenance procedure; private raw captures stay outside repositories.
The wrapper's maximum effects are one creation, three role updates and guarded
inactive-only matching-token deletion. Containment, ownership, admission and
independent verification add physical reads; these effect counts are not a
transport-request cap. Run only the named group:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-linux-roles
uv run --no-sync python scripts/live_lpar_linux_roles.py
uv run --no-sync python scripts/live_test_recovery.py --results test-results-lpar-linux-roles.json
```

Always run recovery, including after success, cancellation, transport failure or
unknown completion. The arm attempts owned inactive cleanup in `finally` and
compares independent before/after partition names and free processor/memory
inventory. Active, foreign-owned or unreadable fixtures remain untouched and
require operator inspection. An unconfirmed cleanup or baseline mismatch cannot
produce passing evidence. Generate any public, redacted, HEAD-bound evidence
through `scripts/live_test_evidence.py`; never publish raw result JSON or treat
configuration readback as Linux, KVM or management-controller deployment proof.


### Partition creation affinity groups (C08, arm 57)

LPAR and VIOS creation accept optional `affinity_group_id`: strict integer 1–255,
`none` for an explicit native no-group selection, or omission to preserve the native default.
CLI text is parsed into that scalar; booleans, floats, zero and 256 are rejected.
Selected-system native controller authority selects profileless creation or classic
named-profile grammar. Affinity does not imply Linux roles or their prerequisites.

The named `lpar-affinity-group` arm is excluded from default and `all` selections.
It creates `none` then `1` serially, using at most one owned inactive AIX/Linux
fixture with 2048 MiB desired memory and 0.5 desired processor units. Profile
application is disabled. A second fixture requires the first observation, guarded
deletion, absence witness and free-resource baseline comparison to pass.

Use a separately authorized exclusive window and deploy the exact candidate commit.
Verify deployed HEAD and imported package source, available resources, no stranded
scratch partitions, and the selected system before these separate invocations:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-affinity-group
uv run --no-sync python scripts/live_lpar_affinity_group.py
uv run --no-sync python scripts/live_test_recovery.py --results test-results-lpar-affinity-group.json
```

Preflight and configuration do not authorize writes. The wrapper accepts no extra
arguments; keep its default result files private. Run independent read-only recovery
after every attempt, including interruption, failure or apparent success. Unknown
identity, ownership or inactive state retains residue for operator inspection;
there is no automatic activation, profile mutation or retry.
Shared cleanup deletes only a freshly identified, matching-token inactive fixture.

Independent contained identity and raw selected `lssyscfg` fields must agree with
each request. Results label `native-partition` separately from classic
`native-profile`; the profile never substitutes for missing partition observation.
Unsupported, malformed, unavailable or mismatched observations fail the proof.
Neither label asserts current/effective state or hypervisor runtime equivalence.

Scenario `st57-lpar-affinity-group-creation` supplies evidence only after an actual
candidate-bound run and clean recovery. Generate filtered evidence with
`scripts/live_test_evidence.py` and scan it before publication; never publish raw
results. Source closure establishes grammar and field availability only. Positive
creation transitions, controller behavior, VIOS and other hardware contexts remain
unevidenced until observed in separately authorized runs; acceptance stays in scope.
