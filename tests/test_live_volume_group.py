"""ST42 guard and recovery behavior against an offline VIOS model."""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from hmcpctl.config import HMCConfig

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import artifacts as live_artifacts
from live_test import config as live_config
from live_test import runner
from live_test.arms import storage_volume_group as vg
from live_test.config import hmc_identity

PV = "hdisk9"
PVID = "0000000000000009"
GROUP = "hpvg1234abcd"
BEFORE = {PV: (PVID, "None"), "hdisk0": ("0000000000000000", "rootvg")}
FREE = {PV: (PVID, "100000")}
GROUPS = frozenset({"rootvg"})


@pytest.mark.parametrize(
    "text,free",
    [
        (None, False),
        ("error: command refused", False),
        ("hdisk9:x:None", False),
        (f"{PV}:{PVID}:None\n{PV}:{PVID}:None", False),
        (f"{PV}:{PVID}:vg;exit", False),
        (f"{PV}:{PVID}:0", True),
        (f"{PV}:{PVID}:-1", True),
        (f"{PV}:{PVID}:N/A", True),
    ],
)
def test_unreadable_inventory_stops(text, free):
    assert vg.physical_volumes(text, free=free) is None


def test_delimited_inventory_preserves_identity():
    assert vg.physical_volumes(f"{PV}:{PVID}:None\n") == {PV: (PVID, "None")}
    assert vg.physical_volumes(f"{PV}:{PVID}:100000\n", free=True) == FREE
    assert vg.create_guard(PV, GROUP, BEFORE, FREE, GROUPS)


@pytest.mark.parametrize(
    "pv,group,inventory,free,groups",
    [
        ("", GROUP, BEFORE, FREE, GROUPS),
        (PV, "", BEFORE, FREE, GROUPS),
        ("hdisk8", GROUP, BEFORE, FREE, GROUPS),
        (PV, "rootvg", BEFORE, FREE, GROUPS),
        (PV, GROUP, BEFORE, {}, GROUPS),
        (PV, GROUP, BEFORE, None, GROUPS),
        (PV, GROUP, {PV: (PVID, "datavg")}, FREE, GROUPS),
        (PV, GROUP, BEFORE, {PV: ("0000000000000099", "100000")}, GROUPS),
        (PV, GROUP, BEFORE, FREE, GROUPS | {GROUP}),
        (PV, GROUP, None, FREE, GROUPS),
        (PV, GROUP, BEFORE, FREE, None),
        ("hdisk9;exit", GROUP, BEFORE, FREE, GROUPS),
    ],
)
def test_create_guard_refuses_unknown_or_unsafe_selection(
    pv, group, inventory, free, groups
):
    assert not vg.create_guard(pv, group, inventory, free, groups)


@pytest.mark.parametrize(
    "group,current,members,volumes",
    [
        ("rootvg", dict(BEFORE, hdisk9=(PVID, GROUP)), {PV}, set()),
        (GROUP, dict(BEFORE, hdisk9=("none", GROUP)), {PV}, set()),
        (GROUP, dict(BEFORE, hdisk9=(PVID, GROUP)), {PV, "hdisk8"}, set()),
        (GROUP, dict(BEFORE, hdisk9=(PVID, GROUP)), {PV}, {"op-disk"}),
        (GROUP, None, {PV}, set()),
        (GROUP, {PV: (PVID, GROUP)}, {PV}, set()),
    ],
)
def test_cleanup_guard_refuses_destructive_or_unreadable_state(
    group, current, members, volumes
):
    assert not vg.cleanup_guard(
        PV, group, BEFORE, current, frozenset(members), frozenset(volumes)
    )


class FakeVIOS:
    def __init__(self):
        self.inventory = dict(BEFORE)
        self.free = dict(FREE)
        self.groups = {"rootvg"}
        self.calls = []
        self.create_status = "PASS"
        self.create_effect = True
        self.cleanup_effect = True
        self.post_readable = True
        self.extra_member = False
        self.lv = False
        self.pvid_lost = False
        self.size_drift = False

    async def call(self, _client, tool, **kwargs):
        self.calls.append((tool, kwargs))
        created = any(name == "hmc_create_volume_group" for name, _ in self.calls)
        if tool == "hmc_list_volume_groups":
            if created and not self.post_readable:
                return "FAIL", None
            return "PASS", [
                {"name": name, "uuid": "vg-uuid"} for name in sorted(self.groups)
            ]
        if tool == "hmc_create_volume_group":
            assert kwargs["physical_volumes"] == [PV]
            assert kwargs["name"] == GROUP
            assert kwargs["vios_name_or_uuid"] == "vios-A"
            assert kwargs["system_name_or_uuid"] == "sys-A"
            if self.create_effect:
                self.groups.add(GROUP)
                self.free.pop(PV)
                self.inventory[PV] = ("none" if self.pvid_lost else PVID, GROUP)
            return self.create_status, {}
        assert tool == "hmc_run_command"
        cmd = shlex.split(kwargs["cmd"])[-1]
        if cmd == "lspv -field pvname pvid vgname -fmt :":
            return "PASS", "".join(
                f"{name}:{pvid}:{group}\n"
                for name, (pvid, group) in sorted(self.inventory.items())
            )
        if cmd == "lspv -free -field pvname pvid size -fmt :":
            return "PASS", "".join(
                f"{name}:{pvid}:{size}\n"
                for name, (pvid, size) in sorted(self.free.items())
            ) or ":"
        if cmd == "lsvg":
            return "PASS", "".join(f"{name}\n" for name in sorted(self.groups))
        if cmd == f"lsvg -pv {GROUP} -field pvname -fmt :":
            return "PASS", PV + ("\nhdisk8" if self.extra_member else "") + "\n"
        if cmd == f"lsvg -lv {GROUP}":
            return (
                "PASS",
                f"{GROUP}:\nLV NAME TYPE LPs PPs PVs LV STATE MOUNT POINT\n"
                + ("op-disk jfs2 1 1 1 open/syncd N/A\n" if self.lv else ""),
            )
        assert cmd == f"reducevg {GROUP} {PV}"
        if self.cleanup_effect:
            self.groups.remove(GROUP)
            self.inventory[PV] = (PVID, "None")
            self.free[PV] = (PVID, "99999" if self.size_drift else "100000")
            return "PASS", "Volume group removed"
        return "FAIL", "cleanup refused"


@pytest.fixture
def scratch(monkeypatch):
    config = replace(
        live_config.LiveTestConfig(),
        system_name="sys-A",
        scratch_pv_name=PV,
        scratch_vg_name=GROUP,
    )
    state = runner.RunState(config=config, group="storage")
    state.artifacts.vios_uuid = "vios-A"
    state.artifacts.vios_partition_id = 1
    fake = FakeVIOS()
    monkeypatch.setattr(runner.RunState, "call", fake.call)
    return state, fake


def mutations(fake):
    return [
        (tool, args)
        for tool, args in fake.calls
        if tool == "hmc_create_volume_group" or "reducevg " in str(args.get("cmd", ""))
    ]


def observation(state):
    assert len(state.observations) == 1
    return state.observations[0]["observation"]


@pytest.mark.asyncio
async def test_roundtrip_creates_once_and_restores_snapshot(scratch):
    state, fake = scratch
    await vg.exercise_volume_group(None, state)
    assert fake.inventory == BEFORE and fake.free == FREE and fake.groups == set(GROUPS)
    assert len(mutations(fake)) == 2
    assert state.artifacts.storage_volume_group_name is None
    assert observation(state)["result"] == "passed"
    assert observation(state)["cleanup"] == "passed"


@pytest.mark.parametrize(
    "fields",
    [
        {"scratch_pv_name": ""},
        {"scratch_vg_name": ""},
        {"scratch_pv_name": "hdisk8"},
        {"scratch_vg_name": "rootvg"},
        {"scratch_pv_name": "hdisk9;exit"},
        {"scratch_vg_name": "hpvgABCDef12"},
        {"system_name": "-system"},
    ],
)
@pytest.mark.asyncio
async def test_bad_configuration_never_mutates(scratch, fields):
    state, fake = scratch
    state.config = replace(state.config, **fields)
    await vg.exercise_volume_group(None, state)
    assert not mutations(fake) and not state.observations
    assert all(row["status"] != "FAIL" for row in state.results)


@pytest.mark.parametrize(
    "case",
    [
        "nonfree",
        "ingroup",
        "collision",
        "pvidmismatch",
        "noidentity",
        "oldartifact",
        "wrongarm",
    ],
)
@pytest.mark.asyncio
async def test_preconditions_never_mutate(scratch, case):
    state, fake = scratch
    if case == "nonfree":
        fake.free.clear()
    if case == "ingroup":
        fake.inventory[PV] = (PVID, "datavg")
    if case == "collision":
        fake.groups.add(GROUP)
    if case == "pvidmismatch":
        fake.free[PV] = ("0000000000000099", "100000")
    if case == "noidentity":
        state.artifacts.vios_partition_id = None
    if case == "oldartifact":
        state.artifacts.storage_volume_group_name = GROUP
    if case == "wrongarm":
        state.group = "all"
    await vg.exercise_volume_group(None, state)
    assert not mutations(fake) and not state.observations
    assert all(row["status"] != "FAIL" for row in state.results)


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.asyncio
async def test_refused_create_readback_prevents_retry(scratch, partial):
    state, fake = scratch
    fake.create_status = "FAIL"
    fake.create_effect = partial
    await vg.exercise_volume_group(None, state)
    assert sum(tool == "hmc_create_volume_group" for tool, _ in fake.calls) == 1
    assert len(mutations(fake)) == (2 if partial else 1)
    assert observation(state)["result"] == "failed"
    assert observation(state)["cleanup"] == "passed"
    assert state.artifacts.storage_volume_group_name is None


@pytest.mark.parametrize("fault", ["post_readable", "extra_member", "lv", "pvid_lost"])
@pytest.mark.asyncio
async def test_unsafe_poststate_never_cleans_or_retries(scratch, fault):
    state, fake = scratch
    setattr(fake, fault, fault != "post_readable")
    await vg.exercise_volume_group(None, state)
    assert len(mutations(fake)) == 1
    assert state.artifacts.storage_volume_group_name == GROUP
    assert observation(state)["cleanup"] == "failed"
    assert any("MANUAL RECOVERY REQUIRED" in row["note"] for row in state.results)


@pytest.mark.parametrize("fault", ["cleanup_effect", "size_drift"])
@pytest.mark.asyncio
async def test_unconfirmed_restoration_keeps_artifact(scratch, fault):
    state, fake = scratch
    setattr(fake, fault, fault != "cleanup_effect")
    await vg.exercise_volume_group(None, state)
    assert len(mutations(fake)) == 2
    assert state.artifacts.storage_volume_group_name == GROUP
    assert observation(state)["cleanup"] == "failed"


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [{}],
        [{"name": "vg;exit"}],
        [{"name": "datavg"}, {"name": "datavg"}],
        ["datavg"],
    ],
)
def test_rest_listing_refuses_filtered_or_ambiguous_data(data):
    assert vg.rest_group_names(data) is None


def test_rest_listing_accepts_both_normalized_and_resource_shapes():
    assert vg.rest_group_names(
        [{"name": "rootvg"}, {"Resource": {"GroupName": "datavg"}}]
    ) == frozenset({"rootvg", "datavg"})


def test_unidentified_physical_volume_is_never_created():
    assert not vg.create_guard(
        PV, GROUP, {PV: ("none", "None")}, {PV: ("none", "100000")}, GROUPS
    )


def test_optin_registry_and_legacy_documents():
    import json
    from dataclasses import asdict

    assert runner.SUBTASKS[42] is vg.exercise_volume_group
    assert runner.SUBTASK_GROUPS["storage"] == [0, 3, 40, 42]
    assert 42 not in runner.SUBTASK_GROUPS["all"]
    assert 42 not in runner.SUBTASK_GROUPS["round2"]
    old_config = json.loads(json.dumps(asdict(live_config.LiveTestConfig())))
    old_config.pop("scratch_pv_name")
    old_config.pop("scratch_vg_name")
    assert live_config.decode_saved_config(old_config) == live_config.LiveTestConfig()
    artifacts = asdict(
        live_artifacts.LiveTestArtifacts(storage_volume_group_name=GROUP)
    )
    assert live_artifacts.decode_artifacts(artifacts).storage_volume_group_name == GROUP
    artifacts.pop("storage_volume_group_name")
    assert live_artifacts.decode_artifacts(artifacts).storage_volume_group_name is None


@pytest.mark.parametrize("kind", ["config", "artifact"])
def test_new_saved_fields_remain_strict(kind):
    import json
    from dataclasses import asdict

    data = (
        json.loads(json.dumps(asdict(live_config.LiveTestConfig())))
        if kind == "config"
        else asdict(live_artifacts.LiveTestArtifacts())
    )
    decode = (
        live_config.decode_saved_config
        if kind == "config"
        else live_artifacts.decode_artifacts
    )
    field = "scratch_pv_name" if kind == "config" else "storage_volume_group_name"
    data[field] = 123
    with pytest.raises(TypeError):
        decode(data)
    data.pop(field)
    data["unknown_field"] = "unexpected"
    with pytest.raises(ValueError):
        decode(data)


@pytest.mark.parametrize(
    "response", ["inventory", "free", "groups", "rest", "failed-read"]
)
@pytest.mark.asyncio
async def test_unreadable_before_snapshot_fails_without_mutation(
    scratch, monkeypatch, response
):
    state, fake = scratch
    original = fake.call

    async def malformed(_state, _client, tool, **arguments):
        status, data = await original(_client, tool, **arguments)
        if tool == "hmc_list_volume_groups" and response == "rest":
            return "PASS", [{}]
        command = arguments.get("cmd", "")
        if response == "inventory" and "lspv -field" in command:
            return "PASS", "hdisk9:invalid-pvid:None"
        if response == "free" and "lspv -free" in command:
            return "PASS", f"{PV}:{PVID}:not-a-size"
        if tool == "hmc_run_command" and shlex.split(command)[-1] == "lsvg":
            if response == "groups":
                return "PASS", "HSCL2970 The IOServer command failed"
            if response == "failed-read":
                return "FAIL", "read refused"
        return status, data

    monkeypatch.setattr(runner.RunState, "call", malformed)
    await vg.exercise_volume_group(None, state)
    assert not mutations(fake)
    assert not state.observations
    assert state.artifacts.storage_volume_group_name is None
    assert any(row["status"] == "FAIL" for row in state.results)


@pytest.mark.parametrize(
    "text", [":", "::", ":\n", " :", ": ", ":\n:", ":\nrow", "row\n:"]
)
def test_empty_free_sentinel_is_exact_and_free_only(text):
    assert vg.physical_volumes(text, free=True) == ({} if text == ":" else None)
    assert vg.physical_volumes(text) is None
    assert vg.physical_volumes(text, free=1) is None
    assert vg.physical_volumes("", free=True) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "preempty",
        "malformed-before",
        "failed-before",
        "malformed-after",
        "membership-refused",
        "schema-command",
        "schema-create",
    ],
)
async def test_registered_runner_native_wire_contract(monkeypatch, tmp_path, fault):
    """Exercise real dispatch/schema/results/restore logic at the MCP boundary."""
    fake = FakeVIOS()
    config = replace(
        live_config.LiveTestConfig(),
        system_name="sys-A",
        scratch_pv_name=PV,
        scratch_vg_name=GROUP,
    )
    hmc = HMCConfig.from_mapping({"host": "hmc.test", "user": "test"})
    destination = tmp_path / "test-results-storage.json"
    destination.write_text(
        json.dumps(
            {
                "config": asdict(config),
                "hmc": hmc_identity(hmc),
                "artifacts": asdict(
                    live_artifacts.LiveTestArtifacts(
                        vios_uuid="vios-A", vios_partition_id=1
                    )
                ),
                "results": [],
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    class NativeClient(Client):
        async def list_tools(self):
            tools = await super().list_tools()
            for tool in tools:
                if fault == "schema-command" and tool.name == "hmc_run_command":
                    tool.input_schema["properties"].pop("cmd")
                if fault == "schema-create" and tool.name == "hmc_create_volume_group":
                    tool.input_schema["properties"].pop("name")
            return tools

        async def call_tool(self, name, arguments=None, **kwargs):
            arguments = arguments or {}
            command = shlex.split(arguments.get("cmd", ""))
            inner = command[-1] if command else ""
            created = any(tool == "hmc_create_volume_group" for tool, _ in fake.calls)
            if inner.startswith("lsvg -pv"):
                if inner != f"lsvg -pv {GROUP} -field pvname -fmt :":
                    raise RuntimeError('Option "-pv" requires a parameter.')
                if fault == "membership-refused":
                    raise RuntimeError("membership read refused")
            status, data = await fake.call(self, name, **arguments)
            if inner.startswith("lspv -free"):
                if fault == "preempty" and not created:
                    data = ":"
                if fault == "malformed-before" and not created:
                    data = "::"
                if fault == "failed-before" and not created:
                    raise RuntimeError("physical inventory read refused")
                if fault == "malformed-after" and created:
                    data = ":\n"
            if status != "PASS":
                raise RuntimeError(str(data))
            return SimpleNamespace(data=data)

    states = []
    state_type = runner.RunState

    def new_state(**arguments):
        state = state_type(**arguments)
        states.append(state)
        return state

    monkeypatch.setattr(runner, "RunState", new_state)
    monkeypatch.setattr(runner, "Client", NativeClient)
    exit_code = await runner.main(
        42, str(destination), group="storage", config=config, hmc_config=hmc
    )
    saved = json.loads(destination.read_text())
    rows = saved["results"]
    writes = mutations(fake)
    pending = saved["artifacts"]["storage_volume_group_name"]
    if fault is None:
        assert exit_code == 0
        assert len(writes) == 2 and writes[0][0] == "hmc_create_volume_group"
        assert shlex.split(writes[1][1]["cmd"])[-1] == f"reducevg {GROUP} {PV}"
        assert observation(states[0])["cleanup"] == "passed"
        assert observation(states[0])["assertions"] == [
            "create-accepted",
            "rest-group-listed",
            "vios-group-listed",
            "selected-pv-only",
        ]
        assert pending is None and fake.inventory == BEFORE and fake.free == FREE
    elif fault == "preempty":
        assert exit_code == 0 and not writes and pending is None
        assert any(row["status"] == "SKIP" for row in rows)
        assert not any(
            row["status"] == "FAIL" or row["result"] == "passed" for row in rows
        )
    else:
        assert exit_code == 1 and any(row["status"] == "FAIL" for row in rows)
        assert not any(row["result"] == "passed" for row in rows)
        if fault in {"malformed-after", "membership-refused"}:
            assert len(writes) == 1 and pending == GROUP
        else:
            assert not writes and pending is None
        if fault == "schema-command":
            assert not any(tool == "hmc_run_command" for tool, _ in fake.calls)
        if fault == "schema-create":
            assert not any(tool == "hmc_create_volume_group" for tool, _ in fake.calls)


# -- the vg-if-match group's probe (#1850, ADR 0328) ------------------------------

_ERROR_BODY = (
    '<HttpErrorResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/'
    'mc/2012_10/"><HTTPStatus>412</HTTPStatus><RequestURI>/rest/api/uom/x</RequestURI>'
    "<Message>REST0412 precondition failed for sys-A</Message></HttpErrorResponse>"
)


class FakeHMC:
    def __init__(self, error=None, groups=(GROUP,)):
        self.error = error
        self.groups = groups
        self.posts = []

    def __call__(self, _config):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def list_volume_groups(self, vios):
        assert vios == "vios-A"
        return [
            {"UUID": f"uuid-{n}", "Resource": {"GroupName": n}} for n in self.groups
        ]

    async def _get_vg_raw_xml(self, vios, vg_uuid):
        return '"current"', ("element", vg_uuid), "unused raw document"

    async def _post_vg_xml(self, vios, vg_uuid, element, *, operation, etag):
        self.posts.append((vg_uuid, element, etag))
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_the_probe_posts_the_groups_own_body_under_a_never_matched_tag(
    monkeypatch,
):
    hmc = FakeHMC(error=vg.HMCError("refused", 412, _ERROR_BODY))
    monkeypatch.setattr(vg, "HMCClient", hmc)

    probe = await vg.if_match_probe(HMCConfig.from_mapping({}), "vios-A", GROUP)

    ((vg_uuid, element, etag),) = hmc.posts
    assert vg_uuid == f"uuid-{GROUP}" and element == ("element", vg_uuid)
    assert etag.startswith('"stale-') and etag != '"current"'
    assert probe == {
        "if_match": "never-matched",
        "status": 412,
        "message": "REST0412 precondition failed for sys-A",
        "codes": ["REST0412"],
    }
    assert "RequestURI" not in json.dumps(probe)


@pytest.mark.asyncio
async def test_an_accepted_post_records_that_if_match_was_not_enforced(monkeypatch):
    monkeypatch.setattr(vg, "HMCClient", FakeHMC())

    probe = await vg.if_match_probe(HMCConfig.from_mapping({}), "vios-A", GROUP)

    assert probe == {"if_match": "never-matched", "status": "2xx"}


@pytest.mark.asyncio
async def test_the_probe_writes_nothing_unless_the_group_is_listed_once(monkeypatch):
    hmc = FakeHMC(groups=(GROUP, GROUP))
    monkeypatch.setattr(vg, "HMCClient", hmc)

    probe = await vg.if_match_probe(HMCConfig.from_mapping({}), "vios-A", GROUP)

    assert probe["status"] is None and hmc.posts == []


@pytest.fixture
def probing(scratch, monkeypatch):
    state, fake = scratch
    state.group = vg.IF_MATCH_GROUP
    state.hmc_config = HMCConfig.from_mapping({})
    seen = []

    async def probe(_config, vios, group):
        seen.append(len(mutations(fake)))
        return {"if_match": "never-matched", "status": 412}

    monkeypatch.setattr(vg, "if_match_probe", probe)
    return state, fake, seen


@pytest.mark.asyncio
async def test_the_probe_runs_between_create_and_cleanup(probing):
    state, fake, seen = probing

    await vg.exercise_volume_group(None, state)

    assert seen == [1]  # after the create, before the reducevg
    assert len(mutations(fake)) == 2 and fake.groups == set(GROUPS)
    assert "if-match-probe-decisive" in observation(state)["assertions"]
    assert observation(state)["result"] == "passed"


@pytest.mark.parametrize(
    "error",
    [vg.HMCError("connection reset"), RuntimeError("unexpected"), KeyError("x")],
)
@pytest.mark.asyncio
async def test_a_probe_that_cannot_ask_still_cleans_up(probing, monkeypatch, error):
    state, fake, _ = probing

    async def broken(*_args):
        raise error

    monkeypatch.setattr(vg, "if_match_probe", broken)

    await vg.exercise_volume_group(None, state)

    assert fake.groups == set(GROUPS) and fake.inventory == BEFORE
    assert observation(state)["result"] == "failed"
    assert observation(state)["cleanup"] == "passed"


@pytest.mark.asyncio
async def test_the_storage_arm_never_probes(scratch, monkeypatch):
    state, _ = scratch

    async def forbidden(*_args):
        raise AssertionError("probed outside vg-if-match")

    monkeypatch.setattr(vg, "if_match_probe", forbidden)

    await vg.exercise_volume_group(None, state)

    assert "if-match-probe-decisive" not in str(state.observations)


@pytest.mark.parametrize("status", [400, 500, None])
@pytest.mark.asyncio
async def test_an_indecisive_answer_fails_the_probe_assertion(
    probing, monkeypatch, status
):
    state, fake, _ = probing

    async def answered(*_args):
        return {"if_match": "never-matched", "status": status}

    monkeypatch.setattr(vg, "if_match_probe", answered)

    await vg.exercise_volume_group(None, state)

    assert "if-match-probe-decisive" not in observation(state)["assertions"]
    assert fake.groups == set(GROUPS)
