"""The vios-backup live arm (ST37, #1349) against a scripted VIOS."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import inspect
import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "live_test_runner.py"
sys.path.insert(0, str(_RUNNER_PATH.parent))
import live_test_recovery as recovery  # noqa: E402
from live_test import vios_backup  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("hmc_live_test_runner", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = sys.modules.get(_SPEC.name) or importlib.util.module_from_spec(_SPEC)
if _SPEC.name not in sys.modules:
    sys.modules[_SPEC.name] = runner
    _SPEC.loader.exec_module(runner)

SYSTEM = "acmesys9"
LPAR = "acmesys9-lp3"
VIOS = "vios-A"
VIOS_UUID = "0000000A-ABCD-4EF0-8ABC-00000000000A"
LPAR_UUID = "00000003-ABCD-4EF0-8ABC-000000000003"
DISK = {
    "id": "vhost0/lp3-disk",
    "lpar_uuid": LPAR_UUID,
    "backing_kind": "VirtualDisk",
    "backing_name": "lp3-vd1",
}
OPTICAL = {
    "id": "vhost0/lp3-vopt",
    "lpar_uuid": LPAR_UUID,
    "backing_kind": "VirtualOpticalMedia",
    "backing_name": "lp3-media",
}
#: The header `lsviosbk -F --header` printed on V10R3 (2026-10-06 live capture).
LSVIOSBK_HEADER = (
    "name,type,sys_name,mtms,vios_name,vios_id,vios_uuid,last_modified,size\n"
)


def _hsclc455(command: str, name: str) -> str:
    return (
        f"SSH command '{command} ... -f {name}' failed with exit status 1: "
        f"HSCLC455 The backup file {name} does not exist."
    )


MUTATIONS = ("hmc_backup_vios", "rmvdev", "hmc_restore_vios", "mkvdev", "rmviosbk")


@dataclass
class FakeVios:
    """A VIOS and its HMC, answering the arm's calls; each knob injects one fault."""

    partitions: str = f"{LPAR},aixlinux,Not Activated\n{VIOS},vioserver,Running\n"
    vioses: tuple[str, ...] = (VIOS,)
    mapped: bool = True
    backups: list[str] = field(default_factory=list)
    backup_fails: bool = False
    backup_invisible: bool = False
    listing_malformed: bool = False
    restore_status: str = "PASS"
    restore_error: str = "SSH command timed out after 2400s"
    restore_restores: bool = True
    ioslevel_answers: bool = True
    snapshot_read_fails_after_restore: bool = False
    rmviosbk_removes: bool = True
    sea_lines: str = "ent5 ent0 Available\n"
    rmc_before: str = "active\n"
    rest_lags: bool = False
    rmvdev_ignored: bool = False
    rest_hidden: bool = False
    vadapter_disagrees: bool = False
    late_vadapter_disagrees: bool = False
    rmc_returns: bool = True
    serial: str = "server,1\nserver,0\n"
    reorder: bool = False
    calls: list[str] = field(default_factory=list)
    restored: bool = False

    def mappings(self, lpar: str | None) -> list[dict[str, Any]]:
        shown = (self.mapped or self.rest_lags) and not self.rest_hidden
        found = [OPTICAL, DISK] if shown else [OPTICAL]
        if self.reorder and self.restored:
            found = list(reversed(found))
        return list(found)

    def answer(self, tool: str, kwargs: dict[str, Any]) -> tuple[str, Any]:
        cmd = kwargs.get("cmd", "")
        self.calls.append(cmd or tool)
        if tool == "hmc_list_vios":
            return "PASS", {
                "entries": [
                    {
                        "UUID": VIOS_UUID if name == VIOS else name,
                        "Resource": {"PartitionName": name},
                    }
                    for name in self.vioses
                ]
            }
        if tool == "hmc_list_storage_mappings":
            if self.snapshot_read_fails_after_restore and self.restored:
                return "FAIL", "read failed"
            return "PASS", self.mappings(kwargs.get("lpar_name_or_uuid"))
        if tool == "hmc_list_vios_backups":
            if self.listing_malformed:
                return "FAIL", "Malformed lsviosbk CSV"
            return "PASS", [
                {"name": name, "type": "viosioconfig"} for name in self.backups
            ]
        if tool == "hmc_backup_vios":
            if self.backup_fails:
                return "FAIL", "mkviosbk refused"
            if not self.backup_invisible:
                # As observed on V10R3: the catalog names it `<name>.tar.gz`.
                self.backups.append(kwargs["backup_name"] + ".tar.gz")
            return "PASS", ""
        if tool == "hmc_restore_vios":
            self.restored = True
            if kwargs["backup_name"] not in self.backups:
                return "FAIL", _hsclc455("rstviosbk", kwargs["backup_name"])
            self.mapped = self.restore_restores
            return (
                self.restore_status,
                "" if self.restore_status == "PASS" else self.restore_error,
            )
        assert tool == "hmc_run_command", tool
        return self.command(cmd)

    def command(self, cmd: str) -> tuple[str, Any]:
        if cmd.startswith("lssyscfg") and cmd.endswith("-F rmc_state"):
            if self.restored and not self.rmc_returns:
                return "PASS", "inactive\n"
            return "PASS", self.rmc_before
        if cmd.startswith("lssyscfg"):
            return "PASS", self.partitions
        if cmd.startswith("lshwres"):
            return "PASS", self.serial
        if cmd.startswith("lsviosbk"):
            if not self.backups:
                return "PASS", "No results were found.\n"
            return "PASS", LSVIOSBK_HEADER + "".join(
                f"{b},viosioconfig,{SYSTEM},SYNTH-01*SYNTH01,{VIOS},100,"
                f"{VIOS_UUID},10/06/2026 00:00:00,7.330\n"
                for b in self.backups
            )
        if cmd.startswith("rmviosbk"):
            name = shlex.split(cmd)[-1]
            if name not in self.backups:
                return "FAIL", _hsclc455("rmviosbk", name)
            if self.rmviosbk_removes:
                self.backups.remove(name)
            return "PASS", ""
        inner = cmd.split(' -c "', 1)[1].rstrip('"')
        if inner.startswith("rmvdev"):
            if self.rmvdev_ignored:
                self.rest_hidden = True
                return "PASS", ""
            self.mapped = False
            return "PASS", ""
        if inner.startswith("mkvdev"):
            self.mapped = True
            return "PASS", ""
        if inner.startswith("lsmap -vadapter"):
            late = self.late_vadapter_disagrees and self.restored
            if not self.mapped or self.vadapter_disagrees or late:
                return "PASS", "SVSA vhost0\nVTD lp3-vopt\n"
            return "PASS", "SVSA vhost0\nVTD lp3-disk\nBacking device lp3-vd1\n"
        if inner == "ioslevel":
            return (
                ("PASS", "3.1.4.10") if self.ioslevel_answers else ("FAIL", "RMC down")
            )
        if inner == "lsmap -all -net":
            lines = self.sea_lines if not self.restored else self.sea_after()
            return "PASS", lines
        if inner == "lsmap -all":
            vtds = ["lp3-vopt", "lp3-disk"] if self.mapped else ["lp3-vopt"]
            if self.reorder and self.restored:
                vtds.reverse()
            return "PASS", "vhost0 C3\n" + "".join(f"VTD {v}\n" for v in vtds)
        return "PASS", f"{inner} output\n"

    def sea_after(self) -> str:
        return self.sea_lines


class SeaChangingVios(FakeVios):
    def sea_after(self) -> str:
        return self.sea_lines + "ent6 ent1 Available\n"


def _state(monkeypatch, vios: FakeVios, group: str | None = "vios-backup"):
    async def scripted(_state, _client, tool, **kwargs):
        return vios.answer(tool, kwargs)

    monkeypatch.setattr(runner.RunState, "call", scripted)
    monkeypatch.setattr(vios_backup, "RESTORE_WAIT_SECONDS", 0)
    monkeypatch.setattr(vios_backup, "POLL_SECONDS", 0)
    monkeypatch.setenv("HMC_SSH_TIMEOUT", str(vios_backup.MIN_SSH_TIMEOUT))
    config = runner.LiveTestConfig(system_name=SYSTEM, lp3_name=LPAR)
    return runner.RunState(config=config, group=group)


async def _run(monkeypatch, vios: FakeVios, group: str | None = "vios-backup"):
    state = _state(monkeypatch, vios, group)
    await vios_backup.exercise_vios_backup(object(), state)
    return state


def _observations(state) -> dict[str, dict[str, Any]]:
    return {entry["operation"]: entry["observation"] for entry in state.observations}


def _mutations(vios: FakeVios) -> list[str]:
    return [
        next(m for m in MUTATIONS if m in call)
        for call in vios.calls
        if any(m in call for m in MUTATIONS)
    ]


@pytest.mark.asyncio
async def test_other_groups_skip_without_any_call(monkeypatch):
    vios = FakeVios()

    state = await _run(monkeypatch, vios, group="round2")

    assert vios.calls == []
    assert [row["status"] for row in state.results] == ["SKIP"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        {"rmc_before": "inactive\n"},
        {"serial": "server,0\n"},
        {"partitions": f"{LPAR},aixlinux,Running\n{VIOS},vioserver,Running\n"},
        {"partitions": f"{LPAR},aixlinux,Not Activated\nother,aixlinux,Running\n"},
        {"vioses": (VIOS, "vios-B")},
        {"mapped": False},
    ],
    ids=[
        "rmc-inactive",
        "no-console",
        "activated",
        "second-client",
        "two-vioses",
        "no-disk-mapping",
    ],
)
async def test_a_failed_precondition_changes_nothing(monkeypatch, fault):
    vios = FakeVios(**fault)

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == []
    assert state.observations == []
    assert any(row["status"] == "SKIP" for row in state.results)


@pytest.mark.asyncio
async def test_round_trip_promotes_all_three_operations(monkeypatch):
    vios = FakeVios()

    state = await _run(monkeypatch, vios)

    observations = _observations(state)
    assert {op: obs["result"] for op, obs in observations.items()} == {
        "vios.list_backups": "passed",
        "vios.backup": "passed",
        "vios.restore": "passed",
    }
    assert observations["vios.backup"]["cleanup"] == "passed"
    assert _mutations(vios) == [
        "hmc_backup_vios",
        "rmvdev",
        "hmc_restore_vios",
        "rmviosbk",
    ]
    assert vios.backups == []
    assert state.artifacts.vios_backup_mapping == "vhost0/lp3-disk"
    assert state.artifacts.vios_backup_vios_uuid == VIOS_UUID
    assert state.artifacts.vios_backup_name.startswith("hmcpctl-live-st37-")


@pytest.mark.asyncio
async def test_a_malformed_parsed_listing_fails_but_does_not_block_the_restore(
    monkeypatch,
):
    vios = FakeVios(listing_malformed=True)

    state = await _run(monkeypatch, vios)

    observations = _observations(state)
    assert observations["vios.list_backups"]["result"] == "failed"
    assert "backup-newly-listed" not in observations["vios.backup"]["assertions"]
    assert observations["vios.restore"]["result"] == "passed"
    assert observations["vios.backup"]["cleanup"] == "passed"


@pytest.mark.asyncio
async def test_a_restore_that_loses_the_mapping_falls_back_to_mkvdev(monkeypatch):
    vios = FakeVios(restore_restores=False)

    state = await _run(monkeypatch, vios)

    observations = _observations(state)
    assert observations["vios.restore"]["result"] == "failed"
    assert "mapping-restored" not in observations["vios.restore"]["assertions"]
    mkvdev = next(call for call in vios.calls if "mkvdev" in call)
    assert '-c "mkvdev -vdev lp3-vd1 -vadapter vhost0 -dev lp3-disk"' in mkvdev
    assert _mutations(vios)[-2:] == ["mkvdev", "rmviosbk"]


@pytest.mark.asyncio
async def test_a_timed_out_restore_call_is_judged_but_left_alone(monkeypatch):
    """The HMC may still be restoring: assert what is there, change nothing."""
    vios = FakeVios(restore_status="FAIL", restore_restores=False)

    state = await _run(monkeypatch, vios)

    observations = _observations(state)
    assert observations["vios.restore"]["result"] == "failed"
    assert _mutations(vios) == ["hmc_backup_vios", "rmvdev", "hmc_restore_vios"]
    assert observations["vios.backup"]["cleanup"] == "not-run"


@pytest.mark.asyncio
async def test_a_restore_that_failed_outright_still_falls_back_and_cleans_up(
    monkeypatch,
):
    vios = FakeVios(
        restore_status="FAIL",
        restore_restores=False,
        restore_error="SSH command 'rstviosbk' failed with exit status 1: HSCL0000",
    )

    state = await _run(monkeypatch, vios)

    assert _mutations(vios)[-2:] == ["mkvdev", "rmviosbk"]
    assert _observations(state)["vios.backup"]["cleanup"] == "passed"


@pytest.mark.asyncio
async def test_a_dropped_session_during_the_restore_is_left_alone(monkeypatch):
    vios = FakeVios(
        restore_status="FAIL",
        restore_restores=False,
        restore_error="SSH command failed: Connection lost",
    )

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == ["hmc_backup_vios", "rmvdev", "hmc_restore_vios"]
    assert _observations(state)["vios.backup"]["cleanup"] == "not-run"


@pytest.mark.asyncio
async def test_a_short_ssh_timeout_changes_nothing(monkeypatch):
    vios = FakeVios()
    state = _state(monkeypatch, vios)
    monkeypatch.setenv("HMC_SSH_TIMEOUT", "300")

    await vios_backup.exercise_vios_backup(object(), state)

    assert vios.calls == []
    assert "HMC_SSH_TIMEOUT" in state.results[0]["note"]


@pytest.mark.asyncio
async def test_restore_and_removal_use_the_name_the_catalog_lists(monkeypatch):
    """Live, V10R3: `-f <name>` is cataloged as `<name>.tar.gz`, and rstviosbk
    and rmviosbk refuse the bare name with HSCLC455."""
    vios = FakeVios()
    restored: list[str] = []
    answer = vios.answer

    def spy(tool, kwargs):
        if tool == "hmc_restore_vios":
            restored.append(kwargs["backup_name"])
        return answer(tool, kwargs)

    vios.answer = spy  # type: ignore[method-assign]

    state = await _run(monkeypatch, vios)

    assert restored == [state.artifacts.vios_backup_name + ".tar.gz"]
    assert _observations(state)["vios.restore"]["result"] == "passed"
    assert _observations(state)["vios.backup"]["cleanup"] == "passed"


@pytest.mark.asyncio
async def test_a_malformed_listing_takes_the_catalog_name_from_the_raw_capture(
    monkeypatch,
):
    vios = FakeVios(listing_malformed=True)

    state = await _run(monkeypatch, vios)

    assert _observations(state)["vios.restore"]["result"] == "passed"
    assert vios.backups == []


@pytest.mark.asyncio
async def test_a_vios_that_never_answers_keeps_the_backup_and_issues_nothing(
    monkeypatch,
):
    # The mapping reads back, but the HMC may still be restoring from the backup.
    vios = FakeVios(restore_status="FAIL", ioslevel_answers=False)

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == ["hmc_backup_vios", "rmvdev", "hmc_restore_vios"]
    assert _observations(state)["vios.backup"]["cleanup"] == "not-run"
    assert vios.backups


@pytest.mark.asyncio
async def test_an_unreadable_state_after_the_restore_permits_no_mutation(monkeypatch):
    vios = FakeVios(snapshot_read_fails_after_restore=True)

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == ["hmc_backup_vios", "rmvdev", "hmc_restore_vios"]
    assert _observations(state)["vios.restore"]["result"] == "failed"


@pytest.mark.asyncio
async def test_a_failed_backup_changes_nothing_else(monkeypatch):
    vios = FakeVios(backup_fails=True)

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == ["hmc_backup_vios"]
    observations = _observations(state)
    assert observations["vios.backup"]["result"] == "failed"
    assert "vios.restore" not in observations
    # The listing had nothing of this run's to show, so it promotes nothing.
    assert "vios.list_backups" not in observations


@pytest.mark.asyncio
async def test_a_backup_still_listed_after_rmviosbk_fails_cleanup(monkeypatch):
    vios = FakeVios(rmviosbk_removes=False)

    state = await _run(monkeypatch, vios)

    backup = _observations(state)["vios.backup"]
    assert backup["cleanup"] == "failed"
    assert backup["result"] == "failed"


@pytest.mark.asyncio
async def test_reordered_listings_still_equal_the_baseline(monkeypatch):
    vios = FakeVios(reorder=True)

    state = await _run(monkeypatch, vios)

    assert "baseline-restored" in _observations(state)["vios.restore"]["assertions"]


@pytest.mark.asyncio
async def test_a_changed_sea_listing_fails_the_baseline(monkeypatch):
    vios = SeaChangingVios()

    state = await _run(monkeypatch, vios)

    restore = _observations(state)["vios.restore"]
    assert restore["result"] == "failed"
    assert "baseline-restored" not in restore["assertions"]
    assert "mapping-restored" in restore["assertions"]
    # Off baseline, the backup is the way back: it is kept, not removed.
    assert "rmviosbk" not in _mutations(vios)
    assert _observations(state)["vios.backup"]["cleanup"] == "not-run"
    assert any(
        row["tool"].startswith("final compare") and row["status"] == "FAIL"
        for row in state.results
    )


@pytest.mark.asyncio
async def test_a_backup_nothing_lists_is_not_restored(monkeypatch):
    vios = FakeVios(backup_invisible=True)

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == ["hmc_backup_vios"]
    assert "vios.restore" not in _observations(state)


@pytest.mark.asyncio
async def test_rmc_that_never_returns_after_the_restore_stops_every_change(
    monkeypatch,
):
    """Operator ruling: no mkvdev and no rmviosbk until RMC is back."""
    vios = FakeVios(restore_restores=False, rmc_returns=False)

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == ["hmc_backup_vios", "rmvdev", "hmc_restore_vios"]
    assert _observations(state)["vios.restore"]["result"] == "failed"
    assert _observations(state)["vios.backup"]["cleanup"] == "not-run"
    assert not any("ioslevel" in call for call in vios.calls)


@pytest.mark.asyncio
async def test_a_rest_feed_that_still_lists_the_removed_vtd_stops_the_restore(
    monkeypatch,
):
    """The VIOS says the VTD is gone; the lagging REST feed does not agree yet."""
    vios = FakeVios(rest_lags=True)

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == ["hmc_backup_vios", "rmvdev"]
    assert "vios.restore" not in _observations(state)
    assert _observations(state)["vios.backup"]["cleanup"] == "not-run"


@pytest.mark.asyncio
async def test_a_vios_lsmap_that_disagrees_with_rest_changes_nothing(monkeypatch):
    vios = FakeVios(vadapter_disagrees=True)

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == []
    assert state.observations == []


@pytest.mark.asyncio
async def test_a_vios_that_still_shows_the_vtd_is_not_restored(monkeypatch):
    """REST reports the VTD gone; the VIOS's own lsmap still lists it."""
    vios = FakeVios(rmvdev_ignored=True)

    state = await _run(monkeypatch, vios)

    assert _mutations(vios) == ["hmc_backup_vios", "rmvdev"]
    assert "vios.restore" not in _observations(state)


#: Every path on which the arm keeps a backup it made (#1349 pass 4).
KEEP_PATHS = {
    "final-compare-fails": SeaChangingVios,
    "late-lsmap-disagreement": lambda: FakeVios(late_vadapter_disagrees=True),
    "unreadable-read-after-restore": lambda: FakeVios(
        snapshot_read_fails_after_restore=True
    ),
    "unsettled-restore": lambda: FakeVios(restore_status="FAIL"),
}


@pytest.mark.parametrize("make", KEEP_PATHS.values(), ids=KEEP_PATHS.keys())
def test_every_kept_backup_reads_as_off_baseline_in_recovery(
    monkeypatch, tmp_path, capsys, make
):
    """Recovery never offers to remove the backup that is the way back."""
    vios = make()
    # Synchronous, because recovery's `main` runs its own event loop.
    state = asyncio.run(_run(monkeypatch, vios))
    assert vios.backups and "rmviosbk" not in _mutations(vios)
    vios.snapshot_read_fails_after_restore = False  # readable again by recovery time
    document = {
        "run": {"subtasks": [37]},
        "config": {"system_name": SYSTEM, "lp3_name": LPAR},
        "artifacts": dataclasses.asdict(state.artifacts),
        "results": state.results,
    }

    async def read_only(tool, **arguments):
        recovery.guard_read_only(tool, arguments)
        return vios.answer(tool, arguments)

    async def checks(
        pcie,
        partition,
        inputs,
        network_inputs=None,
        users=False,
        lpar_config=None,
        lpar_power=None,
        round2=None,
    ):
        return await recovery.check_vios_backup(read_only, inputs)

    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "_run_checks", checks)
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document, default=str), encoding="utf-8")

    assert recovery.main(["--results", str(path)]) == 1
    output = capsys.readouterr().out
    assert "VIOS off baseline, backup kept" in output
    assert "clear with:  rmviosbk" not in output


def test_exercise_stays_within_function_size_guidance():
    source, _ = inspect.getsourcelines(vios_backup.exercise_vios_backup)

    assert len(source) <= 100


@pytest.mark.asyncio
async def test_backup_removal_follows_all_final_baseline_checks(monkeypatch):
    vios = FakeVios()
    events = []
    snapshot = vios_backup._Arm.snapshot
    compare = vios_backup._Arm.compare
    vadapter = vios_backup._Arm.vadapter_shows
    run = vios_backup._Arm.run

    async def read_snapshot(arm, target, label):
        result = await snapshot(arm, target, label)
        if label == "final":
            events.append("final snapshot")
        return result

    def compare_snapshot(arm, baseline, after, label="baseline compare"):
        result = compare(arm, baseline, after, label)
        if label == "final compare":
            events.append("final compare")
        return result

    async def read_vadapter(arm, target):
        result = await vadapter(arm, target)
        if events:
            events.append("final vadapter")
        return result

    async def run_command(arm, label, cmd):
        if label == "rmviosbk":
            events.append("backup removal")
        return await run(arm, label, cmd)

    monkeypatch.setattr(vios_backup._Arm, "snapshot", read_snapshot)
    monkeypatch.setattr(vios_backup._Arm, "compare", compare_snapshot)
    monkeypatch.setattr(vios_backup._Arm, "vadapter_shows", read_vadapter)
    monkeypatch.setattr(vios_backup._Arm, "run", run_command)

    await _run(monkeypatch, vios)

    assert events == [
        "final snapshot",
        "final compare",
        "final vadapter",
        "backup removal",
    ]


@pytest.mark.asyncio
async def test_an_exception_in_the_final_read_propagates_without_removal(monkeypatch):
    vios = FakeVios()
    snapshot = vios_backup._Arm.snapshot

    async def failing_final_read(arm, target, label):
        if label == "final":
            raise RuntimeError("final read interrupted")
        return await snapshot(arm, target, label)

    monkeypatch.setattr(vios_backup._Arm, "snapshot", failing_final_read)

    with pytest.raises(RuntimeError, match="final read interrupted"):
        await _run(monkeypatch, vios)

    assert vios.backups
    assert "rmviosbk" not in _mutations(vios)
