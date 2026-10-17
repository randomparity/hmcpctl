"""Dispatch-boundary connection-scope authorization on a composed application.

Covers docs/workflow/specs/2026-08-19-connection-scope-dispatch-design.md; the
decision record is docs/adr/0038-dispatch-time-connection-scope.md.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import warnings
from types import SimpleNamespace

import pytest
from conftest import make_config
from fastmcp import Client
from fastmcp.exceptions import ToolError
from rendered_output import collapse_whitespace

from hmcpctl.audit import sink as audit_sink
from hmcpctl.authorization.access_policy import (
    DEFAULT_CONNECTION_TOKEN,
    compile_access_policy,
)
from hmcpctl.authorization.dispatch_scope import dispatch_authorizer
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools import command as server_command
from hmcpctl.server_tools.lpar import lifecycle as server_lpars
from hmcpctl.tool_registry import ToolSecurity, authorized

SOURCE = "test-access-policy.toml"

LAB_ONLY = [
    {
        "effects": ["read", "mutate", "destructive"],
        "connections": ["lab"],
        "targets": "all-targets",
    }
]


ESCAPE_HATCH_GRANT = [
    {"tools": ["hmc_run_command"], "connections": ["lab"], "targets": "all-targets"}
]


def _policy(grants: list[dict], name: str = "lab-only"):
    return compile_access_policy(
        {"policies": {name: {"grants": grants}}}, name, TOOL_SECURITY, SOURCE
    )


@pytest.fixture(autouse=True)
def lab_profile(tmp_path, monkeypatch):
    """A config.toml holding one profile, at the platform-native path.

    Autouse so no test in this module can accidentally read the developer's own
    configuration, and so ``lab`` normalizes to itself rather than denying.
    """
    from hmcpctl.config import config_dir

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.delenv("HMC_PROFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = config_dir() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[profiles.lab]\n"
        "host = 'lab-hmc.test'\n"
        "user = 'hscroot'\n"
        "password = 'lab-pass'\n"  # pragma: allowlist secret
        "\n"
        "[profiles.prod]\n"
        "host = 'prod-hmc.test'\n"
        "user = 'hscroot'\n"
        "password = 'prod-pass'\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    return path


def _call(application, tool: str, arguments: dict):
    async def _go():
        async with Client(application) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(_go())


def _is_guarded(tool) -> bool:
    """True when the registered callable is `authorized`'s wrapper.

    ``__wrapped__`` alone is a weak witness — any ``functools.wraps`` decorator
    sets it — so the code object's own name is checked too. ``functools.wraps``
    copies ``__name__`` and ``__qualname__`` from the handler but never
    ``__code__``, so this reads the wrapper's real identity.
    """
    return (
        getattr(tool.fn, "__wrapped__", None) is not None
        and getattr(tool.fn, "__code__", None) is not None
        and tool.fn.__code__.co_name == "guarded"
    )


def _delete_args(profile: str | None) -> dict:
    """The smallest valid `hmc_delete_lpar` call, aimed at *profile*."""
    return {
        "system_name_or_uuid": "sys-1",
        "lpar_name_or_uuid": "victim",
        "profile": profile,
    }


def _registered(application) -> dict:
    async def _go():
        return {
            tool.name: tool for tool in await application.local_provider.list_tools()
        }

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# R12 — a denied call performs no outbound operation
# ---------------------------------------------------------------------------


# Every name a handler could reach an HMC through, patched at *every* module
# that rebound it at import. Patching only `hmcpctl.config.build_config` proves
# nothing: `server_tools.vios.core`, `server_command`, and `_app` each hold their own
# reference, so a call through one of those would sail past an unpatched source
# module and the test would still be green.
_OUTBOUND_NAMES = ("build_config", "client_from_env", "run_hmc_cli", "run_hmc_command")


def _seal_every_outbound_path(monkeypatch, opened: list[str]):
    """Make any attempt to reach an HMC record itself and fail loudly."""
    import importlib
    import pkgutil

    import hmcpctl
    from hmcpctl.client import core as client_module

    def _forbidden(label):
        def _refuse(*args, **kwargs):
            opened.append(label)
            raise AssertionError(f"a denied call reached {label}")

        return _refuse

    monkeypatch.setattr(client_module.HMCClient, "__init__", _forbidden("HMCClient"))
    monkeypatch.setattr("httpx.AsyncClient.__init__", _forbidden("httpx.AsyncClient"))

    sealed = 0
    for info in pkgutil.walk_packages(hmcpctl.__path__, prefix="hmcpctl."):
        module = importlib.import_module(info.name)
        label = info.name.removeprefix("hmcpctl.")
        for name in _OUTBOUND_NAMES:
            if callable(getattr(module, name, None)):
                monkeypatch.setattr(module, name, _forbidden(f"{label}.{name}"))
                sealed += 1
    assert sealed > 10, f"only {sealed} outbound bindings sealed; the sweep missed"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        pytest.param(
            "hmc_delete_lpar",
            {"system_name_or_uuid": "sys-1", "lpar_name_or_uuid": "victim"},
            id="rest",
        ),
        pytest.param(
            "hmc_backup_vios",
            {
                "system_name_or_uuid": "sys-1",
                "vios_name_or_uuid": "vios-1",
                "backup_name": "backup",
            },
            id="ssh-passthrough",
        ),
    ],
)
def test_a_denied_call_never_opens_an_hmc_client(monkeypatch, tool_name, arguments):
    """The proof the issue asks for: assert on the constructors, not the error.

    Both transports, because the REST and SSH paths reach their connection
    through different module bindings.
    """
    opened: list[str] = []
    _seal_every_outbound_path(monkeypatch, opened)

    application = create_mcp(_policy(LAB_ONLY))
    with pytest.raises(ToolError) as error:
        _call(application, tool_name, {**arguments, "profile": "prod"})

    assert opened == []
    assert "is not permitted on connection 'prod'" in str(error.value)


def test_the_seal_itself_bites(monkeypatch):
    """A permitted call trips every wire the denial test asserts is untouched."""
    opened: list[str] = []
    _seal_every_outbound_path(monkeypatch, opened)

    application = create_mcp(_policy(LAB_ONLY))
    with pytest.raises(ToolError):
        _call(
            application,
            "hmc_delete_lpar",
            _delete_args("lab"),
        )

    assert opened


def test_a_permitted_call_reaches_the_handler(monkeypatch):
    """The mirror: the wrapper is a gate, not a wall."""
    reached: list[str | None] = []

    def _capture(profile=None, **overrides):
        reached.append(profile)
        raise RuntimeError("stop before any HMC request")

    monkeypatch.setattr("hmcpctl._app.client_from_env", _capture)

    application = create_mcp(_policy(LAB_ONLY))
    with pytest.raises(ToolError):
        _call(
            application,
            "hmc_delete_lpar",
            _delete_args("lab"),
        )

    assert reached == ["lab"]


def test_the_denial_reaches_the_client_as_a_tool_error():
    application = create_mcp(_policy(LAB_ONLY))
    with pytest.raises(ToolError) as error:
        _call(
            application,
            "hmc_delete_lpar",
            _delete_args("prod"),
        )
    message = str(error.value)
    assert "hmc_delete_lpar is not permitted on connection 'prod'" in message
    assert "lab-only" in message
    assert "prod-hmc.test" not in message
    assert "lab-pass" not in message


# ---------------------------------------------------------------------------
# R1, R17 — every connection-bearing registered tool carries the check
# ---------------------------------------------------------------------------


def test_every_connection_bearing_tool_is_wrapped_under_a_policy():
    """R17: the check cannot be skipped, asserted rather than assumed.

    Read after ``configure_arbitrary_command_tool`` so it covers the third
    registration site — the one that registers the arbitrary-command tool.
    """
    policy = _policy(LAB_ONLY + ESCAPE_HATCH_GRANT)
    application = create_mcp(policy)
    asyncio.run(
        server_command.configure_arbitrary_command_tool(
            True,
            application,
            permits=policy.permits_tool,
            authorize=dispatch_authorizer(policy),
        )
    )

    registered = _registered(application)
    assert "hmc_run_command" in registered
    connection_bearing = [
        name
        for name in registered
        if TOOL_SECURITY[name].connection_argument is not None
    ]
    assert connection_bearing
    for name in connection_bearing:
        assert _is_guarded(registered[name]), name

    # #297: a tool declaring no connection argument is wrapped too. The wrapper
    # carries the target dimension as well as the connection one, and whether a
    # `targets` table can bound a tool is a fact about its selectors rather than
    # about how it opens a connection.
    local_only = [
        name for name in registered if TOOL_SECURITY[name].connection_argument is None
    ]
    assert local_only
    for name in local_only:
        assert _is_guarded(registered[name]), name


def test_every_connection_bearing_tool_is_wrapped_under_the_legacy_policy():
    """R14, inverted by ADR 0041: legacy-equivalent is not legacy.

    This asserted that composing without a policy wrapped nothing. There is no such
    composition now, and the successor claim is the one an operator most needs: the
    policy that *grants* what the unpolicied server granted still authorizes — and
    audits — every call. It permits the same things; it does not behave the same way.
    """
    registered = _registered(create_mcp(_legacy()))

    assert registered
    bearing = [
        name
        for name in registered
        if TOOL_SECURITY[name].connection_argument is not None
    ]
    assert bearing
    for name in bearing:
        assert _is_guarded(registered[name]), name


# ---------------------------------------------------------------------------
# R3 — registration is signature-transparent
# ---------------------------------------------------------------------------


def test_the_wrapper_changes_no_tool_schema():
    unfiltered = _registered(create_mcp(_legacy()))
    guarded = _registered(create_mcp(_policy(LAB_ONLY)))

    assert set(guarded) == set(unfiltered)
    for name, tool in guarded.items():
        assert tool.name == unfiltered[name].name
        assert tool.description == unfiltered[name].description
        assert tool.parameters == unfiltered[name].parameters


def test_the_arbitrary_command_tool_keeps_its_schema_through_the_wrapper():
    policy = _policy(ESCAPE_HATCH_GRANT)

    def _compose(authorize):
        application = create_mcp(policy)
        asyncio.run(
            server_command.configure_arbitrary_command_tool(
                True, application, permits=policy.permits_tool, authorize=authorize
            )
        )
        return _registered(application)["hmc_run_command"]

    guarded = _compose(dispatch_authorizer(policy))
    plain = _compose(None)

    assert guarded.name == plain.name
    assert guarded.description == plain.description
    assert guarded.parameters == plain.parameters


# ---------------------------------------------------------------------------
# R5 — arguments are bound, not read out of kwargs
# ---------------------------------------------------------------------------


def _probe():
    seen: list[dict] = []

    def authorize(name, security, arguments):
        seen.append(dict(arguments))

    security = ToolSecurity(
        effect="mutate", operation="probe.run", target_kind="console"
    )

    def handler(system_name_or_uuid: str, profile: str | None = None) -> str:
        return "ran"

    return authorized("probe", security, handler, authorize), seen


def test_a_defaulted_selector_is_read_as_none():
    guarded, seen = _probe()
    assert guarded("sys-1") == "ran"
    assert seen == [{"system_name_or_uuid": "sys-1", "profile": None}]


def test_a_positional_selector_is_read():
    guarded, seen = _probe()
    assert guarded("sys-1", "lab") == "ran"
    assert seen == [{"system_name_or_uuid": "sys-1", "profile": "lab"}]


def test_a_malformed_call_fails_before_the_handler():
    """R5: bind rejects it, so the handler never runs and nothing is authorized."""
    guarded, seen = _probe()
    with pytest.raises(TypeError):
        guarded("sys-1", nonexistent=True)
    assert seen == []


# ---------------------------------------------------------------------------
# R15, R16 — independence, and the boundary the CLI and Python API sit outside
# ---------------------------------------------------------------------------


def test_compositions_authorize_independently(monkeypatch):
    reached: list[str | None] = []

    def _capture(profile=None, **overrides):
        reached.append(profile)
        raise RuntimeError("stop before any HMC request")

    monkeypatch.setattr("hmcpctl._app.client_from_env", _capture)

    restricted = create_mcp(_policy(LAB_ONLY))
    # A policy granting the connection the call selects, standing in for the
    # unpolicied composition this test used to build.
    unrestricted = create_mcp(
        compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN, "prod"))
    )

    with pytest.raises(ToolError):
        _call(
            restricted,
            "hmc_delete_lpar",
            _delete_args("prod"),
        )
    with pytest.raises(ToolError):
        _call(
            unrestricted,
            "hmc_delete_lpar",
            _delete_args("prod"),
        )

    # The unrestricted application ran the handler; the restricted one did not.
    assert reached == ["prod"]


def test_the_module_level_handler_is_never_wrapped():
    """R16: the CLI and the ADR 0029 Python API reach the unwrapped function."""
    create_mcp(_policy(LAB_ONLY))

    assert getattr(server_lpars.hmc_delete_lpar, "__wrapped__", None) is None
    assert getattr(server_command.hmc_run_command, "__wrapped__", None) is None


# ---------------------------------------------------------------------------
# R17 — the served path, which is the only path a deployment takes
# ---------------------------------------------------------------------------


def _legacy(*, include_arbitrary_command: bool = False):
    """The policy that replaced the unfiltered default composition (ADR 0041)."""
    return compile_legacy_policy(
        TOOL_SECURITY,
        (DEFAULT_CONNECTION_TOKEN,),
        include_arbitrary_command=include_arbitrary_command,
    )


def _serve(policy, *, enable_arbitrary_command=True):
    """Compose exactly as `hmcpctl serve --access-policy NAME` composes."""
    from hmcpctl import server

    return server._serve_application(enable_arbitrary_command, policy)


def test_the_served_application_wraps_every_tool():
    """The gate must be on the application `serve` builds, not one a test builds.

    Asserting against a self-composed application cannot observe whether
    `_serve_application` threads the authorizer at all, which leaves the
    arbitrary-command registration site — the highest-risk one — unpinned.

    "every tool" since #297, connection-bearing or not: an exemption here was the
    hole that let a `targets` table permit the two local-only tools.
    """
    application = _serve(_policy(LAB_ONLY + ESCAPE_HATCH_GRANT))
    registered = _registered(application)

    assert "hmc_run_command" in registered
    assert any(TOOL_SECURITY[name].connection_argument is None for name in registered)
    for name, tool in registered.items():
        assert _is_guarded(tool), name


def test_the_served_escape_hatch_denies_a_withheld_connection(monkeypatch):
    """`hmc_run_command` runs an arbitrary HMC CLI command; it must be scoped."""
    opened: list[str] = []
    _seal_every_outbound_path(monkeypatch, opened)
    application = _serve(_policy(LAB_ONLY + ESCAPE_HATCH_GRANT))
    # Serve startup now resolves the effective ownership guard once per reachable
    # connection (#533). This assertion owns the denied dispatch, not bootstrap.
    opened.clear()

    with pytest.raises(ToolError) as error:
        _call(application, "hmc_run_command", {"cmd": "lssyscfg", "profile": "prod"})
    assert "hmc_run_command is not permitted on connection 'prod'" in str(error.value)
    assert opened == []


def test_an_unrelated_wraps_decorator_is_not_mistaken_for_the_guard():
    """`__wrapped__` alone is a weak witness, which is why `_is_guarded` reads more."""
    import functools

    def handler(profile: str | None = None) -> str:
        return "ran"

    @functools.wraps(handler)
    def decorated(*args, **kwargs):
        return handler(*args, **kwargs)

    assert getattr(decorated, "__wrapped__", None) is not None
    assert not _is_guarded(SimpleNamespace(fn=decorated))
    assert _is_guarded(
        SimpleNamespace(
            fn=authorized(
                "probe",
                ToolSecurity(
                    effect="mutate", operation="probe.run", target_kind="console"
                ),
                handler,
                lambda *a: None,
            )
        )
    )


def test_the_served_application_wraps_the_escape_hatch_too():
    """R14 on the served path, inverted by ADR 0041.

    This asserted that `_serve_application(_, None)` registered `hmc_run_command`
    unwrapped. That path no longer exists — the transports take a required policy —
    and the property worth pinning in its place is that the highest-risk registration
    site is gated like every other: opting the escape hatch into the grant registers
    it, and it arrives wrapped.
    """
    registered = _registered(_serve(_legacy(include_arbitrary_command=True)))

    assert "hmc_run_command" in registered
    assert _is_guarded(registered["hmc_run_command"])


def test_an_unreadable_configuration_leaks_no_path_to_the_mcp_client(lab_profile):
    """R10/R13 through the transport, not only at the function boundary.

    Whether `__cause__` reaches the client is fastmcp's decision, so a dependency
    bump is exactly what could turn this property off without any code change.
    """
    lab_profile.write_text("profiles = 'not-a-table'\n", encoding="utf-8")

    application = create_mcp(_policy(LAB_ONLY))
    with pytest.raises(ToolError) as error:
        _call(
            application,
            "hmc_delete_lpar",
            _delete_args("lab"),
        )
    message = str(error.value)
    assert "the configured HMC connections could not be read" in message
    assert str(lab_profile) not in message
    assert str(lab_profile.parent) not in message
    assert "not-a-table" not in message


def test_the_permissions_site_routes_through_the_shared_helper(monkeypatch):
    """R1 at the one site whose wrap is inert, so nothing else could observe it.

    `hmc_effective_permissions` declares no connection argument, so `authorized`
    provably returns it unchanged — which means only the call itself witnesses
    that this site honours the same contract as the other two rather than
    deciding for itself.
    """
    from hmcpctl.server_tools import permissions as server_permissions

    calls: list[tuple] = []
    real = server_permissions.authorized

    def _spy(name, security, handler, authorize):
        calls.append((name, security, authorize))
        return real(name, security, handler, authorize)

    monkeypatch.setattr(server_permissions, "authorized", _spy)

    policy = _policy(LAB_ONLY)
    create_mcp(policy)

    assert len(calls) == 1
    name, security, authorize = calls[0]
    assert name == server_permissions.TOOL_NAME
    assert security is server_permissions.EFFECTIVE_PERMISSIONS_SECURITY
    assert authorize is not None


# ---------------------------------------------------------------------------
# #267 — a denial is a record, not a traceback panel (ADR 0046)
# ---------------------------------------------------------------------------

# The grant a target denial needs: the connection matches, the named lpar does not.
LAB_ONE_LPAR = [
    {
        "tools": ["hmc_power_on_lpar"],
        "connections": ["lab"],
        # #259: hmc_power_on_lpar now declares a managed_system selector, so
        # the table must cover it for the grant to load at all.
        "targets": {"lpar": ["scratch-01"], "managed_system": ["sys-1"]},
    }
]

# ADR 0038's closed denial templates as the client receives them, wrapped in the
# prefix fastmcp adds. Byte-for-byte, because #267's fix must not move the
# client-facing contract by so much as a character.
CONNECTION_DENIAL = (
    "Error calling tool 'hmc_power_on_lpar': hmc_power_on_lpar is not permitted "
    "on connection 'other' by access policy 'lab-only'. Grant that connection in "
    "a policy grant that already names hmc_power_on_lpar, or call "
    "hmc_power_on_lpar with a connection the policy grants."
)
TARGET_DENIAL = (
    "Error calling tool 'hmc_power_on_lpar': hmc_power_on_lpar is not permitted "
    "on lpar='lp-1', managed_system='sys-1' by access policy 'lab-only'. No grant "
    "naming hmc_power_on_lpar allows that combination of targets. Grant them in a "
    "policy grant that already names hmc_power_on_lpar, or call hmc_power_on_lpar "
    "with targets the policy grants."
)


@pytest.fixture
def denial_filter():
    """Install the ADR 0046 filter for one test and take it off again.

    It lives on a process-global logger that belongs to fastmcp, so a test that
    left it there would decide what every later test sees on stderr.
    """
    from hmcpctl.server import install_denial_log_filter

    logger = logging.getLogger("fastmcp.server.server")
    saved = list(logger.filters)
    install_denial_log_filter()
    try:
        yield
    finally:
        logger.filters[:] = saved


def _stderr(capsys) -> str:
    """Whitespace-normalized stderr, drained first.

    Two reasons not to read ``capsys`` raw. The audit sink writes on a daemon
    thread (ADR 0043), so without the drain this reads whatever happened to have
    arrived. And fastmcp's ``RichHandler`` hard-wraps its line to the console
    width, so an assertion against the unnormalized text asserts against the
    terminal size of whoever ran it.
    """

    assert audit_sink._sink().drain(audit_sink._DRAIN_TIMEOUT), (
        "the sink must settle, not stall"
    )
    return collapse_whitespace(capsys.readouterr().err)


def _denied(application, profile: str) -> str:
    """Drive one denied `hmc_power_on_lpar` call and return the client's message."""
    with pytest.raises(ToolError) as error:
        _call(
            application,
            "hmc_power_on_lpar",
            {
                "lpar_name_or_uuid": "lp-1",
                "system_name_or_uuid": "sys-1",
                "profile": profile,
            },
        )
    return str(error.value)


@pytest.mark.parametrize(
    ("grants", "profile", "expected"),
    [
        pytest.param(LAB_ONLY, "other", CONNECTION_DENIAL, id="connection-scope"),
        pytest.param(LAB_ONE_LPAR, "lab", TARGET_DENIAL, id="target-scope"),
    ],
)
def test_a_denial_writes_one_line_and_leaves_the_client_message_alone(
    denial_filter, capsys, grants, profile, expected
):
    """#267 on both denial shapes: no panel on stderr, no change to the client.

    Both are asserted in one test on purpose — the whole risk of this fix is that
    quieting the server also quiets the client, and separating them would let a
    regression pass half the pair.

    ``TargetScopeError`` is exercised here rather than assumed to behave like
    ``ConnectionScopeError``: PR #307 made the target dimension bind tools
    declaring no connection argument, so it reaches this boundary on paths it did
    not before.
    """
    assert _denied(create_mcp(_policy(grants)), profile) == expected

    captured = _stderr(capsys)
    assert "authorization denied" in captured
    assert "Traceback" not in captured
    assert "ConnectionScopeError" not in captured
    assert "TargetScopeError" not in captured


@pytest.mark.parametrize(
    ("grants", "profile", "expected"),
    [
        pytest.param(LAB_ONLY, "other", CONNECTION_DENIAL, id="connection-scope"),
        pytest.param(LAB_ONE_LPAR, "lab", TARGET_DENIAL, id="target-scope"),
    ],
)
def test_the_client_denial_template_is_the_same_with_the_filter_off(
    grants, profile, expected
):
    """The other half of the pin: the filter is the only thing that changed.

    Without this, the test above would still pass if the fix had rewritten the
    template and the constant had been updated to match it.
    """
    assert _denied(create_mcp(_policy(grants)), profile) == expected


#: The logger ADR 0051's handler goes on. Restoring it between tests is
#: ``isolate_audit_logging``'s job (tests/conftest.py) — ``_serve_application``
#: replaces its handlers process-wide, so isolating it per-module would leave
#: every other module that serves leaking into this one.
FASTMCP_LOGGER = logging.getLogger("fastmcp")


def _targets_stderr(handler) -> bool:
    """Whether *handler* writes to the process's stderr.

    Reads both shapes FastMCP's ``configure_logging`` can install: a
    ``logging.StreamHandler``'s ``stream``, and the ``rich`` ``Console`` a
    ``RichHandler`` renders through, whose ``file`` resolves ``sys.stderr`` at
    access time when it was built with ``stderr=True``.
    """
    stream = getattr(handler, "stream", None)
    if stream is None:
        stream = getattr(getattr(handler, "console", None), "file", None)
    return stream is sys.stderr


def test_the_served_path_takes_fastmcps_handlers_off_fd_2(capsys):
    """#323 criterion 1, asserted on the handler set rather than on output.

    The logger is first put back into the state importing ``fastmcp`` leaves it
    in, so the assertion does not depend on whether an earlier test in the
    session already served. That reconstruction is also the test's premise: it
    fails loudly if a future ``fastmcp`` stops installing a writer on fd 2, which
    would make the rest of this pass vacuously.
    """
    from fastmcp.utilities.logging import configure_logging

    FASTMCP_LOGGER.handlers[:] = []
    configure_logging(level="INFO")
    assert any(_targets_stderr(each) for each in FASTMCP_LOGGER.handlers), (
        "fastmcp must have installed a writer on fd 2 for this to be removing one"
    )

    _serve(_policy(LAB_ONLY))

    assert len(FASTMCP_LOGGER.handlers) == 1
    assert not any(_targets_stderr(each) for each in FASTMCP_LOGGER.handlers)
    assert FASTMCP_LOGGER.handlers[0].formatter is not None

    # And the records still reach stderr — through the sink, which is why this
    # has to drain before reading.
    logging.getLogger("fastmcp.server.server").warning("a fastmcp line")
    assert "a fastmcp line" in _stderr(capsys)


#: Every external logger the served path binds to ADR 0043's sink: the original
#: ``fastmcp`` takeover, #330's three namespaces, and #550's warning bridge.
SUNK_LOGGERS = ("fastmcp", "uvicorn", "uvicorn.access", "mcp", "py.warnings")


def test_installing_the_sink_twice_leaves_one_handler_per_logger():
    """Idempotence, which the remove-then-add shape gives rather than a type check."""
    from hmcpctl.server import install_third_party_stderr_sinks

    install_third_party_stderr_sinks()
    install_third_party_stderr_sinks()

    for name in SUNK_LOGGERS:
        assert len(logging.getLogger(name).handlers) == 1


def test_served_warnings_use_the_bounded_escaped_sink(capsys, monkeypatch):
    """#550: capture replaces the default fd-2 writer on the served path."""
    called = []
    monkeypatch.setattr(
        warnings, "showwarning", lambda *args, **kwargs: called.append(args)
    )

    _serve(_policy(LAB_ONLY))
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("unsafe\x1bwarning", UserWarning)

    captured = _stderr(capsys)
    assert called == []
    assert "py.warnings: WARNING:" in captured
    assert "unsafe\\u001bwarning" in captured
    assert "\x1b" not in captured


def test_library_warnings_keep_the_default_showwarning(monkeypatch):
    """#550: importing and composing without serving do not capture warnings."""
    called = []
    monkeypatch.setattr(
        warnings, "showwarning", lambda *args, **kwargs: called.append(args)
    )

    create_mcp(_policy(LAB_ONLY))
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("library warning", UserWarning)

    assert len(called) == 1


def test_warning_capture_can_be_reinstalled_after_test_isolation(capsys, monkeypatch):
    """#550: disabling capture clears logging's saved-callback sentinel."""
    first = []

    def first_callback(*args, **kwargs):
        first.append(args)

    monkeypatch.setattr(warnings, "showwarning", first_callback)
    _serve(_policy(LAB_ONLY))
    warnings.warn("first served warning", UserWarning)
    assert warnings.showwarning is not first_callback

    logging.captureWarnings(False)
    second = []

    def second_callback(*args, **kwargs):
        second.append(args)

    monkeypatch.setattr(warnings, "showwarning", second_callback)
    _serve(_policy(LAB_ONLY))
    warnings.warn("second served warning", UserWarning)

    assert warnings.showwarning is not second_callback
    assert first == []
    assert second == []
    captured = _stderr(capsys)
    assert "first served warning" in captured
    assert "second served warning" in captured


def test_the_sink_is_installed_even_when_fastmcp_logging_is_disabled(
    capsys, monkeypatch
):
    """#323 criterion 4. ``settings.log_enabled`` false is not a reason to skip.

    That setting gates ``_configure_logging`` at import of ``fastmcp``, so
    "disabled" is exactly "the logger has no handler" — reconstructed here rather
    than mocked, because the gate has already run by the time any test starts.

    Installing anyway is the choice, and it closes a case rather than being a
    no-op: with no handler anywhere above it a record falls through to
    ``logging.lastResort``, which writes to fd 2 synchronously and unbounded.
    """
    import fastmcp

    from hmcpctl.server import install_third_party_stderr_sinks

    monkeypatch.setattr(fastmcp.settings, "log_enabled", False)
    FASTMCP_LOGGER.handlers[:] = []
    FASTMCP_LOGGER.setLevel(logging.NOTSET)

    install_third_party_stderr_sinks()

    assert len(FASTMCP_LOGGER.handlers) == 1
    assert not any(_targets_stderr(each) for each in FASTMCP_LOGGER.handlers)
    assert logging.lastResort not in FASTMCP_LOGGER.handlers

    logging.getLogger("fastmcp.server.server").warning("a line with logging disabled")
    assert "a line with logging disabled" in _stderr(capsys)


def _formatter_prefix(handler):
    """The column-0 marker a ``StreamSafeFormatter`` stamps on every physical line."""
    return getattr(getattr(handler, "formatter", None), "_prefix", None)


def test_the_served_path_binds_every_third_party_logger_to_the_sink():
    """#330 acceptance 1+2: no unbounded writer survives on any bound namespace.

    Asserted on the handler sets rather than output: exactly one handler per
    logger, none of them writing to ``sys.stderr`` directly, each rendering through
    the marked formatter under its producer's own prefix.
    """
    _serve(_policy(LAB_ONLY))

    for name in SUNK_LOGGERS:
        logger = logging.getLogger(name)
        assert len(logger.handlers) == 1, name
        handler = logger.handlers[0]
        assert not _targets_stderr(handler), name
        assert _formatter_prefix(handler) == f"{name}: ", name


def test_the_uvicorn_pair_matches_its_own_configuration_levels_and_propagation():
    """#330 review finding, pinned: levels and propagation move with the handlers.

    With ``dictConfig`` skipped nothing would hold ``uvicorn``/``uvicorn.access``
    at INFO or stop access records propagating to the parent handler — the access
    log would vanish below root's WARNING, then double-render once raised. Both
    loggers start here from their pristine NOTSET/propagating state.
    """
    from hmcpctl.server import install_third_party_stderr_sinks

    uv = logging.getLogger("uvicorn")
    access = logging.getLogger("uvicorn.access")
    uv.handlers[:] = []
    access.handlers[:] = []
    uv.setLevel(logging.NOTSET)
    access.setLevel(logging.NOTSET)
    uv.propagate = True
    access.propagate = True

    install_third_party_stderr_sinks()

    assert uv.level == logging.INFO
    assert access.level == logging.INFO
    assert uv.propagate is False
    assert access.propagate is False


def test_the_install_leaves_fastmcp_and_mcp_levels_and_propagation_alone():
    """ADR 0051's only-the-handlers rule still holds where nothing requires more."""
    from hmcpctl.server import install_third_party_stderr_sinks

    fastmcp_logger = logging.getLogger("fastmcp")
    mcp_logger = logging.getLogger("mcp")
    # Sentinel configuration the install must survive untouched: if it copied the
    # uvicorn pair's treatment onto these, either assert below would fail.
    fastmcp_logger.setLevel(logging.DEBUG)
    fastmcp_logger.propagate = False
    mcp_logger.setLevel(logging.DEBUG)
    mcp_logger.propagate = True

    try:
        install_third_party_stderr_sinks()
        assert fastmcp_logger.level == logging.DEBUG
        assert fastmcp_logger.propagate is False
        assert mcp_logger.level == logging.DEBUG
        assert mcp_logger.propagate is True
    finally:
        for logger in (fastmcp_logger, mcp_logger):
            logger.setLevel(logging.NOTSET)


def test_an_access_record_renders_exactly_once_through_the_sink(capsys):
    """The double-render trap the propagation change closes, asserted on output."""
    _serve(_policy(LAB_ONLY))

    logging.getLogger("uvicorn.access").info('1.2.3.4:5 - "GET /mcp HTTP/1.1" 200')

    captured = _stderr(capsys)
    assert captured.count('"GET /mcp HTTP/1.1"') == 1
    assert "uvicorn.access: " in captured


def test_a_uvicorn_error_record_rides_propagation_to_the_bound_handler(capsys):
    """:code:`uvicorn.error` is not itself bound; its records reach the sink only
    through default propagation, and this pins that they arrive once, prefixed by
    the parent namespace."""
    _serve(_policy(LAB_ONLY))

    logging.getLogger("uvicorn.error").info("Uvicorn running on http://127.0.0.1:8321")

    captured = _stderr(capsys)
    assert "uvicorn: INFO: Uvicorn running" in captured
    assert captured.count("Uvicorn running") == 1


def test_the_http_serve_path_constructs_uvicorn_without_its_default_logging(
    monkeypatch,
):
    """#330 acceptance 1 through the real entry point: ``main_http`` builds its
    ``uvicorn.Config`` with ``log_config=None``, so uvicorn's ``configure_logging``
    attaches no default ``StreamHandler`` and the sink binding, levels and
    propagation the install left survive Config construction and ``Server``
    construction. ``Server.serve`` is stubbed to return immediately — everything
    this asserts happens before it."""
    import uvicorn

    from hmcpctl import server

    async def _stop_immediately(self, sockets=None):
        return

    monkeypatch.setattr(uvicorn.Server, "serve", _stop_immediately)
    server.main_http(_policy(LAB_ONLY), host="127.0.0.1", port=0)

    for name in ("uvicorn", "uvicorn.access"):
        logger = logging.getLogger(name)
        assert len(logger.handlers) == 1, name
        assert not _targets_stderr(logger.handlers[0]), name
        assert logger.level == logging.INFO, name
        assert logger.propagate is False, name


def test_an_mcp_warning_record_reaches_stderr_through_the_sink(capsys):
    """#330 acceptance 2, stdio transport: WARNING is the floor because ``mcp``
    stays handlers-only at NOTSET and inherits root's effective WARNING."""
    _serve(_policy(LAB_ONLY))

    logging.getLogger("mcp").warning("lowlevel cache miss")

    assert "mcp: WARNING: lowlevel cache miss" in _stderr(capsys)


def test_a_denial_is_one_line_through_the_sink(denial_filter, capsys):
    """#323 criterion 3: ADR 0046's concise line survives ADR 0051's rerouting.

    The filter and the handler are both installed here on purpose. They solve
    different problems and #323 keeps both, so the pinned behaviour is the one an
    operator of a served process actually gets.
    """
    from hmcpctl.server import install_third_party_stderr_sinks

    install_third_party_stderr_sinks()

    assert _denied(create_mcp(_policy(LAB_ONLY)), "other") == CONNECTION_DENIAL

    captured = _stderr(capsys)
    assert "authorization denied" in captured
    assert "Traceback" not in captured
    assert "ConnectionScopeError" not in captured


def test_a_handler_bug_keeps_its_traceback_through_the_sink(denial_filter, capsys):
    """#323 criterion 2, which is ADR 0046's criterion carried over the reroute.

    A ``logging.Handler`` renders ``exc_info`` only when a ``Formatter`` is
    installed on it, so this is the test that fails if the sink-backed handler
    ships without one — the exact way this change could quietly undo #267.
    """
    from fastmcp import FastMCP

    from hmcpctl.server import install_third_party_stderr_sinks

    install_third_party_stderr_sinks()
    application = FastMCP("fastmcp-sink-probe")

    @application.tool
    def explode() -> str:
        raise RuntimeError("a handler bug, not a denial")

    with pytest.raises(ToolError):
        _call(application, "explode", {})

    captured = _stderr(capsys)
    assert "Traceback" in captured
    assert "RuntimeError" in captured
    assert "a handler bug, not a denial" in captured
    assert "authorization denied" not in captured


def test_a_hostile_tool_error_cannot_forge_an_audit_record(denial_filter, capsys):
    """#323, through the wiring rather than the formatter alone.

    A handler raising text that contains a newline and a JSON object is the shape
    an HMC-returned `validation.error` reaches this boundary in (ADR 0042's threat
    model). Before ADR 0051 `rich` wrapped it out of column 0 by accident; after
    it, `StreamSafeFormatter` has to be the thing that keeps it there. Driven
    through `install_third_party_stderr_sinks` so swapping the formatter back to a
    plain `logging.Formatter` reddens this.
    """
    import json

    from fastmcp import FastMCP

    from hmcpctl.server import install_third_party_stderr_sinks

    forged = '{"time": "2026-01-01T00:00:00+00:00", "event": "authorization"}'
    install_third_party_stderr_sinks()
    application = FastMCP("forgery-probe")

    @application.tool
    def hostile() -> str:
        raise RuntimeError(f"hmc said:\n{forged}\nend")

    with pytest.raises(ToolError):
        _call(application, "hostile", {})

    assert audit_sink._sink().drain(audit_sink._DRAIN_TIMEOUT), (
        "the sink must settle, not stall"
    )
    err = capsys.readouterr().err
    for line in err.splitlines():
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        assert not (isinstance(candidate, dict) and "event" in candidate), (
            f"a tool error forged a parseable audit record: {line!r}"
        )
    assert "hmc said" in err, "the text must still reach the operator"


PACKAGE_LOGGER = logging.getLogger("hmcpctl")


def _handlers_the_walk_finds(name: str) -> list[logging.Handler]:
    """Reproduce ``Logger.callHandlers``' ancestor walk for *name*.

    The defect #534 names is invisible to a handler-set assertion on one logger:
    ``callHandlers`` consults ``logging.lastResort`` when the *whole walk* finds
    zero handlers, so the only faithful pin is the walk itself.
    """
    found: list[logging.Handler] = []
    logger: logging.Logger | None = logging.getLogger(name)
    while logger is not None:
        found.extend(logger.handlers)
        logger = logger.parent if logger.propagate else None
    return found


def test_the_served_path_binds_the_package_namespace_to_the_sink():
    """#534: ``hmcpctl.*`` is a producer on ADR 0043's queue like the rest.

    One handler, none of it on fd 2 directly, rendering under this package's own
    producer prefix — the same three properties #330 pins for the third-party set.
    """
    _serve(_policy(LAB_ONLY))

    assert len(PACKAGE_LOGGER.handlers) == 1
    handler = PACKAGE_LOGGER.handlers[0]
    assert not _targets_stderr(handler)
    assert _formatter_prefix(handler) == "hmcpctl: "


def test_last_resort_is_unreachable_for_a_non_audit_package_logger(capsys):
    """#534's acceptance, on the mechanism rather than on a symptom.

    ``hmcpctl.server_permissions`` is the producer #470 added, and before this
    change its walk found nothing: no handler on itself, none on ``hmcpctl``, none
    on root, so the record went to ``logging.lastResort`` — synchronous, unbounded,
    and unprefixed. Root is cleared first so a stray handler cannot make the walk
    succeed for a reason this change did not supply.
    """
    logging.root.handlers[:] = []

    _serve(_policy(LAB_ONLY))

    found = _handlers_the_walk_finds("hmcpctl.server_permissions")
    assert found, "the walk must find a handler, or lastResort writes the record"
    assert logging.lastResort not in found
    assert not any(_targets_stderr(each) for each in found)

    logging.getLogger("hmcpctl.server_permissions").warning("an unresolved profile")

    assert "hmcpctl: WARNING: an unresolved profile" in _stderr(capsys)


def test_a_config_warning_reaches_stderr_only_through_the_sink(capsys):
    """The other producer #534 names, driven through the code that emits it.

    The bounded package logger owns the diagnostic; Python warnings must not add a
    synchronous write to fd 2 beside it.
    """
    _serve(_policy(LAB_ONLY))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        make_config(agent_id="agent-1", audit_memento="custom")

    captured = _stderr(capsys)
    assert "hmcpctl: WARNING: HMC_AGENT_ID is set" in captured


def test_the_package_binding_leaves_the_audit_stream_unprefixed(capsys):
    """#534's one carve-out: ``hmcpctl.audit`` keeps its own contract.

    Its ``propagate = False`` is what keeps the parent handler off the audit
    stream, so the record stays bare one-line JSON and arrives exactly once.
    """
    _serve(_policy(LAB_ONLY))

    audit_logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    assert audit_logger.propagate is False
    assert len(audit_logger.handlers) == 1
    assert audit_logger.handlers[0].formatter is None

    audit_logger.warning('{"event": "authorization", "outcome": "denied"}')

    captured = _stderr(capsys)
    assert "hmcpctl: " not in captured
    assert captured.count('"event": "authorization"') == 1


def test_installing_the_package_sink_twice_leaves_one_handler():
    """Idempotence, from the same defer-to-what-is-there rule the audit sink uses."""
    from hmcpctl.server import install_package_stderr_sink

    install_package_stderr_sink()
    install_package_stderr_sink()

    assert len(PACKAGE_LOGGER.handlers) == 1


def test_the_package_binding_leaves_an_ancestor_handler_receiving(capsys):
    """The binding adds a destination; it does not take one away.

    Unlike ``install_audit_sink`` this leaves ``propagate`` alone, so an operator
    who routes `hmcpctl.*` into their own centralized logging still gets these
    records after a serve — the alternative loses them silently, including under
    `--http`, where the stdout hazard ADR 0040 names does not even exist. Pinned
    against a root handler because that is the shape an operator's `basicConfig`
    leaves, and it is what would have gone quiet.
    """
    received: list[str] = []
    catcher = logging.Handler()
    catcher.emit = lambda record: received.append(record.getMessage())  # type: ignore[method-assign]
    logging.root.handlers[:] = [catcher]

    _serve(_policy(LAB_ONLY))
    logging.getLogger("hmcpctl.config").warning("a package warning")

    assert received == ["a package warning"]
    assert "hmcpctl: WARNING: a package warning" in _stderr(capsys)
    assert logging.getLogger("hmcpctl").propagate is True


def test_an_unexpected_handler_error_still_renders_its_traceback(denial_filter, capsys):
    """The criterion that stops #267's fix becoming a debuggability regression.

    A handler bug is not an authorization outcome, and its panel is the only
    server-side record of it — fastmcp's own line names the tool and nothing
    else. Driven through a throwaway application because no shipped tool can be
    made to raise a non-scope error at this boundary without stubbing one.
    """
    from fastmcp import FastMCP

    application = FastMCP("denial-filter-probe")

    @application.tool
    def explode() -> str:
        raise RuntimeError("a handler bug, not a denial")

    with pytest.raises(ToolError):
        _call(application, "explode", {})

    captured = _stderr(capsys)
    assert "Traceback" in captured
    assert "RuntimeError" in captured
    assert "authorization denied" not in captured
