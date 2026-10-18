"""System-wide LPAR profile restore ownership boundary (issue #449)."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.configuration import restore_system_lpar_profiles
from hmcpctl.operations.lpar.ownership import _authorize_system_lpar_profile_restore
from hmcpctl.server_tools.lpar import profiles as server_profiles

SYSTEM_UUID = "11111111-1111-4111-8111-111111111111"
SYSTEM_NAME = "frame1"


def _hmc() -> AsyncMock:
    hmc = AsyncMock()
    hmc.config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "u",
            "password": "p",
            "agent_id": "alice",
        }
    )
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME}
    ]
    return hmc


@pytest.mark.asyncio
async def test_foreign_partition_blocks_restore_before_ssh(caplog) -> None:
    hmc = _hmc()
    hmc.list_logical_partitions.return_value = [
        {
            "UUID": "22222222-2222-4222-8222-222222222222",
            "Resource": {
                "PartitionName": "db01",
                "Description": "[hmcpctl owner:bob created:2026-08-14]",
            },
        }
    ]
    write = AsyncMock()

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(PermissionError, match="db01"),
        patch(
            "hmcpctl.operations.lpar.configuration.restore_lpar_profiles",
            new=write,
        ),
    ):
        await restore_system_lpar_profiles(hmc, SYSTEM_UUID, "/tmp/profiles.bak", 1)

    write.assert_not_awaited()
    records = [json.loads(record.message) for record in caplog.records]
    record = next(record for record in records if record["event"] == "ownership-denied")
    assert record["operation"] == "lpar-profile-restore"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        {"PartitionName": None, "Description": ""},
        {"PartitionName": " ", "Description": ""},
        {"PartitionName": "db01", "Description": 3},
    ],
)
async def test_incomplete_ownership_row_fails_closed(row: dict[str, object]) -> None:
    hmc = _hmc()
    write = AsyncMock()
    hmc.list_logical_partitions.return_value = [{"Resource": row}]

    with pytest.raises(ValueError, match="ownership inventory"):
        await _authorize_system_lpar_profile_restore(hmc, SYSTEM_UUID)

    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_clean_current_inventory_cannot_authorize_opaque_backup() -> None:
    hmc = _hmc()
    hmc.list_logical_partitions.return_value = [
        {
            "UUID": "22222222-2222-4222-8222-222222222222",
            "Resource": {"PartitionName": "db01", "Description": None},
        }
    ]

    with pytest.raises(PermissionError, match="backup-only"):
        await _authorize_system_lpar_profile_restore(hmc, SYSTEM_UUID)


@pytest.mark.asyncio
async def test_unavailable_inventory_fails_with_actionable_error() -> None:
    hmc = _hmc()
    cause = HMCError("feed unavailable")
    hmc.list_logical_partitions.side_effect = cause

    with pytest.raises(ValueError, match="inventory is unavailable") as exc_info:
        await _authorize_system_lpar_profile_restore(hmc, SYSTEM_UUID)

    assert exc_info.value.__cause__ is cause


@pytest.mark.asyncio
async def test_override_skips_inventory_audits_wildcard_and_restores(caplog) -> None:
    hmc = _hmc()
    write = AsyncMock(return_value="restored")

    with (
        caplog.at_level(logging.WARNING),
        patch("hmcpctl.operations.lpar.configuration.restore_lpar_profiles", new=write),
    ):
        result = await restore_system_lpar_profiles(
            hmc,
            SYSTEM_UUID,
            "/tmp/profiles.bak",
            1,
            ownership_override=True,
        )

    assert result == "restored"
    hmc.list_logical_partitions.assert_not_awaited()
    write.assert_awaited_once_with(
        hmc.config, SYSTEM_NAME, "/tmp/profiles.bak", 1, force_keystore=False
    )
    records = [json.loads(record.message) for record in caplog.records]
    record = next(
        record for record in records if record["event"] == "ownership-override"
    )
    assert record["lpar"] == "*"
    assert record["system"] == SYSTEM_NAME


def test_tool_requires_approval_before_opening_client(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(server_profiles, "with_client", run)

    with pytest.raises(PermissionError, match="overwrites every profile"):
        server_profiles.hmc_restore_lpar_profiles(
            SYSTEM_UUID, "/tmp/profiles.bak", restore_type=1
        )

    run.assert_not_called()


def test_tool_delegates_restore_and_override_through_managed_client(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    hmc = _hmc()
    restore = AsyncMock(return_value="restored")

    def run(fn, *, profile=None):
        captured["profile"] = profile
        return asyncio.run(fn(hmc))

    monkeypatch.setattr(server_profiles, "with_client", run)
    monkeypatch.setattr(server_profiles, "restore_system_lpar_profiles", restore)

    result = server_profiles.hmc_restore_lpar_profiles(
        SYSTEM_UUID,
        "/tmp/profiles.bak",
        system_wide_restore_approved=True,
        ownership_override=True,
        profile="lab",
        restore_type=2,
    )

    assert result == "restored"
    assert captured["profile"] == "lab"
    restore.assert_awaited_once_with(
        hmc,
        SYSTEM_UUID,
        "/tmp/profiles.bak",
        2,
        ownership_override=True,
        force_keystore=False,
    )


def test_existing_positional_profile_cannot_become_ownership_override(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    hmc = _hmc()
    restore = AsyncMock(return_value="restored")

    def run(fn, *, profile=None):
        captured["profile"] = profile
        return asyncio.run(fn(hmc))

    monkeypatch.setattr(server_profiles, "with_client", run)
    monkeypatch.setattr(server_profiles, "restore_system_lpar_profiles", restore)

    result = server_profiles.hmc_restore_lpar_profiles(
        SYSTEM_UUID, "/tmp/profiles.bak", True, "lab", restore_type=3
    )

    assert result == "restored"
    assert captured["profile"] == "lab"
    restore.assert_awaited_once_with(
        hmc,
        SYSTEM_UUID,
        "/tmp/profiles.bak",
        3,
        ownership_override=False,
        force_keystore=False,
    )
