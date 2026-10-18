"""Offline independent-baseline contracts; remote boundaries never reach hardware."""

import ast
import copy
import importlib
import json
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from live_test import baseline

from hmcpctl.config import HMCConfig


@pytest.fixture
def document():
    tables = {name: [] for name in baseline.TABLES}
    tables["system"] = [
        {"name": "sys-R1", "type_model": "9009-42A", "serial_num": "serial-R1"}
    ]
    tables["partitions"] = [
        {"lpar_id": "2", "name": "vios-R1", "lpar_env": "vioserver", "state": "Running"}
    ]
    tables["profiles"] = [
        {
            "lpar_name": "vios-R1",
            "name": "default",
            "io_slots": "21010020/none/0,21010021/none/1",
        }
    ]
    return {
        "version": 1,
        "scope": {"host": "hmc.test", "system": "sys-R1", "vios_ids": []},
        "tables": tables,
    }


def test_compare(document, tmp_path, capsys):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(document))
    document["tables"]["profiles"][0]["io_slots"] = "21010021/none/1,21010020/none/0"
    after.write_text(json.dumps(document))
    assert baseline.main(["compare", str(before), str(after)]) == 0
    document["tables"]["partitions"][0]["state"] = "Not Activated"
    after.write_text(json.dumps(document))
    assert baseline.main(["compare", str(before), str(after)]) == 1
    output = capsys.readouterr().out
    assert "partitions" in output
    assert "vios-R1" not in output


@pytest.mark.parametrize(
    "fault", ["table", "field", "duplicate", "type", "vios", "version"]
)
def test_invalid_snapshot(document, fault):
    if fault == "table":
        del document["tables"]["slots"]
    elif fault == "field":
        del document["tables"]["partitions"][0]["state"]
    elif fault == "duplicate":
        document["tables"]["partitions"] *= 2
    elif fault == "type":
        document["tables"]["partitions"][0]["lpar_id"] = 2
    elif fault == "vios":
        document["scope"]["vios_ids"] = [2]
    else:
        document["version"] = True
    with pytest.raises(ValueError):
        baseline.validate(document)


def test_output(document, tmp_path):
    path = tmp_path / "snapshot.json"
    baseline.write_snapshot(path, document)
    assert path.stat().st_mode & 0o777 == 0o600
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        baseline.write_snapshot(path, document)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        baseline.write_snapshot(link, document)
    assert path.read_bytes() == original


def test_cli(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("dry run loaded credentials")

    monkeypatch.setattr(baseline, "build_config", forbidden)
    path = tmp_path / "snapshot.json"
    assert (
        baseline.main(
            [
                "capture",
                "--system",
                "sys-R1",
                "--output",
                str(path),
                "--vios-id",
                "2",
                "--dry-run",
            ]
        )
        == 0
    )
    assert not path.exists()
    assert "VIOS" in capsys.readouterr().out
    path.write_text('{"version":1,"version":1}')
    assert baseline.main(["compare", str(path), str(path)]) == 2


@pytest.mark.asyncio
async def test_capture_scope(document, monkeypatch):
    calls = []

    async def command(config, cmd):
        calls.append(cmd)
        if "-r sys " in cmd:
            return "name,type_model,serial_num\nsys-R1,9009-42A,serial-R1\n"
        assert "-r lpar " in cmd
        return "lpar_id,name,lpar_env,state\n2,vios-R1,vioserver,Running\n"

    monkeypatch.setattr(baseline, "run_hmc_command", command)
    monkeypatch.setattr(
        baseline, "list_dedicated_pcie_slot_rows", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        baseline,
        "read_profile_io_slot_rows",
        AsyncMock(return_value=document["tables"]["profiles"]),
    )
    monkeypatch.setattr(baseline, "list_sriov_adapter_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        baseline,
        "capture_vios",
        AsyncMock(side_effect=AssertionError("VIOS not authorized")),
    )
    captured = await baseline.capture(
        HMCConfig.from_mapping({"host": "hmc.test"}), "sys-R1", []
    )
    assert baseline.validate(captured) == baseline.validate(document)
    assert all(cmd.startswith("lssyscfg ") for cmd in calls)
    with pytest.raises(ValueError):
        await baseline.capture(
            HMCConfig.from_mapping({"host": "hmc.test"}), "sys-R1", [3]
        )


def test_vios_scope():
    assert (
        baseline.volume_rows(
            "datavg:\nLV NAME TYPE LPs PPs PVs LV STATE MOUNT POINT\nx jfs2 4 4 1 open/syncd N/A\n",
            "datavg",
        )[0]["name"]
        == "x"
    )
    for raw in (
        "",
        "ERROR",
        "datavg:\nLV NAME TYPE LPs PPs PVs LV STATE MOUNT POINT\nx\n",
    ):
        with pytest.raises(ValueError):
            baseline.volume_rows(raw, "datavg")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "<feed/>", "<entry/>", "<garbage/>"])
async def test_rest_incomplete(raw):
    hmc = AsyncMock()
    hmc._get.return_value = raw
    with pytest.raises(ValueError):
        await baseline.read_resources(hmc, "/fixture", "VirtualIOServer")


@pytest.mark.asyncio
async def test_explicit_empty_feed():
    hmc = AsyncMock()
    hmc._get.return_value = '<feed xmlns="http://www.w3.org/2005/Atom"/>'
    assert await baseline.read_resources(hmc, "/fixture", "VolumeGroup") == []


def test_mapping_capture():
    from hmcpctl.xmlutil import parse_feed

    fixture = (
        Path(__file__).parents[1]
        / "fixtures/live/rest-vios-scsi-mapping-unmapped-adapter.json"
    )
    resource = parse_feed(json.loads(fixture.read_text())["body"])[0]["Resource"]
    rows = baseline.mapping_rows(resource, "100")
    assert len(rows) == 3
    assert any(row["target"] == "" for row in rows)
    for invalid in (
        {},
        {"VirtualSCSIMappings": ""},
        {"VirtualSCSIMappings": {"error": "unavailable"}},
        {"VirtualSCSIMappings": {"VirtualSCSIMapping": [{}]}},
    ):
        with pytest.raises(ValueError):
            baseline.mapping_rows(invalid, "100")
    assert baseline.mapping_rows({"VirtualSCSIMappings": {"Metadata": {}}}, "100") == []


def test_table_order(document):
    other = copy.deepcopy(document)
    extra = dict(other["tables"]["partitions"][0], lpar_id="3", name="lpar-R1")
    document["tables"]["partitions"].append(extra)
    other["tables"]["partitions"].insert(0, extra)
    assert baseline.validate(document) == baseline.validate(other)


@pytest.mark.parametrize("field", ["state", "name", "lpar_env", "lpar_id"])
def test_empty_partition_fields(document, field):
    document["tables"]["partitions"][0][field] = ""
    with pytest.raises(ValueError):
        baseline.validate(document)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "system,ids", [("\n", []), ("sys-R1", [0]), ("sys-R1", [2, 2])]
)
async def test_selectors_fail_before_transport(system, ids, monkeypatch):
    remote = AsyncMock(side_effect=AssertionError("invalid selector reached transport"))
    monkeypatch.setattr(baseline, "run_hmc_command", remote)
    with pytest.raises(ValueError):
        await baseline.capture(
            HMCConfig.from_mapping({"host": "hmc.test"}), system, ids
        )
    remote.assert_not_called()


def test_failed_capture_leaves_no_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(baseline, "build_config", lambda **kwargs: object())
    monkeypatch.setattr(
        baseline, "capture", AsyncMock(side_effect=RuntimeError("private.test"))
    )
    output = tmp_path / "new.json"
    assert (
        baseline.main(["capture", "--system", "sys-R1", "--output", str(output)]) == 2
    )
    assert not output.exists()
    assert "private.test" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_selected_vios_reads_only(monkeypatch, document):
    from hmcpctl.xmlutil import ATOM_NS

    fixture = (
        Path(__file__).parents[1]
        / "fixtures/live/rest-vios-scsi-mapping-unmapped-adapter.json"
    )
    mapping_body = json.loads(fixture.read_text())["body"]
    vios_uuid = "11111111-1111-1111-1111-111111111111"
    group_uuid = "22222222-2222-2222-2222-222222222222"
    group_body = (
        f'<feed xmlns="{ATOM_NS}"><entry><id>{group_uuid}</id><content>'
        '<VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">'
        "<GroupName>datavg</GroupName></VolumeGroup></content></entry></feed>"
    )
    hmc = AsyncMock()
    hmc.__aenter__.return_value = hmc
    hmc._get.side_effect = [mapping_body, group_body]
    monkeypatch.setattr(baseline, "HMCClient", lambda config: hmc)
    resolver = AsyncMock(return_value=vios_uuid)
    monkeypatch.setattr(baseline, "resolve_vios_uuid", resolver)
    command = AsyncMock(
        return_value="datavg:\nLV NAME TYPE LPs PPs PVs LV STATE MOUNT POINT\nx jfs2 4 4 1 open/syncd N/A\n"
    )
    monkeypatch.setattr(baseline, "run_hmc_command", command)
    config = HMCConfig.from_mapping({"host": "hmc.test"})
    await baseline.capture_vios(
        config, "sys-R1", document["tables"]["partitions"], document["tables"]
    )
    document["scope"]["vios_ids"] = [2]
    result = baseline.validate(document)
    assert len(result["tables"]["vios_mappings"]) == 3
    assert result["tables"]["vios_volumes"][0]["group"] == group_uuid
    resolver.assert_awaited_once_with(hmc, "vios-R1", system_name_or_uuid="sys-R1")
    command.assert_awaited_once_with(
        config, "viosvrcmd -m sys-R1 --id 2 -c 'lsvg -lv datavg'"
    )
    assert [call.args[0] for call in hmc._get.await_args_list] == [
        f"/rest/api/uom/VirtualIOServer/{vios_uuid}?group=ViosSCSIMapping",
        f"/rest/api/uom/VirtualIOServer/{vios_uuid}/VolumeGroup",
    ]


def test_missing_group_rejected(document):
    document["tables"]["vios_volumes"] = [
        {
            "vios_id": "2",
            "group": "missing",
            "name": "x",
            "type": "jfs2",
            "lps": "4",
            "pps": "4",
            "pvs": "1",
            "state": "open/syncd",
            "mount": "N/A",
        }
    ]
    with pytest.raises(ValueError, match="no captured group"):
        baseline.validate(document)


@pytest.mark.parametrize("storage", ["<Storage><VirtualDisk/></Storage>", ""])
def test_empty_xml_backing_rejected(document, tmp_path, storage):
    from hmcpctl.xmlutil import parse_feed

    raw = f"""<entry xmlns="http://www.w3.org/2005/Atom"><content>
    <VirtualIOServer xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
    <VirtualSCSIMappings><VirtualSCSIMapping><ServerAdapter>
    <AdapterName>adapter-R1</AdapterName><VirtualSlotNumber>10</VirtualSlotNumber>
    <RemoteLogicalPartitionID>3</RemoteLogicalPartitionID><RemoteSlotNumber>11</RemoteSlotNumber>
    </ServerAdapter>{storage}<TargetDevice><LogicalVolumeVirtualTargetDevice>
    <TargetName>target-R1</TargetName></LogicalVolumeVirtualTargetDevice></TargetDevice>
    </VirtualSCSIMapping></VirtualSCSIMappings>
    </VirtualIOServer></content></entry>"""
    resource = parse_feed(raw)[0]["Resource"]
    with pytest.raises(ValueError, match="backing"):
        baseline.mapping_rows(resource, "2")
    document["scope"]["vios_ids"] = [2]
    document["tables"]["vios"] = [
        {"id": "2", "name": "vios-R1", "uuid": "11111111-1111-1111-1111-111111111111"}
    ]
    document["tables"]["vios_mappings"] = [
        {
            "vios_id": "2",
            "server": "adapter-R1",
            "slot": "10",
            "remote_id": "3",
            "remote_slot": "11",
            "target": "",
            "client": "{}",
            "backing": json.dumps(
                {
                    "storage": {"VirtualDisk": ""} if storage else {},
                    "target": {
                        "LogicalVolumeVirtualTargetDevice": {"TargetName": "target-R1"}
                    },
                }
            ),
        }
    ]
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps(document))
    assert baseline.main(["compare", str(path), str(path)]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("uuid_field", ["system", "partition", "whitespace"])
async def test_uuid_shaped_names_refused_before_resolution(
    monkeypatch, document, uuid_field
):
    ambiguous = "11111111-1111-1111-1111-111111111111"
    system = ambiguous if uuid_field == "system" else "sys-R1"
    if uuid_field == "whitespace":
        system = f" {ambiguous} "
    partitions = document["tables"]["partitions"]
    if uuid_field == "partition":
        partitions[0]["name"] = ambiguous
    hmc = AsyncMock()
    hmc.__aenter__.return_value = hmc
    monkeypatch.setattr(baseline, "HMCClient", lambda config: hmc)
    hmc._get.side_effect = AssertionError("ambiguous name reached remote reads")
    resolver = AsyncMock(return_value=ambiguous)
    monkeypatch.setattr(baseline, "resolve_vios_uuid", resolver)
    with pytest.raises(ValueError, match="cannot safely select"):
        await baseline.capture_vios(object(), system, partitions, document["tables"])
    resolver.assert_not_awaited()
    hmc._get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "child", ["<error>unavailable</error>", '<link rel="next" href="page2"/>']
)
async def test_incomplete_atom_collection(child):
    hmc = AsyncMock()
    hmc._get.return_value = f'<feed xmlns="{baseline.ATOM_NS}">{child}</feed>'
    with pytest.raises(ValueError, match="feed"):
        await baseline.read_resources(hmc, "/fixture", "VolumeGroup")


@pytest.mark.asyncio
async def test_physical_port_coverage(document, monkeypatch):
    from hmcpctl.ssh import sriov
    from hmcpctl.ssh.commands import HMC_NO_RESULTS

    fixture = (
        Path(__file__).parents[1] / "fixtures/live/cli-sriov-adapters-v11r2-p9.json"
    )
    remote = AsyncMock(return_value=json.loads(fixture.read_text())["stdout"])
    monkeypatch.setattr(sriov, "run_hmc_command", remote)
    document["tables"]["sriov_adapters"] = await sriov.list_sriov_adapter_rows(
        object(), "sys-R1"
    )
    remote.return_value = HMC_NO_RESULTS
    document["tables"]["sriov_physical"] = await sriov.list_sriov_physical_port_rows(
        object(), "sys-R1", "2"
    )
    with pytest.raises(ValueError, match="physical-port coverage"):
        baseline.validate(document)
    remote.side_effect = [
        json.loads(
            (fixture.parent / f"cli-sriov-physport-{level}-v11r2-p9.json").read_text()
        )["stdout"]
        for level in ("roce", "ethc", "eth")
    ]
    document["tables"]["sriov_physical"] = await sriov.list_sriov_physical_port_rows(
        object(), "sys-R1", "2"
    )
    with pytest.raises(ValueError, match="logical-port coverage"):
        baseline.validate(document)
    remote.side_effect = None
    remote.return_value = json.loads(
        (fixture.parent / "cli-sriov-logport-eth-v11r2-p9.json").read_text()
    )["stdout"]
    document["tables"][
        "sriov_logical"
    ] = await sriov.list_sriov_configured_logical_port_rows(object(), "sys-R1", "2")
    assert len(baseline.validate(document)["tables"]["sriov_physical"]) == 4
    assert len(baseline.validate(document)["tables"]["sriov_logical"]) == 5
    document["tables"]["sriov_logical"][0]["phys_port_id"] = "99"
    with pytest.raises(ValueError, match="logical-port coverage"):
        baseline.validate(document)
    document["tables"]["sriov_logical"] = []
    for port in document["tables"]["sriov_physical"]:
        port["curr_eth_logical_ports"] = port["config_logical_ports"] = "0"
    assert baseline.validate(document)["tables"]["sriov_logical"] == []


def test_package_ownership_and_executable_dispatch(monkeypatch):
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                'import sys; sys.modules["live_test_baseline"] = None; '
                "from live_test import baseline; "
                'assert baseline.main.__module__ == "live_test.baseline"'
            ),
        ],
        cwd=Path(__file__).parents[2] / "scripts",
        check=True,
    )
    owner = baseline
    wrapper = importlib.import_module("live_test_baseline")
    assert wrapper.baseline is owner
    assert not {"TABLES", "capture", "validate", "main"} & vars(wrapper).keys()
    calls = []
    monkeypatch.setattr(owner, "main", lambda: calls.append("dispatched") or 7)
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(wrapper.__file__)), run_name="__main__")
    assert result.value.code == 7
    assert calls == ["dispatched"]


def test_package_consumers_do_not_import_executable():
    scripts = Path(__file__).parents[2] / "scripts"
    for path in scripts.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name != "live_test_baseline" for alias in node.names
                ), path
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "live_test_baseline", path
