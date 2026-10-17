"""Root Typer application and command-group composition for the CLI.

The per-domain command modules expose ``register_commands(group)`` functions.
``cli.py`` is the single composition entry point: it imports those modules and
registers every command on the groups defined here before exposing the root app.
"""

from __future__ import annotations

from typing import Any, NoReturn

import typer
from typer import _click
from typer._click.core import ParameterSource
from typer.core import TyperCommand, TyperGroup

from .capabilities import capabilities
from .output import usage_error
from .runtime import GlobalOpts
from .serve import serve


class CLICommand(TyperCommand):
    """Enrich only known missing-input errors using the installed parser."""

    def parse_args(self, ctx: _click.Context, args: list[str]) -> list[str]:
        original = list(args)
        try:
            return super().parse_args(ctx, args)
        except _click.exceptions.MissingParameter as exc:
            opts, _, _ = self.make_parser(ctx).parse_args(original)
            missing = []
            for param in self.get_params(ctx):
                if not param.required:
                    continue
                value = opts.get(param.name)
                if value is None:
                    value = param.value_from_envvar(ctx)
                if value is None:
                    value = param.get_default(ctx, call=False)
                if not callable(value) and param.value_is_missing(value):
                    missing.append(param.get_error_hint(ctx))
            if not missing:
                raise
            raise _click.exceptions.UsageError(
                "Missing required inputs: " + ", ".join(missing), ctx=ctx
            ) from exc


class CLIGroup(TyperGroup):
    """Keep Typer parsing and suggestions, with the CLI's stderr renderer."""

    def parse_args(self, ctx: _click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except _click.exceptions.UsageError as exc:
            self.report_usage(exc)

    def invoke(self, ctx: _click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except _click.exceptions.UsageError as exc:
            self.report_usage(exc)

    @staticmethod
    def report_usage(exc: _click.exceptions.UsageError) -> NoReturn:
        if isinstance(exc, _click.exceptions.NoArgsIsHelpError):
            raise exc
        guidance = ""
        if (
            isinstance(exc, _click.exceptions.NoSuchOption)
            and exc.option_name == "--profile"
        ):
            guidance = "\nPlace root options before the group: hmcpctl --profile NAME GROUP COMMAND"
        usage_error(exc.format_message(), guidance=guidance, ctx=exc.ctx)


app = typer.Typer(
    name="hmcpctl",
    cls=CLIGroup,
    help="IBM HMC (Hardware Management Console) MCP server and CLI.",
    no_args_is_help=True,
)

systems_app = typer.Typer(help="Managed systems (Power servers).", no_args_is_help=True)
lpars_app = typer.Typer(help="Logical partitions (LPARs).", no_args_is_help=True)
adapters_app = typer.Typer(
    help="Virtual adapters (network/storage) on LPARs.", no_args_is_help=True
)
storage_app = typer.Typer(
    help="VIOS storage: volume groups, virtual disks, mappings.", no_args_is_help=True
)
cluster_app = typer.Typer(
    help="Clusters / Shared Storage Pools (logical units).", no_args_is_help=True
)
metrics_app = typer.Typer(
    help="PCM performance/capacity metrics.", no_args_is_help=True
)
network_app = typer.Typer(
    help="Virtual networks / switches / bridges.", no_args_is_help=True
)
templates_app = typer.Typer(
    help="Template library (partition templates).", no_args_is_help=True
)
vios_app = typer.Typer(help="Virtual I/O Servers.", no_args_is_help=True)
console_app = typer.Typer(help="The HMC itself.", no_args_is_help=True)
jobs_app = typer.Typer(help="HMC jobs.", no_args_is_help=True)
raw_app = typer.Typer(help="Raw REST escape hatch.", no_args_is_help=True)
snapshot_app = typer.Typer(help="Portable LPAR snapshots.", no_args_is_help=True)

app.add_typer(systems_app, name="systems")
app.add_typer(lpars_app, name="lpars")
app.add_typer(adapters_app, name="adapters")
app.add_typer(storage_app, name="storage")
app.add_typer(cluster_app, name="cluster")
app.add_typer(metrics_app, name="metrics")
app.add_typer(network_app, name="network")
app.add_typer(templates_app, name="templates")
app.add_typer(vios_app, name="vios")
app.add_typer(console_app, name="console")
app.add_typer(jobs_app, name="jobs")
app.add_typer(raw_app, name="raw")
app.add_typer(snapshot_app, name="snapshot")

memory_pools_app = typer.Typer(help="Shared memory pools.", no_args_is_help=True)
app.add_typer(memory_pools_app, name="memory-pools")

report_app = typer.Typer(
    help="Reports surveyed across every configured connection profile.",
    no_args_is_help=True,
)
app.add_typer(report_app, name="report")

config_app = typer.Typer(
    # Not "profile configuration": this group writes two different files. Naming
    # only profiles here would assert the conflation the docs work to refuse, in
    # the surface an operator reaches straight from `serve`'s refusal.
    help="Connection profiles (config.toml) and server access policies "
    "(access-policy.toml).",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")


@app.callback()
def main(
    ctx: typer.Context,
    host: str | None = typer.Option(
        None, "--host", envvar="HMC_HOST", help="HMC hostname or IP"
    ),
    user: str | None = typer.Option(
        None, "--user", "-u", envvar="HMC_USER", help="HMC user"
    ),
    password: str | None = typer.Option(
        None,
        "--password",
        "-p",
        envvar="HMC_PASSWORD",
        help="HMC password",
        hide_input=True,
    ),
    verify_ssl: bool | None = typer.Option(
        None,
        "--verify-ssl/--no-verify-ssl",
        envvar="HMC_VERIFY_SSL",
        help="Verify the HMC TLS certificate",
    ),
    trust_system_store: bool | None = typer.Option(
        None,
        "--trust-system-store/--no-trust-system-store",
        envvar="HMC_TRUST_SYSTEM_STORE",
        help=(
            "When --verify-ssl is on, trust the OS certificate store via truststore. "
            "An explicit SSL_CERT_FILE takes precedence. On by default."
        ),
    ),
    ca_bundle: str | None = typer.Option(
        None,
        "--ca-bundle",
        envvar="HMC_CA_BUNDLE",
        help=(
            "Path to a PEM file with the CA certificate(s) to trust for the HMC TLS "
            "connection. When set and --verify-ssl is on, takes precedence over "
            "SSL_CERT_FILE and --trust-system-store."
        ),
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        envvar="HMC_PROFILE",
        help="Named profile from ~/.config/hmcpctl/config.toml (or platform equivalent)",
    ),
) -> None:
    option_names = (
        "host",
        "user",
        "password",
        "verify_ssl",
        "trust_system_store",
        "ca_bundle",
        "profile",
    )
    command_line_options = frozenset(
        name
        for name in option_names
        if ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE
    )
    ctx.obj = GlobalOpts(
        host=host,
        user=user,
        password=password,
        verify_ssl=verify_ssl,
        trust_system_store=trust_system_store,
        ca_bundle=ca_bundle,
        profile=profile,
        command_line_options=command_line_options,
    )


app.command()(capabilities)
app.command()(serve)
