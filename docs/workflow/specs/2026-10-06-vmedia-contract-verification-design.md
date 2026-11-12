# Virtual-media contract verification (V4a, #1347)

Part of #628 (epic #620). Pattern: #627 / PR #1320, #629 / PR #1361.

## Problem

Eleven virtual-media and mapping operations have no maturity record. Their
`operations.json` rows are a bulk assignment naming `chkmedia`, `formatmedia`,
`lsmediadev` and SSP or HMC-media jobs that none of them issues. The vmedia arm
(ST16–22) records only through `state.record`, which never promotes.

The arm also assumes it owns the repository it works in. The boundary VIOS already
has an operator-created repository holding a virtual optical device for the test
partition. The #967 ownership guard then skips ST17–20 and ST22 cleanup entirely, so
no media operation is exercised. A VIOS holds one repository, but ST16 probes only
the configured volume group, so a repository in another group reads as absent and
the arm attempts a second create.

Three properties of the arm are unsafe once it works inside a repository it does not
own:

- ST18 and ST20 upload under the configured `LIVE_TEST_ISO_MEDIA_NAME` and later
  delete by that name, whether or not this run's upload succeeded. A same-named
  operator image would be deleted.
- ST22 unmounts every optical mapping on the VIOS, including other partitions' and
  the operator's.
- `live_test_recovery.py` treats any media upload or delete as repository ownership,
  so it would report the operator's repository as left behind.

## Operations in scope

| Operation | Requests issued | Rows bound | Implemented variant |
|---|---|---|---|
| `media.create_repository`, `media.get_repository`, `media.delete_repository` | VolumeGroup GET (and read-modify-write POST) | `rest:virtual-storage-management/volume-group` | `whole-gib-size`, `by-volume-group`, `empty-repository` |
| `media.list`, `media.create`, `media.delete` | VolumeGroup GET (and read-modify-write POST); delete also reads the VIOS mappings | `rest:virtual-storage-management/volume-group` (delete adds `rest:managed-system/virtual-i-o-server`) | `by-volume-group`, `blank-whole-gib`, `unmapped-media` |
| `media.upload_iso` | VolumeGroup GET, web File create/upload/delete | `rest:virtual-storage-management/volume-group` | `allowlisted-http-url` |
| `media.mount`, `media.unmount`, `media.list_mappings`, `storage.list_mappings` | VirtualIOServer `ViosSCSIMapping` GET (and read-modify-write POST) | `rest:managed-system/virtual-i-o-server` | `hmc-created-adapter-pair`, `by-lpar-and-media-name`, `vios-wide-or-lpar-scoped` (both lists) |

Selector resolution, the ownership guard's partition read and the `change_location`
read are shared plumbing and are not bound, as in PR #1320 and PR #1361. The web File
API that `media.upload_iso` streams through has no row in `rows.json`, so its binding
names only the volume-group row; this table is the record of that.

"ST16–22 record through `record_verified`" is read as: every observation of an
operation in the table is verified. Setup rows (discovery, the owned-repository create
in ST16), the owned-repository lifecycle in ST17, ST20's boot rows (#1345) and the
ST22 teardown stay plain and never promote.

## Design

1. **Run-owned media.** The arm only ever removes media this run created. Names
   carry a per-run tag of 8 hex digits; the HMC media-name pattern `[A-Za-z0-9_.]`
   admits no hyphen.
   - The blank medium is `hmcpctl_live_<tag>`, stored in a new artifact,
     `vmedia_blank_name`, before the create call (a create whose response is lost is
     still tracked) and cleared only when a listing shows it absent.
   - The ISO is the configured `LIVE_TEST_ISO_MEDIA_NAME` with `_<tag>` inserted
     before its suffix (`example.iso` → `example_<tag>.iso`). The setting stays,
     because an unknown `LIVE_TEST_*` key is a configuration error in every operator
     `.env`. `vmedia_iso_name` is set before the upload call and cleared when a
     listing shows it absent. The fallback that adopted the first listed medium as the
     ISO name is deleted.
   - Every delete and unmount targets these two artifacts, never the configured name.
2. **Repository discovery (ST16).** After the VIOS and volume-group reads, call
   `hmc_get_media_repository` for every listed group. The fields are under
   `Resource.MediaRepositories.VirtualMediaRepository`; `RepositorySize` is GiB text
   (it can read `10.0`) and is parsed with `Decimal`.
   - **A failed read:** record `media.get_repository` as `read-failed` (not holding)
     and SKIP the steps that depend on it.
   - **One holder:** record `media.get_repository` (scenario `st16-repository-read`;
     `repository-named`, `repository-size-positive`, cleanup `not-required`). Store
     the holder's UUID in a new artifact, `vmedia_vg_uuid`. The repository create and
     delete rows are SKIPs naming the gap.
   - **None:** create in the configured group after the free-space check, as today,
     read it back, set `vmedia_repo_created` and `vmedia_vg_uuid`. These rows stay
     plain.
   - **More than one:** SKIP; a VIOS holds one repository.
3. **ST17 (owned only)** keeps its steps and plain rows. Verified assertions there
   could not run on the boundary system, so they wait for a VIOS without a repository
   (the gap below). Not owned: SKIP naming the gap.
4. **ST18 — ISO upload.** Preconditions, each a SKIP naming a gap: `vmedia_vg_uuid`
   set; the file at `LIVE_TEST_ISO_PATH` exists (the arm never fetches media); the
   configured name absent from the repository. Upload, list, re-upload the same name
   (expected `FileExistsError`), delete, list. `media.upload_iso` (scenario
   `st18-iso-upload`): `upload-accepted`, `media-listed`, `reupload-refused`, cleanup
   `passed` when the run's ISO is no longer listed.
5. **ST19 — blank medium, mount, unmount, delete** (scenario `st19-optical-round-trip`).
   Preconditions: `vmedia_vg_uuid`, the test partition not protected (a protected
   one SKIPs ST19 and ST20 before any HMC call, naming the gap; it never raises, so
   ST21 and ST22 still run) and reading
   `Not Activated` (`hmc_get_lpar_state`; a mount on a running partition is a dynamic
   reconfiguration this arm does not exercise), and repository free space
   (`RepositorySize` minus the listed media sizes) of at least 1 GiB.
   An unknown media size is a SKIP naming the gap; sizes are compared in MiB.
   Baselines (a failed read is a `read-failed` observation of the operation it
   belongs to, and SKIPs the scenario):
   - the repository's media as (name, size) pairs and its `RepositorySize`;
   - every VIOS storage mapping as (id, partition UUID, backing kind, backing name);
   - the vSCSI adapter rows of the VIOS and of the test partition (`lshwres -r
     virtualio --rsubtype scsi --level lpar --filter lpar_ids=<vios id>` and
     `lpar_names=<test partition>`, `-F slot_num,remote_lpar_name,remote_slot_num`,
     the listing the recovery script reads; its builder moves into
     `live_test/arms/vmedia.py` so both issue the same command).
   1. `hmc_create_optical_media` (1024 MiB), then list. `media.create`:
      `create-accepted`, `media-listed`, `size-matches`, `baseline-media-kept`.
      `media.list` (the baseline read): `media-entries-named`; an empty baseline is a
      non-promoting `observed` row.
   2. `hmc_mount_optical_media` on the test partition, then the partition's optical
      mappings and the VIOS's storage mappings. `media.mount`: `mount-accepted`,
      `mapping-listed`, `baseline-mappings-kept`; cleanup from step 4. A mount that
      does not list the run's mapping re-reads both adapter listings at once: a
      failed mount can leave an adapter with no mapping (#1237).
   3. `hmc_delete_optical_media` while mounted, then list: refusal expected. The
      refusal is hmcpctl's own guard, raised before any request; the assertion pins
      that guard, not HMC behaviour.
   4. `hmc_unmount_optical_media`, then the optical mappings, storage mappings and
      both adapter listings. `media.unmount`: `unmount-accepted`, `mapping-absent`,
      `mappings-equal-baseline`, `adapters-equal-baseline`. A live run on 2026-10-01
      (#1237) showed a successful unmount removes the adapter pair the mount created.
   5. `hmc_delete_optical_media`, then list. `media.delete`: `refused-while-mounted`,
      `delete-accepted`, `media-absent`, `media-equal-baseline` (names, sizes and
      `RepositorySize`).

   Any adapter or mapping difference from the baseline after step 2 or 4, a failed
   unmount, or a read-back still listing the run's mapping records `MANUAL RECOVERY
   REQUIRED` with the command that clears it and stops the scenario: the arm never
   removes an adapter, and the delete is skipped while the medium may be mounted.
   ST22 retries the run-owned cleanup. `media.create`'s cleanup is `passed` only when
   step 5's listing equals the baseline.
6. **ST20 — boot.** Gated on an ISO this run uploaded in ST18 and on the ISO file
   existing; otherwise every step SKIPs with the ISO gap reason, and the partition is
   never powered on. When it runs, the steps are today's, with the ISO uploaded under
   the pre-checked configured name. Its rows stay plain: boot-order verification is
   #1345.
7. **ST21 — mapping reads** (scenario `st21-mapping-inventory`, cleanup
   `not-required`). VIOS-wide and test-partition reads of both list tools. An empty
   VIOS-wide listing is a non-promoting `observed` row.
   - `storage.list_mappings`: `mapping-ids-identified` (every `id` is
     `<adapter>/<target>`), `lpar-scope-subset` (the partition's ids are a subset of
     the VIOS-wide ids and each names the partition's UUID), `optical-backing-agrees`
     (the ids whose `backing_kind` is `VirtualOpticalMedia` equal the optical list's
     ids).
   - `media.list_mappings`: `optical-entries-named` (each names a partition UUID and
     a `MediaName`), `lpar-scope-subset`.

   The optical list returns raw `VirtualSCSIMapping` documents, so their ids come from
   `storage_mapping_id`; an entry without one fails the assertion. Partition UUIDs are
   compared case-insensitively. An empty test-partition listing makes
   `lpar-scope-subset` vacuous, so it is recorded as a non-promoting `observed` row.
8. **ST22 — teardown.** The boot-order guard is unchanged. Then, when
   `vmedia_vg_uuid` is set: unmount only mappings whose media is a run-owned name,
   delete only run-owned media, and delete the repository only when this run created
   it. All rows plain. The old teardown read media names from a `MediaName` key the
   tool never returns (it returns `name`), so it never deleted anything; reading
   `name` fixes that.
9. **Recovery.** Run-owned names come from `vmedia_blank_name` and `vmedia_iso_name`
   only; the configured ISO name is no longer one. Repository ownership comes from
   `vmedia_repo_created` or a repository create or delete call only. A new class,
   `run media left`, lists the media in `vmedia_vg_uuid` (`hmc_list_optical_media`
   joins the read-only allowlist) and reports any run-owned name; a run-owned name
   with no recorded volume group is `StateUnreadable`. The repository class reads
   `vmedia_vg_uuid`, falling back to `vg_uuid` for documents written before it.
   `hmc_create_optical_media` joins `_VMEDIA_MUTATIONS`.
10. **Preflight** names the arm's mutations: one blank medium created, mounted to the
    test partition, unmounted and deleted; the configured ISO uploaded and deleted
    when present; the repository created and deleted only when the VIOS has none; the
    test partition powered on only when an ISO was uploaded.
11. **Runner.** Two nullable-string artifacts (`vmedia_vg_uuid`,
    `vmedia_blank_name`), defaulted for documents written before them.
12. **Catalog.** Rebind the rows; a maturity record per operation with the variant
    above; copy the live observations the run emits (ADR 0126). Regenerate the
    runtime projection and `docs/tools/`. The harness change touches no `src/` behaviour, so
    `CHANGELOG.md` changes only if the live run finds a defect there.

## Live authorization and snapshot

The operator authorized repository, optical-media and mapping mutations on the
boundary VIOS, and the test partition's power and boot-order steps in ST20 (relayed by
the campaign orchestrator, 2026-10-06). Only media this run creates may be removed.
That covers the blank medium's create and delete inside the operator's repository and
its mount on the test partition. The live slot makes the run the only writer for its
duration.

Before preflight and after the recovery check, the operator-side snapshot reads, with
read-only tools: the repository's media and size, the VIOS's storage mappings, the
VIOS and test-partition vSCSI adapter rows, and the test partition's state and pending
boot string. The two reads are compared line for line and the result is reported in
the PR; the raw reads stay private.

## Live gaps

This table is the durable record of the gaps; a maturity record has no notes field,
and the arm emits no `missing_scope` row (`missing_scope` is for a confirmed
limitation under ADR 0132). An operation with no live observation stays
`unevidenced`.

| Case | Prerequisite |
|---|---|
| `media.create_repository`, `media.delete_repository` | a VIOS with no media repository (the boundary VIOS has an operator repository); the ST17 lifecycle then needs verified assertions |
| `media.upload_iso` | an ISO at `LIVE_TEST_ISO_PATH` on the runner host |
| `media.mount`, `media.unmount`, `media.delete` (ST19) | a blank-medium create the HMC accepts: V10R3 refused `create_optical_media`'s VolumeGroup document with REST0001 (follow-up) |
| a non-empty `media.list_mappings` | an optical mapping with a medium loaded: the boundary partition's virtual optical device holds none, so the HMC reports it with no optical backing |
| ST20 boot from the virtual CD | an uploaded ISO; boot-order verification is #1345 |

## Success

- Rows bind as tabled; `just capability-inventory` passes.
- Eleven maturity records; live ones carry the run's emitted observations, `failed`
  where an assertion failed. No SKIP or plain row becomes an observation.
- After the run, the snapshot above equals its pre-run read. ST20 powers the test
  partition off before and after its boot, so this holds for a partition that starts
  `Not Activated`; ST20 runs only with an uploaded ISO.
- Unit tests pin each precondition, the run-owned-name rule, the teardown filter,
  the recovery classes and the preflight disclosure.
- `just verify` and `uv run --no-sync prek run --all-files` pass.

## Failure model

1. **Actors and deployments:** the operator running `live_vmedia.py` from the
   live-test host against the V10R3 mutation-boundary system; CI, offline.
2. **Invariants and assets:** the operator's repository, its media and its optical
   mapping to the test partition (never changed); every other mapping on the VIOS;
   the VIOS's server adapters; the test partition's state and boot string; catalog
   truth.
3. **Accepted failure classes:**
   - a failed mount can leave an unmapped server adapter (#1237); ST19's compare and
     the recovery class report it with the `chhwres` remedy. ST20's mount has no such
     compare (its rows are #1345's); the recovery class still reports it;
   - a restored results document can name media or ownership the arm no longer
     holds. Only a run-tagged name (`hmcpctl_live_<8 hex>`, or the configured ISO
     stem with `_<8 hex>`) is ever removed or reported as the run's; a repository
     found before this invocation's own create is never the run's; and ST18, ST19
     and ST20 refuse to start while an earlier invocation's medium is recorded;
   - each blank-medium create and delete rewrites the operator's whole VolumeGroup
     document (ADR 0171), so a lossy round trip of the operator's entries is possible;
     the (name, size) and `RepositorySize` compare reports it;
   - an interrupted run can leave the run's blank medium or mapping; ST22 or the
     operator removes it by its run-unique name;
   - the read-modify-write on the VIOS and volume group loses a concurrent writer's
     change (ADR 0079, 0171), accepted for a single-operator lab window.
4. **Covered elsewhere:** storage lifecycle (#1348); boot-order verification
   (#1345); vSCSI and vFC adapters (#629); environment isolation (#461).

### Threat model

- Boundaries: tool arguments built from configuration and HMC listings; the
  `lshwres` command built from the configured system name and the listed VIOS id.
- Controls: `shlex.quote` on the system name; the VIOS id is an integer; the medium
  name is generated hex; the recovery script admits only read tools.
- Out of scope: a hostile HMC.

## Live result (2026-10-06, V10R3 / POWER9 boundary system)

**First run, at `bf8974b3`.** It stopped at ST19 before any mutation, because the
operator's `LIVE_TEST_PROTECTED_LPAR_NAMES` listed the test partition and the belt
then raised. The belt is now a SKIP that names the gap. The operator corrected the
configuration to protect the VIOS instead.

**Final run, at `b5a410c7`.** Preflight passed and the run completed: 45 rows
(20 PASS, 23 SKIP, 2 FAIL) and four observations.

The before snapshot showed the repository at 10240 MB with 9988 MB free, and the
test partition `Not Activated`.

ST19's `create_optical_media` was refused with HTTP 400 `REST0001 Failed to
unmarshal input payload`, and no medium appeared. That is recorded as a failed
`media.create` observation, and the round trip stopped there, so nothing was
mounted. In ST21, `storage.list_mappings` passed. Its `optical-backing-agrees`
assertion compared two empty sets, though: the operator's virtual optical device
holds no medium, so neither listing reports an optical backing.

Recovery exited 0 (CLEAN). The before and after read-only snapshots matched line
for line, and no adapter residue was left.

| Operation | Result |
|---|---|
| `media.get_repository`, `media.list`, `storage.list_mappings` | passed |
| `media.create` | failed: REST0001 on the VolumeGroup POST |
| the other seven | unevidenced (gaps below) |
