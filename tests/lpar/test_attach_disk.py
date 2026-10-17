"""Tests for attaching a new virtual disk to an existing LPAR."""

from unittest.mock import AsyncMock

import pytest
from conftest import assert_only_these_client_methods_used

from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.assignments import WorkflowStep
from hmcpctl.operations.lpar.provision import (
    AttachDiskResult,
    ProvisionStorage,
    attach_disk_to_lpar,
)

LPAR_UUID = "11111111-1111-1111-1111-111111111111"
VIOS_UUID = "22222222-2222-2222-2222-222222222222"
VG_UUID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _authorize_lpar_mutations(monkeypatch):
    async def authorize(_hmc, _system, lpar, **_kwargs):
        return lpar

    monkeypatch.setattr(
        "hmcpctl.operations.lpar.provision.resolve_and_authorize_lpar_mutation",
        authorize,
    )


def _client() -> AsyncMock:
    client = AsyncMock()
    client.find_partition_by_name.return_value = {"UUID": LPAR_UUID}
    client.list_volume_groups.return_value = [{"UUID": VG_UUID}]
    client.get_logical_partition.return_value = {"Resource": {}}
    return client


def _storage() -> ProvisionStorage:
    return ProvisionStorage(VIOS_UUID, "disk01", vg_uuid=VG_UUID)


@pytest.mark.asyncio
async def test_attach_disk_dry_run_validates_without_mutating() -> None:
    client = _client()

    result = await attach_disk_to_lpar(
        client,
        None,
        "existing-lpar",
        _storage(),
        capacity_mib=1024,
        dry_run=True,
    )

    assert result == AttachDiskResult(
        workflow_completed=False,
        lpar_uuid=LPAR_UUID,
        dry_run=True,
        steps=(
            WorkflowStep("create_disk", "dry_run"),
            WorkflowStep("storage", "dry_run"),
        ),
        warnings=(),
    )
    client.list_volume_groups.assert_awaited_once_with(VIOS_UUID)
    client.create_virtual_disk.assert_not_awaited()
    client.add_vscsi_adapter.assert_not_awaited()
    client.map_storage_to_lpar.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_disk_runs_shared_storage_leg_in_order() -> None:
    client = _client()
    calls: list[str] = []
    client.create_virtual_disk.side_effect = lambda *args: calls.append("create_disk")
    client.add_vscsi_adapter.side_effect = lambda *args: calls.append("vscsi")
    client.map_storage_to_lpar.side_effect = lambda *args: calls.append("storage")

    result = await attach_disk_to_lpar(
        client,
        None,
        LPAR_UUID,
        _storage(),
        capacity_mib=1024,
    )

    # The mapping makes the HMC create its own client/server adapter pair
    # (ADR 0169), so no client adapter is added beforehand (#1030).
    assert calls == ["create_disk", "storage"]
    assert [step.step for step in result.steps] == ["create_disk", "storage"]
    assert result.workflow_completed is True
    assert [step.status for step in result.steps] == ["ok", "ok"]
    assert result.steps[0].result == {
        "disk_name": "disk01",
        "capacity_mib": 1024,
    }
    assert result.steps[1].result == {
        "lpar_uuid": LPAR_UUID,
        "vios_uuid": VIOS_UUID,
        "storage_name": "disk01",
    }


@pytest.mark.asyncio
async def test_attach_disk_reports_a_failed_mapping_after_the_disk() -> None:
    client = _client()
    client.map_storage_to_lpar.side_effect = HMCError("mapping failed")

    result = await attach_disk_to_lpar(
        client,
        None,
        LPAR_UUID,
        _storage(),
        capacity_mib=1024,
    )

    assert [(step.step, step.status) for step in result.steps] == [
        ("create_disk", "ok"),
        ("storage", "error"),
    ]
    assert result.workflow_completed is False
    client.add_vscsi_adapter.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("storage_name", "capacity_mib", "message"),
    [
        ("disk01", 0, "positive multiple of 1024"),
        ("disk01", 1500, "positive multiple of 1024"),
        ("a_sixteen_chars_", 1024, "15 characters"),
    ],
)
async def test_attach_disk_refuses_invalid_disk_before_any_hmc_request(
    dry_run: bool, storage_name: str, capacity_mib: int, message: str
) -> None:
    client = _client()
    storage = ProvisionStorage(VIOS_UUID, storage_name, vg_uuid=VG_UUID)

    with pytest.raises(ValueError, match=message):
        await attach_disk_to_lpar(
            client, None, LPAR_UUID, storage, capacity_mib=capacity_mib, dry_run=dry_run
        )

    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_attach_disk_dry_run_makes_no_unclassified_call() -> None:
    """R18: the whole call set is pinned, not three negatives.

    The test above names the three mutations it expects not to happen. That
    stays green if a fourth is added, which is the inference epic #218
    requirement 5 refuses. Here every method the handler touched is read back and
    compared against the classified read-only set.
    """
    client = _client()

    await attach_disk_to_lpar(
        client,
        None,
        "existing-lpar",
        _storage(),
        capacity_mib=1024,
        dry_run=True,
    )

    used = assert_only_these_client_methods_used(
        client,
        frozenset(
            {
                "find_partition_by_name",  # read: resolve the LPAR name to a UUID
                "get_logical_partition",  # read: UUID pass-through validation
                "list_volume_groups",  # read: volume-group precondition check
            }
        ),
    )
    assert used, "the handler touched nothing; the dry-run path was not exercised"


def _with_sync(client: AsyncMock, sync: str | None) -> None:
    resource = {} if sync is None else {"CurrentProfileSync": sync}
    client.get_logical_partition.return_value = {"Resource": resource}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sync", "lives_in"),
    [
        ("On", "current-configuration-and-profile"),
        ("Disabled", "current-configuration"),
    ],
)
async def test_attach_disk_reports_change_location(sync: str, lives_in: str) -> None:
    """#1069: like provision, attach-disk says where the new mapping lives."""
    client = _client()
    _with_sync(client, sync)

    result = await attach_disk_to_lpar(
        client, None, LPAR_UUID, _storage(), capacity_mib=1024
    )

    assert result.change_location is not None
    assert result.change_location.current_profile_sync == sync
    assert result.change_location.lives_in == lives_in
    assert result.warnings == ()
    client.get_logical_partition.assert_awaited_once_with(LPAR_UUID)


@pytest.mark.asyncio
async def test_attach_disk_reports_change_location_after_a_failed_mapping() -> None:
    client = _client()
    _with_sync(client, "Disabled")
    client.map_storage_to_lpar.side_effect = HMCError("mapping failed")

    result = await attach_disk_to_lpar(
        client, None, LPAR_UUID, _storage(), capacity_mib=1024
    )

    assert result.workflow_completed is False
    assert result.change_location is not None
    assert result.change_location.lives_in == "current-configuration"


@pytest.mark.asyncio
async def test_attach_disk_change_location_read_failure_is_advisory() -> None:
    client = _client()
    client.get_logical_partition.side_effect = HMCError("boom")

    result = await attach_disk_to_lpar(
        client, None, LPAR_UUID, _storage(), capacity_mib=1024
    )

    assert result.workflow_completed is True
    assert result.change_location is None
    assert any("Change location not read" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_attach_disk_change_location_is_read_before_the_storage_leg() -> None:
    client = _client()
    calls: list[str] = []
    client.map_storage_to_lpar.side_effect = lambda *args: calls.append("storage")

    async def read(_uuid):
        calls.append("read")
        return {"Resource": {}}

    client.get_logical_partition.side_effect = read

    await attach_disk_to_lpar(client, None, LPAR_UUID, _storage(), capacity_mib=1024)

    assert calls == ["read", "storage"]


@pytest.mark.asyncio
async def test_attach_disk_dry_run_reads_no_change_location() -> None:
    client = _client()

    result = await attach_disk_to_lpar(
        client, None, LPAR_UUID, _storage(), capacity_mib=1024, dry_run=True
    )

    assert result.change_location is None
    client.get_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_disk_dry_run_refuses_duplicate_volume_group_identity():
    client = _client()
    client.list_volume_groups.return_value = [{"UUID": VG_UUID}, {"UUID": VG_UUID}]
    with pytest.raises(ValueError, match="ambiguous"):
        await attach_disk_to_lpar(
            client, None, LPAR_UUID, _storage(), capacity_mib=1024, dry_run=True
        )
    client.create_virtual_disk.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_disk_typed_storage_requires_volume_group_uuid():
    client = _client()
    with pytest.raises(ValueError, match="vg_uuid must be a canonical UUID"):
        await attach_disk_to_lpar(
            client,
            None,
            LPAR_UUID,
            ProvisionStorage(VIOS_UUID, "disk01", vg_uuid="rootvg"),
            capacity_mib=1024,
            dry_run=True,
        )
    client.list_volume_groups.assert_not_awaited()
    client.create_virtual_disk.assert_not_awaited()
