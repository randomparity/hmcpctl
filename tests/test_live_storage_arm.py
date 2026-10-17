"""The storage live arm (ST0, ST3, ST40; #1348) against a scripted HMC and VIOS."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

_RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "live_test_runner.py"
sys.path.insert(0, str(_RUNNER_PATH.parent))
from live_test import observation, storage, storage_lifecycle  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("hmc_live_test_runner", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = sys.modules.get(_SPEC.name) or importlib.util.module_from_spec(_SPEC)
if _SPEC.name not in sys.modules:
    sys.modules[_SPEC.name] = runner
    _SPEC.loader.exec_module(runner)

SYSTEM = "sys-A"
LPAR = "sys-A-lp3"
LPAR_UUID = "0000AAAA-0000-4000-8000-000000000003"
VIOS = "vios-A-uuid"
VIOS_ID = 1
ROOTVG = "vg-root-uuid"
CONFIGURED_VG = "vg-cfg-uuid"
OPERATOR_DISK = ("vhost0/vtscsi0", LPAR_UUID, "VirtualDisk", "op-disk")
VIOS_ROWS = {"3,sys-A-lp3,2"}
LPAR_ROWS = {"2,vios-A,3"}
MUTATIONS = {
    "hmc_create_virtual_disk",
    "hmc_delete_virtual_disk",
    "hmc_map_storage_to_lpar",
    "hmc_detach_storage_mapping",
    "hmc_attach_disk_to_lpar",
}


def _failure(text: str) -> observation.CallFailure:
    return observation.classify_failure(RuntimeError(text))


@dataclass
class FakeHMC:
    """A VIOS holding the operator's volume and mapping; knobs inject behaviour."""

    configured_name: str
    volumes: dict[str, int] = field(
        default_factory=lambda: {"op-disk": 102400, "VMLibrary": 10240}
    )
    free_mib: int = 448 * 1024
    mappings: set[tuple[str, str, str, str]] = field(
        default_factory=lambda: {OPERATOR_DISK}
    )
    vios_rows: set[str] = field(default_factory=lambda: set(VIOS_ROWS))
    lpar_rows: set[str] = field(default_factory=lambda: set(LPAR_ROWS))
    lpar_state: str = "Not Activated"
    vios_groups: str | None = None
    clusters: list[dict[str, Any]] = field(default_factory=list)
    pools: list[dict[str, Any]] = field(default_factory=list)
    absent_pool: tuple[str, Any] = ("PASS", "")
    create_refused: bool = False
    map_fails_leaving_adapter: bool = False
    guarded_delete_allowed: bool = False
    guarded_delete_ignored: bool = False
    guarded_delete_errors: bool = False
    detach_fails: bool = False
    detach_leaves_adapter: bool = False
    attach_map_fails: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def snapshot(self) -> tuple[object, ...]:
        return (
            dict(self.volumes),
            self.free_mib,
            set(self.mappings),
            set(self.vios_rows),
            set(self.lpar_rows),
        )

    def _volume_listing(self) -> str:
        rows = "".join(
            f"{name}  jfs2  {size // 512}  {size // 512}  1  open/syncd  N/A\n"
            for name, size in sorted(self.volumes.items())
        )
        return (
            f"{self.configured_name}:\n"
            "LV NAME             TYPE       LPs     PPs     PVs  LV STATE      "
            f"MOUNT POINT\n{rows}"
        )

    def _command(self, cmd: str) -> tuple[str, Any]:
        if "lsvg -lv" in cmd:
            return "PASS", self._volume_listing()
        if cmd.endswith("-c lsvg"):
            listing = self.vios_groups
            return "PASS", listing or f"rootvg\n{self.configured_name}\n"
        rows = self.vios_rows if f"lpar_ids={VIOS_ID}" in cmd else self.lpar_rows
        return "PASS", "\n".join(sorted(rows)) or "No results were found."

    def _create(self, name: str, mib: int) -> tuple[str, Any]:
        if self.create_refused:
            return "FAIL", _failure("HMCError: HTTP 406 not acceptable")
        self.volumes[name] = mib
        self.free_mib -= mib
        return "PASS", {}

    def _delete(self, name: str) -> tuple[str, Any]:
        mapped = any(row[3] == name for row in self.mappings)
        if mapped and self.guarded_delete_errors:
            return "FAIL", _failure("HMCError: POST failed (HTTP 500)")
        if mapped and self.guarded_delete_ignored:
            return "PASS", {}
        if mapped and not self.guarded_delete_allowed:
            return "FAIL", _failure(
                f"HMCError: Cannot delete virtual disk {name!r}: it is mapped to "
                "LPAR 'sys-A-lp3'. Use detach_storage_mapping first to remove the "
                "mapping."
            )
        if name not in self.volumes:
            return "FAIL", _failure("HMCError: HTTP 404")
        self.free_mib += self.volumes.pop(name)
        return "PASS", {}

    def _map(self, name: str) -> tuple[str, Any]:
        self.vios_rows.add("5,sys-A-lp3,4")
        if self.map_fails_leaving_adapter:
            return "FAIL", _failure("HMCError: HTTP 500 REST0269")
        self.lpar_rows.add("4,vios-A,5")
        self.mappings.add(("vhost1/vtscsi1", LPAR_UUID, "VirtualDisk", name))
        return "PASS", {"lpar_uuid": LPAR_UUID, "change_location": {}}

    def _detach(self, mapping_id: str) -> tuple[str, Any]:
        if self.detach_fails:
            return "FAIL", _failure("HMCError: HTTP 500")
        self.mappings = {row for row in self.mappings if row[0] != mapping_id}
        if not self.detach_leaves_adapter:
            self.vios_rows.discard("5,sys-A-lp3,4")
            self.lpar_rows.discard("4,vios-A,5")
        return "PASS", {"mapping_id": mapping_id}

    def _attach(self, name: str, mib: int) -> tuple[str, Any]:
        self._create(name, mib)
        if self.attach_map_fails:
            steps = [
                {"step": "create_disk", "status": "ok"},
                {"step": "storage", "status": "error"},
            ]
            return "PASS", {"workflow_completed": False, "steps": steps}
        self._map(name)
        steps = [
            {"step": "create_disk", "status": "ok"},
            {"step": "storage", "status": "ok"},
        ]
        return "PASS", {"workflow_completed": True, "steps": steps}

    async def call(self, _client, tool: str, **kwargs: Any) -> tuple[str, Any]:
        self.calls.append((tool, kwargs))
        if tool == "hmc_list_vios":
            return "PASS", [{"UUID": VIOS, "Resource": {"PartitionID": str(VIOS_ID)}}]
        if tool == "hmc_get_lpar":
            return "PASS", {"uuid": LPAR_UUID}
        if tool == "hmc_get_lpar_state":
            return "PASS", self.lpar_state
        if tool == "hmc_list_volume_groups":
            return "PASS", [
                {"uuid": ROOTVG, "name": "rootvg", "free_space_gib": 100},
                {
                    "uuid": CONFIGURED_VG,
                    "name": self.configured_name,
                    "free_space_gib": self.free_mib / 1024,
                    "free_space_diagnostic": None,
                },
            ]
        if tool == "hmc_run_command":
            return self._command(kwargs["cmd"])
        if tool == "hmc_list_storage_mappings":
            return "PASS", [
                dict(zip(("id", "lpar_uuid", "backing_kind", "backing_name"), row))
                for row in sorted(self.mappings)
            ]
        if tool == "hmc_list_clusters":
            return "PASS", self.clusters
        if tool == "hmc_list_shared_storage_pools":
            return "PASS", self.pools
        if tool == "hmc_get_shared_storage_pool":
            listed = [p for p in self.pools if p["UUID"] == kwargs["ssp_uuid"]]
            return ("PASS", listed[0]) if listed else self.absent_pool
        if tool == "hmc_create_virtual_disk":
            return self._create(kwargs["disk_name"], kwargs["capacity_mib"])
        if tool == "hmc_delete_virtual_disk":
            return self._delete(kwargs["disk_name"])
        if tool == "hmc_map_storage_to_lpar":
            return self._map(kwargs["storage_name"])
        if tool == "hmc_detach_storage_mapping":
            return self._detach(kwargs["mapping_id"])
        if tool == "hmc_attach_disk_to_lpar":
            return self._attach(kwargs["disk_name"], kwargs["capacity_mib"])
        return "PASS", {}


@pytest.fixture
def arm(monkeypatch):
    """A fresh storage-arm run state on synthetic names, and the HMC it talks to."""
    config = replace(
        runner.LiveTestConfig(),
        system_name=SYSTEM,
        lp3_name=LPAR,
        protected_lpar_names=("vios-A",),
    )
    state = runner.RunState(config=config, group="storage")
    hmc = FakeHMC(configured_name=config.vdisk_volume_group_name)
    monkeypatch.setattr(runner.RunState, "call", hmc.call)
    return state, hmc


async def _run(state, *subtasks: int) -> None:
    for number in subtasks:
        await runner.SUBTASKS[number](None, state)


def _observations(state) -> dict[str, list[dict[str, Any]]]:
    seen: dict[str, list[dict[str, Any]]] = {}
    for row in state.observations:
        seen.setdefault(row["operation"], []).append(row["observation"])
    return seen


def _held(state, operation: str) -> list[str]:
    (only,) = _observations(state)[operation]
    return only["assertions"]


def _manual(state) -> list[dict[str, Any]]:
    return [r for r in state.results if "MANUAL RECOVERY" in str(r.get("note", ""))]


def _mutations(hmc) -> list[str]:
    return [tool for tool, _ in hmc.calls if tool in MUTATIONS]


# ---------------------------------------------------------------------------
# The whole arm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_arm_round_trips_two_disks_and_leaves_the_vios_as_found(arm):
    state, hmc = arm
    before = hmc.snapshot()

    await _run(state, *runner.SUBTASK_GROUPS["storage"])

    assert hmc.snapshot() == before
    observations = _observations(state)
    for operation in (
        "storage.list_volume_groups",
        "cluster.get_pool",
        "storage.create_disk",
        "storage.map",
        "storage.detach_mapping",
        "storage.delete_disk",
        "storage.attach_disk",
    ):
        assert [o["result"] for o in observations[operation]] == ["passed"], operation
    # Empty feeds prove no entry shape, so they never promote.
    assert "cluster.list" not in observations
    assert "cluster.list_pools" not in observations
    assert "refused-while-mapped" in _held(state, "storage.delete_disk")
    assert "volume-survives" in _held(state, "storage.detach_mapping")
    assert state.artifacts.storage_disk_name is None
    assert _manual(state) == []
    assert _mutations(hmc) == [
        "hmc_create_virtual_disk",
        "hmc_map_storage_to_lpar",
        "hmc_delete_virtual_disk",
        "hmc_detach_storage_mapping",
        "hmc_delete_virtual_disk",
        "hmc_attach_disk_to_lpar",
        "hmc_detach_storage_mapping",
        "hmc_delete_virtual_disk",
    ]


@pytest.mark.asyncio
async def test_the_arm_touches_only_run_named_volumes_and_their_mappings(arm):
    state, hmc = arm

    await _run(state, *runner.SUBTASK_GROUPS["storage"])

    names = {
        kwargs.get("disk_name") or kwargs.get("storage_name")
        for tool, kwargs in hmc.calls
        if tool in MUTATIONS - {"hmc_detach_storage_mapping"}
    }
    assert names and all(storage_lifecycle.is_run_disk_name(n) for n in names)
    detached = {
        kwargs["mapping_id"]
        for tool, kwargs in hmc.calls
        if tool == "hmc_detach_storage_mapping"
    }
    assert detached == {"vhost1/vtscsi1"}


@pytest.mark.asyncio
async def test_st40_runs_only_in_its_own_arm(arm):
    state, hmc = arm
    state.group = "round2"

    await _run(state, 40)

    assert hmc.calls == []
    assert _mutations(hmc) == []
    assert any(r["note"] == "runs only in the storage arm" for r in state.results)


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_protected_test_partition_is_a_gap(arm):
    state, hmc = arm
    state.config = replace(state.config, protected_lpar_names=(LPAR,))

    await _run(state, 0, 40)

    assert _mutations(hmc) == []
    assert any("protected by operator config" in r["note"] for r in state.results)


@pytest.mark.parametrize(
    "names",
    [
        {"vdisk_volume_group_name": "datavg; rmlv x"},
        {"system_name": "sys A"},
    ],
)
@pytest.mark.asyncio
async def test_a_name_the_recovery_listing_cannot_carry_is_refused(arm, names):
    state, hmc = arm
    state.config = replace(state.config, **names)

    await _run(state, 0, 40)

    assert _mutations(hmc) == []
    assert not any("lsvg -lv" in str(kwargs) for _, kwargs in hmc.calls)
    with pytest.raises(ValueError):
        storage_lifecycle.volume_listing(SYSTEM, VIOS_ID, "datavg; rmlv x")


@pytest.mark.asyncio
async def test_a_running_test_partition_is_not_mapped(arm):
    state, hmc = arm
    hmc.lpar_state = "Running"

    await _run(state, 0, 40)

    assert _mutations(hmc) == []
    assert any("dynamic reconfiguration" in r["note"] for r in state.results)


@pytest.mark.asyncio
async def test_too_little_free_space_creates_nothing(arm):
    state, hmc = arm
    hmc.free_mib = 512

    await _run(state, 0, 40)

    assert _mutations(hmc) == []
    assert any("MiB free in the volume group" in r["note"] for r in state.results)


@pytest.mark.asyncio
async def test_a_restored_run_disk_still_listed_blocks_a_new_one(arm):
    state, hmc = arm
    hmc.volumes["hpctl0a1b2c3d"] = 1024
    state.artifacts.storage_disk_name = "hpctl0a1b2c3d"

    await _run(state, 0, 40)

    assert _mutations(hmc) == []
    assert state.artifacts.storage_disk_name == "hpctl0a1b2c3d"


@pytest.mark.asyncio
async def test_a_restored_run_disk_already_gone_is_forgotten(arm):
    state, hmc = arm
    state.artifacts.storage_disk_name = "hpctl0a1b2c3d"

    await _run(state, 0, 40)

    assert "hmc_create_virtual_disk" in _mutations(hmc)
    assert state.artifacts.storage_disk_name is None


@pytest.mark.asyncio
async def test_the_disk_is_named_before_it_is_created(arm, monkeypatch):
    state, hmc = arm
    named = []

    async def spy(_state, _client, tool, **kwargs):
        if tool == "hmc_create_virtual_disk":
            named.append(state.artifacts.storage_disk_name == kwargs["disk_name"])
        return await hmc.call(_client, tool, **kwargs)

    monkeypatch.setattr(runner.RunState, "call", spy)
    await _run(state, 0, 40)

    assert named and all(named)


# ---------------------------------------------------------------------------
# Failures stay failures, and residue stops the arm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_create_is_a_failed_observation_not_a_skip(arm):
    """Round2 ST14 declared this 406 expected; ST40 never does."""
    state, hmc = arm

    hmc.create_refused = True
    await _run(state, 0, 40)

    (create,) = _observations(state)["storage.create_disk"]
    assert create["result"] == "failed"
    assert "create-accepted" not in create["assertions"]
    assert create["cleanup"] == "passed"
    assert _mutations(hmc) == ["hmc_create_virtual_disk"]
    assert state.artifacts.storage_disk_name is None


@pytest.mark.asyncio
async def test_a_failed_map_that_leaves_an_adapter_is_reported_and_stops_attach(arm):
    state, hmc = arm
    hmc.map_fails_leaving_adapter = True

    await _run(state, 0, 40)

    assert "mapping-listed" not in _held(state, "storage.map")
    assert _observations(state)["storage.map"][0]["result"] == "failed"
    assert any("vSCSI adapters differ" in r["note"] for r in _manual(state))
    # The unmapped volume is still removed; the adapter is the operator's to clear.
    assert _mutations(hmc) == [
        "hmc_create_virtual_disk",
        "hmc_map_storage_to_lpar",
        "hmc_delete_virtual_disk",
    ]
    assert "refused-while-mapped" not in str(
        _observations(state)["storage.delete_disk"]
    )
    assert "storage.attach_disk" not in _observations(state)


@pytest.mark.asyncio
async def test_a_guarded_delete_that_goes_through_stops_the_scenario(arm):
    state, hmc = arm
    hmc.guarded_delete_allowed = True

    await _run(state, 0, 40)

    assert any("was not refused" in r["note"] for r in _manual(state))
    assert "hmc_detach_storage_mapping" not in _mutations(hmc)
    observations = _observations(state)
    (delete,) = observations["storage.delete_disk"]
    assert delete["result"] == "failed" and delete["cleanup"] == "failed"
    assert observations["storage.map"][0]["cleanup"] == "failed"
    assert observations["storage.create_disk"][0]["cleanup"] == "failed"


@pytest.mark.asyncio
async def test_a_guarded_delete_accepted_without_effect_is_not_a_refusal(arm):
    state, hmc = arm
    hmc.guarded_delete_ignored = True

    await _run(state, 0, 40)

    (delete,) = _observations(state)["storage.delete_disk"]
    assert delete["result"] == "failed"
    assert "refused-while-mapped" not in delete["assertions"]


@pytest.mark.asyncio
async def test_only_the_guards_own_refusal_counts_as_refused(arm):
    """An HMC error while the volume is mapped would hide a guard that missed it."""
    state, hmc = arm
    hmc.guarded_delete_errors = True

    await _run(state, 0, 40)

    assert "refused-while-mapped" not in _held(state, "storage.delete_disk")


@pytest.mark.asyncio
async def test_an_unreadable_listing_after_the_guarded_delete_is_not_a_guard_miss(
    arm, monkeypatch
):
    state, hmc = arm
    listing = hmc._volume_listing

    def flaky():
        # Unreadable from the guarded delete on.
        return None if "hmc_delete_virtual_disk" in _mutations(hmc) else listing()

    monkeypatch.setattr(hmc, "_volume_listing", flaky)
    await _run(state, 0, 40)

    assert "storage.delete_disk" not in _observations(state)
    assert any(
        "cannot confirm whether the guarded" in r["note"] for r in _manual(state)
    )
    assert "hmc_detach_storage_mapping" not in _mutations(hmc)


@pytest.mark.asyncio
async def test_a_failed_detach_never_deletes_the_mapped_volume(arm):
    state, hmc = arm
    hmc.detach_fails = True

    await _run(state, 0, 40)

    (detach,) = _observations(state)["storage.detach_mapping"]
    assert detach["result"] == "failed"
    assert "mapping-absent" not in detach["assertions"]
    assert _mutations(hmc).count("hmc_delete_virtual_disk") == 1  # the guarded one
    assert any("may still be mapped" in r["note"] for r in _manual(state))
    assert state.artifacts.storage_disk_name is not None


@pytest.mark.asyncio
async def test_a_detach_that_leaves_the_adapter_pair_fails_its_compare(arm):
    state, hmc = arm
    hmc.detach_leaves_adapter = True

    await _run(state, 0, 40)

    held = _held(state, "storage.detach_mapping")
    assert "mapping-absent" in held
    assert "adapters-equal-baseline" not in held
    assert _observations(state)["storage.map"][0]["cleanup"] == "failed"
    assert "storage.attach_disk" not in _observations(state)


@pytest.mark.asyncio
async def test_a_partial_attach_is_cleaned_up_from_the_listings(arm):
    """The attach tool reports a failed map step as `error`, not as a failure."""
    state, hmc = arm
    hmc.attach_map_fails = True
    before = hmc.snapshot()

    await _run(state, 0, 40)

    (attach,) = _observations(state)["storage.attach_disk"]
    assert attach["result"] == "failed"
    assert attach["assertions"] == ["volume-listed"]
    assert attach["cleanup"] == "passed"
    assert hmc.snapshot() == before
    assert state.artifacts.storage_disk_name is None


# ---------------------------------------------------------------------------
# ST3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_st3_compares_the_groups_with_the_vios_own_listing(arm):
    state, hmc = arm
    hmc.vios_groups = "rootvg\nothervg\n"

    await _run(state, 0, 3)

    (groups,) = _observations(state)["storage.list_volume_groups"]
    assert groups["result"] == "failed"
    assert groups["assertions"] == [
        "configured-group-listed",
        "no-free-space-diagnostic",
    ]


@pytest.mark.asyncio
async def test_st3_promotes_listed_clusters_and_reads_back_a_listed_pool(arm):
    state, hmc = arm
    hmc.clusters = [{"UUID": "c-1", "Resource": {"ClusterName": "cluster-A"}}]
    hmc.pools = [{"UUID": "p-1", "Resource": {"StoragePoolName": "pool-A"}}]

    await _run(state, 0, 3)

    assert _held(state, "cluster.list") == ["clusters-identified"]
    assert _held(state, "cluster.list_pools") == ["pools-identified"]
    assert _held(state, "cluster.get_pool") == [
        "absent-pool-not-returned",
        "listed-pool-returned",
    ]


@pytest.mark.asyncio
async def test_st3_fails_an_unnamed_cluster(arm):
    state, hmc = arm
    hmc.clusters = [{"UUID": "c-1", "Resource": {}}]

    await _run(state, 0, 3)

    assert _observations(state)["cluster.list"][0]["result"] == "failed"


@pytest.mark.parametrize(
    ("answer", "holds"),
    [
        (("PASS", ""), True),
        (("PASS", None), True),
        (("FAIL", _failure("HMCError: GET failed (HTTP 404)")), True),
        (("FAIL", _failure("HMCError: SharedStoragePool not found")), True),
        (("FAIL", _failure("HMCError: GET failed (HTTP 500)")), False),
        (("PASS", {"UUID": "someone-else"}), False),
    ],
)
@pytest.mark.asyncio
async def test_an_absent_pool_must_come_back_empty_or_not_found(arm, answer, holds):
    state, hmc = arm
    hmc.absent_pool = answer

    await _run(state, 0, 3)

    (pool,) = _observations(state)["cluster.get_pool"]
    assert (pool["result"] == "passed") is holds


@pytest.mark.parametrize(
    ("listing", "names"),
    [
        (
            "datavg:\nLV NAME  TYPE  LPs\nlv-a  jfs2  4\nlv-b  jfs2  2\n",
            {"lv-a", "lv-b"},
        ),
        ("datavg:\nLV NAME  TYPE  LPs\n", set()),
        ("Cluster does not exist.", None),
        (None, None),
    ],
)
def test_volume_names_reads_only_an_lsvg_lv_listing(listing, names):
    parsed = storage_lifecycle.volume_names(listing)

    assert parsed == (None if names is None else frozenset(names))


def test_volume_group_names_refuses_a_line_that_is_not_one_name():
    assert storage.volume_group_names("rootvg\ndatavg\n") == {"rootvg", "datavg"}
    assert storage.volume_group_names("HSCL2970 The IOServer command failed") is None


def test_the_run_disk_name_fits_the_vios_limit():
    assert len(storage_lifecycle.DISK_PREFIX) + 8 <= 15
    assert storage_lifecycle.is_run_disk_name("hpctl0a1b2c3d")
    assert not storage_lifecycle.is_run_disk_name("hpctl0A1B2C3D")
    assert not storage_lifecycle.is_run_disk_name("op-disk")


def test_every_registered_storage_stage_is_covered_here():
    assert runner.SUBTASK_GROUPS["storage"] == [0, 3, storage_lifecycle.SUBTASK, 42]
    assert runner.SUBTASKS[40] is storage_lifecycle.exercise_disk_lifecycle
    assert 40 not in runner.SUBTASK_GROUPS["all"]
