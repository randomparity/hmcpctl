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

from hmcpctl import errors as hmc_errors
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
    assert get.call_count == 3  # whole-resource preflight precedes rename
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
async def test_modify_refuses_known_invalid_resources_before_rename(
    mock_hmc, authorized
):
    _, post = _routes(mock_hmc)
    with pytest.raises(ValueError, match="switch the partition"):
        await _modify(LparResources(dedicated=True, desired_procs=2), new_name="lpar-b")
    assert not post.called


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


WEIGHT = f"{SHARED}/UncappedWeight"


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("weight", [0, 255])
@pytest.mark.asyncio
async def test_sharing_xml_insert(mock_hmc, missing, weight):
    lpar = _lpar(_entry())
    shared = _find(lpar, SHARED)
    anchor = _find(lpar, f"{SHARED}/SharedProcessorPoolID")
    anchor.set("preserve", "anchor")
    ET.SubElement(shared, f"{{{UOM}}}Untouched", {"keep": "yes"}).text = "tail"
    if missing:
        shared.remove(_find(lpar, WEIGHT))
    original = ET.tostring(lpar, encoding="unicode")
    _, post = _routes(mock_hmc, entry=original)
    await _update(
        lambda node: partition_updates(
            node, resources=LparResources(uncapped_weight=weight)
        )
    )
    expected = _lpar(original)
    parent = _find(expected, SHARED)
    if missing:
        leaf = ET.Element(f"{{{UOM}}}UncappedWeight", {"kxe": "false", "kb": "CUD"})
        parent.insert(
            list(parent).index(_find(expected, f"{SHARED}/SharedProcessorPoolID")) + 1,
            leaf,
        )
    else:
        leaf = _find(expected, WEIGHT)
    leaf.text = str(weight)
    assert post.call_count == 1
    assert _canonical(_lpar(post.calls.last.request.content)) == _canonical(expected)


def _damaged_transport_lpar(damage):
    lpar = _lpar(_entry())
    shared = _find(lpar, SHARED)
    if damage.startswith("missing-weight"):
        shared.remove(_find(lpar, WEIGHT))
        if damage.endswith("capped"):
            _find(lpar, f"{PPC}/SharingMode").text = "capped"
        elif damage.endswith("dedicated"):
            _find(lpar, f"{PPC}/HasDedicatedProcessors").text = "true"
        elif damage.endswith("mode"):
            _find(lpar, f"{PPC}/SharingMode").text = "Unknown"
        return lpar
    target_path = {
        "container": SHARED,
        "ppc": PPC,
        "leaf": WEIGHT,
        "anchor": f"{SHARED}/SharedProcessorPoolID",
        "ordinary": "PartitionName",
    }
    kind, target = damage.split(":")
    node = _find(lpar, target_path[target])
    parent = next(parent for parent in lpar.iter() if node in list(parent))
    if kind == "duplicate":
        parent.append(DET.fromstring(ET.tostring(node)))
    elif kind == "namespace":
        node.tag = "{urn:wrong}" + localname(node.tag)
    elif kind == "scalar":
        ET.SubElement(node, f"{{{UOM}}}Child").text = "decoy"
    elif kind == "missing":
        parent.remove(node)
        if target == "anchor":
            shared.remove(_find(lpar, WEIGHT))
    return lpar


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate:container",
        "namespace:container",
        "duplicate:ppc",
        "namespace:ppc",
        "duplicate:leaf",
        "namespace:leaf",
        "scalar:leaf",
        "duplicate:anchor",
        "namespace:anchor",
        "scalar:anchor",
        "missing:anchor",
        "missing:container",
        "duplicate:ordinary",
        "namespace:ordinary",
        "scalar:ordinary",
        "missing:ordinary",
        "missing-weight-capped",
        "missing-weight-dedicated",
        "missing-weight-mode",
    ],
)
@pytest.mark.asyncio
async def test_sharing_xml_insert_refuses(mock_hmc, damage):
    lpar = _damaged_transport_lpar(damage)
    _, post = _routes(mock_hmc, entry=ET.tostring(lpar, encoding="unicode"))
    path = "PartitionName" if damage.endswith(":ordinary") else WEIGHT
    with pytest.raises((HMCError, ValueError)) as caught:
        await _update(lambda _node: {path: "0"})
    assert not isinstance(caught.value, getattr(hmc_errors, "LparUpdateError", ()))
    assert not post.called


@pytest.mark.parametrize(
    "path",
    [
        f"{SHARED}/NoSuchField",
        f"{PPC}/CurrentSharingMode",
        f"{PPC}/CurrentSharedProcessorConfiguration/CurrentUncappedWeight",
        f"{PPC}/CurrentSharedProcessorConfiguration/RuntimeUncappedWeight",
    ],
)
@pytest.mark.asyncio
async def test_sharing_xml_insert_refuses_decoys(mock_hmc, path):
    lpar = _observed_lpar()
    _find(lpar, SHARED).remove(_find(lpar, WEIGHT))
    _, post = _routes(mock_hmc, entry=ET.tostring(lpar, encoding="unicode"))
    with pytest.raises((HMCError, ValueError)):
        await _update(lambda _node: {path: "0"})
    assert not post.called


@pytest.mark.asyncio
async def test_sharing_xml_insert_uses_effective_mode_and_preserves_decoys(mock_hmc):
    lpar = _observed_lpar()
    _find(lpar, SHARED).remove(_find(lpar, WEIGHT))
    _find(lpar, f"{PPC}/SharingMode").text = "capped"
    _find(lpar, f"{PPC}/CurrentSharingMode").text = "capped"
    original = ET.tostring(lpar, encoding="unicode")
    _, post = _routes(mock_hmc, entry=original)
    await _update(
        lambda node: partition_updates(
            node, resources=LparResources(sharing_mode="uncapped", uncapped_weight=0)
        )
    )
    written = _lpar(post.calls.last.request.content)
    assert _find(written, WEIGHT).text == "0"
    assert _find(written, f"{PPC}/SharingMode").text == "uncapped"
    for field in [
        "CurrentSharingMode",
        "RuntimeSharingMode",
        "CurrentSharedProcessorConfiguration",
    ]:
        assert _canonical(_find(written, f"{PPC}/{field}")) == _canonical(
            _find(lpar, f"{PPC}/{field}")
        )


@pytest.mark.parametrize("require_etag", [False, True])
@pytest.mark.asyncio
async def test_sharing_write_stage_read_etag(mock_hmc, require_etag):
    get, post = _routes(mock_hmc, etag=None)
    async with HMCClient(make_config()) as hmc:
        assert callable(getattr(hmc, "read_logical_partition_document", None))
        if require_etag:
            with pytest.raises(HMCError, match="no ETag") as caught:
                await hmc.read_logical_partition_document(LPAR, require_etag=True)
            assert not isinstance(caught.value, hmc_errors.LparUpdateError)
        else:
            lpar, etag = await hmc.read_logical_partition_document(LPAR)
            assert etag is None
            assert _canonical(lpar) == _canonical(_lpar(_entry()))
    assert get.call_count == 1 and not post.called
    assert "X-HMC-Schema-Version" not in get.calls.last.request.headers


@pytest.mark.parametrize("bad", ["../ManagedSystem", "not-a-uuid"])
@pytest.mark.asyncio
async def test_sharing_write_stage_read_uuid(mock_hmc, bad):
    async with HMCClient(make_config()) as hmc:
        assert callable(getattr(hmc, "read_logical_partition_document", None))
        with pytest.raises(ValueError, match="UUID"):
            await hmc.read_logical_partition_document(bad)
    assert not any(
        "LogicalPartition" in str(call.request.url) for call in mock_hmc.calls
    )


@pytest.mark.asyncio
async def test_sharing_write_stage(mock_hmc):
    get, post = _routes(mock_hmc, etag='W/"original-etag"')
    result = await _update(
        lambda node: partition_updates(node, resources=LparResources(uncapped_weight=0))
    )
    assert result is not None
    assert get.call_count == 1 and post.call_count == 1
    assert post.calls.last.request.headers["If-Match"] == 'W/"original-etag"'
    assert "X-HMC-Schema-Version" not in post.calls.last.request.headers
    assert _find(_lpar(post.calls.last.request.content), WEIGHT).text == "0"


@pytest.mark.parametrize("stage", ["etag", "parse", "mapper", "structure", "get"])
@pytest.mark.asyncio
async def test_sharing_write_stage_refusals(mock_hmc, stage):
    kwargs = {"etag": None} if stage == "etag" else {}
    if stage == "parse":
        kwargs["entry"] = "<broken"
    if stage == "get":
        kwargs["get_status"] = 500
    _, post = _routes(mock_hmc, **kwargs)
    updates = (
        _raise
        if stage == "mapper"
        else lambda node: {"Missing" if stage == "structure" else WEIGHT: "0"}
    )
    with pytest.raises((HMCError, ValueError)) as caught:
        await _update(updates)
    assert not isinstance(caught.value, getattr(hmc_errors, "LparUpdateError", ()))
    assert not post.called


@pytest.mark.parametrize(
    "status,body,submission",
    [
        (200, "<broken", "acknowledged"),
        (201, "<broken", "acknowledged"),
        (202, "<broken", "acknowledged"),
        (299, "<broken", "acknowledged"),
        (412, "concurrent change", "rejected"),
        (400, "refused", "unknown"),
        (500, "failed", "unknown"),
        (302, "redirect", "unknown"),
        (None, "transport", "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_post_transport_and_parse_certainty(mock_hmc, status, body, submission):
    get, post = _routes(mock_hmc)
    if status is None:
        post.mock(side_effect=httpx.ReadTimeout("synthetic timeout"))
    else:
        post.mock(return_value=httpx.Response(status, text=body))
    with pytest.raises(HMCError) as caught:
        await _update(lambda _node: {WEIGHT: "0"})
    assert hasattr(hmc_errors, "LparUpdateError")
    assert isinstance(caught.value, hmc_errors.LparUpdateError)
    assert caught.value.submission == submission
    assert caught.value.status_code == status
    if submission == "acknowledged":
        assert "Failed to parse" in str(caught.value)
        assert isinstance(caught.value.__cause__, HMCError)
    if status == 412:
        assert "If-Match mismatch" in str(caught.value)
        assert "Nothing was written" in str(caught.value)
    assert get.call_count == 1 and post.call_count == 1


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
@pytest.mark.asyncio
async def test_post_transport_and_parse_certainty_empty_success(mock_hmc, status):
    _, post = _routes(mock_hmc)
    post.mock(return_value=httpx.Response(status))
    assert await _update(lambda _node: {WEIGHT: "0"}) is None
    assert post.call_count == 1


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.asyncio
async def test_update_preflight_edits_only_a_detached_read(mock_hmc, missing):
    lpar = _lpar(_entry())
    if missing:
        _find(lpar, SHARED).remove(_find(lpar, WEIGHT))
    original = ET.tostring(lpar, encoding="unicode")
    get, post = _routes(mock_hmc, entry=original)
    seen = []

    def updates(node):
        seen.append(node)
        return partition_updates(node, resources=LparResources(uncapped_weight=0))

    async with HMCClient(make_config()) as hmc:
        assert callable(getattr(hmc, "validate_logical_partition_update", None))
        assert (
            await hmc.validate_logical_partition_update(LPAR, updates, "weight") is None
        )
        assert _find(seen[0], WEIGHT).text == "0"
        assert not post.called
        await hmc.update_logical_partition(LPAR, updates, "weight")
    assert get.call_count == 2 and post.call_count == 1
    assert seen[0] is not seen[1]
    assert _canonical(_lpar(get.calls[0].response.text)) == _canonical(lpar)


@pytest.mark.asyncio
async def test_update_preflight_edits_only_a_detached_read_refuses(mock_hmc):
    lpar = _damaged_transport_lpar("duplicate:anchor")
    _, post = _routes(mock_hmc, entry=ET.tostring(lpar, encoding="unicode"))
    async with HMCClient(make_config()) as hmc:
        assert callable(getattr(hmc, "validate_logical_partition_update", None))
        with pytest.raises(HMCError):
            await hmc.validate_logical_partition_update(
                LPAR, lambda node: {WEIGHT: "0"}, "weight"
            )
        with pytest.raises(HMCError):
            await hmc.update_logical_partition(
                LPAR, lambda node: {WEIGHT: "0"}, "weight"
            )
    assert not post.called


@pytest.mark.parametrize(
    "status,submission",
    [(200, "acknowledged"), (202, "acknowledged"), (412, "rejected"), (500, "unknown")],
)
@pytest.mark.asyncio
async def test_post_transport_and_parse_certainty_buffer_refusal(
    mock_hmc, status, submission
):
    _, post = _routes(mock_hmc)
    post.mock(
        return_value=httpx.Response(
            status, text="bounded", headers={"Content-Length": str(1 << 30)}
        )
    )
    with pytest.raises(HMCError) as caught:
        await _update(lambda node: {WEIGHT: "0"})
    assert isinstance(caught.value, hmc_errors.LparUpdateError)
    assert caught.value.submission == submission
    assert caught.value.status_code == status
    assert "exceeds limit" in str(caught.value)
    if status == 412:
        assert "If-Match mismatch" in str(caught.value)
        assert "Nothing was written" in str(caught.value)
    assert post.call_count == 1


def test_post_transport_and_parse_certainty_error_compatibility():
    assert hasattr(hmc_errors, "LparUpdateError")
    body = "<Message>" + "é" * 5000 + "</Message>"
    ordinary = HMCError("write failed", 500, body)
    error = hmc_errors.LparUpdateError("write failed", "unknown", 500, body)
    assert isinstance(error, HMCError)
    assert str(error) == str(ordinary)
    assert error.body == ordinary.body
    assert len(error.body.encode("utf-8")) <= hmc_errors.MAX_ERROR_BODY_BYTES
    assert error.status_code == 500


# Independent sharing verification uses real XML mapping, editor and transport.
def _sharing_snapshot(weight="128", current=None, runtime=None, **changes):
    node = _observed_lpar()
    for path, text in {
        f"{PPC}/SharedProcessorConfiguration/UncappedWeight": weight,
        f"{CURRENT_SHARED}/CurrentUncappedWeight": current
        if current is not None
        else weight,
        f"{CURRENT_SHARED}/RuntimeUncappedWeight": runtime
        if runtime is not None
        else weight,
        **changes,
    }.items():
        _find(node, path).text = text
    ET.SubElement(node, f"{{{UOM}}}CurrentProfileSync").text = "On"
    return node


def _sharing_routes(router, before, after, *, status=200, body=None, fresh=None):
    responses = [
        httpx.Response(
            200, text=ET.tostring(node, encoding="unicode"), headers={"ETag": ETAG}
        )
        for node in (before, fresh if fresh is not None else before)
    ]
    if isinstance(after, ET.Element):
        responses.append(
            httpx.Response(200, text=ET.tostring(after, encoding="unicode"))
        )
    else:
        responses.append(httpx.Response(503, text="independent read unavailable"))
    get = router.get(LPAR_PATH, params={"group": "Advanced"}).mock(
        side_effect=responses
    )
    post = router.post(LPAR_PATH, params={"group": "Advanced"}).mock(
        return_value=httpx.Response(status, text=body if body is not None else _entry())
    )
    return get, post


@pytest.mark.asyncio
@pytest.mark.parametrize("weight", [0, 255, None])
@pytest.mark.parametrize("runtime", ["matched", "mismatched", "unavailable"])
async def test_sharing_outcome_independent_configuration(
    mock_hmc, authorized, weight, runtime
):
    after = _sharing_snapshot(str(weight if weight is not None else 73))
    if runtime == "mismatched":
        _find(after, f"{CURRENT_SHARED}/RuntimeUncappedWeight").text = "19"
    elif runtime == "unavailable":
        _find(after, f"{PPC}/RuntimeSharingMode").text = "invalid"
    get, post = _sharing_routes(mock_hmc, _sharing_snapshot(), after)
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(
            hmc, LPAR, LparResources(sharing_mode="uncapped", uncapped_weight=weight)
        )
    assert result["outcome"] == "applied"
    assert result["workflow_completed"] is True
    assert result["configuration_verification"] == "matched"
    assert result["runtime_verification"] == runtime
    assert result["UUID"] == LPAR
    assert result["Resource"][PPC]
    assert result["completion_scope"] == "current-configuration"
    assert result["expected"] == {
        "configured": {
            f"{PPC}/SharingMode": "uncapped",
            f"{PPC}/HasDedicatedProcessors": "false",
            **(
                {f"{PPC}/SharedProcessorConfiguration/UncappedWeight": str(weight)}
                if weight is not None
                else {}
            ),
        },
        "effective_mode": "uncapped",
        "weight": weight,
    }
    assert result["change_location"]["lives_in"] == "current-configuration-and-profile"
    assert bool(result["warnings"]) is (runtime != "matched")
    assert get.call_count == 3 and post.call_count == 1
    assert post.calls.last.request.headers["If-Match"] == ETAG
    assert set(result) == {
        "UUID",
        "Resource",
        "completion_scope",
        "outcome",
        "workflow_completed",
        "submission_status",
        "configuration_verification",
        "runtime_verification",
        "before",
        "after",
        "expected",
        "submission_error",
        "read_error",
        "change_location",
        "warnings",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "change", "outcome"),
    [
        (200, "none", "unverified"),
        (412, "none", "rejected"),
        (503, "none", "unverified"),
        (200, "partial", "partial"),
        (412, "partial", "partial"),
        (503, "partial", "partial"),
        (200, "complete", "applied"),
        (412, "complete", "applied"),
        (503, "complete", "applied"),
    ],
)
async def test_sharing_outcome_submission_precedence(
    mock_hmc, authorized, status, change, outcome
):
    before = _sharing_snapshot()
    after = _sharing_snapshot(
        "0" if change != "none" else "128",
        current="0" if change == "complete" else "128",
    )
    _, post = _sharing_routes(mock_hmc, before, after, status=status, body="not XML")
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(hmc, LPAR, LparResources(uncapped_weight=0))
    assert result["outcome"] == outcome
    assert (
        result["submission_status"]
        == {200: "acknowledged", 412: "rejected", 503: "unknown"}[status]
    )
    assert result["submission_error"]
    assert result["Resource"]["CurrentProfileSync"] == "On"
    assert post.call_count == 1
    if outcome == "applied" and status != 200:
        assert any("causality" in warning for warning in result["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "read",
        "uuid",
        "missing_uuid",
        "malformed_uuid",
        "context",
        "namespace",
        "duplicate",
        "weight",
    ],
)
async def test_sharing_outcome_unavailable_or_malformed_evidence(
    mock_hmc, authorized, damage
):
    after = _sharing_snapshot("0")
    if damage in {"uuid", "malformed_uuid"}:
        _find(after, "Metadata/Atom/AtomID").text = (
            SYSTEM if damage == "uuid" else "invalid"
        )
    elif damage == "missing_uuid":
        atom = _find(after, "Metadata/Atom")
        atom.remove(_find(after, "Metadata/Atom/AtomID"))
    elif damage == "context":
        _find(after, f"{PPC}/CurrentSharingMode").text = "invalid"
    elif damage == "namespace":
        _find(after, f"{PPC}/CurrentSharingMode").tag = "{wrong}CurrentSharingMode"
    elif damage == "duplicate":
        ET.SubElement(
            _find(after, PPC), f"{{{UOM}}}CurrentSharingMode"
        ).text = "uncapped"
    elif damage == "weight":
        _find(after, f"{CURRENT_SHARED}/CurrentUncappedWeight").text = "NaN"
    _sharing_routes(mock_hmc, _sharing_snapshot(), None if damage == "read" else after)
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(hmc, LPAR, LparResources(uncapped_weight=0))
    assert result["workflow_completed"] is False
    if damage in {"read", "uuid", "missing_uuid", "malformed_uuid"}:
        assert result["outcome"] == "unverified"
        assert result["Resource"] == {} and result["after"] is None
        assert result["read_error"] and result["warnings"]
        assert result["change_location"]["lives_in"] == "unknown"
    else:
        assert result["outcome"] == "partial"
    assert result["UUID"] == LPAR


@pytest.mark.asyncio
@pytest.mark.parametrize("numeric", ["0.30", "3e-1", "0.300", "NaN", "０.３", "0.4"])
async def test_mixed_requested_configured_fields_and_runtime_are_independent(
    mock_hmc, authorized, numeric
):
    after = _sharing_snapshot(
        "0",
        **{
            f"{PPC}/SharedProcessorConfiguration/DesiredProcessingUnits": numeric,
            "PartitionMemoryConfiguration/DesiredMemory": "4096.0",
        },
    )
    _find(after, f"{PPC}/RuntimeSharingMode").text = "unknown"
    _sharing_routes(mock_hmc, _sharing_snapshot(), after)
    async with HMCClient(make_config()) as hmc:
        result = await modify_lpar(
            hmc,
            LPAR,
            LparResources(uncapped_weight=0, desired_procs=0.3, desired_memory=4096),
            LparPcieAssignments(),
        )
    matched = numeric in {"0.30", "3e-1", "0.300"}
    assert result.workflow_completed is matched
    verification = (
        "matched" if matched else ("mismatched" if numeric == "0.4" else "unavailable")
    )
    assert result.lpar["configuration_verification"] == verification
    assert result.lpar["runtime_verification"] == "unavailable"
    assert result.steps[0].status == ("ok" if matched else "error")
    assert result.steps[0].result == result.lpar


@pytest.mark.asyncio
async def test_sharing_outcome_full_comparison_before_multibyte_diagnostic_bounds(
    mock_hmc, authorized
):
    before = _sharing_snapshot("0" * 4200 + "128")
    after = _sharing_snapshot("0" * 4200 + "1")
    assert _observe(before)["configured"]["weight"] == 128
    assert _observe(after)["configured"]["weight"] == 1
    _sharing_routes(mock_hmc, before, after)
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(hmc, LPAR, LparResources(uncapped_weight=0))
    assert result["outcome"] == "partial"
    assert result["configuration_verification"] != "matched"
    assert result["expected"]["weight"] == 0
    for observed in (result["before"], result["after"]):
        assert len(observed["configured"]["raw_weight"].encode()) <= 4096
        assert len(next(iter(observed["requested"].values())).encode()) <= 4096
    assert any("truncat" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_sharing_malformed_multibyte_diagnostics_do_not_establish_change(
    mock_hmc, authorized
):
    before = _sharing_snapshot("界" * 2000 + "a")
    after = _sharing_snapshot("界" * 2000 + "b")
    _sharing_routes(mock_hmc, before, after)
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(hmc, LPAR, LparResources(uncapped_weight=0))
    assert result["outcome"] == "unverified"
    assert result["configuration_verification"] == "unavailable"
    assert result["workflow_completed"] is False
    for observed in (result["before"], result["after"]):
        assert len(observed["configured"]["raw_weight"].encode()) <= 4096
        assert len(next(iter(observed["requested"].values())).encode()) <= 4096
    assert any("truncat" in warning for warning in result["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("race", [False, True])
async def test_real_sharing_context_barrier_before_rename_and_post(
    mock_hmc, authorized, race
):
    before = _sharing_snapshot()
    invalid = _sharing_snapshot(**{f"{PPC}/CurrentSharingMode": "capped"})
    get, post = _routes(mock_hmc)
    documents = (
        [before, before, invalid, _sharing_snapshot("0")]
        if race
        else [invalid, invalid, invalid]
    )
    get.mock(
        side_effect=[
            httpx.Response(
                200, text=ET.tostring(node, encoding="unicode"), headers={"ETag": ETAG}
            )
            for node in documents
        ]
    )
    if race:
        result = await _modify(LparResources(uncapped_weight=0), new_name="renamed")
        assert result.workflow_completed is False
        assert [(s.step, s.status) for s in result.steps] == [
            ("rename", "ok"),
            ("resources", "error"),
        ]
        assert result.lpar["Resource"]["PartitionName"] == "lpar-a"
        assert post.call_count == 1
    else:
        with pytest.raises(ValueError, match="CurrentSharingMode disagree"):
            await _modify(LparResources(uncapped_weight=0), new_name="renamed")
        assert not post.called


@pytest.mark.asyncio
async def test_sharing_modify_barrier_incomplete_skips_pcie(mock_hmc, authorized):
    from hmcpctl.operations.lpar.assignments import DedicatedPcieAssignment

    get, post = _sharing_routes(mock_hmc, _sharing_snapshot(), _sharing_snapshot())
    # Preflight, rename RMW, resources RMW, then independent observation.
    get.mock(
        side_effect=[
            httpx.Response(
                200,
                text=ET.tostring(_sharing_snapshot(), encoding="unicode"),
                headers={"ETag": ETAG},
            )
            for _ in range(4)
        ]
    )
    async with HMCClient(make_config()) as hmc:
        result = await modify_lpar(
            hmc,
            LPAR,
            LparResources(uncapped_weight=0),
            LparPcieAssignments(dedicated=(DedicatedPcieAssignment("profile", "123"),)),
            new_name="renamed",
            system_name_or_uuid=SYSTEM,
        )
    assert result.workflow_completed is False
    assert [(s.step, s.status) for s in result.steps] == [
        ("rename", "ok"),
        ("resources", "error"),
        ("dedicated[0]", "skipped"),
    ]
    assert result.steps[0].result["Resource"]["PartitionName"] == "lpar-a"
    assert result.steps[1].result == result.lpar
    assert result.lpar["outcome"] == "unverified"
    assert post.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "rest", "dedicated"),
    [
        ("capped", "capped", False),
        ("uncapped", "uncapped", False),
        ("keep_idle_procs", "keep idle procs", True),
        ("share_idle_procs", "sre idle proces", True),
        ("share_idle_procs_active", "sre idle procs active", True),
        ("share_idle_procs_always", "sre idle procs always", True),
    ],
)
async def test_sharing_outcome_all_modes_and_inapplicable_weights(
    mock_hmc, authorized, requested, rest, dedicated
):
    before = _sharing_snapshot()
    if dedicated:
        for prefix in ("", "Current", "Runtime"):
            _find(before, f"{PPC}/{prefix}HasDedicatedProcessors").text = "true"
            _find(before, f"{PPC}/{prefix}SharingMode").text = "keep idle procs"
    after = DET.fromstring(ET.tostring(before))
    for prefix in ("", "Current", "Runtime"):
        _find(after, f"{PPC}/{prefix}SharingMode").text = rest
    if dedicated or requested == "capped":
        _find(
            after, f"{PPC}/SharedProcessorConfiguration/UncappedWeight"
        ).text = "malformed"
        _find(after, f"{CURRENT_SHARED}/CurrentUncappedWeight").text = "stale"
    _sharing_routes(mock_hmc, before, after)
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(
            hmc, LPAR, LparResources(sharing_mode=requested)
        )
    assert result["outcome"] == "applied"
    assert result["runtime_verification"] == "matched"
    assert result["expected"]["effective_mode"] == rest
    assert result["expected"]["weight"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "path", "value", "rendered"),
    [
        ("min_memory", "PartitionMemoryConfiguration/MinimumMemory", 1024, "1024.00"),
        (
            "desired_memory",
            "PartitionMemoryConfiguration/DesiredMemory",
            4096,
            "4096e0",
        ),
        ("max_memory", "PartitionMemoryConfiguration/MaximumMemory", 8192, "8192.0"),
        (
            "min_procs",
            f"{PPC}/SharedProcessorConfiguration/MinimumProcessingUnits",
            0.1,
            "0.10",
        ),
        (
            "desired_procs",
            f"{PPC}/SharedProcessorConfiguration/DesiredProcessingUnits",
            0.3,
            "3e-1",
        ),
        (
            "max_procs",
            f"{PPC}/SharedProcessorConfiguration/MaximumProcessingUnits",
            2,
            "2.00",
        ),
        (
            "min_vcpus",
            f"{PPC}/SharedProcessorConfiguration/MinimumVirtualProcessors",
            1,
            "1e0",
        ),
        (
            "desired_vcpus",
            f"{PPC}/SharedProcessorConfiguration/DesiredVirtualProcessors",
            3,
            "3.0",
        ),
        (
            "max_vcpus",
            f"{PPC}/SharedProcessorConfiguration/MaximumVirtualProcessors",
            6,
            "6.00",
        ),
    ],
)
@pytest.mark.parametrize("stale", [False, True])
async def test_mixed_requested_configured_fields_every_mapping(
    mock_hmc, authorized, field, path, value, rendered, stale
):
    after = _sharing_snapshot("0", **{path: "99" if stale else rendered})
    _sharing_routes(mock_hmc, _sharing_snapshot(), after)
    async with HMCClient(make_config()) as hmc:
        result = await modify_lpar(
            hmc,
            LPAR,
            LparResources(uncapped_weight=0, **{field: value}),
            LparPcieAssignments(),
        )
    assert result.workflow_completed is not stale
    assert result.lpar["configuration_verification"] == (
        "mismatched" if stale else "matched"
    )
    assert result.lpar["runtime_verification"] == "matched"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("timed out"), httpx.ConnectError("界" * 3000)]
)
async def test_sharing_outcome_transport_unknown_and_bounded_diagnostics(
    mock_hmc, authorized, error
):
    _, post = _sharing_routes(mock_hmc, _sharing_snapshot(), None)
    post.mock(side_effect=error)
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(hmc, LPAR, LparResources(uncapped_weight=0))
    assert result["outcome"] == "unverified"
    assert result["submission_status"] == "unknown"
    assert len(result["submission_error"].encode()) <= 4096
    assert result["read_error"]
    assert result["Resource"] == {}
    assert any("truncat" in warning for warning in result["warnings"]) is isinstance(
        error, httpx.ConnectError
    )
    assert post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "duplicate", "namespace"])
async def test_sharing_modify_barrier_whole_mapping_structure_before_rename(
    mock_hmc, authorized, damage
):
    node = _sharing_snapshot()
    memory = _find(node, "PartitionMemoryConfiguration")
    desired = _find(node, "PartitionMemoryConfiguration/DesiredMemory")
    if damage == "missing":
        memory.remove(desired)
    elif damage == "duplicate":
        ET.SubElement(memory, f"{{{UOM}}}DesiredMemory").text = "3072"
    else:
        desired.tag = "{wrong}DesiredMemory"
    get, post = _routes(mock_hmc, entry=ET.tostring(node, encoding="unicode"))
    with pytest.raises(HMCError, match="DesiredMemory"):
        await _modify(
            LparResources(desired_memory=4096, uncapped_weight=0), new_name="renamed"
        )
    assert get.call_count == 1 and not post.called


@pytest.mark.asyncio
async def test_sharing_outcome_rejection_ignores_numeric_formatting(
    mock_hmc, authorized
):
    after = _sharing_snapshot("0128", current="00128")
    _sharing_routes(mock_hmc, _sharing_snapshot(), after, status=412)
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(
            hmc, LPAR, LparResources(sharing_mode="capped")
        )
    assert result["outcome"] == "rejected"
    assert result["workflow_completed"] is False


@pytest.mark.asyncio
async def test_sharing_rejected_submission_with_uuid_only_read_is_unverified(
    mock_hmc, authorized
):
    after = _sharing_snapshot()
    for child in list(after):
        if localname(child.tag) != "Metadata":
            after.remove(child)
    _, post = _sharing_routes(mock_hmc, _sharing_snapshot(), after, status=412)
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(hmc, LPAR, LparResources(uncapped_weight=0))
    assert result["submission_status"] == "rejected"
    assert result["configuration_verification"] == "unavailable"
    assert result["outcome"] == "unverified"
    assert result["workflow_completed"] is False
    assert post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize(
    ("path", "damage"),
    [
        (PPC, "missing"),
        (f"{PPC}/SharingMode", "missing"),
        (f"{PPC}/SharingMode", "invalid"),
        (f"{SHARED}/UncappedWeight", "missing"),
        (f"{SHARED}/UncappedWeight", "NaN"),
        (f"{SHARED}/UncappedWeight", "256"),
        (f"{PPC}/CurrentSharingMode", "missing"),
        (f"{PPC}/CurrentSharingMode", "invalid"),
        (f"{CURRENT_SHARED}/CurrentUncappedWeight", "missing"),
        (f"{CURRENT_SHARED}/CurrentUncappedWeight", "NaN"),
        (f"{SHARED}/DesiredProcessingUnits", "missing"),
        (f"{SHARED}/DesiredProcessingUnits", "NaN"),
    ],
)
async def test_sharing_rejected_submission_needs_comparable_unchanged_state(
    mock_hmc, authorized, path, damage, changed
):
    after = _sharing_snapshot()
    _find(after, "PartitionMemoryConfiguration/DesiredMemory").text = (
        "4096" if changed else "3072"
    )
    if damage == "missing":
        parent, _, _ = path.rpartition("/")
        (_find(after, parent) if parent else after).remove(_find(after, path))
    else:
        _find(after, path).text = damage
    _, post = _sharing_routes(mock_hmc, _sharing_snapshot(), after, status=412)
    async with HMCClient(make_config()) as hmc:
        result = await modify_lpar(
            hmc,
            LPAR,
            LparResources(uncapped_weight=0, desired_procs=0.5, desired_memory=4096),
            LparPcieAssignments(),
        )
    assert result.lpar["submission_status"] == "rejected"
    assert result.lpar["outcome"] == ("partial" if changed else "unverified")
    assert result.workflow_completed is False
    assert post.call_count == 1


@pytest.mark.asyncio
async def test_sharing_outcome_omitted_weight_requires_configured_current_agreement(
    mock_hmc, authorized
):
    _sharing_routes(
        mock_hmc, _sharing_snapshot(), _sharing_snapshot("73", current="74")
    )
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(
            hmc, LPAR, LparResources(sharing_mode="uncapped")
        )
    assert result["configuration_verification"] == "mismatched"
    assert result["workflow_completed"] is False
    assert result["outcome"] == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [SYSTEM, None])
async def test_sharing_modify_barrier_preflight_identity_before_rename(
    mock_hmc, authorized, identity
):
    before = _sharing_snapshot()
    atom = _find(before, "Metadata/Atom")
    if identity is None:
        atom.remove(_find(before, "Metadata/Atom/AtomID"))
    else:
        _find(before, "Metadata/Atom/AtomID").text = identity
    _, post = _routes(mock_hmc, entry=ET.tostring(before, encoding="unicode"))
    with pytest.raises(ValueError, match="UUID"):
        await _modify(LparResources(uncapped_weight=0), new_name="renamed")
    assert not post.called


@pytest.mark.asyncio
async def test_sharing_outcome_before_uses_actual_fresh_callback(mock_hmc, authorized):
    _sharing_routes(
        mock_hmc,
        _sharing_snapshot(),
        _sharing_snapshot("0"),
        fresh=_sharing_snapshot("64"),
    )
    async with HMCClient(make_config()) as hmc:
        result = await set_lpar_processors(hmc, LPAR, LparResources(uncapped_weight=0))
    assert result["before"]["configured"]["weight"] == 64
    assert result["before"]["current"]["weight"] == 64
    assert result["outcome"] == "applied"
