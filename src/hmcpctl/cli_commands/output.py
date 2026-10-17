"""Output formatting and terminal error handling for CLI commands."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from contextlib import redirect_stdout
from enum import Enum
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar

import typer
from rich.console import Console, RenderableType
from rich.style import StyleType
from rich.table import Table
from rich.text import Text, TextType
from typer import _click
from typer._click.globals import get_current_context

from ..resource_identity import ResourceNotFoundError

if TYPE_CHECKING:
    from ..operations.systems.fleet import FleetListing

# Emoji codes are off: no CLI string uses one, and an HMC value such as ``a:smile:b``
# must print as received (#1029).
console = Console(emoji=False)
err_console = Console(stderr=True, emoji=False)

_T = TypeVar("_T")


class VerbatimTable(Table):
    """A table whose string fields keep literal markup and visibly encode controls.

    Rich parses a plain ``str`` as console markup, so an HMC name holding ``[word]``
    would lose that segment; a ``Text`` is never parsed (#1029).
    """

    def __init__(
        self,
        *,
        title: TextType | None = None,
        caption: TextType | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Rich styles a str title/caption itself but renders a Text one as given, so the
        # Text carries the style Rich would have applied.
        self.title = _verbatim(title, self.title_style or "table.title")
        self.caption = _verbatim(caption, self.caption_style or "table.caption")

    def add_column(
        self, header: RenderableType = "", footer: RenderableType = "", **kwargs: Any
    ) -> None:
        super().add_column(_verbatim(header), _verbatim(footer), **kwargs)

    def add_row(
        self,
        *renderables: RenderableType | None,
        style: StyleType | None = None,
        end_section: bool = False,
    ) -> None:
        cells = (_verbatim(cell) for cell in renderables)
        super().add_row(*cells, style=style, end_section=end_section)


def _verbatim(value: _T, style: StyleType = "") -> _T | Text:
    return (
        Text(escape_controls(value), style=style) if isinstance(value, str) else value
    )


def escape_controls(value: str) -> str:
    """Keep printable Unicode and visibly encode nonprintable field characters."""
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in value)


def terminal_field(value: object, *, err: bool = False) -> str:
    """Encode one human field for its actual destination, independently of color settings."""
    text = str(value)
    return (
        escape_controls(text) if (sys.stderr if err else sys.stdout).isatty() else text
    )


def confirm_on_stderr(text: str, *, abort: bool = False) -> bool:
    """Keep input()'s final prompt character off the machine output stream."""
    with redirect_stdout(sys.stderr):
        return typer.confirm(text, abort=abort, err=True)


def print_raw(body: str) -> None:
    """Protect terminal display while retaining redirected response bytes (ADR 0229)."""
    typer.echo(terminal_field(body), color=True)


def print_json(data: Any) -> None:
    console.print_json(json.dumps(data, default=str), ensure_ascii=sys.stdout.isatty())


def _resource(entry: dict[str, Any]) -> dict[str, Any]:
    return entry.get("Resource") or {}


def first_field(entry: dict[str, Any], *names: str, default: str = "-") -> str:
    """Get the first present resource field as a string."""
    resource = _resource(entry)
    for name in names:
        value = resource.get(name)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict) and "text" in value:
            return str(value["text"])
    return default


def output(
    entries: Any,
    as_json: bool,
    table: VerbatimTable | None = None,
    empty_msg: str = "No results",
) -> None:
    if as_json:
        print_json(entries)
    elif table is not None:
        if table.row_count == 0:
            err_console.print(
                terminal_field(empty_msg, err=True), style="yellow", markup=False
            )
        else:
            console.print(table)
    elif not entries:
        err_console.print(
            terminal_field(empty_msg, err=True), style="yellow", markup=False
        )
    else:
        print_json(entries)


def report_unreadable(listing: FleetListing) -> None:
    """Name each managed system a fleet listing could not read (ADR 0197)."""
    for system in listing.unreadable_systems:
        err_console.print(
            f"Skipped managed system {terminal_field(system.system_name, err=True)} ({terminal_field(system.system_uuid, err=True)}): "
            f"State {terminal_field(system.state, err=True)}",
            style="yellow",
            markup=False,
            soft_wrap=True,
        )


def fail(exc: Exception, *, code: int = 1, guidance: str = "") -> NoReturn:
    """Report an exception and exit with the requested runtime-error code."""
    message = str(exc)
    if isinstance(exc, ResourceNotFoundError):
        commands = {
            "managed system": "systems list",
            "LPAR": "lpars list",
            "VIOS": "vios list",
        }
        if command := commands.get(exc.resource_kind):
            message = (
                f"No {exc.resource_kind} named {exc.selector!r} found. "
                f"Use hmcpctl {command} to inspect available resources."
            )
    err_console.print(
        Text.assemble(
            ("Error:", "red"), f" {terminal_field(message, err=True)}", guidance
        )
    )
    raise typer.Exit(code=code)


def usage_error(
    message: str, *, guidance: str = "", ctx: _click.Context | None = None
) -> NoReturn:
    """Report invalid command arguments using Typer's usage-error exit code."""
    ctx = ctx or get_current_context(silent=True)
    if ctx is not None:
        err_console.print(
            ctx.get_usage(), markup=False, highlight=False, soft_wrap=True
        )
        err_console.print(
            f"Try '{terminal_field(ctx.command_path, err=True)} --help' for help.",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    err_console.print(
        Text.assemble(
            ("Error:", "red"), f" {terminal_field(message, err=True)}", guidance
        )
    )
    raise typer.Exit(code=2)


def partition_not_found(value: str) -> NoReturn:
    """Report a failed partition lookup consistently across CLI domains."""
    err_console.print(
        f"Partition '{terminal_field(value, err=True)}' not found. "
        "Use hmcpctl lpars list to inspect available partitions.",
        style="yellow",
        markup=False,
    )
    raise typer.Exit(code=1)


class InventoryFormat(str, Enum):
    table = "table"
    tsv = "tsv"
    json = "json"


def resolve_inventory_format(
    as_json: bool, format: InventoryFormat | None
) -> InventoryFormat:
    """Select inventory output before performing backend I/O."""
    if as_json and format not in (None, InventoryFormat.json):
        usage_error("--json/-j conflicts with --format; select one output format")
    if as_json:
        return InventoryFormat.json
    if format is not None:
        return format
    return InventoryFormat.table if sys.stdout.isatty() else InventoryFormat.tsv


def inventory_field(entry: dict[str, Any], *names: str) -> Any:
    """Read an inventory resource field, retaining empty strings and numeric zero."""
    for name in names:
        value = _resource(entry).get(name)
        if value is not None:
            return value.get("text") if isinstance(value, dict) else value
    return None


def _inventory_cell(value: Any) -> str:
    return (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    )


def _tsv_cell(value: Any) -> str:
    if value is None:
        return r"\N"
    return (
        _inventory_cell(value)
        .replace("\\", "\\\\")
        .replace("\t", r"\t")
        .replace("\n", r"\n")
        .replace("\r", r"\r")
    )


def print_inventory(
    data: Any,
    format: InventoryFormat,
    columns: tuple[str, ...] = ("Field", "Value"),
    rows: Iterable[Sequence[Any]] | None = None,
    *,
    title: str | None = None,
    empty_msg: str = "No results",
) -> None:
    """Render raw inventory projections without routing machine output through Rich."""
    if format == InventoryFormat.json:
        typer.echo(
            json.dumps(data, ensure_ascii=sys.stdout.isatty(), default=str, indent=2)
        )
        return
    values = list((data or {}).items() if rows is None else rows)
    if format == InventoryFormat.tsv:
        typer.echo("\t".join(_tsv_cell(column) for column in columns))
        for row in values:
            typer.echo("\t".join(_tsv_cell(value) for value in row), color=True)
    else:
        table = VerbatimTable(title=title)
        for column in columns:
            table.add_column(column)
        for row in values:
            table.add_row(
                *("-" if value is None else _inventory_cell(value) for value in row)
            )
        console.print(table)
    if not values:
        err_console.print(
            terminal_field(empty_msg, err=True), style="yellow", markup=False
        )
