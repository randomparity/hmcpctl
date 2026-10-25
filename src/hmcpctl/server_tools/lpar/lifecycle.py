"""MCP tools for LPAR creation, mutation, deletion, and power control."""

from __future__ import annotations

from typing import Any, Literal

from ..._app import (
    with_client,
)
from ...client.core import HMCClient
from ...documents.lpar import LparResources
from ...documents.lpar_memory import LparMemoryChange, LparMemoryMove
from ...jobs import BootMode, PowerOnKeylock, PowerOnOperationType
from ...operations.affinity.rest import ProvisionAffinityAssessment
from ...operations.lpar.activation import (
    LparPowerOnOutcome,
    power_off_lpar,
    power_on_lpar,
)
from ...operations.lpar.assignments import (
    LparPcieAssignments,
    LparPcieWorkflowResult,
)
from ...operations.lpar.core import delete_lpar, rename_lpar
from ...operations.lpar.decommission import DecommissionResult, decommission_lpar
from ...operations.lpar.dlpar import modify_lpar, set_lpar_memory, set_lpar_processors
from ...operations.lpar.memory import change_lpar_memory
from ...operations.lpar.memory_move import move_lpar_memory
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()

# PowerOff operations hmc_power_off_lpar admits. dumprestart is served only by
# hmc_dump_restart_lpar, so a grant of this tool cannot reach the crash (ADR 0188).
PowerOffToolOperation = Literal["shutdown", "osshutdown"]


# Assignment collections can name both a managed system and a nested VIOS.
@tool(
    effect="mutate",
    operation="lpar.modify",
    target_kind="lpar",
    exhaustive_targets=False,
)
def hmc_modify_lpar(
    lpar_name_or_uuid: str,
    resources: LparResources = LparResources(),
    system_name_or_uuid: str | None = None,
    assignments: LparPcieAssignments = LparPcieAssignments(),
    ownership_override: bool = False,
    profile: str | None = None,
) -> LparPcieWorkflowResult:
    """Modify an LPAR's memory or CPU resource assignment.

    lpar_name_or_uuid: accepts either a PartitionName or a UUID
    (find it with hmc_list_lpars). Only the fields you pass are changed.
    Memory values are in MiB. Sharing mode and uncapped_weight update the
    current configuration; runtime effects are independently observed and may
    remain unverified. Weight is an integer 0–255 and zero is explicit. HMC
    admission may refuse a change. Inspect completion_scope, workflow_completed,
    change_location and warnings; incomplete outcomes must not be replayed
    automatically. Activating a profile may replace current-configuration changes;
    location observations do not independently prove profile persistence.
    The partition is read whole and written back under If-Match. dedicated must
    match the partition's current mode (True for whole CPUs, False for shared
    processing units + virtual processors) or be omitted; a switch is refused.
    No activation, restart or compensation is performed.

    Use hmc_rename_lpar for a name change, which requires a managed-system
    selector for ownership authorization.

    Before modifying, inspect the description with hmc_get_lpar_description.
    Under the ADR 0011 advisory protocol, stop and ask the operator when its
    ownership token names a different agent.

    Args:
        lpar_name_or_uuid: PartitionName or UUID of the logical partition to modify.
        resources: Memory and processor fields to change; omitted fields stay unchanged.
        system_name_or_uuid: Managed-system selector required when assignments are present.
        assignments: Declarative dedicated, direct SR-IOV, and vNIC requests.
        ownership_override: Bypass assignment ownership rejection after operator approval.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    return with_client(
        lambda hmc: modify_lpar(
            hmc,
            lpar_name_or_uuid,
            resources,
            assignments,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="lpar.rename", target_kind="lpar")
def hmc_rename_lpar(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    new_name: str,
    ownership_override: bool = False,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Rename one LPAR after enforcing its ownership token.

    Before renaming, inspect the description with hmc_get_lpar_description.
    Under the ADR 0011 advisory protocol, stop and ask the operator when its
    ownership token names a different agent.

    Args:
        system_name_or_uuid: SystemName or UUID containing the logical partition.
        lpar_name_or_uuid: Current PartitionName or UUID of the logical partition.
        new_name: Replacement PartitionName.
        ownership_override: Bypass ownership rejection only after explicit operator approval.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    async def renamed(hmc):
        _, updated = await rename_lpar(
            hmc,
            lpar_name_or_uuid,
            new_name,
            system_name_or_uuid=system_name_or_uuid,
            ownership_override=ownership_override,
        )
        return updated

    return with_client(renamed, profile=profile)


@tool(effect="mutate", operation="lpar.dlpar_proc", target_kind="lpar")
def hmc_dlpar_proc(
    lpar_name_or_uuid: str,
    resources: LparResources = LparResources(),
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> dict[str, Any] | None:
    """Update processor resources in an LPAR's current configuration.

    lpar_name_or_uuid: accepts either a PartitionName or a UUID.
    Reads the whole partition and writes it back under If-Match with only
    the fields you pass changed. For shared partitions, procs are processing
    units (may be fractional, e.g. 0.5); vcpus are virtual processor counts
    (ints). For dedicated partitions, procs are whole CPUs and vcpus and
    uncapped are refused. dedicated must match the partition's current mode
    or be omitted; a switch between dedicated and shared is refused. sharing_mode
    accepts the six supported shared/dedicated sharing modes within that current
    processor mode. uncapped_weight is an update-only integer 0–255; zero is explicit.

    Sharing and weight results describe current-configuration completion and
    separately observed runtime effects, which may remain unverified. HMC admission
    may refuse a change. Inspect workflow_completed, change_location and warnings;
    incomplete outcomes must not be replayed automatically. A profile activation
    may replace current-configuration changes; location observations do not
    independently prove profile persistence. No activation, restart or compensation
    is performed.

    Args:
        lpar_name_or_uuid: PartitionName or UUID of the logical partition.
        resources: Processor fields to change; omitted fields stay unchanged.
        profile: Optional configured HMC profile name; uses the default when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide and the
            owning system is discovered for the ownership check.
        ownership_override: Bypass ownership rejection only after explicit
            operator approval.
    """

    return with_client(
        lambda hmc: set_lpar_processors(
            hmc,
            lpar_name_or_uuid,
            resources,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="lpar.dlpar_mem", target_kind="lpar")
def hmc_dlpar_mem(
    lpar_name_or_uuid: str,
    resources: LparResources = LparResources(),
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
    change: LparMemoryChange | None = None,
) -> dict[str, Any] | None:
    """Change absolute memory resources or typed native current-memory quantities.

    lpar_name_or_uuid: accepts either a PartitionName or a UUID.
    resources reads the whole partition and writes it under If-Match with only
    the memory fields you pass changed. change submits one native add/remove/set
    request against current assigned memory and independently verifies its result.
    Choose one input mode. All memory values are in MiB. Shutdown reductions below
    minimum clear memory and processor assignments; forced no-RMC changes require
    later activation or an operating-system restart. Partial/unverified outcomes
    have workflow_completed=false and must not be retried automatically.

    The write changes the partition's current configuration, not a partition
    profile. With no active RMC connection it applies on next activation only if
    that activation uses the current configuration; activating a profile discards
    it unless CurrentProfileSync is On. The result's change_location and warnings
    say which. No reboot is triggered.

    Args:
        lpar_name_or_uuid: PartitionName or UUID of the running logical partition.
        resources: Memory fields in MiB to change; omitted fields stay unchanged.
        profile: Optional configured HMC profile name; uses the default when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide and the
            owning system is discovered for the ownership check.
        ownership_override: Bypass ownership rejection only after explicit
            operator approval.
        change: Typed native quantity request; requires system_name_or_uuid and
            empty resources. No profile write, activation, rollback or replay.
    """

    if change is not None:
        if resources != LparResources():
            raise ValueError("Choose either resources or change for memory updates")
        if not system_name_or_uuid or not system_name_or_uuid.strip():
            raise ValueError(
                "system_name_or_uuid is required for native memory quantities"
            )
        return with_client(
            lambda hmc: change_lpar_memory(
                hmc,
                lpar_name_or_uuid,
                change,
                system_name_or_uuid=system_name_or_uuid,
                ownership_override=ownership_override,
            ),
            profile=profile,
        )

    return with_client(
        lambda hmc: set_lpar_memory(
            hmc,
            lpar_name_or_uuid,
            resources,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="lpar.delete", target_kind="lpar")
def hmc_delete_lpar(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    ownership_override: bool = False,
    profile: str | None = None,
) -> str:
    """Delete (destroy) an LPAR by name or UUID.

    The partition must be powered off first (use hmc_power_off_lpar and
    confirm with hmc_get_lpar_state). This
    tool refuses to delete a partition whose current state is anything other
    than 'not activated', matching the precondition check pattern used by
    hmc_remove_memory_pool. This permanently removes the partition and its
    profiles from the HMC — it is irreversible. Confirm the target with
    hmc_get_lpar(lpar_name_or_uuid=...) before calling. Returns a confirmation string
    (immediate delete — no job to poll).

    lpar_name_or_uuid: accepts either a PartitionName or a UUID.

    Deletion enforces the description-field ownership token. Foreign-owned or
    malformed tokens are rejected before state checks or deletion. Set
    ownership_override=True only after explicit operator approval.

    Args:
        system_name_or_uuid: SystemName or UUID containing the logical partition.
        lpar_name_or_uuid: PartitionName or UUID of the logical partition to delete.
        ownership_override: Bypass ownership rejection only after operator approval.
        profile: Optional configured HMC profile name; uses the default when omitted.

    Raises:
        HMCError: If the partition state is not 'not activated' (HTTP 409).
    """

    async def delete_lpar_and_confirm(hmc):
        lpar_uuid = await delete_lpar(
            hmc,
            lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
            ownership_override=ownership_override,
        )
        return f"Deleted LPAR {lpar_uuid}"

    return with_client(delete_lpar_and_confirm, profile=profile)


@tool(effect="destructive", operation="lpar.decommission", target_kind="lpar")
def hmc_decommission_lpar(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    dry_run: bool = False,
    ownership_override: bool = False,
    immediate: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
) -> DecommissionResult:
    """Inventory, authorize, and optionally decommission one LPAR.

    This tool orchestrates the high-risk decommission workflow in one call:
    resolve the target LPAR on the selected managed system, enforce the
    ownership token, inventory its adapter and observed storage blast radius,
    require complete vSCSI inventory before mutation, power it off when needed,
    detach its exact vSCSI mappings before client adapters, and finally delete
    the partition while retaining backing storage and foreign mappings.
    Set dry_run=True to render the blast radius, warnings and step plan
    without mutating anything. With dry_run=False, the final delete is
    irreversible once reached.

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

    Args:
        system_name_or_uuid: SystemName or UUID of the managed system containing the target LPAR.
        lpar_name_or_uuid: PartitionName or UUID of the logical partition to inventory or delete.
        dry_run: When True, inventory the blast radius and planned steps without mutating resources.
        ownership_override: Bypass ownership rejection only after explicit operator approval.
        immediate: Whether to request immediate shutdown instead of a delayed one
            before deletion.
        timeout_seconds: Maximum polling duration in seconds for the power-off job;
            must be positive.
        poll_interval: Seconds between power-off job polls; must be positive.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    return with_client(
        lambda hmc: decommission_lpar(
            hmc,
            lpar_name_or_uuid,
            dry_run=dry_run,
            ownership_override=ownership_override,
            immediate=immediate,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="lpar.power_on", target_kind="lpar")
def hmc_power_on_lpar(
    lpar_name_or_uuid: str,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    force: bool = False,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    affinity_assessment: ProvisionAffinityAssessment | None = None,
    ownership_override: bool = False,
    boot_mode: BootMode = "norm",
    partition_profile_uuid: str | None = None,
    operation_type: PowerOnOperationType | None = None,
    keylock: PowerOnKeylock | None = None,
) -> LparPowerOnOutcome:
    """Submit a PowerOn job for a logical partition, optionally against a partition-profile UUID — not `profile`, which selects the HMC connection.

    lpar_name_or_uuid: accepts either a PartitionName or a UUID
    (find it with hmc_list_lpars). Returns ``already_activated``, nullable ``job``,
    and nullable ``message`` fields. A submitted job is in ``job``; check it
    with hmc_get_job. This changes the state of a real partition — confirm the
    target with hmc_get_lpar(lpar_name_or_uuid=...) before calling.

    Two unrelated things here are called a profile. ``profile`` selects which
    configured HMC connection to use. ``partition_profile_uuid`` selects the
    partition profile the partition activates against.

    With ``partition_profile_uuid``, ``warnings`` lists each current virtual
    SCSI, Fibre Channel or Ethernet client adapter whose slot that profile lacks:
    activating the profile removes it. The job is still submitted. No warning
    covers memory or processor changes, which the profile activation discards
    too (see ``partition_profile_uuid``).

    The HMC accepts PowerOn only from the 'not activated' state. If the
    partition is already activated — 'running', 'starting' or 'open firmware' —
    ``already_activated`` is true, ``job`` is null, and ``message`` names the
    state and explains that no job was submitted. Any other state is refused
    with an error naming it, and no job is submitted. Pass force=True to skip
    this check and submit PowerOn unconditionally; the HMC then fails the job
    from any state but 'not activated'.

    Set wait=True to block until the job reaches a terminal state or until
    timeout_seconds elapses; ``job`` then contains the last polled job. When the
    waited job ends successfully, the partition state is read once: 'error' or
    'not activated' is raised as a failed activation naming the state, because the
    HMC can complete the job cleanly while activation fails. Read the reference
    code with hmc_read_lpar_refcodes.

    Args:
        lpar_name_or_uuid: PartitionName or UUID of the logical partition to power on.
        wait: Whether to poll the submitted job until terminal or timed out.
        timeout_seconds: Maximum polling duration in seconds when waiting.
        poll_interval: Seconds between job polls when waiting; must be positive.
        force: Submit PowerOn whatever state the partition reports.
        profile: Optional configured HMC connection profile name — not the
            partition profile, which is partition_profile_uuid; uses the default
            when omitted.
        system_name_or_uuid: Optional SystemName or UUID that disambiguates the
            partition name; when omitted the name is searched fleet-wide. With
            HMC_AUTHORIZE_POWER_OPERATIONS set it also spares the ownership
            guard a fleet-wide search for the partition's owning system.
        affinity_assessment: Optional target-bound captured affinity evidence and
            explicit warning or fail-closed response intent.
        ownership_override: Bypass ADR 0011 ownership rejection only after operator
            approval; has no effect unless HMC_AUTHORIZE_POWER_OPERATIONS is set.
        boot_mode: Boot mode to activate into — norm, dd, ds, of, or sms. Defaults
            to norm, which is what the partition activates into today. Use sms for
            System Management Services or of for the Open Firmware prompt.
        partition_profile_uuid: UUID of the partition profile to activate against.
            This is not the profile argument above, which selects a configured HMC
            connection. Activating a profile discards current-configuration
            changes the profile lacks: memory and processor changes from
            hmc_modify_lpar, hmc_dlpar_mem or hmc_dlpar_proc, and adapter changes.
            CurrentProfileSync On copies a change into the partition's current
            profile only, so it survives activating that profile and no other.
            When omitted the partition activates against its current configuration.
        operation_type: PowerOn operation type; activate states the default
            explicitly and is never sent, because the HMC refuses an
            OperationType parameter for it.
        keylock: Keylock position to activate with — manual or norm (normal), the
            PowerOn job's own spelling, not the creation-time normal/manual/auto.
            Omit it to send no keylock parameter and leave the position to the HMC.
    """

    return with_client(
        lambda hmc: power_on_lpar(
            hmc,
            lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            force=force,
            affinity_assessment=affinity_assessment,
            ownership_override=ownership_override,
            boot_mode=boot_mode,
            partition_profile_uuid=partition_profile_uuid,
            operation_type=operation_type,
            keylock=keylock,
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="lpar.power_off", target_kind="lpar")
def hmc_power_off_lpar(
    lpar_name_or_uuid: str,
    immediate: bool = False,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
    restart: bool = False,
    operation: PowerOffToolOperation = "shutdown",
) -> dict[str, Any] | None:
    """Submit a PowerOff job for a logical partition, optionally restarting it or selecting the shutdown operation.

    lpar_name_or_uuid: accepts either a PartitionName or a UUID.
    system_name_or_uuid disambiguates duplicate partition names; it is otherwise
    unused when lpar_name_or_uuid is already a UUID, unless the server runs with
    HMC_AUTHORIZE_POWER_OPERATIONS set, where it also spares the ownership guard
    a fleet-wide search for the partition's owning system.
    immediate=True forces an immediate power off; immediate=False requests a
    delayed shutdown, which is not an operating-system shutdown.
    Returns the submitted job. This changes the state of a real partition.

    Set wait=True to block until the job reaches a terminal state.

    kdive's PowerAction maps onto this job's own parameters: `off` is
    operation=shutdown with immediate=true; `cycle` and `reset` are the same with
    restart=true; a graceful shutdown is operation=osshutdown, which needs an active
    RMC connection to the partition's operating system.

    operation=shutdown with restart=true requires immediate=true and is refused
    otherwise: on the HMC that combination is a dump restart, which crashes the
    partition (ADR 0164). For an operating-system restart use
    operation=osshutdown with restart=true.

    The force-crash, operation=dumprestart, is not this tool's: it is
    hmc_dump_restart_lpar, a separate grant (ADR 0188). The vendor's fourth value,
    dumpretry, is not accepted.

    Args:
        lpar_name_or_uuid: PartitionName or UUID of the logical partition to power off.
        immediate: Whether to request immediate shutdown instead of a delayed one.
        wait: Whether to poll the submitted job until terminal or timed out.
        timeout_seconds: Maximum polling duration in seconds when waiting.
        poll_interval: Seconds between job polls when waiting; must be positive.
        profile: Optional configured HMC profile name; uses the default when omitted.
        system_name_or_uuid: Optional SystemName or UUID used to disambiguate its name.
            With HMC_AUTHORIZE_POWER_OPERATIONS set it also spares the ownership
            guard a fleet-wide search for the partition's owning system.
        ownership_override: Bypass ADR 0011 ownership rejection only after operator
            approval; has no effect unless HMC_AUTHORIZE_POWER_OPERATIONS is set.
        restart: Restart the partition instead of leaving it off; this is what
            kdive's cycle and reset map to. With operation=shutdown it needs
            immediate=true.
        operation: PowerOff shutdown operation — shutdown or osshutdown.
            osshutdown asks the operating system to shut down and needs an
            active RMC connection to it.
    """

    async def power_off_job(hmc: HMCClient) -> dict[str, Any] | None:
        result = await power_off_lpar(
            hmc,
            lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
            immediate=immediate,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            ownership_override=ownership_override,
            restart=restart,
            operation=operation,
        )
        return result.job

    return with_client(power_off_job, profile=profile)


@tool(effect="destructive", operation="lpar.dump_restart", target_kind="lpar")
def hmc_dump_restart_lpar(
    lpar_name_or_uuid: str,
    allow_dump_restart: bool = False,
    wait: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    profile: str | None = None,
    system_name_or_uuid: str | None = None,
    ownership_override: bool = False,
) -> dict[str, Any] | None:
    """Crash a logical partition and take a platform dump (PowerOff operation=dumprestart).

    This is kdive's force-crash. It is a separate tool from hmc_power_off_lpar so an
    access policy can grant the ordinary stop without it (ADR 0188). It is refused
    unless allow_dump_restart is true. Returns the submitted job; with wait=True it
    blocks until the job is terminal. Do not resubmit a timed-out wait: poll the job.

    Args:
        lpar_name_or_uuid: PartitionName or UUID of the logical partition to crash.
        allow_dump_restart: Confirm the crash and platform dump; without it the call is
            refused and nothing is submitted.
        wait: Whether to poll the submitted job until terminal or timed out.
        timeout_seconds: Maximum polling duration in seconds when waiting.
        poll_interval: Seconds between job polls when waiting; must be positive.
        profile: Optional configured HMC profile name; uses the default when omitted.
        system_name_or_uuid: Optional SystemName or UUID used to disambiguate its name.
            With HMC_AUTHORIZE_POWER_OPERATIONS set it also spares the ownership
            guard a fleet-wide search for the partition's owning system.
        ownership_override: Bypass ADR 0011 ownership rejection only after operator
            approval; has no effect unless HMC_AUTHORIZE_POWER_OPERATIONS is set.
    """

    async def dump_restart_job(hmc: HMCClient) -> dict[str, Any] | None:
        result = await power_off_lpar(
            hmc,
            lpar_name_or_uuid,
            system_name_or_uuid=system_name_or_uuid,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            ownership_override=ownership_override,
            operation="dumprestart",
            allow_dump_restart=allow_dump_restart,
        )
        return result.job

    return with_client(dump_restart_job, profile=profile)


@tool(
    effect="mutate",
    operation="lpar.move_memory",
    target_kind="lpar",
    extra_targets=(("lpar", "target_lpar_name_or_uuid"),),
)
def hmc_move_lpar_memory(
    lpar_name_or_uuid: str,
    target_lpar_name_or_uuid: str,
    system_name_or_uuid: str,
    move: LparMemoryMove,
    profile: str | None = None,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Move dedicated memory once and independently observe both partitions.

    Args:
        lpar_name_or_uuid: Source PartitionName or UUID within the selected system.
        target_lpar_name_or_uuid: Distinct destination PartitionName or UUID.
        system_name_or_uuid: Containing managed system name or UUID, required.
        move: Typed MiB quantity and optional documented no-RMC force intent.
        profile: Configured connection profile; default when omitted.
        ownership_override: Explicit operator override for both ownership checks.

    Source/destination min/max, state, capabilities and region alignment are checked.
    Native firmware and guest admission enforce remaining reservations. Both results
    carry change_location and warnings; forced effects may need an OS restart.
    Partial/unverified effects must not be automatically replayed or compensated.
    """
    if any(
        not value.strip()
        for value in (lpar_name_or_uuid, target_lpar_name_or_uuid, system_name_or_uuid)
    ):
        raise ValueError("Memory move requires source, destination and managed system")
    return with_client(
        lambda hmc: move_lpar_memory(
            hmc,
            lpar_name_or_uuid,
            target_lpar_name_or_uuid,
            move,
            system_name_or_uuid=system_name_or_uuid,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )
