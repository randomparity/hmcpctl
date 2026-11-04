"""Contract tests for client-visible MCP instructions.

The ceiling-aware qualification is docs/adr/0048-ceiling-aware-server-instructions.md;
its end-to-end arm — the string a client actually receives at ``initialize``, against
the ``tools/list`` from the same application — is in tests/app/test_capability_ceiling.py.
"""

from hmcpctl._app import (
    CEILING_HEADING,
    INSTRUCTIONS,
    ceiling_aware_instructions,
    create_mcp,
)
from hmcpctl.server_tools.catalog import PRIMARY_TOOLS

# The tools the prose recommends by name: the ADR 0189 primary catalog it is written
# around (ADR 0330) and the specialists it names. Named here rather than derived, so the
# derivation under test is checked against an independent list and cannot agree with
# itself.
_PRIMARY = (
    "hmc_inventory",
    "hmc_plan_lpar",
    "hmc_provision_lpar",
    "hmc_reconfigure_lpar",
    "hmc_decommission_lpar",
    "hmc_power_lpar",
    "hmc_inspect_lpar",
    "hmc_prepare_host_handoff",
    "hmc_operation_status",
    "hmc_search_tools",
    "hmc_invoke_tool",
)
_RECOMMENDED = (*_PRIMARY, "hmc_capture_lpar_console", "hmc_create_lpar")

_CONVENTIONS_HEADING = "## Resource addressing and asynchronous jobs"
_WORKFLOWS_HEADING = "## Recommended workflows"
_EXPECTED_CONVENTIONS = """## Resource addressing and asynchronous jobs

Parameters ending in `*_name_or_uuid` accept either a resource name or UUID. Parameters
ending in `*_uuid` require a UUID. SSH-passthrough tools usually resolve UUIDs to HMC CLI
names before running the command. VIOS backup catalog tools are exceptions:
`hmc_list_vios_backups` passes a VIOS UUID directly. `hmc_backup_vios` and
`hmc_restore_vios` are SSH-ready with a direct system name plus VIOS UUID; a VIOS name or
system UUID requires REST. A system UUID resolves to its unique MTMS identity, never an HMC
CLI name, with no `lssyscfg` fallback.

For tools that expose asynchronous wait controls, `wait=False` is the default and returns
the submitted job for later polling; `wait=True` polls until the job reaches a terminal
state. Install tools use `wait_timeout_seconds=None` by default, deriving the client-side
polling budget from `hmc_timeout_minutes` plus one poll interval. Other wait-capable tools
use `timeout_seconds=300` by default. `poll_interval=5` is the default number of seconds
between status requests.

"""


def test_instructions_publish_resource_and_wait_conventions():
    """Clients receive one complete conventions block before workflow guidance."""
    instructions = create_mcp().instructions

    assert instructions is not None
    conventions_start = instructions.index(_CONVENTIONS_HEADING)
    workflows_start = instructions.index(_WORKFLOWS_HEADING)

    assert conventions_start < workflows_start
    assert instructions[conventions_start:workflows_start] == _EXPECTED_CONVENTIONS


def test_the_instructions_describe_every_primary_tool():
    """ADR 0330: the prose is written around what ``tools/list`` carries."""
    assert set(_PRIMARY) == PRIMARY_TOOLS
    for name in _PRIMARY:
        assert name in INSTRUCTIONS


def test_a_ceiling_that_withholds_nothing_leaves_the_instructions_untouched():
    """ADR 0048: no suffix and no noise for the case every shipped policy reaches."""
    qualified = ceiling_aware_instructions(lambda _name: True, _RECOMMENDED)

    assert qualified == INSTRUCTIONS
    assert CEILING_HEADING not in qualified


def test_every_tool_the_prose_recommends_is_found_by_the_scan():
    """ADR 0048: the withheld set is read off the prose, not a hand-kept roster.

    A rewrite of the prose that renames or drops a recommendation must move this
    list with it; a scan that stopped matching would leave the names below out of
    a suffix that withholds everything.
    """
    qualified = ceiling_aware_instructions(lambda _name: False, _RECOMMENDED)

    assert CEILING_HEADING in qualified
    for name in _RECOMMENDED:
        assert name in qualified.split(CEILING_HEADING)[1]


def test_only_names_in_the_authoritative_index_are_reported_as_withheld():
    """ADR 0048: the prose names settings and parameters that are not tools.

    ``hmc_timeout_minutes`` matches the same pattern as a tool name. Reporting it
    as a withheld tool would send a client looking for something that never
    existed, so the scan is intersected with the index rather than trusted alone.
    """
    qualified = ceiling_aware_instructions(lambda _name: False, ["hmc_inventory"])
    suffix = qualified.split(CEILING_HEADING)[1]

    assert "hmc_inventory" in suffix
    assert "hmc_timeout_minutes" not in suffix
    assert "hmc_plan_lpar" not in suffix


def test_a_withheld_tool_the_prose_never_names_produces_no_suffix():
    """ADR 0048: the suffix corrects the prose, so it is silent about the rest.

    Most of the ceiling is invisible to a client reading the instructions —
    ``tools/list`` already reports it — and restating it here would turn every
    narrow policy into a wall of text that buries the correction.
    """
    qualified = ceiling_aware_instructions(lambda _name: False, ["hmc_delete_lpar"])

    assert qualified == INSTRUCTIONS


def test_the_inspection_tool_is_recommended_only_when_the_policy_grants_it():
    """ADR 0048: a tools-only ceiling can withhold hmc_effective_permissions.

    That is the policy shape #255 names as worst, so the correction cannot depend
    on it. ``tools/list`` carries the client either way; the extra sentence is
    added only when the call it recommends would succeed.
    """
    granted = ceiling_aware_instructions(
        lambda name: name == "hmc_effective_permissions",
        ["hmc_inventory", "hmc_effective_permissions"],
    )
    withheld = ceiling_aware_instructions(lambda _name: False, ["hmc_inventory"])

    assert "Call hmc_effective_permissions" in granted
    assert "hmc_effective_permissions" not in withheld
    for qualified in (granted, withheld):
        assert "cannot be called, by name or through hmc_invoke_tool" in qualified
        assert "hmc_search_tools finds the rest it exposes" in qualified
