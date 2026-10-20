import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
POLICY_LINKS = {
    "LICENSE": "[License](LICENSE)",
    "CONTRIBUTING.md": "[Contributing](CONTRIBUTING.md)",
    "SECURITY.md": "[Security policy](SECURITY.md)",
}
PROJECT_URLS = {
    "Repository": "https://github.com/randomparity/hmcpctl",
    "Contributing": "https://github.com/randomparity/hmcpctl/blob/main/CONTRIBUTING.md",
    "Security": "https://github.com/randomparity/hmcpctl/security/policy",
}
PRIVATE_ADVISORY_URL = "https://github.com/randomparity/hmcpctl/security/advisories/new"
SELF_FILE_URLS = (
    "github.com/randomparity/hmcpctl/blob/",
    "github.com/randomparity/hmcpctl/tree/",
)
INLINE_LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")
GOVERNANCE_HEADING = "## Contributing, security, and license"
GOVERNANCE_NEXT = None
CONTRIBUTING_HEADING = "# Contributing"
CONTRIBUTING_NEXT = "## Changelog"
LOCAL_PATH_COMMANDS = (
    "just setup",
    "just verify",
    "UV_NO_SYNC=1 uv run --no-sync prek run --all-files",
)
LOCAL_PATH_EXPECTATIONS = (
    "focused",
    "test",
    "pull request",
    "[security policy](security.md)",
    "keep dependencies pinned",
    "agents.md",
)
SECURITY_TITLE = "# Security policy"
SECURITY_HEADING = "## Reporting a vulnerability"
SECURITY_EXPECTATIONS = (
    "do not open a public issue",
    "do not include passwords, access tokens, production data, or other secrets",
)
VIOS_BACKUP_TOOLS = (
    "hmc_list_vios_backups",
    "hmc_backup_vios",
    "hmc_restore_vios",
)
COUNT_NUMBER = r"(?:\*{2})?\d[\d,]*(?:\*{2})?"
TOOL_COUNT_NOUN = r"(?:tools?(?:\s+names?)?|names?)"
NUMBERED_TOOL_COUNT = rf"{COUNT_NUMBER}(?:\s+[\w-]+){{0,3}}\s+{TOOL_COUNT_NOUN}"
FIXED_TOOL_COUNT = re.compile(
    rf"(?:{NUMBERED_TOOL_COUNT}[^.\n]{{0,80}}\bevery ordinary tool\b"
    rf"|\bevery ordinary tool\b[^.\n]{{0,80}}(?:{COUNT_NUMBER}\s+total\b"
    rf"|{NUMBERED_TOOL_COUNT}))",
    re.IGNORECASE,
)


def _section(text: str, heading: str, next_heading: str | None = None) -> str:
    """Return the body between ``heading`` and the heading that follows it.

    Heading *order* is asserted, not mere presence: without it a renamed or reordered
    heading makes the second split return the rest of the document and the slice stops
    being a section.
    """
    assert heading in text, f"missing heading: {heading}"
    if next_heading is None:
        return text.split(heading, 1)[1]
    assert next_heading in text, f"missing heading: {next_heading}"
    assert text.index(heading) < text.index(next_heading), (
        f"'{heading}' must come before '{next_heading}'"
    )
    return text.split(heading, 1)[1].split(next_heading, 1)[0]


def _relocate(text: str, body: str, destination: str) -> str:
    """Move ``body`` out of its own section and under ``destination``."""
    return text.replace(body, "\n\n", 1).replace(destination, f"{destination}{body}", 1)


def _project_metadata() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        document = tomllib.load(file)
    return document["project"]


def test_architecture_documents_name_current_adapter_and_selector_modules() -> None:
    adr = (ROOT / "docs/adr/0013-resource-domain-module-ownership.md").read_text()
    server_doc = ast.get_docstring(
        ast.parse((ROOT / "src/hmcpctl/server.py").read_text())
    )
    app_doc = ast.get_docstring(ast.parse((ROOT / "src/hmcpctl/_app.py").read_text()))

    assert "`ssh/selectors.py` for HMC CLI selectors" in adr
    assert "`ssh_selectors.py`" not in adr
    assert server_doc is not None
    assert "domain adapters under ``server_tools/``" in server_doc
    assert "``server_lpars``" not in server_doc
    assert app_doc is not None
    assert "every ``server_tools/`` domain adapter" in app_doc
    assert "every ``server_*`` domain module" not in app_doc


def test_project_declares_mit_license_and_policy_urls() -> None:
    project = _project_metadata()

    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    classifiers = project.get("classifiers", [])
    assert "License :: OSI Approved :: MIT License" not in classifiers
    assert project["urls"] == PROJECT_URLS


def test_readme_links_canonical_governance_files() -> None:
    readme = (ROOT / "README.md").read_text()
    governance = _section(readme, GOVERNANCE_HEADING, GOVERNANCE_NEXT)

    for path, link in POLICY_LINKS.items():
        assert (ROOT / path).is_file(), f"missing canonical policy file: {path}"
        assert link in governance, f"'{GOVERNANCE_HEADING}' must link {path}"


def test_governance_links_relocated_out_of_their_section_are_caught() -> None:
    """The negative variant: every link stays in the file but leaves its section."""
    readme = (ROOT / "README.md").read_text()
    relocated = _relocate(
        readme, _section(readme, GOVERNANCE_HEADING, GOVERNANCE_NEXT), "## Install\n"
    )

    assert all(link in relocated for link in POLICY_LINKS.values())
    moved = _section(relocated, GOVERNANCE_HEADING, GOVERNANCE_NEXT)
    assert [link for link in POLICY_LINKS.values() if link in moved] == []


def test_readme_links_repository_files_relatively() -> None:
    """ADR 0251: a relative link resolves at the ref the reader is viewing."""
    readme = (ROOT / "README.md").read_text()

    assert [url for url in SELF_FILE_URLS if url in readme] == []
    relative = [
        target.split("#", 1)[0]
        for target in INLINE_LINK_TARGET.findall(readme)
        if "://" not in target and not target.startswith("#")
    ]
    assert relative
    assert [path for path in relative if not (ROOT / path).exists()] == []


def test_library_guide_documents_the_typed_facade_and_its_covered_surface() -> None:
    guide = (ROOT / "docs/python-api.md").read_text()
    library = " ".join(_section(guide, "## Stable connection API").split())

    assert "PEP 561" in library
    assert "py.typed" in library
    assert "exactly six stable names" in library
    assert "Domain operations" in guide
    assert "not compatibility promises" in guide


def test_hmc_compatibility_claims_are_evidence_scoped() -> None:
    readme = (ROOT / "docs/compatibility.md").read_text()
    cheatsheet = (ROOT / "docs" / "hmc-cli-cheatsheet.md").read_text()
    compatibility = readme.split("## HMC version compatibility", 1)[1].split(
        "### Firmware write-path compatibility", 1
    )[0]

    assert "HMC V8 through V11" not in compatibility
    assert "all the POWER generations" not in compatibility
    assert "POWER10 and POWER11" in compatibility
    assert "capabilities/README.md" in compatibility
    assert "HMC V10 or newer" in compatibility
    for tool in VIOS_BACKUP_TOOLS:
        assert tool in compatibility
        assert tool in cheatsheet
    assert "require HMC V10 or newer" in cheatsheet
    assert "HMC V10R3 answers a UOM write with HTTP 406" in readme


def test_access_policy_guidance_matches_connectionless_dispatch_semantics() -> None:
    readme = (ROOT / "docs/mcp-server.md").read_text()
    connectionless = " ".join(
        readme.split("### What the policy does not bound", 1)[1]
        .split("### Migrating to a required access policy", 1)[0]
        .split()
    )

    assert "Every MCP tool is dispatch-wrapped" in connectionless
    assert "connection dimension is vacuous" in connectionless
    assert "by tool or effect class" in connectionless
    assert 'targets = "all-targets"' in connectionless
    assert "withhold" in connectionless.lower()


def test_generated_policy_guidance_does_not_pin_a_stale_tool_count() -> None:
    readme = (ROOT / "docs/mcp-server.md").read_text()
    adr = (
        ROOT
        / "docs"
        / "adr"
        / "0041-fail-closed-startup-and-legacy-policy-generation.md"
    ).read_text()
    migration = readme.split("### Migrating to a required access policy", 1)[1].split(
        "### Narrowing `targets`", 1
    )[0]
    current_adr_guidance = adr.split(
        "- **The generator is the onboarding path for fresh installs too", 1
    )[1].split(
        "- **The audit record tells the caller what the denial deliberately does not", 1
    )[0]

    assert "every ordinary tool" in migration
    assert "every ordinary tool" in current_adr_guidance
    for guidance in (migration, current_adr_guidance):
        assert not FIXED_TOOL_COUNT.search(guidance)

        for fixed_count in (
            "136 tool names, every ordinary tool",
            "129 names, every ordinary tool",
            "**136** tools, every ordinary tool",
            "every ordinary tool is named explicitly (136 total)",
        ):
            stale_guidance = guidance.replace("every ordinary tool", fixed_count, 1)
            assert FIXED_TOOL_COUNT.search(stale_guidance)

    assert not FIXED_TOOL_COUNT.search(
        "Issue #289 requires every ordinary tool to be named explicitly"
    )
    assert not FIXED_TOOL_COUNT.search("31 total non-exhaustive tools")


def test_mcp_guide_preserves_reviewed_policy_during_recovery() -> None:
    readme = (ROOT / "docs/mcp-server.md").read_text()
    migration = readme.split("### Migrating to a required access policy", 1)[1].split(
        "### Detecting access-policy drift", 1
    )[0]

    assert "unknown tool" in migration
    assert "TOML" in migration
    assert "config diff-access-policy" in migration
    assert "init-access-policy --output" in migration
    assert "merge" in migration.lower()
    assert "preserve" in migration.lower()
    assert "Delete it" not in migration


def test_contribution_guide_defines_the_complete_local_path() -> None:
    guide = (ROOT / "CONTRIBUTING.md").read_text()
    local_path = _section(guide, CONTRIBUTING_HEADING, CONTRIBUTING_NEXT)

    for command in LOCAL_PATH_COMMANDS:
        assert f"`{command}`" in local_path, (
            f"'{CONTRIBUTING_HEADING}' must name `{command}`"
        )
    for expectation in LOCAL_PATH_EXPECTATIONS:
        assert expectation in local_path.lower(), (
            f"'{CONTRIBUTING_HEADING}' must state {expectation!r}"
        )


def test_local_path_relocated_into_another_section_is_caught() -> None:
    """The negative variant: the guidance stays in the guide but leaves its section."""
    guide = (ROOT / "CONTRIBUTING.md").read_text()
    relocated = _relocate(
        guide,
        _section(guide, CONTRIBUTING_HEADING, CONTRIBUTING_NEXT),
        f"{CONTRIBUTING_NEXT}\n",
    )

    assert all(f"`{command}`" in relocated for command in LOCAL_PATH_COMMANDS)
    assert all(
        expectation in relocated.lower() for expectation in LOCAL_PATH_EXPECTATIONS
    )
    moved = _section(relocated, CONTRIBUTING_HEADING, CONTRIBUTING_NEXT)
    assert [command for command in LOCAL_PATH_COMMANDS if f"`{command}`" in moved] == []
    assert [
        expectation
        for expectation in LOCAL_PATH_EXPECTATIONS
        if expectation in moved.lower()
    ] == []


def test_security_policy_uses_only_private_vulnerability_reporting() -> None:
    policy = (ROOT / "SECURITY.md").read_text()
    assert policy.index(SECURITY_TITLE) < policy.index(SECURITY_HEADING), (
        f"'{SECURITY_TITLE}' must come before '{SECURITY_HEADING}'"
    )
    reporting = _section(policy, SECURITY_HEADING)

    assert PRIVATE_ADVISORY_URL in reporting
    for expectation in SECURITY_EXPECTATIONS:
        assert expectation in reporting.lower()


def test_reporting_guidance_relocated_out_of_its_section_is_caught() -> None:
    """The negative variant: the guidance stays in the file but leaves its section."""
    policy = (ROOT / "SECURITY.md").read_text()
    relocated = _relocate(
        policy, _section(policy, SECURITY_HEADING), f"{SECURITY_TITLE}\n"
    )

    assert PRIVATE_ADVISORY_URL in relocated
    assert all(
        expectation in relocated.lower() for expectation in SECURITY_EXPECTATIONS
    )
    moved = _section(relocated, SECURITY_HEADING).lower()
    assert PRIVATE_ADVISORY_URL not in moved
    assert [
        expectation for expectation in SECURITY_EXPECTATIONS if expectation in moved
    ] == []
