"""hmc_reconfigure_lpar's client network adapter legs (ADR 0325, #1845).

An add creates a client network adapter on a VLAN; a removal deletes the one adapter
carrying a MAC. The fake HMC holds one partition's client network adapters and the
system's virtual networks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from hmcpctl.errors import HMCError
from hmcpctl.operations.logical.engine import EffectNotApplied, OperationFailed
from hmcpctl.operations.lpar import reconfigure_network as net
from hmcpctl.operations.lpar.reconfigure_network import NetworkPatch

SYS = "0000000a-0000-4000-8000-000000000000"
LPAR = "0000000b-0000-4000-8000-000000000000"
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

    async def list_child(self, parent, uuid, child):
        assert (parent, uuid, child) == (
            "LogicalPartition",
            LPAR,
            "ClientNetworkAdapter",
        )
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
    return SimpleNamespace(connection="c", recorded=records.get)


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
    assert net.removed_slots(plan) == ("3",)
    assert net.adds(plan)
    assert [s[0] for s in net.steps(plan)] == [
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

    async def broken(*_):
        raise HMCError("boom", 500, "secret body")

    fake.list_child = broken
    with pytest.raises(OperationFailed, match="could not be read \\(HMCError\\)"):
        _plan(fake, {"remove": [{"mac": MAC_A}]})


def test_plan_without_networks_key_has_no_steps():
    plan = {"lpar_uuid": LPAR}
    assert net.steps(plan) == []
    assert net.removed_slots(plan) == ()
    assert not net.adds(plan)


# --- writes ------------------------------------------------------------------


def _write(fake, plan, key):
    return _run(net.write_for(fake, plan, key)())


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


def test_add_refusal_is_not_applied_with_its_guidance():
    fake = NetHMC()
    fake.add_error = HMCError("refused", 500, "<Message>REST0269 ...</Message>")
    plan = _plan(fake, {"add": [{"port_vlan_id": 10}]})
    with pytest.raises(EffectNotApplied, match="REST0269"):
        _write(fake, plan, "network_add:0")


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
    def opener(_connection):
        return fake

    return (
        net.add_classifier(lambda _ctx: plan, opener),
        net.remove_classifier(lambda _ctx: plan, opener),
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
    done, seen = _run(net.observed_networks(ctx, fake, plan))
    assert not done
    _write(fake, plan, "network_remove:0")
    done, seen = _run(net.observed_networks(ctx, fake, plan))
    assert done and MAC_A not in seen["networks"]


def test_observed_is_trivially_done_without_networks():
    assert _run(net.observed_networks(_ctx(), NetHMC(), {"lpar_uuid": LPAR})) == (
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
    notes = net.warnings(ctx, plan, saved=False)
    assert any("HTTP 500" in n for n in notes)
    assert any("current configuration only" in n for n in notes)
    assert not any(
        "current configuration only" in n for n in net.warnings(ctx, plan, True)
    )
    assert net.warnings(_ctx(), plan, saved=False) == []


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
    listing = fake.list_child

    async def flaky(*args):
        reads.append(args)
        if len(reads) > 1:
            raise HMCError("relist", 503, "")
        return await listing(*args)

    fake.list_child = flaky
    with pytest.raises(HMCError, match="refused"):
        _write(fake, plan, "network_remove:0")
