"""Structured-concurrency contracts for composite operations."""

from __future__ import annotations

import asyncio

import pytest

from hmcpctl.errors import HMCTLSFingerprintError
from hmcpctl.operations.inventory import composite
from hmcpctl.operations.inventory.composite import (
    _fetch_lpar_data,
    _fetch_system_summary_data,
)


class _FailingCompositeClient:
    async def get_logical_partition(self, _uuid: str):
        raise RuntimeError("primary request failed")

    async def list_child(self, *_args):
        raise AssertionError("child endpoint must not be called")


@pytest.mark.asyncio
async def test_parent_failure_prevents_child_fetch():
    with pytest.raises(RuntimeError, match="primary request failed"):
        await _fetch_lpar_data(_FailingCompositeClient(), "lpar-uuid")


class _MissingParentClient:
    async def list_system_summaries(self):
        return []

    async def get_logical_partition(self, _uuid: str):
        return None

    async def list_child(self, *_args):
        raise RuntimeError("child endpoint must not be called")

    async def get_managed_system(self, _uuid: str):
        return None

    async def list_logical_partitions(self, _uuid: str):
        raise RuntimeError("child endpoint must not be called")

    async def list_vios(self, _uuid: str):
        raise RuntimeError("child endpoint must not be called")


@pytest.mark.asyncio
async def test_missing_lpar_is_reported_before_child_fetch():
    with pytest.raises(ValueError, match="LPAR 'lpar-uuid' not found"):
        await _fetch_lpar_data(_MissingParentClient(), "lpar-uuid")


@pytest.mark.asyncio
async def test_lpar_fetch_returns_parent_and_adapter_inventory():
    parent = {"UUID": "lpar-uuid"}
    adapters = [{"UUID": "adapter-uuid"}]

    class Client:
        async def get_logical_partition(self, uuid: str):
            assert uuid == "lpar-uuid"
            return parent

        async def list_child(self, resource: str, uuid: str, child: str):
            assert (resource, uuid, child) == (
                "LogicalPartition",
                "lpar-uuid",
                "ClientNetworkAdapter",
            )
            return adapters

    assert await _fetch_lpar_data(Client(), "lpar-uuid") == (parent, adapters)


@pytest.mark.asyncio
async def test_missing_system_is_reported_before_child_fetch():
    with pytest.raises(ValueError, match="Managed system 'system-uuid' not found"):
        await _fetch_system_summary_data(_MissingParentClient(), "system-uuid")


@pytest.mark.parametrize("feed", ["lpars", "vios"])
@pytest.mark.asyncio
async def test_pin_mismatch_aborts_composite_summary(monkeypatch, feed):
    failure = HMCTLSFingerprintError("pin mismatch")

    async def parent(*_):
        return {"UUID": "system-uuid"}

    class Client:
        async def list_logical_partitions(self, _):
            if feed == "lpars":
                raise failure
            return []

        async def list_vios(self, _):
            if feed == "vios":
                raise failure
            return []

    monkeypatch.setattr(composite, "operating_system_entry", parent)
    with pytest.raises(HMCTLSFingerprintError) as caught:
        await _fetch_system_summary_data(Client(), "system-uuid")
    assert caught.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("feed", ["lpars", "vios"])
async def test_summary_identity_failure_joins_cancelled_sibling(monkeypatch, feed):
    started, cancelled = asyncio.Event(), asyncio.Event()
    failure = HMCTLSFingerprintError("certificate identity differs")

    async def parent(*_):
        return {"UUID": "system-uuid"}

    async def fail():
        await started.wait()
        raise failure

    async def sibling():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class Client:
        async def list_logical_partitions(self, _):
            return await (fail() if feed == "lpars" else sibling())

        async def list_vios(self, _):
            return await (fail() if feed == "vios" else sibling())

    monkeypatch.setattr(composite, "operating_system_entry", parent)
    async with asyncio.timeout(2):
        with pytest.raises(HMCTLSFingerprintError) as caught:
            await _fetch_system_summary_data(Client(), "system-uuid")
    assert caught.value is failure
    assert str(caught.value) == "certificate identity differs"
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_failure", [False, True])
async def test_summary_keeps_other_task_failures_in_the_exception_chain(
    monkeypatch, identity_failure
):
    failure = HMCTLSFingerprintError("certificate identity differs")
    defect = RuntimeError("unexpected defect")

    async def parent(*_):
        return {"UUID": "system-uuid"}

    class Client:
        async def list_logical_partitions(self, _):
            if identity_failure:
                raise failure
            return []

        async def list_vios(self, _):
            raise defect

    monkeypatch.setattr(composite, "operating_system_entry", parent)
    with pytest.raises(
        HMCTLSFingerprintError if identity_failure else ExceptionGroup
    ) as caught:
        await _fetch_system_summary_data(Client(), "system-uuid")
    group = caught.value.__cause__ if identity_failure else caught.value
    assert isinstance(group, ExceptionGroup)
    assert defect in group.exceptions
    if identity_failure:
        assert caught.value is failure
        assert failure in group.exceptions
