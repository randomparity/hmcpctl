"""Creation-only memory layout values preserve their exact sourced contracts."""

from dataclasses import FrozenInstanceError, asdict, fields
from decimal import Decimal, localcontext

import pytest
from pydantic import TypeAdapter, ValidationError

from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout

COUNT_FIELDS = (
    "min_num_huge_pages",
    "desired_num_huge_pages",
    "max_num_huge_pages",
)


def test_exact_six_fields_omission_and_explicit_zero():
    assert {item.name for item in fields(LparMemoryLayout)} == {
        *COUNT_FIELDS,
        "hpt_ratio",
        "ppt_ratio",
        "kvm_reserved_mem_percent",
    }
    omitted = LparMemoryLayout()
    assert omitted.pairs() == []
    assert set(asdict(omitted).values()) == {None}
    selected = LparMemoryLayout(0, 0, 0, kvm_reserved_mem_percent=Decimal(0))
    assert selected.pairs() == [
        *((name, 0) for name in COUNT_FIELDS),
        ("kvm_reserved_mem_percent", "0"),
    ]
    assert selected != omitted
    with pytest.raises(FrozenInstanceError):
        selected.hpt_ratio = "128"


@pytest.mark.parametrize("name", COUNT_FIELDS)
@pytest.mark.parametrize("value", [-1, True, False, "1", 1.0, Decimal(1)])
def test_counts_are_strict(name, value):
    with pytest.raises(ValueError, match="huge_pages"):
        LparMemoryLayout(**{name: value})


@pytest.mark.parametrize("counts", [(2, 1, 3), (0, 3, 2), (3, None, 2), (None, 2, 1)])
def test_partial_ordering(counts):
    with pytest.raises(ValueError, match="minimum <= desired <= maximum"):
        LparMemoryLayout(*counts)


@pytest.mark.parametrize(
    "counts", [(0, 0, 0), (1, None, 2), (None, 1, None), (None, None, 0)]
)
def test_partial_ordering_does_not_invent_defaults(counts):
    layout = LparMemoryLayout(*counts)
    assert [value for _, value in layout.pairs()] == [
        value for value in counts if value is not None
    ]


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        "1",
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        float("nan"),
        float("inf"),
        Decimal("1.001"),
        Decimal("0.0099999999999999999"),
        Decimal("1.00000000000000001"),
        Decimal("123456789012345678901234567890.001"),
    ],
)
def test_reservation_rejects_invalid_numbers_and_overprecision(value):
    with pytest.raises(ValueError, match="kvm_reserved_mem_percent"):
        LparMemoryLayout(kvm_reserved_mem_percent=value)


@pytest.mark.parametrize(
    "value,text",
    [
        (0, "0"),
        (0.01, "0.01"),
        (Decimal("-0.01"), "-0.01"),
        (Decimal(101), "101"),
        (Decimal("12345.67"), "12345.67"),
        (Decimal("1.2300"), "1.23"),
        (Decimal("1E+3"), "1000"),
    ],
)
def test_reservation_has_no_invented_interval_and_serializes_exactly(value, text):
    layout = LparMemoryLayout(kvm_reserved_mem_percent=value)
    assert isinstance(layout.kvm_reserved_mem_percent, Decimal)
    assert layout.pairs() == [("kvm_reserved_mem_percent", text)]


def test_decimal_value_and_precision_are_independent_of_ambient_context():
    digits = "12345678901234567890123456789012345678901234567890"
    value = Decimal(digits + ".1200")
    with localcontext() as context:
        context.prec = 2
        layout = LparMemoryLayout(kvm_reserved_mem_percent=value)
        assert layout.kvm_reserved_mem_percent == value
        assert layout.pairs() == [("kvm_reserved_mem_percent", digits + ".12")]
        with pytest.raises(ValueError, match="two decimal places"):
            LparMemoryLayout(kvm_reserved_mem_percent=Decimal(digits + ".001"))
    assert LparMemoryLayout(
        kvm_reserved_mem_percent=Decimal("1.00")
    ) == LparMemoryLayout(kvm_reserved_mem_percent=Decimal(1))


@pytest.mark.parametrize("name", ["hpt_ratio", "ppt_ratio"])
@pytest.mark.parametrize(
    "value", ["", "null", "none", "1,2", "a=b", "a\nb", "a b", 'a"b', True, 128]
)
def test_ratio_grammar_is_strict(name, value):
    with pytest.raises(ValueError, match=name):
        LparMemoryLayout(**{name: value})


@pytest.mark.parametrize("kind", ["AIX/Linux", "OS400", "Virtual IO Server"])
def test_ratios_preserve_all_source_partition_contexts(kind):
    layout = LparMemoryLayout(hpt_ratio="1:128", ppt_ratio="future_ratio-1")
    layout.validate_creation(kind)
    assert layout.pairs() == [("hpt_ratio", "1:128"), ("ppt_ratio", "future_ratio-1")]


@pytest.mark.parametrize("kind", ["OS400", "Virtual IO Server"])
@pytest.mark.parametrize("name", [*COUNT_FIELDS, "kvm_reserved_mem_percent"])
def test_field_specific_context_includes_explicit_zero(kind, name):
    value = Decimal(0) if name == "kvm_reserved_mem_percent" else 0
    layout = LparMemoryLayout(**{name: value})
    with pytest.raises(ValueError, match="AIX/Linux|Linux intent"):
        layout.validate_creation(kind)
    layout.validate_creation("AIX/Linux")
    LparMemoryLayout().validate_creation(kind)


@pytest.mark.parametrize(
    "value",
    [
        {"extra": 1},
        {"min_num_huge_pages": "1"},
        {"desired_num_huge_pages": True},
        {"hpt_ratio": 128},
        {"kvm_reserved_mem_percent": True},
        {"kvm_reserved_mem_percent": Decimal("0.001")},
    ],
)
def test_schema_rejects_unknown_fields_coercion_and_invalid_values(value):
    with pytest.raises(ValidationError):
        TypeAdapter(LparMemoryLayout).validate_python(value)


def test_schema_preserves_decimal_and_zero():
    value = Decimal("123456789012345678901234567890.12")
    layout = TypeAdapter(LparMemoryLayout).validate_python(
        {
            "min_num_huge_pages": 0,
            "kvm_reserved_mem_percent": value,
        }
    )
    assert layout.pairs() == [
        ("min_num_huge_pages", 0),
        ("kvm_reserved_mem_percent", str(value)),
    ]
