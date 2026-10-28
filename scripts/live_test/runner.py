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

Registered subtasks span 0 through 67; 26 to 36 are other arms' row ids.
ST53–ST59, ST61, ST64, ST65 and ST67 require named groups and are excluded from default and all selections.
Subtasks 9, 11, 37, 38, 39, 40, 41, 42, 45 and 46 SKIP outside their
own `network`, `users`, `vios-backup`, `pcm`, `lpar-config`, `storage` and
`lpar-power`, `partition-profile-contract` and `partition-profiles` groups. A bare
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
import asyncio
import inspect
import json
import os
import re
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import check_capability_inventory
from fastmcp import Client

from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.config import HMCConfig, env_var_value
from hmcpctl.server import TOOL_SECURITY, _gates, create_mcp
from hmcpctl.server_tools.command import configure_arbitrary_command_tool
from live_test import config as live_config
from live_test import evidence, scenario_validation
from live_test.accelerator_qos import exercise_accelerator_qos
from live_test.bare_cec import exercise_bare_cec
from live_test.config import (
    LiveTestArtifacts,
    LiveTestConfig,
    decode_artifacts,
    decode_saved_config,
)
from live_test.connectivity import inventory_connectivity
from live_test.creation_memory import exercise_creation_memory
from live_test.creation_memory_layout import exercise_creation_memory_layout
from live_test.dedicated_pcie import exercise_dedicated_pcie_assignment
from live_test.detach_probe import exercise_detach_probe
from live_test.escape_hatch import exercise_cli_escape_hatch
from live_test.inventory import capture_lpar_baseline
from live_test.lpar import (
    exercise_lpar_lifecycle,
    mutate_lpar_properties,
    restore_lpar_baseline,
)
from live_test.lpar_affinity_group import exercise_lpar_affinity_group
from live_test.lpar_config import (
    exercise_lpar_config,
    exercise_lpar_reporting,
    exercise_lpar_settings,
)
from live_test.lpar_hardware_memory import exercise_lpar_hardware_memory
from live_test.lpar_linux_roles import exercise_lpar_linux_roles
from live_test.lpar_physical_io import exercise_lpar_physical_io
from live_test.lpar_power import exercise_lpar_power
from live_test.lpar_security_policy import exercise_lpar_security_policy
from live_test.lpar_vtpm import exercise_lpar_vtpm
from live_test.memory_move import exercise_memory_move
from live_test.memory_quantities import exercise_memory_quantities
from live_test.memory_statistics import exercise_memory_statistics, private_destination
from live_test.metrics import (
    exercise_pcm_preferences,
    inspect_metrics_jobs,
    inspect_metrics_templates,
)
from live_test.network import inventory_network, mutate_virtual_networking
from live_test.observation import (
    SCENARIO_ID,
    Assertion,
    CallFailure,
    ExpectedOutcome,
    KnownGap,
    classify_failure,
    grade_assertions,
)
from live_test.partition_profile_contract import exercise as exercise_profile_contract
from live_test.partition_profiles import exercise_partition_profiles
from live_test.processor_move import exercise_processor_move
from live_test.processor_quantities import exercise_processor_quantities
from live_test.processor_sharing_weight import exercise_processor_sharing_weight
from live_test.profiles import exercise_backup_artifacts, inventory_lpar_profiles
from live_test.provisioning import (
    exercise_storage_provisioning,
    validate_provisioning_dry_run,
)
from live_test.sriov import exercise_sriov_assignment
from live_test.storage import inventory_storage
from live_test.storage_lifecycle import exercise_disk_lifecycle
from live_test.storage_volume_group import exercise_volume_group
from live_test.system_profiles import exercise_system_profiles
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

# ---------------------------------------------------------------------------
# Pre-run guard: HMC credentials must resolve before the first dispatch
# ---------------------------------------------------------------------------


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
    if not live_config.ENV_FILE.exists():
        return
    for line in live_config.ENV_FILE.read_text().splitlines():
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
    checkpoint: Callable[[], None] | None = None
    creation_memory_invocation: str | None = None
    creation_memory_layout_invocation: str | None = None

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
        scenario_validation.validate_expected_dispatch(tool, expected)
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
        status, note = grade_assertions(assertions, cleanup)
        if not SCENARIO_ID.fullmatch(scenario):
            raise ValueError(f"scenario id is not a closed-shape token: {scenario!r}")
        passed = status == "PASS"
        self.record(
            subtask,
            tool,
            status,
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
    43: exercise_backup_artifacts,
    44: exercise_system_profiles,
    45: exercise_profile_contract,
    46: exercise_partition_profiles,
    47: exercise_lpar_settings,
    48: exercise_lpar_reporting,
    49: exercise_lpar_physical_io,
    50: exercise_memory_quantities,
    51: exercise_lpar_vtpm,
    52: exercise_lpar_security_policy,
    53: exercise_lpar_linux_roles,
    54: exercise_memory_move,
    67: exercise_memory_statistics,
    61: exercise_processor_move,
    55: exercise_lpar_hardware_memory,
    56: exercise_processor_quantities,
    57: exercise_lpar_affinity_group,
    65: exercise_creation_memory,
    58: exercise_creation_memory_layout,
    59: exercise_processor_sharing_weight,
    64: exercise_accelerator_qos,
}
_SCENARIO_MODULES = frozenset(inspect.getmodule(task) for task in SUBTASKS.values())


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
    "profile-artifacts": [43],
    "system-profiles": [44],
    "partition-profiles": [46],
    "partition-profile-contract": [45],
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
    "lpar-settings": [47],
    "lpar-reporting": [48],
    "lpar-physical-io": [49],
    "lpar-memory-quantities": [50],
    "processor-sharing-weight": [59],
    "lpar-memory-move": [54],
    "lpar-memory-statistics": [67],
    "lpar-accelerator-qos": [64],
    "lpar-processor-move": [61],
    "lpar-processor-quantities": [56],
    "lpar-vtpm": [51],
    "lpar-security-policy": [52],
    "lpar-hardware-memory": [55],
    "creation-memory": [65],
    "lpar-affinity-group": [57],
    "creation-memory-layout": [58],
    "lpar-linux-roles": [53],
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
    if arguments.group == "lpar-memory-statistics" or arguments.subtask == 67:
        try:
            if arguments.group != "lpar-memory-statistics":
                raise ValueError("Statistics reset requires its named opt-in group")
            private_destination(arguments.results_path)
        except (ValueError, OSError, RuntimeError):
            print(
                "Statistics reset requires new canonical absolute private results outside repositories"
            )
            return 1
    try:
        config = LiveTestConfig.from_env_file()
        # Validated here, beside the rest of the configuration: `evidence.emit_observations`
        # runs after a completed hardware run, and an uncaught ValueError there
        # would replace the run summary and failed-test listing with a traceback.
        environment = evidence.read_environment()
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
            evidence.observations_path(arguments.results_path),
        )
        if not evidence.destination_is_ignored(path)
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
        saved_run, saved_artifacts = saved.get("run", {}), saved.get("artifacts")
        if not isinstance(saved_run, dict) or not isinstance(saved_artifacts, dict):
            raise TypeError("results run/artifacts must be JSON objects")
        saved_tasks = saved_run.get("subtasks", [])
        if not isinstance(saved_tasks, list):
            raise TypeError("results subtasks must be an array")
        if (
            state.group == "creation-memory"
            or saved_run.get("group") == "creation-memory"
            or 65 in saved_tasks
            or saved_artifacts.get("creation_memory")
        ):
            raise ValueError("creation-memory context cannot be restored")
        if (
            state.group == "creation-memory-layout"
            or saved_run.get("group") == "creation-memory-layout"
            or 58 in saved_tasks
            or saved_artifacts.get("creation_memory_layout")
        ):
            raise ValueError("creation-memory-layout context cannot be restored")
        # `run` is provenance about the writing run, not state to restore, so it
        # is tolerated rather than required: a document written before the block
        # existed still resumes.
        if set(saved) - {"run"} != {"config", "hmc", "artifacts", "results"}:
            raise ValueError("results document has an unsupported shape")
        if decode_saved_config(saved["config"]) != state.config:
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
        candidate = decode_artifacts(saved["artifacts"])
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
        scenario_validation.validate_declared_outcomes(_SCENARIO_MODULES)
        repo_root = evidence.repository_root()
        known_gaps = (
            evidence.load_known_gaps(environment, repo_root) if repo_root else set()
        )
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
        tasks = sorted(
            n
            for n in SUBTASKS
            if n not in {44, 45, 46, 52, 53, 54, 55, 56, 57, 58, 59, 61, 64, 65, 67}
        )

    # Every invocation preserves its destination; only subsets borrow another arm's context.
    prior_paths = [results_path]
    if 65 in tasks:
        if (
            state.artifacts.creation_memory
            or state.creation_memory_invocation is not None
            or Path(results_path).exists()
            or evidence.observations_path(results_path).exists()
        ):
            print("Existing ST65 context refused; use fresh private results")
            return 1
        prior_paths = []
    elif 58 in tasks:
        if state.artifacts.creation_memory_layout or Path(results_path).exists():
            print("Existing ST58 context refused; use fresh private results")
            return 1
        prior_paths = []
    elif subtask_filter is not None or group is not None:
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
        evidence.write_results(
            Path(results_path),
            json.dumps(
                {
                    "run": evidence.run_provenance(
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

    state.checkpoint = lambda: write_results(partial=True)

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
        evidence.emit_observations(
            state,
            evidence.observations_path(results_path),
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
