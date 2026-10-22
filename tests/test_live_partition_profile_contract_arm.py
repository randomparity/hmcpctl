"""Bounded disposable-profile contract experiment; all transports mocked."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import partition_profile_contract as arm

FIXTURE = (
    Path(__file__).parent / "fixtures/partition_profile_contract/normalized-profile.xml"
)


def test_clone_diagnostic_keeps_configuration_and_separates_root_identity():
    before = FIXTURE.read_text()
    root = arm.profile(before)
    arm.scalar(root, "ProfileName").text = "copy-a"
    arm.scalar(root, "SettingID").text = "987"
    after = arm.serialize(root)
    result = arm.compare_profiles(before, after)
    assert result["strict_equal"] is False
    assert result["configuration_equal"] is True
    assert set(result["root_identity_deltas"]) == {"ProfileName", "SettingID"}
    arm.scalar(root.find(arm.UOM + "ProfileMemory"), "DesiredMemory").text = "999"
    assert not arm.compare_profiles(before, arm.serialize(root))["configuration_equal"]


def test_absent_zero_and_unknown_nested_identity_are_not_normalized():
    before = FIXTURE.read_text()
    root = arm.profile(before)
    memory = root.find(arm.UOM + "ProfileMemory")
    memory.remove(arm.scalar(memory, "DesiredHugePageCount"))
    assert not arm.compare_profiles(before, arm.serialize(root))["configuration_equal"]
    root = arm.profile(before)
    arm.scalar(
        root.find(arm.UOM + "ProcessorAttributes").find(
            arm.UOM + "SharedProcessorConfiguration"
        ),
        "SharedProcessorPoolID",
    ).text = "999"
    assert not arm.compare_profiles(before, arm.serialize(root))["configuration_equal"]


def test_projection_preserves_optional_writable_fields_and_refuses_adapters():
    body = arm.clone_payload(FIXTURE.read_text(), "copy-a")
    root = arm.profile(body)
    assert root.find(arm.UOM + "SettingID") is None
    assert root.find(".//" + arm.UOM + "ManualEntitledModeEnabled") is None
    assert root.find(".//" + arm.UOM + "DesiredHugePageCount").text == "0"
    original = arm.profile(FIXTURE.read_text())
    arm.ET.SubElement(
        original.find(arm.UOM + "IOConfigurationInstance"),
        arm.UOM + "VirtualSlotNumber",
    ).text = "5"
    with pytest.raises(ValueError, match="shape"):
        arm.clone_payload(arm.serialize(original), "copy-a")


def test_memory_patch_preserves_identity_and_requires_exact_bounds():
    before = FIXTURE.read_text()
    changed = arm.memory_payload(before, 512, 768)
    assert (
        arm.scalar(arm.profile(changed), "SettingID").text
        == arm.scalar(arm.profile(before), "SettingID").text
    )
    with pytest.raises(ValueError):
        arm.memory_payload(before, 768, 1024)
    with pytest.raises(ValueError):
        arm.memory_payload(before, 512, 1025)


def test_uncertain_intent_cannot_repeat_or_clean_up(tmp_path):
    journal = arm.Journal(tmp_path / "journal.json")
    journal.intent("create")
    with pytest.raises(ValueError, match="pending"):
        journal.intent("create")
    assert not journal.cleanup_permitted
    journal.settle("create")
    assert journal.cleanup_permitted
    with pytest.raises(ValueError, match="replay"):
        journal.intent("create")


def test_transport_budget_reserves_recovery_and_logout():
    budget = arm.Budget(limit=4)
    for _ in range(3):
        budget.take()
    with pytest.raises(ValueError, match="budget"):
        budget.take()
    budget.take(logoff=True)
    assert budget.used == 4


@pytest.mark.parametrize(
    "field,value",
    [("DesiredHugePageCount", "0"), ("ManualEntitledModeEnabled", "false")],
)
def test_normalization_can_seed_only_observed_absent_optional_values(field, value):
    actual = FIXTURE.read_text()
    root = arm.profile(actual)
    memory = root.find(arm.UOM + "ProfileMemory")
    memory.remove(arm.scalar(memory, field))
    expected = arm.serialize(root)
    assert arm.normalization_permitted(expected, actual)
    changed = arm.profile(actual)
    arm.scalar(changed.find(arm.UOM + "ProfileMemory"), field).text = "123"
    assert not arm.normalization_permitted(expected, arm.serialize(changed))


def test_configuration_diff_reports_complete_order_and_attribute_changes():
    before = FIXTURE.read_text()
    root = arm.profile(before)
    memory = root.find(arm.UOM + "ProfileMemory")
    memory[1], memory[2] = memory[2], memory[1]
    result = arm.compare_profiles(before, arm.serialize(root))
    assert not result["configuration_equal"] and result["differences"]
    assert result["differences_complete"] and not result["recovery_admitted"]
    root = arm.profile(before)
    arm.scalar(root, "SettingID").text = "123"
    assert not arm.normalization_permitted(before, arm.serialize(root))


def test_foreign_namespace_and_duplicate_fields_refuse_projection():
    root = arm.profile(FIXTURE.read_text())
    arm.ET.SubElement(root, "{https://foreign.test/}DesiredMemory").text = "512"
    with pytest.raises(ValueError, match="shape"):
        arm.clone_payload(arm.serialize(root), "copy-a")
    root = arm.profile(FIXTURE.read_text())
    root.append(copy.deepcopy(arm.scalar(root, "SettingID")))
    with pytest.raises(ValueError, match="duplicate"):
        arm.profile(arm.serialize(root))


def experiment(tmp_path, monkeypatch, fault=None):
    import hashlib
    import json
    from types import SimpleNamespace

    import httpx
    from live_test import profile_contract_run as flow
    from live_test.config import LiveTestConfig

    from hmcpctl.config import HMCConfig
    from hmcpctl.ssh import lpar as ssh_lpar
    from hmcpctl.ssh import profiles as ssh_profiles
    from hmcpctl.xmlutil import element_to_dict

    packet = {
        "authorized": True,
        "host": "hmc.test",
        "port": 12443,
        "user": "test-account",
        "agent_id": "test-agent",
        "system_uuid": "00000000-0000-4000-8000-000000000001",
        "system_name": "sys-R1",
        "mtms": "9009-42A*REDACTED",
        "expected_build": "2408210051",
        "tested_commit": "a" * 40,
        "tested_tree": "b" * 40,
        "fixture": "hmcpctl-live-prof-1234abcd",
        "base": "q1724-base",
        "copies": ["q1724-copy-a", "q1724-copy-b"],
        "caller": "q1724-1234abcd",
        "window_dir": str(tmp_path),
    }
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet))
    packet_path.chmod(0o600)
    config = LiveTestConfig(
        system_name="sys-R1",
        partition_profile_contract_packet=str(packet_path),
        partition_profile_contract_sha256=hashlib.sha256(
            packet_path.read_bytes()
        ).hexdigest(),
    )
    hmc_config = HMCConfig.from_mapping(
        {
            **{key: packet[key] for key in ("host", "port", "user", "agent_id")},
            "password": "test-password",  # pragma: allowlist secret — synthetic mocked credential
            "verify_ssl": True,
        }
    )
    resource = json.loads((FIXTURE.parent / "partition-resource.json").read_text())
    world = SimpleNamespace(
        created=False,
        deleted=False,
        records={},
        posts=[],
        puts=[],
        ssh=[],
        instances=[],
        resource=resource,
        packet=packet,
        serial=100,
        snapshots=0,
    )
    fixture_uuid = "00000000-0000-4000-8000-000000000099"
    production = [
        {
            "UUID": f"00000000-0000-4000-8000-{n:012}",
            "kind": "LogicalPartition",
            "Resource": {"PartitionName": f"production-{n}"},
            "profiles": [],
        }
        for n in (10, 11, 12)
    ]

    def install(xml, name):
        world.serial += 1
        uuid = f"00000000-0000-4000-8000-{world.serial:012}"
        root = arm.profile(xml)
        for node in list(root):
            if node.tag in {
                arm.UOM + k
                for k in ("Metadata", "AssociatedPartition", "ProfileType", "SettingID")
            }:
                root.remove(node)
        metadata = arm.ET.SubElement(root, arm.UOM + "Metadata")
        atom = arm.ET.SubElement(metadata, arm.UOM + "Atom")
        arm.ET.SubElement(atom, arm.UOM + "AtomID").text = uuid
        arm.ET.SubElement(atom, arm.UOM + "AtomCreated").text = "12345"
        arm.ET.SubElement(
            root,
            arm.UOM + "AssociatedPartition",
            {
                "href": f"https://hmc.test:12443/rest/api/uom/ManagedSystem/{packet['system_uuid']}/LogicalPartition/{fixture_uuid}",
                "rel": "related",
                "kb": "ROR",
            },
        )
        arm.ET.SubElement(
            root, arm.UOM + "ProfileType", {"kb": "ROR"}
        ).text = "REG_LPAR_PROFILE_TYPE"
        arm.ET.SubElement(root, arm.UOM + "SettingID", {"kb": "ROO"}).text = str(
            world.serial
        )
        memory = root.find(arm.UOM + "ProfileMemory")
        if memory.find(arm.UOM + "ManualEntitledModeEnabled") is None:
            arm.ET.SubElement(
                memory, arm.UOM + "ManualEntitledModeEnabled", {"kb": "ROR"}
            ).text = "false"
        arm.scalar(root, "ProfileName").text = name
        original = arm.profile(FIXTURE.read_text())
        root_order = {n.tag: i for i, n in enumerate(original)}
        memory_order = {
            n.tag: i for i, n in enumerate(original.find(arm.UOM + "ProfileMemory"))
        }
        root[:] = sorted(root, key=lambda n: root_order.get(n.tag, 999))
        memory[:] = sorted(memory, key=lambda n: memory_order.get(n.tag, 999))
        world.records[uuid] = {"root": root, "etag": "1"}
        return uuid

    def entry():
        profiles = []
        links = []
        for uuid, record in world.records.items():
            href = f"https://hmc.test:12443/rest/api/uom/ManagedSystem/{packet['system_uuid']}/LogicalPartition/{fixture_uuid}/LogicalPartitionProfile/{uuid}"
            profiles.append(
                {
                    "UUID": uuid,
                    "link": href,
                    "Resource": element_to_dict(record["root"]),
                }
            )
            links.append({"href": href, "rel": "related"})
        result = copy.deepcopy(world.resource)
        result["PartitionUUID"] = fixture_uuid
        result["AssociatedManagedSystem"] = {
            "href": f"https://hmc.test:12443/rest/api/uom/ManagedSystem/{packet['system_uuid']}"
        }
        result["PartitionProfiles"] = {"link": links}
        return {
            "UUID": fixture_uuid,
            "kind": "LogicalPartition",
            "Resource": result,
            "profiles": sorted(profiles, key=lambda p: p["UUID"]),
        }

    class FakeNative(arm.Native):
        def __init__(self, *args):
            super().__init__(*args)
            world.instances.append(self)

        async def logon(self):
            self.budget.take()

        async def logoff(self):
            self.budget.take(logoff=True)

        async def command(self, command, **kwargs):
            self.budget.take()
            return "Version: 10 Release: 3 Service Pack: 1060 build2408210051"

        async def snapshot(self):
            world.snapshots += 1
            rows = copy.deepcopy(production)
            if world.created and not world.deleted:
                rows.append(entry())
            for _ in range(5 + len(rows)):
                self.budget.take()
            if fault == "cleanup-owner" and len(world.posts) == 2:
                world.resource["Description"] = (
                    "[hmcpctl owner:other created:2026-10-22]"
                )
                rows[-1] = entry()
            return {
                "partitions": sorted(rows, key=lambda e: e["UUID"]),
                "namespace": [],
                "system_auto_off": "false",
                "capacity": {
                    "AssociatedSystemMemoryConfiguration": {
                        "CurrentAvailableSystemMemory": "4096",
                        "CurrentLogicalMemoryBlockSize": "16",
                    },
                    "AssociatedSystemProcessorConfiguration": {
                        "CurrentAvailableSystemProcessorUnits": "1",
                        "MinimumProcessorUnitsPerVirtualProcessor": "0.05",
                    },
                },
            }

        async def _request(self, method, path, **kwargs):
            self.budget.take()
            if method == "PUT":
                world.puts.append(path)
                root = arm.profile(kwargs["content"])
                name = arm.scalar(root, "ProfileName").text
                uuid = install(kwargs["content"], name)
                if fault == "copy-configuration":
                    arm.scalar(
                        world.records[uuid]["root"].find(arm.UOM + "ProfileMemory"),
                        "MemoryWeight",
                    ).text = "7"
                if fault == "copy-source":
                    first = next(iter(world.records.values()))["root"]
                    arm.scalar(
                        first.find(arm.UOM + "ProfileMemory"), "MemoryWeight"
                    ).text = "9"
                return httpx.Response(200)
            uuid = path.rsplit("/", 1)[1]
            record = world.records[uuid]
            if method == "POST":
                world.posts.append(path)
                assert kwargs["headers"]["If-Match"] == record["etag"]
                record["root"] = arm.profile(kwargs["content"])
                record["etag"] = str(int(record["etag"]) + 1)
                if fault == "post-timeout":
                    raise TimeoutError("reply lost after apply")
                return httpx.Response(200)
            assert method == "GET"
            outer = arm.ET.Element(arm.ATOM + "entry")
            arm.ET.SubElement(outer, arm.ATOM + "id").text = uuid
            arm.ET.SubElement(outer, arm.ATOM + "published").text = str(
                self.budget.used
            )
            arm.ET.SubElement(outer, arm.ATOM + "content").append(
                copy.deepcopy(record["root"])
            )
            return httpx.Response(
                200, text=arm.serialize(outer), headers={"ETag": record["etag"]}
            )

        async def get_logical_partition(self, uuid):
            self.budget.take()
            assert uuid == fixture_uuid
            return entry()

        async def get_managed_system(self, uuid):
            self.budget.take()
            return {"UUID": uuid, "Resource": {"SystemName": packet["system_name"]}}

        async def list_system_summaries(self):
            self.budget.take()
            return [
                {"UUID": packet["system_uuid"], "SystemName": packet["system_name"]}
            ]

        async def list_logical_partitions(self, system_uuid=None):
            self.budget.take()
            return [entry()] if world.created and not world.deleted else []

        async def get_quick_property(self, kind, uuid, name):
            self.budget.take()
            return world.resource["PartitionState"]

        async def delete_logical_partition(self, uuid):
            self.budget.take()
            assert uuid == fixture_uuid
            world.deleted = True

    async def ssh(config, command, **kwargs):
        world.ssh.append(command)
        if command.startswith("mksyscfg"):
            assert (
                "min_mem=512" in command
                and "desired_mem=512" in command
                and "max_mem=1024" in command
            )
            assert (
                "desired_proc_units=0.05" in command
                and "max_virtual_slots=2" in command
            )
            assert "all_resources=1" not in command
            world.created = True
            install(FIXTURE.read_text(), packet["base"])
            if fault == "create-timeout":
                raise TimeoutError("create reply lost")
        elif command.startswith("chsyscfg"):
            assert (
                packet["fixture"] in command
                and "owner:test-agent" in command
                and packet["caller"] in command
            )
            world.resource["Description"] = (
                "[hmcpctl owner:test-agent created:2026-10-22] [caller q1724-1234abcd]"
            )
        else:
            assert command.startswith("lssyscfg") and "description" in command
            return world.resource["Description"]
        return ""

    monkeypatch.setattr(arm, "Native", FakeNative)
    monkeypatch.setattr(arm, "verify_revision", lambda packet: None)
    monkeypatch.setattr(ssh_lpar, "run_hmc_command", ssh)
    monkeypatch.setattr(ssh_profiles, "run_hmc_command", ssh)
    return config, hmc_config, world, flow


@pytest.mark.asyncio
async def test_full_experiment_uses_real_serializer_normal_owner_and_independent_recovery(
    tmp_path, monkeypatch
):
    config, hmc, world, flow = experiment(tmp_path, monkeypatch)
    admission = await flow.preflight(hmc, config)
    result = await flow.execute(hmc, config)
    recovered = await flow.recover(hmc, config)
    assert result["completed"] == list(arm.EFFECTS)
    assert result["status"] == "CONTRACT_OBSERVED" and result["deleted"]
    assert len(world.posts) == 2 and len(world.puts) == 2
    assert len(world.ssh) == 3  # create,stamp,normal-delete authorization description
    assert all(
        v["configuration_equal"] and not v["recovery_admitted"]
        for v in result["copies"].values()
    )
    assert recovered["status"] == "CLEAN"
    assert (
        admission["transports_upper_bound"]
        + result["arm_transports_upper_bound"]
        + recovered["transports_upper_bound"]
        <= 192
    )
    assert not result["pending"]
    with pytest.raises(FileExistsError):
        await flow.execute(hmc, config)
    with pytest.raises(FileExistsError):
        await flow.preflight(hmc, config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["create-timeout", "post-timeout", "copy-source", "cleanup-owner"]
)
async def test_unknown_or_drift_retains_fixture_without_replay_or_cleanup(
    tmp_path, monkeypatch, fault
):
    import json

    config, hmc, world, flow = experiment(tmp_path, monkeypatch, fault)
    await flow.preflight(hmc, config)
    with pytest.raises((TimeoutError, ValueError, PermissionError)):
        await flow.execute(hmc, config)
    journal = json.loads((tmp_path / "journal.json").read_text())
    assert not world.deleted and journal["status"] == "STOP_RETAIN"
    if fault != "cleanup-owner":
        assert journal["pending"] is not None
    with pytest.raises(ValueError):
        await flow.recover(hmc, config)
    assert not world.deleted and len(world.puts) <= (
        2 if fault == "cleanup-owner" else 1
    )


@pytest.mark.asyncio
async def test_known_copy_difference_stops_chain_but_permits_known_state_cleanup(
    tmp_path, monkeypatch
):
    config, hmc, world, flow = experiment(tmp_path, monkeypatch, "copy-configuration")
    await flow.preflight(hmc, config)
    result = await flow.execute(hmc, config)
    assert result["status"] == "CONTRACT_DIFFERENCE" and world.deleted
    assert len(world.puts) == 1 and len(world.posts) == 1
    assert not result["copies"]["copy-a"]["configuration_equal"]
    assert (await flow.recover(hmc, config))["status"] == "CLEAN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "other.test"),
        ("port", 443),
        ("user", "other-test-account"),
        ("agent_id", "other-test-agent"),
    ],
)
async def test_effective_target_mismatch_refuses_before_any_native_client(
    tmp_path, monkeypatch, field, value
):
    config, hmc, world, flow = experiment(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="before authentication"):
        await flow.preflight(hmc.model_copy(update={field: value}), config)
    assert world.instances == [] and not world.created


@pytest.mark.asyncio
async def test_modified_packet_refuses_before_revision_check_or_connection(
    tmp_path, monkeypatch
):
    config, hmc, world, flow = experiment(tmp_path, monkeypatch)
    path = Path(config.partition_profile_contract_packet)
    path.write_text(
        path.read_text().replace('"authorized": true', '"authorized": false')
    )
    with pytest.raises(ValueError, match="digest"):
        await flow.preflight(hmc, config)
    assert world.instances == [] and not world.created


def test_real_revision_check_rejects_other_commit_before_authentication():
    with pytest.raises(ValueError, match="deployed clean commit/tree"):
        arm.verify_revision({"tested_commit": "0" * 40, "tested_tree": "0" * 40})


def test_strict_read_only_exempts_outer_published_text():
    from live_test import profile_contract_run as flow

    original = FIXTURE.read_text()
    before = {"xml": original, "etag": "same"}
    tree = arm.fromstring(original)
    tree.find(arm.ATOM + "published").text = "later"
    after = {"xml": arm.serialize(tree), "etag": "same"}
    assert flow.same_read(before, after)
    tree.find(arm.ATOM + "published").set("changed", "yes")
    assert not flow.same_read(before, {"xml": arm.serialize(tree), "etag": "same"})
    assert not flow.same_read(before, {**after, "etag": "different"})
    tree = arm.fromstring(original)
    root = next(tree.iter(arm.UOM + "LogicalPartitionProfile"))
    arm.scalar(root, "SettingID").text = "different"
    assert not flow.same_read(before, {"xml": arm.serialize(tree), "etag": "same"})


@pytest.mark.asyncio
async def test_native_reader_validates_real_containment_etag_and_uuid(
    tmp_path, respx_mock
):
    from urllib.parse import urlsplit

    import httpx

    from hmcpctl.config import HMCConfig

    root = arm.profile(FIXTURE.read_text())
    parent_path = urlsplit(root.find(arm.UOM + "AssociatedPartition").get("href")).path
    parts = parent_path.split("/")
    packet = {
        "host": "hmc.test",
        "port": 12443,
        "user": "test-account",
        "agent_id": "test-agent",
        "system_uuid": parts[5],
    }
    partition_uuid = parts[-1]
    profile_uuid = root.find(".//" + arm.UOM + "AtomID").text
    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "port": 12443,
            "user": "test-account",
            "password": "test-password",  # pragma: allowlist secret — synthetic mocked credential
            "agent_id": "test-agent",
            "verify_ssl": True,
        }
    )
    api = arm.Native(config, packet, arm.Budget(20), tmp_path)
    path = api.profile_path(partition_uuid, profile_uuid)
    route = respx_mock.get("https://hmc.test:12443" + path)
    try:
        route.mock(
            return_value=httpx.Response(
                200, text=FIXTURE.read_text(), headers={"ETag": "opaque"}
            )
        )
        assert (await api.read_profile(partition_uuid, profile_uuid))[
            "etag"
        ] == "opaque"
        route.mock(return_value=httpx.Response(200, text=FIXTURE.read_text()))
        with pytest.raises(ValueError, match="ETag"):
            await api.read_profile(partition_uuid, profile_uuid)
        wrong = FIXTURE.read_text().replace(
            "https://hmc.test:12443", "https://other.test:12443"
        )
        route.mock(
            return_value=httpx.Response(200, text=wrong, headers={"ETag": "opaque"})
        )
        with pytest.raises(ValueError, match="parent/destination"):
            await api.read_profile(partition_uuid, profile_uuid)
    finally:
        await api._http.aclose()


@pytest.mark.asyncio
async def test_error_capture_cannot_retain_echoed_authentication_tokens(
    tmp_path, respx_mock
):
    import json

    import httpx

    from hmcpctl.config import HMCConfig

    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "port": 12443,
            "user": "test-account",
            "password": "test-password",  # pragma: allowlist secret — synthetic mocked credential
            "agent_id": "test-agent",
            "verify_ssl": True,
        }
    )
    packet = {
        "host": "hmc.test",
        "port": 12443,
        "user": "test-account",
        "agent_id": "test-agent",
    }
    api = arm.Native(config, packet, arm.Budget(4), tmp_path)
    respx_mock.get("https://hmc.test:12443/rest/api/uom/example").mock(
        return_value=httpx.Response(
            500,
            text="X-API-Session: private-token; cookie: private-cookie",
            headers={"Set-Cookie": "private-cookie"},
        )
    )
    try:
        await api._request("GET", "/rest/api/uom/example")
    finally:
        await api._http.aclose()
    captured = (tmp_path / "response-1.json").read_text()
    assert "private-token" not in captured and "private-cookie" not in captured
    assert json.loads(captured)["body"] is None


@pytest.mark.asyncio
async def test_independent_recovery_cannot_adopt_incomplete_dispatch(
    tmp_path, monkeypatch
):
    from live_test.config import LiveTestConfig

    from hmcpctl.config import HMCConfig

    for run in (
        {"group": arm.GROUP, "subtasks": [arm.SUBTASK], "tree_clean": False},
        {"group": arm.GROUP, "subtasks": [arm.SUBTASK, 10], "tree_clean": True},
    ):
        with pytest.raises(ValueError, match="provenance"):
            await arm.recover_document(
                HMCConfig.from_mapping({}),
                {"run": run, "config": LiveTestConfig().__dict__},
            )


def test_older_recovery_documents_keep_experiment_disabled():
    import json
    from dataclasses import asdict

    from live_test.config import LiveTestConfig, decode_saved_config

    saved = json.loads(json.dumps(asdict(LiveTestConfig())))
    saved.pop("partition_profile_contract_packet")
    saved.pop("partition_profile_contract_sha256")
    restored = decode_saved_config(saved)
    assert restored.partition_profile_contract_packet == ""
    assert restored.partition_profile_contract_sha256 == ""
