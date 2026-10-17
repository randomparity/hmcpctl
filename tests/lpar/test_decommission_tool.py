"""Tests for the presentation-neutral LPAR decommission workflow and MCP tool."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, get_type_hints
from unittest.mock import AsyncMock, Mock, patch
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

import httpx
import pytest
from conftest import assert_only_these_client_methods_used

from hmcpctl.audit import sink as audit_sink
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.jobs import power_off_lpar_job
from hmcpctl.operations.lpar.assignments import WorkflowStep
from hmcpctl.operations.lpar.decommission import (
    DecommissionAdapterRecord,
    DecommissionBlastRadius,
    DecommissionResult,
    _vscsi_detach_inventory,
    decommission_lpar,
)
from hmcpctl.server_tools.lpar.lifecycle import (
    hmc_decommission_lpar,
)
from hmcpctl.xmlutil import element_to_dict

SYSTEM_UUID = "11111111-1111-1111-1111-111111111111"
LPAR_UUID = "22222222-2222-2222-2222-222222222222"
VIOS_UUID = "33333333-3333-3333-3333-333333333333"
TARGET_PARTITION_ID = "7"


def test_decommission_blast_radius_schema_is_named_and_complete() -> None:
    assert DecommissionBlastRadius.__required_keys__ == {
        "lpar_uuid",
        "lpar_name",
        "partition_id",
        "state",
        "owner",
        "adapters",
        "storage_mappings",
        "unresolved_storage_mapping_count",
        "unavailable_storage_source_count",
    }
    hints = get_type_hints(DecommissionBlastRadius)
    assert hints["adapters"] == tuple[DecommissionAdapterRecord, ...]
    assert DecommissionAdapterRecord.__required_keys__ == {"type", "uuid"}


def _workflow_steps(*entries: dict[str, Any]) -> tuple[WorkflowStep, ...]:
    return tuple(
        WorkflowStep(entry["step"], entry["status"], entry.get("result"))
        for entry in entries
    )


def _lpar(
    uuid: str = LPAR_UUID,
    *,
    name: str = "aix-prod",
    partition_id: str = TARGET_PARTITION_ID,
    state: str = "running",
) -> dict[str, object]:
    return {
        "UUID": uuid,
        "Resource": {
            "PartitionName": name,
            "PartitionID": partition_id,
            "PartitionState": state,
        },
    }


def _adapter(adapter_type: str, uuid: str) -> dict[str, object]:
    return {"UUID": uuid, "AdapterType": adapter_type}


def _storage_detail() -> dict[str, object]:
    return {
        "UUID": VIOS_UUID,
        "Resource": {
            "VirtualSCSIMappings": {
                "VirtualSCSIMapping": [
                    {
                        "UUID": "vscsi-match",
                        "AssociatedLogicalPartition": {
                            "href": f"/rest/api/uom/LogicalPartition/{LPAR_UUID}"
                        },
                        "Storage": {"PhysicalVolume": {"VolumeName": "hdisk10"}},
                    },
                    {
                        "UUID": "vscsi-sparse",
                        "Storage": {"LogicalUnit": {"UnitName": "lu01"}},
                    },
                    {
                        "UUID": "vscsi-other",
                        "AssociatedLogicalPartition": {
                            "href": "/rest/api/uom/LogicalPartition/not-target"
                        },
                        "Storage": {"PhysicalVolume": {"VolumeName": "hdisk99"}},
                    },
                ]
            },
            "VirtualFibreChannelMappings": {
                "VirtualFibreChannelMapping": [
                    {
                        "UUID": "vfc-match",
                        "AssociatedLogicalPartitionID": TARGET_PARTITION_ID,
                        "Port": {"WWPNPair": "C050760AAA C050760BBB"},
                    }
                ]
            },
        },
    }


def _adapter_feed(adapter_type: str, adapters: list[dict[str, object]]) -> str:
    # Constructed/anonymized from captured collection200 Atom wrappers: metadata,
    # native uom:etag and one typed resource. Empty204 is tested separately.
    atom = "{http://www.w3.org/2005/Atom}"
    uom = "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}"
    root = Element(atom + "feed")
    SubElement(root, atom + "id").text = "urn:uuid:" + LPAR_UUID
    SubElement(root, atom + "updated").text = "2026-10-09T00:00:00Z"
    SubElement(root, atom + "link", rel="SELF", href="https://hmc.example.test/feed")
    SubElement(root, atom + "generator").text = "synthetic"
    for adapter in adapters:
        entry = SubElement(root, atom + "entry")
        if adapter.get("UUID") is not None:
            SubElement(entry, atom + "id").text = str(adapter["UUID"])
        SubElement(entry, atom + "title").text = adapter_type
        SubElement(entry, atom + "published").text = "2026-10-09T00:00:00Z"
        SubElement(entry, atom + "author")
        SubElement(entry, uom + "etag").text = "synthetic-version"
        content = SubElement(entry, atom + "content")
        resource = SubElement(content, uom + adapter_type)
        SubElement(SubElement(resource, uom + "Metadata"), uom + "Atom")
        SubElement(resource, uom + "VirtualSlotNumber").text = "2"
    return tostring(root, encoding="unicode")


def _client(*, complete: bool = True) -> AsyncMock:
    hmc = AsyncMock()
    hmc.list_logical_partitions.return_value = [_lpar()]
    hmc.get_logical_partition.return_value = _lpar()
    hmc.get_quick_property.return_value = "not activated"

    async def list_adapters(
        _lpar_uuid: str, adapter_type: str
    ) -> list[dict[str, object]]:
        adapters = {
            "ClientNetworkAdapter": [
                _adapter(
                    "ClientNetworkAdapter", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"
                ),
                _adapter(
                    "ClientNetworkAdapter", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
                ),
            ],
            "VirtualSCSIClientAdapter": [
                _adapter(
                    "VirtualSCSIClientAdapter", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
                ),
                _adapter(
                    "VirtualSCSIClientAdapter", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
                ),
            ],
            "VirtualFibreChannelClientAdapter": [
                _adapter(
                    "VirtualFibreChannelClientAdapter",
                    "cccccccc-cccc-cccc-cccc-ccccccccccc2",
                )
            ],
            "VirtualNICDedicated": [
                _adapter("VirtualNICDedicated", "dddddddd-dddd-dddd-dddd-ddddddddddd2")
            ],
        }
        return adapters[adapter_type]

    hmc.list_adapters.side_effect = list_adapters
    hmc._uom_headers = Mock(side_effect=lambda kind: {"Accept": kind})

    async def adapter_request(method, path, *, headers, uuid_path_arguments):
        assert method == "GET" and uuid_path_arguments == {
            "parent_uuid": path.split("/")[-2]
        }
        kind = path.rsplit("/", 1)[1]
        assert headers == {"Accept": kind}
        entries = await hmc.list_adapters(uuid_path_arguments["parent_uuid"], kind)
        return httpx.Response(200, text=_adapter_feed(kind, entries))

    hmc._request_with_uuid_path_arguments.side_effect = adapter_request
    hmc.list_vios.return_value = [
        {"UUID": VIOS_UUID, "Resource": {"PartitionName": "vios1"}}
    ]
    hmc.get_vios_storage_detail.return_value = (
        _complete_storage_detail() if complete else _storage_detail()
    )
    hmc.wait_for_job_entry.return_value = {
        "UUID": "job-uuid",
        "Resource": {"JobID": "job-uuid", "Status": "COMPLETED_OK"},
    }
    hmc.submit_job.return_value = {
        "UUID": "job-uuid",
        "link": "/rest/api/uom/jobs/job-uuid",
    }
    hmc.delete_storage_mapping = AsyncMock()
    hmc.delete_virtual_disk = AsyncMock()
    hmc.delete_logical_unit = AsyncMock()
    return hmc


def _patch_common(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    from hmcpctl.operations.lpar import decommission as ops

    async def resolve_system_uuid(hmc, value: str) -> str:
        calls.append(f"resolve_system_uuid:{value}")
        assert hmc is not None
        return SYSTEM_UUID

    async def resolve_names(
        hmc, system_uuid: str, fallback: str, lpar_uuid: str
    ) -> tuple[str, str]:
        calls.append(f"resolve_names:{system_uuid}:{fallback}:{lpar_uuid}")
        return ("system-a", "aix-prod")

    async def authorize(
        hmc, system_name: str, lpar_name: str, *, ownership_override: bool = False
    ) -> None:
        calls.append(f"authorize:{system_name}:{lpar_name}:{ownership_override}")

    monkeypatch.setattr(ops, "resolve_system_uuid", resolve_system_uuid)
    monkeypatch.setattr(ops, "resolve_lpar_ownership_names", resolve_names)
    monkeypatch.setattr(
        ops, "authorize_decommission_lpar_ownership_snapshot", authorize
    )


def _tool_result() -> DecommissionResult:
    return DecommissionResult(
        resource_deleted=True,
        workflow_completed=True,
        lpar_uuid=LPAR_UUID,
        dry_run=False,
        steps=_workflow_steps(
            {
                "step": "power_off",
                "status": "ok",
                "result": {"already_off": True, "state": "not activated"},
            },
        ),
        warnings=(),
        blast_radius={
            "lpar_uuid": LPAR_UUID,
            "lpar_name": "aix-prod",
            "partition_id": 7,
            "state": "not activated",
            "owner": None,
            "adapters": (),
            "storage_mappings": (),
            "unresolved_storage_mapping_count": 0,
            "unavailable_storage_source_count": 0,
        },
    )


def test_hmc_decommission_lpar_delegates_with_one_configured_client() -> None:
    expected = _tool_result()
    entered_clients: list[tuple[str | None, object]] = []
    hmc = object()

    @asynccontextmanager
    async def fake_client_context(profile: str | None):
        entered_clients.append((profile, hmc))
        yield hmc

    def fake_client_from_env(profile: str | None = None):
        return fake_client_context(profile)

    with (
        patch("hmcpctl._app.client_from_env", side_effect=fake_client_from_env),
        patch(
            "hmcpctl.server_tools.lpar.lifecycle.decommission_lpar",
            new=AsyncMock(return_value=expected),
        ) as decommission_mock,
    ):
        result = hmc_decommission_lpar(
            system_name_or_uuid="system-a",
            lpar_name_or_uuid="aix-prod",
            dry_run=True,
            ownership_override=True,
            immediate=True,
            timeout_seconds=123,
            poll_interval=7,
            profile="ops",
        )

    assert result == expected
    assert entered_clients == [("ops", hmc)]
    decommission_mock.assert_awaited_once_with(
        hmc,
        "system-a",
        "aix-prod",
        dry_run=True,
        ownership_override=True,
        immediate=True,
        timeout_seconds=123,
        poll_interval=7,
    )


@pytest.mark.asyncio
async def test_decommission_rejects_uuid_outside_selected_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmc = _client()
    hmc.list_logical_partitions.return_value = [_lpar(uuid="other-uuid")]
    authorize = AsyncMock()

    from hmcpctl.operations.lpar import decommission as ops

    monkeypatch.setattr(ops, "resolve_system_uuid", AsyncMock(return_value=SYSTEM_UUID))
    monkeypatch.setattr(
        ops, "authorize_decommission_lpar_ownership_snapshot", authorize
    )

    with pytest.raises(ValueError, match="No LPAR .* on managed system"):
        await decommission_lpar(hmc, "system-a", LPAR_UUID)

    hmc.list_logical_partitions.assert_awaited_once_with(SYSTEM_UUID)
    authorize.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_resolves_uuid_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc_uuid = "abcdefab-cdef-abcd-efab-cdefabcdefab"
    hmc = _client()
    hmc.list_logical_partitions.return_value = [_lpar(uuid=hmc_uuid)]
    hmc.get_logical_partition.return_value = _lpar(uuid=hmc_uuid)
    _patch_common(monkeypatch, calls)

    result = await decommission_lpar(
        hmc,
        "system-a",
        hmc_uuid.upper(),
        dry_run=True,
    )

    assert result.lpar_uuid == hmc_uuid
    hmc.get_logical_partition.assert_awaited_once_with(hmc_uuid)
    assert [call.args[0] for call in hmc.list_adapters.await_args_list] == [
        hmc_uuid
    ] * 4


@pytest.mark.asyncio
async def test_decommission_refuses_incomplete_adapter_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    _patch_common(monkeypatch, calls)

    async def list_adapters(
        _lpar_uuid: str, adapter_type: str
    ) -> list[dict[str, object]]:
        if adapter_type == "ClientNetworkAdapter":
            return [{"AdapterType": adapter_type}]
        return []

    hmc.list_adapters.side_effect = list_adapters

    with pytest.raises(
        ValueError,
        match=("Incomplete ClientNetworkAdapter inventory: missing its UUID"),
    ):
        await decommission_lpar(hmc, "system-a", "aix-prod")

    hmc.submit_job.assert_not_awaited()
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()
    hmc.delete_storage_mapping.assert_not_awaited()
    hmc.delete_virtual_disk.assert_not_awaited()
    hmc.delete_logical_unit.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_warns_when_listed_vios_has_no_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    hmc.list_vios.return_value = [{"Resource": {"PartitionName": "vios-missing-id"}}]
    _patch_common(monkeypatch, calls)

    result = await decommission_lpar(hmc, "system-a", "aix-prod", dry_run=True)

    assert result.blast_radius["storage_mappings"] == ()
    assert result.blast_radius["unresolved_storage_mapping_count"] == 0
    assert result.blast_radius["unavailable_storage_source_count"] == 1
    assert result.warnings == (
        (
            "Storage blast radius may be incomplete: listed VIOS 'vios-missing-id' "
            "has no UUID, so its storage mappings could not be inventoried."
        ),
    )
    hmc.get_vios_storage_detail.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_warns_when_vios_storage_detail_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    hmc.get_vios_storage_detail.return_value = None
    _patch_common(monkeypatch, calls)

    result = await decommission_lpar(hmc, "system-a", "aix-prod", dry_run=True)

    assert result.blast_radius["storage_mappings"] == ()
    assert result.blast_radius["unresolved_storage_mapping_count"] == 0
    assert result.blast_radius["unavailable_storage_source_count"] == 1
    assert result.warnings == (
        (
            f"Storage blast radius may be incomplete: VIOS {VIOS_UUID!r} returned no "
            "storage detail, so its storage mappings could not be inventoried."
        ),
    )
    hmc.get_vios_storage_detail.assert_awaited_once_with(VIOS_UUID)


@pytest.mark.asyncio
async def test_decommission_refuses_when_vios_storage_detail_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    hmc.get_vios_storage_detail.return_value = None
    _patch_common(monkeypatch, calls)

    async def list_adapters(
        _lpar_uuid: str, adapter_type: str
    ) -> list[dict[str, object]]:
        if adapter_type == "ClientNetworkAdapter":
            return [_adapter(adapter_type, "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1")]
        return []

    hmc.list_adapters.side_effect = list_adapters
    hmc.submit_job.side_effect = lambda *args, **kwargs: (
        calls.append("submit_job")
        or {
            "UUID": "job-uuid",
            "link": "/rest/api/uom/jobs/job-uuid",
        }
    )
    hmc.wait_for_job_entry.side_effect = lambda *args, **kwargs: (
        calls.append("wait_for_job_entry")
        or {
            "UUID": "job-uuid",
            "Resource": {"JobID": "job-uuid", "Status": "COMPLETED_OK"},
        }
    )
    hmc.delete_adapter.side_effect = lambda _lpar_uuid, adapter_type, adapter_uuid: (
        calls.append(f"delete_adapter:{adapter_type}:{adapter_uuid}")
    )
    hmc.delete_logical_partition.side_effect = lambda uuid: calls.append(
        f"delete_lpar:{uuid}"
    )

    result = await decommission_lpar(hmc, "system-a", "aix-prod")

    assert result.resource_deleted is False
    assert result.workflow_completed is False
    assert [step.status for step in result.steps] == [
        "skipped",
        "error",
        "skipped",
        "skipped",
    ]
    assert result.blast_radius["unresolved_storage_mapping_count"] == 0
    assert result.blast_radius["unavailable_storage_source_count"] == 1
    assert result.warnings == (
        (
            f"Storage blast radius may be incomplete: VIOS {VIOS_UUID!r} returned no "
            "storage detail, so its storage mappings could not be inventoried."
        ),
    )
    assert calls == [
        "resolve_system_uuid:system-a",
        f"resolve_names:{SYSTEM_UUID}:system-a:{LPAR_UUID}",
        "authorize:system-a:aix-prod:False",
    ]


@pytest.mark.asyncio
async def test_decommission_dry_run_inventories_without_mutating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client(complete=False)
    _patch_common(monkeypatch, calls)

    result = await decommission_lpar(hmc, "system-a", "aix-prod", dry_run=True)

    legacy = dict(result.blast_radius)
    assert legacy.pop("mapping_evidence")
    assert replace(result, blast_radius=legacy) == DecommissionResult(
        resource_deleted=False,
        workflow_completed=True,
        lpar_uuid=LPAR_UUID,
        dry_run=True,
        steps=_workflow_steps(
            {"step": "power_off", "status": "dry_run", "result": {"state": "running"}},
            {
                "step": "detach_storage_mappings",
                "status": "dry_run",
                "result": {"mappings": ()},
            },
            {
                "step": "detach_adapters",
                "status": "dry_run",
                "result": {
                    "adapters": (
                        {
                            "type": "ClientNetworkAdapter",
                            "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
                        },
                        {
                            "type": "ClientNetworkAdapter",
                            "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
                        },
                        {
                            "type": "VirtualSCSIClientAdapter",
                            "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1",
                        },
                        {
                            "type": "VirtualSCSIClientAdapter",
                            "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2",
                        },
                        {
                            "type": "VirtualFibreChannelClientAdapter",
                            "uuid": "cccccccc-cccc-cccc-cccc-ccccccccccc2",
                        },
                        {
                            "type": "VirtualNICDedicated",
                            "uuid": "dddddddd-dddd-dddd-dddd-ddddddddddd2",
                        },
                    )
                },
            },
            {
                "step": "delete_lpar",
                "status": "dry_run",
                "result": {"lpar_uuid": LPAR_UUID},
            },
        ),
        warnings=(
            "Storage blast radius may be incomplete: 1 mapping(s) lacked enough client identity to prove they belong to LPAR 'aix-prod'.",
            f"VIOS {VIOS_UUID!r} has a target mapping without an exact ID.",
            f"VIOS {VIOS_UUID!r} has an unverified vSCSI client UUID.",
            f"VIOS {VIOS_UUID!r} has an unverified vSCSI client UUID.",
        ),
        blast_radius={
            "lpar_uuid": LPAR_UUID,
            "lpar_name": "aix-prod",
            "partition_id": 7,
            "state": "running",
            "owner": None,
            "adapters": (
                {
                    "type": "ClientNetworkAdapter",
                    "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
                },
                {
                    "type": "ClientNetworkAdapter",
                    "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
                },
                {
                    "type": "VirtualSCSIClientAdapter",
                    "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1",
                },
                {
                    "type": "VirtualSCSIClientAdapter",
                    "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2",
                },
                {
                    "type": "VirtualFibreChannelClientAdapter",
                    "uuid": "cccccccc-cccc-cccc-cccc-ccccccccccc2",
                },
                {
                    "type": "VirtualNICDedicated",
                    "uuid": "dddddddd-dddd-dddd-dddd-ddddddddddd2",
                },
            ),
            "storage_mappings": (
                {
                    "vios_uuid": VIOS_UUID,
                    "type": "VirtualSCSIMapping",
                    "uuid": "vscsi-match",
                    "backing_device": "hdisk10",
                },
                {
                    "vios_uuid": VIOS_UUID,
                    "type": "VirtualFibreChannelMapping",
                    "uuid": "vfc-match",
                    "backing_device": "C050760AAA C050760BBB",
                },
            ),
            "unresolved_storage_mapping_count": 1,
            "unavailable_storage_source_count": 0,
        },
    )
    assert calls == [
        "resolve_system_uuid:system-a",
        f"resolve_names:{SYSTEM_UUID}:system-a:{LPAR_UUID}",
        "authorize:system-a:aix-prod:False",
    ]
    hmc.submit_job.assert_not_awaited()
    hmc.wait_for_job_entry.assert_not_awaited()
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()
    hmc.delete_storage_mapping.assert_not_awaited()
    hmc.delete_virtual_disk.assert_not_awaited()
    hmc.delete_logical_unit.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_enforces_ownership_even_for_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmc = _client()

    from hmcpctl.operations.lpar import decommission as ops

    monkeypatch.setattr(ops, "resolve_system_uuid", AsyncMock(return_value=SYSTEM_UUID))
    monkeypatch.setattr(
        ops,
        "resolve_lpar_ownership_names",
        AsyncMock(return_value=("system-a", "aix-prod")),
    )
    monkeypatch.setattr(
        ops,
        "authorize_decommission_lpar_ownership_snapshot",
        AsyncMock(side_effect=PermissionError("foreign owner")),
    )
    with pytest.raises(PermissionError, match="foreign owner"):
        await decommission_lpar(hmc, "system-a", "aix-prod", dry_run=True)

    hmc.get_quick_property.assert_not_awaited()
    hmc.submit_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_allows_explicit_ownership_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    _patch_common(monkeypatch, calls)

    result = await decommission_lpar(
        hmc,
        "system-a",
        "aix-prod",
        dry_run=True,
        ownership_override=True,
    )

    assert result.workflow_completed is True
    assert "authorize:system-a:aix-prod:True" in calls


@pytest.mark.asyncio
async def test_decommission_override_reads_and_reports_both_ownership_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmc = _client()
    hmc.config = HMCConfig(host="hmc.test", user="user", agent_id="alice")

    from hmcpctl.operations.lpar import decommission as ops

    monkeypatch.setattr(ops, "resolve_system_uuid", AsyncMock(return_value=SYSTEM_UUID))
    monkeypatch.setattr(
        ops,
        "resolve_lpar_ownership_names",
        AsyncMock(return_value=("system-a", "aix-prod")),
    )
    descriptions = AsyncMock(
        side_effect=(
            "[hmcpctl owner:bob created:2026-08-14]",
            "[hmcpctl owner:bob created:2026-08-14]",
            "[hmcpctl owner:bob created:2026-08-14]",
            "[hmcpctl owner:bob created:2026-08-14]",
        )
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.get_lpar_description", descriptions
    )

    result = await decommission_lpar(
        hmc, "system-a", "aix-prod", ownership_override=True
    )

    assert result.workflow_completed is True
    assert result.blast_radius["owner"] == "bob"
    assert descriptions.await_count == 4


@pytest.mark.asyncio
async def test_decommission_revalidates_changed_owner_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmc = _client()
    hmc.config = HMCConfig(host="hmc.test", user="user", agent_id="alice")

    from hmcpctl.operations.lpar import decommission as ops

    monkeypatch.setattr(ops, "resolve_system_uuid", AsyncMock(return_value=SYSTEM_UUID))
    monkeypatch.setattr(
        ops,
        "resolve_lpar_ownership_names",
        AsyncMock(return_value=("system-a", "aix-prod")),
    )
    descriptions = AsyncMock(
        side_effect=(
            "[hmcpctl owner:alice created:2026-08-14]",
            "[hmcpctl owner:bob created:2026-08-15]",
        )
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.get_lpar_description", descriptions
    )

    with pytest.raises(PermissionError, match="owned by 'bob'"):
        await decommission_lpar(hmc, "system-a", "aix-prod")

    assert descriptions.await_count == 2
    hmc.submit_job.assert_not_awaited()
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_runs_power_off_adapter_delete_and_lpar_delete_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    _patch_common(monkeypatch, calls)
    hmc.submit_job.side_effect = lambda *args, **kwargs: (
        calls.append("submit_job")
        or {
            "UUID": "job-uuid",
            "link": "/rest/api/uom/jobs/job-uuid",
        }
    )
    hmc.wait_for_job_entry.side_effect = lambda *args, **kwargs: (
        calls.append("wait_for_job_entry")
        or {
            "UUID": "job-uuid",
            "Resource": {"JobID": "job-uuid", "Status": "COMPLETED_OK"},
        }
    )
    hmc.get_quick_property.side_effect = lambda *args, **kwargs: (
        calls.append("get_state") or "not activated"
    )
    hmc.delete_adapter.side_effect = lambda lpar_uuid, adapter_type, adapter_uuid: (
        calls.append(f"delete_adapter:{adapter_type}:{adapter_uuid}")
    )
    hmc.delete_logical_partition.side_effect = lambda uuid: calls.append(
        f"delete_lpar:{uuid}"
    )

    result = await decommission_lpar(
        hmc,
        "system-a",
        "aix-prod",
        immediate=True,
        timeout_seconds=90,
        poll_interval=3,
    )

    assert result.resource_deleted is True
    assert result.workflow_completed is True
    assert result.steps == _workflow_steps(
        {
            "step": "power_off",
            "status": "ok",
            "result": {
                "already_off": False,
                "job_id": "job-uuid",
                "status": "COMPLETED_OK",
            },
        },
        {
            "step": "detach_storage_mappings",
            "status": "ok",
            "result": {
                "mappings": ({"vios_uuid": VIOS_UUID, "mapping_id": "vhost0/vtscsi0"},)
            },
        },
        {
            "step": "detach_adapters",
            "status": "ok",
            "result": {
                "adapters": (
                    {
                        "type": "ClientNetworkAdapter",
                        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
                    },
                    {
                        "type": "ClientNetworkAdapter",
                        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
                    },
                    {
                        "type": "VirtualSCSIClientAdapter",
                        "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1",
                    },
                    {
                        "type": "VirtualSCSIClientAdapter",
                        "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2",
                    },
                    {
                        "type": "VirtualFibreChannelClientAdapter",
                        "uuid": "cccccccc-cccc-cccc-cccc-ccccccccccc2",
                    },
                    {
                        "type": "VirtualNICDedicated",
                        "uuid": "dddddddd-dddd-dddd-dddd-ddddddddddd2",
                    },
                )
            },
        },
        {"step": "delete_lpar", "status": "ok", "result": {"lpar_uuid": LPAR_UUID}},
    )
    assert calls == [
        "resolve_system_uuid:system-a",
        f"resolve_names:{SYSTEM_UUID}:system-a:{LPAR_UUID}",
        "authorize:system-a:aix-prod:False",
        "authorize:system-a:aix-prod:False",
        "submit_job",
        "wait_for_job_entry",
        "authorize:system-a:aix-prod:False",
        "get_state",
        "get_state",
        "authorize:system-a:aix-prod:False",
        "delete_adapter:ClientNetworkAdapter:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
        "delete_adapter:ClientNetworkAdapter:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
        "delete_adapter:VirtualSCSIClientAdapter:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1",
        "delete_adapter:VirtualSCSIClientAdapter:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2",
        "delete_adapter:VirtualFibreChannelClientAdapter:cccccccc-cccc-cccc-cccc-ccccccccccc2",
        "delete_adapter:VirtualNICDedicated:dddddddd-dddd-dddd-dddd-ddddddddddd2",
        f"delete_lpar:{LPAR_UUID}",
    ]


@pytest.mark.asyncio
async def test_decommission_marks_already_off_lpar_without_power_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    hmc.get_logical_partition.return_value = _lpar(state="not activated")
    _patch_common(monkeypatch, calls)

    async def list_adapters(
        _lpar_uuid: str, adapter_type: str
    ) -> list[dict[str, object]]:
        if adapter_type == "ClientNetworkAdapter":
            return [_adapter(adapter_type, "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1")]
        return []

    hmc.list_adapters.side_effect = list_adapters
    hmc.get_quick_property.side_effect = lambda *args, **kwargs: (
        calls.append("get_state") or "not activated"
    )
    hmc.delete_adapter.side_effect = lambda _lpar_uuid, adapter_type, adapter_uuid: (
        calls.append(f"delete_adapter:{adapter_type}:{adapter_uuid}")
    )
    hmc.delete_logical_partition.side_effect = lambda uuid: calls.append(
        f"delete_lpar:{uuid}"
    )

    result = await decommission_lpar(hmc, "system-a", "aix-prod")

    assert result.steps[0] == WorkflowStep(
        "power_off", "ok", {"already_off": True, "state": "not activated"}
    )
    hmc.submit_job.assert_not_awaited()
    hmc.wait_for_job_entry.assert_not_awaited()
    assert calls == [
        "resolve_system_uuid:system-a",
        f"resolve_names:{SYSTEM_UUID}:system-a:{LPAR_UUID}",
        "authorize:system-a:aix-prod:False",
        "authorize:system-a:aix-prod:False",
        "get_state",
        "get_state",
        "authorize:system-a:aix-prod:False",
        "delete_adapter:ClientNetworkAdapter:eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1",
        f"delete_lpar:{LPAR_UUID}",
    ]


@pytest.mark.asyncio
async def test_decommission_stops_when_initially_off_lpar_restarts_before_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    hmc.get_logical_partition.return_value = _lpar(state="not activated")
    hmc.get_quick_property.return_value = "running"
    _patch_common(monkeypatch, calls)

    result = await decommission_lpar(hmc, "system-a", "aix-prod")

    assert result.workflow_completed is False
    assert result.steps == _workflow_steps(
        {
            "step": "power_off",
            "status": "ok",
            "result": {"already_off": True, "state": "not activated"},
        },
        {
            "step": "detach_storage_mappings",
            "status": "error",
            "result": (
                "Cannot detach adapters from LPAR 'aix-prod': current state is "
                "'running'; expected 'not activated'."
            ),
        },
        {"step": "detach_adapters", "status": "skipped"},
        {"step": "delete_lpar", "status": "skipped"},
    )
    hmc.get_quick_property.assert_awaited_once_with(
        "LogicalPartition", LPAR_UUID, "PartitionState"
    )
    hmc.submit_job.assert_not_awaited()
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_stops_when_lpar_restarts_after_power_off_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    hmc.get_quick_property.return_value = "running"
    _patch_common(monkeypatch, calls)

    result = await decommission_lpar(hmc, "system-a", "aix-prod")

    assert result.workflow_completed is False
    assert result.steps[0].status == "ok"
    assert result.steps[0].result["already_off"] is False
    assert result.steps[1] == WorkflowStep(
        "detach_storage_mappings",
        "error",
        (
            "Cannot detach adapters from LPAR 'aix-prod': current state is "
            "'running'; expected 'not activated'."
        ),
    )
    assert result.steps[2] == WorkflowStep("detach_adapters", "skipped")
    assert result.steps[3] == WorkflowStep("delete_lpar", "skipped")
    hmc.submit_job.assert_awaited_once()
    hmc.wait_for_job_entry.assert_awaited_once()
    hmc.get_quick_property.assert_awaited_once_with(
        "LogicalPartition", LPAR_UUID, "PartitionState"
    )
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_stops_when_detach_state_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    hmc.get_quick_property.side_effect = HMCError("state read failed")
    _patch_common(monkeypatch, calls)

    result = await decommission_lpar(hmc, "system-a", "aix-prod")

    assert result.workflow_completed is False
    assert result.steps[1] == WorkflowStep(
        "detach_storage_mappings",
        "error",
        "Could not verify LPAR 'aix-prod' state before detaching adapters: state read failed",
    )
    assert result.steps[2] == WorkflowStep("detach_adapters", "skipped")
    assert result.steps[3] == WorkflowStep("delete_lpar", "skipped")
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job", "fragment"),
    [
        (
            {
                "UUID": "job-uuid",
                "Resource": {
                    "JobID": "job-uuid",
                    "Status": "COMPLETED_WITH_ERROR",
                    "Results": {
                        "JobParameter": {
                            "ParameterName": "result",
                            "ParameterValue": "power failed",
                        }
                    },
                },
            },
            "status 'COMPLETED_WITH_ERROR'",
        ),
        (
            {
                "UUID": "job-uuid",
                "Resource": {"JobID": "job-uuid", "Status": "RUNNING"},
            },
            "timed out",
        ),
    ],
)
async def test_decommission_reports_power_off_failure_and_skips_later_steps(
    monkeypatch: pytest.MonkeyPatch,
    job: dict[str, object],
    fragment: str,
) -> None:
    calls: list[str] = []
    hmc = _client()
    _patch_common(monkeypatch, calls)
    hmc.wait_for_job_entry.return_value = job

    result = await decommission_lpar(hmc, "system-a", "aix-prod")

    assert result.resource_deleted is False
    assert result.workflow_completed is False
    assert result.steps[0].status == "error"
    assert fragment in result.steps[0].result
    assert result.steps[1:] == _workflow_steps(
        {"step": "detach_storage_mappings", "status": "skipped"},
        {"step": "detach_adapters", "status": "skipped"},
        {"step": "delete_lpar", "status": "skipped"},
    )
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_stops_after_first_adapter_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    hmc = _client()
    _patch_common(monkeypatch, calls)

    async def delete_adapter(
        _lpar_uuid: str, adapter_type: str, adapter_uuid: str
    ) -> None:
        calls.append(f"delete_adapter:{adapter_type}:{adapter_uuid}")
        if adapter_uuid == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1":
            raise HMCError("adapter delete failed")

    hmc.delete_adapter.side_effect = delete_adapter

    result = await decommission_lpar(hmc, "system-a", "aix-prod")

    assert result.resource_deleted is False
    assert result.workflow_completed is False
    assert result.steps == _workflow_steps(
        {
            "step": "power_off",
            "status": "ok",
            "result": {
                "already_off": False,
                "job_id": "job-uuid",
                "status": "COMPLETED_OK",
            },
        },
        {
            "step": "detach_storage_mappings",
            "status": "ok",
            "result": {
                "mappings": ({"vios_uuid": VIOS_UUID, "mapping_id": "vhost0/vtscsi0"},)
            },
        },
        {
            "step": "detach_adapters",
            "status": "error",
            "result": {
                "adapters": (
                    {
                        "type": "ClientNetworkAdapter",
                        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
                    },
                    {
                        "type": "ClientNetworkAdapter",
                        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
                    },
                ),
                "error": "adapter delete failed",
            },
        },
        {"step": "delete_lpar", "status": "skipped"},
    )
    assert calls == [
        "resolve_system_uuid:system-a",
        f"resolve_names:{SYSTEM_UUID}:system-a:{LPAR_UUID}",
        "authorize:system-a:aix-prod:False",
        "authorize:system-a:aix-prod:False",
        "authorize:system-a:aix-prod:False",
        "authorize:system-a:aix-prod:False",
        "delete_adapter:ClientNetworkAdapter:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
        "delete_adapter:ClientNetworkAdapter:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
        "delete_adapter:VirtualSCSIClientAdapter:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1",
    ]
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_dry_run_makes_no_unclassified_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R18: the whole call set on the destructive tool's dry-run path.

    `hmc_decommission_lpar` is the one whose dry-run path a caller is most likely
    to trust, and the one that does the most before deciding: it builds the full
    blast radius — every adapter of four kinds, every VIOS on the system and its
    storage detail — before returning. Every one of those is a read, and pinning
    the set is what makes that a classification rather than an assumption.

    Two things happen on this path that a reader might not expect and that
    ADR 0039 records rather than hides: an SSH `lssyscfg` runs to read the
    ownership stamp, and with `ownership_override=True` a warning-level audit
    line is written locally. Neither is an HMC mutation; both are patched out
    here by `_patch_common`, which is why this test pins the REST surface only.
    """
    calls: list[str] = []
    hmc = _client()
    _patch_common(monkeypatch, calls)

    result = await decommission_lpar(hmc, "system-a", "aix-prod", dry_run=True)

    assert result.dry_run is True
    assert result.resource_deleted is False
    used = assert_only_these_client_methods_used(
        hmc,
        frozenset(
            {
                "list_logical_partitions",  # read: find the partition on the system
                "get_logical_partition",  # read: its current state and attributes
                "list_adapters",  # synthetic remote adapter service
                "_request_with_uuid_path_arguments",  # GET: complete four-kind inventory
                "_uom_headers",  # pure: typed read headers
                "list_vios",  # read: every VIOS on the system
                "get_vios_storage_detail",  # read: each VIOS's storage mappings
            }
        ),
    )
    assert used, "the handler touched nothing; the dry-run path was not exercised"


def _audited_client() -> AsyncMock:
    hmc = _client()
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "u", "password": "p", "agent_id": "agent-7"}
    )
    return hmc


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == audit_sink.AUDIT_LOGGER_NAME
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("immediate", [False, True])
async def test_decommission_power_off_is_recorded_before_its_submit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    immediate: bool,
) -> None:
    """#1115, ADR 0180 as amended: the decommission PowerOff writes one record.

    The record is compared with the submitted document, so it cannot describe a
    PowerOff the HMC did not receive, and it is already written when the submit runs.
    """
    hmc = _audited_client()
    _patch_common(monkeypatch, [])
    records_at_submit: list[list[dict[str, Any]]] = []

    async def submit_job(*_args: object) -> dict[str, str]:
        records_at_submit.append(_audit_records(caplog))
        return {"UUID": "job-uuid", "link": "/rest/api/uom/jobs/job-uuid"}

    hmc.submit_job.side_effect = submit_job

    with caplog.at_level(logging.INFO):
        result = await decommission_lpar(
            hmc, "system-a", "aix-prod", immediate=immediate
        )

    assert result.resource_deleted is True
    records = _audit_records(caplog)
    assert [record["event"] for record in records] == ["lpar-power-off"]
    record = records[0]
    assert records_at_submit == [[record]]
    assert (record["operation"], record["immediate"], record["restart"]) == (
        "shutdown",
        immediate,
        False,
    )
    _, document = hmc.submit_job.await_args.args
    assert document == power_off_lpar_job(
        immediate=record["immediate"],
        restart=record["restart"],
        operation=record["operation"],
    )
    assert (record["lpar"], record["host"]) == (LPAR_UUID, "hmc.test")
    assert record["attribution"] == {
        "claim": "agent-7",
        "source": "config:agent_id",
        "verified": False,
    }


@pytest.mark.asyncio
async def test_decommission_power_off_is_recorded_when_the_submit_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A submit that raises may still have reached the HMC, so it is recorded."""
    hmc = _audited_client()
    _patch_common(monkeypatch, [])
    hmc.submit_job.side_effect = HMCError("submit failed")

    with caplog.at_level(logging.INFO):
        result = await decommission_lpar(hmc, "system-a", "aix-prod")

    assert result.steps[0] == WorkflowStep("power_off", "error", "submit failed")
    assert [record["event"] for record in _audit_records(caplog)] == ["lpar-power-off"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "dry_run"),
    [("not activated", False), ("running", True)],
    ids=["already-off", "dry-run"],
)
async def test_decommission_without_a_power_off_submit_records_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    state: str,
    dry_run: bool,
) -> None:
    """No PowerOff is submitted for an already-off partition or a dry run, so no record."""
    hmc = _audited_client()
    hmc.get_logical_partition.return_value = _lpar(state=state)
    _patch_common(monkeypatch, [])

    with caplog.at_level(logging.INFO):
        await decommission_lpar(hmc, "system-a", "aix-prod", dry_run=dry_run)

    hmc.submit_job.assert_not_awaited()
    assert _audit_records(caplog) == []


def _complete_storage_detail() -> dict[str, Any]:
    detail = _storage_detail()
    mappings = detail["Resource"]["VirtualSCSIMappings"]["VirtualSCSIMapping"]
    mappings.pop(1)
    mappings[0]["ServerAdapter"] = {"AdapterName": "vhost0"}
    mappings[0]["TargetDevice"] = {"VirtualSCSITargetDevice": {"TargetName": "vtscsi0"}}
    mappings[1]["AssociatedLogicalPartition"]["href"] = (
        "/rest/api/uom/LogicalPartition/44444444-4444-4444-4444-444444444444"
    )
    return detail


@pytest.mark.asyncio
async def test_decommission_detaches_exact_mapping_before_adapters(monkeypatch):
    calls = []
    hmc = _client()
    hmc.get_vios_storage_detail.return_value = _complete_storage_detail()
    _patch_common(monkeypatch, calls)
    hmc.delete_storage_mapping.side_effect = lambda *args: calls.append(
        ("mapping", args)
    )
    hmc.delete_adapter.side_effect = lambda *args: calls.append(("adapter", args))
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert result.workflow_completed
    assert [step.step for step in result.steps] == [
        "power_off",
        "detach_storage_mappings",
        "detach_adapters",
        "delete_lpar",
    ]
    hmc.delete_storage_mapping.assert_awaited_once_with(
        VIOS_UUID, "vhost0/vtscsi0", LPAR_UUID
    )
    assert next(
        i
        for i, item in enumerate(calls)
        if isinstance(item, tuple) and item[0] == "mapping"
    ) < next(
        i
        for i, item in enumerate(calls)
        if isinstance(item, tuple) and item[0] == "adapter"
    )
    hmc.delete_virtual_disk.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    [
        "detail",
        "collection",
        "identity",
        "client",
        "duplicate",
        "read",
        "listing",
        "malformed-block",
        "malformed-member",
        "foreign-duplicate",
    ],
)
async def test_decommission_incomplete_vscsi_blocks_first_mutation(
    monkeypatch, missing
):
    hmc = _client()
    detail = _complete_storage_detail()
    mappings = detail["Resource"]["VirtualSCSIMappings"]["VirtualSCSIMapping"]
    if missing == "detail":
        detail = None
    elif missing == "collection":
        detail["Resource"].pop("VirtualSCSIMappings")
    elif missing == "identity":
        mappings[0].pop("TargetDevice")
    elif missing == "client":
        mappings[0].pop("AssociatedLogicalPartition")
    elif missing == "duplicate":
        mappings.append(dict(mappings[0]))
    elif missing == "malformed-block":
        detail["Resource"]["VirtualSCSIMappings"] = "unparsed"
    elif missing == "malformed-member":
        mappings.append("unparsed")
    elif missing == "foreign-duplicate":
        mappings[1]["ServerAdapter"] = mappings[0]["ServerAdapter"]
        mappings[1]["TargetDevice"] = mappings[0]["TargetDevice"]
    elif missing == "read":
        hmc.get_vios_storage_detail.side_effect = HMCError("read failed")
    elif missing == "listing":
        hmc.list_vios.side_effect = HMCError("listing failed")
    hmc.get_vios_storage_detail.return_value = detail
    _patch_common(monkeypatch, [])
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert not result.resource_deleted and not result.workflow_completed
    assert [step.status for step in result.steps] == [
        "skipped",
        "error",
        "skipped",
        "skipped",
    ]
    hmc.submit_job.assert_not_awaited()
    hmc.delete_storage_mapping.assert_not_awaited()
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_mapping_error_retains_failure_and_skips_teardown(
    monkeypatch,
):
    hmc = _client()
    hmc.get_vios_storage_detail.return_value = _complete_storage_detail()
    hmc.delete_storage_mapping.side_effect = HMCError("HSCL2957")
    _patch_common(monkeypatch, [])
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert [step.status for step in result.steps] == [
        "ok",
        "error",
        "skipped",
        "skipped",
    ]
    assert result.steps[1].result["error"] == "HSCL2957"
    assert result.steps[1].result["mappings"] == ()
    hmc.delete_storage_mapping.assert_awaited_once()
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_mapping_detach_reports_success_then_stops(monkeypatch):
    hmc = _client()
    detail = _complete_storage_detail()
    mappings = detail["Resource"]["VirtualSCSIMappings"]["VirtualSCSIMapping"]
    second = copy.deepcopy(mappings[0])
    second["TargetDevice"]["VirtualSCSITargetDevice"]["TargetName"] = "vtscsi1"
    mappings.append(second)
    hmc.get_vios_storage_detail.return_value = detail
    hmc.delete_storage_mapping.side_effect = [None, HMCError("second refused")]
    _patch_common(monkeypatch, [])
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert result.steps[1].result == {
        "mappings": ({"vios_uuid": VIOS_UUID, "mapping_id": "vhost0/vtscsi0"},),
        "error": "second refused",
        "failed_mapping": {"vios_uuid": VIOS_UUID, "mapping_id": "vhost0/vtscsi1"},
    }
    assert hmc.delete_storage_mapping.await_count == 2
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block", ["", {}, {"@schemaVersion": "V1_0"}, {"VirtualSCSIMapping": []}]
)
async def test_explicit_empty_vscsi_collection_allows_decommission(monkeypatch, block):
    hmc = _client()
    detail = _complete_storage_detail()
    detail["Resource"]["VirtualSCSIMappings"] = block
    hmc.get_vios_storage_detail.return_value = detail
    _patch_common(monkeypatch, [])
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert result.workflow_completed
    hmc.delete_storage_mapping.assert_not_awaited()


@pytest.mark.asyncio
async def test_inventory_programming_error_is_not_advisory(monkeypatch):
    hmc = _client()
    hmc.get_vios_storage_detail.side_effect = TypeError("bad decoder")
    _patch_common(monkeypatch, [])
    with pytest.raises(TypeError, match="bad decoder"):
        await decommission_lpar(hmc, "system-a", LPAR_UUID, dry_run=True)
    hmc.submit_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_mapping_client_uuid_is_matched_case_insensitively(monkeypatch):
    target = "ABCDEF12-3456-4789-ABCD-0123456789AB"
    hmc = _client()
    hmc.list_logical_partitions.return_value = [_lpar(target)]
    hmc.get_logical_partition.return_value = _lpar(target)
    detail = _complete_storage_detail()
    own = detail["Resource"]["VirtualSCSIMappings"]["VirtualSCSIMapping"][0]
    own["AssociatedLogicalPartition"]["href"] = (
        f"/rest/api/uom/LogicalPartition/{target.lower()}"
    )
    hmc.get_vios_storage_detail.return_value = detail
    _patch_common(monkeypatch, [])
    await decommission_lpar(hmc, "system-a", target)
    hmc.delete_storage_mapping.assert_awaited_once_with(
        VIOS_UUID, "vhost0/vtscsi0", target
    )


@pytest.mark.asyncio
async def test_changed_ownership_after_power_off_denies_mapping_mutation(monkeypatch):
    from hmcpctl.operations.lpar import decommission as ops

    hmc = _client()
    _patch_common(monkeypatch, [])
    authorize = AsyncMock(side_effect=[None, None, HMCError("ownership changed")])
    monkeypatch.setattr(
        ops, "authorize_decommission_lpar_ownership_snapshot", authorize
    )
    with pytest.raises(HMCError, match="ownership changed"):
        await decommission_lpar(hmc, "system-a", LPAR_UUID)
    hmc.submit_job.assert_awaited_once()
    hmc.delete_storage_mapping.assert_not_awaited()
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.fixture
def native_vscsi_block():
    mapping = f"""<VirtualSCSIMapping><AssociatedLogicalPartition
        href="/rest/api/uom/LogicalPartition/{LPAR_UUID}"/>
        <ServerAdapter><AdapterName>vhost3</AdapterName></ServerAdapter>
        <TargetDevice><VirtualSCSITargetDevice><TargetName>vtscsi2</TargetName>
        </VirtualSCSITargetDevice></TargetDevice></VirtualSCSIMapping>"""
    xml = '<VirtualSCSIMappings group="ViosSCSIMapping"><Metadata><Atom/></Metadata>'
    return element_to_dict(fromstring(xml + mapping + "</VirtualSCSIMappings>"))


def test_native_metadata_preserves_exact_target_and_foreign_inventory(
    native_vscsi_block,
):
    block = copy.deepcopy(native_vscsi_block)
    foreign = copy.deepcopy(block["VirtualSCSIMapping"])
    foreign["AssociatedLogicalPartition"]["href"] = (
        "/rest/api/uom/LogicalPartition/44444444-4444-4444-4444-444444444444"
    )
    foreign["ServerAdapter"]["AdapterName"] = "vhost4"
    block["VirtualSCSIMapping"] = [block["VirtualSCSIMapping"], foreign]
    original = copy.deepcopy(block)
    assert _vscsi_detach_inventory(
        {"VirtualSCSIMappings": block}, VIOS_UUID, LPAR_UUID
    ) == (({"vios_uuid": VIOS_UUID, "mapping_id": "vhost3/vtscsi2"},), ())
    assert block == original


def test_native_metadata_only_is_complete_empty():
    block = element_to_dict(
        fromstring(
            '<VirtualSCSIMappings group="ViosSCSIMapping"><Metadata><Atom/></Metadata>'
            "</VirtualSCSIMappings>"
        )
    )
    assert _vscsi_detach_inventory(
        {"VirtualSCSIMappings": block}, VIOS_UUID, LPAR_UUID
    ) == ((), ())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "adapter",
        "target",
        "client",
        "client-uuid",
        "duplicate",
        "foreign-duplicate",
        "collection",
        "unknown-child",
        "null",
        "scalar",
        "nonempty",
        "invalid-atom",
        "extra",
        "duplicate-xml",
        "missing-atom",
        "metadata-attribute",
        "atom-attribute",
    ],
)
async def test_native_metadata_invalid_inventory_has_no_first_mutation(
    monkeypatch, native_vscsi_block, bad
):
    block = copy.deepcopy(native_vscsi_block)
    own = block["VirtualSCSIMapping"]
    if bad in {"adapter", "target", "client"}:
        own.pop(
            {
                "adapter": "ServerAdapter",
                "target": "TargetDevice",
                "client": "AssociatedLogicalPartition",
            }[bad]
        )
    elif bad == "client-uuid":
        own["AssociatedLogicalPartition"]["href"] = (
            "/rest/api/uom/LogicalPartition/invalid"
        )
    elif bad in {"duplicate", "foreign-duplicate"}:
        duplicate = copy.deepcopy(own)
        if bad == "foreign-duplicate":
            duplicate["AssociatedLogicalPartition"]["href"] = (
                "/rest/api/uom/LogicalPartition/44444444-4444-4444-4444-444444444444"
            )
        block["VirtualSCSIMapping"] = [own, duplicate]
    elif bad == "unknown-child":
        block["Unknown"] = ""
    elif bad == "duplicate-xml":
        block["Metadata"] = element_to_dict(
            fromstring(
                "<Container><Metadata><Atom/></Metadata><Metadata><Atom/></Metadata></Container>"
            )
        )["Metadata"]
    elif bad in {"null", "invalid-atom"}:
        block["Metadata"] = None if bad == "null" else {"Atom": None}
    elif bad != "collection":
        xml = {
            "scalar": "<Metadata/>",
            "nonempty": "<Metadata><Atom>value</Atom></Metadata>",
            "extra": "<Metadata><Atom/><Other/></Metadata>",
            "missing-atom": "<Metadata><Other/></Metadata>",
            "metadata-attribute": '<Metadata a="b"><Atom/></Metadata>',
            "atom-attribute": '<Metadata><Atom a="b"/></Metadata>',
        }[bad]
        block["Metadata"] = element_to_dict(fromstring(xml))
    resource = {} if bad == "collection" else {"VirtualSCSIMappings": block}
    hmc = _client()
    hmc.get_vios_storage_detail.return_value = {"Resource": resource}
    _patch_common(monkeypatch, [])
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert not result.workflow_completed and not result.resource_deleted
    hmc.submit_job.assert_not_awaited()
    hmc.delete_storage_mapping.assert_not_awaited()
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_metadata_canonical_detaches_only_exact_own_mapping(
    monkeypatch, native_vscsi_block
):
    hmc = _client()
    hmc.get_vios_storage_detail.return_value = {
        "Resource": {"VirtualSCSIMappings": native_vscsi_block}
    }
    _patch_common(monkeypatch, [])
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert result.workflow_completed and result.resource_deleted
    hmc.delete_storage_mapping.assert_awaited_once_with(
        VIOS_UUID, "vhost3/vtscsi2", LPAR_UUID
    )
    hmc.delete_virtual_disk.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping_removes_client", [True, False])
async def test_decommission_refresh_uses_surviving_original_adapters(
    monkeypatch, mapping_removes_client
):
    hmc = _client()
    _patch_common(monkeypatch, [])
    detached = False
    uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"

    async def inventory(_parent, kind):
        return (
            [_adapter(kind, uuid)]
            if kind == "VirtualSCSIClientAdapter"
            and not (detached and mapping_removes_client)
            else []
        )

    async def mapping(*_args):
        nonlocal detached
        detached = True

    async def adapter(_parent, _kind, identity):
        if mapping_removes_client and detached:
            raise HMCError("stale adapter DELETE", 404, "REST000B")
        assert identity == uuid

    hmc.list_adapters.side_effect = inventory
    hmc.delete_storage_mapping.side_effect = mapping
    hmc.delete_adapter.side_effect = adapter
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert result.workflow_completed and result.resource_deleted
    assert hmc.delete_adapter.await_count == (0 if mapping_removes_client else 1)
    assert hmc.list_adapters.await_count == 8
    assert result.blast_radius["adapters"] == (
        {"type": "VirtualSCSIClientAdapter", "uuid": uuid},
    )
    hmc.delete_logical_partition.assert_awaited_once_with(LPAR_UUID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "new",
        "duplicate",
        "case-alias",
        "missing",
        "invalid",
        "read",
        "empty200",
        "nonempty204",
        "unknown",
        "wrong-type",
        "pagination",
        "content",
        "owner",
        "state",
        "cancel",
    ],
)
async def test_decommission_refresh_rejects_unverified_inventory(monkeypatch, fault):
    hmc = _client()
    _patch_common(monkeypatch, [])
    detached = False
    original_request = hmc._request_with_uuid_path_arguments.side_effect

    async def mapping(*_args):
        nonlocal detached
        detached = True

    async def request(method, path, **kwargs):
        response = await original_request(method, path, **kwargs)
        if not detached:
            return response
        if fault == "read":
            raise HMCError("fresh inventory refused", 403, "synthetic")
        if fault == "cancel":
            raise asyncio.CancelledError
        if fault == "empty200":
            return httpx.Response(200)
        if fault == "nonempty204":
            return httpx.Response(204, text=response.text)
        root = fromstring(response.text)
        atom = "{http://www.w3.org/2005/Atom}"
        uom = "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}"
        entry = root.find(atom + "entry")
        if fault == "unknown":
            SubElement(root, uom + "UnknownAdapter")
        elif fault == "pagination":
            SubElement(
                root, atom + "link", rel="next", href="https://hmc.example.test/page2"
            )
        elif entry is not None:
            identity = entry.find(atom + "id")
            if fault == "new" and path.endswith("/ClientNetworkAdapter"):
                identity.text = "ffffffff-ffff-ffff-ffff-ffffffffffff"
            elif fault in ("duplicate", "case-alias"):
                other = copy.deepcopy(entry)
                if fault == "case-alias":
                    other.find(atom + "id").text = identity.text.upper()
                root.append(other)
            elif fault == "missing":
                entry.remove(identity)
            elif fault == "invalid":
                identity.text = "not-a-uuid"
            elif fault == "wrong-type":
                entry.find(atom + "content")[0].tag = uom + "ForeignAdapter"
            elif fault == "content":
                SubElement(entry.find(atom + "content"), uom + "ForeignAdapter")
        return httpx.Response(200, text=tostring(root, encoding="unicode"))

    hmc.delete_storage_mapping.side_effect = mapping
    hmc._request_with_uuid_path_arguments.side_effect = request
    if fault == "owner":

        async def authorize(*_args, **_kwargs):
            if detached:
                raise PermissionError("ownership changed")

        monkeypatch.setattr(
            "hmcpctl.operations.lpar.decommission.authorize_decommission_lpar_ownership_snapshot",
            authorize,
        )
    if fault == "state":
        hmc.get_quick_property.side_effect = lambda *_args: (
            "running" if detached else "not activated"
        )
    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await decommission_lpar(hmc, "system-a", LPAR_UUID)
    else:
        result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
        assert not result.workflow_completed and not result.resource_deleted
        assert (
            result.steps[-2].status == "error" and result.steps[-1].status == "skipped"
        )
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_mapping_failure_does_not_refresh(monkeypatch):
    hmc = _client()
    _patch_common(monkeypatch, [])
    hmc.delete_storage_mapping.side_effect = HMCError(
        "mapping refused", 500, "synthetic"
    )
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert not result.workflow_completed
    assert hmc.list_adapters.await_count == 4
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_decommission_refresh_genuine_partial_delete_error_stays_failed(
    monkeypatch,
):
    hmc = _client()
    _patch_common(monkeypatch, [])
    hmc.delete_adapter.side_effect = [
        None,
        HMCError("genuine DELETE refused", 404, "REST000B"),
    ]
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert not result.workflow_completed and not result.resource_deleted
    assert result.steps[-2].status == "error" and result.steps[-1].status == "skipped"
    assert len(result.steps[-2].result["adapters"]) == 1
    assert "genuine DELETE refused" in result.steps[-2].result["error"]
    assert hmc.delete_adapter.await_count == 2
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["malformed", "entry", "empty200", "nonempty204", "http", "identity", "collection"],
)
async def test_initial_raw_adapter_inventory_fails_before_first_mutation(
    monkeypatch, fault
):
    hmc = _client()
    _patch_common(monkeypatch, [])
    xml = _adapter_feed(
        "ClientNetworkAdapter",
        [_adapter("ClientNetworkAdapter", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")],
    )
    from xml.etree.ElementTree import fromstring

    root = fromstring(xml)
    if fault == "malformed":
        response = httpx.Response(200, text="<feed")
    elif fault == "entry":
        response = httpx.Response(
            200,
            text=tostring(
                root.find("{http://www.w3.org/2005/Atom}entry"), encoding="unicode"
            ),
        )
    elif fault == "empty200":
        response = httpx.Response(200)
    elif fault == "nonempty204":
        response = httpx.Response(204, text=xml)
    elif fault == "http":
        response = httpx.Response(500, text="synthetic transport failure")
    else:
        atom = "{http://www.w3.org/2005/Atom}"
        if fault == "identity":
            SubElement(
                root.find(atom + "entry"), atom + "id"
            ).text = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
        else:
            SubElement(root, "{urn:unknown}entry")
        response = httpx.Response(200, text=tostring(root, encoding="unicode"))
    hmc._request_with_uuid_path_arguments.return_value = response
    hmc._request_with_uuid_path_arguments.side_effect = None
    with pytest.raises((ValueError, HMCError)):
        await decommission_lpar(hmc, "system-a", LPAR_UUID)
    hmc.submit_job.assert_not_awaited()
    hmc.delete_storage_mapping.assert_not_awaited()
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_empty_204_adapter_feed_is_complete(monkeypatch):
    hmc = _client()
    _patch_common(monkeypatch, [])
    hmc._request_with_uuid_path_arguments.side_effect = None
    hmc._request_with_uuid_path_arguments.return_value = httpx.Response(204)
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert result.workflow_completed and result.resource_deleted
    assert hmc._request_with_uuid_path_arguments.await_count == 8
    hmc.delete_adapter.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", ["entry", "resource"])
async def test_refresh_metadata_cannot_hide_existing_adapter(monkeypatch, hidden):
    from xml.etree.ElementTree import fromstring

    hmc = _client()
    _patch_common(monkeypatch, [])
    detached = False
    identity = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"

    async def inventory(_parent, kind):
        return [_adapter(kind, identity)] if kind == "VirtualSCSIClientAdapter" else []

    async def detach(*_args):
        nonlocal detached
        detached = True

    original_request = hmc._request_with_uuid_path_arguments.side_effect

    async def request(method, path, **kwargs):
        response = await original_request(method, path, **kwargs)
        if detached and path.endswith("/VirtualSCSIClientAdapter"):
            atom = "{http://www.w3.org/2005/Atom}"
            root = fromstring(response.text)
            entry = root.find(atom + "entry")
            root.remove(entry)
            metadata = SubElement(root, atom + "author")
            metadata.append(
                entry if hidden == "entry" else entry.find(atom + "content")[0]
            )
            return httpx.Response(200, text=tostring(root, encoding="unicode"))
        return response

    hmc.list_adapters.side_effect = inventory
    hmc.delete_storage_mapping.side_effect = detach
    hmc._request_with_uuid_path_arguments.side_effect = request
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert detached  # Actual canonical mapping phase was reached.
    assert not result.workflow_completed and not result.resource_deleted
    assert result.steps[2].status == "error" and result.steps[3].status == "skipped"
    hmc.delete_adapter.assert_not_awaited()
    hmc.delete_logical_partition.assert_not_awaited()
    assert await inventory(LPAR_UUID, "VirtualSCSIClientAdapter") == [
        _adapter("VirtualSCSIClientAdapter", identity)
    ]


@pytest.mark.asyncio
async def test_adapter_feed_preserves_native_metadata_and_person_fields(monkeypatch):
    from xml.etree.ElementTree import fromstring

    hmc = _client()
    _patch_common(monkeypatch, [])
    original_request = hmc._request_with_uuid_path_arguments.side_effect

    async def request(method, path, **kwargs):
        response = await original_request(method, path, **kwargs)
        atom = "{http://www.w3.org/2005/Atom}"
        root = fromstring(response.text)
        SubElement(root, atom + "category", term="synthetic")
        for parent in [root, *root.findall(atom + "entry")]:
            author = parent.find(atom + "author")
            if author is None:
                author = SubElement(parent, atom + "author")
            SubElement(author, atom + "name").text = "synthetic author"
            SubElement(author, atom + "uri").text = "https://hmc.example.test/author"
            SubElement(author, atom + "email").text = "synthetic@example.test"
        return httpx.Response(200, text=tostring(root, encoding="unicode"))

    hmc._request_with_uuid_path_arguments.side_effect = request
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID)
    assert result.workflow_completed and result.resource_deleted
    assert hmc.delete_adapter.await_count == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "case", ["client", "mapping-id", "unavailable", "partial", "casefold"]
)
async def test_mapping_evidence_and_rendering(monkeypatch, capsys, dry_run, case):
    from hmcpctl.cli_commands.lpar.decommission import _render_decommission_result
    from hmcpctl.cli_commands.output import InventoryFormat

    hmc = _client()
    _patch_common(monkeypatch, [])
    detail = _complete_storage_detail()
    mappings = detail["Resource"]["VirtualSCSIMappings"]["VirtualSCSIMapping"]
    mappings[0].pop("UUID")
    target = LPAR_UUID
    if case == "client":
        mappings[0].pop("AssociatedLogicalPartition")
    elif case == "mapping-id":
        mappings[0].pop("TargetDevice")
    elif case == "unavailable":
        detail = None
    elif case == "partial":
        for index in (1, 2):
            mapping = copy.deepcopy(mappings[0])
            mapping["TargetDevice"]["VirtualSCSITargetDevice"]["TargetName"] = (
                f"vtscsi{index}"
            )
            mappings.append(mapping)
        hmc.delete_storage_mapping.side_effect = [None, HMCError("second refused")]
    else:
        target = "abcdefab-abcd-abcd-abcd-abcdefabcdef"
        hmc.list_logical_partitions.return_value = [_lpar(target)]
        hmc.get_logical_partition.return_value = _lpar(target)
        mappings[0]["AssociatedLogicalPartition"]["href"] = (
            f"/rest/api/uom/LogicalPartition/{target.upper()}"
        )
    hmc.get_vios_storage_detail.return_value = detail
    result = await decommission_lpar(hmc, "system-a", target, dry_run=dry_run)
    evidence = result.blast_radius["mapping_evidence"]
    assert evidence and all(item["vios_uuid"] == VIOS_UUID for item in evidence)
    assert all(item["backing_device"] != "hdisk99" for item in evidence)
    scsi = [item for item in evidence if item["type"] == "VirtualSCSIMapping"]
    assert all(row["uuid"] == "unknown" for row in scsi)
    if case in ("client", "mapping-id"):
        assert scsi[0]["disposition"] == "unproven"
        assert scsi[0]["missing_identity"] != "none"
        assert scsi[0]["backing_device"] == "hdisk10"
        assert scsi[0]["server_adapter"] == "vhost0"
        if case == "mapping-id":
            assert "ServerAdapter.AdapterName" not in scsi[0]["missing_identity"]
        hmc.delete_storage_mapping.assert_not_awaited()
    elif case == "unavailable":
        assert evidence[0]["disposition"] == "unproven"
        assert evidence[0]["vios_name"] == "vios1"
        hmc.delete_storage_mapping.assert_not_awaited()
    elif case == "partial":
        expected = (
            ["planned detach"] * 3 if dry_run else ["detached", "failed", "retained"]
        )
        assert [row["disposition"] for row in scsi] == expected
    else:
        assert scsi[0]["disposition"] == ("planned detach" if dry_run else "detached")
        assert not any(
            row["type"] == "VirtualSCSIMapping"
            for row in result.blast_radius["storage_mappings"]
        )
    hmc.delete_virtual_disk.assert_not_awaited()
    _render_decommission_result(result, "aix-prod", InventoryFormat.tsv, "system-a")
    rendered = capsys.readouterr()
    assert rendered.out.startswith("Section\tItem\tField\tValue\n")
    assert VIOS_UUID in rendered.out and "--lpar" not in rendered.out
    assert "{'" not in rendered.out and "…" not in rendered.out
    assert "backing_storage" in rendered.out and "retained" in rendered.out


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_unresolved_duplicate_remains_unproven(monkeypatch, dry_run):
    hmc = _client()
    _patch_common(monkeypatch, [])
    detail = _complete_storage_detail()
    mappings = detail["Resource"]["VirtualSCSIMappings"]["VirtualSCSIMapping"]
    unknown = copy.deepcopy(mappings[0])
    unknown.pop("AssociatedLogicalPartition")
    mappings.append(unknown)
    hmc.get_vios_storage_detail.return_value = detail
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID, dry_run=dry_run)
    evidence = result.blast_radius["mapping_evidence"]
    unresolved = [row for row in evidence if row["relationship"] == "unknown"]
    assert len(unresolved) == 1
    assert unresolved[0]["disposition"] == "unproven"
    hmc.delete_storage_mapping.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,field",
    [
        ("PhysicalVolume", "VolumeName"),
        ("VirtualDisk", "DiskName"),
        ("LogicalUnit", "UnitName"),
        ("VirtualOpticalMedia", "MediaName"),
    ],
)
async def test_evidence_preserves_wrapped_backing_text(monkeypatch, kind, field):
    hmc = _client()
    _patch_common(monkeypatch, [])
    resource = element_to_dict(
        fromstring(f"""<VirtualIOServer>
      <VirtualSCSIMappings><VirtualSCSIMapping>
        <AssociatedLogicalPartition href="/rest/api/uom/LogicalPartition/{LPAR_UUID}"/>
        <ServerAdapter><AdapterName ksv="V1_0">vhost0</AdapterName></ServerAdapter>
        <TargetDevice><VirtualSCSITargetDevice><TargetName ksv="V1_0">vtscsi0</TargetName></VirtualSCSITargetDevice></TargetDevice>
        <Storage><{kind}><{field} ksv="V1_0">known-backing</{field}></{kind}></Storage>
      </VirtualSCSIMapping></VirtualSCSIMappings></VirtualIOServer>""")
    )
    hmc.get_vios_storage_detail.return_value = {"Resource": resource}
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID, dry_run=True)
    row = result.blast_radius["mapping_evidence"][0]
    assert row["backing_device"] == "known-backing"
    assert row["mapping_id"] == "vhost0/vtscsi0"
    assert "backing_device" not in result.blast_radius["storage_mappings"][0]


@pytest.mark.asyncio
async def test_empty_metadata_collection_has_no_mapping_evidence(monkeypatch):
    hmc = _client()
    _patch_common(monkeypatch, [])
    resource = element_to_dict(
        fromstring("""<VirtualIOServer>
      <VirtualSCSIMappings group="ViosSCSIMapping"><Metadata><Atom/></Metadata></VirtualSCSIMappings>
      <VirtualFibreChannelMappings group="ViosFCMapping"><Metadata><Atom/></Metadata></VirtualFibreChannelMappings>
      </VirtualIOServer>""")
    )
    hmc.get_vios_storage_detail.return_value = {"Resource": resource}
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID, dry_run=True)
    assert result.blast_radius["mapping_evidence"] == ()


@pytest.mark.asyncio
async def test_evidence_preserves_wrapped_vios_and_fc_identity(monkeypatch):
    hmc = _client()
    _patch_common(monkeypatch, [])
    resource = element_to_dict(
        fromstring(f"""<VirtualIOServer>
      <PartitionName ksv="V1_0">known-vios</PartitionName>
      <VirtualSCSIMappings/>
      <VirtualFibreChannelMappings><VirtualFibreChannelMapping>
      <UUID ksv="V1_0">aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa</UUID>
      <AssociatedLogicalPartition href="/rest/api/uom/LogicalPartition/{LPAR_UUID}"/>
      <Port><WWPNPair ksv="V1_0">known-port-pair</WWPNPair></Port>
      </VirtualFibreChannelMapping></VirtualFibreChannelMappings></VirtualIOServer>""")
    )
    hmc.list_vios.return_value = [{"UUID": VIOS_UUID, "Resource": resource}]
    hmc.get_vios_storage_detail.return_value = {"Resource": resource}
    result = await decommission_lpar(hmc, "system-a", LPAR_UUID, dry_run=True)
    row = result.blast_radius["mapping_evidence"][0]
    assert row["vios_name"] == "known-vios"
    assert row["uuid"] == row["mapping_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert row["backing_device"] == "known-port-pair"
