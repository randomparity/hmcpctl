# Logical LPAR workflows and installation handoff contract

**Issue:** #1216 (H1 of epic #1215)
**Decisions:** [ADR 0189](../../adr/0189-logical-lpar-catalog-and-delegated-authorization.md),
[0190](../../adr/0190-durable-logical-operation-store.md),
[0191](../../adr/0191-installation-ends-at-boot-started.md),
[0192](../../adr/0192-decommission-owned-storage-cleanup.md),
[0193](../../adr/0193-host-handoff-external-management-hold.md),
[0194](../../adr/0194-initial-installation-profiles-and-native-envelope.md)
**Branch:** `feat/logical-lpar-contract-1216` from `main`
**Guardrails:** `just verify`; `uv run --no-sync prek run --all-files`

## Purpose and boundary

This is the contract the epic's children implement: #1218–#1232 here, and iso-chain-loader#23–#25.
It changes no code. Where a value can only come from native evidence, this spec names it as
**undecided** and says who decides it. Nothing undecided may be reported as implemented.

hmcpctl's provisioning responsibility ends at an HMC-observable progress code showing that the
installer boot has started (ADR 0191). Everything after that point is the user's: install
success, disk boot, guest identity and SSH. That boundary was the operator's decision on
2026-10-01.

Not in this design (the approved exclusions):

- implementation of the eleven tools;
- iso-chain-loader changes;
- #637's profile primitives;
- guest SSH and readiness;
- any live run;
- kdive-side code;
- any change to the six-name `hmcpctl.api` facade (the logical tools compose existing
  `src/hmcpctl/operations/` functions instead);
- a generic workflow language.

## Primary tools

All eleven are MCP tools. Every result is a typed record, and every collection in a result
carries `limit` and `truncated`. Effects are as registered under ADR 0189.

**Mutating tools** (provision, reconfigure, decommission, power) also take:

- `request_id`;
- `continuation`: `none` (default), `resume` or `abandon`, plus `boot` for provision only;
- `wait_seconds`.

All of these follow ADR 0190. Provision (for `install`), decommission (for `delete_owned`) and
reconfigure (for `processors`, `memory_mib` or `disks`, ADRs 0314 and 0322) also take `exclusive_writer_window`
(see *Ownership and shared VIOS state*). Specialist and
logical partition mutations take an optional `hold_id` (ADR 0193).

| Tool | Effect | Inputs (beyond `profile` and the common fields) | Result |
| --- | --- | --- | --- |
| `hmc_inventory` | read | `systems?` (≤ 16 selectors), `lpar_state?`, `owner?`, `limit` 1–200 (default 50), `cursor?` | systems and partitions with scoped ids, state, capacity and owner; `sources` with per-source `ok` / `unavailable` / `denied` |
| `hmc_plan_lpar` | read | provision's inputs, with `system_name_or_uuid` optional and a `placement` constraint as the alternative | `plan_digest` (SHA-256 of the canonical inputs and resolved targets), resolved targets, `blockers[]`, `intended_changes[]`, `unverified[]` |
| `hmc_provision_lpar` | destructive | today's inputs plus `adapters.mac?`, `expected_plan_digest?`, `install?`, `exclusive_writer_window` (required true except `dry_run` or `abandon`; ADR 0313) and `boot` (`immediate` default / `deferred`) | `ProvisionResult` (today's fields) plus the operation fields |
| `hmc_reconfigure_lpar` | destructive | `lpar`, `system_name_or_uuid` (required, as ADR 0199), `patch`, `allow_disruption` (default false), `exclusive_writer_window` (required true for `processors`, `memory_mib` or `disks`; ADRs 0314, 0322) | operation fields plus `changes[]`, each `live` / `profile` / `pending_activation` |
| `hmc_decommission_lpar` | destructive | ADR 0027 inputs plus `storage_cleanup` (`retain` default / `delete_owned`) | `DecommissionResult` plus `storage` (`deleted[]`, `retained[]` with reasons, `pending[]`) and the operation fields |
| `hmc_power_lpar` | destructive | `lpar`, `system_name_or_uuid` (required, ADR 0199), `action` (`start` / `stop` / `restart`), `mode` (`graceful` default / `immediate`) | operation fields plus `already_in_state` and `observed_state` |
| `hmc_inspect_lpar` | read | `lpar`, `system_name_or_uuid` (required, ADR 0200), `include` ⊆ {`resources`, `rmc`, `profile_drift`, `refcodes`} | state, RMC, resources, profile drift, ≤ 20 refcodes, `next_actions[]` (tool names only) |
| `hmc_prepare_host_handoff` | mutate | `action` (`prepare` / `release`), `lpar`, `hold`, `consumer_label` (≤ 64 characters), `hold_id` | `{action, hold, document}`; `document` is null on `release` |
| `hmc_operation_status` | read | `operation_id?`, `request_id?`, `state?`, `outcome?`, `limit` 1–50, `cursor?`; with neither id it lists the caller's operations | always a page of at most 50 operation records (a lookup is a page of at most one), each with its newest ≤ 200 events (phase changes and effect intents and outcomes) and `truncated` |
| `hmc_search_tools` | read | `query` (≤ 200 characters) *or* `name`, `limit` 1–20 | names, one-line summaries, effect, maturity; the full input schema only for an exact `name` |
| `hmc_invoke_tool` | destructive | `name`, `arguments` (≤ 64 KiB) | `{name, result}`, where `result` is the invoked tool's own result (the ADR 0189 exception to ADR 0012) |

Selectors are the existing `system_name_or_uuid` / `lpar_name_or_uuid` pairs, with their
existing resolution rules.

### Provision's existing inputs

`hmc_provision_lpar` keeps every existing input and every `ProvisionResult` field, including:

- `adapters`, `storage` (which already creates a disk), `resources`, `partition_type`;
- `power_on`, `dry_run`, `assignments`, `caller_token`;
- the affinity options;
- the nested `storage.vios_uuid` selector.

The new inputs are additive except `request_id`. `request_id` is required on
`hmc_provision_lpar` and `hmc_decommission_lpar`, and on their CLI mirrors as `--request-id`.
This is a pre-release change with no compatibility path. #1225 writes the provision CHANGELOG
entry and #1229 the decommission one.

With `install`, `boot` alone governs power-on: the composite's early power-on step does not run,
and an explicit `power_on=true` is refused. `dry_run=true` records no operation and takes no
lock or guard; `request_id` is still validated. An `expected_plan_digest` that differs from the
freshly computed `plan_digest` is refused before any intent is recorded.

The other new provision inputs are optional:

- `adapters.mac`, the pinned MAC for prepared media (ADR 0191), in lower-case colon form;
- `exclusive_writer_window`, read only when `install` is present.

`storage.vios_uuid` stays required on provision. On `hmc_plan_lpar`'s `placement` path, when no
VIOS or volume group is named and more than one qualifies, planning returns a blocker. It never
chooses the first match.

### Patch and install inputs

`patch` covers `name`, `processors` and `memory_mib`. Omitted fields are preserved.
`allow_disruption=false` refuses live removals (memory or processors decreasing while the
partition is Running) and writes them as `pending_activation`. `true` permits live removal
through DLPAR. #1228 extends `patch` under the same rules with:

- networks (implemented, #1845, ADR 0325);
- added disks;
- `installer_media: detach` (implemented, #1844, ADR 0321);
- `boot: root_disk` (implemented, #1844, ADR 0321).

`install` contains:

- `profile`: `ubuntu-26.04.1` or `rocky-9.8`;
- `network`: `address` as CIDR, `routes` (1–16, including a default), `dns` (≤ 3);
- `ssh_authorized_keys` (1–16 public keys) and `login_user`, built mode only: prepared media
  already carries its own, so a prepared request that supplies them is refused;
- `media`: either `{mode: built}` or `{mode: prepared, url, producer_result}`.

`network.address` is IPv4 CIDR.

### Operation fields

- `operation_id`, `request_id`, `state` (`running` / `interrupted` / `paused` / `terminal`,
  paired with `outcome` as in ADR 0190 Decision 6: `paused` carries `needs_attention` or
  `ready_to_boot`).
- `phase`, per tool:
  - provision: `validating`, `creating`, `configured`, `media_bound`, `ready_to_boot`,
    `installer_booting`;
  - reconfigure: `validating`, `applying`;
  - power: `validating`, `transitioning`;
  - decommission: `validating`, `tearing_down`, `cleaning_storage`.
- `outcome`, or `null` while `running` or `interrupted`:
  - `completed` for reconfigure, power and decommission;
  - `configured`, `ready_to_boot` or `boot_started` for provision;
  - `needs_attention`, `failed` or `abandoned` for any tool.
- `effects[]`, each with `kind`, `target`, `status` (`intended` / `applied` / `not_applied` /
  `uncertain`) and the created identity.
- `warnings[]` and `next_actions[]`.

## Delegated authorization

Under ADR 0189, a logical action runs only when the policy permits every tool in its row and
the dispatch authorizer admits the call, as that tool, for each resolved target.

| Action | Delegated tools |
| --- | --- |
| plan | `hmc_list_systems` (enumerating placement only), `hmc_list_lpars`, `hmc_capacity_report`, `hmc_list_virtual_networks`, `hmc_list_vios`, `hmc_list_volume_groups`, `hmc_get_vios_storage_detail` (ADR 0198) |
| inventory | `hmc_list_systems`, `hmc_list_lpars`, `hmc_capacity_report`, `hmc_list_lpar_ownership`, each authorized per source (ADR 0196) |
| provision (no install) | `hmc_create_lpar`, `hmc_add_network_adapter`, `hmc_add_vscsi_adapter`, `hmc_create_virtual_disk`, `hmc_map_storage_to_lpar`, `hmc_save_current_partition_profile`, `hmc_set_default_partition_profile` (ADR 0313) |
| provision `install` | the row above, plus `hmc_upload_iso`, `hmc_mount_optical_media`, `hmc_set_lpar_boot_order`, `hmc_power_on_lpar`, `hmc_read_lpar_refcodes` |
| `power_on=true` (no `install`) | adds `hmc_power_on_lpar` |
| reconfigure | `hmc_modify_lpar` and `hmc_rename_lpar` for `name`, `hmc_dlpar_proc`, `hmc_dlpar_mem`, `hmc_update_partition_profile` (ADR 0314 Decision 6); `hmc_unmount_optical_media` for `installer_media` and `hmc_set_lpar_boot_order` for `boot` (ADR 0321 Decision 6); `disks.add` adds `hmc_create_virtual_disk`, `hmc_map_storage_to_lpar` and `hmc_save_current_partition_profile`, and `disks.detach` adds `hmc_detach_storage_mapping`, each per VIOS (ADR 0322); `networks.add` adds `hmc_add_network_adapter` and `networks.remove` adds `hmc_delete_adapter`, each with `hmc_save_current_partition_profile` (ADR 0325) |
| power `start` | `hmc_power_on_lpar` |
| power `stop` / `restart` | `hmc_power_off_lpar` |
| decommission `retain` | `hmc_decommission_lpar`'s existing set |
| decommission `delete_owned` | adds `hmc_detach_storage_mapping`, `hmc_unmount_optical_media`, `hmc_delete_optical_media`, `hmc_delete_virtual_disk` |
| inspect | `hmc_get_lpar` (required); `refcodes` adds `hmc_read_lpar_refcodes`; `resources` adds `hmc_list_vios` and `hmc_get_vios_storage_detail` for each VIOS; each section authorized on its own (ADR 0200); `profile_drift` adds #637's profile read |
| handoff `prepare` | `hmc_get_lpar`, `hmc_list_lpar_ownership`, `hmc_get_vios_storage_detail` for each serving VIOS |
| handoff `release` | none beyond the tool itself |
| operation status | none beyond the tool itself; it lists only records with the caller's agent id |

The names of #637's profile-write and profile-read tools are **undecided**, and #637 sets them.
#1225 and #1226 stay blocked on it.

`hmc_power_lpar` never reaches `dumprestart`; that stays `hmc_dump_restart_lpar` (ADR 0188).
`mode=immediate` must be stated explicitly. A timeout never escalates to it.

## Operations, resume and restart

ADR 0190 governs, including its state and continuation table. Each implementer must hold to the
following.

**Intent before writing.** Before any HMC write, the store records the intent. A crash between
intent and outcome leaves the effect `uncertain`. The pre-power-on refcode baseline is recorded
as an effect.

**Resume.** `resume` classifies each `uncertain` effect by a live read keyed on its recorded
identity. It continues only when every effect classifies:

| Effect | Live read that classifies it |
| --- | --- |
| partition create | UUID, then name within the system |
| adapter create | UUID; the MAC for prepared media |
| disk create | VIOS, volume group and name |
| media upload | repository name |
| mount | optical mapping listing |
| boot order | `PendingBootString` |
| ownership stamp | the description |
| profile write (`lpar.profile_write`, ADR 0314) | profile readback: requested values `applied`, earlier values `not_applied` |
| rename | partition name |
| power-on / power-off | partition state, plus the HMC JobID if one was recorded |
| DLPAR change | none — always `needs_attention` |

A DLPAR delta cannot be told apart from a concurrent change, so it is never classified. A
resource found under a different owner stamp is `needs_attention` and is not adopted.

**Restart.** A `restart` resumed after its power-off was applied does not power off again.

**Installer power-on** happens at most once per operation. A `resume` after a boot wait that
ended `needs_attention` re-polls refcodes against the recorded baseline and never powers on
again.

**Continuation calls** need only `request_id` and `continuation`, plus `hold_id` when a hold
was placed since; `hold_id` is outside the request digest (ADR 0190). A call from another agent id
gets "not found", the same answer as a nonexistent operation. A call from another connection is
refused.

**Deployment.** Only the process holding the execution lock runs logical mutations. Another
process refuses a logical mutation, naming the holder, and tries again on its next call. Any
process can write holds and read status. The CLI's operator commands list and abandon operations
under any agent id, and list and release holds (ADR 0190, ADR 0193).

## Persistent and live changes

- **Creation** writes the profile through #637's primitive, then reads it back before
  reporting `configured`. Proof (#1225) is that the configuration survives profile activation.
- **Reconfigure** computes every change before applying any:
  - a change applies `live` only when the partition is Running, RMC is active, and the value is
    within the current min/max;
  - otherwise the change is written to the profile and reported `pending_activation`;
  - a min/max change, or a processor-mode change, is always `pending_activation`, and the
    processor mode is never changed implicitly.
- **No reboot.** hmcpctl never reboots to apply a change. A `pending_activation` change is
  written to the profile and reported, and the preview names the activation it needs.

## Ownership and shared VIOS state

- Provision stamps ownership (ADR 0011) as a required step. Failing to stamp is `failed`, not
  a warning. Created disks and media are recorded in the store.
- VIOS and volume-group writes keep ADR 0169 and ADR 0171's read-modify-write with `If-Match`. No
  live observation shows the HMC enforcing `If-Match`; #879 closed without recording one, so #1230's
  native arms record it. Until a recorded observation exists, the VIOS and volume-group writes the
  logical tools add require `exclusive_writer_window=true`: provision with `install` (disk creation,
  upload, mount), decommission with `delete_owned`, and #1228's attach actions. That flag is the
  caller's assertion that an operator has paused other writers on those VIOS. Without it, planning
  reports a blocker, and the mutating tool refuses before recording any intent, naming the flag;
  `delete_owned` never degrades to `retain`. Provision requires the flag with or without
  `install`, because it writes a partition profile (ADR 0313); the specialist tools keep today's
  behavior and do not take the flag. A 412 response is `failed` and is not retried.
- Reconfigure's profile write requires `exclusive_writer_window=true` whenever the patch has
  `processors` or `memory_mib` (operator decision 2026-11-02; ADR 0242 unchanged, ADR 0314). The
  tool refuses before recording any intent, naming the flag; a rename alone does not take it.
- Decommission storage cleanup follows ADR 0192.

## Installation media and boot

ADR 0191 governs. The order of steps for `install` is (with `boot: deferred` the operation stops
after step 7 at `ready_to_boot` with the partition guard held, and `continuation: boot` runs steps
8–9 after revalidating the mount, boot order, binding and hold, and that the partition is powered
off with the owned root disk as its only disk):

1. create the partition and adapters;
2. record the client network adapter's actual MAC;
3. create and map the root disk;
4. bind media:
   - *prepared*: check the producer result against the request;
   - *built*: invoke the configured build entry with the manifest, then check its result;
5. upload with a digest check;
6. mount;
7. set the boot order to installer media first;
8. read the newest reference code as a baseline, then power on;
9. poll refcodes until a newer row (HMC time and row order) carries a code in the boot-started
   set, or `wait_seconds` runs out, then report `needs_attention` with the observed codes.

[ADR 0326](../../adr/0326-provision-boots-the-installer-once-and-polls-refcodes.md) fixes steps
8–9: the revalidation runs before any power-on in both timings, the baseline is effect
`refcode_baseline` (`lpar.refcode_baseline`), the power-on is ADR 0313's `power_on` effect, and
the poll reads the newest 20 rows every 15 seconds until the call's `wait_seconds` ends, at
least once.
The hold check before power-on joins with #1851.

hmcpctl never acquires the console during boot. A caller who wants the console from its first
byte uses `boot: deferred`, attaches their own console (HMC `mkvterm`/`vtmenu` or the `hmcpctl`
console CLI), then sends `continuation: boot`. Firmware that stops at SMS, Open Firmware or a menu
waiting for input reports `needs_attention` with its reference code; answering it is the caller's,
on their console.

The media name is `hmcpctl_<first 12 hex digits of operation_id>.iso`, which fits upload's
`[A-Za-z0-9_.]{1,79}` rule.

**Producer result fields iso-chain-loader#23 must emit** (a JSON object; `format`
`iso-chain-media-v1` refuses unknown keys, so an added field needs a new format value):

| Field | Built | Prepared | Value |
| --- | --- | --- | --- |
| `format` | yes | yes | `iso-chain-media-v1` |
| `iso_sha256`, `manifest_sha256` | yes | yes | 64 lower-case hex digits |
| `iso_size` | yes | yes | integer bytes |
| `distribution`, `release` | yes | yes | together equal `install.profile`: `ubuntu` + `26.04.1`, or `rocky` + `9.8` |
| `architecture` | yes | yes | `ppc64le` |
| `mac` | yes | yes | lower-case colon form |
| `network` | yes | yes | the same shape and values as `install.network` |
| `operation_binding` | yes | absent | the `operation_id` |
| `url` | yes | absent (the request carries it) | an origin in `HMC_ISO_URL_ALLOWLIST` |

Producer behavior hmcpctl relies on (ADR 0191 Decision 6): the launcher's default entry boots the
installed disk when it carries a boot record, after a bounded menu timeout, and the installer
otherwise; the installer refuses unless exactly one non-optical disk is present and blank. That is
what keeps the media-first boot order from reinstalling on the guest's reboot. Caller SSH keys and
the login user pass through to the producer untouched.

**Undecided:** the boot-started reference-code set and the refcode timestamp's zone handling.
#1850 fills both from native evidence. Until then `boot_started` is never reported.

**Not verified by hmcpctl:**

- that the VIOS copy matches the uploaded image, since there is no readback;
- that the guest can reach the installer source;
- every guest fact.

Results list each of these under `unverified`.

## Host handoff

ADR 0193 governs the hold:

- one per partition, keyed by system and partition UUID;
- checked in every partition-resolution path a mutation uses, through a hook the MCP server and CLI
  install and the library does not, so direct calls, `hmc_invoke_tool` and CLI commands all reach
  it;
- exempt: the handoff tool, console capture, and calls presenting the matching `hold_id`
  (`--hold-id` on the CLI);
- the `hold` result field carries label, agent id and creation time, and `hold_id` only for the
  agent that placed the hold, which passes it to the consumer; the `document` never carries it.

`prepare` without `hold` also reports an existing hold's label, agent id and creation time. The
`prepare` document has three groups of facts:

- `observed`: system and partition identity, state, resources, adapters, MACs, disks and
  mounted media, each from a read made during this call;
- `declared`: install profile, address and routes, plus the login user and key fingerprints
  for built media only, taken from the resource ledger;
- `unverified`: OS, kernel, bootloader, kdump/fadump readiness, SSH reachability and host
  identity, each with the statement that kdive's doctor/adopt checks it.

It also carries the hold, if any, and its scope statement. It carries connection and
credential *references* (profile name, agent id), never secrets. hmcpctl does not register
anything with kdive or allocate anything in it, and `release` never deletes the partition.

## Native envelope and proof

ADR 0194 governs. Proof is two separately authorized arms, `native-ubuntu-boot-started` and
`native-rocky-boot-started`. Each runs through `scripts/live_test_preflight.py` and
`scripts/live_test_recovery.py` against the deployed commit, and its evidence comes from
`scripts/live_test_evidence.py`. CI, QEMU and skipped arms do not count. This issue authorizes
no live run.

## Failure model

**Actors and deployments**

- An MCP client agent on a deployment with a served access policy and one store. Deployments named:
  one long-lived server, or stdio sessions and CLI commands on one host sharing one state
  directory. Logical mutations run one process at a time (the ADR 0190 execution lock).
- An operator with a CLI on the same host.
- Other HMC writers (GUI, other deployments, scripts), whose writes hmcpctl cannot see in advance.
- External consumers: kdive.

**Invariants and assets at stake**

- Unrelated VIOS and volume-group state.
- Existing guest disks.
- Shared media and physical volumes.
- Partitions owned by others.
- The meaning of `boot_started` and of handoff `observed` facts.
- Policy-withheld authority.
- The operation record.

**Accepted failure classes**

- *Writes by other HMC writers during an operation.* Accepted because this is not a fence;
  ADR 0011 and ADR 0190 state it, and the `exclusive_writer_window` bounds the VIOS writes the
  logical tools add.
- *Store or state-directory loss.* The ledger, the holds and the `request_id` dedupe are lost.
  Accepted:
  - a lost store with its `store-id` sentinel present is refused (ADR 0190);
  - deleting the whole directory is an operator action, like `release`;
  - ADR 0192 retains storage it has no record of.
- *Two clients sharing one agent id share operations.* Accepted because MCP carries no
  principal (ADR 0190).
- *A server process exiting mid-operation* (for example a stdio client closing). Accepted: the
  effect in flight becomes `uncertain`, and `resume` reconciles it.
- *Library callers (kdive) are not fenced by holds.* Accepted: stated in the handoff document
  (ADR 0193).
- *Reinstall on reboot if the producer misbehaves.* Covered by iso-chain-loader#23–#25's launcher
  and blank-disk contract; hmcpctl cannot observe it.
- *A Running guest whose install failed.* Out of scope by operator decision (ADR 0191).

**Covered elsewhere**

- Profile primitives: #637.
- `If-Match` enforcement and whether refcodes continue after Running: #1230's native arms
  (#879 closed without observing either).
- Media correctness and installer behavior: iso-chain-loader#23–#25, #6, #8.
- kdive adoption checks: kdive.
- Console leasing: ADR 0172 and kdive.

## Threat model

**Boundaries added**

1. Invoke gateway → any registered tool.
2. Build entry → host process execution.
3. Producer result file → binding decisions.
4. Store file → authorization of resume, cleanup and holds.

**Boundaries widened**

1. Decommission → storage deletion.
2. The upload URL now carries a binding digest.

**Actors**

- An authenticated MCP client holding a narrower grant than the server.
- A local user who can write the state directory or the build output.
- A network peer that serves the media URL.
- A misbehaving producer.

**Controls**

1. Per-tool `authorized()` re-entry; refusal of gateway and `arbitrary-command` tools; an
   identical denial for unknown and withheld names; arguments ≤ 64 KiB.
2. A configuration-only executable and argument template; only the manifest path varies; no
   shell.
3. A strict versioned schema (unknown or duplicate keys refused); every field compared with
   recorded effects; the result file ≤ 64 KiB.
4. A `0700` directory and `0600` file, checked at open; refusal when not private.

For the widened boundaries:

1. Ownership requires store provenance plus a live single-reference check, and incomplete
   inventory retains.
2. The allowlist, no redirects, and a digest compare before upload.

Errors name the failed check without echoing payloads.

**Out of scope**

- Fencing against independent HMC writers.
- A compromised HMC or VIOS.
- Guest-side compromise.
- Secrecy of public SSH keys.

## Success

Each of #1216's eleven completion criteria maps to one place:

- tool schemas and bounds: *Primary tools*;
- identity, resume, retention and restart: *Operations, resume and restart*, ADR 0190;
- authorization, including search and invoke: *Delegated authorization*, ADR 0189;
- ownership, cleanup, persistent versus live: *Persistent and live changes*, *Ownership and
  shared VIOS state*, ADR 0192;
- media binding, builder invocation and the installation boundary, with caller SSH keys as
  opaque producer inputs: *Installation media and boot*, ADR 0191;
- hold and release: *Host handoff*, ADR 0193;
- releases, native envelope and proof arms: *Native envelope and proof*, ADR 0194;
- reconciliation with accepted ADRs: the Status sections of ADR 0189 (ADR 0012, including the
  primary tools' naming), ADR 0192 (ADR 0027), and ADR 0190 (ADR 0005 and ADR 0027: `request_id`
  becomes required);
- facade, workflow language and reuse: *Purpose and boundary*. The logical tools compose the
  existing functions under `src/hmcpctl/operations/`, and `hmcpctl.api` is unchanged.

Each value this spec marks **undecided** names the issue that decides it.

## Follow-ups this decision creates

This PR edits none of them. After it merges, the quest amends each body below, closes #1222 as
not planned, and adds a provenance comment, as the operator decided on 2026-10-01.

| Item | Conflict |
| --- | --- |
| #1222 | Guest SSH readiness is obsolete under ADR 0191; it should be closed or rescoped. |
| #1230 | Acceptance becomes `boot_started` per distro: fill the boot-started code set, settle the refcode timestamp's zone, and record `If-Match` enforcement and refcodes after Running. It also drops the `guest_ready` requirement and its block on #1222. |
| Epic #1215 | Requirement 7 and the SSH success criteria conflict with ADR 0191. |
| iso-chain-loader#24, #25 | The "host-identity handoff" wording conflicts. They should inject caller keys only, refuse unless exactly one blank disk is present, and default the launcher to an installed disk. |
| iso-chain-loader#23 | Must emit the producer result fields above. |
| #1228 | Gains the `installer_media: detach` and `boot: root_disk` patch fields. |
| #1231 / `docs/kdive-tier-a-contract.md` | MCP power calls on a held partition need `hold_id`; the contract doc and its test say so. |
