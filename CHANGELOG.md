# Changelog

All notable user-facing changes to this project are recorded in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/): changes collect under
`[Unreleased]` in its usual categories until a release is cut. Each release's entry keeps those
itemized changes and adds a scope statement, a **Verified** section naming the operations
verified live for it, and a **Known issues** section, in the order set out under
[Docs and changelog](docs/development.md#docs-and-changelog). The entry is also the GitHub
release body.

0.1.0 is the exception. As the first release it has no itemized changes: nothing shipped before
it, and its pre-release development history is in the git history, not in this file.

The six-name `hmcpctl.api` facade is defined by ADR 0118; record changes to it under ordinary
release categories. Domain-module APIs remain pre-release and are not facade movement.

## [Unreleased]

### Added

- LPAR and VIOS creation through CLI/MCP accept eight typed shared-memory and
  saved Active Memory Expansion fields. Shared mode requires an existing pool,
  contained paging providers and independent ownership checks. Saved/current
  configuration verification stops follow-ups on missing evidence without replay.
  Saved AME accepts zero or numeric 1–10 without declaring AIX intent; guest
  compression remains AIX-only runtime guidance, unproven by saved acceptance.
  The new creation variants remain unevidenced on hardware (#1768).

- Native partition GZIP accelerator QoS through `hmc_set_lpar_accelerator_qos`
  and `lpars accelerator-qos` preserves explicit zero and optional native controls.
  Selected-system admission and ownership checks precede one submission; independent
  current/pending readback reports incomplete outcomes without automatic replay.
  Runtime effectiveness and profile persistence remain unverified. The opt-in arm 64
  procedure has offline coverage; hardware contexts remain unevidenced (#1781).

- LPAR and VIOS creation through CLI/MCP accept typed virtual serial adapters
  and an explicit maximum virtual-slot prerequisite. Contained peer ownership,
  one native create and independent saved-profile observations distinguish
  configured, unbound, paired and incomplete outcomes without replay. The
  standalone ST63 procedure remains unevidenced on hardware (#1772).

- Native processor moves through `hmc_move_lpar_processors` and
  `lpars processor-move` support all four dedicated/shared mode pairs with explicit
  count or entitlement inputs. Both endpoints require authorization and independent
  readback; partial or uncertain effects are reported without replay or rollback.
  The opt-in ST61 arm verifies only its declared running shared/shared case when
  executed; no live hardware evidence is claimed by its offline tests (#1779).

- LPAR processor updates and combined modify through CLI/MCP accept sharing
  modes and strict integer `uncapped_weight` values from 0–255, preserving zero
  and omission. Independent configured/current readback determines completion;
  runtime is observed separately. Existing HMC/context guards still apply; new
  variants remain unevidenced on hardware
  ([#1780](https://github.com/randomparity/hmcpctl/issues/1780)).

- Native LPAR processor add/remove/set quantities through `hmc_dlpar_proc(change=...)`
  and `lpars processors` distinguish whole dedicated CPUs/shared virtual processors
  from shared entitlement. Ownership, capacity and state checks precede mutation;
  independent readback reports applied, partial or uncertain outcomes without replay
  (#1778). The opt-in arm 56 has offline coverage; native quantity contexts remain
  unverified on hardware. Existing absolute resource setters retain their behavior.
- LPAR and VIOS creation accept optional `affinity_group_id` as a strict integer
  1–255 or literal `none`, preserving omission. Native creation uses selected-system
  authority and independently verified classic saved-profile configuration. Native
  partition mismatch/unavailability is reported separately; saved or identity uncertainty
  blocks dependent writes without replay. Controller saved proof remains incomplete.
  These observations do not establish current/pending semantics, runtime or placement.
  Opt-in arm 57 exercises serial
  inactive fixtures; affinity hardware evidence remains unrun (#1769).

- LPAR and VIOS creation through CLI/MCP accept optional strict booleans
  `hardware_mem_encryption` and `hardware_mem_expansion`, preserving omission and
  explicit false. Selected-system admission guards native creation; independent
  inactive REST readback compares the named hardware access fields and reports
  uncertain or mismatched outcomes without replay. This establishes access
  availability, not runtime encryption or expansion. Opt-in arm 55 documents one
  owned AIX/Linux true/true fixture; implementation maturity is separate from live
  evidence ([#1771](https://github.com/randomparity/hmcpctl/issues/1771)).

- Native two-target dedicated-memory moves are available through
  `hmc_move_lpar_memory` and `lpars memory-move`, with strict admission and
  authorization for both partitions, independent dual observation, and explicit
  partial/unverified outcomes without automatic replay or compensation (#1775).
  Live hardware contexts remain unevidenced.

- Native LPAR memory add/remove/set quantities and shared I/O entitlement inputs
  are available through `hmc_dlpar_mem(change=...)` and `lpars memory`, with
  guarded admission and independent applied/partial/uncertain readback (#1774).

- Typed secure-boot and keystore policy for LPAR/VIOS creation, reads and updates
  through CLI/MCP (#1765). Strict inline JSON preserves omitted settings and
  explicit false/zero, with selected-system capability/release and ownership
  admission. Signed-update/Linux enables are irreversible; PK bypass skips
  old-key authentication. Readback separates pending from current secure boot
  and reports uncertain outcomes without replay. Configuration proves neither
  guest OS/runtime enforcement nor key readiness; hardware evidence remains
  separate from implementation maturity.

- Typed vTPM creation and inactive updates expose `vtpm_enabled`, `vtpm_version`
  and `vtpm_encryption` for AIX/Linux partitions and VIOS through CLI/MCP.
  Values are admitted against the selected system's advertisements; disabling an
  enabled instance requires separate data-loss acknowledgement. Readback reports
  verified, mismatched or uncertain outcomes without replay or rollback (#1764).
  Opt-in arm 51 exercises one disposable inactive AIX/Linux variant; hardware
  evidence remains separate from implementation maturity.

- Partition creation and reporting configuration expose the five C02 fields in
  their documented contexts, with IBM i admission, explicit false/zero/none,
  ownership guards and verified/uncertain outcomes (#1763). Creation-only
  redundant reporting and update-only IPL remain distinct. Live behavior stays
  unevidenced until commit-bound execution.

- LPAR creation and provisioning accept typed physical-slot/pool and IBM i tagged
  I/O inputs, with explicit HMC-console selection, reused direct SR-IOV declarations,
  fresh relationship checks and native profile verification before application.
  Unknown or partial outcomes stop dependent writes without rollback. Positive
  virtual-slot forms without creation-time adapter linkage remain rejected and
  tracked for #638 reconciliation; hardware evidence is separate (#1773).

- LPAR create/configure supports five typed performance, availability-priority,
  time-reference, simplified-restart and migration flags, with explicit False/0,
  owning authorization and verified/uncertain configuration outcomes (#1762).
  Live coverage remains separate from implementation.

- Saved LPAR and VIOS profile CLI/MCP lifecycle operations provide contained reads,
  typed ETag updates, current-configuration saves with verified retained recovery,
  default selection and guarded deletion. Outcomes separate requested changes,
  preservation drift and execution uncertainty; native product verification is
  not yet run (#1724).

- System-profile CLI and MCP operations bind complete membership, authorize every
  member, and reconcile create, rename/replacement, delete, validation and activation
  outcomes. Activation exposes bounded profile witnesses and explicit uncertainty;
  native product verification remains unrun (#1725).


- `HMC_CERT_FINGERPRINT` (TOML `cert_fingerprint`, `--cert-fingerprint`) trusts an HMC whose
  certificate has the given SHA-256 fingerprint, so a self-signed HMC can be verified without
  a CA bundle. It takes effect with `HMC_VERIFY_SSL=true`, yields only to `HMC_CA_BUNDLE`, and
  refuses a mismatched certificate before any request is sent. Each trusted connection emits
  a `tls-fingerprint-verified` audit record (#1614, ADR 0224).

- SSH dispatch accepts the secret values a command carries and redacts them from
  timeout, connection, process-failure and SSH-library log text; `-i` record refusals
  can omit a secret attribute's value. No operation uses it yet (#655).

- `HMCClient.list_quick_all` fetches `/rest/api/uom/{Type}/quick/All`, or the
  `ManagedSystem/{uuid}/{Child}/quick/All` child form, as one JSON array. No caller
  uses it yet (#244).

- Profile backup artifacts have bounded CLI/MCP listing and exact-name deletion
  with fresh readback. Restore adds explicit keystore-error handling while retaining
  warning output, existing ownership/audit and initialization denial (#1726).
  Failed jobs also retain the HMC's lowercase `errorData` diagnostic spelling.

- Existing-code baseline reconciles all registered operation maturity and automated-test
  links, retaining explicit native gaps and historical failures without promotion (#635).

- Migration and remote-restart workflow verification records bounded implementation
  scope, endpoint/failure assertions and explicit unrun native obligations (#631).

- Offline POWER9 progress-code lookup and 2,202 independently written definitions,
  with public IBM page/code provenance. CLI, MCP and partition inspection reference-code
  rows carry definitions and explicit unknowns while preserving raw codes (#1690).

- Bare-CEC live testing exercises the installed CLI state, reference-code and job-show
  paths with separate asserted CLI observations. Release-window reports state CLI
  coverage or gaps for each claim; v0.1.0 remains MCP-only evidence (#1477).

- Run an explicitly authorized release window with `scripts/run_release_window.py`,
  composing named arms, recovery, strict baselines, private evidence and local promotion;
  dry-run derives the claimed arms without hardware access (#1513).

- Promote completed live-window sidecars with `scripts/promote_live_observations.py`,
  producing a new local branch with validated catalog, release snapshot, packaged
  projection and generated tool documentation; raw results stay uncommitted. Validated,
  unclaimed observations without existing catalog records are reported and omitted (#1512).

- `scripts/check_capability_inventory.py --require-current FILE` gates a release
  on its validated claimed operations, naming non-current evidence and applying
  candidate closure and 90-day age checks without blocking on unclaimed states (#1478).

- Retain release claimed operations with their live-test arms and scenarios, beginning
  with v0.1.0; `just capability-inventory` validates every retained set (#1511).

- `scripts/live_test_baseline.py` captures private, read-only live-window baselines
  and compares independent before/after state, including explicitly selected VIOS
  mappings and volume-group volumes (#1479).

### Changed

- Builds derive the package version from Git tags with `hatch-vcs`: a release tag `vX.Y.Z`
  builds `X.Y.Z`, and every other commit builds `X.Y.(Z+1).devN+g<sha>`, so a development
  build no longer reports the last release's version. Building a tree without Git metadata
  needs `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_HMCPCTL` (#1471, ADR 0250).
- **Breaking:** unscoped `hmc_list_lpars` and `lpar list --json` read one
  `LogicalPartition/quick/All` request when every managed system is `operating` or
  `power off`. Those entries have null `title`/`link` and carry the partition summary as
  `Resource` (`RMCState`, numeric `PartitionID`, no nested configuration), and powered-off
  systems' partitions are listed. Other fleets keep the per-system read, as does a failed
  bulk read, which warns with its HTTP status or failure class (ADR 0248, #1731).

- The HTTP 500 "Nested path contains null property" errors from a managed-system read by UUID
  and from the management-console read now name an `X-HMC-Schema-Version` pin as the likely
  cause and tell the operator to unset `HMC_SCHEMA_VERSION`, instead of advising a firmware
  update (#1648).

- Release and live-testing runbooks now use the authorized release-window command,
  claimed-set gate and tag-time snapshot validation, with explicit recovery and
  promotion review. Ordinary development needs no live rerun (#1514).

- Round2 live testing preserves the configured partition and disk while provisioning
  temporary owned fixtures. Recovery independently compares a fresh system/VIOS baseline
  and original partition properties; missing legacy witnesses fail closed (#1513).

- The `app` extra now pins `fastmcp-slim[server]==4.0.10`, up from 4.0.3; `mcp` and `mcp-types`
  stay at 2.2.0 (#1539).
- Generated tool-reference verification retains the last release’s state regardless of the
  rendering date, with a regression check spanning more than 90 days (#1509).
- CLI and MCP capability discovery retain the last release’s verification state, initially
  v0.1.0, without runtime age decay. Ordinary capability metadata regeneration combines current
  catalog structure with the committed release snapshot; explicit `--release` inventory and
  artifact checks detect candidate closure drift (#1508).
- `hmc_provision_lpar` and `hmcpctl lpars provision` refuse a dedicated PCIe slot
  (`assignments.dedicated`) before any HMC call, dry runs included. Provision powers on the
  current configuration, so a slot written into the new profile was never owned by the
  running partition: the 0.1.0 known issue (#1390). To give a new partition a dedicated slot,
  create it with `hmc_create_lpar` (optionally with `assignments.dedicated`) or assign the slot
  with `hmc_assign_dedicated_pcie_slot`, then power on with the partition profile, as the
  [bare-CEC LPAR recipe](docs/recipes/bare-cec-lpar.md) does. Profile-first provisioning, which
  would lift the refusal, is tracked in #1225.
- `hmc_remove_memory_pool` and `hmcpctl memory-pools remove` no longer take a pool name. A
  managed system has at most one shared memory pool, so the remove now sends the documented
  `chhwres -r mempool -m <system> -o r`. It refuses when the system has no pool, and when any
  partition or profile does not report `mem_mode=ded` (#1245).
- `hmc_create_lpar`'s duplicate-name check and read-back, and `hmc_provision_lpar`'s duplicate-name
  check, read the target managed system instead of searching the HMC, so they refuse before any
  write when the target cannot be read. A partition of the same name on another managed system no
  longer blocks the create. An unscoped lookup of such a name reports the ambiguity, except while
  one of the two systems cannot be read, when it finds the other; pass the system selector (#1309).
- `hmcpctl.documents` no longer re-exports its builders: import each from its owning submodule.
  The logon document builder is now in `hmcpctl.documents.logon` and the RemoteAccess builders
  in `hmcpctl.documents.remote_access`; `hmcpctl.documents.access` keeps the UserProfile builder.
  A change to the UserProfile builder no longer marks every operation's live verification
  stale, only those of the operations that import it (#1495).

### Fixed

- `HMC_AGENT_ID` help text and the config schema now list every character `agent_id`
  refuses, including `/`, `:` and space; the text is built from the validator's own
  table so the two cannot drift (#1476, thanks to @KeuqS for the approach in #1485).

- Migration authorizes and submits the partition UUID selected for validation,
  preventing a reassigned partition name from selecting another partition (#631).

- A failed vSCSI or virtual-optical mapping create no longer says that `list-mappings` does not
  show a leftover VIOS server adapter. A partition-scoped listing omits it, an unscoped one
  shows it with no identity, and `hmc_list_optical_mappings` never shows it. The note still
  points at the `lshwres` server-adapter listing (#1537).
- `hmc_create_lpar` and `hmc_provision_lpar` refuse promptly when the target managed system is not
  operating. Their name check, and every listing or lookup that finds a system's partition or VIOS
  feed empty, now reads the system's `State` from `ManagedSystem/quick/All` instead of
  `ManagedSystem/{uuid}`, which can stall for a system with no connection until `HMC_TIMEOUT`. The
  error names the system, its `State` and any non-blank `StateDetail` (it no longer carries
  `DetailedState`), and asks you to retry when the system reconnects (#1500).
- A managed system given by name is resolved from `ManagedSystem/quick/All` instead of the
  `SystemName` search, and an ambiguous LPAR or VIOS name without a managed-system selector finds
  each candidate's system from `quick/All` and reads only the operating systems. Both stalled for
  minutes on an HMC with a system in `No Connection`. A candidate found on no operating system
  now names the systems that were not read (#1501).
- Tools that turn a managed-system UUID into its name for an HMC CLI command, or for the LPAR
  ownership check, read it from `ManagedSystem/quick/All` instead of `ManagedSystem/{uuid}`,
  which stalled for a system in `No Connection` (#1501).
- Fleet health, the capacity report and placement, the utilization report, and the
  owning-system search a DLPAR or power operation runs without a managed-system selector take
  their systems from `ManagedSystem/quick/All` instead of the full `ManagedSystem` feed, which
  stalled for minutes while a system was in `No Connection`. The owning-system search names a
  frame that refuses its partitions with its state. Capacity and utilization do not read a
  system that is not operating: capacity leaves it out, and the utilization report lists it
  with every figure unknown and a gap naming its state (#1501).
- `hmc_list_systems`, `hmc_get_system`, `systems list` and `systems show` take their systems from
  `ManagedSystem/quick/All`. They read a system's full entry only while it is operating. A system
  that is not operating, for example in `No Connection`, still appears, with a partial entry: its
  `UUID`, and `SystemName`, `State` and `StateDetail` under `Resource`. The `state` filter matches
  `State` exactly instead of calling the HMC's `State` search. `hmc_inventory` and
  `hmc_plan_lpar` find their systems the same way. `hmc_system_summary`, `hmc_backup_vios` with a
  system UUID, and snapshot capture refuse a system that is not operating at once, naming its
  state, instead of waiting for its entry to time out (#1501).
- A partition or VIOS name lookup without a managed-system selector no longer reports a name as
  not found while a managed system cannot be read. The HMC-wide name search answers "not found"
  for a partition on a system in `No Connection`, so a miss now reads the managed-system summary
  and, when any system is not `operating`, fails with an error naming those systems and asking for
  a system selector. When every system operates, not-found is unchanged. This resolves the 0.1.0
  known issue (#1309).
- `hmc_list_lpars`, `hmc_list_vios` and `hmc_list_lpar_ownership` without a system no longer hang
  while a managed system is in `No Connection`: they take the system list from the managed-system
  summary rather than the full managed-system feed, which times out there. An unreadable system's
  `detailed_state` now comes from that summary (#1309).
- `hmc_list_memory_pools` no longer documents pool row field names that no capture has
  confirmed (#1245).
- Creating an HMC user with `remote_user_id` no longer fails with REST0001 on V10R3: the
  `UserProfile` document now sends `RemoteUserID` where the HMC's schema expects it, before
  `AssociatedTaskRole` (#1414). A modify builds the same document; only the create was run live.
- Creating an HMC user without a description no longer fails with REST0344 on V10R3: the create
  now describes the user as `HMC User`, the HMC's own default (#1414). Modifying a user without a
  description still leaves it unchanged.
- `hmc_read_lpar_refcodes` no longer describes an unknown partition as believed to return an
  empty list: the HMC refuses it with `HSCL8012` and the tool raises, as the live probe showed
  (#1475).

## [0.1.0] - 2026-10-12

First tagged release of `hmcpctl`: a CLI, an MCP server and a Python library for the IBM HMC.
It is scoped to two paths. The first is the Tier A contract a kdive host provider relies on to
adopt an existing partition: power, state readback, console capture and job polling
([kdive Tier A contract](https://github.com/randomparity/hmcpctl/blob/v0.1.0/docs/kdive-tier-a-contract.md)). The second is the Tier B bare-CEC
path, which creates an LPAR, gives it a dedicated PCIe slot, activates it and deletes it
([bare-CEC LPAR recipe](https://github.com/randomparity/hmcpctl/blob/v0.1.0/docs/recipes/bare-cec-lpar.md)). The stable Python API is the
six-name `hmcpctl.api` facade of ADR 0118. Every other module is a pre-release domain API. The
pre-release development history is in the git history, not in this file.

### Verified

These 15 operations passed a live run on HMC V10R3 M1060 managing a POWER9 system, and the
verification ledger reads `current` for each at the release commit:

- `system.get`
- `lpar.get`, `lpar.get_state`, `lpar.summary`
- `lpar.power_on`, `lpar.power_off`
- `lpar.capture_console`, `lpar.list_refcodes`
- `lpar.create`, `lpar.delete`
- `job.get`, `job.wait`
- `pcie.list_dedicated_slots`, `pcie.assign_dedicated_slot`, `pcie.unassign_dedicated_slot`

The claim covers the MCP tools the run called and what it asserted. It does not cover the
`hmcpctl` CLI commands that wrap the same operations, and power-off, for example, was verified
for delayed and immediate shutdown only.
[What 0.1.0 supports](https://github.com/randomparity/hmcpctl/blob/v0.1.0/docs/compatibility.md#what-010-supports) maps each operation to its
tool. `just verification-report` prints every operation's current state, and
[`docs/capabilities/`](https://github.com/randomparity/hmcpctl/blob/v0.1.0/docs/capabilities/README.md) holds the ledger, which is authoritative.

### Known issues

- Most of the catalog is not live-verified at this commit. Of 163 operations, 17 read
  `current`, 90 `stale`, 39 `unevidenced` and 17 `unrecorded`. The two `current` operations
  outside the list above are not claimed. The ledger is authoritative for each operation.
- The logical LPAR workflow tools (`hmc_inventory`, `hmc_plan_lpar`, `hmc_power_lpar`,
  `hmc_inspect_lpar`, `hmc_operation_status`, `hmc_search_tools`, `hmc_invoke_tool`) ship
  without verification. Their workflow is the 0.2.0 target (#1215).
- HMC V11 and POWER11 are not verified for this release. Some SR-IOV inventory reads are
  admitted on captured V11R2 systems (ADR 0183), but no claimed operation was exercised there.
- `hmc_provision_lpar` with a dedicated PCIe slot fails live. The slot is written into the new
  partition's profile, but provisioning powers on the current configuration, so the running
  partition does not own the slot. How provisioning should activate is undecided (#1390).
- Network boot is not supported by the LPAR power-on operation (#868).
- `hmc_read_lpar_refcodes`' description says an unknown partition is believed to return an
  empty list. It raises an error instead, as #893 established.
- A name lookup without a managed-system selector uses the HMC-wide search, which may stall,
  or report a partition as not found, while a managed system is in `No Connection` (#1309).
  Passing the system selector avoids it for reads. `hmc_create_lpar`'s and
  `hmc_provision_lpar`'s duplicate-name check and read-back search HMC-wide regardless. Reads
  of a system in `Recovery` or `No Connection` are otherwise uncaptured (#1290).
- A failed storage-mapping create can leave an unmapped VIOS server adapter that
  `storage list-mappings` does not show. Compare the VIOS's virtual SCSI server adapters with
  its mappings after such a failure (#1250).
- The shared memory pool tools assume `lshwres -r mempool` field names and a remove form that
  no capture has confirmed (#1245).
- Setting `remote_user_id` on an HMC user writes facets that no capture has confirmed, so the
  HMC may refuse the request (#1414).
- Builds report the static version `0.1.0` regardless of commit; git-derived dev versions are
  planned (#1471).

[unreleased]: https://github.com/randomparity/hmcpctl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/randomparity/hmcpctl/releases/tag/v0.1.0
