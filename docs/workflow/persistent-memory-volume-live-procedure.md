# Persistent-memory volume verification (#643)

This is a manual procedure. It has no live-test arm, so a run of it records
observations only and promotes no operation past `unevidenced` (ADR 0373). It needs
an exclusive hardware window for the candidate commit. Follow
[live testing](../live-testing.md), including independent before/after snapshots and
recovery. Nothing here authorizes hardware access.

## Prerequisites

- A managed system whose REST `ManagedSystem` entry advertises
  `PersistentMemoryCapable=true` and `dram` in `SupportedPersistentMemoryDeviceTypes`.
- One inactive (`not activated`) AIX/Linux partition that this checkout's ownership
  token owns, with a spare slot (`CurrentDramPersistentMemoryVolumes` below
  `MaximumDramPersistentMemoryVolumes`).
- The deployed checkout's `git rev-parse HEAD` equals the reviewed candidate, and
  `just setup` has run there.
- A private output directory outside the checkout. Keep credentials, selectors and
  raw results private. `sys-R1`, `example-lpar` and `example-vol` below are
  placeholders.

## Steps

Record the system's volumes first. The run must end with exactly this set.

```sh
hmcpctl lpars pmem-list sys-R1 > "$WINDOW_PRIVATE/before.json"
hmcpctl lpars pmem-create example-lpar example-vol --system sys-R1 --size-mib 256 --yes
hmcpctl lpars pmem-list sys-R1 --lpar example-lpar
hmcpctl lpars pmem-rename example-lpar example-vol example-vol2 --system sys-R1 --yes
hmcpctl lpars pmem-show sys-R1 example-vol2
hmcpctl lpars pmem-delete example-lpar example-vol2 --system sys-R1 \
  --acknowledge-data-loss --yes
hmcpctl lpars pmem-list sys-R1 > "$WINDOW_PRIVATE/after.json"
```

Each mutation must print `"status": "verified"`. Any other status exits 1 and must be
inspected with `pmem-list` before anything is retried.

Then check the refusals; each must exit non-zero and change nothing:

- `pmem-create` with `--size-mib 100` (not a block multiple);
- `pmem-create` with the name of an existing volume;
- `pmem-delete` without `--acknowledge-data-loss`.

## Restore

`after.json` must list the same volume UUIDs as `before.json`. Remove any volume this
run left with `pmem-delete`, or on the HMC with
`chhwres -r pmem -m <system> -o r --rsubtype volume --volume <name> -p <partition>`,
and compare again. Stop and report if the sets still differ.

## Gaps

- Create and delete on an active partition are refused by this package before any
  write. The HMC's own refusal was not captured.
- Rename on an active partition is not refused here. The reference states no rule,
  and it was not observed.
- Volumes left unassigned, for example by a profile restore, cannot be renamed or
  deleted through this package.
- Only HMC V10R3 managing a POWER9 system was observed (2026-11-13). POWER10 and
  POWER11 systems have not been observed.
