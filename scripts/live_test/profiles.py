"""LPAR profile inventory scenarios for the live HMC test harness."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from fastmcp import Client

from hmcpctl.jobs import SUCCESSFUL_JOB_STATUSES

from .observation import Assertion
from .results import field

if TYPE_CHECKING:
    from live_test.runner import RunState

# ---------------------------------------------------------------------------
# ST4 — LPAR Properties & Profile Inventory
# ---------------------------------------------------------------------------

_PROFILE_READS = "st4-profile-reads"
_AFFINITY_READS = "st4-affinity-reads"
_POLICY_ACTIONS = {"none", "warn", "fail"}
# Recorded in place of a read's assertions when the call itself failed.
_READ_FAILED = Assertion("read-succeeded", False)


def _score(value: object) -> int | None:
    """Return a 0-100 affinity score as an int, or ``None`` when it is not one."""
    try:
        score = int(str(value))
    except ValueError:
        return None
    return score if 0 <= score <= 100 else None


def _all_rows(data: object, holds: Callable[[Mapping[str, object]], bool]) -> bool:
    """Whether *data* is a non-empty list of mappings that all satisfy *holds*."""
    if not isinstance(data, list) or not data:
        return False
    return all(isinstance(row, Mapping) and holds(row) for row in data)


def _lists(data: object, lpar_name: str) -> bool:
    """Whether a score listing has a row for *lpar_name*."""
    return isinstance(data, list) and any(
        isinstance(row, Mapping) and row.get("lpar_name") == lpar_name for row in data
    )


def _available(data: object) -> bool:
    return field(data, "capability") == "available"


def _unavailable_reason(data: object) -> bool:
    reason = field(data, "unavailable_reason")
    return isinstance(reason, str) and bool(reason)


def _group_scores(data: object, score_field: str) -> bool:
    return _all_rows(
        field(data, "items"), lambda item: _score(item.get(score_field)) is not None
    )


async def _profile_reads(client: Client, state: RunState) -> None:
    config = state.config

    st, data = await state.call(
        client,
        "hmc_get_lpar_description",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record_verified(
        4,
        "hmc_get_lpar_description",
        operation="lpar.get_description",
        scenario=_PROFILE_READS,
        assertions=[Assertion("description-is-text", isinstance(data, str))]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client,
        "hmc_get_lpar_msp",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record_verified(
        4,
        "hmc_get_lpar_msp",
        operation="lpar.get_msp",
        scenario=_PROFILE_READS,
        assertions=[Assertion("msp-is-bool", isinstance(data, bool))]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )

    st, modes = await state.call(
        client, "hmc_get_proc_compat_modes", system_name_or_uuid=config.system_name
    )
    supported = modes if st == "PASS" and isinstance(modes, list) else []
    state.record_verified(
        4,
        "hmc_get_proc_compat_modes",
        operation="system.get_proc_compat_modes",
        scenario=_PROFILE_READS,
        assertions=[Assertion("modes-include-default", "default" in supported)]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=modes,
    )

    st, data = await state.call(
        client,
        "hmc_get_lpar_proc_compat",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record_verified(
        4,
        "hmc_get_lpar_proc_compat",
        operation="lpar.get_proc_compat",
        scenario=_PROFILE_READS,
        assertions=[
            Assertion("current-mode-supported", field(data, "curr") in supported),
            Assertion(
                "profile-mode-supported", field(data, "profile_mode") in supported
            ),
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )


async def _pool_and_vnic_reads(client: Client, state: RunState) -> None:
    config = state.config

    st, data = await state.call(
        client, "hmc_list_memory_pools", system_name_or_uuid=config.system_name
    )
    # An empty list is the no-pool branch, not evidence about a pool row's shape.
    state.record_verified(
        4,
        "hmc_list_memory_pools",
        operation="memory_pool.list",
        scenario=_PROFILE_READS,
        assertions=[
            Assertion("memory-pools-empty-branch", True)
            if data == []
            else Assertion(
                # The row's field names are uncaptured (#1245): assert only shape.
                "memory-pool-rows-parsed",
                _all_rows(data, bool),
            )
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client,
        "hmc_list_vnics",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record(4, "hmc_list_vnics", st, data)


async def _score_reads(client: Client, state: RunState) -> None:
    config = state.config
    lp3 = config.lp3_name

    st, data = await state.call(
        client,
        "hmc_get_lpar_memopt_score",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=lp3,
    )
    state.record_verified(
        4,
        "hmc_get_lpar_memopt_score",
        operation="lpar.get_memopt_score",
        scenario=_AFFINITY_READS,
        assertions=[
            Assertion("score-names-partition", field(data, "lpar_name") == lp3),
            Assertion(
                "score-in-range", _score(field(data, "curr_lpar_score")) is not None
            ),
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client, "hmc_list_lpar_memopt_scores", system_name_or_uuid=config.system_name
    )
    state.record_verified(
        4,
        "hmc_list_lpar_memopt_scores",
        operation="lpar.list_memopt_scores",
        scenario=_AFFINITY_READS,
        assertions=[
            Assertion("partition-listed", _lists(data, lp3)),
            Assertion(
                "scores-in-range",
                _all_rows(
                    data, lambda row: _score(row.get("curr_lpar_score")) is not None
                ),
            ),
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client, "hmc_get_system_memopt_score", system_name_or_uuid=config.system_name
    )
    state.record_verified(
        4,
        "hmc_get_system_memopt_score",
        operation="system.get_memopt_score",
        scenario=_AFFINITY_READS,
        assertions=[
            Assertion(
                "score-in-range", _score(field(data, "curr_sys_score")) is not None
            )
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )


async def _plan_reads(client: Client, state: RunState) -> None:
    config = state.config

    st, data = await state.call(
        client, "hmc_plan_lpar_memopt_scores", system_name_or_uuid=config.system_name
    )
    state.record_verified(
        4,
        "hmc_plan_lpar_memopt_scores",
        operation="lpar.plan_memopt_scores",
        scenario=_AFFINITY_READS,
        assertions=[
            Assertion(
                "predictions-in-range",
                _all_rows(
                    data,
                    lambda row: _score(row.get("predicted_lpar_score")) is not None,
                ),
            ),
            Assertion(
                "prediction-not-guaranteed",
                _all_rows(data, lambda row: row.get("prediction_guaranteed") is False),
            ),
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client, "hmc_plan_system_memopt_score", system_name_or_uuid=config.system_name
    )
    state.record_verified(
        4,
        "hmc_plan_system_memopt_score",
        operation="system.plan_memopt_score",
        scenario=_AFFINITY_READS,
        assertions=[
            Assertion(
                "prediction-in-range",
                _score(field(data, "predicted_sys_score")) is not None,
            ),
            Assertion(
                "prediction-not-guaranteed",
                field(data, "prediction_guaranteed") is False,
            ),
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )


async def _capability_reads(client: Client, state: RunState) -> None:
    """Gated reads: each assertion id names the capability branch the read took."""
    config = state.config

    st, data = await state.call(
        client,
        "hmc_list_resource_group_memopt_scores",
        system_name_or_uuid=config.system_name,
    )
    state.record_verified(
        4,
        "hmc_list_resource_group_memopt_scores",
        operation="resource_group.list_memopt_scores",
        scenario=_AFFINITY_READS,
        assertions=[
            Assertion("capability-available-rows", _group_scores(data, "curr_score"))
            if _available(data)
            else Assertion("capability-unavailable-reason", _unavailable_reason(data))
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client,
        "hmc_plan_resource_group_memopt_scores",
        system_name_or_uuid=config.system_name,
    )
    state.record_verified(
        4,
        "hmc_plan_resource_group_memopt_scores",
        operation="resource_group.plan_memopt_scores",
        scenario=_AFFINITY_READS,
        assertions=[
            Assertion(
                "capability-available-rows", _group_scores(data, "predicted_score")
            )
            if _available(data)
            else Assertion("capability-unavailable-reason", _unavailable_reason(data))
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client,
        "hmc_get_minimum_affinity_policy",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record_verified(
        4,
        "hmc_get_minimum_affinity_policy",
        operation="lpar.get_minimum_affinity_policy",
        scenario=_AFFINITY_READS,
        assertions=[
            Assertion(
                "capability-available-policy",
                _score(field(data, "min_affinity_score")) is not None
                and field(data, "min_affinity_score_action") in _POLICY_ACTIONS,
            )
            if _available(data)
            else Assertion("capability-unavailable-reason", _unavailable_reason(data))
        ]
        if st == "PASS"
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )


async def inventory_lpar_profiles(client: Client, state: RunState) -> None:
    print("\n=== ST4: LPAR Properties & Profile Inventory ===")
    await _profile_reads(client, state)
    await _pool_and_vnic_reads(client, state)
    await _score_reads(client, state)
    await _plan_reads(client, state)
    await _capability_reads(client, state)


ARTIFACT_SUBTASK = 43
ARTIFACT_GROUP = "profile-artifacts"
ARTIFACT_PREFIX = "hmcpctl-live-prof-"
_ARTIFACT_SCENARIO = "st43-profile-artifacts"


def backup_artifact_names(data: object) -> set[str] | None:
    """A complete clean artifact inventory, or None rather than a false empty set."""
    rows = field(data, "artifacts")
    if field(field(data, "job"), "status") not in SUCCESSFUL_JOB_STATUSES:
        return None
    if not isinstance(rows, (list, tuple)):
        return None
    names: set[str] = set()
    for row in rows:
        name = field(row, "file_name")
        if not isinstance(name, str) or not name or name in names:
            return None
        names.add(name)
    return names


async def exercise_backup_artifacts(client: Client, state: RunState) -> None:
    """Exercise one disposable artifact only under its explicit named-arm grant."""
    if state.group != ARTIFACT_GROUP:
        state.skip(43, "hmc_backup_lpar_profiles", "requires the profile-artifacts arm")
        return
    system = state.config.system_name
    status, before = await state.call(
        client, "hmc_list_backup_lpar_profiles", system_name_or_uuid=system
    )
    names = backup_artifact_names(before) if status == "PASS" else None
    if names is None or any(name.startswith(ARTIFACT_PREFIX) for name in names):
        state.record(
            43,
            "hmc_list_backup_lpar_profiles",
            "FAIL",
            before,
            "complete clean scratch-prefix baseline required; no backup created",
        )
        return
    name = ARTIFACT_PREFIX + secrets.token_hex(8)
    status, data = await state.call(
        client,
        "hmc_backup_lpar_profiles",
        system_name_or_uuid=system,
        file_path=name,
        force=False,
    )
    state.record(
        43,
        "hmc_backup_lpar_profiles",
        status,
        data,
        "scratch creation only; no restore",
    )
    if status != "PASS":
        return
    status, listing = await state.call(
        client, "hmc_list_backup_lpar_profiles", system_name_or_uuid=system
    )
    names = backup_artifact_names(listing) if status == "PASS" else None
    listed = names is not None and name in names
    state.record_verified(
        43,
        "hmc_list_backup_lpar_profiles",
        operation="lpar_profile.list_backups",
        scenario=_ARTIFACT_SCENARIO,
        assertions=[Assertion("scratch-listed-exactly", listed)],
        cleanup="not-required",
        data=listing,
    )
    if not listed:
        return
    status, deletion = await state.call(
        client,
        "hmc_delete_backup_lpar_profile",
        system_name_or_uuid=system,
        file_name=name,
    )
    absent = status == "PASS" and field(deletion, "postcondition") == "absent"
    state.record_verified(
        43,
        "hmc_delete_backup_lpar_profile",
        operation="lpar_profile.delete_backup",
        scenario=_ARTIFACT_SCENARIO,
        assertions=[Assertion("scratch-absence-read-back", absent)],
        cleanup="passed" if absent else "failed",
        data=deletion,
    )
