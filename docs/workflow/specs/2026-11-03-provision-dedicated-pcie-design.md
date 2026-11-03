# Provision owns a dedicated PCIe slot through its saved profile (#1841)

## Problem

`hmc_provision_lpar` refuses `assignments.dedicated` (#1390) because a slot written to the
creation profile never reached the activated partition. Provision now activates the profile it
saves (ADR 0313), so the refusal blocks a configuration it can carry. Decision: ADR 0318.

## Behaviour

- `validate_provision_assignments` (operations/lpar/provision.py) refuses a dedicated item whose
  `profile_name` is not `default_profile`, naming ADR 0318. It still runs at the operation, MCP
  tool and CLI entry points before any HMC call, and on dry runs. The preconditions also
  refuse a slot missing from the inventory, owned by a partition, or listed by any profile.
- `_Run._assignments()` drops dedicated items (and SR-IOV items when physical I/O carried them).
- `_Run._persist` runs a `dedicated` leg after `profile_readback` when dedicated items exist:
  `apply_validated_lpar_pcie_assignments` with each item's `profile_name` replaced by the saved
  name. A failed or raising leg skips `default_profile` and `power_on` and ends
  `needs_attention`. Its `dedicated[i]` steps replace the leg's summary step, as `assignments`
  does, from steps that leg produced; `_agrees` treats both keys alike.
- Dry-run step names list `dedicated[i]` after `profile_readback`.
- `delegated_tools` adds `hmc_assign_dedicated_pcie_slot` with `assignments.dedicated`.
- The tool and CLI (`--pcie-assignments`) help describe the carried slot instead of the
  refusal. `hmc_plan_lpar`'s coarse step order is unchanged (follow-up candidate).

## Live arm

The lpar-power arm drops the `dedicated-slot-refused` probe and restores the slot request it
sent before 5517a1c6: with the dedicated-arm configuration for the run's system, the first slot
that `hmc_list_dedicated_pcie_slots` reports unowned and no profile lists, sent as
`{"profile_name": "default_profile", "drc_index": <slot>}`. After P is activated it holds
`pcie-slot-owned` when `lshwres -r io --rsubtype slot -F drc_index,lpar_name` pairs the slot
with P. With no eligible slot it records a gap SKIP and provisions without one. The
assertion is listed only when the slot was sent. Decommission deletes P, which frees the slot.

## Failure model

1. **Actors and deployments:** an MCP agent or CLI operator provisioning on an ADR 0165
   system; the lpar-power arm on the operator's live host.
2. **Invariants and assets:** a slot owned by another partition is never taken; a slot is
   written only to a partition this operation created and stamped; a resumed operation never
   reports `configured` without the slot write applied.
3. **Accepted:** a slot taken by another writer between the preconditions and activation may be
   left out of the activation, since it is written not required, and provision does not re-read
   ownership (no fence, ADR 0190); a slot also named in `physical_io.io_slots` in another form makes the specialist
   refuse, pausing the run; an interrupted slot write needs a human (no classifier, ADR 0318).
4. **Covered elsewhere:** slot-environment admission (ADR 0165, `prevalidate`); removing the
   partition and its profiles (decommission, #1847/#1848).

## Threat model

- **Boundary widened:** provision now reaches `hmc_assign_dedicated_pcie_slot`'s write.
- **Actor:** an MCP client whose grant covers provision but not that specialist.
- **Control:** ADR 0313's delegated permit and per-target check, now listing the specialist;
  the specialist's own ownership check and `drc_index` / profile-name validation.
- **Out of scope:** other HMC writers changing slot ownership mid-run (ADR 0190, no fence).

## Success

1. A dedicated request with `profile_name` `default_profile` is accepted and a clean run ends
   `configured` with `dedicated[i]` ok; another profile name is refused with no HMC request.
2. The slot write targets the saved `hmcpctl_…` profile, after the readback and before the
   default-profile write; a failed slot write ends `needs_attention` with no default or
   power-on write.
3. A withheld `hmc_assign_dedicated_pcie_slot` refuses before anything is recorded.
4. A native lpar-power run holds `pcie-slot-owned` after the profile-first activation.
