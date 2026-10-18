"""ST40: a virtual disk's lifecycle on the test partition (#1348).

The scenario creates one 1 GiB logical volume in the configured volume group, maps
it to the test partition, checks that a delete is refused while it is mapped,
detaches it, and deletes it; then it does the same through `hmc_attach_disk_to_lpar`.
Every observation of a storage operation is recorded through `record_verified`.

The arm only removes the volume it created: its name carries a per-run tag and is
stored in the run's artifacts before the create. Whether the volume exists is
always read back through the VIOS's own `lsvg -lv`, never inferred from a call's
status. The arm never removes an adapter, and deletes the volume only after a
listing shows no mapping backed by it, except for the one guarded delete it
expects to be refused.
"""

from __future__ import annotations

import re
import secrets
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from .observation import Assertion, CallFailure
from .results import field
from .storage import resolve_configured_volume_group, vios_command
from .vmedia import adapter_rows, scsi_adapter_listing, storage_rows

if TYPE_CHECKING:
    from live_test_runner import RunState

    from live_test.config import LiveTestConfig

SUBTASK = 40
GROUP = "storage"
LIFECYCLE_SCENARIO = "st40-disk-lifecycle"
ATTACH_SCENARIO = "st40-attach-disk"
#: 13 characters: the VIOS limits a logical-volume name to 15.
DISK_PREFIX = "hpctl"
DISK_SIZE_MIB = 1024
_RUN_DISK = re.compile(rf"{DISK_PREFIX}[0-9a-f]{{8}}")
#: The names a VIOS-side listing may carry: `viosvrcmd` hands its `-c` string to
#: the VIOS shell, so a name is admitted only from a closed character set.
LISTING_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_NOT_ACTIVATED = "not activated"
#: The refusal `operations.storage.resources.delete_virtual_disk` raises for a
#: mapped disk, before any write.
_GUARD_REFUSAL = "Use detach_storage_mapping first"


class _Stop(Exception):
    """The scenario cannot continue without risking the operator's state."""


def is_run_disk_name(name: object) -> bool:
    """Whether *name* has the shape only this arm gives the volumes it creates."""
    return isinstance(name, str) and _RUN_DISK.fullmatch(name) is not None


def volume_listing(system_name: str, vios_id: int, group: str) -> str:
    """The ``lsvg -lv`` read of one volume group; recovery admits exactly this shape."""
    if not LISTING_NAME.fullmatch(group):
        raise ValueError(f"volume-group name {group!r} is not a plain VIOS name")
    return vios_command(system_name, vios_id, f"lsvg -lv {group}")


def volume_names(listing: object) -> frozenset[str] | None:
    """``lsvg -lv`` output as logical-volume names; None when it is not that listing.

    The listing is the group name, an ``LV NAME`` header, then one volume per line
    (captured live for #1030).
    """
    if not isinstance(listing, str):
        return None
    lines = [line for line in listing.splitlines() if line.strip()]
    header = next(
        (i for i, line in enumerate(lines) if line.split()[:2] == ["LV", "NAME"]),
        None,
    )
    if header is None:
        return None
    return frozenset(line.split()[0] for line in lines[header + 1 :])


@dataclass(frozen=True)
class _Baseline:
    free_mib: int
    volumes: frozenset[str]
    mappings: frozenset[tuple[object, ...]]
    adapters: tuple[frozenset[str], frozenset[str]]


def _protected_reason(config: LiveTestConfig) -> str:
    return (
        f"the test partition {config.lp3_name!r} is protected by operator config "
        "(LIVE_TEST_PROTECTED_LPAR_NAMES); the disk lifecycle needs an unprotected "
        "test partition (gap)"
    )


def _create_assertions(
    accepted: bool, listed: bool, reduced: bool, kept: bool
) -> list[Assertion]:
    return [
        Assertion("create-accepted", accepted),
        Assertion("volume-listed", listed),
        Assertion("free-space-reduced", reduced),
        Assertion("baseline-volumes-kept", kept),
    ]


def _map_assertions(accepted: bool, listed: bool, kept: bool) -> list[Assertion]:
    return [
        Assertion("map-accepted", accepted),
        Assertion("mapping-listed", listed),
        Assertion("baseline-mappings-kept", kept),
    ]


class _Scenario:
    """Reads and recording shared by both ST40 scenarios."""

    def __init__(self, client: Client, state: RunState, vios_id: int) -> None:
        self.client = client
        self.state = state
        self.config = state.config
        self.artifacts = state.artifacts
        self.vios = str(state.artifacts.vios_uuid)
        self.vios_id = vios_id
        self.vg = ""
        self.lpar_uuid = str(state.artifacts.lp3_uuid)
        self.name = ""

    def note(self, tool: str, label: str, result: tuple[str, Any]) -> tuple[str, Any]:
        """Record one call's plain row and hand its result back."""
        self.state.record(SUBTASK, f"{tool} ({label})", *result)
        return result

    def skip(self, reason: str) -> None:
        self.state.skip(SUBTASK, "storage lifecycle", reason)

    def manual(self, what: str, command: str) -> None:
        self.state.record(
            SUBTASK,
            "storage lifecycle",
            "FAIL",
            None,
            f"MANUAL RECOVERY REQUIRED: {what} ({command})",
        )

    async def free_mib(self, label: str) -> int | None:
        st, data = self.note(
            "hmc_list_volume_groups",
            label,
            await self.state.call(
                self.client, "hmc_list_volume_groups", vios_name_or_uuid=self.vios
            ),
        )
        if st != "PASS":
            return None
        group = resolve_configured_volume_group(self.state, SUBTASK, data, ())
        if group is None:
            return None
        self.vg = group.uuid
        return group.free_space_mib

    async def volumes(self, label: str) -> frozenset[str] | None:
        st, listing = self.note(
            "hmc_run_command",
            f"{label} volumes",
            await self.state.call(
                self.client,
                "hmc_run_command",
                cmd=volume_listing(
                    self.config.system_name,
                    self.vios_id,
                    self.config.vdisk_volume_group_name,
                ),
            ),
        )
        return volume_names(listing) if st == "PASS" else None

    async def mappings(self, label: str) -> frozenset[tuple[object, ...]] | None:
        st, data = self.note(
            "hmc_list_storage_mappings",
            label,
            await self.state.call(
                self.client, "hmc_list_storage_mappings", vios_name_or_uuid=self.vios
            ),
        )
        return storage_rows(data) if st == "PASS" else None

    async def adapters(
        self, label: str
    ) -> tuple[frozenset[str], frozenset[str]] | None:
        rows = []
        for attribute, value in (
            ("lpar_ids", self.vios_id),
            ("lpar_names", self.config.lp3_name),
        ):
            st, listing = self.note(
                "hmc_run_command",
                f"{label} adapters {attribute}",
                await self.state.call(
                    self.client,
                    "hmc_run_command",
                    cmd=scsi_adapter_listing(self.config.system_name, attribute, value),
                ),
            )
            parsed = adapter_rows(listing) if st == "PASS" else None
            if parsed is None:
                return None
            rows.append(parsed)
        return rows[0], rows[1]

    async def baseline(self) -> _Baseline | None:
        free = await self.free_mib("baseline")
        if free is None:
            self.skip(
                f"cannot read the free space of {self.config.vdisk_volume_group_name!r}"
            )
            return None
        if free < DISK_SIZE_MIB:
            self.skip(
                f"{free} MiB free in the volume group < {DISK_SIZE_MIB} MiB (gap)"
            )
            return None
        volumes = await self.volumes("baseline")
        mappings = await self.mappings("baseline")
        adapters = await self.adapters("baseline")
        if volumes is None or mappings is None or adapters is None:
            self.skip("cannot read the baseline volumes, mappings or adapters")
            return None
        return _Baseline(free, volumes, mappings, adapters)

    def run_rows(
        self, mappings: frozenset[tuple[object, ...]], name: str
    ) -> list[tuple[object, ...]]:
        return [row for row in mappings if row[3] == name]

    def names_partition(self, row: tuple[object, ...]) -> bool:
        lpar = row[1]
        return isinstance(lpar, str) and lpar.casefold() == self.lpar_uuid.casefold()

    def new_name(self) -> str:
        name = f"{DISK_PREFIX}{secrets.token_hex(4)}"
        # Stored before the create, so a create whose response is lost is still
        # tracked; cleared only when a listing shows the volume absent.
        self.artifacts.storage_disk_name = name
        return name

    def forget(self, name: str) -> None:
        if self.artifacts.storage_disk_name == name:
            self.artifacts.storage_disk_name = None

    def delete_command(self, name: str) -> str:
        return (
            f"hmcpctl storage delete-disk {shlex.quote(self.vios)} --vg "
            f"{shlex.quote(self.vg)} --name {name} --system "
            f"{shlex.quote(self.config.system_name)}"
        )

    def detach_command(self, mapping_id: object = "<mapping id>") -> str:
        vios = shlex.quote(self.vios)
        system = shlex.quote(self.config.system_name)
        return (
            f"hmcpctl storage list-mappings {vios} --system {system}; hmcpctl "
            f"storage detach-mapping {vios} {shlex.quote(str(mapping_id))} "
            f"--system {system}"
        )

    async def detach(
        self, name: str, mapping_id: object, label: str
    ) -> tuple[str, Any, frozenset[tuple[object, ...]] | None, bool]:
        """Detach the run's mapping and re-read; the last item says it is gone."""
        st, data = self.note(
            "hmc_detach_storage_mapping",
            label,
            await self.state.call(
                self.client,
                "hmc_detach_storage_mapping",
                vios_name_or_uuid=self.vios,
                mapping_id=str(mapping_id),
                system_name_or_uuid=self.config.system_name,
            ),
        )
        after = await self.mappings(f"post-{label}")
        gone = after is not None and not self.run_rows(after, name)
        if not gone:
            self.manual(f"{name} may still be mapped", self.detach_command(mapping_id))
        return st, data, after, gone

    def drift(
        self,
        baseline: _Baseline,
        mappings: frozenset[tuple[object, ...]] | None,
        adapters: tuple[frozenset[str], frozenset[str]] | None,
        when: str,
    ) -> bool:
        """Report mappings or adapters that differ from the baseline; True when equal."""
        equal = True
        if mappings != baseline.mappings:
            equal = False
            self.manual(
                f"VIOS storage mappings differ from the baseline {when}",
                self.detach_command(),
            )
        if adapters != baseline.adapters:
            equal = False
            system = shlex.quote(self.config.system_name)
            self.manual(
                f"vSCSI adapters differ from the baseline {when}",
                f"lshwres -r virtualio --rsubtype scsi -m {system} --level lpar; "
                f"remove each adapter the run added with chhwres -r virtualio "
                f"--rsubtype scsi -m {system} -o r --id <partition id> -s <slot>",
            )
        return equal

    async def delete(
        self, name: str, label: str
    ) -> tuple[str, Any, frozenset[str] | None]:
        """Delete the run's unmapped volume and read the volumes back."""
        st, data = self.note(
            "hmc_delete_virtual_disk",
            label,
            await self.state.call(
                self.client,
                "hmc_delete_virtual_disk",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
                disk_name=name,
            ),
        )
        after = await self.volumes(f"post-{label}")
        if after is not None and name not in after:
            self.forget(name)
        else:
            self.manual(
                f"{name} may still be in the volume group", self.delete_command(name)
            )
        return st, data, after


class _Lifecycle(_Scenario):
    """Create, map, guarded delete, detach, delete (scenario st40-disk-lifecycle)."""

    def record_create(self, created: tuple[bool, ...], *, restored: bool) -> None:
        self.state.record_verified(
            SUBTASK,
            "hmc_create_virtual_disk",
            operation="storage.create_disk",
            scenario=LIFECYCLE_SCENARIO,
            assertions=_create_assertions(*created),
            cleanup="passed" if restored else "failed",
            data={"disk_name": self.name},
        )

    def record_map(
        self, mapped: tuple[bool, ...], data: object, *, restored: bool
    ) -> None:
        self.state.record_verified(
            SUBTASK,
            "hmc_map_storage_to_lpar",
            operation="storage.map",
            scenario=LIFECYCLE_SCENARIO,
            assertions=_map_assertions(*mapped),
            cleanup="passed" if restored else "failed",
            data=data,
        )

    async def run(self, baseline: _Baseline) -> bool:
        """Run the lifecycle; True when the VIOS is back at its baseline."""
        self.name = self.new_name()
        created = await self.create(baseline)
        if created is None:
            return False
        try:
            mapping, mapped, map_data, unmapped_clean = await self.map(baseline)
        except _Stop:
            self.record_create(created, restored=False)
            raise
        refused = None
        try:
            if mapping is not None:
                refused = await self.guarded_delete()
                unmapped_clean = await self.detach_and_check(baseline, mapping)
        except _Stop:
            self.record_map(mapped, map_data, restored=False)
            self.record_create(created, restored=False)
            raise
        self.record_map(mapped, map_data, restored=unmapped_clean)
        return await self.final_delete(baseline, created, refused) and unmapped_clean

    async def create(self, baseline: _Baseline) -> tuple[bool, ...] | None:
        st, _ = self.note(
            "hmc_create_virtual_disk",
            "run disk",
            await self.state.call(
                self.client,
                "hmc_create_virtual_disk",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
                disk_name=self.name,
                capacity_mib=DISK_SIZE_MIB,
            ),
        )
        after = await self.volumes("post-create")
        free = await self.free_mib("post-create")
        listed = after is not None and self.name in after
        created = (
            st == "PASS",
            listed,
            free is not None and baseline.free_mib - free >= DISK_SIZE_MIB,
            after is not None and baseline.volumes <= after,
        )
        if after is None:
            self.record_create(created, restored=False)
            self.manual(
                f"cannot confirm whether {self.name} was created",
                self.delete_command(self.name),
            )
            raise _Stop("create state unknown")
        if not listed:
            self.forget(self.name)
            self.record_create(created, restored=after == baseline.volumes)
            return None
        return created

    async def map(
        self, baseline: _Baseline
    ) -> tuple[object | None, tuple[bool, ...], object, bool]:
        """Map the volume.

        Returns the listed mapping id (None when none is listed), the assertion
        values, the call's data, and, for an unlisted mapping, whether mappings and
        adapters still equal the baseline.
        """
        st, data = self.note(
            "hmc_map_storage_to_lpar",
            "run disk",
            await self.state.call(
                self.client,
                "hmc_map_storage_to_lpar",
                vios_name_or_uuid=self.vios,
                storage_name=self.name,
                lpar_name_or_uuid=self.config.lp3_name,
                storage_kind="VirtualDisk",
                system_name_or_uuid=self.config.system_name,
            ),
        )
        after = await self.mappings("post-map")
        if after is None:
            self.manual(
                f"cannot confirm whether {self.name} is mapped", self.detach_command()
            )
            raise _Stop("map state unknown")
        rows = self.run_rows(after, self.name)
        listed = len(rows) == 1 and self.names_partition(rows[0])
        mapped = (st == "PASS", listed, baseline.mappings <= after)
        if not rows:
            # A failed map can leave an adapter with no mapping (#1237).
            clean = self.drift(
                baseline, after, await self.adapters("post-map"), "after a failed map"
            )
            return None, mapped, data, clean
        if not listed:
            self.record_map(mapped, data, restored=False)
            self.manual(
                f"{self.name} is mapped {len(rows)} time(s), not only to the test "
                "partition",
                self.detach_command(),
            )
            raise _Stop("unexpected mapping")
        return rows[0][0], mapped, data, True

    async def guarded_delete(self) -> bool:
        """The delete hmcpctl must refuse while the volume is mapped."""
        st, data = self.note(
            "hmc_delete_virtual_disk",
            "while mapped",
            await self.state.call(
                self.client,
                "hmc_delete_virtual_disk",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
                disk_name=self.name,
            ),
        )
        still = await self.volumes("after the guarded delete")
        if still is None:
            self.manual(
                f"cannot confirm whether the guarded delete removed {self.name}",
                self.detach_command(),
            )
            raise _Stop("guarded delete state unknown")
        if self.name not in still:
            self.state.record_verified(
                SUBTASK,
                "hmc_delete_virtual_disk",
                operation="storage.delete_disk",
                scenario=LIFECYCLE_SCENARIO,
                assertions=[Assertion("refused-while-mapped", False)],
                cleanup="failed",
                data=data,
            )
            self.manual(
                f"the guarded delete of {self.name} was not refused; its mapping "
                "may now name a missing volume",
                self.detach_command(),
            )
            raise _Stop("guarded delete went through")
        # Only hmcpctl's own guard counts: any other failure (the HMC refusing the
        # VolumeGroup write, a failed mapping read) would hide a guard that missed.
        return (
            st == "FAIL"
            and isinstance(data, CallFailure)
            and _GUARD_REFUSAL in data.message
        )

    async def detach_and_check(self, baseline: _Baseline, mapping_id: object) -> bool:
        """Detach and record it; True when mappings and adapters equal the baseline."""
        st, data, after, gone = await self.detach(self.name, mapping_id, "run disk")
        adapters = await self.adapters("post-detach")
        volumes = await self.volumes("post-detach")
        mappings_equal = after == baseline.mappings
        adapters_equal = adapters == baseline.adapters
        if gone:
            self.drift(baseline, after, adapters, "after the detach")
        self.state.record_verified(
            SUBTASK,
            "hmc_detach_storage_mapping",
            operation="storage.detach_mapping",
            scenario=LIFECYCLE_SCENARIO,
            assertions=[
                Assertion("detach-accepted", st == "PASS"),
                Assertion("mapping-absent", gone),
                Assertion("mappings-equal-baseline", mappings_equal),
                Assertion("adapters-equal-baseline", adapters_equal),
                Assertion(
                    "volume-survives", volumes is not None and self.name in volumes
                ),
            ],
            cleanup="not-required",
            data=data,
        )
        if not gone:
            raise _Stop("detach not confirmed")
        return mappings_equal and adapters_equal

    async def final_delete(
        self, baseline: _Baseline, created: tuple[bool, ...], refused: bool | None
    ) -> bool:
        st, data, after = await self.delete(self.name, "run disk")
        free = await self.free_mib("post-delete")
        absent = after is not None and self.name not in after
        volumes_equal = after == baseline.volumes
        free_restored = free == baseline.free_mib
        self.state.record_verified(
            SUBTASK,
            "hmc_delete_virtual_disk",
            operation="storage.delete_disk",
            scenario=LIFECYCLE_SCENARIO,
            assertions=[
                # Only a listed mapping can show the guard refusing.
                *(
                    [Assertion("refused-while-mapped", refused)]
                    if refused is not None
                    else []
                ),
                Assertion("delete-accepted", st == "PASS"),
                Assertion("volume-absent", absent),
                Assertion("volumes-equal-baseline", volumes_equal),
                Assertion("free-space-restored", free_restored),
            ],
            cleanup="not-required",
            data=data,
        )
        restored = absent and volumes_equal and free_restored
        self.record_create(created, restored=restored)
        return restored


class _Attach(_Scenario):
    """`hmc_attach_disk_to_lpar`, then detach and delete (st40-attach-disk)."""

    async def run(self, baseline: _Baseline) -> None:
        name = self.new_name()
        st, data = self.note(
            "hmc_attach_disk_to_lpar",
            "run disk",
            await self.state.call(
                self.client,
                "hmc_attach_disk_to_lpar",
                lpar_name_or_uuid=self.config.lp3_name,
                vios_uuid=self.vios,
                vg_uuid=self.vg,
                disk_name=name,
                capacity_mib=DISK_SIZE_MIB,
                system_name_or_uuid=self.config.system_name,
            ),
        )
        steps = field(data, "steps")
        completed = (
            st == "PASS"
            and field(data, "workflow_completed") is True
            and isinstance(steps, list)
            and len(steps) == 2
            and all(field(step, "status") == "ok" for step in steps)
        )
        listed = mapped = restored = False
        try:
            listed, mapped = await self.clean_up(name)
            restored = await self.restored(baseline)
        finally:
            self.state.record_verified(
                SUBTASK,
                "hmc_attach_disk_to_lpar",
                operation="storage.attach_disk",
                scenario=ATTACH_SCENARIO,
                assertions=[
                    Assertion("workflow-completed", completed),
                    Assertion("volume-listed", listed),
                    Assertion("mapping-listed", mapped),
                ],
                cleanup="passed" if restored else "failed",
                data=data,
            )

    async def clean_up(self, name: str) -> tuple[bool, bool]:
        """Remove what the attach left, as listed; returns (volume, mapping) listed.

        The tool reports a failed step as `error` rather than failing, so cleanup
        follows the listings, not the result.
        """
        volumes = await self.volumes("post-attach")
        mappings = await self.mappings("post-attach")
        if volumes is None or mappings is None:
            self.manual(
                f"cannot confirm what the attach of {name} created",
                self.detach_command(),
            )
            raise _Stop("attach state unknown")
        rows = self.run_rows(mappings, name)
        if len(rows) > 1:
            self.manual(f"{name} is mapped {len(rows)} times", self.detach_command())
            raise _Stop("ambiguous mapping")
        if rows:
            *_, gone = await self.detach(name, rows[0][0], "attach cleanup")
            if not gone:
                raise _Stop("detach not confirmed")
        if name in volumes:
            await self.delete(name, "attach cleanup")
        else:
            self.forget(name)
        return name in volumes, len(rows) == 1 and self.names_partition(rows[0])

    async def restored(self, baseline: _Baseline) -> bool:
        volumes = await self.volumes("post-cleanup")
        mappings = await self.mappings("post-cleanup")
        adapters = await self.adapters("post-cleanup")
        free = await self.free_mib("post-cleanup")
        same = self.drift(baseline, mappings, adapters, "after the attach cleanup")
        return same and volumes == baseline.volumes and free == baseline.free_mib


async def _preconditions(client: Client, state: RunState) -> int | None:
    """The VIOS partition id when ST40 may start; otherwise SKIP and None."""
    config = state.config
    artifacts = state.artifacts

    def skip(reason: str) -> None:
        state.skip(SUBTASK, "storage lifecycle", reason)

    vios_id = artifacts.vios_partition_id
    if not artifacts.vios_uuid or type(vios_id) is not int:
        skip("no VIOS UUID and partition id resolved in ST0")
        return None
    if not artifacts.lp3_uuid:
        skip("no test-partition UUID resolved in ST0")
        return None
    if config.lp3_name in config.protected_lpar_names:
        skip(_protected_reason(config))
        return None
    if not all(
        LISTING_NAME.fullmatch(name)
        for name in (config.system_name, config.vdisk_volume_group_name)
    ):
        # The recovery check reads the volumes only for such names, so the arm
        # never mutates what it could not confirm clean.
        skip(
            "the system or LIVE_TEST_VDISK_VOLUME_GROUP_NAME is not a plain name "
            "(letters, digits, '_', '.', '-')"
        )
        return None
    st, lpar_state = await state.call(
        client,
        "hmc_get_lpar_state",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record(SUBTASK, "hmc_get_lpar_state (precondition)", st, lpar_state)
    if st != "PASS" or not isinstance(lpar_state, str):
        skip("cannot read the test partition's state")
        return None
    if lpar_state.strip().lower() != _NOT_ACTIVATED:
        skip(
            f"the test partition is {lpar_state!r}; a map on a running partition is "
            "a dynamic reconfiguration this arm does not exercise"
        )
        return None
    return vios_id


async def _clear_restored_disk(scenario: _Scenario) -> bool:
    """A volume name restored from an earlier document: clear it when gone."""
    name = scenario.artifacts.storage_disk_name
    if name is None:
        return True
    volumes = await scenario.volumes("restored run disk")
    if volumes is None or name in volumes:
        scenario.skip(
            f"the volume {name!r} an earlier invocation created may still exist; "
            "run scripts/live_test_recovery.py and remove it first"
        )
        return False
    scenario.forget(name)
    return True


async def exercise_disk_lifecycle(client: Client, state: RunState) -> None:
    print("\n=== ST40: Virtual Disk Lifecycle ===")
    if state.group != GROUP:
        state.skip(SUBTASK, "storage lifecycle", "runs only in the storage arm")
        return
    vios_id = await _preconditions(client, state)
    if vios_id is None:
        return
    lifecycle = _Lifecycle(client, state, vios_id)
    if not await _clear_restored_disk(lifecycle):
        return
    try:
        baseline = await lifecycle.baseline()
        if baseline is None or not await lifecycle.run(baseline):
            return
        attach = _Attach(client, state, vios_id)
        baseline = await attach.baseline()
        if baseline is not None:
            await attach.run(baseline)
    except _Stop as stop:
        print(f"  ⚠  ST40 stopped: {stop}")


def disk_mapping_rows(data: object) -> list[Mapping[str, Any]]:
    """Storage mappings backed by a run-named volume."""
    if not isinstance(data, list):
        return []
    return [
        entry
        for entry in data
        if isinstance(entry, Mapping) and is_run_disk_name(entry.get("backing_name"))
    ]
