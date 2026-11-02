"""Live checks that classify an interrupted provision write (ADR 0313 Decision 3)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Self

import pytest

from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.operations.logical import engine
from hmcpctl.operations.logical.engine import BodyResult, OperationRequest
from hmcpctl.operations.logical.models import EffectRecord
from hmcpctl.operations.lpar.provision import (
    ProvisionAdapters,
    ProvisionRequest,
    ProvisionStorage,
)
from hmcpctl.operations.lpar.provision_operation import (
    EFFECT_KINDS,
    provision_classifiers,
)

SYSTEM = "00000000-0000-0000-0000-0000000000a1"
LPAR = "00000000-0000-0000-0000-0000000000b2"
VIOS = "00000000-0000-0000-0000-0000000000c3"
GROUP = "00000000-0000-0000-0000-0000000000d4"
STAMP = "[hmcpctl owner:hmcpctl created:2026-11-02]"
REQUEST = ProvisionRequest(
    name="lpar-a",
    adapters=ProvisionAdapters(100),
    storage=ProvisionStorage(VIOS, "lv-a", vg_uuid=GROUP, capacity_mib=1024),
    resources=LparResources(min_memory=256, desired_memory=512, max_memory=1024),
)


class FakeHMC:
    """The reads the classifiers make, answered from plain attributes."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(agent_id=None)
        self.partitions: list[dict] = []
        self.adapters: list[dict] = []
        self.groups: list[dict] = []
        self.mappings: list[dict] = []
        self.state = "not activated"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def list_logical_partitions(self, _system: str) -> list[dict]:
        return self.partitions

    async def list_adapters(self, _lpar: str, _kind: str) -> list[dict]:
        return self.adapters

    async def list_volume_groups(self, _vios: str) -> list[dict]:
        return self.groups

    async def list_storage_mappings(self, _vios: str) -> list[dict]:
        return self.mappings

    async def get_quick_property(self, *_args: str) -> str:
        return self.state


def _partition(description: str | None = None, uuid: str = LPAR) -> dict:
    resource = {"PartitionName": "lpar-a"}
    if description is not None:
        resource["Description"] = description
    return {"UUID": uuid, "Resource": resource}


def _classify(hmc: FakeHMC, key: str, target: str, *, request=REQUEST, ctx=None):
    effect = EffectRecord(key, EFFECT_KINDS[key], target, "uncertain", None)
    classifier = provision_classifiers(request, lambda _connection: hmc)[effect.kind]
    context = ctx or SimpleNamespace(
        connection="<default>", operation_id="op-self", partition=(SYSTEM, LPAR)
    )
    return asyncio.run(classifier(context, effect))


def _ledger_elsewhere(key: str, kind: str, identity: dict) -> None:
    """Record *identity* in another operation's ledger, as a completed run does."""

    async def body(ctx):
        ctx.ledger_add(key, kind, SYSTEM, LPAR, identity)
        return BodyResult("completed")

    request = OperationRequest("hmc_test_tool", "agent", "<default>", "h", "other", {})
    engine.submit(request, body, wait_seconds=10)


def test_create_classifies_by_name_on_the_system():
    hmc = FakeHMC()
    assert _classify(hmc, "create", f"system:{SYSTEM}").status == "not_applied"
    hmc.partitions = [_partition(STAMP)]
    verdict = _classify(hmc, "create", f"system:{SYSTEM}")
    assert verdict.status == "applied"
    assert verdict.identity == {"lpar_uuid": LPAR, "name": "lpar-a", "resumed": True}


@pytest.mark.parametrize(
    "case", ["foreign", "several", "physical_io", "ledgered elsewhere"]
)
def test_create_never_adopts_what_it_cannot_prove(case):
    hmc = FakeHMC()
    hmc.partitions = [_partition()]
    request = REQUEST
    if case == "foreign":
        hmc.partitions = [_partition("[hmcpctl owner:other created:2026-11-02]")]
    elif case == "several":
        hmc.partitions = [_partition(), _partition(uuid=VIOS)]
    elif case == "physical_io":
        request = replace(REQUEST, physical_io=LparPhysicalIO(console_slot="hmc"))
    else:
        _ledger_elsewhere("partition", "partition", {"lpar_uuid": LPAR})
    verdict = _classify(hmc, "create", f"system:{SYSTEM}", request=request)
    assert verdict.status == "needs_attention"


@pytest.mark.parametrize(
    ("description", "status"),
    [
        (STAMP, "applied"),
        (None, "not_applied"),
        ("[hmcpctl owner:other created:2026-11-02]", "needs_attention"),
    ],
)
def test_stamp_classifies_by_the_description(description, status):
    hmc = FakeHMC()
    hmc.partitions = [_partition(description)]
    assert _classify(hmc, "stamp", f"lpar:{LPAR}").status == status


def _adapter(uuid: str, vlan: str = "100") -> dict:
    return {
        "UUID": uuid,
        "Resource": {"PortVLANID": vlan, "MACAddress": "AABBCCDDEEFF"},
    }


def test_network_classifies_by_the_adapters_on_the_vlan():
    hmc = FakeHMC()
    hmc.adapters = [_adapter("a0", vlan="200")]
    assert _classify(hmc, "network", f"lpar:{LPAR}").status == "not_applied"
    hmc.adapters.append(_adapter("A1"))
    verdict = _classify(hmc, "network", f"lpar:{LPAR}")
    assert (verdict.status, verdict.identity) == (
        "applied",
        {"adapter_uuid": "a1", "mac": "aa:bb:cc:dd:ee:ff"},
    )
    hmc.adapters.append(_adapter("a2"))
    assert _classify(hmc, "network", f"lpar:{LPAR}").status == "needs_attention"


def _group(*disks: str) -> dict:
    virtual = [{"DiskName": name} for name in disks]
    return {"UUID": GROUP, "Resource": {"VirtualDisks": {"VirtualDisk": virtual}}}


def test_disk_classifies_by_name_in_its_group():
    hmc = FakeHMC()
    hmc.groups = [_group("other")]
    assert _classify(hmc, "disk", f"vios:{VIOS}").status == "not_applied"
    hmc.groups = [_group("other", "lv-a")]
    verdict = _classify(hmc, "disk", f"vios:{VIOS}")
    assert verdict.status == "applied"
    assert verdict.identity == {
        "vios_uuid": VIOS,
        "vg_uuid": GROUP,
        "disk_name": "lv-a",
        "capacity_mib": 1024,
    }


def test_disk_another_operation_owns_needs_attention():
    _ledger_elsewhere(
        "root_disk",
        "virtual_disk",
        {"vios_uuid": VIOS, "vg_uuid": GROUP, "disk_name": "lv-a"},
    )
    hmc = FakeHMC()
    hmc.groups = [_group("lv-a")]
    assert _classify(hmc, "disk", f"vios:{VIOS}").status == "needs_attention"


def test_mapping_classifies_by_backing_and_client():
    hmc = FakeHMC()
    mapping = {
        "Resource": {
            "Storage": {"VirtualDisk": {"DiskName": "lv-a"}},
            "AssociatedLogicalPartition": {
                "href": f"https://hmc.test/rest/api/uom/LogicalPartition/{LPAR}"
            },
        }
    }
    assert _classify(hmc, "storage", f"lpar:{LPAR}").status == "not_applied"
    hmc.mappings = [mapping]
    verdict = _classify(hmc, "storage", f"lpar:{LPAR}")
    assert (verdict.status, verdict.identity) == (
        "applied",
        {"vios_uuid": VIOS, "storage_name": "lv-a"},
    )


@pytest.mark.parametrize(
    ("state", "status"), [("running", "applied"), ("not activated", "needs_attention")]
)
def test_power_on_classifies_by_partition_state(state, status):
    hmc = FakeHMC()
    hmc.state = state
    assert _classify(hmc, "power_on", f"lpar:{LPAR}").status == status


def test_a_running_operation_reports_no_step_for_a_write_in_flight():
    from hmcpctl.operations.logical.models import OperationRecord
    from hmcpctl.operations.lpar.provision_operation import result_from_record

    effects = (
        EffectRecord(
            "create", "lpar.create", "system:s", "applied", {"lpar_uuid": LPAR}
        ),
        EffectRecord("stamp", "lpar.ownership_stamp", f"lpar:{LPAR}", "intended", None),
    )
    record = OperationRecord(
        "0" * 32,
        "r",
        "hmc_provision_lpar",
        "<default>",
        SYSTEM,
        LPAR,
        "running",
        "creating",
        None,
        effects,
        (),
        False,
        (),
        (),
        None,
        "t",
        "t",
    )
    result = result_from_record(record)
    assert [(s.step, s.status) for s in result.steps] == [("create", "ok")]
    assert result.ownership_stamped is None
    paused = replace(record, state="paused", outcome="needs_attention")
    assert [s.status for s in result_from_record(paused).steps] == ["ok", "error"]


def test_a_paused_result_carries_the_engine_s_reason():
    from hmcpctl.operations.logical.models import OperationRecord
    from hmcpctl.operations.lpar.provision_operation import result_from_record

    effects = (
        EffectRecord(
            "create", "lpar.create", "system:s", "applied", {"lpar_uuid": LPAR}
        ),
        EffectRecord(
            "network", EFFECT_KINDS["network"], f"lpar:{LPAR}", "uncertain", None
        ),
    )
    body_result = {
        "resource_created": True,
        "workflow_completed": False,
        "lpar_uuid": LPAR,
        "dry_run": False,
        "ownership_stamped": True,
        "steps": [
            {"step": "create", "status": "ok"},
            {"step": "network", "status": "error", "result": "HMCError: x"},
        ],
        "warnings": ["Change location not read"],
    }
    record = OperationRecord(
        "0" * 32,
        "r",
        "hmc_provision_lpar",
        "<default>",
        SYSTEM,
        LPAR,
        "paused",
        "creating",
        "needs_attention",
        effects,
        (),
        False,
        ("effect network: the VLAN's adapters are ambiguous",),
        ("resume", "abandon"),
        body_result,
        "t",
        "t",
    )
    result = result_from_record(record)
    assert result.steps[1].result == "HMCError: x"
    assert result.warnings == (
        "Change location not read",
        "effect network: the VLAN's adapters are ambiguous",
    )
