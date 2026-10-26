"""Contract tests for the live-run evidence renderer.

The output's purpose is to be pasted into a pull request, so the allowlist
cases below are the load-bearing ones: a leak here is public.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

SCRIPTS_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
import live_test_evidence as evidence  # noqa: E402

_SENTINEL = "SENTINEL-9ac1"


def _document(**overrides) -> dict:
    document = {
        "run": {
            "tested_commit": "a" * 40,
            "tree_clean": True,
            "group": "dedicated",
            "subtasks": [24],
            "finished": "2026-09-21T10:00:00+00:00",
        },
        "config": {"system_name": "sys-R1"},
        "hmc": {
            "host": f"{_SENTINEL}-host.internal",
            "port": 12443,
            "user": f"{_SENTINEL}-user",
            "verify_ssl": False,
        },
        "artifacts": {"pcie_run_marker": "pcie-deadbeef"},
        "results": [
            {
                "subtask": 24,
                "tool": "hmc_list_dedicated_pcie_slots",
                "status": "PASS",
                "result": "passed",
                "timestamp": "2026-09-21T09:59:00+00:00",
                "note": f"note carries {_SENTINEL}",
                "data": {"nested": [f"data carries {_SENTINEL}"]},
            }
        ],
    }
    document.update(overrides)
    return document


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "test-results-dedicated.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Only allowlisted, sanitised fields are emitted (Validation 6)
# ---------------------------------------------------------------------------


def test_no_sentinel_from_data_note_or_the_hmc_block_reaches_the_output(
    tmp_path, capsys
):
    assert evidence.main([str(_write(tmp_path, _document()))]) == 0

    captured = capsys.readouterr()
    assert _SENTINEL not in captured.out
    assert _SENTINEL not in captured.err
    assert "hmc_list_dedicated_pcie_slots" in captured.out


def test_a_tool_label_built_from_an_hmc_response_is_truncated(tmp_path, capsys):
    """`vmedia.py` builds labels as `f"tool ({name})"` from HMC text."""
    document = _document()
    document["results"][0]["tool"] = f"hmc_delete_optical_media ({_SENTINEL}.iso)"

    assert evidence.main([str(_write(tmp_path, document))]) == 0

    output = capsys.readouterr().out
    assert _SENTINEL not in output
    assert "hmc_delete_optical_media" in output


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("hmc_list_lpars", "hmc_list_lpars"),
        ("hmc_delete_optical_media (secret.iso)", "hmc_delete_optical_media"),
        ("hmc_run_command (chsyscfg -r prof -i 'x(y)')", "hmc_run_command"),
        ("  padded  ", "padded"),
        ("(leading)", ""),
    ],
)
def test_tool_sanitisation_keeps_only_the_leading_identifier(label, expected):
    assert evidence.sanitise_tool(label) == expected


def test_the_allowlist_is_closed_over_a_document_that_grew_a_field(tmp_path, capsys):
    """A field added to a row later must not appear merely by existing."""
    document = _document()
    document["results"][0]["future_field"] = f"added later, carries {_SENTINEL}"

    assert evidence.main([str(_write(tmp_path, document))]) == 0
    assert _SENTINEL not in capsys.readouterr().out


def test_allowlisted_row_emits_exactly_the_allowlisted_keys():
    row = evidence.allowlisted_row(
        {"subtask": 1, "tool": "t", "status": "PASS", "note": "x", "data": "y"}
    )

    assert set(row) == set(evidence._ROW_ALLOWLIST)


def test_a_row_that_is_not_an_object_does_not_crash_the_render():
    assert set(evidence.allowlisted_row("not a row")) == set(evidence._ROW_ALLOWLIST)


def test_every_allowlisted_field_has_a_column_title():
    """The table is generated from the allowlist; a missing title is a KeyError."""
    assert set(evidence._ROW_ALLOWLIST) <= set(evidence._COLUMN_TITLES)


def test_a_pipe_in_a_value_cannot_forge_a_table_column(tmp_path, capsys):
    document = _document()
    document["results"][0]["result"] = "passed | FAIL | forged"

    assert evidence.main([str(_write(tmp_path, document))]) == 0

    row = next(
        line for line in capsys.readouterr().out.splitlines() if "forged" in line
    )
    assert "\\|" in row
    delimiters = len(re.findall(r"(?<!\\)\|", row))
    assert delimiters == len(evidence._ROW_ALLOWLIST) + 1


# ---------------------------------------------------------------------------
# An unattributable run is refused (Validation 7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run",
    [None, {}, {"tested_commit": None}, {"tested_commit": ""}, {"tested_commit": "  "}],
    ids=["no-block", "empty-block", "null", "empty", "blank"],
)
def test_a_document_without_a_commit_is_refused_with_no_output(tmp_path, capsys, run):
    document = _document()
    if run is None:
        del document["run"]
    else:
        document["run"] = run

    assert evidence.main([str(_write(tmp_path, document))]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot be attributed" in captured.err


def test_a_run_block_that_is_not_an_object_is_refused(tmp_path, capsys):
    assert evidence.main([str(_write(tmp_path, _document(run="a" * 40)))]) == 1
    assert capsys.readouterr().out == ""


def test_an_unreadable_document_is_refused(tmp_path, capsys):
    assert evidence.main([str(tmp_path / "absent.json")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_a_json_document_that_is_not_an_object_is_refused(tmp_path, capsys):
    path = tmp_path / "results.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert evidence.main([str(path)]) == 1
    assert "not a results document" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The matrix matches the document (Validation 8)
# ---------------------------------------------------------------------------


def test_totals_and_rows_match_the_document(tmp_path, capsys):
    document = _document()
    document["results"] = [
        {
            "subtask": n,
            "tool": f"tool_{n}",
            "status": status,
            "result": "r",
            "timestamp": "t",
        }
        for n, status in enumerate(["PASS", "PASS", "FAIL", "SKIP"])
    ]

    assert evidence.main([str(_write(tmp_path, document))]) == 0

    output = capsys.readouterr().out
    assert "**TOTAL 4** · PASS 2 · FAIL 1 · SKIP 1" in output
    for n in range(4):
        assert f"`tool_{n}`" in output


def test_the_commit_and_group_appear_in_the_header(tmp_path, capsys):
    assert evidence.main([str(_write(tmp_path, _document()))]) == 0

    output = capsys.readouterr().out
    assert "a" * 40 in output
    assert "`dedicated`" in output


@pytest.mark.parametrize("recorded", ["V1_0", "(not set)"])
def test_the_recorded_schema_version_appears_in_the_header(tmp_path, capsys, recorded):
    document = _document()
    document["run"]["schema_version"] = recorded

    assert evidence.main([str(_write(tmp_path, document))]) == 0

    assert f"Schema version: `{recorded}`" in capsys.readouterr().out


@pytest.mark.parametrize("recorded", [None, "", "  ", 7, ["V1_0"]])
def test_a_missing_or_malformed_schema_version_renders_a_placeholder(
    tmp_path, capsys, recorded
):
    """Results documents from before #1089 carry no field; none may crash the render."""
    document = _document()
    if recorded is not None:
        document["run"]["schema_version"] = recorded

    assert evidence.main([str(_write(tmp_path, document))]) == 0

    assert "Schema version: `(not recorded)`" in capsys.readouterr().out


def test_a_newline_in_the_schema_version_cannot_add_a_line(tmp_path, capsys):
    document = _document()
    document["run"]["schema_version"] = "V1_0\n| forged | row |"

    assert evidence.main([str(_write(tmp_path, document))]) == 0

    assert "Schema version: `V1_0 | forged | row |`" in capsys.readouterr().out


def test_a_dirty_tree_is_stated_beside_the_commit(tmp_path, capsys):
    """The sha names a tree nobody exercised; a reader must not cite it silently."""
    document = _document()
    document["run"]["tree_clean"] = False

    assert evidence.main([str(_write(tmp_path, document))]) == 0

    output = capsys.readouterr().out
    assert "tree was dirty" in output
    assert "does not name the code that ran" in output


def test_an_unknown_clean_tree_state_is_stated(tmp_path, capsys):
    document = _document()
    document["run"]["tree_clean"] = None

    assert evidence.main([str(_write(tmp_path, document))]) == 0
    assert "clean-tree state unknown" in capsys.readouterr().out


def test_a_clean_tree_adds_no_qualifier(tmp_path, capsys):
    assert evidence.main([str(_write(tmp_path, _document()))]) == 0

    output = capsys.readouterr().out
    assert "tree was dirty" not in output
    assert "state unknown" not in output


def test_an_empty_result_set_renders_a_zero_matrix(tmp_path, capsys):
    assert evidence.main([str(_write(tmp_path, _document(results=[])))]) == 0
    assert "**TOTAL 0** · PASS 0 · FAIL 0 · SKIP 0" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# An interrupted run is marked PARTIAL, never read as a complete one (#1340)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("partial", [True, "yes", 1])
def test_a_partial_run_is_marked_and_does_not_claim_what_ran(tmp_path, capsys, partial):
    """A non-boolean marker fails closed: the runner only ever writes a bool."""
    document = _document()
    document["run"]["partial"] = partial

    assert evidence.main([str(_write(tmp_path, document))]) == 0

    output = capsys.readouterr().out
    assert output.startswith("**PARTIAL run**")
    assert "Subtasks selected (not all ran): `[24]`" in output
    assert "Interrupted: `2026-09-21T10:00:00+00:00`" in output
    assert "Subtasks dispatched" not in output
    assert "Finished" not in output
    assert "a" * 40 in output


@pytest.mark.parametrize("partial", [False, None])
def test_a_complete_or_older_run_renders_as_before(tmp_path, capsys, partial):
    """`None` stands for a document written before #1336, which has no key."""
    document = _document()
    if partial is not None:
        document["run"]["partial"] = partial

    assert evidence.main([str(_write(tmp_path, document))]) == 0

    output = capsys.readouterr().out
    assert output.startswith("Live run on `" + "a" * 40 + "`")
    assert "PARTIAL" not in output
    assert "Subtasks dispatched: `[24]`" in output
    assert "Finished: `2026-09-21T10:00:00+00:00`" in output


def test_system_profiles_private_filtered():
    document = _document()
    document["run"]["group"] = "system-profiles"
    document["run"]["subtasks"] = [44]
    document["artifacts"] = {
        "system_profile_baseline": {"private": _SENTINEL},
        "system_profile_jobs": [{"private": _SENTINEL}],
    }
    document["config"] = {"system_profile_members_json": _SENTINEL}
    document["results"] = [
        {
            "subtask": 44,
            "tool": "hmc_create_system_profile",
            "status": "PASS",
            "data": _SENTINEL,
        }
    ]
    rendered = evidence.render(document)
    assert rendered is not None and _SENTINEL not in rendered
    assert "hmc_create_system_profile" in rendered


def _vios_source_document():
    return {
        "schema_version": 1,
        "source_only": True,
        "run": {
            "group": "vios-ame-source",
            "subtasks": [],
            "tree_clean": True,
            "tested_commit": "a" * 40,
            "partial": False,
        },
        "cases": [
            {
                "outcome": "observed-accepted",
                "deletion": "confirmed-absent",
                "native_error": "PRIVATE-SENTINEL",
                "name": "PRIVATE-SENTINEL",
            }
        ],
        "results": [{"status": "PASS", "tool": "PRIVATE-SENTINEL"}],
        "hmc": {"host": "PRIVATE-SENTINEL"},
    }


def test_vios_source_is_never_a_product_matrix():
    rendered = evidence.render(_vios_source_document())
    assert rendered is not None and "SOURCE ONLY" in rendered
    assert "PRIVATE-SENTINEL" not in rendered and "PASS" not in rendered
    assert "observed-accepted" in rendered


@pytest.mark.parametrize("fault", ["flag", "group", "subtask", "commit", "outcome"])
def test_vios_source_contradiction_never_falls_through(fault):
    document = _vios_source_document()
    if fault == "flag":
        document["source_only"] = False
    elif fault == "group":
        document["run"]["group"] = "dedicated"
    elif fault == "subtask":
        document["run"]["subtasks"] = [58]
    elif fault == "commit":
        document["run"]["tested_commit"] = "PRIVATE-SENTINEL"
    else:
        document["cases"][0]["outcome"] = "PRIVATE-SENTINEL"
    assert evidence.render(document) is None


@pytest.mark.parametrize("field", ["outcome", "deletion"])
@pytest.mark.parametrize("value", [None, [], {}, True, 1, "PRIVATE-SENTINEL"])
def test_vios_source_malformed_observations_are_refused(field, value):
    document = _vios_source_document()
    document["cases"][0][field] = value
    assert evidence.render(document) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run", None),
        ("run", []),
        ("cases", None),
        ("cases", {}),
        ("cases", [None]),
        ("cases", [{}, {}, {}]),
        ("schema_version", 2),
    ],
)
def test_vios_source_malformed_envelope_is_refused(field, value):
    document = _vios_source_document()
    document[field] = value
    assert evidence.render(document) is None


@pytest.mark.parametrize(
    "commit", [None, "A" * 40, "a" * 39, "a" * 41, "a" * 40 + "\n"]
)
def test_vios_source_commit_is_strict_lowercase_sha(commit):
    document = _vios_source_document()
    document["run"]["tested_commit"] = commit
    assert evidence.render(document) is None


@pytest.mark.parametrize("partial", [True, None, 0, "false"])
def test_vios_source_only_literal_false_claims_completed(partial):
    document = _vios_source_document()
    document["run"]["partial"] = partial
    rendered = evidence.render(document)
    assert rendered is not None and "Interrupted/held" in rendered
    assert "Source observations completed" not in rendered


def test_vios_source_empty_observations_remain_source_only():
    document = _vios_source_document()
    document["cases"] = []
    rendered = evidence.render(document)
    assert rendered is not None and "SOURCE ONLY" in rendered
    assert "TOTAL" not in rendered and "PASS" not in rendered
