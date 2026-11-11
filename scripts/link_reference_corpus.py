"""Link the local-only reference corpus into a worktree during `just setup`.

`docs/refs/` (vendored HMC API reference) and `AGENTS.local.md` are gitignored
and exist on one operator host only (see AGENTS.md). A sibling worktree under
`../hmcpctl-worktrees/` starts without either path, so this script symlinks
them in from the main checkout when present, and announces them as
unavailable — rather than staying silent — when the host has no corpus.

Usage:
    python scripts/link_reference_corpus.py

Always exits 0: this step must never fail `just setup`.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 # Call sites document execution authority.
from pathlib import Path

REFERENCE_PATHS = ("docs/refs", "AGENTS.local.md")


def sync_reference_corpus(worktree_root: Path, main_root: Path) -> list[str]:
    """Symlink or report missing local-only reference material.

    Returns the human-readable messages to print, one per path that was
    linked or found unavailable. Never raises: a per-path symlink failure is
    caught and reported as its own message rather than propagated.
    """
    if worktree_root == main_root:
        return []

    messages = []
    for rel in REFERENCE_PATHS:
        link_path = worktree_root / rel
        if link_path.exists() or link_path.is_symlink():
            continue

        target_path = main_root / rel
        if not target_path.exists():
            messages.append(f"reference corpus not available on this host: {rel}")
            continue

        try:
            link_path.parent.mkdir(parents=True, exist_ok=True)
            relative_target = Path(os.path.relpath(target_path, link_path.parent))
            link_path.symlink_to(relative_target)
            messages.append(f"linked {rel} from the main checkout")
        except OSError as error:
            messages.append(f"could not link {rel} from the main checkout: {error}")

    return messages


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    # Callers pass fixed rev-parse queries for the physical checkout and common dir.
    result = subprocess.run(  # nosec B603, B607
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        },
    )
    return result.stdout.strip()


def _resolve_roots() -> tuple[Path, Path]:
    """Resolve the current worktree root and the main checkout root.

    `--git-common-dir` names the main checkout's `.git` directory even when run
    from a linked worktree, where `--git-dir` names the worktree's own
    `.git/worktrees/<name>` instead. That difference is the whole technique.
    """
    worktree_root = Path(_run_git(["rev-parse", "--show-toplevel"]))
    common_dir = Path(_run_git(["rev-parse", "--git-common-dir"]))
    main_root = Path(_run_git(["rev-parse", "--show-toplevel"], cwd=common_dir / ".."))
    return worktree_root, main_root


def main() -> int:
    try:
        worktree_root, main_root = _resolve_roots()
    except (subprocess.CalledProcessError, OSError) as error:
        print(
            f"reference corpus check skipped: could not resolve worktree roots ({error})"
        )
        return 0

    for message in sync_reference_corpus(worktree_root, main_root):
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
