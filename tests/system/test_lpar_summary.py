"""Tests for the hmc_lpar_summary composite tool.

Exercises the tool against a mocked HMC (respx) so the URL mapping and
field-extraction logic in server_tools/composite.py is verified without a live HMC.
"""

from __future__ import annotations

from dataclasses import asdict

import httpx
import pytest
from conftest import captured_lpar_entry, mock_system_summaries

from hmcpctl.operations.inventory.composite import _lpar_summary
from hmcpctl.server_tools.inventory.composite import hmc_lpar_summary

LPAR_UUID = "aabbccdd-1234-5678-abcd-000000000001"
ADAPTER1_UUID = "aabbccdd-1234-5678-abcd-000000000002"
ADAPTER2_UUID = "aabbccdd-1234-5678-abcd-000000000003"

NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"


def _hmc_env(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def _lpar_feed(raw_xml: str = "", **fields: str) -> str:
    body = "\n".join(f'        <{k} xmlns="{NS}">{v}</{k}>' for k, v in fields.items())
    body += raw_xml
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{LPAR_UUID}</id>
    <title>LogicalPartition:{LPAR_UUID}</title>
    <link rel="SELF" href="https://hmc.test:12443/rest/api/uom/LogicalPartition/{LPAR_UUID}"/>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="{NS}">
{body}
      </LogicalPartition>
    </content>
  </entry>
</feed>
"""


def _adapter_feed(uuids: list[str]) -> str:
    entries = []
    for uuid in uuids:
        entries.append(f"""  <entry>
    <id>urn:uuid:{uuid}</id>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <ClientNetworkAdapter xmlns="{NS}">
        <AdapterID>1</AdapterID>
      </ClientNetworkAdapter>
    </content>
  </entry>""")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
{"".join(entries)}
</feed>
"""


EMPTY_FEED = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom"/>
"""

EMPTY_ADAPTER_FEED = EMPTY_FEED


# Parsed V10R3 shapes, captured live from a running dedicated partition (#1183).
RUNNING_DEDICATED = {
    "PartitionMemoryConfiguration": {
        "DesiredMemory": "20480",
        "MaximumMemory": "20480",
        "MinimumMemory": "15360",
        "CurrentMaximumMemory": "20480",
        "CurrentMemory": "20480",
        "CurrentMinimumMemory": "15360",
        "RuntimeMemory": "20480",
        "RuntimeMinimumMemory": "15360",
    },
    "PartitionProcessorConfiguration": {
        "HasDedicatedProcessors": "true",
        "SharingMode": "keep idle procs",
        "CurrentHasDedicatedProcessors": "true",
        "CurrentSharingMode": "keep idle procs",
        "RuntimeHasDedicatedProcessors": "true",
        "SharedProcessorConfiguration": {},
        "DedicatedProcessorConfiguration": {
            "DesiredProcessors": "2",
            "MaximumProcessors": "3",
            "MinimumProcessors": "2",
        },
        "CurrentDedicatedProcessorConfiguration": {
            "CurrentMaximumProcessors": "3",
            "CurrentMinimumProcessors": "2",
            "CurrentProcessors": "2",
            "RunProcessors": "2",
        },
    },
    "OperatingSystemType": {"@attrs": {"ksv": "V1_8_0"}, "text": "Linux"},
    "OperatingSystemVersion": "Unknown",
}

# A REST-created 0.5-unit capped shared partition (#1161 P36, P37).
RUNNING_SHARED = {
    "PartitionMemoryConfiguration": {"DesiredMemory": "2048", "CurrentMemory": "2048"},
    "PartitionProcessorConfiguration": {
        "HasDedicatedProcessors": "false",
        "CurrentSharingMode": "capped",
        "SharedProcessorConfiguration": {
            "DesiredProcessingUnits": "0.5",
            "DesiredVirtualProcessors": "1",
            "MaximumProcessingUnits": "1",
        },
        "CurrentSharedProcessorConfiguration": {
            "CurrentProcessingUnits": "0.5",
            "AllocatedVirtualProcessors": "1",
        },
    },
}


def test_lpar_summary_reads_dedicated_partition_containers():
    summary = _lpar_summary({"Resource": RUNNING_DEDICATED}, [])
    assert summary.current_memory_mib == "20480"
    assert summary.desired_memory_mib == "20480"
    assert summary.current_proc_units == "2"
    assert summary.desired_proc_units == "2"
    assert summary.desired_vcpus is None
    assert summary.has_dedicated_processors is True
    serialized = asdict(summary)
    assert serialized["has_dedicated_processors"] is True
    assert "dedicated_procs" not in serialized
    assert summary.os_type == "Linux"
    assert summary.os_version == "Unknown"


def test_lpar_summary_reads_shared_partition_containers():
    summary = _lpar_summary({"Resource": RUNNING_SHARED}, [])
    assert summary.current_memory_mib == "2048"
    assert summary.current_proc_units == "0.5"
    assert summary.desired_proc_units == "0.5"
    assert summary.desired_vcpus == "1"
    assert summary.has_dedicated_processors is False
    serialized = asdict(summary)
    assert serialized["has_dedicated_processors"] is False
    assert "dedicated_procs" not in serialized


def test_lpar_summary_keeps_inactive_zeros_and_reports_missing_as_none():
    inactive = {
        "PartitionMemoryConfiguration": {"CurrentMemory": "0", "DesiredMemory": "0"},
        "PartitionProcessorConfiguration": {
            "HasDedicatedProcessors": "false",
            "CurrentSharedProcessorConfiguration": {"CurrentProcessingUnits": "0"},
            "SharedProcessorConfiguration": {"DesiredProcessingUnits": "0"},
        },
    }
    summary = _lpar_summary({"Resource": inactive}, [])
    assert summary.current_memory_mib == "0"
    assert summary.current_proc_units == "0"
    assert summary.desired_proc_units == "0"

    bare = _lpar_summary({"Resource": {}}, [])
    assert bare.current_memory_mib is None
    assert bare.desired_memory_mib is None
    assert bare.current_proc_units is None
    assert bare.desired_proc_units is None
    assert bare.has_dedicated_processors is None
    serialized = asdict(bare)
    assert serialized["has_dedicated_processors"] is None
    assert "dedicated_procs" not in serialized


def test_lpar_summary_reads_text_of_attributed_description():
    """V10R3 sends ``<Description ksv=...>``, which parses as a mapping (#1168)."""
    description = {"@attrs": {"ksv": "V1_2_0"}, "text": "Production LPAR"}
    summary = _lpar_summary({"Resource": {"Description": description}}, [])
    assert summary.description == "Production LPAR"


def test_lpar_summary_of_captured_inactive_partition_reads_zeros(monkeypatch, mock_hmc):
    """The captured not-activated entry reads 0 in every container (#1161 P33)."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<feed xmlns="http://www.w3.org/2005/Atom">\n'
                f"{captured_lpar_entry(LPAR_UUID, 'idle-lpar')}\n</feed>"
            ),
        )
    )
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/ClientNetworkAdapter"
    ).mock(return_value=httpx.Response(200, text=EMPTY_ADAPTER_FEED))

    result = hmc_lpar_summary(LPAR_UUID)

    assert result.current_memory_mib == "0"
    assert result.desired_memory_mib == "0"
    assert result.desired_proc_units == "0"
    assert result.current_proc_units is None
    assert result.has_dedicated_processors is None


# ---------------------------------------------------------------------- #
# Core happy-path
# ---------------------------------------------------------------------- #


def test_lpar_summary_by_uuid_returns_flat_dict(monkeypatch, mock_hmc):
    """hmc_lpar_summary(uuid) fetches LPAR + adapters and returns summary."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(
            200,
            text=_lpar_feed(
                PartitionName="aix-prod",
                PartitionState="running",
                ResourceMonitoringControlState="active",
                PartitionType="AIX/Linux",
                PartitionID="3",
                raw_xml=f"""
        <PartitionMemoryConfiguration xmlns="{NS}">
          <DesiredMemory>8192</DesiredMemory>
          <CurrentMemory>4096</CurrentMemory>
        </PartitionMemoryConfiguration>
        <PartitionProcessorConfiguration xmlns="{NS}">
          <HasDedicatedProcessors>false</HasDedicatedProcessors>
          <SharedProcessorConfiguration>
            <DesiredProcessingUnits>1.0</DesiredProcessingUnits>
            <DesiredVirtualProcessors>2</DesiredVirtualProcessors>
          </SharedProcessorConfiguration>
          <CurrentSharedProcessorConfiguration>
            <CurrentProcessingUnits>0.5</CurrentProcessingUnits>
          </CurrentSharedProcessorConfiguration>
        </PartitionProcessorConfiguration>
        <OperatingSystemType xmlns="{NS}" ksv="V1_8_0">AIX</OperatingSystemType>""",
                OperatingSystemVersion="AIX 7.2",
                Description="Production LPAR",
            ),
        )
    )
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/ClientNetworkAdapter"
    ).mock(
        return_value=httpx.Response(
            200, text=_adapter_feed([ADAPTER1_UUID, ADAPTER2_UUID])
        )
    )

    result = hmc_lpar_summary(LPAR_UUID)

    assert result.uuid == LPAR_UUID
    assert result.name == "aix-prod"
    assert result.state == "running"
    assert result.rmc_state == "active"
    assert result.partition_type == "AIX/Linux"
    assert result.partition_id == "3"
    assert result.desired_memory_mib == "8192"
    assert result.current_memory_mib == "4096"
    assert result.current_proc_units == "0.5"
    assert result.has_dedicated_processors is False
    assert result.desired_proc_units == "1.0"
    assert result.desired_vcpus == "2"
    assert result.os_version == "AIX 7.2"
    assert result.os_type == "AIX"
    assert result.client_network_adapter_count == 2
    assert result.description == "Production LPAR"
    assert result.mapped_storage is None


def test_lpar_summary_no_adapters(monkeypatch, mock_hmc):
    """hmc_lpar_summary returns adapter_count=0 when the adapter feed is empty."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(
            200,
            text=_lpar_feed(PartitionName="minimal", PartitionState="not activated"),
        )
    )
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/ClientNetworkAdapter"
    ).mock(return_value=httpx.Response(200, text=EMPTY_ADAPTER_FEED))

    result = hmc_lpar_summary(LPAR_UUID)

    assert result.client_network_adapter_count == 0
    assert result.state == "not activated"


def test_lpar_summary_by_name_resolves_uuid(monkeypatch, mock_hmc):
    """hmc_lpar_summary('myname') resolves via search then fetches summary."""
    _hmc_env(monkeypatch)
    # Name resolution: search → UUID
    search_feed = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{LPAR_UUID}</id>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="{NS}">
        <PartitionName>myname</PartitionName>
      </LogicalPartition>
    </content>
  </entry>
</feed>
"""
    mock_hmc.get("/rest/api/uom/LogicalPartition/search/(PartitionName==myname)").mock(
        return_value=httpx.Response(200, text=search_feed)
    )

    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(
            200,
            text=_lpar_feed(PartitionName="myname", PartitionState="running"),
        )
    )
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/ClientNetworkAdapter"
    ).mock(return_value=httpx.Response(200, text=EMPTY_ADAPTER_FEED))

    result = hmc_lpar_summary("myname")

    assert result.name == "myname"
    assert result.state == "running"
    assert result.uuid == LPAR_UUID


def test_lpar_summary_name_not_found_raises(monkeypatch, mock_hmc):
    """hmc_lpar_summary raises ValueError when the partition name is unknown."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/LogicalPartition/search/(PartitionName==ghost)").mock(
        return_value=httpx.Response(200, text=EMPTY_FEED)
    )
    mock_system_summaries(
        mock_hmc, ("22222222-2222-4222-8222-222222222222", "sys-E2", "operating")
    )

    with pytest.raises(ValueError, match="ghost"):
        hmc_lpar_summary("ghost")


def test_lpar_summary_missing_optional_fields(monkeypatch, mock_hmc):
    """hmc_lpar_summary tolerates an LPAR entry that is missing optional fields."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(
            200, text=_lpar_feed(PartitionName="bare", PartitionState="not activated")
        )
    )
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/ClientNetworkAdapter"
    ).mock(return_value=httpx.Response(200, text=EMPTY_ADAPTER_FEED))

    result = hmc_lpar_summary(LPAR_UUID)

    assert result.name == "bare"
    assert result.os_version is None
    assert result.description is None
    assert result.rmc_state is None
    assert result.mapped_storage is None
