# Target-first single-partition selector family
Full-spec L denominator1000, operator-approved scope1;34 APIs plus direct callers
and mock assertions across13 owning modules. API hazard requires full-spec.

## Scope and migration
For the exact family below, move lpar_name_or_uuid immediately after hmc,
followed by operation positional arguments, then keyword-only system_name_or_uuid.
Retain each existing parameter annotation/options/return type. str|None scope
becomes keyword-only defaultNone; str scope remains a required keyword.
Move argument bindings, not algorithms. Update direct production/script/test
callers, imports aliases and mock call assertions together; inspect *args wrappers
and keyword-only calls individually. No compatibility aliases or overload shims.
The explicitly named list_fc_ports/list_sea_adapters retain optional target
defaultNone; this binding migration does not make their target mandatory.
Bulk list_lpar_memopt_scores and raw/prevalidated identity helpers are excluded
as explicitly approved: different scope semantics. Stable six-name facade unchanged.
ADR0238 records the consistent pre-release convention and direct migration.

## Exact family
- `src/hmcpctl/operations/affinity/ssh.py::get_lpar_memopt_score` — scope `str`
- `src/hmcpctl/operations/affinity/ssh.py::get_minimum_affinity_policy` — scope `str`
- `src/hmcpctl/operations/affinity/ssh.py::set_minimum_affinity_policy` — scope `str`
- `src/hmcpctl/operations/inventory/composite.py::fetch_lpar_summary` — scope `str | None`
- `src/hmcpctl/operations/lpar/assignments.py::apply_lpar_pcie_assignments` — scope `str`
- `src/hmcpctl/operations/lpar/boot_order.py::read_lpar_boot_order` — scope `str`
- `src/hmcpctl/operations/lpar/boot_order.py::set_lpar_boot_order` — scope `str`
- `src/hmcpctl/operations/lpar/boot_order.py::clear_lpar_boot_order` — scope `str`
- `src/hmcpctl/operations/lpar/configuration.py::synchronize_lpar_profile` — scope `str`
- `src/hmcpctl/operations/lpar/configuration.py::configure_lpar_msp` — scope `str`
- `src/hmcpctl/operations/lpar/configuration.py::configure_lpar_processor_compatibility` — scope `str`
- `src/hmcpctl/operations/lpar/decommission.py::decommission_lpar` — scope `str`
- `src/hmcpctl/operations/lpar/dlpar.py::modify_lpar` — scope `str | None`
- `src/hmcpctl/operations/lpar/dlpar.py::set_lpar_processors` — scope `str | None`
- `src/hmcpctl/operations/lpar/dlpar.py::set_lpar_memory` — scope `str | None`
- `src/hmcpctl/operations/lpar/migration.py::remote_restart_lpar` — scope `str`
- `src/hmcpctl/operations/lpar/ownership.py::resolve_and_authorize_lpar_mutation` — scope `str | None`
- `src/hmcpctl/operations/lpar/ownership.py::resolve_and_authorize_lpar_names` — scope `str`
- `src/hmcpctl/operations/lpar/ownership.py::set_lpar_ownership_description` — scope `str`
- `src/hmcpctl/operations/lpar/provision.py::attach_disk_to_lpar` — scope `str | None`
- `src/hmcpctl/operations/virtualization/adapters.py::list_adapters` — scope `str | None`
- `src/hmcpctl/operations/virtualization/adapters.py::add_network_adapter` — scope `str | None`
- `src/hmcpctl/operations/virtualization/adapters.py::add_vscsi_adapter` — scope `str | None`
- `src/hmcpctl/operations/virtualization/adapters.py::add_vfc_adapter` — scope `str | None`
- `src/hmcpctl/operations/virtualization/adapters.py::delete_adapter` — scope `str | None`
- `src/hmcpctl/operations/virtualization/pcie.py::assign_dedicated_pcie_slot` — scope `str`
- `src/hmcpctl/operations/virtualization/pcie.py::unassign_dedicated_pcie_slot` — scope `str`
- `src/hmcpctl/operations/virtualization/pcie.py::assign_sriov_logical_port` — scope `str`
- `src/hmcpctl/operations/virtualization/pcie.py::unassign_sriov_logical_port` — scope `str`
- `src/hmcpctl/operations/virtualization/vnic.py::list_fc_ports` — scope `str`
- `src/hmcpctl/operations/virtualization/vnic.py::list_sea_adapters` — scope `str`
- `src/hmcpctl/operations/virtualization/vnic.py::list_vnics` — scope `str`
- `src/hmcpctl/operations/virtualization/vnic.py::add_vnic` — scope `str`
- `src/hmcpctl/operations/virtualization/vnic.py::remove_vnic` — scope `str`

## Success
C1:34 signatures target-first with approved requiredness and operation-argument
order preserved. Missing required system rejected before client work.
C2: direct consumers preserve original target/system/operation argument values,
authorization and containment checks; nullable scope omitted/None preserves
existing discovery policy. Keyword argument callers retain intended bindings.
C3: no production behavior/wire output/schema changes beyond signature binding;
maintained docs regenerate when affected, historical decisions remain unchanged.

## Failure model
Actors: repository operation callers, CLI/MCP adapters, offline domain tests.
Assets: target/system containment, ownership authorization, mutation destinations,
argument validation and output. Accepted classes: pre-release external direct
callers must migrate; no compatibility path was requested. Live hardware proof
not needed for binding-only change. Other owners: transport/config/auth algorithms
and stable facade remain existing implementations, not rewritten here.

## Validation
Extend existing tests/lpar/test_selector_signatures.py with explicit34-family
inventory and required/optional scope assertions; original signatures must fail.
Required scope failure binds before client access; representative optional omitted
and explicitNone, names/UUID scope calls retain real resolution/auth boundaries.
Preserve existing domain tests and distinct target/system sentinel values in mock
assertions. Do not make tests fit a target/system swap. AST audit permits changed
call argument syntax only for migrated family, no unrelated body rewrites.
Use imports-aware source audit for aliases/attributes to avoid changing same-name
HMCClient/SSH methods with different contracts; never global same-name replacement.
Run affected lpar/network/vios/storage/system/app and unit consumer tests based
on exact references, --no-cov; lint/format/types, docs regenerators when required,
actual14commit hooks. Root final just verify and pinnedallfilesprek.
