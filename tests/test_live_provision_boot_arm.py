"""The provision-boot live arm (ST72, #1849) against a scripted HMC.

It reuses the lpar-power arm's scripted system and adds what an install run
touches: the media repository, the optical mapping and the boot's steps.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import test_live_lpar_power_arm as arm
from live_test import config as live_config
from live_test import provision_boot, runner
from test_live_lpar_power_arm import (
    GROUP,
    SYSTEM,
    VLAN,
    FakeClient,
    World,
    _record,
)

from hmcpctl.ssh.commands import HMC_NO_RESULTS

REPO_UUID = "0A1B2C3D-0000-4000-8000-0000000000D1"
MEDIA = "hmcpctl_000000000000.iso"
SAVED = "hmcpctl_000000000000"


@pytest.fixture(scope="module")
def schemas():
    return arm.schemas.__wrapped__()


@pytest.fixture(scope="module")
def output_schemas():
    return arm.output_schemas.__wrapped__()


@pytest.fixture(autouse=True)
def _typed_outputs(output_schemas):
    """Served results arrive typed, as the live client hands them to the arm."""
    arm._OUTPUTS.update(output_schemas)


@pytest.fixture(autouse=True)
def _no_iso_server(monkeypatch):
    monkeypatch.setattr(provision_boot, "_start_iso_server", lambda *_a: None)
    monkeypatch.setattr(provision_boot, "_power_operations_authorized", lambda: True)
    monkeypatch.setattr(provision_boot, "_POLL_DELAY_S", 0)
    for name in (
        "_STATE_POLL_DELAY_S",
        "_OPERATION_POLL_DELAY_S",
        "_COMPARE_REREAD_DELAY_S",
    ):
        monkeypatch.setattr(provision_boot.lpar_power, name, 0)
    monkeypatch.setattr(provision_boot.lpar_power, "_STATE_POLL_ATTEMPTS", 2)


class BootWorld(World):
    """Answers an install provision as a healthy HMC would, then its teardown."""

    def __init__(self) -> None:
        super().__init__()
        self.media: set[str] = set()
        self.optical: list[str] = []
        self.mount_status = "ok"
        self.activated_profile = SAVED
        self.provisions: list[dict[str, Any]] = []
        self.stop_before_media = False

    def _hmc_run_command(self, kwargs: dict[str, Any]) -> Any:
        cmd = kwargs["cmd"]
        if cmd.endswith("-F curr_profile,state"):
            (entry,) = self.partitions.values()
            return f"{self.activated_profile},{entry['state']}"
        if cmd.endswith("-F slot_num") and "lpar_names=hmcpctl-live-pwr-" in cmd:
            return "2\n4\n" if self.optical else HMC_NO_RESULTS
        if cmd.endswith("-F os_version"):
            return "VIOS 4.1.0.00"
        if cmd == "date":
            return "Tue Nov  3 12:00:00 UTC 2026"
        return super()._hmc_run_command(kwargs)

    def _hmc_read_lpar_refcodes(self, kwargs: dict[str, Any]) -> Any:
        return [
            {
                "lpar_name": kwargs["lpar_name_or_uuid"],
                "time_stamp": "11/03/2026 12:00:00",
                "refcode": code,
                "definition": None,
                "definition_source": None,
            }
            for code in ("CA260200", "C2008119")
        ]

    def _hmc_provision_lpar(self, kwargs: dict[str, Any]) -> Any:
        if kwargs.get("continuation") == "abandon":
            return {**arm._provisioned(False, "terminal"), "steps": []}
        self.provisions.append(kwargs)
        uuid = self._new(kwargs["name"], kwargs["caller_token"], kwargs["resources"])
        name = kwargs["storage"]["storage_name"]
        self.volumes.add(name)
        self.mappings.append(
            {
                "id": "vhost1/vtscsi1",
                "lpar_uuid": uuid,
                "backing_kind": "VirtualDisk",
                "backing_name": name,
            }
        )
        self.adapters.add("3,run,2")
        if self.stop_before_media:
            steps = [{"step": "create", "status": "ok"}]
            return {**self._answer(uuid, steps), "operation": _record("paused")}
        self.media.add(MEDIA)
        steps = [
            {"step": "create", "status": "ok"},
            {"step": "bind_media", "status": "ok", "result": {"media_name": MEDIA}},
            {
                "step": "upload_media",
                "status": "ok",
                "result": {"vg_uuid": REPO_UUID, "media_name": MEDIA},
            },
        ]
        if self.mount_status != "ok":
            steps.append(
                {"step": "mount_media", "status": "error", "result": "REST0269"}
            )
            record = _record("terminal")
            record["outcome"] = "failed"
        else:
            self.optical.append(MEDIA)
            self.partitions[kwargs["name"]]["state"] = "Running"
            steps += [
                {
                    "step": "mount_media",
                    "status": "ok",
                    "result": {"client_slot": 4, "media_name": MEDIA},
                },
                {"step": "profile_readback", "status": "ok"},
                {"step": "revalidate_boot", "status": "ok"},
                {"step": "power_on", "status": "ok"},
                {
                    "step": "wait_boot_started",
                    "status": "error",
                    "result": {"reason": "set empty", "observed": []},
                },
            ]
            record = _record("paused")
        return {**self._answer(uuid, steps), "operation": record}

    def _answer(self, uuid: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "resource_created": True,
            "workflow_completed": False,
            "lpar_uuid": uuid,
            "dry_run": False,
            "ownership_stamped": True,
            "steps": steps,
            "warnings": [],
        }

    def _hmc_list_optical_mappings(self, kwargs: dict[str, Any]) -> Any:
        return [{"media_name": name} for name in self.optical]

    def _hmc_unmount_optical_media(self, kwargs: dict[str, Any]) -> Any:
        self.optical.remove(kwargs["media_name"])
        return f"Unmounted {kwargs['media_name']}"

    def _hmc_delete_optical_media(self, kwargs: dict[str, Any]) -> Any:
        if kwargs["media_name"] in self.optical:
            return ValueError("the medium is mapped")
        self.media.discard(kwargs["media_name"])
        return f"Deleted {kwargs['media_name']}"

    def _hmc_list_optical_media(self, kwargs: dict[str, Any]) -> Any:
        return [{"name": name} for name in sorted(self.media)]


def _run(schemas, world: World, tmp_path: Path, group: str = provision_boot.GROUP):
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"iso")
    state = runner.RunState(
        config=live_config.LiveTestConfig(
            system_name=SYSTEM,
            vdisk_volume_group_name=GROUP,
            provision_vlan_id=VLAN,
            partition_profile_writer_window=True,
            iso_path=str(iso),
        ),
        group=group,
    )
    state.schemas = schemas
    asyncio.run(
        provision_boot.exercise_provision_boot(FakeClient(world, arm._OUTPUTS), state)
    )
    return state


def _observation(state) -> dict[str, Any]:
    (entry,) = state.observations
    return entry["observation"]


def test_a_clean_boot_holds_every_assertion_and_leaves_nothing(schemas, tmp_path):
    world = BootWorld()

    state = _run(schemas, world, tmp_path)

    observation = _observation(state)
    assert observation["result"] == "passed"
    assert observation["cleanup"] == "passed"
    assert len(observation["assertions"]) == 8
    assert world.partitions == {} and world.media == set() and world.optical == []
    assert world.volumes == {"hd5", "lv_op"}
    (sent,) = world.provisions
    assert sent["boot"] == "immediate" and sent["exclusive_writer_window"] is True
    result = sent["install"]["media"]["producer_result"]
    assert result["iso_size"] == 3 and result["mac"] == sent["adapters"]["mac"]
    assert sent["name"].startswith(provision_boot.lpar_power.NAME_PREFIX)
    assert state.artifacts.provision_boot_media == {
        "vios_uuid": arm.VIOS_UUID,
        "vg_uuid": REPO_UUID,
        "media_name": MEDIA,
    }
    tools = world.tools()
    assert tools.index("hmc_unmount_optical_media") < tools.index(
        "hmc_delete_optical_media"
    )
    assert tools.index("hmc_power_off_lpar") < tools.index("hmc_unmount_optical_media")


def test_a_refused_mount_fails_its_assertion_and_still_cleans_up(schemas, tmp_path):
    world = BootWorld()
    world.mount_status = "error"

    state = _run(schemas, world, tmp_path)

    observation = _observation(state)
    assert observation["result"] == "failed"
    assert "mount-accepted" not in observation["assertions"]
    assert world.partitions == {} and world.media == set()


def test_activation_from_another_profile_fails_that_assertion(schemas, tmp_path):
    world = BootWorld()
    world.activated_profile = "default_profile"

    state = _run(schemas, world, tmp_path)

    assert "activated-from-saved-profile" not in _observation(state)["assertions"]


def test_outside_its_group_it_calls_nothing(schemas, tmp_path):
    world = BootWorld()

    _run(schemas, world, tmp_path, group="lpar-power")

    assert world.calls == []


def test_the_run_mac_is_locally_administered_unicast():
    assert provision_boot.run_mac("0a1b2c3d") == "02:00:0a:1b:2c:3d"


def test_a_medium_left_mapped_keeps_the_partition_for_manual_recovery(
    schemas, tmp_path
):
    world = BootWorld()
    world.overrides["hmc_unmount_optical_media"] = lambda _kw: ValueError("busy")

    state = _run(schemas, world, tmp_path)

    assert len(world.partitions) == 1 and world.optical == [MEDIA]
    assert "hmc_delete_lpar" not in world.tools()
    assert "hmc_delete_optical_media" not in world.tools()
    assert _observation(state)["cleanup"] == "failed"
    assert any("MANUAL RECOVERY" in str(row) for row in state.results)


def test_an_unreadable_mapping_listing_is_not_taken_as_unmapped(schemas, tmp_path):
    world = BootWorld()
    world.overrides["hmc_list_optical_mappings"] = lambda _kw: ValueError("down")

    _run(schemas, world, tmp_path)

    assert len(world.partitions) == 1
    assert "hmc_delete_lpar" not in world.tools()


def test_a_failed_first_call_with_a_recorded_operation_is_abandoned(schemas, tmp_path):
    world = BootWorld()
    world.overrides["hmc_provision_lpar"] = lambda kw: (
        {**arm._provisioned(False, "terminal"), "steps": []}
        if kw.get("continuation") == "abandon"
        else ValueError("connection reset")
    )
    world.overrides["hmc_operation_status"] = lambda kw: {
        "operations": [{"request_id": kw["request_id"]}],
        "limit": 50,
        "truncated": False,
        "next_cursor": None,
    }

    state = _run(schemas, world, tmp_path)

    abandons = [
        kw for kw in world.calls_to("hmc_provision_lpar") if kw.get("continuation")
    ]
    assert len(abandons) == 1
    assert state.artifacts.provision_boot_media["media_name"] is None


def test_a_stop_before_any_medium_still_deletes_the_partition(schemas, tmp_path):
    world = BootWorld()
    world.stop_before_media = True

    state = _run(schemas, world, tmp_path)

    assert world.partitions == {} and world.volumes == {"hd5", "lv_op"}
    assert "hmc_unmount_optical_media" not in world.tools()
    assert _observation(state)["cleanup"] == "passed"


# -- native boot-started groups (#1850, ADR 0328) -------------------------------

NATIVE_MAC = "02:00:00:00:00:a1"
NATIVE_NETWORK = {
    "address": "192.0.2.20/24",
    "routes": [{"destination": "0.0.0.0/0", "gateway": "192.0.2.1"}],
    "dns": ["192.0.2.53"],
}


def _native_config(tmp_path: Path, **result_overrides: Any):
    iso = tmp_path / "launcher.iso"
    iso.write_bytes(b"launcher")
    result = {
        "format": "iso-chain-media-v1",
        "iso_sha256": provision_boot._describe(iso)["iso_sha256"],
        "iso_size": 8,
        "manifest_sha256": "1" * 64,
        "distribution": "rocky",
        "release": "9.8",
        "architecture": "ppc64le",
        "mac": NATIVE_MAC,
        "network": NATIVE_NETWORK,
        **result_overrides,
    }
    described = tmp_path / "result.json"
    described.write_text(json.dumps(result), encoding="utf-8")
    return live_config.LiveTestConfig(
        system_name=SYSTEM,
        vdisk_volume_group_name=GROUP,
        provision_vlan_id=VLAN,
        partition_profile_writer_window=True,
        native_boot_mac=NATIVE_MAC.upper(),
        native_rocky_iso_path=str(iso),
        native_rocky_media_result=str(described),
    ), result


def _run_native(schemas, world: World, config, group="native-rocky-boot-started"):
    state = runner.RunState(config=config, group=group)
    state.schemas = schemas
    asyncio.run(
        provision_boot.exercise_provision_boot(FakeClient(world, arm._OUTPUTS), state)
    )
    return state


@pytest.fixture
def _short_window(monkeypatch):
    monkeypatch.setattr(provision_boot, "_RECORD_DELAY_S", 0.01)
    monkeypatch.setattr(provision_boot, "NATIVE_WINDOW_S", 0.05)


@pytest.mark.usefixtures("_short_window")
def test_a_native_run_boots_the_producers_medium_and_records_the_window(
    schemas, tmp_path
):
    world = BootWorld()
    config, result = _native_config(tmp_path)

    state = _run_native(schemas, world, config)

    observation = _observation(state)
    assert observation["scenario"] == provision_boot.SCENARIO
    assert observation["result"] == "passed" and observation["cleanup"] == "passed"
    assert len(observation["assertions"]) == 11
    (sent,) = world.provisions
    assert sent["install"]["media"]["producer_result"] == result
    assert sent["install"]["profile"] == "rocky-9.8"
    assert sent["install"]["network"] == NATIVE_NETWORK
    assert sent["adapters"]["mac"] == NATIVE_MAC
    assert sent["resources"]["desired_memory"] == provision_boot.NATIVE_MEMORY_MIB
    assert state.config.iso_path == config.native_rocky_iso_path
    (row,) = [r for r in state.results if r["tool"] == "hmc_provision_lpar"][-1:]
    data = row["data"]
    assert data["vios_level"] == "VIOS 4.1.0.00"
    assert [c["hmc"] for c in data["hmc_clock"]] == ["Tue Nov  3 12:00:00 UTC 2026"] * 2
    reads = data["refcode_reads"]
    assert len(reads) >= 2
    assert reads[0]["state"] == "running"
    assert [r["refcode"] for r in reads[0]["rows"]] == ["CA260200", "C2008119"]
    assert world.partitions == {} and world.media == set()


@pytest.mark.usefixtures("_short_window")
def test_a_native_window_that_never_reads_running_fails_that_assertion(
    schemas, tmp_path
):
    world = BootWorld()
    world.overrides["hmc_read_lpar_refcodes"] = lambda _kw: ValueError("down")
    config, _ = _native_config(tmp_path)

    state = _run_native(schemas, world, config)

    observation = _observation(state)
    assert observation["result"] == "failed"
    assert "refcodes-read-while-running" not in observation["assertions"]
    assert world.partitions == {}


@pytest.mark.usefixtures("_short_window")
@pytest.mark.parametrize(
    "overrides",
    [
        {"distribution": "ubuntu"},
        {"release": "9.7"},
        {"mac": "02:00:00:00:00:a2"},
        {"iso_size": 9},
        {"iso_sha256": "0" * 64},
        {"format": "iso-chain-media-v0"},
        {"network": None},
    ],
)
def test_a_media_result_that_does_not_describe_the_medium_skips_untouched(
    schemas, tmp_path, overrides
):
    world = BootWorld()
    config, _ = _native_config(tmp_path, **overrides)

    state = _run_native(schemas, world, config)

    assert world.calls == []
    assert state.observations == []
    assert any(r["status"] == "SKIP" for r in state.results)


def test_a_native_group_without_its_media_settings_skips_untouched(schemas, tmp_path):
    world = BootWorld()
    config, _ = _native_config(tmp_path)

    state = _run_native(schemas, world, config, group="native-ubuntu-boot-started")

    assert world.calls == []
    (row,) = state.results
    assert "LIVE_TEST_NATIVE_UBUNTU_ISO_PATH" in str(row)


def test_an_unreadable_media_result_skips_untouched(schemas, tmp_path):
    world = BootWorld()
    config, _ = _native_config(tmp_path)
    Path(config.native_rocky_media_result).write_text("{", encoding="utf-8")

    _run_native(schemas, world, config)

    assert world.calls == []


def test_a_boot_started_run_holds_the_wait_assertion_on_the_handoff_code(
    schemas, tmp_path
):
    world = BootWorld()
    handoff = {"time_stamp": "11/03/2026 10:00:09", "refcode": "CA00E105"}
    world.overrides["hmc_provision_lpar"] = _ending(world, "boot_started", handoff)

    state = _run(schemas, world, tmp_path)

    assert "boot-wait-ended-truthfully" in _observation(state)["assertions"]


def test_a_boot_started_claim_on_another_code_fails_the_wait_assertion(
    schemas, tmp_path
):
    world = BootWorld()
    other = {"time_stamp": "11/03/2026 10:00:09", "refcode": "CA00E14D"}
    world.overrides["hmc_provision_lpar"] = _ending(world, "boot_started", other)

    state = _run(schemas, world, tmp_path)

    assert "boot-wait-ended-truthfully" not in _observation(state)["assertions"]


def _ending(world: BootWorld, outcome: str, row: dict[str, Any]):
    """The healthy answer, ending *outcome* with *row* as the wait's result."""

    def respond(kwargs: dict[str, Any]) -> Any:
        answer = BootWorld._hmc_provision_lpar(world, kwargs)
        if kwargs.get("continuation") == "abandon":
            return answer
        for step in answer["steps"]:
            if step["step"] == "wait_boot_started":
                step.update(status="ok", result=row)
        answer["operation"] = {**answer["operation"], "outcome": outcome}
        return answer

    return respond
