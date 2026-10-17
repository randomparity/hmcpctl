"""The vmedia live arm (ST16-22, #1347) against a scripted HMC and VIOS."""

from __future__ import annotations

import importlib.util
import shlex
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import click
import pytest
from typer.main import get_command

from hmcpctl import cli

_RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "live_test_runner.py"
sys.path.insert(0, str(_RUNNER_PATH.parent))
from live_test import observation, vmedia  # noqa: E402

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
OPERATOR_MEDIA = "operator_install.iso"
DISK_MAPPING = ("vhost0/vtscsi0", LPAR_UUID, "VirtualDisk", "lp3_disk")
OPERATOR_MAPPING = ("vhost0/vtopt0", LPAR_UUID, "VirtualOpticalMedia", OPERATOR_MEDIA)
BOOT_DISK = "/vdevice/v-scsi@30000003/disk@8100000000000000"
BOOT_LAN = "/vdevice/l-lan@30000002:speed=auto,duplex=auto,192.0.2.10,,192.0.2.1"
VIOS_ROWS = {"11,sys-A-lp3,2"}
LPAR_ROWS = {"2,vios-A,11"}
MUTATIONS = {
    "hmc_create_media_repository",
    "hmc_delete_media_repository",
    "hmc_create_optical_media",
    "hmc_delete_optical_media",
    "hmc_upload_iso",
    "hmc_mount_optical_media",
    "hmc_unmount_optical_media",
    "hmc_power_on_lpar",
    "hmc_power_off_lpar",
    "hmc_set_lpar_boot_order",
}


def _failure(text: str) -> observation.CallFailure:
    return observation.classify_failure(RuntimeError(text))


def _repository(size: str = "10") -> dict[str, Any]:
    return {
        "Resource": {
            "MediaRepositories": {
                "VirtualMediaRepository": {
                    "RepositoryName": "VMLibrary",
                    "RepositorySize": size,
                }
            }
        }
    }


@dataclass
class FakeHMC:
    """A VIOS holding an operator repository and mapping; knobs inject behaviour."""

    configured_vg: str
    configured_name: str
    repository_vg: str | None = ROOTVG
    second_holder: bool = False
    repository_size: str = "10"
    media: dict[str, Any] = field(default_factory=lambda: {OPERATOR_MEDIA: 2048})
    mappings: set[tuple[str, str, str, str]] = field(
        default_factory=lambda: {DISK_MAPPING, OPERATOR_MAPPING}
    )
    vios_rows: set[str] = field(default_factory=lambda: set(VIOS_ROWS))
    lpar_rows: set[str] = field(default_factory=lambda: set(LPAR_ROWS))
    lpar_state: str = "Not Activated"
    repository_read_fails: bool = False
    mount_fails_leaving_adapter: bool = False
    unmount_fails: bool = False
    unmount_leaves_adapter: bool = False
    guarded_delete_allowed: bool = False
    guarded_delete_error: str | None = None
    # A concurrent actor removes the medium; its mapping row is left as it was.
    guarded_refusal_drops_media: bool = False
    # The media listing read right after the guarded delete fails once.
    guarded_delete_fails_listing: bool = False
    listing_fails: bool = False
    optical_id_missing: bool = False
    boot_order: dict[str, Any] = field(
        default_factory=lambda: {
            "pending_boot_string": f"{BOOT_DISK} {BOOT_LAN}",
            "boot_device_list": f"{BOOT_DISK} {BOOT_LAN}",
        }
    )
    power_on_fails: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    uploads: int = 0

    def snapshot(self) -> tuple[object, ...]:
        return (
            dict(self.media),
            set(self.mappings),
            set(self.vios_rows),
            set(self.lpar_rows),
            self.lpar_state,
            self.repository_vg,
        )

    def _raw_optical(self, row: tuple[str, str, str, str]) -> dict[str, Any]:
        adapter, target = row[0].split("/")
        raw: dict[str, Any] = {
            "Storage": {"VirtualOpticalMedia": {"MediaName": row[3]}},
            "AssociatedLogicalPartition": {
                "href": f"https://hmc.example.test/rest/api/uom/LogicalPartition/{row[1]}"
            },
            "TargetDevice": {"VirtualOpticalTargetDevice": {"TargetName": target}},
        }
        if not self.optical_id_missing:
            raw["ServerAdapter"] = {"AdapterName": adapter}
        return raw

    def _scoped(self, kwargs: dict[str, Any]) -> list[tuple[str, str, str, str]]:
        lpar = kwargs.get("lpar_name_or_uuid")
        return sorted(
            row for row in self.mappings if lpar is None or row[1] == LPAR_UUID
        )

    def _mount(self, name: str) -> tuple[str, Any]:
        if name not in self.media:
            return "FAIL", _failure(f"HMCError: no media {name}")
        if self.mount_fails_leaving_adapter:
            self.vios_rows.add("12,sys-A-lp3,3")
            return "FAIL", _failure("HMCError: HTTP 500 REST0269")
        self.mappings.add(("vhost1/vtopt1", LPAR_UUID, "VirtualOpticalMedia", name))
        self.vios_rows.add("12,sys-A-lp3,3")
        self.lpar_rows.add("3,vios-A,12")
        return "PASS", {"lpar_uuid": LPAR_UUID, "change_location": {}}

    def _unmount(self, name: str) -> tuple[str, Any]:
        if self.unmount_fails:
            return "FAIL", _failure("HMCError: HTTP 500")
        rows = {row for row in self.mappings if row[3] == name}
        if not rows:
            return "FAIL", _failure(f"HMCError: Optical mapping for media {name!r}")
        self.mappings -= rows
        if not self.unmount_leaves_adapter:
            self.vios_rows.discard("12,sys-A-lp3,3")
            self.lpar_rows.discard("3,vios-A,12")
        return "PASS", {"mapping_id": "vhost1/vtopt1"}

    def _delete(self, name: str) -> tuple[str, Any]:
        mounted = any(row[3] == name for row in self.mappings)
        if mounted and self.guarded_delete_error:
            return "FAIL", _failure(self.guarded_delete_error)
        if mounted and self.guarded_delete_fails_listing:
            self.listing_fails = True
        if mounted and not self.guarded_delete_allowed:
            if self.guarded_refusal_drops_media:
                self.media.pop(name)
            return "FAIL", _failure(
                f"HMCError: Cannot delete optical media {name!r}: it is mounted on "
                f"1 LPAR(s): '{LPAR_UUID}'. Unmount the media first."
            )
        self.media.pop(name, None)
        return "PASS", {}

    async def call(self, _client, tool: str, **kwargs: Any) -> tuple[str, Any]:
        self.calls.append((tool, kwargs))
        if tool == "hmc_list_vios":
            return "PASS", [{"UUID": VIOS, "Resource": {"PartitionID": str(VIOS_ID)}}]
        if tool == "hmc_get_lpar":
            return "PASS", {"uuid": LPAR_UUID}
        if tool == "hmc_list_volume_groups":
            return "PASS", [
                {"uuid": ROOTVG, "name": "rootvg", "free_space_gib": 100},
                {
                    "uuid": self.configured_vg,
                    "name": self.configured_name,
                    "free_space_gib": 64,
                },
            ]
        if tool == "hmc_get_media_repository":
            if self.repository_read_fails:
                return "FAIL", _failure("HMCError: HTTP 500")
            vg = kwargs["vg_uuid"]
            holds = vg == self.repository_vg or (self.second_holder and vg != ROOTVG)
            return "PASS", _repository(self.repository_size) if holds else None
        if tool == "hmc_create_media_repository":
            self.repository_vg = kwargs["vg_uuid"]
            return "PASS", {}
        if tool == "hmc_list_optical_media":
            if self.listing_fails:
                self.listing_fails = False
                return "FAIL", _failure("HMCError: HTTP 500")
            return "PASS", [{"name": n, "size_mib": s} for n, s in self.media.items()]
        if tool == "hmc_create_optical_media":
            self.media[kwargs["media_name"]] = kwargs["size_mib"]
            return "PASS", {}
        if tool == "hmc_upload_iso":
            name = kwargs["media_name"]
            if name in self.media:
                return "FAIL", _failure(
                    f"FileExistsError: Media name '{name}' already exists in repository."
                )
            self.media[name] = 1024
            self.uploads += 1
            return "PASS", {"status": "uploaded", "media_name": name}
        if tool == "hmc_delete_optical_media":
            return self._delete(kwargs["media_name"])
        if tool == "hmc_mount_optical_media":
            return self._mount(kwargs["media_name"])
        if tool == "hmc_unmount_optical_media":
            return self._unmount(kwargs["media_name"])
        if tool == "hmc_list_storage_mappings":
            return "PASS", [
                dict(zip(("id", "lpar_uuid", "backing_kind", "backing_name"), row))
                for row in self._scoped(kwargs)
            ]
        if tool == "hmc_list_optical_mappings":
            return "PASS", [
                self._raw_optical(row)
                for row in self._scoped(kwargs)
                if row[2] == "VirtualOpticalMedia"
            ]
        if tool == "hmc_run_command":
            rows = (
                self.vios_rows
                if f"lpar_ids={VIOS_ID}" in kwargs["cmd"]
                else self.lpar_rows
            )
            return "PASS", "\n".join(sorted(rows)) or "No results were found."
        if tool == "hmc_get_lpar_state":
            return "PASS", self.lpar_state
        if tool == "hmc_read_lpar_boot_order":
            return "PASS", self.boot_order
        if tool == "hmc_power_on_lpar" and self.power_on_fails:
            return "FAIL", _failure("HMCError: boot job failed")
        if tool == "hmc_delete_media_repository":
            if self.media:
                return "FAIL", _failure("HMCError: Cannot delete media repository")
            self.repository_vg = None
            return "PASS", {}
        return "PASS", {}


@pytest.fixture
def arm(monkeypatch, tmp_path):
    """A fresh run state on synthetic names, and the HMC it talks to."""
    config = replace(
        runner.LiveTestConfig(),
        system_name=SYSTEM,
        lp3_name=LPAR,
        iso_path=str(tmp_path / "absent.iso"),
        iso_media_name="install.iso",
        protected_lpar_names=("vios-A",),
    )
    state = runner.RunState(config=config)
    hmc = FakeHMC(
        configured_vg="vg-cfg-uuid", configured_name=config.vdisk_volume_group_name
    )
    monkeypatch.setattr(runner.RunState, "call", hmc.call)
    monkeypatch.setattr(state.iso_http_server, "start", lambda _config: None)
    return state, hmc


async def _run(state, *subtasks: int) -> None:
    for number in subtasks:
        await runner.SUBTASKS[number](None, state)
    for row in state.results:
        _, marker, command = str(row.get("note", "")).partition("(hmcpctl storage ")
        if not marker:
            continue
        command = command.rsplit(")", 1)[0]
        # The restored-ownership remedy asks the operator to choose a listed group.
        if "<group: " in command:
            before, _, rest = command.partition("<group: ")
            _, _, after = rest.partition(">")
            command = before + "selected-vg" + after
        tokens = shlex.split(command)
        root = get_command(cli.app)
        storage = root.commands["storage"]
        leaf = storage.get_command(click.Context(storage), tokens[0])
        assert leaf is not None
        with leaf.make_context(tokens[0], tokens[1:]):
            pass


def _observations(state) -> dict[str, list[dict[str, Any]]]:
    seen: dict[str, list[dict[str, Any]]] = {}
    for row in state.observations:
        seen.setdefault(row["operation"], []).append(row["observation"])
    return seen


def _results(state, operation: str) -> list[str]:
    return [o["result"] for o in _observations(state).get(operation, [])]


def _manual(state) -> list[dict[str, Any]]:
    return [r for r in state.results if "MANUAL RECOVERY" in str(r.get("note", ""))]


def _while_mounted(state) -> dict[str, Any]:
    (row,) = [r for r in state.results if r["tool"].endswith("(while mounted)")]
    return row


def _delete_row(state) -> dict[str, Any]:
    (row,) = [r for r in state.results if r["tool"] == "hmc_delete_optical_media"]
    return row


def _refusal_unverified(state) -> list[dict[str, Any]]:
    return [
        r
        for r in state.results
        if r["tool"] == "hmc_delete_optical_media (refusal unverified)"
    ]


def _mutations(hmc) -> list[tuple[str, dict[str, Any]]]:
    return [(tool, kwargs) for tool, kwargs in hmc.calls if tool in MUTATIONS]


# ---------------------------------------------------------------------------
# The whole arm on a VIOS that already holds an operator repository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_arm_round_trips_a_blank_medium_and_leaves_the_vios_as_found(arm):
    state, hmc = arm
    before = hmc.snapshot()

    await _run(state, *runner.SUBTASK_GROUPS["vmedia"])

    assert hmc.snapshot() == before
    for operation in (
        "media.get_repository",
        "media.list",
        "media.create",
        "media.mount",
        "media.unmount",
        "media.delete",
        "storage.list_mappings",
        "media.list_mappings",
    ):
        assert _results(state, operation) == ["passed"], operation
    assert _manual(state) == []
    assert state.artifacts.vmedia_vg_uuid == ROOTVG
    assert state.artifacts.vmedia_blank_name is None


@pytest.mark.asyncio
async def test_the_arm_never_touches_the_operators_media_or_mapping(arm):
    state, hmc = arm

    await _run(state, *runner.SUBTASK_GROUPS["vmedia"])

    touched = {kwargs.get("media_name") for _, kwargs in _mutations(hmc)}
    assert OPERATOR_MEDIA not in touched
    assert touched <= {
        None,
        *(n for n in touched if n and n.startswith("hmcpctl_live_")),
    }
    assert not {"hmc_create_media_repository", "hmc_delete_media_repository"} & {
        tool for tool, _ in hmc.calls
    }


@pytest.mark.asyncio
async def test_without_an_iso_the_upload_and_boot_are_gaps_and_nothing_powers_on(arm):
    state, hmc = arm

    await _run(state, *runner.SUBTASK_GROUPS["vmedia"])

    tools = {tool for tool, _ in hmc.calls}
    assert not {"hmc_upload_iso", "hmc_power_on_lpar", "hmc_power_off_lpar"} & tools
    assert "media.upload_iso" not in _observations(state)
    skipped = [r for r in state.results if r["subtask"] in (18, 20)]
    assert skipped and all(r["status"] == "SKIP" for r in skipped)
    assert all("gap" in r["note"] for r in skipped)


@pytest.mark.asyncio
async def test_repository_create_and_delete_are_gaps_when_one_exists(arm):
    state, _ = arm

    await _run(state, 16, 17)

    skips = {r["tool"]: r["note"] for r in state.results if r["status"] == "SKIP"}
    assert "VIOS with no media repository" in skips["hmc_create_media_repository"]
    assert (
        "VIOS with no media repository" in skips["hmc_delete_media_repository (main)"]
    )
    assert "media.create_repository" not in _observations(state)


# ---------------------------------------------------------------------------
# ST16 — repository discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_st16_creates_only_when_no_group_holds_a_repository(arm):
    state, hmc = arm
    hmc.repository_vg = None

    await _run(state, 16)

    assert [t for t, _ in _mutations(hmc)] == ["hmc_create_media_repository"]
    assert state.artifacts.vmedia_repo_created
    assert state.artifacts.vmedia_vg_uuid == "vg-cfg-uuid"
    assert state.observations == []


@pytest.mark.asyncio
async def test_st16_records_a_failed_repository_read_as_failed_not_skipped(arm):
    state, hmc = arm
    hmc.repository_read_fails = True

    await _run(state, 16)

    assert _results(state, "media.get_repository") == ["failed"]
    assert state.artifacts.vmedia_vg_uuid is None
    assert _mutations(hmc) == []


@pytest.mark.asyncio
async def test_st16_refuses_two_holders(arm):
    state, hmc = arm
    hmc.second_holder = True

    await _run(state, 16, 19)

    assert state.artifacts.vmedia_vg_uuid is None
    assert _mutations(hmc) == []


@pytest.mark.asyncio
async def test_st16_reads_the_repository_size_as_decimal_gib():
    assert vmedia.repository_fields(_repository("10.0")) == ("VMLibrary", 10)
    assert vmedia.repository_fields(_repository("ten")) == ("VMLibrary", None)
    assert vmedia.repository_fields(None) == (None, None)


@pytest.mark.asyncio
async def test_st16_rederives_the_group_a_restored_document_named(arm):
    state, hmc = arm
    state.artifacts.vmedia_vg_uuid = "stale-vg"
    hmc.repository_read_fails = True

    await _run(state, 16)

    assert state.artifacts.vmedia_vg_uuid is None


# ---------------------------------------------------------------------------
# ST19 — the blank-medium round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_protected_test_partition_is_a_gap_and_the_arm_continues(arm, tmp_path):
    """The operator's protected list stops ST19 and ST20 before any HMC call."""
    state, hmc = arm
    iso = tmp_path / "install.iso"
    iso.write_bytes(b"iso")
    state.config = replace(
        state.config, iso_path=str(iso), protected_lpar_names=(LPAR,)
    )
    await _run(state, 16)
    before = len(hmc.calls)

    await _run(state, 19, 20)

    assert hmc.calls[before:] == []
    skips = [r for r in state.results if r["subtask"] in (19, 20)]
    assert skips and all(r["status"] == "SKIP" for r in skips)
    assert all(
        "protected by operator config" in r["note"] and "(gap)" in r["note"]
        for r in skips
    )
    assert not {"media.create", "media.mount", "media.unmount", "media.delete"} & set(
        _observations(state)
    )

    await _run(state, 21, 22)

    assert _results(state, "media.list_mappings") == ["passed"]
    assert not {"hmc_mount_optical_media", "hmc_unmount_optical_media"} & {
        t for t, _ in hmc.calls
    }


@pytest.mark.asyncio
async def test_st19_skips_when_the_partition_is_running(arm):
    state, hmc = arm
    hmc.lpar_state = "Running"

    await _run(state, 16, 19)

    assert _mutations(hmc) == []
    assert any("Running" in r["note"] for r in state.results if r["status"] == "SKIP")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "media", "reason"),
    [
        ("2", {OPERATOR_MEDIA: 2048}, "< 1024 MiB"),
        ("10", {OPERATOR_MEDIA: None}, "size is unknown"),
    ],
)
async def test_st19_skips_without_room_for_the_blank_medium(arm, size, media, reason):
    state, hmc = arm
    hmc.repository_size, hmc.media = size, media

    await _run(state, 16, 19)

    assert _mutations(hmc) == []
    assert any(reason in r["note"] for r in state.results if r["status"] == "SKIP")


@pytest.mark.asyncio
async def test_st19_names_the_blank_medium_before_creating_it(arm, monkeypatch):
    state, hmc = arm
    seen = []
    original = hmc.call

    async def spy(_state, _client, tool, **kwargs):
        if tool == "hmc_create_optical_media":
            seen.append(state.artifacts.vmedia_blank_name)
        return await original(_client, tool, **kwargs)

    monkeypatch.setattr(runner.RunState, "call", spy)
    await _run(state, 16, 19)

    assert seen and seen[0].startswith(vmedia.BLANK_PREFIX)
    assert len(seen[0]) == len(vmedia.BLANK_PREFIX) + 8


@pytest.mark.asyncio
async def test_a_failed_mount_that_leaves_an_adapter_is_reported_not_removed(arm):
    state, hmc = arm
    hmc.mount_fails_leaving_adapter = True

    await _run(state, 16, 19)

    assert _results(state, "media.mount") == ["failed"]
    assert [o["cleanup"] for o in _observations(state)["media.mount"]] == ["failed"]
    assert "12,sys-A-lp3,3" in str(_manual(state))
    # The medium was never mounted, so it is still removed.
    assert not any(n.startswith(vmedia.BLANK_PREFIX) for n in hmc.media)
    assert "hmc_unmount_optical_media" not in {t for t, _ in hmc.calls}


@pytest.mark.asyncio
async def test_an_unmount_that_fails_stops_before_the_delete_and_st22_clears_it(arm):
    state, hmc = arm
    hmc.unmount_fails = True

    await _run(state, 16, 19)

    assert _results(state, "media.unmount") == ["failed"]
    assert _results(state, "media.mount") == ["failed"]
    assert "media.delete" not in _observations(state)
    assert "unmount-optical-media" in str(_manual(state))
    deletes = [k for t, k in hmc.calls if t == "hmc_delete_optical_media"]
    assert len(deletes) == 1  # only the guarded attempt

    hmc.unmount_fails = False
    before_teardown = len(hmc.calls)
    await _run(state, 22)

    teardown = _mutations(replace(hmc, calls=hmc.calls[before_teardown:]))
    assert [t for t, _ in teardown] == [
        "hmc_unmount_optical_media",
        "hmc_delete_optical_media",
    ]
    assert all(k["media_name"].startswith(vmedia.BLANK_PREFIX) for _, k in teardown)
    assert hmc.media == {OPERATOR_MEDIA: 2048}
    assert state.artifacts.vmedia_blank_name is None


@pytest.mark.asyncio
async def test_an_unmount_that_leaves_the_adapter_pair_fails_its_compare(arm):
    state, hmc = arm
    hmc.unmount_leaves_adapter = True

    await _run(state, 16, 19)

    unmount = _observations(state)["media.unmount"][0]
    assert unmount["result"] == "failed"
    assert "adapters-equal-baseline" not in unmount["assertions"]
    assert _manual(state)
    assert _results(state, "media.delete") == ["passed"]


@pytest.mark.asyncio
async def test_a_delete_the_guard_lets_through_while_mounted_fails(arm):
    state, hmc = arm
    hmc.guarded_delete_allowed = True

    await _run(state, 16, 19)

    delete = _observations(state)["media.delete"][0]
    assert delete["result"] == "failed"
    assert "refused-while-mounted" not in delete["assertions"]
    assert "refused-while-mounted" in _delete_row(state)["note"]
    assert not _refusal_unverified(state)


@pytest.mark.asyncio
async def test_the_guards_refusal_while_mounted_is_expected_not_failed(arm):
    state, _hmc = arm

    await _run(state, 16, 19)

    assert _while_mounted(state)["status"] == "SKIP"
    assert (
        "refused-while-mounted" in _observations(state)["media.delete"][0]["assertions"]
    )
    assert not [r for r in state.results if r["status"] == "FAIL"]
    assert not state.gaps


@pytest.mark.asyncio
async def test_a_delete_that_fails_for_another_reason_while_mounted_fails(arm):
    state, hmc = arm
    hmc.guarded_delete_error = "HMCError: HTTP 500 REST0269"

    await _run(state, 16, 19)

    assert _while_mounted(state)["status"] == "FAIL"
    delete = _observations(state)["media.delete"][0]
    assert "refused-while-mounted" not in delete["assertions"]


@pytest.mark.asyncio
async def test_a_refusal_whose_medium_left_the_listing_is_not_counted_refused(arm):
    state, hmc = arm
    hmc.guarded_refusal_drops_media = True

    await _run(state, 16, 19)

    assert _while_mounted(state)["status"] == "SKIP"
    delete = _observations(state)["media.delete"][0]
    assert "refused-while-mounted" not in delete["assertions"]
    assert delete["result"] == "failed"


@pytest.mark.asyncio
async def test_a_refusal_followed_by_an_unreadable_listing_is_unverified(arm):
    state, hmc = arm
    hmc.guarded_delete_fails_listing = True

    await _run(state, 16, 19)

    assert _while_mounted(state)["status"] == "SKIP"
    (listing,) = [
        r
        for r in state.results
        if r["tool"] == "hmc_list_optical_media (after the guarded delete)"
    ]
    assert listing["status"] == "FAIL"
    (unverified,) = _refusal_unverified(state)
    assert unverified["result"] == "skipped"
    assert "listing" in unverified["note"]
    delete = _observations(state)["media.delete"][0]
    assert "refused-while-mounted" not in delete["assertions"]
    assert delete["result"] == "passed"
    assert "refused-while-mounted" not in _delete_row(state)["note"]
    # The final delete's own listing confirms removal, so nothing is left to recover.
    assert not _manual(state)
    assert not state.gaps


@pytest.mark.asyncio
async def test_an_accepted_delete_with_an_unreadable_listing_still_fails(arm):
    state, hmc = arm
    hmc.guarded_delete_allowed = True
    hmc.guarded_delete_fails_listing = True

    await _run(state, 16, 19)

    delete = _observations(state)["media.delete"][0]
    assert delete["result"] == "failed"
    assert "refused-while-mounted" in _delete_row(state)["note"]
    assert not _refusal_unverified(state)


# ---------------------------------------------------------------------------
# ST18 — the ISO upload, when an ISO exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_st18_uploads_under_a_run_tagged_name_and_removes_it(arm, tmp_path):
    state, hmc = arm
    iso = tmp_path / "install.iso"
    iso.write_bytes(b"iso")
    state.config = replace(state.config, iso_path=str(iso))

    await _run(state, 16, 18)

    (upload,) = _observations(state)["media.upload_iso"]
    assert upload["result"] == "passed"
    assert upload["cleanup"] == "passed"
    names = {k["media_name"] for t, k in hmc.calls if t == "hmc_upload_iso"}
    (name,) = names
    assert name.startswith("install_") and name.endswith(".iso") and len(name) == 20
    assert hmc.media == {OPERATOR_MEDIA: 2048}
    assert state.artifacts.vmedia_iso_name is None


def test_the_iso_name_keeps_the_configured_stem_and_suffix():
    config = replace(runner.LiveTestConfig(), iso_media_name="boot.media.iso")

    first, second = vmedia.iso_media_name(config), vmedia.iso_media_name(config)

    assert first.startswith("boot.media_") and first.endswith(".iso")
    assert first != second


# ---------------------------------------------------------------------------
# ST21 — the mapping reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_st21_fails_an_optical_entry_without_an_identity(arm):
    state, hmc = arm
    hmc.optical_id_missing = True

    await _run(state, 16, 21)

    assert _results(state, "storage.list_mappings") == ["failed"]
    assert _results(state, "media.list_mappings") == ["failed"]


@pytest.mark.asyncio
async def test_st21_records_an_empty_partition_listing_as_non_promoting(arm):
    state, hmc = arm
    hmc.mappings = {("vhost5/vtscsi5", "OTHER-LPAR", "VirtualDisk", "d")}

    await _run(state, 16, 21)

    assert "storage.list_mappings" not in _observations(state)
    assert "media.list_mappings" not in _observations(state)
    empties = [r for r in state.results if r["tool"].endswith("(empty)")]
    assert {r["result"] for r in empties} == {"observed"}


# ---------------------------------------------------------------------------
# ST22 — teardown touches only what the run created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_st22_without_run_media_mutates_nothing_in_an_operator_repository(arm):
    state, hmc = arm
    await _run(state, 16)

    await _run(state, 22)

    assert _mutations(hmc) == []


@pytest.mark.asyncio
async def test_st22_removes_only_the_repository_this_run_created(arm):
    state, hmc = arm
    hmc.repository_vg = None
    hmc.media = {}
    hmc.mappings = {DISK_MAPPING}

    await _run(state, 16, 19, 22)

    assert [t for t, _ in _mutations(hmc)][-1] == "hmc_delete_media_repository"
    assert hmc.repository_vg is None
    assert not state.artifacts.vmedia_repo_created


# ---------------------------------------------------------------------------
# ST20 — boot from the run's ISO, only when one exists
# ---------------------------------------------------------------------------


@pytest.fixture
def with_iso(arm, tmp_path):
    state, hmc = arm
    iso = tmp_path / "install.iso"
    iso.write_bytes(b"iso")
    state.config = replace(state.config, iso_path=str(iso))
    return state, hmc


@pytest.mark.parametrize(
    ("reported", "sets", "skip_reason"),
    [
        (
            {
                "pending_boot_string": BOOT_LAN,
                "boot_device_list": f"{BOOT_DISK} {BOOT_LAN}",
            },
            [[BOOT_DISK, BOOT_LAN], [BOOT_LAN]],
            None,
        ),
        (
            {"pending_boot_string": None, "boot_device_list": BOOT_DISK},
            [],
            "REST0126",
        ),
        (
            {"pending_boot_string": BOOT_LAN, "boot_device_list": None},
            [],
            "no boot device list",
        ),
        (
            {"pending_boot_string": "cd disk", "boot_device_list": BOOT_DISK},
            [],
            "cannot be restored",
        ),
    ],
)
@pytest.mark.asyncio
async def test_st20_writes_only_a_boot_order_it_can_restore(
    with_iso, reported, sets, skip_reason
):
    state, hmc = with_iso
    hmc.boot_order = reported

    await _run(state, 16, 20, 22)

    assert [
        k["devices"] for t, k in hmc.calls if t == "hmc_set_lpar_boot_order"
    ] == sets
    skipped = [
        r
        for r in state.results
        if r["tool"] == "hmc_set_lpar_boot_order (boot device list)"
        and r["status"] == "SKIP"
    ]
    if skip_reason is None:
        assert skipped == []
    else:
        assert len(skipped) == 1 and skip_reason in skipped[0]["note"]
    assert state.artifacts.vmedia_orig_boot_order == []
    assert hmc.media == {OPERATOR_MEDIA: 2048}


@pytest.mark.asyncio
async def test_st20_restores_boot_order_and_unmounts_after_a_failed_boot(with_iso):
    state, hmc = with_iso
    hmc.power_on_fails = True
    before = hmc.snapshot()

    await _run(state, 16, 20, 22)

    tools = [t for t, _ in hmc.calls]
    assert "hmc_unmount_optical_media" in tools
    assert tools.count("hmc_set_lpar_boot_order") == 2
    assert state.artifacts.vmedia_mapping_uuid is None
    assert hmc.snapshot() == before


# ---------------------------------------------------------------------------
# A restored results document never hands the arm an operator object
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restored_untagged_medium_name_is_never_removed(arm):
    """The arm before #1347 recorded the first listed medium as its ISO."""
    state, hmc = arm
    state.artifacts.vmedia_iso_name = OPERATOR_MEDIA
    before = hmc.snapshot()

    await _run(state, 16, 22)

    assert _mutations(hmc) == []
    assert hmc.snapshot() == before


@pytest.mark.asyncio
async def test_restored_ownership_never_claims_an_existing_repository(arm):
    state, hmc = arm
    hmc.repository_vg = "vg-cfg-uuid"
    state.artifacts.vmedia_repo_created = True
    state.artifacts.vg_uuid = "vg-cfg-uuid"
    state.artifacts.vdisk_vg_name = state.config.vdisk_volume_group_name

    await _run(state, 16, 17, 22)

    assert not {"hmc_delete_media_repository", "hmc_create_media_repository"} & {
        t for t, _ in hmc.calls
    }
    assert not state.artifacts.vmedia_repo_created
    assert "no longer treated as owned" in str(_manual(state))


@pytest.mark.asyncio
async def test_a_failed_upload_does_not_block_the_boot_in_the_same_run(arm, tmp_path):
    state, hmc = arm
    iso = tmp_path / "install.iso"
    iso.write_bytes(b"iso")
    state.config = replace(state.config, iso_path=str(iso))
    original = hmc.call
    uploads = 0

    async def first_upload_fails(_self, _client, tool, **kwargs):
        nonlocal uploads
        if tool == "hmc_upload_iso":
            uploads += 1
            if uploads == 1:
                return "FAIL", _failure("HMCError: HTTP 500")
        return await original(_client, tool, **kwargs)

    runner.RunState.call = first_upload_fails
    try:
        await _run(state, 16, 18, 20, 22)
    finally:
        runner.RunState.call = original

    assert "hmc_power_on_lpar" in {t for t, _ in hmc.calls}
    assert hmc.media == {OPERATOR_MEDIA: 2048}


@pytest.mark.asyncio
async def test_a_restored_run_medium_blocks_a_new_one_until_teardown(arm):
    """Overwriting the recorded name would hide the earlier medium from ST22."""
    state, hmc = arm
    earlier = f"{vmedia.BLANK_PREFIX}0a1b2c3d"
    hmc.media[earlier] = 1024
    state.artifacts.vmedia_blank_name = earlier

    await _run(state, 16, 19)

    assert "hmc_create_optical_media" not in {t for t, _ in hmc.calls}
    assert state.artifacts.vmedia_blank_name == earlier

    await _run(state, 22)

    assert earlier not in hmc.media
    assert state.artifacts.vmedia_blank_name is None


@pytest.mark.asyncio
async def test_st18_reports_an_upload_it_cannot_confirm_removed(arm, tmp_path):
    state, hmc = arm
    iso = tmp_path / "install.iso"
    iso.write_bytes(b"iso")
    state.config = replace(state.config, iso_path=str(iso))
    await _run(state, 16)
    original = hmc.call
    lists = 0

    async def flaky(_self, _client, tool, **kwargs):
        nonlocal lists
        if tool == "hmc_list_optical_media":
            lists += 1
            if lists == 2:
                return "FAIL", _failure("HMCError: HTTP 500")
        return await original(_client, tool, **kwargs)

    runner.RunState.call = flaky
    try:
        await _run(state, 18)
    finally:
        runner.RunState.call = original

    (upload,) = _observations(state)["media.upload_iso"]
    assert upload["cleanup"] == "failed"
    assert "delete-media" in str(_manual(state))


@pytest.mark.asyncio
async def test_a_mapping_lost_by_the_unmount_is_a_manual_row(arm, monkeypatch):
    state, hmc = arm
    original = hmc._unmount

    def lossy(name):
        result = original(name)
        hmc.mappings.discard(OPERATOR_MAPPING)
        return result

    monkeypatch.setattr(hmc, "_unmount", lossy)
    await _run(state, 16, 19)

    unmount = _observations(state)["media.unmount"][0]
    assert "mappings-equal-baseline" not in unmount["assertions"]
    assert OPERATOR_MEDIA in str(_manual(state))


# ---------------------------------------------------------------------------
# Every media call names the managed system, so none waits on LPAR parent
# discovery (#1408)
# ---------------------------------------------------------------------------

MEDIA_TOOLS = {
    "hmc_create_optical_media",
    "hmc_delete_optical_media",
    "hmc_mount_optical_media",
    "hmc_unmount_optical_media",
}


def _assert_media_calls_scoped(hmc, *required: str) -> None:
    media = [(tool, kwargs) for tool, kwargs in hmc.calls if tool in MEDIA_TOOLS]
    assert set(required) <= {tool for tool, _ in media}
    unscoped = [
        tool for tool, kwargs in media if kwargs.get("system_name_or_uuid") != SYSTEM
    ]
    assert unscoped == []


@pytest.mark.asyncio
async def test_the_round_trip_scopes_every_media_call_to_the_system(arm):
    state, hmc = arm

    await _run(state, *runner.SUBTASK_GROUPS["vmedia"])

    _assert_media_calls_scoped(hmc, *MEDIA_TOOLS)


@pytest.mark.asyncio
async def test_the_boot_test_scopes_its_mount_and_cleanup_unmount(
    with_iso, monkeypatch
):
    state, hmc = with_iso
    hmc.power_on_fails = True
    original = hmc._mount

    def with_mapping_id(name):
        # The boot-test cleanup unmounts only a mount that named its mapping.
        status, data = original(name)
        return status, {**data, "UUID": "0000BBBB-0000-4000-8000-000000000001"}

    monkeypatch.setattr(hmc, "_mount", with_mapping_id)

    await _run(state, 16, 20)

    _assert_media_calls_scoped(
        hmc, "hmc_mount_optical_media", "hmc_unmount_optical_media"
    )


@pytest.mark.asyncio
async def test_the_teardown_scopes_its_run_media_unmount_and_delete(arm):
    state, hmc = arm
    hmc.unmount_fails = True
    await _run(state, 16, 19)
    hmc.unmount_fails = False
    hmc.calls.clear()

    await _run(state, 22)

    _assert_media_calls_scoped(
        hmc, "hmc_unmount_optical_media", "hmc_delete_optical_media"
    )


def test_every_registered_vmedia_stage_is_covered_here():
    covered = {
        runner.vmedia_bootstrap_and_create_repo,
        runner.vmedia_short_repo_lifecycle,
        runner.vmedia_upload_iso,
        runner.vmedia_mount_unmount,
        runner.vmedia_boot_verification,
        runner.vmedia_mapping_crossvalidation,
        runner.vmedia_teardown,
    }

    assert {
        runner.SUBTASKS[number] for number in runner.SUBTASK_GROUPS["vmedia"]
    } == covered
