"""C03 typed inputs, advertised admission and single-write partition outcomes."""

import csv
import io
import shlex
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
from hmcpctl.operations.lpar.vtpm import (
    VtpmCapabilities,
    admit_vtpm,
    configure_lpar_vtpm,
    read_lpar_vtpm,
    read_vtpm_capabilities,
)
from hmcpctl.ssh.lpar_vtpm import (
    get_lpar_vtpm_state,
    get_vtpm_advertisement,
    set_lpar_vtpm_settings,
)
from hmcpctl.ssh.transport import HMCCLIError

CONFIG = HMCConfig.from_mapping({})
DISABLED = LparVtpmSettings(False)
ENABLED = LparVtpmSettings(True, "1.2")
CAPABILITIES = VtpmCapabilities(("1.2", "2.0"), ("Power10v1", "Power10v2"), True)


def test_strict_typed_request_preserves_omission_and_false():
    assert LparVtpmSettings().pairs() == []
    assert DISABLED.pairs() == [("vtpm_enabled", 0)]
    assert LparVtpmSettings(True, "2.0", "Power10v2").pairs() == [
        ("vtpm_enabled", 1),
        ("vtpm_version", "2.0"),
        ("vtpm_encryption", "Power10v2"),
    ]
    with pytest.raises(ValidationError):
        TypeAdapter(LparVtpmSettings).validate_python({"unexpected": True})


@pytest.mark.parametrize(
    "field,value",
    [
        ("vtpm_enabled", 0),
        ("vtpm_enabled", 1),
        ("vtpm_enabled", "false"),
        ("vtpm_version", 1.2),
        ("vtpm_version", True),
        ("vtpm_version", ""),
        ("vtpm_version", "null"),
        ("vtpm_version", " 1.2"),
        ("vtpm_encryption", "none"),
        ("vtpm_encryption", "Power10v2 "),
        ("vtpm_version", "1.2,name=other"),
        ("vtpm_encryption", 'Power10v1"'),
        ("vtpm_version", "1.2\n"),
        ("vtpm_version", "1.2\\"),
    ],
)
def test_request_refuses_untyped_sentinel_or_record_input(field, value):
    with pytest.raises((ValueError, TypeError)):
        LparVtpmSettings(**{field: value})
    with pytest.raises(ValidationError):
        TypeAdapter(LparVtpmSettings).validate_python({field: value})


@pytest.mark.parametrize("corpus", ["p10", "p11"])
@pytest.mark.parametrize("command", ["mksyscfg", "chsyscfg"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("vtpm_enabled", False),
        ("vtpm_version", "1.2"),
        ("vtpm_encryption", "Power10v2"),
    ],
)
def test_reference_bindings(corpus, command, field, value):
    source = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "refs"
        / f"hmc-commands-{corpus}"
        / "commands"
        / f"{command}.md"
    )
    if source.exists():
        text = source.read_text()
        partition_start = text.index("Attribute names for partitions")
        profile_start = text.index(
            "Attribute names for partition profiles", partition_start
        )
        section = text[partition_start:profile_start]
        start = section.index(f"**{field}**")
        fragment = section[start : start + 650]
        assert "AIX, Linux, and VIOS only" in fragment
        if field != "vtpm_enabled":
            assert "supported_vtpm_" in fragment
    settings = LparVtpmSettings(**{field: value})
    assert settings.pairs() == [(field, int(value) if type(value) is bool else value)]


@pytest.mark.asyncio
async def test_advertisement_preserves_real_null_and_quoted_list_shapes(monkeypatch):
    run = AsyncMock(side_effect=["1.2,null\n", '"1.2,2.0","Power10v1,Power10v2"\n'])
    monkeypatch.setattr("hmcpctl.ssh.lpar_vtpm.run_hmc_command", run)
    assert await get_vtpm_advertisement(CONFIG, "system;quoted") == (("1.2",), ())
    assert await get_vtpm_advertisement(CONFIG, "system;quoted") == (
        CAPABILITIES.versions,
        CAPABILITIES.encryptions,
    )
    assert shlex.split(run.await_args.args[1]) == [
        "lssyscfg",
        "-r",
        "sys",
        "-m",
        "system;quoted",
        "-F",
        "supported_vtpm_versions,supported_vtpm_encryptions",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "1.2",
        "1.2,null,extra",
        "1.2,null\n1.2,null",
        '"1.2,1.2",null',
        '"1.2,",null',
        '"1.2, 2.0",null',
        '"1.2,2.0",',
        '"1.2,2.0',
    ],
)
async def test_advertisement_rejects_ambiguous_shapes(monkeypatch, raw):
    monkeypatch.setattr(
        "hmcpctl.ssh.lpar_vtpm.run_hmc_command", AsyncMock(return_value=raw)
    )
    with pytest.raises(HMCCLIError):
        await get_vtpm_advertisement(CONFIG, "sys-R1")


@pytest.mark.asyncio
@pytest.mark.parametrize("vios,environment", [(False, "aixlinux"), (True, "vioserver")])
async def test_reader_matches_selected_name_type_and_real_disabled_sentinels(
    monkeypatch, vios, environment
):
    run = AsyncMock(
        return_value=f'"part;quoted",{environment},Not Activated,0,null,null\n'
    )
    monkeypatch.setattr("hmcpctl.ssh.lpar_vtpm.run_hmc_command", run)
    observed = await get_lpar_vtpm_state(CONFIG, "sys;quoted", "part;quoted", vios=vios)
    assert observed.settings == DISABLED
    assert observed.state == "Not Activated"
    assert observed.environment == environment
    args = shlex.split(run.await_args.args[1])
    assert args[-1] == "name,lpar_env,state,vtpm_enabled,vtpm_version,vtpm_encryption"
    assert args[args.index("--filter") + 1] == "lpar_names=part;quoted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "p,aixlinux,Not Activated,0,null",
        "p,aixlinux,Not Activated,0,null,null,extra",
        "p,aixlinux,Not Activated,2,null,null",
        "p,os400,Not Activated,0,null,null",
        "p,vioserver,Not Activated,0,null,null",
        "other,aixlinux,Not Activated,0,null,null",
        "p,aixlinux,,0,null,null",
        "p,aixlinux,Not Activated,0,,null",
        "p,aixlinux,Not Activated,0,null,null\np,aixlinux,Not Activated,0,null,null",
    ],
)
async def test_reader_refuses_missing_malformed_foreign_or_wrong_kind(monkeypatch, raw):
    monkeypatch.setattr(
        "hmcpctl.ssh.lpar_vtpm.run_hmc_command", AsyncMock(return_value=raw)
    )
    with pytest.raises(HMCCLIError):
        await get_lpar_vtpm_state(CONFIG, "sys-R1", "p")


@pytest.mark.asyncio
async def test_writer_is_one_fixed_record_not_an_arbitrary_proxy(monkeypatch):
    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar_vtpm.run_hmc_command", run)
    await set_lpar_vtpm_settings(CONFIG, "sys;quoted", "part;quoted", DISABLED)
    assert run.await_count == 1
    assert shlex.split(run.await_args.args[1]) == [
        "chsyscfg",
        "-r",
        "lpar",
        "-m",
        "sys;quoted",
        "-i",
        "name=part;quoted,vtpm_enabled=0",
    ]
    with pytest.raises((ValueError, HMCCLIError)):
        await set_lpar_vtpm_settings(CONFIG, "s", "bad,name", DISABLED)
    assert run.await_count == 1


@pytest.mark.parametrize(
    "requested,before,capabilities",
    [
        (LparVtpmSettings(vtpm_version="9.0"), None, CAPABILITIES),
        (LparVtpmSettings(vtpm_encryption="other"), ENABLED, CAPABILITIES),
        (LparVtpmSettings(vtpm_encryption="Power10v2"), ENABLED, CAPABILITIES),
        (LparVtpmSettings(True, "1.2", "Power10v1"), None, CAPABILITIES),
        (LparVtpmSettings(vtpm_encryption="Power10v1"), None, CAPABILITIES),
        (
            LparVtpmSettings(True, "2.0"),
            None,
            replace(CAPABILITIES, version20_capable=False),
        ),
    ],
)
def test_admission_rejects_unadvertised_or_incompatible_before_effects(
    requested, before, capabilities
):
    with pytest.raises(ValueError):
        admit_vtpm(requested, capabilities, before)


def test_admission_uses_selected_system_not_a_universal_encryption_enum():
    caps = VtpmCapabilities(("2.0",), ("AdvertisedLevel",), True)
    admit_vtpm(LparVtpmSettings(True, "2.0", "AdvertisedLevel"), caps)
    admit_vtpm(DISABLED, CAPABILITIES, ENABLED)
    admit_vtpm(
        LparVtpmSettings(vtpm_encryption="Power10v2"),
        CAPABILITIES,
        LparVtpmSettings(True, "2.0", "Power10v1"),
    )


@pytest.fixture
def boundary(monkeypatch):
    state = SimpleNamespace(
        settings=DISABLED,
        after=None,
        state="Not Activated",
        environment="aixlinux",
        commands=[],
        write_error=None,
        after_error=False,
        wrote=False,
        advertisement="1.2,null\n",
        expected_name="p",
    )
    hmc = SimpleNamespace(
        config=CONFIG,
        get_managed_system=AsyncMock(
            return_value={
                "Resource": {
                    "SystemCapabilities": {
                        "VirtualTrustedPlatformModuleCapable": "true",
                        "VirtualTrustedPlatformModule20Capable": {
                            "text": "false",
                            "@attrs": {"ksv": "V1_12_0"},
                        },
                    }
                }
            }
        ),
    )
    resolve = AsyncMock(return_value=("sys-R1", "p"))
    vios_resolve = AsyncMock(return_value=("sys-R1", "p"))
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.vtpm.resolve_system_uuid",
        AsyncMock(return_value="system-id"),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.vtpm.resolve_and_authorize_lpar_names", resolve
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.vtpm._resolve_and_authorize_vios_names", vios_resolve
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.vtpm._resolve_contained_lpar_names", resolve
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.vtpm._resolve_system_name",
        AsyncMock(return_value="sys-R1"),
    )

    async def run(config, command):
        state.commands.append(command)
        parts = shlex.split(command)
        if parts[0] == "chsyscfg":
            state.wrote = True
            if state.write_error:
                raise state.write_error
            record = parts[parts.index("-i") + 1]
            pairs = dict(item.split("=", 1) for item in record.split(","))
            changes = {
                k: (v == "1" if k == "vtpm_enabled" else v)
                for k, v in pairs.items()
                if k != "name"
            }
            state.settings = replace(state.settings, **changes)
            return ""
        if parts[parts.index("-r") + 1] == "sys":
            return state.advertisement
        if state.wrote and state.after_error:
            raise HMCCLIError("readback failed")
        settings = (
            state.after if state.wrote and state.after is not None else state.settings
        )
        row = [
            state.expected_name,
            state.environment,
            state.state,
            "1" if settings.vtpm_enabled else "0",
            settings.vtpm_version or "null",
            settings.vtpm_encryption or "null",
        ]
        buffer = io.StringIO()
        csv.writer(buffer).writerow(row)
        return buffer.getvalue()

    monkeypatch.setattr("hmcpctl.ssh.lpar_vtpm.run_hmc_command", run)
    return SimpleNamespace(
        hmc=hmc, state=state, resolve=resolve, vios_resolve=vios_resolve
    )


@pytest.mark.asyncio
async def test_full_transport_reader_and_single_update_verify_explicit_fields(boundary):
    b = boundary
    result = await configure_lpar_vtpm(
        b.hmc, "p", ENABLED, system_name_or_uuid="sys-R1"
    )
    assert result.status == "verified"
    assert result.requested == ENABLED and result.observed == ENABLED
    assert len([c for c in b.state.commands if c.startswith("chsyscfg")]) == 1
    b.resolve.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "running",
        "unacknowledged",
        "combined-replacement",
        "empty",
        "strict-ack",
        "denied",
        "foreign",
    ],
)
async def test_pre_effect_gates_refuse_without_a_write(boundary, kind):
    b = boundary
    settings = DISABLED
    ack = False
    b.state.settings = ENABLED
    if kind == "running":
        b.state.state = "Running"
        ack = True
    if kind == "combined-replacement":
        settings = LparVtpmSettings(False, "2.0")
        ack = True
        b.state.advertisement = '"1.2,2.0","Power10v1,Power10v2"'
        b.hmc.get_managed_system.return_value["Resource"]["SystemCapabilities"][
            "VirtualTrustedPlatformModule20Capable"
        ] = "true"
    if kind == "empty":
        settings = LparVtpmSettings()
    if kind == "strict-ack":
        ack = "true"
    if kind == "denied":
        b.resolve.side_effect = PermissionError("foreign owner")
    if kind == "foreign":
        b.state.expected_name = "other"
    with pytest.raises((ValueError, TypeError, PermissionError, HMCCLIError)):
        await configure_lpar_vtpm(
            b.hmc,
            "p",
            settings,
            system_name_or_uuid="sys-R1",
            acknowledge_data_loss=ack,
        )
    assert not b.state.wrote


@pytest.mark.asyncio
async def test_explicit_loss_consent_is_separate_from_ownership_override(boundary):
    b = boundary
    b.state.settings = ENABLED
    result = await configure_lpar_vtpm(
        b.hmc, "p", DISABLED, system_name_or_uuid="sys-R1", acknowledge_data_loss=True
    )
    assert result.status == "verified" and result.observed == replace(
        ENABLED, vtpm_enabled=False
    )
    assert b.resolve.await_args.kwargs["ownership_override"] is False
    assert "acknowledge" not in next(
        c for c in b.state.commands if c.startswith("chsyscfg")
    )


@pytest.mark.asyncio
async def test_disabled_noop_needs_no_destructive_ack_but_enabled_replacement_refuses(
    boundary,
):
    b = boundary
    assert (
        await configure_lpar_vtpm(b.hmc, "p", DISABLED, system_name_or_uuid="sys-R1")
    ).status == "verified"
    b.state.wrote = False
    b.state.settings = ENABLED
    with pytest.raises(ValueError):
        await configure_lpar_vtpm(
            b.hmc,
            "p",
            LparVtpmSettings(vtpm_version="2.0"),
            system_name_or_uuid="sys-R1",
            acknowledge_data_loss=True,
        )
    assert not b.state.wrote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected",
    [
        ("unknown", "write-outcome-unknown"),
        ("unavailable", "readback-unavailable"),
        ("requested", "mismatch"),
        ("preserved", "mismatch"),
    ],
)
async def test_uncertainty_and_drift_never_replay_or_claim_verified(
    boundary, mode, expected
):
    b = boundary
    if mode == "unknown":
        b.state.write_error = HMCCLIError("connection lost")
    if mode == "unavailable":
        b.state.after_error = True
    if mode == "requested":
        b.state.after = DISABLED
    if mode == "preserved":
        b.state.after = replace(ENABLED, vtpm_encryption="ChangedLevel")
    result = await configure_lpar_vtpm(
        b.hmc, "p", LparVtpmSettings(True), system_name_or_uuid="sys-R1"
    )
    assert result.status == expected and result.warnings
    assert len([c for c in b.state.commands if c.startswith("chsyscfg")]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", [None, 1, "false", "yes", {}, {"text": "TRUE"}])
async def test_capability_absence_false_or_unknown_never_admits(boundary, capability):
    b = boundary
    b.hmc.get_managed_system.return_value = {
        "Resource": {
            "SystemCapabilities": {"VirtualTrustedPlatformModuleCapable": capability}
        }
    }
    with pytest.raises((ValueError, HMCCLIError)):
        await configure_lpar_vtpm(b.hmc, "p", ENABLED, system_name_or_uuid="sys-R1")
    assert not b.state.wrote


@pytest.mark.asyncio
async def test_vios_update_uses_its_existing_contained_authorization(boundary):
    b = boundary
    b.state.environment = "vioserver"
    result = await configure_lpar_vtpm(
        b.hmc, "p", ENABLED, system_name_or_uuid="sys-R1", vios=True
    )
    assert result.status == "verified"
    b.vios_resolve.assert_awaited_once()
    b.resolve.assert_not_awaited()
    b.state.wrote = False
    b.vios_resolve.side_effect = PermissionError("foreign owner")
    with pytest.raises(PermissionError):
        await configure_lpar_vtpm(
            b.hmc,
            "p",
            DISABLED,
            system_name_or_uuid="sys-R1",
            vios=True,
            acknowledge_data_loss=True,
        )
    assert not b.state.wrote


@pytest.mark.asyncio
async def test_read_capability_and_contained_state_without_a_write(boundary):
    b = boundary
    assert await read_vtpm_capabilities(b.hmc, "sys-R1") == VtpmCapabilities(
        ("1.2",), (), False
    )
    observed = await read_lpar_vtpm(b.hmc, "p", system_name_or_uuid="sys-R1")
    assert observed.settings == DISABLED
    assert not b.state.wrote


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [None, {"Resource": None}, {"Resource": []}, {"Resource": {"PartitionName": ""}}],
)
async def test_vios_read_refuses_missing_or_malformed_identity(
    boundary, monkeypatch, entry
):
    b = boundary
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.vtpm.resolve_vios_uuid",
        AsyncMock(return_value="vios-id"),
    )
    b.hmc.get_vios = AsyncMock(return_value=entry)
    with pytest.raises(ValueError, match="VIOS"):
        await read_lpar_vtpm(b.hmc, "p", system_name_or_uuid="sys-R1", vios=True)
    assert not b.state.commands
