"""Read-only capture sweep of one HMC (issue #1202). Never run by CI or any recipe.

    python scripts/live_capture_sweep.py --out <private-dir> \\
        [--profile P] [--system S] [--lpar L] [--vios V]

Calls every MCP tool whose ``readOnlyHint`` is true, then a declared list of raw
REST GETs and ``ls*`` commands no tool issues, recording everything through
``scripts/live_test/capture.py`` in raw mode (answers unredacted; logons, requests
and session values still redacted). A guard below the harness refuses, before
transport, any REST call that is not a GET (logon and logoff excepted) and any CLI
command that is not ``ls*``; the harness records the refusal.

Writes ``sweep.capture.jsonl`` (REST and SSH records) and ``tools.capture.jsonl``
(one record per tool call or skip) into ``--out``, created ``0700``, which must lie
outside every git work tree. The output is
raw and private: tokenize it with ``scripts/live_capture_export.py`` and never
commit or paste it. Procedure: docs/live-testing.md, "Capturing an HMC's vocabulary".
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import json
import os
import re
import shlex
import sys
from collections.abc import Awaitable, Callable, Generator, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import quote, urlsplit

import asyncssh

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_test.capture import capture, inside_work_tree

from hmcpctl.client.core import HMCClient

LOGON_PATH = "/rest/api/web/Logon"
_SHELL_META = re.compile(r"[;|&<>`$\n\r\\]")
UOM = "application/vnd.ibm.powervm.uom+xml"
WEB = "application/vnd.ibm.powervm.web+xml"


_Command = TypeVar("_Command", bound=Callable[..., Awaitable[Any]])


class ReadOnlyViolation(RuntimeError):
    """The sweep tried a call that could change the HMC; it was never sent."""


def rest_allowed(method: str, path: str) -> bool:
    verb = method.upper()
    if verb == "GET":
        return True
    return verb in ("PUT", "DELETE") and urlsplit(path).path == LOGON_PATH


def command_allowed(command: object) -> bool:
    text = command if isinstance(command, str) else ""
    words = text.split()
    return bool(words) and words[0].startswith("ls") and not _SHELL_META.search(text)


@contextlib.contextmanager
def read_only_guard() -> Generator[None]:
    """Refuse mutating calls at the transport seams for the life of the block.

    Enter it before ``capture()``: the harness then wraps the guard and records each
    refusal, and the guard still sits between every caller and the network.
    """
    request = HMCClient._request
    run = asyncssh.SSHClientConnection.run
    create_process = asyncssh.SSHClientConnection.create_process

    async def guarded_request(self: Any, method: str, path: str, **kwargs: Any) -> Any:
        if not rest_allowed(method, path):
            raise ReadOnlyViolation(f"refused {method} {path}: the sweep only reads")
        return await request(self, method, path, **kwargs)

    def guard_command(original: _Command) -> _Command:
        async def guarded(conn: Any, *args: Any, **kwargs: Any) -> Any:
            command = args[0] if args else kwargs.get("command")
            if not command_allowed(command):
                raise ReadOnlyViolation(
                    f"refused command {command!r}: the sweep runs only ls*"
                )
            return await original(conn, *args, **kwargs)

        return cast(_Command, guarded)

    HMCClient._request = guarded_request  # type: ignore[method-assign]
    asyncssh.SSHClientConnection.run = guard_command(run)  # type: ignore[method-assign]
    asyncssh.SSHClientConnection.create_process = guard_command(create_process)  # type: ignore[method-assign]
    try:
        yield
    finally:
        HMCClient._request = request  # type: ignore[method-assign]
        asyncssh.SSHClientConnection.run = run  # type: ignore[method-assign]
        asyncssh.SSHClientConnection.create_process = create_process  # type: ignore[method-assign]


# --- tool plan -------------------------------------------------------------------


@dataclass
class Context:
    """What discovery found; each field feeds the tool parameter of the same meaning."""

    system: str | None = None
    lpar: str | None = None
    vios: str | None = None
    console: str | None = None
    vg: str | None = None
    ssp: str | None = None
    template: str | None = None
    user: str | None = None
    adapter: str | None = None
    start_ts: str = field(
        default_factory=lambda: (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


#: Tool parameter -> the context field or constant that supplies it.
RESOLVERS: dict[str, Callable[[Context], Any]] = {
    "system_name_or_uuid": lambda c: c.system,
    "lpar_name_or_uuid": lambda c: c.lpar,
    "vios_name_or_uuid": lambda c: c.vios,
    "console_uuid": lambda c: c.console,
    "vg_uuid": lambda c: c.vg,
    "ssp_uuid": lambda c: c.ssp,
    "template_uuid": lambda c: c.template,
    "user_profile_uuid": lambda c: c.user,
    "adapter_id": lambda c: c.adapter,
    "start_ts": lambda c: c.start_ts,
    "desired_memory_mib": lambda c: 1024,
    "profile_name": lambda c: "default_profile",
}
_RESOURCE_TYPES = (
    "ManagementConsole",
    "ManagedSystem",
    "LogicalPartition",
    "VirtualIOServer",
    "Cluster",
    "SharedStoragePool",
)
#: Tools swept more than once: each entry overrides or adds arguments. A listing's
#: system-scoped form comes first, so discovery picks a partition on that system.
VARIANTS: dict[str, Callable[[Context], list[dict[str, Any]]]] = {
    "hmc_list_systems": lambda c: [{}, {"state": "operating"}],
    "hmc_list_lpars": lambda c: [
        {"system_name_or_uuid": c.system},
        {},
        {"state": "not activated"},
    ],
    "hmc_list_vios": lambda c: [
        {"system_name_or_uuid": c.system},
        {},
        {"state": "running"},
    ],
    "hmc_get_lpar_state": lambda c: [
        {"lpar_name_or_uuid": c.lpar},
        {"lpar_name_or_uuid": c.vios},
    ],
    "hmc_list_resources": lambda c: [{"resource_type": t} for t in _RESOURCE_TYPES],
}


def _pcm_categories(c: Context) -> list[dict[str, Any]]:
    """The variants of every tool that takes a PCM `category`."""
    return [
        {"category": "ManagedSystem", "resource_name_or_uuid": c.system},
        {"category": "LogicalPartition", "resource_name_or_uuid": c.lpar},
    ]


#: Discovery runs first, in this order, and fills the context for every later call.
DISCOVERY = (
    "hmc_list_systems",
    "hmc_list_lpars",
    "hmc_list_vios",
    "hmc_get_console_info",
    "hmc_list_volume_groups",
    "hmc_list_shared_storage_pools",
    "hmc_list_partition_templates",
    "hmc_list_users",
    "hmc_list_sriov_adapters",
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    required: tuple[str, ...]
    properties: tuple[str, ...]


def plan_calls(
    spec: ToolSpec, context: Context
) -> tuple[list[dict[str, Any]], list[str]]:
    """The argument sets to call *spec* with, and the required parameters nobody supplies.

    Required parameters come from the context; an optional one only when it is
    `adapter_id`, so a listing tool also runs unfiltered. A variant's own value
    wins, and a variant naming an undiscovered value is dropped.
    """
    if spec.name in VARIANTS:
        variants = VARIANTS[spec.name](context)
    elif "category" in spec.properties:
        variants = _pcm_categories(context)
    else:
        variants = [{}]
    wanted = [p for p in spec.properties if p in spec.required or p == "adapter_id"]
    calls: list[dict[str, Any]] = []
    missing: set[str] = set()
    for variant in variants:
        args = {p: RESOLVERS[p](context) for p in wanted if p in RESOLVERS}
        args.update(variant)
        if "category" in variant and "system_name_or_uuid" in spec.properties:
            args.setdefault("system_name_or_uuid", context.system)
        absent = [p for p in spec.required if args.get(p) is None]
        absent += [k for k, v in variant.items() if v is None]
        if absent:
            missing.update(absent)
            continue
        args = {k: v for k, v in args.items() if v is not None}
        if args not in calls:
            calls.append(args)
    return calls, sorted(missing)


# --- discovery ---------------------------------------------------------------------


def _leaf(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("#text", value.get("text", ""))
    return str(value or "")


def _rows(data: Any) -> list[dict[str, Any]]:
    # A dataclass tool result (ADR 0197's listing) arrives as a generated model.
    if hasattr(data, "entries"):
        data = data.entries
    elif isinstance(data, dict):
        data = data["entries"] if "entries" in data else data.get("items", [data])
    return (
        [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    )


def first_id(
    data: Any, *, state_key: str | None = None, state: str | None = None
) -> str | None:
    """The first row's UUID (or adapter id), preferring rows in *state* when given."""
    rows = _rows(data)
    if state_key:
        preferred = [
            r
            for r in rows
            if _leaf((r.get("Resource") or {}).get(state_key)).lower() == state
        ]
        rows = preferred or rows
    for row in rows:
        for key in ("UUID", "uuid", "adapter_id"):
            value = row.get(key)
            if value not in (None, "", "null", "unavailable"):
                return str(value)
    return None


def discover(context: Context, tool: str, data: Any) -> None:
    """Fill the context field *tool*'s answer supplies, unless the operator set it."""
    found = {
        "hmc_list_systems": (
            "system",
            first_id(data, state_key="State", state="operating"),
        ),
        "hmc_list_lpars": (
            "lpar",
            first_id(data, state_key="PartitionState", state="running"),
        ),
        "hmc_list_vios": ("vios", first_id(data)),
        "hmc_get_console_info": ("console", first_id(data)),
        "hmc_list_volume_groups": ("vg", first_id(data)),
        "hmc_list_shared_storage_pools": ("ssp", first_id(data)),
        "hmc_list_partition_templates": ("template", first_id(data)),
        "hmc_list_users": ("user", first_id(data)),
        "hmc_list_sriov_adapters": ("adapter", first_id(data)),
    }.get(tool)
    if found and found[1] and getattr(context, found[0]) is None:
        setattr(context, found[0], found[1])


# --- output ---------------------------------------------------------------------


class ToolLog:
    """Appends unredacted tool records to a private file outside every repository."""

    def __init__(self, path: Path) -> None:
        if inside_work_tree(path):
            raise ValueError(
                f"tool log {path} is inside a git work tree; write the sweep to a "
                "private directory outside every repository"
            )
        self._fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        os.fchmod(self._fd, 0o600)

    def write(self, record: dict[str, Any]) -> None:
        os.write(self._fd, (json.dumps(record, default=_jsonable) + "\n").encode())

    def close(self) -> None:
        os.close(self._fd)


def _jsonable(value: Any) -> Any:
    """A dataclass result (FastMCP's generated model) as its fields, else its text."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return str(value)


def prepare_output(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


# --- driving -------------------------------------------------------------------------


def tool_specs(tools: Sequence[Any]) -> list[ToolSpec]:
    """The read-only tools among *tools*, as plannable specs."""
    specs = []
    for tool in tools:
        annotations = tool.annotations
        if annotations is None or not annotations.read_only_hint:
            continue
        schema = tool.input_schema or {}
        specs.append(
            ToolSpec(
                tool.name,
                tuple(schema.get("required", ())),
                tuple(schema.get("properties", {})),
            )
        )
    order = {name: index for index, name in enumerate(DISCOVERY)}
    return sorted(specs, key=lambda s: (order.get(s.name, len(order)), s.name))


async def sweep_tools(
    client: Any, context: Context, log: ToolLog, step: Callable[[str], None]
) -> tuple[int, int]:
    """Call every read-only tool once per planned argument set; returns (calls, skips)."""
    calls = skips = 0
    for spec in tool_specs(await client.list_tools()):
        planned, missing = plan_calls(spec, context)
        if missing:
            log.write(
                {
                    "kind": "skip",
                    "step": spec.name,
                    "tool": spec.name,
                    "missing": missing,
                }
            )
            skips += 1
        for args in planned:
            step(spec.name)
            calls += 1
            record: dict[str, Any] = {
                "kind": "tool",
                "step": spec.name,
                "tool": spec.name,
            }
            record["args"] = args
            try:
                result = await client.call_tool(spec.name, args)
            except Exception as exc:  # noqa: BLE001 - a failing tool is evidence, not a stop
                log.write(
                    {**record, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            data = result.data if result.data is not None else result.structured_content
            log.write({**record, "ok": True, "data": data})
            discover(context, spec.name, data)
    return calls, skips


@dataclass(frozen=True)
class Names:
    """Values a raw probe template may name, by placeholder."""

    sys_uuid: str | None = None
    system_name: str | None = None
    lpar_uuid: str | None = None
    lpar_name: str | None = None
    vios_uuid: str | None = None
    vios_name: str | None = None
    console_uuid: str | None = None

    def fill(self, template: str, *, shell: bool) -> str | None:
        """*template* with its placeholders filled, shell- or URL-quoted; None if unknown."""
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        if any(values.get(n) is None for n in re.findall(r"\{(\w+)\}", template)):
            return None
        escape = shlex.quote if shell else (lambda v: quote(v, safe=""))
        return template.format(**{n: escape(str(v)) for n, v in values.items() if v})


#: (step, path, Accept) for reads no tool issues, ported from the 2026-09-30 sweep.
RAW_GETS: tuple[tuple[str, str, str], ...] = (
    ("xsd-enumerations", "/rest/api/web/schema/inc/Enumerations.xsd", "*/*"),
    ("xsd-web-lpar", "/rest/api/web/schema/LogicalPartition.xsd", "*/*"),
    ("ms-feed", "/rest/api/uom/ManagedSystem", f"{UOM}; type=ManagedSystem"),
    ("ms-quick-all", "/rest/api/uom/ManagedSystem/quick/All", "*/*"),
    ("ms-search-anchor", "/rest/api/uom/ManagedSystem/search", "*/*"),
    ("lpar-feed", "/rest/api/uom/LogicalPartition", f"{UOM}; type=LogicalPartition"),
    ("lpar-quick-anchor", "/rest/api/uom/LogicalPartition/quick", "*/*"),
    (
        "lpar-search-state",
        "/rest/api/uom/LogicalPartition/search/(PartitionState==not%20activated)",
        f"{UOM}; type=LogicalPartition",
    ),
    ("lpar-quick-all", "/rest/api/uom/LogicalPartition/{lpar_uuid}/quick/All", "*/*"),
    (
        "lpar-profiles",
        "/rest/api/uom/LogicalPartition/{lpar_uuid}/LogicalPartitionProfile",
        f"{UOM}; type=LogicalPartitionProfile",
    ),
    (
        "lpar-cna",
        "/rest/api/uom/LogicalPartition/{lpar_uuid}/ClientNetworkAdapter",
        f"{UOM}; type=ClientNetworkAdapter",
    ),
    ("vios-feed", "/rest/api/uom/VirtualIOServer", f"{UOM}; type=VirtualIOServer"),
    (
        "vios-search-state",
        "/rest/api/uom/VirtualIOServer/search/(PartitionState==running)",
        f"{UOM}; type=VirtualIOServer",
    ),
    (
        "vios-search-name",
        "/rest/api/uom/VirtualIOServer/search/(PartitionName=={vios_name})",
        f"{UOM}; type=VirtualIOServer",
    ),
    (
        "vios-quick-state",
        "/rest/api/uom/VirtualIOServer/{vios_uuid}/quick/PartitionState",
        "*/*",
    ),
    (
        "lpar-path-vios",
        "/rest/api/uom/LogicalPartition/{vios_uuid}",
        f"{UOM}; type=LogicalPartition",
    ),
    (
        "vios-groups-repeat",
        "/rest/api/uom/VirtualIOServer/{vios_uuid}?group=ViosSCSIMapping&group=ViosFCMapping",
        f"{UOM}; type=VirtualIOServer",
    ),
    (
        "vios-groups-comma",
        "/rest/api/uom/VirtualIOServer/{vios_uuid}?group=ViosSCSIMapping,ViosFCMapping",
        f"{UOM}; type=VirtualIOServer",
    ),
    ("sriov-root", "/rest/api/uom/SRIOVAdapter", f"{UOM}; type=SRIOVAdapter"),
    ("cluster", "/rest/api/uom/Cluster", f"{UOM}; type=Cluster"),
    ("ssp", "/rest/api/uom/SharedStoragePool", f"{UOM}; type=SharedStoragePool"),
    ("tmpl-atom", "/rest/api/templates/PartitionTemplate", "application/atom+xml"),
    ("pcm-prefs-any", "/rest/api/pcm/ManagedSystem/{sys_uuid}/preferences", "*/*"),
    ("web-file", "/rest/api/web/File", WEB),
    (
        "mc-remote-access",
        "/rest/api/uom/ManagementConsole/{console_uuid}?group=RemoteAccess",
        f"{WEB}; type=ManagementConsole",
    ),
)
#: (step, command) for `ls*` reads no tool issues; values are shell-quoted when filled.
RAW_COMMANDS: tuple[tuple[str, str], ...] = (
    ("lshmc-V", "lshmc -V"),
    ("sys-native", "lssyscfg -r sys -m {system_name}"),
    ("sys-no-m", "lssyscfg -r sys -F uuid,name"),
    ("sys-no-m-name", "lssyscfg -r sys -F name"),
    ("lpar-native", "lssyscfg -r lpar -m {system_name}"),
    ("lpar-no-m", "lssyscfg -r lpar -F uuid,name"),
    (
        "lpar-unknown",
        "lssyscfg -r lpar -m {system_name} --filter lpar_names=zz-no-such-lpar -F name",
    ),
    ("prof-native", "lssyscfg -r prof -m {system_name}"),
    ("io-slot-native", "lshwres -r io --rsubtype slot -m {system_name}"),
    ("mempool", "lshwres -r mempool -m {system_name}"),
    ("proc-lpar", "lshwres -r proc --level lpar -m {system_name}"),
    ("mem-lpar", "lshwres -r mem --level lpar -m {system_name}"),
    ("vio-fc", "lshwres -r virtualio --rsubtype fc --level lpar -m {system_name}"),
    ("vio-eth", "lshwres -r virtualio --rsubtype eth --level lpar -m {system_name}"),
    ("vio-vnic", "lshwres -r virtualio --rsubtype vnic --level lpar -m {system_name}"),
    ("vio-vnicbkdev", "lshwres -r virtualio --rsubtype vnicbkdev -m {system_name}"),
    ("sriov-adapter", "lshwres -r sriov --rsubtype adapter -m {system_name}"),
    (
        "sriov-phys-eth",
        "lshwres -r sriov --rsubtype physport --level eth -m {system_name}",
    ),
    (
        "sriov-phys-ethc",
        "lshwres -r sriov --rsubtype physport --level ethc -m {system_name}",
    ),
    (
        "sriov-phys-roce",
        "lshwres -r sriov --rsubtype physport --level roce -m {system_name}",
    ),
    (
        "sriov-logport",
        "lshwres -r sriov --rsubtype logport --level eth -m {system_name}",
    ),
    ("memopt-resgroup", "lsmemopt -m {system_name} -r resgroup -o currscore --gid all"),
    ("viosbk-all", "lsviosbk -F --header"),
    ("refcode-all", "lsrefcode -r lpar -m {system_name} -n 1"),
)


async def resolve_system(
    hmc: HMCClient, system: str, log: ToolLog
) -> tuple[str | None, str | None]:
    """(UUID, name) of *system* from ``ManagedSystem/quick/All`` (#1499).

    quick/All answers while a system is ``No Connection``, where the ``SystemName``
    search and the ``ManagedSystem/{uuid}`` read stall. A failed read is recorded and
    the half the operator supplied is kept, so the raw phase still runs.
    """
    from hmcpctl.errors import HMCError
    from hmcpctl.resource_identity import is_uuid

    given = (system, None) if is_uuid(system) else (None, system)
    try:
        by_uuid = await hmc._quick_all_system_names()
    except HMCError as exc:
        log.write(
            {
                "kind": "resolve",
                "step": "system",
                "system": system,
                "exception": str(exc),
            }
        )
        return given
    for uuid, name in by_uuid.items():
        if system.lower() == uuid or system == name:
            return uuid, name
    raise ValueError(f"no managed system {system!r} in ManagedSystem/quick/All")


async def resolve_partition(
    hmc: HMCClient, step: str, value: str, sys_uuid: str, log: ToolLog
) -> tuple[str | None, str | None]:
    """(UUID, name) of the LPAR or VIOS *value* on *sys_uuid* (#1532).

    A failed lookup or read is recorded and leaves both unset, so the raw phase still
    runs without that partition's probes. A name the system does not list still
    raises, and so does an HTTP 404 from either call -- for a supplied UUID, that is
    the operator's mistake.
    """
    from hmcpctl.errors import HMCError
    from hmcpctl.resource_identity import resolve_lpar_uuid, resolve_vios_uuid

    resolve, uom = (
        (resolve_lpar_uuid, "LogicalPartition")
        if step == "lpar"
        else (resolve_vios_uuid, "VirtualIOServer")
    )
    try:
        uuid = await resolve(hmc, value, system_name_or_uuid=sys_uuid)
        entry = await hmc.get_uom(uom, uuid) or {}
    except HMCError as exc:
        if exc.status_code == 404:
            raise
        log.write({"kind": "resolve", "step": step, step: value, "exception": str(exc)})
        return None, None
    return uuid, _leaf((entry.get("Resource") or entry).get("PartitionName")) or None


async def resolve_names(hmc: HMCClient, context: Context, log: ToolLog) -> Names:
    if context.system is None:
        raise ValueError("no managed system was discovered; pass --system")
    sys_uuid, system_name = await resolve_system(hmc, context.system, log)
    if sys_uuid is None:
        # Unscoped, a partition name could resolve on another system and mislabel
        # the capture; its placeholders stay unfilled and the raw phase skips them.
        return Names(system_name=system_name, console_uuid=context.console)
    lpar_uuid = lpar_name = vios_uuid = vios_name = None
    if context.lpar:
        lpar_uuid, lpar_name = await resolve_partition(
            hmc, "lpar", context.lpar, sys_uuid, log
        )
    if context.vios:
        vios_uuid, vios_name = await resolve_partition(
            hmc, "vios", context.vios, sys_uuid, log
        )
    return Names(
        sys_uuid=sys_uuid,
        system_name=system_name,
        lpar_uuid=lpar_uuid,
        lpar_name=lpar_name,
        vios_uuid=vios_uuid,
        vios_name=vios_name,
        console_uuid=context.console,
    )


async def sweep_raw(
    hmc: HMCClient, names: Names, log: ToolLog, step: Callable[[str], None]
) -> int:
    """Issue every raw probe whose placeholders are known; returns the number sent."""
    from hmcpctl.ssh.transport import run_hmc_command

    sent = 0
    probes: list[tuple[str, str, str | None]] = [(s, p, a) for s, p, a in RAW_GETS]
    probes += [(s, c, None) for s, c in RAW_COMMANDS]
    for name, template, accept in probes:
        filled = names.fill(template, shell=accept is None)
        if filled is None:
            log.write(
                {
                    "kind": "skip",
                    "step": name,
                    "probe": template,
                    "missing": "placeholder",
                }
            )
            continue
        step(name)
        sent += 1
        with contextlib.suppress(Exception):  # the harness recorded the outcome
            if accept is None:
                await run_hmc_command(hmc.config, filled)
            else:
                await hmc._request("GET", filled, headers={"Accept": accept})
    return sent


async def sweep(out: Path, context: Context) -> str:
    from live_test_runner import served_client

    from hmcpctl.client.client_factory import client_from_env

    directory = prepare_output(out)
    log = ToolLog(directory / "tools.capture.jsonl")
    try:
        with (
            read_only_guard(),
            capture(directory / "sweep.capture.jsonl", raw=True) as cap,
        ):
            async with served_client() as client:
                calls, skips = await sweep_tools(client, context, log, cap.step)
            async with client_from_env() as hmc:
                sent = await sweep_raw(
                    hmc, await resolve_names(hmc, context, log), log, cap.step
                )
    finally:
        log.close()
    return (
        f"{calls} tool calls, {skips} tools skipped, {sent} raw probes -> {directory}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, required=True, help="private output directory"
    )
    parser.add_argument("--profile", help="HMC profile to sweep (sets HMC_PROFILE)")
    parser.add_argument("--system", help="managed system name or UUID")
    parser.add_argument("--lpar", help="partition name or UUID (a running one is best)")
    parser.add_argument("--vios", help="VIOS name or UUID")
    args = parser.parse_args(argv)
    if args.profile:
        os.environ["HMC_PROFILE"] = args.profile
    context = Context(system=args.system, lpar=args.lpar, vios=args.vios)
    try:
        print(asyncio.run(sweep(args.out, context)))
    except (ValueError, OSError) as exc:
        print(f"live_capture_sweep: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
