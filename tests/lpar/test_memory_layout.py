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


@pytest.mark.parametrize("sign", ["", "-"])
@pytest.mark.parametrize("length", [65535, 65536, 65537])
def test_reservation_encoded_boundary(sign, length):
    value = Decimal(sign + "1E+" + str(length - len(sign) - 1))
    if length > 65536:
        with pytest.raises(ValueError, match="65536 ASCII bytes"):
            LparMemoryLayout(kvm_reserved_mem_percent=value)
    else:
        text = LparMemoryLayout(kvm_reserved_mem_percent=value).pairs()[0][1]
        assert len(text.encode("ascii")) == length
        assert Decimal(text) == value


@pytest.mark.parametrize(
    "token,expected", [("0E-1000", "0"), ("-0E-1000", "-0"), ("1200.0000", "1200")]
)
def test_reservation_canonicalizes_without_context_rounding(token, expected):
    with localcontext() as context:
        context.prec = 1
        assert LparMemoryLayout(kvm_reserved_mem_percent=Decimal(token)).pairs() == [
            ("kvm_reserved_mem_percent", expected)
        ]


@pytest.mark.asyncio
async def test_typed_mcp_rejects_compact_oversized_reservation():
    from fastmcp import FastMCP
    from fastmcp.exceptions import ValidationError as MCPValidationError

    mcp = FastMCP("offline-layout-boundary")

    @mcp.tool
    def prospective_create(memory_layout: LparMemoryLayout) -> str:
        return memory_layout.pairs()[0][1]

    tool = await mcp.get_tool("prospective_create")
    with pytest.raises(MCPValidationError, match="65536 ASCII bytes"):
        await tool.run({"memory_layout": {"kvm_reserved_mem_percent": "1E+65536"}})
    boundary = await tool.run(
        {"memory_layout": {"kvm_reserved_mem_percent": "1E+65535"}}
    )
    assert len(boundary.content[0].text) == 65536
    result = await tool.run({"memory_layout": {"kvm_reserved_mem_percent": "0E-1000"}})
    assert result.content[0].text == "0"


def test_zero_is_canonical_before_fixed_formatting(monkeypatch):
    import builtins

    import hmcpctl.documents.lpar_memory_layout as module

    def checked_format(value, specification):
        assert value.as_tuple().exponent == 0
        return builtins.format(value, specification)

    monkeypatch.setattr(module, "format", checked_format, raising=False)
    assert LparMemoryLayout(kvm_reserved_mem_percent=Decimal("0E-1000")).pairs() == [
        ("kvm_reserved_mem_percent", "0")
    ]


def native_table(columns, *rows):
    import csv
    import io

    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerows([columns, *rows])
    return output.getvalue()


@pytest.fixture
def native_layout(monkeypatch):
    from unittest.mock import AsyncMock

    from hmcpctl.config import HMCConfig
    from hmcpctl.ssh import lpar_memory_layout as native

    transport = AsyncMock()
    monkeypatch.setattr(native, "run_hmc_command", transport)
    return native, transport, HMCConfig.from_mapping({})


@pytest.mark.asyncio
async def test_advertised_membership_and_no_unneeded_read(native_layout):
    native, transport, config = native_layout
    await native.admit_memory_layout(
        config, "sys-R1", LparMemoryLayout(min_num_huge_pages=0)
    )
    transport.assert_not_awaited()
    transport.return_value = native_table(
        ["possible_hpt_ratios", "possible_ppt_ratios"], ["128,256", "1:64,future-1"]
    )
    await native.admit_memory_layout(
        config, "sys R1", LparMemoryLayout(hpt_ratio="128", ppt_ratio="future-1")
    )
    assert transport.await_args.args == (
        config,
        "lshwres -r mem -m 'sys R1' --level sys -F possible_hpt_ratios,possible_ppt_ratios --header",
    )
    await native.admit_memory_layout(
        config, "sys-R1", LparMemoryLayout(hpt_ratio="128")
    )
    with pytest.raises(ValueError, match="advertised"):
        await native.admit_memory_layout(
            config, "sys-R1", LparMemoryLayout(hpt_ratio="64")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        "",
        "No results were found.",
        "wrong\n128\n",
        native_table(
            ["possible_hpt_ratios", "possible_ppt_ratios"], ["128", "64"], ["128", "64"]
        ),
        *[
            native_table(["possible_hpt_ratios", "possible_ppt_ratios"], [value, "64"])
            for value in ["", "null", "none", "unknown", "128,128", "128,", "a b"]
        ],
    ],
)
async def test_missing_or_ambiguous_advertisement(native_layout, output):
    from hmcpctl.ssh.transport import HMCCLIError

    native, transport, config = native_layout
    transport.return_value = output
    with pytest.raises((ValueError, HMCCLIError)):
        await native.admit_memory_layout(
            config, "sys-R1", LparMemoryLayout(hpt_ratio="128")
        )


PROFILE_COLUMNS = [
    "lpar_name",
    "name",
    *COUNT_FIELDS,
    "hpt_ratio",
    "ppt_ratio",
    "kvm_reserved_mem_percent",
]


@pytest.mark.asyncio
async def test_strict_profile_read_and_filter(native_layout):
    native, transport, config = native_layout
    transport.return_value = native_table(
        PROFILE_COLUMNS,
        [
            "part R1",
            "default_profile",
            "0",
            "null",
            "2",
            "128",
            "null",
            "12345678901234567890.12",
        ],
    )
    result = await native.get_lpar_memory_layout(config, "sys R1", "part R1")
    assert result == LparMemoryLayout(
        0, None, 2, "128", None, Decimal("12345678901234567890.12")
    )
    assert transport.await_args.args == (
        config,
        "lssyscfg -r prof -m 'sys R1' --filter 'lpar_names=part R1,profile_names=default_profile' -F "
        + ",".join(PROFILE_COLUMNS)
        + " --header",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "index,value",
    [
        (0, "foreign"),
        (1, "other"),
        (2, "-1"),
        (2, "1.0"),
        (2, "١"),
        (2, "unknown"),
        (3, "0"),
        (5, "unknown"),
        (5, "a b"),
        (7, "NaN"),
        (7, "1.001"),
        (7, "1E+65536"),
        (7, "garbage"),
    ],
)
async def test_profile_identity_and_malformed_values(native_layout, index, value):
    native, transport, config = native_layout
    row = ["part-R1", "default_profile", "1", "2", "3", "128", "64", "0"]
    row[index] = value
    transport.return_value = native_table(PROFILE_COLUMNS, row)
    with pytest.raises(ValueError):
        await native.get_lpar_memory_layout(config, "sys-R1", "part-R1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows", [[], [["part-R1", "default_profile", *(["null"] * 6)]] * 2]
)
async def test_profile_requires_single_row(native_layout, rows):
    native, transport, config = native_layout
    transport.return_value = native_table(PROFILE_COLUMNS, *rows)
    with pytest.raises(ValueError, match="exactly one"):
        await native.get_lpar_memory_layout(config, "sys-R1", "part-R1")
