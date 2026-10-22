# LPAR IBM i and reporting configuration

## Authority and scope

Issue #1763 / C02 / token `q1763-619aec68`: five fields, sixteen source
occurrences; approved exclusions empty. Campaign operator authorized beginning
these children against landed core. #637's closure adds no scope.
[ADR 0255](../../adr/0255-lpar-ibmi-configuration.md) chooses an independent
reporting model extending existing owners without moving responsibility.

## Contract

`LparReportingSettings` has optional strict booleans `os400_restricted_io_mode`,
`electronic_err_reporting`, `redundant_err_path_reporting`; `ipl_source` is
`a|b|c|d`; `work_group_id` is strict integer or literal `none`. Python bool is
not an ID; None omits a field, False/0/`none` are explicit. Unknown keys fail.
Create admits restricted I/O, work group, electronic reporting and redundant
reporting; update admits IPL, restricted I/O, work group and electronic reporting.
An unsupported context fails locally before effects. IPL, restricted I/O and
electronic reporting require OS400; work group and redundant reporting are common
to the existing AIX/Linux and OS400 creation types. No new VIOS lifecycle path.

Sources are `docs/refs/hmc-commands-p10/commands/mksyscfg.md:41`, POWER11
`mksyscfg.md:49`, and both `chsyscfg.md:49`, specifically partition sections.
The redundant-reporting occurrence later in chsyscfg is profile-owned, not this
child's update context. These sections give no numeric ID range, common-field
IBM i restriction, universal release floor or restart rule. Reject malformed
values locally; let HMC reject otherwise unsupported IDs/state/releases, with
honest uncertain outcomes. Configuration readback does not prove guest effect.

## Architecture

Keep `LparSettings` C01 behavior unchanged. Add the reporting model in existing
`documents/lpar.py`; fixed read/write functions in `ssh/lpar.py`; owning read/
configure functions and typed result in existing `operations/lpar/configuration.py`.
Use scoped containment/ownership helpers before update. Read `lpar_env` with
fixed `lssyscfg -r lpar` selection, then applicable update fields. Require one
well-shaped row and the same environment on the field read; invalid/missing/
duplicate/unsupported values refuse a write. Attribute-record validation then
shell quoting protects grammar. Configuration does not activate/apply a profile.
Compare supplied plus omitted baseline update fields after one write; return
verified/mismatch/readback-unavailable/write-outcome-unknown, requested/observed
values and warnings. No retry or rollback after an uncertain effect.

Add `reporting_settings` to existing LparCreation and create presenters. Validate
model/context/type and refuse reporting+explicit-keylock before resolving or
creating anything. This preserves existing C01-only keylock behavior. Nonempty
reporting selects mksyscfg, merging its field pairs with C01 and existing resource,
ID/max-slot values. Keep assignment prevalidation, ownership stamp and optional
apply unchanged. Read supplied reporting fields after creation through a separate
workflow step; mismatch/unavailable readback fails the workflow while retaining
the created partition. No post-create creation-only write. Existing settings step
runs and new reporting step must stop later assignments on failure.

Present `configure-reporting` and `get-reporting-settings` CLI commands and
corresponding MCP tools through existing configuration modules. Create exposes
four creation options; update exposes four update options. No raw XML/command
input, new dependency or stable-facade export. Generate tool docs and maturity
projection, update capability bindings and direct structural inventories.

## Failure model

- Actors/deployments: CLI operators and authenticated MCP users on HMCs covered
  by the POWER10/POWER11 command contracts; local tests simulate boundaries.
- Assets/invariants: exact target containment/ownership, C01/resource/options
  preservation, omission semantics, context/type admission, truthful completion.
- Accepted classes: firmware/state rejection and missing readback are visible;
  concurrent external writers can cause mismatch (CLI has no ETag); no live
  execution leaves maturity unevidenced. Trusted administered HMC output has
  existing transport timeout but no byte cap; shape checks occur after collection.
- Covered elsewhere: profile CRUD/activation and residual profile source work;
  transport credentials/timeouts; existing assignment/resource ownership checks.

## Threat model

Typed callers may submit bad values, inapplicable types or foreign targets;
HMC may return malformed/duplicate rows or lose a connection after applying.
Strict models/context admission, owning authorization, fixed field lists,
record+shell encoding and explicit outcome statuses cover these boundaries.
No new auth policy, arbitrary command path or automatic recovery is introduced.

## Verification and live procedure

Focused tests cover sixteen bindings, strict grammar and sentinel omission,
context/type rejection before effects, encoded records, bad snapshots, ownership,
baseline failure, supplied/omitted postconditions and every uncertain outcome.
Creation tests preserve C01/resource/id/max-slot/stamp/apply, reject keylock
conflicts before effects and stop assignments on reporting failure. App tests
cover CLI/MCP forwarding and strict schema. Introduce controlled faults and
observe red before restoring; run final just verify plus pinned prek all-files.

Provide an opt-in named live arm through the existing dispatcher, reserving48
only after inspecting dispatcher admission. Reuse lpar-config resource baseline,
caller-token ownership, inactive-only teardown and recovery witness. Create one
small OS400 disposable partition with explicit False reporting, work group none,
required stamp and no apply. Root first establishes IBM i eligibility; an absent
capability/refusal is a gap, never proof or reason to touch another partition.
Run one finite arm: creation readback, four update fields with IPL a/b/a and
boolean false/true/false round trips, work group none (no fabricated EWLM ID),
then delete inactive owned fixture. No activation, profile mutation, time_ref=True
or power change. Record at most eleven updates, one create, one delete, existing
finite job/SSH timeouts, and before/after names/free-resource comparison.
Candidate exact deployment/import proof precedes preflight; recovery runs on
success and failure; generate redacted commit-bound evidence. Root exclusively
authorizes the hardware window. POWER9 execution is never POWER10/11 hardware
proof; skipped/failed fields stay unevidenced.
