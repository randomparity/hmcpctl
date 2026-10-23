# Typed partition vTPM configuration — #1764

## Problem and authority

C03 in #638 owns `vtpm_enabled`, `vtpm_version` and `vtpm_encryption` in
partition creation and update: three fields, twelve field/context/corpus bindings.
The frozen charter is #1764, token `q1764-e389b305`; exclusions are empty.
The operator additionally approved explicit data-loss acknowledgement, inactive-only
updates, refusal of replacement while enabled, and minimal existing-owner VIOS
presentations. [ADR 0276](../../adr/0276-lpar-virtual-tpm-configuration.md) records
these choices. Profile settings remain owned by #1724.

## Sources and applicability

- Local `docs/refs/hmc-commands-{p10,p11}/commands/mksyscfg.md` and `chsyscfg.md`:
  their partition sections name the three fields for AIX/Linux and VIOS, strict
  0/1 enablement, and system-advertised versions/encryptions. No corpus label
  establishes hardware support. Tests extract these twelve bindings by section.
- [IBM partition properties](https://www.ibm.com/docs/en/power10/9105-41B?topic=partitions-changing-partition-properties-capabilities):
  version 1.2 has no encryption selection; 2.0 uses advertised levels. Its published
  release prerequisites explain why generation alone cannot admit a request.
- [IBM PowerVM encryption-level explanation](https://community.ibm.com/community/user/blogs/joel-wolfrath/2024/02/05/understanding-vtpm-20-encryption-levels):
  replacing an encryption level requires a powered-off partition and removal of
  the existing instance; keys and stored data can be lost. This operation does
  not perform that sequence.
- Retained HMC 10.3.1060 build 2408210051 schemas establish BasePartition fields,
  capability booleans and VIOS inheritance. The bounded read-only source capture
  at `3ad18376` observed version list `1.2`, encryption sentinel `null`, and
  disabled AIX/Linux and VIOS rows with null version/encryption. It is source
  evidence, not feature mutation proof. Private captures are not repository files.

## Architecture and interfaces

Extend the current owners; retain no parallel command proxy or new lifecycle.
`documents/lpar_vtpm.py` owns immutable strict `LparVtpmSettings`, with three
optional fields; None omits, False is explicit, and supplied strings must survive
both typed validation and the existing HMC attribute-record grammar.
`ssh/lpar_vtpm.py` owns fixed system advertisement and partition read/write
commands. Parse exactly one CSV row of the expected width, matching target name
and environment. Normalize the observed `null` sentinel to None only in responses;
never accept it as a user version/encryption. Reject malformed/duplicate list
members, extra rows/columns and unknown booleans instead of guessing.
`operations/lpar/vtpm.py` owns capability/type/state admission, target-specific
containment/ownership and one-write verification. CLI/MCP bodies only adapt inputs
and display typed results. Separate VIOS presenters keep `vios` target grants
separate from `lpar` grants, even though the implementation is shared.

## Admission and effects

Basic typed/record validation precedes transport. Resolve the selected system and
its contained LogicalPartition or VirtualIOServer, and cross the existing
ownership guard before an update. The selected system must advertise vTPM
capability through `ManagedSystem.Resource.AssociatedSystemCapabilities`, the
association to the schema's `SystemCapabilities.Type`. Supplied versions/encryptions
must belong to its fixed-command lists;
2.0 also requires its specific capability. Known compatibility admits encryption
only with effective version 2.0, rejects it for 1.2, and refuses unrecognized
version/encryption compatibility rather than extrapolating a new TPM protocol.
When encryption is supplied during creation, require an explicit compatible
version. Other omitted creation fields retain HMC defaults.

Updates require the observed CLI state `Not Activated`, rechecked by the strict
pre-write partition row. Never power off, activate, apply/synchronize a profile,
manage a trusted-system key, or disable/re-enable implicitly. If vTPM is enabled,
a changed version/encryption is rejected even when disable is included in the
same request; callers explicitly disable, inspect, then configure and enable.
Disabling an enabled instance additionally requires strict
`acknowledge_data_loss=True`, independently of ownership override. Explain key/NV
data loss before effects. An explicit disable of an already disabled instance
needs no data-loss acknowledgement. Refuse empty updates.

Creation carries the settings in the existing single native `mksyscfg` record;
there is no post-create repair write. LPAR creation retains its resource, duplicate,
I/O and ownership-stamp guards and adds a `vtpm` workflow step. A failed/unavailable
step prevents later assignment steps. Explicit caller-requested apply behavior
stays with its existing owner. The existing VIOS create owner uses the same native
writer only when vTPM is requested, with bounded resource/duplicate admission and
scoped VIOS readback; its unchanged no-vTPM path remains REST. A VIOS creation
verification error says that the partition exists/may exist and must be inspected
before retrying; it never creates again or rolls back automatically.

## Outcomes and preservation

Update results report `verified`, `mismatch`, `readback-unavailable`, or
`write-outcome-unknown`, plus requested/observed settings and warnings. A confirmed
write is followed by one strict read. `verified` requires supplied values and
omitted before-values to match; any default initialization or preservation drift
is reported as mismatch with its changed fields, not silently accepted. A transport
exception during submission never causes replay. Creation verifies supplied values
and reports full observed defaults; it has no previous configuration to preserve.
Unknown creation identity/readback is an incomplete workflow, not successful proof.

## Failure model

- Named deployments: AIX/Linux and VIOS on systems whose runtime advertisements
  and supported compatibility rules admit the request; IBM i is refused.
- Must prevent: malformed or unsupported values, foreign/wrong-kind targets,
  missing/ambiguous advertisement, active updates, unacknowledged enabled disable,
  implicit replacement, false verified outcomes and follow-on work after failure.
- Accepted: an external writer can race a preflight or change state after final
  readback; HMC admission remains final. No atomic compare-and-swap is promised.
  Guest Trusted Boot, persisted TPM objects and migration readiness are not proved
  by configuration readback. No model or release receives automatic maturity credit.
- Mitigation/evidence: strict fixed records, existing authorization, one submission,
  explicit uncertain outcomes, no automatic recovery writes, meaningful fault tests,
  and a separate candidate-bound live window with guarded fixture cleanup.

## Validation and shipping

Focused tests cover the twelve reference bindings, strict inputs and omission,
CSV/null/list shapes, compatibility/capability absence, both target guards,
state/data-loss gates, one-write uncertainty, mismatch/preservation, creation
composition and typed presentation/security metadata. Remove a gate deliberately
and observe its negative test fail before restoring it. Do not snapshot prose.
The plan maps these contracts to tests and exact commands. Register arm 51,
`lpar-vtpm`, with preflight and recovery. It creates one inactive, run-owned
AIX/Linux partition, uses only advertised settings, verifies enablement and
explicit disable acknowledgement, then deletes only that fixture and confirms the
independent baseline. No guest boot, VIOS creation, 2.0 or encryption claim follows
when those arms have not run. `just verify`, all pinned hooks and CI precede the
worker's author handoff; root alone merges.
