"""Direct boundary tests for LPAR, LPM, and network client mixins."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

import httpx
import pytest

from hmcpctl.client.client_lpars import LparsMixin
from hmcpctl.client.client_lpm import LpmMixin
from hmcpctl.client.client_network import NetworkMixin
from hmcpctl.client.client_resolution import MAX_PARENT_DISCOVERY_SYSTEMS
from hmcpctl.client.client_storage import StorageMixin
from hmcpctl.client.client_systems import SystemsMixin
from hmcpctl.client.client_templates import TemplatesMixin
from hmcpctl.client.client_updates import UpdatesMixin
from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError, HMCTLSFingerprintError

UUID_A = "12345678-1234-1234-1234-1234567890ab"
UUID_B = "ABCDEFAB-CDEF-CDEF-CDEF-ABCDEFABCDEF"


def test_platform_update_is_owned_by_updates_mixin() -> None:
    assert "submit_platform_update" in UpdatesMixin.__dict__
    assert issubclass(HMCClient, UpdatesMixin)


async def _yield_empty(_uuid: str) -> list[dict]:
    await asyncio.sleep(0)
    return []


class LparsHarness(LparsMixin):
    def __init__(self) -> None:
        self._get = AsyncMock(return_value="")
        self._put = AsyncMock(return_value="")
        self._post = AsyncMock(return_value="")
        self._delete = AsyncMock(return_value=None)
        self.list_uom = AsyncMock(return_value=[])
        self.get_uom = AsyncMock(return_value=None)
        self.search_uom = AsyncMock(return_value=[])
        self.list_system_summaries = AsyncMock(return_value=[])


class LpmHarness(LpmMixin):
    def __init__(self) -> None:
        self.submit_job = AsyncMock(return_value={"UUID": "job"})


class NetworkHarness(NetworkMixin):
    def __init__(self) -> None:
        self.config = HMCConfig(host="hmc.test", user="user", password="password")
        self._rest_base_url = "https://hmc.test:12443"
        self._get = AsyncMock(return_value="")
        self._put = AsyncMock(return_value="")
        self._delete = AsyncMock(return_value=None)


class TemplatesHarness(TemplatesMixin):
    def __init__(self, response: httpx.Response) -> None:
        self._request = AsyncMock(return_value=response)
        self.submit_job = AsyncMock(return_value={"UUID": "job-1"})
        self._session_token = "session-token"


class StorageHarness(StorageMixin):
    def __init__(self) -> None:
        self.config = HMCConfig(host="hmc.test", user="user", password="password")
        self._rest_base_url = "https://hmc.test:12443"
        self._get = AsyncMock(return_value="")
        self._put = AsyncMock(return_value="")
        self._post = AsyncMock(return_value="")
        self._request = AsyncMock(return_value=httpx.Response(200))


class SystemsHarness(SystemsMixin):
    def __init__(self) -> None:
        self.list_system_summaries = AsyncMock(return_value=[])
        self.list_uom = AsyncMock(return_value=[])
        self.get_uom = AsyncMock(return_value=None)
        self.search_uom = AsyncMock(return_value=[])
        self._get = AsyncMock(return_value="")
        self._post = AsyncMock(return_value="")
        self.submit_job = AsyncMock(return_value={"UUID": "job-1"})


def _summary(uuid: str, name: str, state: str = "operating") -> dict:
    """One ``ManagedSystem/quick/All`` item."""
    return {"UUID": uuid, "SystemName": name, "State": state}


def _entry(uuid: str, name: str, resource_type: str) -> dict:
    name_key = "SystemName" if resource_type == "ManagedSystem" else "PartitionName"
    return {
        "UUID": uuid,
        "ResourceType": resource_type,
        "Resource": {name_key: name},
    }


@pytest.mark.asyncio
async def test_lpar_mixin_routes_scoped_and_global_reads():
    client = LparsHarness()
    client.list_system_summaries.return_value = [{"UUID": UUID_A, "State": "operating"}]

    assert await client.list_logical_partitions(UUID_A) == []
    assert await client.get_logical_partition("lpar-1") is None
    assert await client.find_partition_by_name("aix1") is None

    client._get.assert_awaited_once_with(
        f"/rest/api/uom/ManagedSystem/{UUID_A}/LogicalPartition",
        "LogicalPartition",
    )
    client.list_uom.assert_not_awaited()
    client.get_uom.assert_awaited_once_with("LogicalPartition", "lpar-1")
    client.search_uom.assert_awaited_once_with(
        "LogicalPartition", "PartitionName", "aix1"
    )


@pytest.mark.asyncio
async def test_lpar_finder_rejects_ambiguous_names_with_parent_systems():
    client = LparsHarness()
    lpar_a = _entry("lpar-a", "shared", "LogicalPartition")
    lpar_b = _entry("lpar-b", "shared", "LogicalPartition")
    client.search_uom.return_value = [lpar_b, lpar_a]
    client.list_system_summaries.return_value = [
        _summary("sys-b", "system-b"),
        _summary("sys-a", "system-a"),
    ]
    client.list_logical_partitions = AsyncMock(
        side_effect=lambda uuid: [lpar_a] if uuid == "sys-a" else [lpar_b]
    )

    with pytest.raises(ValueError) as exc_info:
        await client.find_partition_by_name("shared")

    message = str(exc_info.value)
    assert message.index("system-a") < message.index("system-b")
    assert all(value in message for value in ("lpar-a", "lpar-b", "sys-a", "sys-b"))


@pytest.mark.asyncio
async def test_lpar_finder_requires_exactly_one_parent_per_candidate():
    client = LparsHarness()
    lpar_a = _entry("lpar-a", "shared", "LogicalPartition")
    lpar_b = _entry("lpar-b", "shared", "LogicalPartition")
    client.search_uom.return_value = [lpar_a, lpar_b]
    client.list_system_summaries.return_value = [
        _summary("sys-a", "system-a"),
        _summary("sys-b", "system-b"),
    ]
    client.list_logical_partitions = AsyncMock(return_value=[lpar_a, lpar_b])

    with pytest.raises(ValueError, match="exactly one managed system"):
        await client.find_partition_by_name("shared")


@pytest.mark.asyncio
async def test_lpar_finder_propagates_parent_discovery_failure():
    client = LparsHarness()
    client.search_uom.return_value = [
        _entry("lpar-a", "shared", "LogicalPartition"),
        _entry("lpar-b", "shared", "LogicalPartition"),
    ]
    client.list_system_summaries.side_effect = HMCError("inventory unavailable")

    with pytest.raises(HMCError, match="inventory unavailable"):
        await client.find_partition_by_name("shared")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent",
    [
        {"SystemName": "system-a", "State": "operating"},
        {"UUID": "sys-a", "State": "operating"},
    ],
)
async def test_lpar_finder_rejects_incomplete_parent_metadata(parent):
    client = LparsHarness()
    client.search_uom.return_value = [
        _entry("lpar-a", "shared", "LogicalPartition"),
        _entry("lpar-b", "shared", "LogicalPartition"),
    ]
    client.list_system_summaries.return_value = [parent]

    with pytest.raises(ValueError, match="cannot identify managed system"):
        await client.find_partition_by_name("shared")


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("candidate_ids", [[None, "lpar-b"], ["lpar-a", "lpar-a"]])
async def test_lpar_finder_rejects_invalid_candidate_ids(scoped, candidate_ids):
    client = LparsHarness()
    candidates = [_entry(uuid, "shared", "LogicalPartition") for uuid in candidate_ids]
    client.search_uom.return_value = candidates
    client.list_logical_partitions = AsyncMock(return_value=candidates)

    with pytest.raises(ValueError, match="candidate UUID metadata"):
        await client.find_partition_by_name(
            "shared", system_uuid="sys-a" if scoped else None
        )


@pytest.mark.asyncio
async def test_lpar_parent_discovery_has_request_budget():
    client = LparsHarness()
    client.search_uom.return_value = [
        _entry("lpar-a", "shared", "LogicalPartition"),
        _entry("lpar-b", "shared", "LogicalPartition"),
    ]
    client.list_system_summaries.return_value = [
        _summary(f"sys-{index}", f"system-{index}")
        for index in range(MAX_PARENT_DISCOVERY_SYSTEMS + 1)
    ]

    with pytest.raises(ValueError, match="supply managed-system scope"):
        await client.find_partition_by_name("shared")


@pytest.mark.asyncio
async def test_lpar_parent_discovery_has_total_deadline(monkeypatch):
    client = LparsHarness()
    client.search_uom.return_value = [
        _entry("lpar-a", "shared", "LogicalPartition"),
        _entry("lpar-b", "shared", "LogicalPartition"),
    ]
    client.list_system_summaries.return_value = [_summary("sys-a", "system-a")]
    client.list_logical_partitions = AsyncMock(side_effect=_yield_empty)
    monkeypatch.setattr(
        "hmcpctl.client.client_resolution.PARENT_DISCOVERY_TIMEOUT_SECONDS", 0
    )

    with pytest.raises(ValueError, match="timed out; supply managed-system scope"):
        await client.find_partition_by_name("shared")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 2])
async def test_lpar_finder_scoped_zero_one_many(count):
    client = LparsHarness()
    matches = [
        _entry(f"lpar-{index}", "shared", "LogicalPartition") for index in range(count)
    ]
    client.list_logical_partitions = AsyncMock(
        return_value=[*matches, _entry("other", "other", "LogicalPartition")]
    )
    client.list_system_summaries.return_value = [_summary("sys-a", "system-a")]

    if count == 2:
        with pytest.raises(ValueError, match="lpar-0.*system-a.*lpar-1"):
            await client.find_partition_by_name("shared", system_uuid="sys-a")
    else:
        expected = matches[0] if matches else None
        assert (
            await client.find_partition_by_name("shared", system_uuid="sys-a")
            == expected
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [None, {"UUID": "sys-a", "State": "operating"}])
async def test_lpar_scoped_ambiguity_requires_parent_name(parent):
    client = LparsHarness()
    client.list_logical_partitions = AsyncMock(
        return_value=[
            _entry("lpar-a", "shared", "LogicalPartition"),
            _entry("lpar-b", "shared", "LogicalPartition"),
        ]
    )
    client.list_system_summaries.return_value = [parent] if parent else []

    with pytest.raises(ValueError, match="cannot identify managed system sys-a"):
        await client.find_partition_by_name("shared", system_uuid="sys-a")


@pytest.mark.asyncio
async def test_lpar_mixin_writes_use_schema_compatible_paths():
    client = LparsHarness()

    assert await client.create_logical_partition(UUID_A, "<lpar/>") is None
    await client.delete_logical_partition(UUID_B)

    client._put.assert_awaited_once_with(
        f"/rest/api/uom/ManagedSystem/{UUID_A}/LogicalPartition",
        "<lpar/>",
        resource_type="LogicalPartition",
        include_schema_version=False,
    )
    client._delete.assert_awaited_once_with(f"/rest/api/uom/LogicalPartition/{UUID_B}")


@pytest.mark.asyncio
async def test_lpm_mixin_submits_each_operation_to_lpar_endpoint():
    client = LpmHarness()

    await client.lpar_migrate(UUID_A, "target-system")
    await client.lpar_migrate_validate(UUID_A, "target-system")
    await client.lpar_migrate_abort(UUID_A)
    await client.lpar_migrate_recover(UUID_A)
    await client.lpar_remote_restart(
        UUID_A, "restart", "source-system", target_managed_system="target-system"
    )

    operations = [
        call.args[0].rsplit("/", 1)[-1] for call in client.submit_job.await_args_list
    ]
    assert operations == [
        "Migrate",
        "MigrateValidate",
        "MigrateAbort",
        "MigrateRecover",
        "RemoteRestart",
    ]
    assert all(
        "target-system" in call.args[1]
        for call in client.submit_job.await_args_list[:2]
    )


def test_lpm_optional_controls_are_keyword_only():
    for method in (LpmMixin.lpar_migrate, LpmMixin.lpar_migrate_validate):
        parameters = inspect.signature(method).parameters
        for name in (
            "target_profile_name",
            "destination_lpar_id",
            "shared_proc_pool_id",
            "wait_time",
        ):
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_network_mixin_routes_empty_feeds_and_delete():
    client = NetworkHarness()

    assert await client.list_virtual_switches(UUID_A) == []
    assert await client.list_virtual_networks(UUID_A) == []
    assert await client.list_network_bridges(UUID_A) == []
    assert (
        await client.create_virtual_network(
            UUID_A, "net", 100, 0, switch_uuid="switch-1"
        )
        is None
    )
    await client.delete_virtual_network(UUID_A, UUID_B)

    assert [call.args[1] for call in client._get.await_args_list] == [
        "VirtualSwitch",
        "VirtualNetwork",
        "NetworkBridge",
    ]
    client._put.assert_awaited_once()
    assert (
        f"https://hmc.test:12443/rest/api/uom/ManagedSystem/{UUID_A}/"
        "VirtualSwitch/switch-1" in client._put.await_args.args[1]
    )
    assert client._put.await_args.kwargs == {
        "resource_type": "VirtualNetwork",
        "include_schema_version": False,
    }
    client._delete.assert_awaited_once_with(
        f"/rest/api/uom/ManagedSystem/{UUID_A}/VirtualNetwork/{UUID_B}"
    )


@pytest.mark.asyncio
async def test_templates_mixin_handles_http_response_contracts():
    client = TemplatesHarness(httpx.Response(204))

    assert await client.list_partition_templates() == []
    client._request.assert_awaited_once_with(
        "GET",
        "/rest/api/templates/PartitionTemplate",
        headers={"Accept": "application/atom+xml"},
    )

    client._request.return_value = httpx.Response(503, text="unavailable")
    with pytest.raises(
        HMCError, match="GET /rest/api/templates/PartitionTemplate failed"
    ):
        await client.list_partition_templates()


@pytest.mark.asyncio
async def test_templates_mixin_routes_deployment_job():
    client = TemplatesHarness(httpx.Response(200))

    assert await client.deploy_partition_template("draft-1", "system-1") == {
        "UUID": "job-1"
    }
    path, document = client.submit_job.await_args.args
    assert path == "/rest/api/templates/PartitionTemplate/draft-1/do/deploy"
    assert "system-1" in document
    assert "session-token" in document


@pytest.mark.asyncio
async def test_storage_mixin_routes_schema_sensitive_operations():
    client = StorageHarness()
    vios_uuid = "11111111-1111-1111-1111-111111111111"

    assert client.get_lpar_link(UUID_B, UUID_A) == (
        f"https://hmc.test:12443/rest/api/uom/ManagedSystem/{UUID_B}/LogicalPartition/{UUID_A}"
    )
    assert await client.list_volume_groups(vios_uuid) == []

    assert client._get.await_args.kwargs == {
        "include_schema_version": False,
        "uuid_path_arguments": {"vios_uuid": vios_uuid},
    }


@pytest.mark.asyncio
async def test_storage_mixin_uses_active_base_in_optical_mapping():
    client = StorageHarness()
    vios_uuid = "11111111-1111-1111-1111-111111111111"
    system_uuid = "33333333-3333-3333-3333-333333333333"
    vios = (
        '<VirtualIOServer xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/'
        f'mc/2012_10/"><Metadata><Atom><AtomID>{vios_uuid}</AtomID></Atom></Metadata>'
        f'<PartitionUUID kb="ROO">{vios_uuid}</PartitionUUID>'
        f'<AssociatedManagedSystem kb="CUD" kxe="false" rel="related" '
        f'href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/{system_uuid}"/>'
        "<VirtualSCSIMappings/></VirtualIOServer>"
    )
    client._request_with_uuid_path_arguments = AsyncMock(
        side_effect=[
            httpx.Response(200, text=vios, headers={"ETag": "etag-1"}),
            httpx.Response(200),
        ]
    )

    await client.create_optical_mapping(vios_uuid, "install.iso", UUID_A)

    body = client._request_with_uuid_path_arguments.await_args.kwargs["content"]
    assert (
        f"https://hmc.test:12443/rest/api/uom/ManagedSystem/{system_uuid}"
        f"/LogicalPartition/{UUID_A}" in body
    )


@pytest.mark.asyncio
async def test_storage_mixin_reports_a_possible_side_effect_after_a_5xx():
    client = StorageHarness()
    after = {"virtual_disks": ["boot", "data"]}
    snapshot = AsyncMock(return_value=after)
    dispatch = AsyncMock(side_effect=HMCError("write failed", 503, "unavailable"))

    with pytest.raises(
        HMCError, match="possible side effect.*Do not retry"
    ) as exc_info:
        await client._reconcile_storage_mutation(
            "create_virtual_disk", snapshot, dispatch
        )

    assert exc_info.value.__cause__ is dispatch.side_effect
    snapshot.assert_awaited_once()
    dispatch.assert_awaited_once()
    assert "readback completed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_systems_mixin_routes_inventory_and_power_jobs():
    client = SystemsHarness()
    client.list_system_summaries = AsyncMock(return_value=[])

    assert await client.get_managed_system("system-1") is None
    assert await client.find_vios_by_name("vios-a") is None
    assert await client.power_off_system(UUID_A, immediate=True) == {"UUID": "job-1"}

    client.list_uom.assert_not_awaited()
    client.get_uom.assert_awaited_once_with("ManagedSystem", "system-1")
    client.search_uom.assert_awaited_once_with(
        "VirtualIOServer", "PartitionName", "vios-a"
    )
    assert client.submit_job.await_args.args[0].endswith(f"/{UUID_A}/do/PowerOff")


@pytest.mark.asyncio
async def test_system_finder_rejects_ambiguous_names():
    client = SystemsHarness()
    client.search_uom.return_value = [
        _entry("sys-b", "duplicate", "ManagedSystem"),
        _entry("sys-a", "duplicate", "ManagedSystem"),
    ]

    with pytest.raises(ValueError, match="sys-a.*sys-b"):
        await client.find_system_by_name("duplicate")


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_ids", [[None, "sys-b"], ["sys-a", "sys-a"]])
async def test_system_finder_rejects_invalid_candidate_ids(candidate_ids):
    client = SystemsHarness()
    client.search_uom.return_value = [
        _entry(uuid, "duplicate", "ManagedSystem") for uuid in candidate_ids
    ]

    with pytest.raises(ValueError, match="candidate UUID metadata"):
        await client.find_system_by_name("duplicate")


@pytest.mark.asyncio
async def test_vios_finder_rejects_ambiguous_names_with_parent_systems():
    client = SystemsHarness()
    vios_a = _entry("vios-a", "shared", "VirtualIOServer")
    vios_b = _entry("vios-b", "shared", "VirtualIOServer")
    client.search_uom.return_value = [vios_b, vios_a]
    client.list_system_summaries = AsyncMock(
        return_value=[
            _summary("sys-a", "system-a"),
            _summary("sys-b", "system-b"),
        ]
    )
    client.list_vios = AsyncMock(
        side_effect=lambda uuid: [vios_a] if uuid == "sys-a" else [vios_b]
    )

    with pytest.raises(ValueError) as exc_info:
        await client.find_vios_by_name("shared")

    message = str(exc_info.value)
    assert message.index("system-a") < message.index("system-b")
    assert all(value in message for value in ("vios-a", "vios-b", "sys-a", "sys-b"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent",
    [
        {"SystemName": "system-a", "State": "operating"},
        {"UUID": "sys-a", "State": "operating"},
    ],
)
async def test_vios_finder_rejects_incomplete_parent_metadata(parent):
    client = SystemsHarness()
    client.search_uom.return_value = [
        _entry("vios-a", "shared", "VirtualIOServer"),
        _entry("vios-b", "shared", "VirtualIOServer"),
    ]
    client.list_system_summaries = AsyncMock(return_value=[parent])

    with pytest.raises(ValueError, match="cannot identify managed system"):
        await client.find_vios_by_name("shared")


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("candidate_ids", [[None, "vios-b"], ["vios-a", "vios-a"]])
async def test_vios_finder_rejects_invalid_candidate_ids(scoped, candidate_ids):
    client = SystemsHarness()
    candidates = [_entry(uuid, "shared", "VirtualIOServer") for uuid in candidate_ids]
    client.search_uom.return_value = candidates
    client.list_vios = AsyncMock(return_value=candidates)

    with pytest.raises(ValueError, match="candidate UUID metadata"):
        await client.find_vios_by_name(
            "shared", system_uuid="sys-a" if scoped else None
        )


@pytest.mark.asyncio
async def test_vios_parent_discovery_has_request_budget():
    client = SystemsHarness()
    client.search_uom.return_value = [
        _entry("vios-a", "shared", "VirtualIOServer"),
        _entry("vios-b", "shared", "VirtualIOServer"),
    ]
    client.list_system_summaries = AsyncMock(
        return_value=[
            _summary(f"sys-{index}", f"system-{index}")
            for index in range(MAX_PARENT_DISCOVERY_SYSTEMS + 1)
        ]
    )

    with pytest.raises(ValueError, match="supply managed-system scope"):
        await client.find_vios_by_name("shared")


@pytest.mark.asyncio
async def test_vios_parent_discovery_has_total_deadline(monkeypatch):
    client = SystemsHarness()
    client.search_uom.return_value = [
        _entry("vios-a", "shared", "VirtualIOServer"),
        _entry("vios-b", "shared", "VirtualIOServer"),
    ]
    client.list_system_summaries = AsyncMock(
        return_value=[_summary("sys-a", "system-a")]
    )
    client.list_vios = AsyncMock(side_effect=_yield_empty)
    monkeypatch.setattr(
        "hmcpctl.client.client_resolution.PARENT_DISCOVERY_TIMEOUT_SECONDS", 0
    )

    with pytest.raises(ValueError, match="timed out; supply managed-system scope"):
        await client.find_vios_by_name("shared")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 2])
async def test_vios_finder_scoped_zero_one_many(count):
    client = SystemsHarness()
    matches = [
        _entry(f"vios-{index}", "shared", "VirtualIOServer") for index in range(count)
    ]
    client.list_vios = AsyncMock(
        return_value=[*matches, _entry("other", "other", "VirtualIOServer")]
    )
    client.list_system_summaries = AsyncMock(
        return_value=[_summary("sys-a", "system-a")]
    )

    if count == 2:
        with pytest.raises(ValueError, match="vios-0.*system-a.*vios-1"):
            await client.find_vios_by_name("shared", system_uuid="sys-a")
    else:
        expected = matches[0] if matches else None
        assert await client.find_vios_by_name("shared", system_uuid="sys-a") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [None, {"UUID": "sys-a", "State": "operating"}])
async def test_vios_scoped_ambiguity_requires_parent_name(parent):
    client = SystemsHarness()
    client.list_vios = AsyncMock(
        return_value=[
            _entry("vios-a", "shared", "VirtualIOServer"),
            _entry("vios-b", "shared", "VirtualIOServer"),
        ]
    )
    client.list_system_summaries = AsyncMock(return_value=[parent] if parent else [])

    with pytest.raises(ValueError, match="cannot identify managed system sys-a"):
        await client.find_vios_by_name("shared", system_uuid="sys-a")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dispatch", "readback"])
async def test_storage_reconciliation_preserves_tls_identity(phase):
    client = StorageHarness()
    failure = HMCTLSFingerprintError("Certificate fingerprint differs")
    dispatch = AsyncMock(
        side_effect=failure if phase == "dispatch" else HMCError("write failed", 503)
    )
    snapshot = AsyncMock(side_effect=failure)
    with pytest.raises(HMCTLSFingerprintError) as caught:
        await client._reconcile_storage_mutation(
            "create_virtual_disk", snapshot, dispatch
        )
    assert caught.value is failure
    dispatch.assert_awaited_once()
    assert snapshot.await_count == (phase == "readback")


@pytest.mark.asyncio
async def test_storage_reconciliation_keeps_ordinary_readback_failure():
    client = StorageHarness()
    write_failure = HMCError("write failed", 503, "unavailable")
    dispatch = AsyncMock(side_effect=write_failure)
    snapshot = AsyncMock(side_effect=HMCError("read unavailable", 502))
    with pytest.raises(HMCError, match="readback failed: read unavailable") as caught:
        await client._reconcile_storage_mutation(
            "create_virtual_disk", snapshot, dispatch
        )
    assert caught.value.__cause__ is write_failure
    assert caught.value.status_code == 503
    assert caught.value.body == write_failure.body
    dispatch.assert_awaited_once()
    snapshot.assert_awaited_once()
