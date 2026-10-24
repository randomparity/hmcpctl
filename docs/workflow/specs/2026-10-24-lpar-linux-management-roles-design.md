# Linux partition management roles and KVM eligibility

## Problem and authority

Issue [#1766](https://github.com/randomparity/hmcpctl/issues/1766) adds two
partition flags in eight P10/P11 creation/update bindings. The frozen
[scope](https://github.com/randomparity/hmcpctl/issues/1766#issuecomment-6378396996)
provides C1–C7; [ADR 0278](../../adr/0278-lpar-linux-management-roles.md) records
transport and intent decisions. Exclusions are empty. This is full-spec M250.
Implementation was released after #1765 landed. Its existing security-policy
parser supplies keystore admission evidence; no duplicate policy owner is added.

## Global Constraints

Python >=3.11; local Python 3.11; CI amd64 Python 3.11, 3.12, 3.13 and 3.14.
No new dependencies. Preserve the six-name stable `hmcpctl.api` facade.
Use existing scoped authorization, fixed command encoders and audit wrappers.
Bootstrap with `just setup`; Python commands use `uv run --no-sync`.
Focused pytest uses `--no-cov`; final `just verify` keeps exact coverage.
Before push run `just verify` and `uv run --no-sync prek run --all-files`.
No hardware execution, deployment, controller takeover or automatic activation.

## Source contract

- S1: `docs/refs/hmc-commands-p10/commands/mksyscfg.md:41`, P10
  `chsyscfg.md:49`, P11 `mksyscfg.md:49` and `chsyscfg.md:49`: each names
  `powervm_mgmt_capable` and `kvm_capable`, Linux only, native values 0/1.
  These are eight field/context bindings, not eight new ledger source units.
- S2: [IBM KVM requirements](https://www.ibm.com/docs/en/linux-on-power-systems?topic=servers-kvm-in-powervm-lpar)
  bind KVM to POWER10 compatibility, dedicated CPUs with sharing disabled,
  supported system/firmware/HMC, no platform keystore or vPMEM, and an inactive
  partition for configuration. Kernel, QEMU, libvirt and Radix requirements
  concern guest deployment; setting a capability flag does not prove them.
  IBM HMC enhancement notice (author-identifying URL withheld from publication)
  specifically requires HMC 10.3.1060.1 for create/enable capability.
- S3: P11 `mksyscfg.md:39` and `chsyscfg.md:33–39` require HMC controller
  authority in controller mode; native creation there prohibits `profile_name`
  and `sync_curr_profile` and establishes current configuration immediately.
- S4: `tests/fixtures/live/rest-ms-entry-mtms-pair.json` establishes exact paths
  `AssociatedSystemCapabilities/KvmOnPowerVMCapable`, `IsClassicHMCManagement`,
  `IsHMCPowerVMManagementController`, system firmware and supported modes.
  Positive KVM advertisements occur in the committed P10/P11 vocabularies.
- S5: `tests/fixtures/live/rest-lpar-entry.json` and `rest-lpar-create-ok.json`
  establish `PowerVMManagementCapable`, `KvmCapable`, `KeyStoreSize`, pending
  compatibility, processor configuration and `AssociatedPersistentMemoryConfiguration`
  volume counts. Do not substitute invented aliases or interpret absent as false.
- S6: [NovaLink requirements](https://www.ibm.com/docs/en/power11/9824-22A?topic=novalink-powervm-system-requirements)
  describe deployment and creation with the management flag. They do not prove
  that enabling the flag requires already-installed NovaLink, nor establish a
  mutual exclusion between the two flags. No such restriction is introduced.

## Ownership and interfaces

Extend `LparSettings` with two optional strict booleans and reuse
`configure_lpar_settings`, `LparSettingsResult`, the creation settings step and
existing CLI/MCP create/configure surfaces. A focused private
`operations/lpar/linux_roles.py` owns prerequisite evidence and admission;
`ssh/lpar.py` retains fixed record encoding. No new public model, operation,
command or MCP tool is needed. Existing ownership/audit wrappers remain owners.

`settings.powervm_mgmt_capable` and `settings.kvm_capable` preserve false as
zero and omit None. Existing strict Python/Pydantic validation applies. CLI
create/configure adds paired enable/disable options; MCP receives these through
its existing `settings` schema. Keep the five-field C01 SSH getter query explicit:
role selection alone adds REST role admission/readback to the existing operation.
No-role requests retain their existing query and authorization behavior. Merge
role evidence into the existing settings result, comparing requested roles and
known preserved roles; a missing requested role is unavailable verification.
Selection of either field, including false, declares Linux intent on AIX/Linux
(C6); OS400/VIOS are refused. No installed-OS classifier or intent parameter.

## Admission and effects

1. Validate typed values, nonempty update, environment and encodable record.
   Resolve selected-system containment and ownership before update reads/writes.
   Creation retains duplicate-name, memory, I/O and ownership-stamping behavior.
2. Read selected-system management authority. Classic mode is admitted only
   from explicit true; otherwise require explicit current-HMC controller true.
   Unknown/contradictory evidence fails before effects. Never acquire authority.
3. A supplied KVM field requires positive selected-system KVM advertisement
   and usable console release >=10.3.1060.1 (retain the fix level from
   `VersionInfo/Minor`, not `Maintenance`). Positive enablement additionally
   requires the documented firmware floor: FW1060.10 on Power10 or FW1110 on
   Power11, with processor generation determined by advertised modes, not host.
4. For update, KVM field selection requires inactive state and no reported
   operation in progress. Positive KVM requires configured dedicated CPUs,
   `keep idle procs`, pending POWER10 compatibility, zero PKS and zero current
   persistent-memory volumes. Missing or malformed required evidence rejects.
   Read PKS through the landed #1765 policy owner; do not duplicate its policy.
5. For create, positive KVM requires explicit dedicated CPU intent and effective
   never-share resources through the existing resource normalizer. It rejects
   shared/unknown resources, incompatible explicit sharing, nonzero requested
   PKS and unrepresented prerequisite combinations. It adds only fixed
   `lpar_proc_compat_mode=POWER10` beside `kvm_capable=1` (C7); no CPU rewrite.
   False/omitted KVM adds no compatibility field or follow-up mutation.
6. Role-bearing creation uses one native record. Explicit keylock, which this
   route cannot encode, is rejected before creation. Classic mode retains
   current profile/apply behavior. Controller mode omits implicit profile name;
   explicit `apply_profile=True` is rejected with guidance to select the
   existing false option. False reports skipped apply and that native creation
   already configured the partition; it must not claim a named apply occurred.
   The same controller admission rejects `LparPhysicalIO` (whose public contract
   is initial-profile I/O), creation SR-IOV and explicit dedicated/SR-IOV
   assignments (whose inputs require `profile_name`) before creation/assignment.
   Guidance names existing apply=false inputs or classic management mode. Keep
   classic mode with role flags and physical I/O supported; do not blanket-reject
   vNIC inputs, which carry no profile name, or invent current-I/O support.
7. Update writes only requested role fields in one `chsyscfg -r lpar` record.
   No activation, reboot, software deployment or controller transfer follows.
   The management flag has no separately documented inactive-state or numeric
   release floor; do not manufacture one from NovaLink deployment instructions.

HMC console closure remains an operator prerequisite in the documented KVM
procedure. No source-backed non-mutating console-occupancy query is established;
the client must neither open a console to test occupancy nor close another
session. Remote rejection or a concurrent state change is reported conservatively.

## Results and recovery

Preflight denial raises an actionable error before submission. One successful
submission plus matching requested/known-preserved role fields is `verified`; a
readable disagreement is `mismatch`, with observed settings. Failed/malformed readback is `readback-unavailable`. A transport/native
write error is `write-outcome-unknown`, not proof of rollback or rejection.
No implicit replay or rollback occurs. No role result warning includes raw remote command output; retain the existing
typed requested/observed settings contract. Creation retains its existing created flag,
stamp and warnings and reuses its settings workflow step; unavailable identity, mismatch
or unverified roles stops subsequent assignments. Verify the fixed compatibility
companion on positive KVM creation, not just the role flag. Runtime KVM/NovaLink
readiness is never inferred from these configuration postconditions (C3/C4).

## Failure model

- Actors and deployments: authorized Python/CLI/MCP callers configuring an
  HMC-managed PowerVM partition; concurrent HMC administrators; P10/P11 command
  contracts, with selected-system evidence governing actual admission.
- Invariants/assets: scoped target and ownership, existing resource allocation,
  controller authority, explicit false/omission, known versus uncertain effects,
  unchanged observed unsupplied flags, and availability of another actor's console.
- Accepted failure classes: post-admission administrator races and transport
  uncertainty cannot be made atomic across REST/SSH; report uncertainty without
  replay. Unrun hardware remains unevidenced. Guest software/runtime readiness
  is not the requested configuration operation and is not claimed.
- Covered elsewhere: existing ownership and connection authorization, SSH
  bounds/encoding and audit; #1765 owns PKS policy; #1724 owns profile behavior;
  existing resource operations own subsequent DLPAR/LPM/DPO actions.

## Threat model

- Added boundaries: two-field settings input and fixed role mutation/readback.
  Widened boundaries: create arguments and operation registry metadata.
- Actors: local callers and authorized MCP users can submit malformed settings
  or foreign selectors; remote HMC data can be malformed/stale. Guest identity
  is not trusted as evidence merely because PartitionType is AIX/Linux.
- Controls: strict booleans/no extra keys, fixed role-admission errors,
  required system scope, existing ownership authorization, fixed encoded keys,
  positive prerequisite evidence, one submission, redacted result/error handling.
- Covered elsewhere: credential/session transport and adversarial administrators
  changing state after admission. No new cross-system mutation or takeover right.

## Validation and live procedure

Behavioral tests cover both booleans/omission, strict invalid types/extra keys,
all eight bindings, classic/controller creation, no-create/no-assignment profile denials, positive
classic role-plus-I/O preservation, authorized/foreign selectors,
capability/release/state/CPU/compatibility/PKS/vPMEM negatives, source-shape
parsing, requested/preserved readback, fixed companion, and every uncertain
outcome. Test a bounded malicious remote-error marker to prove result warnings omit it.
New/high-risk tests must fail under controlled faults and pass after restoration.
CLI/MCP forwarding/schema/authorization tests and regenerated metadata bind the
same typed operation; no new public-facade exports. Relevant static gates run
for design; complete shipping gates run after implementation is released.
A reproducible manual procedure will use the documented preflight, the assigned arm 53 / `lpar-linux-roles`, and mandatory recovery with independent before/after
inventory. Use only owned inactive disposable partitions, check admission
prerequisites, exercise explicit true/false/omission and inspect readback; never
claim deployment success. No live execution or maturity promotion is authorized.

Arm 53 is reserved for this design/mocked proof only. Its eventual native packet
must bind exact candidate/resources/deadlines/controller facts; no reservation
confers execution authority. Guest/runtime/takeover/key effects remain outside it.
