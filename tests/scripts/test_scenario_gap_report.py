"""Tests for the ledger-to-scenario gap report (scripts/scenario_gap_report.py)."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts"
# The report imports the runner, which imports `live_test` and its sibling scripts
# as top-level modules, exactly as running a script from `scripts/` would.
sys.path.insert(0, str(SCRIPTS))
MODULE_SPEC = importlib.util.spec_from_file_location(
    "scenario_gap_report", SCRIPTS / "scenario_gap_report.py"
)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
report = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = report
MODULE_SPEC.loader.exec_module(report)

SCHEMAS = {
    "hmc_list_lpars": {"properties": {"system": {"type": "string"}}},
    "hmc_get_lpar": {
        "properties": {"lpar": {"type": "string"}, "system": {"type": "string"}},
        "required": ["lpar"],
    },
}
OPERATIONS = [
    {"operation": "lpar.list", "tool": "hmc_list_lpars", "row_ids": ["rest:a"]},
    {"operation": "lpar.get", "tool": "hmc_get_lpar", "row_ids": ["rest:a", "rest:b"]},
]
ROWS = ["rest:a", "rest:b", "cli:c"]
ROOTS = {("m", "s")}


def _report(source: str, schemas=SCHEMAS) -> list[str]:
    """The report for one synthetic module ``m`` whose registered scenario is ``s``."""
    scan = report.scan_source(source, "m.py", "m")
    return report.build_report([scan], ROOTS, OPERATIONS, ROWS, schemas)


def test_scan_reads_dispatches_operations_and_unreadable_sites():
    source = (
        "async def scenario(client, state, tool, extra):\n"
        '    await state.call(client, "hmc_list_lpars", system="s", expected=[X])\n'
        "    await state.call(client, tool)\n"
        '    await state.call(client, "hmc_get_lpar", **extra)\n'
        '    state.record_verified(1, "t", operation="lpar.get", scenario="st1-x")\n'
        "    state.record_verified(1, 't', operation=name)\n"
    )

    scan = report.scan_source(source, "m.py", "m")

    assert scan.dispatches == (
        report.Dispatch("m.py:2", "scenario", "hmc_list_lpars", ("system",)),
    )
    assert scan.verified == (report.Verified("m.py:5", "scenario", "lpar.get"),)
    assert scan.unreadable == (
        "m.py:3 dispatch with a non-literal tool or a ** splat",
        "m.py:4 dispatch with a non-literal tool or a ** splat",
        "m.py:6 record_verified without a literal operation",
    )


def test_dispatch_sites_carry_argument_nodes():
    """The one enumerator both the report and the argument guard consume.

    The guard type-checks the expression each keyword supplies, so a site carries
    the node and not only the name; runner keywords never reach the tool.
    """
    source = (
        "async def s(client, state, tool, extra):\n"
        '    await state.call(client, "hmc_list_lpars", system=config.x, expected=[X])\n'
        "    await state.call(client, tool)\n"
        '    await state.call(client, "hmc_get_lpar", **extra)\n'
        '    await state.call(client, "hmc_get_lpar", *extra)\n'
        '    await state.call(client, "hmc_get_lpar", "extra")\n'
        "    await state.call(client, *extra)\n"
        "if True:\n"
        '    state.call(client, "hmc_list_lpars")\n'
        'state.record_verified(1, "t", operation="lpar.get")\n'
    )

    literal, non_literal, splat, star, extra, star_tool, outside = (
        report.dispatch_sites(ast.parse(source))
    )

    assert (literal.lineno, literal.function, literal.tool) == (
        2,
        "s",
        "hmc_list_lpars",
    )
    ((name, node),) = literal.arguments
    assert name == "system"
    assert ast.unparse(node) == "config.x"
    assert literal.unreadable is None
    assert (non_literal.tool, non_literal.unreadable) == (
        None,
        "dispatch with a non-literal tool or a ** splat",
    )
    assert [name for name, _ in splat.arguments] == [None]
    assert splat.unreadable == "dispatch with a non-literal tool or a ** splat"
    positional = "dispatch with a * splat or extra positional arguments"
    assert star.unreadable == positional
    assert extra.unreadable == positional
    assert star_tool.unreadable == "dispatch with a non-literal tool or a ** splat"
    assert (outside.function, outside.unreadable) == (
        None,
        "call outside a top-level function",
    )


def test_unreadable_sites_keep_source_order_across_kinds():
    source = (
        "async def s(client, state, name, tool):\n"
        "    state.record_verified(1, 't', operation=name)\n"
        "    await state.call(client, tool)\n"
    )

    assert report.scan_source(source, "m.py", "m").unreadable == (
        "m.py:2 record_verified without a literal operation",
        "m.py:3 dispatch with a non-literal tool or a ** splat",
    )


def test_report_lists_uncovered_operations_and_rows():
    lines = _report(
        'async def s(client, state):\n    await state.call(client, "hmc_list_lpars")\n'
    )

    assert lines == [
        "uncovered-operation: lpar.get",
        "uncovered-row: cli:c",
        "uncovered-row: rest:b",
        (
            "summary: 1 dispatches; operations 1/2 exercised; rows 1/3 exercised; "
            "0 departed; 0 unregistered; 0 dispatch mismatches; 0 unreadable"
        ),
    ]


def test_record_verified_operation_counts_as_exercised():
    lines = _report(
        'def s(state):\n    state.record_verified(1, "t", operation="lpar.get")\n'
    )

    assert "uncovered-operation: lpar.get" not in lines
    assert "uncovered-row: rest:b" not in lines


def test_report_lists_operations_that_left_the_registry():
    source = (
        "async def s(client, state):\n"
        '    await state.call(client, "hmc_removed_tool")\n'
        '    state.record_verified(1, "t", operation="lpar.removed")\n'
    )

    lines = _report(source)

    assert "unregistered: m.py:2 tool hmc_removed_tool" in lines
    assert not [line for line in lines if line.startswith("dispatch-mismatch:")]
    assert "unregistered: m.py:3 operation lpar.removed" in lines


def test_a_served_tool_missing_from_the_ledger_is_unregistered():
    schemas = {**SCHEMAS, "hmc_new_tool": {"properties": {}}}
    source = (
        'async def s(client, state):\n    await state.call(client, "hmc_new_tool")\n'
    )

    assert "unregistered: m.py:2 tool hmc_new_tool" in _report(source, schemas)


def test_report_lists_unknown_and_missing_required_arguments():
    source = (
        "async def s(client, state):\n"
        '    await state.call(client, "hmc_get_lpar", system="s", bogus=1)\n'
    )

    lines = _report(source)

    assert [line for line in lines if line.startswith("dispatch-mismatch:")] == [
        "dispatch-mismatch: m.py:2 hmc_get_lpar: unknown argument bogus",
        "dispatch-mismatch: m.py:2 hmc_get_lpar: missing required argument lpar",
    ]


def test_report_lists_scenarios_that_left_the_registry():
    """A function no registered scenario reaches, directly or through a helper."""
    scenario = (
        "from . import helpers\n"
        "from .helpers import shared as reused\n"
        "async def s(client, state):\n"
        "    await helpers.listing(client, state)\n"
        "    await reused(client, state)\n"
        "    await local(client, state)\n"
        "async def local(client, state):\n"
        '    await state.call(client, "hmc_list_lpars")\n'
        "async def dropped(client, state):\n"
        '    await state.call(client, "hmc_get_lpar", lpar="x")\n'
        '    state.record_verified(1, "t", operation="lpar.get")\n'
    )
    helpers = (
        "async def listing(client, state):\n"
        '    await state.call(client, "hmc_list_lpars")\n'
        "async def shared(client, state):\n"
        '    await state.call(client, "hmc_list_lpars", system="s")\n'
        "async def orphan(client, state):\n"
        '    await state.call(client, "hmc_list_lpars")\n'
    )
    scans = [
        report.scan_source(scenario, "m.py", "m"),
        report.scan_source(helpers, "helpers.py", "helpers"),
    ]

    lines = report.build_report(scans, ROOTS, OPERATIONS, ROWS, SCHEMAS)

    assert [line for line in lines if line.startswith("departed:")] == [
        "departed: m.py:10 dropped dispatches hmc_get_lpar",
        "departed: m.py:11 dropped records lpar.get",
        "departed: helpers.py:6 orphan dispatches hmc_list_lpars",
    ]
    assert "uncovered-operation: lpar.get" in lines
    assert "uncovered-operation: lpar.list" not in lines
    assert report.exit_status(lines, fail_on_dispatch=True) == 0


def test_absolute_and_function_local_imports_still_reach_a_helper():
    scenario = (
        "from live_test.helpers import listing\n"
        "async def s(client, state):\n"
        "    from .helpers import shared\n"
        "    from live_test import helpers\n"
        "    await listing(client, state)\n"
        "    await shared(client, state)\n"
        "    await helpers.other(client, state)\n"
    )
    helpers = (
        "async def listing(client, state):\n"
        '    await state.call(client, "hmc_get_lpar", bogus=1)\n'
        "async def shared(client, state):\n"
        '    await state.call(client, "hmc_list_lpars")\n'
        "async def other(client, state):\n"
        '    await state.call(client, "hmc_list_lpars", system="s")\n'
    )
    scans = [
        report.scan_source(scenario, "m.py", "m"),
        report.scan_source(helpers, "helpers.py", "helpers"),
    ]

    lines = report.build_report(scans, ROOTS, OPERATIONS, ROWS, SCHEMAS)

    assert not [line for line in lines if line.startswith("departed:")]
    assert (
        "dispatch-mismatch: helpers.py:2 hmc_get_lpar: unknown argument bogus" in lines
    )


def test_module_aliases_and_plain_package_imports_still_reach_a_helper():
    scenario = (
        "import live_test.helpers\n"
        "import live_test.helpers as named\n"
        "async def _impl(client, state):\n"
        '    await state.call(client, "hmc_list_lpars")\n'
        "alias = _impl\n"
        "async def s(client, state):\n"
        "    await alias(client, state)\n"
        "    await live_test.helpers.listing(client, state)\n"
        "    await named.other(client, state)\n"
    )
    helpers = (
        "async def listing(client, state):\n"
        '    await state.call(client, "hmc_list_lpars")\n'
        "async def other(client, state):\n"
        '    await state.call(client, "hmc_list_lpars", system="s")\n'
    )
    scans = [
        report.scan_source(scenario, "m.py", "m"),
        report.scan_source(helpers, "helpers.py", "helpers"),
    ]

    lines = report.build_report(scans, ROOTS, OPERATIONS, ROWS, SCHEMAS)

    assert not [line for line in lines if line.startswith("departed:")]


def test_a_dispatch_outside_a_top_level_function_is_unreadable():
    source = (
        "if True:\n"
        "    async def s(client, state):\n"
        '        await state.call(client, "hmc_list_lpars")\n'
    )

    assert report.scan_source(source, "m.py", "m").unreadable == (
        "m.py:3 call outside a top-level function",
    )


def test_exit_status_fails_only_on_dispatch_findings_under_the_flag():
    clean = ["uncovered-row: cli:c", "departed: m.py:2 x", "summary: ..."]

    for prefix in ("unregistered:", "dispatch-mismatch:", "unreadable:"):
        failing = [*clean, f"{prefix} m.py:2 x"]
        assert report.exit_status(failing, fail_on_dispatch=False) == 0
        assert report.exit_status(failing, fail_on_dispatch=True) == 1
    assert report.exit_status(clean, fail_on_dispatch=True) == 0


def test_report_over_the_repository_is_clean_and_summarised(capsys):
    assert report.main(["--fail-on-dispatch"]) == 0

    out = capsys.readouterr().out.splitlines()
    # A scan that read nothing would also be clean; the floor is what says it read.
    summary = re.fullmatch(
        r"summary: (\d+) dispatches; operations (\d+)/\d+ exercised; .*", out[-1]
    )
    assert summary is not None, out[-1]
    dispatches, operations = summary.groups()
    assert int(dispatches) > 0
    assert int(operations) > 0
    assert not [line for line in out if line.startswith(report._DISPATCH_PREFIXES)]


def test_system_profiles_dispatch(capsys):
    assert report.main(["--fail-on-dispatch"]) == 0
    lines = capsys.readouterr().out.splitlines()
    for operation in ("create", "list", "update", "delete", "activate"):
        assert f"uncovered-operation: system_profile.{operation}" not in lines
    assert not any(
        line.startswith("unreadable: scripts/live_test/system_profiles.py")
        for line in lines
    )
