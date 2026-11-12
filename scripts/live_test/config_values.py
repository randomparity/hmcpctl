"""Convert and validate operator-supplied live-test configuration values."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from hmcpctl.documents.lpar_io import validate_capacity_percent
from hmcpctl.documents.storage import VIRTUAL_DISK_NAME_MAX

_HEX_ID = re.compile(r"[0-9a-f]+")


_VIOS_OBJECT_NAME = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*")


def convert_values(
    values: dict[str, str],
    required_fields: Mapping[str, str],
    optional_fields: Mapping[str, str],
) -> dict[str, Any]:
    """Convert file strings to the configuration's numeric and tuple values."""
    try:
        parsed: dict[str, Any] = {
            field: values[key] for key, field in required_fields.items()
        }
        parsed.update(
            {
                field: values[key]
                for key, field in optional_fields.items()
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
            parsed[optional_fields[suffix_key]] = int(values[suffix_key])
        for key in required_fields:
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
                parsed[required_fields[key]] = int(values[key])
            elif key.endswith("_PROCS"):
                parsed[required_fields[key]] = float(values[key])
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


def validate_values(parsed: dict[str, Any], required_fields: Mapping[str, str]) -> None:
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
        "iso_http_port",
        "vmedia_repository_size_mib",
        "vmedia_short_repository_size_mib",
        "placement_memory_mib",
        "vlan_range_start",
        "vlan_range_end",
    )
    invalid = [name for name in positive_fields if parsed[name] <= 0]
    try:
        validate_capacity_percent(Decimal(str(parsed["sriov_capacity_percent"])))
    except ValueError:
        invalid.append("sriov_capacity_percent")
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
        if parsed[required_fields[key]] % 1024
    ]
    if (
        parsed["vlan_range_start"] > parsed["vlan_range_end"]
        or parsed["vlan_range_end"] > 4094
    ):
        invalid.append("LIVE_TEST_VLAN_RANGE_START/LIVE_TEST_VLAN_RANGE_END")
    if (
        parsed["scratch_create_desired_memory_mib"]
        > parsed["scratch_create_max_memory_mib"]
        or parsed["scratch_create_desired_vcpus"] > parsed["scratch_create_max_vcpus"]
        or parsed["scratch_create_desired_procs"] > parsed["scratch_create_max_procs"]
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
