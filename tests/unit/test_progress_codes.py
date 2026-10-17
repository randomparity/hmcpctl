"""Offline definitions preserve the distinction between code meaning and system state."""

import json
from dataclasses import FrozenInstanceError
from importlib.resources import files

import pytest

from hmcpctl.progress_codes import lookup_progress_code


@pytest.mark.parametrize("value", ["CA00E105", " ca00e105 ", "CA00E105 LP=00002"])
def test_operating_system_handoff(value):
    result = lookup_progress_code(value)
    assert result is not None
    assert result.notation == "CA00E105"
    assert "operating system" in result.definition.lower()
    assert result.pdf_pages == (100,)
    with pytest.raises(FrozenInstanceError):
        result.definition = "changed"


@pytest.mark.parametrize(
    "value", ["", " ", "?", "CA00E105Z", "B2001230 LP=00002", None, 42]
)
def test_unknown_or_malformed(value):
    assert lookup_progress_code(value) is None


@pytest.mark.parametrize(
    ("value", "notation"),
    [
        ("C2001000", "C2001000"),
        ("C200ABCD", "C200XXXX"),
        ("D1A23F01", "D1XX3Y01"),
        ("D1FF1001", "D1FF1XXX"),
        ("C5123456", "C5YXXXXX"),
        ("CA2601FF", "CA26TTSS"),
        ("888-102-32F", "888-102-32X"),
        ("2e6", "02E6"),
        ("c9", "00C9"),
    ],
)
def test_documented_patterns_and_short_indicators(value, notation):
    result = lookup_progress_code(value)
    assert result is not None
    assert result.notation == notation


def test_no_prefix_matching():
    assert lookup_progress_code("CA00E10500") is None
    assert lookup_progress_code("E105") is None
    assert lookup_progress_code("C200ZZZZ") is None


def test_packaged_inventory_is_complete_and_well_formed():
    data = json.loads(files("hmcpctl").joinpath("_progress_codes.json").read_text())
    assert data["source"]["url"] == (
        "https://public.dhe.ibm.com/systems/power/docs/hw/p9/p9eai_prg1.pdf"
    )
    assert len(data["entries"]) == 2202
    assert len({e["notation"] for e in data["entries"]}) == 2202
    for entry in data["entries"]:
        assert entry["definition"].strip()
        assert entry["applicability"].strip()
        assert all(15 <= page <= 136 for page in entry["pdf_pages"])
        result = lookup_progress_code(entry["notation"])
        assert result is not None
        assert result.notation == entry["notation"]


@pytest.mark.parametrize("value", ["C2000000", "C200FFFF"])
def test_variable_range_boundaries(value):
    assert lookup_progress_code(value).notation == "C200XXXX"


def test_variable_range_does_not_spill_into_other_prefix():
    assert lookup_progress_code("C2010000") is None


def test_exact_code_wins_over_overlapping_variable_pattern():
    assert lookup_progress_code("D600430A").notation == "D600430A"
    assert lookup_progress_code("D601430A").notation == "D6xx430A"


def test_equal_specificity_uses_source_order(monkeypatch, tmp_path):
    from hmcpctl import progress_codes

    entries = [
        {
            "notation": "C1XX0001",
            "definition": "First checkpoint.",
            "applicability": "Firmware",
            "pdf_pages": [56],
        },
        {
            "notation": "C100XX01",
            "definition": "Second checkpoint.",
            "applicability": "Firmware",
            "pdf_pages": [57],
        },
    ]
    (tmp_path / "_progress_codes.json").write_text(json.dumps({"entries": entries}))
    monkeypatch.setattr(progress_codes, "files", lambda _package: tmp_path)
    progress_codes._catalog.cache_clear()
    try:
        assert lookup_progress_code("C1000001").notation == "C1XX0001"
    finally:
        progress_codes._catalog.cache_clear()


def test_short_alias_requires_documented_leading_zero():
    assert lookup_progress_code("2E18").notation == "2E18"
    assert lookup_progress_code("E18") is None


@pytest.mark.parametrize(
    ("code", "terms"),
    [
        ("02E6", ("scsi", "configuration")),
        ("00C0", ("dump", "completion")),
        ("888-102-300", ("data-storage", "interrupt")),
        ("CF000100", ("memory", "initialization")),
        ("D1A23F01", ("scom", "dump")),
        ("D6000298", ("shutdown", "begun")),
    ],
)
def test_representative_factual_meanings(code, terms):
    text = lookup_progress_code(code).definition.lower()
    assert all(term in text for term in terms)


def test_catalog_lookup_is_independent_of_process_text_encoding(tmp_path):
    import subprocess
    import sys

    proof = (
        "import locale; locale.setlocale(locale.LC_CTYPE, 'C'); "
        "from hmcpctl.progress_codes import lookup_progress_code; "
        "assert lookup_progress_code('CA00E105').notation == 'CA00E105'"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8=0", "-c", proof],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("notation", "valid", "invalid"),
    [
        ("C104550X", ["C1045500", "C1045501"], ["C1045502", "C104550F"]),
        ("C116C2XX", ["C116C200", "C116C21F"], ["C116C220", "C116C2FF"]),
        ("C16453XX", ["C1645302", "C16453F3"], ["C16453F4", "C164530F"]),
    ],
)
def test_documented_variable_bounds(notation, valid, invalid):
    assert lookup_progress_code(notation).notation == notation
    for code in valid:
        assert lookup_progress_code(code).notation == notation
    for code in invalid:
        assert lookup_progress_code(code) is None


def test_exact_code_outside_variable_bounds_still_wins():
    assert lookup_progress_code("C1645301").notation == "C1645301"


def test_bounded_pattern_ranks_by_admitted_values(monkeypatch, tmp_path):
    from hmcpctl import progress_codes

    entries = [
        {
            "notation": notation,
            "definition": "Checkpoint.",
            "applicability": "Firmware",
            "pdf_pages": [72],
        }
        for notation in ("C116CXXF", "C116C2XX")
    ]
    (tmp_path / "_progress_codes.json").write_text(json.dumps({"entries": entries}))
    monkeypatch.setattr(progress_codes, "files", lambda _package: tmp_path)
    progress_codes._catalog.cache_clear()
    try:
        assert lookup_progress_code("C116C21F").notation == "C116C2XX"
    finally:
        progress_codes._catalog.cache_clear()
