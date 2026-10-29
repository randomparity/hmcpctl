"""ST68 proves finite input, physical boundaries and refusal before further effects."""

import copy
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import lpar_boot_start as arm

from hmcpctl.config import HMCConfig

SYSTEM = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TARGET = "22222222-2222-4222-8222-222222222222"


def packet():
    return {
        "candidate": {
            "commit": "a" * 40,
            "tree": "b" * 40,
            "hmc_identity": {
                "host": "hmc.test",
                "port": 443,
                "user": "operator",
                "verify_ssl": True,
                "agent_id": "test",
            },
        },
        "expires_utc": (datetime.now(UTC) + timedelta(minutes=19)).isoformat(),
        "system_uuid": SYSTEM,
        "partition_type": "AIX/Linux",
        "name": "hmcpctl-live-boot-12345678",
        "caller_token": "q1770-12345678",
        "cleanup_authorized": True,
        "case": "create-auto-0",
        "controllers": [],
        "boot_target": None,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"case": "create-auto-2"},
        {"controllers": [{}]},
        {"system_uuid": SYSTEM.upper()},
        {"cleanup_authorized": 1},
        {"name": "existing"},
        {"caller_token": "q1770-other"},
        {"boot_target": {}},
        {"expires_utc": "2026-01-01"},
    ],
)
def test_packet_refuses_unapproved_structure(change):
    p = packet() | change
    with pytest.raises((TypeError, ValueError)):
        arm.validate_packet(p)


def test_fixed_cases_do_not_accept_native_values_or_lost_false():
    p = packet()
    arm.validate_packet(p)
    assert arm.settings(p).auto_start is False
    p["case"] = "create-omitted"
    assert not arm.settings(p).requested()
    p["case"] = "create-mode-sms"
    p["partition_type"] = "OS400"
    with pytest.raises(ValueError):
        arm.validate_packet(p)


def test_pre_session_config_preserves_identity_and_stricter_limit():
    c = HMCConfig.from_mapping(
        packet()["candidate"]["hmc_identity"]
        | {
            "password": "test",  # pragma: allowlist secret — synthetic test credential
        }
    )
    bounded = arm.bounded_config(c)
    assert bounded.max_response_bytes == 262144
    assert c.max_response_bytes == 32 * 1024 * 1024
    assert arm.serial.connection(c) == arm.serial.connection(bounded)
    assert (
        arm.bounded_config(
            c.model_copy(update={"max_response_bytes": 100})
        ).max_response_bytes
        == 100
    )


def test_current_grant_and_invocation_cannot_be_restored(monkeypatch):
    p = packet()
    monkeypatch.setattr(arm.serial, "revision", lambda _: None)
    c = HMCConfig.from_mapping(p["candidate"]["hmc_identity"])
    w = arm.Window(p, "digest", "digest", True)
    w.check(c, w.invocation)
    for field, value in [
        ("grant", "old"),
        ("exclusive", False),
        ("invocation", "restored"),
    ]:
        broken = copy.copy(w)
        setattr(broken, field, value)
        with pytest.raises((ValueError, PermissionError)):
            broken.check(c, w.invocation)


@pytest.mark.parametrize(
    "mode,limits",
    [
        ("preflight", (32, 4, 4 * 1024**2)),
        ("run", (64, 8, 8 * 1024**2)),
        ("recover", (32, 4, 4 * 1024**2)),
    ],
)
def test_physical_limits_are_enforced_before_dispatch(mode, limits):
    b = arm.Budget(mode, packet())
    assert b.maximum == limits
    b.used[0] = limits[0]
    with pytest.raises(ValueError):
        b.reserve(1, 0, 1, 1)
    assert b.used[0] == limits[0]
    b.used = [0, 0, limits[2]]
    with pytest.raises(ValueError):
        b.reserve(0, 1, 1, 1)


@pytest.mark.asyncio
async def test_actual_response_stream_cap_and_failed_physical_call_count():
    import httpx

    from hmcpctl.errors import HMCError

    p = packet()
    c = HMCConfig.from_mapping(
        p["candidate"]["hmc_identity"]
        | {
            "password": "test",  # pragma: allowlist secret — synthetic test credential
            "max_response_bytes": 4,
        }
    )
    h = arm.CountedHMC(arm.bounded_config(c), arm.Budget("preflight", p))
    await h._http.aclose()
    h._http = httpx.AsyncClient(
        base_url="https://hmc.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"12345", request=request)
        ),
    )
    try:
        with pytest.raises(HMCError, match="exceeds limit"):
            await h._request("GET", "/rest/api/uom/ManagedSystem/" + SYSTEM)
        assert h.budget.used[0] == 1
        assert h.config is c or h.config.max_response_bytes == 4
        assert h.budget.used[2] >= 4
    finally:
        await h._http.aclose()


@pytest.mark.asyncio
async def test_read_only_physical_boundary_rejects_all_resource_writers():
    p = packet()
    c = HMCConfig.from_mapping(
        p["candidate"]["hmc_identity"]
        | {
            "password": "test",  # pragma: allowlist secret — synthetic test credential
        }
    )
    h = arm.CountedHMC(arm.bounded_config(c), arm.Budget("recover", p))
    try:
        for method, path in [
            ("POST", "/rest/api/uom/LogicalPartition/" + TARGET),
            ("DELETE", "/rest/api/uom/VirtualIOServer/" + TARGET),
        ]:
            with pytest.raises(PermissionError):
                await h._request(method, path)
        with pytest.raises(PermissionError):
            await h.ssh(h.config, "chsyscfg -r lpar -m x -i name=other")
        assert h.budget.used == [0, 0, 0]
    finally:
        await h._http.aclose()


@pytest.mark.asyncio
async def test_actual_owned_session_login_and_logout_are_counted():
    import httpx

    c = HMCConfig.from_mapping(
        packet()["candidate"]["hmc_identity"]
        | {
            "password": "test",  # pragma: allowlist secret — synthetic test credential
        }
    )
    h = arm.CountedHMC(arm.bounded_config(c), arm.Budget("preflight", packet()))
    await h._http.aclose()
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200,
            text='<LogonResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"><X-API-Session>owned-test</X-API-Session></LogonResponse>',
            request=request,
        )

    h._http = httpx.AsyncClient(
        base_url="https://hmc.test", transport=httpx.MockTransport(handle)
    )
    async with h:
        assert h._session_token == "owned-test"
    assert calls == [("PUT", "/rest/api/web/Logon"), ("DELETE", "/rest/api/web/Logon")]
    assert h.closed and h._http.is_closed
    assert h.budget.used[0] == 2


def fixture_entry():
    return {
        "identity": {
            "kind": "VirtualIOServer",
            "uuid": TARGET,
            "name": packet()["name"],
            "partition_id": 2,
        },
        "resource": {
            "PartitionState": "not activated",
            "PartitionType": "Virtual IO Server",
            "Description": "",
            "PartitionName": packet()["name"],
            "PartitionID": "2",
            "PartitionMemoryConfiguration": {"CurrentMemory": "0"},
            "PartitionProcessorConfiguration": {"HasDedicatedProcessors": "true"},
            "PartitionIOConfiguration": {"UnknownFutureIO": "retained"},
        },
        "profiles": {
            "profile": {
                "name": "default_profile",
                "etag": "fixed",
                "resource": {
                    "ProfileMemory": {
                        k: "1024"
                        for k in ("MinimumMemory", "DesiredMemory", "MaximumMemory")
                    },
                    "ProcessorAttributes": {
                        "HasDedicatedProcessors": "true",
                        "SharingMode": "keep idle procs",
                        "DedicatedProcessorConfiguration": {
                            k: "1"
                            for k in (
                                "MinimumProcessors",
                                "DesiredProcessors",
                                "MaximumProcessors",
                            )
                        },
                    },
                    "IOConfigurationInstance": {
                        "MaximumVirtualIOSlots": "8",
                        "VirtualOpticonnectPool": "false",
                    },
                },
            }
        },
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("MinimumMemory", None),
        ("DesiredMemory", "2048"),
        ("MaximumMemory", "NaN"),
        ("MaximumMemory", ["1024", "1024"]),
    ],
)
def test_saved_tuple_refuses_missing_wrong_nonfinite_duplicate(field, value):
    e = fixture_entry()
    e["profiles"]["profile"]["resource"]["ProfileMemory"][field] = value
    with pytest.raises((ValueError, TypeError, ArithmeticError)):
        arm.saved_resources(e)


def test_inactive_current_placeholder_is_preserved_and_never_used_as_saved_tuple():
    e = fixture_entry()
    original = copy.deepcopy(e)
    arm.saved_resources(e)
    assert e == original
    e["profiles"]["another"] = copy.deepcopy(e["profiles"]["profile"])
    with pytest.raises(ValueError):
        arm.saved_resources(e)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,readback,execution,verification",
    [
        (None, "expected", "unknown", "unavailable"),
        ("different", "expected", "acknowledged", "mismatch"),
        ("date-shifted", "expected", "acknowledged", "mismatch"),
        ("", "expected", "acknowledged", "mismatch"),
        ("expected", "different", "acknowledged", "mismatch"),
        ("expected", OSError(), "acknowledged", "unavailable"),
        (OSError(), "expected", "unknown", "unavailable"),
        ("expected", "expected", "acknowledged", "verified"),
    ],
)
async def test_actual_stamp_journal_separates_acknowledgment_and_marker(
    monkeypatch, tmp_path, physical_world, result, readback, execution, verification
):
    p = packet() | {"partition_type": "Virtual IO Server"}
    c = HMCConfig.from_mapping(
        p["candidate"]["hmc_identity"]
        | {
            "password": "test",  # pragma: allowlist secret — synthetic test credential
        }
    )
    h = arm.CountedHMC(arm.bounded_config(c), arm.Budget("run", p))
    monkeypatch.setattr(arm.serial, "revision", lambda _: None)
    h.window = arm.Window(p, "digest", "digest", True)
    e = fixture_entry()
    expected = f"[hmcpctl owner:test created:{arm.date.today().isoformat()}] [caller {p['caller_token']}]"
    response = copy.deepcopy(e["resource"])
    response["Description"] = expected if readback == "expected" else readback
    import xml.etree.ElementTree as ET

    from hmcpctl.documents.partition_profile import _records

    e["semantic"] = _records(
        physical_world.element("VirtualIOServer", e["resource"]), False
    )
    h.documents["/rest/api/uom/VirtualIOServer/" + TARGET] = ET.tostring(
        physical_world.element("VirtualIOServer", response), encoding="unicode"
    )
    h.get_uom = AsyncMock(
        side_effect=readback if isinstance(readback, Exception) else None,
        return_value={
            "UUID": TARGET,
            "ResourceType": "VirtualIOServer",
            "Resource": response,
        },
    )
    returned = expected if result == "expected" else result
    if result == "date-shifted":
        returned = expected.replace(
            arm.date.today().isoformat(),
            (arm.date.today() + timedelta(days=1)).isoformat(),
        )
    helper = AsyncMock(
        side_effect=result if isinstance(result, Exception) else None,
        return_value=returned,
    )
    monkeypatch.setattr(arm.native, "stamp_lpar_ownership", helper)
    tmp_path.chmod(0o700)
    path = tmp_path / "intent.json"
    j = {
        "mode": "run",
        "invocation": h.window.invocation,
        "baseline": {"system": {"SystemName": "system"}},
    }
    arm.serial.write(path, j, fresh=True)
    try:
        if verification == "verified":
            await arm.stamp_fixture(h, p, j, path, e)
        else:
            with pytest.raises((ValueError, OSError)):
                await arm.stamp_fixture(h, p, j, path, e)
        stored = arm.serial.read(path)["vios_stamp"]
        assert stored["execution"] == execution
        assert stored["verification"] == verification
        assert stored["returned_description"] == (
            returned if isinstance(returned, str) else None
        )
        helper.assert_awaited_once()
        assert h.effect is None
        assert "delete" not in h.sent
    finally:
        await h._http.aclose()


@pytest.fixture
def physical_world(monkeypatch, tmp_path):
    """Use real HMC/domain/profile/native code; replace only HTTP and SSH providers."""
    import csv
    import io
    import shlex
    import xml.etree.ElementTree as ET
    from types import SimpleNamespace

    import httpx
    from conftest import captured_system_entry

    from hmcpctl.documents.partition_profile import ATOM, N
    from hmcpctl.xmlutil import parse_feed

    tmp_path.chmod(0o700)
    monkeypatch.setattr(arm.serial, "revision", lambda _: None)
    p = packet()
    c = HMCConfig.from_mapping(
        p["candidate"]["hmc_identity"]
        | {
            "password": "test",  # pragma: allowlist secret — synthetic test credential
            "max_response_bytes": 65536,
        }
    )
    w = SimpleNamespace(
        packet=p,
        config=c,
        entries={},
        requests=[],
        ssh=[],
        path=tmp_path / "results.json",
        fail=None,
        pending="  original,exact  ",
        profiles={},
        extra_profiles={},
        etag="fixed",
        stamped=False,
    )
    system = parse_feed(
        f'<feed xmlns="{ATOM}">{captured_system_entry(SYSTEM, "system")}</feed>'
    )[0]["Resource"]
    system.update(
        State="operating",
        IsClassicHMCManagement="true",
        IsHMCPowerVMManagementController="false",
        IsPowerVMManagementController="false",
    )

    def element(tag, data):
        node = ET.Element(N + tag)
        if isinstance(data, dict):
            for key, value in data.items():
                if key == "@attrs":
                    node.attrib.update(value)
                elif key == "text":
                    node.text = str(value)
                elif isinstance(value, list):
                    node.extend(element(key, item) for item in value)
                else:
                    node.append(element(key, value))
        elif data is not None:
            node.text = str(data)
        return node

    def entry(kind, uid, resource):
        result = ET.Element("{" + ATOM + "}entry")
        ET.SubElement(result, "{" + ATOM + "}id").text = uid
        content = ET.SubElement(
            result,
            "{" + ATOM + "}content",
            {"type": "application/vnd.ibm.powervm.uom+xml; type=" + kind},
        )
        content.append(element(kind, resource))
        return result

    def feed(path, kind, items):
        root = ET.Element("{" + ATOM + "}feed")
        ET.SubElement(
            root,
            "{" + ATOM + "}link",
            {"rel": "SELF", "href": "https://hmc.test:443" + path},
        )
        root.extend(entry(kind, uid, r) for uid, r in items)
        return ET.tostring(root, encoding="unicode")

    def add(uid=TARGET, kind="VirtualIOServer", name=None):
        e = fixture_entry()
        r = e["resource"]
        r.update(
            PartitionName=name or p["name"],
            PartitionID="2" if uid == TARGET else "1",
            PartitionType="Virtual IO Server"
            if kind == "VirtualIOServer"
            else p["partition_type"],
            DefaultProfileName="default_profile",
            Metadata={"Atom": {"AtomID": uid, "AtomCreated": "before"}},
            BootListInformation={
                "PendingBootString": {"@attrs": {"kb": "CUD"}, "text": w.pending}
            },
        )
        r["BootListInformation"]["PendingBootString"] = w.pending
        profile = e["profiles"]["profile"]["resource"]
        pid = str(arm.uuid.uuid5(arm.uuid.UUID(uid), "default-profile"))
        profile.update(
            ProfileName="default_profile",
            SettingID="1",
            Metadata={"Atom": {"AtomID": pid, "AtomCreated": "before"}},
            AssociatedPartition={
                "@attrs": {"href": f"https://hmc.test:443/rest/api/uom/{kind}/{uid}"}
            },
            AutoStart="false",
            ConnectionMonitoringEnabled="false",
            BootMode="Normal",
            PowerControlPartitions="",
        )
        r.update(
            IsConnectionMonitoringEnabled="false",
            BootMode="Normal",
            PowerControlPartitions="",
            PartitionProfiles={
                "link": {
                    "@attrs": {
                        "href": f"https://hmc.test:443/rest/api/uom/{kind}/{uid}/LogicalPartitionProfile/{pid}"
                    }
                }
            },
        )
        opts = arm.settings(p)
        if opts.auto_start is not None:
            profile["AutoStart"] = str(opts.auto_start).lower()
        if opts.conn_monitoring is not None:
            r["IsConnectionMonitoringEnabled"] = profile[
                "ConnectionMonitoringEnabled"
            ] = str(opts.conn_monitoring).lower()
        if opts.boot_mode is not None:
            r["BootMode"] = profile["BootMode"] = {
                "norm": "Normal",
                "dd": "Diagnostic_With_Default_Boot_List",
                "ds": "Diagnostic_With_Stored_Boot_List",
                "of": "Open_Firmware",
                "sms": "System_Management_Services",
            }[opts.boot_mode]
        if p["controllers"]:
            links = {
                "link": [
                    {
                        "href": f"https://hmc.test:443/rest/api/uom/{t['kind']}/{t['uuid']}"
                    }
                    for t in p["controllers"]
                ]
            }
            # element() needs XML attributes for the recognized links.
            links = {"link": [{"@attrs": {"href": x["href"]}} for x in links["link"]]}
            r["PowerControlPartitions"] = profile["PowerControlPartitions"] = links
        r["AssociatedPartitionProfile"] = {
            "@attrs": {
                "href": f"https://hmc.test:443/rest/api/uom/{kind}/{uid}/LogicalPartitionProfile/{pid}"
            }
        }
        w.entries[uid] = (kind, r)
        w.profiles[uid] = (pid, profile)
        return r

    w.add = add
    w.element = element

    def handle(request):
        path = request.url.path
        w.requests.append((request.method, path))
        headers = {
            "ETag": w.etag,
            "Content-Type": "application/vnd.ibm.powervm.uom+xml",
        }
        if path == "/rest/api/uom/ManagedSystem/quick/All":
            return httpx.Response(
                200,
                json=[{"UUID": SYSTEM, "SystemName": "system", "State": "operating"}],
                request=request,
            )
        if path == "/rest/api/web/Logon":
            return httpx.Response(
                200,
                text='<LogonResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"><X-API-Session>owned</X-API-Session></LogonResponse>',
                request=request,
            )
        if request.method == "DELETE":
            del w.entries[TARGET]
            del w.profiles[TARGET]
            return httpx.Response(204, request=request)
        if request.method == "POST":
            root = ET.fromstring(request.content)
            pending = root.find(".//" + N + "PendingBootString")
            assert pending is not None
            w.pending = pending.text
            w.entries[TARGET][1]["BootListInformation"]["PendingBootString"] = w.pending
            return httpx.Response(204, request=request)
        if request.method == "PUT":
            add(
                kind="VirtualIOServer"
                if p["partition_type"] == "Virtual IO Server"
                else "LogicalPartition"
            )
            return httpx.Response(
                200,
                text=ET.tostring(
                    entry(w.entries[TARGET][0], TARGET, w.entries[TARGET][1]),
                    encoding="unicode",
                ),
                headers=headers,
                request=request,
            )
        bits = path.split("/")
        if path.endswith("LogicalPartitionProfile"):
            uid = bits[-2]
            pid, r = w.profiles[uid]
            xml = feed(
                path,
                "LogicalPartitionProfile",
                [(pid, r), *w.extra_profiles.get(uid, [])],
            )
        elif "LogicalPartitionProfile" in bits:
            uid = bits[5]
            pid, r = next(
                item
                for item in [w.profiles[uid], *w.extra_profiles.get(uid, [])]
                if item[0] == bits[-1]
            )
            xml = ET.tostring(
                entry("LogicalPartitionProfile", pid, r), encoding="unicode"
            )
        elif bits[-1] in ("LogicalPartition", "VirtualIOServer"):
            kind = bits[-1]
            xml = feed(
                path, kind, [(uid, r) for uid, (k, r) in w.entries.items() if k == kind]
            )
        elif bits[-1] in w.entries:
            kind, r = w.entries[bits[-1]]
            xml = ET.tostring(entry(kind, bits[-1], r), encoding="unicode")
        elif bits[-1] == SYSTEM:
            xml = ET.tostring(
                entry("ManagedSystem", SYSTEM, system), encoding="unicode"
            )
        elif "ManagedSystem" in bits:
            xml = feed(path, "ManagedSystem", [(SYSTEM, system)])
        else:
            raise AssertionError((request.method, path))
        return httpx.Response(200, text=xml, headers=headers, request=request)

    async def ssh(config, command, **kwargs):
        words = shlex.split(command)
        w.ssh.append(words)
        if words[0] == "lscomgmt":
            return "null\n"
        if words[:4] == ["lssyscfg", "-r", "sys", "-F"]:
            return SYSTEM + ",system\n"
        if words[0] == "mksyscfg":
            if w.fail == "create":
                add(
                    kind="VirtualIOServer"
                    if p["partition_type"] == "Virtual IO Server"
                    else "LogicalPartition"
                )
                raise OSError("injected lost acknowledgment")
            add(
                kind="VirtualIOServer"
                if p["partition_type"] == "Virtual IO Server"
                else "LogicalPartition"
            )
            return ""
        if words[0] == "chsyscfg":
            record = next(csv.reader([words[words.index("-i") + 1]], strict=True))
            values = dict(v.split("=", 1) for v in record)
            if "description" in values:
                w.entries[TARGET][1]["Description"] = values["description"]
                w.stamped = True
                if w.fail == "stamp-io":
                    w.entries[TARGET][1]["PartitionIOConfiguration"][
                        "UnknownFutureIO"
                    ] = "drift"
                if w.fail == "stamp-profile":
                    w.etag = "changed"
            else:
                w.pending = values["boot_string"]
                if w.fail == "boot":
                    raise OSError("injected lost acknowledgment")
            if w.fail == "boot-io":
                w.entries[TARGET][1]["PartitionIOConfiguration"]["UnknownFutureIO"] = (
                    "drift"
                )
            if w.fail == "boot-profile":
                w.etag = "changed"
            if w.fail == "boot-last":
                w.pending = "different"
            if w.fail == "boot-current-attr":
                w.entries[TARGET][1]["PartitionIOConfiguration"]["UnknownFutureIO"] = {
                    "@attrs": {"kxe": "true"},
                    "text": "retained",
                }
            if w.fail == "boot-profile-attr":
                w.profiles[TARGET][1]["ProfileMemory"]["MinimumMemory"] = {
                    "@attrs": {"kb": "ROO"},
                    "text": "1024",
                }
            return ""
        if words[-1] == "description":
            return w.entries[TARGET][1]["Description"]
        if words[-1] == "--header":
            stream = io.StringIO()
            writer = csv.writer(stream, lineterminator="\n")
            r = w.entries[TARGET][1]
            writer.writerow(
                [
                    "name",
                    "lpar_id",
                    "lpar_env",
                    "uuid",
                    "description",
                    "pend_boot_string",
                ]
            )
            writer.writerow(
                [
                    r["PartitionName"],
                    r["PartitionID"],
                    "vioserver",
                    TARGET,
                    r["Description"],
                    w.pending,
                ]
            )
            return stream.getvalue()
        raise AssertionError(words)

    w.handle = handle
    monkeypatch.setattr(arm, "run_hmc_command", ssh)

    async def client(mode):
        h = arm.CountedHMC(arm.bounded_config(w.config), arm.Budget(mode, p))
        await h._http.aclose()
        h._http = httpx.AsyncClient(
            base_url="https://hmc.test:443", transport=httpx.MockTransport(handle)
        )
        return h

    w.client = client
    return w


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["AIX/Linux", "Virtual IO Server"])
@pytest.mark.parametrize("case", [c for c in arm.CASES if c.startswith("create-")])
async def test_real_creation_owner_full_physical_route_and_independent_recovery(
    physical_world, environment, case
):
    w = physical_world
    p = w.packet
    p["partition_type"] = environment
    p["case"] = case
    if case.startswith("create-power-"):
        peer = "55555555-5555-4555-8555-555555555555"
        p["controllers"] = [
            {
                "kind": "LogicalPartition",
                "uuid": peer,
                "name": "peer",
                "partition_id": 1,
            }
        ]
        w.add(peer, "LogicalPartition", "peer")["Description"] = (
            "[hmcpctl owner:test created:2026-10-28]"
        )
    h = await w.client("preflight")
    with h.transports():
        async with h:
            baseline = await arm.snapshot(h, p)
    preflight = {
        "local_binding": arm.local_binding(p, h.config),
        "complete": True,
        "session": {"closed": h.closed, "local_closed": h._http.is_closed},
        "packet_sha256": "digest",
        "candidate": p["candidate"],
        "response_cap": h.config.max_response_bytes,
        "captured_utc": datetime.now(UTC).isoformat(),
        "baseline": baseline,
    }
    preflight_counts = h.budget.used.copy()
    h = await w.client("run")
    window = arm.Window(p, "digest", "digest", True)
    if case.startswith("create-power-"):
        with h.transports():
            async with h:
                with pytest.raises(ValueError, match="reserve"):
                    await arm.execute(
                        h, p, "digest", preflight, w.path, window, Path("packet")
                    )
        assert h.sent == set() and TARGET not in w.entries and h.closed
        # Offline measurement only: virtual limits reveal the complete owner call inventory.
        # The production guard above proves the original grant never attempts this case.
        virtual = await w.client("run")
        virtual.budget.maximum = (128, 16, 16 * 1024**2)
        with virtual.transports():
            async with virtual:
                measured = await arm.execute(
                    virtual,
                    p,
                    "digest",
                    preflight,
                    w.path,
                    arm.Window(p, "digest", "digest", True),
                    Path("packet"),
                )
        assert measured["complete"] and virtual.budget.used[0] > 64
        upper = arm.route_plan(p, baseline, virtual.config.max_response_bytes)
        baseline_reads = 4 + 2 * len(baseline["partitions"]) + baseline["profiles"]
        assert virtual.budget.used[0] <= baseline_reads + 1 + upper[0]
        print(
            "ST68_OFFLINE_CASE",
            environment,
            case,
            "ZERO_EFFECT_HOLD",
            h.budget.used,
            "virtual_complete",
            virtual.budget.used,
        )
        return
    try:
        with h.transports():
            async with h:
                result = await arm.execute(
                    h, p, "digest", preflight, w.path, window, Path("packet")
                )
    finally:
        if not h._http.is_closed:
            await h._http.aclose()
    assert result["complete"] and TARGET not in w.entries
    assert (
        h.budget.used[0] <= 64
        and h.budget.used[1] <= 8
        and h.budget.used[2] <= 8 * 1024**2
    )
    result["session"] = {"closed": h.closed, "local_closed": h._http.is_closed}
    result["transport"] = {"counts": h.budget.used, "calls": h.budget.calls}
    recovery = await w.client("recover")
    with recovery.transports():
        async with recovery:
            assert await arm.recover(recovery, p, result) == baseline
    assert recovery.closed
    print(
        "ST68_OFFLINE_CASE",
        environment,
        case,
        "FIT",
        preflight_counts,
        h.budget.used,
        recovery.budget.used,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["LogicalPartition", "VirtualIOServer"])
@pytest.mark.parametrize("case", ["boot-raw", "boot-devices"])
async def test_real_boot_owner_exact_restore_full_baseline_and_read_only_recovery(
    physical_world, kind, case
):
    w = physical_world
    p = w.packet
    p.update(
        case=case,
        partition_type="Virtual IO Server"
        if kind == "VirtualIOServer"
        else "AIX/Linux",
    )
    r = w.add(kind=kind)
    r["Description"] = "[hmcpctl owner:test created:2026-10-28] [caller original]"
    p["boot_target"] = {
        "kind": kind,
        "uuid": TARGET,
        "name": p["name"],
        "partition_id": 2,
        "description": r["Description"],
    }
    initial = w.pending
    h = await w.client("preflight")
    with h.transports():
        async with h:
            baseline = await arm.snapshot(h, p)
            arm.admit(p, baseline, h)
            assert await arm.pending(h, p) == initial
    preflight = {
        "local_binding": arm.local_binding(p, h.config),
        "complete": True,
        "session": {"closed": h.closed, "local_closed": h._http.is_closed},
        "packet_sha256": "digest",
        "candidate": p["candidate"],
        "response_cap": h.config.max_response_bytes,
        "captured_utc": datetime.now(UTC).isoformat(),
        "baseline": baseline,
    }
    h = await w.client("run")
    with h.transports():
        async with h:
            result = await arm.execute(
                h,
                p,
                "digest",
                preflight,
                w.path,
                arm.Window(p, "digest", "digest", True),
                Path("packet"),
            )
    assert result["complete"] and w.pending == initial
    assert not {"create", "delete", "stamp"} & h.sent
    assert h.sent == {"test", "restore"}
    assert h.budget.used[0] <= 64 and h.budget.used[1] <= 8
    result["session"] = {"closed": h.closed, "local_closed": h._http.is_closed}
    result["transport"] = {"counts": h.budget.used, "calls": h.budget.calls}
    recovery = await w.client("recover")
    with recovery.transports():
        async with recovery:
            await arm.recover(recovery, p, result)
    assert recovery.closed and recovery.sent == set()
    print("ST68_OFFLINE_CASE", kind, case, "FIT", h.budget.used, recovery.budget.used)


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [None, "", "   ", "bad\ntext", "x" * 16385])
async def test_unrestorable_baseline_refuses_before_any_test_write(
    physical_world, original
):
    w = physical_world
    p = w.packet
    p.update(case="boot-raw", partition_type="AIX/Linux")
    r = w.add(kind="LogicalPartition")
    r["Description"] = "[hmcpctl owner:test created:2026-10-28]"
    w.pending = original
    r["BootListInformation"]["PendingBootString"] = original
    p["boot_target"] = {
        "kind": "LogicalPartition",
        "uuid": TARGET,
        "name": p["name"],
        "partition_id": 2,
        "description": r["Description"],
    }
    h = await w.client("preflight")
    with h.transports():
        async with h:
            b = await arm.snapshot(h, p)
    f = {
        "local_binding": arm.local_binding(p, h.config),
        "complete": True,
        "session": {"closed": True, "local_closed": True},
        "packet_sha256": "digest",
        "candidate": p["candidate"],
        "response_cap": h.config.max_response_bytes,
        "captured_utc": datetime.now(UTC).isoformat(),
        "baseline": b,
    }
    h = await w.client("run")
    with h.transports():
        async with h:
            with pytest.raises((TypeError, ValueError)):
                await arm.execute(
                    h,
                    p,
                    "digest",
                    f,
                    w.path,
                    arm.Window(p, "digest", "digest", True),
                    Path("packet"),
                )
    assert h.sent == set() and h.closed
    assert not any(
        method in ("POST", "DELETE") and path != "/rest/api/web/Logon"
        for method, path in w.requests
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["create-auto-0", "boot-raw"])
async def test_default_response_clamp_cannot_pretend_whole_route_bytes_fit(
    physical_world, case
):
    w = physical_world
    p = w.packet
    p["case"] = case
    if case == "boot-raw":
        r = w.add(kind="LogicalPartition")
        r["Description"] = "[hmcpctl owner:test created:2026-10-28]"
        p["boot_target"] = {
            "kind": "LogicalPartition",
            "uuid": TARGET,
            "name": p["name"],
            "partition_id": 2,
            "description": r["Description"],
        }
    w.config = w.config.model_copy(update={"max_response_bytes": 32 * 1024**2})
    h = await w.client("preflight")
    with h.transports():
        async with h:
            b = await arm.snapshot(h, p)
    f = {
        "local_binding": arm.local_binding(p, h.config),
        "complete": True,
        "session": {"closed": True, "local_closed": True},
        "packet_sha256": "digest",
        "candidate": p["candidate"],
        "response_cap": h.config.max_response_bytes,
        "captured_utc": datetime.now(UTC).isoformat(),
        "baseline": b,
    }
    h = await w.client("run")
    assert h.config.max_response_bytes == 262144
    with h.transports():
        async with h:
            with pytest.raises(ValueError, match="byte reserve"):
                await arm.execute(
                    h,
                    p,
                    "digest",
                    f,
                    w.path,
                    arm.Window(p, "digest", "digest", True),
                    Path("packet"),
                )
    assert h.sent == set() and h.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "boot",
        "boot-io",
        "boot-profile",
        "boot-last",
        "boot-current-attr",
        "boot-profile-attr",
    ],
)
async def test_uncertain_or_changed_boot_never_restores_and_recovery_observes_residue(
    physical_world, fault
):
    w = physical_world
    p = w.packet
    p.update(case="boot-raw", partition_type="Virtual IO Server")
    r = w.add()
    r["Description"] = "[hmcpctl owner:test created:2026-10-28]"
    p["boot_target"] = {
        "kind": "VirtualIOServer",
        "uuid": TARGET,
        "name": p["name"],
        "partition_id": 2,
        "description": r["Description"],
    }
    h = await w.client("preflight")
    with h.transports():
        async with h:
            b = await arm.snapshot(h, p)
    f = {
        "local_binding": arm.local_binding(p, h.config),
        "complete": True,
        "session": {"closed": True, "local_closed": True},
        "packet_sha256": "digest",
        "candidate": p["candidate"],
        "response_cap": h.config.max_response_bytes,
        "captured_utc": datetime.now(UTC).isoformat(),
        "baseline": b,
    }
    w.fail = fault
    h = await w.client("run")
    with h.transports():
        async with h:
            with pytest.raises((ValueError, OSError, arm.boot_order.HMCError)):
                await arm.execute(
                    h,
                    p,
                    "digest",
                    f,
                    w.path,
                    arm.Window(p, "digest", "digest", True),
                    Path("packet"),
                )
    assert h.sent == {"test"} and h.closed
    j = arm.serial.read(w.path)
    assert j["test"]["execution"] == ("unknown" if fault == "boot" else "acknowledged")
    j["session"] = {"closed": True, "local_closed": True}
    observed = {}
    recovery = await w.client("recover")
    with recovery.transports():
        async with recovery:
            with pytest.raises(ValueError):
                await arm.recover(recovery, p, j, observed)
    assert (
        "current" in observed
        and observed["pending_boot_string"] != "  original,exact  "
    )
    assert recovery.sent == set() and recovery.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["create", "stamp-io", "stamp-profile"])
async def test_actual_creation_or_stamp_fault_forbids_delete_and_retains_named_fixture(
    physical_world, fault
):
    w = physical_world
    p = w.packet
    p["partition_type"] = "Virtual IO Server"
    h = await w.client("preflight")
    with h.transports():
        async with h:
            b = await arm.snapshot(h, p)
    f = {
        "local_binding": arm.local_binding(p, h.config),
        "complete": True,
        "session": {"closed": True, "local_closed": True},
        "packet_sha256": "digest",
        "candidate": p["candidate"],
        "response_cap": h.config.max_response_bytes,
        "captured_utc": datetime.now(UTC).isoformat(),
        "baseline": b,
    }
    w.fail = fault
    h = await w.client("run")
    with h.transports():
        async with h:
            with pytest.raises((ValueError, OSError, arm.boot_order.HMCError)):
                await arm.execute(
                    h,
                    p,
                    "digest",
                    f,
                    w.path,
                    arm.Window(p, "digest", "digest", True),
                    Path("packet"),
                )
    assert TARGET in w.entries and "delete" not in h.sent and h.closed
    j = arm.serial.read(w.path)
    if fault == "create":
        assert j["create"]["execution"] == "unknown" and j["vios_stamp"] is None
    else:
        assert j["vios_stamp"]["execution"] == "acknowledged"
    j["session"] = {"closed": True, "local_closed": True}
    observed = {}
    recovery = await w.client("recover")
    with recovery.transports():
        async with recovery:
            with pytest.raises(ValueError):
                await arm.recover(recovery, p, j, observed)
    assert TARGET in observed["current"]["partitions"] and recovery.sent == set()


@pytest.mark.asyncio
async def test_failed_logon_never_changes_selected_port_or_attempts_second_endpoint(
    monkeypatch,
):
    import httpx

    original = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "u",
            "password": "test",  # pragma: allowlist secret — synthetic test credential
            "verify_ssl": True,
        }
    )
    assert "port" not in original.model_fields_set
    h = arm.CountedHMC(arm.bounded_config(original), arm.Budget("preflight", packet()))
    await h._http.aclose()
    ports, requests = [], []

    def client(port):
        ports.append(port)

        def fail(request):
            requests.append(str(request.url))
            raise httpx.ConnectError("injected first endpoint failure", request=request)

        return httpx.AsyncClient(
            base_url=f"https://hmc.test:{port}", transport=httpx.MockTransport(fail)
        )

    monkeypatch.setattr(h, "_new_http_client", client)
    h._http = client(original.port)
    with pytest.raises(arm.boot_order.HMCError):
        async with h:
            pytest.fail("logon cannot succeed")
    assert h._legacy_port_fallback is False
    assert ports == [443] and len(requests) == 1 and h.config.port == 443
    assert h._http.is_closed and h.budget.used[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "",
        "?group=Basic",
        "?group=Advanced&x=1",
        "?group=Advanced#fragment",
        "?group=Advanced&group=Advanced",
    ],
)
async def test_pending_post_accepts_no_unregistered_path_or_query(monkeypatch, query):
    import httpx

    p = packet()
    c = HMCConfig.from_mapping(
        p["candidate"]["hmc_identity"]
        | {
            "password": "test",  # pragma: allowlist secret — synthetic test credential
        }
    )
    h = arm.CountedHMC(arm.bounded_config(c), arm.Budget("run", p))
    await h._http.aclose()
    physical = []
    h._http = httpx.AsyncClient(
        base_url="https://hmc.test",
        transport=httpx.MockTransport(
            lambda request: (
                physical.append(request) or httpx.Response(204, request=request)
            )
        ),
    )
    monkeypatch.setattr(arm.serial, "revision", lambda _: None)
    h.window = arm.Window(p, "digest", "digest", True)
    h.effect = "test"
    h.target = {"kind": "LogicalPartition", "uuid": TARGET}
    try:
        with pytest.raises(PermissionError):
            await h._request(
                "POST", "/rest/api/uom/LogicalPartition/" + TARGET + query, content=""
            )
        assert physical == []
    finally:
        await h._http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [OSError("first failure"), __import__("asyncio").CancelledError()]
)
async def test_phase_failure_or_cancellation_keeps_owned_closeout_and_original_error(
    physical_world, failure
):
    w = physical_world
    h = await w.client("preflight")
    document = {"mode": "preflight", "complete": False}
    arm.serial.write(w.path, document, fresh=True)

    async def operation():
        raise failure

    with pytest.raises(type(failure)):
        await arm.phase(h, w.packet, document, w.path, operation)
    stored = arm.serial.read(w.path)
    assert stored["error"] == type(failure).__name__
    assert stored["session"] == {"opened": True, "closed": True, "local_closed": True}
    assert stored["transport"]["counts"][0] == 2 and not h.sent


@pytest.mark.asyncio
async def test_actual_cli_three_phase_protocol_and_filtered_final_evidence(
    physical_world, monkeypatch, tmp_path, capsys
):
    import hashlib

    import httpx
    import live_test_evidence

    w = physical_world
    monkeypatch.setattr(arm, "load_profile", lambda: w.config)
    monkeypatch.setattr(
        arm.CountedHMC,
        "_new_http_client",
        lambda self, port: httpx.AsyncClient(
            base_url=f"https://hmc.test:{port}", transport=httpx.MockTransport(w.handle)
        ),
    )
    packet_path = tmp_path / "packet.json"
    preflight_path = tmp_path / "preflight.json"
    arm.serial.write(packet_path, w.packet, fresh=True)
    digest = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    args = ["--packet", str(packet_path), "--packet-sha256", digest]
    # main owns asyncio.run: exercise it in a thread, with the real providers still fake.
    import asyncio

    assert (
        await asyncio.to_thread(
            arm.main, ["preflight", *args, "--output", str(preflight_path)]
        )
        == 0
    )
    assert (
        await asyncio.to_thread(
            arm.main,
            [
                "run",
                *args,
                "--preflight",
                str(preflight_path),
                "--results-file",
                str(w.path),
                "--grant-packet-sha256",
                digest,
                "--exclusive-writer-window",
            ],
        )
        == 0
    )
    run_bytes = w.path.read_bytes()
    assert await asyncio.to_thread(arm.main, ["recover", "--results", str(w.path)]) == 0
    assert w.path.read_bytes() == run_bytes
    recovered = arm.serial.read(w.path.with_name(w.path.name + ".recovery.json"))
    assert recovered["clean"] and recovered["history_verified"]
    public = live_test_evidence.render(recovered)
    assert public is not None and "CLEAN" in public and "hmc.test" not in public
    assert w.pending not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["AIX/Linux", "Virtual IO Server"])
@pytest.mark.parametrize("case", ["create-power-ids", "create-power-names"])
async def test_two_controller_case_refuses_entire_unfittable_route_before_create(
    physical_world, environment, case
):
    w = physical_world
    p = w.packet
    p.update(case=case, partition_type=environment)
    p["controllers"] = [
        {
            "kind": "LogicalPartition",
            "uuid": str(arm.uuid.uuid5(arm.uuid.UUID(SYSTEM), str(i))),
            "name": "peer-" + str(i),
            "partition_id": i,
        }
        for i in (1, 3)
    ]
    for c in p["controllers"]:
        r = w.add(c["uuid"], c["kind"], c["name"])
        r["PartitionID"] = str(c["partition_id"])
        r["Description"] = "[hmcpctl owner:test created:2026-10-28]"
    h = await w.client("preflight")
    with h.transports():
        async with h:
            b = await arm.snapshot(h, p)
    f = {
        "local_binding": arm.local_binding(p, h.config),
        "complete": True,
        "session": {"closed": True, "local_closed": True},
        "packet_sha256": "digest",
        "candidate": p["candidate"],
        "response_cap": h.config.max_response_bytes,
        "captured_utc": datetime.now(UTC).isoformat(),
        "baseline": b,
    }
    h = await w.client("run")
    with h.transports():
        async with h:
            with pytest.raises(ValueError, match="REST count reserve"):
                await arm.execute(
                    h,
                    p,
                    "digest",
                    f,
                    w.path,
                    arm.Window(p, "digest", "digest", True),
                    Path("packet"),
                )
    assert h.sent == set() and TARGET not in w.entries and h.closed


@pytest.mark.asyncio
async def test_maximum_original_inventory_has_finite_physical_snapshot_and_zero_effect_admission(
    physical_world,
):
    w = physical_world
    p = w.packet
    for i in range(4):
        uid = str(arm.uuid.uuid5(arm.uuid.UUID(SYSTEM), "foreign-" + str(i)))
        kind = "LogicalPartition" if i < 2 else "VirtualIOServer"
        r = w.add(uid, kind, "ordinary-" + str(i))
        r["PartitionID"] = str(10 + i)
        extra = copy.deepcopy(w.profiles[uid][1])
        pid = str(arm.uuid.uuid5(arm.uuid.UUID(uid), "second-profile"))
        extra.update(
            ProfileName="spare",
            Metadata={"Atom": {"AtomID": pid, "AtomCreated": "before"}},
        )
        w.extra_profiles[uid] = [(pid, extra)]
    h = await w.client("preflight")
    with h.transports():
        async with h:
            b = await arm.snapshot(h, p)
    assert len(b["partitions"]) == 4 and b["profiles"] == 8
    assert h.budget.used[:2] == [
        22,
        1,
    ]  # 20 snapshot REST + own logon/logoff; one native authority read.
    f = {
        "local_binding": arm.local_binding(p, h.config),
        "complete": True,
        "session": {"closed": True, "local_closed": True},
        "packet_sha256": "digest",
        "candidate": p["candidate"],
        "response_cap": h.config.max_response_bytes,
        "captured_utc": datetime.now(UTC).isoformat(),
        "baseline": b,
    }
    h = await w.client("run")
    with h.transports():
        async with h:
            with pytest.raises(ValueError, match="REST count reserve"):
                await arm.execute(
                    h,
                    p,
                    "digest",
                    f,
                    w.path,
                    arm.Window(p, "digest", "digest", True),
                    Path("packet"),
                )
    assert h.sent == set() and h.closed


@pytest.mark.parametrize(
    "maximum", [arm.serial.LIMIT, 4 * arm.serial.LIMIT, 8 * arm.serial.LIMIT]
)
def test_fixed_caps_roundtrip_private_mode_and_default_unchanged(tmp_path, maximum):
    tmp_path.chmod(0o700)
    p = tmp_path / "evidence.json"
    doc = {"complete": False, "text": "x" * (maximum // 2)}
    arm.serial.write(p, doc, fresh=True, maximum=maximum)
    assert p.stat().st_mode & 0o777 == 0o600
    assert arm.serial.read(p, maximum=maximum) == doc
    if p.stat().st_size > arm.serial.LIMIT:
        with pytest.raises(ValueError):
            arm.serial.read(p)


@pytest.mark.parametrize(
    "maximum",
    [True, 0, 2 * arm.serial.LIMIT, 32 * arm.serial.LIMIT, float(4 * arm.serial.LIMIT)],
)
def test_arbitrary_or_noninteger_ceiling_refused(tmp_path, maximum):
    tmp_path.chmod(0o700)
    with pytest.raises(ValueError):
        arm.serial.private_path(tmp_path / "new", fresh=True, maximum=maximum)
    with pytest.raises(ValueError):
        arm.serial.write(tmp_path / "new", {}, fresh=True, maximum=maximum)
    assert not (tmp_path / "new").exists()


def test_oversized_later_proof_preserves_exact_incomplete_intent(tmp_path):
    tmp_path.chmod(0o700)
    p = tmp_path / "intent.json"
    arm.serial.write(
        p,
        {"complete": False, "phase": "intent"},
        fresh=True,
        maximum=8 * arm.serial.LIMIT,
    )
    before = p.read_bytes()
    with pytest.raises(ValueError):
        arm.serial.write(
            p,
            {"complete": True, "proof": "x" * (8 * arm.serial.LIMIT)},
            maximum=8 * arm.serial.LIMIT,
        )
    assert (
        p.read_bytes() == before
        and arm.serial.read(p, maximum=8 * arm.serial.LIMIT)["complete"] is False
    )
    with pytest.raises(ValueError):
        arm.serial.write(p, {}, fresh=True, maximum=8 * arm.serial.LIMIT)
    assert p.read_bytes() == before


def test_read_is_physically_bounded_before_json_even_if_file_grows(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    tmp_path.chmod(0o700)
    p = tmp_path / "input.json"
    arm.serial.write(p, {"complete": False}, fresh=True)
    original_path, original_open = arm.serial.private_path, Path.open

    def grow(path, **kw):
        result = original_path(path, **kw)
        with original_open(result, "ab") as stream:
            stream.write(b" " * (4 * arm.serial.LIMIT + 1))
        return result

    reads = []

    @contextmanager
    def bounded_open(path, *args, **kw):
        with original_open(path, *args, **kw) as stream:

            class Reader:
                def read(self, n):
                    reads.append(n)
                    return stream.read(n)

            yield Reader()

    monkeypatch.setattr(arm.serial, "private_path", grow)
    monkeypatch.setattr(Path, "open", bounded_open)
    monkeypatch.setattr(
        arm.serial.json, "loads", lambda *_: pytest.fail("JSON must not see overflow")
    )
    with pytest.raises(ValueError, match="bound"):
        arm.serial.read(p, maximum=4 * arm.serial.LIMIT)
    assert reads == [4 * arm.serial.LIMIT + 1]


def test_atomic_replace_failure_keeps_prior_intent_and_reaps_own_temp(
    tmp_path, monkeypatch
):
    tmp_path.chmod(0o700)
    p = tmp_path / "intent.json"
    arm.serial.write(p, {"complete": False}, fresh=True)
    before = p.read_bytes()

    def fail(*_):
        raise OSError("injected replace failure")

    monkeypatch.setattr(arm.serial.os, "replace", fail)
    with pytest.raises(OSError):
        arm.serial.write(p, {"complete": True}, maximum=4 * arm.serial.LIMIT)
    assert p.read_bytes() == before and list(tmp_path.glob(".st63-*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "chsyscfg -r lpar -m system -o apply",
        "chsyscfg -r lpar -m system -i lpar_id=3,boot_string=text",
        "chsyscfg -r lpar -m system -i lpar_id=2,boot_string=text,description=other",
    ],
)
async def test_native_effect_route_cannot_apply_or_retarget_or_add_fields(
    physical_world, command
):
    w = physical_world
    p = w.packet
    h = await w.client("run")
    h.window = arm.Window(p, "digest", "digest", True)
    h.effect = "test"
    h.target = {"kind": "VirtualIOServer", "uuid": TARGET, "partition_id": 2}
    try:
        with pytest.raises(PermissionError):
            await h.ssh(h.config, command)
        assert not h.sent and w.ssh == []
    finally:
        await h._http.aclose()
