"""Independent storage, optical-media and boot-residue recovery witnesses."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, cast

from hmcpctl.ssh.commands import HMC_NO_RESULTS
from live_test.arms import (
    provision_boot,
    storage_lifecycle,
    storage_volume_group,
    vmedia_media_change,
)
from live_test.arms.storage import scsi_adapter_listing
from live_test.arms.vmedia import (
    _BOOT_BASELINE_STEP,
    _mapping_identity,
    is_run_media_name,
)
from live_test.recovery.common import Finding, StateUnreadable

from .common import NOT_ACTIVATED, recorded_calls

#: The vMedia arm (`SUBTASK_GROUPS["vmedia"]`) and its media-change capture (#1284),
#: whose residue is the same test-partition classes. Any other dispatched subtask is
#: reported as not witnessed.
MEDIA_SUBTASKS = frozenset(range(16, 23)) | {vmedia_media_change.SUBTASK}


#: The vMedia calls that make the repository the run's own. Media calls are not
#: among them: the arm creates and removes its own media inside a repository it
#: did not create (#1347), and those are read by name instead.
_REPOSITORY_TOOLS = frozenset(
    {"hmc_create_media_repository", "hmc_delete_media_repository"}
)


#: Every mutating call the vMedia arm makes, each covered by a class below:
#: power-off is the arm's own end state, and mount/unmount residue is read
#: whenever the arm ran. A test pins this against the arm's source.
MEDIA_MUTATIONS = _REPOSITORY_TOOLS | {
    "hmc_upload_iso",
    "hmc_create_optical_media",
    "hmc_delete_optical_media",
    "hmc_power_on_lpar",
    "hmc_power_off_lpar",
    "hmc_set_lpar_boot_order",
    "hmc_mount_optical_media",
    "hmc_unmount_optical_media",
    "hmc_load_optical_media",
    "hmc_unload_optical_media",
}


@dataclass(frozen=True)
class ProvisionBootMediaInputs:
    """The installer medium the provision-boot arm (ST72) recorded."""

    system_name: str
    vios_uuid: str
    vg_uuid: str | None
    media_name: str | None


def provision_boot_media_inputs_from_document(
    document: dict[str, Any], subtasks: list[int]
) -> ProvisionBootMediaInputs | None:
    """ST72's medium, or `None` when the arm never reached its provision call."""
    config, artifacts = document.get("config"), document.get("artifacts")
    if provision_boot.SUBTASK not in subtasks or not isinstance(artifacts, dict):
        return None
    media = artifacts.get("provision_boot_media")
    system = config.get("system_name") if isinstance(config, dict) else None
    if not isinstance(media, dict) or not media or not system:
        return None
    return ProvisionBootMediaInputs(
        str(system),
        str(media.get("vios_uuid") or ""),
        media.get("vg_uuid"),
        media.get("media_name"),
    )


async def check_provision_boot_media(
    call, inputs: ProvisionBootMediaInputs
) -> list[Finding]:
    """The run's medium still in the repository; unreadable when it was unreported."""
    if not inputs.media_name or not inputs.vg_uuid:
        raise StateUnreadable(
            "the provision-boot medium's name or repository group (the provision call "
            f"reported none); list VIOS {inputs.vios_uuid}'s media repository for "
            "hmcpctl_<12 hex>.iso"
        )
    status, data = await call(
        "hmc_list_optical_media",
        vios_name_or_uuid=inputs.vios_uuid,
        vg_uuid=inputs.vg_uuid,
        system_name_or_uuid=inputs.system_name,
    )
    names = (
        {entry.get("name") for entry in data if isinstance(entry, dict)}
        if status == "PASS" and isinstance(data, list)
        else None
    )
    if names is None:
        raise StateUnreadable(f"the media in {inputs.vg_uuid} ({status})")
    if inputs.media_name not in names:
        return []
    where = f"{_q(inputs.vios_uuid)} --vg {_q(inputs.vg_uuid)}"
    return [
        Finding(
            f"medium {inputs.media_name}",
            f"the provision-boot medium is still in volume group {inputs.vg_uuid}",
            "if a partition still maps it, unmount it there first "
            "(hmc_unmount_optical_media); then "
            f"hmcpctl storage delete-media {where} --name {_q(inputs.media_name)} "
            f"--system {_q(inputs.system_name)}",
        )
    ]


@dataclass(frozen=True)
class LparResidueInputs:
    """What a run did to its configured test partition, as its document records.

    A subset run restores artifacts from an earlier document, so ST17-ST22 can
    act on ownership a previous invocation recorded. The flags therefore read the
    outstanding artifacts as well as this document's rows.
    """

    system_name: str
    lpar_name: str
    vios_uuid: str | None
    vios_partition_id: int | None
    vg_uuid: str | None
    iso_names: frozenset[str]
    vmedia_ran: bool
    provisioned: bool
    repository_owned: bool
    powered_on: bool
    boot_written: bool
    boot_baseline: str | None
    storage_ran: bool = False
    media_change_vtds: frozenset[str] = frozenset()
    scratch_vg_ran: bool = False
    scratch_vg_pending: str | None = None
    volume_group: str = ""

    @property
    def applies(self) -> bool:
        return (
            self.vmedia_ran
            or self.storage_ran
            or self.scratch_vg_ran
            or self.provisioned
            or self.repository_owned
            or self.powered_on
            or self.boot_written
        )


def _boot_baseline(document: dict[str, Any], saved: list[str]) -> str | None:
    """ST20's pending boot string before its write, else the saved boot order."""
    for row in recorded_calls(document, {20}, {"hmc_read_lpar_boot_order"}):
        data = row.get("data")
        if (
            row.get("tool") == _BOOT_BASELINE_STEP
            and row.get("status") == "PASS"
            and isinstance(data, dict)
            and isinstance(data.get("pending_boot_string"), str)
        ):
            return data["pending_boot_string"]
    return " ".join(saved) if saved else None


def _media_change_vtds(document: dict[str, Any]) -> frozenset[str]:
    """The VTD names ST71 recorded before it touched them (#1284)."""
    rows = document.get("results")
    return frozenset(
        row["data"]["vtd"]
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        and row.get("subtask") == vmedia_media_change.SUBTASK
        and row.get("tool") == vmedia_media_change.VTD_ROW
        and isinstance(row.get("data"), dict)
        and isinstance(row["data"].get("vtd"), str)
        and vmedia_media_change.VTD_NAME.fullmatch(row["data"]["vtd"])
    )


def lpar_inputs_from_document(
    document: Any, subtasks: list[int]
) -> LparResidueInputs | None:
    """Read the test-partition residue a run could have left, or `None` for none."""
    if not isinstance(document, dict):
        return None
    config = document.get("config")
    artifacts = document.get("artifacts")
    if not isinstance(config, dict) or not isinstance(artifacts, dict):
        return None
    saved_boot = artifacts.get("vmedia_orig_boot_order")
    saved_boot = saved_boot if isinstance(saved_boot, list) else []
    inputs = LparResidueInputs(
        system_name=str(config.get("system_name") or ""),
        lpar_name=str(config.get("lp3_name") or ""),
        vios_uuid=artifacts.get("vios_uuid"),
        vios_partition_id=artifacts.get("vios_partition_id"),
        # A document written before #1347 names the repository's group `vg_uuid`.
        vg_uuid=artifacts.get("vmedia_vg_uuid") or artifacts.get("vg_uuid"),
        # Only names the run created: the configured ISO name may be an operator's,
        # and so may a name an older arm recorded.
        iso_names=frozenset(
            name
            for name in (
                artifacts.get("vmedia_iso_name"),
                artifacts.get("vmedia_blank_name"),
                *(artifacts.get("vmedia_change_names") or []),
            )
            if is_run_media_name(name, str(config.get("iso_media_name") or ""))
        ),
        vmedia_ran=bool(MEDIA_SUBTASKS & set(subtasks)),
        provisioned=bool(
            recorded_calls(document, {14}, {"hmc_provision_lpar", "hmc_delete_lpar"})
        ),
        repository_owned=artifacts.get("vmedia_repo_created") is True
        or bool(recorded_calls(document, MEDIA_SUBTASKS, _REPOSITORY_TOOLS)),
        powered_on=bool(
            recorded_calls(
                document, {20, vmedia_media_change.SUBTASK}, {"hmc_power_on_lpar"}
            )
        ),
        boot_written=bool(saved_boot)
        or bool(recorded_calls(document, {20, 22}, {"hmc_set_lpar_boot_order"})),
        boot_baseline=_boot_baseline(document, saved_boot),
        storage_ran=storage_lifecycle.SUBTASK in subtasks,
        media_change_vtds=_media_change_vtds(document),
        scratch_vg_ran=storage_volume_group.SUBTASK in subtasks
        or artifacts.get("storage_volume_group_name") is not None,
        scratch_vg_pending=artifacts.get("storage_volume_group_name"),
        volume_group=str(config.get("vdisk_volume_group_name") or ""),
    )
    return inputs if inputs.applies else None


def _required(value: Any, field: str, purpose: str) -> Any:
    if not value:
        raise StateUnreadable(
            f"the document records no {field}, needed to read {purpose}"
        )
    return value


def _q(value: object) -> str:
    return shlex.quote(str(value))


async def _optical_mapping_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether the run's ISO is still mounted to the test partition."""
    if not inputs.vmedia_ran:
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "optical mappings")
    status, data = await call(
        "hmc_list_optical_mappings",
        vios_name_or_uuid=vios,
        lpar_name_or_uuid=inputs.lpar_name,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(data, list):
        raise StateUnreadable(
            f"could not list optical mappings for {inputs.lpar_name} ({status})"
        )
    identities = [_mapping_identity(entry) for entry in data]
    if None in identities:
        raise StateUnreadable(
            f"an optical mapping for {inputs.lpar_name} names no partition or media"
        )
    left = sorted(
        {
            media
            for _, media in cast(list[tuple[str, str]], identities)
            if media in inputs.iso_names
        }
    )
    if not left:
        return None
    return Finding(
        "optical mapping left",
        f"{', '.join(left)} is still mounted to {inputs.lpar_name} from VIOS {vios}",
        "; ".join(
            f"hmcpctl storage unmount-optical-media {_q(vios)} --lpar {_q(inputs.lpar_name)} "
            f"--name {_q(media)} --system {_q(inputs.system_name)}"
            for media in left
        ),
    )


def _adapter_slots_toward(listing: str, lpar_name: str) -> list[str]:
    """Slots of the listed server adapters whose client is *lpar_name*."""
    text = listing.strip()
    if not text or text == HMC_NO_RESULTS:
        return []
    slots = []
    for line in text.splitlines():
        fields = line.strip().split(",")
        if len(fields) < 2 or not fields[0].isdigit():
            raise StateUnreadable(f"unexpected server adapter row {line!r}")
        if fields[1] == lpar_name:
            slots.append(fields[0])
    return slots


async def _unmapped_server_adapters(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether a VIOS server adapter toward the test partition has no mapping.

    A mapping names its adapter (`vhost0`), the listing its slot, so the two are
    compared by count. REST shows an unmapped adapter as a mapping holding only its
    `ServerAdapter`, with no partition link, so the partition's mappings never include
    it and a join would need the slot the projection does not carry (#1250).
    """
    if not (inputs.vmedia_ran or inputs.provisioned or inputs.storage_ran):
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "server adapters")
    vios_id = inputs.vios_partition_id
    if type(vios_id) is not int:
        raise StateUnreadable(
            "the document records no integer artifacts.vios_partition_id, "
            "needed to list server adapters"
        )
    status, listing = await call(
        "hmc_run_command",
        cmd=scsi_adapter_listing(inputs.system_name, "lpar_ids", vios_id),
    )
    if status != "PASS" or not isinstance(listing, str):
        raise StateUnreadable(
            f"could not list server adapters on VIOS {vios_id} ({status})"
        )
    slots = _adapter_slots_toward(listing, inputs.lpar_name)
    status, mappings = await call(
        "hmc_list_storage_mappings",
        vios_name_or_uuid=vios,
        lpar_name_or_uuid=inputs.lpar_name,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(mappings, list):
        raise StateUnreadable(
            f"could not list storage mappings for {inputs.lpar_name} ({status})"
        )
    ids = [entry.get("id") if isinstance(entry, dict) else None for entry in mappings]
    compare = (
        f"compare slots {', '.join(slots) or '(none)'} with `hmcpctl storage "
        f"list-mappings {_q(vios)} --lpar {_q(inputs.lpar_name)} "
        f"--system {_q(inputs.system_name)}`"
    )
    if any(not isinstance(id_, str) or "/" not in id_ for id_ in ids):
        raise StateUnreadable(
            f"a storage mapping for {inputs.lpar_name} names no server adapter; {compare}"
        )
    mapped = {id_.split("/", 1)[0] for id_ in cast(list[str], ids)}
    if len(slots) == len(mapped):
        return None
    if len(slots) < len(mapped):
        # A mapped adapter the listing does not name for this partition would
        # cancel out an unmapped one, so the count cannot say which is which.
        raise StateUnreadable(
            f"{len(mapped)} mapped server adapter(s) toward {inputs.lpar_name} but "
            f"{len(slots)} listed for it; {compare}"
        )
    return Finding(
        "unmapped server adapter",
        f"VIOS {vios_id} has {len(slots)} vSCSI server adapter(s) toward "
        f"{inputs.lpar_name} (slots {', '.join(slots)}) but {len(mapped)} mapped",
        f"{compare}; remove each unmapped one with `chhwres -r virtualio --rsubtype "
        f"scsi -m {_q(inputs.system_name)} -o r --id {vios_id} -s <slot>`",
    )


async def _empty_optical_target_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether ST71 left a virtual optical device with no medium loaded (#1284).

    The optical listing keeps only media-backed mappings and the adapter count treats
    the device as mapping its adapter, so neither class above sees an empty device.
    """
    if not inputs.media_change_vtds:
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "storage mappings")
    vios_id = inputs.vios_partition_id
    if type(vios_id) is not int:
        raise StateUnreadable(
            "the document records no integer artifacts.vios_partition_id, "
            "needed to name an empty optical device's removal"
        )
    status, mappings = await call(
        "hmc_list_storage_mappings",
        vios_name_or_uuid=vios,
        lpar_name_or_uuid=inputs.lpar_name,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(mappings, list):
        raise StateUnreadable(
            f"could not list storage mappings for {inputs.lpar_name} ({status})"
        )
    empty = sorted(
        entry["id"].split("/", 1)[1]
        for entry in mappings
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str)
        and "/" in entry["id"]
        and "backing_kind" in entry
        and entry["backing_kind"] is None
        and entry.get("backing_name") is None
        and entry["id"].split("/", 1)[1] in inputs.media_change_vtds
    )
    if not empty:
        return None
    return Finding(
        "empty optical target left",
        f"{', '.join(empty)} on VIOS {vios} maps {inputs.lpar_name} with no medium",
        "; ".join(
            f"viosvrcmd -m {_q(inputs.system_name)} --id {vios_id} "
            f"-c {_q(f'rmvdev -vtd {target}')}"
            for target in empty
        ),
    )


async def _repository_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether the media repository this run owned is still there."""
    if not inputs.repository_owned:
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "the media repository")
    vg = _required(inputs.vg_uuid, "artifacts.vmedia_vg_uuid", "the media repository")
    status, data = await call(
        "hmc_get_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS":
        raise StateUnreadable(f"could not read the media repository in {vg} ({status})")
    if not data:
        return None
    where = f"{_q(vios)} --vg {_q(vg)}"
    system = f"--system {_q(inputs.system_name)}"
    return Finding(
        "media repository left",
        f"the repository the run created in volume group {vg} on VIOS {vios} "
        "still exists",
        f"hmcpctl storage list-optical-media {where} {system}; "
        f"hmcpctl storage delete-media {where} --name <each ISO> {system}; "
        f"hmcpctl storage delete-media-repo {where} {system}",
    )


async def _run_media_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether a medium the run created is still in the repository."""
    if not (inputs.vmedia_ran and inputs.iso_names):
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "the run's media")
    vg = _required(inputs.vg_uuid, "artifacts.vmedia_vg_uuid", "the run's media")
    status, data = await call(
        "hmc_list_optical_media",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
        system_name_or_uuid=inputs.system_name,
    )
    names = (
        [entry.get("name") for entry in data if isinstance(entry, dict)]
        if (status == "PASS" and isinstance(data, list))
        else None
    )
    if names is None:
        raise StateUnreadable(f"could not list the media in {vg} ({status})")
    left = sorted(inputs.iso_names & set(names))
    if not left:
        return None
    return Finding(
        "run media left",
        f"{', '.join(left)} created by the run is still in volume group {vg} on "
        f"VIOS {vios}",
        "; ".join(
            f"hmcpctl storage delete-media {_q(vios)} --vg {_q(vg)} --name {_q(name)} "
            f"--system {_q(inputs.system_name)}"
            for name in left
        ),
    )


async def _run_disk_mapping_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether a mapping backed by a run-named volume is still on the VIOS."""
    if not inputs.storage_ran:
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "storage mappings")
    status, data = await call(
        "hmc_list_storage_mappings",
        vios_name_or_uuid=vios,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(data, list):
        raise StateUnreadable(f"could not list storage mappings on {vios} ({status})")
    left = sorted(
        (str(entry.get("id")), str(entry.get("backing_name")))
        for entry in storage_lifecycle.disk_mapping_rows(data)
    )
    if not left:
        return None
    system = f"--system {_q(inputs.system_name)}"
    return Finding(
        "run disk mapping left",
        "; ".join(f"{name} is still mapped as {id_}" for id_, name in left)
        + f" on VIOS {vios}",
        "; ".join(
            f"hmcpctl storage detach-mapping {_q(vios)} {_q(id_)} {system}"
            for id_, _ in left
        ),
    )


async def _run_disk_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether a run-named volume is still in the configured volume group."""
    if not inputs.storage_ran:
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "logical volumes")
    vios_id = inputs.vios_partition_id
    group = inputs.volume_group
    if type(vios_id) is not int or not all(
        storage_lifecycle.LISTING_NAME.fullmatch(name)
        for name in (inputs.system_name, group)
    ):
        raise StateUnreadable(
            "the document records no integer artifacts.vios_partition_id, or a "
            "system or volume-group name the read-only listing cannot carry"
        )
    status, listing = await call(
        "hmc_run_command",
        cmd=storage_lifecycle.volume_listing(inputs.system_name, vios_id, group),
    )
    names = storage_lifecycle.volume_names(listing) if status == "PASS" else None
    if names is None:
        raise StateUnreadable(f"could not list the volumes in {group} ({status})")
    left = sorted(name for name in names if storage_lifecycle.is_run_disk_name(name))
    if not left:
        return None
    system = f"--system {_q(inputs.system_name)}"
    return Finding(
        "run disk left",
        f"{', '.join(left)} created by the run is still in volume group {group} on "
        f"VIOS {vios}",
        f"hmcpctl storage list-vgs {_q(vios)} {system} (for the group's UUID); "
        + "; ".join(
            f"hmcpctl storage delete-disk {_q(vios)} --vg <{group} UUID> --name "
            f"{name} {system}"
            for name in left
        ),
    )


async def _partition_running(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether ST20 left the test partition running after its boot probe."""
    if not inputs.powered_on:
        return None
    status, state = await call(
        "hmc_get_lpar_state",
        system_name_or_uuid=inputs.system_name,
        lpar_name_or_uuid=inputs.lpar_name,
    )
    if status != "PASS" or not isinstance(state, str):
        raise StateUnreadable(
            f"could not read the state of {inputs.lpar_name} ({status})"
        )
    # REST's PartitionState is lower case ("not activated"); only the CLI title-cases it.
    if state.strip().lower() == NOT_ACTIVATED.lower():
        return None
    return Finding(
        "test partition running",
        f"{inputs.lpar_name} on {inputs.system_name} is {state!r}; ST20 powered it "
        f"on and its cleanup leaves it {NOT_ACTIVATED}",
        f"hmcpctl lpars power-off {_q(inputs.lpar_name)} --system "
        f"{_q(inputs.system_name)} --immediate --yes",
    )


async def _boot_string_drift(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether the pending boot string differs from the one the run captured."""
    if not inputs.boot_written:
        return None
    baseline = inputs.boot_baseline
    if not baseline:
        raise StateUnreadable(
            f"the run wrote the boot order of {inputs.lpar_name} but the document "
            "records no baseline to compare with"
        )
    status, data = await call(
        "hmc_read_lpar_boot_order",
        system_name_or_uuid=inputs.system_name,
        lpar_name_or_uuid=inputs.lpar_name,
    )
    observed = data.get("pending_boot_string") if isinstance(data, dict) else None
    if (
        status != "PASS"
        or not isinstance(data, dict)
        or not isinstance(observed or "", str)
    ):
        raise StateUnreadable(
            f"could not read the boot order of {inputs.lpar_name} ({status})"
        )
    if (observed or "").split() == baseline.split():
        return None
    return Finding(
        "boot string drift",
        f"pending boot string of {inputs.lpar_name} is {observed!r}, not the "
        f"captured baseline {baseline!r}",
        f"hmcpctl lpars set-boot-order {_q(inputs.system_name)} "
        f"{_q(inputs.lpar_name)} {' '.join(_q(path) for path in baseline.split())}",
    )


async def _run_volume_group_left(call, inputs: LparResidueInputs) -> Finding | None:
    if not inputs.scratch_vg_ran:
        return None
    pending = inputs.scratch_vg_pending
    if pending is not None and not storage_volume_group.is_run_group(pending):
        raise StateUnreadable("document records an invalid pending scratch group")
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "scratch volume groups")
    status, data = await call(
        "hmc_list_volume_groups",
        vios_name_or_uuid=vios,
        system_name_or_uuid=inputs.system_name,
    )
    names = storage_volume_group.rest_group_names(data) if status == "PASS" else None
    if names is None:
        raise StateUnreadable("could not list scratch volume groups")
    left = sorted(name for name in names if storage_volume_group.is_run_group(name))
    if not left and pending is None:
        return None
    return Finding(
        "run volume group left" if left else "scratch group restoration unconfirmed",
        f"{', '.join(left) or pending} on VIOS {vios}; "
        + (
            f"baseline restoration for pending {pending} remains unconfirmed"
            if pending
            else "group remains listed"
        ),
        "compare the durable run and private before/after snapshots with independent "
        "PV/group reads; follow ST42 cleanup guards before removal; do not retry create "
        "or repair physical-volume metadata automatically",
    )


#: In the order an operator should clear them.
_PARTITION_CHECKS = (
    _partition_running,
    _optical_mapping_left,
    _run_media_left,
    _run_disk_mapping_left,
    _run_disk_left,
    _run_volume_group_left,
    _unmapped_server_adapters,
    _repository_left,
    _boot_string_drift,
    _empty_optical_target_left,
)


async def check_test_partition(call, inputs: LparResidueInputs) -> list[Finding]:
    """Every applicable test-partition class, each read even if another fails.

    The reads are independent, unlike the PCIe chain, so one unreadable class
    does not hide another's residue.
    """
    findings: list[Finding] = []
    unread: list[str] = []
    for check_one in _PARTITION_CHECKS:
        try:
            finding = await check_one(call, inputs)
        except StateUnreadable as unreadable:
            unread.append(str(unreadable))
            continue
        if finding:
            findings.append(finding)
    if unread:
        raise StateUnreadable("; ".join(unread), findings)
    return findings
