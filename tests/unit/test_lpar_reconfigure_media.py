"""hmc_reconfigure_lpar's installer-media detach and root-disk boot (#1844, ADR 0321).

The partition's provision ledger names the medium and the root disk; the fake HMC holds
the optical mappings, the root disk mapping and the pending boot string.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import live_fixture
from unit.test_lpar_reconfigure import LPAR, SYS, FakeHMC, _effects, _run
from unit.test_lpar_reconfigure import hmc as hmc  # noqa: PLC0414 - fixture import

from hmcpctl.errors import HMCError, LparUpdateError
from hmcpctl.operations.logical import store
from hmcpctl.operations.logical.engine import EffectNotApplied, OperationFailed
from hmcpctl.operations.lpar import reconfigure, reconfigure_media
from hmcpctl.operations.lpar.reconfigure import ReconfigurePatch

VIOS = "0000000c-0000-4000-d000-000000000000"
MEDIA = "hmcpctl_000000000001.iso"
DISK = "lpar_a_root"
DISK_PATH = "/vdevice/v-scsi@30000002/disk@8100000000000000"
OPTICAL_PATH = "/vdevice/v-scsi@30000004/disk@8100000000000000"
PROVISION = "00000000000000000000000000000001"
BOTH = {"boot": "root_disk", "installer_media": "detach"}


def _ledger(operation=PROVISION, key="partition", kind="partition", identity=None):
    with store.session() as conn, store.write_transaction(conn):
        store.add_ledger(
            conn,
            operation,
            key=key,
            kind=kind,
            system_uuid=SYS,
            partition_uuid=LPAR,
            identity=identity or {"lpar_uuid": LPAR, "name": "web1"},
        )


def _provisioned(media=True, disk=True):
    _ledger()
    if media:
        _ledger(
            key="installer_media",
            kind="installer_media",
            identity={"vios_uuid": VIOS, "media_name": MEDIA},
        )
    if disk:
        _ledger(
            key="root_disk",
            kind="virtual_disk",
            identity={"vios_uuid": VIOS, "vg_uuid": "vg", "disk_name": DISK},
        )


def _vscsi(slot, storage, lpar=LPAR):
    return {
        "Resource": {
            "AssociatedLogicalPartition": {"href": f"/LogicalPartition/{lpar}"},
            "ClientAdapter": {"VirtualSlotNumber": str(slot)},
            "ServerAdapter": {"AdapterName": "vhost0"},
            "TargetDevice": {
                "VirtualTarget": {"LogicalUnitAddress": "0x8100000000000000"}
            },
            "Storage": storage,
        }
    }


class MediaHMC:
    """The VIOS and boot state, on the base fake's partition and profile."""

    def __init__(self, fake: FakeHMC):
        self.fake = fake
        self.mounted = [MEDIA]
        self.disk_mappings = [_vscsi(2, {"VirtualDisk": {"DiskName": DISK}})]
        self.pending: str | None = None
        self.unmount_error: Exception | None = None
        self.boot_error: Exception | None = None
        self.list_error: Exception | None = None

    async def list_optical_mappings(self, vios, lpar):
        assert (vios, lpar) == (VIOS, LPAR)
        if self.list_error is not None:
            raise self.list_error
        return [
            _vscsi(4, {"VirtualOpticalMedia": {"MediaName": name}})["Resource"]
            for name in self.mounted
        ]

    async def list_storage_mappings(self, vios):
        assert vios == VIOS
        return self.disk_mappings

    async def read_boot(self, _hmc, lpar, *, system_name_or_uuid):
        assert (lpar, system_name_or_uuid) == (LPAR, SYS)
        return {"pending_boot_string": self.pending}

    async def set_boot(self, _hmc, lpar, devices, *, system_name_or_uuid):
        assert (lpar, system_name_or_uuid) == (LPAR, SYS)
        self.fake.calls.append("boot")
        if self.boot_error is not None:
            raise self.boot_error
        self.pending = " ".join(devices)

    async def unmount(self, _hmc, vios, lpar, *, system_name_or_uuid, media_name):
        assert (vios, lpar, system_name_or_uuid) == (VIOS, LPAR, SYS)
        self.fake.calls.append("unmount")
        if self.unmount_error is not None:
            raise self.unmount_error
        self.mounted.remove(media_name)


@pytest.fixture
def media(hmc, monkeypatch):
    fake = MediaHMC(hmc)
    hmc.list_optical_mappings = fake.list_optical_mappings
    hmc.list_storage_mappings = fake.list_storage_mappings
    monkeypatch.setattr(reconfigure_media, "read_lpar_boot_order", fake.read_boot)
    monkeypatch.setattr(reconfigure_media, "set_lpar_boot_order", fake.set_boot)
    monkeypatch.setattr(reconfigure_media, "unmount_optical_media", fake.unmount)
    return fake


def _writes(fake: MediaHMC):
    return [c for c in fake.fake.calls if c in {"boot", "unmount", "profile"}]


# ---------------------------------------------------------------------------
# Ownership from the ledger (ADR 0321 Decision 1)
# ---------------------------------------------------------------------------


def test_provision_owned_returns_the_recorded_identity():
    _provisioned()
    assert reconfigure_media.provision_owned(SYS, LPAR, "installer_media") == {
        "vios_uuid": VIOS,
        "media_name": MEDIA,
    }


def test_provision_owned_refuses_without_a_store():
    with pytest.raises(OperationFailed, match="no operation store"):
        reconfigure_media.provision_owned(SYS, LPAR, "installer_media")


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(lambda: _provisioned(media=False), id="no-media-entry"),
        pytest.param(lambda: _ledger(), id="partition-only"),
        pytest.param(
            lambda: (_provisioned(), _ledger(operation="0" * 31 + "2")),
            id="two-provisions",
        ),
        pytest.param(
            lambda: (
                _provisioned(),
                _ledger(
                    key="second",
                    kind="installer_media",
                    identity={"vios_uuid": VIOS, "media_name": "other.iso"},
                ),
            ),
            id="two-media-entries",
        ),
        pytest.param(
            lambda: (
                _ledger(),
                _ledger(
                    key="installer_media",
                    kind="installer_media",
                    identity={"vios_uuid": VIOS},
                ),
            ),
            id="missing-field",
        ),
        pytest.param(
            lambda: (
                _ledger(
                    operation="0" * 31 + "2",
                    key="installer_media",
                    kind="installer_media",
                    identity={"vios_uuid": VIOS, "media_name": MEDIA},
                ),
                _ledger(),
            ),
            id="another-operations-media",
        ),
    ],
)
def test_provision_owned_refuses_anything_but_one_entry(seed):
    seed()
    with pytest.raises(OperationFailed):
        reconfigure_media.provision_owned(SYS, LPAR, "installer_media")


# ---------------------------------------------------------------------------
# Planning, writes and verification through the operation body
# ---------------------------------------------------------------------------


def test_boot_then_detach_are_journaled_after_the_profile_and_verified(media):
    _provisioned()
    media.pending = OPTICAL_PATH
    record = _run({"memory_mib": 1024, **BOTH})
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert _effects(record) == [
        ("plan", "lpar.reconfigure_plan", "applied"),
        ("profile", "lpar.profile_write", "applied"),
        ("current", "lpar.current_config", "applied"),
        ("boot", "lpar.boot_order", "applied"),
        ("media_detach", "vios.optical_unmount", "applied"),
    ]
    assert _writes(media) == ["profile", "boot", "unmount"]
    assert (media.pending, media.mounted) == (DISK_PATH, [])
    applied = {c["field"]: c["applied"] for c in record.result["changes"]}
    assert applied == {
        "memory_mib": "live",
        "boot": "next_activation",
        "installer_media": "live",
    }
    assert record.result["observed"]["boot"] == DISK_PATH
    assert record.result["observed"]["installer_media"] == "detached"
    assert any("install had finished" in w for w in record.result["warnings"])


def test_detached_media_and_a_set_boot_order_plan_nothing(media):
    _provisioned()
    media.mounted, media.pending = [], DISK_PATH
    record = _run(BOTH)
    assert (record.outcome, record.result["changes"]) == ("completed", [])
    assert _effects(record) == [("plan", "lpar.reconfigure_plan", "applied")]
    assert _writes(media) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda m: setattr(m, "mounted", [MEDIA, MEDIA]), "mapped 2 times"),
        (lambda m: setattr(m, "disk_mappings", []), "mapped 0 times"),
        (
            lambda m: setattr(
                m, "disk_mappings", [_vscsi("x", {"VirtualDisk": {"DiskName": DISK}})]
            ),
            "no client slot",
        ),
        (
            lambda m: setattr(m, "list_error", HMCError("down", 503)),
            "could not be read",
        ),
    ],
)
def test_planning_refusals_write_nothing(media, change, message):
    _provisioned()
    change(media)
    record = _run(BOTH)
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert message in record.warnings[-1] or message in str(record)
    assert _effects(record) == []
    assert _writes(media) == []


def test_an_unprovisioned_partition_is_refused_before_any_effect(media):
    record = _run({"installer_media": "detach"})
    assert record.outcome == "failed"
    assert _effects(record) == []


def test_a_refused_unmount_still_mapped_fails_when_nothing_else_landed(media):
    _provisioned()
    media.unmount_error = HMCError("VIOS refused", 500)
    record = _run({"installer_media": "detach"})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert _effects(record)[-1] == (
        "media_detach",
        "vios.optical_unmount",
        "not_applied",
    )
    assert media.mounted == [MEDIA]


def test_a_refused_unmount_after_the_boot_write_pauses_and_resume_retries(media):
    _provisioned()
    media.unmount_error = HMCError("VIOS refused", 500)
    first = _run(BOTH)
    assert (first.state, first.outcome) == ("paused", "needs_attention")
    assert ("boot", "lpar.boot_order", "applied") in _effects(first)
    assert "power the partition off" in " ".join(first.result["warnings"])
    media.unmount_error = None
    done = _run(BOTH, continuation="resume")
    assert (done.state, done.outcome) == ("terminal", "completed")
    assert media.mounted == []
    assert _writes(media) == ["boot", "unmount", "unmount"]


def test_an_unmount_that_errors_but_landed_is_applied(media, monkeypatch):
    _provisioned()

    async def lost_answer(_hmc, vios, lpar, *, system_name_or_uuid, media_name):
        media.mounted.remove(media_name)
        raise HMCError("timeout", 504)

    monkeypatch.setattr(reconfigure_media, "unmount_optical_media", lost_answer)
    record = _run({"installer_media": "detach"})
    assert record.outcome == "completed"
    assert any("HTTP 504" in w for w in record.result["warnings"])


def test_a_captured_half_done_detach_past_the_guard_warns_of_the_client_adapter(
    media, monkeypatch
):
    _provisioned()
    _as_captured_on_a_running_client(media)
    monkeypatch.setattr(reconfigure_media, "unmount_optical_media", media.unmount)
    record = _run({"installer_media": "detach"})
    assert record.outcome == "completed"
    assert media.mounted == []
    assert any("leftover client vSCSI adapter" in w for w in record.result["warnings"])


def test_an_unclassifiable_unmount_is_uncertain_and_resume_classifies_it(media):
    _provisioned()
    media.unmount_error = HMCError("timeout", 504)
    reads = iter([None, HMCError("down", 503)])

    async def flaky(vios, lpar):
        error = next(reads, None)
        if error is not None:
            raise error
        return await MediaHMC.list_optical_mappings(media, vios, lpar)

    media.fake.list_optical_mappings = flaky
    first = _run({"installer_media": "detach"})
    assert first.outcome == "needs_attention"
    assert _effects(first)[-1] == ("media_detach", "vios.optical_unmount", "uncertain")
    media.mounted = []
    done = _run({"installer_media": "detach"}, continuation="resume")
    assert done.outcome == "completed"
    assert _effects(done)[-1] == ("media_detach", "vios.optical_unmount", "applied")


def test_detach_skips_the_partition_basis(media, monkeypatch):
    _provisioned()
    plan_media = reconfigure.plan_media

    async def plan_then_halt(hmc, system_uuid, lpar_uuid):
        planned = await plan_media(hmc, system_uuid, lpar_uuid)
        # The installer halts between planning and the write: state and RMC change.
        media.fake.state, media.fake.rmc = "not activated", "inactive"
        return planned

    monkeypatch.setattr(reconfigure, "plan_media", plan_then_halt)
    record = _run({"installer_media": "detach"})
    assert record.outcome == "completed"
    assert media.mounted == []


def _as_captured_on_a_running_client(media: MediaHMC) -> None:
    """The #1844 Q5 answer: the VIOS side goes, the client adapter stays, then a 500."""
    captured = live_fixture("rest-vios-optical-unmount-running-no-rmc")

    async def unmount(_hmc, vios, lpar, *, system_name_or_uuid, media_name):
        media.fake.calls.append("unmount")
        media.mounted.remove(media_name)
        raise HMCError("possible side effect", captured["status"], captured["body"])

    media.unmount = unmount


def test_the_captured_running_unmount_is_the_half_done_detach_the_guard_refuses():
    body = live_fixture("rest-vios-optical-unmount-running-no-rmc")["body"]
    assert "REST0126" in body and "HSCL2957" in body


@pytest.mark.parametrize(
    ("state", "rmc"), [("running", "inactive"), ("open firmware", "")]
)
def test_detach_refuses_an_activated_partition_without_rmc(
    media, monkeypatch, state, rmc
):
    _provisioned()
    _as_captured_on_a_running_client(media)
    monkeypatch.setattr(reconfigure_media, "unmount_optical_media", media.unmount)
    media.fake.state, media.fake.rmc = state, rmc
    record = _run({"installer_media": "detach"})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "Power the partition off, then detach" in str(record)
    assert _effects(record) == []
    assert (_writes(media), media.mounted) == ([], [MEDIA])


def test_detach_rechecks_rmc_before_its_write(media, monkeypatch):
    _provisioned()
    _as_captured_on_a_running_client(media)
    monkeypatch.setattr(reconfigure_media, "unmount_optical_media", media.unmount)
    plan_media = reconfigure.plan_media

    async def plan_then_lose_rmc(hmc, system_uuid, lpar_uuid):
        planned = await plan_media(hmc, system_uuid, lpar_uuid)
        media.fake.rmc = "inactive"
        return planned

    monkeypatch.setattr(reconfigure, "plan_media", plan_then_lose_rmc)
    record = _run(BOTH)
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert ("boot", "lpar.boot_order", "applied") in _effects(record)
    assert "media_detach" not in [key for key, _, _ in _effects(record)]
    assert media.mounted == [MEDIA]


def test_sync_on_refuses_pending_profile_values_with_a_boot_write(media):
    _provisioned()
    media.fake.sync = "On"
    record = _run({"memory_mib": 600, "boot": "root_disk"})
    assert record.outcome == "failed"
    assert _writes(media) == []


def test_a_rejected_boot_write_fails_without_attention(media):
    _provisioned()
    media.boot_error = LparUpdateError("refused", "rejected", 409)
    record = _run({"boot": "root_disk"})
    assert record.outcome == "failed"
    assert _effects(record)[-1] == ("boot", "lpar.boot_order", "not_applied")


def test_an_unknown_boot_write_resumes_by_the_pending_string(media):
    _provisioned()
    media.boot_error = LparUpdateError("unverified", "unknown", 500)
    first = _run({"boot": "root_disk"})
    assert _effects(first)[-1] == ("boot", "lpar.boot_order", "uncertain")
    media.pending, media.boot_error = DISK_PATH, None
    done = _run({"boot": "root_disk"}, continuation="resume")
    assert (done.outcome, _effects(done)[-1][2]) == ("completed", "applied")


# ---------------------------------------------------------------------------
# Writes and classifiers in isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error", [ValueError("no identity"), PermissionError("owner"), LookupError("gone")]
)
def test_media_write_refusals_before_the_post_are_not_applied(media, error):
    media.unmount_error = error
    plan = {
        "lpar_uuid": LPAR,
        "system_uuid": SYS,
        "media": {"vios_uuid": VIOS, "media_name": MEDIA},
    }
    write = reconfigure_media.media_write(media.fake, plan)
    with pytest.raises(EffectNotApplied):
        asyncio.run(write())


def test_media_write_reraises_when_the_listing_also_fails(media):
    media.unmount_error = HMCError("timeout", 504)
    media.list_error = HMCError("down", 503)
    plan = {
        "lpar_uuid": LPAR,
        "system_uuid": SYS,
        "media": {"vios_uuid": VIOS, "media_name": MEDIA},
    }
    with pytest.raises(HMCError, match="timeout"):
        asyncio.run(reconfigure_media.media_write(media.fake, plan)())


@pytest.mark.parametrize(
    ("mounted", "status"),
    [([], "applied"), ([MEDIA], "not_applied"), ([MEDIA, MEDIA], "needs_attention")],
)
def test_media_classifier(media, mounted, status):
    media.mounted = mounted
    plan = {
        "lpar_uuid": LPAR,
        "system_uuid": SYS,
        "media": {"vios_uuid": VIOS, "media_name": MEDIA},
    }
    classify = reconfigure_media.media_classifier(
        lambda _ctx: plan, lambda _c, _host: media.fake
    )
    ctx = type("Ctx", (), {"connection": "<default>", "host": "hmc.test"})()
    assert asyncio.run(classify(ctx, None)).status == status


@pytest.mark.parametrize(
    ("pending", "status"),
    [
        (DISK_PATH, "applied"),
        (OPTICAL_PATH, "not_applied"),
        ("other", "needs_attention"),
    ],
)
def test_boot_classifier(media, pending, status):
    media.pending = pending
    plan = {
        "lpar_uuid": LPAR,
        "system_uuid": SYS,
        "boot": {"before": OPTICAL_PATH, "after": DISK_PATH},
    }
    classify = reconfigure_media.boot_classifier(
        lambda _ctx: plan, lambda _c, _host: media.fake
    )
    ctx = type("Ctx", (), {"connection": "<default>", "host": "hmc.test"})()
    assert asyncio.run(classify(ctx, None)).status == status


def test_patch_accepts_only_the_literal_values():
    assert ReconfigurePatch(boot="root_disk").needs_window is False
    assert ReconfigurePatch(installer_media="detach").needs_window is True
    with pytest.raises(ValueError):
        ReconfigurePatch(boot="disk")
    with pytest.raises(ValueError):
        ReconfigurePatch(installer_media="eject")
