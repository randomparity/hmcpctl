# ADR 0278: Linux partition management roles and KVM eligibility

## Status

Accepted — issue #1766; implementation released after #1765 landed. Hardware
execution and guest deployment remain unevidenced.

## Context

Two Linux-only native flags are documented for partition creation and update.
The current AIX/Linux partition type cannot prove an installed Linux guest.
KVM also needs POWER10 compatibility, which the current create interface cannot
select. The operator approved field selection as Linux intent and a fixed
same-record POWER10 companion when creating with `kvm_capable=True`.
The accepted [scope](https://github.com/randomparity/hmcpctl/issues/1766#issuecomment-6378396996)
retains two fields/eight bindings and empty exclusions.

## Decision

Extend `LparSettings` and its existing create/configure presentations and result.
Use fixed native records and conditional REST role evidence, preserving the
existing C01-only SSH query on no-role paths. Reuse creation, authorization,
ownership, audit and readback owners; add no public model or tool.
Selecting either field, including false, declares Linux intent; it is not
installed-OS evidence. Admit only source-backed selected-system capability,
management authority and effective configuration. Do not infer mutual exclusion
between flags or require already-installed NovaLink/KVM software.

Positive KVM creation adds fixed `lpar_proc_compat_mode=POWER10` to that same
record. False/omitted KVM adds no compatibility effect. Preserve caller CPU
intent; reject incompatible resources instead of silently changing them.
Controller-mode create omits the prohibited implicit profile name and rejects
explicit apply and existing profile-dependent I/O requests before effects, with
existing apply=false guidance. `LparPhysicalIO` promises initial-profile
verification; dedicated/SR-IOV assignment inputs require a profile name. Preserve
KVM-only classic-mode I/O behavior and do not invent a current-I/O contract.
Management-field selection, including false, requires exact native
`lscomgmt -m <selected system> -F is_controller` value `1`. Native null plus the
observed classic tuple classifies legacy profile grammar only; it does not
authorize the management field. P10/P11 installios and lpar-netboot document
value 1 as co-management/profileless; the native false-field creation refusal disproved
the earlier classic-management admission assumption. Unknown evidence refuses.
Do not transfer controller authority or activate/restart the partition.

Verify requested and known-preserved role values and the creation companion. Represent
mismatch, unavailable verification and uncertain write outcome explicitly;
never automatically replay or claim guest functionality. The
[specification](../workflow/specs/2026-10-24-lpar-linux-management-roles-design.md)
contains the source bindings, failure model and validation contracts.

## Consequences

Operators must supply compatible resources and separately prepare Linux software.
HMC/transport races remain possible and uncertain results require inspection.
Creation may leave an owned partition whose role verification failed; later
assignment steps stop. #1765's landed security-policy parser supplies PKS
evidence through its existing typed state; no second policy parser is introduced.

## Considered & rejected

- **No change.** verified: #1766 requires the eight listed flag/context bindings;
  source at `784a8a1e` exposes neither role through creation/configuration APIs.
- **Infer installed Linux or add an intent parameter.** verified: the operator
  explicitly selected field-declared Linux intent for this issue; the existing
  `PartitionType` combines AIX/Linux.
- **Post-create compatibility mutation.** verified: the operator authorized only
  a fixed companion in the creation record; a second mutation adds partial effects.
- **Generic attribute proxy or new compatibility input.** judgment: expands
  surface beyond two flags and weakens typed admission without serving C1–C7.
- **Silently change CPUs, apply profiles or take controller authority.** verified:
  C7 prohibits resource rewrites/takeover; documented controller mode prohibits
  the named-profile request, which must be rejected rather than reported applied.
- **Separate role model and MCP tool.** verified: at `784a8a1e`,
  `LparSettings`, `configure_lpar_settings` and `LparSettingsResult` already
  express typed optional fields and the four required outcome states. Conditional
  REST evidence avoids widening the fixed C01 SSH query on no-role requests.
