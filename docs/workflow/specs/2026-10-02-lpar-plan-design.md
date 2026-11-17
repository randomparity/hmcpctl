# Planning an LPAR: `hmc_plan_lpar`

**Issue:** #1221 (H5 of epic #1215)
**Decision:** [ADR 0198](../../adr/0198-lpar-plan-delegates-to-the-reads-it-makes.md), which
replaces the plan row of [ADR 0189](../../adr/0189-logical-lpar-catalog-and-delegated-authorization.md)
Decision 2
**Contract:** the `hmc_plan_lpar` row of
[the logical LPAR workflows spec](2026-10-01-logical-lpar-workflows-design.md#primary-tools)
**Branch:** `feat/plan-lpar-1221` from `main`
**Guardrails:** `just verify`; `uv run --no-sync prek run --all-files`

## Problem

`find_placement` (`operations/inventory/capacity.py`) ranks systems by free memory and processor
units only. `provision_lpar`'s preflight checks the name, the VLAN and a named volume group, and
raises on the first failure. Nothing tells an agent, before it writes anything, whether the
request fits a system, which VIOS and volume group it would use, which install inputs are wrong,
or what provisioning would change.

## Scope

One primary read tool, `hmc_plan_lpar`, for one connection per call. It reads and never writes,
and it reserves nothing. It also defines the request types and the digest function that #1225's
provisioning consumes.

Excluded, with their owners (operator-approved 2026-10-02):

- provision consuming the plan: `expected_plan_digest`, `request_id`, the store, and execution
  revalidation (#1225);
- reconfigure planning (#1226);
- producer-result binding, media build and upload (#1227);
- the boot-started code set (#1230);
- search and invoke (#1219);
- the default listing switch (#1232);
- a CLI mirror (none);
- reservation (never);
- any `hmcpctl.api` facade change (ADR 0118);
- any live run.

### Inputs

`PlanRequest` holds the request fields, all in `operations/lpar/plan.py`:

| Input | Rule |
| --- | --- |
| `name` | the new partition name, as `hmc_create_lpar` takes it |
| `system_name_or_uuid` | one selector; exactly one of this and `placement` |
| `placement` | `{systems?: list[str]}`: 1–16 non-empty selectors, or omitted to enumerate the connection's systems |
| `resources` | `LparResources`; the served parameter has `hmc_provision_lpar`'s default; the shared-processor vCPU rule (`shared_units_over_vcpus`) applies |
| `partition_type` | `AIX/Linux` (default) or `OS400`, as `validate_partition_type` allows |
| `adapters` | `PlanAdapters`: `port_vlan_id` (1–4094) and `mac?` |
| `storage` | `PlanStorage`: `storage_name`, `kind` (`VirtualDisk` default, or `PhysicalVolume`), `vios_uuid?`, `vg_uuid?`, `capacity_mib?` |
| `assignments` | `LparPcieAssignments`, default empty, as provision takes it |
| `caller_token` | optional; `validate_caller_token`'s grammar, where `''` is an error |
| `minimum_affinity_policy` | optional; `validate_minimum_affinity_policy` |
| `affinity_assessment` | optional; `validate_affinity_request`, and its system and LPAR identities must equal `system_name_or_uuid` and `name`, so it requires `system_name_or_uuid` |
| `power_on` | optional bool; omitted means true without `install`; `true` with `install` is an input error (H1 spec, *Primary tools*) |
| `boot` | `immediate` (default) or `deferred`; `deferred` without `install` is an input error |
| `install` | optional `LparInstall` (below) |
| `exclusive_writer_window` | bool, default false |

These are provision's inputs (H1 spec, `hmc_provision_lpar` row) minus `dry_run`,
`expected_plan_digest` and the continuation fields, which only an execution has. The connection
(`profile`) is the tool's argument, not a request field.

`storage.capacity_mib` means "create this new disk". With it, `kind` must be `VirtualDisk` and the
disk name must be absent on the chosen VIOS. Without it, the named storage must already exist.
`vg_uuid` without `vios_uuid` is an input error, because a volume group belongs to one VIOS.

Each dedicated PCIe assignment must name the profile provision saves, `default_profile`
(ADR 0318). A mismatch raises an input error identifying `assignments.dedicated.profile_name`.
The error omits the rejected value.

`LparInstall` holds the install fields:

- `profile`: `ubuntu-26.04.1` or `rocky-9.8` (ADR 0194);
- `network`, an `InstallNetwork` with these parts:
  - `address`: an IPv4 interface in CIDR form, prefix 1–32;
  - `routes`: 1–16 entries of `{destination, gateway}`, where `destination` is an IPv4 CIDR
    network, exactly one route is `0.0.0.0/0`, and every `gateway` is an IPv4 address inside
    `address`'s network;
  - `dns`: 0–3 IPv4 addresses;
- `ssh_authorized_keys`: 1–16 printable strings (no line break or control character), each
  1–8192 characters;
- `login_user`: 1–32 characters matching `[a-z_][a-z0-9_-]*`;
- `media`, an `InstallMedia` with `mode` and the fields that mode needs:
  - `built`: no other field. `ssh_authorized_keys` and `login_user` are both required.
  - `prepared`: an `http` or `https` `url` and a `producer_result` JSON object serialized to at
    most 64 KiB. `ssh_authorized_keys` and `login_user` must be absent, and `adapters.mac` is
    required.

`install` requires `storage.capacity_mib`, because the installer gets a new owned disk (ADR
0191 Decision 6). `adapters.mac` is accepted only with prepared media. It must be in lower-case
colon form, with no group or broadcast bit (`int(first octet) & 1 == 0`).

Every rule above is checked before the HMC session opens. A violation of one of these rules is
a tool error that names the first bad input and echoes no key, URL or producer-result content. A
value of the wrong JSON type fails the MCP schema first, and that framework error can echo the
value, as it does for every tool. A blank `system_name_or_uuid`,
`adapters.mac`, `storage.vios_uuid` or `storage.vg_uuid` reads as absent (ADR 0094); the
normalization lives in `check_request`, which `plan_digest` also applies, so a caller of either
gets the same request. `caller_token` is not normalized.

### Authorization (ADR 0198)

The handler first checks that the policy permits every delegated tool the request needs:
`hmc_list_lpars`, `hmc_capacity_report`, `hmc_list_virtual_networks`, `hmc_list_vios` and
`hmc_list_volume_groups`, plus `hmc_list_systems` when `placement` enumerates and
`hmc_get_vios_storage_detail` when storage already exists (no `capacity_mib`). The first
withheld tool refuses the call before the session opens, and the refusal names that tool.

Each read is then admitted through `dispatch_authorizer`, as its tool, for its target:

- the system, for the three system-kind tools (`hmc_list_lpars`, `hmc_list_virtual_networks`,
  `hmc_list_vios`), as `system_name_or_uuid`. The value is the caller's selector text when the
  caller named the system, and the system UUID when `placement` enumerated it (ADR 0196's rule);
- each VIOS, for the two VIOS tools, as `vios_name_or_uuid` set to the VIOS UUID and
  `system_name_or_uuid` spelled as above;
- no target, for the console tools `hmc_list_systems` and `hmc_capacity_report`.

Target scope matches spellings literally, so a targets table admits a VIOS read only when it
lists the VIOS UUID. A denied target becomes a `denied` blocker naming the tool, and that target
is not read. `hmc_capacity_report` is a console tool, and the policy compiler admits it only
under `all-targets`, so planning under a targets table needs a second grant of it there.

`hmc_plan_lpar` registers as `read`, `operation="lpar.plan"`, `target_kind="console"`, with
`system_name_or_uuid` and the nested `storage.vios_uuid` declared as selectors, so the audit
record names them, and `exhaustive_targets=False`. Placement names no system, and the VIOS and
volume group sit below its signature, so the delegated tools carry the target bound, as with
ADR 0196 Decision 4.

### Checks

Each candidate system is checked in this order. A failed check adds a blocker, and the later
checks still run when their inputs exist.

1. **System.** `State` is `operating`. If not, this is the only check for that system.
2. **Capacity** (`system_capacity`).
   - `desired_memory` must not exceed free memory.
   - `desired_procs` must not exceed free processor units. The same comparison applies to
     dedicated whole processors, since a dedicated processor consumes one unit.
   - An omitted desired figure is `unverified`.
   - A missing or unparseable system figure (`system_capacity` raises `ValueError`) is an
     `unavailable` blocker for `hmc_capacity_report`, never zero.
3. **Name.** No partition on this system is named `name`, read from the system's partition
   feed (`hmc_list_lpars`). Provision's preflight refuses the name on any system of the
   connection; planning reads only candidate systems, so `unverified` says so.
4. **VLAN.** A `VirtualNetwork` on the system carries `port_vlan_id` (`_check_vlan_exists`'s
   rule).
5. **VIOS and volume group.** The candidate VIOSes are the system's VIOSes with
   `PartitionState` `running`, or only `vios_uuid` when it is given.
   - A given `vios_uuid` that is not running, or not on this system, is a blocker.
   - A VIOS qualifies when its volume groups (the given `vg_uuid` only, when given) include:
     - for a new disk, a volume group whose `free_space_gib × 1024` is at least `capacity_mib`,
       provided no volume group on that VIOS already holds a virtual disk named `storage_name`;
     - for existing `VirtualDisk` storage, the volume group that holds that disk.
   - Existing `PhysicalVolume` storage qualifies a VIOS only when it is named in `vios_uuid`.
     Its existence is `unverified`, because no delegated read lists unassigned physical volumes.
   - Exactly one qualifying (VIOS, volume group) pair resolves. Zero is a blocker. More than one
     is an `ambiguous` blocker that lists at most 5 pairs as `<VIOS UUID>/<volume-group
     UUID>`, which fit the detail's 500 characters, and asks the caller to name one; it
     never takes the first.
   - A `VolumeGroup` with an unreadable `FreeSpace` (`free_space_diagnostic` set, or null) cannot
     qualify for a new disk, and the blocker says why.
6. **Existing mapping.** For existing storage, the resolved VIOS's SCSI mappings
   (`hmc_get_vios_storage_detail`, read with `_storage_mapping`'s rule) must not map
   `storage_name` to any partition.
7. **Writer window.** With `install`, `exclusive_writer_window` must be true. Otherwise the
   check adds the blocker `exclusive_writer_window_required` (H1 spec, *Ownership and shared
   VIOS state*).
8. **Prepared URL.** The prepared `url`'s host must be in `HMC_ISO_URL_ALLOWLIST`
   (`_require_allowlisted_iso_url` over `iso_url_allowlist_entries`; its `ValueError` and
   `HMCError` both mean not allowed). This is a request-level blocker, checked once. Its `detail`
   is fixed text naming `HMC_ISO_URL_ALLOWLIST`, never the URL or the allowlist.

An HMC read failure for a check becomes an `unavailable` blocker naming the tool. Planning never
retries. A transport failure stops reading further candidates, and each unread candidate is
reported `unavailable`.

### Placement

`placement` evaluates at most 16 systems:

- the `systems` selectors, deduplicated by spelling and then by resolved UUID, each admitted as
  `hmc_list_lpars` before it is resolved (ADR 0196 Decision 3's rule); a name several systems
  share is an `unavailable` candidate; or
- the connection's enumerated systems, in UUID order. More than 16 sets `candidates_truncated`,
  and the rest are not read.

Every evaluated candidate runs every check. Candidates are ranked by `find_placement`'s key:
ascending free memory, then free processor units, then name, then UUID, with unknown capacity
last. The first candidate with no blockers is selected. With `system_name_or_uuid`, the one
system is the only candidate.

An `ambiguous` candidate is blocked, so a lower-ranked unblocked candidate can be selected over
a better fit whose storage pair was ambiguous. That candidate stays in `candidates[]` with the
pairs listed; a caller who prefers it re-plans naming the system and the pair.

### Result

`LparPlan`:

- `connection`: the policy label for `profile`.
- `plan_digest`: present only when a candidate is selected and the plan has no request-level
  blocker; `null` otherwise.
- `selected`: the `PlanTargets` of the selected candidate, or `null`. `PlanTargets` holds:
  - `system`: `{id, uuid, name}`;
  - `vios`: `{uuid, name}`, or `null`;
  - `volume_group`: `{uuid, name}`, or `null`.
- `blockers[]`: request-level blockers (check 7, check 8, `denied` or `unavailable` for
  `hmc_list_systems` when enumeration is refused or fails, and `no_candidate`).
- `candidates[]`: one `PlanCandidate` per evaluated system, in rank order, each with these
  fields:
  - `targets`;
  - `free_memory_mib` and `free_proc_units`, `null` when unknown;
  - `blockers[]`, at most 16, then `blockers_truncated`.
- `candidates_limit` (16) and `candidates_truncated`.
- `intended_changes[]`: the changes provisioning would make on the selected targets, in order.
  The list is empty when nothing is selected.
- `unverified[]`.

A `PlanBlocker` is `{code, check, target, tool, detail}`:

- `code` is one of `denied`, `unavailable`, `system_not_operating`, `insufficient_memory`,
  `insufficient_processors`, `name_exists`, `vlan_missing`, `vios_not_running`,
  `storage_unplaceable`, `ambiguous`, `storage_mapped`, `exclusive_writer_window_required`,
  `url_not_allowlisted`, `no_candidate`;
- `check` is the check's name;
- `target` is the scoped id or `null`;
- `tool` is the delegated tool for `denied` and `unavailable`, otherwise `null`;
- `detail` is at most 500 characters.

`intended_changes[]` entries are `{order, kind, target, detail}`. The list describes the writes
the H1 spec's provision row names; it is not in the digest, and #1225 owns the execution order.
Their `kind` values, in order:

1. `create_partition`;
2. `stamp_ownership`;
3. `set_minimum_affinity_policy`, with `minimum_affinity_policy` only;
4. `add_network_adapter`;
5. `add_vscsi_adapter`;
6. `create_virtual_disk`, with `capacity_mib` only;
7. `map_storage`;
8. `assign_pcie`, once, with the count of each `assignments` collection, when any is set;
9. `write_profile` (#637's profile write).

Without `install`, these follow:

10. `power_on`, with `power_on` only;
11. `assess_affinity`, with `affinity_assessment` only.

With `install`, these follow instead:

10. `bind_media`;
11. `upload_media`;
12. `mount_media`;
13. `set_boot_order`;
14. `power_on`, whose `detail` says that `boot: deferred` stops before it;
15. `assess_affinity`, with `affinity_assessment` only.

`unverified[]` is a list of fixed statements. Each one is included when its condition holds:

- always: the capacity is observed, not reserved, and may change before provisioning;
- desired memory or processors omitted: the HMC's default for that figure is not checked;
- always: that no partition on a non-candidate system of the connection has the name;
- `PhysicalVolume` storage: the physical volume's existence;
- `assignments` not empty: PCIe, SR-IOV and vNIC prevalidation, which provision runs over SSH;
- `minimum_affinity_policy`: the system's support for it, which provision checks over SSH;
- with `install`, all of these:
  - the native envelope (POWER9/POWER10, HMC V10R3 M1060 or V11R2, VIOS level;
    ADR 0194);
  - the media binding and producer result (#1227), and the built-media build entry;
  - that the VIOS copy matches the upload;
  - that the guest can reach the installer source;
  - `boot_started`, which no operation reports until #1230.

### Digest

`plan_digest(request, targets, connection) -> str` is the lower-case hex SHA-256 of
`json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`. `document`
holds these keys:

- `"format": "hmc-lpar-plan-v1"`;
- `"connection"`;
- `"request"`: every `PlanRequest` field except `placement`, after `check_request`'s
  normalization, with
  `system_name_or_uuid`, `storage.vios_uuid` and `storage.vg_uuid` replaced by the selected
  UUIDs in lower case;
- `"targets"`: those three UUIDs.

`None` fields are kept as `null`; a `datetime` is encoded as its ISO 8601 text and a `Decimal` as its string.
A field whose meaning depends on another is hashed as what it means: without `install`,
`power_on` is its effective boolean and `exclusive_writer_window` is `false`; with `install`,
`power_on` is `null`; and `affinity_assessment.system_name_or_uuid` is the selected system UUID. Provision (#1225) will build the same `PlanRequest` from its
own inputs and call this function; until it lands, nothing consumes the digest. The digest binds the request and the targets, not the
observations. Revalidation is a fresh plan at execution time (#1225).

### Failure model

**Actors and deployments**

- An MCP client agent on a server with a served access policy, one connection per call.
- Concurrent HMC writers: the GUI, other deployments and scripts.

**Invariants and assets at stake**

- No HMC write and no reservation from planning.
- Policy-withheld authority: no read the policy denies, and no data from a denied target.
- The resolved VIOS and volume group: never a first match among several.
- The digest contract #1225 recomputes.

**Accepted failure classes**

- *State changing between plan and provision*: accepted, because the plan is not a guarantee.
  `unverified` says so, and #1225 re-plans.
- *The same name on a non-candidate system*: accepted, because planning reads only candidate
  systems. `unverified` says so, and provision's preflight refuses the name before any write.
- *PCIe assignments and the minimum-affinity capability not checked*: accepted, because they
  need SSH reads outside the plan row. `unverified` lists them, and provision's preflight checks
  them before any write.
- *Systems beyond 16 not evaluated*: accepted, because the bound is stated and
  `candidates_truncated` reports it.
- *Native envelope and physical-volume existence not checked*: accepted, because no delegated
  read provides them and `unverified` lists them.

**Covered elsewhere**

- Producer-result binding: #1227.
- Execution revalidation and digest refusal: #1225.
- Boot-started codes: #1230.

### Threat model

**Boundaries added**

1. Caller input to request validation: the install network, keys, URL and producer result.
2. A logical tool to the delegated reads.

**Actors**

- An authenticated MCP client holding a narrower grant than the server.

**Controls**

1. Bounded, typed inputs checked before the session opens, with errors that echo no key, URL
   or producer-result content; a network address the caller sent can appear in its own error.
   The producer result is only size- and type-checked; it is never parsed for binding here.
2. All-or-nothing permits plus per-target admission (ADR 0198), with an ADR 0040 record per
   admission; a permit refusal is a tool error naming the tool, with no record of its own. A
   denied target is never read.

**Out of scope**

- Fencing against concurrent writers.
- A compromised HMC.

## Success

- An explicit request on an operating system with capacity, one running VIOS whose one volume
  group fits, and an existing VLAN returns a digest, `selected` targets, the intended changes and
  no blockers.
- Each check in *Checks* produces its blocker code under the condition it states, and no other
  blocker for that condition.
- Two qualifying VIOS or volume-group pairs produce `ambiguous`, with no selection and a null
  digest.
- Placement over several systems selects the first unblocked candidate in rank order and lists
  every evaluated candidate with its blockers.
- A withheld delegated tool refuses the call, naming the tool. A denied target becomes a `denied`
  blocker and is not read.
- Each input rule rejects its violation before any HMC request.
- The digest is stable under argument order, selector spelling and a blank versus absent
  optional string, and changes with any request field or resolved UUID.
- Planning issues no HMC write. The test fake defines only the read methods planning may call,
  and each scenario asserts that its call log contains only those.

## Validation

Tests in `tests/unit/test_lpar_plan.py` drive the operation with a fake HMC client. Tests in
`tests/app/test_lpar_plan_tool.py` drive the served tool over MCP with real access policies and a
patched client, as `tests/app/test_logical_inventory_tool.py` does. They cover registration, the
policy refusal, target denial, the audit records and the schema. `just tool-docs` and the capability inventory
regenerate the generated docs. `docs/mcp-server.md` and `CHANGELOG.md` gain entries.
