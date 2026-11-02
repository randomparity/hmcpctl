# `hmc_reconfigure_lpar` design (#1226)

Decision record: [ADR 0314](../../adr/0314-lpar-reconfigure-persists-through-the-profile.md).
Contract: the H1 spec (`2026-10-01-logical-lpar-workflows-design.md`), ADRs 0189, 0190, 0195,
0199 (the wiring precedent) and 0242 (the profile write it reuses).

## Problem

A resource change made through `hmc_modify_lpar`, `hmc_dlpar_mem` or `hmc_dlpar_proc` changes
only the partition's current configuration; with profile sync off, a later profile activation
discards it (#1170). Agents need one durable logical operation that changes processors, memory
and the partition name and says where each change lives.

## Scope

**Tool.** `hmc_reconfigure_lpar`, effect `destructive`, operation `lpar.reconfigure`, MCP only
(no CLI mirror, as `hmc_power_lpar`). Inputs: `request_id`, `lpar_name_or_uuid`,
`system_name_or_uuid` (both required for a new operation, as ADR 0199 Decision 2), `patch`,
`allow_disruption` (default false), `exclusive_writer_window` (default false), `continuation`
(`none` / `resume` / `abandon`), `wait_seconds`, `profile`. Returns the `OperationRecord`.

**Patch** (`ReconfigurePatch`, `extra="forbid"`, at least one field): `name` (new partition
name), `processors` (strict int > 0), `memory_mib` (strict int > 0). Meanings are ADR 0314
Decision 1. Omitted fields are never read into a write.

**Handler gates, before anything is recorded** (every call, continuations included):

- `processors` or `memory_mib` present and `exclusive_writer_window` not true → refused,
  naming the flag (operator decision 1; ADR 0242 unchanged).
- every delegated tool the patch can use is permitted and authorizes this partition and
  system (ADR 0314 Decision 6); otherwise refused.

**Body.** Phase `validating` (first run) resolves the system and partition, holds the ADR 0190
guard, runs the ADR 0011 ownership check, reads the partition and its target profile, and
computes the plan: every change and its classification (ADR 0314 Decisions 2–4) before any
write. It records the plan as effect `plan`. Phase `applying` then writes, in order: `profile`,
`current`, `rename` — skipping each one the plan does not need. A replay (any run after `plan`
is applied) re-resolves the partition by its recorded UUID, re-runs the ownership check and the
stale-plan checks for writes not yet applied, then continues from the recorded plan. Finally it
polls the partition and profile and verifies each change (Decision 7).

**Result** (`result` of the record): `system_uuid`, `lpar_uuid`, `profile_name`,
`changes[]` (`field`, `before`, `after`, `applied` ∈ `live` / `profile` /
`pending_activation`, `reason`), `activation` (`null`, or the profile name a
`pending_activation` change waits for), `observed` (the verified values), `warnings[]`.

**Reuse.** The `profile` effect calls `update_partition_profile` (ADR 0242) unchanged. Its
kind `lpar.profile_write` and classifier factory live in `operations/logical/profile_write.py`
so #1225 registers the same kind with its own expected-patch reader (operator decision 2).
Live and inactive current-configuration writes use `HMCClient.update_logical_partition` with
`partition_updates`, the specialists' own write. The specialists are unchanged.

## Failure model

1. **Actors and deployments**
   - an MCP agent through the served tool, under the server's access policy;
   - one hmcpctl process holding the execution lock (ADR 0190); an operator reading status.
2. **Invariants and assets at stake**
   - a running partition never loses memory or processors unless `allow_disruption=true`;
   - never a reboot, a power action or a processor-mode change;
   - no HMC write before every change is computed; no write when target, owner or plan is stale;
   - a reported `live` / `profile` change is verified in the profile and, where live, the
     current configuration; anything unverified ends `needs_attention`, never `completed`;
   - effect keys and kinds are a persisted contract (ADR 0195).
3. **Accepted failure classes**
   - an interrupted current-configuration write always needs attention (no classifier): DLPAR
     cannot be told from a concurrent change (H1 spec *Resume*);
   - with profile sync on, a later current-configuration change by another writer can overwrite
     a `pending_activation` profile value; the result warns (ADR 0201). This operation's own
     writes cannot: such a plan is refused (ADR 0314 Decision 3);
   - other HMC writers inside the window are not fenced; the caller's `exclusive_writer_window`
     assertion, the profile ETag and the pre-write re-read are the controls (ADR 0242);
   - a guest that does not take a DLPAR memory removal is HMC-reported failure → uncertain →
     `needs_attention`.
4. **Covered elsewhere**
   - network, disk, installer media and boot patching → #1228;
   - provision's use of `lpar.profile_write` → #1225;
   - the specialist tools → unchanged, their own ADRs.

### Threat model

- **Boundaries widened:** a new destructive MCP entry point whose arguments reach
  `update_partition_profile` and `update_logical_partition`.
- **Actor:** an authenticated agent constrained by the access policy; the HMC is trusted.
- **Controls:** the handler's delegated permit + authorize per tool (ADR 0189 Decision 2);
  the body's ownership check every run; `ReconfigurePatch` strict types; `name` passes
  `escape_xml` and must be unused on the system; warnings carry UUIDs, never request text.
- **Out of scope:** independent HMC actors (accepted above); `ownership_override` is not
  offered (as ADR 0199 Decision 7).

## Success

1. A patch with only the given fields changes only those fields; the plan records every change
   before the first write. (criterion 1)
2. Classification follows ADR 0314 Decisions 2–3 for each of: not activated; running with RMC
   active and the value within current bounds; RMC inactive; beyond current bounds; beyond
   profile bounds; a decrease with `allow_disruption` false and true; profile and current values
   that disagree; sync on with a mixed plan (refused). (criteria 2, 3)
3. A continuation re-authorizes and re-checks the owner; a write not yet applied whose basis
   changed (state, RMC, sync, current values, profile ETag) fails without writing; a resume after
   an applied write continues past it. (criterion 4; stale plan)
4. An open `profile` effect resumes by readback; an open current-configuration effect pauses
   `needs_attention`; a partial apply (profile applied, DLPAR uncertain) pauses with both
   recorded. (criterion 5)
5. Processors or memory without `exclusive_writer_window=true` is refused before recording.
   (criterion 6)
6. Tool docs regenerate, CHANGELOG and the capability inventory/maturity record the operation,
   and the H1 spec names the window and ADR 0314. (criteria 6, 9)
7. Live proof per criterion 10 within the established boundary (docs/live-testing.md).

## Validation

Focused tests in `tests/unit/test_lpar_reconfigure.py` (body, classifiers, plan) and
`tests/app/test_lpar_reconfigure_tool.py` (handler gates, registration, schema) with respx
fixtures shaped like `tests/unit/test_lpar_power.py`; repository guardrails `just verify` and
`uv run --no-sync prek run --all-files`. Live arm afterwards, under a LIVE-GRANT.
