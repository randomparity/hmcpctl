"""hmcpctl command line interface.

Usage examples:
    hmcpctl capabilities                 # show operation maturity
    hmcpctl serve --access-policy NAME # run the MCP server over stdio
    hmcpctl systems list              # list managed systems as a table
    hmcpctl lpars list --json         # list LPARs as JSON
    hmcpctl lpars show mylpar         # find an LPAR by name and show it
    hmcpctl console info              # HMC version / connectivity check

This module is the explicit composition root: ``cli_commands.app`` owns command
groups, while domain modules expose registration functions called here to build
the complete tree.
"""

from __future__ import annotations

from types import ModuleType

from .cli_commands import (
    config,
    jobs,
    metrics,
    raw,
    report,
    snapshot,
    templates,
)
from .cli_commands import (
    console as console_commands,
)
from .cli_commands.app import (
    CLICommand,
    adapters_app,
    cluster_app,
    config_app,
    console_app,
    jobs_app,
    lpars_app,
    memory_pools_app,
    metrics_app,
    network_app,
    raw_app,
    report_app,
    snapshot_app,
    storage_app,
    systems_app,
    templates_app,
    vios_app,
)
from .cli_commands.app import (
    app as app,  # noqa: PLC0414 - PEP 484 explicit re-export; see the module docstring
)
from .cli_commands.app import (
    main as main,  # noqa: PLC0414 - PEP 484 explicit re-export; see the module docstring
)
from .cli_commands.lpar import (
    config as lpar_config,
)
from .cli_commands.lpar import (
    console as lpar_console,
)
from .cli_commands.lpar import (
    create as lpar_create,
)
from .cli_commands.lpar import (
    decommission as lpar_decommission,
)
from .cli_commands.lpar import (
    inventory as lpar_inventory,
)
from .cli_commands.lpar import (
    lifecycle as lpar_lifecycle,
)
from .cli_commands.lpar import (
    migration as lpar_migration,
)
from .cli_commands.lpar import (
    modify as lpar_modify,
)
from .cli_commands.lpar import (
    profiles as lpar_profiles,
)
from .cli_commands.lpar import (
    provision as lpar_provision,
)
from .cli_commands.output import (
    console as console,  # noqa: PLC0414 - PEP 484 explicit re-export; see the module docstring
)
from .cli_commands.runtime import (
    GlobalOpts as GlobalOpts,  # noqa: PLC0414 - PEP 484 explicit re-export; see the module docstring
)
from .cli_commands.storage import cluster
from .cli_commands.storage import resources as storage
from .cli_commands.systems import core as systems
from .cli_commands.systems import memory_pools
from .cli_commands.vios import core as vios
from .cli_commands.vios import labels as vios_labels
from .cli_commands.virtualization import adapters, network, pcie, vnic


def _register_commands() -> None:
    """Compose the complete command tree from explicit domain registrations."""
    registrations = (
        (adapters, adapters_app),
        (cluster, cluster_app),
        (config, config_app),
        (console_commands, console_app),
        (jobs, jobs_app),
        (lpar_inventory, lpars_app),
        (lpar_lifecycle, lpars_app),
        (lpar_console, lpars_app),
        (lpar_create, lpars_app),
        (lpar_decommission, lpars_app),
        (lpar_migration, lpars_app),
        (lpar_config, lpars_app),
        (lpar_modify, lpars_app),
        (lpar_profiles, lpars_app),
        (lpar_provision, lpars_app),
        (memory_pools, memory_pools_app),
        (metrics, metrics_app),
        (network, network_app),
        (pcie, network_app),
        (raw, raw_app),
        (report, report_app),
        (snapshot, snapshot_app),
        (storage, storage_app),
        (systems, systems_app),
        (templates, templates_app),
        (vnic, network_app),
        (vios, vios_app),
        (vios_labels, vios_app),
    )
    lpar_panels: dict[ModuleType, str] = {
        lpar_inventory: "Inventory",
        lpar_lifecycle: "Lifecycle",
        lpar_create: "Lifecycle",
        lpar_decommission: "Lifecycle",
        lpar_provision: "Lifecycle",
        lpar_console: "Console",
        lpar_migration: "Migration",
        lpar_config: "Configuration",
        lpar_modify: "Configuration",
        lpar_profiles: "Configuration",
    }
    for module, group in registrations:
        start = len(group.registered_commands)
        module.register_commands(group)
        for command in group.registered_commands[start:]:
            command.cls = CLICommand
            if group is lpars_app:
                command.rich_help_panel = lpar_panels[module]
            elif group is storage_app:
                name = command.name or ""
                if name.startswith(("list-", "get-")):
                    command.rich_help_panel = "Inventory"
                elif name in ("map", "detach-mapping"):
                    command.rich_help_panel = "Mappings"
                elif "media" in name or name == "upload-iso":
                    command.rich_help_panel = "Optical media"
                else:
                    command.rich_help_panel = "Disks"
    for command in app.registered_commands:
        command.cls = CLICommand
    for group in app.registered_groups:
        if group.typer_instance is not None:
            group.typer_instance.info.epilog = (
                "Place connection options before the group: "
                f"hmcpctl --profile NAME {group.name} COMMAND. "
                "Use hmcpctl --help for root connection options."
            )


_register_commands()
