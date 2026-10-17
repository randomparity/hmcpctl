from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import hmcpctl.config as config_module
from hmcpctl.config import HMCConfig, build_config
from hmcpctl.resource_identity import (
    ResourceNotFoundError,
    resolve_lpar_uuid,
    resolve_system_name,
    resolve_vios_uuid,
)


@pytest.mark.parametrize(
    ("base", "port_is_explicit"),
    [
        (HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"}), False),
        (
            HMCConfig.from_mapping(
                {"host": "h", "user": "u", "password": "p", "port": 12443}
            ),
            True,
        ),
    ],
)
def test_build_config_preserves_port_provenance_across_unrelated_override(
    monkeypatch, base, port_is_explicit
):
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.setattr(
        config_module, "resolve_config_path", lambda: Path("config.toml")
    )
    monkeypatch.setattr(config_module, "load_profile", lambda profile=None: base)

    config = build_config(profile="prod", verify_ssl=True)

    assert ("port" in config.model_fields_set) is port_is_explicit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolver", "finder_name", "resource_name", "resource_uuid"),
    [
        (resolve_lpar_uuid, "find_partition_by_name", "aix1", "lpar-uuid"),
        (resolve_vios_uuid, "find_vios_by_name", "vios1", "vios-uuid"),
    ],
)
async def test_partition_resolver_forwards_resolved_system_scope(
    resolver, finder_name, resource_name, resource_uuid
):
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": "system-uuid", "SystemName": "system-name"}
    ]
    getattr(hmc, finder_name).return_value = {"UUID": resource_uuid}

    assert (
        await resolver(hmc, resource_name, system_name_or_uuid="system-name")
        == resource_uuid
    )

    hmc.list_system_summaries.assert_awaited_once_with()
    getattr(hmc, finder_name).assert_awaited_once_with(
        resource_name, system_uuid="system-uuid"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", [resolve_lpar_uuid, resolve_vios_uuid])
@pytest.mark.parametrize("scope", [None, "", "   "])
async def test_partition_resolver_uuid_pass_through_without_scope(resolver, scope):
    hmc = AsyncMock()
    resource_uuid = "11111111-1111-1111-1111-111111111111"

    assert (
        await resolver(hmc, resource_uuid, system_name_or_uuid=scope) == resource_uuid
    )

    hmc.list_system_summaries.assert_not_awaited()
    hmc.find_partition_by_name.assert_not_awaited()
    hmc.find_vios_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_lpar_resolver_preserves_no_match_guidance():
    hmc = AsyncMock()
    hmc.find_partition_by_name.return_value = None

    with pytest.raises(
        ResourceNotFoundError,
        match="No LPAR named 'missing' found. Use hmc_list_lpars to list available partitions.",
    ) as raised:
        await resolve_lpar_uuid(hmc, "missing")

    assert raised.value.resource_kind == "LPAR"
    assert raised.value.selector == "missing"
    assert isinstance(raised.value, ValueError)


SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summaries",
    [
        [],
        ["x"],
        [{"UUID": "33333333-3333-4333-8333-333333333333", "SystemName": "sys-b"}],
        [{"UUID": SYSTEM_UUID}],
        [{"UUID": SYSTEM_UUID, "SystemName": ""}],
        [{"UUID": SYSTEM_UUID, "SystemName": "   "}],
        [{"UUID": SYSTEM_UUID, "SystemName": 1}],
    ],
)
async def test_system_name_resolver_reports_unusable_payload_as_not_found(summaries):
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = summaries

    with pytest.raises(
        ResourceNotFoundError,
        match=f"No managed system '{SYSTEM_UUID}' with a SystemName found. "
        "Use hmc_list_systems to inspect available systems.",
    ) as raised:
        await resolve_system_name(hmc, SYSTEM_UUID)

    assert raised.value.resource_kind == "managed system"
    assert raised.value.selector == SYSTEM_UUID
    hmc.list_system_summaries.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_system_name_resolver_resolves_uuid_and_passes_name_through():
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": "sys-a"}
    ]

    assert await resolve_system_name(hmc, "sys-b") == "sys-b"
    hmc.list_system_summaries.assert_not_awaited()
    assert await resolve_system_name(hmc, SYSTEM_UUID) == "sys-a"
    hmc.list_system_summaries.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolver", "feed"),
    [(resolve_lpar_uuid, "list_logical_partitions"), (resolve_vios_uuid, "list_vios")],
)
@pytest.mark.parametrize("member", [True, False])
async def test_scoped_uuid_requires_selected_system_membership(resolver, feed, member):
    hmc = AsyncMock()
    uuid = "abcdefab-1111-4111-8111-111111111111"
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": "sys-a"}
    ]
    getattr(hmc, feed).return_value = (
        [{"UUID": uuid}] if member else [{"UUID": "other"}]
    )
    if member:
        assert (
            await resolver(hmc, uuid.upper(), system_name_or_uuid="sys-a")
            == uuid.upper()
        )
    else:
        with pytest.raises(ValueError, match="does not belong to managed system"):
            await resolver(hmc, uuid, system_name_or_uuid="sys-a")
    getattr(hmc, feed).assert_awaited_once_with(SYSTEM_UUID)
    hmc.find_partition_by_name.assert_not_awaited()
    hmc.find_vios_by_name.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolver", "feed"),
    [(resolve_lpar_uuid, "list_logical_partitions"), (resolve_vios_uuid, "list_vios")],
)
async def test_scoped_uuid_preserves_feed_error(resolver, feed):
    from hmcpctl.errors import HMCError

    hmc = AsyncMock()
    error = HMCError("feed unavailable", status_code=404)
    getattr(hmc, feed).side_effect = error
    with pytest.raises(HMCError) as caught:
        await resolver(hmc, SYSTEM_UUID, system_name_or_uuid=SYSTEM_UUID)
    assert caught.value is error
