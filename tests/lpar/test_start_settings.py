"""Creation-only boot/start values preserve omission and reject ambiguous input."""

import pytest
from pydantic import TypeAdapter, ValidationError

from hmcpctl.documents.lpar_start import LparStartSettings


@pytest.mark.parametrize("value", [None, False, True])
def test_strict_create_values(value):
    settings = LparStartSettings(auto_start=value, conn_monitoring=value)
    expected = (
        []
        if value is None
        else [("auto_start", int(value)), ("conn_monitoring", int(value))]
    )
    assert settings.pairs() == expected
    assert TypeAdapter(LparStartSettings).validate_python(
        {"auto_start": value}
    ) == LparStartSettings(auto_start=value)


@pytest.mark.parametrize("mode", ["norm", "dd", "ds", "of", "sms"])
def test_strict_create_values_modes(mode):
    settings = LparStartSettings(boot_mode=mode)
    settings.validate_creation("AIX/Linux")
    settings.validate_creation("Virtual IO Server")
    with pytest.raises(ValueError, match="OS400"):
        settings.validate_creation("OS400")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"auto_start": 0},
        {"auto_start": "false"},
        {"conn_monitoring": 1},
        {"boot_mode": "Normal"},
        {"power_ctrl_lpar_ids": (True,)},
        {"power_ctrl_lpar_ids": (0,)},
        {"power_ctrl_lpar_ids": ()},
        {"power_ctrl_lpar_ids": (2, 2)},
        {"power_ctrl_lpar_names": ("",)},
        {"power_ctrl_lpar_names": ("peer", "peer")},
        {"power_ctrl_lpar_names": ()},
        {"power_ctrl_lpar_ids": (2,), "power_ctrl_lpar_names": ("peer",)},
    ],
)
def test_strict_create_values_refuse(kwargs):
    with pytest.raises((ValueError, TypeError)):
        LparStartSettings(**kwargs)


@pytest.mark.parametrize(
    "kwargs", [{"auto_start": 0}, {"power_ctrl_lpar_ids": [True]}, {"unknown": 1}]
)
def test_typed_boundary_refuses_coercion(kwargs):
    with pytest.raises((ValidationError, ValueError)):
        TypeAdapter(LparStartSettings).validate_python(kwargs)


def test_names_are_exact_lookups_not_native_values():
    assert LparStartSettings(
        power_ctrl_lpar_names=('peer,"=',)
    ).power_ctrl_lpar_names == ('peer,"=',)
    assert LparStartSettings(auto_start=False).pairs() == [("auto_start", 0)]
