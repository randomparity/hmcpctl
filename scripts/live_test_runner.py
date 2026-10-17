"""Live integration test runner: HMC MCP tools against real hardware.

Calls the tools through the in-process FastMCP client against the HMC named by
the configuration. Results print to stdout as they complete and are written to
a JSON document on exit.

This mutates a managed system. The procedure is docs/live-testing.md: run
`scripts/live_test_preflight.py` to see what a selection will touch,
`scripts/live_{round2,vmedia,sriov,dedicated,bare_cec,profiles,users,vios_backup,pcm,
lpar_config,lpar_power,storage}.py` to dispatch one arm,
`scripts/live_test_evidence.py` to produce a citable matrix, and
`scripts/live_test_recovery.py` afterwards to confirm nothing is stranded.

Usage:
    uv run --no-sync python scripts/live_test_runner.py [SUBTASK] [options]

`--no-sync` is required: a bare `uv run` prunes the `app` extra and the runner
stops importing (AGENTS.md).

With no selection every subtask runs, 0 through 42: there is none from 26 to 36,
which are other arms' row ids, and 9, 11, 37, 38, 39, 40, 41 and 42 SKIP outside their
own `network`, `users`, `vios-backup`, `pcm`, `lpar-config`, `storage` and
`lpar-power` groups. A bare
number runs that one subtask; `--group NAME` runs one arm. Results go to `test-results-<group>.json`, or `test-results-round2.json`
for a bare or whole-suite run, unless
`--results-file` names another path. That path must be git-ignored.

Pre-run requirement: HMC credentials, from the environment, a `config.toml`
profile in the platform config directory, or a local .env file. That directory
is `~/.config/hmcpctl` on Linux and `~/Library/Application Support/hmcpctl` on
macOS. The runner never creates or patches .env: when credentials are absent,
it exits with manual configuration instructions.

HMC_SCHEMA_VERSION is not among them. It is opt-in and unset by default
(`src/hmcpctl/config.py`), and where it lands is per call site rather than per
HTTP method: UOM requests carry `X-HMC-Schema-Version` unless their own call
site passes `include_schema_version=False` (issue #96 — grep that flag under
`src/hmcpctl/client/` for the current set, which no list here can track), every
`/rest/api/web/` request carries it (issue #99), and requests that build their
own headers never carry it — including `submit_job`, so the job path every
power operation takes is unaffected either way. A run therefore starts with or
without it. `docs/compatibility.md` is the full account.

The run header prints the resolved value, including `(not set)`, to stdout, and
the results document records the same string as `run.schema_version`. Every
emitted observation records it too, as `schema_version` (ADR 0186); a value that
field's grammar cannot hold is warned about at startup and writes no observations.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import inspect
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import check_capability_inventory
from fastmcp import Client
from live_test.bare_cec import exercise_bare_cec
from live_test.connectivity import inventory_connectivity
from live_test.detach_probe import exercise_detach_probe
from live_test.escape_hatch import exercise_cli_escape_hatch
from live_test.inventory import capture_lpar_baseline
from live_test.lpar import (
    exercise_lpar_lifecycle,
    mutate_lpar_properties,
    restore_lpar_baseline,
)
from live_test.lpar_config import exercise_lpar_config
from live_test.lpar_power import exercise_lpar_power
from live_test.metrics import (
    exercise_pcm_preferences,
    inspect_metrics_jobs,
    inspect_metrics_templates,
)
from live_test.network import inventory_network, mutate_virtual_networking
from live_test.observation import (
    CLEANUP,
    SCENARIO_ID,
    Assertion,
    CallFailure,
    ExpectedOutcome,
    KnownGap,
    classify_failure,
)
from live_test.pcie import exercise_dedicated_pcie_assignment, exercise_sriov_assignment
from live_test.profiles import inventory_lpar_profiles
from live_test.provisioning import (
    exercise_storage_provisioning,
    validate_provisioning_dry_run,
)
from live_test.storage import inventory_storage
from live_test.storage_lifecycle import exercise_disk_lifecycle
from live_test.storage_volume_group import exercise_volume_group
from live_test.users import exercise_users, inventory_users
from live_test.vios_backup import exercise_vios_backup
from live_test.vmedia import (
    IsoHttpServer,
    vmedia_boot_verification,
    vmedia_bootstrap_and_create_repo,
    vmedia_mapping_crossvalidation,
    vmedia_mount_unmount,
    vmedia_short_repo_lifecycle,
    vmedia_teardown,
    vmedia_upload_iso,
)

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.config import HMCConfig, env_var_value
from hmcpctl.documents.storage import VIRTUAL_DISK_NAME_MAX
from hmcpctl.server import TOOL_SECURITY, _gates, create_mcp
from hmcpctl.server_tools.command import configure_arbitrary_command_tool

# ---------------------------------------------------------------------------
# Pre-run guard: HMC credentials must resolve before the first dispatch
# ---------------------------------------------------------------------------

_ENV_FILE = Path(".env")

_ENVIRONMENT_PREFIX = "LIVE_TEST_ENV_"

#: The environment a live observation was made in. Both or neither: a lone key
#: is a configuration error, caught at startup rather than after a hardware run.
ENVIRONMENT_KEYS = ("LIVE_TEST_ENV_HMC_RELEASE", "LIVE_TEST_ENV_HARDWARE_FAMILY")

#: A run records its release with the maintenance level, so one HMC is never
#: catalogued under two tokens (#1335). Stricter than the catalog's own
#: `HMC_RELEASE`, which still admits the bare `V<n>R<n>` rows recorded earlier.
RUN_HMC_RELEASE = re.compile(r"V\d+R\d+M\d+")

_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?P<name>password|passwd|token|secret|api[_-]?key)"
    r"(?P<separator>\s*(?:=|:)\s*)(?P<quote>['\"]?)(?P<value>[^\s,;'\"&]+)"
    r"(?P=quote)"
)
_URL_USERINFO_RE = re.compile(r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s@]+@")
#: Labels admit `_` (#927). It is a word character, so without it `\b` cannot fire
#: inside an underscore label: the match began at the next dot-separated label and
#: left every label up to the underscore one readable, or missed a two-label name.
_HOSTNAME_RE = re.compile(
    r"(?i)\b(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z]{2,63}\b"
)
#: A single-dot `name.ext` with one of these extensions is a filename, not a
#: host (#914). None is an IANA TLD — `.py`, `.md`, `.sh` and `.zip` are, so
#: they stay out; a multi-label name is redacted whatever its last label.
_FILENAME_EXTENSIONS = frozenset(
    {
        "cfg",
        "conf",
        "csv",
        "ini",
        "iso",
        "json",
        "log",
        "toml",
        "txt",
        "xml",
        "yaml",
        "yml",
    }
)
_ABSOLUTE_PATH_RE = re.compile(r"(?<![:\w])/(?:[^\s/]+/)*[^\s,;:'\")]+")


def _redact_hostname(match: re.Match[str]) -> str:
    stem, _, extension = match.group().rpartition(".")
    if "." not in stem and extension.lower() in _FILENAME_EXTENSIONS:
        return match.group()
    return "<REDACTED-HOST>"


def _redact_failure_text(value: str) -> str:
    """Replace sensitive values in runner failure diagnostics."""
    value = _SECRET_VALUE_RE.sub(r"\g<name>\g<separator><REDACTED-SECRET>", value)
    value = _URL_USERINFO_RE.sub(r"\g<scheme><REDACTED-URL-USERINFO>@", value)
    value = _HOSTNAME_RE.sub(_redact_hostname, value)
    return _ABSOLUTE_PATH_RE.sub("<REDACTED-PATH>", value)


def _redact_failure_data(data: Any) -> Any:
    """Preserve failure result shapes while redacting their string leaves."""
    if isinstance(data, dict):
        return {key: _redact_failure_data(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_redact_failure_data(value) for value in data]
    return _redact_failure_text(str(data))


#: The `HMC_*` names whose reader folds their casing: `HMCConfig`'s own fields,
#: and only those. `HMC_PROFILE` and a profile's `password_env` target carry the
#: prefix but are looked up in `os.environ` directly (see the "Variable names are
#: matched without regard to case" section of docs/environment-variables.md), so
#: treating a `hmc_profile` export as already-set would suppress the `.env` line
#: spelling it canonically while nothing ever read the variant.
#:
#: Held folded **down**, and matched that way below, because that is the relation
#: pydantic-settings uses and the one `env_var_value` looks names up with. Over
#: Unicode the two directions are different relations: `hmc_ssh_Key_file`
#: lowers onto `ssh_key_file` and so is a name the loader reads, while its
#: upper-fold is not `HMC_SSH_KEY_FILE` — an upper-cased gate would miss it, fall
#: through to the exact-case test, and re-open the `.env`-outranks-the-export
#: inversion for exactly the names this set exists to cover.
_FOLDED_ENV_NAMES = frozenset(
    f"hmc_{field.lower()}" for field in HMCConfig.model_fields
)


def _already_set(name: str) -> bool:
    """Return whether *name* is already set using HMCConfig's case-insensitive lookup."""
    if name.lower() in _FOLDED_ENV_NAMES:
        return env_var_value(name) is not None
    return name in os.environ


def _load_dotenv() -> None:
    """Load key=value pairs from .env into os.environ (simple, no deps)."""
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and not _already_set(key):
            os.environ[key] = val


def _bootstrap_config() -> bool:
    """Populate HMC_* env vars from config.toml profile, then .env fallback.

    Priority (highest first):
      1. Already-set HMC_* environment variables
      2. The default profile in the platform config directory's config.toml
      3. Local .env file (legacy key=value pairs)

    Exits with a clear message when no usable credentials are found.
    """
    from hmcpctl.config import ConfigError, config_dir, load_profile

    # Try the TOML config first.
    try:
        cfg = load_profile()
        # Inject values that are not already set by the environment.
        mapping = {
            "HMC_HOST": cfg.host,
            "HMC_PORT": str(cfg.port),
            "HMC_USER": cfg.user,
            "HMC_PASSWORD": cfg.password,
            "HMC_VERIFY_SSL": str(cfg.verify_ssl).lower(),
            "HMC_SCHEMA_VERSION": cfg.schema_version,
        }
        for key, val in mapping.items():
            if val and not _already_set(key):
                os.environ[key] = val
        print("  Credentials loaded from configured profile")
        return True
    except ConfigError as exc:
        print(
            f"  ⚠️  config.toml: {_redact_failure_text(str(exc))} — falling back to .env"
        )

    # Fallback: local .env
    _load_dotenv()

    # env_var_value, not os.environ.get: this predicts whether `HMCConfig` will
    # resolve a password, and `HMCConfig` reads the name case-blind. An exact-case
    # test refused to start on a lower- or mixed-case `hmc_password` export that
    # would have connected (#543).
    if not env_var_value("HMC_PASSWORD"):
        print("❌  No HMC credentials found.")
        # The resolved path, not a Linux literal: this same message sent a
        # macOS operator to a directory their platform never reads.
        print(f"   Configure {config_dir() / 'config.toml'} or a local .env file.")
        return False
    return True


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
        "LIVE_TEST_SCRATCH_PV_NAME": "scratch_pv_name",
        "LIVE_TEST_SCRATCH_VG_NAME": "scratch_vg_name",
        "LIVE_TEST_DEDICATED_PCIE_SYSTEM_NAME": "dedicated_pcie_system_name",
        "LIVE_TEST_DEDICATED_PCIE_LPAR_PREFIX": "dedicated_pcie_lpar_prefix",
        "LIVE_TEST_DEDICATED_PCIE_PROFILE_NAME": "dedicated_pcie_profile_name",
        "LIVE_TEST_DEDICATED_PCIE_DRC_INDEX": "dedicated_pcie_drc_index",
        "LIVE_TEST_ACCEPT_PLATFORM_DUMP": "accept_platform_dump",
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
        path = path or _ENV_FILE
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
            if key.startswith(_ENVIRONMENT_PREFIX):
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
        return cls(**parsed)


@dataclass
class LiveTestArtifacts:
    """Mutable discoveries and recovery state owned by one live-test invocation."""

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


#: Stand-in for an argument whose value is not knowable without running the
#: scenario. The static dispatch guard checks the types it *can* resolve and
#: passes over the rest rather than guessing at them.
UNRESOLVED_ARGUMENT: Any = object()

#: JSON-schema type name to the Python types that satisfy it. `number` admits
#: `int` under the PEP 484 numeric tower; `bool` is excluded from both numeric
#: entries because it is an `int` subclass and would otherwise pass as a
#: quantity.
_SCHEMA_TYPES: Mapping[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
    "null": (type(None),),
}


#: Schema keys that carry no constraint on a value's type. A property holding
#: only these admits anything, so reading no type from it is the right answer
#: rather than a gap.
_UNCONSTRAINING_KEYS = frozenset(
    {"description", "default", "title", "examples", "deprecated", "readOnly"}
)


def _declared_types(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """The JSON-schema type names one property accepts.

    An optional parameter is served as `anyOf[{type: T}, {type: null}]` rather
    than a bare `type`, so both spellings have to be read to see `T` at all.
    """
    declared = schema.get("type")
    if isinstance(declared, str):
        return (declared,)
    if isinstance(declared, list):
        return tuple(entry for entry in declared if isinstance(entry, str))
    return tuple(
        name
        for branch in schema.get("anyOf") or ()
        if isinstance(branch, Mapping)
        for name in _declared_types(branch)
    )


def _type_problem(
    tool: str, name: str, value: Any, schema: Mapping[str, Any]
) -> str | None:
    """Report a supplied value whose Python type no declared schema type admits."""
    if value is UNRESOLVED_ARGUMENT:
        return None
    declared = _declared_types(schema)
    accepted = tuple(
        accepted_type
        for declared_name in declared
        for accepted_type in _SCHEMA_TYPES.get(declared_name, ())
    )
    if not accepted:
        if schema.keys() - _UNCONSTRAINING_KEYS:
            # The property constrains something this guard cannot read — `$ref`,
            # `allOf`, `oneOf`, `const`, a bare `enum`. Staying quiet would
            # disable the check for that argument with nothing to show for it,
            # which is the failure mode the guard exists to prevent, so say so.
            return (
                f"{tool}: {name} has a schema shape this guard cannot read: "
                f"{sorted(schema.keys() - _UNCONSTRAINING_KEYS)}"
            )
        # A property with no constraints at all admits anything, so nothing to check.
        return None
    if isinstance(value, bool) and bool not in accepted:
        # `bool` is an `int` subclass, so a plain isinstance check would admit it
        # wherever a quantity is declared and report `True` as a capacity of 1.
        return f"{tool}: {name} expects {' or '.join(declared)}, got bool"
    if isinstance(value, accepted):
        return None
    return f"{tool}: {name} expects {' or '.join(declared)}, got {type(value).__name__}"


def _dispatch_problems(
    tool: str,
    arguments: Iterable[str] | Mapping[str, Any],
    schemas: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    """Report every way a dispatch disagrees with the tool's served input schema.

    Passing a mapping also checks each supplied value's type, which is what
    catches a scenario handing a tool an `int` where it declares a `str`. Names
    alone are accepted so a caller that has no values can still check those.
    """
    schema = schemas.get(tool)
    if schema is None:
        return (f"{tool} is not a registered tool",)
    properties = schema.get("properties") or {}
    values: Mapping[str, Any] = (
        arguments
        if isinstance(arguments, Mapping)
        else dict.fromkeys(arguments, UNRESOLVED_ARGUMENT)
    )
    supplied = list(values)
    problems = [
        f"{tool}: unknown argument {name}"
        for name in supplied
        if name not in properties
    ]
    problems += [
        f"{tool}: missing required argument {name}"
        for name in schema.get("required") or ()
        if name not in supplied
    ]
    problems += [
        problem
        for name, value in values.items()
        if name in properties
        and (problem := _type_problem(tool, name, value, properties[name])) is not None
    ]
    return tuple(problems)


def _result_for(status: str) -> str:
    """Map a printed status to its result vocabulary entry.

    None of the three promotes; ``passed`` is reachable only through
    :meth:`RunState.record_verified`.
    """
    if status == "FAIL":
        return "failed"
    if status == "SKIP":
        return "skipped"
    return "observed"


def _observation_id(subtask: int, tool: str) -> str:
    """Derive an observation id from the recording site's subtask and tool."""
    return f"st{subtask}-{tool.split(' (')[0].replace('_', '-')}"


@dataclass
class RunState:
    """Mutable output owned by a single invocation of the live runner."""

    config: LiveTestConfig = field(default_factory=LiveTestConfig)
    artifacts: LiveTestArtifacts = field(default_factory=LiveTestArtifacts)
    results: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    known_gaps: set[tuple[str, str]] = field(default_factory=set)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    iso_http_server: IsoHttpServer = field(default_factory=IsoHttpServer)
    # The `--group` this run was dispatched with; a system-wide step gates on it.
    group: str | None = None
    hmc_config: HMCConfig | None = None

    async def call(
        self,
        client: Client,
        tool: str,
        *,
        expected: Sequence[ExpectedOutcome] = (),
        reuse_gaps: bool = True,
        **kwargs: Any,
    ) -> tuple[str, Any]:
        """Call a tool and return a PASS or FAIL result without raising."""
        # FastMCP would reject an invalid dispatch anyway; checking here is what
        # gives the failure a stable reason instead of a pydantic rendering, and
        # keeps a harness defect from ever reaching the real HMC.
        _validate_expected_dispatch(tool, expected)
        problems = (
            _dispatch_problems(tool, kwargs, self.schemas) if self.schemas else ()
        )
        if problems:
            return "FAIL", CallFailure(
                "InvalidDispatch", "; ".join(problems), "", None, False
            )
        if reuse_gaps:
            for outcome in expected:
                if (
                    not outcome.transient
                    and (outcome.operation, outcome.variant) in self.known_gaps
                ):
                    return "SKIP", KnownGap(outcome)
        try:
            result = await client.call_tool(tool, kwargs)
            if hasattr(result, "data") and result.data is not None:
                return "PASS", result.data
            if hasattr(result, "content"):
                parts = [
                    str(block.text) if hasattr(block, "text") else str(block)
                    for block in result.content
                ]
                text = "\n".join(parts)
            else:
                text = str(result)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = text
            return "PASS", data
        except Exception as exc:  # noqa: BLE001 - the harness records any tool failure as a FAIL row; totality is the contract
            return "FAIL", classify_failure(exc)

    def record(
        self,
        subtask: int,
        tool: str,
        status: str,
        data: Any,
        note: str = "",
        *,
        result: str | None = None,
    ) -> None:
        """Append and print one result entry.

        `result` is supplied only by `record_verified`, the one path that can
        reach `passed`. Passing it in keeps the row and its verdict written
        together, rather than patching `results[-1]` after the fact where any
        later change to how rows are appended would retarget the patch.
        """
        if isinstance(data, CallFailure):
            # Only the message is persisted: the traceback text stays on the
            # ``CallFailure`` for the caller that classifies it, and never reaches
            # the results document, whose redaction pass reads string leaves.
            safe_data: Any = _redact_failure_text(data.message)
        else:
            if is_dataclass(data) and not isinstance(data, type):
                # FastMCP serves a dataclass tool result as a generated dataclass.
                data = asdict(data)
            safe_data = _redact_failure_data(data) if status == "FAIL" else data
        entry = {
            "subtask": subtask,
            "tool": tool,
            "status": status,
            "result": result or _result_for(status),
            "timestamp": datetime.now(UTC).isoformat(),
            "note": note,
            "data": (
                safe_data
                if isinstance(safe_data, (dict, list))
                else str(safe_data)[:2000]
            ),
        }
        self.results.append(entry)
        icon = "✅" if status == "PASS" else ("⚠️" if status == "SKIP" else "❌")
        note_str = f" — {note}" if note else ""
        print(f"  {icon} ST{subtask} {tool}{note_str}")
        if status == "FAIL":
            print(f"     ERROR: {str(safe_data)[:300]}")

    def skip(self, subtask: int, tool: str, reason: str) -> None:
        """Record a skipped operation."""
        self.record(subtask, tool, "SKIP", None, reason)

    def record_with_expected(
        self,
        subtask: int,
        tool: str,
        status: str,
        data: Any,
        expected: Sequence[ExpectedOutcome],
    ) -> None:
        """Turn a declared HMC limitation into SKIP; record other outcomes verbatim.

        An ``InvalidDispatch`` failure is the harness's own defect, so it is
        recorded before any declaration is consulted — that is the substitution
        the old substring match allowed.
        """
        if status == "SKIP" and isinstance(data, KnownGap):
            self.skip(subtask, tool, f"known gap: {data.outcome.reason}")
            return
        if (
            status == "FAIL"
            and isinstance(data, CallFailure)
            and data.exception_type != "InvalidDispatch"
        ):
            for outcome in expected:
                if outcome.matches(data):
                    self.skip(subtask, tool, outcome.reason)
                    if not outcome.transient and not any(
                        row["operation"] == outcome.operation
                        and row["missing_scope"]["variant"] == outcome.variant
                        for row in self.gaps
                    ):
                        self.gaps.append(
                            {
                                "operation": outcome.operation,
                                "missing_scope": {
                                    "variant": outcome.variant,
                                    "parameters": [],
                                    "confirmation": {
                                        "observed_at": datetime.now(UTC).strftime(
                                            "%Y-%m-%dT%H:%M:%SZ"
                                        )
                                    },
                                },
                            }
                        )
                    return
        self.record(subtask, tool, status, data)

    def record_verified(
        self,
        subtask: int,
        tool: str,
        *,
        operation: str,
        scenario: str,
        assertions: Sequence[Assertion],
        cleanup: str,
        data: Any,
        surface: str = "mcp",
    ) -> None:
        """Record one scenario's asserted postconditions — the only promoting path."""
        if surface not in {"mcp", "cli"}:
            raise ValueError("verification surface must be mcp or cli")
        if not assertions:
            raise ValueError(
                "a verified observation must assert at least one condition"
            )
        if cleanup not in CLEANUP:
            raise ValueError(f"cleanup disposition is not a known value: {cleanup!r}")
        if not SCENARIO_ID.fullmatch(scenario):
            raise ValueError(f"scenario id is not a closed-shape token: {scenario!r}")
        passed = all(item.holds for item in assertions) and cleanup in {
            "passed",
            "not-required",
        }
        unmet = [item.id for item in assertions if not item.holds]
        note = "" if passed else "unmet: " + ", ".join(unmet or [f"cleanup {cleanup}"])
        self.record(
            subtask,
            tool,
            "PASS" if passed else "FAIL",
            data,
            note,
            result="passed" if passed else "failed",
        )
        recorded = {
            "operation": operation,
            # ``tested_commit``, ``closure_fingerprint``, ``hmc_release``,
            # ``hardware_family`` and ``schema_version`` are filled at emission;
            # ``channel`` is not derivable there, so it is written here.
            "observation": {
                "id": _observation_id(
                    subtask,
                    tool.lower().replace(" ", "-") if surface == "cli" else tool,
                ),
                "channel": "live",
                "result": "passed" if passed else "failed",
                "scenario": scenario,
                "observed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cleanup": cleanup,
                "assertions": [item.id for item in assertions if item.holds],
            },
        }
        if surface == "cli":
            self.results[-1]["cli_observation"] = recorded
        else:
            self.observations.append(recorded)


# ---------------------------------------------------------------------------
# ST0 — Capture baseline LPAR state
# ---------------------------------------------------------------------------


SUBTASKS = {
    0: capture_lpar_baseline,
    1: inventory_connectivity,
    2: inventory_network,
    3: inventory_storage,
    4: inventory_lpar_profiles,
    5: inspect_metrics_templates,
    6: inventory_users,
    7: exercise_cli_escape_hatch,
    8: exercise_lpar_lifecycle,
    9: mutate_virtual_networking,
    10: mutate_lpar_properties,
    11: exercise_users,
    12: inspect_metrics_jobs,
    13: validate_provisioning_dry_run,
    14: exercise_storage_provisioning,
    15: restore_lpar_baseline,
    16: vmedia_bootstrap_and_create_repo,
    17: vmedia_short_repo_lifecycle,
    18: vmedia_upload_iso,
    19: vmedia_mount_unmount,
    20: vmedia_boot_verification,
    21: vmedia_mapping_crossvalidation,
    22: vmedia_teardown,
    23: exercise_sriov_assignment,
    24: exercise_dedicated_pcie_assignment,
    25: exercise_bare_cec,
    37: exercise_vios_backup,
    38: exercise_pcm_preferences,
    39: exercise_lpar_config,
    40: exercise_disk_lifecycle,
    41: exercise_lpar_power,
    42: exercise_volume_group,
}
_SCENARIO_MODULES = frozenset(inspect.getmodule(task) for task in SUBTASKS.values())


def _validate_expected_dispatch(tool: str, expected: Sequence[ExpectedOutcome]) -> None:
    for outcome in expected:
        security = TOOL_SECURITY.get(tool)
        if security is None or outcome.operation != security.operation:
            raise ValueError(f"expected outcome operation does not match tool {tool}")


def _declared_names(
    node: ast.expr, declarations: Mapping[str, ExpectedOutcome]
) -> list[str]:
    if not isinstance(node, ast.List) or not all(
        isinstance(item, ast.Name) and item.id in declarations for item in node.elts
    ):
        raise ValueError(
            "expected outcomes must be a literal list of module declarations"
        )
    return [item.id for item in node.elts if isinstance(item, ast.Name)]


def _result_names(nodes: Sequence[ast.expr]) -> tuple[str, str]:
    if len(nodes) != 2 or not all(isinstance(node, ast.Name) for node in nodes):
        raise ValueError("declared results require a named status/data pair")
    names = tuple(node.id for node in nodes if isinstance(node, ast.Name))
    if names[0] == names[1]:
        raise ValueError("declared status and data names must differ")
    return names[0], names[1]


def _call_result_names(
    node: ast.Call, parents: Mapping[ast.AST, ast.AST]
) -> tuple[str, str]:
    awaiter = parents.get(node)
    assignment = parents.get(awaiter) if awaiter else None
    if (
        not isinstance(awaiter, ast.Await)
        or not isinstance(assignment, ast.Assign)
        or len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Tuple)
    ):
        raise ValueError("declared calls require an assigned awaited status/data pair")
    return _result_names(assignment.targets[0].elts)


def _validate_declared_function(
    function: ast.AsyncFunctionDef,
    declarations: Mapping[str, ExpectedOutcome],
) -> None:
    parents = {
        child: node
        for node in ast.walk(function)
        for child in ast.iter_child_nodes(node)
    }
    events = sorted(
        (
            node
            for node in ast.walk(function)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute))
            or (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    pending: dict[tuple[str, str], list[str]] = {}
    for node in events:
        if isinstance(node, ast.Name):
            if any(node.id in binding for binding in pending):
                raise ValueError(
                    f"unrecorded declared result overwritten in {function.name}"
                )
            continue
        assert isinstance(node.func, ast.Attribute)
        if node.func.attr == "record_with_expected":
            if len(node.args) != 5:
                raise ValueError(
                    "expected-outcome recording must use five positional arguments"
                )
            names = _declared_names(node.args[4], declarations)
            if pending.pop(_result_names(node.args[2:4]), None) != names:
                raise ValueError(
                    f"expected-outcome result pairing mismatch in {function.name}"
                )
        if node.func.attr != "call":
            continue
        expected = next(
            (kw.value for kw in node.keywords if kw.arg == "expected"), None
        )
        if expected is None:
            continue
        binding = _call_result_names(node, parents)
        names = _declared_names(expected, declarations)
        tool = node.args[1] if len(node.args) > 1 else None
        if not isinstance(tool, ast.Constant) or not isinstance(tool.value, str):
            raise ValueError(  # noqa: TRY004 - invalid scenario source is a startup configuration error
                "declared dispatch requires a literal tool name"
            )
        _validate_expected_dispatch(tool.value, [declarations[name] for name in names])
        pending[binding] = names
    if pending:
        raise ValueError(f"unrecorded declared results in {function.name}")


def _validate_declared_outcomes() -> None:
    """Validate every registered scenario before any client can reach hardware."""
    registered = {security.operation for security in TOOL_SECURITY.values()}
    for module in _SCENARIO_MODULES:
        if module is None:
            raise ValueError("cannot resolve a live scenario module")
        declarations = {
            name: value
            for name, value in vars(module).items()
            if isinstance(value, ExpectedOutcome)
        }
        if any(
            outcome.operation not in registered for outcome in declarations.values()
        ):
            raise ValueError(
                f"unregistered expected outcome operation in {module.__name__}"
            )
        for function in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(function, ast.AsyncFunctionDef):
                _validate_declared_function(function, declarations)


def _load_known_gaps(
    environment: tuple[str, str] | None,
    repo_root: Path,
    catalog_path: Path | None = None,
) -> set[tuple[str, str]]:
    document = check_capability_inventory.load_json(
        catalog_path or repo_root / "docs/capabilities/maturity.json"
    )
    errors: list[str] = []
    check_capability_inventory._exact_keys(
        document,
        {"format_version", "admission_policy", "operations"},
        "maturity.json",
        errors,
    )
    if (
        type(document.get("format_version")) is not int
        or document.get("format_version")
        != check_capability_inventory.MATURITY_FORMAT_VERSION
        or document.get("admission_policy") != "existing-runtime-guards"
    ):
        errors.append("maturity.json: invalid format version or admission policy")
    records = check_capability_inventory._objects(
        check_capability_inventory._array(document, "operations", errors),
        "maturity operations",
        errors,
    )
    handlers = {
        tool.operation: tool.handler
        for tool in check_capability_inventory.discover_registry()
    }
    check_capability_inventory._validate_maturity(records, set(handlers), errors)
    if errors:
        raise ValueError("invalid gap catalog: " + "; ".join(errors))
    known: set[tuple[str, str]] = set()
    if environment is not None:
        for record in records:
            implementation = cast(dict[str, Any], record["implementation"])
            operation = cast(str, record["operation"])
            for scope in implementation["missing_scope"]:
                if "confirmation" not in scope:
                    continue
                fingerprint = check_capability_inventory.closure_fingerprint(
                    repo_root, handlers[operation].rsplit(".", 1)[0]
                )
                if check_capability_inventory.gap_is_current(
                    scope["confirmation"], environment, fingerprint
                ):
                    known.add((operation, scope["variant"]))
    return known


# Release-selectable scenarios; numbers are dispatcher subtasks, not result row IDs.
RELEASE_SCENARIO_SUBTASKS = {
    "st1-system-inventory": 1,
    "st1-lpar-inventory": 1,
    "st29-dedicated-pcie": 24,
    "st29-pcie-inventory": 24,
    "st35-bare-cec": 25,
    "st36-io-slots": 24,
    "st41-lpar-power": 41,
}


SUBTASK_GROUPS: dict[str, list[int]] = {
    # ST11 creates an HMC user, so only its own arm dispatches it (#632).
    "round2": [n for n in range(16) if n != 11],
    "vmedia": list(range(16, 23)),
    "sriov": [23],
    "dedicated": [24],
    "bare-cec": [25],
    "profiles": [0, 4, 10, 15],
    "users": [11],
    # ST9 runs only here: its round trips need the operator's network authorization.
    "network": [2, 9],
    # Not in "all": the arm restores a VIOS's I/O configuration and needs its own
    # operator authorization (docs/live-testing.md).
    "vios-backup": [37],
    # Not in "all": the arm changes the managed system's PCM collection preferences,
    # which every PCM consumer of the system shares (docs/live-testing.md).
    "pcm": [38],
    # Not in "all": the arm creates, resizes, activates and deletes its own
    # partition (#1345).
    "lpar-config": [39],
    # ST40 mutates the VIOS's storage, so only its own arm dispatches it (#1348).
    "storage": [0, 3, 40, 42],
    # Not in "all": the arm creates, powers, provisions and deletes its own
    # partitions and a VIOS volume (#1346).
    "lpar-power": [41],
    "all": list(range(26)),
}


@dataclass(frozen=True)
class RunnerArguments:
    """Validated live-run selection and result destination."""

    subtask: int | None
    group: str | None
    results_path: str
    detach_probe: bool = False


def _parse_arguments(argv: list[str] | None = None) -> RunnerArguments:
    """Parse the live-run selection without performing configuration or HMC work."""
    # RawDescriptionHelpFormatter: the default re-wraps the docstring into one
    # paragraph, collapsing the usage line and the `--no-sync` requirement into
    # prose an operator skims past.
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "subtask",
        nargs="?",
        type=int,
        choices=sorted(SUBTASKS),
        help="run one numbered subtask",
    )
    selection.add_argument(
        "--group",
        choices=tuple(SUBTASK_GROUPS),
        help="run one named subtask group",
    )
    parser.add_argument(
        "--results-file",
        help="write results to this path instead of the selection-specific default",
    )
    parser.add_argument(
        "--detach-probe",
        action="store_true",
        help="run only the bounded ST41 mapping/RMC investigation",
    )
    parsed = parser.parse_args(argv)
    if parsed.detach_probe and parsed.group != "lpar-power":
        parser.error("--detach-probe requires --group lpar-power")
    results_path = parsed.results_file
    if results_path is None:
        results_path = (
            f"test-results-{parsed.group}.json"
            if parsed.group is not None
            else "test-results-round2.json"
        )
    return RunnerArguments(
        parsed.subtask, parsed.group, results_path, parsed.detach_probe
    )


def _run_from_arguments(argv: list[str] | None = None) -> int:
    """Validate arguments, then bootstrap configuration and execute the live run."""
    arguments = _parse_arguments(argv)
    try:
        config = LiveTestConfig.from_env_file()
        # Validated here, beside the rest of the configuration: `_emit_observations`
        # runs after a completed hardware run, and an uncaught ValueError there
        # would replace the run summary and failed-test listing with a traceback.
        environment = _read_environment()
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    # Both destinations are checked before the run, not after it. The results
    # document holds every tool response verbatim on the success path — the raw
    # HMC data `.gitignore` line 1 describes as internal hostnames, addresses and
    # serials — so a `--results-file` stem no pattern covers must cost a startup
    # exit, never a completed run against real hardware whose output then has
    # nowhere safe to land.
    unsafe = [
        str(path)
        for path in (
            Path(arguments.results_path),
            _observations_path(arguments.results_path),
        )
        if not _destination_is_ignored(path)
    ]
    if unsafe:
        print(
            "❌ git does not ignore " + ", ".join(unsafe) + " — choose a results "
            "path matching an ignored pattern (see .gitignore)"
        )
        return 1
    if not _bootstrap_config():
        return 1
    # `_bootstrap_config` reads `.env` only when the TOML profile fails, so a
    # resolved profile would otherwise leave every `.env`-only `HMC_*` value
    # unread — including the HMC_SCHEMA_VERSION the run header reports below.
    # Loading it here and not earlier keeps the profile's priority over `.env`.
    _load_dotenv()
    return asyncio.run(
        main(
            subtask_filter=arguments.subtask,
            results_path=arguments.results_path,
            group=arguments.group,
            detach_probe=arguments.detach_probe,
            config=config,
            environment=environment,
        )
    )


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


def _decode_saved_config(value: Any) -> LiveTestConfig:
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
    return LiveTestConfig(**parsed)


def _decode_artifacts(value: Any) -> LiveTestArtifacts:
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


def _hmc_identity(config: HMCConfig) -> dict[str, str | int | bool]:
    """Return the non-secret HMC identity bound to a persisted artifact set."""
    return {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "verify_ssl": config.verify_ssl,
    }


def _restore_artifacts_from_results(
    state: RunState,
    hmc_config: HMCConfig,
    results_path: str = "test-results-round2.json",
) -> bool:
    """Pre-seed artifacts from a compatible previous live-test report."""
    path = Path(results_path)
    try:
        if not path.exists():
            return False
        saved = json.loads(path.read_text())
        if not isinstance(saved, dict):
            raise TypeError("results document must be a JSON object")
        # `run` is provenance about the writing run, not state to restore, so it
        # is tolerated rather than required: a document written before the block
        # existed still resumes.
        if set(saved) - {"run"} != {"config", "hmc", "artifacts", "results"}:
            raise ValueError("results document has an unsupported shape")
        if _decode_saved_config(saved["config"]) != state.config:
            raise ValueError("results configuration does not match this run")
        saved_hmc = saved["hmc"]
        current_hmc = _hmc_identity(hmc_config)
        if not isinstance(saved_hmc, dict) or set(saved_hmc) != set(current_hmc):
            raise TypeError("results hmc identity must contain the expected fields")
        if any(
            type(saved_hmc[key]) is not type(value)
            for key, value in current_hmc.items()
        ):
            raise TypeError("results hmc identity has the wrong JSON type")
        if saved_hmc != current_hmc:
            raise ValueError("results HMC identity does not match this run")
        candidate = _decode_artifacts(saved["artifacts"])
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        KeyError,
    ) as exc:
        print(f"  ⚠️  Could not restore artifacts from {results_path}: {exc}")
        return False

    # Recovery witnesses belong to one invocation, never to restored context.
    candidate.lp3_baseline.pop("round2_snapshot", None)
    candidate.lp3_baseline.pop("round2_fixture", None)
    state.artifacts = candidate
    print(
        f"  ℹ  Artifacts restored from {results_path} "
        f"(vios_uuid={candidate.vios_uuid}, "
        f"system_uuid={candidate.system_uuid}, "
        f"vg_uuid={candidate.vg_uuid})"
    )
    return True


def _write_results(path: Path, document: str) -> None:
    """Atomically replace the persisted live-test report."""
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(document)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _read_environment(path: Path | None = None) -> tuple[str, str] | None:
    """Read the observation environment from `.env`: both keys, or neither."""
    path = path or _ENV_FILE
    values: dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key in ENVIRONMENT_KEYS and value:
                values[key] = value
    if not values:
        return None
    if len(values) != len(ENVIRONMENT_KEYS):
        raise ValueError(
            "invalid live-test configuration: "
            + " and ".join(ENVIRONMENT_KEYS)
            + " must be set together"
        )
    # Grammars applied here rather than at copy-in: these two strings are the only
    # free text an observation carries, and a hostname or a serial typed into
    # either would otherwise be written to disk and discovered only when a human
    # pastes it into `maturity.json`. The message never echoes the value.
    release, family = values[ENVIRONMENT_KEYS[0]], values[ENVIRONMENT_KEYS[1]]
    invalid = [
        f"{key} does not match its grammar (expected {form})"
        for key, value, pattern, form in (
            (
                ENVIRONMENT_KEYS[0],
                release,
                RUN_HMC_RELEASE,
                "V<n>R<n>M<n>, e.g. V10R3M1060",
            ),
            (
                ENVIRONMENT_KEYS[1],
                family,
                check_capability_inventory.HARDWARE_FAMILY,
                "POWER<n>, e.g. POWER10",
            ),
        )
        if not pattern.fullmatch(value)
    ]
    if invalid:
        raise ValueError("invalid live-test configuration: " + ", ".join(invalid))
    return release, family


def _git(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _repository_root() -> Path | None:
    """The repository the runner is checked out in, whatever directory it ran from.

    `Path.cwd()` would silently produce an empty import closure when the runner
    is invoked from a subdirectory, giving every observation the digest of no
    files and marking it stale forever.
    """
    result = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    root = Path(result.stdout.strip())
    return root if (root / "src" / "hmcpctl").is_dir() else None


def _run_provenance(
    tasks: Sequence[int],
    group: str | None,
    repo_root: Path | None,
    schema_version: str,
    partial: bool,
    *,
    detach_probe: bool = False,
) -> dict[str, Any]:
    """What this run was, so a matrix taken from it can be dated.

    Written unconditionally, unlike the sibling observations document, which is
    skipped when nothing resolves or the runner is outside the repository — the
    run that fails early is the one whose provenance matters most. Outside a
    repository the commit is `None`, which is a reportable state; a missing
    block is not.

    `tree_clean` qualifies `commit`: with `src` or `scripts` dirty the sha names
    a tree that was not the one exercised, so evidence cannot cite it.

    `schema_version` is the resolved `HMC_SCHEMA_VERSION` (`(not set)` when
    unset), the same string the run header prints, so a matrix can say which
    request environment produced it.

    `partial` is true when an exception or interrupt ended the subtask loop:
    `subtasks` then names what was selected, not what ran, and only the rows
    recorded before the stop are present.
    """
    commit: str | None = None
    tree_clean: bool | None = None
    if repo_root is not None:
        head = _git(repo_root, "rev-parse", "HEAD")
        if head.returncode == 0:
            commit = head.stdout.strip()
        tree_clean = _tree_is_clean(repo_root)
    provenance = {
        "tested_commit": commit,
        "tree_clean": tree_clean,
        "group": group,
        "subtasks": list(tasks),
        "schema_version": schema_version,
        "finished": datetime.now(UTC).isoformat(),
        "partial": partial,
    }

    if detach_probe:
        provenance["detach_probe"] = True
    return provenance


def _destination_is_ignored(path: Path, repo_root: Path | None = None) -> bool:
    """Report whether Git would let *path* be committed from where it is written.

    Exit 0 means ignored, 1 means the path sits in a repository unignored, and
    anything else means no repository, or a path outside one — in which case
    there is nothing to commit into and the write is safe.
    """
    return (
        _git(repo_root or Path.cwd(), "check-ignore", "-q", str(path)).returncode != 1
    )


def _observations_path(results_path: str) -> Path:
    """The observations file written beside a run's results document."""
    path = Path(results_path)
    return path.with_name(f"{path.stem}-observations.json")


def _tree_is_clean(repo_root: Path) -> bool:
    """Report whether the implementation the observation names is what is committed."""
    result = _git(repo_root, "status", "--porcelain", "--", "src", "scripts")
    return result.returncode == 0 and not result.stdout.strip()


def _emit_observations(
    state: RunState,
    path: Path,
    environment: tuple[str, str] | None,
    schema_version: str,
    repo_root: Path,
) -> bool:
    """Write the run's catalog-shaped observations, or say why it wrote none."""
    if environment is None:
        print("no LIVE_TEST_ENV_* settings — observations not written")
        return False
    if not check_capability_inventory.SCHEMA_VERSION.fullmatch(schema_version):
        print(
            "HMC_SCHEMA_VERSION is not V<n>_<n>[_<n>...] or unset — observations not written"
        )
        return False
    if not state.observations and not state.gaps:
        print("no verified observations or confirmed gaps — nothing to write")
        return False
    if not _tree_is_clean(repo_root):
        print("src/ or scripts/ is modified — observations not written")
        return False
    # `--results-file` lets an operator name any stem, so no fixed `.gitignore`
    # pattern can establish that the destination is ignored; ask Git instead.
    # `_run_from_arguments` checks the same thing before the run; this covers a
    # direct call to `main`.
    if not _destination_is_ignored(path, repo_root):
        print(f"{path} is not ignored by git — observations not written")
        return False
    head = _git(repo_root, "rev-parse", "HEAD")
    if head.returncode != 0:
        print("cannot resolve HEAD — observations not written")
        return False
    handlers = {
        tool.operation: tool.handler
        for tool in check_capability_inventory.discover_registry()
    }
    document: list[dict[str, Any]] = []
    seen: set[str] = set()
    for recorded in state.observations:
        handler = handlers.get(recorded["operation"])
        if handler is None:
            # Skip the one unresolvable row rather than discarding the document:
            # every other observation came from the same expensive hardware run,
            # and `test_verified_scenarios_name_registered_operations` is what
            # catches a mistyped operation before it ever reaches a live run.
            print(
                f"  ⚠️  unknown operation {recorded['operation']} — observation skipped"
            )
            continue
        observation = dict(recorded["observation"])
        if observation["id"] in seen:
            print(
                f"duplicate observation id {observation['id']} — observations not written"
            )
            return False
        seen.add(observation["id"])
        observation["tested_commit"] = head.stdout.strip()
        observation["hmc_release"], observation["hardware_family"] = environment
        observation["schema_version"] = schema_version
        observation["closure_fingerprint"] = (
            check_capability_inventory.closure_fingerprint(
                repo_root, handler.rsplit(".", 1)[0]
            )
        )
        document.append(
            {"operation": recorded["operation"], "observation": observation}
        )
    for recorded in state.gaps:
        handler = handlers.get(recorded["operation"])
        if handler is None:
            print(f"unknown operation {recorded['operation']} — gap skipped")
            continue
        scope = dict(recorded["missing_scope"])
        scope["confirmation"] = {
            **scope["confirmation"],
            "tested_commit": head.stdout.strip(),
            "hmc_release": environment[0],
            "hardware_family": environment[1],
            "closure_fingerprint": check_capability_inventory.closure_fingerprint(
                repo_root, handler.rsplit(".", 1)[0]
            ),
        }
        document.append({"operation": recorded["operation"], "missing_scope": scope})
    if not document:
        print("no resolvable observations — nothing written")
        return False
    _write_results(path, json.dumps(document, indent=2))
    print(f"Observations written to {path}")
    return True


@asynccontextmanager
async def served_client() -> AsyncGenerator[Client]:
    """A client connected to the application exactly as a live run composes it.

    The one owner of that composition: every live-test entry point that reads the
    served schemas goes through here, rather than composing its own application.
    """
    # The escape hatch is opted in because this harness drives `hmc_run_command`
    # against a real HMC. `permits` and `authorize` come from the policy just composed:
    # calling the toggle with neither would register the tool whatever the policy says
    # and leave its handler unwrapped, so the live run would stop being evidence about
    # the path an operator actually takes.
    policy = compile_legacy_policy(
        TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,), include_arbitrary_command=True
    )
    mcp = create_mcp(policy)
    permits, authorize = _gates(policy)
    await configure_arbitrary_command_tool(
        True, mcp, permits=permits, authorize=authorize
    )
    async with Client(mcp) as client:
        yield client


async def served_schemas(client: Client) -> dict[str, dict[str, Any]]:
    """The input schema *client* is served for each tool, by tool name."""
    return {tool.name: tool.input_schema for tool in await client.list_tools()}


async def main(
    subtask_filter: int | None = None,
    results_path: str = "test-results-round2.json",
    group: str | None = None,
    config: LiveTestConfig | None = None,
    hmc_config: HMCConfig | None = None,
    environment: tuple[str, str] | None = None,
    detach_probe: bool = False,
) -> int:
    if detach_probe and (group != "lpar-power" or subtask_filter is not None):
        print("--detach-probe requires --group lpar-power without a subtask")
        return 1
    if config is None:
        try:
            config = LiveTestConfig.from_env_file()
        except ValueError as exc:
            print(f"❌ {_redact_failure_text(str(exc))}")
            return 1
    try:
        _validate_declared_outcomes()
        repo_root = _repository_root()
        known_gaps = _load_known_gaps(environment, repo_root) if repo_root else set()
    except (OSError, ValueError) as exc:
        print(f"❌ {_redact_failure_text(str(exc))}")
        return 1
    state = RunState(config=config, known_gaps=known_gaps, group=group)
    hmc_config = hmc_config or HMCConfig()
    state.hmc_config = hmc_config
    print(f"Starting live integration tests at {datetime.now(UTC).isoformat()}")
    schema_version = env_var_value("HMC_SCHEMA_VERSION") or "(not set)"
    print(f"HMC_SCHEMA_VERSION={schema_version}")
    if (
        environment is not None
        and not check_capability_inventory.SCHEMA_VERSION.fullmatch(schema_version)
    ):
        # A warning, not an exit: a run starts whatever the variable holds (#875),
        # and preflight predicts exactly that. Said here so the operator can stop
        # before the hardware run rather than learn it from the emission step.
        print(
            "⚠️  HMC_SCHEMA_VERSION is not V<n>_<n>[_<n>...] or unset — this run's "
            "observations will not be written"
        )

    # Determine which sub-tasks to run
    if subtask_filter is not None:
        tasks = [subtask_filter]
    elif group is not None:
        tasks = SUBTASK_GROUPS.get(group, [])
        if not tasks:
            print(f"Unknown group {group!r}. Valid groups: {', '.join(SUBTASK_GROUPS)}")
            return 1
    else:
        tasks = sorted(SUBTASKS.keys())

    # Every invocation preserves its destination; only subsets borrow another arm's context.
    prior_paths = [results_path]
    if subtask_filter is not None or group is not None:
        prior_paths.extend(["test-results-vmedia.json", "test-results-round2.json"])
    for prior in prior_paths:
        if Path(prior).exists():
            restored = _restore_artifacts_from_results(state, hmc_config, prior)
            if prior == results_path and not restored:
                print(
                    "❌ Existing results cannot be restored; use another --results-file."
                )
                return 1
            break

    def write_results(partial: bool) -> None:
        _write_results(
            Path(results_path),
            json.dumps(
                {
                    "run": _run_provenance(
                        tasks,
                        group,
                        repo_root,
                        schema_version,
                        partial,
                        detach_probe=detach_probe,
                    ),
                    "config": asdict(state.config),
                    "hmc": _hmc_identity(hmc_config),
                    "artifacts": asdict(state.artifacts),
                    "results": state.results,
                },
                indent=2,
                default=str,
            ),
        )

    try:
        async with served_client() as client:
            state.schemas = await served_schemas(client)
            for n in tasks:
                fn = SUBTASKS.get(n)
                if fn:
                    if detach_probe and n == 41:
                        await exercise_detach_probe(client, state)
                    else:
                        await fn(client, state)
                else:
                    state.record(n, "runner", "FAIL", f"Unknown sub-task {n}")
    except BaseException:
        # An interrupted run is the one whose operator needs the ST0 baseline and
        # the run marker to restore by hand, so persist what was gathered before
        # re-raising. A failed write must not replace the run's own failure.
        try:
            write_results(partial=True)
            print(f"Partial results written to {results_path}")
        except (OSError, TypeError, ValueError) as exc:
            print(
                f"⚠️  Could not write partial results: {_redact_failure_text(str(exc))}"
            )
        raise
    finally:
        state.iso_http_server.close()

    write_results(partial=False)

    if repo_root is None:
        print("not inside the hmcpctl repository — observations not written")
    else:
        _emit_observations(
            state,
            _observations_path(results_path),
            environment,
            schema_version,
            repo_root,
        )

    total = len(state.results)
    passed = sum(1 for r in state.results if r["status"] == "PASS")
    failed = sum(1 for r in state.results if r["status"] == "FAIL")
    skipped = sum(1 for r in state.results if r["status"] == "SKIP")
    print(f"\n{'=' * 60}")
    print(f"TOTAL: {total}  ✅ PASS: {passed}  ❌ FAIL: {failed}  ⚠️  SKIP: {skipped}")
    print(f"Results written to {results_path}")

    if failed:
        print("\nFailed tests:")
        for r in state.results:
            if r["status"] == "FAIL":
                print(f"  ST{r['subtask']} {r['tool']}")
                print(f"    {str(_redact_failure_data(r['data']))[:200]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_from_arguments())
