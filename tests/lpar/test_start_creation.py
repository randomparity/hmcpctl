"""Start creation admission, record composition and independent configuration proof."""

import csv
import io
import shlex
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from lpar.test_serial_creation import target as serial_target  # noqa: F401 — shared boundary fixture

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter
from hmcpctl.documents.lpar_start import LparStartSettings
from hmcpctl.operations.lpar.start import (
    admit_start_creation,
    recheck_start_controllers,
)
from hmcpctl.ssh import lpar as native

pytestmark = pytest.mark.asyncio

SYSTEM = "11111111-1111-4111-8111-111111111111"
PEER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def entry(kind="LogicalPartition", name="peer", native_id="7", uuid=PEER):
    return {
        "UUID": uuid,
        "ResourceType": kind,
        "Resource": {
            "PartitionName": name,
            "PartitionID": native_id,
            "Description": "",
        },
    }


@pytest.fixture
def hmc():
    client = AsyncMock(spec=HMCClient)
    client.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "u", "agent_id": "agent"}
    )
    client.list_logical_partitions.return_value = [entry()]
    client.list_vios.return_value = []
    client.get_uom.return_value = entry()
    return client


@pytest.mark.parametrize("kind", ["LogicalPartition", "VirtualIOServer"])
@pytest.mark.parametrize("by_name", [False, True])
async def test_controllers_authorized_before_create(hmc, kind, by_name):
    peer = entry(kind)
    hmc.list_logical_partitions.return_value = (
        [peer] if kind == "LogicalPartition" else []
    )
    hmc.list_vios.return_value = [peer] if kind == "VirtualIOServer" else []
    hmc.get_uom.return_value = peer
    settings = LparStartSettings(
        **(
            {"power_ctrl_lpar_names": ("peer",)}
            if by_name
            else {"power_ctrl_lpar_ids": (7,)}
        )
    )
    admission = await admit_start_creation(hmc, SYSTEM, "system", settings)
    assert admission.settings.power_ctrl_lpar_ids == (7,)
    assert admission.settings.power_ctrl_lpar_names is None
    assert admission.controllers[0].kind == kind
    await recheck_start_controllers(hmc, admission)
    hmc.create_logical_partition.assert_not_called()
    hmc.create_vios.assert_not_called()


@pytest.mark.parametrize(
    "fault", ["missing", "ambiguous", "foreign", "malformed", "denied", "unreadable"]
)
async def test_controllers_refuse_before_create(hmc, fault):
    if fault == "missing":
        hmc.list_logical_partitions.return_value = []
    elif fault == "ambiguous":
        hmc.list_vios.return_value = [entry("VirtualIOServer", uuid=SYSTEM)]
    elif fault == "foreign":
        hmc.get_uom.return_value = entry(uuid=SYSTEM)
    elif fault == "malformed":
        hmc.list_logical_partitions.return_value = [entry(native_id="True")]
    elif fault == "denied":
        peer = entry()
        peer["Resource"]["Description"] = "[hmcpctl owner:other created:2026-10-28]"
        hmc.get_uom.return_value = peer
    else:
        hmc.list_vios.return_value = None
    with pytest.raises((ValueError, TypeError, PermissionError)):
        await admit_start_creation(
            hmc, SYSTEM, "system", LparStartSettings(power_ctrl_lpar_ids=(7,))
        )
    hmc.create_logical_partition.assert_not_called()
    hmc.create_vios.assert_not_called()


async def test_controllers_rechecked_for_identity_and_ownership(hmc):
    admission = await admit_start_creation(
        hmc, SYSTEM, "system", LparStartSettings(power_ctrl_lpar_ids=(7,))
    )
    hmc.get_uom.return_value = entry(name="changed")
    with pytest.raises(ValueError):
        await recheck_start_controllers(hmc, admission)


async def test_record_composes_all_native_families(monkeypatch):
    commands = []

    async def command(config, cmd):
        commands.append(cmd)
        return ""

    monkeypatch.setattr(native, "run_hmc_command", command)
    await native.create_lpar_via_cli(
        HMCConfig.from_mapping({}),
        "system",
        "new",
        resources=LparResources(min_memory=1024, desired_memory=1024, max_memory=1024),
        max_virtual_slots=8,
        virtual_serial_adapters=(
            VirtualSerialAdapter(2, "client", False, remote_lpar_id=7, remote_slot=3),
        ),
        start_settings=LparStartSettings(
            auto_start=False, conn_monitoring=True, power_ctrl_lpar_ids=(7, 8)
        ),
    )
    record = shlex.split(commands[0])[-1]
    assert record.endswith('"power_ctrl_lpar_ids=7,8"')
    cells = next(csv.reader(io.StringIO(record)))
    assert "auto_start=0" in cells and "conn_monitoring=1" in cells
    assert any(cell.startswith("virtual_serial_adapters=") for cell in cells)
    assert cells[-1] == "power_ctrl_lpar_ids=7,8"


@pytest.mark.parametrize("controller", [False, True])
@pytest.mark.parametrize(
    "field,value,current,saved",
    [
        ("auto_start", False, None, "AutoStart"),
        (
            "conn_monitoring",
            False,
            "IsConnectionMonitoringEnabled",
            "ConnectionMonitoringEnabled",
        ),
        ("boot_mode", "norm", "BootMode", "BootMode"),
    ],
)
async def test_readback_planes(
    hmc, monkeypatch, controller, field, value, current, saved
):
    from types import SimpleNamespace

    from hmcpctl.operations.lpar import profiles, start

    observed = entry(name="new")
    observed["Resource"].update(
        PartitionType="AIX/Linux", DefaultProfileName="default_profile"
    )
    text = "Normal" if field == "boot_mode" else "false"
    if current:
        observed["Resource"][current] = text
    hmc.list_logical_partitions.return_value = [observed]
    hmc.get_uom.return_value = observed
    reader = AsyncMock(return_value=SimpleNamespace(fields={saved: text}))
    monkeypatch.setattr(profiles, "read_partition_profile", reader)
    admission = await admit_start_creation(
        hmc, SYSTEM, "system", LparStartSettings(**{field: value})
    )
    step = await start.created_start_step(
        hmc, admission, observed, "new", "AIX/Linux", controller_mode=controller
    )
    assert step.status == ("error" if controller and field == "auto_start" else "ok")
    if controller:
        reader.assert_not_called()
    if current:
        hmc.get_uom.return_value["Resource"].pop(current)
        unavailable = await start.created_start_step(
            hmc, admission, observed, "new", "AIX/Linux", controller_mode=controller
        )
        assert unavailable.status == "error"


@pytest.mark.parametrize(
    "fault", ["empty", "foreign", "duplicate", "missing", "malformed"]
)
async def test_power_control_readback(hmc, monkeypatch, fault):
    from types import SimpleNamespace

    from hmcpctl.operations.lpar import profiles, start

    admission = await admit_start_creation(
        hmc, SYSTEM, "system", LparStartSettings(power_ctrl_lpar_ids=(7,))
    )
    local = entry(name="new", native_id="8", uuid=SYSTEM)
    local["Resource"].update(
        PartitionType="AIX/Linux", DefaultProfileName="default_profile"
    )
    collection = {
        "link": {"href": f"https://hmc.test/rest/api/uom/LogicalPartition/{PEER}"}
    }
    local["Resource"]["PowerControlPartitions"] = deepcopy(collection)
    hmc.list_logical_partitions.return_value = [entry(), local]

    async def get(kind, uuid, **kwargs):
        return entry() if uuid == PEER else local

    hmc.get_uom.side_effect = get
    monkeypatch.setattr(
        profiles,
        "read_partition_profile",
        AsyncMock(
            return_value=SimpleNamespace(fields={"PowerControlPartitions": collection})
        ),
    )
    assert (
        await start.created_start_step(hmc, admission, local, "new", "AIX/Linux")
    ).status == "ok"
    bad = local["Resource"]
    if fault == "empty":
        bad["PowerControlPartitions"] = ""
    elif fault == "foreign":
        bad["PowerControlPartitions"] = {
            "link": {
                "href": f"https://foreign.test/rest/api/uom/LogicalPartition/{PEER}"
            }
        }
    elif fault == "duplicate":
        bad["PowerControlPartitions"] = {
            "link": [collection["link"], collection["link"]]
        }
    elif fault == "missing":
        bad.pop("PowerControlPartitions")
    else:
        bad["PowerControlPartitions"] = {"Unexpected": "opaque"}
    assert (
        await start.created_start_step(hmc, admission, local, "new", "AIX/Linux")
    ).status == "error"


async def test_actual_creators_authorize_controllers_before_effect(serial_target):  # noqa: F811 — imported pytest boundary fixture
    from lpar.test_serial_creation import invoke

    b = serial_target
    b.peer["Resource"]["Description"] = "[hmcpctl owner:other created:2026-10-28]"
    with pytest.raises(PermissionError):
        await invoke(
            b, adapters=(), start_settings=LparStartSettings(power_ctrl_lpar_ids=(7,))
        )
    assert "create" not in b.events and "stamp" not in b.events
    b.hmc.create_logical_partition.assert_not_called()
    b.hmc.create_vios.assert_not_called()


@pytest.mark.parametrize("fault", ["create", "timeout"])
async def test_actual_creators_unknown_stop_without_fallback(serial_target, fault):  # noqa: F811 — imported pytest boundary fixture
    from lpar.test_serial_creation import invoke

    from hmcpctl.errors import HMCError

    b = serial_target
    b.fault = fault
    with pytest.raises(HMCError):
        await invoke(b, adapters=(), start_settings=LparStartSettings(auto_start=False))
    assert b.events.count("create") == 1
    assert "stamp" not in b.events and "apply" not in b.events
    b.hmc.create_logical_partition.assert_not_called()
    b.hmc.create_vios.assert_not_called()


@pytest.mark.parametrize("fault", [None, "identity", "field"])
async def test_postcreate_identity_and_family_barriers(
    serial_target,  # noqa: F811 — imported pytest boundary fixture
    monkeypatch,
    fault,
):
    from types import SimpleNamespace

    from lpar.test_serial_creation import invoke

    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar import profiles

    b = serial_target
    b.local["Resource"]["AutoStart"] = "false"
    original_get = b.hmc.get_uom.side_effect

    async def get(kind, uuid, **kwargs):
        found = await original_get(kind, uuid)
        if fault == "identity" and uuid == b.local["UUID"]:
            found["Resource"]["PartitionID"] = "999"
        return found

    b.hmc.get_uom.side_effect = get
    monkeypatch.setattr(
        profiles,
        "read_partition_profile",
        AsyncMock(
            return_value=SimpleNamespace(
                fields={"AutoStart": "true" if fault == "field" else "false"}
            )
        ),
    )
    if b.kind == "VirtualIOServer" and fault is not None:
        with pytest.raises(HMCError):
            await invoke(
                b, adapters=(), start_settings=LparStartSettings(auto_start=False)
            )
    else:
        result = await invoke(
            b,
            adapters=(),
            start_settings=LparStartSettings(auto_start=False),
            **({"apply_profile": True} if b.kind == "LogicalPartition" else {}),
        )
        if b.kind == "LogicalPartition":
            settings = next(step for step in result.steps if step.step == "settings")
            assert settings.status == ("ok" if fault is None else "error")
        else:
            assert (
                result["start_verification"]["start_settings"]["status"] == "verified"
            )
    assert b.events.count("create") == 1
    if fault is not None:
        assert "apply" not in b.events
    if fault == "identity":
        assert "stamp" not in b.events
