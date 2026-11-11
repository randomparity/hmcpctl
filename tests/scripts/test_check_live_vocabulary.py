"""Tests for the live-vocabulary guard (scripts/check_live_vocabulary.py, issue #1202)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[2] / "scripts" / "check_live_vocabulary.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "check_live_vocabulary", MODULE_PATH
)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
guard = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules["check_live_vocabulary"] = guard
MODULE_SPEC.loader.exec_module(guard)

VOCABULARY = {
    "firmware": "vX",
    "source": "test",
    "rest": {
        "endpoints": [
            {
                "method": "GET",
                "path": "/rest/api/uom/ManagedSystem/{uuid}",
                "status": 200,
            },
            {
                "method": "GET",
                "path": "/rest/api/uom/LogicalPartition/search/(PartitionName=={value})",
                "status": 200,
            },
            {
                "method": "GET",
                "path": "/rest/api/pcm/ManagedSystem/{uuid}/ProcessedMetrics?StartTS={value}",
                "status": 403,
            },
            {"method": "PUT", "path": "/rest/api/uom/Written", "status": 200},
        ],
        "errors": [],
        "values": {"PartitionState": ["<text>", "open firmware"], "Other": ["x"]},
        "element_enums": {"PartitionState": "LogicalPartitionState.Enum"},
    },
    "cli": {
        "commands": [
            {"command": "lssyscfg -r lpar -m {} -F name"},
            {"command": "lshwres -r sriov --rsubtype physport -m {} --level eth"},
        ],
        "values": {},
    },
}
ENUMS = {
    "firmware": "vX",
    "types": {"LogicalPartitionState.Enum": ["running", "error"]},
}


def _evidence() -> guard.Evidence:
    return guard.Evidence(
        allowed={"PartitionState": {"running", "error", "open firmware"}},
        bindings={"PartitionState": "LogicalPartitionState.Enum"},
        endpoints=[
            e["path"] for e in VOCABULARY["rest"]["endpoints"] if e["method"] == "GET"
        ],
        commands=[c["command"] for c in VOCABULARY["cli"]["commands"]],
    )


def _values(file: str, text: str) -> list[tuple[str, str]]:
    return [
        (v.subject, v.value) for v in guard.literal_violations(file, text, _evidence())
    ]


# --- evidence ------------------------------------------------------------------


def test_load_evidence_unions_enum_and_observed_literals(tmp_path: Path) -> None:
    directory = tmp_path / guard.VOCABULARY_DIR
    directory.mkdir(parents=True)
    (directory / "vx-p9.json").write_text(json.dumps(VOCABULARY))
    (directory / "enums-vx.json").write_text(json.dumps(ENUMS))
    evidence = guard.load_evidence(tmp_path)
    assert evidence.allowed == {"PartitionState": {"running", "error", "open firmware"}}
    assert "/rest/api/uom/Written" not in evidence.endpoints
    assert "Other" not in evidence.allowed  # unbound elements are not checked


def test_load_evidence_unions_firmware_releases(tmp_path: Path) -> None:
    """A value any release answered or enumerates is valid; no release overrides another."""
    directory = tmp_path / guard.VOCABULARY_DIR
    directory.mkdir(parents=True)
    (directory / "va-p9.json").write_text(json.dumps(VOCABULARY))
    newer = json.loads(json.dumps(VOCABULARY))
    newer["rest"]["values"] = {"PartitionState": ["migrating"]}
    (directory / "vb-p10.json").write_text(json.dumps(newer))
    (directory / "enums-va.json").write_text(json.dumps(ENUMS))
    later = {"types": {"LogicalPartitionState.Enum": ["suspended"]}}
    (directory / "enums-vb.json").write_text(json.dumps(later))
    allowed = guard.load_evidence(tmp_path).allowed["PartitionState"]
    assert allowed == {"running", "error", "suspended", "open firmware", "migrating"}


def test_an_enum_list_can_bind_an_element(tmp_path: Path) -> None:
    directory = tmp_path / guard.VOCABULARY_DIR
    directory.mkdir(parents=True)
    (directory / "vx-p9.json").write_text(json.dumps(VOCABULARY))
    jobs = {
        "types": {"JobStatus.Enum": ["COMPLETED_OK"]},
        "elements": {"Status": "JobStatus.Enum"},
    }
    (directory / "enums-documented-jobs.json").write_text(json.dumps(jobs))
    evidence = guard.load_evidence(tmp_path)
    assert evidence.allowed["Status"] == {"COMPLETED_OK"}
    assert evidence.bindings["Status"] == "JobStatus.Enum"


def test_a_stated_binding_overrides_an_inferred_one(tmp_path: Path) -> None:
    """A vocabulary that inferred `Status` as a VNIC status does not loosen job statuses (#1498).

    `Status` is shared by jobs and VNIC backing devices; the exporter binds it by local
    name, so a capture whose only statuses are LINK_DOWN and OPERATIONAL infers the VNIC
    enum. The documented job-status list states the element's binding and wins.
    """
    directory = tmp_path / guard.VOCABULARY_DIR
    directory.mkdir(parents=True)
    inferred = json.loads(json.dumps(VOCABULARY))
    inferred["rest"]["values"] = {"Status": ["LINK_DOWN", "OPERATIONAL"]}
    inferred["rest"]["element_enums"] = {"Status": "BackingDeviceStatus.Enum"}
    (directory / "vx-p10.json").write_text(json.dumps(inferred))
    vnic = {
        "types": {"BackingDeviceStatus.Enum": ["LINK_DOWN", "OPERATIONAL", "UNKNOWN"]}
    }
    (directory / "enums-vx-p10.json").write_text(json.dumps(vnic))
    jobs = {
        "types": {"JobStatus.Enum": ["COMPLETED_OK"]},
        "elements": {"Status": "JobStatus.Enum"},
    }
    (directory / "enums-documented-jobs.json").write_text(json.dumps(jobs))
    evidence = guard.load_evidence(tmp_path)
    assert evidence.allowed["Status"] == {"COMPLETED_OK", "LINK_DOWN", "OPERATIONAL"}
    assert evidence.bindings["Status"] == "JobStatus.Enum"


def test_documented_job_statuses_mark_what_was_captured() -> None:
    """Each documented status says whether a committed vocabulary captured it."""
    directory = Path(__file__).parents[2] / guard.VOCABULARY_DIR
    jobs = json.loads((directory / "enums-documented-jobs.json").read_text())
    names = jobs["types"]["JobStatus.Enum"]
    assert sorted(jobs["evidence"]) == sorted(names)
    observed: set[str] = set()
    for path in directory.glob("v*.json"):
        observed |= set(
            json.loads(path.read_text())["rest"]["values"].get("Status", [])
        )
    for name, mark in jobs["evidence"].items():
        assert mark == ("captured" if name in observed else "documented-only"), name
    assert all(source.startswith("docs/refs/") for source in jobs["sources"])


#: #1516's re-derived-vocabularies table: each element and its enum. A stated binding outside it
#: needs a source of its own, cited in the stated list (#1529).
STATED_BY_1516 = {
    "AIXCapable": "AIXCapability.Enum",
    "ConfiguredOptions": "SRIOVPhysicalPortOptions.Enum",
    "CurrentManufacturingDefaulConfigurationtBootMode": "PartitionBootMode.Enum",
    "CurrentManufacturingDefaultConfigurationSource": "IPLSource.Enum",
    "CurrentOptions": "SRIOVPhysicalPortOptions.Enum",
    "CurrentPowerOnSide": "ServiceProcessorSide.Enum",
    "CurrentPowerSavingMode": "EnergyManagementModeType.Enum",
    "CurrentSharingMode": "LogicalPartitionProcessorSharingMode.Enum",
    "PartitionType": "LogicalPartitionEnvironment.Enum",
    "PendingManufacturingDefaulConfigurationtBootMode": "PartitionBootMode.Enum",
    "PendingManufacturingDefaultConfigurationSource": "IPLSource.Enum",
    "PendingPowerOnLogicalPartitionStartPolicy": "PowerOnStartPolicy.Enum",
    "PendingPowerOnSide": "ServiceProcessorSide.Enum",
    "PortCapabilities": "SRIOVPortCapability.Enum",
    "PowerOnLogicalPartitionStartPolicy": "PowerOnStartPolicy.Enum",
    "RequiredPowerSavingMode": "EnergyManagementModeType.Enum",
    "SupportedOptions": "SRIOVPhysicalPortOptions.Enum",
    "SupportedPowerSavingModeTypes": "EnergyManagementModeType.Enum",
}


def test_stated_bindings_hold_every_captured_value() -> None:
    """Each stated binding is the only one for its element, and admits every capture (#1529).

    A stated binding replaces every inferred one for its local name in every vocabulary,
    so one bound to the wrong object's enum would reject values a firmware answered.
    """
    directory = Path(__file__).parents[2] / guard.VOCABULARY_DIR
    stated = json.loads((directory / "enums-stated-bindings.json").read_text())
    assert stated["elements"] == STATED_BY_1516
    assert set(stated["evidence"]) == set(STATED_BY_1516)
    types: dict[str, set[str]] = {}
    for path in directory.glob("enums-*.json"):
        if path.name == "enums-stated-bindings.json":
            continue
        enum_list = json.loads(path.read_text())
        for name, values in enum_list["types"].items():
            types.setdefault(name, set()).update(values)
        assert not STATED_BY_1516.keys() & enum_list.get("elements", {}), path.name
    for path in directory.glob("v*.json"):
        rest = json.loads(path.read_text())["rest"]
        for element, enum in stated["elements"].items():
            inferred = rest["element_enums"].get(element, enum)
            assert inferred == enum, (path.name, element)
            seen = {v for v in rest["values"].get(element, []) if v[:1] != "<"}
            assert seen <= types[enum], (path.name, element, seen - types[enum])


def test_partition_type_is_checked_by_its_stated_binding() -> None:
    """No vocabulary infers `PartitionType`'s enum; the stated list binds it (#1529)."""
    evidence = guard.load_evidence(Path(__file__).parents[2])
    text = (
        'A = "<PartitionType>AIX/Linux</PartitionType>"\n'
        'B = "<PartitionType>NotAnEnvironment</PartitionType>"\n'
    )
    found = guard.literal_violations("t.py", text, evidence)
    assert [(v.subject, v.value) for v in found] == [
        ("PartitionType", "NotAnEnvironment")
    ]
    assert evidence.bindings["PartitionType"] == "LogicalPartitionEnvironment.Enum"


def test_load_evidence_refuses_an_empty_directory(tmp_path: Path) -> None:
    (tmp_path / guard.VOCABULARY_DIR).mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        guard.load_evidence(tmp_path)


# --- literals ----------------------------------------------------------------------


def test_xml_leaf_in_a_python_string_is_checked() -> None:
    text = 'BODY = "<PartitionState kb=\\"ROO\\">Running</PartitionState>"\n'
    assert _values("t.py", text) == [("PartitionState", "Running")]


def test_captured_and_enum_values_pass() -> None:
    text = (
        'A = "<PartitionState>open firmware</PartitionState>"\n'
        'B = "<PartitionState>error</PartitionState>"\n'
    )
    assert _values("t.py", text) == []


def test_fstring_placeholder_values_are_skipped() -> None:
    assert _values("t.py", 'A = f"<PartitionState>{state}</PartitionState>"\n') == []


def test_xml_file_leaves_are_checked_with_lines() -> None:
    found = guard.literal_violations(
        "t.xml", "<a>\n<PartitionState>Started</PartitionState>\n</a>", _evidence()
    )
    assert [(v.value, v.line) for v in found] == [("Started", 2)]


@pytest.mark.parametrize(
    "expression",
    [
        'lpar.get("PartitionState") == "Running"',
        'lpar["PartitionState"] != "Running"',
        'lpar.get("PartitionState") in ("running", "Running")',
        '"Running" == lpar.get("PartitionState")',
        'str(lpar.get("PartitionState", "")).strip() == "Running"',
    ],
)
def test_comparisons_keyed_by_an_element_are_checked(expression: str) -> None:
    assert _values("t.py", f"ok = {expression}\n") == [("PartitionState", "Running")]


def test_lowered_comparison_uses_lowered_values() -> None:
    assert (
        _values("t.py", 'ok = lpar.get("PartitionState").lower() == "running"\n') == []
    )
    assert (
        _values("t.py", 'ok = lpar.get("PartitionState").upper() == "RUNNING"\n') == []
    )
    found = _values("t.py", 'ok = lpar.get("PartitionState").lower() == "Running"\n')
    assert found == [("PartitionState", "Running")]


def test_dict_entries_keyed_by_an_element_are_checked() -> None:
    assert _values("t.py", 'd = {"PartitionState": "Not Activated"}\n') == [
        ("PartitionState", "Not Activated")
    ]


def test_dict_value_holding_xml_is_not_a_value() -> None:
    text = 'd = {"PartitionState": "<PartitionState>running</PartitionState>"}\n'
    assert _values("t.py", text) == []


def test_unbound_elements_are_not_checked() -> None:
    assert _values("t.py", 'd = {"Status": "COMPLETED"}\n') == []


# --- REST paths --------------------------------------------------------------------


def _paths(text: str) -> list[str]:
    return [p for p, _ in guard.rest_templates("src/hmcpctl/x.py", text)]


def test_read_paths_come_from_functions_that_get() -> None:
    text = '''
async def read(self, uuid):
    """Docstring /rest/api/uom/Docs is ignored."""
    path = f"/rest/api/uom/ManagedSystem/{uuid}"
    return await self._get(path, "ManagedSystem")

async def write(self, uuid, body):
    await self._put(f"/rest/api/uom/Written/{uuid}", body)

async def power(self, uuid):
    return await self._get(f"/rest/api/uom/LogicalPartition/{uuid}/do/PowerOn")
'''
    assert _paths(text) == ["/rest/api/uom/ManagedSystem/{}"]


def test_returned_path_builders_count_as_reads() -> None:
    text = """
def job_path(job_id):
    path = f"/rest/api/uom/jobs/{job_id}"
    return path
"""
    assert _paths(text) == ["/rest/api/uom/jobs/{}"]


def test_request_get_counts_as_a_read() -> None:
    text = (
        'async def f(self):\n    return await self._request("GET", "/rest/api/uom/X")\n'
    )
    assert _paths(text) == ["/rest/api/uom/X"]


def test_helper_calls_with_literal_types_become_paths() -> None:
    text = """
async def f(self, uuid, name):
    await self.list_uom("Cluster")
    await self.get_uom("SharedStoragePool", uuid)
    await self.search_uom("VirtualIOServer", "PartitionName", name)
    await self.get_quick_property("LogicalPartition", uuid, "PartitionState")
    await self.list_child(parent_type="ManagedSystem", parent_uuid=uuid, child_type="X")
    await self.get_uom(kind, uuid)
"""
    assert sorted(_paths(text)) == [
        "/rest/api/uom/Cluster",
        "/rest/api/uom/LogicalPartition/{}/quick/PartitionState",
        "/rest/api/uom/ManagedSystem/{}/X",
        "/rest/api/uom/SharedStoragePool/{}",
        "/rest/api/uom/VirtualIOServer/search/(PartitionName=={})",
    ]


@pytest.mark.parametrize(
    ("template", "captured"),
    [
        ("/rest/api/uom/ManagedSystem/{}", True),
        ("/rest/api/uom/ManagedSystem/{}?group=Advanced", False),
        ("/rest/api/uom/LogicalPartition/search/(PartitionName=={})", True),
        ("/rest/api/uom/LogicalPartition/search/(PartitionState=={})", False),
        ("/rest/api/pcm/{}/{}?{}", True),
        ("/rest/api/uom/Written", False),
        ("/rest/api/uom/", True),
    ],
)
def test_path_is_captured(template: str, captured: bool) -> None:
    assert guard.path_is_captured(template, _evidence().endpoints) is captured


# --- CLI commands --------------------------------------------------------------------


def test_cli_templates_skip_messages() -> None:
    text = """
cmd = f"lssyscfg -r lpar -m {shlex.quote(s)} -F name"
msg = f"lsmemopt row {i} is missing"
"""
    assert [c for c, _ in guard.cli_templates("x.py", text)] == [
        "lssyscfg -r lpar -m {} -F name"
    ]


@pytest.mark.parametrize(
    ("template", "captured"),
    [
        ("lssyscfg -r lpar -m {} -F lpar_env", True),
        ("lssyscfg -r lpar -F uuid,name", False),  # no -m: a different, failing form
        ("lssyscfg -r prof -m {}", False),
        ("lshwres -r sriov --rsubtype physport -m {} --level {}", True),
        ("lshwres -r sriov --rsubtype physport -m {} --level roce", False),
        ("lshwres -r sriov --rsubtype physport -m {}", False),
        ("lsfoo -x", False),
    ],
)
def test_command_is_captured(template: str, captured: bool) -> None:
    assert guard.command_is_captured(template, _evidence().commands) is captured


# --- allowlist and driver ---------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / guard.VOCABULARY_DIR
    directory.mkdir(parents=True)
    (directory / "vx-p9.json").write_text(json.dumps(VOCABULARY))
    (directory / "enums-vx.json").write_text(json.dumps(ENUMS))
    (tmp_path / "src" / "hmcpctl").mkdir(parents=True)
    (tmp_path / "src" / "hmcpctl" / "m.py").write_text(
        "async def f(self, u):\n"
        '    return await self._get(f"/rest/api/uom/Missing/{u}")\n'
    )
    (tmp_path / "tests" / "t.py").write_text('d = {"PartitionState": "Running"}\n')
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return tmp_path


def test_violations_fail_and_are_named(repo: Path, capsys) -> None:
    assert guard.main(["--root", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "tests/t.py:1: PartitionState='Running'" in err
    assert (
        "src/hmcpctl/m.py:2: GET /rest/api/uom/Missing/{} has no captured endpoint"
        in err
    )


def test_generated_allowlist_passes_and_keeps_reasons(repo: Path, capsys) -> None:
    assert guard.main(["--root", str(repo), "--write-allowlist"]) == 0
    allowlist = repo / guard.ALLOWLIST
    entries = json.loads(allowlist.read_text())
    assert all("#1202" in e["reason"] for e in entries)
    entries[0]["reason"] = "hand-written reason #1202"
    allowlist.write_text(json.dumps(entries))
    assert guard.main(["--root", str(repo)]) == 0
    assert guard.main(["--root", str(repo), "--write-allowlist"]) == 0
    assert json.loads(allowlist.read_text())[0]["reason"] == "hand-written reason #1202"


def test_stale_entry_fails(repo: Path, capsys) -> None:
    guard.main(["--root", str(repo), "--write-allowlist"])
    (repo / "tests" / "t.py").write_text('d = {"PartitionState": "running"}\n')
    assert guard.main(["--root", str(repo)]) == 1
    assert "stale entry literal tests/t.py 'PartitionState'" in capsys.readouterr().err


def test_entry_without_issue_reason_fails(repo: Path, capsys) -> None:
    guard.main(["--root", str(repo), "--write-allowlist"])
    allowlist = repo / guard.ALLOWLIST
    entries = json.loads(allowlist.read_text())
    entries[0]["reason"] = "later"
    allowlist.write_text(json.dumps(entries))
    assert guard.main(["--root", str(repo)]) == 1
    assert "has no reason citing #1202" in capsys.readouterr().err


def test_live_fixtures_are_not_scanned(repo: Path) -> None:
    live = repo / "tests" / "fixtures" / "live" / "rest-x.json"
    live.write_text(json.dumps({"body": "<PartitionState>Weird</PartitionState>"}))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    files = guard.tracked_files(repo)
    found, _ = guard.collect_violations(repo, files, guard.load_evidence(repo))
    assert all(not v.file.startswith("tests/fixtures/live/") for v in found)


def test_passes_on_the_committed_tree(capsys) -> None:
    """The committed vocabularies and allowlist agree with the tree; exit 0."""
    assert guard.main([]) == 0


# --- inline exemptions ------------------------------------------------------------


def _exempt(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    violations = guard.literal_violations("t.py", text, _evidence())
    kept, errors = guard.apply_exemptions("t.py", text, violations)
    return [(v.subject, v.value) for v in kept], errors


def test_exemption_with_a_reason_suppresses_its_line() -> None:
    text = (
        'd = {"PartitionState": "Sideways"}  # live-vocabulary: allow unknown branch\n'
    )
    assert _exempt(text) == ([], [])


def test_exemption_covers_only_its_own_line() -> None:
    text = (
        'a = {"PartitionState": "Sideways"}  # live-vocabulary: allow unknown branch\n'
        'b = {"PartitionState": "Running"}\n'
    )
    assert _exempt(text) == ([("PartitionState", "Running")], [])


def test_exemption_without_a_reason_fails() -> None:
    kept, errors = _exempt(
        'd = {"PartitionState": "Sideways"}  # live-vocabulary: allow\n'
    )
    assert kept == []
    assert errors == ["t.py:1: live-vocabulary exemption has no reason"]


def test_stale_exemption_fails() -> None:
    kept, errors = _exempt(
        'd = {"PartitionState": "running"}  # live-vocabulary: allow x\n'
    )
    assert kept == []
    assert errors == [
        "t.py:1: stale live-vocabulary exemption suppresses nothing; delete it"
    ]


def test_exemption_errors_fail_the_guard(repo: Path, capsys) -> None:
    (repo / "tests" / "t.py").write_text(
        'd = {"PartitionState": "running"}  # live-vocabulary: allow x\n'
    )
    guard.main(["--root", str(repo), "--write-allowlist"])
    assert guard.main(["--root", str(repo)]) == 1
    assert "stale live-vocabulary exemption" in capsys.readouterr().err


@pytest.mark.parametrize(
    "statement",
    [
        'fake.PartitionState = "Running"',
        'PartitionState = "Running"',
        '_entry("vios-z", PartitionState="Running")',
    ],
)
def test_assignments_naming_an_element_are_checked(statement: str) -> None:
    assert _values("t.py", statement + "\n") == [("PartitionState", "Running")]


def test_job_status_assignments_are_checked_as_status() -> None:
    """Regression: `fake_hmc.wait_job_status = "running"` passed the gate."""
    evidence = _evidence()
    evidence.allowed["Status"] = {"RUNNING", "COMPLETED_OK"}
    found = guard.literal_violations(
        "t.py",
        'fake_hmc.wait_job_status = "running"\nok = f(job_status="RUNNING")\n',
        evidence,
    )
    assert [(v.subject, v.value, v.line) for v in found] == [("Status", "running", 1)]


def test_other_snake_case_parameters_are_not_mapped() -> None:
    """hmcpctl's own parameters take CLI forms (`sharing_mode="keep_idle_procs"`)."""
    assert _values("t.py", 'f(partition_state="Running", status="Degraded")\n') == []


@pytest.mark.parametrize("redirected_git_checkout", [False, True], indirect=True)
def test_repository_query_uses_physical_checkout(redirected_git_checkout):
    root = redirected_git_checkout
    assert guard.tracked_files(root) == ["src/physical.py"]


@pytest.mark.parametrize(
    "name", ["ordinary.py", "space name.py", 'quote"name.py', "tab\tname.py", "λ.py"]
)
def test_tracked_path_spelling_reaches_the_vocabulary_diagnostic(repo, capsys, name):
    original = repo / "tests" / "t.py"
    original.rename(repo / "tests" / name)
    (repo / "src" / "hmcpctl" / "m.py").write_text("answer = 1\n")
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.quotePath", "true"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)

    assert guard.main(["--root", str(repo)]) == 1

    assert f"tests/{name}:1: PartitionState='Running'" in capsys.readouterr().err
