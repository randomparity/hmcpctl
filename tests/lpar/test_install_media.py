"""Producer-result binding, device paths and the repository group (ADR 0191 D3, ADR 0319)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from hmcpctl.operations.lpar.install_media import (
    InstallMedia,
    InstallNetwork,
    InstallRoute,
    LparInstall,
    MediaMismatch,
    bind_prepared,
    client_slot,
    device_path,
    media_name_for,
    repository_group,
)

MAC = "02:00:00:00:00:01"
URL = "https://media.example.test/lpar-a.iso"
NETWORK = InstallNetwork(
    address="10.0.0.5/24",
    routes=(InstallRoute("0.0.0.0/0", "10.0.0.1"),),
    dns=("10.0.0.2",),
)


def producer_result(**changes: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "format": "iso-chain-media-v1",
        "iso_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "iso_size": 4096,
        "distribution": "ubuntu",
        "release": "26.04.1",
        "architecture": "ppc64le",
        "mac": MAC,
        "network": {
            "address": "10.0.0.5/24",
            "routes": [{"destination": "0.0.0.0/0", "gateway": "10.0.0.1"}],
            "dns": ["10.0.0.2"],
        },
    }
    result.update(changes)
    return {k: v for k, v in result.items() if v is not None}


def prepared(result: dict[str, Any] | None = None) -> LparInstall:
    return LparInstall(
        profile="ubuntu-26.04.1",
        network=NETWORK,
        media=InstallMedia(
            mode="prepared",
            url=URL,
            producer_result=producer_result() if result is None else result,
        ),
    )


def test_a_matching_result_binds_its_digest_size_url_and_mac():
    binding = bind_prepared(prepared(), MAC)
    assert (binding.url, binding.iso_sha256, binding.iso_size, binding.mac) == (
        URL,
        "a" * 64,
        4096,
        MAC,
    )


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"format": "iso-chain-media-v2"}, "format"),
        ({"iso_sha256": "A" * 64}, "iso_sha256"),
        ({"manifest_sha256": "b" * 63}, "manifest_sha256"),
        ({"iso_size": 0}, "iso_size"),
        ({"iso_size": True}, "iso_size"),
        ({"iso_size": "4096"}, "iso_size"),
        ({"release": "9.8"}, "distribution"),
        ({"architecture": "x86_64"}, "architecture"),
        ({"mac": "02:00:00:00:00:02"}, "mac"),
        ({"network": {"address": "10.0.0.6/24", "routes": [], "dns": []}}, "network"),
        ({"operation_binding": "0" * 32}, "operation_binding"),
        ({"url": URL}, "url"),
        ({"extra": 1}, "extra"),
        ({"architecture": None}, "architecture"),
    ],
)
def test_each_field_that_does_not_bind_is_named_without_its_value(changes, field):
    with pytest.raises(MediaMismatch) as caught:
        bind_prepared(prepared(producer_result(**changes)), MAC)
    message = str(caught.value)
    assert f"producer_result.{field}:" in message
    for value in changes.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in message


def test_a_network_without_its_dns_list_does_not_bind():
    network = producer_result()["network"]
    del network["dns"]
    with pytest.raises(MediaMismatch, match="network"):
        bind_prepared(prepared(producer_result(network=network)), MAC)


def test_no_pinned_mac_does_not_bind():
    with pytest.raises(MediaMismatch, match="mac"):
        bind_prepared(prepared(), None)


def test_built_media_does_not_bind_as_prepared():
    install = replace(prepared(), media=InstallMedia(mode="built"))
    with pytest.raises(MediaMismatch, match="format"):
        bind_prepared(install, MAC)


def test_media_name_fits_the_upload_name_rule():
    assert media_name_for("0123456789abcdef" * 2) == "hmcpctl_0123456789ab.iso"


def _mapping(slot: object = "4", lun: object = "0x8100000000000000") -> dict:
    # The #1284 capture's optical mapping shape (rest-vios-scsi-mapping-replace-storage).
    return {
        "ClientAdapter": {"VirtualSlotNumber": slot},
        "Storage": {"VirtualOpticalMedia": {"MediaName": "m"}},
        "TargetDevice": {"VirtualOpticalTargetDevice": {"LogicalUnitAddress": lun}},
    }


def test_device_path_follows_the_recipe_form():
    path = device_path(_mapping())
    assert path == "/vdevice/v-scsi@30000004/disk@8100000000000000"
    assert path is not None and client_slot(path) == 4
    assert device_path({"Resource": _mapping(slot={"text": "3"})}) == (
        "/vdevice/v-scsi@30000003/disk@8100000000000000"
    )


@pytest.mark.parametrize(
    "mapping",
    [
        _mapping(slot=None),
        _mapping(slot="x"),
        _mapping(lun=None),
        _mapping(lun="8100000000000000"),
        {"ClientAdapter": {"VirtualSlotNumber": "4"}},
    ],
)
def test_device_path_is_none_without_a_slot_and_lun(mapping):
    assert device_path(mapping) is None


class _Groups:
    def __init__(self, *repos: bool) -> None:
        self.groups = [
            {
                "UUID": f"VG-{index}",
                "Resource": (
                    {"MediaRepositories": {"VirtualMediaRepository": {}}}
                    if repo
                    else {}
                ),
            }
            for index, repo in enumerate(repos)
        ]

    async def list_volume_groups(self, _vios: str) -> list[dict]:
        return self.groups


def test_repository_group_is_the_one_group_holding_a_repository():
    assert asyncio.run(repository_group(_Groups(False, True), "vios")) == "vg-1"  # type: ignore[arg-type]


@pytest.mark.parametrize("repos", [(), (False,), (True, True)])
def test_repository_group_refuses_none_or_several(repos):
    with pytest.raises(ValueError, match="exactly one media repository"):
        asyncio.run(repository_group(_Groups(*repos), "vios"))  # type: ignore[arg-type]
