"""All five assigned rows and 152 independent source units retain truthful route bindings."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from hmcpctl.jobs import requests

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests/fixtures/system_profiles/reference-contracts.json"


def test_five_rows_all_units_bound():
    fixture = json.loads(FIXTURE.read_text())
    rows = {
        r["id"]: r
        for r in json.loads((ROOT / "docs/capabilities/rows.json").read_text())["rows"]
    }
    operations = {
        r["operation"]: r
        for r in json.loads((ROOT / "docs/capabilities/operations.json").read_text())[
            "operations"
        ]
    }
    assert len(fixture["bindings"]) == 5
    units = []
    for row_id, binding in fixture["bindings"].items():
        row = rows[row_id]
        assert binding["source_units"] == row["source_units"]
        assert set(binding["source_units"]) == set(row["modes"]) | set(
            row["parameters"]
        )
        assert {u.split(":", 1)[0] for u in binding["source_units"]} == {
            "rest-p10",
            "rest-p11",
        }
        assert operations[binding["operation"]]["row_ids"] == [row_id]
        assert binding["product_native_proof"] == "not-run"
        assert binding["native_rest_transport"] == (
            "activation-only-implemented-unevidenced"
            if binding["operation"] == "system_profile.activate"
            else "implemented-unevidenced"
            if binding["route"] == "rest-job"
            else "unresolved-not-used"
        )
        units.extend(binding["source_units"])
    assert len(units) == len(set(units)) == 152
    assert fixture["borrowed_owners"] == {
        "LPARSaveCurrentConfig": 638,
        "VIOSSaveCurrentConfig": 698,
        "rmprofdata": 698,
    }


def test_reference_parameter_matrix():
    fixture = json.loads(FIXTURE.read_text())
    units = {
        u["id"]: u
        for u in json.loads((ROOT / "docs/capabilities/corpora.json").read_text())[
            "source_units"
        ]
    }
    documents = {
        "system_profile.create": requests.create_system_profile_job(
            "name", ("a", "b"), ("p", "q")
        ),
        "system_profile.update": requests.update_system_profile_job(
            "name", "renamed", ("a", "b"), ("p", "q")
        ),
        "system_profile.activate": requests.activate_system_profile_job("name", True),
    }
    responses = {"returnCode", "result", "detailedStatus"}
    headings = {"Request Parameter", "Response Parameter", "Parameter", "Method"}
    for binding in fixture["bindings"].values():
        fields = {
            units[u]["text"]
            for u in binding["source_units"]
            if units[u]["kind"] == "rest-field"
        }
        assert fields == set(binding["native_fields"])
        assert {
            units[u]["text"]
            for u in binding["source_units"]
            if units[u]["kind"] == "capability-statement"
        } == {"versions:10.3.1060.0"}
        if binding["operation"] in documents:
            root = ET.fromstring(documents[binding["operation"]])
            actual = {
                p.find("{*}ParameterName").text
                for p in root.findall(".//{*}JobParameter")
            }
            assert actual == fields - responses - headings
            assert root.tag.endswith("JobRequest")
            assert root.find(".//{*}GroupName").text == "ManagedSystem"
            assert root.find(".//{*}OperationName").text == binding["native_operation"]
        assert {
            units[u]["text"]
            for u in binding["source_units"]
            if units[u]["kind"] == "rest-method"
        } == {"PUT"}
        if binding["operation"] == "system_profile.activate":
            assert binding["native_rest_test"] == "unresolved-not-used"
        if binding["operation"] not in documents:
            assert binding["route"] == "ssh-command"
            assert binding["native_rest_transport"] == "unresolved-not-used"
    assert not hasattr(requests, "get_system_profiles_job")
    assert not hasattr(requests, "delete_system_profile_job")
