"""Schema preparation reuse with fresh application authority and metadata."""

from __future__ import annotations

import asyncio
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier
from unittest.mock import patch

import pytest
from fastmcp import Client, Context
from fastmcp.exceptions import ValidationError as ToolValidationError
from fastmcp.tools.base import Tool
from fastmcp.tools.function_tool import FunctionTool

from hmcpctl._app import create_mcp as create_base_mcp
from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.server import TOOL_SECURITY, _gates, create_mcp
from hmcpctl.server_tools.command import configure_arbitrary_command_tool
from hmcpctl.tool_registry import (
    ToolDefinition,
    annotations_for,
    authorized,
    tool_module,
)


class Receiver:
    def __init__(self):
        self.tools = {}

    def add_tool(self, tool):
        self.tools[tool.name] = tool
        return tool


def probe(asynchronous=False, default=1):
    hits = []
    if asynchronous:

        async def handler(value: int = default) -> dict[str, int]:
            hits.append(value)
            return {"value": value}
    else:

        def handler(value: int = default) -> dict[str, int]:
            hits.append(value)
            return {"value": value}

    return handler, hits


def collect(handler, effect="read"):
    decorate, register, _security = tool_module()
    decorate(
        effect=effect,
        operation=f"probe.{effect}",
        target_kind="none",
        connection_argument=None,
    )(handler)
    return register


def bind(register, authorize=lambda *_args: None, permits=lambda _name: True):
    receiver = Receiver()
    register(receiver, permits=permits, authorize=authorize)
    return receiver


@pytest.fixture
def preparations(monkeypatch):
    calls = []
    original = Tool.from_function

    def measured(fn, **kwargs):
        calls.append(fn)
        return original(fn, **kwargs)

    monkeypatch.setattr(Tool, "from_function", measured)
    return calls


def test_sequential_registration_reuses_preparation(preparations):
    handler, _hits = probe()
    register = collect(handler)
    first, second = bind(register), bind(register)
    assert len(preparations) == 1
    assert first.tools["handler"] is not second.tools["handler"]
    assert first.tools["handler"].fn is not second.tools["handler"].fn


def test_ceiling_precedes_cold_and_warm_preparation(preparations):
    handler, _hits = probe()
    register = collect(handler)
    assert not bind(register, permits=lambda _: False).tools
    assert preparations == []
    bind(register)
    assert not bind(register, permits=lambda _: False).tools
    assert len(preparations) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_fresh_authorization_and_argument_validation(asynchronous):
    handler, hits = probe(asynchronous)
    register = collect(handler)
    seen = []

    def allow(*args):
        seen.append("allow")

    def deny(*args):
        seen.append("deny")
        raise PermissionError("synthetic policy refusal")

    first, second = bind(register, allow), bind(register, deny)

    async def invoke():
        await first.tools["handler"].run({"value": 2})
        assert hits == [2] and seen == ["allow"]
        with pytest.raises(PermissionError, match="synthetic policy refusal"):
            await second.tools["handler"].run({"value": 3})
        assert hits == [2] and seen == ["allow", "deny"]
        with pytest.raises(ToolValidationError):
            await first.tools["handler"].run({"value": "not-an-integer"})
        assert hits == [2] and seen == ["allow", "deny"]

    asyncio.run(invoke())


def test_schema_and_annotation_mutation_isolated():
    handler, _hits = probe()
    register = collect(handler)
    first = bind(register).tools["handler"]
    second = bind(register).tools["handler"]
    first.parameters["properties"]["value"]["default"] = 999
    first.output_schema["x-probe"] = {"values": ["changed"]}
    first.annotations = annotations_for("destructive")
    for other in [second, bind(register).tools["handler"]]:
        assert other.parameters["properties"]["value"]["default"] == 1
        assert "x-probe" not in other.output_schema
        assert other.annotations == annotations_for("read")
        assert other.parameters is not first.parameters


def test_new_definitions_do_not_reuse_by_name_or_handler(preparations):
    handler, _hits = probe()
    first = bind(collect(handler, "read")).tools["handler"]
    second = bind(collect(handler, "mutate")).tools["handler"]
    replacement, _hits = probe(default=2)
    third = bind(collect(replacement)).tools["handler"]
    assert len(preparations) == 3
    assert first.annotations == annotations_for("read")
    assert second.annotations == annotations_for("mutate")
    assert third.parameters["properties"]["value"]["default"] == 2


def test_preparation_failure_not_cached_or_registered(monkeypatch):
    handler, hits = probe()
    register = collect(handler)
    original = Tool.from_function
    attempts = []

    def once(fn, **kwargs):
        attempts.append(fn)
        if len(attempts) == 1:
            raise ValueError("synthetic preparation failure")
        return original(fn, **kwargs)

    monkeypatch.setattr(Tool, "from_function", once)
    failed = Receiver()
    with pytest.raises(ValueError, match="synthetic preparation failure"):
        register(failed, permits=lambda _: True, authorize=lambda *_: None)
    assert not failed.tools and not hits
    bind(register)
    bind(register)
    assert len(attempts) == 2


def test_copy_failure_does_not_register_or_execute(monkeypatch, preparations):
    handler, hits = probe()
    register = collect(handler)
    bind(register)
    original = FunctionTool.model_copy

    def broken(*args, **kwargs):
        raise ValueError("synthetic copy failure")

    monkeypatch.setattr(FunctionTool, "model_copy", broken)
    failed = Receiver()
    with pytest.raises(ValueError, match="synthetic copy failure"):
        register(failed, permits=lambda _: True, authorize=lambda *_: None)
    assert not failed.tools and not hits
    monkeypatch.setattr(FunctionTool, "model_copy", original)
    bind(register)
    assert len(preparations) == 1


def test_definition_lifetime_is_not_process_global():
    handler, _hits = probe()
    register = collect(handler)
    definition = weakref.ref(register.__self__.definitions[0])
    receiver = bind(register)
    del register
    gc.collect()
    assert definition() is None
    assert receiver.tools["handler"].fn is not handler


def test_concurrent_cold_bindings_are_isolated(preparations):
    handler, _hits = probe()
    register = collect(handler)
    ready = Barrier(4)

    def compose(_):
        ready.wait(timeout=30)
        return bind(register).tools["handler"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        tools = list(pool.map(compose, range(4)))
    assert 1 <= len(preparations) <= 4
    before = len(preparations)
    bind(register)
    assert len(preparations) == before
    assert len({id(tool.parameters) for tool in tools}) == 4
    assert len({id(tool.fn) for tool in tools}) == 4


def test_template_does_not_retain_authorizer_before_invocation():
    handler, _hits = probe()
    register = collect(handler)

    class Gate:
        def __call__(self, *args):
            return None

    gate = Gate()
    reference = weakref.ref(gate)
    receiver = bind(register, gate)
    del receiver, gate
    gc.collect()
    assert reference() is None


def test_sdk_context_injection_remains_supported():
    async def contextual(ctx: Context) -> dict[str, bool]:
        return {"context_present": isinstance(ctx, Context)}

    register = collect(contextual)
    application = create_base_mcp()
    register(application, permits=lambda _: True, authorize=lambda *_: None)

    async def invoke():
        async with Client(application) as client:
            response = await client.call_tool("contextual", {})
            assert response.data == {"context_present": True}

    asyncio.run(invoke())


def uncached(definition, authorize):
    return Tool.from_function(
        authorized(definition.name, definition.security, definition.handler, authorize),
        annotations=annotations_for(definition.security.effect),
    )


@pytest.mark.parametrize("selection", ["legacy", "narrow", "empty"])
def test_complete_wire_records_equal_uncached_reference(selection):
    if selection == "legacy":
        policy = compile_legacy_policy(
            TOOL_SECURITY, ("<default>",), include_arbitrary_command=True
        )
        expected = set(TOOL_SECURITY)
    else:
        names = ["hmc_get_system"] if selection == "narrow" else []
        grants = (
            [{"tools": names, "connections": ["<default>"], "targets": "all-targets"}]
            if names
            else []
        )
        policy = compile_access_policy(
            {"policies": {"test": {"grants": grants}}},
            "test",
            TOOL_SECURITY,
            "test-policy.toml",
        )
        expected = set(names)

    async def records(reference):
        with (
            patch.object(ToolDefinition, "_bind", uncached, create=True)
            if reference
            else nullcontext()
        ):
            application = create_mcp(policy)
        permits, authorize = _gates(policy)
        await configure_arbitrary_command_tool(
            True, application, permits=permits, authorize=authorize
        )
        async with Client(application) as client:
            tools = await client.list_tools()
        assert {tool.name for tool in tools} == expected
        return sorted(
            (tool.model_dump() for tool in tools), key=lambda row: row["name"]
        )

    async def compare():
        assert await records(False) == await records(True)

    asyncio.run(compare())
