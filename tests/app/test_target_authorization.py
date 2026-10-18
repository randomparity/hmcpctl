"""Dispatch-boundary target-scope authorization on a composed application.

Covers docs/workflow/specs/2026-08-19-target-scope-dispatch-design.md; the
decision record is docs/adr/0039-dispatch-time-target-scope.md.

The outbound-sealing fixture and the config fixture are imported from the #222
module rather than rebuilt: a second copy of "every name a handler could reach an
HMC through" would drift from the first, and the first is the one that was
reviewed.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

# `lab_profile` is autouse in its own module, so importing it applies it here
# too — no test in this module names it, and doing so would shadow the import.
from test_connection_authorization import (  # noqa: F401 - imported for autouse
    SOURCE,
    _policy,
    _seal_every_outbound_path,
    lab_profile,
)

from hmcpctl.authorization.access_policy import AccessPolicyError
from hmcpctl.authorization.connection_scope import ConnectionScopeError
from hmcpctl.authorization.dispatch_scope import dispatch_authorizer
from hmcpctl.authorization.target_scope import TargetScopeError
from hmcpctl.config import config_dir
from hmcpctl.operations.lpar.provision import ProvisionAdapters, ProvisionStorage
from hmcpctl.server import TOOL_SECURITY, create_mcp

# Two grants that are individually narrow and, combined, would authorize a
# deletion neither of them describes. This is the fail-open ADR 0036 named and
# ADR 0039's module split exists to make structurally unavailable.
SPLIT_GRANTS = [
    {
        "tools": ["hmc_delete_lpar"],
        "connections": ["lab"],
        "targets": {"managed_system": ["sys-1"], "lpar": ["victim"]},
    },
    {
        "tools": ["hmc_delete_lpar"],
        "connections": ["prod"],
        "targets": {"managed_system": ["sys-2"], "lpar": ["other"]},
    },
]

LAB_NARROW = [
    {
        "tools": ["hmc_delete_lpar"],
        "connections": ["lab"],
        "targets": {"managed_system": ["sys-1"], "lpar": ["victim"]},
    }
]


def _call(application, tool: str, arguments: dict):
    async def _go():
        async with Client(application) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(_go())


def _authorize(grants, tool: str, arguments: dict, name: str = "lab-only"):
    """Run the real authorizer over the real classification for one call."""
    policy = _policy(grants, name)
    return dispatch_authorizer(policy)(tool, TOOL_SECURITY[tool], arguments)


def _delete(profile="lab", system="sys-1", lpar="victim") -> dict:
    return {
        "system_name_or_uuid": system,
        "lpar_name_or_uuid": lpar,
        "profile": profile,
        "ownership_override": False,
    }


# ---------------------------------------------------------------------------
# R1 — the conjunction is per grant, never a union across grants
# ---------------------------------------------------------------------------


def test_one_grants_connection_cannot_combine_with_anothers_targets():
    """The fail-open this whole change is shaped around.

    Grant 0 permits `lab` on sys-1/victim. Grant 1 permits `prod` on
    sys-2/other. A call naming `lab` *and* sys-2/other satisfies one dimension
    from each and must be refused: no single grant describes it, and ADR 0036
    fixed that a grant is evaluated conjunctively.

    With one enforced dimension this test could not exist — a union across
    grants and a conjunction inside one give the same answer for a single
    condition. It is the second dimension that makes them differ, which is why
    the loop moved into its own module.
    """
    with pytest.raises(TargetScopeError):
        _authorize(
            SPLIT_GRANTS, "hmc_delete_lpar", _delete(system="sys-2", lpar="other")
        )


def test_the_mirror_direction_is_refused_too():
    """Grant 1's connection with grant 0's targets, so the loop is not merely
    order-dependent."""
    with pytest.raises(TargetScopeError):
        _authorize(
            SPLIT_GRANTS, "hmc_delete_lpar", _delete(profile="prod", system="sys-1")
        )


def test_each_grant_still_permits_the_call_it_describes():
    """The split grants are not simply inert: both halves work whole."""
    assert _authorize(SPLIT_GRANTS, "hmc_delete_lpar", _delete()) is None
    assert (
        _authorize(
            SPLIT_GRANTS,
            "hmc_delete_lpar",
            _delete(profile="prod", system="sys-2", lpar="other"),
        )
        is None
    )


# ---------------------------------------------------------------------------
# R5, R6 — matching on the real classification
# ---------------------------------------------------------------------------


def test_a_matching_table_permits():
    assert _authorize(LAB_NARROW, "hmc_delete_lpar", _delete()) is None


@pytest.mark.parametrize(
    ("system", "lpar"),
    [("sys-2", "victim"), ("sys-1", "other"), ("sys-2", "other")],
)
def test_any_selector_outside_the_table_denies(system, lpar):
    with pytest.raises(TargetScopeError):
        _authorize(LAB_NARROW, "hmc_delete_lpar", _delete(system=system, lpar=lpar))


def test_add_vnic_requires_authorization_for_backing_vios():
    """The backing VIOS is an independently mutable target of vNIC creation."""
    grant = {
        "tools": ["hmc_add_vnic"],
        "connections": ["lab"],
        "targets": {"managed_system": ["sys-1"], "lpar": ["victim"]},
    }
    arguments = {
        "system_name_or_uuid": "sys-1",
        "lpar_name_or_uuid": "victim",
        "vios_name": "vios-1",
        "vios_lpar_id": "2",
        "adapter_id": "1",
        "physical_port_id": "0",
        "capacity_percent": 20.0,
        "port_vlan_id": 100,
        "profile": "lab",
    }
    with pytest.raises(
        AccessPolicyError, match="requires a target constraint for kind 'vios'"
    ):
        _authorize([grant], "hmc_add_vnic", arguments)


VIOS_BACKUP_ARGUMENTS = {
    "system_name_or_uuid": "sys-1",
    "vios_name_or_uuid": "vios-1",
    "backup_name": "backup",
    "backup_type": "vios",
    "profile": "lab",
}


@pytest.mark.parametrize(
    "grant_selector",
    [
        {"tools": ["hmc_backup_vios"]},
        {"effects": ["mutate"]},
    ],
    ids=["explicit-tool", "effect"],
)
def test_backup_vios_all_targets_grants_authorize(grant_selector):
    """Named-tool and effect grants can authorize backup only when unbounded."""
    grant = {
        **grant_selector,
        "connections": ["lab"],
        "targets": "all-targets",
    }

    assert _authorize([grant], "hmc_backup_vios", VIOS_BACKUP_ARGUMENTS) is None


def test_backup_vios_named_target_table_is_rejected_with_all_targets_guidance():
    """Required selector metadata does not make SSP backup narrowly grantable."""
    grants = [
        {
            "tools": ["hmc_backup_vios"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"], "vios": ["vios-1"]},
        }
    ]

    with pytest.raises(AccessPolicyError, match="all-targets"):
        _policy(grants)


def test_backup_vios_effect_target_table_denies_before_io(monkeypatch):
    """Effect/table reach fails dispatch authorization before either transport."""
    opened: list[str] = []
    _seal_every_outbound_path(monkeypatch, opened)
    grants = [
        {
            "effects": ["mutate"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"], "vios": ["vios-1"]},
        }
    ]

    application = create_mcp(_policy(grants))
    with pytest.raises(ToolError, match="all-targets"):
        _call(application, "hmc_backup_vios", VIOS_BACKUP_ARGUMENTS)

    assert opened == []


@pytest.mark.parametrize(
    ("tool", "legacy_arguments", "missing_fields"),
    [
        (
            "hmc_backup_vios",
            {
                "vios_name_or_uuid": "vios-1",
                "backup_type": "ssp",
                "profile": "lab",
            },
            ("system_name_or_uuid", "backup_name"),
        ),
        (
            "hmc_restore_vios",
            {
                "vios_name_or_uuid": "vios-1",
                "backup_name": "backup",
                "profile": "lab",
                "system_name_or_uuid": "sys-1",
            },
            ("backup_type",),
        ),
    ],
    ids=["backup", "restore"],
)
def test_legacy_vios_backup_payloads_fail_validation_before_io(
    monkeypatch, tool, legacy_arguments, missing_fields
):
    """Old named payloads omit replacement fields and never reach a handler."""
    opened: list[str] = []
    _seal_every_outbound_path(monkeypatch, opened)
    grants = [
        {
            "tools": [tool],
            "connections": ["lab"],
            "targets": "all-targets",
        }
    ]

    application = create_mcp(_policy(grants))
    with pytest.raises(ToolError) as error:
        _call(application, tool, legacy_arguments)

    message = str(error.value)
    assert all(field in message for field in missing_fields)
    assert opened == []


def test_an_omitted_optional_selector_denies_on_a_destructive_tool():
    """`hmc_power_off_lpar` is the carry-forward's live instance.

    Its `managed_system` selector is optional, so before this change a grant
    reading `lpar = ["victim"]` authorized powering off a partition of that name
    on every system the connection reached.
    """
    grants = [
        {
            "tools": ["hmc_power_off_lpar"],
            "connections": ["lab"],
            "targets": {"lpar": ["victim"], "managed_system": ["sys-1"]},
        }
    ]
    arguments = {
        "lpar_name_or_uuid": "victim",
        "system_name_or_uuid": None,
        "immediate": False,
        "profile": "lab",
    }
    with pytest.raises(TargetScopeError, match="system_name_or_uuid"):
        _authorize(grants, "hmc_power_off_lpar", arguments)

    assert (
        _authorize(
            grants, "hmc_power_off_lpar", {**arguments, "system_name_or_uuid": "sys-1"}
        )
        is None
    )


def test_remote_access_update_is_bound_to_its_console_selector():
    grants = [
        {
            "effects": ["mutate"],
            "connections": ["lab"],
            "targets": {"console": ["console-1"]},
        }
    ]
    targets = TOOL_SECURITY["hmc_configure_remote_access"].targets
    assert {(target.kind, target.argument) for target in targets} == {
        ("console", "console_uuid")
    }
    with pytest.raises(TargetScopeError, match="target"):
        _authorize(
            grants,
            "hmc_configure_remote_access",
            {"console_uuid": "console-2", "values": {}, "profile": "lab"},
        )
    assert (
        _authorize(
            grants,
            "hmc_configure_remote_access",
            {"console_uuid": "console-1", "values": {}, "profile": "lab"},
        )
        is None
    )


@pytest.mark.parametrize(
    "tool",
    ["hmc_create_lpar", "hmc_modify_lpar"],
)
def test_a_table_grant_never_reaches_a_composite_its_selectors_cannot_bound(tool):
    grants = [
        {
            "effects": ["mutate"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"]},
        }
    ]
    with pytest.raises(TargetScopeError, match="all-targets"):
        _authorize(
            grants,
            tool,
            {
                "system_name_or_uuid": "sys-1",
                "lpar_name_or_uuid": "victim",
                "name": "new-lpar",
                "profile": "lab",
            },
        )


ATTACH_DISK_GRANTS = [
    {
        "tools": ["hmc_attach_disk_to_lpar"],
        "connections": ["lab"],
        "targets": {
            "lpar": ["victim"],
            "managed_system": ["sys-1"],
            "vios": ["vios-uuid-1"],
        },
    }
]

ATTACH_DISK_CALL = {
    "lpar_name_or_uuid": "victim",
    "system_name_or_uuid": "sys-1",
    "vios_uuid": "vios-uuid-1",
    "vg_uuid": "vg-uuid-1",
    "disk_name": "data0",
    "capacity_mib": 1024,
    "profile": "lab",
}


def test_a_table_grant_reaches_attach_disk_when_every_selector_matches():
    """#1086: the LPAR, its system, and the VIOS bound every object it touches.

    `vg_uuid` and `disk_name` are addressed inside the declared VIOS, which ADR
    0039 treats as contained. The named-tool grant loading at all is part of the
    assertion: a non-exhaustive tool named beside a table is refused at load.
    """
    assert (
        _authorize(ATTACH_DISK_GRANTS, "hmc_attach_disk_to_lpar", ATTACH_DISK_CALL)
        is None
    )


@pytest.mark.parametrize(
    "override",
    [
        {"lpar_name_or_uuid": "other"},
        {"system_name_or_uuid": "sys-2"},
        {"vios_uuid": "vios-uuid-2"},
    ],
    ids=["lpar", "system", "vios"],
)
def test_attach_disk_denies_any_selector_outside_the_table(override):
    with pytest.raises(TargetScopeError, match="target"):
        _authorize(
            ATTACH_DISK_GRANTS,
            "hmc_attach_disk_to_lpar",
            {**ATTACH_DISK_CALL, **override},
        )


def test_attach_disk_denies_an_omitted_system_under_a_table():
    """The optional system selector (#223): unpinned means any system's `victim`."""
    with pytest.raises(TargetScopeError, match="system_name_or_uuid"):
        _authorize(
            ATTACH_DISK_GRANTS,
            "hmc_attach_disk_to_lpar",
            {**ATTACH_DISK_CALL, "system_name_or_uuid": None},
        )


def test_a_table_grant_still_never_reaches_provision_lpar():
    """The tool stays non-exhaustive, so no table grant reaches it.

    A well-formed call extracts both identities and still denies under
    target-unboundable: `assignments` names vNIC backing VIOSes, DRC indexes and
    SR-IOV ports that no selector declares (#1086). A call whose structured arguments
    are None is malformed rather than narrow: the second extraction rule reads
    it UNREADABLE, which denies under `all-targets` too.
    """
    grants = [
        {
            "effects": ["mutate"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"], "vios": ["vios-uuid-1"]},
        }
    ]
    well_formed = {
        "system_name_or_uuid": "sys-1",
        "name": "new-lpar",
        "adapters": ProvisionAdapters(port_vlan_id=1),
        "storage": ProvisionStorage(vios_uuid="vios-uuid-1", storage_name="rootvg"),
        "profile": "lab",
    }
    with pytest.raises(TargetScopeError, match="all-targets"):
        _authorize(grants, "hmc_provision_lpar", well_formed)

    with pytest.raises(TargetScopeError, match="readable"):
        _authorize(grants, "hmc_provision_lpar", {**well_formed, "storage": None})


def test_all_targets_reaches_everything_the_table_could_not():
    """#225's legacy-equivalent exposure must stay expressible."""
    grants = [
        {
            "effects": ["read", "mutate", "destructive"],
            "connections": ["lab"],
            "targets": "all-targets",
        }
    ]
    assert (
        _authorize(
            grants,
            "hmc_configure_remote_access",
            {"console_uuid": "anything", "values": {}, "profile": "lab"},
        )
        is None
    )
    assert (
        _authorize(
            grants,
            "hmc_power_off_lpar",
            {
                "lpar_name_or_uuid": "anything",
                "system_name_or_uuid": None,
                "immediate": False,
                "profile": "lab",
            },
        )
        is None
    )


# ---------------------------------------------------------------------------
# R14 — a target denial performs no outbound operation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "arguments", "grants"),
    [
        pytest.param(
            "hmc_delete_lpar",
            {"system_name_or_uuid": "sys-2", "lpar_name_or_uuid": "other"},
            LAB_NARROW,
            id="value-outside-the-table",
        ),
        pytest.param(
            "hmc_power_off_lpar",
            {"lpar_name_or_uuid": "victim"},
            [
                {
                    "tools": ["hmc_power_off_lpar"],
                    "connections": ["lab"],
                    "targets": {"lpar": ["victim"], "managed_system": ["sys-1"]},
                }
            ],
            id="omitted-optional-selector",
        ),
    ],
)
def test_a_target_denial_opens_no_hmc_connection(
    monkeypatch, tool_name, arguments, grants
):
    """Asserted on client construction, not on the return value.

    A denial that returned the right error while still resolving an HMCConfig,
    opening a socket, or running an SSH command would satisfy the type signature
    and defeat the purpose. Every binding a handler could reach an HMC through is
    sealed first, including the copies each `server_*` module rebound at import.
    """
    opened: list[str] = []
    _seal_every_outbound_path(monkeypatch, opened)

    application = create_mcp(_policy(grants))
    with pytest.raises(ToolError):
        _call(application, tool_name, {**arguments, "profile": "lab"})

    assert opened == []


# ---------------------------------------------------------------------------
# R15 — the denial names the dimension that blocked
# ---------------------------------------------------------------------------


def test_a_withheld_connection_still_raises_the_connection_error():
    """ADR 0038's behaviour is unchanged when no grant matches the connection."""
    with pytest.raises(ConnectionScopeError):
        _authorize(LAB_NARROW, "hmc_delete_lpar", _delete(profile="prod"))


def test_a_matching_connection_with_a_withheld_target_raises_the_target_error():
    with pytest.raises(TargetScopeError):
        _authorize(LAB_NARROW, "hmc_delete_lpar", _delete(system="sys-2"))


@pytest.mark.parametrize(
    "tool",
    ["hmc_plan_lpar_memopt_scores", "hmc_plan_system_memopt_score"],
)
def test_affinity_scenario_lpars_are_not_authorization_targets(tool):
    arguments = {
        "system_name_or_uuid": "sys-1",
        "prioritized": {"names": ["not-in-lpar-grant"]},
        "excluded": {"ids": [99]},
        "profile": "lab",
    }
    grants = [
        {
            "tools": [tool, "hmc_get_lpar_memopt_score"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"], "lpar": ["other"]},
        }
    ]

    assert _authorize(grants, tool, arguments) is None
    with pytest.raises(TargetScopeError):
        _authorize(
            [
                {
                    **grants[0],
                    "targets": {"managed_system": ["sys-2"], "lpar": ["other"]},
                }
            ],
            tool,
            arguments,
        )


def test_the_message_selection_cannot_change_the_decision():
    """A call the split grants *do* cover is permitted, not merely mis-labelled.

    `connection_matched` is assigned inside the loop and read only after it. If
    it were consulted as part of the decision, this call — which grant 0 covers
    whole — could not still be permitted while the two tests above deny.
    """
    assert _authorize(SPLIT_GRANTS, "hmc_delete_lpar", _delete()) is None


# ---------------------------------------------------------------------------
# R16 — the denial discloses nothing
# ---------------------------------------------------------------------------


def test_a_target_denial_leaks_nothing_to_the_mcp_client():
    """On the real transport, since whether `__cause__` reaches the client is
    fastmcp's decision and a dependency bump could change it."""
    configuration = config_dir() / "config.toml"
    application = create_mcp(_policy(LAB_NARROW))
    with pytest.raises(ToolError) as error:
        _call(
            application,
            "hmc_delete_lpar",
            {
                "system_name_or_uuid": "sys-2",
                "lpar_name_or_uuid": "other",
                "profile": "lab",
            },
        )
    message = str(error.value)

    assert "hmc_delete_lpar" in message
    assert "'lab-only'" in message
    # The caller's own values come back; nothing else does.
    assert "sys-2" in message and "other" in message
    for secret in (
        "lab-pass",
        "prod-pass",
        "lab-hmc.test",
        "prod-hmc.test",
        "hscroot",
        str(configuration),
        str(configuration.parent),
        "sys-1",
        "victim",
        SOURCE,
    ):
        assert secret not in message, secret


def test_a_denial_message_is_one_line():
    """A control character in a selector cannot forge a line in an operator log."""
    application = create_mcp(_policy(LAB_NARROW))
    with pytest.raises(ToolError) as error:
        _call(
            application,
            "hmc_delete_lpar",
            {
                "system_name_or_uuid": "sys-1\nALLOWED: everything",
                "lpar_name_or_uuid": "victim",
                "profile": "lab",
            },
        )
    assert "\n" not in str(error.value)


# ---------------------------------------------------------------------------
# R17 — dry_run is never an authorization input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("system", ["sys-1", "sys-2"])
def test_dry_run_changes_no_authorization_decision(dry_run, system):
    """The flag is a client claim. It never widens a grant and never narrows one.

    Epic #218 goal 7: an ordinary tool argument must never select or widen the
    server's access policy. A `dry_run` that downgraded the effect class would be
    exactly that, and the server could only verify the claim by running the
    handler it is deciding whether to run.
    """
    grants = [
        {
            "tools": ["hmc_decommission_lpar"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"], "lpar": ["victim"]},
        }
    ]
    arguments = {
        "system_name_or_uuid": system,
        "lpar_name_or_uuid": "victim",
        "dry_run": dry_run,
        "profile": "lab",
    }
    if system == "sys-1":
        assert _authorize(grants, "hmc_decommission_lpar", arguments) is None
    else:
        with pytest.raises(TargetScopeError):
            _authorize(grants, "hmc_decommission_lpar", arguments)


def test_the_decision_modules_never_mention_dry_run():
    """R17 structurally: the three modules that decide contain no such reference.

    A grep rather than a behaviour test, because the property is "this is never
    read", and no call can demonstrate the absence of a branch.
    """
    from pathlib import Path

    from hmcpctl.authorization import connection_scope, dispatch_scope, target_scope

    for module in (dispatch_scope, target_scope, connection_scope):
        source = Path(module.__file__).read_text(encoding="utf-8")
        # The word appears in prose explaining why it is not read; the check is
        # that no *code* line references it.
        code = [
            line
            for line in source.splitlines()
            if "dry_run" in line and not line.lstrip().startswith("#")
        ]
        assert not code, f"{module.__name__}: {code}"


# ---------------------------------------------------------------------------
# A second argument that overrides a declared selector (threat-scan finding)
# ---------------------------------------------------------------------------


def test_a_second_argument_cannot_override_the_authorized_selector():
    """`hmc_get_job` declares `job_id` and then lets `job_href` replace it.

    `client.get_job_entry` fetches `urlparse(job_href).path` and never reads
    `job_id`, so a table grant would authorize one job identity while the
    server reads another — the exact escape `exhaustive_targets` exists to
    refuse, reached through an argument the *name* tables did not know about.

    Both job tools are therefore unbounded, and a table grant refuses them
    whether or not `job_href` is passed: authorization must not depend on which
    optional argument a caller chose to send, or the check becomes a race with
    the caller.
    """
    grants = [
        {
            "tools": ["hmc_get_job"],
            "connections": ["lab"],
            "targets": {"job": ["job-1111"]},
        }
    ]
    # Naming it beside a table is refused at load, so the grant cannot exist.
    with pytest.raises(Exception, match="all-targets"):
        _policy(grants)

    # Reached through an effect class instead, the call is refused at dispatch —
    # with and without the overriding argument.
    effect_grant = [
        {"effects": ["read"], "connections": ["lab"], "targets": {"job": ["job-1111"]}}
    ]
    for href in (None, "https://elsewhere.invalid/rest/api/uom/jobs/job-9999"):
        with pytest.raises(TargetScopeError, match="all-targets"):
            _authorize(
                effect_grant,
                "hmc_get_job",
                {"job_id": "job-1111", "job_href": href, "profile": "lab"},
            )

    # `all-targets` still reaches it, so #225's legacy exposure is unaffected.
    wide = [{"effects": ["read"], "connections": ["lab"], "targets": "all-targets"}]
    assert (
        _authorize(
            wide,
            "hmc_get_job",
            {"job_id": "job-1111", "job_href": None, "profile": "lab"},
        )
        is None
    )


def test_a_read_tool_is_denied_on_a_target_outside_the_table():
    """R3 on the live registry: the dimension binds reads, not only mutations.

    Issue #223's title says "mutations". ADR 0039 widens that deliberately —
    `hmc_get_lpar` against a withheld partition returns its configuration, which
    is the disclosure the epic exists to bound — and this is the behavioural
    test for the widening.
    """
    grants = [
        {
            "effects": ["read"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"], "lpar": ["victim"]},
        }
    ]
    # #259 case 1 closed: `hmc_get_lpar` now declares a `managed_system`
    # selector beside `lpar_name_or_uuid`, so the system is pinned too.
    assert [t.argument for t in TOOL_SECURITY["hmc_get_lpar"].targets] == [
        "lpar_name_or_uuid",
        "system_name_or_uuid",
    ]
    args = {
        "lpar_name_or_uuid": "secret-db",
        "system_name_or_uuid": "sys-1",
        "profile": "lab",
    }
    with pytest.raises(TargetScopeError):
        _authorize(grants, "hmc_get_lpar", args)

    assert (
        _authorize(grants, "hmc_get_lpar", {**args, "lpar_name_or_uuid": "victim"})
        is None
    )


def test_an_unpinned_list_call_is_denied():
    """ "Every partition on every system" is not what a narrow table granted."""
    grants = [
        {
            "effects": ["read"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"]},
        }
    ]
    with pytest.raises(TargetScopeError, match="system_name_or_uuid"):
        _authorize(
            grants, "hmc_list_lpars", {"system_name_or_uuid": None, "profile": "lab"}
        )
    assert (
        _authorize(
            grants, "hmc_list_lpars", {"system_name_or_uuid": "sys-1", "profile": "lab"}
        )
        is None
    )


def test_a_fleet_ambiguous_read_is_denied_without_its_system():
    """#259 case 1 at the boundary on a tool that previously took no system.

    Before the change `hmc_get_lpar` declared only `lpar_name_or_uuid`, so
    `lpar = ["victim"]` matched that name on every system the connection
    reached. Now the omitted selector is ABSENT and denies, exactly as it
    always did on `hmc_power_off_lpar`.
    """
    grants = [
        {
            "tools": ["hmc_get_lpar"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"], "lpar": ["victim"]},
        }
    ]
    arguments = {
        "lpar_name_or_uuid": "victim",
        "system_name_or_uuid": None,
        "profile": "lab",
    }
    with pytest.raises(TargetScopeError, match="system_name_or_uuid"):
        _authorize(grants, "hmc_get_lpar", arguments)
    with pytest.raises(TargetScopeError):
        _authorize(
            grants, "hmc_get_lpar", {**arguments, "system_name_or_uuid": "sys-2"}
        )
    assert (
        _authorize(
            grants, "hmc_get_lpar", {**arguments, "system_name_or_uuid": "sys-1"}
        )
        is None
    )


def test_migrate_denies_when_the_source_system_is_outside_the_table():
    """#259 case 2 at the boundary: one allowlist entry covers both endpoints.

    A grant naming `{ lpar = ["db-01"], managed_system = ["S2"] }` used to
    authorize evacuating db-01 off any source system into S2, because the
    source was not a declared selector at all. The source now arrives as its
    own `managed_system` selector and must match the same allowlist.
    """
    grants = [
        {
            "tools": ["hmc_migrate_lpar"],
            "connections": ["lab"],
            "targets": {"lpar": ["db-01"], "managed_system": ["S2"]},
        }
    ]
    arguments = {
        "lpar_name_or_uuid": "db-01",
        "target_system_name_or_uuid": "S2",
        "system_name_or_uuid": "S1",
        "profile": "lab",
    }
    with pytest.raises(TargetScopeError):
        _authorize(grants, "hmc_migrate_lpar", arguments)
    with pytest.raises(TargetScopeError, match="system_name_or_uuid"):
        _authorize(
            grants, "hmc_migrate_lpar", {**arguments, "system_name_or_uuid": None}
        )
    assert (
        _authorize(
            grants, "hmc_migrate_lpar", {**arguments, "system_name_or_uuid": "S2"}
        )
        is None
    )


def test_swapping_two_selector_values_between_their_kinds_denies():
    """Selector confusion on the live registry, not only on a fixture."""
    grants = [
        {
            "tools": ["hmc_delete_lpar"],
            "connections": ["lab"],
            "targets": {"managed_system": ["sys-1"], "lpar": ["victim"]},
        }
    ]
    with pytest.raises(TargetScopeError):
        _authorize(grants, "hmc_delete_lpar", _delete(system="victim", lpar="sys-1"))


def test_profile_backup_delete_requires_all_targets_before_io(monkeypatch):
    opened: list[str] = []
    _seal_every_outbound_path(monkeypatch, opened)
    application = create_mcp(
        _policy(
            [
                {
                    "effects": ["destructive"],
                    "connections": ["lab"],
                    "targets": {"managed_system": ["sys-1"]},
                }
            ]
        )
    )
    with pytest.raises(ToolError, match="all-targets"):
        _call(
            application,
            "hmc_delete_backup_lpar_profile",
            {
                "system_name_or_uuid": "sys-1",
                "file_name": "backup-1",
                "profile": "lab",
            },
        )
    assert opened == []


def test_profile_backup_list_denies_cross_system_before_io(monkeypatch):
    opened: list[str] = []
    _seal_every_outbound_path(monkeypatch, opened)
    application = create_mcp(
        _policy(
            [
                {
                    "tools": ["hmc_list_backup_lpar_profiles"],
                    "connections": ["lab"],
                    "targets": {"managed_system": ["sys-1"]},
                }
            ]
        )
    )
    with pytest.raises(ToolError, match="not permitted"):
        _call(
            application,
            "hmc_list_backup_lpar_profiles",
            {
                "system_name_or_uuid": "sys-2",
                "profile": "lab",
            },
        )
    assert opened == []
