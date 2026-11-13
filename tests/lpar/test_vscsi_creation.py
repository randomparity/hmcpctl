"""vSCSI client records at creation: grammar, admission, render and readback (ADR 0376)."""

import csv
import io
import shlex
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter
from hmcpctl.documents.lpar_vscsi import (
    VirtualScsiClientAdapter as Client,
)
from hmcpctl.documents.lpar_vscsi import (
    validate_vscsi_input,
)
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.assignments import LparPcieAssignments
from hmcpctl.operations.lpar.creation import core
from hmcpctl.operations.lpar.creation import workflow as workflows
from hmcpctl.ssh.lpar_vscsi import parse_vscsi_profile
from hmcpctl.ssh.transport import HMCCLIError

SYSTEM = "11111111-1111-4111-8111-111111111111"
LOCAL = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
VIOS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
CLIENT = Client(2, 20, False, remote_lpar_id=7)
PRIVATE = "private-transport-marker"


def entry(uuid, name, native_id, kind, environment="AIX/Linux"):
    return {
        "UUID": uuid,
        "ResourceType": kind,
        "Resource": {
            "PartitionName": name,
            "PartitionID": str(native_id),
            "PartitionType": environment,
            "Description": "",
            "DefaultProfileName": "default_profile",
            "PartitionIOConfiguration": {
                "MaximumVirtualIOSlots": "8" if uuid == LOCAL else "64"
            },
        },
    }


def profile(scsi, serial="none", name="new", native_id=9):
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow(
        f"{k}={v}"
        for k, v in {
            "name": "default_profile",
            "lpar_name": name,
            "lpar_id": native_id,
            "max_virtual_slots": 8,
            "virtual_serial_adapters": serial,
            "virtual_eth_adapters": "none",
            "virtual_fc_adapters": "none",
            "virtual_scsi_adapters": scsi,
            "vnic_adapters": "none",
        }.items()
    )
    return output.getvalue()


@pytest.fixture(params=("AIX/Linux", "OS400"))
def target(request, monkeypatch, ibmi_console):
    environment = request.param
    local = entry(LOCAL, "new", 9, "LogicalPartition", environment)
    vios = entry(VIOS, "vios-R1", 7, "VirtualIOServer", "Virtual IO Server")
    other = entry(OTHER, "example-lpar", 5, "LogicalPartition")
    feeds = {"LogicalPartition": [other], "VirtualIOServer": [vios]}
    b = SimpleNamespace(
        environment=environment,
        local=local,
        vios=vios,
        feeds=feeds,
        created=False,
        events=[],
        controller=False,
        fault=None,
        local_record=profile("2/client/7/vios-R1/20/0"),
    )
    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "test", "agent_id": "agent"}
    )
    hmc.get_managed_system.return_value = {
        "UUID": SYSTEM,
        "Resource": {
            "SystemName": "system",
            "IsClassicHMCManagement": "true",
            "IsHMCPowerVMManagementController": "false",
            "IsPowerVMManagementController": "false",
            "AssociatedSystemMemoryConfiguration": {"ConfigurableSystemMemory": "8192"},
        },
    }
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM, "SystemName": "system", "State": "operating"}
    ]
    hmc.list_logical_partitions.side_effect = lambda system: deepcopy(
        feeds["LogicalPartition"]
    )
    hmc.list_vios.side_effect = lambda system: deepcopy(feeds["VirtualIOServer"])
    resources = {LOCAL: local, VIOS: vios, OTHER: other}

    async def get(kind, uuid):
        b.events.append("identity:" + uuid)
        return deepcopy(resources[uuid])

    hmc.get_uom.side_effect = get
    hmc.get_logical_partition.return_value = deepcopy(local)

    async def find(name, **kwargs):
        return deepcopy(local) if b.created else None

    hmc.find_partition_by_name.side_effect = find

    async def command(config, cmd):
        if cmd == "lshmc -V":
            return "Version: 10\nRelease: 3\nService Pack: 1060"
        if "-F type_model" in cmd:
            return "8375-42A"
        if cmd.startswith("lssyscfg -r sys"):
            return f"{SYSTEM},system"
        if cmd.startswith("lscomgmt"):
            b.events.append("controller")
            return "1" if b.controller else "null"
        if cmd.startswith("mksyscfg"):
            b.events.append("create")
            b.created = True
            feeds["LogicalPartition"].append(local)
            if b.fault == "create":
                raise HMCCLIError(PRIVATE)
            return ""
        if "description=" in cmd:
            b.events.append("stamp")
            return ""
        if "-o apply" in cmd:
            b.events.append("apply")
        return ""

    async def read(config, system, name, profile_name, *, max_output_bytes):
        b.events.append("profile:" + name)
        if b.fault == "profile":
            raise HMCError(PRIVATE)
        return b.local_record

    b.command = AsyncMock(side_effect=command)
    b.reader = AsyncMock(side_effect=read)
    for module in (
        "hmcpctl.ssh.lpar",
        "hmcpctl.ssh.profiles",
        "hmcpctl.operations.lpar.linux_roles",
        "hmcpctl.ssh.sriov",
    ):
        monkeypatch.setattr(module + ".run_hmc_command", b.command)
    monkeypatch.setattr("hmcpctl.ssh.lpar_serial.read_lpar_profile_record", b.reader)
    monkeypatch.setattr("hmcpctl.ssh.lpar_vscsi.read_lpar_profile_record", b.reader)
    b.console = ibmi_console(environment)
    b.hmc = hmc
    return b


async def invoke(b, *, adapters=(CLIENT,), **options):
    options.setdefault("physical_io", b.console)
    creation = core.LparCreation(
        "new",
        b.environment,
        options.pop("resources", LparResources(desired_memory=1024)),
        max_virtual_slots=8,
        virtual_scsi_adapters=adapters,
        **options,
    )
    return await workflows.create_lpar(b.hmc, SYSTEM, creation, LparPcieAssignments())


def native_record(b):
    commands = [
        c.args[1] for c in b.command.await_args_list if c.args[1].startswith("mksyscfg")
    ]
    assert len(commands) == 1
    return shlex.split(commands[0])[-1]


def vscsi_step(result):
    return next(s for s in result.steps if s.step == "virtual_scsi_adapters")


# --- record grammar --------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"remote_lpar_id": 7, "remote_lpar_name": "vios-R1"}, "exactly one"),
        ({}, "exactly one"),
        ({"remote_lpar_id": 0}, "remote_lpar_id"),
        ({"remote_lpar_name": "vios/R1"}, "delimiter"),
        ({"remote_lpar_name": ""}, "nonempty"),
    ],
)
def test_record_refuses_selector_shapes(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Client(2, 20, False, **kwargs)


@pytest.mark.parametrize(
    ("local", "remote", "message"), [(1, 20, "local_slot"), (2, 1, "remote_slot")]
)
def test_record_refuses_reserved_slots(local, remote, message):
    with pytest.raises(ValueError, match=message):
        Client(local, remote, False, remote_lpar_id=7)


def test_record_has_no_role_and_refuses_extra_keys():
    with pytest.raises(TypeError):
        Client(2, 20, False, remote_lpar_id=7, role="server")  # type: ignore[call-arg]
    adapter = TypeAdapter(tuple[Client, ...])
    with pytest.raises(ValidationError):
        adapter.validate_python(
            [
                {
                    "local_slot": 2,
                    "remote_slot": 20,
                    "is_required": False,
                    "remote_lpar_id": 7,
                    "role": "server",
                }
            ]
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            [
                {
                    "local_slot": 2,
                    "remote_slot": 20,
                    "is_required": 0,
                    "remote_lpar_id": 7,
                }
            ]
        )


def test_record_renders_six_components():
    assert CLIENT.native() == "2/client/7//20/0"
    assert Client(3, 21, True, remote_lpar_name="vios-R1").native() == (
        "3/client//vios-R1/21/1"
    )


# --- slot rules ------------------------------------------------------------


def test_slots_render_collection_and_empty_input():
    second = Client(3, 21, True, remote_lpar_name="vios-R1")
    assert validate_vscsi_input((CLIENT, second), 8) == (
        "2/client/7//20/0,3/client//vios-R1/21/1"
    )
    assert validate_vscsi_input((), None) == ""


@pytest.mark.parametrize(
    ("adapters", "maximum", "serial", "error"),
    [
        ((CLIENT, Client(2, 21, False, remote_lpar_id=7)), 8, (), ValueError),
        ((Client(8, 20, False, remote_lpar_id=7),), 8, (), ValueError),
        ((CLIENT,), None, (), ValueError),
        (
            (CLIENT,),
            8,
            (VirtualSerialAdapter(2, "server", False, 0),),
            ValueError,
        ),
        ([CLIENT], 8, (), TypeError),
        ((object(),), 8, (), TypeError),
    ],
    ids=[
        "duplicate",
        "at-maximum",
        "no-maximum",
        "serial-collision",
        "list",
        "foreign",
    ],
)
def test_slots_refuse_collisions_and_bounds(adapters, maximum, serial, error):
    with pytest.raises(error):
        validate_vscsi_input(adapters, maximum, serial)


def test_slots_bound_encoded_collection():
    adapters = tuple(
        Client(slot, 20, False, remote_lpar_name="v" * 600) for slot in range(2, 200)
    )
    with pytest.raises(ValueError, match="64 KiB"):
        validate_vscsi_input(adapters, 1000)


def test_keylock_refused_with_vscsi():
    with pytest.raises(ValueError, match="keylock"):
        core.validate_lpar_creation(
            core.LparCreation(
                "new",
                "OS400",
                LparResources(desired_memory=1024),
                keylock="normal",
                max_virtual_slots=8,
                virtual_scsi_adapters=(CLIENT,),
            )
        )


# --- tagged I/O binding (ADR 0382) ------------------------------------------


def tagged_creation(io, adapters=(CLIENT,), serial=()):
    return core.LparCreation(
        "new",
        "OS400",
        LparResources(desired_memory=1024),
        max_virtual_slots=8,
        physical_io=io,
        virtual_serial_adapters=serial,
        virtual_scsi_adapters=adapters,
    )


@pytest.mark.parametrize(
    "io",
    [
        LparPhysicalIO(load_source_slot="2", console_slot="hmc"),
        LparPhysicalIO(alt_restart_device_slot="2", console_slot="hmc"),
        LparPhysicalIO(
            load_source_slot="2", alt_restart_device_slot="2", console_slot="hmc"
        ),
    ],
    ids=["load-source", "alt-restart", "both"],
)
def test_tagged_binding_admits_a_declared_client(io):
    core.validate_lpar_creation(tagged_creation(io))


@pytest.mark.parametrize(
    ("name", "slot", "serial"),
    [
        ("load_source_slot", "3", ()),
        ("load_source_slot", "0", ()),
        ("alt_restart_device_slot", "1", ()),
        ("load_source_slot", "3", (VirtualSerialAdapter(3, "server", False, 0),)),
        ("alt_restart_device_slot", "4", ()),
    ],
    ids=["undeclared", "hmc-serial-0", "hmc-serial-1", "declared-serial", "ethernet"],
)
def test_tagged_binding_refuses_other_virtual_slots(name, slot, serial):
    # Creation has no virtual ethernet input, so an ethernet slot is undeclared.
    io = LparPhysicalIO(console_slot="hmc", **{name: slot})
    with pytest.raises(ValueError, match="vSCSI client adapter declared"):
        core.validate_lpar_creation(tagged_creation(io, serial=serial))


def test_tagged_binding_refuses_without_any_client():
    io = LparPhysicalIO(load_source_slot="2", console_slot="hmc")
    with pytest.raises(ValueError, match="vSCSI client adapter declared"):
        core.validate_lpar_creation(tagged_creation(io, adapters=()))


@pytest.mark.asyncio
async def test_ibmi_native_create_requires_console(target):
    b = target
    if b.environment == "OS400":
        with pytest.raises(ValueError, match="console_slot"):
            await invoke(b, physical_io=LparPhysicalIO())
        assert "create" not in b.events and "stamp" not in b.events
    else:
        result = await invoke(b, physical_io=LparPhysicalIO())
        assert vscsi_step(result).status == "ok"


# --- readback parser -------------------------------------------------------


def test_parse_profile_binds_identity_and_shape():
    assert parse_vscsi_profile(profile("none"), "new", 9, "default_profile") == {}
    assert parse_vscsi_profile(
        profile("2/client/7/vios-R1/20/0"), "new", 9, "default_profile"
    ) == {2: ("2", "client", "7", "vios-R1", "20", "0")}
    for bad in ("2/client/7/vios-R1/20", "x/client/7//20/0", "2/client/7//any/0"):
        with pytest.raises(ValueError):
            parse_vscsi_profile(profile(bad), "new", 9, "default_profile")
    with pytest.raises(ValueError, match="duplicate"):
        parse_vscsi_profile(
            profile("2/client/7//20/0,2/client/7//21/0"), "new", 9, "default_profile"
        )
    with pytest.raises(ValueError, match="identity"):
        parse_vscsi_profile(profile("none"), "other", 9, "default_profile")


# --- admission, render and observation ------------------------------------


@pytest.mark.asyncio
async def test_admitted_client_renders_once_and_verifies(target):
    b = target
    result = await invoke(b, apply_profile=True)
    record = native_record(b)
    assert '"virtual_scsi_adapters=2/client/7//20/0"' in record
    expected = {"AIX/Linux": "aixlinux", "OS400": "os400"}[b.environment]
    assert f"lpar_env={expected}" in record
    assert b.events.index("identity:" + VIOS) < b.events.index("create")
    step = vscsi_step(result)
    assert step.status == "ok"
    assert step.result == {
        "configuration": "verified",
        "plane": "saved-default-profile",
        "vios_server_adapters": "not-created",
        "vios_mappings": "not-created",
        "pairing": "unproven",
        "records": [{"slot": 2, "status": "verified"}],
    }
    assert result.workflow_completed and result.ownership_stamped is True
    steps = [s.step for s in result.steps]
    assert steps.index("virtual_scsi_adapters") < steps.index("apply_profile")
    b.hmc.create_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_name_selector_resolves_the_same_vios(target):
    b = target
    b.local_record = profile("2/client/7/vios-R1/20/1")
    adapter = Client(2, 20, True, remote_lpar_name="vios-R1")
    result = await invoke(b, adapters=(adapter,))
    assert '"virtual_scsi_adapters=2/client//vios-R1/20/1"' in native_record(b)
    assert vscsi_step(result).status == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "error", "match"),
    [
        (Client(2, 20, False, remote_lpar_id=5), ValueError, "not a VIOS"),
        (
            Client(2, 20, False, remote_lpar_name="example-lpar"),
            ValueError,
            "not a VIOS",
        ),
        (Client(2, 20, False, remote_lpar_name="new"), ValueError, "not contained"),
        (Client(2, 20, False, remote_lpar_id=99), ValueError, "not contained"),
        (Client(2, 64, False, remote_lpar_id=7), ValueError, "below the VIOS maximum"),
    ],
    ids=["lpar-id", "lpar-name", "pending", "unresolved", "remote-slot"],
)
async def test_admission_refuses_before_any_effect(target, adapter, error, match):
    with pytest.raises(error, match=match):
        await invoke(target, adapters=(adapter,))
    assert "create" not in target.events and "stamp" not in target.events


@pytest.mark.asyncio
async def test_foreign_owned_vios_refused_before_create(target):
    target.vios["Resource"]["Description"] = "[hmcpctl owner:other created:2026-11-13]"
    with pytest.raises(PermissionError):
        await invoke(target)
    assert "create" not in target.events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed", "status"),
    [
        ("2/client/7/vios-R1/21/0", "mismatch"),
        ("2/client/8/vios-R1/20/0", "mismatch"),
        ("2/client/7/other/20/0", "mismatch"),
        ("2/server/7/vios-R1/20/0", "mismatch"),
        ("2/client/7/vios-R1/20/1", "mismatch"),
        ("2/client///20/0", "mismatch"),
        ("none", "mismatch"),
        ("2/client/7/vios-R1/20/0,3/client/7/vios-R1/21/0", "verified"),
    ],
    ids=[
        "remote-slot",
        "remote-id",
        "remote-name",
        "role",
        "required",
        "no-selector",
        "missing",
        "extra",
    ],
)
async def test_readback_mismatch_blocks_apply(target, observed, status):
    b = target
    b.local_record = profile(observed)
    result = await invoke(b, apply_profile=True)
    step = vscsi_step(result)
    assert step.status == "error"
    assert step.result["configuration"] == "unverified"
    assert step.result["records"] == [{"slot": 2, "status": status}]
    assert step.result["pairing"] == "unproven"
    assert not result.workflow_completed
    assert "apply" not in b.events


@pytest.mark.asyncio
async def test_unavailable_readback_names_type_only(target):
    target.fault = "profile"
    result = await invoke(target, apply_profile=True)
    step = vscsi_step(result)
    assert step.status == "error"
    assert step.result["observation_error"] == "HMCError"
    assert PRIVATE not in repr(step.result)
    assert "apply" not in target.events


@pytest.mark.asyncio
async def test_controller_mode_reads_no_profile(target):
    target.controller = True
    if target.environment == "OS400":
        # The required console is initial-profile I/O, which controller mode refuses.
        with pytest.raises(ValueError, match="initial-profile I/O"):
            await invoke(target, apply_profile=False)
        assert "create" not in target.events
        return
    result = await invoke(target, apply_profile=False)
    step = vscsi_step(result)
    assert step.status == "error"
    assert step.result["plane"] == "current-unavailable"
    assert step.result["vios_mappings"] == "not-created"
    target.reader.assert_not_awaited()


@pytest.mark.asyncio
async def test_serial_and_vscsi_declared_together_both_verify(target):
    b = target
    serial = VirtualSerialAdapter(3, "server", False, 0)
    b.local_record = profile("2/client/7/vios-R1/20/0", serial="3/server/0////0")
    creation = core.LparCreation(
        "new",
        b.environment,
        LparResources(desired_memory=1024),
        max_virtual_slots=8,
        physical_io=b.console,
        virtual_serial_adapters=(serial,),
        virtual_scsi_adapters=(CLIENT,),
        apply_profile=True,
    )
    result = await workflows.create_lpar(b.hmc, SYSTEM, creation, LparPcieAssignments())
    record = native_record(b)
    assert '"virtual_serial_adapters=3/server/0////0"' in record
    assert '"virtual_scsi_adapters=2/client/7//20/0"' in record
    statuses = {s.step: s.status for s in result.steps}
    assert statuses["virtual_serial_adapters"] == "ok"
    assert statuses["virtual_scsi_adapters"] == "ok"
    assert result.workflow_completed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [{}, {"hardware_mem_encryption": True}, {"affinity_group_id": 1}],
    ids=["vscsi-only", "with-hardware-memory", "with-affinity"],
)
async def test_failed_create_propagates_without_retry(target, monkeypatch, options):
    if options:
        # Admission of the companion feature is not under test here.
        monkeypatch.setattr(
            "hmcpctl.operations.partition_creation._CreationFeatureCoordinator.admit_host_features",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "hmcpctl.operations.partition_creation.read_creation_authority",
            AsyncMock(return_value=(None, False)),
        )
    target.fault = "create"
    with pytest.raises(HMCCLIError):
        await invoke(target, **options)
    assert target.events.count("create") == 1
    assert "stamp" not in target.events and "apply" not in target.events
