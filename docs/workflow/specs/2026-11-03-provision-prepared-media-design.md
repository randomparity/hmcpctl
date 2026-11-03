# Prepared installer media in `hmc_provision_lpar`

**Issue:** #1842 (piece 1 of #1227, epic #1215) · **Decisions:** ADR 0191 (D1, D3, D5, D6),
ADR 0313, [ADR 0319](../../adr/0319-provision-mounts-installer-media-before-the-profile-save.md) ·
**Contract:** [H1 spec](2026-10-01-logical-lpar-workflows-design.md) *Installation media and boot* ·
**Branch:** `feat/provision-install-media-1842` · **Guardrails:** `just verify`;
`uv run --no-sync prek run --all-files`

## Problem

Provision has no `install` input. Nothing binds an iso-chain-loader producer result to the partition
it built. Nothing uploads media against an expected digest, mounts it, writes a media-first boot order,
or pauses at `ready_to_boot`.

## Scope

In scope: the MCP tool `hmc_provision_lpar` accepts `install` with `media.mode: prepared`, together with
`adapters.mac` and `boot: deferred`, and runs it to `ready_to_boot`. Out of scope, with owners:

- built mode → #1843;
- power-on, refcodes, `continuation: boot`, `boot_started` and `boot: immediate` with `install` → #1849;
- detach → #1844;
- media deletion → #1848;
- the CLI mirror → a follow-up the orchestrator files;
- repository create and delete → not done;
- the dedicated-PCIe refusal → #1841.

**Components.**

- **`operations/lpar/install_media.py` (new)** owns everything the install input needs that is not an HMC
  write:
  - the install input types, `Boot`, `InstallProfile` and `INSTALL_PROFILES`, moved here from
    `operations/lpar/plan.py`, which imports them (moving them avoids a `plan` → `provision` →
    `plan` import cycle);
  - `check_install(install)` and `check_mac(mac, install)`, the field checks moved out of `plan.py`,
    whose `_check_install` and `_check_mac` now call them;
  - `bind_prepared(install, mac) -> MediaBinding`;
  - `media_name_for(operation_id)`, which returns `hmcpctl_<first 12 hex digits>.iso`;
  - `device_path(mapping)`;
  - `repository_group(hmc, vios_uuid)`;
  - `UNVERIFIED`.
- **`operations/lpar/provision.py`**:
  - `ProvisionAdapters` gains `mac`; `ProvisionRequest` gains `install` and `boot`;
    `ProvisionResult` gains `unverified: tuple[str, ...] = ()`;
  - a new `validate_provision_install(request)` is called from `request_from_arguments` and refuses:
    - `boot: deferred` or `adapters.mac` without `install`;
    - `install` with `power_on`, without `storage.capacity_mib`, with `boot: immediate` (naming #1849),
      or with `media.mode: built` (naming #1843);
    - anything `check_install` or `check_mac` refuses;
    - a producer result `bind_prepared` refuses.
  - The preconditions add, for `install`, an allowlisted `media.url` (`require_allowlisted_iso_url`) and
    `repository_group` on `storage.vios_uuid`. The dry-run step list adds `bind_media`, `upload_media`
    and `mount_media` before `save_profile`, and `set_boot_order` after `default_profile`.
  - `_add_network` takes the pinned MAC: with `adapters.mac` the network leg creates the adapter with
    `mac_address` set to it, as 12 hex digits. The network classifier then also requires the found
    adapter's MAC to equal the pin; a different MAC is `needs_attention`.
- **`operations/storage/resources.py`**: `upload_iso` gains keyword-only `expected_sha256` and
  `expected_size`. After the download, and before any web File is created, a difference raises
  `ValueError`, so nothing is uploaded. Callers that omit both see no change.
- **`operations/lpar/provision_operation.py`** (ADR 0319 order) adds the steps in the *Body* section below.
- **`server_tools/lpar/provision.py`**:
  - takes `install` and `boot`;
  - a new request with `install` and no `power_on` records `power_on=false`;
  - `delegated_tools` adds `hmc_upload_iso`, `hmc_mount_optical_media` and `hmc_set_lpar_boot_order`
    with `install`;
  - `_check_plan` passes `install`, `boot`, `adapters.mac` and the caller's `exclusive_writer_window`;
  - the docstring states the install path.
- **`operations/lpar/plan.py`** `_intended_changes` follows ADR 0319: `bind_media`, `upload_media`
  and `mount_media` come before `write_profile`, and `set_boot_order` after it. The plan digest is
  unchanged, because it hashes the request.
- **Docs:** `just tool-docs`; `docs/capabilities/maturity.json` gains a `prepared-install-media`
  implemented variant and a `built-install-media` missing variant owned by #1843; `CHANGELOG.md`.

**Producer result binding** (`bind_prepared`):

- The keys must be exactly `format`, `iso_sha256`, `manifest_sha256`, `iso_size`, `distribution`,
  `release`, `architecture`, `mac` and `network`. An unknown key, a missing key, `operation_binding` or
  `url` is refused.
- `format` is `iso-chain-media-v1`.
- Both digests are 64 lower-case hex digits.
- `iso_size` is an integer above 0 and not a bool.
- `distribution` and `release` together equal `install.profile`.
- `architecture` is `ppc64le`.
- `mac` equals `adapters.mac`.
- `network` equals the JSON form of `install.network`: `address`, `routes` as
  `[{destination, gateway}]`, and `dns` as a list.
- A refusal names the field and never its value. In the handler it is a `ValueError` before anything is
  recorded; in the body it is `OperationFailed` (`failed`).

**Body** (`_Run`):

1. After the ADR 0313 legs succeed, `_media` sets phase `media_bound` and re-runs `bind_prepared`. It
   requires the `network` effect's MAC to equal the binding's MAC and the `disk` effect's `disk_name` to
   equal `storage.storage_name`. Otherwise it raises `OperationFailed`. It records step `bind_media`.
2. Effect `upload`, kind `vios.optical_media_upload`, target `vios:<uuid>`, step `upload_media`:
   - `repository_group`, then `upload_iso(..., expected_sha256, expected_size)`;
   - a `ValueError` becomes `EffectNotApplied`;
   - this effect and `mount` are called through `ctx.effect` directly, not `_leg`, so an
     `EffectNotApplied` propagates and the engine ends the run `failed`;
   - every other error settles the effect `uncertain` and pauses the run;
   - identity `{vios_uuid, vg_uuid, media_name}`;
   - once applied, a ledger entry `installer_media` (key `installer_media`) with
     `{vios_uuid, media_name}`.
3. Effect `mount`, kind `vios.optical_mapping`, target `lpar:<uuid>`, step `mount_media`:
   - `mount_optical_media`, then one listing read;
   - the write raises `HMCError`, settling `uncertain`, unless exactly one listed optical mapping of
     this partition has the media name and a device path;
   - identity `{vios_uuid, media_name, client_slot, lun}`;
   - a refusal whose HMC error names `REST0269` (no applied current configuration) raises
     `EffectNotApplied`, and the run ends `failed`. Its text says a refused mapping create can leave a
     VIOS server adapter with no target device, using the `client_storage` wording and its `lshwres`
     check. A resume therefore never re-runs a mount the HMC refused for that reason.
4. `_persist`, as ADR 0313 runs it. With `install`, `readback_problem` also requires a
   `ProfileVirtualSCSIClientAdapter` whose `VirtualSlotNumber` is the mount's `client_slot`.
5. Effect `boot_order`, kind `lpar.boot_order`, target `lpar:<uuid>`, step `set_boot_order`:
   - `set_lpar_boot_order(devices=[optical, root disk])`, which verifies its own write;
   - identity `{pending_boot_string}`.
6. Phase `ready_to_boot`. `BodyResult("ready_to_boot", ...)` carries `workflow_completed=true` and
   `unverified=UNVERIFIED`. The engine records `paused`/`ready_to_boot`, which accepts `boot` and `abandon`.
   The handler still offers only `resume` and `abandon`; `boot` is #1849's.
7. `result_from_record` treats `ready_to_boot` as it treats `configured`.

**Device paths.** `device_path` reads a mapping's `ClientAdapter/VirtualSlotNumber` and
`TargetDevice/*/LogicalUnitAddress`. It returns `/vdevice/v-scsi@<hex(0x30000000 + slot)>/disk@<LUN
without 0x, lower-case>`, or `None`. This is the form the recipe proved bootable on V10R3 (`lpar-iso-install.md` §5). The root disk's path
comes from the storage mapping with backing `storage.storage_name` on this partition.

**Classifiers (H1 *Resume*):**

- `upload`: the VIOS comes from `request.storage.vios_uuid` and the name from
  `media_name_for(ctx.operation_id)`; the target is not parsed. The media name is listed in the repository group → `applied`; otherwise `not_applied`; an
  unreadable or ambiguous group → `needs_attention`.
- `mount`: the optical mappings of this partition with that `MediaName`:
  - one with a device path → `applied`;
  - none → `not_applied`;
  - otherwise → `needs_attention`.
- `boot_order`: the pending boot string equals the expected string, recomputed from the live mappings →
  `applied`; otherwise `not_applied`. A rewrite is idempotent, and the guarded partition is powered off.

**Repository group.** `repository_group(hmc, vios_uuid)` returns the UUID of the one volume group whose
`Resource` carries `MediaRepositories/VirtualMediaRepository`. Zero groups or several groups is a
`ValueError` naming the VIOS. No repository is created.

## Failure model

1. **Actors and deployments:**
   - an MCP agent on a policy-bounded server with one ADR 0190 store;
   - a producer (iso-chain-loader) whose result and ISO the agent relays;
   - an HTTP origin on `HMC_ISO_URL_ALLOWLIST`;
   - other HMC writers, paused by `exclusive_writer_window`.
2. **Invariants and assets:**
   - media is never uploaded unless the download's SHA-256 and size equal the bound values;
   - media is never mounted unless its result binds this request and the partition's actual MAC;
   - no overwrite of, or rebuild into, existing media, enforced by upload's name refusal;
   - `ready_to_boot` only with media mounted, the optical adapter in the saved default profile, and the
     boot order written;
   - effect and ledger kinds are a persisted contract (ADR 0195).
3. **Accepted:**
   - The VIOS copy is never read back. It is reported in `unverified`, as is guest installer-source
     reachability (ADR 0191 D5, H1).
   - A crash during an upload whose medium is then listed classifies as `applied` with no digest
     re-check. The name is unique to the operation, so only this operation's transfer can be listed.
   - The ~64 KiB result plus the other arguments can exceed `store.MAX_REQUEST_BYTES` (64 KiB). That
     request is refused as `request_too_large`.
   - A stored operation recorded before `adapters.mac` existed conflicts if a continuation re-sends
     `adapters` (pre-release; ADR 0313 precedent).
   - A mount refused with REST0269 ends `failed`, possibly leaving a VIOS server adapter
     (`client_storage.py` note). Provision's REST create sets the current configuration
     (`core.py` `_REST_CREATE_CONFIGURED`, #1164) and its `mksyscfg` path applies it, so the mount
     runs on an applied configuration. The recipe's §4 refusal followed a CLI create. Native proof is
     #1849's.
   - Any other mount error pauses. A resume that finds no mapping re-runs the mount once per resume.
     A refused create may leave a server adapter that the listing cannot show (ADR 0315
     Consequences).
4. **Covered elsewhere:**
   - the power-on, and whether the optical device survives activation natively → #1849/#1850;
   - built mode → #1843;
   - media cleanup → #1848;
   - the CLI mirror → orchestrator follow-up.

## Threat model

- **Boundaries widened:**
  - the producer result (caller-supplied JSON, ≤ 64 KiB, parsed by MCP into a dict);
  - `media.url`, fetched server-side;
  - three more delegated specialist writes.
- **Actor:** an MCP client allowed to call provision. It may send any result and any URL.
- **Controls:**
  - an exact key set, typed field checks, and refusals that never echo a value;
  - the URL passes `require_allowlisted_iso_url` before recording and again in `upload_iso`; redirects
    are refused (existing);
  - the digest and size are compared before the upload;
  - permit plus per-target authorization of `hmc_upload_iso`, `hmc_mount_optical_media` and
    `hmc_set_lpar_boot_order` before anything is recorded (existing `_authorize`);
  - device paths are built from integers and hex parsed from HMC reads, and `join_boot_device_paths`
    validates them.
- **Out of scope:**
  - a malicious allowlisted origin serving bytes that match a digest the caller supplied (the caller
    vouches for the producer);
  - a local user who can write the store.

## Success

1. With `install` (prepared) and `boot: deferred`, a clean run journals `upload`, `mount`, `profile`,
   `default_profile` and `boot_order` in that order, ends `paused`/`ready_to_boot`, ledgers
   `installer_media`, and its result lists `unverified`.
2. Each refusal named in `validate_provision_install`, and each `bind_prepared` field mismatch, refuses
   before anything is recorded.
3. The network leg requests `adapters.mac`. A created adapter whose MAC differs from `adapters.mac` ends `failed`, and no upload is attempted.
4. A digest or size mismatch uploads nothing, and the run ends `failed`. A mount refused with
   REST0269 ends `failed`. A mount write that lists no device path settles `uncertain`.
5. The `upload`, `mount` and `boot_order` classifiers, and the pinned-MAC `network` check, return
   the verdicts listed above.
6. A readback that lacks the optical slot pauses `needs_attention` without writing the boot order.
7. `hmc_plan_lpar`'s `intended_changes` order for `install` matches provision's dry-run order.
   Without `install`, the delegated tools, the step order and the readback are unchanged, and the
   existing provision tests pass.
8. `just tool-docs-check`, `just capability-inventory` and `just verify` are green.

## Validation

Focused tests:

- `tests/lpar/test_install_media.py`: binding, `device_path` and `repository_group`;
- `tests/lpar/test_provision_install.py`: the body through `engine.submit` with a fake client, the
  classifiers, and handler refusals and delegation;
- `tests/storage/test_upload_iso.py`: the digest gate.

For requests without `install`, the existing plan and provision suites pass unchanged (Success 7).
`tests/unit/test_lpar_plan.py`'s install step-order test is updated to the ADR 0319 order. Callers
of the moved install types import them from `install_media`; `plan.py` keeps no re-export.
