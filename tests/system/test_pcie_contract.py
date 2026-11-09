from __future__ import annotations

import ast
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_io import ProfileIoSlot
from hmcpctl.operations.virtualization.pcie import _is_exact_admitted_environment
from hmcpctl.ssh.commands import (
    HMC_NO_RESULTS,
    parse_hmc_delimited_rows,
    parse_hmc_result_rows,
)
from hmcpctl.ssh.profiles import parse_profile_io_slots
from hmcpctl.ssh.sriov import list_sriov_physical_port_rows
from hmcpctl.ssh.transport import HMCCLIError

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "pcie"
EXPECTED_FIXTURES = {
    "power8-profile-contract.json",
    "power8-profile.json",
    "power9-io-slot.json",
    "power9-sriov-adapter.json",
    "power9-sriov-logport.json",
    "power9-sriov-physport.json",
    "power10-sriov-contract.json",
    "power11-sriov-contract.json",
    "power9-v10r3m1060-live-ioslots.json",
    "power9-v10r3m1060-live-sriov.json",
    "power9-v10r3m1060-live-vnic.json",
}
P9_URL = (
    "https://www.ibm.com/docs/en/power9/0000-REF?topic=POWER9_REF%2Fp9edm%2Flshwres.htm"
)
EXPECTED_PROVENANCE = {
    "power8-profile.json": (
        "https://www.ibm.com/docs/en/power8/8284-22A?topic=commands-lssyscfg",
        "lssyscfg > -r prof > -F > sriov_eth_logical_ports",
    ),
    "power8-profile-contract.json": (
        "https://www.ibm.com/docs/en/power8/8284-22A?topic=commands-chsyscfg",
        "chsyscfg > -r prof > io_slots,sriov_eth_logical_ports",
    ),
    "power9-io-slot.json": (
        P9_URL,
        "lshwres > -r io > --rsubtype slot > -F > drc_index,description,lpar_name",
    ),
    "power9-sriov-adapter.json": (
        P9_URL,
        "lshwres > -r sriov > --rsubtype adapter > adapter_ids",
    ),
    "power9-sriov-physport.json": (
        P9_URL,
        "lshwres > -r sriov > --rsubtype physport > --level eth > adapter_ids,phys_port_ids",
    ),
    "power9-sriov-logport.json": (
        P9_URL,
        (
            "lshwres > -r sriov > --rsubtype logport > --level eth > "
            "adapter_ids,logical_port_ids,phys_port_ids"
        ),
    ),
    "power10-sriov-contract.json": (
        "https://www.ibm.com/docs/en/power10/7063-CR1?topic=commands-chhwres",
        (
            "chhwres > -r io > -o a/r > -l; chhwres > -r sriov > "
            "slot_id,adapter_id,logical_port_id,capacity,max_capacity,"
            "min_eth_capacity_granularity"
        ),
    ),
    "power11-sriov-contract.json": (
        "https://www.ibm.com/docs/en/power11/9824-42A?topic=commands-chhwres",
        (
            "chhwres > -r io > -o a/r > -l; chhwres > -r sriov > "
            "slot_id,adapter_id,logical_port_id,capacity,max_capacity,"
            "min_eth_capacity_granularity"
        ),
    ),
}


@pytest.mark.parametrize(
    ("text", "fields", "delimiter"),
    [
        ("a,b\n1,2\n", (), ","),
        ("a,b\n1,2\n", ("a", "a"), ","),
        ("a,b\n1,2\n", ("a", " b"), ","),
        ("a,b\n1,2\n", ("a", "b"), ""),
        ("a,b\n1,2\n", ("a", "b"), "\n"),
    ],
)
def test_parser_rejects_invalid_contract(
    text: str, fields: tuple[str, ...], delimiter: str
) -> None:
    with pytest.raises(ValueError):
        parse_hmc_delimited_rows(text, fields, delimiter)


@pytest.mark.parametrize(
    "text",
    ["", " \n\t", "a,c\n1,2", "a,b\n1", 'a,b\n1,"bad', 'a,b\n1,"x\ny"'],
)
def test_parser_rejects_malformed_output(text: str) -> None:
    with pytest.raises(ValueError):
        parse_hmc_delimited_rows(text, ("a", "b"))


@pytest.mark.parametrize("text", [HMC_NO_RESULTS, f"  {HMC_NO_RESULTS}\n"])
def test_result_rows_treat_the_hmc_sentinel_as_no_rows(text: str) -> None:
    assert parse_hmc_result_rows(text, ("a", "b"), "test read") == []


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_result_rows_admit_blank_output_only_when_asked(text: str) -> None:
    assert (
        parse_hmc_result_rows(text, ("a", "b"), "test read", blank_is_empty=True) == []
    )
    with pytest.raises(HMCCLIError, match="missing its header"):
        parse_hmc_result_rows(text, ("a", "b"), "test read")


def test_result_rows_wrap_parse_failures_with_operation_and_fields() -> None:
    with pytest.raises(
        HMCCLIError,
        match=r"^test read response did not match the expected a,b fields: .*header",
    ) as caught:
        parse_hmc_result_rows("a,c\n1,2\n", ("a", "b"), "test read")
    assert isinstance(caught.value.__cause__, ValueError)


def test_result_rows_return_parsed_rows() -> None:
    assert parse_hmc_result_rows("a,b\n1,2\n", ("a", "b"), "test read") == [
        {"a": "1", "b": "2"}
    ]


def test_parser_preserves_csv_values_and_empty_rows() -> None:
    text = '\na,b\n\n" x,y ",\n,\n'
    assert parse_hmc_delimited_rows(text, ("a", "b")) == [
        {"a": " x,y ", "b": ""},
        {"a": "", "b": ""},
    ]


def test_parser_accepts_header_only_and_final_line_without_newline() -> None:
    assert parse_hmc_delimited_rows("a,b", ("a", "b")) == []
    assert parse_hmc_delimited_rows("a,b\n1,2", ("a", "b")) == [{"a": "1", "b": "2"}]


def _evidence_records() -> list[dict[str, object]]:
    assert {path.name for path in FIXTURES.glob("*.json")} == EXPECTED_FIXTURES
    return [
        json.loads((FIXTURES / name).read_text()) for name in sorted(EXPECTED_FIXTURES)
    ]


@pytest.mark.asyncio
async def test_captured_roce_rows_are_accepted_with_empty_ethc_companion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Name the record rather than taking the first live capture: three fixtures
    # now match that description, and the SR-IOV one is the only one carrying a
    # `physical-ports` probe.
    records = dict(zip(sorted(EXPECTED_FIXTURES), _evidence_records(), strict=True))
    capture = records["power9-v10r3m1060-live-sriov.json"]
    assert capture["record_kind"] == "live-capture"
    assert capture["system_model"] == "8375-42A"
    roce_probe = next(
        probe for probe in capture["probes"] if probe["name"] == "physical-ports"
    )
    # The #214 record was transcribed: its rows print `eth` at --level roce, which
    # no byte capture of this adapter does (#1202). Its command is still the
    # projection before #1035 appended one attribute; the #1035 capture of the
    # current projection is what the reader must accept.
    assert capture["support"] == "transcribed"
    current = json.loads(
        (
            ROOT
            / "tests"
            / "fixtures"
            / "sriov"
            / "sriov-physport-granularity-v10r3.json"
        ).read_text()
    )["selection_cases"][0]
    # This adapter lists nothing at --level eth on V10R3 (ADR 0183).
    run = AsyncMock(
        side_effect=[
            current["roce"]["stdout"],
            current["ethc"]["stdout"],
            HMC_NO_RESULTS,
        ]
    )
    monkeypatch.setattr("hmcpctl.ssh.sriov.run_hmc_command", run)

    rows = await list_sriov_physical_port_rows(
        HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"}),
        "system-a",
        "1",
    )

    assert rows == current["expected_rows"]
    assert {row["phys_port_type"] for row in rows} == {"roce"}
    roce_command = run.await_args_list[0].args[1]
    assert roce_command == roce_probe["command"].replace(
        "curr_eth_logical_ports --header",
        "curr_eth_logical_ports,min_eth_capacity_granularity --header",
    )
    assert run.await_args_list[1].args[1] == roce_command.replace(
        "--level roce", "--level ethc"
    )
    assert run.await_args_list[2].args[1] == roce_command.replace(
        "--level roce", "--level eth"
    )
    fixture_sha256 = "fc498798590b1163a53a4f1bba4d40a0e12c4d010112b4a65ff16c03a53e9114"  # pragma: allowlist secret -- pinned fixture checksum
    fixture_bytes = (FIXTURES / "power9-v10r3m1060-live-sriov.json").read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == fixture_sha256
    with pytest.raises(AssertionError):
        assert (
            hashlib.sha256(fixture_bytes + b"perturbed").hexdigest() == fixture_sha256
        )


def test_evidence_records_have_closed_versioned_shapes() -> None:
    common = {
        "record_kind",
        "evidence_kind",
        "documentation_family",
        "hmc_release",
        "source_url",
        "source_locator",
        "claim_summary",
        "support",
    }
    for name, record in zip(
        sorted(EXPECTED_FIXTURES), _evidence_records(), strict=True
    ):
        if record["record_kind"] == "live-capture":
            assert record["evidence_kind"] == "live-capture"
            assert record["hmc_release"] == "V10R3 M1060 build 2408210051"
            assert record["system_model"] == "8375-42A"
            if record["support"] == "transcribed":
                assert record["transcription_note"]
            else:
                assert record["support"] == "captured"
            assert str(record["source_url"]).startswith("https://github.com/")
            assert record["probes"]
            for probe in record["probes"]:
                assert set(probe) == {
                    "name",
                    "command",
                    "fields",
                    "exit_status",
                    "stdout",
                    "stderr",
                }
            continue
        assert record["evidence_kind"] == "documentation"
        assert record["documentation_family"] in {
            "Power8",
            "Power9",
            "Power10",
            "Power11",
        }
        assert record["hmc_release"] == "not-established"
        assert str(record["source_url"]).startswith("https://www.ibm.com/")
        assert record["source_locator"] and record["claim_summary"]
        assert (record["source_url"], record["source_locator"]) == EXPECTED_PROVENANCE[
            name
        ]
        if record["record_kind"] == "read-fixture":
            assert set(record) == common | {"command", "fields", "parser_examples"}
            assert record["support"] == "documented"
            assert str(record["command"]).startswith(("lssyscfg ", "lshwres "))
            example = record["parser_examples"]
            assert isinstance(example, dict) and example["kind"] == "synthetic"
            assert parse_hmc_delimited_rows(example["stdout"], record["fields"])
        else:
            optional_unit = {"capacity_unit"} if "capacity_unit" in record else set()
            assert set(record) == common | {"admitted_claims"} | optional_unit
            assert record["support"] == "unknown"
            assert record["admitted_claims"]


def test_evidence_pins_identity_and_capacity_semantics() -> None:
    records = {
        path.stem: json.loads(path.read_text()) for path in FIXTURES.glob("*.json")
    }
    vnic = records["power9-v10r3m1060-live-vnic"]
    assert vnic["probes"][0]["fields"][-2:] == [
        "backing_devices",
        "backing_device_states",
    ]
    assert vnic["probes"][1]["fields"] == [
        "lpar_name",
        "lpar_id",
        "type",
        "adapter_id",
        "physical_port_id",
        "logical_port_id",
        "capacity",
        "desired_capacity",
        "max_capacity",
        "desired_max_capacity",
        "failover_priority",
        "is_active",
        "status",
    ]
    slot = records["power9-io-slot"]
    rows = parse_hmc_delimited_rows(slot["parser_examples"]["stdout"], slot["fields"])
    assert [row["drc_index"] for row in rows] == ["21010003", "21010004"]
    # An unowned slot prints the literal `null` (tests/fixtures/live/cli-io-slots.json).
    assert rows[1]["lpar_name"] == "null"
    assert records["power9-sriov-logport"]["admitted_claims"][0] == (
        "system + adapter_id + logical_port_id selectors"
    )
    assert records["power8-profile"]["command"] == (
        "lssyscfg -r prof -m sys1 -F sriov_eth_logical_ports --header"
    )
    assert records["power8-profile"]["fields"] == ["sriov_eth_logical_ports"]
    assert records["power9-io-slot"]["command"] == (
        "lshwres -r io --rsubtype slot -m sys1 "
        "-F drc_index,description,lpar_name --header"
    )
    assert records["power9-io-slot"]["fields"] == [
        "drc_index",
        "description",
        "lpar_name",
    ]
    assert records["power9-sriov-adapter"]["admitted_claims"] == [
        "system + adapter_id selector",
        "read fields remain unknown",
    ]
    assert records["power9-sriov-physport"]["admitted_claims"] == [
        "system + adapter_id + phys_port_id selectors",
        "physical-port reads select --level roce or ethc, never eth (ADR 0113)",
        "read fields remain unknown",
    ]
    expected_capacity_claims = [
        "dedicated-slot dynamic operations use -r io -o a/r -l",
        "adapter mode uses -o a/r with slot_id",
        "logical-port operations use adapter_id and logical_port_id",
        "capacity, max_capacity, and minimum granularity are percent with up to two decimals",
    ]
    for family in ("power10-sriov-contract", "power11-sriov-contract"):
        assert records[family]["capacity_unit"] == "percent"
        assert records[family]["admitted_claims"] == expected_capacity_claims
        assert "two decimals" in records[family]["admitted_claims"][-1]
        assert Decimal("10.25").as_tuple().exponent == -2


def test_dedicated_profile_io_slots_capture_is_pinned() -> None:
    fixture = FIXTURES / "power9-v10r3m1060-live-ioslots.json"
    record = json.loads(fixture.read_text())
    assert record["source_url"] == (
        "https://github.com/randomparity/hmc-mcp/issues/881#issuecomment-5779662835"
    )
    assert record["support"] == "captured"
    assert [probe["name"] for probe in record["probes"]] == [
        "hmc-version",
        "system-model",
        "profile-names",
        "io-slots-readback",
        "invalid-attribute-control",
    ]
    probes = {probe["name"]: probe for probe in record["probes"]}
    assert {name: probe["exit_status"] for name, probe in probes.items()} == {
        "hmc-version": 0,
        "system-model": 0,
        "profile-names": 0,
        "io-slots-readback": 0,
        "invalid-attribute-control": 1,
    }
    assert {probe["stderr"] for probe in record["probes"]} == {""}
    # These two probes print no identifier, so their lengths are the ones the
    # capture comment's own byte-count table can check against the published
    # (post-redaction) transcript. The other three are shorter than the table by
    # exactly the identifiers that were redacted out of them.
    assert len(probes["hmc-version"]["stdout"].encode()) == 236
    assert len(probes["invalid-attribute-control"]["stdout"].encode()) == 126
    readback = probes["io-slots-readback"]
    assert readback["command"] == (
        "lssyscfg -r prof -m sys-R1 -F lpar_name,name,io_slots --header"
    )
    assert readback["fields"] == ["lpar_name", "name", "io_slots"]
    assert [
        row["io_slots"]
        for row in parse_hmc_delimited_rows(readback["stdout"], readback["fields"])
    ] == [
        "21020013/none/1,21040015/none/1,21010020/none/0",
        "21020013/none/1,21040015/none/1",
    ]
    # The negative control is what makes the readback positive evidence rather
    # than a silent no-op: the HMC rejects an unknown -F attribute by name.
    assert probes["invalid-attribute-control"]["stdout"] == (
        "An invalid attribute was entered.  The invalid attribute is "
        "bogus_attr_xyz.  Please correct your entry and retry the command.\n"
    )
    fixture_sha256 = "07673ea2272fe9f1d5be3f37bebe8fe91d5fbebdb60c40cd641250895932d75b"  # pragma: allowlist secret -- pinned fixture checksum
    fixture_bytes = fixture.read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == fixture_sha256
    with pytest.raises(AssertionError):
        assert (
            hashlib.sha256(fixture_bytes + b"perturbed").hexdigest() == fixture_sha256
        )


def test_captured_io_slots_parse_with_a_none_pool() -> None:
    record = json.loads((FIXTURES / "power9-v10r3m1060-live-ioslots.json").read_text())
    readback = {probe["name"]: probe for probe in record["probes"]}["io-slots-readback"]
    rows = parse_hmc_delimited_rows(readback["stdout"], readback["fields"])

    parsed = [parse_profile_io_slots(row["io_slots"]) for row in rows]

    assert parsed[1] == (
        ProfileIoSlot("21020013", None, True),
        ProfileIoSlot("21040015", None, True),
    )
    assert parsed[0][-1] == ProfileIoSlot("21010020", None, False)
    assert {slot.pool_id for slots in parsed for slot in slots} == {None}


# The vnic capture carries no `hmc-version` probe, so it has nothing to check here.
@pytest.mark.parametrize(
    "fixture",
    ["power9-v10r3m1060-live-ioslots.json", "power9-v10r3m1060-live-sriov.json"],
)
def test_captured_hmc_version_is_the_exact_admitted_envelope(fixture: str) -> None:
    record = json.loads((FIXTURES / fixture).read_text())
    probes = {probe["name"]: probe for probe in record["probes"]}

    assert _is_exact_admitted_environment(
        probes["hmc-version"]["stdout"], record["system_model"]
    )


def test_operation_matrix_fails_closed_for_every_mutation_row() -> None:
    spec = (
        ROOT
        / "docs"
        / "workflow"
        / "specs"
        / "2026-08-20-pcie-capability-contract-design.md"
    ).read_text()
    rows = {
        columns[0]: columns[1:]
        for line in spec.splitlines()
        if line.startswith(("| Assign/unassign", "| Switch adapter"))
        if len(columns := [part.strip() for part in line.strip("|").split("|")]) == 5
    }
    assert set(rows) == {
        "Assign/unassign dedicated slot",
        "Assign/unassign SR-IOV logical port",
        "Switch adapter shared/dedicated mode",
    }
    assert all(len(outcomes) == 4 for outcomes in rows.values())
    assert (
        "do not compose Power10/11 mutation evidence with Power9 read evidence"
        in rows["Assign/unassign dedicated slot"][2]
    )
    # The dedicated row's profile cells mutate inside ADR 0165's envelope under
    # ADR 0166 and nowhere else; its dynamic cell still does not mutate at all.
    for outcome in rows["Assign/unassign dedicated slot"][:2]:
        assert "admitted by ADR 0165" in outcome
        assert "ADR 0166" in outcome
        assert "unavailable unconditionally outside it" in outcome
        # The envelope is the whole of what ADR 0165 confines, so widening it in
        # the spec must redden here rather than pass unremarked.
        assert "V10R3 M1060" in outcome
        assert "8375-42A" in outcome
    assert "do not mutate" in rows["Assign/unassign dedicated slot"][2]
    assert all(
        "do not mutate" in outcome
        for outcome in rows["Assign/unassign SR-IOV logical port"]
    )
    assert all(
        "do not mutate" in outcome
        for outcome in rows["Switch adapter shared/dedicated mode"][1:]
    )
    for token in (
        "lssyscfg -r lpar -m SYSTEM --filter lpar_ids=ID -F state,rmc_state",
        "RoCE profile grammar is unknown",
        "every non-success",
        "available-empty",
        "Issue #214 owns replacing or removing that path",
    ):
        assert token in spec


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    value: ast.expr = node.func
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def _canonical_ast(value: object) -> object:
    if isinstance(value, ast.AST):
        fields = []
        for name in value._fields:
            item = _canonical_ast(getattr(value, name, None))
            if item not in (None, [], ()):
                fields.append((name, item))
        return (type(value).__name__, fields)
    if isinstance(value, list):
        return [_canonical_ast(item) for item in value]
    return value


def test_sriov_mutation_surface_replaces_legacy_mode_and_never_forces() -> None:
    source = (ROOT / "src" / "hmcpctl" / "ssh" / "sriov.py").read_text()
    current = ast.parse(source)
    functions = {
        node.name: node
        for node in current.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "set_sriov_adapter_mode" not in functions
    for name in (
        "assign_sriov_logical_port_dynamic",
        "unassign_sriov_logical_port_profile",
    ):
        rendered = ast.unparse(functions[name])
        assert "--force" not in rendered
    assert "-o s --id" not in source
