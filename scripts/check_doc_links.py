"""Guard: every relative link in a tracked Markdown file resolves, fragment included.

A relative link must name a tracked file or a directory holding one. When it carries a
``#fragment`` and names a tracked Markdown file, the fragment must be one of the
anchors GitHub renders for that file: an ATX heading slugged the GitHub way (with
``-1``, ``-2``, ... on repeats) or the ``id``/``name`` of an ``<a>`` tag.

The tree is ``git ls-files``, not the directory listing, so an untracked file --
including a local-only reference corpus linked into a worktree -- is neither read nor
a valid target, and a tracked file deleted from the work tree is reported missing.

Skipped by design: targets with a URI scheme or starting with ``//`` (external),
links inside fenced code blocks and inline code spans, fragments on non-Markdown
targets, setext headings, and HTML ``href``/``src`` attributes.

Usage:
    python scripts/check_doc_links.py [--repo-root <path>]
"""

from __future__ import annotations

import argparse
import bisect
import os
import posixpath
import re
import subprocess  # nosec B404
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

_REPO_ROOT = Path(__file__).resolve().parent.parent
_QUERY_TIMEOUT_SECONDS = 60

_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_CODE_SPAN = re.compile(
    r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.DOTALL
)
_LINK = re.compile(
    r"!?\[((?:[^\[\]]|\[[^\[\]]*\])*)\]"
    r"\(\s*(<[^>\n]*>|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
_REFERENCE = re.compile(
    r"^ {0,3}\[(?!\^)[^\]\n]+\]:[ \t]*(<[^>\n]*>|\S+)", re.MULTILINE
)
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_ATX = re.compile(
    r"^(?:[ \t]*(?:>|[-*+]|\d+[.)])[ \t]*)* {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$"
)
_HTML_ANCHOR = re.compile(
    r"<a\s[^>]*\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class DocLinksError(Exception):
    """The guard could not read its inputs."""


def _tracked(root: Path) -> list[str]:
    """Tracked paths in *root*, read with inherited Git routing removed."""
    command = ["git", "ls-files", "-z"]
    try:
        # A fixed, read-only Git inventory query.
        result = subprocess.run(  # nosec B603 B607
            command,
            cwd=root,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DocLinksError(
            f"could not run `git ls-files -z` in {root}: {error}"
        ) from error
    if result.returncode != 0:
        reason = " ".join(result.stderr.split())
        raise DocLinksError(f"`git ls-files -z` failed in {root}: {reason}")
    return [path for path in result.stdout.split("\0") if path]


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _without_fences(text: str) -> str:
    """*text* with every fenced code block blanked, line breaks kept."""
    lines = text.split("\n")
    fence = ""
    for index, line in enumerate(lines):
        match = _FENCE.match(line)
        if fence:
            if (
                match
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
            ):
                fence = "" if not match.group(2).strip() else fence
            lines[index] = ""
        elif match:
            fence = match.group(1)
            lines[index] = ""
    return "\n".join(lines)


def slugify(text: str) -> str:
    """The anchor GitHub renders for a heading whose source text is *text*."""
    text = _CODE_SPAN.sub(lambda match: match.group(2), text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text).replace("*", "")
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every fragment a link may name in the Markdown document *text*."""
    html: set[str] = set()
    slugs: set[str] = set()
    seen: Counter[str] = Counter()
    for line in _without_fences(text).split("\n"):
        html.update(_HTML_ANCHOR.findall(line))
        heading = _ATX.match(line)
        if heading:
            base = slugify(heading.group(1))
            slug = base
            while slug in slugs:
                seen[base] += 1
                slug = f"{base}-{seen[base]}"
            slugs.add(slug)
    return html | slugs


def _targets(text: str) -> list[tuple[int, str]]:
    """``(line, target)`` for each link and reference definition outside code."""
    prose = _CODE_SPAN.sub(_blank, _without_fences(text))
    starts = [0] + [offset + 1 for offset, char in enumerate(prose) if char == "\n"]
    found = [
        (match.start(), match.group(2))
        for match in _LINK.finditer(prose)
        if not _BLANK_LINE.search(match.group(1))
    ]
    found += [(match.start(), match.group(1)) for match in _REFERENCE.finditer(prose)]
    return sorted(
        (bisect.bisect_right(starts, offset), target) for offset, target in found
    )


class _Tree:
    """The tracked files present in the work tree, and the directories holding them."""

    def __init__(self, root: Path, tracked: list[str]) -> None:
        self.root = root
        self.files = {path for path in tracked if (root / path).is_file()}
        self.dirs = {
            parent
            for path in self.files
            for parent in Path(path).parents
            if parent != Path(".")
        }
        self._anchors: dict[str, set[str]] = {}

    def read(self, path: str) -> str:
        try:
            return (self.root / path).read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise DocLinksError(f"{path} is not UTF-8: {error.reason}") from error
        except OSError as error:
            raise DocLinksError(f"could not read {path}: {error.strerror}") from error

    def exists(self, path: str) -> bool:
        return path == "." or path in self.files or Path(path) in self.dirs

    def anchors(self, path: str) -> set[str]:
        if path not in self._anchors:
            self._anchors[path] = anchors(self.read(path))
        return self._anchors[path]


def _problem(tree: _Tree, source: str, target: str) -> str | None:
    """``missing`` or ``anchor`` when *target*, linked from *source*, does not resolve."""
    link, _, fragment = target.removeprefix("<").removesuffix(">").partition("#")
    path = unquote(link.partition("?")[0])
    if not path:
        resolved = source
    elif path.startswith("/"):
        resolved = posixpath.normpath(path.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    if not tree.exists(resolved):
        return "missing"
    in_markdown = resolved.endswith(".md") and resolved in tree.files
    if fragment and in_markdown and unquote(fragment) not in tree.anchors(resolved):
        return "anchor"
    return None


def _is_external(target: str) -> bool:
    bare = target.removeprefix("<")
    return not bare or bool(_SCHEME.match(bare)) or bare.startswith("//")


def check(root: Path) -> list[str]:
    """``<file>:<line>: <missing|anchor>: <target>`` for every broken relative link."""
    return _scan(root)[0]


def _scan(root: Path) -> tuple[list[str], int, int]:
    """The broken-link reports, the relative links checked, and the documents read."""
    tree = _Tree(root, _tracked(root))
    documents = sorted(path for path in tree.files if path.endswith(".md"))
    if not documents:
        raise DocLinksError(f"no tracked Markdown file found in {root}")
    problems: list[str] = []
    checked = 0
    for source in documents:
        for line, target in _targets(tree.read(source)):
            if _is_external(target):
                continue
            checked += 1
            kind = _problem(tree, source, target)
            if kind:
                problems.append(f"{source}:{line}: {kind}: {target}")
    return problems, checked, len(documents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help=f"repository to check (default: {_REPO_ROOT})",
    )
    root: Path = parser.parse_args(argv).repo_root.resolve()
    try:
        problems, checked, documents = _scan(root)
    except DocLinksError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"OK: {checked} relative link(s) in {documents} Markdown file(s) resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
