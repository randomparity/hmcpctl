"""Literal markup and encoded terminal fields at CLI boundaries -- #1029, #1275.

Rich parses every ``str`` it prints as console markup, so an HMC name, a raw REST body
or a CLI argument holding ``[word]`` loses that segment, and a crafted argument can
restyle a confirmation line. ``rich.markup.escape`` is no cure: it doubles a trailing
backslash that Rich then prints. Each command test below prints such a value and asserts
it survives; the AST guard keeps new print sites and tables from reintroducing the defect.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from rich.console import Console
from rich.table import Table
from typer.testing import CliRunner

import hmcpctl.cli_commands
from hmcpctl import cli
from hmcpctl.cli_commands import console as cli_console
from hmcpctl.cli_commands import raw as cli_raw
from hmcpctl.cli_commands.lpar import config as cli_lpars
from hmcpctl.cli_commands.lpar import inventory as cli_lpar_inventory
from hmcpctl.cli_commands.output import VerbatimTable
from hmcpctl.cli_commands.systems import core as cli_systems
from hmcpctl.cli_commands.systems import memory_pools as cli_memory_pools
from hmcpctl.cli_commands.virtualization import pcie as cli_pcie
from hmcpctl.operations.affinity.ssh import ResourceGroupAffinityResult
from hmcpctl.ssh.affinity import MemoptResourceGroupSelector

RUNNER = CliRunner()

CLI_COMMANDS = Path(hmcpctl.cli_commands.__file__).parent
CONSOLES = frozenset({"console", "err_console"})
# Console methods that parse a str argument as markup.
MARKUP_METHODS = frozenset({"print", "log", "rule", "input", "status"})
# Modules that may build a Rich console or table directly: the shared owner only.
RICH_OWNERS = frozenset({"output.py"})
# #1248 owns lpar/profiles.py's set-boot-order line, which still interpolates an
# escape()d value into markup; that form, and its rich.markup import, are tolerated
# there and nowhere else.
ESCAPE_TOLERATED = frozenset({"lpar/profiles.py"})


def _is_call_to(node: ast.AST, names: frozenset[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in names
    )


def _is_text(node: ast.AST) -> bool:
    """``Text(...)`` or ``Text.assemble(...)``: renderables Rich never parses."""
    if _is_call_to(node, frozenset({"Text"})):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "assemble"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "Text"
    )


def _table_names(function: ast.AST | None) -> set[str]:
    """Names *function* binds exactly once, to ``VerbatimTable(...)`` or by annotation.

    Any second binding -- a loop, ``with``, ``except``, comprehension or tuple target,
    or another assignment -- makes the name untrusted.
    """
    if function is None:
        return set()
    bindings: dict[str, list[bool]] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bindings.setdefault(node.id, []).append(False)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bindings.setdefault(node.name, []).append(False)
        elif isinstance(node, ast.arg):
            annotated = node.annotation is not None and "VerbatimTable" in ast.unparse(
                node.annotation
            )
            bindings.setdefault(node.arg, []).append(annotated)
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _is_call_to(node.value, frozenset({"VerbatimTable"}))
        ):
            bindings[node.targets[0].id] = [True] * len(bindings[node.targets[0].id])
    return {name for name, kinds in bindings.items() if kinds == [True]}


def _markup_safe(node: ast.AST, tables: set[str], tolerate_escape: bool) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.JoinedStr):
        return all(
            tolerate_escape and _is_call_to(part.value, frozenset({"escape"}))
            for part in node.values
            if isinstance(part, ast.FormattedValue)
        )
    if isinstance(node, ast.IfExp):
        return all(
            _markup_safe(branch, tables, tolerate_escape)
            for branch in (node.body, node.orelse)
        )
    if isinstance(node, ast.BoolOp):
        return all(_markup_safe(v, tables, tolerate_escape) for v in node.values)
    if isinstance(node, ast.Name):
        return node.id in tables
    return _is_text(node)


def _passes_markup_false(call: ast.Call) -> bool:
    return any(
        keyword.arg == "markup"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def _is_console_print(node: ast.AST) -> bool:
    """A markup-parsing console call, also reached through a module (``output.console``)."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in MARKUP_METHODS
    ):
        return False
    receiver = node.func.value
    if isinstance(receiver, ast.Attribute):
        return receiver.attr in CONSOLES
    return isinstance(receiver, ast.Name) and receiver.id in CONSOLES


def _imports_rich_directly(node: ast.AST, allowed: frozenset[str]) -> bool:
    """A ``rich`` import outside *allowed*, or ``output``'s plain ``Table`` re-imported."""
    if isinstance(node, ast.Import):
        modules = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module:
        if node.module.endswith("output") and any(
            alias.name == "Table" or (alias.name in CONSOLES and alias.asname)
            for alias in node.names
        ):
            return True
        modules = [node.module]
    else:
        return False
    return any(
        (module == "rich" or module.startswith("rich.")) and module not in allowed
        for module in modules
    )


def _parses_markup(node: ast.AST) -> bool:
    """``Text.from_markup(...)`` or ``rich.markup.render(...)``: markup by another door."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("from_markup", "render")
    )


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, ast.AST | None]:
    """Map every node to its innermost enclosing function (``None`` at module level)."""
    owner: dict[ast.AST, ast.AST | None] = {tree: None}
    for parent in ast.walk(tree):
        scope = (
            parent
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
            else owner[parent]
        )
        for child in ast.iter_child_nodes(parent):
            owner[child] = scope
    return owner


def markup_violations(source: str, filename: str) -> list[str]:
    """Return ``file:line`` entries for print sites and imports that break the rule.

    A ``console``/``err_console`` ``print`` call anywhere in the module must pass
    ``markup=False`` or print only literals, ``Text`` renderables and names its function
    binds exactly once to ``VerbatimTable``. No module calls ``from_markup`` or
    ``render``, and only ``output.py`` may import ``output.Table`` or from ``rich``
    beyond ``rich.text``; ``output``'s consoles keep their names. Binding
    ``console.print`` to another name, rebinding a table name by import, ``match``,
    ``def`` or ``global``, and assigning ``table.title`` later are not detected.
    """
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    tolerate_escape = filename in ESCAPE_TOLERATED
    found: list[str] = []
    for node in ast.walk(tree):
        if _parses_markup(node):
            found.append(f"{filename}:{node.lineno}: parses markup")
        if _is_console_print(node) and not _passes_markup_false(node):
            tables = _table_names(owner[node])
            if not all(_markup_safe(arg, tables, tolerate_escape) for arg in node.args):
                found.append(f"{filename}:{node.lineno}: interpolated console.print")
    allowed = frozenset({"rich.text"} | ({"rich.markup"} if tolerate_escape else set()))
    if Path(filename).name not in RICH_OWNERS:
        for node in ast.walk(tree):
            if _imports_rich_directly(node, allowed):
                found.append(f"{filename}:{node.lineno}: imports rich directly")
    return sorted(set(found))


def _terminal_safe(node: ast.AST, tables: set[str], err: bool | None) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return node.id in tables
    if isinstance(node, ast.JoinedStr):
        return all(
            (
                _terminal_safe(part.value, tables, err)
                and (
                    part.format_spec is None
                    or _terminal_safe(part.format_spec, tables, err)
                )
            )
            for part in node.values
            if isinstance(part, ast.FormattedValue)
        )
    if isinstance(node, ast.IfExp):
        return all(
            _terminal_safe(part, tables, err) for part in (node.body, node.orelse)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return all(
            _terminal_safe(part, tables, err) for part in (node.left, node.right)
        )
    if _is_call_to(node, frozenset({"escape_controls"})):
        return True
    if _is_call_to(node, frozenset({"terminal_field"})):
        destination = next(
            (k.value for k in node.keywords if k.arg == "err"), ast.Constant(False)
        )
        return (
            err is not None
            and isinstance(destination, ast.Constant)
            and destination.value is err
        )
    if _is_text(node):
        return all(
            _terminal_safe(
                arg.elts[0] if isinstance(arg, ast.Tuple) else arg, tables, err
            )
            for arg in node.args
        )
    if _is_call_to(node, frozenset({"escape"})):
        return all(_terminal_safe(arg, tables, err) for arg in node.args)
    return False


def terminal_violations(source: str, filename: str) -> list[str]:
    """Guard known text sink forms; output.py owns byte/JSON exceptions and is tested directly."""
    if filename == "output.py":
        return []
    tree = ast.parse(source)
    owners = _enclosing_functions(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if target in (
            "typer.echo",
            "typer.secho",
            "sys.stdout.write",
            "sys.stderr.write",
            "console.print_json",
            "err_console.print_json",
            "print",
        ):
            found.append(f"{filename}:{node.lineno}: unowned text output")
        elif _is_console_print(node) or target in (
            "typer.confirm",
            "confirm_on_stderr",
            "typer.prompt",
            "typer.BadParameter",
        ):
            err = (
                "err_console" in target.split(".")[:-1]
                or target in ("typer.BadParameter", "confirm_on_stderr")
                or any(
                    k.arg == "err"
                    and isinstance(k.value, ast.Constant)
                    and k.value.value is True
                    for k in node.keywords
                )
            )
            tables = _table_names(owners[node])
            if not all(_terminal_safe(arg, tables, err) for arg in node.args):
                found.append(f"{filename}:{node.lineno}: unencoded terminal field")
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ("add_row", "add_column")
            and any(
                _is_text(arg) and not _terminal_safe(arg, set(), None)
                for arg in node.args
            )
        ):
            found.append(f"{filename}:{node.lineno}: unencoded styled table field")
    return found


def test_cli_commands_print_external_text_verbatim():
    violations = [
        violation
        for path in sorted(CLI_COMMANDS.rglob("*.py"))
        for guard in (markup_violations, terminal_violations)
        for violation in guard(
            path.read_text(encoding="utf-8"),
            path.relative_to(CLI_COMMANDS).as_posix(),
        )
    ]
    assert violations == [], (
        "Rich parses these as markup. Print the value with markup=False (style= for "
        "colour), wrap mixed styling in Text.assemble, or build the table with "
        "output.VerbatimTable; rich.markup.escape is lossy (#1029):\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize(
    ("snippet", "flagged"),
    (
        ('def f():\n    console.print(f"[green]Created {name}[/green]")', True),
        ('def f():\n    console.print(f"[green]{escape(name)}[/green]")', True),
        ("def f():\n    console.print(body)", True),
        ('console.print(f"{name}")', True),
        (
            "def f():\n    t = VerbatimTable()\n    for t in x:\n        console.print(t)",
            True,
        ),
        (
            "def f():\n    t = VerbatimTable()\n    (t,) = (x,)\n    console.print(t)",
            True,
        ),
        ("def f():\n    t = VerbatimTable()\n    [console.print(t) for t in xs]", True),
        ("def f(t: VerbatimTable):\n    t = x\n    console.print(t)", True),
        ('def f():\n    output.console.print(f"{x}")', True),
        ("from rich import print", True),
        ("from .output import console as c", True),
        ('def f():\n    console.rule(f"{name}")', True),
        ('def f():\n    err_console.log(f"{name}")', True),
        ("from rich.markup import escape", True),
        ("from .output import Table", True),
        ("def f(t):\n    t.add_row(Text.from_markup(name))", True),
        ("from rich.panel import Panel", True),
        ("from rich.table import Table", True),
        ("import rich.console", True),
        ("import rich", True),
        ("from rich import table", True),
        ('async def f():\n    err_console.print(f"{name}")', True),
        ('def f():\n    console.print(f"Created {name}", markup=False)', False),
        ('def f():\n    console.print(Text.assemble(("a", "red"), name))', False),
        ("def f():\n    t = VerbatimTable()\n    console.print(t)", False),
        ('def f():\n    console.print("[green]literal[/green]")', False),
    ),
)
def test_markup_guard_flags_interpolated_sites(snippet, flagged):
    assert bool(markup_violations(snippet, "probe.py")) is flagged


def test_markup_guard_tolerates_escape_only_where_1248_owns_the_line():
    snippet = 'def f():\n    console.print(f"[green]{escape(name)}[/green]")'
    assert markup_violations(snippet, "lpar/profiles.py") == []


def test_verbatim_table_title_and_caption_keep_rich_default_styles():
    def render(table_class):
        table = table_class(title="Title", caption="Caption")
        table.add_column("h")
        table.add_row("c")
        recorder = Console(force_terminal=True, color_system="truecolor", width=40)
        with recorder.capture() as capture:
            recorder.print(table)
        return capture.get()

    assert render(VerbatimTable) == render(Table)


def test_verbatim_table_renders_title_header_and_cells_as_given():
    table = VerbatimTable(title="t[bold]x[/bold]", caption="cap[bold]x")
    table.add_column("h[bold]x")
    table.add_row("c[bold]x[/bold] :smile:")
    recorder = Console(record=True, width=80)
    recorder.print(table)

    text = recorder.export_text()
    for expected in (
        "t[bold]x[/bold]",
        "cap[bold]x",
        "h[bold]x",
        "c[bold]x[/bold] :smile:",
    ):
        assert expected in text


# A value Rich would read as a bold tag pair and an emoji code if it parsed it, ending in
# the backslash that rich.markup.escape doubles.
MARKED = "x[bold]y[/bold]:smile:\\"
# A raw body Rich would reflow and click would strip from a pipe: longer than 80
# columns, with a tab and an ANSI colour code.
# MARKED exactly, not followed by the extra backslash escape() would have added.
PRINTED_VERBATIM = re.compile(re.escape(MARKED) + r"(?!\\)")
RAW_BODY = f"<V>{MARKED}\t\x1b[31m{'word ' * 30}</V>"


def _stub(monkeypatch, module, value, *names):
    """Make *module*'s client/SSH runners return *value* without any I/O."""
    for name in names:
        monkeypatch.setattr(module, name, lambda *_args, **_kwargs: value)
    monkeypatch.setattr(module, "ssh_config", lambda: None, raising=False)


def _rg_result(mode):
    return ResourceGroupAffinityResult(
        capability="available",
        mode=mode,
        system="sys1",
        selector=MemoptResourceGroupSelector(all=True),
        items=[
            {
                "resource_group_name": MARKED,
                "resource_group_id": 3,
                "curr_score": 90,
                "predicted_score": 95,
            }
        ],
        unavailable_reason=None,
    )


@pytest.mark.parametrize(
    ("module", "value", "runners", "argv"),
    (
        (
            cli_lpars,
            _rg_result("current"),
            ("with_client",),
            ["lpars", "resource-group-memopt-scores", "sys1", "--all"],
        ),
        (
            cli_lpars,
            _rg_result("calculated"),
            ("with_client",),
            ["lpars", "plan-resource-group-memopt-scores", "sys1", "--all"],
        ),
        (
            cli_console,
            {"link": MARKED, "Resource": {"ManagementConsoleName": MARKED}},
            ("with_client",),
            ["console", "info"],
        ),
        (cli_lpar_inventory, MARKED, ("with_client",), ["lpars", "state", "lp1"]),
        (
            cli_lpars,
            "",
            ("with_client",),
            ["lpars", "set-description", MARKED, "sys1", "d", "--yes"],
        ),
        (
            cli_lpars,
            "",
            ("with_client",),
            ["lpars", "set-msp", MARKED, "sys1", "true", "--yes"],
        ),
        (
            cli_lpars,
            "prof1",
            ("run_cli_coroutine",),
            ["lpars", "set-proc-compat", MARKED, "sys1", "POWER10", "--yes"],
        ),
        (
            cli_pcie,
            "",
            ("with_client",),
            ["network", "set-sriov-mode", "sys1", MARKED, "sriov"],
        ),
        (
            cli_memory_pools,
            "",
            ("run_cli_coroutine",),
            ["memory-pools", "remove", MARKED, "--yes"],
        ),
    ),
)
def test_issue_listed_sites_print_hmc_and_argument_text_verbatim(
    monkeypatch, module, value, runners, argv
):
    _stub(monkeypatch, module, value, *runners)

    result = RUNNER.invoke(cli.app, argv)

    assert result.exit_code == 0, result.output
    assert PRINTED_VERBATIM.search(result.stdout), result.stdout


def test_systems_list_table_prints_bracketed_system_name_verbatim(monkeypatch):
    system = {"UUID": "u1", "Resource": {"SystemName": MARKED, "State": "operating"}}
    _stub(monkeypatch, cli_systems, [system], "with_client")

    result = RUNNER.invoke(cli.app, ["systems", "list", "--format", "table"])

    assert result.exit_code == 0, result.output
    assert PRINTED_VERBATIM.search(result.stdout), result.stdout


@pytest.mark.parametrize(
    ("value", "argv"),
    (
        ((RAW_BODY, {}), ["raw", "get", "/p"]),
        (RAW_BODY, ["raw", "post", "/p", "<x/>", "--yes"]),
    ),
)
def test_raw_commands_print_the_body_as_received(monkeypatch, value, argv):
    _stub(monkeypatch, cli_raw, value, "with_client")

    result = RUNNER.invoke(cli.app, argv)

    assert result.exit_code == 0, result.output
    assert result.stdout == RAW_BODY + "\n"


@pytest.mark.parametrize(
    "source",
    [
        "console.print(name, markup=False)",
        'err_console.print(Text.assemble(("Error", "red"), name))',
        'typer.confirm(f"Delete {name}?")',
        'confirm_on_stderr(f"Delete {name}?")',
        "confirm_on_stderr(terminal_field(name))",
        "typer.echo(body, color=True)",
        "console.print_json(json.dumps(data))",
        'console.print(f"Value: {name}", markup=False)',
        "err_console.print(terminal_field(name), markup=False)",
        'table.add_row(Text(name, style="red"))',
    ],
)
def test_terminal_guard_rejects_unencoded_fields(source):
    assert terminal_violations(source, "example.py")


@pytest.mark.parametrize(
    "source",
    [
        'console.print(f"Value: {terminal_field(name)}", markup=False)',
        "err_console.print(terminal_field(name, err=True), markup=False)",
        'typer.confirm(f"Delete {terminal_field(name)}?")',
        'confirm_on_stderr(f"Delete {terminal_field(name, err=True)}?")',
        'console.print(Text.assemble(("Error", "red"), terminal_field(name)))',
        'console.print("[green]Done[/green]")',
        'table.add_row(Text(escape_controls(name), style="red"))',
    ],
)
def test_terminal_guard_accepts_encoded_fields(source):
    assert terminal_violations(source, "example.py") == []
