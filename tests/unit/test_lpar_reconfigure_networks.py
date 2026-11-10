"""hmc_reconfigure_lpar's client network adapter legs (ADR 0325, #1845).

An add creates a client network adapter on a VLAN; a removal deletes the one adapter
carrying a MAC. The fake HMC holds one partition's client network adapters and the
system's virtual networks.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from unit.test_lpar_reconfigure import LPAR, SYS, FakeHMC, _effects
from unit.test_lpar_reconfigure import hmc as base_hmc  # noqa: F401 - a fixture

from hmcpctl.errors import HMCError
from hmcpctl.jobs import job_outcome
from hmcpctl.operations.logical import engine, store
from hmcpctl.operations.logical.engine import (
    EffectNotApplied,
    OperationFailed,
    OperationRequest,
)
from hmcpctl.operations.lpar import profile_persist, reconfigure
from hmcpctl.operations.lpar import reconfigure_network as net
from hmcpctl.operations.lpar.profiles import PartitionProfileOutcome
from hmcpctl.operations.lpar.reconfigure import ReconfigurePatch
from hmcpctl.operations.lpar.reconfigure_network import NetworkPatch

MAC_A = "02:00:00:00:00:0a"
MAC_B = "02:00:00:00:00:0b"
MAC_NEW = "02:00:00:00:00:0c"
STOPPED = {"state": "not activated", "rmc": "inactive"}
RUNNING = {"state": "running", "rmc": "active"}


def _entry(uuid, mac, vlan, slot):
    return {
        "UUID": uuid,
        "Resource": {
            "MACAddress": mac.replace(":", "").upper(),
            "PortVLANID": str(vlan),
            "VirtualSlotNumber": str(slot),
        },
    }


class NetHMC:
    """One partition's client network adapters and the system's VLANs."""

    def __init__(self):
        self.adapters = [_entry("UA", MAC_A, 10, 2), _entry("UB", MAC_B, 20, 3)]
        self.vlans = {10, 20, 30}
        self.calls = []
        self.add_error = None
        self.delete_error = None
        self.delete_lands = True
        self.response = True

    async def _network_adapter_creation_feed(self, uuid, *, allow_empty=False):
        assert uuid == LPAR and allow_empty is True
        return list(self.adapters)

    async def list_virtual_networks(self, system):
        assert system == SYS
        return [{"Resource": {"NetworkVLANID": str(v)}} for v in sorted(self.vlans)]

    async def add_network_adapter(self, lpar, vlan, *, mac_address=None):
        assert lpar == LPAR
        self.calls.append(("add", vlan, mac_address))
        if self.add_error is not None:
            raise self.add_error
        slot = 2 + len(self.adapters) + 10
        mac = mac_address or f"0200000000{slot:02x}"
        mac = ":".join(mac.lower()[i : i + 2] for i in range(0, 12, 2))
        entry = _entry(f"U{slot}", mac, vlan, slot)
        self.adapters.append(entry)
        return entry if self.response else None

    async def delete_adapter(self, lpar, kind, uuid):
        assert (lpar, kind) == (LPAR, "ClientNetworkAdapter")
        self.calls.append(("delete", uuid))
        if self.delete_lands:
            self.adapters = [a for a in self.adapters if a["UUID"].lower() != uuid]
        if self.delete_error is not None:
            raise self.delete_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run(coro):
    return asyncio.run(coro)


def _plan(fake, patch, view=STOPPED, allow=False):
    planned = _run(
        net.plan_networks(
            fake, NetworkPatch(**patch), view, SYS, LPAR, allow_disruption=allow
        )
    )
    return {"lpar_uuid": LPAR, "system_uuid": SYS, "networks": planned, "save": "p1"}


def _ctx(records=None):
    records = records or {}
    return SimpleNamespace(host="hmc.test", connection="c", recorded=records.get)


def _record(status, identity=None):
    return SimpleNamespace(status=status, identity=identity)


# --- the patch model ---------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"add": [{"port_vlan_id": 0}]},
        {"add": [{"port_vlan_id": 4095}]},
        {"add": [{"port_vlan_id": 10, "mac": "02:00:00:00:00:0A"}]},
        {"add": [{"port_vlan_id": 10, "mac": "03:00:00:00:00:0a"}]},
        {"remove": [{"mac": "0200000000aa"}]},
        {"add": [{"port_vlan_id": 10, "mac": MAC_A}], "remove": [{"mac": MAC_A}]},
        {"add": [{"port_vlan_id": 10}, {"port_vlan_id": 10}]},
        {"add": [{"port_vlan_id": 10, "mac": MAC_NEW}, {"port_vlan_id": 10}]},
        {"add": [{"port_vlan_id": v} for v in range(1, 6)]},
        {"remove": [{"mac": f"02:00:00:00:01:{n:02x}"} for n in range(5)]},
        {"add": [{"port_vlan_id": 10, "vsi_type_id": 1}]},
    ],
)
def test_patch_model_refuses(patch):
    with pytest.raises(ValidationError):
        NetworkPatch(**patch)


def test_patch_model_accepts_pinned_adds_on_one_vlan():
    patch = NetworkPatch(
        add=[
            {"port_vlan_id": 10, "mac": MAC_NEW},
            {"port_vlan_id": 10, "mac": "02:00:00:00:00:0d"},
        ]
    )
    assert len(patch.add) == 2


# --- planning ----------------------------------------------------------------


def test_plan_records_removal_identity_and_unpinned_baseline():
    fake = NetHMC()
    plan = _plan(
        fake,
        {
            "add": [{"port_vlan_id": 10}, {"port_vlan_id": 30, "mac": MAC_NEW}],
            "remove": [{"mac": MAC_B}],
        },
    )
    assert plan["networks"] == {
        "add": [
            {"port_vlan_id": 10, "mac": None, "baseline": ["ua"]},
            {"port_vlan_id": 30, "mac": MAC_NEW, "baseline": None},
        ],
        "remove": [
            {"mac": MAC_B, "adapter_uuid": "ub", "port_vlan_id": "20", "slot": "3"}
        ],
    }
    assert net.removed_slots(plan.get("networks")) == ("3",)
    assert net.adds(plan.get("networks"))
    assert [s[0] for s in net.steps(plan.get("networks"), plan["lpar_uuid"])] == [
        "network_add:0",
        "network_add:1",
        "network_remove:0",
    ]


def test_changes_report_profile_when_stopped_and_live_when_running():
    plan = _plan(NetHMC(), {"add": [{"port_vlan_id": 10}], "remove": [{"mac": MAC_A}]})
    stopped = net.network_changes(plan["networks"], active=False)
    assert [(c["field"], c["applied"]) for c in stopped] == [
        ("networks.add", "profile"),
        ("networks.remove", "profile"),
    ]
    assert stopped[1]["before"] == {"mac": MAC_A, "port_vlan_id": "10"}
    assert {c["applied"] for c in net.network_changes(plan["networks"], True)} == {
        "live"
    }


def test_plan_reads_partition_without_adapters():
    fake = NetHMC()
    fake.adapters = []
    plan = _plan(fake, {"add": [{"port_vlan_id": 10}]})
    assert plan["networks"]["add"][0]["baseline"] == []


@pytest.mark.parametrize(
    ("patch", "view", "allow", "message"),
    [
        ({"remove": [{"mac": MAC_NEW}]}, STOPPED, False, "0 client network adapters"),
        ({"add": [{"port_vlan_id": 99}]}, STOPPED, False, "no virtual network"),
        (
            {"add": [{"port_vlan_id": 10, "mac": MAC_A}]},
            STOPPED,
            False,
            "already has an adapter",
        ),
        (
            {"add": [{"port_vlan_id": 10}]},
            {"state": "running", "rmc": "inactive"},
            True,
            "needs it running with RMC active",
        ),
        (
            {"add": [{"port_vlan_id": 10}]},
            {"state": "open firmware", "rmc": "inactive"},
            True,
            "needs it running with RMC active",
        ),
        ({"remove": [{"mac": MAC_A}]}, RUNNING, False, "allow_disruption=true"),
    ],
)
def test_plan_refusals(patch, view, allow, message):
    with pytest.raises(OperationFailed, match=message):
        _plan(NetHMC(), patch, view, allow)


def test_plan_refuses_a_mac_listed_twice():
    fake = NetHMC()
    fake.adapters.append(_entry("UC", MAC_A, 30, 4))
    with pytest.raises(OperationFailed, match="2 client network adapters"):
        _plan(fake, {"remove": [{"mac": MAC_A}]})


def test_plan_allows_live_changes_on_running_partition_with_disruption():
    plan = _plan(NetHMC(), {"remove": [{"mac": MAC_A}]}, RUNNING, True)
    assert plan["networks"]["remove"][0]["adapter_uuid"] == "ua"


def test_plan_names_an_unreadable_inventory():
    fake = NetHMC()

    async def broken(*_, **__):
        raise HMCError("boom", 500, "secret body")

    fake._network_adapter_creation_feed = broken
    with pytest.raises(OperationFailed, match="could not be read \\(HMCError\\)"):
        _plan(fake, {"remove": [{"mac": MAC_A}]})


def test_plan_without_networks_key_has_no_steps():
    plan = {"lpar_uuid": LPAR}
    assert net.steps(plan.get("networks"), plan["lpar_uuid"]) == []
    assert net.removed_slots(plan.get("networks")) == ()
    assert not net.adds(plan.get("networks"))


# --- writes ------------------------------------------------------------------


def _write(fake, plan, key):
    return _run(net.write_for(fake, plan.get("networks"), plan["lpar_uuid"], key)())


def test_unpinned_add_records_the_created_identity():
    fake = NetHMC()
    plan = _plan(fake, {"add": [{"port_vlan_id": 30}]})
    identity = _write(fake, plan, "network_add:0")
    assert identity["port_vlan_id"] == "30" and identity["adapter_uuid"] == "u14"
    assert fake.calls == [("add", 30, None)]


def test_pinned_add_sends_the_mac_without_colons():
    fake = NetHMC()
    plan = _plan(fake, {"add": [{"port_vlan_id": 30, "mac": MAC_NEW}]})
    assert _write(fake, plan, "network_add:0")["mac"] == MAC_NEW
    assert fake.calls == [("add", 30, "02000000000c")]


def test_add_without_response_identity_finds_the_new_adapter():
    fake = NetHMC()
    fake.response = False
    plan = _plan(fake, {"add": [{"port_vlan_id": 10}]})
    assert _write(fake, plan, "network_add:0")["adapter_uuid"] == "u14"


@pytest.mark.parametrize("code", ["REST0269", "HSCLB021"])
@pytest.mark.parametrize("altered_format", [False, True])
def test_add_refusal_is_not_applied_with_its_guidance(
    monkeypatch, code, altered_format
):
    if altered_format:
        original_str = HMCError.__str__
        monkeypatch.setattr(
            HMCError,
            "__str__",
            lambda error: original_str(error).replace(" (HTTP 500)", " [HTTP 500]"),
        )
    fake = NetHMC()
    fake.add_error = HMCError(
        "refused", 500, f"<Message>{code} cookie=secret</Message>"
    )
    plan = _plan(fake, {"add": [{"port_vlan_id": 10}]})
    with pytest.raises(EffectNotApplied, match=code) as refused:
        _write(fake, plan, "network_add:0")
    assert "secret" not in str(refused.value)
    assert "HTTP 500" not in str(refused.value)
    assert ("chsyscfg" if code == "REST0269" else "RMC state") in str(refused.value)
    assert refused.value.__cause__ is fake.add_error
    assert fake.calls == [("add", 10, None)]


def test_other_add_error_stays_open():
    fake = NetHMC()
    fake.add_error = HMCError("timeout", 504, "")
    plan = _plan(fake, {"add": [{"port_vlan_id": 10}]})
    with pytest.raises(HMCError, match="timeout"):
        _write(fake, plan, "network_add:0")


def test_removal_deletes_only_the_planned_adapter():
    fake = NetHMC()
    plan = _plan(fake, {"remove": [{"mac": MAC_B}]})
    assert _write(fake, plan, "network_remove:0") == {
        "adapter_uuid": "ub",
        "mac": MAC_B,
        "slot": "3",
    }
    assert fake.calls == [("delete", "ub")]
    assert [a["UUID"] for a in fake.adapters] == ["UA"]


def test_removal_already_gone_is_applied_without_a_delete():
    fake = NetHMC()
    plan = _plan(fake, {"remove": [{"mac": MAC_B}]})
    fake.adapters = fake.adapters[:1]
    assert _write(fake, plan, "network_remove:0")["adapter_uuid"] == "ub"
    assert fake.calls == []


def test_removal_refuses_an_adapter_whose_mac_changed():
    fake = NetHMC()
    plan = _plan(fake, {"remove": [{"mac": MAC_B}]})
    fake.adapters[1] = _entry("UB", MAC_NEW, 20, 3)
    with pytest.raises(EffectNotApplied, match="no longer carries the planned MAC"):
        _write(fake, plan, "network_remove:0")
    assert fake.calls == []


def test_removal_error_after_delete_is_applied_with_status():
    fake = NetHMC()
    fake.delete_error = HMCError("partial", 500, "HSCL2957")
    plan = _plan(fake, {"remove": [{"mac": MAC_B}]})
    assert _write(fake, plan, "network_remove:0")["error_status"] == 500


def test_removal_error_with_adapter_listed_stays_open():
    fake = NetHMC()
    fake.delete_error = HMCError("refused", 500, "")
    fake.delete_lands = False
    plan = _plan(fake, {"remove": [{"mac": MAC_B}]})
    with pytest.raises(HMCError, match="refused"):
        _write(fake, plan, "network_remove:0")


# --- classifiers -------------------------------------------------------------


def _classify(classifier, plan, key):
    effect = SimpleNamespace(key=key, target=f"lpar:{LPAR}")
    return _run(classifier(_ctx(), effect))


def _classifiers(fake, plan):
    def opener(_connection, _host):
        return fake

    return (
        net.add_classifier(
            lambda _ctx: (plan["lpar_uuid"], plan.get("networks")), opener
        ),
        net.remove_classifier(
            lambda _ctx: (plan["lpar_uuid"], plan.get("networks")), opener
        ),
    )


def test_unpinned_add_classifies_by_new_adapters_on_its_vlan():
    fake = NetHMC()
    plan = _plan(fake, {"add": [{"port_vlan_id": 10}]})
    add, _ = _classifiers(fake, plan)
    assert _classify(add, plan, "network_add:0").status == "not_applied"
    fake.adapters.append(_entry("UN", MAC_NEW, 10, 9))
    applied = _classify(add, plan, "network_add:0")
    assert applied.status == "applied" and applied.identity["adapter_uuid"] == "un"
    fake.adapters.append(_entry("UM", "02:00:00:00:00:0d", 10, 8))
    assert _classify(add, plan, "network_add:0").status == "needs_attention"


def test_pinned_add_classifies_by_its_mac():
    fake = NetHMC()
    plan = _plan(fake, {"add": [{"port_vlan_id": 30, "mac": MAC_NEW}]})
    add, _ = _classifiers(fake, plan)
    assert _classify(add, plan, "network_add:0").status == "not_applied"
    fake.adapters.append(_entry("UN", MAC_NEW, 30, 9))
    assert _classify(add, plan, "network_add:0").status == "applied"


def test_removal_classifies_by_the_planned_uuid():
    fake = NetHMC()
    plan = _plan(fake, {"remove": [{"mac": MAC_B}]})
    _, remove = _classifiers(fake, plan)
    assert _classify(remove, plan, "network_remove:0").status == "not_applied"
    fake.adapters[1] = _entry("UB", MAC_NEW, 20, 3)
    assert _classify(remove, plan, "network_remove:0").status == "needs_attention"
    fake.adapters = fake.adapters[:1]
    assert _classify(remove, plan, "network_remove:0").status == "applied"


# --- observation and warnings ------------------------------------------------


def test_observed_waits_for_adds_and_removals():
    fake = NetHMC()
    plan = _plan(fake, {"add": [{"port_vlan_id": 30}], "remove": [{"mac": MAC_A}]})
    added = _write(fake, plan, "network_add:0")
    ctx = _ctx({"network_add:0": _record("applied", added)})
    done, seen = _run(
        net.observed_networks(ctx, fake, plan.get("networks"), plan["lpar_uuid"])
    )
    assert not done
    _write(fake, plan, "network_remove:0")
    done, seen = _run(
        net.observed_networks(ctx, fake, plan.get("networks"), plan["lpar_uuid"])
    )
    assert done and MAC_A not in seen["networks"]


def test_observed_is_trivially_done_without_networks():
    assert _run(net.observed_networks(_ctx(), NetHMC(), None, LPAR)) == (
        True,
        {},
    )


def test_warnings_name_unsaved_changes_and_delete_errors():
    fake = NetHMC()
    plan = _plan(fake, {"remove": [{"mac": MAC_B}]})
    ctx = _ctx(
        {
            "network_remove:0": _record(
                "applied", {"adapter_uuid": "ub", "error_status": 500}
            )
        }
    )
    notes = net.warnings(
        ctx,
        plan.get("networks"),
        plan["lpar_uuid"],
        saved=False,
        save_profile=plan.get("save"),
    )
    assert any("HTTP 500" in n for n in notes)
    assert any("current configuration only" in n for n in notes)
    assert not any(
        "current configuration only" in n
        for n in net.warnings(
            ctx,
            plan.get("networks"),
            plan["lpar_uuid"],
            True,
            save_profile=plan.get("save"),
        )
    )
    assert (
        net.warnings(
            _ctx(),
            plan.get("networks"),
            plan["lpar_uuid"],
            saved=False,
            save_profile=plan.get("save"),
        )
        == []
    )


def test_add_refused_before_submission_is_not_applied():
    fake = NetHMC()
    fake.add_error = ValueError("bad document")
    plan = _plan(fake, {"add": [{"port_vlan_id": 10}]})
    with pytest.raises(EffectNotApplied, match="before submission"):
        _write(fake, plan, "network_add:0")


def test_add_that_cannot_be_identified_stays_open():
    fake = NetHMC()
    fake.response = False
    plan = _plan(fake, {"add": [{"port_vlan_id": 10}]})
    fake.adapters.append(_entry("UX", "02:00:00:00:00:0e", 10, 7))
    with pytest.raises(HMCError, match="could not be identified"):
        _write(fake, plan, "network_add:0")


def test_removal_error_with_unreadable_relist_keeps_the_delete_error():
    fake = NetHMC()
    fake.delete_error = HMCError("refused", 500, "")
    plan = _plan(fake, {"remove": [{"mac": MAC_B}]})
    reads = []
    listing = fake._network_adapter_creation_feed

    async def flaky(*args, **kwargs):
        reads.append(args)
        if len(reads) > 1:
            raise HMCError("relist", 503, "")
        return await listing(*args, **kwargs)

    fake._network_adapter_creation_feed = flaky
    with pytest.raises(HMCError, match="refused"):
        _write(fake, plan, "network_remove:0")


# --- the operation body (ADR 0325 with ADR 0322's save) ----------------------


def _profile_entry(name, slots):
    adapters = [{"VirtualSlotNumber": str(slot)} for slot in slots]
    return {
        "Resource": {
            "ProfileName": name,
            "IOConfigurationInstance": {
                "ProfileVirtualIOAdapters": {
                    "ProfileVirtualIOAdapterSubclass": {
                        "ProfileClientNetworkAdapter": adapters
                    }
                }
            },
        }
    }


class BodyMixin(NetHMC):
    """Client network adapters, the console level and the profile feed a save reads."""

    def network(self):
        NetHMC.__init__(self)
        self.saved_slots = {"2", "3"}
        self.recovery = []
        self.level = "1060"

    async def list_child(self, parent, uuid, child):
        assert (parent, uuid) == ("LogicalPartition", LPAR)
        if child == "ClientNetworkAdapter":
            return list(self.adapters)
        if child != "LogicalPartitionProfile":
            return []
        return [_profile_entry("default", self.saved_slots)] + [
            _profile_entry(name, ()) for name in self.recovery
        ]

    async def list_adapters(self, uuid, adapter_type):
        assert adapter_type != "ClientNetworkAdapter"
        return []

    async def get_console_info(self):
        info = {"Version": "10", "Release": "3", "ServicePackName": self.level}
        return {"Resource": {"VersionInfo": info}}

    async def add_network_adapter(self, lpar, vlan, *, mac_address=None):
        entry = await NetHMC.add_network_adapter(
            self, lpar, vlan, mac_address=mac_address
        )
        self.calls[-1] = f"add {vlan}"
        return entry

    async def delete_adapter(self, lpar, kind, uuid):
        await NetHMC.delete_adapter(self, lpar, kind, uuid)
        self.calls[-1] = f"delete {uuid}"


@pytest.fixture
def body(base_hmc, monkeypatch):  # noqa: F811 - the fixture, by parameter name
    fake = base_hmc
    fake.__class__ = type("NetworkHMC", (BodyMixin, FakeHMC), {})
    fake.network()
    fake.state = "not activated"
    fake.saves = []
    fake.save_lands = True

    async def read(_hmc, system, lpar, name):
        assert (system, lpar, name) == (SYS, LPAR, "default")
        return SimpleNamespace(etag="E1")

    async def save(_hmc, system, lpar, name, **kwargs):
        fake.calls.append("save")
        fake.saves.append(kwargs)
        fake.recovery.append(f"hmcprec-{len(fake.recovery):016x}")
        if fake.save_lands:
            fake.saved_slots = {
                str(a["Resource"]["VirtualSlotNumber"]) for a in fake.adapters
            }
        return PartitionProfileOutcome(
            action="save",
            system_uuid=SYS,
            partition_uuid=LPAR,
            partition_type="LogicalPartition",
            profile_name="default",
            profile_uuid=None,
            execution="unknown",
            requested_change="unverified",
            job=job_outcome("save-job", {"Resource": {"Status": "RUNNING"}}),
            recovery_verified=True,
            error=None,
        )

    monkeypatch.setattr(profile_persist, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(profile_persist, "POLL_SECONDS", 0)
    monkeypatch.setattr(profile_persist, "read_partition_profile", read)
    monkeypatch.setattr(profile_persist, "save_current_partition_profile", save)
    return fake


def _submit(patch, *, continuation="none", allow_disruption=False):
    model = ReconfigurePatch(**patch)
    request = OperationRequest(
        "hmc_reconfigure_lpar",
        "agent-a",
        "<default>",
        "hmc.test",
        "n1",
        {
            "lpar_name_or_uuid": "web1",
            "system_name_or_uuid": "sysA",
            "patch": patch,
            "allow_disruption": allow_disruption,
        },
    )
    return engine.submit(
        request,
        reconfigure.reconfigure_body(model, allow_disruption, "sysA", "web1"),
        continuation=continuation,
        wait_seconds=10,
        classifiers=reconfigure.RECONFIGURE_CLASSIFIERS,
    )


def _writes(fake):
    return [c for c in fake.calls if c != "authorize"]


def test_networks_need_the_window_and_their_tools():
    patch = ReconfigurePatch(
        networks={"add": [{"port_vlan_id": 10}], "remove": [{"mac": MAC_A}]}
    )
    assert patch.needs_window
    assert reconfigure.delegated_tools(patch) == (
        "hmc_add_network_adapter",
        "hmc_save_current_partition_profile",
        "hmc_delete_adapter",
    )
    with pytest.raises(ValidationError, match="networks"):
        ReconfigurePatch()


def test_an_add_creates_the_adapter_and_saves_the_default_profile(body):
    record = _submit({"networks": {"add": [{"port_vlan_id": 30}]}})
    assert (record.state, record.outcome) == ("terminal", "completed"), record.warnings
    assert _writes(body) == ["add 30", "save"]
    assert _effects(record)[1:] == [
        ("network_add:0", "lpar.client_network_adapter", "applied"),
        ("profile_save", "lpar.profile_save", "applied"),
    ]
    assert body.saves == [
        {"overwrite": True, "expected_etag": "E1", "exclusive_writer_window": True}
    ]
    change = record.result["changes"][0]
    assert (change["field"], change["applied"]) == ("networks.add", "profile")
    assert record.result["observed"]["networks"][-1] == "02:00:00:00:00:0e"


def test_a_removal_deletes_one_adapter_and_its_slot_leaves_the_profile(body):
    record = _submit({"networks": {"remove": [{"mac": MAC_B}]}})
    assert record.outcome == "completed", record.warnings
    assert _writes(body) == ["delete ub", "save"]
    assert body.saved_slots == {"2"}
    assert [a["UUID"] for a in body.adapters] == ["UA"]


def test_removing_the_last_adapter_confirms_an_empty_profile(body):
    body.adapters = body.adapters[:1]
    body.saved_slots = {"2"}
    record = _submit({"networks": {"remove": [{"mac": MAC_A}]}})
    assert record.outcome == "completed", record.warnings
    assert body.saved_slots == set()


def test_adds_run_before_removals(body):
    record = _submit(
        {"networks": {"add": [{"port_vlan_id": 30}], "remove": [{"mac": MAC_A}]}}
    )
    assert record.outcome == "completed", record.warnings
    assert _writes(body) == ["add 30", "delete ua", "save"]


def test_an_unconfirmed_save_pauses_warns_and_resumes_without_adding_again(body):
    body.save_lands = False
    record = _submit({"networks": {"add": [{"port_vlan_id": 30}]}})
    assert record.outcome == "needs_attention"
    assert any("current configuration only" in w for w in record.result["warnings"])
    body.save_lands = True
    resumed = _submit(
        {"networks": {"add": [{"port_vlan_id": 30}]}}, continuation="resume"
    )
    assert resumed.outcome == "completed", resumed.warnings
    assert _writes(body).count("add 30") == 1


def test_a_live_removal_needs_disruption_and_reports_live(body):
    body.state = "running"
    refused = _submit({"networks": {"remove": [{"mac": MAC_A}]}})
    assert (refused.outcome, refused.effects) == ("failed", ())
    assert "allow_disruption=true" in refused.warnings[0]


@pytest.mark.parametrize(
    ("setup", "patch", "message"),
    [
        (lambda f: setattr(f, "level", "1050"), {}, "10.3.1060"),
        (
            lambda f: f.profile.find(".//{*}DesiredMemory").__setattr__("text", "1024"),
            {},
            "desired memory differs",
        ),
        (
            lambda f: setattr(f, "state", "running"),
            {"memory_mib": 4096},
            "pending_activation",
        ),
    ],
)
def test_persistence_guards_refuse_before_any_write(body, setup, patch, message):
    setup(body)
    record = _submit({**patch, "networks": {"add": [{"port_vlan_id": 30}]}})
    assert (record.outcome, record.effects) == ("failed", ())
    assert message in record.warnings[0]
    assert "a change that saves" in record.warnings[0]
    assert _writes(body) == []


def test_a_crash_inside_the_add_is_classified_on_resume(body):
    calls = {"n": 0}
    real = body.add_network_adapter

    async def dies(lpar, vlan, *, mac_address=None):
        calls["n"] += 1
        if calls["n"] == 1:
            await real(lpar, vlan, mac_address=mac_address)
            raise HMCError("connection reset", None, "")
        return await real(lpar, vlan, mac_address=mac_address)

    body.add_network_adapter = dies
    record = _submit({"networks": {"add": [{"port_vlan_id": 30}]}})
    assert record.outcome == "needs_attention"
    resumed = _submit(
        {"networks": {"add": [{"port_vlan_id": 30}]}}, continuation="resume"
    )
    assert resumed.outcome == "completed", resumed.warnings
    assert calls["n"] == 1


def test_a_plan_on_a_busy_partition_fits_the_store():
    fake = NetHMC()
    fake.adapters = [
        _entry(
            f"{n:08x}-0000-4000-8000-000000000000",
            f"02:00:00:00:{n // 256:02x}:{n % 256:02x}",
            100 + n,
            n,
        )
        for n in range(60)
    ] + [
        _entry(
            f"{n:08x}-0000-4000-9000-000000000000",
            f"02:00:00:01:00:{n:02x}",
            10,
            70 + n,
        )
        for n in range(4)
    ]
    plan = _plan(
        fake,
        {
            "add": [{"port_vlan_id": 10}, {"port_vlan_id": 20}, {"port_vlan_id": 30}],
            "remove": [{"mac": f"02:00:00:00:00:{n:02x}"} for n in range(4)],
        },
    )
    assert len(json.dumps(plan, sort_keys=True)) < store.MAX_IDENTITY_BYTES // 2


def test_a_removal_save_that_did_not_land_is_not_confirmed(body):
    body.save_lands = False
    record = _submit({"networks": {"remove": [{"mac": MAC_B}]}})
    assert record.outcome == "needs_attention"
    assert _effects(record)[-1][0] == "profile_save"
    assert _effects(record)[-1][2] != "applied"


def test_verification_waits_for_a_removed_adapter_to_go(body):
    body.delete_lands = False
    record = _submit({"networks": {"remove": [{"mac": MAC_B}]}})
    assert record.outcome == "needs_attention"
    assert "save" not in _writes(body)


def test_plan_refuses_a_removal_whose_adapter_reports_no_slot():
    fake = NetHMC()
    del fake.adapters[1]["Resource"]["VirtualSlotNumber"]
    with pytest.raises(OperationFailed, match="reports no virtual slot"):
        _plan(fake, {"remove": [{"mac": MAC_B}]})


def test_an_unclassified_add_error_warns_without_the_hmc_body(body):
    body.add_error = HMCError("bad gateway", 502, "<Message>cookie=secret</Message>")
    record = _submit({"networks": {"add": [{"port_vlan_id": 30}]}})
    assert record.outcome == "needs_attention"
    text = " ".join([*record.warnings, *record.result["warnings"]])
    assert "HMCError" in text and "secret" not in text


@pytest.mark.parametrize(
    "other", [{"boot": "root_disk"}, {"installer_media": "detach"}]
)
def test_networks_cannot_share_a_patch_with_the_installer_legs(other):
    with pytest.raises(ValidationError, match="cannot share a patch"):
        ReconfigurePatch(networks={"remove": [{"mac": MAC_A}]}, **other)
