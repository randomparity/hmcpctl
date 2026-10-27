# Partition GZIP accelerator QoS design

Preparation candidate for #1781 / D08, branch `feat/lpar-accelerator-qos-1781`.
Base: `1aad137647baa771497870e01fbc875149666140`; default branch: `main`.
Lane: full-spec; frozen complexity M, denominator 250. The source of authority is
[the approved scope](https://github.com/randomparity/hmcpctl/issues/1781#issuecomment-6423532168),
including both 2026-10-26 HMC-final validation decisions and empty exclusions.
[ADR 0305](../../adr/0305-lpar-accelerator-qos.md) proposes the ownership decision.
This candidate does not authorize implementation, live execution or maturity promotion.

## Scope and sources

Provide the typed partition GZIP set operation, CLI and MCP presentations, source
contracts, negative/postcondition tests, generated capability/tool documentation,
and a named opt-in live procedure with original-baseline and recovery checks.
System-wide writes, arbitrary resource forwarding, stable-facade exports and new
dependencies are outside the existing D08 allocation, not new exclusions.

| Evidence | Structural locator | What it establishes |
| --- | --- | --- |
| P10 exact allocation | `commands-p10:commands/chhwres:L00197:command-synopsis` | Partition GZIP set selector |
| P11 exact allocation | `commands-p11:commands/chhwres:L00199:command-synopsis` | Same selector in the other corpus |
| P10 optional arguments | `docs/refs/hmc-commands-p10/commands/chhwres.md:198`, `:234`, `:235`, `:236` | Wait/detail/force contexts and restart qualification |
| P11 optional arguments | `docs/refs/hmc-commands-p11/commands/chhwres.md:200`, `:237`, `:238`, `:239` | Corresponding P11 contract |
| Native inventory | `docs/refs/hmc-commands-p10/commands/lshwres.md:81`; P11 same section | System/partition accelerator read, GZIP subtype and partition filter |
| Retained source capture, 2026-10-26 | Native `type,max_qos,curr_avail_qos,pend_avail_qos`; partition `lpar_id,type,curr_qos,pend_qos` | Field names and one observation, not universal limits |

The two allocated selector hashes are
`1569925f-b684fa57-af026870-da88d243-275cd489-8eec6d12-aaf9d5aa-d54617c0`.
Reference-contract fixtures retain structural tokens, locators and hashes only.
No reference prose or raw hardware inventory enters tracked artifacts.
The capture observed general accelerator capability and supported `gzip`, with
IBM i accelerator capability false. Native current/pending were both zero; XML
`HardwareAcceleratorQoS` contained metadata only. Absence remains absence.
No POWER-generation or HMC-release floor, 100/128 ceiling, or runtime mapping is inferred.

## Interface and owner

The proposed `LparAcceleratorQos` request has required strict integer `qos >= 0`,
optional strict integer `wait_minutes >= 0`, optional strict integer
`detail_level` in 1..4, and strict boolean `force=False`. Unknown fields refuse.
Zero wait is the native no-OS-command-timeout setting, not an unbounded local SSH
wait; the existing SSH deadline still applies and can produce uncertain effects.
Omitted wait/detail are omitted from the command. A CLI parser may convert integer
argument text; the typed library/MCP model does not coerce strings, floats or booleans.

`set_lpar_accelerator_qos(hmc, lpar_name_or_uuid, change, *,
system_name_or_uuid, ownership_override=False) -> dict[str, Any]` owns admission,
the single submission and readback. The new owner is
`src/hmcpctl/operations/lpar/accelerator.py`; no old accelerator path is removed.
The request resides in `src/hmcpctl/documents/lpar_accelerator.py` and fixed native
commands in `src/hmcpctl/ssh/accelerator.py`.

CLI: `hmcpctl lpars accelerator-qos PARTITION --system SYSTEM --qos N`, with
`--wait-minutes`, `--detail-level`, `--force`, `--yes`, `--ownership-override`.
MCP: `hmc_set_lpar_accelerator_qos(lpar_name_or_uuid, system_name_or_uuid,
change, profile=None, ownership_override=False)`, operation
`lpar.set_accelerator_qos`, mutating effect and `lpar` target metadata.
Both call the same owner; connection and target authorization run before effects.
No separate inventory tool or general accelerator-management API is added.

## Admission and one attempt

1. Validate the typed request and nonblank selectors. Resolve the managed-system
   UUID and exactly one partition across its LPAR and VIOS feeds. Require a valid
   UUID, positive numeric partition ID and stable name/type association.
2. Fetch the selected operating system with matching UUID/name. Require reported
   `AssociatedSystemCapabilities.HardwareAcceleratorCapable=true` and exactly
   one `SupportedHardwareAcceleratorTypes.HardwareAcceleratorType.Type=gzip`.
   An OS400 target additionally requires `IBMiHardwareAcceleratorCapable=true`.
   Admit only reported AIX/Linux, Virtual IO Server and OS400 partition types.
3. Require a concrete state from the shared `PARTITION_STATES`, excluding literal
   `Unknown`, and no positively reported operation in progress. Missing/malformed
   state/operation evidence refuses. Do not translate the memory owner's
   running/inactive eligibility list into an accelerator restriction. HMC decides
   undocumented eligibility among concrete states under the approved exception.
4. For AIX/Linux/VIOS without reported active RMC require explicit force. Force
   means the caller acknowledges intentional network isolation and later OS
   restart; no separate network-topology probe can prove that intent. Reject
   force, wait and detail options for OS400 because the published contexts do not
   establish those accelerator flags there. Unknown RMC refuses rather than
   becoming a false no-RMC observation. Known RMC values are `active`, `inactive`, `none`; captured
   `RMCState.Enum` also has `unknown`, which refuses here.
5. Read system and selected-partition native GZIP inventory. Require exactly one
   row of each expected identity/type, and nonnegative integral captured numeric
   fields. Available quantities and maximum are observations, not invented
   per-partition allocation bounds. Do not compare requested QoS to sampled 128.
6. Authorize ownership using `authorize_lpar_mutation`; preserve its override and
   audit behavior. Recheck containment, partition identity/state/RMC, system
   capability/type/name and the native baseline before submission; repeat ownership
   authorization last. Drift refuses; these checks are not a lock or CAS.
7. Send one fixed command with canonical numeric partition ID, shell-quoted system
   name and typed numeric options. Explicit zero is sent, including when native
   baseline is zero. Never infer a no-op, reset or clear operation from zero.

## Observation and result

After normal submission completion or a handled HMCError, attempt an independent
partition read and classify command and observation separately. Cancellation and
unexpected exceptions propagate without a readback or result guarantee; callers
must inspect the target independently before acting. Reconfirm selected-system
containment, UUID/type/name, numeric ID and system identity before trusting native
readback. Preserve successful
current and pending fields even when the other observation is unavailable. Native
`curr_qos` is called current; `pend_qos` is called pending; neither is called runtime.
XML collection state is reported separately without synthesizing QoS from emptiness.

Result fields: `UUID`, `requested_qos`, `submission` (`completed`, `rejected`,
`unknown`), `submission_error`, `before`, `after`, `observation_error`, `outcome`,
`workflow_completed`, `completion_scope="native-current-and-pending"`,
`effective_runtime="unavailable"`, `profile_persistence="unverified"`,
`restart_required`, and `warnings`. Before/after contain identity, native current,
native pending and independently labelled XML collection state. Missing fields
are null with an error, never zero. System baseline capacity remains separately labelled.

`observed` requires both current and pending equal requested; it reports observation
without claiming the command caused it. `workflow_completed` additionally requires
submission `completed`. A native positive exit without exit signal is `rejected`;
lost transport, signal or absent exit status is `unknown`. A failed command with
matching readback remains incomplete. Nonmatching changed values are `partial`;
unchanged values plus a normal native refusal are `rejected`; other nonmatching,
changed identity or unavailable reads are `unverified`. Available evidence is kept.
No automatic retry, rollback, profile write, activation or restart is performed.

`restart_required` is true after a successful forced no-RMC command and null
otherwise; null does not mean no restart is needed. Other states receive
an explicit unknown application-timing warning. Even ordinary active-RMC success
cannot establish effective guest runtime with the available observations.

## Success and validation

- Both allocated corpus selectors and their optional argument context are bound to
  the typed request, finite serializer and named operation without claiming whole-row coverage.
- Negative model/capability/type/state/RMC/ownership/containment tests observe zero
  setter calls; permitted known states reach HMC validation without an invented whitelist.
- Zero, large nonnegative quantities, native refusal, transport uncertainty,
  changed/missing current or pending, XML absence and identity drift have distinct tests.
- CLI cancellation prevents connection; CLI incomplete results exit nonzero; MCP
  requires the selected connection/system/partition grant and exposes typed schema.
- A separate live oracle checks the named current/pending postcondition. A finite
  successful run alone can supply evidence for the actually exercised case.
  Existing source reads, mock tests and an unexecuted procedure supply no live proof.

## Failure model

1. **Actors and deployments**
   - Local CLI operators, authorized MCP clients and direct Python callers against
     supported HMC endpoints; offline tests and separately authorized live runs.
2. **Invariants and assets at stake**
   - The selected owned partition's accelerator allocation; no foreign-system write.
   - Accurate uncertainty, native current/pending separation, no unsupported runtime claim.
3. **Accepted failure classes**
   - Undocumented allocation/state eligibility: HMC final validation explicitly approved.
   - Concurrent native changes between reads: native set has no CAS; rechecks detect
     observable drift, and results preserve uncertainty without replay.
   - Effective-runtime/profile evidence unavailable: explicitly unavailable, never success there.
4. **Covered elsewhere**
   - Credentials/SSH host verification/deadlines: existing transport/configuration owners.
   - MCP connection/target authorization and ownership audit: existing registered guards.

## Named live procedure and adoption holds

Root assigned **ST64** on 2026-10-26 after closing the visible reservation inventory.
Group: `lpar-accelerator-qos`; scenario: `st64-lpar-accelerator-qos`; wrapper:
`scripts/live_lpar_accelerator_qos.py`. This mechanical assignment binds the plan
only; design review, technical adoption and implementation remain pending.
Read-only preflight, original baseline and recovery remain mandatory. No automatic
compensating write follows a failed or uncertain set. Live use needs its own root
window and candidate identity; it is not a merge gate. Ordinary code-body plan size
is measured against M250; a format exception is not inherited from sibling issues.
