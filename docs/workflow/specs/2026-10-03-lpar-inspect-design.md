# `hmc_inspect_lpar`: one partition's state, sections and next actions

**Issue:** #1224 (epic #1215) · **Decision:** [ADR 0200](../../adr/0200-lpar-inspect-reports-each-section-on-its-own.md)
· **Governing:** [H1 spec](2026-10-01-logical-lpar-workflows-design.md), ADRs 0189, 0196, 0199
· **Branch:** `feat/inspect-lpar-1224` from `main` · **Guardrails:** `just verify`;
`uv run --no-sync prek run --all-files`

## Purpose and boundary

Agents explain an incomplete boot or configuration today by calling `hmc_get_lpar`,
`hmc_read_lpar_refcodes` and the VIOS reads themselves. `hmc_inspect_lpar` makes those reads in
one read-only call and says which of them answered.

Out of scope (operator-approved exclusions): console capture, leasing and takeover
(`hmc_capture_lpar_console`, ADR 0189 Decision 5, ADR 0172); guest readiness (ADR 0191); the
profile read (#637); power actions (`hmc_power_lpar`); holds (#1231); the default listing switch
(#1232); KGDB transport; any live run (#1230, #1232).

## Contract

**Inputs:** `lpar_name_or_uuid` (required), `system_name_or_uuid` (required), `include`
(a list of `resources`, `rmc`, `profile_drift`, `refcodes`; default `["rmc", "refcodes"]`;
duplicates collapse; an empty list includes no section), `profile`.

**Result** (`LparInspection`):

| Field | Meaning |
| --- | --- |
| `connection`, `id` | ADR 0189 scoped id `<connection>/<system uuid>/<partition uuid>` |
| `system_uuid`, `uuid`, `name`, `partition_id`, `state` | from the base read |
| `rmc` | `{source, state}`, or null when not included; `unavailable` when the read carries no RMC state |
| `resources` | `{current_memory_mib, desired_memory_mib, current_proc_units, desired_proc_units, desired_vcpus, has_dedicated_processors, storage_source, vios[], mappings[], unresolved_mappings}`, or null |
| `refcodes` | `{source, codes[]}` with at most 20 rows, newest first, or null |
| `profile_drift` | a `source`, always `unavailable` (ADR 0200 Decision 4), or null |
| `next_actions` | tool names |

A `source` is `{status: ok | unavailable | denied, tool, detail}`. `detail` is null on `ok` and
at most 500 characters otherwise. The `resources` figures come from the base read and are
always present when `resources` is included; `storage_source` covers only `vios[]`,
`mappings[]` and `unresolved_mappings`. `vios[]` holds one `{uuid, status, detail}` per VIOS on
the system, in listing order, at most 16. A listed VIOS with no UUID is a row with `uuid: null`
and status `unavailable`, and is never admitted; a VIOS whose storage detail is empty is
`unavailable`. `mappings[]` holds the decommission mapping records (`vios_uuid`, `type`,
`uuid`, `backing_device` when known) that name this partition, from VIOS rows that are `ok`
only. `storage_source.status` is the VIOS list's status when that is not `ok`; otherwise
`denied` if any VIOS was denied, else `unavailable` if any was not read, else `ok`. A system
with more than 16 VIOSes reads the first 16 and reports `unavailable`.

**`next_actions`**, filtered to tools the policy's capability ceiling admits (a target grant
may still deny one for this partition):

| State | Tools |
| --- | --- |
| `not activated` | `hmc_power_lpar` |
| `error`, `open firmware` | `hmc_capture_lpar_console` |
| `starting`, `shutting down`, `suspending`, `resuming`, `migrating not active`, `migrating running`, `hardware discovery` | `hmc_inspect_lpar` |
| `running`, with `rmc` included and its state not `active` or `busy` | `hmc_capture_lpar_console` |
| any other | none |

**Registration:** ADR 0200 Decision 6. It is in `PRIMARY_TOOLS` and registers only when the
policy permits it.

## Authorization

The handler checks `permits("hmc_get_lpar")` and authorizes the call as `hmc_get_lpar` with the
caller's two selectors; a withheld tool raises `PermissionError` naming it, and a denied target
raises the authorizer's error. Each section then calls `admit(tool, targets)`, which returns the
denial text when the policy withholds the tool or denies the target, and `None` otherwise:

| Section | Delegated tool | Targets |
| --- | --- | --- |
| `refcodes` | `hmc_read_lpar_refcodes` | caller's partition and system selectors |
| `resources` (list) | `hmc_list_vios` | caller's system selector |
| `resources` (each VIOS) | `hmc_get_vios_storage_detail` | VIOS UUID, caller's system selector |

`rmc` and the `resources` figures come from the base read. `profile_drift` reads nothing.
VIOSes are admitted by UUID, so a policy bounding `hmc_get_vios_storage_detail` lists them by
UUID.

## Errors

- The system does not resolve, or the selector matches zero or several partitions on it: the
  call fails with a `ValueError` naming the selector and system. Matching reuses
  decommission's `resolve_target_lpar` (a UUID selector matches only a UUID).
- An `HMCError` from the base read propagates as a tool error.
- A section's `HMCError` (the SSH transport's `HMCCLIError` included) or `ValueError` becomes
  `unavailable` with bounded detail; the other sections still answer.
- After an `HMCTransportError` from a VIOS read, the remaining VIOSes are not read and are
  reported `unavailable` with "the HMC stopped answering", as ADR 0196 and 0198 do.

## Failure model

1. **Actors and deployments:** an MCP agent on a stdio or HTTP deployment under an ADR 0038
   access policy; an operator reading the result.
2. **Invariants and assets at stake:** no data from a source the policy denies reaches the
   result; nothing is written to the HMC and no console is acquired; a section that did not
   answer never reads as healthy; the published result schema.
3. **Accepted failure classes:** state can change between sections, which are separate reads;
   a degraded HMC can hold the call for one REST timeout per read up to the first transport
   failure (base, VIOS list, one VIOS), after which the rest is `unavailable`, plus the
   `refcodes` tail: with a UUID system selector that is one REST timeout to resolve the system
   name in a new session, an SSH fallback lookup, and the `lsrefcode` SSH timeout; `detail` repeats the HMC or SSH error text, which can name the configured
   HMC host, already known to the caller; more than 16 VIOSes is reported `unavailable`.
4. **Covered elsewhere:** the profile read (#637); console capture (`hmc_capture_lpar_console`);
   guest readiness (ADR 0191); ADR 0040 audit records (`dispatch_authorizer`).

## Threat model

- **Boundaries added:** one MCP tool; three delegated authorization points (table above).
- **Actor:** an authenticated MCP agent whose policy may admit the partition and withhold the
  VIOS or refcode tools.
- **Controls:** the inspect grant plus `hmc_get_lpar` for the base read; per-section `admit`
  before each read; selectors reach the HMC only through the existing resolvers and
  `list_lpar_refcodes`, which validates and quotes them; output bounded (20 refcodes, 16 VIOS,
  500-character detail).
- **Out of scope:** console session effects (no console is touched).

## Testing

Unit tests drive `inspect_lpar` with a fake HMC and fake `admit` and SSH reader: each status per
section, the VIOS precedence rule (two VIOSes, one denied), a UUID-less VIOS and an empty
storage detail, the stop after a transport failure, figures present under a denied VIOS list,
the 16-VIOS and 20-refcode bounds, `next_actions` per state, and the not-found and ambiguous
selectors. App tests drive the registered tool: a withheld
`hmc_get_lpar` refuses the call, a withheld VIOS tool reports `denied`, and next actions are
filtered by the policy. The registry, catalog and generated docs gain the one tool.
