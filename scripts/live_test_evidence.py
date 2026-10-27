"""Render a citable PASS/SKIP/FAIL matrix from a live-run results document.

PR #869's matrix was hand-copied from a terminal. It carried no commit, so
nothing contradicted it when the branch moved two weeks and the arm stopped
being able to import. A matrix that cannot be falsified is not evidence.

Usage:
    uv run --no-sync python scripts/live_test_evidence.py test-results-round2.json

Output is Markdown, meant to be pasted into a pull request, issue or ADR.

**Refuses a document it cannot attribute.** No `run.tested_commit` means no
matrix and a non-zero exit, because an unattributed matrix is the failure this
script exists to prevent. A run whose tree was dirty is rendered with that
stated: the sha names a tree nobody exercised.

**Marks an interrupted run PARTIAL.** A document whose `run.partial` is present
and not `false` came from a run an exception or interrupt stopped: its rows end
where the run stopped, and `run.subtasks` is the selection, not what ran. The
matrix still renders, because its rows remain attributable, but its first line
says PARTIAL and the selection line is labelled as such. A non-boolean marker
fails closed as partial; a document without the key renders as before.

**Renders through a closed allowlist.** Result rows are HMC-derived and are
*not* uniformly redacted — `RunState.record` applies its redaction pass only
when `status == "FAIL"`, and the `tool` label on no path at all. So this emits
only `subtask`, `status`, `result`, `timestamp`, and a `tool` truncated at its
first `(`; never a row's `data` or `note`, and never the document's `hmc`
block, which carries the HMC hostname and account name. An allowlist fails
closed as the document grows a field.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: Everything this script will print from a result row. Adding a field here is
#: a deliberate act with a redaction question attached; the default is silence.
_ROW_ALLOWLIST = ("subtask", "tool", "status", "result", "timestamp")

_COLUMN_TITLES = {
    "subtask": "Subtask",
    "tool": "Tool",
    "status": "Status",
    "result": "Result",
    "timestamp": "Timestamp",
}

_STATUSES = ("PASS", "FAIL", "SKIP")

_PARTIAL_HEADING = (
    "**PARTIAL run** — interrupted before it finished. Its rows end where it "
    "stopped, and the call in flight then has no row."
)


def _cell(key: str, value: str) -> str:
    """One table cell, with `|` escaped so a value cannot forge a column."""
    escaped = value.replace("|", "\\|")
    return f"`{escaped}`" if key == "tool" else escaped


def sanitise_tool(label: Any) -> str:
    """The tool identifier alone, with any HMC-derived parenthetical dropped.

    `vmedia.py` builds labels like `hmc_delete_optical_media (<name>)` straight
    from an HMC response, and nothing redacts a `tool` value on any path. The
    identifier before the first `(` is the part this repository writes.
    """
    return str(label).split("(")[0].strip()


def allowlisted_row(row: Any) -> dict[str, str]:
    """One result row reduced to the fields that may be published."""
    if not isinstance(row, dict):
        return dict.fromkeys(_ROW_ALLOWLIST, "?")
    rendered = {key: str(row.get(key, "")) for key in _ROW_ALLOWLIST}
    rendered["tool"] = sanitise_tool(row.get("tool", ""))
    return rendered


def _provenance(document: dict[str, Any]) -> tuple[str, str] | None:
    """The run's commit and a qualifier, or `None` when it cannot be attributed."""
    run = document.get("run")
    if not isinstance(run, dict):
        return None
    commit = run.get("tested_commit")
    if not isinstance(commit, str) or not commit.strip():
        return None
    if run.get("tree_clean") is False:
        return commit, (
            " — **tree was dirty**: `src/` or `scripts/` had uncommitted changes, "
            "so this sha does not name the code that ran"
        )
    if run.get("tree_clean") is None:
        return commit, " — clean-tree state unknown"
    return commit, ""


def _schema_version(run: dict[str, Any]) -> str:
    """The recorded `HMC_SCHEMA_VERSION`, on one line, or a placeholder for older documents."""
    recorded = run.get("schema_version")
    if isinstance(recorded, str) and recorded.strip():
        return " ".join(recorded.split())
    return "(not recorded)"


def _is_partial(run: dict[str, Any]) -> bool:
    """Whether the run was interrupted; anything but an absent key or `false` is."""
    return run.get("partial", False) is not False


def _render_vios_ame_source(document: dict[str, Any]) -> str | None:
    run = document.get("run")
    if (
        not isinstance(run, dict)
        or document.get("source_only") is not True
        or run.get("group") != "vios-ame-source"
        or run.get("subtasks") != []
        or run.get("tree_clean") is not True
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
    ):
        return None
    commit = run.get("tested_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    cases = document.get("cases")
    if not isinstance(cases, list) or len(cases) > 2:
        return None
    outcomes = {
        "observed-accepted",
        "observed-rejected",
        "unavailable",
        "mismatch",
        "unknown",
    }
    cleanup = {"not-attempted", "pending", "submitted", "confirmed-absent"}
    lines = [
        "SOURCE ONLY — inactive VIOS configuration observations",
        "",
        f"Source revision: `{commit}`",
        "",
        "This is not product maturity or guest compression evidence.",
        "",
        "| Case | Observation | Cleanup |",
        "| --- | --- | --- |",
    ]
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            return None
        outcome, deleted = case.get("outcome"), case.get("deletion")
        if (
            not isinstance(outcome, str)
            or outcome not in outcomes
            or not isinstance(deleted, str)
            or deleted not in cleanup
        ):
            return None
        lines.append(f"| {index} | {outcome} | {deleted} |")
    lines.extend(
        [
            "",
            "Interrupted/held"
            if run.get("partial") is not False
            else "Source observations completed",
            "Independent recovery and session closeout remain private prerequisites.",
        ]
    )
    return "\n".join(lines)


def _render_vios_boot_source(document: dict[str, Any]) -> str | None:
    run = document.get("run")
    if (
        not isinstance(run, dict)
        or document.get("source_only") is not True
        or run.get("group") != "vios-boot-source"
        or run.get("subtasks") != []
        or run.get("tree_clean") is not True
        or type(run.get("partial")) is not bool
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
    ):
        return None
    commit = run.get("tested_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    cases = document.get("cases")
    deletion = document.get("deletion")
    outcomes = {"unobserved", "exact", "rejected", "mismatch", "unavailable", "unknown"}
    executions = {"not-attempted", "intent", "acknowledged", "rejected", "unknown"}
    if not isinstance(cases, list) or len(cases) > 8 or not isinstance(deletion, dict):
        return None
    observation = deletion.get("observation")
    if (
        not isinstance(observation, dict)
        or not isinstance(observation.get("outcome"), str)
        or observation["outcome"] not in outcomes
        or not isinstance(deletion.get("execution"), str)
        or deletion["execution"] not in executions
    ):
        return None
    lines = [
        "SOURCE ONLY — fixed VIOS pending-boot observations",
        "",
        f"Source revision: `{commit}`",
        "",
        "| Case | Observation |",
        "| --- | --- |",
    ]
    for index, case in enumerate(cases):
        if (
            not isinstance(case, dict)
            or type(case.get("index")) is not int
            or case["index"] != index
            or not isinstance(case.get("observation"), str)
            or case["observation"] not in outcomes
        ):
            return None
        lines.append(f"| {index + 1} | {case['observation']} |")
    lines += [
        "",
        f"Cleanup: {deletion['execution']} / {observation['outcome']}",
        "Interrupted/held" if run["partial"] else "Source observations completed",
        "Independent recovery and session closeout remain private prerequisites.",
    ]
    return "\n".join(lines)


def render(document: dict[str, Any]) -> str | None:
    """The Markdown matrix, or `None` when the document cannot be attributed."""
    run = document.get("run")
    if (isinstance(run, dict) and run.get("group") == "vios-boot-source") or (
        document.get("source_only") is True
        and isinstance(run, dict)
        and run.get("group") != "vios-ame-source"
    ):
        return _render_vios_boot_source(document)
    if document.get("source_only") is True or (
        isinstance(run, dict) and run.get("group") == "vios-ame-source"
    ):
        return _render_vios_ame_source(document)
    attribution = _provenance(document)
    if attribution is None:
        return None
    commit, qualifier = attribution
    run = document["run"]
    rows = [allowlisted_row(row) for row in document.get("results", [])]
    counts = {
        status: sum(1 for row in rows if row["status"] == status)
        for status in _STATUSES
    }

    partial = _is_partial(run)
    subtasks_label = (
        "Subtasks selected (not all ran)" if partial else "Subtasks dispatched"
    )
    finished_label = "Interrupted" if partial else "Finished"
    selection = (
        f"Group: `{run.get('group') or '(none)'}` · "
        f"Schema version: `{_schema_version(run)}` · "
        f"{subtasks_label}: `{run.get('subtasks')}` · "
        f"{finished_label}: `{run.get('finished')}`"
    )
    totals = (
        f"**TOTAL {len(rows)}** · PASS {counts['PASS']} · "
        f"FAIL {counts['FAIL']} · SKIP {counts['SKIP']}"
    )
    provenance_note = (
        "Rendered by `scripts/live_test_evidence.py` from the run's own results "
        "document. Row `data` and `note` fields and the document's `hmc` block "
        "are withheld: they carry HMC-derived text that is redacted only on "
        "FAIL rows."
    )

    interruption = [_PARTIAL_HEADING, ""] if partial else []
    lines = [
        *interruption,
        f"Live run on `{commit}`{qualifier}",
        "",
        selection,
        "",
        totals,
        "",
        "| " + " | ".join(_COLUMN_TITLES[key] for key in _ROW_ALLOWLIST) + " |",
        "|" + "---|" * len(_ROW_ALLOWLIST),
    ]
    # Columns are generated from the allowlist rather than written out, so the
    # allowlist is the single control: a field added to it is published, and a
    # field absent from it cannot be published by a second list drifting.
    lines.extend(
        "| " + " | ".join(_cell(key, row[key]) for key in _ROW_ALLOWLIST) + " |"
        for row in rows
    )
    lines.extend(["", provenance_note])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("results", type=Path, help="a test-results-*.json document")
    args = parser.parse_args(argv)

    try:
        document = json.loads(args.results.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"ERROR: cannot read {args.results}: {error}", file=sys.stderr)
        return 1
    if not isinstance(document, dict):
        print(f"ERROR: {args.results} is not a results document", file=sys.stderr)
        return 1

    matrix = render(document)
    if matrix is None:
        print(
            f"ERROR: {args.results} carries no run.tested_commit — it cannot be "
            "attributed to a commit, and an unattributed matrix is not evidence.\n"
            "       Re-run with a runner that stamps run provenance.",
            file=sys.stderr,
        )
        return 1

    print(matrix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
