# ADR 0375: Classify configured shared processor pool slots from the REST feed

## Status

Accepted (2026-11-13), under the operator-confirmed campaign dispatch for #1975.

## Context

`GET ManagedSystem/{uuid}/SharedProcessorPool` answers with one entry per firmware
pool slot. On the 2026-11-13 capture (V10R3 M1060, POWER9 `sys-R1`) the feed held 64
entries, PoolID 0–63, while `lshwres -r procpool` listed only the two configured pools:
`DefaultPool` (0) and `SharedPool01` (1). The feed carries no field that says whether a
slot is configured. Unconfigured slots report every unit field as 0 and the placeholder
name `SharedPoolNN` (two-digit pool ID). The configured pool 1 also carries a
placeholder-shaped name, `SharedPool01`, with maximum 20 and reserved 1. Pool 0 omits
`MaximumProcessingUnits`, `CurrentReservedProcessingUnits` and
`PendingReservedProcessingUnits`, and is the only entry carrying `AssignedPartitions`.

IBM's `chhwres` reference configures a pool by setting `max_pool_proc_units`
(`docs/refs/hmc-commands-p10/commands/chhwres.md`, "Configure the shared processor pool
with ID 1"). Pool update (#1976) and partition pool assignment (#1977) will validate
against whichever projection this read defines.

## Decision

A pool is **configured** when any of these holds:

1. its PoolID is 0 (the default pool is always configured);
2. `MaximumProcessingUnits`, `CurrentReservedProcessingUnits` or
   `PendingReservedProcessingUnits` is present and greater than zero;
3. its name differs from the placeholder `SharedPool` followed by its PoolID as two
   digits.

Every result names the rule by the fixed identifier
`default-or-nonzero-units-or-renamed`. The list operation returns configured pools by
default and reports how many unconfigured slots it omitted; an explicit
`include_unconfigured` returns all slots with their flag. Get-by-ID and get-by-name read
every slot, so an unconfigured slot is reachable and reported as `configured: false`.

An absent unit field projects as `null` (not applicable), never 0. An absent
`AssignedPartitions` projects as `null` (not reported); a present, empty one projects as
an empty list.

## Consequences

- On the captured feed the rule selects exactly the CLI's pools, {0, 1}.
- Clause 3 errs toward reporting: a slot renamed without capacity reads as configured.
  The rule cannot misclassify a slot with nonzero units as unconfigured.
- A pool configured with maximum 0 and a placeholder name would read as unconfigured; no
  captured or documented case produces one. The live scenario compares the rule against
  `lshwres -r procpool` on every run, which is where a disagreement surfaces.
- Whether REST reports `AssignedPartitions` on a non-default pool with members is
  uncaptured. Reading it as `null` keeps "not reported" distinct from "no members". The
  live comparison counts `null` as no members, so the first run against a populated
  non-default pool fails its membership assertion rather than passing over the case.
- The default pool reports `AvailableProcUnits` 0 while hosting partitions. The projection
  passes the HMC's value through; it is not the default pool's free capacity, and a writer
  (#1977) must not read it as such.

## Considered & rejected

- **Classify by name alone.** verified: the 2026-11-13 feed's configured pool 1 is named
  `SharedPool01`, the placeholder shape, so a name rule drops it.
- **Classify by `MaximumProcessingUnits > 0` alone.** judgment: equivalent on the capture,
  but a renamed or reserved-only slot would vanish silently; the wider rule costs two
  comparisons.
- **Read the CLI's `lshwres -r procpool` instead.** judgment: adds an SSH dependency to a
  read the REST feed answers, and the CLI omits the UUIDs #1976 needs to address a pool.
- **Return all 64 slots unfiltered.** judgment: 62 placeholder rows bury the two pools a
  caller asked about; the filtered default plus `include_unconfigured` keeps both views.
