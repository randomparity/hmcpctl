"""LPAR operations share target-first, keyword-only system selection."""

import inspect
from importlib import import_module
from unittest.mock import AsyncMock

import pytest

from hmcpctl.operations.lpar.activation import (
    power_off_lpar,
    power_on_lpar,
    submit_power_on_lpar,
)
from hmcpctl.operations.lpar.core import delete_lpar, rename_lpar
from hmcpctl.operations.lpar.migration import (
    abort_lpar_migration,
    migrate_lpar,
    migrate_lpar_with_affinity_preflight,
    recover_lpar_migration,
    validate_lpar_migration,
)


@pytest.mark.parametrize(
    "operation",
    [
        power_on_lpar,
        power_off_lpar,
        submit_power_on_lpar,
        validate_lpar_migration,
        migrate_lpar,
        migrate_lpar_with_affinity_preflight,
        abort_lpar_migration,
        recover_lpar_migration,
    ],
)
def test_target_precedes_keyword_only_optional_system(operation):
    signature = inspect.signature(operation)
    parameters = list(signature.parameters.values())
    assert [p.name for p in parameters[:2]] == ["hmc", "lpar_name_or_uuid"]
    system = signature.parameters["system_name_or_uuid"]
    assert system.kind is inspect.Parameter.KEYWORD_ONLY
    assert system.default is None


@pytest.mark.parametrize("operation", [delete_lpar, rename_lpar])
def test_target_precedes_keyword_only_required_system(operation):
    signature = inspect.signature(operation)
    parameters = list(signature.parameters.values())
    assert [p.name for p in parameters[:2]] == ["hmc", "lpar_name_or_uuid"]
    if operation is rename_lpar:
        assert parameters[2].name == "new_name"
        assert parameters[2].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    system = signature.parameters["system_name_or_uuid"]
    assert system.kind is inspect.Parameter.KEYWORD_ONLY
    assert system.default is inspect.Parameter.empty


@pytest.mark.parametrize("operation", [delete_lpar, rename_lpar])
@pytest.mark.asyncio
async def test_missing_required_system_fails_before_client_access(operation):
    hmc = AsyncMock()
    args = ("partition", "renamed") if operation is rename_lpar else ("partition",)
    with pytest.raises(TypeError, match="system_name_or_uuid"):
        await operation(hmc, *args)
    assert hmc.mock_calls == []


SELECTOR_FAMILY = [
    ("hmcpctl.operations.affinity.ssh", "get_lpar_memopt_score", False),
    ("hmcpctl.operations.affinity.ssh", "get_minimum_affinity_policy", False),
    ("hmcpctl.operations.affinity.ssh", "set_minimum_affinity_policy", False),
    ("hmcpctl.operations.inventory.composite", "fetch_lpar_summary", True),
    ("hmcpctl.operations.lpar.assignments", "apply_lpar_pcie_assignments", False),
    ("hmcpctl.operations.lpar.boot_order", "read_lpar_boot_order", False),
    ("hmcpctl.operations.lpar.boot_order", "set_lpar_boot_order", False),
    ("hmcpctl.operations.lpar.boot_order", "clear_lpar_boot_order", False),
    ("hmcpctl.operations.lpar.configuration", "synchronize_lpar_profile", False),
    ("hmcpctl.operations.lpar.configuration", "configure_lpar_msp", False),
    (
        "hmcpctl.operations.lpar.configuration",
        "configure_lpar_processor_compatibility",
        False,
    ),
    ("hmcpctl.operations.lpar.decommission", "decommission_lpar", False),
    ("hmcpctl.operations.lpar.dlpar", "modify_lpar", True),
    ("hmcpctl.operations.lpar.dlpar", "set_lpar_processors", True),
    ("hmcpctl.operations.lpar.dlpar", "set_lpar_memory", True),
    ("hmcpctl.operations.lpar.migration", "remote_restart_lpar", False),
    ("hmcpctl.operations.lpar.ownership", "resolve_and_authorize_lpar_mutation", True),
    ("hmcpctl.operations.lpar.ownership", "resolve_and_authorize_lpar_names", False),
    ("hmcpctl.operations.lpar.ownership", "set_lpar_ownership_description", False),
    ("hmcpctl.operations.lpar.provision", "attach_disk_to_lpar", True),
    ("hmcpctl.operations.virtualization.adapters", "list_adapters", True),
    ("hmcpctl.operations.virtualization.adapters", "add_network_adapter", True),
    ("hmcpctl.operations.virtualization.adapters", "add_vscsi_adapter", True),
    ("hmcpctl.operations.virtualization.adapters", "add_vfc_adapter", True),
    ("hmcpctl.operations.virtualization.adapters", "delete_adapter", True),
    ("hmcpctl.operations.virtualization.pcie", "assign_dedicated_pcie_slot", False),
    ("hmcpctl.operations.virtualization.pcie", "unassign_dedicated_pcie_slot", False),
    ("hmcpctl.operations.virtualization.pcie", "assign_sriov_logical_port", False),
    ("hmcpctl.operations.virtualization.pcie", "unassign_sriov_logical_port", False),
    ("hmcpctl.operations.virtualization.vnic", "list_fc_ports", False),
    ("hmcpctl.operations.virtualization.vnic", "list_sea_adapters", False),
    ("hmcpctl.operations.virtualization.vnic", "list_vnics", False),
    ("hmcpctl.operations.virtualization.vnic", "add_vnic", False),
    ("hmcpctl.operations.virtualization.vnic", "remove_vnic", False),
]


@pytest.mark.parametrize("module,name,optional", SELECTOR_FAMILY)
def test_complete_selector_family(module, name, optional):
    operation = getattr(import_module(module), name)
    signature = inspect.signature(operation)
    parameters = list(signature.parameters.values())
    assert [p.name for p in parameters[:2]] == ["hmc", "lpar_name_or_uuid"]
    system = signature.parameters["system_name_or_uuid"]
    assert system.kind is inspect.Parameter.KEYWORD_ONLY
    assert system.default is (None if optional else inspect.Parameter.empty)
    if name in {"list_fc_ports", "list_sea_adapters"}:
        assert parameters[1].default is None


@pytest.mark.parametrize(
    "module,name,optional", [case for case in SELECTOR_FAMILY if not case[2]]
)
@pytest.mark.asyncio
async def test_required_family_scope_precedes_client_access(module, name, optional):
    operation = getattr(import_module(module), name)
    hmc = AsyncMock()
    signature = inspect.signature(operation)
    args = [hmc, "partition"]
    keywords = {}
    for parameter in list(signature.parameters.values())[2:]:
        if (
            parameter.name == "system_name_or_uuid"
            or parameter.default is not inspect.Parameter.empty
        ):
            continue
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            keywords[parameter.name] = object()
        else:
            args.append(object())
    with pytest.raises(TypeError, match="system_name_or_uuid"):
        await operation(*args, **keywords)
    assert hmc.mock_calls == []
