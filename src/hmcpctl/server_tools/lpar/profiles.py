"""MCP tools for LPAR profile backup/restore/sync and I/O slot assignment."""

from __future__ import annotations

from ..._app import (
    ssh_with_client,
    with_client,
)
from ...documents.partition_profile import ProfilePatch
from ...operations.lpar.configuration import (
    restore_system_lpar_profiles,
    synchronize_lpar_profile,
)
from ...operations.lpar.profile_artifacts import (
    BackupProfileDeletion,
    BackupProfileInventory,
    delete_backup_profile_artifact,
    list_backup_profile_artifacts,
)
from ...operations.lpar.profiles import (
    PartitionProfileInventory,
    PartitionProfileOutcome,
    PartitionProfileRecord,
    delete_partition_profile,
    list_partition_profiles,
    read_partition_profile,
    save_current_partition_profile,
    set_default_partition_profile,
    update_partition_profile,
)
from ...operations.virtualization.pcie import (
    assign_dedicated_pcie_slot,
    unassign_dedicated_pcie_slot,
)
from ...ssh.profiles import ProfileRestoreType, ProfileSyncMode, backup_lpar_profiles
from ...tool_registry import tool_module

# destructive because force=True silently overwrites an existing backup file on the HMC
tool, register_tools, tool_security = tool_module()


@tool(
    effect="read", operation="lpar_profile.list_backups", target_kind="managed_system"
)
def hmc_list_backup_lpar_profiles(
    system_name_or_uuid: str, profile: str | None = None
) -> BackupProfileInventory:
    """List HMC-side profile backup artifacts on one managed system.

    Requires HMC 10.3.1061.0 or later and the observed JSON artifact schema.
    Returns a complete bounded inventory, not local files. A failed, warning or
    timed-out job has artifacts=null, distinct from a successful empty list.
    Raw job payloads are omitted; job status, identity and bounded errors remain.

    Args:
        system_name_or_uuid: Exact managed system name or UUID whose backups are listed.
        profile: Optional TOML connection profile; omitted uses the configured default.
    """
    return with_client(
        lambda hmc: list_backup_profile_artifacts(hmc, system_name_or_uuid),
        profile=profile,
    )


@tool(
    effect="destructive",
    operation="lpar_profile.delete_backup",
    target_kind="managed_system",
    exhaustive_targets=False,
)
def hmc_delete_backup_lpar_profile(
    system_name_or_uuid: str, file_name: str, profile: str | None = None
) -> BackupProfileDeletion:
    """Delete one exact HMC profile backup name after complete inventory admission.

    Requires destructive all-targets authorization; a basename is not a new
    containment guarantee. Rejects paths and ambiguous/missing inventories.
    Reports absent only after a successful job and fresh exact-name readback;
    present/unknown/not-checked are not clean deletion success. Never retries a
    mutation or selects another file. Independent writers are not fenced.
    Retain required recovery backups; no local-file transfer or restore occurs.

    Args:
        system_name_or_uuid: Exact managed system name or UUID owning the backup.
        file_name: Exact listed HMC backup name; paths and ambiguous names are rejected.
        profile: Optional TOML connection profile; omitted uses the configured default.
    """
    return with_client(
        lambda hmc: delete_backup_profile_artifact(hmc, system_name_or_uuid, file_name),
        profile=profile,
    )


# Not exhaustive: `file_path` names a file on the HMC's own filesystem, which no
# TargetKind can express, and `force=True` overwrites whatever is already there.
# ADR 0036 placed `file_path` outside every grant; ADR 0039 makes that
# enforceable by granting this tool only under `targets = "all-targets"`.
@tool(
    effect="destructive",
    operation="lpar_profile.backup",
    target_kind="managed_system",
    exhaustive_targets=False,
)
def hmc_backup_lpar_profiles(
    system_name_or_uuid: str,
    file_path: str,
    force: bool = False,
    profile: str | None = None,
) -> str:
    """Backup all LPAR profiles on a Power system via the HMC CLI.

    Runs ``bkprofdata -m <system_name> -f <file_path>`` on the HMC via SSH
    and returns the raw command output.

    The system may be given by CLI name or by UUID; a UUID is resolved to
    its CLI name via REST (falling back to an lssyscfg lookup over SSH when
    the REST API is unreachable) before the command runs.

    **IMPORTANT:** file_path is on the HMC filesystem, not the local machine.
    The backup file will be created at that path on the HMC host.

    Args:
        system_name_or_uuid: The name or UUID of the managed system (Power server).
        file_path: Path on the HMC filesystem where the backup file will be saved.
        force: When True, passes ``--force`` to ``bkprofdata`` so that an
            existing file at ``file_path`` is overwritten. Defaults to False.
        profile: optional TOML profile name; when omitted the env-default HMC is used.

    Returns:
        The raw HMC CLI output.

    Raises:
        ValueError: if ``file_path`` is empty or whitespace-only.
    """
    if not file_path or not file_path.strip():
        raise ValueError("file_path must not be empty")
    return ssh_with_client(
        lambda config, system_name, _: backup_lpar_profiles(
            config, system_name, file_path, force=force
        ),
        system_name_or_uuid=system_name_or_uuid,
        profile=profile,
    )


# Not exhaustive, for the same reason as its backup sibling: `file_path` is an
# HMC-side path no TargetKind names, and the restore rewrites the profiles of
# every partition on the system from whatever that file contains.
@tool(
    effect="destructive",
    operation="lpar_profile.restore",
    target_kind="managed_system",
    exhaustive_targets=False,
)
def hmc_restore_lpar_profiles(
    system_name_or_uuid: str,
    file_path: str,
    system_wide_restore_approved: bool = False,
    profile: str | None = None,
    ownership_override: bool = False,
    *,
    restore_type: ProfileRestoreType,
    force_keystore: bool = False,
) -> str:
    """Restore LPAR profiles from a backup file via the HMC CLI.

    Runs ``rstprofdata -m <system_name> -l <restore_type> -f <file_path>`` on
    the HMC via SSH and returns the raw command output.

    The system may be given by CLI name or by UUID; a UUID is resolved to
    its CLI name via REST (falling back to an lssyscfg lookup over SSH when
    the REST API is unreachable) before the command runs.

    **IMPORTANT:** file_path is on the HMC filesystem, not the local machine.
    The backup file must already exist at that path on the HMC host.

    WARNING: Restoring overwrites the current LPAR profile configuration.
    Confirm the system_name_or_uuid and file_path before calling, then set
    system_wide_restore_approved=True. The destructive managed-system tool grant and
    its authorization audit record provide the administrative authorization boundary;
    this explicit acknowledgement prevents an ordinary partition mutation workflow
    from invoking the system-wide restore accidentally.

    WARNING: A restore_type=3 merge also resets ``resource_config`` from 1 to 0 on
    every Not Activated partition. This was observed on a V10R3 HMC with a POWER9
    system, even when merging a backup taken moments earlier. Restore each such
    partition by re-applying its current profile (``curr_profile``) with
    ``chsyscfg -r lpar -m <system> -o apply -p <lpar> -n <profile>``.

    Args:
        system_name_or_uuid: The name or UUID of the managed system (Power server).
        file_path: Path on the HMC filesystem where the backup file is located.
        system_wide_restore_approved: Explicit operator approval to overwrite every
            LPAR profile on the selected managed system.
        profile: optional TOML profile name; when omitted the env-default HMC is used.
        ownership_override: Bypass current and opaque-backup ownership rejection after
            operator approval; emits an audit record covering every profile.
        force_keystore: Ignore partition-keystore restore errors explicitly; stdout and
            stderr warnings remain visible. Defaults to False; no ownership bypass.
        restore_type: The HMC restore type, required with no default: 1 restores the
            backup file in full; 2 merges current and backup data, the backup winning
            conflicts; 3 merges them, the current data winning conflicts, and resets
            ``resource_config`` on Not Activated partitions (see WARNING). Type 4
            (initialize, which deletes every partition) is not offered.

    Returns:
        The raw HMC CLI output."""
    if not system_wide_restore_approved:
        raise PermissionError(
            "restoring LPAR profiles overwrites every profile on the managed system; "
            "retry with system_wide_restore_approved=true after operator approval"
        )
    return with_client(
        lambda hmc: restore_system_lpar_profiles(
            hmc,
            system_name_or_uuid,
            file_path,
            restore_type,
            ownership_override=ownership_override,
            force_keystore=force_keystore,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="lpar_profile.sync", target_kind="lpar")
def hmc_sync_lpar_profile(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    ownership_override: bool = False,
    profile: str | None = None,
    mode: ProfileSyncMode = "enable",
) -> str:
    """Set an LPAR's profile-synchronization setting (``sync_curr_profile``).

    Runs ``chsyscfg -r lpar -m <system_name> -i "name=<lpar_name>,sync_curr_profile=<n>"``
    on the HMC via SSH and returns the raw command output. This changes a
    persistent partition setting (ADR 0201); it is not a one-shot save of the
    running configuration:

    - ``enable`` (1): keep the partition's current configuration synchronized
      with its active profile.
    - ``disable`` (0): stop synchronizing.
    - ``suspend`` (2): pause synchronization until the profile is next
      activated or applied.

    WARNING: with ``enable`` on an activated partition, synchronization keeps
    the active profile in step with the running configuration and so can
    overwrite that profile's definition. Confirm the system_name_or_uuid and
    lpar_name_or_uuid before calling.

    The system and partition may be given by CLI name or by UUID; UUIDs
    are resolved to their CLI names via REST (falling back to an lssyscfg
    lookup over SSH when the REST API is unreachable) before the command
    runs.

    Args:
        system_name_or_uuid: The name or UUID of the managed system (Power server).
        lpar_name_or_uuid: The name or UUID of the logical partition.
        ownership_override: Bypass ownership rejection after operator approval.
        profile: optional TOML profile name; when omitted the env-default HMC is used.
        mode: ``enable``, ``disable`` or ``suspend``; defaults to ``enable``.

    Returns:
        The raw HMC CLI output."""

    return with_client(
        lambda hmc: synchronize_lpar_profile(
            hmc,
            lpar_name_or_uuid,
            ownership_override=ownership_override,
            mode=mode,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="pcie.assign_dedicated_slot", target_kind="lpar")
def hmc_assign_dedicated_pcie_slot(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_name: str,
    drc_index: str,
    ownership_override: bool = False,
    profile: str | None = None,
) -> None:
    """Assign a dedicated PCIe slot when safe profile readback is available.

    Refuses outside the ADR 0165 envelope (HMC V10R3 M1060 on managed-system model
    8375-42A, matched exactly). The target LPAR must be in the 'Not Activated' state;
    on any other state this tool refuses rather than mutate a profile whose change
    would not take effect. Also refuses when another LPAR's profile already lists
    the slot, since two partitions listing the same slot would contend for it at
    activation.

    Args:
        system_name_or_uuid: The name or UUID of the managed system (Power server).
        lpar_name_or_uuid: The name or UUID of the logical partition to assign the slot to.
        profile_name: The name of the profile to modify.
        drc_index: The DRC (Dynamic Reconfiguration Connector) index of the physical I/O
            slot, exactly eight uppercase hexadecimal digits (e.g. "21010003").
        ownership_override: Bypass ownership rejection only after operator approval.
        profile: Optional configured HMC profile name.
    """

    async def _go(hmc) -> None:
        await assign_dedicated_pcie_slot(
            hmc,
            lpar_name_or_uuid,
            profile_name,
            drc_index,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        )

    return with_client(_go, profile=profile)


@tool(effect="mutate", operation="pcie.unassign_dedicated_slot", target_kind="lpar")
def hmc_unassign_dedicated_pcie_slot(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_name: str,
    drc_index: str,
    ownership_override: bool = False,
    profile: str | None = None,
) -> None:
    """Unassign a dedicated PCIe slot when safe profile readback is available.

    Refuses outside the ADR 0165 envelope (HMC V10R3 M1060 on managed-system model
    8375-42A, matched exactly). The target LPAR must be in the 'Not Activated' state;
    on any other state this tool refuses rather than mutate a profile whose change
    would not take effect. Unlike assign, this operation does not refuse when
    another LPAR's profile also lists the slot — that check only guards adding a
    new listing.

    Args:
        system_name_or_uuid: Managed-system name or UUID.
        lpar_name_or_uuid: Logical-partition name or UUID.
        profile_name: LPAR profile containing the dedicated slot.
        drc_index: Normalized dedicated-slot DRC index, exactly eight uppercase
            hexadecimal digits (e.g. "21010003").
        ownership_override: Bypass ownership rejection only after operator approval.
        profile: Optional configured HMC profile name.
    """

    async def _go(hmc) -> None:
        await unassign_dedicated_pcie_slot(
            hmc,
            lpar_name_or_uuid,
            profile_name,
            drc_index,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        )

    return with_client(_go, profile=profile)


# Saved-profile lifecycle; VIOS tools retain their own policy target kind.
@tool(effect="read", operation="lpar_profile.list", target_kind="lpar")
def hmc_list_partition_profiles(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile: str | None = None,
) -> PartitionProfileInventory:
    """List bounded saved profiles and their contained names,fields and opaque ETags.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        lpar_name_or_uuid: Exact logical-partition name or UUID on the selected system.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: list_partition_profiles(
            hmc,
            system_name_or_uuid,
            lpar_name_or_uuid,
            partition_type="LogicalPartition",
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar_profile.read", target_kind="lpar")
def hmc_read_partition_profile(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_selector: str,
    profile: str | None = None,
) -> PartitionProfileRecord:
    """Read one exact saved-profile name or UUID within its owning partition.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        lpar_name_or_uuid: Exact logical-partition name or UUID on the selected system.
        profile_selector: Exact saved-profile name or UUID,not an HMC connection profile.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: read_partition_profile(
            hmc,
            system_name_or_uuid,
            lpar_name_or_uuid,
            profile_selector,
            partition_type="LogicalPartition",
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="lpar_profile.update", target_kind="lpar")
def hmc_update_partition_profile(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_selector: str,
    expected_etag: str,
    patch: ProfilePatch,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    profile: str | None = None,
) -> PartitionProfileOutcome:
    """Patch typed saved fields with If-Match while preserving the complete fetched document.

    Requires the coordinated writer window and normal ownership authorization. No automatic retry or rollback.
    Completed execution plus verified requested change and drifted preservation means applied with preservation drift; reconciliation is required.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        lpar_name_or_uuid: Exact logical-partition name or UUID on the selected system.
        profile_selector: Exact saved-profile name or UUID,not an HMC connection profile.
        expected_etag: Opaque ETag from a fresh contained profile read; mandatory for overwriting an existing profile.
        patch: Typed saved-profile fields; omitted fields retain their exact represented content.
        exclusive_writer_window: Assert operator coordination of the partition,its profiles and referencing system profiles through reconciliation; no independent-writer fencing.
        ownership_override: Use the existing audited ownership override after operator approval.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: update_partition_profile(
            hmc,
            system_name_or_uuid,
            lpar_name_or_uuid,
            profile_selector,
            expected_etag,
            patch,
            partition_type="LogicalPartition",
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="lpar_profile.save_current", target_kind="lpar")
def hmc_save_current_partition_profile(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    name: str,
    overwrite: bool = False,
    expected_etag: str | None = None,
    recovery_profile_selector: str | None = None,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> PartitionProfileOutcome:
    """Save current configuration to a new profile,or force-save after a retained recovery copy verifies.

    Requires the coordinated writer window and normal ownership authorization. No automatic retry or rollback.
    A failed/uncertain overwrite can change the destination; retained recovery content is not an unchanged destination. Never delete required recovery material.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        lpar_name_or_uuid: Exact logical-partition name or UUID on the selected system.
        name: Exact saved-profile name to create or overwrite.
        overwrite: Explicit consent to replace an existing profile only after verified retained recovery.
        expected_etag: Opaque ETag from a fresh contained profile read; mandatory for overwriting an existing profile.
        recovery_profile_selector: Optional existing contained recovery profile to revalidate against the complete baseline; omitted creates one retained copy.
        exclusive_writer_window: Assert operator coordination of the partition,its profiles and referencing system profiles through reconciliation; no independent-writer fencing.
        ownership_override: Use the existing audited ownership override after operator approval.
        timeout_seconds: Native job polling timeout in seconds; an uncertain result is never retried automatically.
        poll_interval: Positive native job polling interval in seconds.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: save_current_partition_profile(
            hmc,
            system_name_or_uuid,
            lpar_name_or_uuid,
            name,
            partition_type="LogicalPartition",
            overwrite=overwrite,
            expected_etag=expected_etag,
            recovery_profile_selector=recovery_profile_selector,
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="lpar_profile.set_default", target_kind="lpar")
def hmc_set_default_partition_profile(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_selector: str,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> PartitionProfileOutcome:
    """Select a contained default profile with strict native job results and fresh readback.

    Requires the coordinated writer window and normal ownership authorization. No automatic retry or rollback.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        lpar_name_or_uuid: Exact logical-partition name or UUID on the selected system.
        profile_selector: Exact saved-profile name or UUID,not an HMC connection profile.
        exclusive_writer_window: Assert operator coordination of the partition,its profiles and referencing system profiles through reconciliation; no independent-writer fencing.
        ownership_override: Use the existing audited ownership override after operator approval.
        timeout_seconds: Native job polling timeout in seconds; an uncertain result is never retried automatically.
        poll_interval: Positive native job polling interval in seconds.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: set_default_partition_profile(
            hmc,
            system_name_or_uuid,
            lpar_name_or_uuid,
            profile_selector,
            partition_type="LogicalPartition",
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="lpar_profile.delete", target_kind="lpar")
def hmc_delete_partition_profile(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_selector: str,
    expected_etag: str,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    profile: str | None = None,
) -> PartitionProfileOutcome:
    """Delete only an unprotected saved profile after an empty unfiltered system-profile inventory.

    Requires the coordinated writer window and normal ownership authorization. No automatic retry or rollback.
    Current,default,last,synchronized,linked-active and full-system profiles are protected. Any nonempty or unreadable system-profile inventory refuses deletion.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        lpar_name_or_uuid: Exact logical-partition name or UUID on the selected system.
        profile_selector: Exact saved-profile name or UUID,not an HMC connection profile.
        expected_etag: Opaque ETag from a fresh contained profile read; mandatory for overwriting an existing profile.
        exclusive_writer_window: Assert operator coordination of the partition,its profiles and referencing system profiles through reconciliation; no independent-writer fencing.
        ownership_override: Use the existing audited ownership override after operator approval.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: delete_partition_profile(
            hmc,
            system_name_or_uuid,
            lpar_name_or_uuid,
            profile_selector,
            expected_etag,
            partition_type="LogicalPartition",
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(effect="read", operation="vios_profile.list", target_kind="vios")
def hmc_list_vios_partition_profiles(
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
    profile: str | None = None,
) -> PartitionProfileInventory:
    """List bounded saved profiles and their contained names,fields and opaque ETags.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        vios_name_or_uuid: Exact VIOS name or UUID on the selected system.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: list_partition_profiles(
            hmc,
            system_name_or_uuid,
            vios_name_or_uuid,
            partition_type="VirtualIOServer",
        ),
        profile=profile,
    )


@tool(effect="read", operation="vios_profile.read", target_kind="vios")
def hmc_read_vios_partition_profile(
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
    profile_selector: str,
    profile: str | None = None,
) -> PartitionProfileRecord:
    """Read one exact saved-profile name or UUID within its owning partition.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        vios_name_or_uuid: Exact VIOS name or UUID on the selected system.
        profile_selector: Exact saved-profile name or UUID,not an HMC connection profile.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: read_partition_profile(
            hmc,
            system_name_or_uuid,
            vios_name_or_uuid,
            profile_selector,
            partition_type="VirtualIOServer",
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="vios_profile.update", target_kind="vios")
def hmc_update_vios_partition_profile(
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
    profile_selector: str,
    expected_etag: str,
    patch: ProfilePatch,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    profile: str | None = None,
) -> PartitionProfileOutcome:
    """Patch typed saved fields with If-Match while preserving the complete fetched document.

    Requires the coordinated writer window and normal ownership authorization. No automatic retry or rollback.
    Completed execution plus verified requested change and drifted preservation means applied with preservation drift; reconciliation is required.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        vios_name_or_uuid: Exact VIOS name or UUID on the selected system.
        profile_selector: Exact saved-profile name or UUID,not an HMC connection profile.
        expected_etag: Opaque ETag from a fresh contained profile read; mandatory for overwriting an existing profile.
        patch: Typed saved-profile fields; omitted fields retain their exact represented content.
        exclusive_writer_window: Assert operator coordination of the partition,its profiles and referencing system profiles through reconciliation; no independent-writer fencing.
        ownership_override: Use the existing audited ownership override after operator approval.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: update_partition_profile(
            hmc,
            system_name_or_uuid,
            vios_name_or_uuid,
            profile_selector,
            expected_etag,
            patch,
            partition_type="VirtualIOServer",
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="vios_profile.save_current", target_kind="vios")
def hmc_save_current_vios_partition_profile(
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
    name: str,
    overwrite: bool = False,
    expected_etag: str | None = None,
    recovery_profile_selector: str | None = None,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> PartitionProfileOutcome:
    """Save current configuration to a new profile,or force-save after a retained recovery copy verifies.

    Requires the coordinated writer window and normal ownership authorization. No automatic retry or rollback.
    A failed/uncertain overwrite can change the destination; retained recovery content is not an unchanged destination. Never delete required recovery material.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        vios_name_or_uuid: Exact VIOS name or UUID on the selected system.
        name: Exact saved-profile name to create or overwrite.
        overwrite: Explicit consent to replace an existing profile only after verified retained recovery.
        expected_etag: Opaque ETag from a fresh contained profile read; mandatory for overwriting an existing profile.
        recovery_profile_selector: Optional existing contained recovery profile to revalidate against the complete baseline; omitted creates one retained copy.
        exclusive_writer_window: Assert operator coordination of the partition,its profiles and referencing system profiles through reconciliation; no independent-writer fencing.
        ownership_override: Use the existing audited ownership override after operator approval.
        timeout_seconds: Native job polling timeout in seconds; an uncertain result is never retried automatically.
        poll_interval: Positive native job polling interval in seconds.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: save_current_partition_profile(
            hmc,
            system_name_or_uuid,
            vios_name_or_uuid,
            name,
            partition_type="VirtualIOServer",
            overwrite=overwrite,
            expected_etag=expected_etag,
            recovery_profile_selector=recovery_profile_selector,
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="vios_profile.set_default", target_kind="vios")
def hmc_set_default_vios_partition_profile(
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
    profile_selector: str,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> PartitionProfileOutcome:
    """Select a contained default profile with strict native job results and fresh readback.

    Requires the coordinated writer window and normal ownership authorization. No automatic retry or rollback.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        vios_name_or_uuid: Exact VIOS name or UUID on the selected system.
        profile_selector: Exact saved-profile name or UUID,not an HMC connection profile.
        exclusive_writer_window: Assert operator coordination of the partition,its profiles and referencing system profiles through reconciliation; no independent-writer fencing.
        ownership_override: Use the existing audited ownership override after operator approval.
        timeout_seconds: Native job polling timeout in seconds; an uncertain result is never retried automatically.
        poll_interval: Positive native job polling interval in seconds.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: set_default_partition_profile(
            hmc,
            system_name_or_uuid,
            vios_name_or_uuid,
            profile_selector,
            partition_type="VirtualIOServer",
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="vios_profile.delete", target_kind="vios")
def hmc_delete_vios_partition_profile(
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
    profile_selector: str,
    expected_etag: str,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    profile: str | None = None,
) -> PartitionProfileOutcome:
    """Delete only an unprotected saved profile after an empty unfiltered system-profile inventory.

    Requires the coordinated writer window and normal ownership authorization. No automatic retry or rollback.
    Current,default,last,synchronized,linked-active and full-system profiles are protected. Any nonempty or unreadable system-profile inventory refuses deletion.

    Args:
        system_name_or_uuid: Exact managed-system name or UUID.
        vios_name_or_uuid: Exact VIOS name or UUID on the selected system.
        profile_selector: Exact saved-profile name or UUID,not an HMC connection profile.
        expected_etag: Opaque ETag from a fresh contained profile read; mandatory for overwriting an existing profile.
        exclusive_writer_window: Assert operator coordination of the partition,its profiles and referencing system profiles through reconciliation; no independent-writer fencing.
        ownership_override: Use the existing audited ownership override after operator approval.
        profile: Optional configured HMC connection profile; distinct from saved partition profiles.
    """
    return with_client(
        lambda hmc: delete_partition_profile(
            hmc,
            system_name_or_uuid,
            vios_name_or_uuid,
            profile_selector,
            expected_etag,
            partition_type="VirtualIOServer",
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )
