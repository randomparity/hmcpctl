"""Persistent-memory volume client paths and guarded operations (#643, ADR 0373).

Volume bodies are the tokenized V10R3 captures in ``tests/fixtures/pmem/``;
system and partition entries carry the admission fields that capture read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from conftest import make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.persistent_memory import PersistentMemoryVolume
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.memory import persistent
from hmcpctl.operations.lpar.memory.persistent import (
    create_persistent_memory_volume,
    delete_persistent_memory_volume,
    get_persistent_memory_volume,
    list_persistent_memory_volumes,
    rename_persistent_memory_volume,
)
from hmcpctl.resource_identity import ResourceNotFoundError
from hmcpctl.xmlutil import parse_feed

FIXTURES = Path(__file__).parents[1] / "fixtures" / "pmem"
SYSTEM = "00000001-abcd-4ef0-8abc-000000000001"
LPAR = "00000005-ABCD-4EF0-8ABC-000000000005"
VOLUME = "00000004-ABCD-4EF0-8ABC-000000000004"
COLLECTION = f"/rest/api/uom/ManagedSystem/{SYSTEM}/VirtualPersistentMemoryVolume"


def _volume_entry(**fields: str | None) -> dict[str, Any]:
    entry = parse_feed((FIXTURES / "volume-feed.xml").read_text())[0]
    for field, value in fields.items():
        if value is None:
            entry["Resource"].pop(field, None)
        else:
            entry["Resource"][field] = value
    return entry


def _system(
    capable: str = "true", types: str | None = "dram", block: str | None = "256"
) -> dict[str, Any]:
    configuration: dict[str, Any] = {}
    if types is not None:
        configuration["SupportedPersistentMemoryDeviceTypes"] = types
    if block is not None:
        configuration["DramPersistentMemoryVolumeBlockSize"] = block
    return {
        "Resource": {
            "AssociatedSystemCapabilities": {"PersistentMemoryCapable": capable},
            "AssociatedPersistentMemoryConfiguration": configuration,
        }
    }


def _lpar(
    state: str = "not activated",
    kind: str = "AIX/Linux",
    current: str | None = "0",
    maximum: str | None = "4",
    partition_id: str = "1",
) -> dict[str, Any]:
    configuration: dict[str, Any] = {}
    if current is not None:
        configuration["CurrentDramPersistentMemoryVolumes"] = current
    if maximum is not None:
        configuration["MaximumDramPersistentMemoryVolumes"] = maximum
    return {
        "Resource": {
            "PartitionID": partition_id,
            "PartitionType": kind,
            "PartitionState": state,
            "AssociatedPersistentMemoryConfiguration": configuration,
        }
    }


class FakeHMC:
    """The client boundary: reads answer from state, writes are recorded."""

    def __init__(self, volumes: list[dict[str, Any]] | None = None) -> None:
        self.volumes = list(volumes or [])
        self.system = _system()
        self.lpar = _lpar()
        self.writes: list[tuple[str, Any]] = []
        self.write_error: HMCError | None = None
        self.read_error_after_write: HMCError | None = None
        self.apply_writes = True
        self.get_managed_system = AsyncMock(side_effect=lambda _u: self.system)
        self.get_logical_partition = AsyncMock(side_effect=lambda _u: self.lpar)
        self.created: dict[str, Any] = _volume_entry(
            Uuid="00000051-ABCD-4EF0-8ABC-000000000051", Name="new-vol"
        )

    def _read_guard(self) -> None:
        if self.writes and self.read_error_after_write is not None:
            raise self.read_error_after_write

    async def list_persistent_memory_volumes(
        self, system_uuid: str, partition_id: int | None = None
    ) -> list[dict[str, Any]]:
        self._read_guard()
        return list(self.volumes)

    async def get_persistent_memory_volume(
        self, system_uuid: str, volume_uuid: str
    ) -> dict[str, Any] | None:
        self._read_guard()
        return next(
            (v for v in self.volumes if v["Resource"]["Uuid"]["text"] == volume_uuid),
            None,
        )

    async def create_persistent_memory_volume(self, system_uuid: str, xml: str):
        self.writes.append(("create", xml))
        if self.write_error:
            raise self.write_error
        if self.apply_writes:
            self.volumes.append(self.created)
        return self.created

    async def update_persistent_memory_volume(self, system_uuid, volume_uuid, xml):
        self.writes.append(("update", xml))
        if self.write_error:
            raise self.write_error
        if self.apply_writes:
            name = xml.split("<Name", 1)[1].split(">", 1)[1].split("<", 1)[0]
            self.volumes = [_volume_entry(Name=name)]

    async def delete_persistent_memory_volume(self, system_uuid, volume_uuid):
        self.writes.append(("delete", volume_uuid))
        if self.write_error:
            raise self.write_error
        if self.apply_writes:
            self.volumes = []


@pytest.fixture
def guard():
    with patch.object(
        persistent,
        "resolve_and_authorize_lpar_mutation",
        AsyncMock(return_value=LPAR),
    ) as mock:
        yield mock


# -- client paths -------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_lists_target_query_and_reads_empty_collection(mock_hmc):
    feed = (FIXTURES / "volume-feed.xml").read_text()
    targeted = mock_hmc.get(COLLECTION, params={"target": "1"}).mock(
        return_value=httpx.Response(200, text=feed)
    )
    empty = mock_hmc.get(COLLECTION, params={}).mock(return_value=httpx.Response(204))
    async with HMCClient(make_config()) as client:
        assert len(await client.list_persistent_memory_volumes(SYSTEM, 1)) == 1
        assert await client.list_persistent_memory_volumes(SYSTEM) == []
    assert targeted.called and empty.called


@pytest.mark.asyncio
async def test_client_rejects_negative_partition_id(mock_hmc):
    async with HMCClient(make_config()) as client:
        with pytest.raises(ValueError, match="partition_id"):
            await client.list_persistent_memory_volumes(SYSTEM, -1)


@pytest.mark.asyncio
async def test_client_writes_use_adr_0178_headers(mock_hmc):
    entry = (FIXTURES / "volume-entry.xml").read_text()
    put = mock_hmc.put(COLLECTION).mock(return_value=httpx.Response(200, text=entry))
    post = mock_hmc.post(f"{COLLECTION}/{VOLUME}").mock(
        return_value=httpx.Response(200, text=entry)
    )
    delete = mock_hmc.delete(f"{COLLECTION}/{VOLUME}").mock(
        return_value=httpx.Response(204)
    )
    get = mock_hmc.get(f"{COLLECTION}/{VOLUME}").mock(
        return_value=httpx.Response(200, text=entry)
    )
    async with HMCClient(make_config()) as client:
        created = await client.create_persistent_memory_volume(SYSTEM, "<x/>")
        await client.update_persistent_memory_volume(SYSTEM, VOLUME, "<x/>")
        await client.delete_persistent_memory_volume(SYSTEM, VOLUME)
        read = await client.get_persistent_memory_volume(SYSTEM, VOLUME)
    assert created["UUID"] == "00000051-ABCD-4EF0-8ABC-000000000051"
    assert read["UUID"] == created["UUID"]
    for route in (put, post):
        headers = route.calls.last.request.headers
        assert headers["Accept"] == "*/*"
        assert headers["Content-Type"].endswith("type=VirtualPersistentMemoryVolume")
    assert delete.called and get.called


@pytest.mark.asyncio
async def test_client_refuses_non_uuid_volume(mock_hmc):
    async with HMCClient(make_config()) as client:
        with pytest.raises(ValueError):
            await client.delete_persistent_memory_volume(SYSTEM, "../x")


# -- reads --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_by_partition_returns_its_volumes():
    hmc = FakeHMC([_volume_entry()])
    with patch.object(persistent, "resolve_lpar_uuid", AsyncMock(return_value=LPAR)):
        volumes = await list_persistent_memory_volumes(hmc, SYSTEM, "example-lpar")
    assert [v.name for v in volumes] == ["example-vol"]


@pytest.mark.asyncio
async def test_list_by_partition_rejects_foreign_volume():
    hmc = FakeHMC([_volume_entry(AssociatedPartitionId="9")])
    with (
        patch.object(persistent, "resolve_lpar_uuid", AsyncMock(return_value=LPAR)),
        pytest.raises(ValueError, match="assigned elsewhere"),
    ):
        await list_persistent_memory_volumes(hmc, SYSTEM, "example-lpar")


@pytest.mark.asyncio
async def test_list_all_volumes():
    hmc = FakeHMC([_volume_entry()])
    assert len(await list_persistent_memory_volumes(hmc, SYSTEM)) == 1


@pytest.mark.asyncio
async def test_get_by_name_and_uuid():
    hmc = FakeHMC([_volume_entry()])
    by_name = await get_persistent_memory_volume(hmc, SYSTEM, "example-vol")
    by_uuid = await get_persistent_memory_volume(hmc, SYSTEM, VOLUME)
    assert by_name == by_uuid


@pytest.mark.asyncio
async def test_get_unknown_uuid_and_name_raise_not_found():
    hmc = FakeHMC([])
    with pytest.raises(ResourceNotFoundError):
        await get_persistent_memory_volume(hmc, SYSTEM, VOLUME)
    with pytest.raises(ResourceNotFoundError):
        await get_persistent_memory_volume(hmc, SYSTEM, "absent")


@pytest.mark.asyncio
async def test_get_ambiguous_name_raises():
    hmc = FakeHMC(
        [_volume_entry(), _volume_entry(Uuid="00000009-ABCD-4EF0-8ABC-000000000009")]
    )
    with pytest.raises(ValueError, match="matches 2 volumes"):
        await get_persistent_memory_volume(hmc, SYSTEM, "example-vol")


# -- create -------------------------------------------------------------------


def _refusal_cases() -> list[Any]:
    return [
        pytest.param(
            {"system": _system(capable="false")},
            "PersistentMemoryCapable",
            id="incapable",
        ),
        pytest.param({"system": _system(types="pmem")}, "dram", id="no-dram"),
        pytest.param({"system": _system(block=None)}, "BlockSize", id="block-absent"),
        pytest.param({"system": _system(block="0")}, "BlockSize", id="block-zero"),
        pytest.param({"size_mib": 100}, "multiple", id="misaligned"),
        pytest.param({"size_mib": 0}, "positive", id="zero"),
        pytest.param({"lpar": _lpar(state="running")}, "inactive", id="active"),
        pytest.param({"lpar": _lpar(kind="Virtual IO Server")}, "AIX/Linux", id="vios"),
        pytest.param({"lpar": _lpar(current="4")}, "4 of its 4", id="at-maximum"),
        pytest.param({"lpar": _lpar(maximum=None)}, "Maximum", id="maximum-absent"),
        pytest.param({"name": "example-vol"}, "already exists", id="name-in-use"),
        pytest.param({"name": "bad name"}, "1-64", id="bad-name"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "message"), _refusal_cases())
async def test_create_refuses_without_writing(guard, change, message):
    hmc = FakeHMC([_volume_entry()])
    hmc.system = change.get("system", hmc.system)
    hmc.lpar = change.get("lpar", hmc.lpar)
    with pytest.raises(ValueError, match=message):
        await create_persistent_memory_volume(
            hmc,
            SYSTEM,
            "example-lpar",
            change.get("name", "new-vol"),
            change.get("size_mib", 256),
        )
    assert hmc.writes == []


@pytest.mark.asyncio
async def test_create_rejects_non_boolean_affinity(guard):
    with pytest.raises(TypeError, match="affinity"):
        await create_persistent_memory_volume(
            FakeHMC(), SYSTEM, "lp", "new-vol", 256, affinity=1
        )
    guard.assert_not_called()


@pytest.mark.asyncio
async def test_create_verified_sends_captured_document(guard):
    hmc = FakeHMC()
    result = await create_persistent_memory_volume(
        hmc, SYSTEM, "example-lpar", "new-vol", 256
    )
    assert result.status == "verified"
    assert result.observed is not None and result.observed.name == "new-vol"
    ((kind, document),) = hmc.writes
    assert kind == "create"
    assert (
        "<AssociatedPartitionId" in document
        and ">1</AssociatedPartitionId>" in document
    )
    guard.assert_awaited_once_with(
        hmc, "example-lpar", system_name_or_uuid=SYSTEM, ownership_override=False
    )


@pytest.mark.asyncio
async def test_create_mismatch_when_not_yet_visible(guard):
    hmc = FakeHMC()
    hmc.apply_writes = False
    result = await create_persistent_memory_volume(hmc, SYSTEM, "lp", "new-vol", 256)
    assert result.status == "mismatch"
    assert "re-read" in result.warnings[-1]


@pytest.mark.asyncio
async def test_create_mismatch_on_differing_readback(guard):
    hmc = FakeHMC()
    hmc.created = _volume_entry(Name="new-vol", Size="512")
    result = await create_persistent_memory_volume(hmc, SYSTEM, "lp", "new-vol", 256)
    assert result.status == "mismatch"
    assert "size_mib" in result.warnings[0]


@pytest.mark.asyncio
async def test_create_readback_unavailable(guard):
    hmc = FakeHMC()
    hmc.read_error_after_write = HMCError("GET failed", 503, "")
    result = await create_persistent_memory_volume(hmc, SYSTEM, "lp", "new-vol", 256)
    assert result.status == "readback-unavailable"


@pytest.mark.asyncio
async def test_create_write_outcome_unknown_reports_readback(guard):
    hmc = FakeHMC()
    hmc.write_error = HMCError("PUT failed", 500, "")
    result = await create_persistent_memory_volume(hmc, SYSTEM, "lp", "new-vol", 256)
    assert result.status == "write-outcome-unknown"
    assert result.observed is None
    assert "no replay" in result.warnings[0]


# -- rename -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_verified_resends_size_and_partition(guard):
    hmc = FakeHMC([_volume_entry()])
    result = await rename_persistent_memory_volume(
        hmc, SYSTEM, "lp", "example-vol", "renamed"
    )
    assert result.status == "verified"
    ((kind, document),) = hmc.writes
    assert kind == "update"
    assert ">256</Size>" in document and ">1</AssociatedPartitionId>" in document


@pytest.mark.asyncio
async def test_rename_mismatch_when_name_unchanged(guard):
    hmc = FakeHMC([_volume_entry()])
    hmc.apply_writes = False
    result = await rename_persistent_memory_volume(hmc, SYSTEM, "lp", VOLUME, "renamed")
    assert result.status == "mismatch"


@pytest.mark.asyncio
async def test_rename_write_error_is_outcome_unknown(guard):
    hmc = FakeHMC([_volume_entry()])
    hmc.write_error = HMCError("POST failed", 500, "")
    result = await rename_persistent_memory_volume(hmc, SYSTEM, "lp", VOLUME, "renamed")
    assert result.status == "write-outcome-unknown"
    assert result.observed is not None and result.observed.name == "example-vol"


@pytest.mark.asyncio
async def test_rename_readback_unavailable(guard):
    hmc = FakeHMC([_volume_entry()])
    hmc.read_error_after_write = HMCError("GET failed", 503, "")
    result = await rename_persistent_memory_volume(hmc, SYSTEM, "lp", VOLUME, "renamed")
    assert result.status == "readback-unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "new_name", "message"),
    [
        pytest.param(
            {"AssociatedPartitionId": "9"},
            "renamed",
            "not the selected",
            id="other-partition",
        ),
        pytest.param(
            {"AssociatedPartitionId": None}, "renamed", "not assigned", id="unassigned"
        ),
        pytest.param({}, "example-vol", "already named", id="same-name"),
        pytest.param({}, "a b", "1-64", id="bad-name"),
    ],
)
async def test_rename_refuses_without_writing(guard, entry, new_name, message):
    hmc = FakeHMC([_volume_entry(**entry)])
    with pytest.raises(ValueError, match=message):
        await rename_persistent_memory_volume(hmc, SYSTEM, "lp", VOLUME, new_name)
    assert hmc.writes == []


@pytest.mark.asyncio
async def test_rename_refuses_name_in_use(guard):
    other = _volume_entry(Uuid="00000009-ABCD-4EF0-8ABC-000000000009", Name="taken")
    hmc = FakeHMC([_volume_entry(), other])
    with pytest.raises(ValueError, match="already exists"):
        await rename_persistent_memory_volume(hmc, SYSTEM, "lp", VOLUME, "taken")
    assert hmc.writes == []


# -- delete -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_requires_acknowledgement(guard):
    hmc = FakeHMC([_volume_entry()])
    with pytest.raises(ValueError, match="acknowledge_data_loss"):
        await delete_persistent_memory_volume(hmc, SYSTEM, "lp", VOLUME)
    guard.assert_not_called()
    assert hmc.writes == []


@pytest.mark.asyncio
async def test_delete_refuses_active_partition(guard):
    hmc = FakeHMC([_volume_entry()])
    hmc.lpar = _lpar(state="running")
    with pytest.raises(ValueError, match="inactive"):
        await delete_persistent_memory_volume(
            hmc, SYSTEM, "lp", VOLUME, acknowledge_data_loss=True
        )
    assert hmc.writes == []


@pytest.mark.asyncio
async def test_delete_refuses_volume_of_other_partition(guard):
    hmc = FakeHMC([_volume_entry(AssociatedPartitionId="9")])
    with pytest.raises(ValueError, match="not the selected"):
        await delete_persistent_memory_volume(
            hmc, SYSTEM, "lp", VOLUME, acknowledge_data_loss=True
        )
    assert hmc.writes == []


@pytest.mark.asyncio
async def test_delete_verified_absent(guard):
    hmc = FakeHMC([_volume_entry()])
    result = await delete_persistent_memory_volume(
        hmc, SYSTEM, "lp", "example-vol", acknowledge_data_loss=True
    )
    assert (result.status, hmc.writes) == ("verified", [("delete", VOLUME)])


@pytest.mark.asyncio
async def test_delete_mismatch_when_still_present(guard):
    hmc = FakeHMC([_volume_entry()])
    hmc.apply_writes = False
    result = await delete_persistent_memory_volume(
        hmc, SYSTEM, "lp", VOLUME, acknowledge_data_loss=True
    )
    assert result.status == "mismatch"
    assert isinstance(result.observed, PersistentMemoryVolume)


@pytest.mark.asyncio
async def test_delete_write_error_and_unreadable(guard):
    hmc = FakeHMC([_volume_entry()])
    hmc.write_error = HMCError("DELETE failed", 500, "")
    unknown = await delete_persistent_memory_volume(
        hmc, SYSTEM, "lp", VOLUME, acknowledge_data_loss=True
    )
    assert unknown.status == "write-outcome-unknown"
    assert unknown.observed is not None
    hmc = FakeHMC([_volume_entry()])
    hmc.read_error_after_write = HMCError("GET failed", 503, "")
    unreadable = await delete_persistent_memory_volume(
        hmc, SYSTEM, "lp", VOLUME, acknowledge_data_loss=True
    )
    assert unreadable.status == "readback-unavailable"


# -- fail-closed reads and doubly failed writes ---------------------------------


@pytest.mark.asyncio
async def test_create_refuses_unreadable_partition_and_counts(guard):
    hmc = FakeHMC()
    hmc.lpar = None
    with pytest.raises(ValueError, match="PartitionID"):
        await create_persistent_memory_volume(hmc, SYSTEM, "lp", "new-vol", 256)
    hmc.lpar = _lpar(current=None)
    with pytest.raises(ValueError, match="CurrentDram"):
        await create_persistent_memory_volume(hmc, SYSTEM, "lp", "new-vol", 256)
    assert hmc.writes == []


@pytest.mark.asyncio
async def test_volume_selector_must_be_a_non_empty_string(guard):
    with pytest.raises(TypeError, match="volume"):
        await get_persistent_memory_volume(FakeHMC(), SYSTEM, "")
    with pytest.raises(TypeError, match="volume"):
        await rename_persistent_memory_volume(FakeHMC(), SYSTEM, "lp", "", "renamed")


@pytest.mark.asyncio
async def test_failed_write_with_failed_readback_reports_no_observation(guard):
    error = HMCError("refused", 500, "")
    created = FakeHMC()
    created.write_error = created.read_error_after_write = error
    renamed = FakeHMC([_volume_entry()])
    renamed.write_error = renamed.read_error_after_write = error
    deleted = FakeHMC([_volume_entry()])
    deleted.write_error = deleted.read_error_after_write = error
    results = [
        await create_persistent_memory_volume(created, SYSTEM, "lp", "new-vol", 256),
        await rename_persistent_memory_volume(renamed, SYSTEM, "lp", VOLUME, "x"),
        await delete_persistent_memory_volume(
            deleted, SYSTEM, "lp", VOLUME, acknowledge_data_loss=True
        ),
    ]
    assert [(r.status, r.observed) for r in results] == [
        ("write-outcome-unknown", None)
    ] * 3
    assert all("also failed" in r.warnings[-1] for r in results)


@pytest.mark.asyncio
async def test_failed_delete_reports_whether_the_volume_is_gone(guard):
    gone = FakeHMC([_volume_entry()])

    async def delete_then_fail(_system: str, _volume: str) -> None:
        gone.volumes = []
        raise HMCError("DELETE failed", 500, "")

    gone.delete_persistent_memory_volume = delete_then_fail
    result = await delete_persistent_memory_volume(
        gone, SYSTEM, "lp", VOLUME.lower(), acknowledge_data_loss=True
    )
    assert result.status == "write-outcome-unknown"
    assert "no longer lists" in result.warnings[-1]


@pytest.mark.asyncio
async def test_failed_create_and_rename_report_their_readback(guard):
    created = FakeHMC()
    created.write_error = HMCError("PUT failed", 500, "")
    renamed = FakeHMC([_volume_entry()])
    renamed.write_error = HMCError("POST failed", 500, "")
    create = await create_persistent_memory_volume(
        created, SYSTEM, "lp", "new-vol", 256
    )
    rename = await rename_persistent_memory_volume(renamed, SYSTEM, "lp", VOLUME, "x")
    assert "0 volume(s)" in create.warnings[-1]
    assert "'example-vol'" in rename.warnings[-1]
