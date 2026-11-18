"""hmc_reconfigure_lpar over MCP under real access policies (#1226, ADR 0189, ADR 0314)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from unit.test_lpar_reconfigure import LPAR, SYS, FakeHMC
from unit.test_lpar_reconfigure import hmc as hmc  # noqa: PLC0414 - fixture import
from unit.test_lpar_reconfigure_media import (
    MEDIA,
    VIOS,
    MediaHMC,
    _ledger,
    _provisioned,
)
from unit.test_lpar_reconfigure_media import media as media  # noqa: PLC0414 - fixture

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.operations.logical import engine, holds, store
from hmcpctl.operations.lpar.reconfiguration import core
from hmcpctl.resource_identity import guard_partition
from hmcpctl.server import PRIMARY_TOOLS, TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import reconfigure as handler

TOOL = "hmc_reconfigure_lpar"
PROFILE_WRITE = "hmc_update_partition_profile"
MEM, PROC = "hmc_dlpar_mem", "hmc_dlpar_proc"
MODIFY, RENAME = "hmc_modify_lpar", "hmc_rename_lpar"
UNMOUNT, BOOT = "hmc_unmount_optical_media", "hmc_set_lpar_boot_order"
NEW = {"lpar_name_or_uuid": "web1", "system_name_or_uuid": "sysA"}
MEMORY = {**NEW, "patch": {"memory_mib": 1024}, "exclusive_writer_window": True}
DETACH = {
    **NEW,
    "patch": {"installer_media": "detach"},
    "exclusive_writer_window": True,
}


@pytest.fixture(autouse=True)
def environment_only_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep build_config off a developer's own TOML profiles (AGENTS.md, HMC_* leaks)."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.delenv("HMC_AGENT_ID", raising=False)


def _app(*grants: dict) -> FastMCP:
    policy = compile_access_policy(
        {"policies": {"test": {"grants": list(grants)}}},
        "test",
        TOOL_SECURITY,
        "test-lpar-reconfigure.toml",
    )
    return create_mcp(policy)


def _grant(*tools: str, targets: Any = "all-targets") -> dict:
    return {"tools": list(tools), "connections": ["<default>"], "targets": targets}


def _call(app: FastMCP, arguments: dict) -> dict:
    async def go() -> dict:
        async with Client(app) as client:
            result = await client.call_tool(
                TOOL, {"wait_seconds": 10, "request_id": "r1", **arguments}
            )
            assert result.structured_content is not None
            return result.structured_content

    return asyncio.run(go())


def test_tool_is_listed_destructive_and_primary():
    security = TOOL_SECURITY[TOOL]
    assert (security.effect, security.target_kind) == ("destructive", "console")
    assert TOOL in PRIMARY_TOOLS

    async def listed() -> Any:
        async with Client(_app(_grant(TOOL))) as client:
            return {tool.name: tool for tool in await client.list_tools()}[TOOL]

    tool = asyncio.run(listed())
    assert tool.annotations is not None and tool.annotations.destructive_hint is True
    schema = tool.input_schema["properties"]
    assert {"patch", "allow_disruption", "exclusive_writer_window"} <= set(schema)


def test_memory_change_runs_with_its_delegated_authority(hmc: FakeHMC):
    record = _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), MEMORY)
    assert (record["state"], record["outcome"]) == ("terminal", "completed")
    assert record["result"]["changes"][0]["applied"] == "live"
    assert record["partition_uuid"] == LPAR


def test_memory_without_the_writer_window_is_refused_before_recording(hmc: FakeHMC):
    arguments = {**MEMORY, "exclusive_writer_window": False}
    with pytest.raises(ToolError, match="exclusive_writer_window"):
        _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), arguments)
    with pytest.raises(ToolError, match="no operation has request_id"):
        _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), {"continuation": "resume"})
    assert hmc.calls == []


@pytest.mark.parametrize(
    ("patch", "grants", "missing"),
    [
        ({"memory_mib": 1024}, (PROFILE_WRITE,), MEM),
        ({"memory_mib": 1024}, (MEM,), PROFILE_WRITE),
        ({"processors": 3}, (MEM, PROFILE_WRITE), PROC),
        ({"name": "web2"}, (MODIFY,), RENAME),
        ({"name": "web2"}, (RENAME,), MODIFY),
    ],
)
def test_each_field_needs_its_delegated_tools(hmc: FakeHMC, patch, grants, missing):
    arguments = {**NEW, "patch": patch, "exclusive_writer_window": True}
    with pytest.raises(ToolError, match=missing):
        _call(_app(_grant(TOOL, *grants)), arguments)
    assert hmc.calls == []


def test_rename_alone_needs_no_profile_authority_or_window(hmc: FakeHMC):
    record = _call(
        _app(_grant(TOOL, MODIFY, RENAME)), {**NEW, "patch": {"name": "web2"}}
    )
    assert record["outcome"] == "completed"
    assert hmc.name == "web2"


def test_a_target_the_specialist_grant_excludes_is_denied(hmc: FakeHMC):
    app = _app(
        _grant(TOOL, PROFILE_WRITE),
        _grant(MEM, targets={"lpar": ["other"], "managed_system": ["sysA"]}),
    )
    with pytest.raises(ToolError, match=MEM):
        _call(app, MEMORY)
    assert hmc.calls == []


def test_resume_is_authorized_from_the_record(hmc: FakeHMC):
    hmc.lag = True
    first = _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), MEMORY)
    assert first["outcome"] == "needs_attention"
    with pytest.raises(ToolError, match=MEM):
        _call(_app(_grant(TOOL, PROFILE_WRITE)), {"continuation": "resume"})
    hmc.lag, hmc.memory[1] = False, 1024
    done = _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), {"continuation": "resume"})
    assert (done["state"], done["outcome"]) == ("terminal", "completed")


def test_a_new_operation_needs_its_selectors_and_patch(hmc: FakeHMC):
    with pytest.raises(ToolError, match="patch"):
        _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), NEW)


def test_an_unknown_patch_field_is_refused(hmc: FakeHMC):
    arguments = {**MEMORY, "patch": {"memory_mib": 1024, "boot_mode": "Normal"}}
    with pytest.raises(ToolError):
        _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), arguments)
    assert hmc.calls == []


def test_detach_without_the_writer_window_is_refused(media: MediaHMC):
    _provisioned()
    with pytest.raises(ToolError, match="exclusive_writer_window"):
        _call(_app(_grant(TOOL, UNMOUNT)), {**DETACH, "exclusive_writer_window": False})
    assert media.mounted == [MEDIA]


def test_detach_runs_under_an_all_targets_unmount_grant(media: MediaHMC):
    _provisioned()
    record = _call(_app(_grant(TOOL, UNMOUNT)), DETACH)
    assert (record["state"], record["outcome"]) == ("terminal", "completed")
    assert media.mounted == []


def _targets(vios=VIOS, *, system="sysA", lpar="web1"):
    return {"lpar": [lpar], "managed_system": [system], "vios": [vios]}


def test_detach_binds_the_admitted_partition_and_medium(media: MediaHMC):
    _provisioned()
    app = _app(_grant(TOOL), _grant(UNMOUNT, targets=_targets()))
    record = _call(app, DETACH)
    assert record["outcome"] == "completed"
    assert media.mounted == []
    binding = next(e for e in record["effects"] if e["key"] == "admitted_media")
    assert binding["identity"] == {
        "system_uuid": SYS,
        "lpar_uuid": LPAR,
        "vios_uuid": VIOS,
        "media_name": MEDIA,
    }


@pytest.mark.parametrize(
    "grants",
    [
        [_grant(UNMOUNT, targets=_targets("vios1"))],
        [
            _grant(UNMOUNT, targets=_targets(lpar="other")),
            _grant(UNMOUNT, targets=_targets(system="other")),
        ],
    ],
)
def test_unmatched_grants_refuse_before_admission(media: MediaHMC, grants):
    _provisioned()
    arguments = {**DETACH, "patch": {"installer_media": "detach", "name": "web2"}}
    with pytest.raises(ToolError, match=UNMOUNT) as exc:
        _call(_app(_grant(TOOL, MODIFY, RENAME), *grants), arguments)
    assert VIOS not in str(exc.value)
    assert media.mounted == [MEDIA] and media.fake.name == "web1"
    with store.read_session() as conn:
        assert conn is not None and store.find_operation(conn, "hmcpctl", "r1") is None


def _suspend_before_worker(app, monkeypatch, arguments=DETACH):
    with monkeypatch.context() as start:
        start.setattr(engine, "_start_worker", lambda *_args: None)
        return _call(app, {**arguments, "wait_seconds": 0})


def _replace_ledger_media(name):
    with store.session() as conn, store.write_transaction(conn):
        conn.execute("DELETE FROM ledger WHERE kind = 'installer_media'")
    if name is not None:
        _ledger(
            key="installer_media",
            kind="installer_media",
            identity={"vios_uuid": VIOS, "media_name": name},
        )


def test_specialist_connection_denial_precedes_identity_lookup(media: MediaHMC):
    grant = {**_grant(UNMOUNT, targets=_targets()), "connections": ["other"]}
    with pytest.raises(ToolError, match=UNMOUNT):
        _call(_app(_grant(TOOL), grant), DETACH)
    assert media.fake.calls == []
    with store.read_session() as conn:
        assert conn is None or store.find_operation(conn, "hmcpctl", "r1") is None


def test_specialist_must_authorize_the_connection_bound_to_the_operation(
    media, monkeypatch
):
    _provisioned()
    monkeypatch.setattr(handler, "connection_label", lambda *_args, **_kwargs: "other")
    with pytest.raises(ToolError, match="connection changed"):
        _call(_app(_grant(TOOL, UNMOUNT)), DETACH)
    assert media.fake.calls == [] and media.mounted == [MEDIA]


@pytest.mark.parametrize("bad", ["missing", "ambiguous", "malformed"])
def test_unusable_ledger_refuses_without_disclosing_identity(media: MediaHMC, bad):
    _provisioned(media=False)
    if bad != "missing":
        _ledger(
            key="installer_media",
            kind="installer_media",
            identity={
                "vios_uuid": VIOS if bad == "ambiguous" else "alias",
                "media_name": MEDIA,
            },
        )
    if bad == "ambiguous":
        _ledger(
            key="another",
            kind="installer_media",
            identity={"vios_uuid": VIOS, "media_name": MEDIA},
        )
    with pytest.raises(ToolError, match="check the original request") as exc:
        _call(_app(_grant(TOOL, UNMOUNT)), DETACH)
    assert all(value not in str(exc.value) for value in (LPAR, VIOS, MEDIA, "entries"))
    assert media.mounted == [MEDIA]
    with store.read_session() as conn:
        assert conn is not None and store.find_operation(conn, "hmcpctl", "r1") is None


def test_held_partition_refuses_detach_without_disclosing_hold(media, monkeypatch):
    _provisioned()
    holds.place_hold(
        SYS,
        LPAR,
        consumer_label="fixture-owner",
        agent_id="fixture-agent",
        connection="<default>",
    )

    async def resolve(_hmc, _lpar, **_kwargs):
        guard_partition(LPAR)
        return LPAR

    monkeypatch.setattr(core, "resolve_and_authorize_lpar_mutation", resolve)
    with pytest.raises(ToolError, match="check the original request") as exc:
        _call(_app(_grant(TOOL), _grant(UNMOUNT, targets=_targets())), DETACH)
    assert all(
        value not in str(exc.value)
        for value in (LPAR, "fixture-owner", "fixture-agent")
    )
    assert media.mounted == [MEDIA]
    with store.read_session() as conn:
        assert conn is not None and store.find_operation(conn, "hmcpctl", "r1") is None


@pytest.mark.parametrize("arguments", [DETACH, MEMORY])
def test_continuation_binds_the_record_used_to_authorize_its_patch(
    media, monkeypatch, arguments
):
    _provisioned()
    app = _app(_grant(TOOL, MEM, PROFILE_WRITE, UNMOUNT))
    first = _call(app, arguments)
    recorded = engine.recorded_request("hmcpctl", "r1", TOOL)
    assert recorded is not None and recorded[0] == first["operation_id"]
    monkeypatch.setattr(engine, "recorded_request", lambda *_: ("0" * 32, recorded[1]))
    with pytest.raises(
        ToolError, match="changed during admission|check the original request"
    ):
        _call(app, {"continuation": "resume"})
    with store.read_session() as conn:
        assert conn is not None
        assert store.read_record(conn, first["operation_id"]).outcome == "completed"


def test_crash_before_guard_resumes_only_the_admitted_medium(media, monkeypatch):
    _provisioned()
    app = _app(_grant(TOOL, MODIFY, RENAME), _grant(UNMOUNT, targets=_targets()))
    mixed = {**DETACH, "patch": {"name": "web2", "installer_media": "detach"}}
    first = _suspend_before_worker(app, monkeypatch, mixed)
    assert first["partition_uuid"] is None
    _replace_ledger_media("changed.iso")
    with pytest.raises(ToolError, match="verify"):
        _call(app, {"continuation": "resume"})
    assert media.fake.name == "web1" and media.mounted == [MEDIA]
    _replace_ledger_media(MEDIA)
    done = _call(app, {"continuation": "resume"})
    assert done["outcome"] == "completed"
    assert media.fake.name == "web2" and media.mounted == []


@pytest.mark.parametrize("action", ["replay", "abandon"])
def test_journal_only_refuses_conflict_and_uses_remaining_local_evidence(
    media, monkeypatch, action
):
    _provisioned()
    app = _app(_grant(TOOL), _grant(UNMOUNT, targets=_targets()))
    first = _suspend_before_worker(app, monkeypatch)
    arguments = DETACH if action == "replay" else {"continuation": "abandon"}

    def unexpected_client(*_args):
        pytest.fail("journal-only detach opened an HMC client")

    monkeypatch.setattr(core, "open_client", unexpected_client)
    _replace_ledger_media("changed.iso")
    with pytest.raises(ToolError, match="verify"):
        _call(app, arguments)
    _replace_ledger_media(None)
    record = _call(app, arguments)
    assert record["operation_id"] == first["operation_id"]
    assert record["outcome"] == ("abandoned" if action == "abandon" else None)
    assert media.mounted == [MEDIA]


def test_historical_journal_with_no_evidence_requires_all_targets(media, monkeypatch):
    _provisioned()
    scoped = _app(_grant(TOOL), _grant(UNMOUNT, targets=_targets()))
    first = _suspend_before_worker(scoped, monkeypatch)
    with store.session() as conn, store.write_transaction(conn):
        conn.execute(
            "DELETE FROM effects WHERE operation_id=?", (first["operation_id"],)
        )
    _replace_ledger_media(None)
    with pytest.raises(ToolError, match=UNMOUNT):
        _call(scoped, {"continuation": "abandon"})
    broad = _app(_grant(TOOL, UNMOUNT))
    with pytest.raises(ToolError, match="verify"):
        _call(broad, {"continuation": "resume"})
    assert _call(broad, DETACH)["operation_id"] == first["operation_id"]
    assert _call(broad, {"continuation": "abandon"})["outcome"] == "abandoned"
    assert media.mounted == [MEDIA]


def test_wrong_host_cannot_replay_bound_detach(media, monkeypatch):
    _provisioned()
    app = _app(_grant(TOOL, UNMOUNT))
    _suspend_before_worker(app, monkeypatch)
    monkeypatch.setenv("HMC_HOST", "other.test")
    with pytest.raises(ToolError, match="verify"):
        _call(app, DETACH)
    assert media.mounted == [MEDIA]


def test_recorded_plan_must_match_binding_before_resume(media, monkeypatch):
    _provisioned()
    app = _app(_grant(TOOL), _grant(UNMOUNT, targets=_targets()))
    first = _call(app, DETACH)
    with store.session() as conn, store.write_transaction(conn):
        plan = store.load_effects(conn, first["operation_id"])["plan"].identity
        assert plan is not None
        plan["media"]["media_name"] = "changed.iso"
        conn.execute(
            "UPDATE effects SET identity_json=? WHERE operation_id=? AND key='plan'",
            (json.dumps(plan), first["operation_id"]),
        )
        store.set_state(conn, first["operation_id"], "paused", "needs_attention")
    with pytest.raises(ToolError, match="verify"):
        _call(app, {"continuation": "resume"})
    assert media.fake.calls.count("unmount") == 1


@pytest.mark.parametrize(
    ("patch", "grants", "missing"),
    [({"installer_media": "detach"}, (), UNMOUNT), ({"boot": "root_disk"}, (), BOOT)],
)
def test_media_and_boot_need_their_delegated_tools(
    media: MediaHMC, patch, grants, missing
):
    arguments = {**NEW, "patch": patch, "exclusive_writer_window": True}
    with pytest.raises(ToolError, match=missing):
        _call(_app(_grant(TOOL, *grants)), arguments)


def test_boot_alone_needs_no_writer_window(media: MediaHMC):
    _provisioned()
    record = _call(_app(_grant(TOOL, BOOT)), {**NEW, "patch": {"boot": "root_disk"}})
    assert record["outcome"] == "completed"
    assert media.pending is not None
