"""hmc_reconfigure_lpar's disk patch over MCP under real access policies (ADR 0322)."""

from __future__ import annotations

import pytest
from app.test_lpar_reconfigure_tool import (  # noqa: F401 - autouse fixture
    NEW,
    TOOL,
    _app,
    _call,
    _grant,
    environment_only_config,
)
from fastmcp.exceptions import ToolError
from unit.test_lpar_reconfigure import hmc as base_hmc  # noqa: F401 - a fixture
from unit.test_lpar_reconfigure_disks import ADD, VIOS
from unit.test_lpar_reconfigure_disks import hmc as hmc  # noqa: PLC0414 - a fixture

CREATE, MAP = "hmc_create_virtual_disk", "hmc_map_storage_to_lpar"
SAVE, DETACH = "hmc_save_current_partition_profile", "hmc_detach_storage_mapping"
ADDS = {**NEW, "patch": {"disks": {"add": [ADD]}}, "exclusive_writer_window": True}
DETACHES = {
    **NEW,
    "patch": {"disks": {"detach": [{"vios_uuid": VIOS, "name": "root1"}]}},
    "exclusive_writer_window": True,
    "allow_disruption": True,
}


def test_an_add_runs_with_its_delegated_authority(hmc):
    record = _call(_app(_grant(TOOL, CREATE, MAP, SAVE)), ADDS)
    assert (record["state"], record["outcome"]) == ("terminal", "completed")
    assert record["result"]["changes"][0]["field"] == "disks.add"


def test_disks_without_the_writer_window_are_refused_before_recording(hmc):
    arguments = {**ADDS, "exclusive_writer_window": False}
    with pytest.raises(ToolError, match="exclusive_writer_window"):
        _call(_app(_grant(TOOL, CREATE, MAP, SAVE)), arguments)
    assert hmc.calls == []


@pytest.mark.parametrize(
    ("arguments", "grants", "missing"),
    [
        (ADDS, (MAP, SAVE), CREATE),
        (ADDS, (CREATE, SAVE), MAP),
        (ADDS, (CREATE, MAP), SAVE),
        (DETACHES, (CREATE, MAP, SAVE), DETACH),
    ],
)
def test_each_disk_change_needs_its_delegated_tools(hmc, arguments, grants, missing):
    with pytest.raises(ToolError, match=missing):
        _call(_app(_grant(TOOL, *grants)), arguments)
    assert hmc.calls == []


def test_a_vios_the_specialist_grant_excludes_is_denied(hmc):
    app = _app(
        _grant(TOOL, MAP, SAVE),
        _grant(CREATE, targets={"vios": ["other-vios"], "managed_system": ["sysA"]}),
    )
    with pytest.raises(ToolError, match=CREATE):
        _call(app, ADDS)
    assert hmc.calls == []


def test_a_detach_runs_with_its_delegated_authority(hmc):
    record = _call(_app(_grant(TOOL, DETACH)), DETACHES)
    assert record["outcome"] == "completed"
    assert [m[0] for m in hmc.maps] == ["shared"]
