# LPAR plan assignment phases

## Scope

An operator reading `hmc_plan_lpar` needs to know which PCIe assignments will already
exist when the partition profile is saved. Provision writes direct SR-IOV logical ports
and vNICs on the current configuration, mounts installer media when requested, then
saves and reads back the profile. Dedicated slots go into that saved profile.
[Issue 1902](https://github.com/randomparity/hmcpctl/issues/1902) brings the preview
and dedicated profile-name check into agreement with those phases.
[ADR 0318](../../adr/0318-provision-writes-dedicated-slots-into-its-saved-profile.md)
and [ADR 0319](../../adr/0319-provision-mounts-installer-media-before-the-profile-save.md)
define the execution order.

## Contract

The MCP entry point and the planning operation already validate the typed request with
`check_request`. This function calls provision's `validate_provision_assignments`
before returning. A mismatch raises a planner `ValueError` before delegated reads.
The message names `assignments.dedicated.profile_name` and the supported profile
from `DEFAULT_PROFILE_NAME`. A caller can put sensitive text in the rejected name, so
the planner omits that value and suppresses the original exception in its traceback.

For a selected target, each populated phase produces one `assign_pcie` entry targeting
the selected system. In the first phase the detail gives the counts of populated
`sriov` and `vnics` collections in execution order. Optional media binding, upload
and mounting follow. After `write_profile`, the second phase gives the count of
dedicated slots. The activation, apply-profile or installer boot steps then follow
their existing order. Empty phases produce zero entries.
Each detail contains collection counts independent of the number of slot identifiers.

Mixed requests can therefore have two entries with the same kind. Readers use the
existing numeric `order` to distinguish them. The digest continues to include its format, connection,
normalized request and selected targets. Provision owns the execution and dynamic availability checks.

## Success

- A valid dedicated assignment followed by an invalid one refuses the request before HMC reads.
  A valid-only request succeeds. The refusal identifies the field and omits the rejected text.
- An install plan with mixed assignments reports current counts before media, then
  profile write, dedicated count and boot steps. Each phase reports its own counts.
- Dedicated-only, current-only and empty requests report respectively one late entry,
  one early entry and zero entries. A large dedicated collection still produces a single count.

## Failure model

- **Actors and deployments** — MCP clients and callers of the typed planner on supported Python.
- **Invariants and assets** — the ordered, bounded preview and the early dedicated profile refusal.
- **Accepted failure classes** — HMC state can change after the preview. A plan reserves no resources.
- **Covered elsewhere** — provision checks live hardware availability. Issue #1903 owns provision
  CLI install and continuation boot support. Issue #1904 owns detach authorization on the ledger VIOS.

Use Python >=3.11 and the existing locked dependencies. Verification uses offline HMC fixtures.
