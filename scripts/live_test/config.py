"""Shared live-test configuration and saved recovery-document contracts."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.documents.storage import VIRTUAL_DISK_NAME_MAX

if TYPE_CHECKING:
    from hmcpctl.config import HMCConfig


ENV_FILE = Path(".env")


ENVIRONMENT_PREFIX = "LIVE_TEST_ENV_"


_HEX_ID = re.compile(r"[0-9a-f]+")


_VIOS_OBJECT_NAME = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class LiveTestConfig:
    """Validated operator configuration for live-test executions."""

    creation_memory_case: str = ""
    creation_memory_primary_vios_name: str = ""
    creation_memory_secondary_vios_name: str = ""
    creation_memory_fresh_delete_authorized: bool = False
    paging_expected_current_vios: str = ""
    paging_writer_window: bool = False
    accelerator_qos_request_json: str = ""
    creation_memory_layout_hpt_ratio: str = ""
    creation_memory_layout_ppt_ratio: str = ""
    creation_memory_layout_fresh_vios_delete_authorized: bool = False
    system_name: str = "example-lt-609-system"
    lp3_name: str = "example-lt-609-lpar"
    scratch_name: str = "example-lt-609-scratch"
    vdisk_name: str = "lt609-disk"
    scratch_pv_name: str = ""
    scratch_vg_name: str = ""
    profile_artifact_suffix_hex_length: int = 16
    partition_profile_target: str = ""
    partition_profile_kind: str = "LogicalPartition"
    partition_profile_writer_window: bool = False
    partition_profile_memory_mib: int = 0
    partition_profile_contract_packet: str = ""
    partition_profile_contract_sha256: str = ""
    system_profile_members_json: str = ""
    system_profile_writer_window: bool = False
    system_profile_activation_action: str = "none"
    system_profile_continue_on_error: bool = False
    system_profile_ownership_override: bool = False
    # The native boot-started arms' launcher media and their one pinned MAC (#1850).
    native_boot_mac: str = ""
    native_ubuntu_iso_path: str = ""
    native_ubuntu_media_result: str = ""
    native_rocky_iso_path: str = ""
    native_rocky_media_result: str = ""
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
        "LIVE_TEST_PAGING_EXPECTED_CURRENT_VIOS": "paging_expected_current_vios",
        "LIVE_TEST_PAGING_WRITER_WINDOW": "paging_writer_window",
        "LIVE_TEST_ACCELERATOR_QOS_REQUEST_JSON": "accelerator_qos_request_json",
        "LIVE_TEST_CREATION_MEMORY_CASE": "creation_memory_case",
        "LIVE_TEST_CREATION_MEMORY_PRIMARY_VIOS_NAME": "creation_memory_primary_vios_name",
        "LIVE_TEST_CREATION_MEMORY_SECONDARY_VIOS_NAME": "creation_memory_secondary_vios_name",
        "LIVE_TEST_CREATION_MEMORY_FRESH_DELETE_AUTHORIZED": "creation_memory_fresh_delete_authorized",
        "LIVE_TEST_CREATION_MEMORY_LAYOUT_HPT_RATIO": "creation_memory_layout_hpt_ratio",
        "LIVE_TEST_CREATION_MEMORY_LAYOUT_PPT_RATIO": "creation_memory_layout_ppt_ratio",
        "LIVE_TEST_CREATION_MEMORY_LAYOUT_FRESH_VIOS_DELETE_AUTHORIZED": "creation_memory_layout_fresh_vios_delete_authorized",
        "LIVE_TEST_PARTITION_PROFILE_TARGET": "partition_profile_target",
        "LIVE_TEST_PARTITION_PROFILE_KIND": "partition_profile_kind",
        "LIVE_TEST_PARTITION_PROFILE_WRITER_WINDOW": "partition_profile_writer_window",
        "LIVE_TEST_PARTITION_PROFILE_MEMORY_MIB": "partition_profile_memory_mib",
        "LIVE_TEST_PARTITION_PROFILE_CONTRACT_PACKET": "partition_profile_contract_packet",
        "LIVE_TEST_PARTITION_PROFILE_CONTRACT_SHA256": "partition_profile_contract_sha256",
        "LIVE_TEST_SYSTEM_PROFILE_MEMBERS_JSON": "system_profile_members_json",
        "LIVE_TEST_SYSTEM_PROFILE_WRITER_WINDOW": "system_profile_writer_window",
        "LIVE_TEST_SYSTEM_PROFILE_ACTIVATION_ACTION": "system_profile_activation_action",
        "LIVE_TEST_SYSTEM_PROFILE_CONTINUE_ON_ERROR": "system_profile_continue_on_error",
        "LIVE_TEST_SYSTEM_PROFILE_OWNERSHIP_OVERRIDE": "system_profile_ownership_override",
        "LIVE_TEST_NATIVE_BOOT_MAC": "native_boot_mac",
        "LIVE_TEST_NATIVE_UBUNTU_ISO_PATH": "native_ubuntu_iso_path",
        "LIVE_TEST_NATIVE_UBUNTU_MEDIA_RESULT": "native_ubuntu_media_result",
        "LIVE_TEST_NATIVE_ROCKY_ISO_PATH": "native_rocky_iso_path",
        "LIVE_TEST_NATIVE_ROCKY_MEDIA_RESULT": "native_rocky_media_result",
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
            if "partition_profile_memory_mib" in parsed:
                parsed["partition_profile_memory_mib"] = int(
                    parsed["partition_profile_memory_mib"]
                )
            for field_name in (
                "creation_memory_fresh_delete_authorized",
                "creation_memory_layout_fresh_vios_delete_authorized",
                "paging_writer_window",
                "partition_profile_writer_window",
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


def hmc_identity(config: HMCConfig) -> dict[str, str | int | bool]:
    """Return the non-secret HMC identity bound to a persisted artifact set."""
    return {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "verify_ssl": config.verify_ssl,
    }


def decode_saved_config(value: Any) -> LiveTestConfig:
    """Decode the canonical JSON form of a live-test configuration."""
    if not isinstance(value, dict):
        raise TypeError("results config must be a JSON object")
    expected = asdict(LiveTestConfig())
    parsed = dict(value)
    parsed.setdefault("accelerator_qos_request_json", "")
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
        "paging_expected_current_vios",
        "paging_writer_window",
        "creation_memory_case",
        "creation_memory_primary_vios_name",
        "creation_memory_secondary_vios_name",
        "creation_memory_fresh_delete_authorized",
        "creation_memory_layout_hpt_ratio",
        "creation_memory_layout_ppt_ratio",
        "creation_memory_layout_fresh_vios_delete_authorized",
        "partition_profile_target",
        "partition_profile_kind",
        "partition_profile_writer_window",
        "partition_profile_memory_mib",
        "partition_profile_contract_packet",
        "partition_profile_contract_sha256",
        "system_profile_members_json",
        "system_profile_writer_window",
        "system_profile_activation_action",
        "system_profile_continue_on_error",
        "system_profile_ownership_override",
        "native_boot_mac",
        "native_ubuntu_iso_path",
        "native_ubuntu_media_result",
        "native_rocky_iso_path",
        "native_rocky_media_result",
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
    LparMemoryLayout(
        hpt_ratio=parsed["creation_memory_layout_hpt_ratio"] or None,
        ppt_ratio=parsed["creation_memory_layout_ppt_ratio"] or None,
    )
    return LiveTestConfig(**parsed)
