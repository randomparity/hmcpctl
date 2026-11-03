# `hmc_reconfigure_lpar` network attachments design (#1845)

Decision record: [ADR 0325](../../adr/0325-reconfigure-adds-and-removes-client-network-adapters.md).
Contract: the H1 spec (`2026-10-01-logical-lpar-workflows-design.md`), ADR 0314 (reconfigure),
ADR 0322 (#1846: the `profile_save` leg, its persistence guard and its console-level check),
ADR 0315 (client Ethernet add refusals), ADR 0321 (half-applied detach without RMC).

## Problem

Reconfigure cannot add a client network adapter to a partition, or remove one. The adapter
specialists change only the current configuration, so a profile activation undoes them
(`operations/lpar/profile_sync.py`).

## Prerequisite

This design builds on #1846's code: `profile_persist.py`, and in `reconfigure.py`
`require_persistable`, `_require_profile_jobs`, `_persist`, `SAVE_KEY` and the plan's `save`
key. The branch merges #1846's branch (a merge commit, never a rebase) and is not merge-ready
until #1846 is on `main`.

## Scope

**Patch.** `ReconfigurePatch.networks` (optional, `extra="forbid"`):

- `add[]` (0–4): `port_vlan_id` (StrictInt, 1–4094); `mac` (optional, lower-case colon form, a
  unicast address; provision's `check_mac` rule).
- `remove[]` (0–4): `mac` (required, the same form).
- At least one item; no MAC twice across both lists; two adds without `mac` never share a VLAN.

**Plan** (phase `validating`, before any write). A patch with `networks` reads the default
profile under ADR 0314 Decision 2, then:

- reads the partition's client network adapters once. It records each adapter's UUID, MAC
  (`mac_colon_form`), VLAN and slot as `networks.baseline`;
- a removal needs exactly one baseline adapter with its MAC. The plan records `{mac,
  adapter_uuid, port_vlan_id, slot}`; zero or several matches are refused;
- an add with `mac` is refused when a baseline adapter already has that MAC;
- an add's VLAN must name a virtual network on the system (`provision.check_vlan_exists`);
- the console must be at HMC 10.3.1060 or later (`_require_profile_jobs`);
- on an activated partition (any state other than `not activated`), every item needs state
  `running` and RMC `active`, and a removal also needs `allow_disruption=true`;
- the persistence guard (`require_persistable`) refuses two cases: any `pending_activation`
  change in the plan, and a default profile whose desired memory, processors or units differ
  from the partition's;
- each item joins `changes[]` as `networks.add` (`after` = the item) or `networks.remove`
  (`before` = `{mac, port_vlan_id}`). `applied` is `live` when activated, otherwise `profile`;
- the plan's `save` becomes the default profile's name. `networks.removed_slots` lists the
  removals' slots.

**Effects** (keys and kinds are persisted, ADR 0195). They come after ADR 0322's disk steps and
before `profile_save`:

| Key | Kind | Target | Identity |
| --- | --- | --- | --- |
| `network_remove:<i>` | `lpar.client_network_adapter_remove` | `lpar:<uuid>` | `{adapter_uuid, mac, slot}` |
| `network_add:<i>` | `lpar.client_network_adapter` | `lpar:<uuid>` | `{adapter_uuid, mac, slot, port_vlan_id}` |

Each write, like ADR 0322's disk writes, first re-runs the ADR 0011 check and the plan-basis
staleness check (`_stale`).

- **Remove write.** It re-lists the adapters. The planned UUID must still carry the planned
  MAC, otherwise `EffectNotApplied`. It then calls `hmc.delete_adapter(lpar,
  "ClientNetworkAdapter", uuid)`. An `HMCError` is followed by one re-list: still listed →
  `EffectNotApplied` naming the HTTP status; gone → applied, with `error_status` in the
  identity and a result warning.
- **Add write.** It calls `operations.virtualization.adapters.add_network_adapter` with the
  partition UUID, the system UUID, the VLAN and `mac_address` (colons stripped). That helper
  keeps ADR 0315's REST0269 and HSCLB021 guidance. An `HMCError` or `ValueError` raised before
  the add returns is `EffectNotApplied` when one re-list shows no new adapter on the VLAN. The
  identity comes from the response. When the response carries no MAC or UUID, the identity is
  the one adapter on the VLAN that is absent from the baseline.
- **Classifiers.** A removal is `not_applied` when the UUID is listed with the MAC, `applied`
  when the UUID is absent, and `needs_attention` when the UUID is listed with another MAC. An
  add with a pinned MAC is `applied` when an adapter has that MAC and `not_applied` otherwise.
  An add without one looks at the adapters on its VLAN that are absent from the baseline: none →
  `not_applied`, one → `applied`, several → `needs_attention`.

**Verification.** ADR 0314's poll also requires that every removed UUID is absent and every
added identity's UUID is listed. The result's `observed.networks` lists the MACs the partition
reads.

**Persistence.** It is ADR 0322's `_persist` unchanged, with one addition. The plan's
`removed_slots` pass to `profile_persist.save_current_over` and to the leg's readback and
classifier. A removed slot that is still in the profile, and that no current client adapter
holds, counts as unsaved. It is an additive keyword whose default `()` keeps #1846's behaviour.
When a network effect applied but `profile_save` did not, the result warns that a profile
activation would undo the change: the added adapters would be dropped and the removed ones
restored.

**Handler.** `needs_window` includes `networks`. The delegated tools are
`hmc_add_network_adapter` for adds, `hmc_delete_adapter` for removals, and
`hmc_save_current_partition_profile` for either. Each is authorized with the `lpar` and
`managed_system` targets. The tool docstring and `docs/tools/` are regenerated.

**Docs.** CHANGELOG; the H1 spec's patch and delegated-authorization rows; a `maturity.json`
variant `networks`. It sits in `missing_scope` until the live arm passes and moves to
`implemented_scope` after; the running-partition DLPAR path stays in `missing_scope`.

## Failure model

1. **Actors and deployments**
   - an MCP agent through the served tool, under the access policy;
   - the one hmcpctl process holding the ADR 0190 execution lock;
   - the live arm on the operator's test boundary.
2. **Invariants and assets at stake**
   - a removal deletes only the one planned adapter UUID, and only while it carries the MAC;
   - no VIOS object, virtual network or other partition is written;
   - the force-save never runs over pending profile values (persistence guard);
   - `completed` with a network change means the default profile lists every current client
     adapter and no removed slot (as bounded in entry 3);
   - effect keys and kinds are a persisted contract.
3. **Accepted failure classes**
   - the force-save copies the whole current configuration (ADR 0322 Failure model; the
     operator scoped the guard to desired values). The prior content stays in one `hmcprec-`
     copy per run;
   - slot-based readback: a removed slot reused by a new current adapter reads as saved. That is
     correct, because the profile then lists the new adapter in that slot;
   - an add interrupted after the HMC created the adapter but before identity capture, with an
     unpinned MAC and another writer adding to the same VLAN, classifies `needs_attention`.
     Accepted under the writer window;
   - a removal on a running partition cuts the guest's traffic on that adapter. Gated by
     `allow_disruption`;
   - an add on a not-activated partition with no applied configuration fails REST0269 as
     `EffectNotApplied`, with ADR 0315's guidance;
   - other writers inside the window are not fenced (ADR 0242, as in ADR 0322).
4. **Covered elsewhere**
   - added disks and the save leg itself → #1846 (ADR 0322);
   - installer media and boot → #1844 (ADR 0321);
   - vNIC / SR-IOV and VSI fields → #1828 and its children, ADR 0315.

### Threat model

- **Boundaries widened.** `hmc_reconfigure_lpar` arguments now reach client adapter creates and
  deletes, plus the profile force-save.
- **Actor.** An authenticated agent under the access policy; the HMC is trusted.
- **Controls.** Strict typed patch (VLAN range, MAC regex, bounded lists, `extra="forbid"`);
  delegated permit and authorize per tool (ADR 0189 Decision 2); the ADR 0011 check before every
  write; the remove write's UUID+MAC re-check; the XML builder's existing escaping. Messages
  carry UUIDs, MACs the caller supplied, and HMC codes, never raw HMC bodies.
- **Out of scope.** Independent HMC actors (accepted above).

## Success

1. An add on a not-activated partition creates the adapter, force-saves the default profile and
   ends `completed` with change `profile`. On a running partition with RMC active it reports
   `live`. (criteria 1, 3)
2. A removal deletes only the planned adapter, and the profile no longer lists its slot; the
   other adapters stay. (criteria 2, 3, 7)
3. Each of these is refused before intent, with a message naming the cause: no matching MAC, or
   several; a pinned MAC already present; a VLAN with no virtual network; an activated partition
   that is not running with RMC active; a removal on an activated partition without
   `allow_disruption`; a pending-activation change or a profile/partition desired-value
   mismatch; a console below 10.3.1060; more than 4 items; a duplicate MAC; `networks` without
   `exclusive_writer_window`; an unpermitted delegated tool. (criteria 4, 6, 8)
4. Each new kind classifies `applied` / `not_applied` / `needs_attention` by its live read.
   Resume after an applied `network_add:<i>` does not add again. A save whose helper reports
   unknown is `applied` only when the readback holds, removed slots included. (criteria 5, 7)
5. Tool docs regenerate. `maturity.json`, the CHANGELOG and the H1 spec rows name the change.
   (criterion 10)
6. Live (arm 70, extended, under a LIVE-GRANT). On the not-activated scratch partition, an add
   on `LIVE_TEST_PROVISION_VLAN_ID` reads back `profile`, and the default profile lists the
   adapter. A profile activation shows it in the running configuration. After power-off, a
   removal by its MAC reads back `profile`, and the profile no longer lists its slot. Teardown
   deletes the partition. Recovery reports a leftover scratch partition as today. (criteria 9,
   10)

## Validation

Focused tests:

- a new `tests/unit/test_lpar_reconfigure_networks.py`: the patch model, the plan refusals, the
  writes, the classifiers, verification, persistence wiring and resume;
- `tests/unit/test_profile_persist.py`: the `removed_slots` readback;
- `tests/app/test_lpar_reconfigure_tool.py`: the window and delegation gates;
- `tests/test_live_lpar_reconfigure_arm.py`: the arm's network leg.

Then `just static`, the four `just test-group` groups, `just smoke`, `just build`,
`just verify-artifacts` and `uv run --no-sync prek run --all-files`. Under a LIVE-GRANT, run
preflight, then the arm, then recovery, then `scripts/live_test_evidence.py`.
