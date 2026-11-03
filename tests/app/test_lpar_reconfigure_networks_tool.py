"""hmc_reconfigure_lpar's network patch over MCP under real access policies (ADR 0325)."""

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
from unit.test_lpar_reconfigure_networks import MAC_A
from unit.test_lpar_reconfigure_networks import body as hmc  # noqa: F401 - a fixture

ADD, DELETE = "hmc_add_network_adapter", "hmc_delete_adapter"
SAVE = "hmc_save_current_partition_profile"
ADDS = {
    **NEW,
    "patch": {"networks": {"add": [{"port_vlan_id": 30}]}},
    "exclusive_writer_window": True,
}
REMOVES = {
    **NEW,
    "patch": {"networks": {"remove": [{"mac": MAC_A}]}},
    "exclusive_writer_window": True,
}


def test_an_add_runs_with_its_delegated_authority(hmc):  # noqa: F811
    record = _call(_app(_grant(TOOL, ADD, SAVE)), ADDS)
    assert (record["state"], record["outcome"]) == ("terminal", "completed")
    assert record["result"]["changes"][0]["field"] == "networks.add"


def test_a_removal_runs_with_its_delegated_authority(hmc):  # noqa: F811
    record = _call(_app(_grant(TOOL, DELETE, SAVE)), REMOVES)
    assert record["outcome"] == "completed"
    assert [a["UUID"] for a in hmc.adapters] == ["UB"]


def test_networks_without_the_writer_window_are_refused_before_recording(hmc):  # noqa: F811
    arguments = {**ADDS, "exclusive_writer_window": False}
    with pytest.raises(ToolError, match="exclusive_writer_window"):
        _call(_app(_grant(TOOL, ADD, SAVE)), arguments)
    assert hmc.calls == []


@pytest.mark.parametrize(
    ("arguments", "grants", "missing"),
    [
        (ADDS, (SAVE,), ADD),
        (ADDS, (ADD,), SAVE),
        (REMOVES, (SAVE, ADD), DELETE),
        (REMOVES, (DELETE,), SAVE),
    ],
)
def test_each_network_change_needs_its_delegated_tools(hmc, arguments, grants, missing):  # noqa: F811
    with pytest.raises(ToolError, match=missing):
        _call(_app(_grant(TOOL, *grants)), arguments)
    assert hmc.calls == []
