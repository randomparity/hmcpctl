# Recovery check witnesses every arm's test-partition residue (#1249)

## Problem

`scripts/live_test_recovery.py` is run after every live run, but it witnesses only the
dedicated PCIe arm. Given any other arm's results document, `inputs_from_document` returns
`None` and `main` exits 0 — "that run created nothing to recover" — a clean verdict on a
system nobody looked at. The vMedia arm changes the configured test partition's power state
and pending boot string, mounts optical media to it, can leave a VIOS vSCSI server adapter with
no mapping (#1237), and creates a media repository. None of that is read.

## Decision

Every arm's results document already records what this check needs, in structured form:

- `config.system_name` and `config.lp3_name` — the run's configured test partition. This is
  "the run's configured test LPAR" the issue keys on; no new flag or artifact field is needed.
- `artifacts.vios_uuid`, `artifacts.vios_partition_id`, `artifacts.vg_uuid`, and the vMedia
  ownership artifacts `vmedia_repo_created`, `vmedia_iso_name`, `vmedia_orig_boot_order`.
- `run.subtasks` — every subtask the run dispatched, written unconditionally (ADR 0162).
- `results` rows — `{subtask, tool, status, data}`; a row's `tool` label opens with the MCP
  tool name. A `SKIP` row is either a call never made (`RunState.skip`) or a call answered
  with a declared expected outcome (`record_with_expected`), which by declaration is an HMC
  refusal or no-op; either way that row changed nothing.

A subset run restores artifacts from an earlier document (`live_test_runner.py:1717-1722`), so
ST17–ST22 can act on ownership a previous invocation recorded. Applicability therefore reads
the outstanding artifacts as well as this document's rows, and the absolute classes are read
whenever vMedia ran at all.

| Class | Applies when | Clean when the live read shows |
|---|---|---|
| optical mapping left | any of 16–22 dispatched | no optical mapping to the test partition backed by the run's ISO |
| unmapped server adapter | any of 16–22 dispatched, or a non-`SKIP` ST14 `hmc_provision_lpar`/`hmc_delete_lpar` row | no more vSCSI server adapters toward the test partition than mapped ones |
| media repository left | `vmedia_repo_created`, or a non-`SKIP` 16–22 row for `hmc_create_media_repository`, `hmc_delete_media_repository`, `hmc_upload_iso` or `hmc_delete_optical_media` | no repository in the run's volume group |
| test partition running | a non-`SKIP` ST20 `hmc_power_on_lpar` row | state `Not Activated` |
| boot string drift | a non-`SKIP` 20/22 `hmc_set_lpar_boot_order` row, or a non-empty `vmedia_orig_boot_order` | pending boot string equals the baseline |

Those trigger tools are every mutating tool `vmedia.py` calls except `hmc_power_off_lpar` (the
arm's end state is off) and the mount/unmount pair (covered by the unconditional classes); a
test pins that set against the source so a new mutating call cannot go unwitnessed. Power is
judged only after ST20's power-on: ST14 provisions with `power_on=True`
(`scripts/live_test/arms/provisioning.py:186`), so "off" is not an absolute expectation.

The boot baseline is the `data.pending_boot_string` of the ST20 row labelled
`hmc_read_lpar_boot_order (baseline)` (a new `vmedia.py` constant, imported like
`_SET_BOOT_ORDER_STEP`), else `vmedia_orig_boot_order`; with neither the class is unjudgeable.
The run's ISO names are `config.iso_media_name` and `vmedia_iso_name` when set.

The adapter check lists the VIOS's server adapters with the #1237 command, filtered by
partition id because the document records the VIOS's id, not its name (`lpar_ids` is a valid
`scsi` filter, `docs/refs/hmc-commands-p10/commands/lshwres.md:103`): `lshwres -r virtualio
--rsubtype scsi -m <system> --level lpar --filter lpar_ids=<vios id> -F
slot_num,remote_lpar_name,remote_slot_num`. It counts rows naming the test partition against
the distinct server adapters (`id` before `/`) in `hmc_list_storage_mappings` for it. Mappings
carry an adapter name, not a slot, so a count is the honest comparison; more mapped adapters
than listed ones is unjudgeable, since the surplus could cancel an unmapped one. A mapping with no
`<adapter>/<device>` id makes the class unjudgeable. #1250 later captured the REST shape of an
unmapped adapter: a mapping with no partition link, so it never reaches the partition's list.
The message still prints the slot rows and the listing to compare by hand.

**Coverage is explicit.** Subtasks 16–22 are witnessed by the table and 24–25 by the existing
PCIe checks. Every other dispatched subtask is listed as `NOT WITNESSED`, and the runbook says
what each of those arms changes and how to check it by hand. The report header prints the
document's `run.group`, `run.tested_commit` and `run.finished`, so the operator
sees which run was witnessed.

**Exit codes stay 0/1/2.** 0: every dispatched subtask is witnessed and every applicable class
read clean. 1: something is stranded and nothing is unjudged; each finding prints its clearing
command. 2: a read failed, a class lacks a document field it needs, the document records no
`run.subtasks`, or a dispatched subtask is not witnessed. Every applicable class is read even
after another fails, and exit 2 prints every finding and names every unjudged class and
not-witnessed subtask.

**Read-only guard.** `_READ_ONLY_TOOLS` gains `hmc_read_lpar_boot_order`,
`hmc_list_optical_mappings`, `hmc_list_storage_mappings` and `hmc_get_media_repository`, each
`effect="read"`. `hmc_run_command` is admitted only for commands opening with `lssyscfg ` or
`lshwres `, and only when the command carries none of `; | & $ \` < > ( )` or a newline: the
listing now interpolates a system name from a results document, so the guard refuses a command
that could chain a second one.

Remedies are printed, never run (ADR 0162): `hmcpctl lpars power-off`, `hmcpctl lpars
set-boot-order`, `hmcpctl storage unmount-optical-media`, `hmcpctl storage delete-media` then
`delete-media-repo`, and `chhwres -r virtualio --rsubtype scsi -o r --id <vios id> -s <slot>`
(`docs/refs/hmc-commands-p10/commands/chhwres.md:34`) after comparing slots with mappings.

`AGENTS.md` needs no change: it already sends every live run through `docs/live-testing.md`,
whose step 4 carries the coverage and the by-hand list.

## Failure model

1. **Actors and deployments** — one operator at a terminal on a host with HMC reach, after a
   live run, holding a results document. Never CI (AGENTS.md).
2. **Invariants and assets at stake** — the script never mutates the managed system; exit 0
   is never printed for a dispatched subtask nobody witnessed; a results document is trusted
   to be the one the runner wrote, but its strings never reach a command unquoted.
3. **Accepted failure classes** — a foreign optical mapping backed by an ISO of the same name,
   or a pre-existing unmapped adapter toward the test partition, is reported (reporting is
   not acting; the operator decides). An interrupted run writes no document
   (`_write_results` runs after the subtask loop, `live_test_runner.py:1724-1751`), so the
   file on disk is the previous run's; the header names that run, and the runbook sends an
   interrupted run to the by-hand list.
4. **Covered elsewhere** — remediation: unowned, excluded. Joining adapters to mappings by
   REST shape: #1250 kept the count, since the projection carries no slot. Round2's partition, user, network and property changes, SR-IOV, and ad
   hoc windows: reported as `NOT WITNESSED` (exit 2); extending coverage is unowned.

## Threat model

1. **Boundaries** — widened: the read-only guard on `hmc_run_command` now admits a second
   command family. Added: a command string built from results-document values (system name,
   VIOS partition id), and printed remedies and read-tool arguments built from the LPAR name,
   VIOS uuid, VG uuid, ISO names and boot paths.
2. **Actors** — the local operator, who chose the document; the document's writer, the
   runner, from `.env` the operator controls. No remote or tenant actor reaches the script.
3. **Controls** — the system name is `shlex.quote`d and the VIOS id must be an `int`; the
   guard refuses any command not opening with `lssyscfg ` or `lshwres `, or carrying a shell
   metacharacter, before it is sent; tools stay on an allowlist whose members are `read`;
   every document value in a printed remedy is `shlex.quote`d, and remedies are never run.
4. **Out of scope** — a hostile results document beyond what the guard refuses: the operator
   wrote it with their own run.

## Considered & rejected

- **A `--lpar` flag.** judgment: the document already records the configured partition the
  run used; a second source can disagree with it.
- **New `LiveTestArtifacts` fields recording each change.** judgment: each needs a
  `setdefault` migration in `_decode_artifacts` (as `test_user_uuid` has,
  `scripts/live_test_runner.py:1331-1333`) and an arm write, while the rows and the existing
  ownership artifacts already record each change.
- **Absolute expectations for power.** verified: ST14 provisions the test partition with
  `power_on=True` (`scripts/live_test/arms/provisioning.py:186`), so a clean round2 run would read
  as stranded.
- **Exit 3 for not-witnessed subtasks.** judgment: the frozen criterion puts "could not be
  judged" under exit 2, and a fourth code escapes the runbook's "exit 2 is not clean" rule.
- **Narrowing the runbook only.** judgment: the issue prefers the checks; the honest-interim
  text is kept for what the checks still do not witness.

## Validation

Focused tests in `tests/scripts/test_live_test_recovery.py`, all through the guarded stub
call path:

| Contract | Mode | Case |
|---|---|---|
| each class detected, and clean when not | focused-test | one stranded and one clean case per class |
| applicability from rows and restored artifacts | focused-test | `SKIP` trigger makes no read; ST22-only and ST18-only documents read; ST14 rows trigger adapters |
| trigger tools cover every mutating `vmedia.py` call | focused-test | source scan against `TOOL_SECURITY` effects |
| unreadable or missing input is exit 2, others still read | focused-test | failed read, missing `vios_uuid`, no baseline, id-less mapping |
| exit codes 0/1/2 and the report header | focused-test | `main` over vmedia, round2, dedicated and run-less documents |
| guard admits the new reads only | focused-test | new tools pass; `lshwres` passes; metacharacters refused |
| new tools are `read` and served | focused-test | registry effect check; existing served-composition test |
| PCIe behaviour unchanged | focused-test | existing cases stay green |
| runbook invocation | focused-test | existing `tests/test_live_testing_doc.py` flags test |
