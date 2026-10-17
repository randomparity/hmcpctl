"""The source inventory checker detects omissions without retaining IBM prose."""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_progress_codes",
    Path(__file__).parents[2] / "scripts" / "check_progress_codes.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_checker)
extract_inventory = _checker.extract_inventory
validate_catalog = _checker.validate_catalog


def test_extracts_unheaded_entries_and_keeps_code_start_page(tmp_path):
    source = tmp_path / "source.md"
    source.write_text(
        "## PDF page 94\n### Firmware\n#### C700XXXX\n"
        "### Other firmware\n**Explanation:**\nFirmware checkpoint.\n"
        "## PDF page 95\nD1XX3Y01\n**Explanation:**\nCapture.\n"
    )
    assert extract_inventory(source) == [
        {"notation": "C700XXXX", "applicability": "Firmware", "pdf_pages": [94]},
        {"notation": "D1XX3Y01", "applicability": "Other firmware", "pdf_pages": [95]},
    ]


def test_rejects_unassociated_explanation(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("## PDF page 94\n**Explanation:**\nOrphan.\n")
    with pytest.raises(ValueError, match="unassociated explanation"):
        extract_inventory(source)


def test_missing_entry_is_not_complete():
    with pytest.raises(ValueError, match="inventory"):
        validate_catalog({"entries": []}, [{"notation": "CA00E105"}])


def test_packaged_catalog_matches_frozen_inventory():
    from importlib.resources import files
    from pathlib import Path

    catalog = json.loads(files("hmcpctl").joinpath("_progress_codes.json").read_text())
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures" / "progress-code-inventory.json"
        ).read_text()
    )
    validate_catalog(catalog)
    assert catalog["source"]["inventory_sha256"] == fixture["inventory_sha256"]
    assert len(catalog["entries"]) == fixture["count"]


@pytest.mark.parametrize(
    "mutation", ["empty-definition", "duplicate", "missing-page", "bad-notation"]
)
def test_rejects_catalog_defects(mutation):
    from importlib.resources import files

    data = json.loads(files("hmcpctl").joinpath("_progress_codes.json").read_text())
    if mutation == "empty-definition":
        data["entries"][0]["definition"] = " "
    elif mutation == "duplicate":
        data["entries"].append(data["entries"][0])
    elif mutation == "missing-page":
        data["entries"][0]["pdf_pages"] = []
    else:
        data["entries"][0]["notation"] = "../bad"
    with pytest.raises(ValueError):
        validate_catalog(data)
