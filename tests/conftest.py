"""Shared pytest fixtures for the hmcpctl suite."""

import io
import json
import logging
import os
import secrets
import select
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh
import fastmcp  # noqa: F401 — imported for its import-time logging configuration
import httpx
import pytest
import respx

from hmcpctl import config
from hmcpctl.audit import sink as audit_sink
from hmcpctl.audit.sink import AUDIT_LOGGER_NAME
from hmcpctl.config import HMCConfig

#: The ``fastmcp`` logger and its handlers as importing ``fastmcp`` leaves them.
#: Captured at collection time, before any test can serve, because ADR 0051 made
#: ``server._serve_application`` *replace* those handlers — so unlike the audit
#: logger, whose pristine state is "empty", no later code can reconstruct this one.
#:
#: ``import fastmcp`` above is what makes the snapshot the right one rather than an
#: empty list: ``fastmcp/__init__.py`` attaches the handlers, and nothing else this
#: module imports pulls that package in. Snapshotting before it would silently
#: replace every test's ``fastmcp`` logger with an unconfigured one.
_FASTMCP_LOGGER = logging.getLogger("fastmcp")
_PRISTINE_FASTMCP = (
    list(_FASTMCP_LOGGER.handlers),
    _FASTMCP_LOGGER.level,
    _FASTMCP_LOGGER.propagate,
)

_THIRD_PARTY_LOGGERS = tuple(
    logging.getLogger(name)
    for name in ("uvicorn", "uvicorn.access", "mcp", "py.warnings")
)
#: Pristine state of the loggers #330's install binds beyond ``fastmcp``: captured
#: at collection like ``_PRISTINE_FASTMCP``, though for these it is simply "empty,
#: NOTSET, propagating" — nothing configures them at import while ``dictConfig``
#: never runs. Snapshotted rather than assumed so a future import-time configurator
#: changes the fixture's behaviour instead of being silently overwritten by it.
_PRISTINE_THIRD_PARTY = tuple(
    (list(each.handlers), each.level, each.propagate) for each in _THIRD_PARTY_LOGGERS
)


#: The package logger #534's install binds. Unlike ``fastmcp`` its pristine state is
#: reconstructible — nothing configures it at import — so the fixture resets it to
#: empty rather than applying a snapshot. Only the handler list: the install leaves
#: ``propagate`` and the level alone, so nothing else about it moves.
_PACKAGE_LOGGER = logging.getLogger("hmcpctl")
_PRISTINE_SHOWWARNING = warnings.showwarning


@pytest.fixture(autouse=True)
def enable_tls_verification_for_tests(monkeypatch):
    """Keep mocked HMC connections secure unless a test opts out explicitly."""
    wanted = {f"HMC_{field.upper()}".lower() for field in HMCConfig.model_fields}
    wanted.add("hmc_profile")
    for name in [n for n in os.environ if n.lower() in wanted]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HMC_VERIFY_SSL", "true")
    monkeypatch.delenv("HMC_AUTHORIZE_POWER_OPERATIONS", raising=False)


@pytest.fixture(autouse=True)
def default_power_ownership_guard_off(monkeypatch):
    """Give every test the shipped default of the ADR 0092 §4 power guard.

    `HMCConfig` reads `HMC_AUTHORIZE_POWER_OPERATIONS` from the environment
    like every other field, so a developer who exports it turns the ownership
    guard on inside every test that builds its config from the environment —
    the tool and CLI layers. Those tests then send the guard's ownership name
    lookups at routes respx never registered, and fail for a reason that has
    nothing to do with what they assert.

    Cleared here rather than in each module: the default is what nearly every
    test means to exercise. A test that wants the guard on sets the variable
    in its own body, which runs after this fixture, or builds the config with
    `HMCConfig.from_mapping` and bypasses the environment entirely (ADR 0096).

    Every casing, not just the canonical one: `HMCConfig` leaves
    pydantic-settings' `case_sensitive` at its `False` default, so a developer or
    CI runner exporting `hmc_authorize_power_operations` sets the field exactly
    as the upper-case spelling does — and `server_tools.permissions` reads the casing
    itself to decide the reported `source`. Clearing only the exact name leaves
    both observable in the tests that pin them.
    """
    spellings = [n for n in os.environ if n.upper() == "HMC_AUTHORIZE_POWER_OPERATIONS"]
    for name in spellings:
        monkeypatch.delenv(name, raising=False)


#: `HMC_*` variables the suite depends on being unset. Not all thirteen fields —
#: only the names a test actually asserts the absence of, each demonstrated to red
#: the suite when exported.
_UNSET_FOR_TESTS = ("HMC_AGENT_ID", "HMC_SCHEMA_VERSION", "HMC_ISO_URL_ALLOWLIST")


@pytest.fixture(autouse=True)
def no_ambient_hmc_settings(monkeypatch):
    """Give every test an unset value for each of those, in every casing.

    The same hazard as the fixture above, and #543 widened it: `agent_id` reaches
    the `X-Audit-Memento` header, the ADR 0011 ownership stamp, and — since the
    authorization record's attribution stopped reading the variable exact-case —
    the ADR 0040 audit stream as well. A developer or CI runner exporting
    `hmc_agent_id`, which is the export #543 exists because operators make, turns
    every one of those into a value the assertions do not expect, and the failure
    message names a claimant rather than a casing. `hmc_schema_version` and
    `hmc_iso_url_allowlist` reached the client's header tests and
    `from_mapping`'s isolation test the same way.

    A test that wants one of these sets it in its own body, which runs after this.
    """
    # Folded down, matching pydantic-settings: an upper-fold both misses spellings
    # the loader reads and matches spellings it ignores.
    wanted = {name.lower() for name in _UNSET_FOR_TESTS}
    for name in [n for n in os.environ if n.lower() in wanted]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_operation_state(monkeypatch, tmp_path_factory):
    """Point the ADR 0190 operation store at a per-test directory nobody created yet.

    A developer's own ``HMCPCTL_STATE_DIR`` must never receive a test's records, and
    the execution lock a test takes must not outlive it.
    """
    from hmcpctl.operations.logical import store

    root = tmp_path_factory.getbasetemp() / "operation-state" / secrets.token_hex(8)
    monkeypatch.setenv(store.STATE_DIR_ENV, str(root))
    yield
    store.release_execution_locks()


@pytest.fixture(autouse=True)
def wide_consoles(monkeypatch):
    """Render every Rich console at one wide width, whatever the terminal is (#1586).

    A narrow terminal wraps or truncates the text CLI and server tests match on, so
    ``COLUMNS`` is pinned for the test and for any subprocess it starts. It is wide
    enough that a message carrying a long temporary path stays on one line. Typer builds
    its help, error and traceback consoles per call, so they read it then; it prefers
    ``TERMINAL_WIDTH``, frozen into ``MAX_WIDTH`` at import, so that is cleared too.

    Rich freezes a console's width at construction when ``COLUMNS`` is set, which
    ``setenv`` cannot reach. The consoles built before the test are the CLI's two in
    ``output`` -- which every command module imports by name, so the objects are the
    ones to change -- and fastmcp's logging handlers. Clearing ``_width`` (what Rich's
    ``width`` setter assigns) makes each read ``COLUMNS`` again. Patching the
    attribute, not the property, lets monkeypatch restore the stored value rather
    than the computed width the property would read back.
    """
    from rich.logging import RichHandler
    from typer import rich_utils

    from hmcpctl.cli_commands import output

    monkeypatch.setenv("COLUMNS", "1000")
    monkeypatch.delenv("TERMINAL_WIDTH", raising=False)
    monkeypatch.setattr(rich_utils, "MAX_WIDTH", None)
    handlers = [h for h in _PRISTINE_FASTMCP[0] if isinstance(h, RichHandler)]
    for console in (output.console, output.err_console, *(h.console for h in handlers)):
        monkeypatch.setattr(console, "_width", None)


@pytest.fixture(autouse=True)
def reset_audit_memento_override_dedup():
    """Give every test a fresh ``config`` override-warning flag (#546).

    The flag is process-global by design: a served process builds a fresh config
    inside every tool body, while the diagnostic has information only once.

    Cleared at setup as well as teardown, and autouse rather than opted into,
    for the reason ``isolate_audit_logging`` gives just below: the tests that
    merely *construct* an overriding config leak the state as readily as the ones
    that assert on it, and any enumeration of them goes stale. Matches the
    explicit ``server_permissions._reported_unresolved.clear()`` in
    ``tests/app/test_power_guard_report.py``.
    """
    config._reported_memento_override = False
    yield
    config._reported_memento_override = False


def _restore_fastmcp_logger() -> None:
    handlers, level, propagate = _PRISTINE_FASTMCP
    _FASTMCP_LOGGER.handlers[:] = handlers
    _FASTMCP_LOGGER.setLevel(level)
    _FASTMCP_LOGGER.propagate = propagate


def _restore_third_party_loggers() -> None:
    for logger, (handlers, level, propagate) in zip(
        _THIRD_PARTY_LOGGERS, _PRISTINE_THIRD_PARTY
    ):
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.fixture(autouse=True)
def isolate_audit_logging():
    """Give every test a pristine ``hmcpctl.audit`` logger, and restore it after.

    ``audit_sink.install_audit_sink`` mutates process-global state: it sets
    ``propagate = False`` unconditionally, attaches a handler, and sets a level.
    Several tests call it directly, and ``server._serve_application`` calls it too
    — which ``tests/app/test_capability_ceiling.py`` and
    ``tests/app/test_connection_authorization.py`` drive in-process. Whichever runs
    first would otherwise leave the sink installed for the rest of the session, and
    a later ``caplog`` assertion would capture nothing and pass vacuously.

    So it resets at setup as well as restoring at teardown: restoring alone would
    faithfully restore an earlier test's contamination, which is the failure this
    exists to prevent. Autouse and in ``conftest.py`` rather than opted into
    per-file, because the files that merely *emit* a record — every test driving an
    ADR 0011 ownership override — are as able to leak state as the ones that read
    records, and any enumeration of them goes stale.

    It also snapshots and restores ``logging.root.handlers``, which it never
    modifies itself. That is a deliberate belt-and-braces sweep, not dead code:
    several audit tests clear or add root handlers to observe
    ``logging.lastResort``, which ``callHandlers`` consults only after an ancestor
    walk finds none. Each of those cleans up after itself; this is the backstop for
    one that forgets, since a stray root handler silently changes what every later
    test captures.

    Since ADR 0051 it does the same for the ``fastmcp`` logger, whose handlers
    ``_serve_application`` also replaces; since #330's amendment it does it for the
    three further loggers that install binds — ``uvicorn``, ``uvicorn.access`` and
    ``mcp`` — which add level and propagation to what must be restored. Those
    loggers need their pristine state captured at import rather than reset to empty,
    so the snapshots live at module level and this fixture only applies them — at
    setup as well as teardown, for the reason above.

    Since #534 the ``hmcpctl`` logger is reset the same way, for the same reason:
    ``server.install_package_stderr_sink`` attaches a handler to it, and a handler
    left behind by a serving test would take a later test's ``hmcpctl.*`` records
    onto the sink and out of whatever that test meant to read them from.
    """
    logging.captureWarnings(False)
    warnings.showwarning = _PRISTINE_SHOWWARNING
    _restore_fastmcp_logger()
    _restore_third_party_loggers()
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_root = list(logging.root.handlers)
    saved_package = list(_PACKAGE_LOGGER.handlers)
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    _PACKAGE_LOGGER.handlers[:] = []
    try:
        yield
    finally:
        logging.captureWarnings(False)
        warnings.showwarning = _PRISTINE_SHOWWARNING
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
        logging.root.handlers[:] = saved_root
        _PACKAGE_LOGGER.handlers[:] = saved_package
        _restore_fastmcp_logger()
        _restore_third_party_loggers()
        # ADR 0043 made delivery asynchronous, so a record emitted here can still
        # be in flight after the test returns. Settle the sink and clear anything
        # it is owed, or one test's records land in another test's captured output
        # — or, when the writer is only scheduled after the test's `sys.stderr`
        # redirection is undone, on the console. Settling it against a throwaway
        # stream is what keeps that late write somewhere harmless; a test that
        # cares about the content has already waited for it with an explicit
        # `flush`/`drain`.
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            audit_sink._sink().drain(audit_sink._DRAIN_TIMEOUT)
        finally:
            sys.stderr = stderr
        with audit_sink._sink()._state:
            audit_sink._sink()._dropped = 0


@dataclass
class FullPipe:
    """A pipe whose buffer is full and whose write end blocks. See ADR 0043.

    `stream` is what a test puts on `sys.stderr`; `read_fd` is the end nothing is
    reading, which is what makes the next write block.
    """

    stream: object
    read_fd: int
    capacity: int

    def read_available(self) -> bytes:
        """Drain whatever is buffered, freeing the writer. Never blocks.

        A bare ``os.read`` would block once the buffer is empty, which is exactly
        the state this leaves behind — so it would hang the *test* on the second
        call instead of the server on the first.
        """
        chunks: list[bytes] = []
        while select.select([self.read_fd], [], [], 0.1)[0]:
            chunk = os.read(self.read_fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


@pytest.fixture
def full_stderr_pipe():
    """A genuinely full, genuinely blocking pipe — not a mock of one.

    `O_NONBLOCK` is a property of the open file description rather than of the
    descriptor, so setting it, writing until `BlockingIOError`, and clearing it
    again leaves the buffer full *and* the descriptor blocking. That is the exact
    condition issue #269 describes: `write()` neither returns nor raises.

    Teardown closes the read end first, which wakes a blocked writer with `EPIPE`
    — an `OSError` the sink already treats as a drop — so the daemon thread is
    never left parked on a descriptor the next test cannot free.
    """
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    capacity = 0
    try:
        while True:
            capacity += os.write(write_fd, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(write_fd, True)
    stream = os.fdopen(write_fd, "w")
    try:
        yield FullPipe(stream=stream, read_fd=read_fd, capacity=capacity)
    finally:
        os.close(read_fd)
        audit_sink._sink().drain(audit_sink._DRAIN_TIMEOUT)
        try:
            stream.close()
        except OSError:
            pass


BASE = "https://hmc.test:443"

LOGON_RESPONSE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<LogonResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/">
  <X-API-Session>test-session-token-123</X-API-Session>
</LogonResponse>
"""

LIVE_FIXTURES = Path(__file__).parent / "fixtures" / "live"


def live_fixture(name: str) -> dict[str, Any]:
    """Return the tokenized capture ``tests/fixtures/live/<name>.json`` (#1161)."""
    return json.loads((LIVE_FIXTURES / f"{name}.json").read_text())


def live_response(name: str) -> tuple[str, httpx.Response]:
    """Return a captured REST request path and the HMC's answer, for respx."""
    capture = live_fixture(name)
    response = httpx.Response(
        capture["status"],
        text=capture["body"],
        headers={"Content-Type": capture["content_type"]},
    )
    return capture["path"], response


def live_process_error(name: str) -> asyncssh.ProcessError:
    """Return a captured nonzero-exit CLI answer as asyncssh raises it (#1202).

    The HMC prints its refusal on stdout and leaves stderr empty.
    """
    capture = live_fixture(name)
    return asyncssh.ProcessError(
        env={},
        command=capture["command"],
        subsystem=None,
        exit_status=capture["exit_status"],
        exit_signal=None,
        returncode=capture["exit_status"],
        stdout=capture["stdout"],
        stderr=capture["stderr"],
    )


# A PowerOn submission as a V10R3 HMC answers it: a `JobResponse` whose numeric
# JobID differs from the entry UUID, with a one-segment SELF link (#1161, P1).
JOB_ENTRY = live_fixture("rest-poweron-submit")["body"]
JOB_ID = "1787837921266"
# A read of another PowerOn job while it runs: the entry UUID is per read and the
# first SELF link is the HMC's malformed `nulljobs/{JobID}` (#1161, P2).
RUNNING_JOB_ENTRY = live_fixture("rest-job-running")["body"]
RUNNING_JOB_ID = "1787837921267"
# A read of the JOB_ENTRY job after it finished (#1161, P2). A finished job reads
# COMPLETED_OK; the documented job statuses have no bare COMPLETED
# (docs/refs/hmc-rest-api-p10/016-job-status.md:16-27).
COMPLETED_JOB_ENTRY = live_fixture("rest-job-completed-ok")["body"]

# The memory and processor elements a whole-partition read-modify-write edits
# (#1057), for LogicalPartition entries that tests hand to rename or DLPAR.
LPAR_RESOURCE_CONFIG = """\
      <PartitionMemoryConfiguration>
        <DesiredMemory>4096</DesiredMemory>
        <MaximumMemory>16384</MaximumMemory>
        <MinimumMemory>1024</MinimumMemory>
      </PartitionMemoryConfiguration>
      <PartitionProcessorConfiguration>
        <HasDedicatedProcessors>false</HasDedicatedProcessors>
        <SharedProcessorConfiguration>
          <DesiredProcessingUnits>0.5</DesiredProcessingUnits>
          <DesiredVirtualProcessors>1</DesiredVirtualProcessors>
        </SharedProcessorConfiguration>
      </PartitionProcessorConfiguration>
"""

# Single-resource uom entries used by SSH tools to resolve a system / LPAR
# UUID to its CLI name via REST. Both accept {uuid} and {name} placeholders.
SYSTEM_ENTRY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{uuid}</id>
  <title>ManagedSystem:{name}</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <SystemName>{name}</SystemName>
    </ManagedSystem>
  </content>
</entry>
"""

LPAR_ENTRY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{uuid}</id>
  <title>LogicalPartition:{name}</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <PartitionName>{name}</PartitionName>
    </LogicalPartition>
  </content>
</entry>
"""


def mock_system_summary(router, system_uuid: str, system_name: str):
    """Register ``ManagedSystem/quick/All`` listing one operating system."""
    return mock_system_summaries(router, (system_uuid, system_name, "operating"))


def mock_uuid_resolution(
    router,
    system_uuid: str,
    system_name: str,
    lpar_uuid: str | None = None,
    lpar_name: str | None = None,
):
    """Register respx routes resolving system/lpar UUIDs to their CLI names.

    SSH-passthrough tools look up a UUID via REST before running the HMC
    command, so tests must mock the ``ManagedSystem/quick/All`` and
    ``get_logical_partition`` GETs in addition to ``asyncssh.connect``.
    """
    mock_system_summary(router, system_uuid, system_name)
    if lpar_uuid is not None:
        lpar_entry = LPAR_ENTRY.format(uuid=lpar_uuid, name=lpar_name)
        router.get(f"/rest/api/uom/LogicalPartition/{lpar_uuid}").mock(
            return_value=httpx.Response(200, text=lpar_entry)
        )
        feed_entry = (
            lpar_entry.split("?>", 1)[1]
            .strip()
            .replace(' xmlns="http://www.w3.org/2005/Atom"', "", 1)
        )
        router.get(f"/rest/api/uom/ManagedSystem/{system_uuid}/LogicalPartition").mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<feed xmlns="http://www.w3.org/2005/Atom">'
                    f"{feed_entry}</feed>"
                ),
            )
        )


def make_config(**kw) -> HMCConfig:
    """Build a test HMCConfig; any field may be overridden via **kw."""
    defaults = {
        "host": "hmc.test",
        "user": "hscroot",
        "password": "abc123",
        "verify_ssl": True,
    }
    defaults.update(kw)
    return HMCConfig(**defaults)


@pytest.fixture
def mock_hmc():
    """respx router with logon/logoff pre-mocked; add per-test routes."""
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.put("/rest/api/web/Logon").mock(
            return_value=httpx.Response(200, text=LOGON_RESPONSE)
        )
        router.delete("/rest/api/web/Logon").mock(return_value=httpx.Response(204))
        yield router


def mock_change_location(router, lpar_uuid: str, sync: str = "Disabled"):
    """Register the partition GET adapter and mapping commands read first (#981)."""
    entry = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        f"<id>urn:uuid:{lpar_uuid}</id>"
        '<content type="application/vnd.ibm.powervm.uom+xml">'
        '<LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">'
        f'<CurrentProfileSync kb="CUD" kxe="false">{sync}</CurrentProfileSync>'
        "</LogicalPartition></content></entry>"
    )
    return router.get(f"/rest/api/uom/LogicalPartition/{lpar_uuid}").mock(
        return_value=httpx.Response(200, text=entry)
    )


def mock_system_summaries(router, *systems: tuple[str, str, str]):
    """Register ManagedSystem/quick/All with one (UUID, name, State) summary each (#1309).

    Keyed as captured in ``tests/fixtures/live/rest-ms-quick-all.json``.
    """
    body = [
        {"UUID": uuid, "SystemName": name, "State": state, "StateDetail": ""}
        for uuid, name, state in systems
    ]
    return router.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(200, json=body)
    )


# The session lifecycle is the only non-GET traffic a read-only call may produce.
_SESSION_PATHS = frozenset({"/rest/api/web/Logon"})


def assert_no_mutating_requests(router) -> int:
    """Assert *router* recorded no HMC mutation, and return the request count.

    Epic #218 requirement 5 asks that a ``dry_run=True`` path "provably performs
    no mutation" be *classified and tested explicitly rather than inferred from
    client claims". A per-route ``assert not route.called`` cannot do that: it
    proves the one route a test happened to register was not taken, and stays
    green when the handler mutates through a route nobody registered.

    This reads every request the call actually made, so a new write on the
    dry-run path fails whether or not the test anticipated it. ADR 0039 records
    what remains on those paths and is deliberately not a mutation: reads, and —
    on ``hmc_decommission_lpar`` — an SSH ``lssyscfg`` and a local audit line.
    """
    offending = [
        f"{call.request.method} {call.request.url.path}"
        for call in router.calls
        if call.request.method != "GET" and call.request.url.path not in _SESSION_PATHS
    ]
    assert not offending, f"a dry-run path mutated the HMC: {offending}"
    return len(router.calls)


def assert_only_these_client_methods_used(client, allowed: frozenset[str]) -> set[str]:
    """Assert *client* saw only *allowed* methods, and return the ones it saw.

    The mock-client counterpart of :func:`assert_no_mutating_requests`, for the
    handlers whose tests drive an ``AsyncMock`` rather than a transport. It pins
    the whole call set rather than asserting a handful of ``assert_not_awaited``
    negatives, so a *new* call on a dry-run path fails the test whether or not it
    mutates — which is the point: epic #218 requirement 5 asks the path to be
    classified, and a call nobody classified is exactly what must not slip in.
    """
    used = {name.split(".")[0] for name, _args, _kwargs in client.mock_calls if name}
    unexpected = used - allowed
    assert not unexpected, (
        f"unclassified calls on a dry-run path: {sorted(unexpected)}. Each is "
        "either a mutation (a defect) or a read that must be added to the pinned "
        "set with its classification."
    )
    return used


# A ManagedSystem <entry> in the shape V10R3 served for #1175 (read-only capture
# 2026-09-30, POWER9): capacity sits in the memory and processor configuration
# containers, several leaves carry a ``ksv`` attribute, and MTMS is structured.
# Defaults are the captured values; the serial is a placeholder. ``omit`` drops
# the named top-level elements so tests can prove a missing one is not zero.
_CAPTURED_SYSTEM_ELEMENTS = {
    "AssociatedSystemMemoryConfiguration": """\
      <AssociatedSystemMemoryConfiguration>
        <Metadata><Atom/></Metadata>
        <ConfigurableSystemMemory>{configurable_mem}</ConfigurableSystemMemory>
        <CurrentAvailableSystemMemory>{available_mem}</CurrentAvailableSystemMemory>
        <InstalledSystemMemory>{configurable_mem}</InstalledSystemMemory>
        <PendingAvailableSystemMemory>{available_mem}</PendingAvailableSystemMemory>
        <MemoryUsedByHypervisor>10432</MemoryUsedByHypervisor>
        <CurrentAssignedMemoryToPartitions ksv="V1_10_0">8192</CurrentAssignedMemoryToPartitions>
      </AssociatedSystemMemoryConfiguration>""",
    "AssociatedSystemProcessorConfiguration": """\
      <AssociatedSystemProcessorConfiguration>
        <Metadata><Atom/></Metadata>
        <ConfigurableSystemProcessorUnits>{configurable_proc}</ConfigurableSystemProcessorUnits>
        <CurrentAvailableSystemProcessorUnits>{available_proc}</CurrentAvailableSystemProcessorUnits>
        <InstalledSystemProcessorUnits>{configurable_proc}</InstalledSystemProcessorUnits>
        <DeconfiguredSystemProcessorUnits group="Hypervisor">0</DeconfiguredSystemProcessorUnits>
      </AssociatedSystemProcessorConfiguration>""",
    "MachineTypeModelAndSerialNumber": """\
      <MachineTypeModelAndSerialNumber>
        <Metadata><Atom/></Metadata>
        <MachineType>8375</MachineType>
        <Model>42A</Model>
        <SerialNumber>SERIAL0</SerialNumber>
      </MachineTypeModelAndSerialNumber>""",
    "State": "      <State>operating</State>",
    "SystemFirmware": '      <SystemFirmware ksv="V1_2_0">VL950_FW950.00 (39)</SystemFirmware>',
    "SystemType": '      <SystemType ksv="V1_7_0">fsp</SystemType>',
}


def captured_system_entry(
    uuid: str,
    name: str,
    *,
    configurable_mem: str = "131072",
    available_mem: str = "112448",
    configurable_proc: str = "20",
    available_proc: str = "18",
    omit: tuple[str, ...] = (),
) -> str:
    """A feed ``<entry>`` for one ManagedSystem in the captured V10R3 shape."""
    body = "\n".join(
        element.format(
            configurable_mem=configurable_mem,
            available_mem=available_mem,
            configurable_proc=configurable_proc,
            available_proc=available_proc,
        )
        for key, element in _CAPTURED_SYSTEM_ELEMENTS.items()
        if key not in omit
    )
    return f"""  <entry>
    <id>urn:uuid:{uuid}</id>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <Metadata><Atom/></Metadata>
      <SystemName>{name}</SystemName>
{body}
      </ManagedSystem>
    </content>
  </entry>"""


# A LogicalPartition <entry> in the captured V10R3 shape of a partition that is
# not activated: every memory and processor figure reads "0" although its
# profile holds memory, so nothing may sum these into assigned capacity.
def captured_lpar_entry(uuid: str, name: str, state: str = "not activated") -> str:
    return f"""  <entry>
    <id>urn:uuid:{uuid}</id>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>{name}</PartitionName>
        <PartitionState>{state}</PartitionState>
        <PartitionMemoryConfiguration>
          <Metadata><Atom/></Metadata>
          <CurrentMaximumMemory>0</CurrentMaximumMemory>
          <CurrentMemory>0</CurrentMemory>
          <DesiredMemory>0</DesiredMemory>
          <MaximumMemory>0</MaximumMemory>
          <MinimumMemory>0</MinimumMemory>
          <RuntimeMemory>0</RuntimeMemory>
        </PartitionMemoryConfiguration>
        <PartitionProcessorConfiguration>
          <Metadata><Atom/></Metadata>
          <SharedProcessorConfiguration>
            <Metadata><Atom/></Metadata>
            <DesiredProcessingUnits>0</DesiredProcessingUnits>
            <DesiredVirtualProcessors>0</DesiredVirtualProcessors>
            <MaximumProcessingUnits>0</MaximumProcessingUnits>
            <UncappedWeight>128</UncappedWeight>
          </SharedProcessorConfiguration>
        </PartitionProcessorConfiguration>
      </LogicalPartition>
    </content>
  </entry>"""


def volume_group_with_repository(*, media: bool = True) -> str:
    """The captured VolumeGroup entry carrying the captured media repository (#1202).

    No captured VolumeGroup holds a repository: the one captured repository came in
    a VirtualIOServer feed, under its ``ViosStorage`` group. The reference places
    the same object in both reads (docs/refs/hmc-rest-api-p10/
    virtual-storage-management/215-virtual-media-repository.md:23-24), so it is
    spliced in ahead of the VolumeGroup's PhysicalVolumes. ``media=False`` drops
    the two VirtualOpticalMedia and leaves the OpticalMedia container holding only
    its Metadata, as every captured empty container does.
    """
    import re

    feed = live_fixture("rest-ms-vios-feed-media")["body"]
    found = re.search(r"<MediaRepositories\b.*?</MediaRepositories>", feed, re.DOTALL)
    assert found is not None
    repository = found.group(0)
    if not media:
        repository = re.sub(
            r"\s*<VirtualOpticalMedia\b.*?</VirtualOpticalMedia>",
            "",
            repository,
            flags=re.DOTALL,
        )
    entry = live_fixture("rest-volume-group")["body"]
    return entry.replace("<PhysicalVolumes ", f"{repository}\n    <PhysicalVolumes ", 1)
