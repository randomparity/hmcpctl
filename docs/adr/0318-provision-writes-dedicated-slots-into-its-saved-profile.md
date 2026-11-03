# 0318 — Provision writes dedicated PCIe slots into the profile it saves

## Status

Accepted (2026-11-03), issue #1841 (piece 3 of #1225). Extends ADR 0313; lifts the refusal
of the #1390 design.

## Context

- Provision saves the current configuration into a new profile `hmcpctl_<12 hex>` and
  activates from it (ADR 0313 Decision 10).
- A dedicated slot is a profile-only write: `assign_dedicated_pcie_slot` edits a named profile
  of a Not Activated partition and verifies it by exact `io_slots` readback (ADR 0165).
- Live on V10R3 (2026-10-07, docs/live-testing.md) a slot written to the creation profile was
  not owned after a current-configuration activation, so the current configuration does not
  carry it, and a save of that configuration would not either.
- The caller cannot name `hmcpctl_<12 hex>`: it derives from the operation id.

## Decision

1. Provision accepts `assignments.dedicated`. Each item's `profile_name` must be
   `default_profile`, the profile the create writes; any other name is refused before any HMC
   call. Provision writes the slot into the profile it saves, which supersedes
   `default_profile` as the default.
2. A new effect `dedicated` (kind `lpar.pcie_assignments`, target `lpar:<uuid>`, identity
   `{steps}`) runs after `profile_readback` and before `default_profile`. It applies the
   dedicated items with `profile_name` replaced by the saved name. The `assignments` effect
   no longer carries dedicated items.
3. The kind keeps no classifier: an uncertain or failed slot write pauses as
   `needs_attention` for a human (ADR 0313 Decision 5).
4. `hmc_assign_dedicated_pcie_slot` joins the delegated tools when `assignments.dedicated` is
   given (ADR 0313 Decision 8).

## Consequences

- With `power_on`, the activated partition owns each requested slot, provided no running
  partition owns it; an activation refused for that reason pauses at `power_on`.
- `default_profile` never lists the slot; only the saved profile does.
- The steps report `dedicated[i]` after `profile_readback`, not after `storage`.

## Considered & rejected

- **Keep the refusal.** judgment: fit. #1841 requires lifting it.
- **Write the slot to the creation profile before the save.** verified: the 2026-10-07 V10R3
  run (docs/live-testing.md, "The lpar-power arm") shows the slot absent from the current
  configuration, which is what the save copies.
- **Add the slot by DLPAR after power-on.** verified: needs RMC and a running OS, which a
  partition at firmware lacks (docs/workflow/specs/2026-10-13-provision-dedicated-pcie-refusal-design.md).
- **Ignore `profile_name`.** judgment: fit. A silently ignored field misleads the caller.
- **A new kind with a live classifier.** judgment: cost. The H1 Resume table gives
  assignments no classifier, and a human already resolves an assignment pause.
