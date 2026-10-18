"""Presentation contract tests for verified vNIC workflows."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from hmcpctl.operations.virtualization.vnic import (
    VnicBackingSelector,
    VnicChangeResult,
    VnicPartialError,
)
from hmcpctl.server_tools.virtualization.vnic import (
    hmc_add_vnic,
    hmc_remove_vnic,
)


def _result(operation: str, slot_num: str) -> VnicChangeResult:
    return VnicChangeResult(
        operation=operation,  # type: ignore[arg-type]
        mutation_dispatched=True,
        changed=True,
        selector=None,
        slot_num=slot_num,
        vnic_before=(),
        backing_before=(),
        vnic_after=(),
        backing_after=(),
        vnic_after_read_succeeded=True,
        backing_after_read_succeeded=True,
        output="done",
        errors=(),
    )


def test_add_vnic_builds_typed_selector(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "h")
    monkeypatch.setenv("HMC_USER", "u")
    monkeypatch.setenv("HMC_PASSWORD", "p")
    operation = AsyncMock(return_value=_result("add", "4"))
    monkeypatch.setattr("hmcpctl.server_tools.virtualization.vnic.add_vnic", operation)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("hmcpctl._app.client_from_env", lambda _profile: client)

    result = hmc_add_vnic(
        "system",
        "lpar",
        vios_name="vios1",
        vios_lpar_id="2",
        adapter_id="1",
        physical_port_id="0",
        capacity_percent=20.25,
        port_vlan_id=100,
    )

    args = operation.await_args.args
    assert args[1] == "lpar"
    assert operation.await_args.kwargs["system_name_or_uuid"] == "system"
    assert args[2] == VnicBackingSelector("vios1", "2", "1", "0", Decimal("20.25"))
    assert args[3] == 100
    assert result["slot_num"] == "4"
    assert result["changed"] is True


def test_add_vnic_forwards_ownership_override(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "h")
    monkeypatch.setenv("HMC_USER", "u")
    monkeypatch.setenv("HMC_PASSWORD", "p")
    operation = AsyncMock(return_value=_result("add", "4"))
    monkeypatch.setattr("hmcpctl.server_tools.virtualization.vnic.add_vnic", operation)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("hmcpctl._app.client_from_env", lambda _profile: client)

    hmc_add_vnic(
        "system",
        "lpar",
        "vios1",
        "2",
        "1",
        "0",
        20.25,
        100,
        ownership_override=True,
    )

    assert operation.await_args.kwargs == {
        "ownership_override": True,
        "system_name_or_uuid": "system",
    }


def test_add_vnic_partial_error_retains_serialized_result(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "h")
    monkeypatch.setenv("HMC_USER", "u")
    monkeypatch.setenv("HMC_PASSWORD", "p")
    partial = VnicPartialError("incomplete", _result("add", "4"))
    operation = AsyncMock(side_effect=partial)
    monkeypatch.setattr("hmcpctl.server_tools.virtualization.vnic.add_vnic", operation)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("hmcpctl._app.client_from_env", lambda _profile: client)

    with pytest.raises(VnicPartialError) as caught:
        hmc_add_vnic("system", "lpar", "vios1", "2", "1", "0", 20.25, 100)

    assert caught.value.result is partial.result
    assert '"operation": "add"' in str(caught.value)
    assert '"slot_num": "4"' in str(caught.value)


def test_remove_vnic_uses_slot_num(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "h")
    monkeypatch.setenv("HMC_USER", "u")
    monkeypatch.setenv("HMC_PASSWORD", "p")
    operation = AsyncMock(return_value=_result("remove", "4"))
    monkeypatch.setattr(
        "hmcpctl.server_tools.virtualization.vnic.remove_vnic", operation
    )
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("hmcpctl._app.client_from_env", lambda _profile: client)

    result = hmc_remove_vnic("system", "lpar", slot_num="4")

    assert operation.await_args.args[1:] == ("lpar", "4")
    assert operation.await_args.kwargs == {
        "ownership_override": False,
        "system_name_or_uuid": "system",
    }
    assert result["operation"] == "remove"


def test_remove_vnic_forwards_ownership_override(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "h")
    monkeypatch.setenv("HMC_USER", "u")
    monkeypatch.setenv("HMC_PASSWORD", "p")
    operation = AsyncMock(return_value=_result("remove", "4"))
    monkeypatch.setattr(
        "hmcpctl.server_tools.virtualization.vnic.remove_vnic", operation
    )
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("hmcpctl._app.client_from_env", lambda _profile: client)

    hmc_remove_vnic("system", "lpar", slot_num="4", ownership_override=True)

    assert operation.await_args.kwargs == {
        "ownership_override": True,
        "system_name_or_uuid": "system",
    }
