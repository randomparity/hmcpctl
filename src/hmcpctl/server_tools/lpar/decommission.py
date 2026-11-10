"""``hmc_decommission_lpar``: tear down an LPAR as one journaled operation (ADR 0320).

``storage_cleanup=delete_owned`` deletes the partition's ledger-owned storage as the
specialists that own those writes (ADR 0189 Decision 2, ADR 0323 Decision 5), so the
handler needs the application's ``permits`` and ``authorize`` gates and is built by a
factory, as ``hmc_power_lpar`` is. Its own security is unchanged from the ``@tool`` it
replaces: kind ``lpar``, exhaustive selectors.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastmcp import FastMCP

from hmcpctl.operations.lpar.decommission.core import DecommissionResult
from hmcpctl.operations.lpar.decommission.operation import (
    STORAGE_CLEANUP,
    TOOL_NAME,
    decommission_body,
    decommission_classifiers,
    deletes_owned,
    effective_arguments,
    given_arguments,
    new_arguments,
    owned_vios,
    preview_target,
    result_from_record,
)

from ..._app import with_client
from ...authorization.target_scope import TargetScopeError
from ...config import build_config
from ...operations.logical import engine, store
from ...operations.logical.engine import OperationRequest
from ...tool_registry import (
    Authorize,
    TargetSelector,
    ToolSecurity,
    annotations_for,
    authorized,
    validate_security,
)
from ..delegation import authorize_as

DECOMMISSION_TOOL_NAME = TOOL_NAME
DECOMMISSION_SECURITY = ToolSecurity(
    effect="destructive",
    operation="lpar.decommission",
    target_kind="lpar",
    targets=(
        TargetSelector("managed_system", "system_name_or_uuid", False),
        TargetSelector("lpar", "lpar_name_or_uuid", False),
    ),
    exhaustive_targets=True,
)
# The H1 spec's decommission delete_owned row: an unmap of a disk mapping, an unmount
# (the unmap of an optical mapping), and the two deletes (ADR 0323 Decision 5).
DELEGATED = (
    "hmc_detach_storage_mapping",
    "hmc_unmount_optical_media",
    "hmc_delete_optical_media",
    "hmc_delete_virtual_disk",
)
WINDOW_WARNING = (
    "storage_cleanup=delete_owned needs exclusive_writer_window=true to run: the caller "
    "asserts no other writer changes these VIOS volume groups until the operation ends"
)
Continuation = Literal["none", "resume", "abandon"]
StorageCleanup = Literal["retain", "delete_owned"]


def decommission_authorization_targets(
    name: str, arguments: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Recover immutable original selectors after dispatch's connection eligibility check."""
    if name != TOOL_NAME or arguments.get("continuation") not in ("resume", "abandon"):
        return None
    try:
        config = build_config(profile=arguments["profile"])
        recorded = engine.recorded_arguments(
            config.agent_id or "hmcpctl", arguments["request_id"], TOOL_NAME
        )
        selectors = ("system_name_or_uuid", "lpar_name_or_uuid")
        if not isinstance(recorded, Mapping) or any(
            not isinstance(recorded.get(key), str) or not recorded[key].strip()
            for key in selectors
        ):
            raise ValueError("unavailable recorded selectors")
        return {key: recorded[key] for key in selectors}
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
        raise TargetScopeError(
            "hmc_decommission_lpar cannot authorize this operation's recorded targets"
        ) from None


def _require_window(window: bool | None) -> None:
    if window is not True:
        raise ValueError(
            "exclusive_writer_window=true is required with storage_cleanup=delete_owned "
            "(H1 spec, ADR 0323): its volume-group writes have no observed If-Match "
            "enforcement; pass it once no other writer changes these VIOS until the "
            "operation ends. A resume asserts it again"
        )


def _authorize_storage(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
    arguments: Mapping[str, Any],
    target: tuple[str, str],
    profile: str | None,
) -> frozenset[str]:
    """Authorize the cleanup specialists for every owned item's VIOS; return that set."""
    for name in DELEGATED:
        if not permits(name):
            raise PermissionError(
                f"{name} is not permitted by this server's access policy; "
                f"{TOOL_NAME} storage_cleanup=delete_owned needs it (ADR 0189)"
            )
    vioses = owned_vios(target)
    for vios in sorted(vioses):
        by_kind = {
            "managed_system": arguments["system_name_or_uuid"],
            "lpar": arguments["lpar_name_or_uuid"],
            "vios": vios,
        }
        for name in DELEGATED:
            authorize_as(tool_security, authorize, name, by_kind, profile)
    return vioses


def decommission_handler(
    tool_security: Mapping[str, ToolSecurity],
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> Callable[..., DecommissionResult]:
    """The unwrapped ``hmc_decommission_lpar`` handler bound to the served gates."""

    def hmc_decommission_lpar(
        lpar_name_or_uuid: str | None = None,
        dry_run: bool = False,
        ownership_override: bool | None = None,
        immediate: bool | None = None,
        timeout_seconds: int | None = None,
        poll_interval: int | None = None,
        storage_cleanup: StorageCleanup | None = None,
        exclusive_writer_window: bool | None = None,
        continuation: Continuation = "none",
        wait_seconds: int = 60,
        *,
        request_id: str,
        system_name_or_uuid: str | None = None,
        profile: str | None = None,
    ) -> DecommissionResult:
        """Inventory, authorize, and optionally decommission one LPAR.

        This tool orchestrates the high-risk decommission workflow in one call:
        resolve the target LPAR on the selected managed system, enforce the
        ownership token, inventory its adapter and observed storage blast radius,
        require complete vSCSI inventory before mutation, power it off when needed,
        detach its exact vSCSI mappings before client adapters, and finally delete
        the partition. Set dry_run=True to render the blast radius, warnings and step
        plan without mutating anything or recording an operation. With dry_run=False,
        the final delete is irreversible once reached.

        Backing storage is retained by default (``storage_cleanup=retain``). With
        ``storage_cleanup=delete_owned`` (ADR 0192, ADR 0323), after the partition is
        deleted the operation deletes each virtual disk and installer medium the
        operation store records as created for this partition, only when one read of
        every VIOS shows no mapping to another partition; physical volumes, media
        repositories and anything not recorded are always retained. An unreadable
        VIOS or unclassifiable mapping retains every owned item and pauses for
        ``resume``. It needs ``exclusive_writer_window=true`` and a policy permitting
        ``hmc_detach_storage_mapping``, ``hmc_unmount_optical_media``,
        ``hmc_delete_optical_media`` and ``hmc_delete_virtual_disk`` for each owning
        VIOS, or the call is refused and nothing is recorded.

        Durable (ADR 0190, ADR 0320): the call returns within ``wait_seconds`` and the
        work continues; poll ``hmc_operation_status`` with ``request_id``. A repeat with
        the same arguments returns the same operation and never powers off, detaches or
        deletes twice. A step that fails pauses the operation as ``needs_attention``;
        ``continuation=resume`` checks each interrupted write live and continues from a
        fresh inventory, and ``abandon`` stops it without writing. A power-off that
        failed with the partition still active pauses on every resume: abandon it and
        start a new request_id. A new request whose inventory or ownership check fails
        is refused and nothing is recorded.

        Ownership enforcement runs even for dry runs. If the LPAR description
        names a different owner, stop and ask the operator before proceeding. Set
        ownership_override=True only after explicit operator approval.

        Returns a structured result with these fields:

        - ``resource_deleted`` — whether the final LPAR delete completed.
        - ``workflow_completed`` — whether every requested workflow step completed.
        - ``lpar_uuid`` — UUID of the resolved target LPAR.
        - ``dry_run`` — whether the call only inventoried the blast radius.
        - ``steps`` — ordered per-step status and curated result records.
        - ``warnings`` — non-fatal warnings discovered during inventory.
        - ``blast_radius`` — curated inventory of the LPAR, adapters, and observed
          storage mappings.
        - ``operation`` — the operation record (null for a dry run).
        - ``storage`` — with ``delete_owned``: ``deleted``, ``retained`` (each with a
          ``reason``) and ``pending`` items; null under ``retain``.

        Args:
            request_id: Caller id for this operation, 1-64 of A-Z a-z 0-9 . _ -.
            system_name_or_uuid: SystemName or UUID of the managed system containing the
                target LPAR; required for a new operation.
            lpar_name_or_uuid: PartitionName or UUID of the logical partition to inventory
                or delete; required for a new operation.
            dry_run: When True, inventory the blast radius and planned steps without
                mutating resources or recording an operation.
            ownership_override: Default false; bypass ownership rejection only after
                explicit operator approval.
            immediate: Whether to request immediate shutdown instead of a delayed one
                before deletion (default false).
            timeout_seconds: Maximum polling duration in seconds for the power-off job;
                must be positive (default 300).
            poll_interval: Seconds between power-off job polls; must be positive
                (default 5).
            storage_cleanup: retain (default) keeps all backing storage; delete_owned
                deletes the partition's recorded, unshared virtual disks and installer
                media after the partition.
            exclusive_writer_window: Required true with delete_owned on a new operation
                or resume: the caller asserts no other writer changes these VIOS volume
                groups until the operation ends.
            continuation: none, resume or abandon; a continuation needs only request_id,
                and any other value given must match the operation's.
            wait_seconds: Seconds to wait before returning, 0-600.
            profile: Optional configured HMC profile name; uses the default when omitted.
        """
        store.validate_request_id(request_id)
        if dry_run and continuation != "none":
            raise ValueError("dry_run cannot continue an operation")
        if storage_cleanup is not None and storage_cleanup not in STORAGE_CLEANUP:
            raise ValueError(
                f"storage_cleanup must be one of {', '.join(STORAGE_CLEANUP)}"
            )
        given = given_arguments(
            system_name_or_uuid=system_name_or_uuid,
            lpar_name_or_uuid=lpar_name_or_uuid,
            ownership_override=ownership_override,
            immediate=immediate,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            storage_cleanup=storage_cleanup,
        )
        if dry_run:
            # Reads no operation store's records, so a store needing recovery previews.
            arguments = new_arguments(given)
            result = with_client(
                lambda hmc: preview_target(hmc, arguments), profile=profile
            )[0]
            if deletes_owned(arguments) and exclusive_writer_window is not True:
                warnings = (*result.warnings, WINDOW_WARNING)
                result = dataclasses.replace(result, warnings=warnings)
            return result
        config = build_config(profile=profile)
        agent_id = config.agent_id or "hmcpctl"
        effective, exists = effective_arguments(
            given, agent_id, request_id, continuation
        )
        owned = deletes_owned(effective)
        writes = continuation != "abandon"
        if owned and writes:
            _require_window(exclusive_writer_window)
        target = None
        if not exists or (owned and continuation == "resume"):
            if exists:
                target = engine.recorded_partition(agent_id, request_id, TOOL_NAME)
            if target is None:
                # Preconditions refuse a new operation before anything is recorded.
                target = with_client(
                    lambda hmc: preview_target(hmc, effective), profile=profile
                )[1]
        authorized_vios: frozenset[str] = frozenset()
        if owned and writes and target is not None:
            authorized_vios = _authorize_storage(
                tool_security, permits, authorize, effective, target, profile
            )
        record = engine.submit(
            OperationRequest(
                TOOL_NAME,
                agent_id,
                store.connection_label(profile, tool=TOOL_NAME),
                config.host,
                request_id,
                effective if continuation == "none" else given,
            ),
            decommission_body(effective, authorized_vios=authorized_vios),
            classifiers=decommission_classifiers(),
            continuation=continuation,
            wait_seconds=wait_seconds,
        )
        return result_from_record(record, delete_owned=owned)

    return hmc_decommission_lpar


def register_decommission_tool(
    mcp: FastMCP,
    tool_security: Mapping[str, ToolSecurity],
    *,
    permits: Callable[[str], bool],
    authorize: Authorize,
) -> None:
    """Register ``hmc_decommission_lpar`` on *mcp* when the ceiling admits it."""
    if not permits(TOOL_NAME):
        return
    handler = decommission_handler(tool_security, permits, authorize)
    validate_security(DECOMMISSION_SECURITY, handler)
    mcp.tool(
        authorized(TOOL_NAME, DECOMMISSION_SECURITY, handler, authorize),
        annotations=annotations_for(DECOMMISSION_SECURITY.effect),
    )
