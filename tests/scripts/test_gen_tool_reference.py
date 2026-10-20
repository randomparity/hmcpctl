"""Tests for the tool-reference generator (scripts/gen_tool_reference.py).

The diff gate proves the committed copy is *current*, never that it is *right* --
both sides of that comparison come from this generator. So the raise paths and the
rendering contract are tested here, against synthetic registries, and the gate is
left to prove only what it can.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hmcpctl.operation_maturity as maturity
from hmcpctl.server import TOOL_SECURITY
from hmcpctl.tool_registry import ToolSecurity

ROOT = Path(__file__).parents[2]
MODULE_PATH = ROOT / "scripts" / "gen_tool_reference.py"
MODULE_SPEC = importlib.util.spec_from_file_location("gen_tool_reference", MODULE_PATH)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
gen_tool_reference = importlib.util.module_from_spec(MODULE_SPEC)
# Registered before execution because @dataclass resolves its own module out of
# sys.modules while the class body runs; without this the import raises.
sys.modules[MODULE_SPEC.name] = gen_tool_reference
MODULE_SPEC.loader.exec_module(gen_tool_reference)

COMMITTED = ROOT / "docs" / "tools"
# Relative, so it opens the tool reference of the revision being read (ADR 0251).
README_POINTER = "](docs/tools/index.md)"

SECURITY = {
    "hmc_alpha": ToolSecurity(
        effect="read", operation="alpha.list", target_kind="lpar"
    ),
    "hmc_beta": ToolSecurity(
        effect="destructive", operation="beta.delete", target_kind="console"
    ),
}
DESCRIPTIONS = {
    "hmc_alpha": "List alphas.\n\nArgs:\n    profile: TOML profile name.\n",
    "hmc_beta": "Delete one beta.\n",
}


def _records(**overrides):
    """Records built from the synthetic registry, with *overrides* applied."""
    descriptions = {**DESCRIPTIONS, **overrides.pop("descriptions", {})}
    security = {**SECURITY, **overrides.pop("security", {})}
    exposed = overrides.pop("exposed", lambda name: True)
    assert not overrides
    return gen_tool_reference.build_records(descriptions, security, exposed)


def _install_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: object
) -> None:
    (tmp_path / "_operation_maturity.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    monkeypatch.setattr(maturity.resources, "files", lambda _package: tmp_path)


def test_records_carry_registry_metadata_and_the_docstring_summary() -> None:
    alpha, beta = _records()

    assert (alpha.name, alpha.effect, alpha.operation, alpha.target_kind) == (
        "hmc_alpha",
        "read",
        "alpha.list",
        "lpar",
    )
    # First line only: every handler in this package has a multiline docstring, so
    # the Args block must not reach the table cell.
    assert alpha.summary == "List alphas."
    assert beta.domain == "beta"


def test_records_join_shared_maturity_and_render_sparse_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_projection(
        tmp_path,
        monkeypatch,
        {
            "format_version": 1,
            "runtime_eligibility": "existing-runtime-guards",
            "operations": [
                {
                    "operation": "alpha.list",
                    "implementation": "partial",
                    "verification": "stale",
                    "reason": "closure-changed",
                    "observed_at": "2026-09-01T12:00:00Z",
                }
            ],
        },
    )

    alpha, beta = _records()

    assert (
        alpha.implementation,
        alpha.verification,
        alpha.verification_reason,
        alpha.runtime_eligibility,
    ) == ("partial", "stale", "closure-changed", "existing-runtime-guards")
    assert (
        beta.implementation,
        beta.verification,
        beta.verification_reason,
        beta.runtime_eligibility,
    ) == ("unrecorded", "unrecorded", None, "existing-runtime-guards")

    pages = gen_tool_reference.render_pages((alpha, beta))
    assert (
        "| Tool | Effect | Operation | Target | Implementation | Verification | "
        "Runtime eligibility | Summary |" in pages["alpha.md"]
    )
    assert (
        "| `partial` | `stale (closure-changed)` | `existing-runtime-guards` |"
        in (pages["alpha.md"])
    )
    assert (
        "| `unrecorded` | `unrecorded` | `existing-runtime-guards` |"
        in (pages["beta.md"])
    )
    assert "../capabilities/README.md" in pages["index.md"]
    for value in ("absent", "partial", "implemented"):
        assert f"`{value}`" in pages["index.md"]
    for value in ("unevidenced", "current", "stale", "failed"):
        assert f"`{value}`" in pages["index.md"]
    assert "`unrecorded`" in pages["index.md"]
    assert "`existing-runtime-guards`" in pages["index.md"]


def test_rendered_verification_is_identical_more_than_90_days_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_projection(
        tmp_path,
        monkeypatch,
        {
            "format_version": 1,
            "runtime_eligibility": "existing-runtime-guards",
            "operations": [
                {
                    "operation": "alpha.list",
                    "implementation": "implemented",
                    "verification": "current",
                    "reason": None,
                    "observed_at": "2026-09-01T12:00:00Z",
                }
            ],
        },
    )
    rendered = []
    for reference_date in (
        datetime(2026, 9, 2, tzinfo=UTC),
        datetime(2027, 9, 2, tzinfo=UTC),
    ):

        class FrozenDatetime(datetime):
            instant = reference_date

            @classmethod
            def now(cls, tz=None):
                return (
                    cls.instant.astimezone(tz)
                    if tz is not None
                    else cls.instant.replace(tzinfo=None)
                )

        monkeypatch.setattr(maturity, "datetime", FrozenDatetime)
        pages = gen_tool_reference.render_pages(_records())
        rendered.append({name: page.encode("utf-8") for name, page in pages.items()})

    assert rendered[0] == rendered[1]
    assert b"| `implemented` | `current` |" in rendered[1]["alpha.md"]
    assert b"| `unrecorded` | `unrecorded` |" in rendered[1]["beta.md"]


def test_records_reject_malformed_projection_grammar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_projection(tmp_path, monkeypatch, {"format_version": 1})

    with pytest.raises(maturity.OperationMaturityError, match="projection root"):
        _records()


def test_a_missing_registered_tool_raises_rather_than_being_omitted() -> None:
    with pytest.raises(gen_tool_reference.ToolReferenceError) as error:
        gen_tool_reference.build_records(
            {"hmc_alpha": DESCRIPTIONS["hmc_alpha"]}, SECURITY, lambda _: True
        )

    assert "hmc_beta" in str(error.value)
    assert "omitted" in str(error.value)


def test_an_exposed_tool_with_no_security_record_raises() -> None:
    with pytest.raises(gen_tool_reference.ToolReferenceError) as error:
        _records(descriptions={"hmc_gamma": "Do gamma things."})

    assert "hmc_gamma" in str(error.value)
    assert "ToolSecurity" in str(error.value)


def test_a_blank_description_raises_rather_than_emitting_an_empty_cell() -> None:
    with pytest.raises(gen_tool_reference.ToolReferenceError) as error:
        _records(descriptions={"hmc_beta": "   \n\n"})

    assert "hmc_beta" in str(error.value)
    assert "empty" in str(error.value)


def test_an_operation_without_a_domain_split_raises() -> None:
    with pytest.raises(gen_tool_reference.ToolReferenceError) as error:
        _records(
            security={
                "hmc_beta": ToolSecurity(
                    effect="read", operation="beta", target_kind="console"
                )
            }
        )

    assert "hmc_beta" in str(error.value)
    assert "domain.verb" in str(error.value)


def test_a_pipe_in_a_summary_is_escaped_so_the_row_keeps_its_columns() -> None:
    (alpha, _) = _records(descriptions={"hmc_alpha": "List alphas | betas."})

    page = gen_tool_reference.render_pages([alpha])["alpha.md"]
    row = next(line for line in page.splitlines() if line.startswith("| `hmc_alpha`"))
    assert r"\|" in row
    assert row.replace(r"\|", "").count("|") == 9


def test_rendering_is_deterministic_over_an_unordered_registry() -> None:
    # Two tools share the `alpha` domain, so row order inside a page -- not only
    # the set of pages -- is what this pins.
    records = _records(
        security={
            "hmc_aardvark": ToolSecurity(
                effect="read", operation="alpha.get", target_kind="lpar"
            )
        },
        descriptions={"hmc_aardvark": "Get one aardvark.\n"},
    )
    assert [record.name for record in records][:2] == ["hmc_aardvark", "hmc_alpha"]

    forward = gen_tool_reference.render_pages(records)

    assert gen_tool_reference.render_pages(reversed(records)) == forward
    assert gen_tool_reference.render_pages(records) == forward


def test_grouping_is_a_parameter_not_a_hardcoded_domain() -> None:
    pages = gen_tool_reference.render_pages(
        _records(), group_key=lambda record: record.effect
    )

    assert set(pages) == {"read.md", "destructive.md", "index.md"}


def test_a_group_named_index_raises_instead_of_losing_its_page() -> None:
    with pytest.raises(gen_tool_reference.ToolReferenceError) as error:
        gen_tool_reference.render_pages(_records(), group_key=lambda record: "index")

    assert "index.md" in str(error.value)


def test_every_page_carries_the_banner_and_the_registered_set_decision() -> None:
    pages = gen_tool_reference.render_pages(_records())

    assert set(pages) == {"alpha.md", "beta.md", "index.md"}
    for name, text in pages.items():
        assert text.startswith(gen_tool_reference.BANNER), name
        assert "just tool-docs" in text, name
        assert gen_tool_reference.SCOPE_NOTE in text, name
        assert text.endswith("\n"), name


def test_every_page_says_the_summary_is_a_first_line_and_where_the_rest_lives() -> None:
    pages = gen_tool_reference.render_pages(_records())

    note = gen_tool_reference.SUMMARY_NOTE
    assert "first line of the tool's MCP description" in note
    assert "refuses" in note
    assert "`tools/list`" in note
    assert (
        "complete text, including the conditions under which a tool refuses, "
        "is the tool handler's docstring"
    ) in note
    assert "`src/hmcpctl/server_tools/`" in note
    for name, text in pages.items():
        intro = text.split("\n| ", 1)[0]
        assert note in intro, name


def test_a_tool_a_default_deployment_withholds_is_named_on_its_page() -> None:
    pages = gen_tool_reference.render_pages(
        _records(exposed=lambda name: name != "hmc_beta")
    )

    assert (
        "Registered but not exposed by a default deployment: `hmc_beta`"
        in (pages["beta.md"])
    )
    assert "## Not exposed by default" in pages["index.md"]
    assert "- **2** tools are registered." in pages["index.md"]
    assert "- **1** are exposed by a default deployment." in pages["index.md"]


def test_check_reports_a_stale_page_a_missing_page_and_an_orphan(tmp_path) -> None:
    pages = gen_tool_reference.render_pages(_records())
    gen_tool_reference.write_pages(pages, tmp_path)
    assert gen_tool_reference.check_pages(pages, tmp_path) == []

    (tmp_path / "alpha.md").write_text("hand-edited\n", encoding="utf-8")
    (tmp_path / "beta.md").unlink()
    (tmp_path / "orphan.md").write_text("left behind\n", encoding="utf-8")
    problems = "\n".join(gen_tool_reference.check_pages(pages, tmp_path))

    assert "stale: " in problems
    assert "hand-edited" in problems
    assert f"missing: {tmp_path / 'beta.md'}" in problems
    assert f"not generated by this script: {tmp_path / 'orphan.md'}" in problems


def test_writing_removes_a_page_the_generator_no_longer_emits(tmp_path) -> None:
    gen_tool_reference.write_pages(
        gen_tool_reference.render_pages(_records()), tmp_path
    )
    (alpha,) = [record for record in _records() if record.name == "hmc_alpha"]

    gen_tool_reference.write_pages(gen_tool_reference.render_pages([alpha]), tmp_path)

    assert not (tmp_path / "beta.md").exists()
    assert (tmp_path / "alpha.md").exists()


def test_writing_replaces_existing_page_atomically(tmp_path, monkeypatch) -> None:
    """A failed replacement leaves the prior generated page intact."""
    path = tmp_path / "alpha.md"
    original = f"{gen_tool_reference.BANNER}\nold\n"
    path.write_text(original, encoding="utf-8")

    def fail_replace(_temporary, _path):
        raise OSError("replacement failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        gen_tool_reference._write_page(path, f"{gen_tool_reference.BANNER}\nnew\n")

    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".alpha.md.*.tmp"))


def test_check_caps_the_full_diffs_but_still_names_every_stale_page(tmp_path) -> None:
    cap = gen_tool_reference._MAX_DIFFS
    pages = {
        f"page{index}.md": f"{gen_tool_reference.BANNER}\ngenerated {index}\n"
        for index in range(cap + 2)
    }
    gen_tool_reference.write_pages(pages, tmp_path)
    for name in pages:
        (tmp_path / name).write_text("hand-edited\n", encoding="utf-8")

    problems = gen_tool_reference.check_pages(pages, tmp_path)

    assert len(problems) == len(pages)
    assert sum("hand-edited" in problem for problem in problems) == cap


def test_writing_refuses_to_delete_a_page_it_did_not_write(tmp_path) -> None:
    hand_written = tmp_path / "architecture.md"
    hand_written.write_text("# Architecture\n", encoding="utf-8")

    with pytest.raises(gen_tool_reference.ToolReferenceError) as error:
        gen_tool_reference.write_pages(
            gen_tool_reference.render_pages(_records()), tmp_path
        )

    assert str(hand_written) in str(error.value)
    assert hand_written.read_text(encoding="utf-8") == "# Architecture\n"
    # The refusal has to happen before any page is written, or a run that ends in
    # ToolReferenceError still leaves 35 generated pages in someone's directory.
    assert list(tmp_path.iterdir()) == [hand_written]


def test_writing_refuses_on_a_page_it_cannot_decode(tmp_path) -> None:
    undecodable = tmp_path / "notes.md"
    undecodable.write_bytes(b"\xff\xfe not utf-8\n")

    with pytest.raises(gen_tool_reference.ToolReferenceError) as error:
        gen_tool_reference.write_pages(
            gen_tool_reference.render_pages(_records()), tmp_path
        )

    assert str(undecodable) in str(error.value)
    assert list(tmp_path.iterdir()) == [undecodable]


def test_check_mode_exits_one_on_a_stale_tree_and_names_the_fix(
    tmp_path, capsys
) -> None:
    assert gen_tool_reference.main(["--output", str(tmp_path)]) == 0
    (tmp_path / "index.md").write_text("stale\n", encoding="utf-8")

    assert gen_tool_reference.main(["--check", "--output", str(tmp_path)]) == 1

    captured = capsys.readouterr()
    assert "just tool-docs" in captured.err
    assert "does not match the tool registry" in captured.err


def test_the_committed_reference_matches_the_live_registry() -> None:
    """The same comparison `just tool-docs-check` makes, inside the suite."""
    records = gen_tool_reference.build_records(
        asyncio.run(gen_tool_reference.load_descriptions()),
        TOOL_SECURITY,
        gen_tool_reference.default_exposure(),
    )

    assert len(records) == len(TOOL_SECURITY)
    assert (
        gen_tool_reference.check_pages(
            gen_tool_reference.render_pages(records), COMMITTED
        )
        == []
    )


def test_served_descriptions_omit_the_docstring_sections_the_note_says_are_not_sent() -> (
    None
):
    """SUMMARY_NOTE tells readers `tools/list` omits these; pin that to the live walk."""
    descriptions = asyncio.run(gen_tool_reference.load_descriptions())

    sections = ("Args:", "Returns:", "Raises:")
    leaked = sorted(
        name
        for name, description in descriptions.items()
        if any(line.strip() in sections for line in (description or "").splitlines())
    )
    assert leaked == []


def test_the_readme_points_at_the_generated_reference_and_keeps_no_tool_table() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert README_POINTER in readme
    # A hand-maintained copy is the drift this generator exists to remove, so the
    # README must not grow tool rows back.
    assert "| `hmc_" not in readme
