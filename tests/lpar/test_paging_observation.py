"""Constructed schema-shaped evidence, never live paging-availability proof.

Current memory3043acbf, pool2f95cc34, device c61a1e10 and collection63704445
qualify the element nesting; profile5916499b qualifies optional configured P/S.
Native column relationships follow REDP4470-01 Examples4-4/4-6. Optional runtime
availability remains unproved. Fixtures intentionally include misleading free
devices and independent configured/current identities.
"""

import importlib.util
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.ssh import memory as native
from hmcpctl.xmlutil import ATOM_NS

SYSTEM = "11111111-1111-4111-8111-111111111111"
TARGET = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PRIMARY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECONDARY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
POOL = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def observation_module():
    name = "hmcpctl.operations.lpar.paging_observation"
    assert importlib.util.find_spec(name), "observation owner is missing"
    return __import__(name, fromlist=["observe_paging"])


def link(kind, uuid):
    return f'<link href="https://hmc.test/rest/api/uom/{kind}/{uuid}"/>'


def provider(name, number):
    return (
        f"<PartitionName>{name}</PartitionName><PartitionID>{number}</PartitionID>"
        "<PartitionType>Virtual IO Server</PartitionType>"
        "<PartitionMemoryConfiguration><SharedMemoryEnabled>false</SharedMemoryEnabled>"
        "</PartitionMemoryConfiguration>"
    )


def entry(kind, uuid, content):
    return (
        f'<entry xmlns="{ATOM_NS}"><id>urn:uuid:{uuid}</id><content>'
        f'<{kind} xmlns="{UOM_NS}">{content}</{kind}></content></entry>'
    )


def feed(*entries):
    return f'<feed xmlns="{ATOM_NS}">' + "".join(entries) + "</feed>"


@pytest.fixture
def paging_boundary(monkeypatch):
    memory = (
        "<SharedMemoryEnabled>true</SharedMemoryEnabled>"
        f"<PrimaryPagingServicePartition>{link('VirtualIOServer', PRIMARY)}</PrimaryPagingServicePartition>"
        f"<SecondaryPagingServicePartition>{link('VirtualIOServer', SECONDARY)}</SecondaryPagingServicePartition>"
        f"<CurrentPagingServicePartition>{link('VirtualIOServer', SECONDARY)}</CurrentPagingServicePartition>"
    )
    target = (
        "<PartitionName>consumer</PartitionName><PartitionID>9</PartitionID>"
        "<PartitionType>AIX/Linux</PartitionType><PartitionState>running</PartitionState>"
        f"<PartitionMemoryConfiguration>{memory}</PartitionMemoryConfiguration>"
    )
    device = (
        f"<AssociatedLogicalPartition>{link('LogicalPartition', TARGET)}</AssociatedLogicalPartition>"
        "<DeviceName>disk-a</DeviceName><RedundantDeviceName>disk-b</RedundantDeviceName>"
        "<IsRedundant>true</IsRedundant><DeviceState>Unknown</DeviceState>"
        "<UniqueDeviceID>device-one</UniqueDeviceID>"
    )
    pool = (
        "<PoolID>0</PoolID>"
        f"<PagingServicePartitionOne>{link('VirtualIOServer', PRIMARY)}</PagingServicePartitionOne>"
        f"<PagingServicePartitionTwo>{link('VirtualIOServer', SECONDARY)}</PagingServicePartitionTwo>"
        f"<PagingDevices><ReservedStorageDevice>{device}</ReservedStorageDevice></PagingDevices>"
        "<FreeRedundantMemoryDevices><ReservedStorageDevice><IsRedundant>true</IsRedundant>"
        "</ReservedStorageDevice></FreeRedundantMemoryDevices>"
    )
    rows = {
        "ManagedSystem": entry(
            "ManagedSystem",
            SYSTEM,
            "<SystemName>system</SystemName>"
            "<AssociatedSystemCapabilities><ActiveMemorySharingCapable>true</ActiveMemorySharingCapable>"
            "</AssociatedSystemCapabilities>",
        ),
        "LogicalPartition": entry("LogicalPartition", TARGET, target),
        "VirtualIOServer-a": entry("VirtualIOServer", PRIMARY, provider("paging-a", 7)),
        "VirtualIOServer-b": entry(
            "VirtualIOServer", SECONDARY, provider("paging-b", 8)
        ),
        "SharedMemoryPool": entry("SharedMemoryPool", POOL, pool),
    }

    async def get(path, kind, **kwargs):
        if path.endswith("/LogicalPartition"):
            return feed(rows["LogicalPartition"])
        if path.endswith("/VirtualIOServer"):
            return feed(rows["VirtualIOServer-a"], rows["VirtualIOServer-b"])
        if path.endswith("/SharedMemoryPool"):
            return feed(rows["SharedMemoryPool"])
        if path.lower().endswith(PRIMARY):
            return rows["VirtualIOServer-a"]
        if path.lower().endswith(SECONDARY):
            return rows["VirtualIOServer-b"]
        return rows[kind]

    hmc = SimpleNamespace(
        config=HMCConfig.from_mapping({"host": "hmc.test"}),
        _get=AsyncMock(side_effect=get),
    )
    current = AsyncMock(
        return_value="lpar_name=consumer,lpar_id=9,curr_paging_vios_name=paging-b"
    )
    devices = AsyncMock(
        return_value="lpar_id=9,lpar_name=consumer,is_redundant=1,paging_vios_id=7,"
        "paging_vios_name=paging-a,redundant_paging_vios_id=8,redundant_paging_vios_name=paging-b,"
        "device_name=disk-a,redundant_device_name=disk-b,state=Unknown,redundant_state=Error"
    )
    monkeypatch.setattr(native, "read_lpar_paging_memory", current, raising=False)
    monkeypatch.setattr(native, "read_lpar_paging_devices", devices, raising=False)
    return SimpleNamespace(
        hmc=hmc, rows=rows, current=current, devices=devices, original=deepcopy(rows)
    )


async def observe(boundary):
    mod = observation_module()
    target = mod.PartitionIdentity(TARGET, "consumer", 9, "LogicalPartition")
    return await mod.observe_paging(boundary.hmc, SYSTEM, "system", target)


@pytest.mark.asyncio
async def test_current_config_pair_and_current_secondary(paging_boundary):
    result = await observe(paging_boundary)
    assert result.unavailable == result.invalid == ()
    assert result.configured.plane == "current-configuration"
    assert result.configured.primary.uuid == PRIMARY
    assert result.current_uom.uuid == result.current_native.uuid == SECONDARY
    assert result.paths == ((PRIMARY, "disk-a"), (SECONDARY, "disk-b"))
    assert result.pool_id == 0 and result.unique_device_id == "device-one"
    assert result.uom_redundant is result.native_redundant is True
    assert ("native.state", "Unknown") in result.states
    paging_boundary.current.assert_awaited_once()
    paging_boundary.devices.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "foreign-association",
        "two-assignments",
        "missing-association",
        "not-redundant",
        "free-only",
        "foreign-provider",
        "duplicate-config",
        "foreign-namespace",
        "partial-pair",
    ],
)
async def test_required_topology_fails_closed(paging_boundary, fault):
    b = paging_boundary
    if fault == "foreign-association":
        b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace(TARGET, SYSTEM)
    elif fault == "two-assignments":
        b.devices.return_value += "\n" + b.devices.return_value
    elif fault == "missing-association":
        b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace(
            "AssociatedLogicalPartition", "UnrelatedPartition"
        )
    elif fault == "not-redundant":
        b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace(
            "<IsRedundant>true", "<IsRedundant>false"
        )
    elif fault == "free-only":
        b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace(
            "PagingDevices", "FreeMemoryDevicesFromPagingServicePartitionOne"
        )
    elif fault == "foreign-provider":
        b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace(
            "https://hmc.test", "https://foreign.test"
        )
    elif fault == "duplicate-config":
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            "<SharedMemoryEnabled>true</SharedMemoryEnabled>",
            "<SharedMemoryEnabled>true</SharedMemoryEnabled>" * 2,
        )
    elif fault == "foreign-namespace":
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            "<SharedMemoryEnabled>", '<SharedMemoryEnabled xmlns="urn:foreign">'
        )
    else:
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            "SecondaryPagingServicePartition", "IgnoredSecondary"
        )
    result = await observe(b)
    assert result.unavailable or result.invalid
    b.current.assert_awaited_once()
    b.devices.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("addition", [",is_redundant=1", ",bare-fragment"])
async def test_duplicate_critical_keys_and_bare_fragments(paging_boundary, addition):
    paging_boundary.devices.return_value += addition
    result = await observe(paging_boundary)
    assert result.unavailable


@pytest.mark.asyncio
async def test_duplicate_local_names_and_uuid_case(paging_boundary):
    b = paging_boundary
    b.devices.return_value = b.devices.return_value.replace("disk-b", "disk-a")
    b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace("disk-b", "disk-a")
    for key in b.rows:
        for uuid in [PRIMARY, SECONDARY, TARGET, POOL]:
            b.rows[key] = b.rows[key].replace(uuid, uuid.upper())
    result = await observe(b)
    assert result.unavailable == result.invalid == ()
    assert result.paths == ((PRIMARY, "disk-a"), (SECONDARY, "disk-a"))


@pytest.mark.asyncio
async def test_current_readers_disagree(paging_boundary):
    paging_boundary.current.return_value = (
        "lpar_name=consumer,curr_paging_vios_name=paging-a"
    )
    result = await observe(paging_boundary)
    assert (
        result.current_uom.uuid == SECONDARY and result.current_native.uuid == PRIMARY
    )
    assert result.invalid


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["AIX/Linux", "OS400", "Virtual IO Server"])
async def test_documented_contexts(paging_boundary, environment):
    b = paging_boundary
    b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
        "AIX/Linux", environment
    )
    result = await observe(b)
    if environment == "Virtual IO Server":
        assert result.invalid or result.unavailable  # wrong kind is not a VIOS identity
    else:
        assert result.unavailable == result.invalid == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "foreign-parent",
        "foreign-memory-namespace",
        "duplicate-pair",
        "mixed-case",
    ],
)
async def test_associated_profile_binding(paging_boundary, fault):
    b = paging_boundary
    profile_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    memory = (
        b.rows["LogicalPartition"]
        .split("<PartitionMemoryConfiguration>")[1]
        .split("</PartitionMemoryConfiguration>")[0]
    )
    for role in ["Primary", "Secondary"]:
        value = memory.split(f"<{role}PagingServicePartition>")[1].split(
            f"</{role}PagingServicePartition>"
        )[0]
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            f"<{role}PagingServicePartition>{value}</{role}PagingServicePartition>", ""
        )
    association = f"<AssociatedPartitionProfile>{link('LogicalPartition', TARGET).replace('/' + TARGET, '/' + TARGET + '/LogicalPartitionProfile/' + profile_id)}</AssociatedPartitionProfile>"
    b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
        "</LogicalPartition>", association + "</LogicalPartition>"
    )
    content = (
        "<ProfileName>associated</ProfileName><SettingID>profile-setting</SettingID>"
        f"<AssociatedPartition>{link('LogicalPartition', TARGET)}</AssociatedPartition>"
        "<Metadata><Atom><AtomID>profile-atom</AtomID><AtomCreated>timestamp</AtomCreated></Atom></Metadata>"
        f"<ProfileMemory>{memory}</ProfileMemory>"
    )
    if fault == "foreign-parent":
        content = content.replace(TARGET, SYSTEM)
    elif fault == "foreign-memory-namespace":
        content = content.replace(
            "<PrimaryPagingServicePartition>",
            '<PrimaryPagingServicePartition xmlns="urn:foreign">',
        )
    elif fault == "duplicate-pair":
        content = content.replace(
            "<PrimaryPagingServicePartition>",
            "<PrimaryPagingServicePartition/><PrimaryPagingServicePartition>",
        )
    if fault == "mixed-case":
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            TARGET, TARGET.upper()
        )
        content = content.replace(TARGET, TARGET.upper())
    response = SimpleNamespace(
        status_code=200,
        text=entry(
            "LogicalPartitionProfile",
            profile_id.upper() if fault == "mixed-case" else profile_id,
            content,
        ),
        headers={"ETag": "profile-etag"},
    )
    b.hmc._request_with_uuid_path_arguments = AsyncMock(return_value=response)
    result = await observe(b)
    if fault and fault != "mixed-case":
        assert result.unavailable
    else:
        assert result.unavailable == result.invalid == ()
        assert result.configured.plane == "associated-profile"
        assert result.configured.profile_uuid == profile_id
        assert result.configured.profile_name == "associated"
        assert result.configured.etag == "profile-etag"
        assert result.current_uom.uuid == SECONDARY
    b.hmc._request_with_uuid_path_arguments.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_not_fallback(paging_boundary):
    b = paging_boundary
    for role in ["Primary", "Secondary"]:
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            role + "PagingServicePartition", "Unrelated" + role
        )
    b.hmc._request_with_uuid_path_arguments = AsyncMock()
    result = await observe(b)
    assert result.unavailable and result.configured is None
    b.hmc._request_with_uuid_path_arguments.assert_not_called()


@pytest.mark.asyncio
async def test_vios_consumer_kind_and_association(paging_boundary):
    b = paging_boundary
    target = (
        b.rows["LogicalPartition"]
        .replace("LogicalPartition", "VirtualIOServer")
        .replace("AIX/Linux", "Virtual IO Server")
    )
    old = b.hmc._get.side_effect

    async def get(path, kind, **kwargs):
        if path.endswith("/LogicalPartition"):
            return feed()
        if path.endswith("/VirtualIOServer"):
            return feed(
                b.rows["VirtualIOServer-a"], b.rows["VirtualIOServer-b"], target
            )
        if path.lower().endswith(TARGET):
            return target
        return await old(path, kind, **kwargs)

    b.hmc._get.side_effect = get
    b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace(
        "/LogicalPartition/", "/VirtualIOServer/"
    )
    mod = observation_module()
    result = await mod.observe_paging(
        b.hmc,
        SYSTEM,
        "system",
        mod.PartitionIdentity(TARGET, "consumer", 9, "VirtualIOServer"),
    )
    assert result.unavailable == result.invalid == ()
    assert result.target.kind == "VirtualIOServer" and result.shared is True


@pytest.mark.asyncio
async def test_unavailable_current_keeps_independent_native(paging_boundary):
    b = paging_boundary
    b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
        "CurrentPagingServicePartition", "UnrelatedCurrent"
    )
    result = await observe(b)
    assert result.unavailable and result.current_uom is None
    assert result.current_native.uuid == SECONDARY
    assert result.paths == ((PRIMARY, "disk-a"), (SECONDARY, "disk-b"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionState", "not activated"),
        ("SharedMemoryEnabled", "false"),
        ("IsOperationInProgress", "true"),
    ],
)
async def test_valid_negative_eligibility_is_retained(paging_boundary, field, value):
    b = paging_boundary
    if field == "IsOperationInProgress":
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            "</LogicalPartition>", f"<{field}>{value}</{field}></LogicalPartition>"
        )
    else:
        original = "running" if field == "PartitionState" else "true"
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            f"<{field}>{original}</{field}>", f"<{field}>{value}</{field}>"
        )
    result = await observe(b)
    assert result.invalid
    assert result.current_native.uuid == SECONDARY


@pytest.mark.asyncio
async def test_optional_states_do_not_establish_readiness(paging_boundary):
    b = paging_boundary
    b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace(
        "<DeviceState>Unknown</DeviceState>", ""
    )
    b.devices.return_value = b.devices.return_value.replace(
        ",state=Unknown,redundant_state=Error", ""
    )
    result = await observe(b)
    assert result.unavailable == result.invalid == ()
    assert all(state is None for _, state in result.states)


@pytest.mark.asyncio
async def test_duplicate_uom_assignments_refuse(paging_boundary):
    b = paging_boundary
    device = (
        b.rows["SharedMemoryPool"]
        .split("<PagingDevices>")[1]
        .split("</PagingDevices>")[0]
    )
    b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace(
        "<PagingDevices>" + device, "<PagingDevices>" + device * 2
    )
    assert (await observe(b)).unavailable


@pytest.mark.asyncio
@pytest.mark.parametrize("plane", ["configured", "current", "native-paths"])
async def test_positive_different_identity_is_drift_not_missing(paging_boundary, plane):
    b = paging_boundary
    third = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    extra = entry("VirtualIOServer", third, provider("paging-c", 10))
    old = b.hmc._get.side_effect

    async def get(path, kind, **kwargs):
        body = await old(path, kind, **kwargs)
        return (
            body.replace("</feed>", extra + "</feed>")
            if path.endswith("/VirtualIOServer")
            else body
        )

    b.hmc._get.side_effect = get
    if plane == "configured":
        before = f"<PrimaryPagingServicePartition>{link('VirtualIOServer', PRIMARY)}</PrimaryPagingServicePartition>"
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            before, before.replace(PRIMARY, third)
        )
    elif plane == "current":
        before = f"<CurrentPagingServicePartition>{link('VirtualIOServer', SECONDARY)}</CurrentPagingServicePartition>"
        b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
            before, before.replace(SECONDARY, third)
        )
        b.current.return_value = b.current.return_value.replace("paging-b", "paging-c")
    else:
        b.devices.return_value = b.devices.return_value.replace(
            "paging_vios_id=7", "paging_vios_id=10"
        ).replace("paging_vios_name=paging-a", "paging_vios_name=paging-c")
    result = await observe(b)
    assert result.invalid and not result.unavailable
    if plane == "configured":
        assert result.configured.primary.uuid == third
    elif plane == "current":
        assert result.current_uom.uuid == result.current_native.uuid == third
    else:
        assert (third, "disk-a") in result.paths


@pytest.mark.asyncio
async def test_uncontained_consumer_is_never_fetched(paging_boundary):
    mod = observation_module()
    foreign = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    target = mod.PartitionIdentity(foreign, "consumer", 9, "LogicalPartition")
    result = await mod.observe_paging(paging_boundary.hmc, SYSTEM, "system", target)
    assert "consumer-not-contained" in result.invalid
    assert not any(
        call.args[0].lower().endswith(foreign)
        for call in paging_boundary.hmc._get.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["pool", "provider"])
async def test_missing_pool_or_provider_detail_retains_independent_facts(
    paging_boundary, missing
):
    from hmcpctl.errors import HMCError

    b = paging_boundary
    get = b.hmc._get.side_effect

    async def unavailable(path, *args, **kwargs):
        if (
            path.endswith("/SharedMemoryPool")
            if missing == "pool"
            else path.lower().endswith(PRIMARY)
        ):
            raise HMCError("read unavailable")
        return await get(path, *args, **kwargs)

    b.hmc._get.side_effect = unavailable
    result = await observe(b)
    assert result.unavailable and result.invalid == ()
    assert result.current_native.uuid == result.current_uom.uuid == SECONDARY
    assert result.configured.primary.uuid == PRIMARY and len(result.paths) == 2
    assert result.native_redundant is True
    if missing == "provider":
        assert result.pool_uuid == POOL and result.pool_id == 0
        assert result.unique_device_id == "device-one" and result.uom_redundant is True
        assert len(result.providers) == 2 and result.provider_dedicated == (None, True)
    else:
        assert result.pool_uuid is None and result.uom_redundant is None
