"""Render generic recovery findings and partial-run disposition."""

from __future__ import annotations

from typing import Any

from .common import Finding


def is_partial(document: dict[str, Any]) -> bool:
    """Whether the run was interrupted; anything but an absent key or `false` is."""
    run = document.get("run")
    return isinstance(run, dict) and run.get("partial", False) is not False


def report(
    document: dict[str, Any],
    findings: list[Finding],
    unwitnessed: list[int],
    unread: list[str],
) -> None:
    run = value if isinstance(value := document.get("run"), dict) else {}
    artifacts = document.get("artifacts")
    partial = is_partial(document)
    print(
        f"{'PARTIAL ' if partial else ''}recovery check for the "
        f"{run.get('group') or '(no group)'} run at "
        f"{run.get('tested_commit') or '(no commit)'}, "
        f"{'interrupted' if partial else 'finished'} "
        f"{run.get('finished') or '(unknown)'}"
    )
    marker = artifacts.get("pcie_run_marker") if isinstance(artifacts, dict) else None
    if marker:
        print(f"PCIe run marker {marker}")
    print("=" * 60)
    for finding in findings:
        print(f"STRANDED  {finding.what}")
        print(f"          {finding.detail}")
        print(f"  clear with:  {finding.remedy}")
    for message in unread:
        print(f"NOT READ  {message}")
    if unwitnessed:
        print(
            f"NOT WITNESSED  subtasks {', '.join(map(str, unwitnessed))}: check what "
            "they change by hand (docs/live-testing.md, step 4)"
        )
    if findings:
        print(
            "\nThis script issues no mutating call. Run the commands above "
            "yourself, then re-run this check."
        )
    if partial:
        print(
            "PARTIAL  the run was interrupted: the call in flight has no row, so "
            "inspect its partial evidence and independent checks (docs/live-testing.md)"
        )
    elif not findings and not unwitnessed and not unread:
        print("CLEAN  nothing this run dispatched was left behind")
