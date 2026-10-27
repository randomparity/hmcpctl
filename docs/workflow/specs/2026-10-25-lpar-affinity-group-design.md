# Conventional affinity group at partition creation

Private proposed saved-configuration amendment; implementation/review release pending.
[Scope](https://github.com/randomparity/hmcpctl/issues/1769#issuecomment-6399637239),
q1769-1c63e8d1; full-spec, S100, one PR, EMPTY; C1–C5 and two occurrences retained.
Approval call_6f289dcf9ef4484b99a26406c941d29c/0 changes the success meaning only.
Decision: [ADR0302](../../adr/0302-lpar-creation-affinity-group.md).

## Problem and success

C1–C5 retain typed creation and CLI/MCP coverage, source-backed semantics, containment,
authorization/audit, tests/docs/maturity/procedure and shipping guardrails. Replace
“effective assignment” with independently verified saved affinity configuration;
retain separate native observation, never a runtime or placement claim.
Classic succeeds only with fresh contained UUID/name/positive numeric PartitionID/
environment, default-profile identity and requested saved value. Controller remains
admitted profileless but incomplete until a source-backed saved proof is available.

## Sources and scope

P10 docs/refs/hmc-commands-p10/commands/mksyscfg.md:41 and P11 equivalent:49
admit exact none or1–255 for processor/memory grouping; neither adds field-specific
Linux/KVM, CPU, release or hardware floors. P10/P11 lssyscfg:34–35 supply separate
partition/profile filter/projection grammar, not affinity current/pending semantics.
tests/fixtures/live/cli-prof-lp3.json:2–6 proves profile none; vocabulary fixtures
v10r3-p10-9080-hex.json:2969,3097 and v11r2-p11-9242-21b.json:4257,4393 prove
field availability only. The retained433c investigation proves a product comparison
failure with profile1/partition-none, not its provider cause or effective placement.
AIX/Linux, IBM i and VIOS, classic/controller and P10/P11 bindings remain in scope.
ResourceGroup #639, profile lifecycle #1724 and minimum-affinity owners stay separate.

## Inputs, owners and creation

Direct/MCP accept strict integer1–255, exact lowercase none or omission. CLI parses
ASCII decimal text/none before acquiring a client. Reject bool, floats, numeric
strings at typed boundaries, zero,256, negatives, whitespace/case variants and records.
Keep LparCreation/create_vios/native optional fields, stable facade six names, separate
LPAR/VIOS presentations, authorization wrappers, audit and existing resource intent.
Selection uses one native record; omission preserves routing and adds no affinity read.
Reject native keylock incompatibility before I/O. No REST-first attempt for selection.
Core owns one native authority observation; selected roles retain C05 admission.
Role/layout/affinity plus dedicated/SR-IOV assignments pass the complete collection
as preflight_assignments before physical-I/O removes later SR-IOV follow-ups.
Reject wrong type, empty/vNIC-only or unselected use before I/O; None retains defaults.
Controller refuses profile assignments before foreign inventory/create effects;
classic prevalidates the whole collection. No caller supplies authority evidence.
Classic uses default_profile; controller omits profile_name and refuses explicit
apply or initial-profile I/O before submission. Preserve all_resources where each
selected family permits it, C06 ratios, C07 update-weight refusal, C10 capability
and C11 serial admission/identity/readback. No inferred controller persistence.

## Independent saved proof and native observations

After create and the existing ownership path, resolve containment and fresh resource
identity independently. Classic requires fresh DefaultProfileName == default_profile;
missing/changed identity means unavailable, with no guessed profile or current fallback.
Read lssyscfg -r prof using lpar_ids/profile_names and exact header
lpar_id,lpar_name,name,lpar_env,affinity_group_id. Require one row matching numeric ID,
partition name, default_profile and native environment; accept only none or canonical1–255.
Separately attempt lssyscfg -r lpar, lpar_ids, header
lpar_id,name,lpar_env,affinity_group_id. Retain build_filter/shlex.quote and strict parser.
Read failures are isolated per plane after the shared identity gate; a failed native
value read cannot suppress the independent profile attempt. A contradictory identity
or ambiguous selected identity remains a safety failure even if another value matches.
Unsupported/transport/missing/malformed value evidence is readback-unavailable; a
valid unequal value is mismatch. No raw provider payload/exception chain is returned.
Controller queries only native partition; no fabricated named-profile or saved value.

The affinity_group step retains requested and observed maps. Top result.status becomes
saved-configuration-verified, mismatch (saved value differs), or readback-unavailable.
Add observation_status with matches-request/mismatch/readback-unavailable per queried
plane; observed contains normalized values or null. Native never receives “verified”.
Step ok requires saved proof and consistent contained identity; otherwise error.
Always include bounded message: “Saved affinity configuration verified; native partition
observation is separate. Current/pending semantics and physical placement are unproved”
or “Partition exists or may exist; saved affinity configuration unverified. Inspect
before retrying; no rollback performed.” Controller explains no saved-proof source.
Classic profile=request with valid partition!=request now passes the saved criterion;
native-only unavailable evidence also cannot replace or defeat independent saved proof.
Wrong/unknown shared identity, wrong/unknown saved value or ownership still stops effects.

LPAR preserves resource_created, ownership and incomplete workflow truth; strict stamp
exceptions remain. All requested-family steps independently gate follow-ups. Only an
existing explicit optional apply may follow required saved/identity/ownership proof;
false/None adds no apply effect. Requested true requires an ok acknowledgement before
assignments: error, absent or skipped acknowledgement blocks them. No second apply.
VIOS retains dict entry-or-HMCError: success adds affinity_verification containing this
same result; omission preserves the entry. Failure text reports bounded normalized
observations/statuses when available. Preserve any serial_verification metadata.
Separate CLI/MCP renderers expose these facts without renaming saved as effective.

## Failure and threat model

Authenticated Python/CLI/MCP callers and untrusted HMC replies on supported hosts are
the actors; boundaries widen only creation input/native records/readback. Strict scalar
and request-context validation, fixed-key encoding, containment, authority, ownership,
audit and existing SSH timeout protect single-create ordering. Transport/authority races
remain possible: report uncertainty and inspect, never retry/rollback automatically.
No atomic-fencing, guest, hypervisor placement or controller saved-state guarantee.

## Validation and operational proof

The plan covers each changed contract plus retained C1–C5. Python>=3.11; CI amd64
Ubuntu24.04/Python3.11–3.14; no dependency/floor changes. No hardware gate runs here.
ST57/group lpar-affinity-group/scenario st57-lpar-affinity-group-creation stays opt-in:
none then1, one owned inactive AIX/Linux fixture, apply false, independent saved/native
reads, owned inactive-only deletion, baseline/absence comparison, preflight/recovery.
A controller native match cannot pass ST57 saved proof. Preserve failed/partial diagnostic
observations without promoting them. The old none/none and failed positive source-only
history stays unchanged; new candidate-bound evidence is required for any future credit.
VIOS/controller/other IDs/releases/hardware remain disclosed unrun gaps, not exclusions.
