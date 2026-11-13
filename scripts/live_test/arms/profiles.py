"""LPAR profile inventory scenarios for the live HMC test harness."""

from __future__ import annotations

import secrets
import shlex
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from fastmcp import Client

from hmcpctl.errors import HMCCLIError
from hmcpctl.jobs import SUCCESSFUL_JOB_STATUSES
from hmcpctl.ssh.commands import _parse_lshwres_output

from ..observation import Assertion
from ..results import field

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


_POOL_READS = "st4-processor-pool-reads"
#: ``lshwres -r procpool`` unit attributes and the REST projection fields they mirror.
_CLI_POOL_UNITS = {
    "max_pool_proc_units": "maximum_processing_units",
    "curr_reserved_pool_proc_units": "reserved_processing_units",
    "pend_reserved_pool_proc_units": "pending_reserved_processing_units",
}


def _same_units(cli: str, rest: object) -> bool:
    try:
        return rest is not None and Decimal(cli) == Decimal(str(rest))
    except InvalidOperation:
        return False


def _member_count(lpar_ids: str) -> int:
    return 0 if lpar_ids == "none" else len(lpar_ids.split(","))


def pool_assertions(
    pools: object, rows: list[dict[str, str]] | None
) -> list[Assertion]:
    """Compare configured REST pools with ``lshwres -r procpool`` name=value rows.

    ``rows`` is None when either read failed; that records the failed read.
    """
    if rows is None:
        return [_READ_FAILED]
    rest = {
        pool.get("pool_id"): pool
        for pool in (pools if isinstance(pools, list) else ())
        if isinstance(pool, Mapping)
    }
    cli = {row.get("shared_proc_pool_id", ""): row for row in rows}
    cli_ids = {int(key) for key in cli if key.isdigit()}
    ids = bool(rest) and len(cli_ids) == len(cli) and cli_ids == set(rest)
    pairs = [(row, rest[int(key)]) for key, row in cli.items()] if ids else []
    return [
        Assertion("pool-ids-match", ids),
        Assertion(
            "pool-names-match",
            ids and all(pool.get("name") == row.get("name") for row, pool in pairs),
        ),
        Assertion(
            "pool-units-match",
            ids
            and all(
                _same_units(row[cli_key], pool.get(rest_key))
                for row, pool in pairs
                for cli_key, rest_key in _CLI_POOL_UNITS.items()
                if cli_key in row
            ),
        ),
        Assertion(
            "pool-membership-match",
            ids
            and all(
                _member_count(row.get("lpar_ids", "none"))
                == len(pool.get("assigned_partition_uuids") or ())
                for row, pool in pairs
            ),
        ),
    ]


async def _cli_pool_rows(
    client: Client, state: RunState, system: str
) -> list[dict[str, str]] | None:
    status, raw = await state.call(
        client, "hmc_run_command", cmd=f"lshwres -r procpool -m {shlex.quote(system)}"
    )
    if status != "PASS" or not isinstance(raw, str):
        return None
    try:
        return _parse_lshwres_output(raw)
    except HMCCLIError:
        return None


async def _processor_pool_reads(client: Client, state: RunState) -> None:
    """Compare the pool read with ``lshwres -r procpool``; read-only (#1975)."""
    system = state.config.system_name
    status, listing = await state.call(
        client, "hmc_list_shared_processor_pools", system_name_or_uuid=system
    )
    rows = await _cli_pool_rows(client, state, system) if status == "PASS" else None
    state.record_verified(
        4,
        "hmc_list_shared_processor_pools",
        operation="processor_pool.list",
        scenario=_POOL_READS,
        assertions=pool_assertions(field(listing, "pools"), rows),
        cleanup="not-required",
        data=listing,
    )
    # One observation for both gets: its id derives from (subtask, tool), and the
    # evidence emitter refuses a run whose observation ids repeat.
    assertions: list[Assertion] = []
    reads: dict[str, object] = {}
    read_failed = False
    for assertion_id, selector in (
        ("default-pool-by-id", 0),
        ("default-pool-by-name", "DefaultPool"),
    ):
        status, data = await state.call(
            client,
            "hmc_get_shared_processor_pool",
            system_name_or_uuid=system,
            pool=selector,
        )
        reads[assertion_id] = data
        pool = field(data, "pool")
        default = field(pool, "pool_id") == 0 and field(pool, "configured") is True
        assertions.append(Assertion(assertion_id, status == "PASS" and default))
        read_failed = read_failed or status != "PASS"
    if read_failed:
        assertions.append(_READ_FAILED)
    state.record_verified(
        4,
        "hmc_get_shared_processor_pool",
        operation="processor_pool.get",
        scenario=_POOL_READS,
        assertions=assertions,
        cleanup="not-required",
        data=reads,
    )


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
    await _processor_pool_reads(client, state)
    await _score_reads(client, state)
    await _plan_reads(client, state)
    await _capability_reads(client, state)


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
    suffix_length = state.config.profile_artifact_suffix_hex_length
    if type(suffix_length) is not int or suffix_length not in (12, 16):
        raise ValueError("profile artifact suffix length must be 12 or 16")
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
    name = ARTIFACT_PREFIX + secrets.token_hex(suffix_length // 2)
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
