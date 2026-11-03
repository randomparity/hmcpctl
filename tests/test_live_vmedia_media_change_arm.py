"""The optical media change capture (ST71, #1284) against a scripted HMC and VIOS."""

from __future__ import annotations

import contextlib
import shlex
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from hmcpctl.documents.common import UOM_NS
from hmcpctl.errors import HMCError

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import live_test_recovery as recovery
from live_test import config as live_config
from live_test import runner, vmedia_media_change
from test_live_vmedia_arm import (
    LPAR,
    LPAR_UUID,
    SYSTEM,
    FakeHMC,
    _failure,
    _run,
)

VTD = "vtopt1"
COLLISION_BODY = (
    "<HttpErrorResponse><Message>VTD vtopt1 name is already used in another mapping"
    "</Message></HttpErrorResponse>"
)


def _tag(name: str) -> str:
    return f"{{{UOM_NS}}}{name}"


@dataclass
class MediaHMC(FakeHMC):
    """FakeHMC plus a VIOS that loads and unloads media in the mounted VTD."""

    vtd: str = VTD
    reserved: bool = False
    rest_swaps: bool = False
    collision_orphan: bool = False
    collision_accepted: bool = False
    rename_on_swap: str | None = None
    loads_fail_after_power: int = 0
    ignored_power_offs: int = 0
    q1_unmaps: bool = False
    optical_reads_before_failure: int | None = None
    failing_loads: int = 0
    cli: list[str] = field(default_factory=list)
    posts: list[str] = field(default_factory=list)

    def _row(self) -> tuple[str, str, Any, Any] | None:
        return next((r for r in self.mappings if r[0].endswith(f"/{self.vtd}")), None)

    def _load(self, name: str | None) -> None:
        row = self._row()
        assert row is not None
        self.mappings.discard(row)
        kind = "VirtualOpticalMedia" if name else None
        self.mappings.add((row[0], LPAR_UUID, kind, name))

    def _mount(self, name: str) -> tuple[str, Any]:
        status, data = super()._mount(name)
        if status == "PASS" and self.vtd != "vtopt1":
            row = ("vhost1/vtopt1", LPAR_UUID, "VirtualOpticalMedia", name)
            self.mappings.discard(row)
            self.mappings.add((f"vhost1/{self.vtd}", *row[1:]))
        return status, data

    def _unmount(self, name: str) -> tuple[str, Any]:
        rows = {row for row in self.mappings if row[3] == name}
        if not rows:
            return "FAIL", _failure(f"HMCError: Optical mapping for media {name!r}")
        self.mappings -= rows
        self.vios_rows.discard("12,sys-A-lp3,3")
        self.lpar_rows.discard("3,vios-A,12")
        return "PASS", {}

    def _viosvrcmd(self, cmd: str) -> tuple[str, Any]:
        return self._vios(shlex.split(shlex.split(cmd)[-1]))

    def _vios(self, inner: list[str]) -> tuple[str, Any]:
        self.cli.append(" ".join(inner))
        verb, release = inner[0], "-release" in inner
        if verb == "help":
            return "PASS", f"Usage: {inner[1]} [-f] [-release] -vtd TargetDevice"
        row = self._row()
        if verb == "lsvopt":
            held = row[3] if row and row[3] else "No Media"
            return "PASS", f"Media                    Size(mb)\n{held}     1024\n"
        if self.reserved and not release:
            return "FAIL", _failure("HMCCLIError: the device is reserved")
        if verb == "loadopt" and row and row[3]:
            return "FAIL", _failure("HMCCLIError: the device holds media")
        if verb == "loadopt" and self.failing_loads:
            self.failing_loads -= 1
            return "FAIL", _failure("HMCCLIError: loadopt failed")
        self.reserved = self.reserved and not release
        self._load(inner[inner.index("-disk") + 1] if verb == "loadopt" else None)
        return "PASS", ""

    async def call(self, _client, tool: str, **kwargs: Any) -> tuple[str, Any]:
        if tool in ("hmc_load_optical_media", "hmc_unload_optical_media"):
            self.calls.append((tool, kwargs))
            assert kwargs["target_device"] == self.vtd
            verb = "loadopt" if tool == "hmc_load_optical_media" else "unloadopt"
            inner = [verb, *(["-release"] if kwargs["release"] else [])]
            if "media_name" in kwargs:
                inner += ["-disk", kwargs["media_name"]]
            status, data = self._vios([*inner, "-vtd", kwargs["target_device"]])
            return status, ({"target_device": self.vtd} if status == "PASS" else data)
        if tool == "hmc_run_command" and kwargs["cmd"].startswith("viosvrcmd"):
            self.calls.append((tool, kwargs))
            return self._viosvrcmd(kwargs["cmd"])
        if tool == "hmc_power_on_lpar" and not self.power_on_fails:
            self.reserved = True
            self.failing_loads = self.loads_fail_after_power
            self.lpar_state = "Running"
        if tool == "hmc_power_off_lpar":
            self.calls.append((tool, kwargs))
            if self.ignored_power_offs:
                self.ignored_power_offs -= 1
                return "FAIL", _failure("HMCError: power off failed")
            self.lpar_state = "Not Activated"
            return "PASS", {}
        if (
            tool == "hmc_list_optical_mappings"
            and self.optical_reads_before_failure is not None
        ):
            if self.optical_reads_before_failure == 0:
                self.calls.append((tool, kwargs))
                return "FAIL", _failure("HMCError: HTTP 500")
            self.optical_reads_before_failure -= 1
        if tool == "hmc_list_storage_mappings" and kwargs.get("lpar_name_or_uuid"):
            self.calls.append((tool, kwargs))
            return "PASS", [
                dict(zip(("id", "lpar_uuid", "backing_kind", "backing_name"), row))
                for row in sorted(self.mappings, key=str)
                if row[1] == LPAR_UUID
            ]
        return await super().call(_client, tool, **kwargs)

    def _raw_optical(self, row: tuple[str, str, str, str]) -> dict[str, Any]:
        raw = super()._raw_optical(row)
        raw["TargetDevice"]["VirtualOpticalTargetDevice"]["TargetName"] = row[0].split(
            "/"
        )[1]
        return raw

    def element(self) -> ET.Element:
        mappings = ET.Element(_tag("VirtualSCSIMappings"))
        for row in sorted(self.mappings, key=str):
            mapping = ET.SubElement(mappings, _tag("VirtualSCSIMapping"))
            if row[2] == "VirtualOpticalMedia":
                media = ET.SubElement(
                    ET.SubElement(mapping, _tag("Storage")), _tag("VirtualOpticalMedia")
                )
                ET.SubElement(media, _tag("MediaName")).text = row[3]
            device = ET.SubElement(
                ET.SubElement(mapping, _tag("TargetDevice")),
                _tag("VirtualOpticalTargetDevice"),
            )
            ET.SubElement(device, _tag("TargetName")).text = row[0].split("/")[1]
        return mappings

    async def rmw(self, _hmc, operation, _path, _args, mutate, _note="") -> str:
        mappings = self.element()
        before = len(mappings)
        mutate(mappings, "sys-uuid")
        self.posts.append(operation)
        if len(mappings) > before:
            if self.collision_orphan:
                self.vios_rows.add("13,sys-A-lp3,4")
            if self.collision_accepted:
                self.mappings.add(
                    ("vhost2/vtopt2", LPAR_UUID, "VirtualOpticalMedia", "x")
                )
                return ""
            raise HMCError("POST failed", 500, COLLISION_BODY)
        names = mappings.findall(f".//{_tag('MediaName')}")
        if self.q1_unmaps:
            row = self._row()
            assert row is not None
            self.mappings.discard(row)
            self.vios_rows.discard("12,sys-A-lp3,3")
            self.lpar_rows.discard("3,vios-A,12")
            return ""
        if self.rest_swaps:
            self._load(names[-1].text)
            if self.rename_on_swap:
                row = self._row()
                assert row is not None
                self.mappings.discard(row)
                self.vtd = self.rename_on_swap
                self.mappings.add((f"vhost1/{self.vtd}", *row[1:]))
        return ""


class _Client:
    def __init__(self, _config) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def get_lpar_link(self, system: str, lpar: str) -> str:
        return f"https://hmc.test/rest/api/uom/ManagedSystem/{system}/LogicalPartition/{lpar}"


@pytest.fixture
def arm(monkeypatch, tmp_path):
    config = replace(
        live_config.LiveTestConfig(),
        system_name=SYSTEM,
        lp3_name=LPAR,
        iso_path=str(tmp_path / "absent.iso"),
        iso_media_name="install.iso",
        protected_lpar_names=("vios-A",),
    )
    state = runner.RunState(config=config)
    state.group = vmedia_media_change.GROUP
    state.hmc_config = object()  # type: ignore[assignment]
    hmc = MediaHMC(
        configured_vg="vg-cfg-uuid", configured_name=config.vdisk_volume_group_name
    )
    hmc.mappings = {m for m in hmc.mappings if m[2] != "VirtualOpticalMedia"}
    monkeypatch.setattr(runner.RunState, "call", hmc.call)
    monkeypatch.setattr(vmedia_media_change, "_rmw_vios_mapping", hmc.rmw)
    monkeypatch.setattr(vmedia_media_change, "HMCClient", _Client)
    monkeypatch.setattr(vmedia_media_change, "_CAPTURES", tmp_path / "hmc-captures")
    captured: list = []
    monkeypatch.setattr(
        vmedia_media_change,
        "capture",
        lambda path: captured.append(path) or contextlib.nullcontext(),
    )
    return state, hmc, captured


def _rows(state, prefix: str) -> list[dict[str, Any]]:
    return [r for r in state.results if r["tool"].startswith(prefix)]


def _manual(state) -> list[str]:
    return [r["note"] for r in state.results if "MANUAL RECOVERY" in str(r["note"])]


def _mutated(hmc) -> bool:
    mutating = {
        "hmc_create_optical_media",
        "hmc_mount_optical_media",
        "hmc_power_on_lpar",
        "hmc_load_optical_media",
        "hmc_unload_optical_media",
    }
    return bool(hmc.posts or hmc.cli or any(t in mutating for t, _ in hmc.calls))


@pytest.mark.asyncio
async def test_the_capture_runs_q1_to_q4_and_leaves_the_vios_as_found(arm):
    state, hmc, captured = arm
    before = hmc.snapshot()

    await _run(state, 16, 71, 22)

    media = [
        kwargs["media_name"]
        for tool, kwargs in hmc.calls
        if tool == "hmc_create_optical_media"
    ]

    assert hmc.posts == ["capture q4 collision", "capture q1 replace storage"]
    assert hmc.cli == [
        "help loadopt",
        "help unloadopt",
        "lsvopt -vtd vtopt1",
        "lsvopt -vtd vtopt1",  # q1
        "unloadopt -vtd vtopt1",  # q2, through the tools
        "lsvopt -vtd vtopt1",
        f"loadopt -disk {media[0]} -vtd vtopt1",
        "lsvopt -vtd vtopt1",
        f"loadopt -disk {media[1]} -vtd vtopt1",  # occupied, through the tool
        f"loadopt -disk {media[1]} -vtd vtopt1",  # and raw
        "lsvopt -vtd vtopt1",
        "unloadopt -vtd vtopt1",  # q3, refused while reserved
        "lsvopt -vtd vtopt1",
        "unloadopt -release -vtd vtopt1",
        "lsvopt -vtd vtopt1",
        f"loadopt -release -disk {media[1]} -vtd vtopt1",
        "lsvopt -vtd vtopt1",
        "unloadopt -release -vtd vtopt1",
        "lsvopt -vtd vtopt1",
        f"loadopt -disk {media[0]} -vtd vtopt1",
        "lsvopt -vtd vtopt1",
    ]
    checks = {
        r["tool"]: r["status"] for r in state.results if r["tool"].endswith(" check")
    }
    assert checks == {
        "q2 unload check": "PASS",
        "q2 load check": "PASS",
        "q2 occupied check": "PASS",
        "q3 unload check": "FAIL",  # the fake reserves the device at power-on
        "q3 unload release check": "PASS",
        "q3 load release check": "PASS",
        "q3 unload release again check": "PASS",
        "q3 load check": "PASS",
    }
    (collision,) = _rows(state, "ViosSCSIMapping POST (q4")
    assert str(collision["data"]["status"]) == "500"
    assert "name is already used" in collision["data"]["body"]
    assert _rows(state, "hmc_power_on_lpar (q3)")
    assert _manual(state) == []
    assert hmc.snapshot() == before
    assert state.artifacts.vmedia_change_names == []
    assert len(captured) == 1 and captured[0].name.startswith("vmedia-media-change-")


@pytest.mark.parametrize(
    "setup",
    [
        lambda state, hmc: setattr(state, "group", "vmedia"),
        lambda state, hmc: setattr(
            state, "config", replace(state.config, protected_lpar_names=(LPAR,))
        ),
        lambda state, hmc: setattr(hmc, "lpar_state", "Running"),
        lambda state, hmc: setattr(hmc, "repository_size", "1"),
        lambda state, hmc: state.artifacts.vmedia_change_names.append(
            "hmcpctl_live_0a1b2c3d"
        ),
    ],
    ids=["wrong-group", "protected", "running", "no-room", "earlier-capture"],
)
@pytest.mark.asyncio
async def test_a_refusal_makes_no_mutation(arm, setup):
    state, hmc, _ = arm
    setup(state, hmc)

    await _run(state, 16, 71)

    assert not _mutated(hmc)


@pytest.mark.asyncio
async def test_an_unusable_vtd_name_stops_before_any_command(arm):
    state, hmc, _ = arm
    hmc.vtd = "vt;rm"

    await _run(state, 16, 71)

    assert hmc.cli == [] and hmc.posts == []
    assert any("unusable VTD name" in r["note"] for r in state.results)
    assert not any(r[3] in state.artifacts.vmedia_change_names for r in hmc.mappings)


@pytest.mark.asyncio
async def test_the_plain_unload_gets_release_only_after_it_fails(arm, monkeypatch):
    state, hmc, _ = arm
    original = hmc._vios

    def no_reservation(inner: list[str]):
        hmc.reserved = False
        return original(inner)

    monkeypatch.setattr(hmc, "_vios", no_reservation)

    await _run(state, 16, 71, 22)

    tools = [r["tool"] for r in state.results]
    assert "hmc_unload_optical_media (q3 unload release)" not in tools
    # The release forms still run once each, so the tools prove them either way.
    assert [c.split(" -disk")[0] for c in hmc.cli if "-release" in c] == [
        "loadopt -release",
        "unloadopt -release -vtd vtopt1",
    ]
    assert all(
        r["status"] == "PASS" for r in state.results if r["tool"].endswith(" check")
    )
    assert _manual(state) == []


@pytest.mark.asyncio
async def test_an_orphan_adapter_is_reported_and_q1_to_q3_still_run(arm):
    state, hmc, _ = arm
    hmc.collision_orphan = True

    await _run(state, 16, 71)

    (answer,) = _rows(state, "q4 orphan adapter")
    assert answer["data"] == {"vios": ["13,sys-A-lp3,4"], "lpar": []}
    assert any("-o r --id 1 -s 13)" in note for note in _manual(state))
    assert "capture q1 replace storage" in hmc.posts
    assert _rows(state, "hmc_power_on_lpar (q3)")


@pytest.mark.asyncio
async def test_the_mounts_own_adapter_is_not_a_q4_orphan(arm):
    state, _hmc, _ = arm

    await _run(state, 16, 71)

    (delta,) = _rows(state, "mount adapter delta")
    assert delta["data"] == {"added": ["12,sys-A-lp3,3", "3,vios-A,12"]}
    (answer,) = _rows(state, "q4 orphan adapter")
    assert answer["data"] == {"vios": [], "lpar": []}


@pytest.mark.asyncio
async def test_an_accepted_collision_stops_and_cleanup_still_runs(arm):
    state, hmc, _ = arm
    hmc.collision_accepted = True

    await _run(state, 16, 71)

    assert hmc.posts == ["capture q4 collision"]
    assert any("stopped" in r["note"] for r in state.results)
    assert _rows(state, "hmc_unmount_optical_media (cleanup)")
    assert _rows(state, "hmc_delete_optical_media (cleanup)")
    assert not _rows(state, "hmc_power_on_lpar")


@pytest.mark.asyncio
async def test_cleanup_reloads_an_empty_drive_before_unmounting(arm):
    """Q3's loads both fail, so the drive is empty when cleanup starts."""
    state, hmc, _ = arm
    hmc.loads_fail_after_power = 2

    await _run(state, 16, 71, 22)

    assert _rows(state, "hmc_load_optical_media (q3 load release)")
    assert _rows(state, "hmc_load_optical_media (cleanup load)")
    assert _rows(state, "hmc_unmount_optical_media (cleanup)")
    assert _manual(state) == []
    assert state.artifacts.vmedia_change_names == []


@pytest.mark.asyncio
async def test_an_empty_drive_cleanup_cannot_reload_is_a_manual_row(arm):
    state, hmc, _ = arm
    hmc.loads_fail_after_power = 4

    await _run(state, 16, 71)

    assert any("rmvdev -vtd vtopt1" in note for note in _manual(state))


@pytest.mark.asyncio
async def test_an_unreadable_empty_check_in_cleanup_is_a_manual_row(arm, monkeypatch):
    state, hmc, _ = arm
    base_call = hmc.call

    async def call(_state, client, tool, **kwargs):
        if tool == "hmc_list_storage_mappings" and kwargs.get("lpar_name_or_uuid"):
            return "FAIL", _failure("HMCError: HTTP 500")
        return await base_call(client, tool, **kwargs)

    monkeypatch.setattr(runner.RunState, "call", call)

    await _run(state, 16, 71)

    assert any(
        "could not be read" in note and "rmvdev -vtd vtopt1" in note
        for note in _manual(state)
    )


@pytest.mark.asyncio
async def test_the_vtd_is_rederived_when_q1_renames_it(arm):
    state, hmc, _ = arm
    hmc.rest_swaps = True
    hmc.rename_on_swap = "vtopt7"

    await _run(state, 16, 71, 22)

    (renamed,) = _rows(state, "q1 target renamed")
    assert renamed["data"] == {"vtd": "vtopt7"}
    assert "unloadopt -vtd vtopt7" in hmc.cli
    assert _manual(state) == []


@pytest.mark.asyncio
async def test_recovery_reads_the_arms_own_power_on_and_media(arm):
    state, _hmc, _ = arm

    await _run(state, 16, 71)

    document = {
        "run": {"subtasks": [16, 71, 22], "group": vmedia_media_change.GROUP},
        "config": {
            "system_name": SYSTEM,
            "lp3_name": LPAR,
            "iso_media_name": "install.iso",
        },
        "artifacts": {
            "vios_uuid": state.artifacts.vios_uuid,
            "vios_partition_id": state.artifacts.vios_partition_id,
            "vmedia_change_names": ["hmcpctl_live_0a1b2c3d"],
        },
        "results": state.results,
    }
    inputs = recovery.lpar_inputs_from_document(document, [16, 71, 22])

    assert inputs is not None
    assert inputs.powered_on and inputs.vmedia_ran
    assert inputs.media_change_vtds == {"vtopt1"}
    assert inputs.iso_names == {"hmcpctl_live_0a1b2c3d"}


@pytest.mark.asyncio
async def test_st22_removes_media_a_stopped_capture_left(arm):
    state, hmc, _ = arm
    leftover = "hmcpctl_live_0a1b2c3d"
    hmc.media[leftover] = 1024
    state.artifacts.vmedia_change_names.append(leftover)

    await _run(state, 16, 22)

    assert leftover not in hmc.media
    assert state.artifacts.vmedia_change_names == []


@pytest.mark.asyncio
async def test_a_failed_power_on_leaves_q3_unanswered(arm):
    """No client opened the device, so a plain unloadopt answers nothing about Q3."""
    state, hmc, _ = arm
    hmc.power_on_fails = True

    await _run(state, 16, 71, 22)

    assert not _rows(state, "hmc_run_command (q3")
    assert any("q3 unanswered" in r["note"] for r in state.results)
    assert _manual(state) == []


@pytest.mark.asyncio
async def test_cleanup_powers_off_a_partition_q3_left_running(arm):
    state, hmc, _ = arm
    hmc.ignored_power_offs = 1

    await _run(state, 16, 71, 22)

    (off,) = _rows(state, "hmc_power_off_lpar (cleanup)")
    assert off["status"] == "PASS"
    assert hmc.lpar_state == "Not Activated"
    assert _manual(state) == []


@pytest.mark.asyncio
async def test_a_cleanup_power_off_that_fails_is_a_manual_row(arm):
    state, hmc, _ = arm
    hmc.ignored_power_offs = 2

    await _run(state, 16, 71)

    assert any("chsysstate" in note and "--immed" in note for note in _manual(state))


@pytest.mark.asyncio
async def test_a_q1_that_unmaps_the_medium_skips_q2_and_q3(arm):
    state, hmc, _ = arm
    hmc.q1_unmaps = True

    await _run(state, 16, 71, 22)

    assert not any(c.startswith(("unloadopt", "loadopt")) for c in hmc.cli)
    assert not _rows(state, "hmc_power_on_lpar")
    assert any("no run medium is mapped after q1" in r["note"] for r in state.results)
    assert _manual(state) == []
    assert state.artifacts.vmedia_change_names == []


@pytest.mark.asyncio
async def test_an_unreadable_cleanup_listing_is_a_manual_row(arm):
    state, hmc, _ = arm
    hmc.optical_reads_before_failure = 2

    await _run(state, 16, 71)

    assert any("optical mappings could not be read" in n for n in _manual(state))
