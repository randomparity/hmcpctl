# Scoped LPAR inspection

## Problem and authority

Issue #1696, within epic #1695, requires explicit managed-system scope to
constrain names and UUIDs. LPAR show/state omit the existing operation argument;
shared UUID resolvers and get_lpar bypass it. Scope q1696-3dae3185 governs this
change. Output formats (#1698), VG syntax (#1697), recovery wording (#1702),
the final journey (#1704), unrelated groups and facade expansion are excluded.

## Design

Add optional --system/-s to LPAR show/state and forward it to the existing
operations. Keep their current output and missing-target behavior.
Move the LPAR UUID containment check from ownership into resource_identity,
where scoped reads and mutations both use it; remove redundant ownership checks.
Use the same case-insensitive membership rule against the selected system's
VIOS feed for scoped VIOS UUIDs. Names continue through existing exact-name
resolution; omitted/blank scope retains current UUID passthrough and discovery.
The selected-system feed establishes membership before target reads or writes;
its existing disconnected-system diagnostic must survive. Feed failures propagate
the original HMCError, including subtype/status, so callers retain transport-error
handling; confirmed non-membership remains ValueError. Ownership override
bypasses ownership policy, not an explicitly selected system.
Get_lpar must constrain UUIDs without changing its absent-name None result.
Audit CLI systems/lpars/vios/storage target forwarding and direct dependencies;
repair only dropped scope or the same UUID bypass. Required scope remains on
LPAR delete, decommission and capture-console; explain the ownership/workflow
reason in help. Update docs/cli.md and directly affected command recipes.
See [ADR 0226](../../adr/0226-scoped-lpar-inspection.md).

## Global Constraints

Python >=3.11; CI amd64 Python 3.11–3.14. No new dependencies, UUID-only client
API changes, or stable facade exports. No live HMC mutations are authorized.

## Success

Within the four audited groups, supplied managed-system scope reaches target
resolution. LPAR show/state accept both spellings; duplicate names resolve only
inside the chosen system; wrong-system UUIDs fail before target effects. Missing
targets and disconnected systems remain distinct; existing ambiguity, discovery
bounds, ownership checks and required destructive/console scope remain intact.

## Failure model

- Actors and deployments: operators and agents using CLI, MCP, or operation
  functions against authenticated HMC; offline CI transport fixtures.
- Invariants/assets: selected target belongs to the supplied system; no wrong
  partition read/write; ownership authorization remains mandatory where enabled.
- Accepted classes: concurrent hardware relocation after membership observation
  is governed by existing HMC operations, not a new transaction guarantee;
  unavailable selected scope fails closed through existing feed diagnostics.
- Covered elsewhere: output/recovery/VG/journey exclusions above; UUID-only
  transport validation remains client-owned; live hardware evidence unrun.

## Threat model

- Boundaries: caller-provided selectors enter existing CLI/operation resolution;
  HMC-provided feed identities establish membership. No new or widened boundary.
- Actors/trust: authenticated caller may provide mismatched names/UUIDs; HMC is
  trusted for current membership, but absent identity cannot prove containment.
- Controls: exact name matching, case-insensitive UUID membership, selected feed
  only, existing path validation and bounded unscoped discovery. Errors identify
  supplied scope/target, without new credentials or hardware metadata emission.
- Out of scope: compromised HMC and post-check hardware races; existing backend
  security and job contracts govern these, with no new guarantees.

## Validation

Focused parser tests prove scope forwarding and required-scope help. Transport
or operation tests prove name collisions, wrong-parent/missing UUIDs, blank and
omitted scope, mixed UUID case, selected disconnected system, no mutation on
failure and ownership preservation. Exercise existing ambiguity/discovery tests.
Run focused pytest before implementation (expected failures), then after; run
just verify and pinned prek before push. Record CI separately. Documentation
prose has no executable behavior beyond parser-tested command examples.
