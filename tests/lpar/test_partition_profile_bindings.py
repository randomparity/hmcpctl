"""All five assigned rows retain their 170 independent source units and truthful maturity."""

from __future__ import annotations

import json
from pathlib import Path

from hmcpctl.server_tools.lpar import profiles as tools

ROOT = Path(__file__).parents[2]


def test_all_five_rows_and_170_units_have_complete_owned_route_bindings():
    contract = json.loads(
        (
            ROOT / "tests/fixtures/partition_profiles/reference-contracts.json"
        ).read_text()
    )
    rows = {
        r["id"]: r
        for r in json.loads((ROOT / "docs/capabilities/rows.json").read_text())["rows"]
    }
    operations = {
        r["operation"]: r
        for r in json.loads((ROOT / "docs/capabilities/operations.json").read_text())[
            "operations"
        ]
    }
    maturity = {
        r["operation"]: r
        for r in json.loads((ROOT / "docs/capabilities/maturity.json").read_text())[
            "operations"
        ]
    }
    security = tools.tool_security()
    units = set()
    assigned = {
        id
        for id, row in rows.items()
        if row.get("disposition", {}).get("issue") == 1724
    }
    assert assigned == set(contract["bindings"]) and len(assigned) == 5
    for id, binding in contract["bindings"].items():
        assert binding["source_units"] == rows[id]["source_units"]
        assert set(binding["source_units"]) == set(rows[id]["modes"]) | set(
            rows[id]["parameters"]
        )
        assert (
            binding["modes"] == rows[id]["modes"]
            and binding["parameters"] == rows[id]["parameters"]
        )
        assert {u.split(":", 1)[0] for u in binding["source_units"]} == {
            "rest-p10",
            "rest-p11",
        }
        units.update(binding["source_units"])
    assert len(units) == 170
    profile_row = "rest:managed-system/logical-partition-profile"
    for family, kind in [("lpar_profile", "lpar"), ("vios_profile", "vios")]:
        for action in [
            "list",
            "read",
            "update",
            "save_current",
            "set_default",
            "delete",
        ]:
            op = family + "." + action
            assert op in operations, op
            assert operations[op]["row_ids"]
            assert set(operations[op]["row_ids"]) <= assigned
            assert maturity[op]["implementation"]["state"] == "implemented"
            assert maturity[op]["implementation"]["missing_scope"] == []
            assert maturity[op]["evidence"] == []
            assert security[operations[op]["tool"]].target_kind == kind
            if action in {"list", "read", "update", "delete"}:
                assert operations[op]["row_ids"] == [profile_row]
            if action in {"save_current", "set_default"}:
                fragment = (
                    "savecurrentconfig"
                    if action == "save_current"
                    else "changedefaultprofilename"
                )
                assert any(fragment in row for row in operations[op]["row_ids"])
