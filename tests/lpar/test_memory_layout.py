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


LAYOUT_SYSTEM = "00000001-abcd-4ef0-8abc-000000000001"
LAYOUT_PARTITION = "00000002-abcd-4ef0-8abc-000000000002"


@pytest.fixture
def layout_creation_target(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hmcpctl.client.core import HMCClient
    from hmcpctl.config import HMCConfig
    from hmcpctl.operations.lpar import core
    from hmcpctl.operations.lpar.linux_roles import RoleCapabilities

    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping({"host": "hmc.test", "user": "test"})
    entry = {
        "UUID": LAYOUT_PARTITION,
        "Resource": {"PartitionName": "part-R1", "PartitionType": "AIX/Linux"},
    }
    hmc.find_partition_by_name.side_effect = [None, entry]
    hmc.create_logical_partition.return_value = entry
    hmc.get_managed_system.return_value = {
        "UUID": LAYOUT_SYSTEM,
        "Resource": {
            "SystemName": "sys-R1",
            "AssociatedSystemMemoryConfiguration": {"ConfigurableSystemMemory": "8192"},
        },
    }
    monkeypatch.setattr(
        core, "resolve_system_uuid", AsyncMock(return_value=LAYOUT_SYSTEM)
    )
    monkeypatch.setattr(
        core, "resolve_system_cli_name", AsyncMock(return_value="sys-R1")
    )
    mode = AsyncMock(return_value=RoleCapabilities(False, False, None, None, False))
    monkeypatch.setattr(core, "read_role_capabilities", mode)
    stamp = AsyncMock(return_value=(True, ()))
    monkeypatch.setattr(core, "stamp_created_lpar_ownership", stamp)
    apply = AsyncMock()
    monkeypatch.setattr(core, "apply_lpar_profile_via_cli", apply)
    write = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", write)
    read = AsyncMock()

    def response(config, command):
        if command.startswith("lshwres"):
            return native_table(
                ["possible_hpt_ratios", "possible_ppt_ratios"],
                ["1:64,1:128", "1:128,1:256"],
            )
        return native_table(
            PROFILE_COLUMNS,
            ["part-R1", "default_profile", "0", "1", "2", "1:128", "1:256", "0.00"],
        )

    read.side_effect = response
    monkeypatch.setattr("hmcpctl.ssh.lpar_memory_layout.run_hmc_command", read)
    return SimpleNamespace(
        hmc=hmc,
        entry=entry,
        mode=mode,
        stamp=stamp,
        apply=apply,
        write=write,
        read=read,
    )


def layout_creation(layout=None, **kwargs):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.core import LparCreation

    return LparCreation(
        "part-R1",
        "AIX/Linux",
        LparResources(
            desired_memory=1024,
            min_memory=1024,
            max_memory=1024,
            desired_procs=1,
            dedicated=True,
        ),
        memory_layout=layout or LparMemoryLayout(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_layout_single_native_record_contains_six_fields(layout_creation_target):
    import shlex

    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    layout = LparMemoryLayout(0, 1, 2, "1:128", "1:256", Decimal("0.00"))
    result = await create_and_stamp_lpar(
        target.hmc, "selector", layout_creation(layout)
    )
    record = shlex.split(target.write.await_args.args[1])[-1]
    assert all(f"{name}={value}" in record.split(",") for name, value in layout.pairs())
    target.write.assert_awaited_once()
    target.hmc.create_logical_partition.assert_not_awaited()
    target.mode.assert_awaited_once_with(
        target.hmc, LAYOUT_SYSTEM, need_kvm=False, need_management=False
    )
    assert result.memory_layout_step.status == "ok"
    assert result.memory_layout_step.result.status == "verified"


@pytest.mark.asyncio
async def test_layout_omission_preserves_rest_without_reads_or_steps(
    layout_creation_target,
):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    result = await create_and_stamp_lpar(target.hmc, "selector", layout_creation())
    target.hmc.create_logical_partition.assert_awaited_once()
    target.mode.assert_not_awaited()
    target.read.assert_not_awaited()
    target.write.assert_not_awaited()
    assert result.memory_layout_step is None


@pytest.mark.asyncio
async def test_layout_keylock_rejected_without_traffic(layout_creation_target):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    with pytest.raises(ValueError, match="keylock"):
        await create_and_stamp_lpar(
            target.hmc,
            "selector",
            layout_creation(LparMemoryLayout(hpt_ratio="1:128"), keylock="normal"),
        )
    assert target.hmc.mock_calls == []
    target.mode.assert_not_awaited()
    target.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "mismatch", "error"])
async def test_layout_postcondition_distinguishes_missing_and_mismatch(
    layout_creation_target, mode
):
    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    request = LparMemoryLayout(min_num_huge_pages=0, desired_num_huge_pages=1)
    if mode == "error":
        target.read.side_effect = HMCError("unavailable")
    else:
        target.read.side_effect = None
        target.read.return_value = native_table(
            PROFILE_COLUMNS,
            [
                "part-R1",
                "default_profile",
                "2" if mode == "mismatch" else "0",
                "null",
                "null",
                "null",
                "null",
                "null",
            ],
        )
    result = await create_and_stamp_lpar(
        target.hmc, "selector", layout_creation(request, apply_profile=True)
    )
    assert result.memory_layout_step.status == "error"
    observation = result.memory_layout_step.result
    assert observation.status == ("mismatch" if mode == "mismatch" else "unavailable")
    assert "desired_num_huge_pages" in observation.unavailable
    target.apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["null", "unknown", "malformed"])
async def test_layout_contradiction_requires_a_valid_row_before_workflow_promotion(
    layout_creation_target, missing
):
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.workflows import create_lpar

    target = layout_creation_target
    target.read.side_effect = None
    target.read.return_value = native_table(
        PROFILE_COLUMNS,
        ["part-R1", "default_profile", "2", missing, "null", "null", "null", "null"],
    )
    result = await create_lpar(
        target.hmc,
        "selector",
        layout_creation(
            LparMemoryLayout(min_num_huge_pages=0, desired_num_huge_pages=1),
            apply_profile=True,
        ),
        LparPcieAssignments(),
    )
    step = next(step for step in result.steps if step.step == "memory_layout")
    assert step.status == "error"
    observation = step.result
    assert observation.status == ("mismatch" if missing == "null" else "unavailable")
    assert observation.observed == (
        {"min_num_huge_pages": 2} if missing == "null" else {}
    )
    assert observation.unavailable == (
        ("desired_num_huge_pages",)
        if missing == "null"
        else ("min_num_huge_pages", "desired_num_huge_pages")
    )
    assert result.resource_created is True
    assert result.workflow_completed is False
    target.write.assert_awaited_once()
    target.apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["bad,name", "bad=name", 'bad"name'])
async def test_layout_invalid_record_preserves_reason_without_reconciliation(
    layout_creation_target, name
):
    from dataclasses import replace

    from hmcpctl.operations.lpar.core import _create_via_cli
    from hmcpctl.ssh.transport import HMCCLIError

    target = layout_creation_target
    creation = replace(
        layout_creation(LparMemoryLayout(min_num_huge_pages=0)), name=name
    )
    with pytest.raises(HMCCLIError, match="attribute 'name'.*contains") as caught:
        await _create_via_cli(target.hmc, LAYOUT_SYSTEM, "selector", creation)
    assert name in str(caught.value)
    assert "Remove" in str(caught.value)
    target.write.assert_not_awaited()
    target.hmc.find_partition_by_name.assert_not_awaited()
    target.hmc.create_logical_partition.assert_not_awaited()
    target.stamp.assert_not_awaited()
    target.apply.assert_not_awaited()
    target.hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_controller_omits_profile_and_reports_unavailable(
    layout_creation_target,
):
    import shlex

    from hmcpctl.operations.lpar.core import create_and_stamp_lpar
    from hmcpctl.operations.lpar.linux_roles import RoleCapabilities

    target = layout_creation_target
    target.mode.return_value = RoleCapabilities(True, False, None, None, False)
    result = await create_and_stamp_lpar(
        target.hmc,
        "selector",
        layout_creation(LparMemoryLayout(hpt_ratio="1:128"), apply_profile=False),
    )
    assert "profile_name=" not in shlex.split(target.write.await_args.args[1])[-1]
    assert result.memory_layout_step.result.status == "unavailable"
    assert result.memory_layout_step.result.unavailable == ("hpt_ratio",)
    assert all(
        not c.args[1].startswith("lssyscfg") for c in target.read.await_args_list
    )
    target.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_controller_apply_denial_precedes_create(layout_creation_target):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar
    from hmcpctl.operations.lpar.linux_roles import RoleCapabilities

    target = layout_creation_target
    target.mode.return_value = RoleCapabilities(True, False, None, None, False)
    with pytest.raises(ValueError, match="[Cc]ontroller"):
        await create_and_stamp_lpar(
            target.hmc,
            "selector",
            layout_creation(LparMemoryLayout(hpt_ratio="1:128"), apply_profile=True),
        )
    target.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "partial", "unknown"])
@pytest.mark.parametrize(
    ("failure", "absent_outcome"),
    [
        ("stderr", "rejected"),
        ("stdout", "rejected"),
        ("timeout", "unknown"),
        ("connection", "unknown"),
        ("opaque", "unknown"),
        ("uncoded", "unknown"),
        ("command-only", "unknown"),
        ("signal", "unknown"),
        ("missing-status", "unknown"),
        ("zero-status", "unknown"),
    ],
)
async def test_layout_reconciliation_never_retries_or_stamps(
    layout_creation_target, outcome, failure, absent_outcome
):
    from dataclasses import replace

    import asyncssh

    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar
    from hmcpctl.ssh.transport import (
        HMCCLIError,
        _command_connection_error,
        _command_timeout_error,
        _process_error,
    )

    target = layout_creation_target
    name = "part-R1" if failure == "uncoded" else "part: HSCLA4A0"
    target.entry["Resource"]["PartitionName"] = name

    async def fail(config, command):
        if failure == "timeout":
            raise _command_timeout_error(config, command)
        if failure == "connection":
            raise _command_connection_error(command, ConnectionError("transport lost"))
        if failure == "opaque":
            raise HMCCLIError("HSCLA4A0: private-provider-detail")
        response = (
            "private-provider-detail"
            if failure in {"uncoded", "command-only"}
            else "HSCLA4A0: private-provider-detail"
        )
        status = {"signal": None, "missing-status": None, "zero-status": 0}.get(
            failure, 1
        )
        raise _process_error(
            command,
            asyncssh.ProcessError(
                env={},
                command=command,
                subsystem=None,
                exit_status=status,
                exit_signal="TERM" if failure == "signal" else None,
                returncode=-15 if failure == "signal" else status,
                stdout=response if failure == "stdout" else "",
                stderr="" if failure == "stdout" else response,
            ),
        )

    target.write.side_effect = fail
    second = (
        None
        if outcome == "rejected"
        else target.entry
        if outcome == "partial"
        else HMCError("lookup unavailable")
    )
    target.hmc.find_partition_by_name.side_effect = [None, second]
    expected = absent_outcome if outcome == "rejected" else outcome
    with pytest.raises(HMCError, match=expected) as caught:
        await create_and_stamp_lpar(
            target.hmc,
            "selector",
            replace(layout_creation(LparMemoryLayout(hpt_ratio="1:128")), name=name),
        )
    assert "private-provider-detail" not in str(caught.value)
    assert "HSCLA4A0" not in str(caught.value)
    assert "Inspect scoped identity before retrying" in str(caught.value)
    assert target.hmc.find_partition_by_name.await_count == 2
    target.write.assert_awaited_once()
    target.hmc.create_logical_partition.assert_not_awaited()
    target.stamp.assert_not_awaited()
    target.apply.assert_not_awaited()
    target.hmc.delete_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_identity", [None, "wrong-name", "bad-uuid", "wrong-type"])
async def test_layout_identity_unavailable_never_stamps_or_reads_profile(
    layout_creation_target, bad_identity
):
    from copy import deepcopy

    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    entry = deepcopy(target.entry)
    if bad_identity is None:
        entry = None
    elif bad_identity == "wrong-name":
        entry["Resource"]["PartitionName"] = "other-partition"
    elif bad_identity == "bad-uuid":
        entry["UUID"] = "not-a-uuid"
    else:
        entry = []
    target.hmc.find_partition_by_name.side_effect = [None, entry]
    result = await create_and_stamp_lpar(
        target.hmc, "selector", layout_creation(LparMemoryLayout(hpt_ratio="1:128"))
    )
    assert result.resource_created is True and result.lpar is None
    assert result.memory_layout_step.result.status == "unavailable"
    target.stamp.assert_not_awaited()
    assert all(
        not c.args[1].startswith("lssyscfg") for c in target.read.await_args_list
    )


@pytest.mark.asyncio
async def test_layout_successful_native_write_with_lookup_error_is_unavailable(
    layout_creation_target,
):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    target.hmc.find_partition_by_name.side_effect = [
        None,
        RuntimeError("private-provider-detail"),
    ]
    result = await create_and_stamp_lpar(
        target.hmc, "selector", layout_creation(LparMemoryLayout(hpt_ratio="1:128"))
    )
    assert (
        result.resource_created
        and result.memory_layout_step.result.status == "unavailable"
    )
    target.stamp.assert_not_awaited()
    target.write.assert_awaited_once()
    assert "private-provider-detail" not in str(result)


@pytest.mark.asyncio
async def test_layout_uncoded_native_failure_with_absence_remains_unknown(
    layout_creation_target,
):
    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar
    from hmcpctl.ssh.transport import HMCCLIError

    target = layout_creation_target
    target.write.side_effect = HMCCLIError("transport lost")
    target.hmc.find_partition_by_name.side_effect = [None, None]
    with pytest.raises(HMCError, match="unknown"):
        await create_and_stamp_lpar(
            target.hmc, "selector", layout_creation(LparMemoryLayout(hpt_ratio="1:128"))
        )
    target.write.assert_awaited_once()
    target.stamp.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_percentage_zero_adds_no_kvm_role_or_companion(
    layout_creation_target,
):
    import shlex

    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    result = await create_and_stamp_lpar(
        target.hmc,
        "selector",
        layout_creation(LparMemoryLayout(kvm_reserved_mem_percent=Decimal(0))),
    )
    record = shlex.split(target.write.await_args.args[1])[-1]
    assert "kvm_reserved_mem_percent=0" in record
    assert "kvm_capable=" not in record and "lpar_proc_compat_mode=" not in record
    target.hmc.get_console_info.assert_not_awaited()
    target.mode.assert_awaited_once_with(
        target.hmc, LAYOUT_SYSTEM, need_kvm=False, need_management=False
    )
    assert result.memory_layout_step.status == "ok"


@pytest.mark.asyncio
async def test_layout_ratio_admission_refuses_before_single_create(
    layout_creation_target,
):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    with pytest.raises(ValueError, match="not advertised"):
        await create_and_stamp_lpar(
            target.hmc, "selector", layout_creation(LparMemoryLayout(hpt_ratio="1:512"))
        )
    target.write.assert_not_awaited()
    target.stamp.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_controller_initial_profile_io_denied(layout_creation_target):
    from hmcpctl.documents.lpar_io import LparPhysicalIO
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar
    from hmcpctl.operations.lpar.linux_roles import RoleCapabilities

    target = layout_creation_target
    target.mode.return_value = RoleCapabilities(True, False, None, None, False)
    with pytest.raises(ValueError, match="initial-profile"):
        await create_and_stamp_lpar(
            target.hmc,
            "selector",
            layout_creation(
                LparMemoryLayout(hpt_ratio="1:128"),
                physical_io=LparPhysicalIO(console_slot="hmc"),
            ),
        )
    target.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_role_selection_retains_independent_kvm_admission(
    layout_creation_target,
):
    from hmcpctl.documents.lpar import LparSettings
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    with pytest.raises(ValueError, match="KVM selection requires"):
        await create_and_stamp_lpar(
            target.hmc,
            "selector",
            layout_creation(
                LparMemoryLayout(kvm_reserved_mem_percent=Decimal(0)),
                settings=LparSettings(kvm_capable=False),
            ),
        )
    target.mode.assert_awaited_once_with(
        target.hmc, LAYOUT_SYSTEM, need_kvm=True, need_management=False
    )
    target.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ok", "error", None])
async def test_layout_workflow_assignment_barrier(
    layout_creation_target, monkeypatch, status
):
    from unittest.mock import AsyncMock

    from hmcpctl.operations.lpar import workflows
    from hmcpctl.operations.lpar.assignments import (
        AssignmentResult,
        LparPcieAssignments,
    )
    from hmcpctl.operations.lpar.core import LparCreationResult
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    target = layout_creation_target
    creation = layout_creation(LparMemoryLayout(hpt_ratio="1:128"))
    step = None if status is None else WorkflowStep("memory_layout", status)
    monkeypatch.setattr(workflows, "prevalidate_lpar_pcie_assignments", AsyncMock())
    monkeypatch.setattr(
        workflows,
        "create_and_stamp_lpar",
        AsyncMock(
            return_value=LparCreationResult(
                True, target.entry, True, (), memory_layout_step=step
            )
        ),
    )
    apply = AsyncMock(return_value=AssignmentResult(True, True, ()))
    monkeypatch.setattr(workflows, "apply_validated_lpar_pcie_assignments", apply)
    result = await workflows.create_lpar(
        target.hmc, "selector", creation, LparPcieAssignments()
    )
    assert result.resource_created is True
    assert result.workflow_completed is (status == "ok")
    if status == "ok":
        apply.assert_awaited_once()
    else:
        apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed",
    ["hardware_memory", "security_policy", "settings", "reporting_settings", "vtpm"],
)
async def test_layout_preserves_each_mixed_workflow_barrier(
    layout_creation_target, monkeypatch, failed
):
    from unittest.mock import AsyncMock

    from hmcpctl.documents.lpar import LparReportingSettings, LparSettings
    from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
    from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
    from hmcpctl.operations.lpar import workflows
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.core import LparCreationResult
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    target = layout_creation_target
    creation = layout_creation(
        LparMemoryLayout(hpt_ratio="1:128"),
        hardware_mem_encryption=False,
        security_policy=LparSecurityPolicy(secure_boot=0),
        settings=LparSettings(allow_perf_collection=False),
        reporting_settings=LparReportingSettings(work_group_id=0),
        vtpm_settings=LparVtpmSettings(vtpm_enabled=False),
    )
    steps = {
        key: WorkflowStep(name, "error" if name == failed else "ok")
        for key, name in [
            ("hardware_memory_step", "hardware_memory"),
            ("security_policy_step", "security_policy"),
            ("settings_step", "settings"),
            ("reporting_step", "reporting_settings"),
            ("vtpm_step", "vtpm"),
            ("memory_layout_step", "memory_layout"),
        ]
    }
    monkeypatch.setattr(workflows, "prevalidate_lpar_pcie_assignments", AsyncMock())
    monkeypatch.setattr(
        workflows,
        "create_and_stamp_lpar",
        AsyncMock(
            return_value=LparCreationResult(True, target.entry, True, (), **steps)
        ),
    )
    apply = AsyncMock()
    monkeypatch.setattr(workflows, "apply_validated_lpar_pcie_assignments", apply)
    result = await workflows.create_lpar(
        target.hmc, "selector", creation, LparPcieAssignments()
    )
    assert result.resource_created and not result.workflow_completed
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_admission_read_failure_is_not_create_uncertainty(
    layout_creation_target,
):
    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    target.read.side_effect = HMCError("advertisement read unavailable")
    with pytest.raises(HMCError, match="advertisement read unavailable"):
        await create_and_stamp_lpar(
            target.hmc, "selector", layout_creation(LparMemoryLayout(hpt_ratio="1:128"))
        )
    assert target.hmc.find_partition_by_name.await_count == 1
    target.write.assert_not_awaited()
    target.stamp.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_os400_ratios_do_not_borrow_linux_role_prerequisites(
    layout_creation_target,
):
    import shlex
    from dataclasses import replace

    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    target.entry["Resource"]["PartitionType"] = "OS400"
    creation = replace(
        layout_creation(LparMemoryLayout(hpt_ratio="1:128")), partition_type="OS400"
    )
    result = await create_and_stamp_lpar(target.hmc, "selector", creation)
    assert "lpar_env=os400" in shlex.split(target.write.await_args.args[1])[-1]
    assert result.memory_layout_step.status == "ok"
    target.mode.assert_awaited_once_with(
        target.hmc, LAYOUT_SYSTEM, need_kvm=False, need_management=False
    )


@pytest.mark.asyncio
async def test_layout_invalid_os400_count_refused_before_traffic(
    layout_creation_target,
):
    from dataclasses import replace

    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    creation = replace(
        layout_creation(LparMemoryLayout(min_num_huge_pages=0)), partition_type="OS400"
    )
    with pytest.raises(ValueError, match="AIX/Linux"):
        await create_and_stamp_lpar(target.hmc, "selector", creation)
    assert target.hmc.mock_calls == []
    target.mode.assert_not_awaited()
    target.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_native_record_composes_existing_owners(layout_creation_target):
    import shlex

    from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
    from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
    from hmcpctl.ssh.lpar import create_lpar_via_cli

    target = layout_creation_target
    await create_lpar_via_cli(
        target.hmc.config,
        "sys-R1",
        "part-R1",
        memory_layout=LparMemoryLayout(hpt_ratio="1:128"),
        hardware_mem_encryption=False,
        security_policy=LparSecurityPolicy(secure_boot=0),
        vtpm_settings=LparVtpmSettings(vtpm_enabled=False),
    )
    parts = shlex.split(target.write.await_args.args[1])
    assert parts[:3] == ["mksyscfg", "-r", "lpar"]
    record = parts[-1].split(",")
    assert {
        "hpt_ratio=1:128",
        "hardware_mem_encryption=0",
        "secure_boot=0",
        "vtpm_enabled=0",
    } <= set(record)
    target.write.assert_awaited_once()


@pytest.mark.asyncio
async def test_layout_mixed_native_error_retains_code_without_provider_chain(
    layout_creation_target,
):
    import traceback

    import asyncssh

    from hmcpctl.ssh.lpar import create_lpar_via_cli
    from hmcpctl.ssh.transport import HMCCLIError, _process_error

    target = layout_creation_target
    target.write.side_effect = _process_error(
        "mksyscfg",
        asyncssh.ProcessError(
            env={},
            command="mksyscfg",
            subsystem=None,
            exit_status=1,
            exit_signal=None,
            returncode=1,
            stdout="",
            stderr="HSCLA4A0: private-provider-detail",
        ),
    )
    with pytest.raises(HMCCLIError, match="HSCLA4A0") as caught:
        await create_lpar_via_cli(
            target.hmc.config,
            "sys-R1",
            "part-R1",
            memory_layout=LparMemoryLayout(hpt_ratio="1:128"),
            hardware_mem_encryption=False,
        )
    assert "private-provider-detail" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert caught.value.__context__ is None
    assert caught.value.remote_refusal_code == "HSCLA4A0"
    target.write.assert_awaited_once()


@pytest.mark.asyncio
async def test_layout_workflow_controller_assignment_denial_before_prevalidation(
    layout_creation_target, monkeypatch
):
    from unittest.mock import AsyncMock

    from hmcpctl.operations.lpar import workflows
    from hmcpctl.operations.lpar.assignments import (
        DedicatedPcieAssignment,
        LparPcieAssignments,
    )
    from hmcpctl.operations.lpar.linux_roles import RoleCapabilities

    target = layout_creation_target
    mode = AsyncMock(return_value=RoleCapabilities(True, False, None, None, False))
    monkeypatch.setattr(
        workflows, "resolve_system_uuid", AsyncMock(return_value=LAYOUT_SYSTEM)
    )
    monkeypatch.setattr(workflows, "read_role_capabilities", mode)
    prevalidate = AsyncMock()
    monkeypatch.setattr(workflows, "prevalidate_lpar_pcie_assignments", prevalidate)
    create = AsyncMock()
    monkeypatch.setattr(workflows, "create_and_stamp_lpar", create)
    assignments = LparPcieAssignments(
        dedicated=(DedicatedPcieAssignment("default_profile", "21010010"),)
    )
    with pytest.raises(ValueError, match="controller mode"):
        await workflows.create_lpar(
            target.hmc,
            "selector",
            layout_creation(LparMemoryLayout(hpt_ratio="1:128")),
            assignments,
        )
    mode.assert_awaited_once_with(
        target.hmc, LAYOUT_SYSTEM, need_kvm=False, need_management=False
    )
    prevalidate.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_workflow_keylock_refused_before_assignment_preflight(
    layout_creation_target, monkeypatch
):
    from unittest.mock import AsyncMock

    from hmcpctl.operations.lpar import workflows
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments

    target = layout_creation_target
    prevalidate = AsyncMock()
    monkeypatch.setattr(workflows, "prevalidate_lpar_pcie_assignments", prevalidate)
    with pytest.raises(ValueError, match="keylock"):
        await workflows.create_lpar(
            target.hmc,
            "selector",
            layout_creation(LparMemoryLayout(hpt_ratio="1:128"), keylock="normal"),
            LparPcieAssignments(),
        )
    prevalidate.assert_not_awaited()
    assert target.hmc.mock_calls == []


@pytest.mark.asyncio
async def test_layout_stamp_failure_blocks_apply(layout_creation_target):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    target.stamp.return_value = (False, ("stamp unavailable",))
    result = await create_and_stamp_lpar(
        target.hmc,
        "selector",
        layout_creation(LparMemoryLayout(hpt_ratio="1:128"), apply_profile=True),
    )
    assert result.resource_created and result.ownership_stamped is False
    assert result.memory_layout_step.status == "ok"
    assert result.apply_step.status == "skipped"
    target.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_does_not_prevalidate_current_huge_pool(layout_creation_target):
    from hmcpctl.operations.lpar.core import create_and_stamp_lpar

    target = layout_creation_target
    target.hmc.get_managed_system.return_value["Resource"][
        "AssociatedSystemMemoryConfiguration"
    ].update(CurrentAvailableHugePages="0")
    result = await create_and_stamp_lpar(
        target.hmc,
        "selector",
        layout_creation(LparMemoryLayout(desired_num_huge_pages=1)),
    )
    assert result.memory_layout_step.status == "ok"
    target.write.assert_awaited_once()


@pytest.mark.asyncio
async def test_layout_workflow_stamp_failure_blocks_assignments(
    layout_creation_target, monkeypatch
):
    from unittest.mock import AsyncMock

    from hmcpctl.operations.lpar import workflows
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.core import LparCreationResult
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    target = layout_creation_target
    monkeypatch.setattr(workflows, "prevalidate_lpar_pcie_assignments", AsyncMock())
    monkeypatch.setattr(
        workflows,
        "create_and_stamp_lpar",
        AsyncMock(
            return_value=LparCreationResult(
                True,
                target.entry,
                False,
                (),
                memory_layout_step=WorkflowStep("memory_layout", "ok"),
            )
        ),
    )
    apply = AsyncMock()
    monkeypatch.setattr(workflows, "apply_validated_lpar_pcie_assignments", apply)
    result = await workflows.create_lpar(
        target.hmc,
        "selector",
        layout_creation(LparMemoryLayout(hpt_ratio="1:128")),
        LparPcieAssignments(),
    )
    assert result.resource_created and not result.workflow_completed
    apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["settings", "reporting", "vtpm"])
@pytest.mark.parametrize("selected", [True, False])
async def test_layout_ancillary_provider_errors_are_private(
    layout_creation_target, monkeypatch, setting, selected
):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from hmcpctl.documents.lpar import (
        LparReportingSettings,
        LparResources,
        LparSettings,
    )
    from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar import core, workflows
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.vtpm import VtpmCapabilities

    target = layout_creation_target
    options = {
        "settings": {"settings": LparSettings(allow_perf_collection=False)},
        "reporting": {"reporting_settings": LparReportingSettings(work_group_id=0)},
        "vtpm": {"vtpm_settings": LparVtpmSettings(vtpm_enabled=False)},
    }
    getters = {
        "settings": "get_lpar_settings",
        "reporting": "get_lpar_reporting_settings",
        "vtpm": "get_lpar_vtpm_state",
    }
    monkeypatch.setattr(
        core,
        getters[setting],
        AsyncMock(side_effect=HMCError("private-provider-marker")),
    )
    monkeypatch.setattr(
        core,
        "read_vtpm_capabilities",
        AsyncMock(return_value=VtpmCapabilities(("1.2",), ("RSA2048",), False)),
    )
    creation = replace(
        layout_creation(
            LparMemoryLayout(hpt_ratio="1:128") if selected else None,
            apply_profile=True if selected else None,
            **options[setting],
        ),
        resources=LparResources(),
    )
    assignments = AsyncMock()
    monkeypatch.setattr(workflows, "apply_validated_lpar_pcie_assignments", assignments)
    result = await workflows.create_lpar(
        target.hmc, "selector", creation, LparPcieAssignments()
    )
    assert result.resource_created and not result.workflow_completed
    assert any(step.status == "error" for step in result.steps)
    if selected:
        assert "private-provider-marker" not in str(result)
        target.apply.assert_not_awaited()
        assignments.assert_not_awaited()
    else:
        assert "private-provider-marker" in str(result)


@pytest.mark.parametrize("generation,line", [("p10", 41), ("p11", 49)])
@pytest.mark.parametrize(
    "selector", [*COUNT_FIELDS, "hpt_ratio", "ppt_ratio", "kvm_reserved_mem_percent"]
)
def test_twelve_creation_reference_bindings(generation, line, selector):
    import hashlib
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (
        root
        / "docs"
        / "refs"
        / f"hmc-commands-{generation}"
        / "commands"
        / "mksyscfg.md"
    )
    if not source.is_file():
        pytest.skip(
            f"Local {generation} mksyscfg corpus absent; source binding is unevidenced"
        )
    inventory = json.loads((root / "docs/capabilities/corpora.json").read_text())
    topic_id = f"commands-{generation}:commands/mksyscfg"
    topic = next(item for item in inventory["topics"] if item["id"] == topic_id)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == topic["sha256"].replace(
        "-", ""
    )
    unit = next(
        item
        for item in inventory["source_units"]
        if item["id"] == f"{topic_id}:L{line:05}:command-option"
    )
    assert unit["line"] == line and unit["text"] == "-f"
    assert unit["accounting"] == {"id": "cli:commands/mksyscfg", "kind": "row"}
    text = source.read_text().splitlines()[line - 1]
    assert text.startswith("| **-f** |")
    assert f"[**{selector}**]" in text


def test_memory_layout_exact_field_scope():
    import json
    from pathlib import Path

    expected = {*COUNT_FIELDS, "hpt_ratio", "ppt_ratio", "kvm_reserved_mem_percent"}
    assert {item.name for item in fields(LparMemoryLayout)} == expected
    root = Path(__file__).resolve().parents[2]
    catalog = json.loads((root / "docs/capabilities/maturity.json").read_text())
    variants = {
        row["operation"]: {
            scope["variant"]: scope
            for scope in row["implementation"]["implemented_scope"]
        }
        for row in catalog["operations"]
        if row["operation"] in {"lpar.create", "vios.create"}
    }
    lpar = variants["lpar.create"]["c06-creation-memory-layout"]
    vios = variants["vios.create"]["c06-vios-page-table-ratios"]
    assert {item["name"] for item in lpar["parameters"]} == {
        f"memory_layout.{name}" for name in expected
    }
    assert {item["name"] for item in vios["parameters"]} == {
        "memory_layout.hpt_ratio",
        "memory_layout.ppt_ratio",
    }
