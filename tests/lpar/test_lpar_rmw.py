"""LPAR modify writes: whole-partition read-modify-write (#1057).

V10R3 rejects a sparse ``LogicalPartition`` POST by schema validation, so rename, DLPAR
and ``lpars modify`` read the whole partition in the ``Advanced`` group, change only the
mapped elements' text, and POST it back under ``If-Match``. The fixture follows the live
V10R3 read's nesting with synthetic values.
"""

import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import make_config, mock_system_summaries
from defusedxml import ElementTree as DET

from hmcpctl.client.core import HMCClient
from hmcpctl.documents import lpar as lpar_documents
from hmcpctl.documents.lpar import LparResources, partition_updates
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.assignments import LparPcieAssignments
from hmcpctl.operations.lpar.core import rename_lpar
from hmcpctl.operations.lpar.dlpar import (
    modify_lpar,
    set_lpar_memory,
    set_lpar_processors,
)
from hmcpctl.xmlutil import localname

LPAR = "aaaa0000-0000-0000-0000-000000000001"
LPAR_PATH = f"/rest/api/uom/LogicalPartition/{LPAR}"
ETAG = "-91212356"
UOM = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
PPC = "PartitionProcessorConfiguration"


def _el(name: str, value: str, kb: str = "CUD") -> str:
    return f'<{name} kb="{kb}" kxe="false">{value}</{name}>'


def _processors(dedicated: bool, has_dedicated: bool) -> str:
    config = (
        f"""<DedicatedProcessorConfiguration kb="CUD" kxe="false" schemaVersion="V1_0">
            <Metadata><Atom/></Metadata>
            {_el("DesiredProcessors", "2")}{_el("MaximumProcessors", "4")}
            {_el("MinimumProcessors", "1")}
        </DedicatedProcessorConfiguration>"""
        if dedicated
        else f"""<SharedProcessorConfiguration kb="CUD" kxe="false" schemaVersion="V1_0">
            <Metadata><Atom/></Metadata>
            {_el("DesiredProcessingUnits", "0.3")}{_el("DesiredVirtualProcessors", "3")}
            {_el("MaximumProcessingUnits", "2")}{_el("MaximumVirtualProcessors", "6")}
            {_el("MinimumProcessingUnits", "0.1")}{_el("MinimumVirtualProcessors", "1")}
            {_el("SharedProcessorPoolID", "0")}{_el("UncappedWeight", "128")}
        </SharedProcessorConfiguration>"""
    )
    flag = (
        _el("HasDedicatedProcessors", str(dedicated).lower()) if has_dedicated else ""
    )
    mode = "keep idle procs" if dedicated else "uncapped"
    return f"""<{PPC} kb="CUD" kxe="false" schemaVersion="V1_0">
        <Metadata><Atom/></Metadata>
        {flag}{_el("CurrentHasDedicatedProcessors", str(dedicated).lower(), "ROR")}
        {config}{_el("SharingMode", mode)}
        {_el("CurrentSharingMode", mode, "ROR")}
    </{PPC}>"""


def _entry(
    *, dedicated: bool = False, has_dedicated: bool = True, name: str = "lpar-a"
) -> str:
    return f"""<entry xmlns="http://www.w3.org/2005/Atom">
    <id>{LPAR}</id>
    <title>LogicalPartition</title>
    <content type="application/vnd.ibm.powervm.uom+xml; type=LogicalPartition">
        <LogicalPartition:LogicalPartition xmlns:LogicalPartition="{UOM}" xmlns="{UOM}" schemaVersion="V1_0">
    <Metadata><Atom><AtomID>{LPAR}</AtomID></Atom></Metadata>
    {_el("AllowPerformanceDataCollection", "false")}
    <PartitionMemoryConfiguration kb="CUD" kxe="false" schemaVersion="V1_0">
        <Metadata><Atom/></Metadata>
        {_el("DesiredMemory", "3072")}{_el("MaximumMemory", "6144")}
        {_el("MinimumMemory", "1536")}{_el("CurrentMemory", "3072", "ROR")}
    </PartitionMemoryConfiguration>
    {_el("PartitionName", name, "CUR")}
    {_processors(dedicated, has_dedicated)}
    {_el("PartitionType", "AIX/Linux", "COD")}
</LogicalPartition:LogicalPartition>
    </content>
</entry>"""


# The live V10R3 read nests the memory figures (MiB) under this container.
_SYSTEM_ENTRY = f"""<entry xmlns="http://www.w3.org/2005/Atom">
    <content type="application/vnd.ibm.powervm.uom+xml; type=ManagedSystem">
        <ManagedSystem:ManagedSystem xmlns:ManagedSystem="{UOM}" xmlns="{UOM}">
            <AssociatedSystemMemoryConfiguration kb="CUD" kxe="false" schemaVersion="V1_0">
                {_el("ConfigurableSystemMemory", "131072", "ROR")}
            </AssociatedSystemMemoryConfiguration>
        </ManagedSystem:ManagedSystem>
    </content>
</entry>"""


def _routes(
    mock_hmc,
    *,
    etag: str | None = ETAG,
    entry: str | None = None,
    get_status: int = 200,
    post_status: int = 200,
):
    headers = {"ETag": etag} if etag else {}
    body = entry if entry is not None else _entry()
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM}").mock(
        return_value=httpx.Response(200, text=_SYSTEM_ENTRY)
    )
    mock_system_summaries(mock_hmc, (SYSTEM, "sys-a", "operating"))
    get = mock_hmc.get(LPAR_PATH, params={"group": "Advanced"}).mock(
        return_value=httpx.Response(get_status, text=body, headers=headers)
    )
    post = mock_hmc.post(LPAR_PATH, params={"group": "Advanced"}).mock(
        return_value=httpx.Response(post_status, text=body)
    )
    return get, post


def _lpar(xml: str | bytes) -> ET.Element:
    root = DET.fromstring(xml)
    if localname(root.tag) == "LogicalPartition":
        return root
    return next(el for el in root.iter() if localname(el.tag) == "LogicalPartition")


def _canonical(el: ET.Element) -> str:
    return ET.canonicalize(ET.tostring(el), strip_text=True)


def _find(lpar: ET.Element, path: str) -> ET.Element:
    found = lpar.find("/".join(f"{{{UOM}}}{part}" for part in path.split("/")))
    assert found is not None, path
    return found


def _expected(entry: str, changes: dict[str, str]) -> str:
    lpar = _lpar(entry)
    for path, text in changes.items():
        _find(lpar, path).text = text
    return _canonical(lpar)


async def _update(updates, subject: str = "the partition", lpar: str = LPAR):
    async with HMCClient(make_config()) as hmc:
        return await hmc.update_logical_partition(lpar, updates, subject)


# ------------------------------------------------------------------ #
# LparsMixin.update_logical_partition
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_update_posts_whole_partition_with_only_mapped_text(mock_hmc):
    get, post = _routes(mock_hmc)
    changes = {
        "PartitionName": "lpar-b",
        "PartitionMemoryConfiguration/DesiredMemory": "4096",
    }

    result = await _update(lambda _lpar: changes)

    assert result is not None
    assert get.call_count == 1 and post.call_count == 1
    assert "X-HMC-Schema-Version" not in get.calls.last.request.headers
    request = post.calls.last.request
    assert request.headers["If-Match"] == ETAG
    assert request.headers["Accept"] == "*/*"
    assert request.headers["Content-Type"] == (
        "application/vnd.ibm.powervm.uom+xml; type=LogicalPartition"
    )
    assert _canonical(_lpar(request.content)) == _expected(_entry(), changes)


@pytest.mark.asyncio
async def test_update_hands_the_read_partition_to_the_mapping(mock_hmc):
    _routes(mock_hmc)
    seen: list[str] = []

    await _update(
        lambda lpar: (
            seen.append(_find(lpar, "PartitionName").text or "")
            or {"PartitionName": "x"}
        )
    )

    assert seen == ["lpar-a"]


def _raise(_lpar):
    raise ValueError("mapping refused")


@pytest.mark.parametrize(
    ("kwargs", "updates", "error", "message"),
    [
        ({"etag": None}, {"PartitionName": "x"}, HMCError, "no ETag"),
        (
            {},
            {f"{PPC}/NoSuchElement": "x"},
            HMCError,
            f"has no {PPC}/NoSuchElement; refusing to write the partition",
        ),
        (
            {"entry": "<entry xmlns='http://www.w3.org/2005/Atom'/>"},
            {"PartitionName": "x"},
            HMCError,
            "no LogicalPartition",
        ),
        ({"entry": "<not-xml"}, {"PartitionName": "x"}, HMCError, "not valid XML"),
        ({"get_status": 500}, {"PartitionName": "x"}, HMCError, "failed"),
        ({}, {}, ValueError, "nothing to write for the partition"),
        ({}, _raise, ValueError, "mapping refused"),
    ],
)
@pytest.mark.asyncio
async def test_update_refuses_before_any_post(
    mock_hmc, kwargs, updates, error, message
):
    _, post = _routes(mock_hmc, **kwargs)

    with pytest.raises(error, match=message):
        await _update(updates if callable(updates) else lambda _lpar: updates)

    assert not post.called


@pytest.mark.asyncio
async def test_update_reports_412_as_nothing_written(mock_hmc):
    _routes(mock_hmc, post_status=412)

    with pytest.raises(HMCError, match="Nothing was written") as exc_info:
        await _update(lambda _lpar: {"PartitionName": "x"})

    assert exc_info.value.status_code == 412


@pytest.mark.asyncio
async def test_update_raises_on_a_rejected_post(mock_hmc):
    _routes(mock_hmc, post_status=400)

    with pytest.raises(HMCError, match="POST .* failed") as exc_info:
        await _update(lambda _lpar: {"PartitionName": "x"})

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_update_refuses_a_non_uuid_before_any_request(mock_hmc):
    with pytest.raises(ValueError):
        await _update(lambda _lpar: {"PartitionName": "x"}, lpar="../ManagedSystem")

    assert not any("LogicalPartition" in str(c.request.url) for c in mock_hmc.calls)


# ------------------------------------------------------------------ #
# partition_updates
# ------------------------------------------------------------------ #

SHARED = f"{PPC}/SharedProcessorConfiguration"
DEDICATED = f"{PPC}/DedicatedProcessorConfiguration"


def test_partition_updates_maps_a_name_and_memory():
    assert partition_updates(
        _lpar(_entry()),
        name="lpar-b",
        resources=LparResources(min_memory=1024, desired_memory=2048, max_memory=8192),
    ) == {
        "PartitionName": "lpar-b",
        "PartitionMemoryConfiguration/DesiredMemory": "2048",
        "PartitionMemoryConfiguration/MaximumMemory": "8192",
        "PartitionMemoryConfiguration/MinimumMemory": "1024",
    }


def test_partition_updates_maps_shared_processors_in_the_current_mode():
    assert partition_updates(
        _lpar(_entry()),
        resources=LparResources(
            min_procs=0.5,
            desired_procs=1.0,
            max_procs=2.25,
            min_vcpus=1,
            desired_vcpus=2,
            max_vcpus=4,
        ),
    ) == {
        f"{SHARED}/DesiredProcessingUnits": "1",
        f"{SHARED}/MaximumProcessingUnits": "2.25",
        f"{SHARED}/MinimumProcessingUnits": "0.5",
        f"{SHARED}/DesiredVirtualProcessors": "2",
        f"{SHARED}/MaximumVirtualProcessors": "4",
        f"{SHARED}/MinimumVirtualProcessors": "1",
        f"{PPC}/HasDedicatedProcessors": "false",
    }


def test_partition_updates_maps_dedicated_processors_in_the_current_mode():
    assert partition_updates(
        _lpar(_entry(dedicated=True)),
        resources=LparResources(
            desired_procs=3.0, max_procs=4, sharing_mode="keep_idle_procs"
        ),
    ) == {
        f"{DEDICATED}/DesiredProcessors": "3",
        f"{DEDICATED}/MaximumProcessors": "4",
        f"{PPC}/SharingMode": "keep idle procs",
        f"{PPC}/HasDedicatedProcessors": "true",
    }


@pytest.mark.parametrize(
    ("resources", "mode"),
    [
        (LparResources(uncapped=True), "uncapped"),
        (LparResources(uncapped=False), "capped"),
        (LparResources(sharing_mode="capped"), "capped"),
        (LparResources(uncapped=True, sharing_mode="capped"), "uncapped"),
    ],
)
def test_partition_updates_sets_the_shared_sharing_mode_without_a_weight(
    resources, mode
):
    updates = partition_updates(_lpar(_entry()), resources=resources)

    assert updates == {
        f"{PPC}/SharingMode": mode,
        f"{PPC}/HasDedicatedProcessors": "false",
    }


@pytest.mark.parametrize(
    ("mode", "rest"),
    [
        ("keep_idle_procs", "keep idle procs"),
        ("share_idle_procs", "sre idle proces"),
        ("share_idle_procs_active", "sre idle procs active"),
        ("share_idle_procs_always", "sre idle procs always"),
    ],
)
def test_partition_updates_writes_the_rest_spelling_of_a_dedicated_mode(mode, rest):
    """The REST schema refuses the CLI spelling with REST0001 (#1185)."""
    updates = partition_updates(
        _lpar(_entry(dedicated=True)), resources=LparResources(sharing_mode=mode)
    )

    assert updates[f"{PPC}/SharingMode"] == rest


def test_partition_updates_maps_nothing_for_dedicated_alone():
    assert (
        partition_updates(_lpar(_entry()), resources=LparResources(dedicated=False))
        == {}
    )


def test_partition_updates_refuses_missing_context_for_a_mode_only_request():
    with pytest.raises(ValueError, match="HasDedicatedProcessors"):
        partition_updates(
            _lpar(_entry(has_dedicated=False)), resources=LparResources(uncapped=False)
        )


@pytest.mark.parametrize(
    ("dedicated", "resources", "message"),
    [
        (False, LparResources(dedicated=True, desired_procs=2), "switch the partition"),
        (
            True,
            LparResources(dedicated=False, desired_procs=0.5),
            "switch the partition",
        ),
        (True, LparResources(desired_vcpus=2), "shared-processor partition"),
        (True, LparResources(uncapped=True), "shared-processor partition"),
        (
            True,
            LparResources(desired_procs=1.5),
            "DesiredProcessors=1.5 is not a whole",
        ),
        (True, LparResources(min_procs=0.5), "MinimumProcessors=0.5 is not a whole"),
        (True, LparResources(max_procs=float("inf")), "max_procs=inf must be a finite"),
        (
            False,
            LparResources(desired_procs=float("nan")),
            "desired_procs=nan must be a finite",
        ),
        (False, LparResources(desired_vcpus=-1), "desired_vcpus=-1 must be a finite"),
        (False, LparResources(sharing_mode="bogus"), "sharing_mode must be one of"),  # type: ignore[arg-type]
    ],
)
def test_partition_updates_refuses_what_the_read_cannot_carry(
    dedicated, resources, message
):
    with pytest.raises(ValueError, match=message):
        partition_updates(_lpar(_entry(dedicated=dedicated)), resources=resources)


@pytest.mark.asyncio
async def test_a_mode_only_request_on_a_read_without_the_mode_is_refused(mock_hmc):
    _, post = _routes(mock_hmc, entry=_entry(has_dedicated=False))

    with pytest.raises(ValueError, match="HasDedicatedProcessors"):
        await _update(
            lambda lpar: partition_updates(lpar, resources=LparResources(uncapped=True))
        )

    assert not post.called


@pytest.mark.asyncio
async def test_a_processor_change_posts_only_its_fields(mock_hmc):
    _, post = _routes(mock_hmc)
    resources = LparResources(desired_procs=0.4, desired_vcpus=4)

    await _update(lambda lpar: partition_updates(lpar, resources=resources))

    assert _canonical(_lpar(post.calls.last.request.content)) == _expected(
        _entry(),
        {
            f"{SHARED}/DesiredProcessingUnits": "0.4",
            f"{SHARED}/DesiredVirtualProcessors": "4",
        },
    )


# ------------------------------------------------------------------ #
# Operations
# ------------------------------------------------------------------ #

SYSTEM = "cccc0000-0000-0000-0000-000000000001"


@pytest.fixture
def authorized(monkeypatch):
    """Authorization is covered elsewhere; here it resolves straight to LPAR."""
    guard = AsyncMock(return_value=LPAR)
    for module in ("core", "dlpar"):
        monkeypatch.setattr(
            f"hmcpctl.operations.lpar.{module}.resolve_and_authorize_lpar_mutation",
            guard,
        )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.dlpar.prevalidate_lpar_pcie_assignments", AsyncMock()
    )
    return guard


async def _rename(name: str):
    async with HMCClient(make_config()) as hmc:
        return await rename_lpar(hmc, LPAR, name, system_name_or_uuid=SYSTEM)


async def _modify(resources: LparResources, new_name: str | None = None):
    async with HMCClient(make_config()) as hmc:
        return await modify_lpar(
            hmc,
            LPAR,
            resources,
            LparPcieAssignments(),
            new_name=new_name,
            system_name_or_uuid=SYSTEM,
        )


def _posted(post) -> list[bytes]:
    return [call.request.content for call in post.calls]


@pytest.mark.asyncio
async def test_rename_posts_only_the_new_name_and_no_create_only_field(
    mock_hmc, authorized
):
    get, post = _routes(mock_hmc)

    lpar_uuid, _ = await _rename("lpar-b")

    assert lpar_uuid == LPAR and get.call_count == 1 and post.call_count == 1
    assert post.calls.last.request.headers["If-Match"] == ETAG
    assert _canonical(_lpar(post.calls.last.request.content)) == _expected(
        _entry(), {"PartitionName": "lpar-b"}
    )


@pytest.mark.asyncio
async def test_modify_writes_rename_then_resources_as_two_read_modify_writes(
    mock_hmc, authorized
):
    get, post = _routes(mock_hmc)

    result = await _modify(LparResources(desired_memory=4096), new_name="lpar-b")

    assert [(step.step, step.status) for step in result.steps] == [
        ("rename", "ok"),
        ("resources", "ok"),
    ]
    assert get.call_count == 2
    rename, resources = (_canonical(_lpar(body)) for body in _posted(post))
    assert rename == _expected(_entry(), {"PartitionName": "lpar-b"})
    assert resources == _expected(
        _entry(), {"PartitionMemoryConfiguration/DesiredMemory": "4096"}
    )


@pytest.mark.asyncio
async def test_modify_refuses_desired_memory_above_configurable_before_any_write(
    mock_hmc, authorized
):
    get, post = _routes(mock_hmc)

    with pytest.raises(ValueError, match=r"8388608 MiB.*131072 MiB"):
        await _modify(LparResources(desired_memory=8388608), new_name="lpar-b")

    assert not get.called
    assert not post.called


@pytest.mark.asyncio
async def test_modify_reports_the_rename_when_the_resources_leg_is_refused(
    mock_hmc, authorized
):
    _, post = _routes(mock_hmc)

    result = await _modify(
        LparResources(dedicated=True, desired_procs=2), new_name="lpar-b"
    )

    assert [(step.step, step.status) for step in result.steps] == [
        ("rename", "ok"),
        ("resources", "error"),
    ]
    assert "switch the partition" in result.warnings[0]
    assert post.call_count == 1


@pytest.mark.asyncio
async def test_a_refused_resources_leg_without_a_rename_raises(mock_hmc, authorized):
    _, post = _routes(mock_hmc)

    with pytest.raises(ValueError, match="switch the partition"):
        await _modify(LparResources(dedicated=True, desired_procs=2))

    assert not post.called


@pytest.mark.parametrize(
    "operation", [_rename, lambda name: _modify(LparResources(), name)]
)
@pytest.mark.asyncio
async def test_a_name_xml_cannot_carry_is_refused_before_any_request(
    mock_hmc, authorized, operation
):
    get, post = _routes(mock_hmc)

    with pytest.raises(ValueError, match="U\\+0001"):
        await operation("lpar\x01b")

    authorized.assert_not_awaited()
    assert not get.called and not post.called


@pytest.mark.parametrize(
    ("operation", "resources", "kind"),
    [
        (set_lpar_processors, LparResources(desired_memory=1024), "processor"),
        (set_lpar_memory, LparResources(desired_procs=1.0), "memory"),
    ],
)
@pytest.mark.asyncio
async def test_dlpar_refuses_no_fields_before_any_request(
    mock_hmc, authorized, operation, resources, kind
):
    get, _ = _routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match=f"at least one {kind} field"):
            await operation(hmc, LPAR, resources, system_name_or_uuid=SYSTEM)

    authorized.assert_not_awaited()
    assert not get.called


@pytest.mark.asyncio
async def test_dlpar_memory_writes_only_memory_fields(mock_hmc, authorized):
    _, post = _routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        await set_lpar_memory(
            hmc,
            LPAR,
            LparResources(max_memory=8192, desired_procs=1.0),
            system_name_or_uuid=SYSTEM,
        )

    body = post.calls.last.request.content
    assert b"PartitionType" in body  # the partition's own read-back value, unchanged
    assert _canonical(_lpar(body)) == _expected(
        _entry(), {"PartitionMemoryConfiguration/MaximumMemory": "8192"}
    )


@pytest.mark.asyncio
async def test_dlpar_processors_write_only_processor_fields(mock_hmc, authorized):
    _, post = _routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        await set_lpar_processors(
            hmc,
            LPAR,
            LparResources(desired_procs=0.4, desired_memory=8192),
            system_name_or_uuid=SYSTEM,
        )

    assert _canonical(_lpar(post.calls.last.request.content)) == _expected(
        _entry(), {f"{SHARED}/DesiredProcessingUnits": "0.4"}
    )


@pytest.mark.parametrize("value", [-1, 256, True, False, 1.0, "0"])
def test_sharing_input_rejects_invalid_weight(value):
    with pytest.raises(ValueError, match="uncapped_weight"):
        partition_updates(
            _lpar(_entry()), resources=LparResources(uncapped_weight=value)
        )


@pytest.mark.parametrize("value", [0, 1, "true"])
def test_sharing_input_rejects_non_boolean_alias(value):
    with pytest.raises(ValueError, match="uncapped.*boolean"):
        partition_updates(_lpar(_entry()), resources=LparResources(uncapped=value))


@pytest.mark.parametrize(
    ("mode", "alias"),
    [("capped", True), ("uncapped", False), ("keep_idle_procs", True)],
)
def test_sharing_input_refuses_weight_bearing_contradictions(mode, alias):
    with pytest.raises(ValueError, match="sharing_mode.*uncapped|shared"):
        partition_updates(
            _lpar(_entry()),
            resources=LparResources(
                sharing_mode=mode, uncapped=alias, uncapped_weight=0
            ),
        )


@pytest.mark.parametrize("weight", [None, 0, 255])
def test_sharing_zero_and_bounds_preserve_presence(weight):
    resources = LparResources(uncapped_weight=weight)
    updates = partition_updates(_lpar(_entry()), resources=resources)
    if weight is None:
        assert updates == {}
    else:
        assert updates == {
            f"{SHARED}/UncappedWeight": str(weight),
            f"{PPC}/HasDedicatedProcessors": "false",
        }
    assert callable(getattr(lpar_documents, "has_sharing_change", None))
    assert lpar_documents.has_sharing_change(resources) is (weight is not None)


@pytest.mark.parametrize("weight", [0, 255])
@pytest.mark.parametrize("procs", [None, 0.5])
def test_sharing_zero_and_bounds_reject_create_before_empty_block(weight, procs):
    resources = LparResources(uncapped_weight=weight, desired_procs=procs)
    with pytest.raises(ValueError, match="uncapped_weight.*update"):
        lpar_documents.build_lpar_document("example", resources=resources)
    with pytest.raises(ValueError, match="uncapped_weight.*update"):
        lpar_documents.build_vios_document("example", resources=resources)


@pytest.mark.parametrize(
    ("alias", "mode", "expected"),
    [
        (True, "capped", "uncapped"),
        (False, "uncapped", "uncapped"),
        (False, None, "capped"),
        (None, "capped", "capped"),
        (None, None, None),
    ],
)
def test_sharing_exact_legacy_alias(alias, mode, expected):
    resources = LparResources(uncapped=alias, sharing_mode=mode)
    updates = partition_updates(_lpar(_entry()), resources=resources)
    assert updates.get(f"{PPC}/SharingMode") == expected


def _context_lpar(**values):
    lpar = _lpar(_entry())
    for name, value in values.items():
        node = _find(lpar, f"{PPC}/{name}")
        if value is None:
            _find(lpar, PPC).remove(node)
        else:
            node.text = value
    return lpar


@pytest.mark.parametrize(
    "field", ["HasDedicatedProcessors", "CurrentHasDedicatedProcessors"]
)
@pytest.mark.parametrize("value", [None, "TRUE", "1", "", "true"])
def test_sharing_context_refuses_unresolved_flags(field, value):
    with pytest.raises(ValueError, match="dedicated|DedicatedProcessors"):
        partition_updates(
            _context_lpar(**{field: value}), resources=LparResources(uncapped=True)
        )


@pytest.mark.parametrize("field", ["SharingMode", "CurrentSharingMode"])
@pytest.mark.parametrize("value", [None, "Unknown", "keep idle procs", "capped"])
def test_sharing_context_refuses_unresolved_modes(field, value):
    with pytest.raises(ValueError, match="mode|SharingMode"):
        partition_updates(
            _context_lpar(**{field: value}), resources=LparResources(uncapped=True)
        )


@pytest.mark.parametrize(
    "mode",
    [
        "keep_idle_procs",
        "share_idle_procs",
        "share_idle_procs_active",
        "share_idle_procs_always",
    ],
)
def test_sharing_dedicated_modes_reject_shared_context(mode):
    with pytest.raises(ValueError, match="shared"):
        partition_updates(_lpar(_entry()), resources=LparResources(sharing_mode=mode))


@pytest.mark.parametrize("mode", ["capped", "uncapped"])
def test_sharing_dedicated_modes_reject_shared_request(mode):
    with pytest.raises(ValueError, match="dedicated|shared-processor"):
        partition_updates(
            _lpar(_entry(dedicated=True)), resources=LparResources(sharing_mode=mode)
        )


@pytest.mark.parametrize("weight", [0, 255])
def test_sharing_dedicated_modes_refuse_weight(weight):
    with pytest.raises(ValueError, match="dedicated|shared-processor"):
        partition_updates(
            _lpar(_entry(dedicated=True)),
            resources=LparResources(uncapped_weight=weight),
        )


def test_sharing_context_retains_quantity_only_fallback():
    updates = partition_updates(
        _lpar(_entry(has_dedicated=False)), resources=LparResources(desired_procs=0.5)
    )
    assert updates[f"{SHARED}/DesiredProcessingUnits"] == "0.5"


@pytest.mark.parametrize("weight", [0, 255])
@pytest.mark.parametrize("mode", [None, "capped"])
def test_existing_capped_weight_refusal(weight, mode):
    lpar = _context_lpar(SharingMode="capped", CurrentSharingMode="capped")
    with pytest.raises(ValueError, match="uncapped"):
        partition_updates(
            lpar, resources=LparResources(sharing_mode=mode, uncapped_weight=weight)
        )


@pytest.mark.parametrize("weight", [0, 255])
def test_sharing_context_explicit_uncapping_and_weight(weight):
    lpar = _context_lpar(SharingMode="capped", CurrentSharingMode="capped")
    updates = partition_updates(
        lpar, resources=LparResources(uncapped=True, uncapped_weight=weight)
    )
    assert updates == {
        f"{PPC}/SharingMode": "uncapped",
        f"{SHARED}/UncappedWeight": str(weight),
        f"{PPC}/HasDedicatedProcessors": "false",
    }


@pytest.mark.parametrize("mode", ["capped", "uncapped"])
@pytest.mark.parametrize("previous", [None, "0", "128", "255"])
def test_mode_only_transition_does_not_synthesize_weight(mode, previous):
    lpar = _lpar(_entry())
    node = _find(lpar, f"{SHARED}/UncappedWeight")
    if previous is None:
        _find(lpar, SHARED).remove(node)
    else:
        node.text = previous
    updates = partition_updates(lpar, resources=LparResources(sharing_mode=mode))
    assert updates == {
        f"{PPC}/SharingMode": mode,
        f"{PPC}/HasDedicatedProcessors": "false",
    }


CURRENT_SHARED = f"{PPC}/CurrentSharedProcessorConfiguration"


def _observed_lpar():
    lpar = _lpar(_entry())
    ppc = _find(lpar, PPC)
    ET.SubElement(ppc, f"{{{UOM}}}RuntimeHasDedicatedProcessors").text = "false"
    ET.SubElement(ppc, f"{{{UOM}}}RuntimeSharingMode").text = "uncapped"
    current = ET.SubElement(ppc, f"{{{UOM}}}CurrentSharedProcessorConfiguration")
    ET.SubElement(current, f"{{{UOM}}}CurrentUncappedWeight").text = "64"
    ET.SubElement(current, f"{{{UOM}}}RuntimeUncappedWeight").text = "0"
    return lpar


def _observe(lpar, paths=()):
    assert callable(getattr(lpar_documents, "sharing_observation", None))
    return lpar_documents.sharing_observation(lpar, paths)


def test_sharing_observation_separates_exact_configured_current_runtime_paths():
    lpar = _observed_lpar()
    # Similar names at the wrong depth must never replace the exact schema paths.
    ET.SubElement(_find(lpar, PPC), f"{{{UOM}}}CurrentUncappedWeight").text = "255"
    observed = _observe(lpar, (f"{SHARED}/DesiredProcessingUnits",))
    assert observed == {
        "uuid": LPAR,
        "configured": {
            "dedicated": False,
            "mode": "uncapped",
            "weight": 128,
            "weight_status": "valid",
            "raw_weight": "128",
        },
        "current": {
            "dedicated": False,
            "mode": "uncapped",
            "weight": 64,
            "weight_status": "valid",
            "raw_weight": "64",
        },
        "runtime": {
            "dedicated": False,
            "mode": "uncapped",
            "weight": 0,
            "weight_status": "valid",
            "raw_weight": "0",
        },
        "requested": {f"{SHARED}/DesiredProcessingUnits": "0.3"},
    }


@pytest.mark.parametrize(
    ("text", "status", "weight"),
    [
        (None, "missing", None),
        ("", "malformed", None),
        ("-1", "malformed", None),
        ("256", "malformed", None),
        ("1.0", "malformed", None),
        ("１２", "malformed", None),
        ("0", "valid", 0),
        ("255", "valid", 255),
    ],
)
def test_sharing_observation_weight_status_preserves_raw(text, status, weight):
    lpar = _observed_lpar()
    node = _find(lpar, f"{SHARED}/UncappedWeight")
    if text is None:
        _find(lpar, SHARED).remove(node)
    else:
        node.text = text
    configured = _observe(lpar)["configured"]
    assert configured["weight_status"] == status
    assert configured["weight"] == weight
    assert configured["raw_weight"] == text


@pytest.mark.parametrize("dedicated", [False, True])
def test_sharing_observation_capped_dedicated_weight_is_inapplicable(dedicated):
    lpar = _lpar(_entry(dedicated=dedicated))
    if not dedicated:
        _find(lpar, f"{PPC}/SharingMode").text = "capped"
        _find(lpar, f"{PPC}/CurrentSharingMode").text = "capped"
    observed = _observe(lpar)
    for branch in ("configured", "current"):
        assert observed[branch]["weight_status"] == "inapplicable"
        assert observed[branch]["weight"] is None


@pytest.mark.parametrize(
    "kind",
    [
        "root-namespace",
        "root-name",
        "missing-id",
        "bad-id",
        "duplicate-id",
        "foreign-id",
        "nested-id",
    ],
)
def test_sharing_observation_refuses_untrusted_identity(kind):
    lpar = _observed_lpar()
    identity = _find(lpar, "Metadata/Atom/AtomID")
    if kind == "root-namespace":
        lpar.tag = "{urn:other}LogicalPartition"
    elif kind == "root-name":
        lpar.tag = f"{{{UOM}}}VirtualIOServer"
    elif kind == "missing-id":
        _find(lpar, "Metadata/Atom").remove(identity)
    elif kind == "bad-id":
        identity.text = "not-a-uuid"
    elif kind == "duplicate-id":
        ET.SubElement(_find(lpar, "Metadata/Atom"), f"{{{UOM}}}AtomID").text = LPAR
    elif kind == "foreign-id":
        identity.tag = "{urn:other}AtomID"
    else:
        ET.SubElement(identity, f"{{{UOM}}}AtomID").text = LPAR
    with pytest.raises(ValueError):
        _observe(lpar)


@pytest.mark.parametrize("branch", ["configured", "current", "runtime"])
@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "foreign", "nested", "invalid"]
)
def test_sharing_observation_unavailable_branch_preserves_other_evidence(
    branch, damage
):
    lpar = _observed_lpar()
    name = {
        "configured": "SharingMode",
        "current": "CurrentSharingMode",
        "runtime": "RuntimeSharingMode",
    }[branch]
    node = _find(lpar, f"{PPC}/{name}")
    if damage == "missing":
        _find(lpar, PPC).remove(node)
    elif damage == "duplicate":
        ET.SubElement(_find(lpar, PPC), node.tag).text = node.text
    elif damage == "foreign":
        node.tag = f"{{urn:other}}{name}"
    elif damage == "nested":
        ET.SubElement(node, f"{{{UOM}}}Unexpected").text = "uncapped"
    else:
        node.text = "Unknown"
    observed = _observe(lpar, (f"{SHARED}/DesiredProcessingUnits",))
    assert observed[branch] is None
    assert observed["requested"] == {f"{SHARED}/DesiredProcessingUnits": "0.3"}
    other = "current" if branch == "configured" else "configured"
    assert observed[other]["mode"] == "uncapped"
    assert observed[other]["weight"] == (64 if other == "current" else 128)


@pytest.mark.parametrize("damage", ["duplicate", "foreign", "nested"])
def test_sharing_observation_rejects_ambiguous_requested_scalar(damage):
    lpar = _observed_lpar()
    path = f"{SHARED}/DesiredProcessingUnits"
    node = _find(lpar, path)
    if damage == "duplicate":
        ET.SubElement(_find(lpar, SHARED), node.tag).text = "0.3"
    elif damage == "foreign":
        node.tag = "{urn:other}DesiredProcessingUnits"
    else:
        ET.SubElement(node, f"{{{UOM}}}Unexpected").text = "0.3"
    assert _observe(lpar, (path,))["requested"][path] is None


@pytest.mark.parametrize("damage", ["duplicate", "foreign", "nested"])
def test_sharing_observation_ambiguous_weight_is_malformed(damage):
    lpar = _observed_lpar()
    node = _find(lpar, f"{CURRENT_SHARED}/CurrentUncappedWeight")
    if damage == "duplicate":
        ET.SubElement(_find(lpar, CURRENT_SHARED), node.tag).text = "64"
    elif damage == "foreign":
        node.tag = "{urn:other}CurrentUncappedWeight"
    else:
        ET.SubElement(node, f"{{{UOM}}}Unexpected").text = "64"
    observed = _observe(lpar)
    assert observed["current"]["weight_status"] == "malformed"
    assert observed["current"]["weight"] is None
    assert observed["configured"]["weight"] == 128


def test_sharing_context_helpers_return_effective_mode_without_mapping_it():
    assert callable(getattr(lpar_documents, "validate_sharing_update", None))
    assert callable(getattr(lpar_documents, "validate_resource_inputs", None))
    assert callable(getattr(lpar_documents, "reject_update_weight_on_create", None))
    resources = LparResources(uncapped_weight=0)
    assert lpar_documents.validate_resource_inputs(resources) is None
    assert lpar_documents.validate_sharing_update(_lpar(_entry()), resources) == (
        False,
        "uncapped",
    )
    assert lpar_documents.validate_sharing_update(
        _lpar(_entry(has_dedicated=False)), LparResources()
    ) == (False, None)
    assert lpar_documents.reject_update_weight_on_create(LparResources()) is None
    with pytest.raises(ValueError, match="uncapped_weight.*update"):
        lpar_documents.reject_update_weight_on_create(resources)
