"""hmc_reconfigure_lpar's added and detached virtual disks (ADR 0322, #1846).

An add creates, maps and ledger-owns a disk, then force-saves the default profile so
the client adapter survives a profile activation; a detach removes only the one
planned mapping. The fake HMC models one VIOS with one volume group.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from unit.test_lpar_reconfigure import ARGS, LPAR, OTHER, SYS, FakeHMC, _effects
from unit.test_lpar_reconfigure import hmc as base_hmc  # noqa: F401 - a fixture

from hmcpctl.errors import HMCError
from hmcpctl.operations.logical import engine, store
from hmcpctl.operations.logical.engine import OperationRequest
from hmcpctl.operations.lpar import profile_persist, reconfigure
from hmcpctl.operations.lpar.reconfigure import ReconfigurePatch

VIOS = "0000000c-0000-4000-8000-000000000000"
VG = "0000000c-0000-4000-9000-000000000000"
ADD = {"vios_uuid": VIOS, "vg_uuid": VG, "name": "data1", "capacity_mib": 2048}


def _mapping(name, lpar, adapter, target="vtscsi0"):
    return {
        "Resource": {
            "ServerAdapter": {"AdapterName": adapter},
            "TargetDevice": {"VirtualDiskTargetDevice": {"TargetName": target}},
            "AssociatedLogicalPartition": {
                "href": f"https://hmc.test/rest/api/uom/LogicalPartition/{lpar.upper()}"
            },
            "Storage": {"VirtualDisk": {"DiskName": name}},
        }
    }


def _profile_entry(name, slots):
    adapters = [{"VirtualSlotNumber": str(slot)} for slot in slots]
    return {
        "Resource": {
            "ProfileName": name,
            "IOConfigurationInstance": {
                "ProfileVirtualIOAdapters": {
                    "ProfileVirtualIOAdapterSubclass": {
                        "ProfileVirtualSCSIClientAdapter": adapters
                    }
                }
            },
        }
    }


class StorageMixin:
    """VIOS storage, client adapters and the profile feed the save reads back."""

    def storage(self):
        self.disks = {"root1"}
        # (disk name, client lpar, server adapter, client slot)
        self.maps = [("root1", LPAR, "vhost0", 2), ("shared", OTHER, "vhost1", 2)]
        self.saved_slots = {2}
        self.recovery = []
        self.level = "1060"
        self.disk_error = None
        self.free_gib = 100

    async def list_volume_groups(self, vios):
        assert vios == VIOS
        disks = [{"DiskName": name} for name in sorted(self.disks)]
        resource = {
            "GroupName": "datavg",
            "FreeSpace": str(self.free_gib),
            "VirtualDisks": {"VirtualDisk": disks},
        }
        return [{"UUID": VG, "Resource": resource}]

    async def create_virtual_disk(self, vios, vg, name, capacity):
        assert (vios, vg, capacity) == (VIOS, VG, 2048)
        self.calls.append(f"create {name}")
        if self.disk_error is not None:
            raise self.disk_error
        self.disks.add(name)

    async def map_storage_to_lpar(self, vios, kind, name, lpar, target):
        assert (vios, kind, target) == (VIOS, "VirtualDisk", None)
        self.calls.append(f"map {name}")
        slot = 2 + sum(1 for m in self.maps if m[1] == lpar)
        self.maps.append((name, lpar, f"vhost{len(self.maps)}", slot))

    async def list_storage_mappings(self, vios, lpar=None):
        assert vios == VIOS and lpar is None
        return [_mapping(n, lpar_, a) for n, lpar_, a, _ in self.maps]

    async def delete_storage_mapping(self, vios, mapping_id, lpar):
        self.calls.append(f"detach {mapping_id}")
        self.maps = [
            m
            for m in self.maps
            if not (f"{m[2]}/vtscsi0" == mapping_id and m[1] == lpar)
        ]

    async def get_console_info(self):
        info = {"Version": "10", "Release": "3", "ServicePackName": self.level}
        return {"Resource": {"VersionInfo": info}}

    async def list_child(self, parent, uuid, child):
        assert (parent, uuid, child) == (
            "LogicalPartition",
            LPAR,
            "LogicalPartitionProfile",
        )
        return [_profile_entry("default", self.saved_slots)] + [
            _profile_entry(name, ()) for name in self.recovery
        ]

    async def list_adapters(self, uuid, adapter_type):
        if adapter_type != "VirtualSCSIClientAdapter":
            return []
        slots = [slot for _, lpar, _, slot in self.maps if lpar == uuid]
        return [{"Resource": {"VirtualSlotNumber": str(slot)}} for slot in slots]


@pytest.fixture
def hmc(base_hmc, monkeypatch):  # noqa: F811 - the fixture, by parameter name
    fake = base_hmc
    fake.__class__ = type("DiskHMC", (StorageMixin, FakeHMC), {})
    fake.storage()
    fake.saves = []

    async def read(_hmc, system, lpar, name):
        assert (system, lpar, name) == (SYS, LPAR, "default")
        return SimpleNamespace(etag=f"E{fake.etag}")

    async def save(_hmc, system, lpar, name, **kwargs):
        fake.calls.append("save")
        fake.saves.append(kwargs)
        fake.recovery.append(f"hmcprec-{len(fake.recovery):016x}")
        fake.saved_slots = {slot for _, lpar, _, slot in fake.maps if lpar == LPAR}
        return SimpleNamespace(
            execution="unknown",
            requested_change="unverified",
            job=object(),
            recovery_verified=True,
            error=None,
        )

    async def vios_on(_hmc, vios, *, system_name_or_uuid):
        if (vios, system_name_or_uuid) != (VIOS, SYS):
            raise LookupError("VIOS is not on the system")
        return vios

    monkeypatch.setattr(reconfigure, "resolve_vios_uuid", vios_on)
    monkeypatch.setattr(profile_persist, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(profile_persist, "POLL_SECONDS", 0)
    monkeypatch.setattr(profile_persist, "read_partition_profile", read)
    monkeypatch.setattr(profile_persist, "save_current_partition_profile", save)
    return fake


def _run(patch, *, continuation="none", allow_disruption=False):
    model = ReconfigurePatch(**patch)
    request = OperationRequest(
        "hmc_reconfigure_lpar",
        "agent-a",
        "<default>",
        "hmc.test",
        "d1",
        {**ARGS, "patch": patch, "allow_disruption": allow_disruption},
    )
    return engine.submit(
        request,
        reconfigure.reconfigure_body(model, allow_disruption, "sysA", "web1"),
        continuation=continuation,
        wait_seconds=10,
        classifiers=reconfigure.RECONFIGURE_CLASSIFIERS,
    )


def _ledger():
    with store.read_session() as conn:
        return store.ledger_entries(conn, "virtual_disk", SYS)


def _writes(fake):
    return [c for c in fake.calls if c != "authorize"]


# ---------------------------------------------------------------------------
# The patch model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("disks", "message"),
    [
        ({}, "at least one add or detach"),
        ({"add": [ADD, ADD]}, "same VIOS and disk name twice"),
        ({"add": [ADD], "detach": [{"vios_uuid": VIOS, "name": "data1"}]}, "twice"),
        ({"add": [{**ADD, "capacity_mib": 1000}]}, "multiple of 1024"),
        ({"add": [{**ADD, "name": "a" * 16}]}, "limits backing-device names"),
        ({"add": [{**ADD, "name": " "}]}, "must not be blank"),
        ({"add": [{**ADD, "vios_uuid": "vios1"}]}, "pattern"),
        ({"add": [{**ADD, "name": f"d{i}"} for i in range(5)]}, "at most 4"),
    ],
)
def test_the_patch_refuses_malformed_disks(disks, message):
    with pytest.raises(ValueError, match=message):
        ReconfigurePatch(disks=disks)


def test_disks_need_the_window_and_their_tools():
    patch = ReconfigurePatch(
        disks={"add": [ADD], "detach": [{"vios_uuid": VIOS, "name": "root1"}]}
    )
    assert patch.needs_window
    assert reconfigure.delegated_tools(patch) == (
        "hmc_create_virtual_disk",
        "hmc_map_storage_to_lpar",
        "hmc_save_current_partition_profile",
        "hmc_detach_storage_mapping",
    )
    assert reconfigure.disk_vios_uuids(patch, "hmc_create_virtual_disk") == (VIOS,)
    assert reconfigure.disk_vios_uuids(patch, "hmc_modify_lpar") == ()
    assert reconfigure.delegated_tools(ReconfigurePatch(name="x")) == (
        "hmc_modify_lpar",
        "hmc_rename_lpar",
    )


def test_the_largest_patch_fits_the_recorded_plan():
    adds = [{**ADD, "name": f"{i}" * 15} for i in range(4)]
    detaches = [{"vios_uuid": VIOS, "name": f"{i + 4}" * 15} for i in range(4)]
    disks = {
        "add": [{**a, "vios_uuid": VIOS, "vg_uuid": VG} for a in adds],
        "detach": [{**d, "mapping_id": "vhost1023/vtscsi1023"} for d in detaches],
    }
    plan = {
        "basis": {"name": "n" * 256},
        "changes": reconfigure._disk_changes(disks, True),
        "disks": disks,
        "save": "p" * 31,
        "system_uuid": SYS,
        "lpar_uuid": LPAR,
        "profile_uuid": SYS,
        "profile_name": "p" * 31,
        "profile_etag": "e" * 64,
        "rename": {"before": "n" * 256, "after": "m" * 256},
    }
    assert len(json.dumps(plan, sort_keys=True)) < store.MAX_IDENTITY_BYTES


# ---------------------------------------------------------------------------
# Adds
# ---------------------------------------------------------------------------


def test_an_add_creates_maps_owns_and_saves_the_default_profile(hmc):
    hmc.state = "not activated"
    record = _run({"disks": {"add": [ADD]}})
    assert (record.state, record.outcome) == ("terminal", "completed"), record.warnings
    assert _writes(hmc) == ["create data1", "map data1", "save"]
    assert _effects(record)[1:] == [
        ("disk:0", "vios.virtual_disk", "applied"),
        ("map:0", "vios.storage_map", "applied"),
        ("profile_save", "lpar.profile_save", "applied"),
    ]
    assert [(op, lpar, ident) for op, lpar, ident in _ledger()] == [
        (
            record.operation_id,
            LPAR,
            {"vios_uuid": VIOS, "vg_uuid": VG, "disk_name": "data1"},
        )
    ]
    assert hmc.saves == [
        {"overwrite": True, "expected_etag": "E1", "exclusive_writer_window": True}
    ]
    change = record.result["changes"][0]
    assert (change["field"], change["applied"], change["after"]["name"]) == (
        "disks.add",
        "profile",
        "data1",
    )
    assert any("hmcprec-0000000000000000" in w for w in record.result["warnings"])


def test_an_add_to_a_running_partition_is_live(hmc):
    record = _run({"disks": {"add": [ADD]}})
    assert record.outcome == "completed"
    assert record.result["changes"][0]["applied"] == "live"


@pytest.mark.parametrize(
    ("state", "rmc"), [("running", "inactive"), ("open firmware", "inactive")]
)
def test_an_add_to_an_activated_partition_needs_rmc(hmc, state, rmc):
    hmc.state, hmc.rmc = state, rmc
    record = _run({"disks": {"add": [ADD]}})
    assert (record.outcome, record.effects) == ("failed", ())
    assert "running with RMC active" in record.warnings[0]


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda f: f.disks.add("data1"), "already has a virtual disk"),
        (lambda f: setattr(f, "level", "1050"), "10.3.1060"),
        (lambda f: setattr(f, "free_gib", 1), "GiB free"),
        (
            lambda f: f.profile.find(".//{*}DesiredMemory").__setattr__("text", "1024"),
            "desired memory differs",
        ),
    ],
)
def test_add_refusals_write_nothing(hmc, setup, message):
    setup(hmc)
    record = _run({"disks": {"add": [ADD]}})
    assert (record.outcome, record.effects) == ("failed", ())
    assert message in record.warnings[0]
    assert _writes(hmc) == []


def test_a_vios_on_another_managed_system_is_refused(hmc):
    other = {**ADD, "vios_uuid": "0000000d-0000-4000-8000-000000000000"}
    record = _run({"disks": {"add": [other]}})
    assert (record.outcome, record.effects) == ("failed", ())
    assert "is not on managed system" in record.warnings[0]


def test_a_volume_group_missing_from_the_vios_is_refused(hmc):
    record = _run({"disks": {"add": [{**ADD, "vg_uuid": SYS}]}})
    assert "is not on VIOS" in record.warnings[0]


def test_an_add_with_a_pending_change_is_refused(hmc):
    hmc.state, hmc.rmc = "running", "active"
    record = _run({"memory_mib": 2048, "disks": {"add": [ADD]}})
    assert (record.outcome, record.effects) == ("failed", ())
    assert "pending_activation" in record.warnings[0]


def test_a_failed_save_pauses_with_the_disk_owned_and_warns(hmc, monkeypatch):
    hmc.state = "not activated"

    async def refused(*_args, **_kwargs):
        return SimpleNamespace(
            execution="rejected",
            requested_change="unverified",
            job=None,
            recovery_verified=False,
            error="refused",
        )

    monkeypatch.setattr(profile_persist, "save_current_partition_profile", refused)
    record = _run({"disks": {"add": [ADD]}})
    assert record.outcome == "needs_attention"
    assert _effects(record)[-1] == ("profile_save", "lpar.profile_save", "not_applied")
    assert len(_ledger()) == 1
    assert any("current configuration only" in w for w in record.result["warnings"])


def test_resume_after_an_uncertain_disk_classifies_ledgers_and_saves(hmc):
    hmc.state = "not activated"
    hmc.disk_error = HMCError("lost reply")
    record = _run({"disks": {"add": [ADD]}})
    assert record.outcome == "needs_attention"
    assert _ledger() == []
    hmc.disk_error = None
    hmc.disks.add("data1")  # the lost reply's write had landed
    record = _run({"disks": {"add": [ADD]}}, continuation="resume")
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert _writes(hmc).count("create data1") == 1
    assert len(_ledger()) == 1


def test_resume_after_an_uncertain_save_classifies_by_readback(hmc, monkeypatch):
    hmc.state = "not activated"
    real = profile_persist.save_current_partition_profile

    async def raised_after_landing(*args, **kwargs):
        await real(*args, **kwargs)
        hmc.saved_slots = set()
        raise HMCError("job status misread")

    monkeypatch.setattr(
        profile_persist, "save_current_partition_profile", raised_after_landing
    )
    record = _run({"disks": {"add": [ADD]}})
    assert record.outcome == "needs_attention"
    assert _effects(record)[-1][2] == "uncertain"
    hmc.saved_slots = {2, 3}
    record = _run({"disks": {"add": [ADD]}}, continuation="resume")
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert _writes(hmc).count("save") == 1


async def _classify(kind, effect_key, plan, ctx_partition=(SYS, LPAR)):
    effect = SimpleNamespace(key=effect_key, kind=kind, target="", status="uncertain")
    ctx = SimpleNamespace(
        connection="<default>",
        partition=ctx_partition,
        operation_id="op-1",
        recorded=lambda key: SimpleNamespace(status="applied", identity=plan),
    )
    return await reconfigure.RECONFIGURE_CLASSIFIERS[kind](ctx, effect)


def _disk_plan(**extra):
    return {
        "system_uuid": SYS,
        "lpar_uuid": LPAR,
        "disks": {
            "add": [{**ADD}],
            "detach": [
                {"vios_uuid": VIOS, "name": "root1", "mapping_id": "vhost0/vtscsi0"}
            ],
        },
        **extra,
    }


@pytest.mark.asyncio
async def test_disk_map_and_unmap_classifiers(hmc):
    plan = _disk_plan()
    assert (
        await _classify("vios.virtual_disk", "disk:0", plan)
    ).status == "not_applied"
    assert (await _classify("vios.storage_map", "map:0", plan)).status == "not_applied"
    assert (
        await _classify("vios.storage_unmap", "unmap:0", plan)
    ).status == "not_applied"
    hmc.disks.add("data1")
    await hmc.map_storage_to_lpar(VIOS, "VirtualDisk", "data1", LPAR, None)
    await hmc.delete_storage_mapping(VIOS, "vhost0/vtscsi0", LPAR)
    disk = await _classify("vios.virtual_disk", "disk:0", plan)
    assert (disk.status, disk.identity["disk_name"]) == ("applied", "data1")
    assert (await _classify("vios.storage_map", "map:0", plan)).status == "applied"
    assert (await _classify("vios.storage_unmap", "unmap:0", plan)).status == "applied"


@pytest.mark.asyncio
async def test_a_disk_another_operation_owns_needs_attention(hmc):
    hmc.disks.add("data1")
    with store.session() as conn, store.write_transaction(conn):
        store.add_ledger(
            conn,
            "op-other",
            key="root_disk",
            kind="virtual_disk",
            system_uuid=SYS,
            partition_uuid=LPAR,
            identity={"vios_uuid": VIOS, "vg_uuid": VG, "disk_name": "data1"},
        )
    verdict = await _classify("vios.virtual_disk", "disk:0", _disk_plan())
    assert verdict.status == "needs_attention"


@pytest.mark.parametrize(
    "disks",
    [{"add": [ADD]}, {"detach": [{"vios_uuid": VIOS, "name": "root1"}]}],
)
def test_a_rename_with_disks_completes_and_resumes_past_the_rename(hmc, disks):
    hmc.state = "not activated"
    record = _run({"name": "web2", "disks": disks})
    assert (record.state, record.outcome) == ("terminal", "completed"), record.warnings
    assert hmc.name == "web2"
    assert _writes(hmc)[0] == "the partition name" and len(_writes(hmc)) > 1


def test_a_resume_after_the_rename_continues_to_the_disk(hmc, monkeypatch):
    hmc.state = "not activated"
    hmc.disk_error = HMCError("lost reply")
    record = _run({"name": "web2", "disks": {"add": [ADD]}})
    assert record.outcome == "needs_attention"
    hmc.disk_error = None
    record = _run({"name": "web2", "disks": {"add": [ADD]}}, continuation="resume")
    assert (record.state, record.outcome) == ("terminal", "completed"), record.warnings
    assert "data1" in hmc.disks


# ---------------------------------------------------------------------------
# Detaches
# ---------------------------------------------------------------------------


def test_a_detach_removes_only_its_mapping_and_keeps_the_disk(hmc):
    hmc.state = "not activated"
    record = _run({"disks": {"detach": [{"vios_uuid": VIOS, "name": "root1"}]}})
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert _writes(hmc) == ["detach vhost0/vtscsi0"]
    assert [m[0] for m in hmc.maps] == ["shared"]
    assert hmc.disks == {"root1"}
    assert _ledger() == []
    assert record.result["changes"][0]["field"] == "disks.detach"


def test_a_detach_from_an_activated_partition_needs_allow_disruption(hmc):
    patch = {"disks": {"detach": [{"vios_uuid": VIOS, "name": "root1"}]}}
    record = _run(patch)
    assert (record.outcome, record.effects) == ("failed", ())
    assert "allow_disruption=true" in record.warnings[0]


def test_a_detach_from_an_activated_partition_with_allow_disruption(hmc):
    patch = {"disks": {"detach": [{"vios_uuid": VIOS, "name": "root1"}]}}
    record = _run(patch, allow_disruption=True)
    assert record.outcome == "completed"
    assert record.result["changes"][0]["applied"] == "live"


@pytest.mark.parametrize("name", ["shared", "absent"])
def test_a_detach_needs_one_mapping_to_this_partition(hmc, name):
    hmc.state = "not activated"
    record = _run({"disks": {"detach": [{"vios_uuid": VIOS, "name": name}]}})
    assert (record.outcome, record.effects) == ("failed", ())
    assert "exactly one is needed" in record.warnings[0]


def test_a_mapping_changed_since_planning_is_not_detached(hmc, monkeypatch):
    hmc.state = "not activated"
    real = reconfigure._apply

    async def moved(ctx, client, plan):
        hmc.maps = [("root1", LPAR, "vhost9", 2), *hmc.maps[1:]]
        await real(ctx, client, plan)

    monkeypatch.setattr(reconfigure, "_apply", moved)
    record = _run({"disks": {"detach": [{"vios_uuid": VIOS, "name": "root1"}]}})
    assert record.outcome == "failed"
    assert "no longer maps the planned disk" in record.warnings[0]
    assert _writes(hmc) == []
