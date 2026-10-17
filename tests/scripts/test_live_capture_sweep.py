"""Tests for the read-only capture sweep (scripts/live_capture_sweep.py, issue #1202).

Nothing here reaches an HMC: the transport seams are replaced by recorders, and the
MCP client by a fake that serves tool definitions and answers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncssh
import httpx
import pytest
from conftest import captured_lpar_entry, live_response, make_config

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_capture_sweep as sweep
from live_test.capture import capture

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.resource_identity import ResourceNotFoundError

# --- guard ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("GET", "/rest/api/uom/ManagedSystem", True),
        ("get", "/rest/api/uom/ManagedSystem", True),
        ("PUT", "/rest/api/web/Logon", True),
        ("DELETE", "/rest/api/web/Logon", True),
        ("POST", "/rest/api/web/Logon", False),
        ("PUT", "/rest/api/web/Logon/jobs", False),
        ("PUT", "/rest/api/uom/LogicalPartition/x/do/PowerOn", False),
        ("POST", "/rest/api/uom/ManagedSystem/x", False),
        ("DELETE", "/rest/api/uom/LogicalPartition/x", False),
    ],
)
def test_rest_allowed(method: str, path: str, allowed: bool) -> None:
    assert sweep.rest_allowed(method, path) is allowed


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("lssyscfg -r lpar -m sys1", True),
        ("lshmc -V", True),
        ("lssyscfg -r lpar -m 'a b' --filter 'lpar_names=x'", True),
        ("chsysstate -r lpar -o on -n x", False),
        ("rmsyscfg -r lpar -n x", False),
        ("lssyscfg -r lpar; rmsyscfg -r lpar -n x", False),
        ("lssyscfg -r lpar && chsyscfg", False),
        ("lssyscfg -r lpar | tee x", False),
        ("lssyscfg $(rmsyscfg)", False),
        ("lssyscfg -r lpar\nrmsyscfg", False),
        ("", False),
        (None, False),
    ],
)
def test_command_allowed(command: Any, allowed: bool) -> None:
    assert sweep.command_allowed(command) is allowed


class _Seams:
    """Replaces the transport originals with recorders before the guard captures them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sent: list[Any] = []

        async def request(client: Any, method: str, path: str, **kwargs: Any) -> str:
            self.sent.append((method, path))
            return "response"

        async def run(conn: Any, *args: Any, **kwargs: Any) -> str:
            self.sent.append(args[0] if args else kwargs.get("command"))
            return "result"

        monkeypatch.setattr(HMCClient, "_request", request)
        monkeypatch.setattr(asyncssh.SSHClientConnection, "run", run)
        monkeypatch.setattr(asyncssh.SSHClientConnection, "create_process", run)


def test_guard_refuses_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _Seams(monkeypatch)

    async def drive() -> None:
        with sweep.read_only_guard():
            assert await HMCClient._request(None, "GET", "/a") == "response"
            with pytest.raises(sweep.ReadOnlyViolation):
                await HMCClient._request(None, "POST", "/a")
            assert await asyncssh.SSHClientConnection.run(None, "lshmc -V") == "result"
            with pytest.raises(sweep.ReadOnlyViolation):
                await asyncssh.SSHClientConnection.run(None, command="rmsyscfg -n x")
            with pytest.raises(sweep.ReadOnlyViolation):
                await asyncssh.SSHClientConnection.create_process(None)

    asyncio.run(drive())
    assert seams.sent == [("GET", "/a"), "lshmc -V"]


def test_guard_restores_the_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    _Seams(monkeypatch)
    before = (HMCClient._request, asyncssh.SSHClientConnection.run)
    with pytest.raises(RuntimeError), sweep.read_only_guard():
        raise RuntimeError("boom")
    assert (HMCClient._request, asyncssh.SSHClientConnection.run) == before


def test_capture_records_the_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guard sits below the harness, so a refused call still leaves a record."""
    seams = _Seams(monkeypatch)
    destination = tmp_path / "sweep.capture.jsonl"

    async def drive() -> None:
        with (
            sweep.read_only_guard(),
            capture(destination),
            pytest.raises(sweep.ReadOnlyViolation),
        ):
            await HMCClient._request(None, "PUT", "/rest/api/uom/x/do/PowerOff")

    asyncio.run(drive())
    record = json.loads(destination.read_text())
    assert record["method"] == "PUT"
    assert record["exception"].startswith("ReadOnlyViolation: refused PUT")
    assert seams.sent == []


# --- planning -------------------------------------------------------------------


def _spec(name: str, required: tuple[str, ...] = (), optional: tuple[str, ...] = ()):
    return sweep.ToolSpec(name, required, required + optional)


def _context(**values: Any) -> sweep.Context:
    return sweep.Context(start_ts="2026-09-30T00:00:00Z", **values)


def test_required_parameters_come_from_the_context() -> None:
    spec = _spec(
        "hmc_get_lpar", ("system_name_or_uuid", "lpar_name_or_uuid"), ("profile",)
    )
    calls, missing = sweep.plan_calls(spec, _context(system="S", lpar="L"))
    assert calls == [{"system_name_or_uuid": "S", "lpar_name_or_uuid": "L"}]
    assert missing == []


def test_unresolvable_parameter_is_reported_not_guessed() -> None:
    calls, missing = sweep.plan_calls(_spec("hmc_get_job", ("job_id",)), _context())
    assert calls == []
    assert missing == ["job_id"]


def test_undiscovered_context_is_reported() -> None:
    calls, missing = sweep.plan_calls(
        _spec("hmc_list_vnics", ("lpar_name_or_uuid",)), _context()
    )
    assert calls == []
    assert missing == ["lpar_name_or_uuid"]


def test_optional_filters_are_left_unset() -> None:
    spec = _spec("hmc_list_clusters", optional=("system_name_or_uuid",))
    assert sweep.plan_calls(spec, _context(system="S")) == ([{}], [])


def test_adapter_id_is_filled_when_discovered() -> None:
    spec = _spec(
        "hmc_list_sriov_physical_ports", ("system_name_or_uuid",), ("adapter_id",)
    )
    calls, _ = sweep.plan_calls(spec, _context(system="S", adapter="1"))
    assert calls == [{"system_name_or_uuid": "S", "adapter_id": "1"}]


def test_variants_run_each_form() -> None:
    calls, missing = sweep.plan_calls(
        _spec("hmc_list_lpars", optional=("system_name_or_uuid", "state")),
        _context(system="S"),
    )
    assert calls == [{"system_name_or_uuid": "S"}, {}, {"state": "not activated"}]
    assert missing == []


def test_variant_with_an_undiscovered_value_is_dropped() -> None:
    calls, missing = sweep.plan_calls(
        _spec("hmc_get_lpar_state", ("lpar_name_or_uuid",)), _context(lpar="L")
    )
    assert calls == [{"lpar_name_or_uuid": "L"}]
    assert missing == ["lpar_name_or_uuid"]


def test_pcm_tools_run_per_category() -> None:
    spec = _spec(
        "hmc_processed_metrics",
        ("category", "resource_name_or_uuid", "start_ts"),
        ("system_name_or_uuid",),
    )
    calls, _ = sweep.plan_calls(spec, _context(system="S", lpar="L"))
    assert calls == [
        {
            "start_ts": "2026-09-30T00:00:00Z",
            "category": "ManagedSystem",
            "resource_name_or_uuid": "S",
            "system_name_or_uuid": "S",
        },
        {
            "start_ts": "2026-09-30T00:00:00Z",
            "category": "LogicalPartition",
            "resource_name_or_uuid": "L",
            "system_name_or_uuid": "S",
        },
    ]


# --- discovery ----------------------------------------------------------------------


def test_discovery_prefers_operating_systems_and_running_lpars() -> None:
    context = _context()
    systems = [
        {"UUID": "s-off", "Resource": {"State": "power off"}},
        {"UUID": "s-on", "Resource": {"State": {"#text": "operating"}}},
    ]
    lpars = [
        {"UUID": "l-off", "Resource": {"PartitionState": "not activated"}},
        {"UUID": "l-on", "Resource": {"PartitionState": "running"}},
    ]
    sweep.discover(context, "hmc_list_systems", systems)
    sweep.discover(context, "hmc_list_lpars", lpars)
    sweep.discover(context, "hmc_list_volume_groups", [{"uuid": "vg-1"}])
    sweep.discover(context, "hmc_list_sriov_adapters", {"items": [{"adapter_id": "1"}]})
    assert (context.system, context.lpar, context.vg, context.adapter) == (
        "s-on",
        "l-on",
        "vg-1",
        "1",
    )


@pytest.mark.parametrize(
    "wrap",
    [
        lambda rows: {"entries": rows, "unreadable_systems": []},
        lambda rows: SimpleNamespace(entries=rows, unreadable_systems=[]),
    ],
    ids=["structured-content", "generated-model"],
)
def test_discovery_reads_a_fleet_listing_envelope(wrap) -> None:
    """hmc_list_lpars / hmc_list_vios return a FleetListing (ADR 0197)."""
    context = _context()
    lpars = [{"UUID": "l-on", "Resource": {"PartitionState": "running"}}]
    sweep.discover(context, "hmc_list_lpars", wrap(lpars))
    sweep.discover(context, "hmc_list_vios", wrap([{"UUID": "v-1"}]))
    assert (context.lpar, context.vios) == ("l-on", "v-1")


def test_discovery_never_overrides_the_operator() -> None:
    context = _context(system="mine")
    sweep.discover(context, "hmc_list_systems", [{"UUID": "other"}])
    assert context.system == "mine"


def test_discovery_skips_unavailable_ids() -> None:
    context = _context()
    sweep.discover(
        context, "hmc_list_sriov_adapters", {"items": [{"adapter_id": "unavailable"}]}
    )
    assert context.adapter is None


# --- driving --------------------------------------------------------------------------


def _tool(name: str, read_only: bool, required: tuple[str, ...] = ()) -> Any:
    return SimpleNamespace(
        name=name,
        annotations=SimpleNamespace(read_only_hint=read_only),
        input_schema={
            "required": list(required),
            "properties": {name: {} for name in required},
        },
    )


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[Any]:
        return [
            _tool("hmc_get_system", True, ("system_name_or_uuid",)),
            _tool("hmc_power_on_lpar", False, ("lpar_name_or_uuid",)),
            _tool("hmc_list_systems", True),
            _tool("hmc_get_job", True, ("job_id",)),
            _tool("hmc_get_console_info", True),
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        if name == "hmc_get_console_info":
            raise RuntimeError("refused")
        data = [{"UUID": "sys-1", "Resource": {"State": "operating"}}]
        return SimpleNamespace(data=data, structured_content=None)


def test_sweep_tools_runs_read_only_tools_after_discovery(tmp_path: Path) -> None:
    client = _Client()
    log = sweep.ToolLog(tmp_path / "tools.capture.jsonl")
    steps: list[str] = []
    try:
        calls, skips = asyncio.run(
            sweep.sweep_tools(client, _context(), log, steps.append)
        )
    finally:
        log.close()
    names = [name for name, _ in client.calls]
    assert "hmc_power_on_lpar" not in names
    assert names[:3] == ["hmc_list_systems", "hmc_list_systems", "hmc_get_console_info"]
    assert ("hmc_get_system", {"system_name_or_uuid": "sys-1"}) in client.calls
    assert (calls, skips) == (4, 1)
    records = [
        json.loads(line)
        for line in (tmp_path / "tools.capture.jsonl").read_text().splitlines()
    ]
    assert {
        "kind": "skip",
        "step": "hmc_get_job",
        "tool": "hmc_get_job",
        "missing": ["job_id"],
    } in records
    failed = [r for r in records if r.get("ok") is False]
    assert failed[0]["error"] == "RuntimeError: refused"
    assert steps == names


def test_tool_log_records_a_dataclass_result_as_json(tmp_path: Path) -> None:
    """FastMCP serves a dataclass result as a generated dataclass (ADR 0197)."""

    @dataclasses.dataclass
    class Skipped:
        system_name: str

    @dataclasses.dataclass
    class Listing:
        entries: list
        unreadable_systems: list

    path = tmp_path / "tools.capture.jsonl"
    log = sweep.ToolLog(path)
    log.write({"data": Listing([{"UUID": "l-1"}], [Skipped("sys-R1")])})
    log.close()
    assert json.loads(path.read_text())["data"] == {
        "entries": [{"UUID": "l-1"}],
        "unreadable_systems": [{"system_name": "sys-R1"}],
    }


def test_tool_log_is_private(tmp_path: Path) -> None:
    path = tmp_path / "tools.capture.jsonl"
    path.write_text("")
    path.chmod(0o644)
    log = sweep.ToolLog(path)
    log.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_tool_log_refuses_a_destination_in_a_work_tree() -> None:
    inside = Path(__file__).parents[2] / "tools-not-ignored.json"
    with pytest.raises(ValueError, match="inside a git work tree"):
        sweep.ToolLog(inside)
    assert not inside.exists()


def test_tool_log_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("")
    link = tmp_path / "tools.capture.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        sweep.ToolLog(link)


def test_output_directory_is_private(tmp_path: Path) -> None:
    directory = sweep.prepare_output(tmp_path / "a" / "b")
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700


# --- raw probes --------------------------------------------------------------------


def test_names_fill_quotes_for_shell_and_url() -> None:
    names = sweep.Names(sys_uuid="u-1", system_name="sys one")
    assert names.fill("lssyscfg -r lpar -m {system_name}", shell=True) == (
        "lssyscfg -r lpar -m 'sys one'"
    )
    assert names.fill("/rest/api/uom/ManagedSystem/{sys_uuid}", shell=False) == (
        "/rest/api/uom/ManagedSystem/u-1"
    )
    assert names.fill("/rest/api/uom/VirtualIOServer/{vios_uuid}", shell=False) is None
    assert (
        names.fill("/x/(SystemName=={system_name})", shell=False)
        == "/x/(SystemName==sys%20one)"
    )


def test_raw_commands_pass_the_guard() -> None:
    names = sweep.Names(sys_uuid="u", system_name="s; rmsyscfg")
    for _, template in sweep.RAW_COMMANDS:
        filled = names.fill(template, shell=True)
        assert filled is not None
        assert filled.startswith("ls")
    # A hostile name is quoted, and the guard still refuses its metacharacter.
    assert not sweep.command_allowed(
        names.fill("lssyscfg -m {system_name}", shell=True)
    )


def test_raw_gets_are_reads() -> None:
    assert all(path.startswith("/rest/api/") for _, path, _ in sweep.RAW_GETS)


def test_sweep_raw_skips_unknown_placeholders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, Any]] = []

    class _Hmc:
        config = object()

        async def _request(self, method: str, path: str, **kwargs: Any) -> None:
            sent.append((method, path))
            if path.endswith("Enumerations.xsd"):
                raise RuntimeError("recorded by the harness")

    async def run(config: Any, command: str) -> str:
        sent.append(("ssh", command))
        return ""

    monkeypatch.setattr("hmcpctl.ssh.transport.run_hmc_command", run)
    log = sweep.ToolLog(tmp_path / "tools.capture.jsonl")
    try:
        count = asyncio.run(
            sweep.sweep_raw(_Hmc(), sweep.Names("u", "s"), log, lambda _: None)
        )
    finally:
        log.close()
    assert count == len(sent)
    assert ("ssh", "lssyscfg -r sys -m s") in sent
    assert all("{" not in str(item) for item in sent)
    skips = (tmp_path / "tools.capture.jsonl").read_text()
    assert "lpar-quick-all" in skips


# --- name resolution (#1499) ------------------------------------------------------

#: A ``No Connection`` system in the captured quick/All answer.
DISCONNECTED_NAME = "sys-R8"
DISCONNECTED_UUID = "00000219-abcd-4ef0-8abc-000000000219"
QUICK_ALL = "rest-ms-quick-all-no-connection-v11r2"


def _stalled(mock_hmc: Any) -> tuple[Any, Any]:
    """The two reads that timed out on a No Connection system in #1290."""
    timeout = httpx.ReadTimeout("read timed out")
    search = mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/search/(SystemName=={DISCONNECTED_NAME})"
    ).mock(side_effect=timeout)
    read = mock_hmc.get(f"/rest/api/uom/ManagedSystem/{DISCONNECTED_UUID}").mock(
        side_effect=timeout
    )
    return search, read


def _resolve(tmp_path: Path, system: str) -> tuple[Any, list[dict[str, Any]]]:
    async def run() -> Any:
        async with HMCClient(make_config()) as hmc:
            return await sweep.resolve_names(hmc, sweep.Context(system=system), log)

    path = tmp_path / "tools.capture.jsonl"
    log = sweep.ToolLog(path)
    try:
        names = asyncio.run(run())
    finally:
        log.close()
    return names, [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("system", [DISCONNECTED_NAME, DISCONNECTED_UUID])
def test_system_resolves_from_quick_all_without_the_stalling_reads(
    mock_hmc: Any, tmp_path: Path, system: str
) -> None:
    search, read = _stalled(mock_hmc)
    path, response = live_response(QUICK_ALL)
    mock_hmc.get(path).mock(return_value=response)

    names, records = _resolve(tmp_path, system)

    assert (names.sys_uuid, names.system_name) == (DISCONNECTED_UUID, DISCONNECTED_NAME)
    assert not search.called and not read.called
    assert records == []


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        (DISCONNECTED_NAME, (None, DISCONNECTED_NAME)),
        (DISCONNECTED_UUID, (DISCONNECTED_UUID, None)),
    ],
)
def test_a_timed_out_system_read_is_recorded_and_the_raw_phase_runs(
    mock_hmc: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    expected: tuple[str | None, str | None],
) -> None:
    _stalled(mock_hmc)
    path, _ = live_response(QUICK_ALL)
    mock_hmc.get(path).mock(side_effect=httpx.ReadTimeout("read timed out"))

    names, records = _resolve(tmp_path, system)

    assert (names.sys_uuid, names.system_name) == expected
    assert [(r["kind"], r["step"], r["system"]) for r in records] == [
        ("resolve", "system", system)
    ]
    assert "timed out" in records[0]["exception"]

    sent: list[str] = []

    async def run(config: Any, command: str) -> str:
        sent.append(command)
        return ""

    monkeypatch.setattr("hmcpctl.ssh.transport.run_hmc_command", run)
    log = sweep.ToolLog(tmp_path / "raw.capture.jsonl")
    hmc = SimpleNamespace(config=object(), _request=lambda *a, **k: asyncio.sleep(0))
    try:
        asyncio.run(sweep.sweep_raw(hmc, names, log, lambda _: None))
    finally:
        log.close()
    assert "lshmc -V" in sent
    assert ("lssyscfg -r sys -m sys-R8" in sent) is (names.system_name is not None)


def test_a_system_quick_all_does_not_list_is_refused(
    mock_hmc: Any, tmp_path: Path
) -> None:
    path, response = live_response(QUICK_ALL)
    mock_hmc.get(path).mock(return_value=response)

    with pytest.raises(ValueError, match="no-such-system"):
        _resolve(tmp_path, "no-such-system")


def test_partitions_are_not_resolved_without_the_system_uuid(
    mock_hmc: Any, tmp_path: Path
) -> None:
    path, _ = live_response(QUICK_ALL)
    mock_hmc.get(path).mock(side_effect=httpx.ReadTimeout("read timed out"))
    unscoped = mock_hmc.get(url__regex=r"/(LogicalPartition|VirtualIOServer)/").mock(
        return_value=httpx.Response(200, text="<feed/>")
    )

    async def run() -> Any:
        async with HMCClient(make_config()) as hmc:
            context = sweep.Context(system=DISCONNECTED_NAME, lpar="lp-1", vios="vio-1")
            return await sweep.resolve_names(hmc, context, log)

    log = sweep.ToolLog(tmp_path / "tools.capture.jsonl")
    try:
        names = asyncio.run(run())
    finally:
        log.close()

    assert names == sweep.Names(system_name=DISCONNECTED_NAME)
    assert not unscoped.called


# --- partition and VIOS lookups (#1532) -------------------------------------------

LPAR_UUID = "3c1a5b2e-0000-4000-8000-000000001532"
VIOS_UUID = "4d2b6c3f-0000-4000-8000-000000001532"


#: An ``operating`` system in a second captured quick/All answer.
OPERATING_NAME = "sys-11"
OPERATING_QUICK_ALL = "rest-ms-quick-all"


def _resolve_partitions(
    mock_hmc: Any, tmp_path: Path, context: sweep.Context, quick_all: str = QUICK_ALL
) -> tuple[Any, list[dict[str, Any]]]:
    path, response = live_response(quick_all)
    mock_hmc.get(path).mock(return_value=response)

    async def run() -> Any:
        async with HMCClient(make_config()) as hmc:
            return await sweep.resolve_names(hmc, context, log)

    capture_path = tmp_path / "tools.capture.jsonl"
    log = sweep.ToolLog(capture_path)
    try:
        names = asyncio.run(run())
    finally:
        log.close()
    lines = capture_path.read_text().splitlines()
    return names, [json.loads(line) for line in lines]


def _raw_commands(
    names: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    sent: list[str] = []

    async def run(config: Any, command: str) -> str:
        sent.append(command)
        return ""

    monkeypatch.setattr("hmcpctl.ssh.transport.run_hmc_command", run)
    log = sweep.ToolLog(tmp_path / "raw.capture.jsonl")
    hmc = SimpleNamespace(config=object(), _request=lambda *a, **k: asyncio.sleep(0))
    try:
        asyncio.run(sweep.sweep_raw(hmc, names, log, lambda _: None))
    finally:
        log.close()
    return sent


@pytest.fixture
def contained_partitions(mock_hmc):
    for kind, uuid in [("LogicalPartition", LPAR_UUID), ("VirtualIOServer", VIOS_UUID)]:
        entry = captured_lpar_entry(uuid, "target").replace("LogicalPartition", kind)
        mock_hmc.get(f"/rest/api/uom/ManagedSystem/{DISCONNECTED_UUID}/{kind}").mock(
            return_value=httpx.Response(
                200, text=f'<feed xmlns="http://www.w3.org/2005/Atom">{entry}</feed>'
            )
        )


@pytest.mark.usefixtures("contained_partitions")
@pytest.mark.parametrize(
    ("step", "context", "route"),
    [
        (
            "lpar",
            sweep.Context(system=DISCONNECTED_NAME, lpar="lp-1"),
            rf"/ManagedSystem/{DISCONNECTED_UUID}/LogicalPartition",
        ),
        (
            "lpar",
            sweep.Context(system=DISCONNECTED_NAME, lpar=LPAR_UUID),
            rf"/LogicalPartition/{LPAR_UUID}",
        ),
        (
            "vios",
            sweep.Context(system=DISCONNECTED_NAME, vios="vio-1"),
            rf"/ManagedSystem/{DISCONNECTED_UUID}/VirtualIOServer",
        ),
        (
            "vios",
            sweep.Context(system=DISCONNECTED_NAME, vios=VIOS_UUID),
            rf"/VirtualIOServer/{VIOS_UUID}",
        ),
        (
            "lpar",
            sweep.Context(system=DISCONNECTED_NAME, lpar=LPAR_UUID),
            rf"/ManagedSystem/{DISCONNECTED_UUID}/LogicalPartition",
        ),
        (
            "vios",
            sweep.Context(system=DISCONNECTED_NAME, vios=VIOS_UUID),
            rf"/ManagedSystem/{DISCONNECTED_UUID}/VirtualIOServer",
        ),
    ],
    ids=[
        "lpar-lookup",
        "lpar-read",
        "vios-lookup",
        "vios-read",
        "lpar-membership",
        "vios-membership",
    ],
)
def test_a_timed_out_partition_lookup_is_recorded_and_the_raw_phase_runs(
    mock_hmc: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    context: sweep.Context,
    route: str,
) -> None:
    mock_hmc.get(f"/rest/api/uom{route}").mock(
        side_effect=httpx.ReadTimeout("read timed out")
    )

    names, records = _resolve_partitions(mock_hmc, tmp_path, context)

    assert names == sweep.Names(
        sys_uuid=DISCONNECTED_UUID, system_name=DISCONNECTED_NAME
    )
    value = context.lpar if step == "lpar" else context.vios
    assert [(r["kind"], r["step"], r[step]) for r in records] == [
        ("resolve", step, value)
    ]
    assert "timed out" in records[0]["exception"]
    sent = _raw_commands(names, tmp_path, monkeypatch)
    assert "lssyscfg -r sys -m sys-R8" in sent
    assert not any("lp-1" in command or "vio-1" in command for command in sent)


@pytest.mark.parametrize(
    "context",
    [
        sweep.Context(system=OPERATING_NAME, lpar="lp-1"),
        sweep.Context(system=OPERATING_NAME, vios="vio-1"),
    ],
    ids=["lpar", "vios"],
)
def test_a_partition_name_that_is_not_found_still_fails(
    mock_hmc: Any, tmp_path: Path, context: sweep.Context
) -> None:
    mock_hmc.get(url__regex=r"/(LogicalPartition|VirtualIOServer)$").mock(
        return_value=httpx.Response(204)
    )

    with pytest.raises(ResourceNotFoundError, match="No (LPAR|VIOS) named"):
        _resolve_partitions(mock_hmc, tmp_path, context, OPERATING_QUICK_ALL)


@pytest.mark.parametrize(
    "context",
    [
        sweep.Context(system=DISCONNECTED_NAME, lpar=LPAR_UUID),
        sweep.Context(system=DISCONNECTED_NAME, vios=VIOS_UUID),
    ],
    ids=["lpar", "vios"],
)
@pytest.mark.usefixtures("contained_partitions")
def test_a_partition_uuid_that_is_not_found_still_fails(
    mock_hmc: Any, tmp_path: Path, context: sweep.Context
) -> None:
    mock_hmc.get(url__regex=rf"/({LPAR_UUID}|{VIOS_UUID})$").mock(
        return_value=httpx.Response(404, text="not found")
    )

    with pytest.raises(HMCError) as excinfo:
        _resolve_partitions(mock_hmc, tmp_path, context)
    assert excinfo.value.status_code == 404
