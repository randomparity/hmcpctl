"""Release metadata contract: every release tag ships with a changelog entry."""

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELEASE_HEADING = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def test_latest_release_tag_has_a_changelog_entry() -> None:
    # ADR 0250: the release version is the newest reachable `vX.Y.Z` tag.
    describe = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert describe.returncode == 0, (
        "no release tag reachable from HEAD; fetch tags (git fetch --tags): "
        f"{describe.stderr.strip()}"
    )
    version = describe.stdout.strip().removeprefix("v")
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert version in set(_RELEASE_HEADING.findall(changelog)), (
        f"release tag v{version} has no '## [{version}]' entry in CHANGELOG.md; a "
        "release cannot ship unrecorded"
    )
