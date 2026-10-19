"""Shared live-test configuration and saved recovery-document contracts."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar

from hmcpctl.documents.storage import VIRTUAL_DISK_NAME_MAX

ENV_FILE = Path(".env")


ENVIRONMENT_PREFIX = "LIVE_TEST_ENV_"


_HEX_ID = re.compile(r"[0-9a-f]+")


_VIOS_OBJECT_NAME = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class LiveTestConfig:
    """Validated operator configuration for live-test executions."""

    system_name: str = "example-lt-609-system"
    lp3_name: str = "example-lt-609-lpar"
    scratch_name: str = "example-lt-609-scratch"
    vdisk_name: str = "lt609-disk"
    scratch_pv_name: str = ""
    scratch_vg_name: str = ""
    profile_artifact_suffix_hex_length: int = 16
    system_profile_members_json: str = ""
    system_profile_writer_window: bool = False
    system_profile_activation_action: str = "none"
    system_profile_continue_on_error: bool = False
    system_profile_ownership_override: bool = False
    scratch_create_desired_memory_mib: int = 1536
    scratch_create_max_memory_mib: int = 3072
    scratch_create_desired_vcpus: int = 3
    scratch_create_max_vcpus: int = 6
    scratch_create_desired_procs: float = 0.3
    scratch_create_max_procs: float = 0.6
    scratch_modify_desired_memory_mib: int = 2304
    scratch_modify_max_memory_mib: int = 4608
    dry_run_lpar_name: str = "example-lt-609-dry-run"
    dry_run_storage_name: str = "example-lt-609-dry-disk"
    vdisk_volume_group_name: str = "example-lt-609-vg"
    dry_run_memory_mib: int = 1536
    provision_min_memory_mib: int = 1536
    provision_desired_memory_mib: int = 3072
    provision_max_memory_mib: int = 6144
    provision_desired_vcpus: int = 3
    provision_max_vcpus: int = 6
    # ST13/ST14's client-adapter VLAN (it must already have a virtual network) and
    # test-disk size: set explicitly because a fresh lab has neither fixture (#970).
    provision_vlan_id: int = 1
    provision_disk_mib: int = 10240
    protected_lpar_names: tuple[str, ...] = (
        "example-lt-609-protected-a",
        "example-lt-609-protected-b",
    )
    sriov_adapter_id: int = 17
    sriov_physical_port_id: int = 9
    sriov_logical_port_id: str = "917003"
    sriov_capacity_percent: float = 2.0
    sriov_profile_name: str = "example-lt-609-profile"
    # The dedicated PCIe arm creates and deletes a partition on the system it
    # names, so it refuses to run on a default: an empty system name or LPAR
    # prefix SKIPs the arm rather than selecting one (issue #217).
    dedicated_pcie_system_name: str = ""
    dedicated_pcie_lpar_prefix: str = ""
    dedicated_pcie_profile_name: str = ""
    dedicated_pcie_drc_index: str = ""
    # The bare-cec arm's platform-dump opt-in: only "true" runs dumprestart.
    accept_platform_dump: str = ""
    iso_path: str = "/srv/example-lt-609/example-lt-609.iso"
    iso_media_name: str = "example-lt-609.iso"
    iso_bind_host: str = "0.0.0.0"
    iso_advertised_host: str = "iso.example.test"
    iso_http_port: int = 18090
    vmedia_repository_size_mib: int = 6144
    vmedia_short_repository_size_mib: int = 1024
    placement_memory_mib: int = 3072
    vlan_range_start: int = 3100
    vlan_range_end: int = 3199

    @property
    def iso_filename(self) -> str:
        """Return the file name published by this run's ISO server."""
        return Path(self.iso_path).name

    @property
    def iso_host(self) -> str:
        """Return the host and port visible to the HMC."""
        return f"{self.iso_advertised_host}:{self.iso_http_port}"

    @property
    def iso_url(self) -> str:
        """Return the HMC-visible URL for the configured ISO."""
        return f"http://{self.iso_host}/{self.iso_filename}"

    _CONFIG_FIELDS: ClassVar[dict[str, str]] = {
        "LIVE_TEST_SYSTEM_NAME": "system_name",
        "LIVE_TEST_LPAR_NAME": "lp3_name",
        "LIVE_TEST_SCRATCH_LPAR_NAME": "scratch_name",
        "LIVE_TEST_VDISK_NAME": "vdisk_name",
        "LIVE_TEST_SCRATCH_CREATE_DESIRED_MEMORY_MIB": "scratch_create_desired_memory_mib",
        "LIVE_TEST_SCRATCH_CREATE_MAX_MEMORY_MIB": "scratch_create_max_memory_mib",
        "LIVE_TEST_SCRATCH_CREATE_DESIRED_VCPUS": "scratch_create_desired_vcpus",
        "LIVE_TEST_SCRATCH_CREATE_MAX_VCPUS": "scratch_create_max_vcpus",
        "LIVE_TEST_SCRATCH_CREATE_DESIRED_PROCS": "scratch_create_desired_procs",
        "LIVE_TEST_SCRATCH_CREATE_MAX_PROCS": "scratch_create_max_procs",
        "LIVE_TEST_SCRATCH_MODIFY_DESIRED_MEMORY_MIB": "scratch_modify_desired_memory_mib",
        "LIVE_TEST_SCRATCH_MODIFY_MAX_MEMORY_MIB": "scratch_modify_max_memory_mib",
        "LIVE_TEST_DRY_RUN_LPAR_NAME": "dry_run_lpar_name",
        "LIVE_TEST_DRY_RUN_STORAGE_NAME": "dry_run_storage_name",
        "LIVE_TEST_VDISK_VOLUME_GROUP_NAME": "vdisk_volume_group_name",
        "LIVE_TEST_DRY_RUN_MEMORY_MIB": "dry_run_memory_mib",
        "LIVE_TEST_PROVISION_MIN_MEMORY_MIB": "provision_min_memory_mib",
        "LIVE_TEST_PROVISION_DESIRED_MEMORY_MIB": "provision_desired_memory_mib",
        "LIVE_TEST_PROVISION_MAX_MEMORY_MIB": "provision_max_memory_mib",
        "LIVE_TEST_PROVISION_DESIRED_VCPUS": "provision_desired_vcpus",
        "LIVE_TEST_PROVISION_MAX_VCPUS": "provision_max_vcpus",
        "LIVE_TEST_PROVISION_VLAN_ID": "provision_vlan_id",
        "LIVE_TEST_PROVISION_DISK_MIB": "provision_disk_mib",
        "LIVE_TEST_PROTECTED_LPAR_NAMES": "protected_lpar_names",
        "LIVE_TEST_SRIOV_ADAPTER_ID": "sriov_adapter_id",
        "LIVE_TEST_SRIOV_PHYSICAL_PORT_ID": "sriov_physical_port_id",
        "LIVE_TEST_SRIOV_LOGICAL_PORT_ID": "sriov_logical_port_id",
        "LIVE_TEST_SRIOV_CAPACITY_PERCENT": "sriov_capacity_percent",
        "LIVE_TEST_SRIOV_PROFILE_NAME": "sriov_profile_name",
        "LIVE_TEST_ISO_PATH": "iso_path",
        "LIVE_TEST_ISO_MEDIA_NAME": "iso_media_name",
        "LIVE_TEST_ISO_BIND_HOST": "iso_bind_host",
        "LIVE_TEST_ISO_ADVERTISED_HOST": "iso_advertised_host",
        "LIVE_TEST_ISO_HTTP_PORT": "iso_http_port",
        "LIVE_TEST_VMEDIA_REPOSITORY_SIZE_MIB": "vmedia_repository_size_mib",
        "LIVE_TEST_VMEDIA_SHORT_REPOSITORY_SIZE_MIB": "vmedia_short_repository_size_mib",
        "LIVE_TEST_PLACEMENT_MEMORY_MIB": "placement_memory_mib",
        "LIVE_TEST_VLAN_RANGE_START": "vlan_range_start",
        "LIVE_TEST_VLAN_RANGE_END": "vlan_range_end",
    }

    #: Settings read from the same authoritative ``.env`` as ``_CONFIG_FIELDS``
    #: but not required, because they configure one opt-in arm rather than the
    #: run as a whole. An absent key leaves the field at its declared default,
    #: and the arm that owns it decides what that means — the dedicated PCIe
    #: arm SKIPs. They are read here, not from ``os.environ``, so an ambient
    #: export cannot redirect an arm that creates and deletes partitions
    #: (ADR 0115).
    _OPTIONAL_CONFIG_FIELDS: ClassVar[dict[str, str]] = {
        "LIVE_TEST_SYSTEM_PROFILE_MEMBERS_JSON": "system_profile_members_json",
        "LIVE_TEST_SYSTEM_PROFILE_WRITER_WINDOW": "system_profile_writer_window",
        "LIVE_TEST_SYSTEM_PROFILE_ACTIVATION_ACTION": "system_profile_activation_action",
        "LIVE_TEST_SYSTEM_PROFILE_CONTINUE_ON_ERROR": "system_profile_continue_on_error",
        "LIVE_TEST_SYSTEM_PROFILE_OWNERSHIP_OVERRIDE": "system_profile_ownership_override",
        "LIVE_TEST_SCRATCH_PV_NAME": "scratch_pv_name",
        "LIVE_TEST_SCRATCH_VG_NAME": "scratch_vg_name",
        "LIVE_TEST_DEDICATED_PCIE_SYSTEM_NAME": "dedicated_pcie_system_name",
        "LIVE_TEST_DEDICATED_PCIE_LPAR_PREFIX": "dedicated_pcie_lpar_prefix",
        "LIVE_TEST_DEDICATED_PCIE_PROFILE_NAME": "dedicated_pcie_profile_name",
        "LIVE_TEST_DEDICATED_PCIE_DRC_INDEX": "dedicated_pcie_drc_index",
        "LIVE_TEST_ACCEPT_PLATFORM_DUMP": "accept_platform_dump",
        "LIVE_TEST_PROFILE_ARTIFACT_SUFFIX_HEX_LENGTH": "profile_artifact_suffix_hex_length",
    }

    #: Settings a release stopped reading. A `.env` still carrying one loads, with
    #: a notice, so retiring a setting does not stop every arm on every host.
    #: `LIVE_TEST_TEST_USER_NAME`: the users arm mints its scratch user's name (#632).
    #: `LIVE_TEST_NETWORK_TEST_LPAR_NAME`: #1361 removed ST9's network-test partition.
    _RETIRED_CONFIG_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"LIVE_TEST_TEST_USER_NAME", "LIVE_TEST_NETWORK_TEST_LPAR_NAME"}
    )

    @classmethod
    def from_env_file(cls, path: Path | None = None) -> LiveTestConfig:
        """Load required live-test identifiers from one authoritative local file."""
        path = path or ENV_FILE
        if not path.is_file():
            raise ValueError(f"live-test configuration file not found: {path}")
        values: dict[str, str] = {}
        duplicates: list[str] = []
        for line_number, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if not key.startswith("LIVE_TEST_"):
                continue
            if key.startswith(ENVIRONMENT_PREFIX):
                # Read separately by `_read_environment`; not a config field.
                continue
            if key in cls._RETIRED_CONFIG_KEYS:
                print(f"{key} (line {line_number}) is retired and ignored")
                continue
            if key not in cls._CONFIG_FIELDS and key not in cls._OPTIONAL_CONFIG_FIELDS:
                duplicates.append(f"unknown setting {key} (line {line_number})")
                continue
            if key in values:
                duplicates.append(f"{key} (line {line_number})")
            values[key] = value
        errors = [key for key in cls._CONFIG_FIELDS if not values.get(key)] + duplicates
        if errors:
            raise ValueError("invalid live-test configuration: " + ", ".join(errors))
        parsed = cls._convert_values(values)
        cls._validate_values(parsed)
        return cls(**parsed)

    @classmethod
    def _convert_values(cls, values: dict[str, str]) -> dict[str, Any]:
        """Convert file strings to the configuration's numeric and tuple values."""
        try:
            parsed: dict[str, Any] = {
                field: values[key] for key, field in cls._CONFIG_FIELDS.items()
            }
            parsed.update(
                {
                    field: values[key]
                    for key, field in cls._OPTIONAL_CONFIG_FIELDS.items()
                    if key in values
                }
            )
            for field_name in (
                "system_profile_writer_window",
                "system_profile_continue_on_error",
                "system_profile_ownership_override",
            ):
                if field_name in parsed:
                    if parsed[field_name] not in ("true", "false"):
                        raise ValueError(f"{field_name} requires true or false")
                    parsed[field_name] = parsed[field_name] == "true"
            suffix_key = "LIVE_TEST_PROFILE_ARTIFACT_SUFFIX_HEX_LENGTH"
            if suffix_key in values:
                if values[suffix_key] not in ("12", "16"):
                    raise ValueError(f"{suffix_key} must be 12 or 16")
                parsed[cls._OPTIONAL_CONFIG_FIELDS[suffix_key]] = int(
                    values[suffix_key]
                )
            for key in cls._CONFIG_FIELDS:
                if key == "LIVE_TEST_SRIOV_LOGICAL_PORT_ID":
                    continue  # the HMC reports hex ids such as 2700400a
                if key.endswith(
                    (
                        "_MIB",
                        "_VCPUS",
                        "_SLOT",
                        "_PARTITION_ID",
                        "_PORT",
                        "_ID",
                        "_START",
                        "_END",
                    )
                ):
                    parsed[cls._CONFIG_FIELDS[key]] = int(values[key])
                elif key.endswith("_PROCS"):
                    parsed[cls._CONFIG_FIELDS[key]] = float(values[key])
            parsed["sriov_capacity_percent"] = float(
                values["LIVE_TEST_SRIOV_CAPACITY_PERCENT"]
            )
            parsed["protected_lpar_names"] = tuple(
                name.strip()
                for name in values["LIVE_TEST_PROTECTED_LPAR_NAMES"].split(",")
                if name.strip()
            )
        except ValueError as exc:
            raise ValueError(f"invalid live-test configuration: {exc}") from exc
        return parsed

    @classmethod
    def _validate_values(cls, parsed: dict[str, Any]) -> None:
        """Reject unsafe identifiers and inconsistent live-test resource limits."""
        # Fields that must be strictly positive (> 0).
        # sriov_physical_port_id is intentionally excluded: physical port IDs
        # are zero-indexed on Power hardware, so port 0 is valid.
        positive_fields = (
            "scratch_create_desired_memory_mib",
            "scratch_create_max_memory_mib",
            "scratch_create_desired_vcpus",
            "scratch_create_max_vcpus",
            "scratch_modify_desired_memory_mib",
            "scratch_modify_max_memory_mib",
            "dry_run_memory_mib",
            "provision_min_memory_mib",
            "provision_desired_memory_mib",
            "provision_max_memory_mib",
            "provision_desired_vcpus",
            "provision_max_vcpus",
            "provision_vlan_id",
            "provision_disk_mib",
            "sriov_adapter_id",
            "sriov_capacity_percent",
            "iso_http_port",
            "vmedia_repository_size_mib",
            "vmedia_short_repository_size_mib",
            "placement_memory_mib",
            "vlan_range_start",
            "vlan_range_end",
        )
        invalid = [name for name in positive_fields if parsed[name] <= 0]
        # Processing units are floats, and `nan` would slip past a `<= 0` test.
        invalid += [
            name
            for name in ("scratch_create_desired_procs", "scratch_create_max_procs")
            if not 0 < parsed[name] < math.inf
        ]
        # sriov_physical_port_id must be non-negative (>= 0).
        if parsed["sriov_physical_port_id"] < 0:
            invalid.append("sriov_physical_port_id")
        if not parsed["protected_lpar_names"]:
            invalid.append("LIVE_TEST_PROTECTED_LPAR_NAMES")
        if len(parsed["vdisk_name"]) > VIRTUAL_DISK_NAME_MAX:
            invalid.append(
                f"LIVE_TEST_VDISK_NAME must be at most {VIRTUAL_DISK_NAME_MAX} characters"
            )
        # Both names identify VIOS storage objects; the group enters the CLI listing.
        invalid += [
            f"{key} may contain only letters, digits, '.', '_' and '-', "
            "and must not start with '-'"
            for key, field_name in (
                ("LIVE_TEST_VDISK_VOLUME_GROUP_NAME", "vdisk_volume_group_name"),
                ("LIVE_TEST_VDISK_NAME", "vdisk_name"),
            )
            if not _VIOS_OBJECT_NAME.fullmatch(parsed[field_name])
        ]
        # Interpolated unquoted into the recovery `chhwres` command strings.
        if not _HEX_ID.fullmatch(parsed["sriov_logical_port_id"]):
            invalid.append(
                "LIVE_TEST_SRIOV_LOGICAL_PORT_ID must be lowercase hexadecimal digits"
            )
        if parsed["iso_http_port"] > 65535:
            invalid.append("LIVE_TEST_ISO_HTTP_PORT")
        if parsed["provision_vlan_id"] > 4094:
            invalid.append("LIVE_TEST_PROVISION_VLAN_ID")
        # The HMC's RepositorySize and DiskCapacity are whole GiB; the create tools
        # refuse anything else, which would otherwise surface mid-arm after a delete
        # (#963, #970).
        invalid += [
            f"{key} must be a multiple of 1024"
            for key in (
                "LIVE_TEST_VMEDIA_REPOSITORY_SIZE_MIB",
                "LIVE_TEST_VMEDIA_SHORT_REPOSITORY_SIZE_MIB",
                "LIVE_TEST_PROVISION_DISK_MIB",
            )
            if parsed[cls._CONFIG_FIELDS[key]] % 1024
        ]
        if (
            parsed["vlan_range_start"] > parsed["vlan_range_end"]
            or parsed["vlan_range_end"] > 4094
        ):
            invalid.append("LIVE_TEST_VLAN_RANGE_START/LIVE_TEST_VLAN_RANGE_END")
        if (
            parsed["scratch_create_desired_memory_mib"]
            > parsed["scratch_create_max_memory_mib"]
            or parsed["scratch_create_desired_vcpus"]
            > parsed["scratch_create_max_vcpus"]
            or parsed["scratch_create_desired_procs"]
            > parsed["scratch_create_max_procs"]
            or parsed["scratch_modify_desired_memory_mib"]
            > parsed["scratch_modify_max_memory_mib"]
            or not (
                parsed["provision_min_memory_mib"]
                <= parsed["provision_desired_memory_mib"]
                <= parsed["provision_max_memory_mib"]
            )
            or parsed["provision_desired_vcpus"] > parsed["provision_max_vcpus"]
        ):
            invalid.append("inconsistent resource limits")
        if invalid:
            raise ValueError("invalid live-test configuration: " + ", ".join(invalid))


@dataclass
class LiveTestArtifacts:
    """Mutable discoveries and recovery state owned by one live-test invocation."""

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


def decode_saved_config(value: Any) -> LiveTestConfig:
    """Decode the canonical JSON form of a live-test configuration."""
    if not isinstance(value, dict):
        raise TypeError("results config must be a JSON object")
    expected = asdict(LiveTestConfig())
    parsed = dict(value)
    # A results document written before #1030 removed these settings is still a
    # valid restore source; every other field difference remains a mismatch.
    parsed.pop("dry_run_vios_slot", None)
    parsed.pop("dry_run_vios_partition_id", None)
    # And before #632 retired the configured test-user name, or #1377 the
    # network-test partition.
    parsed.pop("test_user", None)
    parsed.pop("nettest_name", None)
    # ST42 settings are optional and disabled in older results documents.
    parsed.setdefault("scratch_pv_name", "")
    parsed.setdefault("scratch_vg_name", "")
    parsed.setdefault("profile_artifact_suffix_hex_length", 16)
    for name in (
        "system_profile_members_json",
        "system_profile_writer_window",
        "system_profile_activation_action",
        "system_profile_continue_on_error",
        "system_profile_ownership_override",
    ):
        parsed.setdefault(name, expected[name])
    if set(parsed) != set(expected):
        raise ValueError("results config fields do not match LiveTestConfig")
    protected = parsed["protected_lpar_names"]
    if not isinstance(protected, list) or not all(
        isinstance(name, str) for name in protected
    ):
        raise TypeError(
            "results config protected_lpar_names must be an array of strings"
        )
    parsed["protected_lpar_names"] = tuple(protected)
    for name, expected_value in expected.items():
        if name != "protected_lpar_names" and type(parsed[name]) is not type(
            expected_value
        ):
            raise TypeError(f"results config {name} has the wrong JSON type")
    if parsed["profile_artifact_suffix_hex_length"] not in (12, 16):
        raise ValueError(
            "results config profile_artifact_suffix_hex_length must be 12 or 16"
        )
    return LiveTestConfig(**parsed)


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
    ):
        parsed.setdefault(name, default)
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
