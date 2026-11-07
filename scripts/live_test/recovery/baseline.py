"""Independent baseline recovery admission and read-only witnesses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hmcpctl.config import HMCConfig
from live_test import baseline as independent_baseline
from live_test import config as live_config
from live_test import runner
from live_test.observation import plain_data
from live_test.profiles import ARTIFACT_PREFIX, backup_artifact_names
from live_test.recovery.common import Finding, StateUnreadable


@dataclass(frozen=True)
class Round2Inputs:
    """Fresh pre-mutation baseline and identity for the complete round2 dispatch."""

    snapshot: dict[str, Any]
    hmc: dict[str, Any]
    lpar: str
    description: str
    compatibility: dict[str, Any]


def round2_inputs_from_document(
    document: dict[str, Any], subtasks: list[int]
) -> Round2Inputs | None:
    if document.get("run", {}).get("group") != "round2":
        return None
    if subtasks != runner.SUBTASK_GROUPS["round2"]:
        raise ValueError("round2 dispatch does not match the complete named arm")
    config = live_config.decode_saved_config(document.get("config"))
    artifacts = document.get("artifacts")
    saved = artifacts.get("lp3_baseline") if isinstance(artifacts, dict) else None
    if not isinstance(artifacts, dict) or not isinstance(saved, dict):
        raise TypeError("round2 has no producer baseline")
    captured = independent_baseline.validate(saved.get("round2_snapshot"))
    hmc = document.get("hmc")
    if (
        not isinstance(hmc, dict)
        or captured["scope"]["host"] != hmc.get("host")
        or captured["scope"]["system"] != config.system_name
    ):
        raise ValueError("round2 snapshot scope does not match its run")
    if captured["scope"]["vios_ids"] != [artifacts.get("vios_partition_id")]:
        raise ValueError("round2 snapshot omits its serving VIOS")
    description, compatibility = saved.get("description"), saved.get("proc_compat")
    if (
        not isinstance(description, str)
        or not isinstance(compatibility, dict)
        or not compatibility.get("profile")
        or not compatibility.get("profile_mode")
    ):
        raise ValueError("round2 description/profile baseline is incomplete")
    return Round2Inputs(captured, hmc, config.lp3_name, description, compatibility)


async def check_round2(call, inputs: Round2Inputs) -> list[Finding]:
    config = HMCConfig()
    if runner._hmc_identity(config) != inputs.hmc:
        raise StateUnreadable("current HMC identity does not match round2 results")
    scope = inputs.snapshot["scope"]
    # This installed capture interface performs only REST GETs and listing SSH commands.
    now = independent_baseline.validate(
        await independent_baseline.capture(config, scope["system"], scope["vios_ids"])
    )
    findings = []
    if now != inputs.snapshot:
        findings.append(
            Finding(
                "round2 baseline drift",
                "independent system/VIOS tables differ",
                "inspect the retained baseline and current system; restore manually",
            )
        )
    for tool, expected in (
        ("hmc_get_lpar_description", inputs.description),
        ("hmc_get_lpar_proc_compat", inputs.compatibility),
    ):
        status, data = await call(
            tool, system_name_or_uuid=scope["system"], lpar_name_or_uuid=inputs.lpar
        )

        data = plain_data(data)
        if status != "PASS" or type(data) is not type(expected):
            raise StateUnreadable("round2 property read is incomplete", findings)
        if tool == "hmc_get_lpar_description":
            data = data.removesuffix("\n").removesuffix("\r")
        if data != expected:
            findings.append(
                Finding(
                    "round2 property drift",
                    f"{tool} differs from baseline",
                    "restore the property from the retained private baseline",
                )
            )
    return findings


async def check_profile_artifacts(call, system_name: str) -> list[Finding]:
    """Independently list reserved-prefix residue without deleting or restoring."""
    if not isinstance(system_name, str) or not system_name.strip():
        raise StateUnreadable("the profile-artifacts managed-system identity")
    status, listing = await call(
        "hmc_list_backup_lpar_profiles", system_name_or_uuid=system_name
    )
    names = backup_artifact_names(listing) if status == "PASS" else None
    if names is None:
        raise StateUnreadable("the complete profile-backup inventory")
    return [
        Finding(
            f"profile backup {name}",
            "an artifact-arm scratch backup remains; no automatic cleanup attempted",
            "inspect the retained run and selected system, then explicitly delete only the exact disposable backup after operator approval",
        )
        for name in sorted(names)
        if name.startswith(ARTIFACT_PREFIX)
    ]
