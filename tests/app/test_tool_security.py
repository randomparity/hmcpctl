"""Exhaustive contract tests for the live tool security classification.

Every registered MCP tool must carry one authoritative ToolSecurity record, and
the MCP annotations shipped to clients must be derived from it. These tests fail
when a tool omits the metadata, contradicts its handler, or silently changes
classification. See docs/adr/0035-enforceable-tool-security-metadata.md.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import get_args, get_type_hints
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from hmcpctl import tool_registry
from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.authorization.dispatch_scope import dispatch_authorizer
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.server import TOOL_MODULES, TOOL_SECURITY, create_mcp
from hmcpctl.server_tools import command as server_command
from hmcpctl.server_tools import permissions as server_permissions
from hmcpctl.server_tools.vios import core as server_vios
from hmcpctl.tool_registry import (
    EFFECTS,
    REQUIRED_TARGET_ARGUMENTS,
    UNBOUNDED_ARGUMENTS,
    TargetSelector,
    ToolSecurity,
    annotations_for,
    validate_security,
)

# Composed here rather than imported: ADR 0041 removed the module-level application, so
# every consumer builds its own. The legacy-equivalent policy registers exactly the
# surface the unpolicied composition used to (pinned by G2 in
# tests/app/test_fail_closed_startup.py), and the dispatch wrapper is schema-transparent,
# so every assertion below reads the same registry it always did.
_LEGACY = compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,))
mcp = create_mcp(_LEGACY)

# The escape hatch's gates come from a policy that *grants* it. Since ADR 0041
# `configure_arbitrary_command_tool` requires both gates, and the flag and the grant
# compose conjunctively (ADR 0036) — so a toggle test asserting "enabled means
# registered" has to supply the grant as well as the flag, which is what an operator
# enabling it actually does.
_HATCH = compile_legacy_policy(
    TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,), include_arbitrary_command=True
)


# Every module a live tool handler is defined in. `server_command` and
# `server_permissions` are not domain modules, so they are absent from
# `TOOL_MODULES`, and the escape hatch is the one tool that most needs checking.
_TOOL_MODULES = (*TOOL_MODULES, server_command, server_permissions)

# The classification as it stood before ADR 0035, transcribed from _app.py at
# 20f3068. A frozen regression snapshot: no tool is ever added here.
LEGACY_READ_ONLY = frozenset(
    {
        "hmc_get_console_info",
        "hmc_list_systems",
        "hmc_system_summary",
        "hmc_list_lpars",
        "hmc_get_lpar",
        "hmc_get_lpar_state",
        "hmc_lpar_summary",
        "hmc_list_vios",
        "hmc_get_vios_storage_detail",
        "hmc_list_resources",
        "hmc_get_job",
        "hmc_fleet_health",
        "hmc_capacity_report",
        "hmc_find_placement",
        "hmc_get_system",
        "hmc_wait_for_job",
        "hmc_list_adapters",
        "hmc_list_configured_hosts",
        "hmc_list_volume_groups",
        "hmc_list_virtual_switches",
        "hmc_list_virtual_networks",
        "hmc_list_network_bridges",
        "hmc_list_fc_ports",
        "hmc_list_sea_adapters",
        "hmc_list_partition_templates",
        "hmc_get_partition_template",
        "hmc_list_clusters",
        "hmc_list_shared_storage_pools",
        "hmc_get_shared_storage_pool",
        "hmc_get_pcm_preferences",
        "hmc_processed_metrics",
        "hmc_processed_metric_links",
        "hmc_aggregated_metrics",
        "hmc_aggregated_metric_links",
        "hmc_get_user",
        "hmc_list_task_roles",
        "hmc_list_resource_roles",
        "hmc_get_remote_access",
        "hmc_list_vios_backups",
        "hmc_get_lpar_description",
        "hmc_get_lpar_msp",
        "hmc_get_proc_compat_modes",
        "hmc_get_lpar_proc_compat",
        "hmc_list_io_slots",
        "hmc_list_memory_pools",
        "hmc_list_vnics",
        "hmc_get_media_repository",
        "hmc_list_optical_media",
        "hmc_list_optical_mappings",
        "hmc_list_storage_mappings",
    }
)

LEGACY_DESTRUCTIVE = frozenset(
    {
        "hmc_power_off_lpar",
        "hmc_dump_restart_lpar",
        "hmc_delete_lpar",
        "hmc_decommission_lpar",
        "hmc_delete_vios",
        "hmc_delete_adapter",
        "hmc_delete_virtual_network",
        "hmc_delete_media_repository",
        "hmc_delete_optical_media",
        "hmc_delete_virtual_disk",
        "hmc_delete_logical_unit",
        "hmc_delete_user",
        "hmc_remove_memory_pool",
        "hmc_remove_vnic",
        "hmc_power_off_system",
        "hmc_power_off_vios",
        "hmc_migrate_abort_lpar",
        "hmc_remote_restart_lpar",
        # Saving current configuration can overwrite an existing saved profile.
        "hmc_save_current_partition_profile",
        "hmc_save_current_vios_partition_profile",
        "hmc_restore_vios",
        "hmc_restore_lpar_profiles",
        "hmc_backup_lpar_profiles",
        "hmc_sync_lpar_profile",
        "hmc_unmount_optical_media",
        "hmc_detach_storage_mapping",
    }
)


def _tools_by_name(enable_arbitrary_command: bool = False):
    asyncio.run(
        server_command.configure_arbitrary_command_tool(
            enable_arbitrary_command,
            mcp,
            permits=_HATCH.permits_tool,
            authorize=dispatch_authorizer(_HATCH),
        )
    )
    try:
        return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    finally:
        if enable_arbitrary_command:
            asyncio.run(
                server_command.configure_arbitrary_command_tool(
                    False,
                    mcp,
                    permits=_HATCH.permits_tool,
                    authorize=dispatch_authorizer(_HATCH),
                )
            )


def test_every_live_tool_has_security_metadata():
    """G1: no live tool escapes the classification, toggle on or off."""
    assert set(_tools_by_name()) <= set(TOOL_SECURITY)
    assert set(_tools_by_name(True)) == set(TOOL_SECURITY)
    assert "hmc_run_command" in TOOL_SECURITY


def test_the_duplicate_optical_detach_tool_stays_removed():
    """#362: `hmc_detach_optical_mapping` is gone by name, not by arithmetic.

    It duplicated `hmc_unmount_optical_media` byte for byte and was removed with
    no alias. The exposed-tool count in test_application_boundaries.py would let
    a later change re-add it and net back to the same number, so pin the name.
    """
    assert "hmc_detach_optical_mapping" not in TOOL_SECURITY
    assert "hmc_detach_optical_mapping" not in _tools_by_name(True)
    assert "hmc_unmount_optical_media" in TOOL_SECURITY


def test_annotations_are_derived_from_the_effect_class():
    """G4: the shipped hint is a function of the declared effect, nothing else."""
    for name, tool in _tools_by_name(True).items():
        assert tool.annotations == annotations_for(TOOL_SECURITY[name].effect), name


def test_declared_effects_use_the_closed_vocabulary():
    for name, security in TOOL_SECURITY.items():
        assert security.effect in EFFECTS, name


def test_available_hmc_ptfs_is_mutating_job_submission():
    security = TOOL_SECURITY["hmc_submit_available_hmc_ptfs_query"]
    tool = _tools_by_name()["hmc_submit_available_hmc_ptfs_query"]

    assert security.effect == "mutate"
    assert tool.annotations.read_only_hint is False


def test_selectors_and_connection_arguments_are_public_parameters():
    """G3: every declared selector is really an argument a caller supplies."""
    for name, tool in _tools_by_name(True).items():
        properties = set(tool.parameters.get("properties", {}))
        security = TOOL_SECURITY[name]
        for target in security.targets:
            if target.container is not None:
                # A nested selector (#260) is a field of a structured parameter,
                # so it must be public one level down in that object's schema.
                nested = tool.parameters["properties"][target.container].get(
                    "properties", {}
                )
                assert target.argument in nested, (name, target.path)
                continue
            assert target.argument in properties, (name, target.argument)
        if security.connection_argument is not None:
            assert security.connection_argument in properties, name


@pytest.mark.parametrize(
    "tool_name, argument, expected_required",
    [
        ("hmc_power_off_lpar", "system_name_or_uuid", False),
        ("hmc_dump_restart_lpar", "system_name_or_uuid", False),
        ("hmc_power_off_vios", "system_name_or_uuid", False),
        ("hmc_delete_vios", "system_name_or_uuid", False),
        ("hmc_restore_vios", "system_name_or_uuid", True),
        ("hmc_list_lpars", "system_name_or_uuid", False),
        ("hmc_delete_lpar", "lpar_name_or_uuid", True),
        ("hmc_migrate_lpar", "target_system_name_or_uuid", True),
    ],
)
def test_optional_selectors_are_marked_optional(tool_name, argument, expected_required):
    """G3 anchors: #223 must be able to tell an omitted selector from a required one."""
    selector = next(
        target
        for target in TOOL_SECURITY[tool_name].targets
        if target.argument == argument
    )
    assert selector.required is expected_required


def test_multi_kind_tools_declare_every_target():
    """G6: the omission the argument table exists to make impossible."""
    migrate = {(t.kind, t.argument) for t in TOOL_SECURITY["hmc_migrate_lpar"].targets}
    assert ("lpar", "lpar_name_or_uuid") in migrate
    assert ("managed_system", "target_system_name_or_uuid") in migrate

    attach = {
        (t.kind, t.argument) for t in TOOL_SECURITY["hmc_attach_disk_to_lpar"].targets
    }
    assert ("lpar", "lpar_name_or_uuid") in attach
    assert ("vios", "vios_uuid") in attach


def test_every_lpar_selector_tool_declares_a_managed_system_selector():
    """#259 case 1: an `lpar` name is unique within a system, not the fleet.

    ADR 0039 carried this as a residual over the lpar tools that took no
    managed-system argument, so `lpar = ["db-01"]` authorized that name on
    every system the granted connection reached. Every tool that declares an
    `lpar` selector now also declares a `managed_system` selector, which makes
    ADR 0039's "every declared selector must be supplied and matched" rule
    total over the kind instead of partial.
    """
    offenders = sorted(
        name
        for name, security in TOOL_SECURITY.items()
        if any(t.kind == "lpar" for t in security.targets)
        and not any(t.kind == "managed_system" for t in security.targets)
    )
    assert not offenders, (
        "these tools authorize an lpar identity no system selector pins: "
        f"{offenders}. Add a system_name_or_uuid parameter and pass it to "
        "resolution."
    )


@pytest.mark.parametrize(
    "tool_name",
    ["hmc_migrate_lpar", "hmc_migrate_validate_lpar", "hmc_remote_restart_lpar"],
)
def test_lpm_tools_declare_their_source_system(tool_name):
    """#259 case 2: LPM tools bound the source, not only the destination.

    `(lpar, target_system_name_or_uuid)` alone authorized evacuating a
    partition of that name off any source system into the named destination.
    The source arrives as its own `managed_system` selector, so one allowlist
    entry must match both endpoints.
    """
    pairs = {(t.kind, t.argument) for t in TOOL_SECURITY[tool_name].targets}
    assert ("managed_system", "system_name_or_uuid") in pairs


def test_provision_lpar_declares_its_nested_selectors():
    """#260: the VIOS identity one level below the signature is declared.

    Extraction, the audit record, and denial messages see it. #1030 removed the
    nested slot-number selector with the vSCSI step it fed; the tool stays
    non-exhaustive because widening its grant is a separate decision. Pinning
    the container too, since a dotted extra that lost its container would
    silently stop extracting anything.
    """
    security = TOOL_SECURITY["hmc_provision_lpar"]
    assert security.exhaustive_targets is False
    assert [(t.kind, t.path, t.required) for t in security.targets] == [
        ("managed_system", "system_name_or_uuid", True),
        ("vios", "storage.vios_uuid", True),
    ]


def test_backup_vios_non_exhaustive_scope_keeps_required_selector_metadata():
    """SSP scope changes grant semantics, not selector extraction or audit."""
    security = TOOL_SECURITY["hmc_backup_vios"]

    assert security.exhaustive_targets is False
    assert {
        (selector.kind, selector.argument, selector.required)
        for selector in security.targets
    } == {
        ("managed_system", "system_name_or_uuid", True),
        ("vios", "vios_name_or_uuid", True),
    }


def test_target_declarations_are_internally_consistent():
    """G6: V7 and V8 hold for every record in the index.

    tool() validates what it collects, so this restates those rules for records
    that reach TOOL_SECURITY another way — today the hand-built escape-hatch
    constant, tomorrow anything registered outside the collector.
    """
    for name in _tools_by_name(True):
        security = TOOL_SECURITY[name]
        if security.target_kind == "none":
            assert security.targets == (), name
            assert security.connection_argument is None, name
            continue
        if security.target_kind != "console":
            assert any(t.kind == security.target_kind for t in security.targets), name
        arguments = [t.argument for t in security.targets]
        assert len(arguments) == len(set(arguments)), name


# Written independently of tool_registry.REQUIRED_TARGET_ARGUMENTS. Deriving the
# expectation from that table would make the coverage test tautological: deleting
# a row would silently strip every selector it produced and still pass.
EXPECTED_TARGET_ARGUMENTS = {
    "lpar_name_or_uuid": "lpar",
    "lpar_uuid": "lpar",
    "system_name_or_uuid": "managed_system",
    "target_system_name_or_uuid": "managed_system",
    "vios_name_or_uuid": "vios",
    "vios_uuid": "vios",
    "vios_partition_id": "vios",
    "cluster_uuid": "cluster",
    "ssp_uuid": "shared_storage_pool",
    "console_uuid": "console",
    "job_id": "job",
    "template_uuid": "template",
    "draft_template_uuid": "template",
    "policy_name": "password_policy",
    "resource_name_or_uuid": "metric_resource",
}


def test_the_argument_table_matches_its_independent_expectation():
    """G6: a deleted table row silently strips selectors; pin the table itself."""
    assert dict(REQUIRED_TARGET_ARGUMENTS) == EXPECTED_TARGET_ARGUMENTS


@pytest.mark.parametrize(
    "tool_name, expected",
    [
        ("hmc_submit_available_hmc_ptfs_query", {("console", "console_uuid")}),
        ("hmc_update_console_software", {("console", "console_uuid")}),
        ("hmc_get_job", {("job", "job_id")}),
        ("hmc_get_partition_template", {("template", "template_uuid")}),
        ("hmc_get_shared_storage_pool", {("shared_storage_pool", "ssp_uuid")}),
        ("hmc_processed_metrics", {("metric_resource", "resource_name_or_uuid")}),
        ("hmc_create_logical_unit", {("cluster", "cluster_uuid")}),
        (
            "hmc_add_vscsi_adapter",
            {("lpar", "lpar_name_or_uuid"), ("vios", "vios_partition_id")},
        ),
        ("hmc_delete_user", {("user", "user_profile_uuid")}),
    ],
)
def test_selectors_are_built_for_every_table_kind(tool_name, expected):
    """G6: one live tool per table kind, pinned literally."""
    built = {(t.kind, t.argument) for t in TOOL_SECURITY[tool_name].targets}
    assert expected <= built


def test_every_table_argument_becomes_a_target():
    """G6: a handler taking an identity argument cannot silently drop it."""
    for name, tool in _tools_by_name(True).items():
        declared = {t.argument for t in TOOL_SECURITY[name].targets}
        expected = set(tool.parameters.get("properties", {})) & set(
            REQUIRED_TARGET_ARGUMENTS
        )
        assert expected <= declared, (name, sorted(expected - declared))


def test_operation_identities_are_unique():
    """G2: two tools may not claim one operation."""
    operations = [security.operation for security in TOOL_SECURITY.values()]
    assert len(operations) == len(set(operations))


DESTRUCTIVE_NAME_PREFIXES = (
    "hmc_delete_",
    "hmc_remove_",
    "hmc_power_off_",
    "hmc_restore_",
    "hmc_detach_",
    "hmc_unmount_",
    "hmc_sync_",
    "hmc_decommission_",
    "hmc_install_",
)

# Destructive tools whose names carry no such prefix. Listed so the matched set
# below can be asserted exactly, which names the offending tool on failure
# instead of reporting a count that moved.
DESTRUCTIVE_WITHOUT_PREFIX = frozenset(
    {
        # System-profile activation can power every contained member.
        "hmc_activate_system_profile",
        "hmc_backup_lpar_profiles",
        # Disabling vTPM can destroy keys and stored TPM data (ADR 0276).
        "hmc_configure_lpar_security_policy",
        "hmc_configure_vios_security_policy",
        "hmc_configure_lpar_vtpm",
        "hmc_configure_vios_vtpm",
        "hmc_dump_restart_lpar",
        # ADR 0189: classed by the most severe tool it can reach.
        "hmc_invoke_tool",
        "hmc_migrate_abort_lpar",
        # ADR 0199: its stop and restart actions power the partition off.
        "hmc_power_lpar",
        "hmc_remote_restart_lpar",
        # Reset discards existing statistics history irreversibly (ADR 0288).
        "hmc_reset_lpar_memory_statistics",
        # Saving current configuration can overwrite an existing saved profile.
        "hmc_save_current_partition_profile",
        "hmc_save_current_vios_partition_profile",
        # #247: the firmware/software update tools overwrite existing software
        # they did not create (ADR 0035 amendment). No prefix is newly reliable:
        # "update" sits mid-name on hmc_vios_update and would over-match future
        # non-destructive update helpers, so the names are pinned here instead.
        "hmc_update_console_software",
        "hmc_update_firmware",
        "hmc_vios_update",
        "hmc_vios_upgrade",
    }
)


# Handlers defined inside a factory, which the module-level AST passes cannot reach.
_FACTORY_TOOLS = frozenset(
    {
        "hmc_effective_permissions",
        "hmc_search_tools",
        "hmc_invoke_tool",
        "hmc_inspect_lpar",
        "hmc_inventory",
        "hmc_plan_lpar",
        "hmc_power_lpar",
    }
)


def test_destructively_named_tools_are_destructive():
    """G7: defence in depth against the likeliest misclassification.

    A heuristic, not a charter criterion: a tool deliberately named against the
    convention is a discussion, not a gate failure. It cannot catch a new tool
    named outside these prefixes, which is why the declared effect stays a
    reviewed human judgement.

    """
    matched = {n for n in TOOL_SECURITY if n.startswith(DESTRUCTIVE_NAME_PREFIXES)}
    misclassified = {n for n in matched if TOOL_SECURITY[n].effect != "destructive"}
    assert misclassified == set()

    declared = {n for n, s in TOOL_SECURITY.items() if s.effect == "destructive"}
    assert declared == matched | DESTRUCTIVE_WITHOUT_PREFIX


def test_software_and_firmware_update_tools_are_destructive():
    """#247: the human decision ADR 0035 deferred is recorded as destructive.

    Each tool overwrites firmware or system software it did not create — the
    ADR 0035 boundary criterion verbatim — so a policy granting only `mutate`
    must not reach a firmware flash.
    """
    for name in (
        "hmc_update_console_software",
        "hmc_vios_update",
        "hmc_vios_upgrade",
        "hmc_update_firmware",
    ):
        assert TOOL_SECURITY[name].effect == "destructive", name


def test_arbitrary_command_is_absent_by_default_and_maximally_classified():
    """G8: the escape hatch is off by default and is the only arbitrary command."""
    default = _tools_by_name()
    assert "hmc_run_command" not in default
    assert not [n for n in default if TOOL_SECURITY[n].effect == "arbitrary-command"]

    enabled = _tools_by_name(True)
    assert [n for n in enabled if TOOL_SECURITY[n].effect == "arbitrary-command"] == [
        "hmc_run_command"
    ]
    validate_security(
        server_command.HMC_RUN_COMMAND_SECURITY, server_command.hmc_run_command
    )


def test_only_local_tools_open_no_hmc_connection():
    """G10: every tool that reaches an HMC declares how its connection is chosen."""
    local_only = {
        "hmc_list_configured_hosts",
        "hmc_effective_permissions",
        "hmc_search_tools",
        "hmc_invoke_tool",
        "hmc_snapshot_validate",
        "hmc_snapshot_inspect",
        "hmc_snapshot_assess_affinity",
    }
    for name, security in TOOL_SECURITY.items():
        if name in local_only:
            assert security.target_kind == "none", name
            assert security.connection_argument is None, name
        else:
            assert security.connection_argument == "profile", name


def test_no_classification_regresses_against_the_pre_adr_sets():
    """G11: a permutation-proof pin on the classification this change inherited."""
    for name in LEGACY_READ_ONLY:
        assert TOOL_SECURITY[name].effect == "read", name
    for name in LEGACY_DESTRUCTIVE:
        assert TOOL_SECURITY[name].effect == "destructive", name
    assert TOOL_SECURITY["hmc_read_lpar_boot_order"].effect == "read"


def test_the_derivation_tables_are_read_only():
    """The two tables producing every hint and every selector must not be edited."""
    from hmcpctl import tool_registry

    with pytest.raises(TypeError):
        tool_registry.REQUIRED_TARGET_ARGUMENTS["lpar_name_or_uuid"] = "vios"
    with pytest.raises(TypeError):
        tool_registry._ANNOTATIONS["read"] = (False, True)
    with pytest.raises(TypeError):
        tool_registry._ANNOTATIONS["read"][0] = False


def test_annotations_for_hands_out_an_independent_copy():
    """A shared instance would let one in-place edit re-flag a whole effect class."""
    first = annotations_for("read")
    second = annotations_for("read")
    assert first == second
    assert first is not second

    first.title = "changed"
    assert second.title is None
    assert annotations_for("read").title is None


def test_the_classification_index_is_read_only():
    """A record any policy layer reads must not be replaceable at runtime."""
    with pytest.raises(TypeError):
        TOOL_SECURITY["hmc_delete_lpar"] = TOOL_SECURITY["hmc_get_lpar"]


def test_legacy_classification_sets_are_gone():
    """G9: replace, don't deprecate."""
    from hmcpctl import _app, server

    for module in (_app, server):
        for removed in (
            "READ_ONLY_TOOLS",
            "DESTRUCTIVE_TOOLS",
            "_READ_ONLY",
            "_DESTRUCTIVE",
            "_STATE_CHANGING",
        ):
            assert not hasattr(module, removed), f"{module.__name__}.{removed}"


# A handler's connection routes through exactly these shared helpers. Every one
# of them resolves an HMCConfig from `config.build_config`, so a call that omits
# the handler's declared connection argument reaches the deployment default
# whatever the caller — and the access policy — named.
_CONNECTION_BUILDERS = frozenset(
    {
        "build_config",
        "client_from_env",
        "run_limited_collection",
        "ssh_with_client",
        "with_config",
        "with_client",
    }
)

# `host` is deliberately singled out: `build_config` skips the whole profile
# branch when an explicit host is given, exactly as HMC_HOST does, so a handler
# passing one would route around ADR 0038's normalization entirely.
_CONNECTION_OVERRIDES = frozenset({"host"})

_Def = ast.FunctionDef | ast.AsyncFunctionDef
_Scope = _Def | ast.Lambda


def _call_name(call: ast.Call) -> str | None:
    """The called name, for builder matching."""
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None)


def _module_functions(tree: ast.Module) -> dict[str, _Def]:
    """Every top-level function in a module, keyed by name.

    Every one of them, not only the ``_``-prefixed ones: a public same-module
    helper that drops the selector is exactly as fail-open as a private one. No
    handler in ``src/`` routes that way today, so the synthetic
    ``dropped-by-a-public-helper`` case is what keeps this from silently
    narrowing back.
    """
    return {node.name: node for node in tree.body if isinstance(node, _Def)}


def _own_scope(function: _Scope) -> Iterator[ast.AST]:
    """Every node in *function*'s own scope, stopping at a nested one.

    A nested ``def`` or ``lambda`` is a separate frame: its parameters may shadow
    the selector, and a name bound inside it is not a rebinding of the enclosing
    one. Walking through them made both the routing check and the rebind check
    answer about the wrong binding.
    """
    body = function.body if isinstance(function.body, list) else [function.body]
    stack: list[ast.AST] = [function.args, *body]
    # Decorators and a return annotation belong to the enclosing frame and are
    # evaluated at import, so they cannot carry a per-call selector — but a
    # connection opened there is still a connection nothing authorized.
    stack.extend(getattr(function, "decorator_list", []))
    if getattr(function, "returns", None) is not None:
        stack.append(function.returns)
    while stack:
        node = stack.pop()
        yield node
        # A nested frame is yielded so the caller can recurse into it as its own
        # scope, but never descended into from here.
        if not isinstance(node, _Scope):
            stack.extend(ast.iter_child_nodes(node))


def _nested_selector(function: _Scope, argument: str) -> str | None:
    """What *argument* is called inside *function*, or None when it is shadowed.

    A nested frame closes over the enclosing selector unless its own parameter
    list rebinds the name — except when that parameter's *default* is the
    selector, which is how ``def _go(profile=profile)`` and its rename sibling
    ``def _go(chosen=profile)`` carry the value in rather than shadow it.
    """
    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults = [None] * (len(positional) - len(arguments.defaults))
    defaults += list(arguments.defaults)
    pairs = [
        *zip(positional, defaults),
        *zip(arguments.kwonlyargs, arguments.kw_defaults),
        *[(star, None) for star in (arguments.vararg, arguments.kwarg) if star],
    ]
    for parameter, default in pairs:
        if isinstance(default, ast.Name) and default.id == argument:
            return parameter.arg
    return (
        None if any(parameter.arg == argument for parameter, _ in pairs) else argument
    )


def _bound_names(function: _Scope) -> set[str]:
    """Every name *function*'s own scope binds after its parameters.

    Store-context ``Name`` nodes cover assignment, augmented assignment,
    annotated assignment, ``for``, ``with ... as``, walrus, and tuple unpacking,
    without the false positives a target-expression walk produced for
    ``results[profile] = ...`` and ``profile.cached = 1``. The four forms that
    bind without a Store-context ``Name`` are added explicitly.
    """
    names: set[str] = set()
    for node in _own_scope(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.comprehension):
            names |= {
                child.id
                for child in ast.walk(node.target)
                if isinstance(child, ast.Name)
            }
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.alias) and node.asname:
            names.add(node.asname)
        elif isinstance(node, ast.MatchAs) and node.name:
            names.add(node.name)
    return names


def _bound_parameter(helper: _Def, call: ast.Call, argument: str) -> str | None:
    """The parameter *helper* binds the caller's connection argument to, if any."""
    for keyword in call.keywords:
        if isinstance(keyword.value, ast.Name) and keyword.value.id == argument:
            return keyword.arg
    positional = [
        parameter.arg for parameter in [*helper.args.posonlyargs, *helper.args.args]
    ]
    for index, node in enumerate(call.args):
        if (
            isinstance(node, ast.Name)
            and node.id == argument
            and index < len(positional)
        ):
            return positional[index]
    return None


def _assert_builder_call(call: ast.Call, argument: str | None, where: str) -> None:
    """Refuse a connection builder that does not receive the authorized value."""
    # A splat hides its keys from the parser, so the override check below could
    # not see a `host` travelling inside one. Refuse the shape.
    assert all(keyword.arg is not None for keyword in call.keywords), (
        f"{where} splats a mapping into a connection builder, which this check "
        "cannot read; pass the connection argument explicitly"
    )
    # The *value* must be the declared argument, wherever it is supplied.
    # `client_from_env(profile='some-other-profile')` routes somewhere the
    # authorization never decided about, and the keyword arm is the only one
    # available to the SSH family, whose `profile` is keyword-only on
    # `_app.ssh_with_client`. The keyword's own name is not constrained: a tool
    # declaring `connection_argument="connection"` writes `profile=connection`.
    supplied = [*call.args, *(keyword.value for keyword in call.keywords)]
    assert argument is not None and any(
        isinstance(node, ast.Name) and node.id == argument for node in supplied
    ), (
        f"{where} does not receive the value of the declared connection argument, "
        "so it routes to a connection the access policy did not authorize"
    )
    assert not {keyword.arg for keyword in call.keywords} & _CONNECTION_OVERRIDES, (
        f"{where} passes a connection override that skips profile resolution"
    )


def _assert_routes(
    function: _Scope,
    argument: str | None,
    helpers: dict[str, _Def],
    tool: str,
    seen: set[str],
) -> int:
    """Check *function*'s connection builders, following same-module helpers.

    *argument* is the name currently holding the caller's connection selector, or
    None when the call chain did not pass one — in which case any connection
    builder below is routing to the deployment default and fails.

    Returns the number of connection builders reached, so a handler that reaches
    none at all is caught by the caller rather than passing vacuously.
    """
    name_of = getattr(function, "name", "<lambda>")
    if argument is not None:
        assert argument not in _bound_names(function), (
            f"{tool}: {name_of} rebinds {argument!r}; the connection the access "
            "policy authorized is the one the handler must use"
        )
    reached = 0
    for node in _own_scope(function):
        if isinstance(node, _Scope):
            # A nested frame: its parameters shadow the enclosing selector, so a
            # builder inside one that redeclares the name gets no selector at all.
            reached += _assert_routes(
                node,
                _nested_selector(node, argument) if argument is not None else None,
                helpers,
                tool,
                seen,
            )
        elif isinstance(node, ast.Call):
            called = _call_name(node)
            if called in _CONNECTION_BUILDERS:
                reached += 1
                _assert_builder_call(
                    node, argument, f"{tool}: {called}() at {name_of}:{node.lineno}"
                )
            elif (
                isinstance(node.func, ast.Name)
                and called in helpers
                and called not in seen
            ):
                helper = helpers[called]
                reached += _assert_routes(
                    helper,
                    _bound_parameter(helper, node, argument)
                    if argument is not None
                    else None,
                    helpers,
                    tool,
                    seen | {called},
                )
    return reached


def _assert_no_config_construction(function: _Def, tool: str) -> None:
    """Refuse a handler that builds its own HMCConfig.

    It would hand that config straight to ``HMCClient``, reaching an HMC through
    no builder this walk knows. None does today; refusing the construction is
    what keeps the builder set closed.
    """
    assert "HMCConfig" not in {
        _call_name(node) for node in ast.walk(function) if isinstance(node, ast.Call)
    }, f"{tool}: constructs its own HMCConfig, bypassing profile resolution"


def _assert_handler_routes(
    function: _Def, argument: str | None, helpers: dict[str, _Def], tool: str
) -> int:
    """Check one handler end to end, including that it opens a connection at all.

    A handler declaring *no* connection argument needs no second rule: it enters
    the walk with ``argument=None``, so the first connection builder it reaches
    is refused for receiving nothing — which is the correct verdict, since a
    connection nothing selects is a connection no access policy can scope.
    """
    _assert_no_config_construction(function, tool)
    reached = _assert_routes(function, argument, helpers, tool, {tool})
    if argument is not None:
        assert reached, f"{tool}: declares {argument!r} but opens no HMC connection"
    return reached


def test_every_handler_routes_the_connection_argument_it_declares():
    """G12: a declared connection argument is used, not merely accepted.

    Authorization decides on the value of ``connection_argument``; a handler that
    then resolves its client without it makes the decision be about a connection
    the call does not make. That is a fail-open, and it is what
    ``hmc_set_lpar_boot_order`` and ``hmc_clear_lpar_boot_order`` did before #222.

        The check is static and follows same-module helpers down the call chain,
        while recognizing the shared connection builders imported from ``_app``.
        It does not follow any other helper imported from another module, a
        ``functools.partial``, or a callable held in a variable; ADR 0038 records
        that residual.

    The two tools that declare *no* connection argument are checked in the same
    pass, from the other side: entering the walk with no selector, the first
    builder either of them reached would be refused. Nothing else would notice
    one growing a `client_from_env()` call, since neither `validate_security`
    nor the dispatch wrapper reads a handler's body.
    """
    root = Path(server_command.__file__).parent
    checked: set[str] = set()

    for path in sorted(root.rglob("*.py")):
        functions = _module_functions(ast.parse(path.read_text(encoding="utf-8")))
        for name in sorted(functions.keys() & set(TOOL_SECURITY)):
            _assert_handler_routes(
                functions[name],
                TOOL_SECURITY[name].connection_argument,
                functions,
                name,
            )
            checked.add(name)

    # `hmc_effective_permissions`, the two gateway tools, `hmc_inventory`,
    # `hmc_plan_lpar`, `hmc_power_lpar` and `hmc_inspect_lpar` are defined inside a
    # factory rather than at module level, so they are the names this pass cannot
    # reach; every other tool, including the ones that declare no connection argument,
    # is checked.
    assert set(TOOL_SECURITY) - checked == _FACTORY_TOOLS


def _walk(source: str, argument: str | None = "profile") -> int:
    """Run the G12 walk over a synthetic module, as it runs over a real one."""
    functions = _module_functions(ast.parse(source))
    return _assert_handler_routes(
        functions["hmc_probe"], argument, functions, "hmc_probe"
    )


# Each source is a shape no handler in `src/` exhibits, so without these the
# corresponding assertion in `_assert_routes` could be deleted and the suite
# would stay green — which is exactly what happened to three of them.
_REFUSED = {
    "no-argument-at-all": (
        "does not receive the value",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return client_from_env()
""",
    ),
    "hardcoded-keyword": (
        "does not receive the value",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return client_from_env(profile="prod")
""",
    ),
    "another-parameter-as-the-keyword": (
        "does not receive the value",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return client_from_env(profile=system_name_or_uuid)
""",
    ),
    "mapping-splat": (
        "splats a mapping",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return client_from_env(profile, **{"host": "attacker"})
""",
    ),
    "host-override": (
        "connection override",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return client_from_env(profile, host="attacker")
""",
    ),
    "rebound-to-a-literal": (
        "rebinds 'profile'",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    profile = "prod"
    return client_from_env(profile)
""",
    ),
    "rebound-in-a-loop": (
        "rebinds 'profile'",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    for profile in ["prod"]:
        pass
    return client_from_env(profile)
""",
    ),
    "rebound-inside-a-helper": (
        "rebinds 'chosen'",
        """
def open_client(chosen=None):
    chosen = "prod"
    return client_from_env(chosen)


def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return open_client(profile)
""",
    ),
    "dropped-by-a-public-helper": (
        "does not receive the value",
        """
def open_client(chosen=None):
    return client_from_env()


def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return open_client(profile)
""",
    ),
    "shadowed-by-a-nested-parameter": (
        "does not receive the value",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    async def _go(profile=None):
        async with client_from_env(profile) as hmc:
            return hmc

    return _run(_go)
""",
    ),
    "rebound-in-a-comprehension": (
        "rebinds 'profile'",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return [client_from_env(profile) for profile in ["prod"]]
""",
    ),
    "rebound-by-an-except-clause": (
        "rebinds 'profile'",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    try:
        pass
    except ValueError as profile:
        pass
    return client_from_env(profile)
""",
    ),
    "rebound-by-an-import-alias": (
        "rebinds 'profile'",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    import os as profile

    return client_from_env(profile)
""",
    ),
    "rebound-by-a-walrus": (
        "rebinds 'profile'",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    if (profile := "prod"):
        return client_from_env(profile)
    return None
""",
    ),
    "rebound-by-tuple-unpacking": (
        "rebinds 'profile'",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    profile, _ = ("prod", 1)
    return client_from_env(profile)
""",
    ),
    "rebound-by-a-with-clause": (
        "rebinds 'profile'",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    with open("x") as profile:
        return client_from_env(profile)
""",
    ),
    "connection-opened-in-a-decorator": (
        "does not receive the value",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    @_wrap(client_from_env())
    def _go():
        return None

    return client_from_env(profile)
""",
    ),
    "connection-opened-in-a-return-annotation": (
        "does not receive the value",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None) -> client_from_env():
    return client_from_env(profile)
""",
    ),
    "rebound-by-a-match-capture": (
        "rebinds 'profile'",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    match system_name_or_uuid:
        case profile:
            pass
    return client_from_env(profile)
""",
    ),
    "shadowed-by-a-nested-star-parameter": (
        "does not receive the value",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    def _go(*profile):
        return client_from_env(profile)

    return _go()
""",
    ),
    "handler-builds-its-own-config": (
        "constructs its own HMCConfig",
        """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    config = HMCConfig()
    return client_from_env(profile)
""",
    ),
    "dropped-at-the-helper-hop": (
        "does not receive the value",
        """
def open_client(chosen=None):
    return client_from_env(chosen)


def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return open_client()
""",
    ),
}

_ACCEPTED = {
    "direct-positional": """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return client_from_env(profile)
""",
    "direct-keyword": """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return build_config(profile=profile)
""",
    "keyword-only-hop": """
def _open(system, *, chosen=None):
    return client_from_env(chosen)


def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return _open(system_name_or_uuid, chosen=profile)
""",
    "positional-only-hop": """
def open_client(chosen=None, /):
    return client_from_env(chosen)


def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return open_client(profile)
""",
    "renamed-across-two-hops": """
def _outer(picked=None):
    return _inner(picked)


def _inner(final=None):
    return client_from_env(final)


def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return _outer(profile)
""",
    "subscript-target-is-not-a-rebind": """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    results = {}
    results[profile] = client_from_env(profile)
    return results
""",
    "attribute-target-is-not-a-rebind": """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    client = client_from_env(profile)
    client.chosen = profile
    return client
""",
    "an-uncalled-module-helper-is-never-walked": """
def _fmt(profile):
    profile = profile.upper()
    return profile


def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return client_from_env(profile)
""",
    "a-differently-named-selector": """
def hmc_probe(system_name_or_uuid: str, connection: str | None = None):
    return client_from_env(profile=connection)
""",
    "nested-parameter-carrying-the-selector": """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    async def _go(profile=profile):
        async with client_from_env(profile) as hmc:
            return hmc

    return _run(_go)
""",
    "nested-parameter-renaming-the-selector": """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    async def _go(chosen=profile):
        async with client_from_env(chosen) as hmc:
            return hmc

    return _run(_go)
""",
    "nested-async-body": """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    async def _go():
        async with client_from_env(profile) as hmc:
            return hmc

    return _run(_go)
""",
}


@pytest.mark.parametrize(("expected", "source"), _REFUSED.values(), ids=_REFUSED)
def test_the_routing_guardrail_refuses_every_unrouted_shape(expected, source):
    """Each assertion in the G12 walk, pinned against a synthetic handler.

    No handler in ``src/`` exhibits any of these, so this is the only thing
    standing between the guardrail and a silent regression.
    """
    with pytest.raises(AssertionError, match=expected):
        _walk(source)


@pytest.mark.parametrize("source", _ACCEPTED.values(), ids=_ACCEPTED)
def test_the_routing_guardrail_accepts_every_correct_shape(source):
    """The other half: a guard that refused these would fail on correct code."""
    argument = "connection" if "connection: str" in source else "profile"
    assert _walk(source, argument) == 1


def test_the_guardrail_refuses_a_handler_that_opens_no_connection():
    """A handler that declares a selector and never connects passes vacuously."""
    with pytest.raises(AssertionError, match="opens no HMC connection"):
        _walk(
            """
def hmc_probe(system_name_or_uuid: str, profile: str | None = None):
    return {}
"""
        )


def test_the_guardrail_refuses_a_connectionless_handler_that_connects():
    """The other direction: no declared selector means no connection may be opened.

    Neither `validate_security` nor the dispatch wrapper reads a handler's body,
    so this is the only thing that would notice `hmc_list_configured_hosts`
    growing an HMC call.
    """
    with pytest.raises(AssertionError, match="does not receive the value"):
        _walk(
            """
def hmc_probe(system_name_or_uuid: str):
    return client_from_env()
""",
            None,
        )


# Written out rather than derived, for the reason the selector-coverage
# expectation is: a set computed from `security.targets` would agree with any
# registry, including one in which a composite silently became narrowable.
# ADR 0039 grants every name below only under `targets = "all-targets"`.
_NOT_EXHAUSTIVE = frozenset(
    {
        # No selector at all, so a `targets` table has nothing to bind on.
        "hmc_operation_status",
        "hmc_inventory",
        "hmc_capacity_report",
        "hmc_get_console_info",
        "hmc_effective_permissions",
        "hmc_search_tools",
        "hmc_invoke_tool",
        "hmc_find_placement",
        "hmc_fleet_health",
        "hmc_list_clusters",
        "hmc_list_configured_hosts",
        "hmc_list_partition_templates",
        "hmc_list_resources",
        "hmc_list_shared_storage_pools",
        "hmc_list_systems",
        "hmc_snapshot_validate",
        "hmc_snapshot_inspect",
        "hmc_snapshot_assess_affinity",
        "hmc_run_command",
        # System selectors cannot enumerate the system-profile member union.
        "hmc_create_system_profile",
        "hmc_update_system_profile",
        "hmc_delete_system_profile",
        "hmc_activate_system_profile",
        # Selectors, but they do not name every resource the call acts on.
        "hmc_backup_lpar_profiles",
        "hmc_delete_backup_lpar_profile",
        "hmc_backup_vios",
        "hmc_create_lpar",
        "hmc_create_vios",
        "hmc_modify_lpar",
        "hmc_restore_lpar_profiles",
        "hmc_restore_vios",
        "hmc_provision_lpar",
        "hmc_plan_lpar",
        # Selectors, but a continuation names none; the delegated tool binds (ADR 0199).
        "hmc_power_lpar",
        "hmc_reset_lpar_memory_statistics",
        # Selectors, but the VIOS reads sit below the signature (ADR 0200).
        "hmc_inspect_lpar",
        # Selectors, but one of them is a per-system slot number the fleet-wide
        # `vios` allowlist cannot pin down.
        "hmc_add_vfc_adapter",
        "hmc_add_vscsi_adapter",
        # A declared selector that a second argument overrides outright.
        "hmc_get_job",
        "hmc_wait_for_job",
    }
)


def test_the_tools_a_targets_table_cannot_bound_are_exactly_these():
    """G13: `exhaustive_targets` is pinned, not merely present.

    A tool moving into this set loses every narrow grant an operator wrote for
    it; a tool moving out gains the ability to be narrowed by a table that
    cannot see everything it touches. Both are decisions, and neither should be
    reachable by editing a signature.
    """
    actual = {
        name
        for name, security in TOOL_SECURITY.items()
        if not security.exhaustive_targets
    }
    assert actual == _NOT_EXHAUSTIVE


def test_every_selector_less_tool_is_unbounded_and_no_other_is_by_accident():
    """G13: the two halves of the set have different causes, and both must hold."""
    for name, security in TOOL_SECURITY.items():
        if not security.targets:
            assert security.exhaustive_targets is False, name
    declared = _NOT_EXHAUSTIVE - {
        name for name, s in TOOL_SECURITY.items() if not s.targets
    }
    assert declared == {
        "hmc_create_system_profile",
        "hmc_update_system_profile",
        "hmc_delete_system_profile",
        "hmc_activate_system_profile",
        "hmc_add_vfc_adapter",
        "hmc_add_vscsi_adapter",
        "hmc_backup_lpar_profiles",
        "hmc_delete_backup_lpar_profile",
        "hmc_backup_vios",
        "hmc_create_lpar",
        "hmc_create_vios",
        "hmc_get_job",
        "hmc_inspect_lpar",
        "hmc_modify_lpar",
        "hmc_plan_lpar",
        "hmc_power_lpar",
        "hmc_reset_lpar_memory_statistics",
        "hmc_provision_lpar",
        "hmc_restore_lpar_profiles",
        "hmc_restore_vios",
        "hmc_wait_for_job",
    }


# ---------------------------------------------------------------------------
# G14-G16 (#223) — the static half of the target dimension.
#
# G12 above proves a declared *connection* argument is actually routed. These
# three prove the *target* side of the same contract: that a tool claiming its
# selectors bound it is not accepting an identity they cannot see, that a
# declared selector is used rather than merely accepted, and that every selector
# is of a type the boundary can read. See ADR 0039.
# ---------------------------------------------------------------------------


def _selector_annotations() -> dict[str, object]:
    """Every declared selector's resolved annotation, keyed `tool.path`."""
    resolved: dict[str, object] = {}
    for module in _TOOL_MODULES:
        for name, security in TOOL_SECURITY.items():
            handler = getattr(module, name, None)
            if handler is None:
                continue
            hints = get_type_hints(handler)
            for target in security.targets:
                if target.container is not None:
                    # A nested selector (#260) types its field through the
                    # container's own annotation, one level down.
                    container = get_type_hints(hints[target.container])[target.argument]
                else:
                    container = hints.get(target.argument)
                resolved[f"{name}.{target.path}"] = container
    return resolved


def test_every_selector_argument_is_a_type_the_boundary_can_read():
    """G14: `str`, `str | None`, or `int` — nothing else.

    ADR 0039's extraction renders exactly those and refuses everything else as
    UNREADABLE. Refusing is fail-closed, but a *live* tool that always denies is
    a dead tool, so the guarantee has to be that the fourth type never ships.
    That makes UNREADABLE reachable only by a direct caller of the wrapped
    object, which is what the ADR claims.
    """
    allowed = {str, int, str | None}
    resolved = _selector_annotations()
    assert resolved, "no selector annotations resolved; the walk found no handlers"
    unexpected = {
        where: annotation
        for where, annotation in resolved.items()
        if annotation not in allowed
    }
    assert not unexpected, f"selector arguments of an unreadable type: {unexpected}"


def _nested_field_names(annotation: object) -> list[str]:
    """The field names of a dataclass or pydantic model, one level down.

    One level, deliberately: it is what `hmc_provision_lpar` needed and what the
    format can describe. A two-level nest would be caught by neither this check
    nor `build_targets`, which is recorded rather than hidden — the third level
    of nesting is not a shape any tool in `src/` has.
    """
    names: list[str] = []
    for member in get_args(annotation) or (annotation,):
        if is_dataclass(member) and isinstance(member, type):
            names.extend(field.name for field in dataclass_fields(member))
        elif isinstance(member, type) and issubclass(member, BaseModel):
            names.extend(member.model_fields)
    return names


def _unbounded_identities(handler, security) -> list[str]:
    """Identity-bearing argument names *security* does not declare as selectors."""
    declared = {target.argument for target in security.targets}
    found: list[str] = []
    for parameter, annotation in get_type_hints(handler).items():
        if parameter == "return":
            continue
        if parameter in UNBOUNDED_ARGUMENTS:
            found.append(parameter)
        if parameter in REQUIRED_TARGET_ARGUMENTS and parameter not in declared:
            found.append(parameter)
        for field_name in _nested_field_names(annotation):
            if field_name in UNBOUNDED_ARGUMENTS or (
                field_name in REQUIRED_TARGET_ARGUMENTS and field_name not in declared
            ):
                found.append(f"{parameter}.{field_name}")
    return sorted(set(found))


def test_no_exhaustive_tool_accepts_an_identity_its_selectors_cannot_bound():
    """G15: `exhaustive_targets=True` is checked, not taken on trust.

    A declaration nobody checks is a comment. This is the check that catches the
    next `hmc_provision_lpar`: an identity arriving through a nested field, or
    through an argument no TargetKind can express, while the tool claims a
    policy `targets` table bounds it.
    """
    offenders = {}
    for module in _TOOL_MODULES:
        for name, security in TOOL_SECURITY.items():
            handler = getattr(module, name, None)
            if handler is None or not security.exhaustive_targets:
                continue
            if found := _unbounded_identities(handler, security):
                offenders[name] = found
    assert not offenders, (
        "these tools claim their selectors bound them but accept an identity the "
        f"selectors cannot name: {offenders}. Declare exhaustive_targets=False, or "
        "expose the identity as a top-level selector argument."
    )


def test_the_declared_set_is_exactly_what_the_check_finds():
    """G15: the declaration matches the derivation, in both directions.

    ADR 0039 rejected deriving `exhaustive_targets` at registration and kept it
    a declaration. That trade is only honest if the two agree, so this asserts
    the agreement rather than assuming it.
    """
    found = {}
    for module in _TOOL_MODULES:
        for name, security in TOOL_SECURITY.items():
            handler = getattr(module, name, None)
            if handler is None:
                continue
            if unbounded := _unbounded_identities(handler, security):
                found[name] = unbounded
    assert found == {
        "hmc_add_vfc_adapter": ["vios_partition_id"],
        "hmc_add_vscsi_adapter": ["vios_partition_id"],
        "hmc_backup_lpar_profiles": ["file_path"],
        "hmc_get_job": ["job_href"],
        "hmc_restore_lpar_profiles": ["file_path"],
        "hmc_run_command": ["cmd"],
        "hmc_wait_for_job": ["job_href"],
    }


def test_every_handler_reads_the_target_selectors_it_declares():
    """G16: a declared selector is used, not merely accepted.

    The target-dimension twin of G12. A handler that accepts
    `lpar_name_or_uuid` and never reads it would be authorized against a target
    it does not act on — the shape `hmc_set_lpar_boot_order` had for the
    connection argument before #222.

    Its limit is stated rather than implied: it proves the value is *read*, not
    that it reaches the right sink. Following it to a sink would need G12's
    builder table, and there is no target equivalent of `client_from_env`.
    """
    root = Path(server_command.__file__).parent
    unread: dict[str, list[str]] = {}
    checked: set[str] = set()

    for path in sorted(root.rglob("*.py")):
        functions = _module_functions(ast.parse(path.read_text(encoding="utf-8")))
        for name in sorted(functions.keys() & set(TOOL_SECURITY)):
            body = functions[name]
            loaded = {
                node.id
                for node in ast.walk(body)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            missing = [
                target.path
                for target in TOOL_SECURITY[name].targets
                # A nested selector (#260) is read by passing its container
                # onward, so the handler must load the container; the field is
                # read by the operation it is handed to, one module out.
                if (target.container or target.argument) not in loaded
            ]
            if missing:
                unread[name] = missing
            checked.add(name)

    # The same names G12 cannot reach, for the same reason: they are defined inside
    # a factory rather than at module level. One that declares a selector is found
    # as a nested definition instead, and held to the same rule.
    assert set(TOOL_SECURITY) - checked == _FACTORY_TOOLS
    selecting = {name for name in _FACTORY_TOOLS if TOOL_SECURITY[name].targets}
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, _Def) and node.name in selecting:
                loaded = {
                    n.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                }
                missing = [
                    target.path
                    for target in TOOL_SECURITY[node.name].targets
                    if (target.container or target.argument) not in loaded
                ]
                if missing:
                    unread[node.name] = missing
                checked.add(node.name)
    assert not unread, f"handlers that accept a selector and never read it: {unread}"
    assert selecting <= checked


@pytest.mark.parametrize(
    "label, source, expected",
    [
        (
            "a-nested-identity-the-selectors-cannot-see",
            """
@dataclass
class Storage:
    vios_uuid: str


def hmc_probe(system_name_or_uuid: str, storage: Storage, profile: str | None = None):
    return client_from_env(profile)
""",
            ["storage.vios_uuid"],
        ),
        (
            "a-top-level-identity-left-out-of-the-selectors",
            """
def hmc_probe(system_name_or_uuid: str, cluster_uuid: str, profile: str | None = None):
    return client_from_env(profile)
""",
            ["cluster_uuid"],
        ),
        (
            "an-argument-no-target-kind-can-express",
            """
def hmc_probe(system_name_or_uuid: str, file_path: str, profile: str | None = None):
    return client_from_env(profile)
""",
            ["file_path"],
        ),
    ],
)
def test_the_unbounded_identity_check_bites(label, source, expected):
    """G15: each refusal is proven on a shape no handler in `src/` exhibits.

    Without these the assertion could be deleted and the suite would stay green,
    which is what happened to three of G12's.
    """
    namespace: dict[str, object] = {"dataclass": dataclass, "client_from_env": None}
    exec(compile(source, f"<{label}>", "exec"), namespace)  # noqa: S102
    handler = namespace["hmc_probe"]
    security = ToolSecurity(
        effect="mutate",
        operation="probe.run",
        target_kind="managed_system",
        targets=(TargetSelector("managed_system", "system_name_or_uuid", True),),
        exhaustive_targets=True,
    )
    assert _unbounded_identities(handler, security) == expected


def test_the_unread_selector_check_bites():
    """G16: a handler that accepts a selector and ignores it is refused."""
    source = """
def hmc_probe(lpar_name_or_uuid: str, profile: str | None = None):
    return client_from_env(profile)
"""
    body = _module_functions(ast.parse(source))["hmc_probe"]
    loaded = {
        node.id
        for node in ast.walk(body)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    assert "lpar_name_or_uuid" not in loaded
    assert "profile" in loaded


# Where a call loads its *payload* from — a NIM boot server, a firmware
# repository, an ISO — is not an HMC resource, so no TargetKind names one and no
# `targets` allowlist can bound it. ADR 0039 places that outside the target
# dimension deliberately rather than by omission: each of these calls still
# mutates only the resources its selectors declare, and constraining where the
# payload comes from is a different control (ingress) this policy does not offer.
#
# The line against UNBOUNDED_ARGUMENTS is *not* which side the named thing lives
# on. It is whether a `targets` table can bound the identity, which for an
# HMC-side file reduces to ADR 0039's containment question: is the resource
# reached from a declared selector, or not? `file_path` fails it because
# `bkprofdata -f` and `rstprofdata -f` take an absolute console path the declared
# `-m` system does not constrain — which is why the tool that only *reads* that
# path is unbounded too, and why "the file is written" is not the rule.
# `backup_name` on `hmc_restore_vios` passes it, and ADR 0044 records why; the
# side of the filesystem decided neither case.
#
# Every name below is a remote host or a source outside the HMC, which no
# `targets` table could reach under any design, so refusing the tool would buy
# nothing.
#
# `iso_source` was the awkward one: it used to accept anything, and anything
# without an http(s) scheme was read as a path on the **MCP server host** and
# uploaded into the granted VIOS's media repository (#261). ADR 0049 closed that
# by admitting only `http` and `https`, so the name is now a remote URL like the
# rest of this set and belongs here for the same reason they do. It stays named
# in this comment rather than quietly folded in, because the classification was
# once a judgement call and the record of why should not evaporate with the
# branch that made it one.
#
# What ADR 0049 did *not* close is the tool fetching a caller-supplied URL from
# the MCP server's network position — that is #303, and it is still a source
# outside the HMC that no `targets` table could reach.
_PAYLOAD_SOURCE_ARGUMENTS = frozenset(
    {
        "repository",
        "platform_update",
        "install_source",
        "nim_gateway",
        "nim_subnetmask",
        "lpar_ip",
        "vios_ip",
        "iso_source",
    }
)
#
# `nim_ip` left the set with #410: the InstallLPAR/InstallVIOS REST jobs it fed
# do not exist (ADR 0069), and the installios CLI bridge (ADR 0070) has no
# external NIM-server address — the HMC itself serves the image named by
# `install_source`, which joined the set in its place.


def test_payload_source_arguments_are_out_of_the_target_dimension_by_decision():
    """G15: the tools this decision covers, and the reason it is not an omission.

    Each of these mutates exactly the resource its selectors declare — a system,
    a VIOS, a console, a partition — while reading its payload from a source the
    caller chose. That is a real risk and it is not this dimension's: a `targets`
    allowlist bounds *what is acted on*, and none of these acts on anything the
    allowlist cannot already name.

    The enumeration is the point. A threat scan found `iso_source` missing from
    an earlier version of this set, which meant the "a sixth cannot join them
    silently" claim below was false at the moment it was written.

    Membership here never meant "harmless". `iso_source` was in this set while it
    still read the MCP server's own filesystem (#261); it stayed in the set when
    ADR 0049 cut that branch away, because neither the risk nor its removal was
    ever a *target* question. This test pins classification, so it is not
    evidence that any member is safe — only that a `targets` allowlist is not the
    thing that would make it so.
    """
    found = {}
    for module in _TOOL_MODULES:
        for name, security in TOOL_SECURITY.items():
            handler = getattr(module, name, None)
            if handler is None:
                continue
            hits = sorted(_PAYLOAD_SOURCE_ARGUMENTS & set(get_type_hints(handler)))
            if hits:
                found[name] = (security.exhaustive_targets, hits)

    assert found == {
        "hmc_install_vios": (
            True,
            ["install_source", "nim_gateway", "nim_subnetmask", "vios_ip"],
        ),
        "hmc_update_console_software": (True, ["repository"]),
        "hmc_update_firmware": (True, ["platform_update"]),
        "hmc_upload_iso": (True, ["iso_source"]),
        "hmc_vios_update": (True, ["repository"]),
        "hmc_vios_upgrade": (True, ["repository"]),
    }
    # The two tables must stay disjoint, or the decision above would silently
    # contradict the one UNBOUNDED_ARGUMENTS encodes: a name cannot both be
    # outside the dimension and be the reason a tool is refused by it.
    assert not (_PAYLOAD_SOURCE_ARGUMENTS & UNBOUNDED_ARGUMENTS)


def test_every_unbounded_name_carries_its_reason_beside_the_set():
    """G15: the list and the prose beside it cannot drift apart unnoticed.

    #264 was that drift: the comment stated the line as which side of the HMC a
    name lives on, while the set was maintained on ADR 0039's containment
    question, and `backup_name` fell in the gap between them. A name added to the
    set without a reason written beside it is how the next one happens, so this
    fails the author rather than the reader.

    `backup_name` is named too, although it is deliberately *not* a member: it is
    the case that made the criterion explicit, and a comment that stopped
    mentioning it would have lost the only worked example of a name on the
    bounded side.
    """
    source = Path(tool_registry.__file__).read_text(encoding="utf-8")
    head, anchor, _ = source.partition("UNBOUNDED_ARGUMENTS: frozenset")
    assert anchor, (
        "UNBOUNDED_ARGUMENTS was renamed or reformatted; re-anchor this check"
    )
    opening = "# Public argument names that carry the identity"
    assert opening in head, (
        f"the comment opening {opening!r} moved; re-anchor this check"
    )
    comment = head[head.rindex(opening) :]

    undocumented = sorted(
        name for name in UNBOUNDED_ARGUMENTS if f"`{name}`" not in comment
    )
    assert not undocumented, (
        f"these names are in UNBOUNDED_ARGUMENTS with no reason recorded beside "
        f"the set: {undocumented}. Add the bullet saying what identity the name "
        "carries and why no targets table can bound it."
    )
    assert "`backup_name`" in comment, (
        "the UNBOUNDED_ARGUMENTS comment no longer explains why `backup_name` is "
        "not a member; see ADR 0044."
    )

    # The sentence #264 was actually filed about lives here, above
    # `_PAYLOAD_SOURCE_ARGUMENTS`, not beside the set in `tool_registry.py`. It
    # said the line was "*which side* the named thing lives on", which is what
    # gave the wrong answer for `backup_name`. Pin the retired formulation out and
    # the replacement in, so this file's rule text cannot drift back or fall
    # silent about the case that exposed it.
    own_source = Path(__file__).read_text(encoding="utf-8")
    _, opened, guardrail = own_source.partition(
        "# The line against UNBOUNDED_ARGUMENTS"
    )
    assert opened, "the UNBOUNDED_ARGUMENTS guardrail comment has gone missing"
    guardrail, closed, _ = guardrail.partition("_PAYLOAD_SOURCE_ARGUMENTS = frozenset")
    assert closed, "_PAYLOAD_SOURCE_ARGUMENTS was renamed; re-anchor this check"
    assert "*which side* the named thing lives on" not in guardrail, (
        "the guardrail comment has returned to the side-of-the-filesystem rule "
        "that ADR 0044 retired; it answers `backup_name` wrongly."
    )
    assert "`backup_name`" in guardrail, (
        "the guardrail comment no longer names `backup_name`, the case that showed "
        "the rule and its application had diverged; see ADR 0044."
    )


def test_restore_vios_scope_and_backup_name_containment_are_independent(monkeypatch):
    """G15: SSP effect scope does not undo the backup-name containment guard.

    Live-HMC evidence for #282 established that an SSP restore is cluster-scoped,
    so one VIOS selector cannot bound every resource the operation affects. ADR
    0044 independently keeps ``backup_name`` bounded because it is a catalog name,
    and its validation remains required after the tool becomes non-exhaustive.

    `asyncssh.connect` is patched to raise rather than mocked to succeed: if the
    guard were removed, each call would fall through to the SSH layer, and this
    makes that a loud failure here instead of a socket attempt from the suite.
    """
    assert not TOOL_SECURITY["hmc_restore_vios"].exhaustive_targets
    assert "backup_name" not in UNBOUNDED_ARGUMENTS

    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")
    vios_uuid = "00000000-0000-0000-0000-000000000003"
    # A representative sample, not the full set and not one per refused route:
    # tests/vios/test_vios_backup.py owns exhaustive coverage of all four, and a
    # second copy of that list here would be two lists free to disagree — which is
    # the defect this whole change is about.
    escapes = ["../other/x.tar", "..", "-operation"]

    with patch(
        "hmcpctl.ssh.transport.asyncssh.connect",
        side_effect=AssertionError("reached the SSH layer"),
    ):
        for escape in escapes:
            with pytest.raises(ValueError, match="backup_name"):
                server_vios.hmc_restore_vios(
                    "system-name",
                    vios_uuid,
                    escape,
                    backup_type="ssp",
                    restart_if_required=False,
                )


def test_power_on_profile_is_not_a_separate_target_selector():
    """Profile containment is enforced under the declared LPAR target (ADR 0039).

    Runtime acceptance and rejection are exercised in tests/lpar/test_power.py;
    this check keeps the security inventory's selector contract explicit.
    """
    import inspect

    from hmcpctl.server_tools.lpar.lifecycle import hmc_power_on_lpar
    from hmcpctl.tool_registry import (
        REQUIRED_TARGET_ARGUMENTS,
        UNBOUNDED_ARGUMENTS,
    )

    parameters = inspect.signature(hmc_power_on_lpar).parameters
    assert "partition_profile_uuid" in parameters
    assert "partition_profile_uuid" not in REQUIRED_TARGET_ARGUMENTS
    assert "partition_profile_uuid" not in UNBOUNDED_ARGUMENTS
