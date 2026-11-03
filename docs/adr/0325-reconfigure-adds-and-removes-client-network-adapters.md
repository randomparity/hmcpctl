# 0325 — Reconfigure adds client network adapters by VLAN and removes them by MAC

## Status

Accepted (2026-11-03), issue #1845 (piece 2 of #1228). This record extends ADR 0314's
`hmc_reconfigure_lpar` with a `networks` patch family. Its persistence reuses the
`lpar.profile_save` leg of ADR 0322 (#1846).

## Context

- A client network adapter is a partition-side virtual Ethernet adapter. Its port VLAN must name an
  existing virtual network. Adding or deleting one changes only the partition's current
  configuration (`operations/lpar/profile_sync.py`). A profile activation drops an adapter that the
  profile lacks. It also restores an adapter that the profile still lists.
- `ProfilePatch` carries no adapter fields (ADR 0313). The operator decided on 2026-11-03 that
  reconfigure persists a client-adapter add or removal with ADR 0322's force-save of the current
  configuration over the default profile.
- On one partition, the MAC is the adapter field that the guest sees and that provision records
  (ADR 0313, `adapters.mac`). The slot is an HMC placement detail, and the adapter UUID is an REST
  handle. A VLAN can carry several adapters.
- On an activated partition, an adapter add or delete is a DLPAR operation. A detach on a running
  partition without RMC was captured half-applied (HSCL2957, ADR 0321 Context). An Ethernet add on
  a not-activated partition with no applied configuration is refused with REST0269 (ADR 0315).
- Profile readbacks compare virtual slot numbers only (`profile_sync._profile_slots`). The HMC
  chooses the slot of an adapter added without one, so it may reuse a slot that a removal freed.

## Decision

1. **Patch.** `networks.add[]` holds 0–4 items. Each item has a `port_vlan_id` and an optional
   `mac`, a unicast address in lower-case colon form (`install_media`'s MAC pattern with the group
   bit clear), created as given. No `vsi_*` or policy fields are accepted. `networks.remove[]`
   holds 0–4 items, each with a `mac` of the same form. The patch needs at least one item. No MAC
   may appear twice. An add without a `mac` shares its VLAN with no other add.
2. **A removal selects exactly one adapter.** It is the partition's one client network adapter whose
   MAC equals `mac`. When none matches, or more than one does, the patch is refused before intent.
   The delete targets the planned adapter UUID only, after a re-read confirms that the UUID still
   carries that MAC. No other adapter, VLAN or VIOS object is touched.
3. **When it may run.** On an activated partition, any `networks` item needs the partition to be
   `running` with RMC `active`. A removal there also needs `allow_disruption=true`. Each change
   reports `live` on an activated partition and `profile` on a partition that is not activated.
   An add whose VLAN has no virtual network is refused at planning.
4. **Persistence.** Any planned network change sets the plan's `save` to the default profile, so
   ADR 0322's `profile_save` runs last, after the verification poll ends `completed`. The ADR 0322
   guard refuses the patch before any write in two cases: it holds a `pending_activation` change,
   or the default profile's desired memory, processors or units differ from the partition's. The
   save's readback adds one check for removals. A removed adapter's slot must be gone from the
   profile unless a current client adapter now holds that slot. A removal-only save may leave a
   profile with no virtual adapter, so the leg's "a profile with no adapter is unsaved" rule
   applies only when the plan adds one. `execution: unknown` counts only when that readback holds.
5. **Effects and classifiers.** The order is adds first (`network_add:<i>`,
   `lpar.client_network_adapter`), then removals (`network_remove:<i>`,
   `lpar.client_network_adapter_remove`), each with target `lpar:<uuid>`. Both come after ADR
   0322's disk steps and before `profile_save`. Adding first means no new adapter can take a slot
   that a removal frees, so a removed slot in the profile always means the save did not land. For
   each unpinned add, the plan records the UUIDs of the adapters already on its VLAN as that add's
   baseline. A removal classifies by whether the planned UUID is still listed: listed with the
   MAC → `not_applied`; absent → `applied`; listed with a different MAC → `needs_attention`. An add
   with a pinned MAC classifies by that MAC. An add without one classifies by the adapters on its
   VLAN that are absent from its baseline: none → `not_applied`; one → `applied`; more →
   `needs_attention`. A write decides its own outcome only when it is definite. The HMC's
   classified add refusals (ADR 0315) and a refusal raised before submission are `not_applied`. A
   removal whose UUID is already absent is applied. Any other error leaves the effect open, and
   the classifier decides it on resume.
6. **Authority.** Adds delegate to `hmc_add_network_adapter` and removals to
   `hmc_delete_adapter`. Both delegate to `hmc_save_current_partition_profile`, authorized with the
   `lpar` and `managed_system` targets. `networks` requires `exclusive_writer_window=true`, because
   the force-save is a profile write (ADR 0242, ADR 0314).

## Consequences

- A removal also deletes whatever traffic that adapter carries in the guest. That is why it needs
  `allow_disruption` while activated.
- The default profile becomes a copy of the current configuration. Each run keeps one `hmcprec-`
  recovery profile (ADR 0242, ADR 0322).
- When an add or removal applies but the save does not, the profile disagrees with the partition.
  The result warns that a later profile activation would undo the change. Resume saves; abandon
  leaves the profile as it was.
- A not-activated partition that has never had a configuration applied cannot add an adapter until
  a profile apply (REST0269 guidance, ADR 0315).
- A removed adapter's MAC can be reused by the HMC later, so a MAC alone never proves the removal
  after the run ends. The recorded UUID decides the removal.

## Considered & rejected

- **Do nothing; keep `hmc_add_network_adapter` and `hmc_delete_adapter`.** judgment: fit. Issue
  #1845 and the H1 spec assign networks to reconfigure, persisted into the profile.
- **Remove by VLAN.** judgment: fit. A VLAN can carry several adapters on one partition, so
  "detach only the selected connection" would need a second selector anyway.
- **Remove by slot or by adapter UUID.** judgment: fit. Neither is what a caller or a guest knows
  the connection by. Provision returns the MAC, and the plan still records the UUID and slot for
  the write.
- **Persist with a profile patch instead of a force-save.** verified: `ProfilePatch` in
  `src/hmcpctl/documents/partition_profile.py` at main `38c34e81` has no adapter field (ADR 0313).
  The operator's 2026-11-03 decision selects the force-save.
- **Accept `vsi_*` and the policy fields on add.** verified: ADR 0315 records that a complete VSI
  profile routes the add through the VIOS and fails with HSCLB021. The policy fields belong to the
  specialist (#1828).
- **Allow a removal on a running partition without RMC and warn.** judgment: fit. Issue #1845's
  charter requires the refusal (criterion 8). The only capture is the analogous vSCSI mapping
  removal recorded in ADR 0321 Context (HSCL2957); no client network adapter delete without RMC
  has been captured.
- **Remove first, then add.** judgment: fit. A slot-only readback cannot tell a removed adapter
  from a new one that reused its slot, and ordering the adds first removes that case.
