"""Twelve C09 source bindings pin finite facts to the retained P10/P11 units."""

import hashlib
import inspect
from pathlib import Path

import pytest

from hmcpctl.documents.lpar_start import LparStartSettings
from hmcpctl.jobs.requests import BOOT_MODES
from hmcpctl.operations.lpar.boot_order import set_lpar_boot_order
from hmcpctl.operations.vios.boot_order import set_vios_boot_order

ROOT = Path(__file__).resolve().parents[2]
UNITS = {
    ("p10", "mksyscfg"): (
        41,
        "7de4a82c08e2cefe5def7e2d587463d44517437e6381818a836539e7d7b5d021",  # pragma: allowlist secret - reference content SHA256
    ),
    ("p11", "mksyscfg"): (
        49,
        "f2ccafe54fcc30064c1a1d336a9929ddd6a51cfa9e4c3be7caddcc6d38239e28",  # pragma: allowlist secret - reference content SHA256
    ),
    ("p10", "chsyscfg"): (
        49,
        "e14024928bb85b1765350cbd4b84aeaf8c2903e2d66bdc8afca70a97107ad9dd",  # pragma: allowlist secret - reference content SHA256
    ),
    ("p11", "chsyscfg"): (
        49,
        "27a15965b418a3e7b5d990cfffd5978f1c8495944f98ee49675b2329c7d5fadb",  # pragma: allowlist secret - reference content SHA256
    ),
}
FIELDS = (
    "auto_start",
    "boot_mode",
    "power_ctrl_lpar_ids",
    "power_ctrl_lpar_names",
    "conn_monitoring",
    "boot_string",
)


@pytest.mark.parametrize("generation", ["p10", "p11"])
@pytest.mark.parametrize("field", FIELDS)
def test_twelve_c09_bindings(generation, field):
    import re

    command = "chsyscfg" if field == "boot_string" else "mksyscfg"
    path = ROOT / f"docs/refs/hmc-commands-{generation}/commands/{command}.md"
    if not path.is_file():
        pytest.skip(
            f"{generation}:{command}:{field} source binding UNEVIDENCED: retained local corpus absent"
        )
    raw = path.read_bytes()
    line, digest = UNITS[generation, command]
    assert hashlib.sha256(raw).hexdigest() == digest, (
        "Reference unit changed; reconcile facts before accepting new source"
    )
    clause = (
        raw.decode()
        .splitlines()[line - 1]
        .split(f"**{field}**", 1)[1]
        .split("[**", 1)[0]
    )
    if field in ("auto_start", "conn_monitoring"):
        assert set(re.findall(r"\*\*([01])\*\*", clause)) == {"0", "1"}
        for value in (False, True):
            assert (field, int(value)) in LparStartSettings(**{field: value}).pairs()
    elif field == "boot_mode":
        assert set(re.findall(r"\*\*([a-z]+)\*\*", clause)) == set(BOOT_MODES)
        for mode in BOOT_MODES:
            assert LparStartSettings(boot_mode=mode).pairs() == [(field, mode)]
    elif field == "boot_string":
        assert all(
            re.search(r"\b" + token + r"\b", clause)
            for token in ("AIX", "Linux", "VIOS")
        )
        for setter in (set_lpar_boot_order, set_vios_boot_order):
            assert inspect.signature(setter).parameters["boot_string"].default is None
    else:
        selectors = (7, 8) if field.endswith("ids") else ("peer-a", "peer-b")
        assert getattr(LparStartSettings(**{field: selectors}), field) == selectors
        with pytest.raises(ValueError):
            LparStartSettings(
                power_ctrl_lpar_ids=(7,), power_ctrl_lpar_names=("peer-a",)
            )
