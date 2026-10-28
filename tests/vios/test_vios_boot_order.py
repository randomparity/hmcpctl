"""VIOS boot text uses contained identity and independent complete native CSV."""

import csv
import io
import shlex
from unittest.mock import AsyncMock

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.operations.vios import boot_order as boot

SYSTEM = "11111111-1111-4111-8111-111111111111"
VIOS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FIELDS = ["name", "lpar_id", "lpar_env", "uuid", "description", "pend_boot_string"]
VALUES = [
    "c09-plain",
    "c09,comma",
    "c09=equals",
    "c09'single",
    'c09"double',
    "c09\\slash",
    "c09 space",
    "c09,='\"\\ combined",
]


def header(text, **changes):
    data = dict(zip(FIELDS, ["vios-a", "7", "vioserver", VIOS, "", text], strict=True))
    data.update(changes)
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(FIELDS)
    writer.writerow(data.values())
    return out.getvalue()


@pytest.fixture
def target(monkeypatch):
    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "u", "agent_id": "agent"}
    )
    hmc.list_vios.return_value = [{"UUID": VIOS}]
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM, "SystemName": "system", "State": "operating"}
    ]
    hmc.get_vios.return_value = {
        "UUID": VIOS,
        "ResourceType": "VirtualIOServer",
        "Resource": {
            "PartitionName": "vios-a",
            "PartitionType": "Virtual IO Server",
            "PartitionID": "7",
            "Description": "",
        },
    }
    state = {"text": "baseline", "commands": [], "header": None, "failure": None}

    async def command(config, cmd, **kwargs):
        state["commands"].append(cmd)
        assert kwargs["max_output_bytes"] == 65536
        if cmd.startswith("chsyscfg"):
            if state["failure"]:
                raise TimeoutError("private-provider-data")
            cells = next(csv.reader(io.StringIO(shlex.split(cmd)[-1]), strict=True))
            assert cells[0] == "lpar_id=7"
            state["text"] = cells[1].split("=", 1)[1]
            return ""
        assert cmd.startswith("lssyscfg -r lpar") and cmd.endswith("-F --header")
        return state["header"] if state["header"] is not None else header(state["text"])

    monkeypatch.setattr(boot, "run_hmc_command", command)
    return hmc, state


@pytest.mark.asyncio
@pytest.mark.parametrize("text", VALUES, ids=range(8))
async def test_native_roundtrip_contract(target, text):
    hmc, state = target
    result = await boot.set_vios_boot_order(
        hmc, VIOS, system_name_or_uuid=SYSTEM, boot_string=text
    )
    assert result == {
        "vios_uuid": VIOS,
        "vios_name": "vios-a",
        "pending_boot_string": text,
    }
    assert sum(cmd.startswith("chsyscfg") for cmd in state["commands"]) == 1
    assert (
        await boot.read_vios_boot_order(hmc, VIOS, system_name_or_uuid=SYSTEM) == result
    )
    hmc.get_logical_partition.assert_not_called()


@pytest.mark.asyncio
async def test_observed_empty_is_null(target):
    hmc, state = target
    state["text"] = ""
    result = await boot.read_vios_boot_order(hmc, VIOS, system_name_or_uuid=SYSTEM)
    assert result["pending_boot_string"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "truncated",
        header("x", uuid=SYSTEM),
        header("x", name="other"),
        header("x", lpar_env="aixlinux"),
        header("x", lpar_id="8"),
        header("x") + "extra,row\n",
        "name,name\na,b\n",
        'name,lpar_id\n"unclosed,7\n',
        header("x").replace("pend_boot_string", "missing"),
        header("x\ufffd"),
    ],
    ids=range(10),
)
async def test_native_header_rejects_lossy_missing_or_foreign(target, bad):
    hmc, state = target
    state["header"] = bad
    with pytest.raises(HMCError):
        await boot.read_vios_boot_order(hmc, VIOS, system_name_or_uuid=SYSTEM)


@pytest.mark.asyncio
async def test_unknown_native_write_has_no_retry_or_false_success(target):
    hmc, state = target
    state["failure"] = True
    with pytest.raises(HMCError, match="unverified") as exc:
        await boot.set_vios_boot_order(
            hmc, VIOS, system_name_or_uuid=SYSTEM, boot_string="raw"
        )
    assert "private-provider-data" not in str(exc.value)
    assert len(state["commands"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["foreign-owner", "identity-drift", "missing-description", "invalid-input"]
)
async def test_native_admission_refuses_before_write(target, fault):
    from copy import deepcopy

    hmc, state = target
    if fault == "foreign-owner":
        hmc.get_vios.return_value["Resource"]["Description"] = (
            "[hmcpctl owner:other created:2026-10-28]"
        )
    elif fault == "identity-drift":
        first = deepcopy(hmc.get_vios.return_value)
        second = deepcopy(first)
        second["Resource"]["PartitionName"] = "changed"
        hmc.get_vios.side_effect = [first, second]
    elif fault == "missing-description":
        hmc.get_vios.return_value["Resource"].pop("Description")
    with pytest.raises((HMCError, PermissionError, ValueError)):
        await boot.set_vios_boot_order(
            hmc,
            VIOS,
            system_name_or_uuid=SYSTEM,
            boot_string="\n" if fault == "invalid-input" else "raw",
        )
    assert state["commands"] == []


@pytest.mark.asyncio
async def test_write_acknowledgment_is_not_independent_text_proof(target):
    hmc, state = target
    state["header"] = header("different")
    with pytest.raises(HMCError, match="mismatch"):
        await boot.set_vios_boot_order(
            hmc, VIOS, system_name_or_uuid=SYSTEM, boot_string="raw"
        )
    assert sum(cmd.startswith("chsyscfg") for cmd in state["commands"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control", ["\u0085", "\u202e"], ids=["next-line", "bidi-override"]
)
async def test_native_header_refuses_nonprintable_unicode_cells(target, control):
    hmc, state = target
    state["header"] = header("x" + control)
    with pytest.raises(HMCError):
        await boot.read_vios_boot_order(hmc, VIOS, system_name_or_uuid=SYSTEM)


@pytest.mark.parametrize(
    "identity,text",
    [(True, "x"), (0, "x"), (-1, "x"), (7, ""), (7, "  "), (7, "x\n"), (7, "x\u202e")],
    ids=range(7),
)
def test_fixed_two_cell_encoder_refuses_invalid_input(identity, text):
    with pytest.raises((TypeError, ValueError)):
        boot._encode_boot_record(identity, text)


@pytest.mark.asyncio
async def test_native_printable_unicode_and_spacing_are_lossless(target):
    hmc, _state = target
    text = "  λ café  "
    result = await boot.set_vios_boot_order(
        hmc, VIOS, system_name_or_uuid=SYSTEM, boot_string=text
    )
    assert result["pending_boot_string"] == text
