"""Run artifacts and backward-compatible decoding of saved results."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from .creation_journals import (
    _validate_creation_memory,
    _validate_creation_memory_layout,
)


@dataclass
class LiveTestArtifacts:
    """Mutable discoveries and recovery state owned by one live-test invocation."""

    creation_memory: dict = field(default_factory=dict)
    creation_memory_layout: dict = field(default_factory=dict)
    partition_profile_lifecycle: dict = field(default_factory=dict)
    system_profile_names: list[str] = field(default_factory=list)
    system_profile_baseline: dict = field(default_factory=dict)
    system_profile_phase: str | None = None
    system_profile_jobs: list[dict] = field(default_factory=list)
    system_uuid: str | None = None
    lp3_uuid: str | None = None
    scratch_uuid: str | None = None
    vios_uuid: str | None = None
    vios_partition_id: int | None = None
    console_uuid: str | None = None
    test_user_name: str | None = None
    test_vlan_id: int | None = None
    test_vswitch_id: int | None = None
    test_network_uuid: str | None = None
    test_adapter_uuid: str | None = None
    job_uuid_sample: str | None = None
    vg_uuid: str | None = None
    vdisk_vg_name: str | None = None
    lp3_baseline: dict[str, Any] = field(default_factory=dict)
    vmedia_repo_created: bool = False
    vmedia_iso_name: str | None = None
    vmedia_mapping_uuid: str | None = None
    vmedia_orig_boot_order: list[str] = field(default_factory=list)
    # The volume group holding the VIOS's one media repository, whoever created it,
    # and the blank medium the vmedia arm (ST19) creates in it (#1347).
    vmedia_vg_uuid: str | None = None
    vmedia_blank_name: str | None = None
    # The two blank media the media-change capture (ST71) creates, each recorded before
    # its create so recovery and ST22 can find one a lost response left (#1284).
    vmedia_change_names: list[str] = field(default_factory=list)
    # The logical volume the storage arm (ST40) is about to create, recorded before
    # the create so a lost response is still tracked (#1348).
    storage_disk_name: str | None = None
    storage_volume_group_name: str | None = None
    # What the dedicated PCIe arm created, so `live_test_recovery.py` can check
    # teardown from outside the run that attempted it. The marker is per-run
    # random, so nothing outside the document can reconstruct these.
    pcie_run_marker: str | None = None
    pcie_fixture_lpar: str | None = None
    pcie_drc_index: str | None = None
    pcie_baseline_io_slots: str | None = None
    # What the vios-backup arm (ST37) is about to change, recorded before the backup so
    # `live_test_recovery.py` can find a kept backup or a missing disk mapping.
    vios_backup_vios: str | None = None
    vios_backup_vios_uuid: str | None = None
    vios_backup_name: str | None = None
    vios_backup_mapping: str | None = None
    vios_backup_backing: str | None = None
    # The installer medium the provision-boot arm (ST72) may leave: the VIOS before
    # the provision call, then the repository group and medium name it reports.
    provision_boot_media: dict = field(default_factory=dict)


_VIOS_BACKUP_ARTIFACTS = (
    "vios_backup_vios",
    "vios_backup_vios_uuid",
    "vios_backup_name",
    "vios_backup_mapping",
    "vios_backup_backing",
)


_VMEDIA_REPOSITORY_ARTIFACTS = ("vmedia_vg_uuid", "vmedia_blank_name")


_ARTIFACT_NULLABLE_STRINGS = frozenset(
    {
        "system_profile_phase",
        "system_uuid",
        "lp3_uuid",
        "scratch_uuid",
        "vios_uuid",
        "console_uuid",
        "test_user_name",
        "test_network_uuid",
        "test_adapter_uuid",
        "job_uuid_sample",
        "vg_uuid",
        "vdisk_vg_name",
        "vmedia_iso_name",
        "vmedia_mapping_uuid",
        "storage_disk_name",
        "storage_volume_group_name",
        *_VIOS_BACKUP_ARTIFACTS,
        *_VMEDIA_REPOSITORY_ARTIFACTS,
    }
)


_ARTIFACT_NULLABLE_INTS = frozenset(
    {"vios_partition_id", "test_vlan_id", "test_vswitch_id"}
)


def decode_artifacts(value: Any) -> LiveTestArtifacts:
    """Decode a complete artifact object without mutating live run state."""
    if not isinstance(value, dict):
        raise TypeError("results artifacts must be a JSON object")
    expected_fields = {item.name for item in fields(LiveTestArtifacts)}
    parsed = dict(value)
    # A results document written before #632 replaced `test_user_uuid` with
    # `test_user_name` is still a valid restore source; the old UUID named a
    # configured user, never this run's, so it is dropped rather than restored.
    # Every other field difference remains a mismatch.
    parsed.pop("test_user_uuid", None)
    parsed.setdefault("creation_memory", {})
    _validate_creation_memory(parsed["creation_memory"])
    parsed.setdefault("creation_memory_layout", {})
    _validate_creation_memory_layout(parsed["creation_memory_layout"])
    parsed.setdefault("partition_profile_lifecycle", {})
    if not isinstance(parsed["partition_profile_lifecycle"], dict):
        raise TypeError("partition-profile lifecycle must be an object")
    parsed.setdefault("test_user_name", None)
    # And one written before #1377 dropped the unused network-test partition.
    parsed.pop("nettest_uuid", None)
    parsed.setdefault("storage_volume_group_name", None)
    # Likewise a document written before the vios-backup arm (#1349) existed, or
    # before the vmedia arm tracked its repository and medium (#1347).
    for name in (*_VIOS_BACKUP_ARTIFACTS, *_VMEDIA_REPOSITORY_ARTIFACTS):
        parsed.setdefault(name, None)
    for name, default in (
        ("system_profile_names", []),
        ("system_profile_baseline", {}),
        ("system_profile_phase", None),
        ("system_profile_jobs", []),
        ("vmedia_change_names", []),
    ):
        parsed.setdefault(name, default)
    if not isinstance(parsed["vmedia_change_names"], list) or not all(
        isinstance(n, str) for n in parsed["vmedia_change_names"]
    ):
        raise TypeError("vmedia change names must be a string array")
    if not isinstance(parsed["system_profile_names"], list) or not all(
        isinstance(n, str) for n in parsed["system_profile_names"]
    ):
        raise TypeError("system-profile names must be a string array")
    if (
        not isinstance(parsed["system_profile_baseline"], dict)
        or not isinstance(parsed["system_profile_jobs"], list)
        or not all(isinstance(j, dict) for j in parsed["system_profile_jobs"])
    ):
        raise TypeError("system-profile baseline/jobs are malformed")
    if set(parsed) != expected_fields:
        raise ValueError("results artifact fields do not match LiveTestArtifacts")
    for name in _ARTIFACT_NULLABLE_STRINGS:
        if parsed[name] is not None and not isinstance(parsed[name], str):
            raise TypeError(f"results artifact {name} must be a string or null")
    for name in _ARTIFACT_NULLABLE_INTS:
        if parsed[name] is not None and type(parsed[name]) is not int:
            raise TypeError(f"results artifact {name} must be an integer or null")
    if type(parsed["vmedia_repo_created"]) is not bool:
        raise TypeError("results artifact vmedia_repo_created must be a boolean")
    baseline = parsed["lp3_baseline"]
    if not isinstance(baseline, dict) or not all(
        isinstance(key, str) for key in baseline
    ):
        raise TypeError("results artifact lp3_baseline must be an object")
    boot_order = parsed["vmedia_orig_boot_order"]
    if not isinstance(boot_order, list) or not all(
        isinstance(entry, str) for entry in boot_order
    ):
        raise TypeError(
            "results artifact vmedia_orig_boot_order must be an array of strings"
        )
    parsed["lp3_baseline"] = dict(baseline)
    parsed["vmedia_orig_boot_order"] = list(boot_order)
    return LiveTestArtifacts(**parsed)
