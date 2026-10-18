"""Structural checks for the kdive Tier A contract page (issue #878).

Every backticked token on the page must resolve against the installed package or be
an allowlisted prose term, so a renamed symbol, tool, parameter or setting fails here
rather than in kdive's provider.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path
from typing import Any

from hmcpctl.config import HMCConfig
from hmcpctl.jobs.requests import POWER_OFF_OPERATIONS
from hmcpctl.operations.lpar.activation import power_off_lpar, submit_power_on_lpar
from hmcpctl.server import TOOL_SECURITY

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "kdive-tier-a-contract.md"
PAGE_LINK = "(kdive-tier-a-contract.md)"
BACKTICKED = re.compile(r"`([^`\n]+)`")
TOOL = re.compile(r"hmc_[a-z_]+")
PARAMETER = re.compile(r"([a-z_]+)=(\S+)")
SETTING = re.compile(r"HMC_[A-Z_]+")

EXPECTED_IMPORT_PATHS = {
    "hmcpctl.operations.lpar",
    "hmcpctl.operations.lpar.activation.submit_power_on_lpar",
    "hmcpctl.operations.lpar.activation.power_off_lpar",
    "hmcpctl.operations.lpar.inventory.get_lpar",
    "hmcpctl.operations.lpar.inventory.get_lpar_state",
    "hmcpctl.operations.lpar.activation.LparPowerResult",
    "hmcpctl.operations.lpar.console.capture_lpar_console_by_selector",
    "hmcpctl.operations.lpar.ownership.list_lpar_ownership",
    "hmcpctl.operations.systems.fleet.FleetListing",
    "hmcpctl.operations.systems.fleet.FleetListing.entries",
    "hmcpctl.ssh.console.ConsoleCapture.released",
    "hmcpctl.ssh.console.ConsoleCapture.error",
    "hmcpctl.ssh.console.ConsoleHeldError",
    "hmcpctl.ssh.console.ConsoleHoldLostError",
    "hmcpctl.ssh.console.WritableConsoleSession",
    "hmcpctl.operations.jobs.get_job",
    "hmcpctl.api",
}
EXPECTED_TOOLS = {
    "hmc_power_on_lpar",
    "hmc_power_off_lpar",
    "hmc_dump_restart_lpar",
    "hmc_get_lpar",
    "hmc_get_lpar_state",
    "hmc_capture_lpar_console",
    "hmc_list_lpar_ownership",
    "hmc_get_job",
}
EXPECTED_SETTINGS = {"HMC_AUTHORIZE_POWER_OPERATIONS", "HMC_SSH_VERIFY_HOST_KEY"}
#: kdive's vocabulary, the vendor's, a result key, a builtin, or an HMC CLI command.
PROSE_TERMS = {
    "PowerAction",
    "on",
    "off",
    "cycle",
    "reset",
    "dumpretry",
    "already_activated",
    "ValueError",
    "mkvterm",
    "rmvterm",
    "~/.ssh/known_hosts",
}


def _page_tokens() -> list[str]:
    return BACKTICKED.findall(PAGE.read_text(encoding="utf-8"))


def _resolve(dotted: str) -> None:
    """Import the longest module prefix of *dotted*, then walk attributes or dataclass fields."""
    parts = dotted.split(".")
    for split in range(len(parts), 0, -1):
        try:
            target: Any = importlib.import_module(".".join(parts[:split]))
        except ModuleNotFoundError:
            continue
        break
    else:
        raise AssertionError(f"no importable module in {dotted!r}")
    for part in parts[split:]:
        if hasattr(target, part):
            target = getattr(target, part)
        else:
            assert part in getattr(target, "__dataclass_fields__", {}), dotted
            return


def _classify(token: str, short_names: set[str]) -> str:
    if token.startswith("hmcpctl."):
        _resolve(token)
        return "import"
    if TOOL.fullmatch(token):
        assert token in TOOL_SECURITY, f"unregistered tool {token!r}"
        return "tool"
    if match := PARAMETER.fullmatch(token):
        name, value = match.groups()
        assert any(
            name in inspect.signature(operation).parameters
            for operation in (submit_power_on_lpar, power_off_lpar)
        ), f"not a power operation parameter: {token!r}"
        if name == "operation":
            assert value in POWER_OFF_OPERATIONS, (
                f"not an accepted PowerOff operation: {token!r}"
            )
        return "parameter"
    if SETTING.fullmatch(token):
        assert token.removeprefix("HMC_").lower() in HMCConfig.model_fields, token
        return "setting"
    if token in HMCConfig.model_fields:
        return "field"
    if token in short_names:
        return "short"
    assert token in PROSE_TERMS, (
        f"unresolvable token {token!r}: resolve it or allowlist it"
    )
    return "prose"


def test_page_has_no_fenced_blocks_the_token_scan_would_misread() -> None:
    assert "```" not in PAGE.read_text(encoding="utf-8")


def test_every_backticked_token_resolves_or_is_a_prose_term() -> None:
    tokens = _page_tokens()
    # A bare name counts only as the last segment of a dotted path the page also resolves.
    short_names = {
        token.rsplit(".", 1)[-1] for token in tokens if token.startswith("hmcpctl.")
    }
    found: dict[str, set[str]] = {}
    for token in tokens:
        found.setdefault(_classify(token, short_names), set()).add(token)

    assert found.get("import") == EXPECTED_IMPORT_PATHS
    assert found.get("tool") == EXPECTED_TOOLS
    assert found.get("setting") == EXPECTED_SETTINGS
    assert found.get("field") == {"authorize_power_operations", "ssh_verify_host_key"}
    assert found.get("parameter"), "the PowerAction mapping names no parameters"


def test_page_is_linked_once_from_the_index() -> None:
    assert (ROOT / "docs" / "index.md").read_text(encoding="utf-8").count(
        PAGE_LINK
    ) == 1
