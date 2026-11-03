"""Prepared installer media in hmc_provision_lpar (ADR 0191, ADR 0319)."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import parse_iso_url_allowlist
from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError
from hmcpctl.operations.logical import engine, store
from hmcpctl.operations.logical.engine import OperationRequest
from hmcpctl.operations.logical.models import EffectRecord
from hmcpctl.operations.lpar import provision_operation, reconfigure_media
from hmcpctl.operations.lpar.install_media import (
    UNVERIFIED,
    InstallMedia,
    InstallNetwork,
    InstallRoute,
    LparInstall,
    MediaMismatch,
    media_name_for,
)
from hmcpctl.operations.lpar.plan import (
    PlanAdapters,
    PlanRequest,
    PlanStorage,
    PlanSystem,
    PlanTargets,
    _intended_changes,
    check_request,
)
from hmcpctl.operations.lpar.provision import (
    ProvisionAdapters,
    ProvisionRequest,
    ProvisionStorage,
    _provision_step_names,
    preflight_install,
    validate_provision_install,
)
from hmcpctl.operations.lpar.provision_operation import (
    DISK_LEDGER,
    EFFECT_KINDS,
    MEDIA_LEDGER,
    provision_body,
    provision_classifiers,
    request_from_arguments,
    result_from_record,
)
from hmcpctl.server_tools.catalog import TOOL_SECURITY
from hmcpctl.server_tools.lpar.provision import delegated_tools, provision_handler

SYSTEM = "00000000-0000-0000-0000-0000000000a1"
LPAR = "00000000-0000-0000-0000-0000000000b2"
VIOS = "00000000-0000-0000-0000-0000000000c3"
GROUP = "00000000-0000-0000-0000-0000000000d4"
REPO_GROUP = "00000000-0000-0000-0000-0000000000e5"
MAC = "02:00:00:00:00:01"
URL = "https://media.example.test/lpar-a.iso"
OPTICAL = "/vdevice/v-scsi@30000004/disk@8100000000000000"
DISK = "/vdevice/v-scsi@30000002/disk@8100000000000000"
NETWORK = InstallNetwork(
    address="10.0.0.5/24", routes=(InstallRoute("0.0.0.0/0", "10.0.0.1"),)
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
            "dns": [],
        },
    }
    result.update(changes)
    return result


INSTALL = LparInstall(
    profile="ubuntu-26.04.1",
    network=NETWORK,
    media=InstallMedia(mode="prepared", url=URL, producer_result=producer_result()),
)
REQUEST = ProvisionRequest(
    name="lpar-a",
    adapters=ProvisionAdapters(100, MAC),
    storage=ProvisionStorage(VIOS, "lv-a", vg_uuid=GROUP, capacity_mib=1024),
    resources=LparResources(min_memory=256, desired_memory=512, max_memory=1024),
    power_on=False,
    install=INSTALL,
    boot="deferred",
)


def _scsi_mapping(slot: int, backing: dict[str, Any]) -> dict[str, Any]:
    return {
        "AssociatedLogicalPartition": {
            "href": f"https://hmc.test/rest/api/uom/LogicalPartition/{LPAR}"
        },
        "ClientAdapter": {"VirtualSlotNumber": str(slot)},
        "ServerAdapter": {"VirtualSlotNumber": str(slot + 10)},
        "Storage": backing,
        "TargetDevice": {"Device": {"LogicalUnitAddress": "0x8100000000000000"}},
    }


class FakeHMC:
    """The client reads the media legs and classifiers make."""

    def __init__(self, operation_id: str = "0" * 32) -> None:
        self.config = SimpleNamespace(
            agent_id=None,
            iso_url_allowlist_entries=parse_iso_url_allowlist("media.example.test"),
        )
        self.media_name = media_name_for(operation_id)
        self.optical: list[dict[str, Any]] = []
        self.media: list[dict[str, Any]] = []
        self.pending: str | None = None
        self.groups = [
            {"UUID": GROUP, "Resource": {}},
            {
                "UUID": REPO_GROUP.upper(),
                "Resource": {"MediaRepositories": {"VirtualMediaRepository": {}}},
            },
        ]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def mount(self, name: str | None = None) -> None:
        backing = {"VirtualOpticalMedia": {"MediaName": name or self.media_name}}
        self.optical.append(_scsi_mapping(4, backing))

    async def list_volume_groups(self, _vios: str) -> list[dict[str, Any]]:
        return self.groups

    async def list_optical_media(self, _vios: str, _group: str) -> list[dict]:
        return self.media

    async def list_optical_mappings(self, _vios: str, _lpar: str) -> list[dict]:
        return self.optical

    async def list_storage_mappings(self, _vios: str) -> list[dict]:
        return [_scsi_mapping(2, {"VirtualDisk": {"DiskName": "lv-a"}})]


class Writes:
    """The HMC writes a provision body makes, answered as a healthy HMC would."""

    def __init__(self, hmc: FakeHMC, monkeypatch: pytest.MonkeyPatch) -> None:
        self.hmc = hmc
        self.calls: list[str] = []
        self.adapter_mac: str | None = MAC
        self.network_mac: list[str | None] = []
        self.upload_kwargs: dict[str, Any] = {}
        self.mount_error: Exception | None = None
        self.upload_error: Exception | None = None
        self.readback: str | None = None
        self.optical_slot: list[int | None] = []
        self.boot: list[list[str]] = []
        po = provision_operation
        patch = monkeypatch.setattr
        patch(po, "resolve_system_uuid", AsyncMock(return_value=SYSTEM))
        patch(po, "create_and_stamp_lpar", self.create)
        patch(po, "_stamp_state", AsyncMock(return_value="ours"))
        patch(po, "_add_network", self.network)
        patch(po, "_create_disk", self.record("disk"))
        patch(po, "_map_storage", self.record("storage"))
        patch(po, "_read_change_location", AsyncMock(return_value=(None, ())))
        patch(po, "upload_iso", self.upload)
        patch(po, "mount_optical_media", self.mount)
        patch(po, "set_lpar_boot_order", self.set_boot)
        patch(po, "save_current_partition_profile", self.record("save"))
        patch(po, "set_default_partition_profile", self.default)
        patch(po.profile_write, "profile_by_name", self.profile)
        patch(po, "readback_problem", self.problem)

    def record(self, name: str):
        async def write(*_args: Any, **_kwargs: Any) -> None:
            self.calls.append(name)

        return write

    async def create(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls.append("create")
        return SimpleNamespace(
            lpar={"UUID": LPAR}, physical_io_step=None, apply_step=None
        )

    async def network(self, _hmc: Any, _lpar: str, _vlan: int, mac: str | None) -> dict:
        self.calls.append("network")
        self.network_mac.append(mac)
        hex_mac = (self.adapter_mac or "").replace(":", "").upper()
        return {"UUID": "adapter-1", "Resource": {"MACAddress": hex_mac}}

    async def upload(self, _hmc: Any, vios: str, group: str, name: str, url: str, **kw):
        self.calls.append("upload")
        self.upload_kwargs = {
            "vios": vios,
            "group": group,
            "name": name,
            "url": url,
            **kw,
        }
        if self.upload_error is not None:
            raise self.upload_error
        self.hmc.media.append({"MediaName": name})

    async def mount(self, _hmc: Any, _vios: str, _lpar: str, **kw: Any) -> None:
        self.calls.append("mount")
        if self.mount_error is not None:
            raise self.mount_error
        self.hmc.mount(kw["media_name"])

    async def set_boot(self, _hmc: Any, _lpar: str, devices: list[str], **_kw: Any):
        self.calls.append("boot_order")
        self.boot.append(devices)

    async def default(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls.append("default_profile")
        return SimpleNamespace(execution="completed", requested_change="verified")

    async def profile(self, *_args: Any) -> Any:
        return SimpleNamespace(uuid="profile-1", element=None)

    def problem(
        self, _element: Any, _request: Any, _sriov: int, slot=None
    ) -> str | None:
        self.optical_slot.append(slot)
        return self.readback


_IDS = itertools.count()


def _submit(
    writes: Writes, request: ProvisionRequest = REQUEST, continuation="none", rid=None
):
    rid = rid or f"install-{next(_IDS)}"
    operation = OperationRequest(
        "hmc_provision_lpar", "agent", "<default>", "hmc.test", rid, {"r": rid}
    )
    hmc = writes.hmc
    record = engine.submit(
        operation,
        provision_body(SYSTEM, request, lambda _c: hmc),
        classifiers=provision_classifiers(request, lambda _c: hmc),
        continuation=continuation,
        wait_seconds=30,
    )
    hmc.media_name = media_name_for(record.operation_id)
    return record


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> Writes:
    return Writes(FakeHMC(), monkeypatch)


def _ledger(record) -> list[tuple[str, dict]]:
    with store.read_session() as conn:
        assert conn is not None
        entries = store.ledger_entries(conn, MEDIA_LEDGER, SYSTEM)
    return [(op, identity) for op, _p, identity in entries if op == record.operation_id]


def test_a_clean_install_run_pauses_ready_to_boot_with_the_media_first(writes):
    record = _submit(writes)

    assert (record.state, record.outcome, record.phase) == (
        "paused",
        "ready_to_boot",
        "ready_to_boot",
    )
    assert writes.calls == [
        "create",
        "network",
        "disk",
        "storage",
        "upload",
        "mount",
        "save",
        "default_profile",
        "boot_order",
    ]
    keys = [(e.key, e.kind) for e in record.effects]
    assert keys[-5:] == [
        ("upload", "vios.optical_media_upload"),
        ("mount", "vios.optical_mapping"),
        ("profile", EFFECT_KINDS["profile"]),
        ("default_profile", "lpar.default_profile"),
        ("boot_order", "lpar.boot_order"),
    ]
    name = media_name_for(record.operation_id)
    assert writes.network_mac == [MAC]
    assert writes.upload_kwargs == {
        "vios": VIOS,
        "group": REPO_GROUP,
        "name": name,
        "url": URL,
        "system_name_or_uuid": SYSTEM,
        "expected_sha256": "a" * 64,
        "expected_size": 4096,
    }
    assert writes.optical_slot == [4]
    assert writes.boot == [[OPTICAL, DISK]]
    assert _ledger(record) == [
        (record.operation_id, {"vios_uuid": VIOS, "media_name": name})
    ]
    result = result_from_record(record)
    assert result.workflow_completed is True
    assert result.unverified == UNVERIFIED
    assert [step.step for step in result.steps][-7:] == [
        "bind_media",
        "upload_media",
        "mount_media",
        "save_profile",
        "profile_readback",
        "default_profile",
        "set_boot_order",
    ]
    assert result.steps[-1].step == "set_boot_order"
    assert record.next_actions == ("boot", "abandon")


def test_reconfigure_finds_the_media_and_root_disk_this_provision_recorded(writes):
    """#1844 reads exactly the ledger entries an install run writes (ADR 0321 D1)."""
    record = _submit(writes)

    media = reconfigure_media.provision_owned(SYSTEM, LPAR, MEDIA_LEDGER)
    disk = reconfigure_media.provision_owned(SYSTEM, LPAR, DISK_LEDGER)

    assert media == {
        "vios_uuid": VIOS,
        "media_name": media_name_for(record.operation_id),
    }
    assert (
        disk["vios_uuid"] == VIOS and disk["disk_name"] == REQUEST.storage.storage_name
    )


def test_an_adapter_whose_mac_differs_from_the_pin_fails_before_any_upload(writes):
    writes.adapter_mac = "02:00:00:00:00:09"

    record = _submit(writes)

    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "upload" not in writes.calls
    assert "does not carry adapters.mac" in record.warnings[0]


def test_a_digest_mismatch_uploads_nothing_and_fails(writes):
    writes.upload_error = ValueError("the downloaded ISO's SHA-256 or size differs")

    record = _submit(writes)

    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "mount" not in writes.calls
    upload = next(e for e in record.effects if e.key == "upload")
    assert upload.status == "not_applied"


def test_an_allowlist_narrowed_before_the_upload_fails_without_the_url(writes):
    writes.hmc.config.iso_url_allowlist_entries = parse_iso_url_allowlist(
        "other.example.test"
    )

    record = _submit(writes)

    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "upload" not in writes.calls
    assert "media.example.test" not in record.warnings[0]


def test_a_mount_refused_for_rest0269_fails_and_names_the_server_adapter(writes):
    writes.mount_error = HMCError("REST0269 Perform Apply profile", 500, "")

    record = _submit(writes)

    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "server adapter" in record.warnings[0]
    assert "save" not in writes.calls


def test_a_mount_listing_with_no_device_path_pauses_uncertain(writes, monkeypatch):
    async def mount(_hmc, _vios, _lpar, **kw):
        writes.calls.append("mount")
        writes.hmc.optical.append(
            {"Storage": {"VirtualOpticalMedia": {"MediaName": kw["media_name"]}}}
        )

    monkeypatch.setattr(provision_operation, "mount_optical_media", mount)

    record = _submit(writes)

    assert (record.state, record.outcome) == ("paused", "needs_attention")
    mount_effect = next(e for e in record.effects if e.key == "mount")
    assert mount_effect.status == "uncertain"
    assert "save" not in writes.calls


def test_a_readback_without_the_optical_slot_never_writes_the_boot_order(writes):
    writes.readback = "the saved profile has no virtual SCSI client adapter"

    record = _submit(writes)

    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert "boot_order" not in writes.calls
    result = result_from_record(record)
    assert result.steps[-1].step == "set_boot_order"
    assert result.steps[-1].status == "skipped"


def test_a_resume_after_an_uncertain_mount_classifies_by_the_mapping_listing(writes):
    writes.mount_error = HMCError("socket closed", 502, "")
    rid = f"install-{next(_IDS)}"
    first = _submit(writes, rid=rid)
    assert first.outcome == "needs_attention"

    writes.mount_error = None
    writes.hmc.media_name = media_name_for(first.operation_id)
    writes.hmc.mount()
    resumed = _submit(writes, continuation="resume", rid=rid)

    assert resumed.outcome == "ready_to_boot"
    assert writes.calls.count("mount") == 1


def _classify(hmc: FakeHMC, key: str, target: str, request=REQUEST, recorded=None):
    effect = EffectRecord(key, EFFECT_KINDS[key], target, "uncertain", None)
    classifier = provision_classifiers(request, lambda _c: hmc)[effect.kind]
    ctx = SimpleNamespace(
        connection="<default>",
        operation_id="0" * 32,
        partition=(SYSTEM, LPAR),
        recorded=lambda k: recorded if k == "mount" else None,
    )
    return asyncio.run(classifier(ctx, effect))


def test_the_upload_classifier_reads_the_repository_by_name():
    hmc = FakeHMC()
    assert _classify(hmc, "upload", f"vios:{VIOS}").status == "not_applied"
    hmc.media = [{"MediaName": hmc.media_name}]
    verdict = _classify(hmc, "upload", f"vios:{VIOS}")
    assert verdict.status == "applied"
    assert verdict.identity == {
        "vios_uuid": VIOS,
        "vg_uuid": REPO_GROUP,
        "media_name": hmc.media_name,
    }
    hmc.groups = []
    assert _classify(hmc, "upload", f"vios:{VIOS}").status == "needs_attention"


def test_the_mount_classifier_reads_the_optical_mapping_listing():
    hmc = FakeHMC()
    assert _classify(hmc, "mount", f"lpar:{LPAR}").status == "not_applied"
    hmc.mount("someone-else.iso")
    assert _classify(hmc, "mount", f"lpar:{LPAR}").status == "not_applied"
    hmc.mount()
    verdict = _classify(hmc, "mount", f"lpar:{LPAR}")
    assert verdict.status == "applied"
    assert verdict.identity == {
        "vios_uuid": VIOS,
        "media_name": hmc.media_name,
        "client_slot": 4,
        "device_path": OPTICAL,
    }
    hmc.mount()
    assert _classify(hmc, "mount", f"lpar:{LPAR}").status == "needs_attention"


@pytest.mark.parametrize(
    ("pending", "status"),
    [(f"{OPTICAL} {DISK}", "applied"), (DISK, "not_applied"), (None, "not_applied")],
)
def test_the_boot_order_classifier_compares_the_pending_boot_string(
    monkeypatch, pending, status
):
    async def read(_hmc, _lpar, *, system_name_or_uuid):
        assert system_name_or_uuid == SYSTEM
        return {"pending_boot_string": pending}

    monkeypatch.setattr(provision_operation, "read_lpar_boot_order", read)
    mount = EffectRecord(
        "mount", "vios.optical_mapping", "x", "applied", {"device_path": OPTICAL}
    )
    verdict = _classify(FakeHMC(), "boot_order", f"lpar:{LPAR}", recorded=mount)
    assert verdict.status == status


def test_a_pinned_mac_adapter_that_differs_needs_attention():
    hmc = FakeHMC()

    async def adapters(_lpar, _kind):
        return [
            {
                "UUID": "a",
                "Resource": {"PortVLANID": "100", "MACAddress": "020000000009"},
            }
        ]

    hmc.list_adapters = adapters  # type: ignore[attr-defined]
    verdict = _classify(hmc, "network", f"lpar:{LPAR}")
    assert verdict.status == "needs_attention"
    hmc_ok = FakeHMC()

    async def pinned(_lpar, _kind):
        return [
            {
                "UUID": "a",
                "Resource": {"PortVLANID": "100", "MACAddress": "020000000001"},
            }
        ]

    hmc_ok.list_adapters = pinned  # type: ignore[attr-defined]
    assert _classify(hmc_ok, "network", f"lpar:{LPAR}").status == "applied"


def test_the_readback_requires_the_optical_adapter_slot():
    import xml.etree.ElementTree as ET

    from hmcpctl.operations.lpar.provision_operation import _vscsi_in_slot

    ns = "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}"
    root = ET.Element(f"{ns}LogicalPartitionProfile")
    adapter = ET.SubElement(root, f"{ns}ProfileVirtualSCSIClientAdapter")
    ET.SubElement(adapter, f"{ns}VirtualSlotNumber").text = "2"
    assert not _vscsi_in_slot(root, 4)
    ET.SubElement(adapter, f"{ns}VirtualSlotNumber").text = "4"
    assert _vscsi_in_slot(root, 4)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"install": None}, "boot: deferred applies only with install"),
        ({"install": None, "boot": "immediate"}, "adapters.mac: accepted only"),
        ({"power_on": True}, "power_on: with install"),
        ({"boot": "immediate"}, "#1849"),
        ({"storage": ProvisionStorage(VIOS, "lv-a", vg_uuid=GROUP)}, "capacity_mib"),
        (
            {
                "install": replace(
                    INSTALL,
                    media=InstallMedia(mode="built"),
                    login_user="ubuntu",
                    ssh_authorized_keys=("ssh-ed25519 AAAA key",),
                ),
                "adapters": ProvisionAdapters(100),
            },
            "#1843",
        ),
        ({"adapters": ProvisionAdapters(100)}, "adapters.mac: prepared media requires"),
    ],
)
def test_install_inputs_this_provision_cannot_run_are_refused(changes, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(")):
        validate_provision_install(replace(REQUEST, **changes))


def test_a_result_that_does_not_bind_is_refused_before_recording():
    bad = replace(
        INSTALL,
        media=InstallMedia(
            mode="prepared", url=URL, producer_result=producer_result(release="9.8")
        ),
    )
    with pytest.raises(MediaMismatch, match="distribution"):
        validate_provision_install(replace(REQUEST, install=bad))


def test_preflight_refuses_an_unlisted_url_and_a_vios_without_one_repository():
    hmc = FakeHMC()
    asyncio.run(preflight_install(hmc, REQUEST))  # type: ignore[arg-type]
    hmc.groups = []
    with pytest.raises(ValueError, match="exactly one media repository"):
        asyncio.run(preflight_install(hmc, REQUEST))  # type: ignore[arg-type]
    hmc = FakeHMC()
    hmc.config.iso_url_allowlist_entries = parse_iso_url_allowlist("other.example.test")
    with pytest.raises(ValueError, match="HMC_ISO_URL_ALLOWLIST") as caught:
        asyncio.run(preflight_install(hmc, REQUEST))  # type: ignore[arg-type]
    assert "media.example.test" not in str(caught.value)


def test_install_delegates_the_media_specialists_only_with_install():
    media = {"hmc_upload_iso", "hmc_mount_optical_media", "hmc_set_lpar_boot_order"}
    assert media <= set(delegated_tools(REQUEST))
    plain = replace(
        REQUEST, install=None, boot="immediate", adapters=ProvisionAdapters(100)
    )
    assert not media & set(delegated_tools(plain))
    assert "hmc_power_on_lpar" not in delegated_tools(REQUEST)


def _arguments() -> dict[str, Any]:
    return {
        "system_name_or_uuid": SYSTEM,
        "name": "lpar-a",
        "adapters": ProvisionAdapters(100, MAC),
        "storage": ProvisionStorage(VIOS, "lv-a", vg_uuid=GROUP, capacity_mib=1024),
        "install": INSTALL,
        "boot": "deferred",
    }


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "u")
    monkeypatch.setenv("HMC_PASSWORD", "p")

    def make(permits=lambda _name: True):
        return provision_handler(TOOL_SECURITY, permits, lambda *_args: None)

    return make


def test_a_withheld_media_specialist_refuses_before_anything_is_recorded(handler):
    call = handler(lambda name: name != "hmc_upload_iso")
    with pytest.raises(PermissionError, match="hmc_upload_iso"):
        call(request_id="withheld-1", exclusive_writer_window=True, **_arguments())
    assert (
        engine.recorded_arguments("hmcpctl", "withheld-1", "hmc_provision_lpar") is None
    )


def test_a_mismatched_result_refuses_through_the_handler(handler):
    arguments = _arguments()
    arguments["install"] = replace(
        INSTALL,
        media=InstallMedia(
            mode="prepared", url=URL, producer_result=producer_result(mac=None)
        ),
    )
    with pytest.raises(ValueError, match="producer_result.mac"):
        handler()(request_id="mismatch-1", exclusive_writer_window=True, **arguments)
    assert (
        engine.recorded_arguments("hmcpctl", "mismatch-1", "hmc_provision_lpar") is None
    )


def test_install_records_power_on_off_by_default():
    from hmcpctl.server_tools.lpar.provision import _effective, _given

    given = _given(**_arguments())
    effective, _ = _effective(given, "agent", "power-default", "none")
    assert effective["power_on"] is False
    _, request = request_from_arguments(effective)
    assert request.power_on is False and request.boot == "deferred"


def test_the_dry_run_order_matches_the_plan_order():
    plan = check_request(
        PlanRequest(
            name="lpar-a",
            adapters=PlanAdapters(100, MAC),
            storage=PlanStorage("lv-a", "VirtualDisk", VIOS, GROUP, 1024),
            resources=REQUEST.resources,
            system_name_or_uuid=SYSTEM,
            boot="deferred",
            install=INSTALL,
            exclusive_writer_window=True,
        )
    )
    targets = PlanTargets(PlanSystem(f"c/{SYSTEM}", SYSTEM, "sys-R1"), None, None)
    plan_kinds = [
        c.kind
        for c in _intended_changes(plan, targets)
        if c.kind
        in (
            "bind_media",
            "upload_media",
            "mount_media",
            "write_profile",
            "set_boot_order",
        )
    ]
    names = {"save_profile": "write_profile"}
    provision_steps = [
        names.get(n, n)
        for n in _provision_step_names(REQUEST)
        if names.get(n, n) in plan_kinds
    ]
    assert plan_kinds == provision_steps
