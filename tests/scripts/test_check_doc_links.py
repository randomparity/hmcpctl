"""Tests for the relative Markdown link and anchor gate (scripts/check_doc_links.py).

Every fixture repository holds at least one link that must resolve and, where the
case is about a report, exactly one that must not. Asserting the whole report list
keeps a case from passing because the guard read nothing at all.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[2] / "scripts" / "check_doc_links.py"
MODULE_SPEC = importlib.util.spec_from_file_location("check_doc_links", MODULE_PATH)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
check_doc_links = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(check_doc_links)

pytestmark = pytest.mark.usefixtures("isolated_git")


def _git(root: Path, *args: str) -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "-C", str(root), *args], env=environment, check=True)


def _repo(root: Path, files: dict[str, str | bytes]) -> Path:
    """A git repository at *root* tracking exactly *files*."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    _git(root, "add", "--all")
    return root


def _report(root: Path) -> list[str]:
    return check_doc_links.check(root)


def test_missing_file_is_reported(tmp_path, capsys):
    root = _repo(
        tmp_path / "repo",
        {
            "README.md": "See [guide](docs/guide.md) and [gone](docs/gone.md).\n",
            "docs/guide.md": "# Guide\n",
        },
    )
    assert check_doc_links.main(["--repo-root", str(root)]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "README.md:1: missing: docs/gone.md"
    ]


@pytest.mark.parametrize(
    ("good", "bad", "reported"),
    [
        pytest.param("[a](/docs/x.md)", "[a](/docs/y.md)", "/docs/y.md", id="rooted"),
        pytest.param("[a](#top)", "[a](#bottom)", "#bottom", id="self-fragment"),
        pytest.param("[a](<x.md>)", "[a](<y.md>)", "<y.md>", id="angle"),
        pytest.param('[a](x.md "t")', '[a](y.md "t")', "y.md", id="title"),
        pytest.param("[a]: x.md", "[b]: y.md", "y.md", id="reference-definition"),
        pytest.param(
            "[a](x.md?plain=1)", "[a](y.md?plain=1)", "y.md?plain=1", id="query"
        ),
        pytest.param("[a](sp%20ace.md)", "[a](no%20pe.md)", "no%20pe.md", id="percent"),
        pytest.param("![a](img.png)", "![a](gone.png)", "gone.png", id="image"),
        pytest.param(
            "[wrapped\ntext](x.md)", "[wrapped\ntext](y.md)", "y.md", id="wrapped"
        ),
        pytest.param("[a](../docs/)", "[a](../nodir/)", "../nodir/", id="directory"),
        pytest.param("[a](../)", "[a](../../)", "../../", id="repository-root"),
    ],
)
def test_resolution_forms(tmp_path, good, bad, reported):
    root = _repo(
        tmp_path / "repo",
        {
            "docs/page.md": f"# Top\n\n{good}\n\n{bad}\n",
            "docs/x.md": "# X\n",
            "docs/sp ace.md": "# S\n",
            "docs/img.png": "png\n",
        },
    )
    line = 4 + good.count("\n") + 1
    assert _report(root) == [f"docs/page.md:{line}: " + _kind(reported) + reported]


def _kind(target: str) -> str:
    return "anchor: " if target.startswith("#") else "missing: "


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Plain Title", "plain-title"),
        ("`code` and **bold**", "code-and-bold"),
        ("A [link](https://example.test) here", "a-link-here"),
        ("Q&A: what? (yes)", "qa-what-yes"),
        ("Keep_under-score", "keep_under-score"),
        ("<span>Tagged</span> text", "tagged-text"),
        ("Trailing hashes ##", "trailing-hashes"),
    ],
)
def test_slugify(heading, slug):
    assert check_doc_links.anchors(f"## {heading}\n") == {slug}


def test_duplicate_heading_suffix():
    text = "# Same\n\n```sh\n# Same\n```\n\n## Same\n\n### Same\n"
    assert check_doc_links.anchors(text) == {"same", "same-1", "same-2"}


def test_html_anchor(tmp_path):
    root = _repo(
        tmp_path / "repo",
        {
            "a.md": '<a id="kept"></a>\n<a name="named"></a>\n# H\n',
            "b.md": "[x](a.md#kept) [y](a.md#named) [z](a.md#absent)\n",
        },
    )
    assert _report(root) == ["b.md:1: anchor: a.md#absent"]


def test_external_and_code_links_skipped(tmp_path):
    page = (
        "[web](https://example.test/x.md) [mail](mailto:a@example.test)\n"
        "[proto](//example.test/x.md) [ok](x.md)\n"
        "`[span](gone-span.md)`\n"
        "\n"
        "```md\n[fenced](gone-fenced.md)\n```\n"
        "~~~\n[tilde](gone-tilde.md)\n~~~\n"
        "[^1]: footnote text\n"
        "[split by\n\na blank line](gone-split.md)\n"
        "[real](gone-real.md)\n"
    )
    root = _repo(tmp_path / "repo", {"page.md": page, "x.md": "# X\n"})
    assert _report(root) == ["page.md:15: missing: gone-real.md"]


def test_untracked_target_is_missing(tmp_path):
    root = _repo(tmp_path / "repo", {"page.md": "[a](local.md)\n"})
    (root / "local.md").write_text("# Local\n", encoding="utf-8")
    assert _report(root) == ["page.md:1: missing: local.md"]


def test_deleted_tracked_target_is_missing(tmp_path):
    root = _repo(tmp_path / "repo", {"page.md": "[a](x.md)\n", "x.md": "# X\n"})
    (root / "x.md").unlink()
    assert _report(root) == ["page.md:1: missing: x.md"]


def test_path_above_repository_is_missing(tmp_path):
    root = _repo(tmp_path / "repo", {"page.md": "[a](../outside.md)\n"})
    (tmp_path / "outside.md").write_text("# Out\n", encoding="utf-8")
    assert _report(root) == ["page.md:1: missing: ../outside.md"]


@pytest.mark.parametrize("redirected_git_checkout", [False, True], indirect=True)
def test_redirected_git_checkout(redirected_git_checkout):
    root = redirected_git_checkout
    assert check_doc_links._tracked(root) == ["src/physical.py"]


def test_not_a_repository(tmp_path, capsys):
    assert check_doc_links.main(["--repo-root", str(tmp_path)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1
    assert err[0].startswith("ERROR: `git ls-files -z` failed")


def test_git_cannot_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert check_doc_links.main(["--repo-root", str(tmp_path)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1
    assert err[0].startswith("ERROR: could not run `git ls-files -z`")


def test_no_markdown_files(tmp_path, capsys):
    root = _repo(tmp_path / "repo", {"a.txt": "text\n"})
    assert check_doc_links.main(["--repo-root", str(root)]) == 1
    assert capsys.readouterr().err.splitlines() == [
        f"ERROR: no tracked Markdown file found in {root}"
    ]


def test_non_utf8_file(tmp_path, capsys):
    root = _repo(tmp_path / "repo", {"bad.md": b"caf\xe9\n"})
    assert check_doc_links.main(["--repo-root", str(root)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1
    assert err[0].startswith("ERROR: bad.md is not UTF-8")


def test_clean_repository_passes(tmp_path, capsys):
    root = _repo(
        tmp_path / "repo", {"a.md": "# A\n\n[self](#a) [b](b.md#b)\n", "b.md": "# B\n"}
    )
    assert check_doc_links.main(["--repo-root", str(root)]) == 0
    assert (
        capsys.readouterr().out
        == "OK: 2 relative link(s) in 2 Markdown file(s) resolve.\n"
    )


def test_committed_repository_passes():
    assert check_doc_links.main([]) == 0
