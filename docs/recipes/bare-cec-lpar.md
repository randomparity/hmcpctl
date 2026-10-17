# Bare-CEC LPAR recipe

> **MCP operations verified** on HMC V10R3 M1060 with an 8375-42A (POWER9) managed system
> during the v0.1.0 live window (#879). That window did not run these CLI wrappers.
> Expected outcomes below describe the corresponding operations; CLI presentation is
> documented separately and is not a historical CLI verification claim.

The bare-CEC arm now additionally runs the CLI `lpars state`, `lpars refcodes`, and
`jobs show` reads against its fixture and activation job. Their assertions produce
separate CLI result-row observations (ADR 0221). The release-window dry-run and final
private summary list CLI coverage or an explicit gap for each claimed operation.
Other CLI wrappers in this recipe remain outside this bounded CLI verification;
a future release must state those gaps unless it supplies additional live evidence.
Offline tests of the new steps do not establish hardware verification.

This recipe brings up one Linux LPAR that uses no VIOS, shared storage, or virtual network. It
creates the partition, gives it a dedicated physical PCIe slot, activates it to the SMS menu,
observes it, and tears it down. The steps follow the `bare-cec` live-test arm
(`scripts/live_test/bare_cec.py`). Every command below mutates only the partition this run
creates and the one slot you select.

> **Sensitive data:** command output contains system and partition names, UUIDs, slot DRC
> indexes, and reference codes. Keep transcripts in a protected location. Do not paste real
> values into issues, pull requests, or this file. The values below are placeholders.

## Prerequisites

- A connection profile for an HMC and managed system you own. Dedicated slot assignment is
  admitted only for HMC V10R3 M1060 with managed-system model 8375-42A (ADR 0165). Outside that
  envelope, `network assign-dedicated-pcie-slot` refuses.
- One dedicated PCIe slot that no partition owns. You pick it in step 1.
- `HMC_AUTHORIZE_POWER_OPERATIONS=true`. This is optional for the CLI but the live window runs
  under it. It turns on the ownership guard for power commands, so `power-on` and `power-off`
  refuse a partition another owner stamped. Every power command below passes `--system`, so
  the guard checks that system instead of searching the fleet. Never add
  `--ownership-override` to these commands.

```bash
export HMC_PROFILE=<profile-name>
export HMC_AUTHORIZE_POWER_OPERATIONS=true
SYSTEM_NAME=<managed-system-name>
LPAR_NAME=<new-lpar-name>
RUN_TOKEN=<run-unique-token>
hmcpctl config show
hmcpctl systems show "$SYSTEM_NAME" --json
SYSTEM=<managed-system-uuid-from-UUID-field>
```

`config show` should list the intended profile with `authorize_power_operations` set to `True`.
`systems show` prints the managed system as JSON; copy its `UUID` into `SYSTEM`.

## 1. Inventory the dedicated slots

```bash
hmcpctl network list-dedicated-pcie-slots "$SYSTEM" --json
DRC_INDEX=<drc-index-of-an-unowned-slot>
```

Expected: `capability` is `"available"` and `items` lists slots with `drc_index`, `description`,
and `owner_lpar`. Pick a slot whose `owner_lpar` is `null`. Record its `drc_index`.

## 2. Create the partition

`lpars create` refuses more than one virtual processor without processing units, so pass all
three resource axes explicitly. These are the arm's values.

```bash
hmcpctl lpars create "$LPAR_NAME" --system "$SYSTEM" \
  --min-mem 1024 --mem 2048 --max-mem 4096 \
  --min-procs 0.1 --procs 0.5 --max-procs 1.0 \
  --min-vcpus 1 --vcpus 1 --max-vcpus 1 \
  --caller-token "$RUN_TOKEN" --yes
hmcpctl lpars state "$LPAR_NAME"
```

Expected: `Created LPAR '<new-lpar-name>'` and the partition as JSON; copy its `UUID` into
`LPAR`. `lpars state` prints `not activated`.

The command prints the partition, then its step list as a second JSON document. It writes one
partition profile, `default_profile`. On V10R3 the REST create succeeds and the partition comes
back with a current configuration and an `AssociatedPartitionProfile` link, which step 4 reads.
Only when the HMC refuses the REST create does the command fall back to `mksyscfg` and then apply
the profile without powering the partition on; the step list then shows `apply_profile` as `ok`.
A fallback partition created with `--no-apply`, or whose apply failed, shows zero memory and
processors until the profile is applied or the partition is activated (#939).

## 3. Assign the dedicated slot

```bash
LPAR=<lpar-uuid-from-create-output>
hmcpctl network assign-dedicated-pcie-slot "$SYSTEM_NAME" "$LPAR" default_profile "$DRC_INDEX"
```

Expected: no output and exit status 0. The command writes the slot into `default_profile` and
verifies it by exact profile readback. It refuses unless the partition is `not activated`, and
a readback that does not match exits non-zero.

## 4. Activate to SMS

A PowerOn names its partition profile by UUID. Read it from the partition.

```bash
hmcpctl lpars show "$LPAR" --json
PROFILE_UUID=<uuid-at-the-end-of-AssociatedPartitionProfile-href>
hmcpctl lpars power-on "$LPAR" --system "$SYSTEM" \
  --partition-profile "$PROFILE_UUID" --boot-mode sms \
  --wait --timeout 900 --interval 5 --yes
```

Expected: `lpars show` prints the partition, whose `AssociatedPartitionProfile` link ends in the
profile UUID. `power-on` prints `Job submitted for <lpar-uuid>` and the finished job as JSON;
its status is `COMPLETED_OK`. Copy the job's `JobID` into `JOB_ID`.

A PowerOn that names no profile starts the partition from its current configuration and also
reaches `open firmware` (#879). This recipe names the profile so the activation does not depend on
the current configuration matching `default_profile`.

## 5. Observe the partition

```bash
JOB_ID=<job-id-from-power-on-output>
hmcpctl jobs show "$JOB_ID"
hmcpctl lpars state "$LPAR"
hmcpctl lpars refcodes "$SYSTEM_NAME" "$LPAR_NAME" --count 5 --json
CONSOLE_LOG=<new-file-for-the-console-bytes>
hmcpctl lpars capture-console "$LPAR_NAME" --system "$SYSTEM_NAME" --duration 30 \
  --max-bytes 65536 --idle-timeout 10 --output "$CONSOLE_LOG"
```

Expected: `jobs show` prints the same job, found, with a successful status. `lpars state`
prints `open firmware` while the partition sits at the SMS menu, or `running`. `lpars refcodes`
prints up to five recent reference codes for the partition.

`lpars capture-console` records the console for at most 30 seconds or 64 KiB, stopping after 10
seconds without output, and never sends input. It writes the raw bytes to `CONSOLE_LOG`, which
must not exist yet, and prints one line to stderr, for example
`stop reason: idle; bytes: 2048; released: true`. The bytes carry terminal escape sequences:
read them with `less -R` or a log viewer. Exit status 3 means `released: false`: the console
may still be held, so run the `rmvterm` command the line names on the HMC before another capture;
if the line says another client ended the hold (`ConsoleHoldLostError`), leave that session alone.
Exit status 1 with a message that the console is held means another session has it open.

## 6. Power off

```bash
hmcpctl lpars power-off "$LPAR" --system "$SYSTEM" --immediate \
  --wait --timeout 900 --interval 5 --yes
hmcpctl lpars state "$LPAR"
```

Expected: `Job submitted for <lpar-uuid>` with a successful job, then `not activated`. Do not
continue until the state reads `not activated`: unassign refuses an active partition.

## 7. Unassign the slot

Confirm the partition is the one this run created before each destructive step. The
description must carry `[caller <run-unique-token>]`.

```bash
hmcpctl lpars get-description "$LPAR_NAME" "$SYSTEM_NAME"
hmcpctl network unassign-dedicated-pcie-slot "$SYSTEM_NAME" "$LPAR" default_profile "$DRC_INDEX"
```

Expected: the description shows the owner stamp and your caller token; stop if it does not.
The unassign prints nothing and exits 0 after its readback shows the slot gone from the profile.

## 8. Delete the partition

```bash
hmcpctl lpars get-description "$LPAR_NAME" "$SYSTEM_NAME"
hmcpctl lpars delete "$LPAR" --system "$SYSTEM" --yes
hmcpctl lpars show "$LPAR_NAME" --json
hmcpctl network list-dedicated-pcie-slots "$SYSTEM" --json
```

Expected: the description again carries your caller token. `lpars delete` prints
`Deleted LPAR <lpar-uuid>`. `lpars show` then reports the partition as not found and exits
non-zero. The slot list shows the `DRC_INDEX` slot with `owner_lpar` `null`.

## Recovery

If a step fails, stop and keep the transcript. Read `lpars state "$LPAR"` before deciding
anything. A partition that is not `not activated` must be powered off (step 6) before unassign
or delete. If the caller-token readback does not match, touch nothing and investigate.
