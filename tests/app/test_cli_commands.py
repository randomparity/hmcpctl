"""Direct CLI command tests via ``typer.testing.CliRunner``.

The REST-backed commands all funnel through ``cli_runtime.client`` and the SSH-backed
commands through ``ssh_commands.run_hmc_command``. These tests monkeypatch those
two boundaries so every command runs against a scripted fake — no HTTP, no SSH.

This closes the CLI blind spot where only the helpers (``ssh_config``,
``output``) were exercised and no command body was ever invoked, so the
highest-complexity commands (lpars power/create/modify/delete, storage
create-vg/create-disk) had zero direct coverage.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
import xml.etree.ElementTree as ET  # nosec B405 - parses a literal test partition
from typing import Self
from unittest.mock import AsyncMock

import click
import httpx
import pytest
import typer
from click import unstyle
from conftest import live_fixture
from rendered_output import collapse_whitespace
from typer.main import get_command
from typer.testing import CliRunner

from hmcpctl import cli
from hmcpctl.cli_commands import app as cli_command_app
from hmcpctl.cli_commands import metrics as cli_metrics
from hmcpctl.cli_commands import runtime as cli_runtime
from hmcpctl.cli_commands import snapshot as cli_snapshot
from hmcpctl.cli_commands.lpar import config as cli_lpars
from hmcpctl.cli_commands.lpar import decommission as cli_lpar_decommission
from hmcpctl.cli_commands.lpar import inventory as cli_lpar_inventory
from hmcpctl.cli_commands.lpar import lifecycle as cli_lpar_lifecycle
from hmcpctl.cli_commands.lpar import migration as cli_lpar_migration
from hmcpctl.cli_commands.lpar import modify as cli_lpar_modify
from hmcpctl.cli_commands.lpar import provision as cli_lpar_provision
from hmcpctl.cli_commands.storage import cluster as cli_storage_cluster
from hmcpctl.cli_commands.storage import resources as cli_storage_resources
from hmcpctl.cli_commands.vios import core as cli_vios_core
from hmcpctl.cli_commands.vios import labels as cli_vios_labels
from hmcpctl.cli_commands.virtualization import pcie as cli_pcie
from hmcpctl.cli_commands.virtualization import vnic as cli_vnic
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.operations.inventory.composite import _lpar_summary
from hmcpctl.operations.lpar import ownership as lpar_ownership
from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
from hmcpctl.operations.lpar.migration import LpmResult
from hmcpctl.operations.lpar.profile_sync import ChangeLocation
from hmcpctl.operations.lpar.provision import AttachDiskResult, ProvisionResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.operations.storage.resources import (
    OpticalMedia,
    StorageMapping,
    StorageMapResult,
    VolumeGroup,
)
from hmcpctl.operations.virtualization.adapters import AdapterResult
from hmcpctl.operations.virtualization.vnic import VnicChangeResult, VnicPartialError
from hmcpctl.ssh import affinity as ssh_affinity
from hmcpctl.ssh import commands as ssh_commands
from hmcpctl.ssh import io_inventory, sriov, vnic
from hmcpctl.ssh import lpar as ssh_lpar
from hmcpctl.ssh import profiles as ssh_profiles
from hmcpctl.ssh import refcodes as ssh_refcodes
from hmcpctl.xmlutil import parse_feed


@pytest.fixture
def storage_terminal_input(monkeypatch):
    """Prompt tests represent terminal stdin; real pipes are tested separately."""
    import typer.testing

    monkeypatch.setattr(
        typer.testing._NamedTextIOWrapper, "isatty", lambda self: self.name == "<stdin>"
    )


LPAR_NAME = "lpar1"

# Shaped like the ownership stamp ``hmcpctl.ssh.lpar`` writes -- a bracketed
# string an unescaped ``console.print`` interprets as Rich markup and drops.
# See #965.
OWNERSHIP_STAMP = "[hmcpctl owner:ops created:2026-09-23] [caller ci-token]"


def _patch_ssh_command(monkeypatch, replacement) -> None:
    """Replace the transport name owned by every SSH domain used by the CLI."""
    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "hscroot",
            "password": "test",  # pragma: allowlist secret
        }
    )
    for module in (cli_lpars, cli_lpar_inventory, cli_pcie, cli_vnic):
        monkeypatch.setattr(module, "ssh_config", lambda: config, raising=False)
    for module in (
        ssh_affinity,
        ssh_lpar,
        io_inventory,
        ssh_refcodes,
        sriov,
        vnic,
        ssh_profiles,
    ):
        monkeypatch.setattr(module, "run_hmc_command", replacement)


LPAR_UUID = "11111111-1111-4111-8111-111111111111"
PARTITION_PROFILE_UUID = "00000000-0000-0000-0000-0000000000aa"
SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"
UOM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
VG_UUID = "33333333-3333-4333-8333-333333333333"
VIOS_UUID = "44444444-4444-4444-8444-444444444444"
JOB_UUID = "55555555-5555-4555-8555-555555555555"
CLUSTER_UUID = "77777777-7777-4777-8777-777777777777"
SSP_UUID = "88888888-8888-4888-8888-888888888888"
TEMPLATE_UUID = "99999999-9999-4999-8999-999999999999"

RUNNER = CliRunner()


@pytest.mark.parametrize(
    "command",
    (
        cli_lpars.lpars_set_description,
        cli_lpar_decommission.lpars_decommission,
        cli_lpar_modify.lpars_modify,
        cli_lpar_provision.lpars_provision,
        cli_metrics.metrics_show,
        cli_snapshot.snapshot_capture,
        cli_storage_resources.storage_map,
        cli_vios_core.vios_power_on,
        cli_vios_core.vios_power_off,
        cli_lpar_lifecycle._power_lpar,
        cli_lpar_lifecycle.lpars_delete,
    ),
)
def test_rest_command_delegates_do_not_open_a_local_client(command) -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(command)))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert not {"client", "run_cli_coroutine"} & calls


@pytest.fixture(autouse=True)
def _configured_ssh_config(monkeypatch) -> None:
    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "hscroot",
            "password": "test",  # pragma: allowlist secret
        }
    )
    for module in (cli_lpars, cli_lpar_inventory, cli_pcie, cli_vnic):
        monkeypatch.setattr(module, "ssh_config", lambda: config, raising=False)


UNSYNCED = ChangeLocation("Disabled", "current-configuration")


class FakeHMC:
    """Scripted async-context-manager stand-in for :class:`HMCClient`.

    Each method records its call on ``calls`` (name, args, kwargs) and returns
    canned data; set ``fail_on`` to a method name to make that method raise
    :class:`HMCError`, exercising the CLI's ``fail`` error path.
    """

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.config = HMCConfig.from_mapping({"host": "hmc.test", "user": "user"})
        self.fail_on: str | None = None
        self.fail_status = 500
        self.lpar = {
            "UUID": LPAR_UUID,
            "Resource": {
                "PartitionName": LPAR_NAME,
                "PartitionID": 1,
                "PartitionState": "running",
                "PartitionType": "AIX/Linux",
                "OperatingSystemVersion": "7.2",
                "ResourceMonitoringControlState": "active",
            },
        }
        self.vg = {
            "UUID": VG_UUID,
            "Resource": {
                "GroupName": "rootvg",
                "FreeSpaceInMBytes": "5120",
                "GroupCapacity": "102400",
            },
        }
        self.disk = {
            "UUID": "66666666-6666-4666-8666-666666666666",
            "Resource": {"DiskName": "bootvol"},
        }
        self.job = {
            "UUID": JOB_UUID,
            "link": f"/jobs/{JOB_UUID}",
            "Resource": {"JobName": "PowerOn", "Status": "RUNNING"},
        }
        self.system = {
            "UUID": SYSTEM_UUID,
            "Resource": {
                "SystemName": "sys1",
                "State": "operating",
                "MachineTypeModelAndSerialNumber": {
                    "MachineType": "9119",
                    "Model": "MHE",
                    "SerialNumber": "SN00001",
                },
                "IPAddress": "10.0.0.1",
            },
        }
        self.vios = {
            "UUID": VIOS_UUID,
            "Resource": {
                "PartitionName": "vios1",
                "PartitionID": 2,
                "PartitionState": "running",
                "IOSLevel": "3.1.0",
            },
        }
        # The captured V10R3 ManagementConsole entry (#1202).
        self.console = parse_feed(live_fixture("rest-management-console")["body"])[0]
        self.cluster = {"UUID": CLUSTER_UUID, "Resource": {"ClusterName": "cl1"}}
        self.ssp = {
            "UUID": SSP_UUID,
            "Resource": {
                "StoragePoolName": "pool1",
                "Capacity": "1024",
                "FreeSpace": "512",
            },
        }
        self.template = {
            "UUID": TEMPLATE_UUID,
            "Resource": {"partitionTemplateName": "tpl1"},
        }
        self.vios_storage_detail = {"Resource": {"VirtualSCSIMappings": ""}}
        self.pcm_prefs = {"LongTermMonitorEnabled": True, "AggregationEnabled": False}
        self.metric_links = [
            {
                "link": "/rest/api/pcm/ProcessedMetrics/ManagedSystem_xxx_a_b_30.json",
                "title": "m1",
            }
        ]
        self.metrics_json = {"data": [1, 2, 3]}
        self.fetch_json_404 = False
        self.wait_job_status = "COMPLETED_OK"

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))
        if self.fail_on == name:
            raise HMCError(
                f"simulated {name} failure",
                self.fail_status,
                "<xml><Message>boom</Message></xml>",
            )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def list_logical_partitions(self, system_uuid=None):
        self._record("list_logical_partitions", system_uuid)
        return [self.lpar]

    async def search_uom(self, resource_type, property_name, value):
        self._record("search_uom", resource_type, property_name, value)
        return [self.lpar]

    async def get_logical_partition(self, uuid):
        self._record("get_logical_partition", uuid)
        return self.lpar

    async def find_partition_by_name(self, name, system_uuid=None):
        if system_uuid is None:
            self._record("find_partition_by_name", name)
        else:
            self._record("find_partition_by_name", name, system_uuid=system_uuid)
        return self.lpar if name == LPAR_NAME else None

    async def get_quick_property(self, resource_type, uuid, property_name):
        self._record("get_quick_property", resource_type, uuid, property_name)
        return self.lpar["Resource"]["PartitionState"]

    async def submit_job(self, job_path, job_request_xml):
        self._record("submit_job", job_path, job_request_xml)
        return self.job

    async def list_child(self, parent_type, parent_uuid, child_type):
        # ADR 0039 containment: power-on reads the partition's own profile feed
        # before carrying a caller-supplied LogicalPartitionProfile.
        self._record("list_child", parent_type, parent_uuid, child_type)
        return [{"UUID": PARTITION_PROFILE_UUID}]

    async def create_logical_partition(self, system_uuid, xml):
        self._record("create_logical_partition", system_uuid, xml)
        return self.lpar

    async def update_logical_partition(self, lpar_uuid, updates, subject):
        partition = ET.fromstring(
            f'<LogicalPartition xmlns="{UOM_NS}"><PartitionProcessorConfiguration>'
            "<HasDedicatedProcessors>false</HasDedicatedProcessors>"
            "</PartitionProcessorConfiguration></LogicalPartition>"
        )
        self._record("update_logical_partition", lpar_uuid, updates(partition), subject)
        return self.lpar

    async def delete_logical_partition(self, lpar_uuid):
        self._record("delete_logical_partition", lpar_uuid)

    async def lpar_migrate(
        self, lpar_uuid, target, *, target_profile_name=None, wait_time=None
    ):
        self._record(
            "lpar_migrate",
            lpar_uuid,
            target,
            target_profile_name,
            wait_time=wait_time,
        )
        return self.job

    async def lpar_migrate_validate(
        self, lpar_uuid, target, *, target_profile_name=None, wait_time=None
    ):
        self._record(
            "lpar_migrate_validate",
            lpar_uuid,
            target,
            target_profile_name,
            wait_time=wait_time,
        )
        return self.job

    async def lpar_migrate_abort(self, lpar_uuid):
        self._record("lpar_migrate_abort", lpar_uuid)
        return self.job

    async def lpar_migrate_recover(self, lpar_uuid):
        self._record("lpar_migrate_recover", lpar_uuid)
        return self.job

    async def lpar_remote_restart(self, lpar_uuid, operation, source, **kwargs):
        self._record("lpar_remote_restart", lpar_uuid, operation, source, **kwargs)
        return self.job

    async def create_volume_group(self, vios_uuid, name, physical_volumes):
        self._record("create_volume_group", vios_uuid, name, physical_volumes)
        return self.vg

    async def create_virtual_disk(self, vios_uuid, vg_uuid, disk_name, capacity_mib):
        self._record("create_virtual_disk", vios_uuid, vg_uuid, disk_name, capacity_mib)
        return self.disk

    def _uom_headers(self, kind):
        return {"Accept": kind}

    async def _request_with_uuid_path_arguments(
        self, method, path, *, headers, uuid_path_arguments
    ):
        self._record("_request_with_uuid_path_arguments", method, path)
        kind = path.rsplit("/", 1)[1]
        kinds = (
            "ClientNetworkAdapter",
            "VirtualSCSIClientAdapter",
            "VirtualFibreChannelClientAdapter",
            "VirtualNICDedicated",
        )
        assert kind in kinds and method == "GET"
        assert uuid_path_arguments == {"parent_uuid": LPAR_UUID}
        assert headers == {"Accept": kind}
        atom = "{http://www.w3.org/2005/Atom}"
        uom = "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}"
        root = ET.Element(atom + "feed")
        entry = ET.SubElement(root, atom + "entry")
        ET.SubElement(
            entry, atom + "id"
        ).text = f"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa{kinds.index(kind)}"
        content = ET.SubElement(entry, atom + "content")
        ET.SubElement(content, uom + kind)
        return httpx.Response(200, text=ET.tostring(root, encoding="unicode"))

    async def list_adapters(self, lpar_uuid, adapter_type):
        self._record("list_adapters", lpar_uuid, adapter_type)
        return [{"UUID": "adapter-1", "Resource": {"PortVLANID": "100"}}]

    async def add_network_adapter(
        self,
        lpar_uuid,
        vlan,
        *,
        slot_number=None,
        virtual_switch_id=None,
        tagged=False,
        mac_address=None,
    ):
        self._record(
            "add_network_adapter",
            lpar_uuid,
            vlan,
            slot_number,
            virtual_switch_id,
            tagged,
            mac_address,
        )
        return {"UUID": "adapter-1"}

    async def add_vscsi_adapter(
        self, lpar_uuid, vios_partition_id, vios_slot, slot_number=None
    ):
        self._record(
            "add_vscsi_adapter",
            lpar_uuid,
            vios_partition_id,
            vios_slot,
            slot_number,
        )
        return {"UUID": "vscsi-adapter-1"}

    async def delete_adapter(self, lpar_uuid, adapter_type, adapter_uuid):
        self._record("delete_adapter", lpar_uuid, adapter_type, adapter_uuid)

    async def list_virtual_networks(self, system_uuid):
        self._record("list_virtual_networks", system_uuid)
        return [{"UUID": "network-1", "Resource": {"NetworkName": "prod"}}]

    async def list_virtual_switches(self, system_uuid):
        self._record("list_virtual_switches", system_uuid)
        return [
            {
                "UUID": "switch-1",
                "Resource": {"SwitchID": "2", "SwitchName": "ETHERNET0"},
            }
        ]

    async def list_network_bridges(self, system_uuid):
        self._record("list_network_bridges", system_uuid)
        return [{"UUID": "bridge-1", "Resource": {"PortVLANID": "1"}}]

    async def create_virtual_network(
        self, system_uuid, name, vlan, vswitch, *, switch_uuid, tagged
    ):
        self._record(
            "create_virtual_network",
            system_uuid,
            name,
            vlan,
            vswitch,
            switch_uuid=switch_uuid,
            tagged=tagged,
        )
        return {"UUID": "network-1"}

    async def delete_virtual_network(self, system_uuid, network_uuid):
        self._record("delete_virtual_network", system_uuid, network_uuid)

    async def list_volume_groups(self, vios_uuid):
        self._record("list_volume_groups", vios_uuid)
        return [self.vg]

    async def map_storage_to_lpar(self, vios_uuid, kind, disk, lpar_uuid, target):
        self._record("map_storage_to_lpar", vios_uuid, kind, disk, lpar_uuid, target)
        return {"UUID": "mapping-1"}

    async def create_media_repository(self, vios_uuid, vg_uuid, size_mib):
        self._record("create_media_repository", vios_uuid, vg_uuid, size_mib)
        return {"UUID": "repo-1"}

    async def delete_media_repository(self, vios_uuid, vg_uuid):
        self._record("delete_media_repository", vios_uuid, vg_uuid)

    async def list_optical_media(self, vios_uuid, vg_uuid):
        return []

    async def raw_get(self, path):
        self._record("raw_get", path)
        return "<response/>", {}

    async def raw_post(self, path, body, content_type="application/xml"):
        self._record("raw_post", path, body, content_type)
        return "<ok/>"

    # -- systems --------------------------------------------------------- #
    async def list_system_summaries(self):
        self._record("list_system_summaries")
        resource = self.system["Resource"]
        return [
            {
                "UUID": self.system["UUID"],
                "SystemName": resource.get("SystemName"),
                "State": resource.get("State"),
                "StateDetail": "",
            }
        ]

    async def get_managed_system(self, uuid):
        self._record("get_managed_system", uuid)
        return self.system if uuid == SYSTEM_UUID else None

    async def wait_for_job_entry(
        self,
        job_id,
        timeout_seconds=300,
        poll_interval=5,
        *,
        job_href=None,
    ):
        self._record(
            "wait_for_job_entry",
            job_id,
            timeout_seconds,
            poll_interval,
            job_href=job_href,
        )
        return {
            **self.job,
            "Resource": {**self.job["Resource"], "Status": self.wait_job_status},
        }

    async def power_on_system(self, system_uuid):
        self._record("power_on_system", system_uuid)
        return self.job

    async def power_off_system(self, system_uuid, immediate=False):
        self._record("power_off_system", system_uuid, immediate)
        return self.job

    # -- vios ------------------------------------------------------------ #
    async def list_vios(self, system_uuid=None):
        self._record("list_vios", system_uuid)
        return [self.vios]

    async def get_vios_storage_detail(self, vios_uuid):
        self._record("get_vios_storage_detail", vios_uuid)
        return self.vios_storage_detail

    async def power_on_vios(self, vios_uuid):
        self._record("power_on_vios", vios_uuid)
        return self.job

    async def power_off_vios(self, vios_uuid, immediate=False):
        self._record("power_off_vios", vios_uuid, immediate)
        return self.job

    # -- console --------------------------------------------------------- #
    async def get_console_info(self):
        self._record("get_console_info")
        return self.console

    # -- cluster / SSP --------------------------------------------------- #
    async def list_clusters(self):
        self._record("list_clusters")
        return [self.cluster]

    async def list_shared_storage_pools(self):
        self._record("list_shared_storage_pools")
        return [self.ssp]

    async def create_logical_unit(
        self,
        cluster_uuid,
        lu_name,
        lu_size_gib,
        *,
        lu_type="THIN",
        device_type="VirtualIO_Disk",
        cloned_from=None,
    ):
        self._record(
            "create_logical_unit",
            cluster_uuid,
            lu_name,
            lu_size_gib,
            lu_type,
            device_type,
            cloned_from,
        )
        return self.job

    async def delete_logical_unit(self, cluster_uuid, lu_udid):
        self._record("delete_logical_unit", cluster_uuid, lu_udid)
        return self.job

    # -- templates ------------------------------------------------------- #
    async def list_partition_templates(self):
        self._record("list_partition_templates")
        return [self.template]

    async def get_partition_template(self, template_uuid):
        self._record("get_partition_template", template_uuid)
        if template_uuid != TEMPLATE_UUID:
            raise HMCError("GET partition template failed", 404, "not found")
        return self.template

    async def deploy_partition_template(self, draft_template_uuid, target_system_uuid):
        self._record(
            "deploy_partition_template", draft_template_uuid, target_system_uuid
        )
        return self.job

    # -- jobs ------------------------------------------------------------ #
    async def get_job_entry(self, job_id, *, job_href=None):
        self._record("get_job_entry", job_id, job_href=job_href)
        return self.job if job_id == JOB_UUID else None

    # -- pcm metrics ----------------------------------------------------- #
    async def get_pcm_preferences(self, category, uuid):
        self._record("get_pcm_preferences", category, uuid)
        return self.pcm_prefs

    async def set_pcm_preferences(self, category, uuid, **flags):
        self._record("set_pcm_preferences", category, uuid, **flags)

    async def get_processed_metric_links(
        self,
        category,
        uuid,
        start_ts,
        end_ts=None,
        no_of_samples=None,
        *,
        system_uuid=None,
    ):
        kwargs = {"system_uuid": system_uuid} if system_uuid is not None else {}
        self._record(
            "get_processed_metric_links",
            category,
            uuid,
            start_ts,
            end_ts,
            no_of_samples,
            **kwargs,
        )
        return self.metric_links

    async def get_aggregated_metric_links(
        self,
        category,
        uuid,
        start_ts,
        end_ts=None,
        no_of_samples=None,
        *,
        system_uuid=None,
    ):
        kwargs = {"system_uuid": system_uuid} if system_uuid is not None else {}
        self._record(
            "get_aggregated_metric_links",
            category,
            uuid,
            start_ts,
            end_ts,
            no_of_samples,
            **kwargs,
        )
        return self.metric_links

    async def fetch_json(self, link):
        self._record("fetch_json", link)
        if self.fetch_json_404:
            raise HMCError("GET failed", 404, "not found")
        return self.metrics_json


@pytest.fixture
def fake_hmc(monkeypatch):
    """Wire the CLI's client factory to a scripted FakeHMC for every command."""
    hmc = FakeHMC()
    monkeypatch.setattr(cli_runtime, "HMCClient", lambda _config: hmc)

    async def legacy_description(*_args):
        return "legacy partition"

    async def stamped(*_args, **_kwargs):
        return "[hmcpctl owner:hmcpctl created:2026-08-14]"

    monkeypatch.setattr(lpar_ownership, "get_lpar_description", legacy_description)
    monkeypatch.setattr(lpar_ownership, "stamp_lpar_ownership", stamped)
    return hmc


def test_connection_options_do_not_leak_between_invocations(monkeypatch):
    seen = []
    hmc = FakeHMC()

    def client_factory(config):
        seen.append(config)
        return hmc

    monkeypatch.setattr(cli_runtime, "HMCClient", client_factory)
    # HMC_HOST env var (any casing) and the real config.toml both leak a host
    # value into the second bare invocation. Suppress both.
    for name in tuple(__import__("os").environ):
        if name.casefold() == "hmc_host":
            monkeypatch.delenv(name)
    monkeypatch.setattr("hmcpctl.config.resolve_config_path", lambda: None)

    first = RUNNER.invoke(
        cli.app,
        ["--host", "first-hmc", "--user", "first-user", "lpars", "list"],
    )
    second = RUNNER.invoke(cli.app, ["lpars", "list"])

    assert first.exit_code == second.exit_code == 0
    assert seen[0].host == "first-hmc"
    assert seen[0].user == "first-user"
    assert seen[1].host == ""
    assert seen[1].user == ""


@pytest.mark.parametrize(
    ("args", "expected_call", "output"),
    [
        (
            ["adapters", "list", LPAR_UUID, "--json"],
            ("list_adapters", (LPAR_UUID, "ClientNetworkAdapter"), {}),
            "adapter-1",
        ),
        (
            [
                "adapters",
                "add-network",
                LPAR_UUID,
                "--vlan",
                "100",
                "--slot",
                "4",
                "--yes",
            ],
            (
                "add_network_adapter",
                (LPAR_UUID, 100, 4, None, False, None),
                {},
            ),
            "Added network adapter",
        ),
        (
            [
                "adapters",
                "delete",
                LPAR_UUID,
                "--type",
                "ClientNetworkAdapter",
                "--uuid",
                "adapter-1",
                "--yes",
            ],
            ("delete_adapter", (LPAR_UUID, "ClientNetworkAdapter", "adapter-1"), {}),
            "Deleted ClientNetworkAdapter",
        ),
        (
            ["network", "list-switches", SYSTEM_UUID, "--json"],
            ("list_virtual_switches", (SYSTEM_UUID,), {}),
            "switch-1",
        ),
        (
            ["network", "list-networks", SYSTEM_UUID, "--json"],
            ("list_virtual_networks", (SYSTEM_UUID,), {}),
            "network-1",
        ),
        (
            [
                "network",
                "create",
                SYSTEM_UUID,
                "--name",
                "prod",
                "--vlan",
                "100",
                "--virtual-switch-id",
                "2",
                "--tagged",
                "--yes",
            ],
            (
                "create_virtual_network",
                (SYSTEM_UUID, "prod", 100, 2),
                {"switch_uuid": "switch-1", "tagged": True},
            ),
            "Created virtual network",
        ),
        (
            ["network", "delete", SYSTEM_UUID, "--uuid", "network-1", "--yes"],
            ("delete_virtual_network", (SYSTEM_UUID, "network-1"), {}),
            "Deleted virtual network",
        ),
        (
            ["network", "list-bridges", SYSTEM_UUID, "--json"],
            ("list_network_bridges", (SYSTEM_UUID,), {}),
            "bridge-1",
        ),
        (
            ["storage", "list-vgs", VIOS_UUID, "--json"],
            ("list_volume_groups", (VIOS_UUID,), {}),
            VG_UUID,
        ),
        (
            [
                "storage",
                "map",
                VIOS_UUID,
                "--lpar",
                LPAR_UUID,
                "--name",
                "bootvol",
                "--target",
                "vtscsi0",
                "--yes",
            ],
            (
                "map_storage_to_lpar",
                (VIOS_UUID, "VirtualDisk", "bootvol", LPAR_UUID, "vtscsi0"),
                {},
            ),
            "operation\tmap",
        ),
        (
            [
                "storage",
                "create-media-repo",
                VIOS_UUID,
                "--vg",
                VG_UUID,
                "--size-mib",
                "2048",
                "--yes",
            ],
            ("create_media_repository", (VIOS_UUID, VG_UUID, 2048), {}),
            "operation\tcreate-media-repo",
        ),
        (
            ["storage", "delete-media-repo", VIOS_UUID, "--vg", VG_UUID, "--yes"],
            ("delete_media_repository", (VIOS_UUID, VG_UUID), {}),
            "operation\tdelete-media-repo",
        ),
    ],
)
def test_cli_command_wiring_matrix(fake_hmc, args, expected_call, output):
    result = RUNNER.invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert expected_call in fake_hmc.calls
    assert output in result.stdout


def test_network_cli_translates_create_rejection(fake_hmc):
    fake_hmc.fail_on = "create_virtual_network"
    fake_hmc.fail_status = 406
    result = RUNNER.invoke(
        cli.app,
        [
            "network",
            "create",
            SYSTEM_UUID,
            "--name",
            "prod",
            "--vlan",
            "100",
            "--virtual-switch-id",
            "2",
            "--yes",
        ],
    )
    assert result.exit_code == 1
    assert "virtual network create request" in result.stderr


def test_network_cli_refuses_an_unknown_switch_id_before_the_create(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "network",
            "create",
            SYSTEM_UUID,
            "--name",
            "prod",
            "--vlan",
            "100",
            "--virtual-switch-id",
            "7",
            "--yes",
        ],
    )
    assert result.exit_code == 1
    assert "no VirtualSwitch has SwitchID 7" in result.stderr
    assert not any(call[0] == "create_virtual_network" for call in fake_hmc.calls)


@pytest.mark.parametrize(
    "args",
    [
        [
            "adapters",
            "delete",
            LPAR_UUID,
            "--type",
            "ClientNetworkAdapter",
            "--uuid",
            "adapter-1",
        ],
        ["network", "delete", SYSTEM_UUID, "--uuid", "network-1"],
        ["storage", "delete-media-repo", VIOS_UUID, "--vg", VG_UUID],
    ],
)
@pytest.mark.usefixtures("storage_terminal_input")
def test_destructive_cli_commands_abort_without_confirmation(fake_hmc, args):
    result = RUNNER.invoke(cli.app, args, input="n\n")

    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert fake_hmc.calls == []


# --------------------------------------------------------------------------- #
# lpars list / show / state
# --------------------------------------------------------------------------- #


def test_lpars_list_table(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "list"])

    assert result.exit_code == 0
    assert LPAR_NAME in result.stdout
    assert "running" in result.stdout
    assert fake_hmc.calls == [
        ("list_system_summaries", (), {}),
        ("list_logical_partitions", (SYSTEM_UUID,), {}),
    ]


def test_lpars_list_json(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [entry["UUID"] for entry in payload["entries"]] == [LPAR_UUID]
    assert payload["unreadable_systems"] == []
    assert fake_hmc.calls == [
        ("list_system_summaries", (), {}),
        ("list_logical_partitions", (SYSTEM_UUID,), {}),
    ]


def test_lpars_list_state_filter(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "list", "--state", "running"])

    assert result.exit_code == 0
    assert LPAR_NAME in result.stdout
    # The partition feed, filtered locally: V10R3 cannot search a state (#1202).
    assert fake_hmc.calls == [
        ("list_system_summaries", (), {}),
        ("list_logical_partitions", (SYSTEM_UUID,), {}),
    ]


def test_lpars_list_names_a_skipped_system_on_stderr(fake_hmc):
    resource = {**fake_hmc.system["Resource"], "State": "No Connection"}
    fake_hmc.system = {**fake_hmc.system, "Resource": resource}

    result = RUNNER.invoke(cli.app, ["lpars", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "entries": [],
        "unreadable_systems": [
            {
                "system_name": "sys1",
                "system_uuid": SYSTEM_UUID,
                "state": "No Connection",
                "detailed_state": None,
            }
        ],
    }
    assert (
        f"Skipped managed system sys1 ({SYSTEM_UUID}): State No Connection"
        in result.stderr
    )
    assert fake_hmc.calls == [("list_system_summaries", (), {})]


def test_lpars_summary_renders_numeric_zero(monkeypatch):
    from hmcpctl.operations.inventory.composite import _lpar_summary

    summary = _lpar_summary(
        {
            "Resource": {
                "PartitionName": "zero-lpar",
                "PartitionMemoryConfiguration": {
                    "CurrentMemory": 0,
                    "DesiredMemory": 0,
                },
                "PartitionProcessorConfiguration": {
                    "HasDedicatedProcessors": "false",
                    "CurrentSharedProcessorConfiguration": {
                        "CurrentProcessingUnits": 0.0
                    },
                },
            }
        },
        [],
    )
    monkeypatch.setattr(
        "hmcpctl.cli_commands.lpar.inventory.with_client", lambda _operation: summary
    )

    result = RUNNER.invoke(cli.app, ["lpars", "summary", "zero-lpar"])

    assert result.exit_code == 0
    rows = dict(line.split("\t") for line in result.stdout.splitlines()[1:])
    assert rows["current_memory_mib"] == "0"
    assert rows["current_proc_units"] == "0.0"


def test_lpars_show_by_uuid(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "show", LPAR_UUID])

    assert result.exit_code == 0
    assert LPAR_NAME in result.stdout
    assert fake_hmc.calls == [("get_logical_partition", (LPAR_UUID,), {})]


def test_lpars_show_by_name_resolves(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "show", LPAR_NAME])

    assert result.exit_code == 0
    assert LPAR_UUID in result.stdout
    assert fake_hmc.calls == [("find_partition_by_name", (LPAR_NAME,), {})]


def test_lpars_show_not_found_exits_1(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "show", "ghost"])

    assert result.exit_code == 1
    assert "not found" in result.stderr
    assert fake_hmc.calls == [("find_partition_by_name", ("ghost",), {})]


def test_lpars_state_entry_without_uuid_reports_not_found(fake_hmc, monkeypatch):
    """A resolved entry that lacks a UUID is a miss (None), never an empty
    string — an empty UUID must not flow into the REST path."""

    async def found_no_uuid(name):
        fake_hmc._record("find_partition_by_name", name)
        return {"Resource": {"PartitionName": LPAR_NAME}}

    monkeypatch.setattr(fake_hmc, "find_partition_by_name", found_no_uuid)
    result = RUNNER.invoke(cli.app, ["lpars", "state", LPAR_NAME])

    assert result.exit_code == 1
    assert "not found" in result.stderr
    assert fake_hmc.calls == [("find_partition_by_name", (LPAR_NAME,), {})]


def test_lpars_state_uses_typed_resolver_miss(fake_hmc, monkeypatch):
    from hmcpctl.cli_commands.lpar import inventory
    from hmcpctl.resource_identity import ResourceNotFoundError

    async def missing_lpar(*_args, **_kwargs):
        raise ResourceNotFoundError("LPAR", LPAR_NAME, "typed resolver miss")

    monkeypatch.setattr(inventory, "get_lpar_state", missing_lpar)

    result = RUNNER.invoke(cli.app, ["lpars", "state", LPAR_NAME])

    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_lpars_state(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "state", LPAR_UUID])

    assert result.exit_code == 0
    assert result.stdout.strip() == "running"
    assert fake_hmc.calls == [
        ("get_quick_property", ("LogicalPartition", LPAR_UUID, "PartitionState"), {})
    ]


# --------------------------------------------------------------------------- #
# lpars power (jobs)
# --------------------------------------------------------------------------- #


def test_lpars_power_on_submits_power_on_job(fake_hmc):
    result = RUNNER.invoke(
        cli.app, ["lpars", "power-on", LPAR_UUID, "--force", "--yes"]
    )

    assert result.exit_code == 0
    assert "Job submitted" in result.stdout
    assert [c[0] for c in fake_hmc.calls] == ["submit_job"]
    path, job_xml = fake_hmc.calls[0][1]
    assert path == f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOn"
    assert "PowerOn</OperationName>" in job_xml


@pytest.mark.parametrize(
    "command",
    [
        ["adapters", "add-network", LPAR_UUID, "--vlan", "100", "--yes"],
        [
            "adapters",
            "delete",
            LPAR_UUID,
            "--type",
            "ClientNetworkAdapter",
            "--uuid",
            "adapter-1",
            "--yes",
        ],
    ],
    ids=["add", "delete"],
)
def test_adapter_commands_print_where_the_change_lives(fake_hmc, command):
    """#981: the fake partition reports no CurrentProfileSync."""
    result = RUNNER.invoke(cli.app, command)

    assert result.exit_code == 0
    assert "CurrentProfileSync is not reported" in result.stdout
    names = [name for name, _args, _kwargs in fake_hmc.calls]
    assert "get_logical_partition" in names


def test_lpars_power_on_activation_flags_reach_the_job(fake_hmc):
    """--boot-mode, --partition-profile and --keylock reach the job; activate does not."""
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "power-on",
            LPAR_UUID,
            "--force",
            "--yes",
            "--boot-mode",
            "sms",
            "--partition-profile",
            PARTITION_PROFILE_UUID,
            "--operation-type",
            "activate",
            "--keylock",
            "manual",
        ],
    )

    assert result.exit_code == 0
    # Select by name: the ADR 0039 containment read precedes the submission.
    assert (
        "list_child",
        ("LogicalPartition", LPAR_UUID, "LogicalPartitionProfile"),
    ) in [(name, args) for name, args, _ in fake_hmc.calls]
    submitted = [args for name, args, _ in fake_hmc.calls if name == "submit_job"]
    assert len(submitted) == 1
    _, job_xml = submitted[0]
    assert '<ParameterValue kb="CUR" kxe="false">sms</ParameterValue>' in job_xml
    assert ">LogicalPartitionProfile</ParameterName>" in job_xml
    assert PARTITION_PROFILE_UUID in job_xml
    # #1392: --operation-type activate is accepted and never sent.
    assert ">OperationType</ParameterName>" not in job_xml
    assert ">keylock</ParameterName>" in job_xml
    assert '<ParameterValue kb="CUR" kxe="false">manual</ParameterValue>' in job_xml
    # #981: the fake's adapters carry no slot, so the profile check warns about each.
    assert "Warning: The partition profile lacks" in result.stdout
    assert "ClientNetworkAdapter in virtual slot not reported" in result.stdout


def test_lpars_power_on_partition_profile_help_states_the_discard():
    """#1384: activating a profile discards current-configuration changes it lacks."""
    result = RUNNER.invoke(cli.app, ["lpars", "power-on", "--help"])

    assert result.exit_code == 0
    # Single words only: Rich wraps the help column at the runner's terminal width.
    assert "discards" in result.stdout
    assert "CurrentProfileSync" in result.stdout


@pytest.mark.parametrize("flags", [["--boot-mode", "bogus"], ["--keylock", "normal"]])
def test_lpars_power_on_rejects_an_unknown_activation_value(fake_hmc, flags):
    """Typer refuses a non-member before the command body runs, so no job is sent."""
    result = RUNNER.invoke(
        cli.app, ["lpars", "power-on", LPAR_UUID, "--force", "--yes", *flags]
    )

    assert result.exit_code == 2
    assert fake_hmc.calls == []


def test_lpars_power_on_skips_running_partition_without_force(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "power-on", LPAR_UUID, "--yes"])

    assert result.exit_code == 0
    assert "already running" in result.stdout
    assert [call[0] for call in fake_hmc.calls] == ["get_quick_property"]


def test_lpars_power_off_resolves_name_then_submits(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "power-off", LPAR_NAME, "--yes"])

    assert result.exit_code == 0
    assert "Job submitted" in result.stdout
    names = [c[0] for c in fake_hmc.calls]
    assert names == ["find_partition_by_name", "submit_job"]
    path = fake_hmc.calls[1][1][0]
    assert "/do/PowerOff" in path
    assert LPAR_UUID in path


def test_lpars_power_off_forwards_shutdown_parameters(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "power-off",
            LPAR_NAME,
            "--operation",
            "osshutdown",
            "--restart",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "Job submitted" in result.stdout
    document = fake_hmc.calls[1][1][1]
    assert '<ParameterName kb="ROR" kxe="false">restart</ParameterName>' in document
    assert '<ParameterValue kb="CUR" kxe="false">true</ParameterValue>' in document
    assert (
        '<ParameterValue kb="CUR" kxe="false">osshutdown</ParameterValue>' in document
    )


def test_lpars_power_off_prompt_names_restart_and_operation(fake_hmc):
    """The prompt is the only place a human is asked, so it says what will happen."""
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "power-off",
            LPAR_UUID,
            "--restart",
            "--operation",
            "dumprestart",
            "--allow-dump-restart",
        ],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "with restart" in result.stdout
    assert "operation=dumprestart" in result.stdout
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


def test_lpars_power_off_declined_confirm_aborts(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "power-off", LPAR_UUID], input="n\n")

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    # No job submitted — the confirm gates the destructive call. A UUID needs
    # no resolution, so the fake client was never called.
    assert fake_hmc.calls == []


@pytest.mark.parametrize(
    "command",
    [
        ["lpars", "power-on", LPAR_UUID, "--force"],
        ["systems", "power-on", SYSTEM_UUID],
        ["systems", "power-off", SYSTEM_UUID],
        ["vios", "power-on", VIOS_UUID],
        ["vios", "power-off", VIOS_UUID],
    ],
)
def test_power_commands_forward_submission_link_when_waiting(fake_hmc, command):
    result = RUNNER.invoke(
        cli.app,
        [*command, "--yes", "--wait", "--timeout", "90", "--interval", "3"],
    )

    assert result.exit_code == 0
    assert fake_hmc.calls[-1] == (
        "wait_for_job_entry",
        (JOB_UUID, 90, 3),
        {"job_href": f"/jobs/{JOB_UUID}"},
    )


def test_lpars_power_off_confirms_public_selector_then_submits(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "power-off", LPAR_NAME], input="y\n")

    assert result.exit_code == 0
    assert "Job submitted" in result.stdout
    names = [c[0] for c in fake_hmc.calls]
    assert names == ["find_partition_by_name", "submit_job"]
    path = fake_hmc.calls[1][1][0]
    assert "/do/PowerOff" in path
    assert LPAR_UUID in path


# --------------------------------------------------------------------------- #
# lpars create / modify / delete
# --------------------------------------------------------------------------- #


def test_lpars_create(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "create", "newlpar", "--system", SYSTEM_UUID, "--yes"],
    )

    assert result.exit_code == 0
    assert "Created LPAR 'newlpar'" in result.stdout
    assert fake_hmc.calls[0] == (
        "find_partition_by_name",
        ("newlpar",),
        {"system_uuid": SYSTEM_UUID},
    )
    assert fake_hmc.calls[1] == ("list_system_summaries", (), {})
    assert fake_hmc.calls[2] == ("get_managed_system", (SYSTEM_UUID,), {})
    name, args, _ = fake_hmc.calls[3]
    assert name == "create_logical_partition"
    assert args[0] == SYSTEM_UUID
    assert "newlpar" in args[1]  # the partition XML carries the name
    # A REST create configures the partition itself; the apply is reported skipped (#1164)
    assert '"status": "skipped"' in result.stdout
    assert "the REST create set the current configuration" in result.stdout


@pytest.mark.parametrize(("flags", "expected"), [((), True), (("--no-apply",), False)])
def test_lpars_create_passes_the_apply_choice(monkeypatch, fake_hmc, flags, expected):
    create = AsyncMock(
        return_value=LparPcieWorkflowResult(True, True, {}, True, (), ())
    )
    monkeypatch.setattr("hmcpctl.cli_commands.lpar.create.create_lpar", create)

    result = RUNNER.invoke(
        cli.app,
        ["lpars", "create", "newlpar", "--system", SYSTEM_UUID, *flags, "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert create.await_args.args[2].apply_profile is expected


def test_lpars_create_declined_confirm_aborts(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "create", "newlpar", "--system", SYSTEM_UUID],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


def test_lpars_create_rejects_duplicate_name(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "create", LPAR_NAME, "--system", SYSTEM_UUID, "--yes"],
    )

    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert fake_hmc.calls == [
        ("find_partition_by_name", (LPAR_NAME,), {"system_uuid": SYSTEM_UUID})
    ]


def test_lpars_create_rejects_invalid_partition_type_before_client_call(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "create",
            "newlpar",
            "--system",
            SYSTEM_UUID,
            "--type",
            "Windows",
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert "--type: partition_type must be one of" in result.stderr
    assert fake_hmc.calls == []


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--type", "Windows", "--type: partition_type must be one of"),
        ("--storage-kind", "Tape", "--storage-kind must be one of"),
    ],
)
def test_lpars_provision_rejects_invalid_vocabulary_before_client_call(
    fake_hmc, option, value, message
):
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "provision",
            "--system",
            SYSTEM_UUID,
            "--name",
            "newlpar",
            "--vlan",
            "100",
            "--vios-uuid",
            VIOS_UUID,
            "--storage-name",
            "rootvg",
            option,
            value,
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert message in result.stderr
    assert fake_hmc.calls == []


def test_lpars_provision_passes_nondefault_request_to_operation(monkeypatch, fake_hmc):
    provision = AsyncMock(
        return_value=ProvisionResult(
            resource_created=True,
            workflow_completed=True,
            lpar_uuid=LPAR_UUID,
            dry_run=False,
            ownership_stamped=True,
            steps=(),
            warnings=(),
        )
    )
    monkeypatch.setattr(cli_lpar_provision, "provision_lpar", provision)

    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "provision",
            "--system",
            SYSTEM_UUID,
            "--name",
            "newlpar",
            "--vlan",
            "200",
            "--vios-uuid",
            VIOS_UUID,
            "--storage-name",
            "hdisk7",
            "--storage-kind",
            "PhysicalVolume",
            "--vg-uuid",
            VG_UUID,
            "--type",
            "OS400",
            "--min-mem",
            "512",
            "--mem",
            "6144",
            "--max-mem",
            "12288",
            "--vcpus",
            "4",
            "--max-vcpus",
            "8",
            "--no-power-on",
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"workflow_completed": true' in result.output
    provision.assert_awaited_once()
    request = provision.await_args.kwargs["request"]
    assert provision.await_args.args == (fake_hmc,)
    assert request.name == "newlpar"
    assert request.partition_type == "OS400"
    assert request.adapters.port_vlan_id == 200
    assert request.storage.vios_uuid == VIOS_UUID
    assert (
        request.storage.storage_name,
        request.storage.kind,
        request.storage.vg_uuid,
    ) == (
        "hdisk7",
        "PhysicalVolume",
        VG_UUID,
    )
    assert (
        request.resources.min_memory,
        request.resources.desired_memory,
        request.resources.max_memory,
        request.resources.desired_vcpus,
        request.resources.max_vcpus,
    ) == (512, 6144, 12288, 4, 8)
    assert request.power_on is False


def test_lpars_provision_renders_change_location(monkeypatch, fake_hmc):
    """#1056: provision prints the same CurrentProfileSync line add-network does."""
    provision = AsyncMock(
        return_value=ProvisionResult(
            resource_created=True,
            workflow_completed=True,
            lpar_uuid=LPAR_UUID,
            dry_run=False,
            ownership_stamped=True,
            steps=(),
            warnings=(),
            change_location=UNSYNCED,
        )
    )
    monkeypatch.setattr(cli_lpar_provision, "provision_lpar", provision)

    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "provision",
            "--system",
            SYSTEM_UUID,
            "--name",
            "newlpar",
            "--vlan",
            "100",
            "--vios-uuid",
            VIOS_UUID,
            "--storage-name",
            "rootvg",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "CurrentProfileSync is Disabled" in result.stdout


def test_lpars_modify_renames(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "modify",
            LPAR_UUID,
            "--system",
            SYSTEM_UUID,
            "--name",
            "renamed",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "Modified LPAR" in result.stdout
    name, args, _ = fake_hmc.calls[-1]
    assert name == "update_logical_partition"
    assert args[0] == LPAR_UUID
    assert args[1] == {"PartitionName": "renamed"}


def test_lpars_modify_reports_incomplete_workflow_with_nonzero_exit(
    monkeypatch, fake_hmc
):
    monkeypatch.setattr(
        cli_lpar_modify,
        "modify_lpar",
        AsyncMock(
            return_value=LparPcieWorkflowResult(
                False,
                False,
                {"UUID": LPAR_UUID},
                None,
                (WorkflowStep("assign", "error", "failed"),),
                ("assignment failed",),
            )
        ),
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "modify",
            LPAR_UUID,
            "--system",
            SYSTEM_UUID,
            "--name",
            "renamed",
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert '"workflow_completed": false' in result.output
    assert "assignment failed" in result.output


def test_lpars_modify_rename_requires_system_before_client_use(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "modify", LPAR_UUID, "--name", "renamed", "--yes"],
    )

    assert result.exit_code == 2
    assert "--system is required when renaming an LPAR" in result.stderr
    assert fake_hmc.calls == []


def test_lpars_modify_with_no_options_exits_2(fake_hmc):
    result = RUNNER.invoke(cli.app, ["lpars", "modify", LPAR_UUID])

    assert result.exit_code == 2
    assert "Error:" in result.stderr
    assert "Nothing to change" in result.stderr
    assert fake_hmc.calls == []


def test_lpars_modify_procs_only_keeps_sharing_mode(fake_hmc):
    """A proc-only modify without --dedicated/--capped must not flip the mode."""
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "modify", LPAR_UUID, "--procs", "0.5", "--yes"],
    )

    assert result.exit_code == 0
    _, args, _ = next(
        call for call in fake_hmc.calls if call[0] == "update_logical_partition"
    )
    assert args[1] == {
        "PartitionProcessorConfiguration/SharedProcessorConfiguration/DesiredProcessingUnits": (
            "0.5"
        ),
        "PartitionProcessorConfiguration/HasDedicatedProcessors": "false",
    }


def test_lpars_modify_dedicated_flag_must_match_the_partition_mode(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "modify", LPAR_UUID, "--procs", "2", "--dedicated", "--yes"],
    )

    assert result.exit_code != 0
    assert "switch the partition between dedicated and shared" in str(
        result.exception
    ) + (result.output)
    assert not any(call[0] == "update_logical_partition" for call in fake_hmc.calls)


def test_lpars_delete(fake_hmc):
    async def powered_off(*_args):
        return "not activated"

    fake_hmc.get_quick_property = powered_off
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "delete", LPAR_UUID, "--system", SYSTEM_UUID, "--yes"],
    )

    assert result.exit_code == 0
    assert "Deleted LPAR" in result.stdout
    assert fake_hmc.calls[-1] == ("delete_logical_partition", (LPAR_UUID,), {})


def test_lpars_delete_rejects_running_partition(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "delete", LPAR_UUID, "--system", SYSTEM_UUID, "--yes"],
    )

    assert result.exit_code == 1
    assert "must be 'not activated' to delete" in result.stderr
    assert all(call[0] != "delete_logical_partition" for call in fake_hmc.calls)


def test_lpars_delete_not_found_exits_1(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "delete", "ghost", "--system", SYSTEM_UUID, "--yes"],
    )

    assert result.exit_code == 1
    assert "No LPAR named 'ghost' found" in result.stderr


def test_lpars_delete_denies_foreign_owned_partition_without_transport(
    fake_hmc, monkeypatch
):
    async def foreign_description(*_args):
        return "[hmcpctl owner:other-agent created:2026-08-14]"

    monkeypatch.setattr(lpar_ownership, "get_lpar_description", foreign_description)

    result = RUNNER.invoke(
        cli.app,
        ["lpars", "delete", LPAR_UUID, "--system", SYSTEM_UUID, "--yes"],
    )

    assert result.exit_code == 1
    assert "owned by" in result.stderr
    assert all(call[0] != "delete_logical_partition" for call in fake_hmc.calls)


def test_lpars_decommission_help_lists_cli_contract(fake_hmc):
    root_command = get_command(cli.app)
    decommission_command = root_command.commands["lpars"].commands["decommission"]
    option_names = {
        option
        for parameter in decommission_command.params
        for option in parameter.opts
        if option.startswith("--")
    }

    assert option_names == {
        "--system",
        "--dry-run",
        "--ownership-override",
        "--immediate",
        "--timeout-seconds",
        "--poll-interval",
        "--json",
        "--format",
        "--yes",
    }


def test_lpars_decommission_dry_run_skips_confirmation(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "decommission",
            LPAR_NAME,
            "--system",
            SYSTEM_UUID,
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.stderr
    assert "power_off" in result.stdout
    assert "dry_run" in result.stdout
    assert "Aborted" not in result.stderr
    assert "delete_logical_partition" not in [name for name, _, _ in fake_hmc.calls]


def test_lpars_decommission_declined_confirmation_aborts(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "decommission", LPAR_NAME, "--system", SYSTEM_UUID],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


def test_lpars_decommission_confirmed_prompt_names_target_and_executes(fake_hmc):
    fake_hmc.lpar["Resource"]["PartitionState"] = "not activated"

    result = RUNNER.invoke(
        cli.app,
        ["lpars", "decommission", LPAR_NAME, "--system", SYSTEM_UUID],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert (
        f"Decommission LPAR '{LPAR_NAME}' on system '{SYSTEM_UUID}'?" in result.output
    )
    assert "decommissioned successfully" in result.stderr
    assert "vSCSI mappings before client adapters" in result.output
    assert "retains backing storage" in result.output
    names = [name for name, _, _ in fake_hmc.calls]
    assert "delete_logical_partition" in names
    assert "Aborted" not in result.stderr


def test_lpars_decommission_yes_executes_workflow(fake_hmc):
    fake_hmc.lpar["Resource"]["PartitionState"] = "not activated"

    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "decommission",
            LPAR_NAME,
            "--system",
            SYSTEM_UUID,
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "decommissioned successfully" in result.stderr
    names = [name for name, _, _ in fake_hmc.calls]
    assert "delete_logical_partition" in names
    assert "Aborted" not in result.stderr


def test_lpars_decommission_incomplete_human_result_exits_1_after_rendering(fake_hmc):
    fake_hmc.lpar["Resource"]["PartitionState"] = "not activated"
    fake_hmc.fail_on = "delete_logical_partition"

    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "decommission",
            LPAR_NAME,
            "--system",
            SYSTEM_UUID,
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert "was not fully decommissioned" in result.stderr
    assert "delete_lpar" in result.stdout
    assert "error" in result.stdout
    assert all(
        word in result.stdout
        for word in ["simulated", "delete_logical_partition", "failure"]
    )


def test_lpars_decommission_incomplete_json_result_exits_1_after_rendering(fake_hmc):
    fake_hmc.lpar["Resource"]["PartitionState"] = "not activated"
    fake_hmc.fail_on = "delete_logical_partition"

    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "decommission",
            LPAR_NAME,
            "--system",
            SYSTEM_UUID,
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["resource_deleted"] is False
    assert payload["workflow_completed"] is False
    assert payload["dry_run"] is False
    assert payload["steps"][-1]["step"] == "delete_lpar"
    assert payload["steps"][-1]["status"] == "error"
    assert (
        "simulated delete_logical_partition failure" in payload["steps"][-1]["result"]
    )


def test_lpars_decommission_denies_foreign_owned_partition(fake_hmc, monkeypatch):
    async def foreign_description(*_args):
        return "[hmcpctl owner:other-agent created:2026-08-14]"

    monkeypatch.setattr(lpar_ownership, "get_lpar_description", foreign_description)

    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "decommission",
            LPAR_UUID,
            "--system",
            SYSTEM_UUID,
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert "owned by" in result.stderr
    assert "delete_logical_partition" not in [name for name, _, _ in fake_hmc.calls]


def test_lpars_decommission_json_renders_dataclass_shape(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "decommission",
            LPAR_NAME,
            "--system",
            SYSTEM_UUID,
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "resource_deleted": False,
        "workflow_completed": True,
        "lpar_uuid": LPAR_UUID,
        "dry_run": True,
        "steps": [
            {"step": "power_off", "status": "dry_run", "result": {"state": "running"}},
            {
                "step": "detach_storage_mappings",
                "status": "dry_run",
                "result": {"mappings": []},
            },
            {
                "step": "detach_adapters",
                "status": "dry_run",
                "result": {
                    "adapters": [
                        {
                            "type": "ClientNetworkAdapter",
                            "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa0",
                        },
                        {
                            "type": "VirtualSCSIClientAdapter",
                            "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
                        },
                        {
                            "type": "VirtualFibreChannelClientAdapter",
                            "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
                        },
                        {
                            "type": "VirtualNICDedicated",
                            "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3",
                        },
                    ]
                },
            },
            {
                "step": "delete_lpar",
                "status": "dry_run",
                "result": {"lpar_uuid": LPAR_UUID},
            },
        ],
        "warnings": [],
        "blast_radius": {
            "lpar_uuid": LPAR_UUID,
            "lpar_name": LPAR_NAME,
            "partition_id": 1,
            "state": "running",
            "owner": None,
            "adapters": [
                {
                    "type": "ClientNetworkAdapter",
                    "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa0",
                },
                {
                    "type": "VirtualSCSIClientAdapter",
                    "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
                },
                {
                    "type": "VirtualFibreChannelClientAdapter",
                    "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
                },
                {
                    "type": "VirtualNICDedicated",
                    "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3",
                },
            ],
            "storage_mappings": [],
            "mapping_evidence": [],
            "unresolved_storage_mapping_count": 0,
            "unavailable_storage_source_count": 0,
        },
    }


# --------------------------------------------------------------------------- #
# adapter and storage vocabularies
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("command", ["list", "delete"])
def test_adapters_reject_invalid_type_before_client_call(fake_hmc, command):
    args = ["adapters", command, LPAR_UUID, "--type", "UnknownAdapter"]
    if command == "delete":
        args.extend(["--uuid", "adapter-1", "--yes"])

    result = RUNNER.invoke(cli.app, args)

    assert result.exit_code == 2
    assert "UnknownAdapter" in result.stderr
    assert fake_hmc.calls == []


@pytest.mark.parametrize(
    ("module", "operation", "args", "returned"),
    [
        ("virtualization.adapters", "list_adapters", ["adapters", "list"], []),
        (
            "virtualization.adapters",
            "add_network_adapter",
            ["adapters", "add-network", "--vlan", "100", "--yes"],
            AdapterResult(LPAR_UUID, None, UNSYNCED),
        ),
        (
            "virtualization.adapters",
            "add_vscsi_adapter",
            ["adapters", "add-vscsi", "--vios-id", "1", "--vios-slot", "5", "--yes"],
            AdapterResult(LPAR_UUID, None, UNSYNCED),
        ),
        (
            "virtualization.adapters",
            "add_vfc_adapter",
            ["adapters", "add-vfc", "--vios-id", "1", "--vios-slot", "6", "--yes"],
            AdapterResult(LPAR_UUID, None, UNSYNCED),
        ),
        (
            "virtualization.adapters",
            "delete_adapter",
            [
                "adapters",
                "delete",
                "--type",
                "ClientNetworkAdapter",
                "--uuid",
                "adapter-1",
                "--yes",
            ],
            UNSYNCED,
        ),
        (
            "storage.resources",
            "attach_disk_to_lpar",
            [
                "storage",
                "attach-disk",
                "--vios",
                VIOS_UUID,
                "--vg",
                VG_UUID,
                "--name",
                "bootvol",
                "--capacity-mib",
                "1024",
                "--dry-run",
            ],
            AttachDiskResult(False, LPAR_UUID, True, (), ()),
        ),
        (
            "lpar.inventory",
            "fetch_lpar_summary",
            ["lpars", "summary", "--json"],
            _lpar_summary({"Resource": {}}, []),
        ),
        (
            "lpar.migration",
            "migrate_lpar",
            ["lpars", "migrate", "--target", "sys2", "--no-validate-first", "--yes"],
            LpmResult(LPAR_UUID, None),
        ),
        (
            "lpar.migration",
            "migrate_lpar_with_affinity_preflight",
            ["lpars", "migrate-affinity", "--target", "sys2", "--yes"],
            LpmResult(LPAR_UUID, None),
        ),
        (
            "lpar.migration",
            "validate_lpar_migration",
            ["lpars", "migrate-validate", "--target", "sys2", "--yes"],
            LpmResult(LPAR_UUID, None),
        ),
        (
            "lpar.migration",
            "abort_lpar_migration",
            ["lpars", "migrate-abort", "--yes"],
            LpmResult(LPAR_UUID, None),
        ),
        (
            "lpar.migration",
            "recover_lpar_migration",
            ["lpars", "migrate-recover", "--yes"],
            LpmResult(LPAR_UUID, None),
        ),
    ],
)
@pytest.mark.parametrize(
    ("scope_args", "expected_system"),
    [
        (["--system", SYSTEM_UUID], SYSTEM_UUID),
        (["-s", SYSTEM_UUID], SYSTEM_UUID),
        ([], None),
    ],
)
def test_lpar_scoped_commands_pass_system_scope(
    fake_hmc,
    monkeypatch,
    module,
    operation,
    args,
    returned,
    scope_args,
    expected_system,
):
    seen = []

    async def fake_operation(_hmc, system, lpar, *_args, **_kwargs):
        seen.append((system, lpar))
        return returned

    monkeypatch.setattr(f"hmcpctl.cli_commands.{module}.{operation}", fake_operation)
    command, rest = args[:2], args[2:]

    result = RUNNER.invoke(cli.app, [*command, LPAR_NAME, *rest, *scope_args])

    assert result.exit_code == 0, result.output
    assert seen == [(expected_system, LPAR_NAME)]


def test_storage_map_rejects_invalid_kind_before_client_call(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "map",
            VIOS_UUID,
            "--lpar",
            LPAR_UUID,
            "--name",
            "disk1",
            "--kind",
            "UnknownStorage",
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert "UnknownStorage" in result.stderr
    assert fake_hmc.calls == []


# --------------------------------------------------------------------------- #
# storage create
# --------------------------------------------------------------------------- #


def test_storage_create_vg(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "create-vg",
            VIOS_UUID,
            "--name",
            "datavg",
            "--pvs",
            "hdisk1, hdisk2",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "target\tdatavg" in result.stdout
    name, args, _ = fake_hmc.calls[0]
    assert name == "create_volume_group"
    assert args[0] == VIOS_UUID
    assert args[2] == ["hdisk1", "hdisk2"]


def test_storage_create_vg_requires_pvs(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "create-vg",
            VIOS_UUID,
            "--name",
            "datavg",
            "--pvs",
            " , ",
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert "Error:" in result.stderr
    assert "Provide at least one physical volume" in result.stderr
    assert fake_hmc.calls == []


def test_storage_create_disk(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "create-disk",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "bootvol",
            "--capacity-mib",
            "1024",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "capacity_mib\t1024" in result.stdout
    assert fake_hmc.calls[0] == ("list_volume_groups", (VIOS_UUID,), {})
    name, args, _ = fake_hmc.calls[1]
    assert name == "create_virtual_disk"
    assert args[1] == VG_UUID
    assert args[3] == 1024


def test_storage_attach_disk_runs_workflow(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "attach-disk",
            LPAR_NAME,
            "--vios",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "bootvol",
            "--capacity-mib",
            "1024",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "target\tbootvol" in result.stdout
    mutations = [
        name
        for name, _, _ in fake_hmc.calls
        if name in {"create_virtual_disk", "add_vscsi_adapter", "map_storage_to_lpar"}
    ]
    assert mutations == ["create_virtual_disk", "map_storage_to_lpar"]


def test_storage_attach_disk_dry_run_does_not_mutate(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "attach-disk",
            LPAR_UUID,
            "--vios",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "bootvol",
            "--capacity-mib",
            "1024",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "outcome\tdry_run" in result.stdout
    mutations = {"create_virtual_disk", "add_vscsi_adapter", "map_storage_to_lpar"}
    assert not any(name in mutations for name, _, _ in fake_hmc.calls)


def test_storage_attach_disk_partial_failure_is_visible_and_nonzero(fake_hmc):
    fake_hmc.fail_on = "map_storage_to_lpar"

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "attach-disk",
            LPAR_UUID,
            "--vios",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "bootvol",
            "--capacity-mib",
            "1024",
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert "create_disk" in result.stdout
    assert "vscsi" not in result.stdout
    assert "storage" in result.stdout
    assert "error" in result.stdout
    assert "simulated map_storage_to_lpar failure" in result.stdout


def test_storage_attach_disk_json_incomplete_workflow_exits_1(fake_hmc):
    """The --json branch exits 1 without rendering the step table.

    No operation is patched: ``attach_disk_to_lpar`` returns the frozen
    ``AttachDiskResult`` dataclass, and the command calls ``asdict()`` on it. A
    stub returning a dict or a namespace would raise ``TypeError`` before the
    branch ran, which ``CliRunner`` also reports as exit 1 -- so the JSON body,
    not the exit code, is what proves this branch executed.
    """
    fake_hmc.fail_on = "map_storage_to_lpar"

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "attach-disk",
            LPAR_UUID,
            "--vios",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "bootvol",
            "--capacity-mib",
            "1024",
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["workflow_completed"] is False
    assert payload["lpar_uuid"] == LPAR_UUID
    assert "Attach-disk steps" not in result.stdout


def _attach_disk_result(*, completed: bool, warnings=(), location=UNSYNCED):
    return AttachDiskResult(
        workflow_completed=completed,
        lpar_uuid=LPAR_UUID,
        dry_run=False,
        steps=(WorkflowStep("storage", "ok" if completed else "error", None),),
        warnings=warnings,
        change_location=location,
    )


def _invoke_attach_disk(monkeypatch, result):
    monkeypatch.setattr(
        cli_storage_resources, "attach_disk_to_lpar", AsyncMock(return_value=result)
    )
    return RUNNER.invoke(
        cli.app,
        [
            "storage",
            "attach-disk",
            LPAR_UUID,
            "--vios",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "bootvol",
            "--capacity-mib",
            "1024",
            "--yes",
        ],
    )


def test_storage_attach_disk_renders_change_location(monkeypatch, fake_hmc):
    """#1069: attach-disk prints the CurrentProfileSync line provision prints."""
    result = _invoke_attach_disk(monkeypatch, _attach_disk_result(completed=True))

    assert result.exit_code == 0
    assert "CurrentProfileSync is Disabled" in result.stdout


def test_storage_attach_disk_renders_location_warning(monkeypatch, fake_hmc):
    result = _invoke_attach_disk(
        monkeypatch,
        _attach_disk_result(
            completed=True, warnings=("Change location not read: boom",), location=None
        ),
    )

    assert result.exit_code == 0
    assert "Change location not read: boom" in result.stdout
    assert "CurrentProfileSync" not in result.stdout


def test_storage_attach_disk_incomplete_renders_change_location(monkeypatch, fake_hmc):
    result = _invoke_attach_disk(monkeypatch, _attach_disk_result(completed=False))

    assert result.exit_code == 1
    assert "CurrentProfileSync is Disabled" in result.stdout


# --------------------------------------------------------------------------- #
# storage: command bodies (#240)
#
# Storage commands use runtime.with_client(), so patch their operation boundary.
# --------------------------------------------------------------------------- #


def _storage_app() -> typer.Typer:
    app = typer.Typer()
    app.callback()(cli_command_app.main)
    cli_storage_resources.register_commands(app)
    return app


@pytest.mark.parametrize(
    ("command", "operation", "operation_result"),
    [
        (
            ["list-mappings", VIOS_UUID],
            "list_storage_mappings",
            [StorageMapping("vhost0/vtscsi0", None, None, None)],
        ),
        (
            ["detach-mapping", VIOS_UUID, "vhost0/vtscsi0", "--yes"],
            "detach_storage_mapping",
            UNSYNCED,
        ),
        (
            [
                "upload-iso",
                VIOS_UUID,
                "--vg",
                VG_UUID,
                "--name",
                "aix.iso",
                "https://images.test/aix.iso",
            ],
            "upload_iso",
            {"status": "uploaded", "media_name": "aix.iso"},
        ),
    ],
)
def test_storage_commands_use_root_connection_options(
    command, operation, operation_result, monkeypatch
):
    built_options = []

    def fake_build_config(**options):
        built_options.append(options)
        return HMCConfig.from_mapping({})

    monkeypatch.setattr(cli_runtime, "build_config", fake_build_config)
    monkeypatch.setattr(cli_runtime, "HMCClient", lambda _config: FakeHMC())
    monkeypatch.setattr(
        cli_storage_resources, operation, AsyncMock(return_value=operation_result)
    )

    result = RUNNER.invoke(
        _storage_app(), ["--profile", "operator", "--host", "root-hmc", *command]
    )

    assert result.exit_code == 0
    assert len(built_options) == 1
    assert built_options[0]["profile"] == "operator"
    assert built_options[0]["host"] == "root-hmc"


def test_storage_list_vgs_renders_a_table(fake_hmc, monkeypatch):
    async def fake_list(_hmc, vios, *, system_name_or_uuid=None):
        system = system_name_or_uuid
        assert system == "system-a"
        assert vios == VIOS_UUID
        return [VolumeGroup(VG_UUID, "rootvg", 100, 5, None)]

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.list_volume_groups", fake_list
    )

    result = RUNNER.invoke(
        cli.app,
        ["storage", "list-vgs", VIOS_UUID, "--system", "system-a", "--format", "table"],
    )

    assert result.exit_code == 0
    assert "rootvg" in result.stdout
    assert "Volume Groups" in result.stdout
    assert "Free (GiB)" in result.stdout


def test_storage_delete_disk_deletes_when_confirmed(fake_hmc, monkeypatch):
    seen = {}

    async def fake_delete(_hmc, vios, vg, name, *, system_name_or_uuid=None):
        seen.update(vios=vios, vg=vg, name=name)
        return {"UUID": "disk-1"}

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.delete_virtual_disk", fake_delete
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "delete-disk",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "bootvol",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "target\tbootvol" in result.stdout
    assert seen == {"vios": VIOS_UUID, "vg": VG_UUID, "name": "bootvol"}


@pytest.mark.usefixtures("storage_terminal_input")
def test_storage_delete_disk_declined_confirmation_aborts(fake_hmc, monkeypatch):
    called = []

    async def fake_delete(*args):
        called.append(args)

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.delete_virtual_disk", fake_delete
    )

    result = RUNNER.invoke(
        cli.app,
        ["storage", "delete-disk", VIOS_UUID, "--vg", VG_UUID, "--name", "bootvol"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert called == []


@pytest.mark.usefixtures("storage_terminal_input")
def test_storage_map_declined_confirmation_aborts(fake_hmc, monkeypatch):
    called = []

    async def fake_map(*args):
        called.append(args)

    monkeypatch.setattr("hmcpctl.cli_commands.storage.resources.map_storage", fake_map)

    result = RUNNER.invoke(
        cli.app,
        ["storage", "map", VIOS_UUID, "--lpar", LPAR_UUID, "--name", "bootvol"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert called == []


@pytest.mark.usefixtures("storage_terminal_input")
def test_storage_create_media_repo_declined_confirmation_aborts(fake_hmc, monkeypatch):
    called = []

    async def fake_create(*args):
        called.append(args)

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.create_media_repository", fake_create
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "create-media-repo",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--size-mib",
            "2048",
        ],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert called == []


def test_storage_create_media_creates_when_confirmed(fake_hmc, monkeypatch):
    seen = {}

    async def fake_create(_hmc, vios, vg, name, size_mib, *, system_name_or_uuid=None):
        seen.update(vios=vios, vg=vg, name=name, size_mib=size_mib)
        return {"MediaName": "aix.iso"}

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.create_optical_media", fake_create
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "create-media",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "aix.iso",
            "--size-mib",
            "4096",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "target\taix.iso" in result.stdout
    assert seen == {
        "vios": VIOS_UUID,
        "vg": VG_UUID,
        "name": "aix.iso",
        "size_mib": 4096,
    }


@pytest.mark.usefixtures("storage_terminal_input")
def test_storage_create_media_declined_confirmation_aborts(fake_hmc, monkeypatch):
    called = []

    async def fake_create(*args):
        called.append(args)

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.create_optical_media", fake_create
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "create-media",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "aix.iso",
            "--size-mib",
            "4096",
        ],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert called == []


def test_storage_delete_media_deletes_when_confirmed(fake_hmc, monkeypatch):
    seen = {}

    async def fake_delete(_hmc, vios, vg, media_name, *, system_name_or_uuid=None):
        seen.update(vios=vios, vg=vg, media_name=media_name)

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.delete_optical_media", fake_delete
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "delete-media",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "aix.iso",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "target\taix.iso" in result.stdout
    assert seen == {"vios": VIOS_UUID, "vg": VG_UUID, "media_name": "aix.iso"}


@pytest.mark.usefixtures("storage_terminal_input")
def test_storage_delete_media_declined_confirmation_aborts(fake_hmc, monkeypatch):
    called = []

    async def fake_delete(*args):
        called.append(args)

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.delete_optical_media", fake_delete
    )

    result = RUNNER.invoke(
        cli.app,
        ["storage", "delete-media", VIOS_UUID, "--vg", VG_UUID, "--name", "aix.iso"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert called == []


@pytest.mark.usefixtures("storage_terminal_input")
def test_storage_mount_optical_media_forwards_selectors(fake_hmc, monkeypatch):
    mount = AsyncMock(
        return_value=StorageMapResult(
            LPAR_UUID, {"UUID": "mapping-1", "MediaName": "install.iso"}, UNSYNCED
        )
    )
    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.mount_optical_media", mount
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "mount-optical-media",
            VIOS_UUID,
            "--lpar",
            LPAR_UUID,
            "--name",
            "install.iso",
            "--system",
            SYSTEM_UUID,
            "--target-device",
            "vtopt0",
            "--ownership-override",
        ],
        input="y\n",
    )

    assert result.exit_code == 0
    mount.assert_awaited_once_with(
        fake_hmc,
        VIOS_UUID,
        LPAR_UUID,
        media_name="install.iso",
        target_device="vtopt0",
        ownership_override=True,
        system_name_or_uuid=SYSTEM_UUID,
    )
    assert "target\tinstall.iso" in result.stdout
    assert SYSTEM_UUID in result.stdout
    assert "vtopt0" in result.stdout
    assert '"mapping-1"' not in result.stdout
    assert "CurrentProfileSync is Disabled" in result.stdout


def test_storage_unmount_optical_media_forwards_selectors(fake_hmc, monkeypatch):
    unmount = AsyncMock(return_value=UNSYNCED)
    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.unmount_optical_media", unmount
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "unmount-optical-media",
            VIOS_UUID,
            "--lpar",
            LPAR_UUID,
            "--name",
            "install.iso",
            "--system",
            SYSTEM_UUID,
            "--ownership-override",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    unmount.assert_awaited_once_with(
        fake_hmc,
        VIOS_UUID,
        LPAR_UUID,
        media_name="install.iso",
        ownership_override=True,
        system_name_or_uuid=SYSTEM_UUID,
    )
    assert "target\tinstall.iso" in result.stdout
    assert "backing_storage\tretained" in result.stdout
    assert "CurrentProfileSync is Disabled" in result.stdout


@pytest.mark.usefixtures("storage_terminal_input")
def test_storage_mount_optical_media_decline_does_not_mutate(fake_hmc, monkeypatch):
    mount = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.mount_optical_media", mount
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "mount-optical-media",
            VIOS_UUID,
            "--lpar",
            LPAR_UUID,
            "--name",
            "install.iso",
        ],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    mount.assert_not_awaited()


@pytest.mark.usefixtures("storage_terminal_input")
def test_storage_unmount_optical_media_decline_does_not_mutate(fake_hmc, monkeypatch):
    unmount = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.unmount_optical_media", unmount
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "unmount-optical-media",
            VIOS_UUID,
            "--lpar",
            LPAR_UUID,
            "--name",
            "install.iso",
        ],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    unmount.assert_not_awaited()


def test_storage_optical_media_command_help():
    result = RUNNER.invoke(cli.app, ["storage", "mount-optical-media", "--help"])
    assert result.exit_code == 0
    help_text = click.unstyle(result.stdout)
    assert "--system" in help_text
    assert "--target-device" in help_text
    assert "--ownership-override" in help_text
    assert "--yes" in help_text

    result = RUNNER.invoke(cli.app, ["storage", "unmount-optical-media", "--help"])
    assert result.exit_code == 0
    help_text = click.unstyle(result.stdout)
    assert "--system" in help_text
    assert "--ownership-override" in help_text
    assert "--yes" in help_text


def test_storage_get_media_repo_renders_name_and_size(fake_hmc, monkeypatch):
    async def fake_get(_hmc, vios, vg, *, system_name_or_uuid=None):
        assert (vios, vg) == (VIOS_UUID, VG_UUID)
        return {"Resource": {"RepositoryName": "VMLibrary", "RepositorySize": "10240"}}

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.get_media_repository", fake_get
    )

    result = RUNNER.invoke(
        cli.app, ["storage", "get-media-repo", VIOS_UUID, "--vg", VG_UUID]
    )

    assert result.exit_code == 0
    assert "VMLibrary" in result.stdout
    assert "10240" in result.stdout


def test_storage_get_media_repo_labels_the_size_in_gib(fake_hmc, monkeypatch):
    """RepositorySize is GiB on the HMC (#963); the rendered unit must say so."""

    async def fake_get(_hmc, _vios, _vg, *, system_name_or_uuid=None):
        return {"Resource": {"RepositoryName": "VMLibrary", "RepositorySize": "64"}}

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.get_media_repository", fake_get
    )

    result = RUNNER.invoke(
        cli.app,
        ["storage", "get-media-repo", VIOS_UUID, "--vg", VG_UUID, "--format", "table"],
    )

    assert result.exit_code == 0
    assert "RepositorySize in GiB" in result.stdout
    assert '"RepositorySize":"64"' in result.stdout


def test_storage_get_media_repo_reports_empty(fake_hmc, monkeypatch):
    async def fake_get(_hmc, _vios, _vg, *, system_name_or_uuid=None):
        return {}

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.get_media_repository", fake_get
    )

    result = RUNNER.invoke(
        cli.app, ["storage", "get-media-repo", VIOS_UUID, "--vg", VG_UUID]
    )

    assert result.exit_code == 0
    assert "No media repository found" in result.stderr


def test_storage_get_media_repo_json(fake_hmc, monkeypatch):
    async def fake_get(_hmc, _vios, _vg, *, system_name_or_uuid=None):
        return {"Resource": {"RepositoryName": "VMLibrary"}}

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.get_media_repository", fake_get
    )

    result = RUNNER.invoke(
        cli.app, ["storage", "get-media-repo", VIOS_UUID, "--vg", VG_UUID, "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"Resource": {"RepositoryName": "VMLibrary"}}


def test_storage_list_optical_media_renders_a_table(fake_hmc, monkeypatch):
    async def fake_list(_hmc, vios, vg, *, system_name_or_uuid=None):
        assert (vios, vg) == (VIOS_UUID, VG_UUID)
        return [OpticalMedia("aix.iso", 4096)]

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.list_optical_media", fake_list
    )

    result = RUNNER.invoke(
        cli.app, ["storage", "list-optical-media", VIOS_UUID, "--vg", VG_UUID]
    )

    assert result.exit_code == 0
    assert "aix.iso" in result.stdout
    assert "4096" in result.stdout
    assert "Type" not in result.stdout


def test_storage_list_optical_media_reports_empty(fake_hmc, monkeypatch):
    async def fake_list(_hmc, _vios, _vg, *, system_name_or_uuid=None):
        return []

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.list_optical_media", fake_list
    )

    result = RUNNER.invoke(
        cli.app, ["storage", "list-optical-media", VIOS_UUID, "--vg", VG_UUID]
    )

    assert result.exit_code == 0
    assert "No optical media found" in result.stderr


def test_storage_list_optical_media_json(fake_hmc, monkeypatch):
    async def fake_list(_hmc, _vios, _vg, *, system_name_or_uuid=None):
        return [OpticalMedia("aix.iso", None)]

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.list_optical_media", fake_list
    )

    result = RUNNER.invoke(
        cli.app, ["storage", "list-optical-media", VIOS_UUID, "--vg", VG_UUID, "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [{"name": "aix.iso", "size_mib": None}]


def test_storage_list_mappings_renders_virtual_disk(fake_hmc, monkeypatch):
    async def fake_mappings(_hmc, vios, lpar, *, system_name_or_uuid=None):
        system = system_name_or_uuid
        assert (system, vios, lpar) == (None, VIOS_UUID, None)
        return [StorageMapping("vhost0/vtscsi0", "lpar1", "VirtualDisk", "bootvol")]

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.list_storage_mappings", fake_mappings
    )

    result = RUNNER.invoke(cli.app, ["storage", "list-mappings", VIOS_UUID])

    assert result.exit_code == 0
    assert "vhost0/vtscsi0" in result.stdout
    assert "bootvol" in result.stdout
    assert "VirtualDisk" in result.stdout


def test_storage_list_mappings_renders_physical_volume(fake_hmc, monkeypatch):
    async def fake_mappings(_hmc, vios, lpar, *, system_name_or_uuid=None):
        system = system_name_or_uuid
        assert (system, vios, lpar) == (None, VIOS_UUID, LPAR_UUID)
        return [StorageMapping("vhost1/vtscsi1", "lpar1", "PhysicalVolume", "hdisk9")]

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.list_storage_mappings", fake_mappings
    )

    result = RUNNER.invoke(
        cli.app, ["storage", "list-mappings", VIOS_UUID, "--lpar", LPAR_UUID]
    )

    assert result.exit_code == 0
    assert "hdisk9" in result.stdout
    assert "PhysicalVolume" in result.stdout


def test_storage_list_mappings_json(fake_hmc, monkeypatch):
    async def fake_mappings(_hmc, _vios, _lpar, *, system_name_or_uuid=None):
        return [
            StorageMapping("vhost0/vtscsi0", None, None, None),
            StorageMapping(None, None, None, None),
        ]

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.list_storage_mappings", fake_mappings
    )

    result = RUNNER.invoke(cli.app, ["storage", "list-mappings", VIOS_UUID, "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [
        {
            "id": "vhost0/vtscsi0",
            "lpar_uuid": None,
            "backing_kind": None,
            "backing_name": None,
        },
        {"id": None, "lpar_uuid": None, "backing_kind": None, "backing_name": None},
    ]


def test_storage_detach_mapping_deletes_when_confirmed(fake_hmc, monkeypatch):
    seen = {}

    async def fake_detach(
        _hmc, vios, mapping_id, *, system_name_or_uuid=None, ownership_override
    ):
        system = system_name_or_uuid
        seen.update(
            system=system,
            vios=vios,
            mapping_id=mapping_id,
            ownership_override=ownership_override,
        )
        return UNSYNCED

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.detach_storage_mapping", fake_detach
    )

    result = RUNNER.invoke(
        cli.app, ["storage", "detach-mapping", VIOS_UUID, "vhost0/vtscsi0", "--yes"]
    )

    assert result.exit_code == 0
    assert "target\tvhost0/vtscsi0" in result.stdout
    assert "CurrentProfileSync is Disabled" in result.stdout
    assert seen == {
        "system": None,
        "vios": VIOS_UUID,
        "mapping_id": "vhost0/vtscsi0",
        "ownership_override": False,
    }


def test_storage_detach_mapping_reports_one_failure_and_exits_1(fake_hmc, monkeypatch):
    """A failing detach reports once, on stderr, and exits 1.

    The command used to wrap ``_run`` in ``except Exception``, which caught
    ``_run``'s own ``typer.Exit`` sentinel -- ``typer.Exit`` subclasses
    ``RuntimeError`` -- and printed a second, information-free line on stdout.
    """

    async def fake_detach(
        _hmc,
        _vios,
        _mapping_id,
        *,
        system_name_or_uuid=None,
        ownership_override: bool,
    ):
        assert not ownership_override
        raise HMCError("mapping is in use")

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.detach_storage_mapping", fake_detach
    )

    result = RUNNER.invoke(
        cli.app, ["storage", "detach-mapping", VIOS_UUID, "vhost0/vtscsi0", "--yes"]
    )

    assert result.exit_code == 1
    assert "Error: mapping is in use" in result.stderr
    assert "Failed to delete storage mapping" not in result.stdout
    assert "Deleted storage mapping" not in result.stdout


def test_run_propagates_a_typer_exit_code_unchanged():
    """``_run`` must not rewrite a closure's chosen exit code to 1.

    ``typer.Exit`` subclasses ``RuntimeError``, so the catch-all would otherwise
    route the sentinel through ``fail`` and report exit 1 -- turning a usage
    error (code 2) into a generic failure.
    """

    async def _go() -> None:
        raise typer.Exit(code=2)

    with pytest.raises(typer.Exit) as excinfo:
        cli_runtime.run_cli_coroutine(_go)

    assert excinfo.value.exit_code == 2


def test_with_client_propagates_a_typer_exit_code_unchanged(monkeypatch):
    """``with_client`` shares ``_run``'s control-flow passthrough."""

    def boom(coroutine):
        coroutine.close()
        raise typer.Exit(code=2)

    monkeypatch.setattr("hmcpctl.cli_commands.runtime.asyncio.run", boom)

    with pytest.raises(typer.Exit) as excinfo:
        cli_runtime.with_client(lambda hmc: None)

    assert excinfo.value.exit_code == 2


def test_storage_upload_iso_reports_uploaded_media(fake_hmc, monkeypatch):
    async def fake_upload(
        _hmc, vios, vg, media_name, iso_source, *, system_name_or_uuid
    ):
        assert (vios, vg, media_name) == (VIOS_UUID, VG_UUID, "aix.iso")
        assert iso_source == "https://images.test/aix.iso"
        assert system_name_or_uuid is None
        return {
            "status": "uploaded",
            "media_name": "aix.iso",
            "media_size_bytes": 1048576,
            "source_sha256": "abc123",
            "media": {"MediaName": "aix.iso"},
        }

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.upload_iso", fake_upload
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "upload-iso",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "aix.iso",
            "https://images.test/aix.iso",
        ],
    )

    assert result.exit_code == 0
    assert "outcome\tuploaded" in result.stdout
    assert "target\taix.iso" in result.stdout
    assert "media_size_bytes\t1048576" in result.stdout


def test_storage_upload_iso_json(fake_hmc, monkeypatch):
    async def fake_upload(
        _hmc, _vios, _vg, _media_name, _iso_source, *, system_name_or_uuid
    ):
        assert system_name_or_uuid is None
        return {"status": "uploaded", "media_name": "aix.iso"}

    monkeypatch.setattr(
        "hmcpctl.cli_commands.storage.resources.upload_iso", fake_upload
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "upload-iso",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "aix.iso",
            "https://images.test/aix.iso",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "uploaded", "media_name": "aix.iso"}


# --------------------------------------------------------------------------- #
# failure path (fail)
# --------------------------------------------------------------------------- #


def test_client_error_reports_and_exits_1(fake_hmc):
    fake_hmc.fail_on = "list_logical_partitions"
    result = RUNNER.invoke(cli.app, ["lpars", "list"])

    assert result.exit_code == 1
    assert "Error:" in result.stderr
    assert "boom" in result.stderr


# --------------------------------------------------------------------------- #
# SSH-backed commands (run_hmc_command seam)
# --------------------------------------------------------------------------- #


def test_lpars_get_description_via_ssh(monkeypatch):
    async def fake(cfg, cmd):
        return "my lpar description\n"

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(cli.app, ["lpars", "get-description", "lpar1", "sys1"])

    assert result.exit_code == 0
    assert "my lpar description" in result.stdout


def test_lpars_get_description_preserves_bracketed_ownership_stamp(monkeypatch):
    """A bracketed ownership stamp round-trips intact through the CLI -- #965.

    Rich's console treats an unescaped ``[...]`` segment as a markup tag and
    drops it, so an HMC-returned description shaped like the ownership stamp
    must reach the terminal verbatim, brackets included.
    """

    async def fake(cfg, cmd):
        return OWNERSHIP_STAMP + "\n"

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(cli.app, ["lpars", "get-description", "lpar1", "sys1"])

    assert result.exit_code == 0
    assert OWNERSHIP_STAMP in result.stdout


def test_lpars_get_msp_via_ssh(monkeypatch):
    async def fake(cfg, cmd):
        return "1\n"

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(cli.app, ["lpars", "get-msp", "lpar1", "sys1"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "enabled"


def test_lpars_refcodes_via_ssh(monkeypatch):
    captured: dict[str, str] = {}

    async def fake(cfg, cmd):
        captured["cmd"] = cmd
        return (
            "lpar_name,time_stamp,refcode\n"
            "web01,2026-09-21 10:00:00,C2001150\n"
            "web01,2026-09-21 09:59:00,C2001140\n"
        )

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(
        cli.app, ["lpars", "refcodes", "sys1", "web01", "--count", "3", "--json"]
    )

    assert result.exit_code == 0, result.stdout
    assert captured["cmd"] == (
        "lsrefcode -r lpar -m sys1 --filter lpar_names=web01"
        " -n 3 -F lpar_name,time_stamp,refcode --header"
    )
    rows = json.loads(result.stdout)
    assert all(row["definition"] for row in rows)
    assert [
        {k: row[k] for k in ("lpar_name", "time_stamp", "refcode")} for row in rows
    ] == [
        {
            "lpar_name": "web01",
            "time_stamp": "2026-09-21 10:00:00",
            "refcode": "C2001150",
        },
        {
            "lpar_name": "web01",
            "time_stamp": "2026-09-21 09:59:00",
            "refcode": "C2001140",
        },
    ]


def test_lpars_refcodes_reports_an_empty_read(monkeypatch):
    async def fake(cfg, cmd):
        return "No results were found.\n"

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(cli.app, ["lpars", "refcodes", "sys1", "web01"])

    assert result.exit_code == 0, result.stdout
    assert "No reference codes found" in result.output


def test_lpars_memopt_score_via_ssh(monkeypatch):
    captured: dict[str, str] = {}

    async def fake(cfg, cmd):
        captured["cmd"] = cmd
        return "lpar_name=lpar1,lpar_id=1,curr_lpar_score=100\n"

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(cli.app, ["lpars", "memopt-score", "lpar1", "sys1"])

    assert result.exit_code == 0
    assert captured["cmd"] == (
        "lsmemopt -m sys1 -r lpar -o currscore --filter lpar_names=lpar1"
    )
    assert "100" in result.stdout


def test_lpars_memopt_score_json(monkeypatch):
    async def fake(cfg, cmd):
        return "lpar_name=lpar1,lpar_id=1,curr_lpar_score=none\n"

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(
        cli.app, ["lpars", "memopt-score", "lpar1", "sys1", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "lpar_name": "lpar1",
        "lpar_id": "1",
        "curr_lpar_score": "none",
    }


def test_lpars_memopt_score_error(monkeypatch):
    async def fake(cfg, cmd):
        raise ssh_commands.HMCCLIError("The partition named lpar1 was not found.")

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(cli.app, ["lpars", "memopt-score", "lpar1", "sys1"])

    assert result.exit_code == 1
    assert "Error:" in result.stderr


def test_lpars_memopt_scores_via_ssh(monkeypatch):
    captured: dict[str, str] = {}

    async def fake(cfg, cmd):
        captured["cmd"] = cmd
        return (
            "lpar_name=lpar1,lpar_id=1,curr_lpar_score=100\n"
            "lpar_name=lpar2,lpar_id=2,curr_lpar_score=95\n"
        )

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(cli.app, ["lpars", "memopt-scores", "sys1"])

    assert result.exit_code == 0
    assert captured["cmd"] == "lsmemopt -m sys1 -r lpar -o currscore"
    assert "lpar2" in result.stdout


def test_lpars_memopt_scores_with_lpar_filter(monkeypatch):
    captured: dict[str, str] = {}

    async def fake(cfg, cmd):
        captured["cmd"] = cmd
        return "lpar_name=lpar1,lpar_id=1,curr_lpar_score=100\n"

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(
        cli.app, ["lpars", "memopt-scores", "sys1", "--lpar", "lpar1", "--json"]
    )

    assert result.exit_code == 0
    assert captured["cmd"] == (
        "lsmemopt -m sys1 -r lpar -o currscore --filter lpar_names=lpar1"
    )
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert data[0]["lpar_name"] == "lpar1"


def test_lpars_memopt_scores_missing_system(monkeypatch):
    result = RUNNER.invoke(cli.app, ["lpars", "memopt-scores"])

    assert result.exit_code == 2
    assert "Missing" in result.stderr


def test_lpars_memopt_scores_empty(monkeypatch):
    async def fake(cfg, cmd):
        return ""

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(cli.app, ["lpars", "memopt-scores", "sys1"])

    assert result.exit_code == 0
    assert "No memory-optimization scores reported" in result.stdout


@pytest.mark.parametrize(
    ("command", "operation", "payload", "expected"),
    [
        (
            "system-memopt-score",
            "get_system_memopt_score",
            {"curr_sys_score": "72"},
            "current: 72",
        ),
        (
            "plan-memopt-scores",
            "plan_lpar_memopt_scores",
            [
                {
                    "lpar_name": "web",
                    "lpar_id": "3",
                    "curr_lpar_score": "61",
                    "predicted_lpar_score": "84",
                    "prediction_guaranteed": False,
                }
            ],
            "prediction guaranteed: no",
        ),
        (
            "plan-system-memopt-score",
            "plan_system_memopt_score",
            {
                "curr_sys_score": "70",
                "predicted_sys_score": "88",
                "prediction_guaranteed": False,
            },
            "predicted: 88",
        ),
    ],
)
def test_affinity_cli_human_output(monkeypatch, command, operation, payload, expected):
    async def fake_operation(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(cli_lpars, operation, fake_operation)
    result = RUNNER.invoke(cli.app, ["lpars", command, "sys1"])

    assert result.exit_code == 0
    assert expected in unstyle(result.stdout)


def test_affinity_planning_cli_json_and_name_selectors(monkeypatch):
    captured = {}

    async def fake_operation(_config, system, prioritized, excluded):
        captured.update(system=system, prioritized=prioritized, excluded=excluded)
        return [{"prediction_guaranteed": False}]

    monkeypatch.setattr(cli_lpars, "plan_lpar_memopt_scores", fake_operation)
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "plan-memopt-scores",
            "sys1",
            "--prioritize-name",
            "web",
            "--prioritize-name",
            "api",
            "--exclude-name",
            "batch",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [{"prediction_guaranteed": False}]
    assert captured["system"] == "sys1"
    assert captured["prioritized"].names == ("web", "api")
    assert captured["excluded"].names == ("batch",)


def test_affinity_planning_cli_id_selectors(monkeypatch):
    captured = {}

    async def fake_operation(_config, _system, prioritized, excluded):
        captured.update(prioritized=prioritized, excluded=excluded)
        return {"prediction_guaranteed": False}

    monkeypatch.setattr(cli_lpars, "plan_system_memopt_score", fake_operation)
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "plan-system-memopt-score",
            "sys1",
            "--prioritize-id",
            "2",
            "--exclude-id",
            "9",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["prioritized"].ids == (2,)
    assert captured["excluded"].ids == (9,)


def test_affinity_planning_cli_rejects_mixed_selector_forms(monkeypatch):
    called = False

    async def fake_operation(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cli_lpars, "plan_lpar_memopt_scores", fake_operation)
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "plan-memopt-scores",
            "sys1",
            "--prioritize-name",
            "web",
            "--exclude-id",
            "9",
        ],
    )

    assert result.exit_code == 2
    assert "must use the same representation" in result.output
    assert called is False


def test_affinity_planning_cli_rejects_oversized_selector_before_operation(
    monkeypatch,
):
    operation = AsyncMock()
    monkeypatch.setattr(cli_lpars, "plan_lpar_memopt_scores", operation)

    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "plan-memopt-scores",
            "sys1",
            "--prioritize-name",
            "'" * 400 + "a" * 42,
            "--exclude-name",
            "'" * 400 + "b" * 43,
        ],
    )

    assert result.exit_code == 2
    assert "option package exceeds 4096 UTF-8 bytes" in result.output
    operation.assert_not_awaited()


def test_affinity_planning_cli_rejects_overlapping_selectors(monkeypatch):
    called = False

    async def fake_operation(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cli_lpars, "plan_system_memopt_score", fake_operation)
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "plan-system-memopt-score",
            "sys1",
            "--prioritize-name",
            "web",
            "--exclude-name",
            "web",
        ],
    )

    assert result.exit_code == 2
    assert "must not overlap" in result.output
    assert called is False


def test_affinity_cli_propagates_hmc_errors(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise ssh_commands.HMCCLIError("calcscore unavailable")

    monkeypatch.setattr(cli_lpars, "plan_system_memopt_score", fail)
    result = RUNNER.invoke(cli.app, ["lpars", "plan-system-memopt-score", "sys1"])

    assert result.exit_code == 1
    assert "calcscore unavailable" in result.output


@pytest.mark.parametrize(
    ("args", "expected_call"),
    [
        (
            [
                "lpars",
                "migrate",
                LPAR_NAME,
                "--target",
                "sys1",
                "--profile",
                "target-profile",
                "--wait-time",
                "60",
                "--yes",
            ],
            (
                "lpar_migrate",
                (LPAR_UUID, "sys1", "target-profile"),
                {"wait_time": 60},
            ),
        ),
        (
            ["lpars", "migrate-validate", LPAR_NAME, "--target", "sys1", "--yes"],
            (
                "lpar_migrate_validate",
                (LPAR_UUID, "sys1", None),
                {"wait_time": None},
            ),
        ),
        (
            ["lpars", "migrate-abort", LPAR_NAME, "--yes"],
            ("lpar_migrate_abort", (LPAR_UUID,), {}),
        ),
        (
            ["lpars", "migrate-recover", LPAR_NAME, "--yes"],
            ("lpar_migrate_recover", (LPAR_UUID,), {}),
        ),
        (
            [
                "lpars",
                "remote-restart",
                LPAR_NAME,
                "--operation",
                "restart",
                "--system",
                "sys1",
                "--target",
                "target",
                "--yes",
            ],
            (
                "lpar_remote_restart",
                (LPAR_UUID, "restart", "sys1"),
                {
                    "target_managed_system": "target",
                    "target_managed_system_uuid": None,
                    "use_current_data": False,
                    "retain_devices": False,
                },
            ),
        ),
    ],
)
def test_lpm_commands_delegate_resolved_arguments(fake_hmc, args, expected_call):
    result = RUNNER.invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert expected_call in fake_hmc.calls
    assert "Submitted" in result.stdout
    assert LPAR_UUID in result.stdout


def test_migrate_cli_defaults_to_validation_first(fake_hmc):
    result = RUNNER.invoke(
        cli.app, ["lpars", "migrate", LPAR_NAME, "--target", "sys1", "--yes"]
    )

    assert result.exit_code == 0, result.output
    names = [name for name, _args, _kwargs in fake_hmc.calls]
    assert names.index("lpar_migrate_validate") < names.index("lpar_migrate")
    assert "wait_for_job_entry" in names


def test_migrate_cli_can_bypass_validation(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "migrate",
            LPAR_NAME,
            "--target",
            "sys1",
            "--no-validate-first",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    names = [name for name, _args, _kwargs in fake_hmc.calls]
    assert "lpar_migrate_validate" not in names
    assert "lpar_migrate" in names


@pytest.mark.parametrize(
    ("args", "expected_request", "expected_controls"),
    [
        (
            ["lpars", "migrate-affinity", LPAR_NAME, "--target", "sys1", "--yes"],
            {
                "source_current_score": None,
                "destination_estimated_score": None,
                "destination_check_basis": "calculated",
                "configured_minimum": None,
                "capability": "available",
                "response": "warn",
                "preflight_timeout_seconds": 5.0,
            },
            {
                "wait": False,
                "timeout_seconds": 300,
                "poll_interval": 5,
                "ownership_override": False,
            },
        ),
        (
            [
                "lpars",
                "migrate-affinity",
                LPAR_NAME,
                "--target",
                "sys1",
                "--source-score",
                "90",
                "--destination-estimate",
                "80",
                "--check-basis",
                "migration-check",
                "--configured-minimum",
                "70",
                "--capability",
                "unavailable",
                "--response",
                "fail",
                "--preflight-timeout",
                "1.25",
                "--wait",
                "--timeout",
                "120",
                "--interval",
                "10",
                "--ownership-override",
                "--yes",
            ],
            {
                "source_current_score": 90,
                "destination_estimated_score": 80,
                "destination_check_basis": "migration-check",
                "configured_minimum": 70,
                "capability": "unavailable",
                "response": "fail",
                "preflight_timeout_seconds": 1.25,
            },
            {
                "wait": True,
                "timeout_seconds": 120,
                "poll_interval": 10,
                "ownership_override": True,
            },
        ),
    ],
)
def test_migrate_affinity_cli_builds_preflight_request(
    monkeypatch, args, expected_request, expected_controls
):
    calls = []

    async def migrate_with_preflight(*operation_args, **operation_controls):
        calls.append((operation_args, operation_controls))

    def run_operation(_name, operation, _action, _target, _yes):
        asyncio.run(operation(object()))

    monkeypatch.setattr(
        cli_lpar_migration,
        "migrate_lpar_with_affinity_preflight",
        migrate_with_preflight,
    )
    monkeypatch.setattr(cli_lpar_migration, "_lpm_run", run_operation)

    result = RUNNER.invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    [(operation_args, operation_controls)] = calls
    assert operation_args[1] is None
    assert operation_args[2] == LPAR_NAME
    assert operation_args[3].target_system_name_or_uuid == "sys1"
    request = operation_args[4]
    for field, expected in expected_request.items():
        assert getattr(request, field) == expected
    assert request.capability_limits == (
        "Destination affinity is estimated, not guaranteed.",
    )
    assert operation_controls == expected_controls


def test_migrate_cli_rejects_effective_wait_timing_before_confirmation(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "migrate", LPAR_NAME, "--target", "sys1", "--interval", "0"],
    )

    assert result.exit_code == 1
    assert fake_hmc.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["lpars", "migrate-abort", LPAR_NAME, "--yes"],
        ["lpars", "migrate-recover", LPAR_NAME, "--yes"],
        [
            "lpars",
            "remote-restart",
            LPAR_NAME,
            "--operation",
            "restart",
            "--system",
            "sys1",
            "--target",
            "target",
            "--yes",
        ],
    ],
)
def test_lpm_recovery_commands_forward_wait_timing(fake_hmc, args):
    result = RUNNER.invoke(
        cli.app, [*args, "--wait", "--timeout", "60", "--interval", "2"]
    )

    assert result.exit_code == 0, result.output
    assert (
        "wait_for_job_entry",
        (JOB_UUID, 60, 2),
        {"job_href": f"/jobs/{JOB_UUID}"},
    ) in fake_hmc.calls


def test_lpm_recovery_command_renders_timeout_outcome(fake_hmc):
    fake_hmc.wait_job_status = "RUNNING"
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "migrate-abort",
            LPAR_NAME,
            "--yes",
            "--wait",
            "--timeout",
            "0",
            "--interval",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f'"job_id": "{JOB_UUID}"' in result.stdout
    assert '"status": "RUNNING"' in result.stdout
    assert '"timed_out": true' in result.stdout
    assert '"error": null' in result.stdout
    assert '"job": {' in result.stdout


def test_lpm_recovery_command_rejects_invalid_timing_before_submission(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "migrate-abort",
            LPAR_NAME,
            "--yes",
            "--wait",
            "--timeout",
            "-1",
        ],
    )

    assert result.exit_code == 1
    assert fake_hmc.calls == []


@pytest.mark.parametrize(
    ("args", "command_fragments"),
    [
        (
            ["lpars", "set-description", "lpar1", "sys1", "new text", "--yes"],
            ("chsyscfg", "name=lpar1", "description=new text"),
        ),
        (
            ["lpars", "set-msp", "vios1", "sys1", "true", "--yes"],
            ("chsyscfg", "name=vios1", "msp=1"),
        ),
        (
            ["lpars", "set-proc-compat", "lpar1", "sys1", "POWER10", "--yes"],
            ("chsyscfg -r prof", "lpar_name=lpar1", "lpar_proc_compat_mode=POWER10"),
        ),
        (
            ["network", "set-sriov-mode", "sys1", "1", "sriov"],
            ("lshwres", "sriov", "adapter"),
        ),
    ],
)
def test_destructive_ssh_commands_delegate_valid_arguments(
    monkeypatch, fake_hmc, args, command_fragments
):
    commands: list[str] = []

    async def fake(_config, command):
        commands.append(command)
        if command == "lshmc -V":
            return "Version: 10\nRelease: 3\nService Pack: 1060\n"
        if "-r sys" in command and "type_model" in command:
            return "8375-42A\n"
        if "--rsubtype adapter" in command:
            fields = "adapter_id,slot_id,config_state,functional_state,phys_loc,phys_ports,logical_ports,adapter_max_logical_ports,sriov_status"
            return f"{fields}\n1,21010020,sriov,1,U,2,120,120,running\n"
        if command.startswith("lssyscfg"):
            return "vioserver\n"
        return "updated\n"

    _patch_ssh_command(monkeypatch, fake)

    result = RUNNER.invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert all(fragment in commands[-1] for fragment in command_fragments)


@pytest.mark.parametrize(
    "args",
    [
        ["lpars", "set-description", "lpar1", "sys1", "new text", "--yes"],
        ["lpars", "set-msp", "vios1", "sys1", "true", "--yes"],
    ],
)
def test_destructive_ssh_commands_preserve_bracketed_result(
    monkeypatch, fake_hmc, args
):
    """The trailing HMC result line round-trips a bracketed stamp intact -- #965."""

    async def fake(_config, command):
        if command == "lshmc -V":
            return "Version: 10\nRelease: 3\nService Pack: 1060\n"
        if "-r sys" in command and "type_model" in command:
            return "8375-42A\n"
        if command.startswith("lssyscfg"):
            return "vioserver\n"
        return OWNERSHIP_STAMP + "\n"

    _patch_ssh_command(monkeypatch, fake)

    result = RUNNER.invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert OWNERSHIP_STAMP in result.stdout


def test_network_set_sriov_mode_preserves_bracketed_result(monkeypatch, fake_hmc):
    """The trailing confirmation line round-trips a bracketed stamp intact -- #965.

    ``set_sriov_adapter_mode`` never returns raw HMC stdout on the verified-
    already-in-mode path it exercises today, so the operation is stubbed
    directly to prove the CLI's own print call (pcie.py) escapes whatever
    that operation returns.
    """

    async def fake_set_mode(hmc, system_name_or_uuid, adapter_id, mode):
        return OWNERSHIP_STAMP

    monkeypatch.setattr(
        "hmcpctl.cli_commands.virtualization.pcie.set_sriov_adapter_mode",
        fake_set_mode,
    )
    result = RUNNER.invoke(cli.app, ["network", "set-sriov-mode", "sys1", "1", "sriov"])

    assert result.exit_code == 0, result.output
    assert OWNERSHIP_STAMP in result.stdout


def test_add_vnic_cli_replaces_legacy_options():
    result = RUNNER.invoke(cli.app, ["network", "add-vnic", "--help"])

    assert result.exit_code == 0
    output = unstyle(result.output)
    for option in (
        "--vios-name",
        "--vios-lpar-id",
        "--adapter-id",
        "--physical-port-id",
        "--capacity-percent",
        "--port-vlan-id",
    ):
        assert option in output
    for legacy in ("--backing-devices", "--virtual-switch-name", "--capacity "):
        assert legacy not in output


def test_remove_vnic_cli_names_slot_selector():
    result = RUNNER.invoke(cli.app, ["network", "remove-vnic", "--help"])

    assert result.exit_code == 0
    assert "slot_num" in result.output
    assert "vnic_id" not in result.output


def test_lpars_set_msp_refuses_foreign_vios_owner(monkeypatch, fake_hmc):
    """The CLI crosses the operations-layer ownership guard before chsyscfg (#1318)."""
    commands: list[str] = []

    async def fake(_config, command):
        commands.append(command)
        return "vioserver\n"

    async def foreign(*_args):
        return "[hmcpctl owner:another-agent created:2026-08-14]"

    _patch_ssh_command(monkeypatch, fake)
    monkeypatch.setattr(lpar_ownership, "get_lpar_description", foreign)

    result = RUNNER.invoke(
        cli.app, ["lpars", "set-msp", "vios1", "sys1", "true", "--yes"]
    )

    assert result.exit_code != 0
    assert "ownership_override=true" in str(result.exception) + result.output
    assert not any(command.startswith("chsyscfg") for command in commands)


def test_lpars_set_msp_forwards_ownership_override(monkeypatch, fake_hmc):
    """``--ownership-override`` reaches the operation as ``ownership_override``."""
    operation = AsyncMock(return_value="")
    monkeypatch.setattr(cli_lpars, "configure_lpar_msp", operation)

    result = RUNNER.invoke(
        cli.app,
        ["lpars", "set-msp", "vios1", "sys1", "false", "--yes", "--ownership-override"],
    )

    assert result.exit_code == 0, result.output
    operation.assert_awaited_once()
    assert operation.call_args.args[1:] == ("sys1", "vios1", False)
    assert operation.call_args.kwargs == {"ownership_override": True}


@pytest.mark.parametrize("command", ["add-vnic", "remove-vnic"])
def test_vnic_mutation_cli_exposes_ownership_override(command):
    result = RUNNER.invoke(cli.app, ["network", command, "--help"])

    assert result.exit_code == 0
    assert "--ownership-override" in unstyle(result.output)


def _vnic_result(operation: str) -> VnicChangeResult:
    return VnicChangeResult(
        operation=operation,  # type: ignore[arg-type]
        mutation_dispatched=True,
        changed=True,
        selector=None,
        slot_num="4",
        vnic_before=(),
        backing_before=(),
        vnic_after=(),
        backing_after=(),
        vnic_after_read_succeeded=True,
        backing_after_read_succeeded=True,
        output="done",
        errors=(),
    )


def test_add_vnic_cli_default_confirmation_keeps_stdout_json(monkeypatch):
    operation = AsyncMock(return_value=_vnic_result("add"))
    monkeypatch.setattr("hmcpctl.cli_commands.virtualization.vnic.add_vnic", operation)
    monkeypatch.setattr(
        "hmcpctl.cli_commands.virtualization.vnic.with_client",
        lambda fn: asyncio.run(fn(object())),
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "network",
            "add-vnic",
            "system",
            "lpar",
            "--vios-name",
            "vios1",
            "--vios-lpar-id",
            "2",
            "--adapter-id",
            "1",
            "--physical-port-id",
            "0",
            "--capacity-percent",
            "20.25",
            "--port-vlan-id",
            "100",
            "--ownership-override",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["operation"] == "add"
    assert "Add vNIC" in result.stderr
    assert operation.await_args.kwargs == {"ownership_override": True}


def test_remove_vnic_cli_default_confirmation_keeps_partial_stdout_json(monkeypatch):
    partial = VnicPartialError("incomplete", _vnic_result("remove"))
    operation = AsyncMock(side_effect=partial)
    monkeypatch.setattr(
        "hmcpctl.cli_commands.virtualization.vnic.remove_vnic", operation
    )
    monkeypatch.setattr(
        "hmcpctl.cli_commands.virtualization.vnic.with_client",
        lambda fn: asyncio.run(fn(object())),
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "network",
            "remove-vnic",
            "system",
            "lpar",
            "4",
            "--ownership-override",
        ],
        input="y\n",
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["operation"] == "remove"
    assert "Remove vNIC" in result.stderr
    assert operation.await_args.kwargs == {"ownership_override": True}


# --------------------------------------------------------------------------- #
# network I/O slots (SSH-backed)
# --------------------------------------------------------------------------- #


def test_network_list_io_slots_via_ssh(monkeypatch):
    async def fake(cfg, cmd):
        return live_fixture("cli-io-slots-default")["stdout"]

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(cli.app, ["network", "list-io-slots", "sys-R1"])

    assert result.exit_code == 0
    assert "21020013" in result.stdout
    assert "sys-R1-vios1" in result.stdout


def test_network_list_io_slots_invalid_pci_class_exits_2(monkeypatch):
    async def fake(cfg, cmd):
        return ""

    _patch_ssh_command(monkeypatch, fake)
    result = RUNNER.invoke(
        cli.app, ["network", "list-io-slots", "sys1", "--pci-class", "bogus"]
    )

    assert result.exit_code == 2
    assert "bogus" in result.stderr
    assert "nvme" in result.stderr


# --------------------------------------------------------------------------- #
# raw REST escape hatch
# --------------------------------------------------------------------------- #


def test_raw_post_requires_confirmation(fake_hmc):
    """raw post is gated by the same --yes/confirm convention as other mutating commands."""
    result = RUNNER.invoke(
        cli.app,
        ["raw", "post", "/rest/api/uom/LogicalPartition", "<lpar/>"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


def test_raw_get_prints_response_body(fake_hmc):
    result = RUNNER.invoke(cli.app, ["raw", "get", "/rest/api/uom/VirtualSwitch"])

    assert result.exit_code == 0
    assert "<response/>" in result.stdout
    assert fake_hmc.calls == [("raw_get", ("/rest/api/uom/VirtualSwitch",), {})]


def test_raw_post_with_yes_sends_request(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["raw", "post", "/rest/api/uom/LogicalPartition", "<lpar/>", "--yes"],
    )

    assert result.exit_code == 0
    name, args, _ = fake_hmc.calls[0]
    assert name == "raw_post"
    assert args[0] == "/rest/api/uom/LogicalPartition"
    assert args[1] == "<lpar/>"


def test_raw_post_file_missing_errors(fake_hmc, tmp_path):
    """A @file body that cannot be read exits via the CLI Error path, no request sent."""
    result = RUNNER.invoke(
        cli.app,
        [
            "raw",
            "post",
            "/rest/api/uom/LogicalPartition",
            f"@{tmp_path / 'nope.xml'}",
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert "Error" in result.stderr
    assert "cannot read body file" in result.stderr
    assert fake_hmc.calls == []


def test_raw_post_file_body_sent(fake_hmc, tmp_path):
    """A @file body is read from disk and posted verbatim."""
    body_file = tmp_path / "body.xml"
    body_file.write_text("<lpar><name>x</name></lpar>", encoding="utf-8")

    result = RUNNER.invoke(
        cli.app,
        ["raw", "post", "/rest/api/uom/LogicalPartition", f"@{body_file}", "--yes"],
    )

    assert result.exit_code == 0
    name, args, _ = fake_hmc.calls[0]
    assert name == "raw_post"
    assert args[0] == "/rest/api/uom/LogicalPartition"
    assert args[1] == "<lpar><name>x</name></lpar>"


# --------------------------------------------------------------------------- #
# systems
# --------------------------------------------------------------------------- #


def test_systems_list_table(fake_hmc):
    result = RUNNER.invoke(cli.app, ["systems", "list"])

    assert result.exit_code == 0
    assert "sys1" in result.stdout
    assert "9119-MHE*SN00001" in result.stdout
    assert fake_hmc.calls == [
        ("list_system_summaries", (), {}),
        ("get_managed_system", (SYSTEM_UUID,), {}),
    ]


def test_systems_list_json(fake_hmc):
    result = RUNNER.invoke(cli.app, ["systems", "list", "--json"])

    assert result.exit_code == 0
    assert SYSTEM_UUID in result.stdout
    assert fake_hmc.calls == [
        ("list_system_summaries", (), {}),
        ("get_managed_system", (SYSTEM_UUID,), {}),
    ]


def test_systems_show(fake_hmc):
    result = RUNNER.invoke(cli.app, ["systems", "show", SYSTEM_UUID])

    assert result.exit_code == 0
    assert "sys1" in result.stdout
    assert fake_hmc.calls == [
        ("list_system_summaries", (), {}),
        ("get_managed_system", (SYSTEM_UUID,), {}),
    ]


def test_systems_show_by_name(fake_hmc):
    result = RUNNER.invoke(cli.app, ["systems", "show", "sys1"])

    assert result.exit_code == 0
    assert "sys1" in result.stdout
    assert fake_hmc.calls == [
        ("list_system_summaries", (), {}),
        ("get_managed_system", (SYSTEM_UUID,), {}),
    ]


def test_systems_show_not_found_exits_1(fake_hmc):
    result = RUNNER.invoke(cli.app, ["systems", "show", "ghost"])

    assert result.exit_code == 1
    assert "not found" in result.stderr
    assert fake_hmc.calls == [("list_system_summaries", (), {})]


def test_systems_power_on(fake_hmc):
    result = RUNNER.invoke(cli.app, ["systems", "power-on", SYSTEM_UUID, "--yes"])

    assert result.exit_code == 0
    assert "Submitted PowerOn" in result.stdout
    assert fake_hmc.calls == [("power_on_system", (SYSTEM_UUID,), {})]


def test_systems_power_on_declined_confirm_aborts(fake_hmc):
    result = RUNNER.invoke(cli.app, ["systems", "power-on", SYSTEM_UUID], input="n\n")

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


def test_systems_power_off(fake_hmc):
    result = RUNNER.invoke(cli.app, ["systems", "power-off", SYSTEM_UUID, "--yes"])

    assert result.exit_code == 0
    assert "Submitted PowerOff" in result.stdout
    assert fake_hmc.calls == [("power_off_system", (SYSTEM_UUID, False), {})]


def test_systems_power_off_immediate(fake_hmc):
    result = RUNNER.invoke(
        cli.app, ["systems", "power-off", SYSTEM_UUID, "--immediate", "--yes"]
    )

    assert result.exit_code == 0
    assert "Immediate PowerOff" in result.stdout
    assert fake_hmc.calls == [("power_off_system", (SYSTEM_UUID, True), {})]


def test_systems_power_off_declined_confirm_aborts(fake_hmc):
    result = RUNNER.invoke(cli.app, ["systems", "power-off", SYSTEM_UUID], input="n\n")

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


# --------------------------------------------------------------------------- #
# vios
# --------------------------------------------------------------------------- #


def test_vios_list_table(fake_hmc):
    result = RUNNER.invoke(cli.app, ["vios", "list"])

    assert result.exit_code == 0
    assert "vios1" in result.stdout
    assert "3.1.0" in result.stdout
    assert fake_hmc.calls == [
        ("list_system_summaries", (), {}),
        ("list_vios", (SYSTEM_UUID,), {}),
    ]


def test_vios_list_json(fake_hmc):
    result = RUNNER.invoke(cli.app, ["vios", "list", "--json"])

    assert result.exit_code == 0
    assert [entry["UUID"] for entry in json.loads(result.stdout)["entries"]] == [
        VIOS_UUID
    ]
    assert fake_hmc.calls == [
        ("list_system_summaries", (), {}),
        ("list_vios", (SYSTEM_UUID,), {}),
    ]


def test_vios_list_restricted_to_system(fake_hmc):
    result = RUNNER.invoke(cli.app, ["vios", "list", "--system", SYSTEM_UUID])

    assert result.exit_code == 0
    assert fake_hmc.calls == [("list_vios", (SYSTEM_UUID,), {})]


def test_vios_power_on(fake_hmc):
    result = RUNNER.invoke(cli.app, ["vios", "power-on", VIOS_UUID, "--yes"])

    assert result.exit_code == 0
    assert "Submitted PowerOn" in result.stdout
    assert fake_hmc.calls == [("power_on_vios", (VIOS_UUID,), {})]


@pytest.mark.parametrize("command", ["power-on", "power-off"])
@pytest.mark.parametrize(
    ("scope_args", "expected_system"),
    [
        (["--system", SYSTEM_UUID], SYSTEM_UUID),
        (["-s", SYSTEM_UUID], SYSTEM_UUID),
        ([], None),
    ],
)
def test_vios_power_commands_pass_system_scope(
    fake_hmc, monkeypatch, command, scope_args, expected_system
):
    seen = []

    async def fake_power_vios(_hmc, vios, *, system_name_or_uuid, **_kwargs):
        seen.append((system_name_or_uuid, vios))
        return {}

    monkeypatch.setattr(cli_vios_core, "power_vios", fake_power_vios)

    result = RUNNER.invoke(cli.app, ["vios", command, VIOS_UUID, "--yes", *scope_args])

    assert result.exit_code == 0, result.output
    assert seen == [(expected_system, VIOS_UUID)]


def test_vios_power_on_declined_confirm_aborts(fake_hmc):
    result = RUNNER.invoke(cli.app, ["vios", "power-on", VIOS_UUID], input="n\n")

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


def test_vios_power_off(fake_hmc):
    result = RUNNER.invoke(cli.app, ["vios", "power-off", VIOS_UUID, "--yes"])

    assert result.exit_code == 0
    assert "Submitted PowerOff" in result.stdout
    assert fake_hmc.calls == [("power_off_vios", (VIOS_UUID, False), {})]


def test_vios_power_off_immediate(fake_hmc):
    result = RUNNER.invoke(
        cli.app, ["vios", "power-off", VIOS_UUID, "--immediate", "--yes"]
    )

    assert result.exit_code == 0
    assert "Immediate PowerOff" in result.stdout
    assert fake_hmc.calls == [("power_off_vios", (VIOS_UUID, True), {})]


def test_vios_power_off_declined_confirm_aborts(fake_hmc):
    result = RUNNER.invoke(cli.app, ["vios", "power-off", VIOS_UUID], input="n\n")

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


def _patch_vios_label_cli(
    monkeypatch, operation_name: str, result: object
) -> AsyncMock:
    operation = AsyncMock(return_value=result)
    monkeypatch.setattr(cli_vios_labels, operation_name, operation)
    monkeypatch.setattr(
        cli_vios_labels, "ssh_config", lambda: HMCConfig.from_mapping({})
    )
    return operation


def test_vios_label_commands_are_registered():
    result = RUNNER.invoke(cli.app, ["vios", "--help"])
    assert result.exit_code == 0, result.output
    for name in (
        "list-fc-port-labels",
        "set-fc-port-label",
        "remove-fc-port-label",
        "list-vfc-group-labels",
        "create-vfc-group-label",
        "update-vfc-group-label",
        "remove-vfc-group-label",
    ):
        assert name in result.stdout


def test_vios_list_fc_port_labels_json(monkeypatch):
    operation = _patch_vios_label_cli(
        monkeypatch, "list_vios_fc_port_labels", [{"port_name": "fcs0"}]
    )
    result = RUNNER.invoke(
        cli.app,
        ["vios", "list-fc-port-labels", "system-a", "--vios-id", "2", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"port_name": "fcs0"}]
    operation.assert_awaited_once()


@pytest.mark.parametrize(
    ("arguments", "operation_name", "prompt_parts"),
    [
        (
            [
                "set-fc-port-label",
                "system-a",
                "fcs0",
                "label-a",
                "--vios-name",
                "vios-a",
            ],
            "set_vios_fc_port_label",
            ["system-a", "fcs0", "vios-a", "label-a"],
        ),
        (
            ["remove-fc-port-label", "system-a", "fcs0", "--vios-id", "2"],
            "remove_vios_fc_port_label",
            ["system-a", "fcs0", "vios_id=2"],
        ),
        (
            [
                "create-vfc-group-label",
                "system-a",
                "group-a",
                "--vios-name",
                "a",
                "--vios-name",
                "b",
            ],
            "create_vios_vfc_group_label",
            ["system-a", "group-a", "vios_names=['a', 'b']"],
        ),
        (
            [
                "update-vfc-group-label",
                "system-a",
                "group-a",
                "--action",
                "rename",
                "--new-name",
                "group-b",
            ],
            "update_vios_vfc_group_label",
            ["system-a", "group-a", "group-b"],
        ),
        (
            ["remove-vfc-group-label", "system-a", "group-a"],
            "remove_vios_vfc_group_label",
            ["system-a", "group-a"],
        ),
    ],
)
def test_vios_label_mutations_confirm_on_stderr_and_emit_json(
    monkeypatch, arguments: list[str], operation_name: str, prompt_parts: list[str]
):
    operation = _patch_vios_label_cli(
        monkeypatch, operation_name, {"operation": "done"}
    )
    result = RUNNER.invoke(cli.app, ["vios", *arguments], input="y\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"operation": "done"}
    assert all(part in result.stderr for part in prompt_parts)
    operation.assert_awaited_once()


def test_vios_label_declined_confirmation_does_not_dispatch(monkeypatch):
    operation = _patch_vios_label_cli(monkeypatch, "remove_vios_fc_port_label", {})
    result = RUNNER.invoke(
        cli.app,
        ["vios", "remove-fc-port-label", "system-a", "fcs0", "--vios-name", "a"],
        input="n\n",
    )
    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    operation.assert_not_awaited()


def test_vios_label_confirmation_escapes_control_characters(monkeypatch):
    operation = _patch_vios_label_cli(monkeypatch, "set_vios_fc_port_label", {})
    result = RUNNER.invoke(
        cli.app,
        [
            "vios",
            "set-fc-port-label",
            "system-a",
            "fcs0",
            "safe\nspoofed-target",
            "--vios-name",
            "vios-a",
        ],
        input="n\n",
    )
    assert result.exit_code == 1
    assert "safe\\nspoofed-target" in result.stderr
    assert "safe\nspoofed-target" not in result.stderr
    operation.assert_not_awaited()


def test_vios_group_members_and_yes_are_preserved(monkeypatch):
    operation = _patch_vios_label_cli(
        monkeypatch, "update_vios_vfc_group_label", {"operation": "add"}
    )
    result = RUNNER.invoke(
        cli.app,
        [
            "vios",
            "update-vfc-group-label",
            "system-a",
            "group-a",
            "--action",
            "add-members",
            "--vios-id",
            "2",
            "--vios-id",
            "3",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"operation": "add"}
    assert operation.await_args.args[3] == "add-members"
    assert operation.await_args.kwargs["vios_ids"] == [2, 3]


# --------------------------------------------------------------------------- #
# console
# --------------------------------------------------------------------------- #


def test_console_info(fake_hmc):
    result = RUNNER.invoke(cli.app, ["console", "info"])

    assert result.exit_code == 0
    assert "1060" in result.stdout
    assert "NetworkInterfaces" in result.stdout and "eth0" in result.stdout
    assert fake_hmc.calls == [("get_console_info", (), {})]


def test_console_info_json(fake_hmc):
    result = RUNNER.invoke(cli.app, ["console", "info", "--json"])

    assert result.exit_code == 0
    assert "1060" in result.stdout
    assert fake_hmc.calls == [("get_console_info", (), {})]


def test_console_info_none_reports_empty(fake_hmc):
    fake_hmc.console = None
    result = RUNNER.invoke(cli.app, ["console", "info"])

    assert result.exit_code == 0
    assert "No ManagementConsole data returned" in result.stderr
    assert fake_hmc.calls == [("get_console_info", (), {})]


# --------------------------------------------------------------------------- #
# cluster / SSP
# --------------------------------------------------------------------------- #


def _cluster_app() -> typer.Typer:
    app = typer.Typer()
    app.callback()(cli_command_app.main)
    cli_storage_cluster.register_commands(app)
    return app


def test_cluster_list_table(fake_hmc):
    result = RUNNER.invoke(_cluster_app(), ["list"])

    assert result.exit_code == 0
    assert "cl1" in result.stdout
    assert fake_hmc.calls == [("list_clusters", (), {})]


def test_cluster_list_json(fake_hmc):
    result = RUNNER.invoke(_cluster_app(), ["list", "--json"])

    assert result.exit_code == 0
    assert CLUSTER_UUID in result.stdout
    assert fake_hmc.calls == [("list_clusters", (), {})]


def test_cluster_list_ssps(fake_hmc):
    result = RUNNER.invoke(_cluster_app(), ["list-ssps"])

    assert result.exit_code == 0
    assert "pool1" in result.stdout
    assert "1024" in result.stdout
    assert fake_hmc.calls == [("list_shared_storage_pools", (), {})]


def test_cluster_create_lu(fake_hmc):
    result = RUNNER.invoke(
        _cluster_app(),
        [
            "create-lu",
            CLUSTER_UUID,
            "--name",
            "vol1",
            "--lu-size-gib",
            "50",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "Submitted CreateLogicalUnit job" in result.stdout
    name, args, _ = fake_hmc.calls[0]
    assert name == "create_logical_unit"
    assert args[0] == CLUSTER_UUID
    assert args[1] == "vol1"
    assert args[2] == 50
    assert args[3] == "THIN"  # default lu_type
    assert args[4] == "VirtualIO_Disk"  # default device_type
    assert args[5] is None  # no clone source


def test_cluster_create_lu_declined_confirm_aborts(fake_hmc):
    result = RUNNER.invoke(
        _cluster_app(),
        [
            "create-lu",
            CLUSTER_UUID,
            "--name",
            "vol1",
            "--lu-size-gib",
            "50",
        ],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


@pytest.mark.parametrize(
    "option,value",
    [("--type", "SPARSE"), ("--device-type", "PhysicalDisk")],
)
def test_cluster_create_lu_rejects_invalid_vocabulary(fake_hmc, option, value):
    result = RUNNER.invoke(
        _cluster_app(),
        [
            "create-lu",
            CLUSTER_UUID,
            "--name",
            "vol1",
            "--lu-size-gib",
            "50",
            option,
            value,
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert value in result.stderr
    assert fake_hmc.calls == []


def test_cluster_delete_lu(fake_hmc):
    result = RUNNER.invoke(
        _cluster_app(),
        ["delete-lu", CLUSTER_UUID, "--udid", "udid-1", "--yes"],
    )

    assert result.exit_code == 0
    assert "Submitted DeleteLogicalUnit job" in result.stdout
    assert fake_hmc.calls == [("delete_logical_unit", (CLUSTER_UUID, "udid-1"), {})]


def test_cluster_delete_lu_declined_confirm_aborts(fake_hmc):
    result = RUNNER.invoke(
        _cluster_app(),
        ["delete-lu", CLUSTER_UUID, "--udid", "udid-1"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #


def test_templates_list_table(fake_hmc):
    result = RUNNER.invoke(cli.app, ["templates", "list"])

    assert result.exit_code == 0
    assert "tpl1" in result.stdout
    assert fake_hmc.calls == [("list_partition_templates", (), {})]


def test_templates_list_json(fake_hmc):
    result = RUNNER.invoke(cli.app, ["templates", "list", "--json"])

    assert result.exit_code == 0
    assert TEMPLATE_UUID in result.stdout
    assert fake_hmc.calls == [("list_partition_templates", (), {})]


def test_templates_cli_translates_not_acceptable_error(fake_hmc):
    fake_hmc.fail_on = "list_partition_templates"
    fake_hmc.fail_status = 406

    result = RUNNER.invoke(cli.app, ["templates", "list"])

    assert result.exit_code == 1
    assert "refused the media type" in result.stderr


def test_templates_show(fake_hmc):
    result = RUNNER.invoke(cli.app, ["templates", "show", TEMPLATE_UUID])

    assert result.exit_code == 0
    assert "tpl1" in result.stdout
    assert fake_hmc.calls == [("get_partition_template", (TEMPLATE_UUID,), {})]


def test_templates_show_propagates_not_found_error(fake_hmc):
    result = RUNNER.invoke(cli.app, ["templates", "show", "ghost"])

    assert result.exit_code == 1
    assert "not found" in result.stderr
    assert fake_hmc.calls == [("get_partition_template", ("ghost",), {})]


def test_templates_deploy(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["templates", "deploy", TEMPLATE_UUID, "--system", SYSTEM_UUID, "--yes"],
    )

    assert result.exit_code == 0
    assert "Deploy job" in result.stdout
    assert "ownership stamp not attempted" in result.stdout
    assert fake_hmc.calls == [
        ("deploy_partition_template", (TEMPLATE_UUID, SYSTEM_UUID), {})
    ]


def test_templates_deploy_rejects_invalid_wait_timing_before_client_use(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "templates",
            "deploy",
            TEMPLATE_UUID,
            "--system",
            SYSTEM_UUID,
            "--wait",
            "--poll-interval",
            "0",
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert "poll_interval must be greater than 0" in result.stderr
    assert fake_hmc.calls == []


def test_templates_deploy_waits_through_shared_workflow(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "templates",
            "deploy",
            TEMPLATE_UUID,
            "--system",
            SYSTEM_UUID,
            "--wait",
            "--timeout",
            "60",
            "--poll-interval",
            "1",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert fake_hmc.calls == [
        ("list_logical_partitions", (SYSTEM_UUID,), {}),
        ("deploy_partition_template", (TEMPLATE_UUID, SYSTEM_UUID), {}),
        (
            "wait_for_job_entry",
            (JOB_UUID, 60, 1),
            {"job_href": f"/jobs/{JOB_UUID}"},
        ),
        ("list_logical_partitions", (SYSTEM_UUID,), {}),
    ]


def test_templates_deploy_declined_confirm_aborts(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["templates", "deploy", TEMPLATE_UUID, "--system", SYSTEM_UUID],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #


def test_jobs_show(fake_hmc):
    result = RUNNER.invoke(cli.app, ["jobs", "show", JOB_UUID])

    assert result.exit_code == 0
    assert "PowerOn" in result.stdout
    assert fake_hmc.calls == [("get_job_entry", (JOB_UUID,), {"job_href": None})]


def test_jobs_show_not_found_exits_1(fake_hmc):
    result = RUNNER.invoke(cli.app, ["jobs", "show", "ghost"])

    assert result.exit_code == 1
    assert "not found" in result.stderr
    assert fake_hmc.calls == [("get_job_entry", ("ghost",), {"job_href": None})]


def test_jobs_show_forwards_self_link(fake_hmc):
    href = f"/jobs/{JOB_UUID}"
    result = RUNNER.invoke(cli.app, ["jobs", "show", JOB_UUID, "--job-href", href])

    assert result.exit_code == 0
    assert fake_hmc.calls == [("get_job_entry", (JOB_UUID,), {"job_href": href})]


def test_jobs_wait(fake_hmc):
    fake_hmc.job["Resource"]["Status"] = "COMPLETED_OK"
    result = RUNNER.invoke(cli.app, ["jobs", "wait", JOB_UUID])

    assert result.exit_code == 0
    assert "COMPLETED_OK" in result.stdout
    assert fake_hmc.calls == [("get_job_entry", (JOB_UUID,), {"job_href": None})]


def test_jobs_wait_not_found_exits_1_after_one_poll(fake_hmc):
    result = RUNNER.invoke(cli.app, ["jobs", "wait", "ghost"])

    assert result.exit_code == 1
    assert "not found" in result.stderr
    assert fake_hmc.calls == [("get_job_entry", ("ghost",), {"job_href": None})]


@pytest.mark.parametrize(
    ("command", "operation", "expected"),
    [
        (
            ["lpars", "read-boot-order", "sys1", LPAR_UUID],
            "read_lpar_boot_order",
            {"system_name_or_uuid": "sys1", "lpar_name_or_uuid": LPAR_UUID},
        ),
        (
            [
                "lpars",
                "set-boot-order",
                "sys1",
                LPAR_UUID,
                "/vdevice/v-scsi@30000003/disk@8100000000000000",
                "/vdevice/l-lan@30000002:speed=auto,duplex=auto,192.0.2.10,,192.0.2.1",
                "--ownership-override",
            ],
            "set_lpar_boot_order",
            {
                "system_name_or_uuid": "sys1",
                "lpar_name_or_uuid": LPAR_UUID,
                "devices": [
                    "/vdevice/v-scsi@30000003/disk@8100000000000000",
                    "/vdevice/l-lan@30000002:speed=auto,duplex=auto,192.0.2.10,,192.0.2.1",
                ],
                "ownership_override": True,
            },
        ),
        (
            ["lpars", "clear-boot-order", "sys1", LPAR_UUID],
            "clear_lpar_boot_order",
            {
                "system_name_or_uuid": "sys1",
                "lpar_name_or_uuid": LPAR_UUID,
                "ownership_override": False,
            },
        ),
    ],
)
def test_boot_order_commands_delegate_to_operations(
    fake_hmc, monkeypatch, command, operation, expected
):
    seen = {}

    async def fake_operation(hmc, **kwargs):
        assert hmc is fake_hmc
        seen.update(kwargs)
        return {"PendingBootString": "/vdevice/v-scsi@30000003/disk@8100000000000000"}

    monkeypatch.setattr(
        f"hmcpctl.cli_commands.lpar.profiles.{operation}", fake_operation
    )

    result = RUNNER.invoke(cli.app, command)

    assert result.exit_code == 0
    assert seen == expected


BOOT_PATH = "/vdevice/v-scsi@30000003/disk@8100000000000000"


def _stub_set_boot_order(monkeypatch, document):
    async def fake_operation(hmc, **_kwargs):
        return document

    monkeypatch.setattr(
        "hmcpctl.cli_commands.lpar.profiles.set_lpar_boot_order", fake_operation
    )


def test_set_boot_order_prints_confirmation_and_read_back_only(fake_hmc, monkeypatch):
    document = {
        "Resource": {
            "PartitionName": "lpar-sentinel",
            "BootListInformation": {"PendingBootString": {"text": BOOT_PATH}},
        }
    }
    _stub_set_boot_order(monkeypatch, document)

    result = RUNNER.invoke(
        cli.app, ["lpars", "set-boot-order", "sys1", LPAR_UUID, BOOT_PATH]
    )

    assert result.exit_code == 0
    assert f"Boot order set to: {BOOT_PATH}" in result.stdout
    assert f"Pending boot string: {BOOT_PATH}" in result.stdout
    assert "lpar-sentinel" not in result.stdout


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (None, "-"),
        (
            {"Resource": {"BootListInformation": {"PendingBootString": BOOT_PATH}}},
            BOOT_PATH,
        ),
        ({"Resource": {"BootListInformation": {"PendingBootString": ""}}}, "-"),
        (
            {"Resource": {"BootListInformation": {"PendingBootString": {"@ksv": "1"}}}},
            "-",
        ),
    ],
)
def test_set_boot_order_read_back_shapes(fake_hmc, monkeypatch, document, expected):
    _stub_set_boot_order(monkeypatch, document)

    result = RUNNER.invoke(
        cli.app, ["lpars", "set-boot-order", "sys1", LPAR_UUID, BOOT_PATH]
    )

    assert result.exit_code == 0
    assert f"Pending boot string: {expected}" in result.stdout


def test_set_boot_order_json_prints_full_document(fake_hmc, monkeypatch):
    document = {"Resource": {"PartitionName": "lpar-sentinel"}}
    _stub_set_boot_order(monkeypatch, document)

    result = RUNNER.invoke(
        cli.app, ["lpars", "set-boot-order", "sys1", LPAR_UUID, BOOT_PATH, "--json"]
    )

    assert result.exit_code == 0
    assert "lpar-sentinel" in result.stdout
    assert "Pending boot string" not in result.stdout


@pytest.mark.parametrize("devices", [["tape"], ["network,cd"], ["/ok", "disk"]])
def test_set_boot_order_rejects_invalid_device_before_operation(monkeypatch, devices):
    called = False

    async def fake_operation(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        "hmcpctl.cli_commands.lpar.profiles.set_lpar_boot_order", fake_operation
    )

    result = RUNNER.invoke(
        cli.app, ["lpars", "set-boot-order", "sys1", LPAR_UUID, *devices]
    )

    assert result.exit_code == 2
    assert "Invalid boot device path" in result.output
    assert called is False


# --------------------------------------------------------------------------- #
# pcm metrics
# --------------------------------------------------------------------------- #


def test_metrics_prefs(fake_hmc):
    result = RUNNER.invoke(cli.app, ["metrics", "prefs", "ManagedSystem", SYSTEM_UUID])

    assert result.exit_code == 0
    assert "LongTermMonitorEnabled" in result.stdout
    assert fake_hmc.calls == [
        ("get_pcm_preferences", ("ManagedSystem", SYSTEM_UUID), {})
    ]


def test_metrics_cli_translates_authority_error(fake_hmc):
    fake_hmc.fail_on = "get_pcm_preferences"
    fake_hmc.fail_status = 403

    result = RUNNER.invoke(cli.app, ["metrics", "prefs", "ManagedSystem", SYSTEM_UUID])

    assert result.exit_code == 1
    assert "does not have PCM authority" in result.stderr


def test_metrics_set_prefs_requires_confirmation(fake_hmc):
    """metrics set-prefs is gated by the same --yes/confirm convention as other mutating commands."""
    result = RUNNER.invoke(
        cli.app,
        ["metrics", "set-prefs", "ManagedSystem", SYSTEM_UUID, "--ltm"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert fake_hmc.calls == []


def test_metrics_set_prefs_builds_flags(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "metrics",
            "set-prefs",
            "ManagedSystem",
            SYSTEM_UUID,
            "--ltm",
            "--no-aggregation",
            "--compute-ltm",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert "Updated" in result.stdout
    assert fake_hmc.calls == [
        (
            "set_pcm_preferences",
            ("ManagedSystem", SYSTEM_UUID),
            {
                "LongTermMonitorEnabled": True,
                "AggregationEnabled": False,
                "ComputeLTMEnabled": True,
            },
        )
    ]


def test_metrics_set_prefs_no_flags_exits_2(fake_hmc):
    result = RUNNER.invoke(
        cli.app, ["metrics", "set-prefs", "ManagedSystem", SYSTEM_UUID]
    )

    assert result.exit_code == 2
    assert "Error:" in result.stderr
    assert "No flags supplied" in result.stderr
    assert fake_hmc.calls == []


def test_network_set_sriov_mode_rejects_invalid_mode(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        ["network", "set-sriov-mode", "system-1", "adapter-1", "invalid"],
    )

    assert result.exit_code == 2
    assert "invalid" in result.stderr
    assert "sriov" in result.stderr
    assert "dedicated" in result.stderr
    assert fake_hmc.calls == []


def test_metrics_show_processed(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "metrics",
            "show",
            "ManagedSystem",
            SYSTEM_UUID,
            "--start",
            "2024-01-01T00:00:00Z",
        ],
    )

    assert result.exit_code == 0
    assert fake_hmc.calls == [
        (
            "get_processed_metric_links",
            ("ManagedSystem", SYSTEM_UUID, "2024-01-01T00:00:00Z", None, None),
            {},
        )
    ]


def test_metrics_show_aggregated(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "metrics",
            "show",
            "ManagedSystem",
            SYSTEM_UUID,
            "--start",
            "2024-01-01T00:00:00Z",
            "--aggregated",
        ],
    )

    assert result.exit_code == 0
    assert fake_hmc.calls == [
        (
            "get_aggregated_metric_links",
            ("ManagedSystem", SYSTEM_UUID, "2024-01-01T00:00:00Z", None, None),
            {},
        )
    ]


def test_metrics_show_logical_partition_forwards_owning_system(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "metrics",
            "show",
            "LogicalPartition",
            LPAR_UUID,
            "--system",
            SYSTEM_UUID,
            "--start",
            "2024-01-01T00:00:00Z",
        ],
    )

    assert result.exit_code == 0
    assert fake_hmc.calls == [
        ("list_logical_partitions", (SYSTEM_UUID,), {}),
        (
            "get_processed_metric_links",
            ("LogicalPartition", LPAR_UUID, "2024-01-01T00:00:00Z", None, None),
            {"system_uuid": SYSTEM_UUID},
        ),
    ]


def test_metrics_show_logical_partition_requires_owning_system(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "metrics",
            "show",
            "LogicalPartition",
            LPAR_UUID,
            "--start",
            "2024-01-01T00:00:00Z",
        ],
    )

    assert result.exit_code == 1
    assert "system_name_or_uuid" in str(result.exception)
    assert fake_hmc.calls == []


def test_metrics_show_rejects_system_selector_for_managed_system(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "metrics",
            "show",
            "ManagedSystem",
            SYSTEM_UUID,
            "--system",
            SYSTEM_UUID,
            "--start",
            "2024-01-01T00:00:00Z",
        ],
    )

    assert result.exit_code == 1
    assert "only for LogicalPartition" in str(result.exception)
    assert fake_hmc.calls == []


@pytest.mark.parametrize("command", ["prefs", "set-prefs"])
def test_metrics_preference_help_is_managed_system_only(command):
    result = RUNNER.invoke(cli.app, ["metrics", command, "--help"])

    assert result.exit_code == 0
    assert "ManagedSystem" in result.stdout
    assert "preferences are" in result.stdout
    assert "unavailable" in result.stdout


def test_metrics_show_fetch_downloads_latest(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "metrics",
            "show",
            "ManagedSystem",
            SYSTEM_UUID,
            "--start",
            "2024-01-01T00:00:00Z",
            "--fetch",
        ],
    )

    assert result.exit_code == 0
    assert '"data"' in result.stdout
    names = [c[0] for c in fake_hmc.calls]
    assert names == ["get_processed_metric_links", "fetch_json"]


def test_metrics_show_fetch_404_reports_empty(fake_hmc):
    """A 404 from fetch_json (metrics aged out of retention) is no data, not an error."""
    fake_hmc.fetch_json_404 = True
    result = RUNNER.invoke(
        cli.app,
        [
            "metrics",
            "show",
            "ManagedSystem",
            SYSTEM_UUID,
            "--start",
            "2024-01-01T00:00:00Z",
            "--fetch",
        ],
    )

    assert result.exit_code == 0
    assert "{}" in result.stdout
    assert "Error" not in result.stderr


# --------------------------------------------------------------------------- #
# memory pools (SSH-backed)
# --------------------------------------------------------------------------- #


def test_memory_pools_list_table(monkeypatch):
    async def fake_list(config, system_name):
        return [{"pool_mem": "4096", "max_pool_mem": "8192"}]

    monkeypatch.setattr(
        "hmcpctl.cli_commands.systems.memory_pools.list_memory_pools", fake_list
    )
    result = RUNNER.invoke(cli.app, ["memory-pools", "list", "sys1"])

    assert result.exit_code == 0
    assert "4096" in result.stdout
    assert "8192" in result.stdout


def test_memory_pools_list_json(monkeypatch):
    async def fake_list(config, system_name):
        return [{"pool_mem": "4096", "max_pool_mem": "8192"}]

    monkeypatch.setattr(
        "hmcpctl.cli_commands.systems.memory_pools.list_memory_pools", fake_list
    )
    result = RUNNER.invoke(cli.app, ["memory-pools", "list", "sys1", "--json"])

    assert result.exit_code == 0
    assert '"pool_mem": "4096"' in result.stdout


def test_memory_pools_list_empty(monkeypatch):
    async def fake_list(config, system_name):
        return []

    monkeypatch.setattr(
        "hmcpctl.cli_commands.systems.memory_pools.list_memory_pools", fake_list
    )
    result = RUNNER.invoke(cli.app, ["memory-pools", "list", "sys1"])

    assert result.exit_code == 0
    assert "No memory pools found" in result.stderr


def test_memory_pools_remove_with_yes(monkeypatch):
    async def fake_remove(config, system_name):
        return "pool removed\n"

    monkeypatch.setattr(
        "hmcpctl.cli_commands.systems.memory_pools.remove_memory_pool", fake_remove
    )
    result = RUNNER.invoke(cli.app, ["memory-pools", "remove", "sys1", "--yes"])

    assert result.exit_code == 0
    assert "removed" in result.stdout
    assert "pool removed" in result.stdout


def test_memory_pools_remove_preserves_bracketed_result(monkeypatch):
    """The trailing HMC result line round-trips a bracketed stamp intact -- #965."""

    async def fake_remove(config, system_name):
        return OWNERSHIP_STAMP + "\n"

    monkeypatch.setattr(
        "hmcpctl.cli_commands.systems.memory_pools.remove_memory_pool", fake_remove
    )
    result = RUNNER.invoke(cli.app, ["memory-pools", "remove", "sys1", "--yes"])

    assert result.exit_code == 0
    assert OWNERSHIP_STAMP in result.stdout


def test_memory_pools_remove_declined_confirm_aborts(monkeypatch):
    result = RUNNER.invoke(cli.app, ["memory-pools", "remove", "sys1"], input="n\n")

    assert result.exit_code == 1
    assert "Aborted" in result.stderr


def test_lpm_decline_does_not_enter_client_lifecycle(monkeypatch):
    called = False

    def forbidden_client(_operation):
        nonlocal called
        called = True
        raise AssertionError("declined migration must not create a client")

    monkeypatch.setattr(cli_lpar_migration, "with_client", forbidden_client)
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "migrate", LPAR_NAME, "--target", "sys1"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted" in result.stderr
    assert called is False


def test_storage_mount_optical_media_help_names_the_rest0269_precondition():
    """#1237: the refusal's precondition is stated before the write."""
    result = RUNNER.invoke(cli.app, ["storage", "mount-optical-media", "--help"])

    assert result.exit_code == 0
    assert "REST0269" in result.stdout
    assert "activate or apply a profile" in collapse_whitespace(result.stdout)


@pytest.mark.parametrize("command", ["create", "provision"])
def test_lpars_create_and_provision_refuse_a_vios_type_before_client_call(
    fake_hmc, command
):
    """V10R3 answers a LogicalPartition PUT typed Virtual IO Server with 500 REST0140."""
    args = ["lpars", command]
    args += (
        ["acmesys9-lp3", "--system", SYSTEM_UUID]
        if command == "create"
        else [
            "--system",
            SYSTEM_UUID,
            "--name",
            "acmesys9-lp3",
            "--vlan",
            "100",
            "--vios-uuid",
            VIOS_UUID,
            "--storage-name",
            "rootvg",
        ]
    )
    result = RUNNER.invoke(cli.app, [*args, "--type", "Virtual IO Server", "--yes"])

    assert result.exit_code == 2
    assert "hmc_create_vios" in result.stderr
    assert fake_hmc.calls == []


def test_lpars_provision_refuses_a_dedicated_slot_before_client_call(
    fake_hmc, monkeypatch, tmp_path
):
    provision = AsyncMock()
    monkeypatch.setattr(cli_lpar_provision, "provision_lpar", provision)
    assignments = tmp_path / "assignments.json"
    assignments.write_text(
        json.dumps(
            {"dedicated": [{"profile_name": "default_profile", "drc_index": "1"}]}
        ),
        encoding="utf-8",
    )

    result = RUNNER.invoke(
        cli.app,
        [
            "lpars",
            "provision",
            "--system",
            SYSTEM_UUID,
            "--name",
            "newlpar",
            "--vlan",
            "100",
            "--vios-uuid",
            VIOS_UUID,
            "--storage-name",
            "rootvg",
            "--pcie-assignments",
            str(assignments),
            "--yes",
        ],
    )

    assert result.exit_code == 2
    assert "hmc_assign_dedicated_pcie_slot" in unstyle(result.stderr)
    provision.assert_not_awaited()
    assert fake_hmc.calls == []


@pytest.mark.parametrize("as_json", [False, True])
def test_lpars_refcode_definitions_and_unknowns(monkeypatch, as_json):
    async def fake(_cfg, _cmd):
        return (
            "lpar_name,time_stamp,refcode\n"
            "web01,now,CA00E105 LP=00002\nweb01,earlier,B2001230 LP=00002\n"
        )

    _patch_ssh_command(monkeypatch, fake)
    args = ["lpars", "refcodes", "sys1", "web01", "--count", "2"]
    result = RUNNER.invoke(
        cli.app, args + (["--json"] if as_json else ["--format", "table"])
    )
    assert result.exit_code == 0, result.output
    if as_json:
        rows = json.loads(result.stdout)
        assert "operating system" in rows[0]["definition"].lower()
        assert rows[0]["refcode"] == "CA00E105 LP=00002"
        assert rows[1]["definition"] is None
        assert rows[1]["definition_source"] is None
    else:
        text = collapse_whitespace(result.stdout)
        assert "operating system" in text.lower()
        assert "Unknown definition" in text
        assert "B2001230" in text


@pytest.mark.parametrize("as_json", [False, True])
def test_refcode_table_escapes_terminal_controls_but_json_preserves_them(
    monkeypatch, as_json
):
    raw = "B2001230\x1b]52;c;YWJj\x1b\\\u202e"

    async def fake(_cfg, _cmd):
        return f"lpar_name,time_stamp,refcode\nweb01,now,{raw}\n"

    _patch_ssh_command(monkeypatch, fake)
    args = ["lpars", "refcodes", "sys1", "web01"]
    result = RUNNER.invoke(
        cli.app, args + (["--json"] if as_json else ["--format", "table"])
    )
    assert result.exit_code == 0, result.output
    if as_json:
        assert json.loads(result.stdout)[0]["refcode"] == raw
    else:
        assert "\x1b" not in result.stdout
        assert "\u202e" not in result.stdout
        assert "\\x1b" in result.stdout


@pytest.mark.parametrize("command", ["show", "state"])
@pytest.mark.parametrize("flag", ["--system", "-s"])
@pytest.mark.parametrize("target", [LPAR_UUID, LPAR_NAME])
def test_lpar_inspection_carries_system_scope(fake_hmc, command, flag, target):
    result = RUNNER.invoke(cli.app, ["lpars", command, target, flag, SYSTEM_UUID])
    assert result.exit_code == 0, result.output
    if target == LPAR_UUID:
        assert fake_hmc.calls[0] == ("list_logical_partitions", (SYSTEM_UUID,), {})
    else:
        assert fake_hmc.calls[0] == (
            "find_partition_by_name",
            (LPAR_NAME,),
            {"system_uuid": SYSTEM_UUID},
        )


@pytest.mark.parametrize("command", ["show", "state"])
def test_lpar_inspection_rejects_wrong_system_uuid(fake_hmc, monkeypatch, command):
    monkeypatch.setattr(fake_hmc, "list_logical_partitions", AsyncMock(return_value=[]))
    result = RUNNER.invoke(cli.app, ["lpars", command, LPAR_UUID, "-s", SYSTEM_UUID])
    assert result.exit_code == 1, result.output
    assert "does not belong to managed system" in result.output
    assert fake_hmc.calls == []


@pytest.mark.parametrize("command", ["delete", "decommission", "capture-console"])
def test_required_lpar_scope_rejects_before_hmc_calls(fake_hmc, command, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", True)
    result = RUNNER.invoke(cli.app, ["lpars", command, LPAR_UUID])
    assert result.exit_code == 2, result.output
    assert "--system" in unstyle(result.output)
    assert fake_hmc.calls == []


@pytest.mark.parametrize("command", ["show", "state"])
@pytest.mark.parametrize(
    "system", [SYSTEM_UUID, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]
)
def test_lpar_inspection_disambiguates_duplicate_names(
    fake_hmc, monkeypatch, command, system
):
    from types import MethodType

    from hmcpctl.client.core import HMCClient

    other_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    expected = LPAR_UUID if system == SYSTEM_UUID else other_uuid

    async def partitions(selected):
        return [
            {
                **fake_hmc.lpar,
                "UUID": LPAR_UUID if selected == SYSTEM_UUID else other_uuid,
            }
        ]

    monkeypatch.setattr(fake_hmc, "list_logical_partitions", partitions)
    monkeypatch.setattr(
        fake_hmc,
        "find_partition_by_name",
        MethodType(HMCClient.find_partition_by_name, fake_hmc),
    )
    args = ["lpars", command, LPAR_NAME, "-s", system]
    if command == "show":
        args.append("--json")
    result = RUNNER.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    if command == "show":
        assert json.loads(result.stdout)["UUID"] == expected
    else:
        assert fake_hmc.calls == [
            ("get_quick_property", ("LogicalPartition", expected, "PartitionState"), {})
        ]


@pytest.mark.parametrize("command", ["show", "state"])
def test_scoped_lpar_inspection_missing_name(fake_hmc, command):
    result = RUNNER.invoke(cli.app, ["lpars", command, "ghost", "-s", SYSTEM_UUID])
    assert result.exit_code == 1, result.output
    assert fake_hmc.calls == [
        ("find_partition_by_name", ("ghost",), {"system_uuid": SYSTEM_UUID})
    ]


def test_storage_wrong_system_uuid_refuses_before_deletion(fake_hmc, monkeypatch):
    monkeypatch.setattr(fake_hmc, "list_vios", AsyncMock(return_value=[]))
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "delete-disk",
            VIOS_UUID,
            "--vg",
            VG_UUID,
            "--name",
            "disk1",
            "--system",
            SYSTEM_UUID,
            "--yes",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "does not belong to managed system" in result.output
    assert fake_hmc.calls == []


@pytest.mark.parametrize("command", ["show", "state"])
def test_scoped_lpar_inspection_missing_system(fake_hmc, command):
    result = RUNNER.invoke(
        cli.app, ["lpars", command, LPAR_UUID, "-s", "absent-system"]
    )
    assert result.exit_code == 1, result.output
    assert "No managed system named" in result.output


@pytest.mark.parametrize("vg", ["rootvg", VG_UUID])
@pytest.mark.parametrize("vios", ["vios01", VIOS_UUID])
def test_storage_named_attach_selectors(fake_hmc, vg, vios):
    fake_hmc.vg["Resource"]["GroupName"] = "rootvg"
    fake_hmc.find_vios_by_name = AsyncMock(return_value={"UUID": VIOS_UUID})
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "attach-disk",
            LPAR_UUID,
            "--vios",
            vios,
            "--vg",
            vg,
            "--name",
            "disk01",
            "--capacity-mib",
            "1024",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output

    if vios == "vios01":
        fake_hmc.find_vios_by_name.assert_awaited_once_with("vios01")
    else:
        fake_hmc.find_vios_by_name.assert_not_awaited()
    assert not any(name == "create_virtual_disk" for name, _, _ in fake_hmc.calls)


def test_storage_map_uses_name_option(fake_hmc):
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "map",
            VIOS_UUID,
            "--lpar",
            LPAR_UUID,
            "--name",
            "disk01",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["get-media-repo", VIOS_UUID, VG_UUID],
        ["delete-media", VIOS_UUID, VG_UUID, "install.iso", "--yes"],
        ["mount-optical-media", VIOS_UUID, LPAR_UUID, "install.iso", "--yes"],
        ["map", VIOS_UUID, "--lpar", LPAR_UUID, "--disk", "disk01", "--yes"],
    ],
)
def test_storage_retired_positional_syntax_makes_no_requests(fake_hmc, arguments):
    result = RUNNER.invoke(cli.app, ["storage", *arguments])
    assert result.exit_code == 2
    assert fake_hmc.calls == []
