"""MCP tools for LPAR configuration exposed only by the HMC CLI."""

from __future__ import annotations

from pydantic import StrictBool

from hmcpctl.operations.lpar.ownership import set_lpar_ownership_description

from ..._app import (
    ssh_with_client,
    with_client,
)
from ...documents.lpar import LparReportingSettings, LparSettings
from ...documents.lpar_security_policy import (
    LparSecurityPolicy,
    SecurityPolicyResult,
    SecurityPolicyState,
)
from ...documents.lpar_vtpm import LparVtpmSettings, VtpmState
from ...operations.affinity.ssh import (
    MinimumAffinityPolicyResult,
    ResourceGroupAffinityResult,
    get_lpar_memopt_score,
    get_minimum_affinity_policy,
    get_system_memopt_score,
    list_lpar_memopt_scores,
    list_resource_group_memopt_scores,
    plan_lpar_memopt_scores,
    plan_resource_group_memopt_scores,
    plan_system_memopt_score,
    set_minimum_affinity_policy,
)
from ...operations.lpar.configuration import (
    LparReportingSettingsResult,
    LparSettingsResult,
    ProcessorCompatibilityMode,
    configure_lpar_msp,
    configure_lpar_processor_compatibility,
    configure_lpar_reporting_settings,
    configure_lpar_settings,
    read_lpar_reporting_settings,
    read_lpar_settings,
)
from ...operations.lpar.security_policy import (
    configure_lpar_security_policy,
    read_lpar_security_policy,
)
from ...operations.lpar.vtpm import VtpmResult, configure_lpar_vtpm, read_lpar_vtpm
from ...ssh.affinity import (
    MemoptLparSelector,
    MemoptResourceGroupSelector,
    MinimumAffinityPolicy,
    validate_memopt_scenario,
)
from ...ssh.profiles import (
    get_lpar_description,
    get_lpar_msp,
    get_lpar_proc_compat,
)
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


@tool(effect="read", operation="lpar.get_minimum_affinity_policy", target_kind="lpar")
def hmc_get_minimum_affinity_policy(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile: str | None = None,
) -> MinimumAffinityPolicyResult:
    """Return an LPAR's minimum-affinity policy when supported.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition name or UUID from ``hmc_list_lpars``.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: get_minimum_affinity_policy(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="lpar.set_minimum_affinity_policy", target_kind="lpar")
def hmc_set_minimum_affinity_policy(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    policy: MinimumAffinityPolicy,
    ownership_override: bool = False,
    profile: str | None = None,
) -> str:
    """Set an LPAR's POWER11 minimum-affinity policy after authorization.

    ``fail`` is never selected by default; callers must pass it explicitly in
    ``policy.min_affinity_score_action``.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition name or UUID from ``hmc_list_lpars``.
        policy: Required score and deliberately selected action.
        ownership_override: Bypass ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: set_minimum_affinity_policy(
            hmc,
            lpar_name_or_uuid,
            policy,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.get_memopt_score", target_kind="lpar")
def hmc_get_lpar_memopt_score(
    system_name_or_uuid: str, lpar_name_or_uuid: str, profile: str | None = None
) -> dict[str, object]:
    """Return an LPAR's current memory-optimization affinity score.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition name or UUID from ``hmc_list_lpars``.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: get_lpar_memopt_score(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.list_memopt_scores", target_kind="managed_system")
def hmc_list_lpar_memopt_scores(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str | None = None,
    profile: str | None = None,
) -> list[dict[str, object]]:
    """List current memory-optimization affinity scores for a system's LPARs.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Optional partition name or UUID to filter to.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: list_lpar_memopt_scores(
            hmc, system_name_or_uuid, lpar_name_or_uuid
        ),
        profile=profile,
    )


@tool(effect="read", operation="system.get_memopt_score", target_kind="managed_system")
def hmc_get_system_memopt_score(
    system_name_or_uuid: str, profile: str | None = None
) -> dict[str, object]:
    """Return a managed system's current memory-optimization affinity score.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: get_system_memopt_score(hmc, system_name_or_uuid),
        profile=profile,
    )


@tool(effect="read", operation="lpar.plan_memopt_scores", target_kind="managed_system")
def hmc_plan_lpar_memopt_scores(
    system_name_or_uuid: str,
    prioritized: MemoptLparSelector | None = None,
    excluded: MemoptLparSelector | None = None,
    profile: str | None = None,
) -> list[dict[str, object]]:
    """Return predicted LPAR affinity scores without applying optimization.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        prioritized: Optional LPAR names or IDs to prioritize in the scenario.
        excluded: Optional LPAR names or IDs to exclude from the scenario.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    validate_memopt_scenario(prioritized, excluded)
    return with_client(
        lambda hmc: plan_lpar_memopt_scores(
            hmc, system_name_or_uuid, prioritized, excluded
        ),
        profile=profile,
    )


@tool(effect="read", operation="system.plan_memopt_score", target_kind="managed_system")
def hmc_plan_system_memopt_score(
    system_name_or_uuid: str,
    prioritized: MemoptLparSelector | None = None,
    excluded: MemoptLparSelector | None = None,
    profile: str | None = None,
) -> dict[str, object]:
    """Return a predicted system affinity score without applying optimization.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        prioritized: Optional LPAR names or IDs to prioritize in the scenario.
        excluded: Optional LPAR names or IDs to exclude from the scenario.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    validate_memopt_scenario(prioritized, excluded)
    return with_client(
        lambda hmc: plan_system_memopt_score(
            hmc, system_name_or_uuid, prioritized, excluded
        ),
        profile=profile,
    )


@tool(
    effect="read",
    operation="resource_group.list_memopt_scores",
    target_kind="managed_system",
)
def hmc_list_resource_group_memopt_scores(
    system_name_or_uuid: str,
    selector: MemoptResourceGroupSelector | None = None,
    profile: str | None = None,
) -> ResourceGroupAffinityResult:
    """Return current resource-group affinity scores when supported.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        selector: Resource-group names, IDs, or all groups; all when omitted.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: list_resource_group_memopt_scores(
            hmc, system_name_or_uuid, selector
        ),
        profile=profile,
    )


@tool(
    effect="read",
    operation="resource_group.plan_memopt_scores",
    target_kind="managed_system",
)
def hmc_plan_resource_group_memopt_scores(
    system_name_or_uuid: str,
    selector: MemoptResourceGroupSelector | None = None,
    profile: str | None = None,
) -> ResourceGroupAffinityResult:
    """Return potential resource-group affinity scores without running DPO.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        selector: Resource-group names, IDs, or all groups; all when omitted.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: plan_resource_group_memopt_scores(
            hmc, system_name_or_uuid, selector
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.get_description", target_kind="lpar")
def hmc_get_lpar_description(
    system_name_or_uuid: str, lpar_name_or_uuid: str, profile: str | None = None
) -> str:
    """Return an LPAR's CLI-only description, resolving names or UUIDs.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition name or UUID from ``hmc_list_lpars``.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """
    return ssh_with_client(
        lambda config, system_name, lpar_name: get_lpar_description(
            config, system_name, lpar_name
        ),
        system_name_or_uuid=system_name_or_uuid,
        lpar_name_or_uuid=lpar_name_or_uuid,
        profile=profile,
    )


@tool(effect="mutate", operation="lpar.set_description", target_kind="lpar")
def hmc_set_lpar_description(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    description: str,
    ownership_override: bool = False,
    profile: str | None = None,
) -> str:
    """Set an LPAR's CLI-only description after validating printable ASCII.

    The current description's ownership token is enforced before overwrite.
    Foreign-owned or malformed tokens are rejected. Set ownership_override=True
    only after explicit operator approval.

    Plain text keeps the ownership stamp: on a stamped partition the field
    becomes ``[hmcpctl owner:<id> created:<date>]``, its ``[caller <token>]``
    segment if any, then the text; read it back with hmc_get_lpar_description.
    Text carrying its own complete stamp re-stamps or hands over the partition
    and is written as given; a ``[hmcpctl`` or ``[caller `` fragment without a
    complete stamp is always rejected. With ownership_override=True the text is
    written as given, stamp or not, which is how a stamp is removed.

    A description carrying a character the HMC's attribute record treats as
    structure is rejected, with an error naming the character. The HMC writes
    the description through that record, so such text would be read as further
    attributes rather than as the description (ADR 0045).

    WARNING: This changes LPAR configuration on the selected HMC.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition name or UUID from ``hmc_list_lpars``.
        description: New printable-ASCII partition text, carrying no character
            the HMC attribute record treats as structure. Written after the
            current ownership stamp unless it carries its own.
        ownership_override: Permit overwriting a foreign or malformed ownership token,
            and replacing the whole description, only after explicit operator
            approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: set_lpar_ownership_description(
            hmc,
            lpar_name_or_uuid,
            description,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.get_msp", target_kind="lpar")
def hmc_get_lpar_msp(
    system_name_or_uuid: str, lpar_name_or_uuid: str, profile: str | None = None
) -> bool:
    """Return an LPAR's CLI-only Migratable Service Partition flag.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition name or UUID from ``hmc_list_lpars``.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """
    return ssh_with_client(
        lambda config, system_name, lpar_name: get_lpar_msp(
            config, system_name, lpar_name
        ),
        system_name_or_uuid=system_name_or_uuid,
        lpar_name_or_uuid=lpar_name_or_uuid,
        profile=profile,
    )


@tool(effect="mutate", operation="lpar.set_msp", target_kind="lpar")
def hmc_set_lpar_msp(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    enabled: bool,
    ownership_override: bool = False,
    profile: str | None = None,
) -> str:
    """Set a VIOS partition's Migratable Service Partition flag.

    The command rejects non-VIOS partitions. WARNING: this changes LPAR
    configuration on the selected HMC.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: VIOS partition name or UUID from ``hmc_list_vios``.
        enabled: Whether to enable the Migratable Service Partition flag.
        ownership_override: Bypass ownership rejection after operator approval.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: configure_lpar_msp(
            hmc,
            lpar_name_or_uuid,
            enabled,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.get_proc_compat", target_kind="lpar")
def hmc_get_lpar_proc_compat(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_name: str | None = None,
    profile: str | None = None,
) -> dict[str, str]:
    """Return an LPAR's processor compatibility modes and a profile's mode.

    ``desired`` and ``curr`` describe the partition; ``profile`` and
    ``profile_mode`` name a partition profile and its ``lpar_proc_compat_mode``,
    the attribute ``hmc_set_lpar_proc_compat`` writes.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition name or UUID from ``hmc_list_lpars``.
        profile_name: Partition profile to read; the default profile when omitted.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """
    return ssh_with_client(
        lambda config, system_name, lpar_name: get_lpar_proc_compat(
            config, system_name, lpar_name, profile_name
        ),
        system_name_or_uuid=system_name_or_uuid,
        lpar_name_or_uuid=lpar_name_or_uuid,
        profile=profile,
    )


@tool(effect="mutate", operation="lpar.set_proc_compat", target_kind="lpar")
def hmc_set_lpar_proc_compat(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    mode: ProcessorCompatibilityMode,
    ownership_override: bool = False,
    profile_name: str | None = None,
    profile: str | None = None,
) -> str:
    """Set the processor compatibility mode on an LPAR's partition profile.

    The HMC accepts ``lpar_proc_compat_mode`` only on a partition profile, so
    this changes the named profile (the partition's default profile when
    ``profile_name`` is omitted) and reports which one. Read it back with
    ``hmc_get_lpar_proc_compat``.

    WARNING: This changes LPAR configuration on the selected HMC.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Partition name or UUID from ``hmc_list_lpars``.
        mode: Desired mode supported by the system; enumerate legal values with
            ``hmc_get_proc_compat_modes``.
        ownership_override: Bypass ownership rejection after operator approval.
        profile_name: Partition profile to change; the default profile when omitted.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return with_client(
        lambda hmc: configure_lpar_processor_compatibility(
            hmc,
            lpar_name_or_uuid,
            mode,
            profile_name=profile_name,
            ownership_override=ownership_override,
            system_name_or_uuid=system_name_or_uuid,
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="lpar.configure", target_kind="lpar")
def hmc_configure_lpar(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    settings: LparSettings,
    ownership_override: bool = False,
    profile: str | None = None,
) -> LparSettingsResult:
    """Configure partition settings and verify preserved/supplied values.

    A running time-reference partition can govern hypervisor/service-processor
    time. This changes partition configuration, without activating it or changing
    a guest clock. Migration/restart capability flags do not prove readiness.
    Linux role field selection, including false, declares Linux intent without
    proving the installed OS. KVM selection requires inactivity and no operation
    in progress; enablement requires existing POWER10, dedicated never-share CPUs,
    zero keystore and no persistent memory. CPUs and compatibility are unchanged.
    Management authority must be classic or held by this HMC; no takeover occurs.
    Readback proves configuration only, not KVM/NovaLink guest deployment.
    Inspect unknown, unavailable or mismatching outcomes before retrying.

    Args:
        system_name_or_uuid: Containing managed system name or UUID.
        lpar_name_or_uuid: AIX/Linux or IBM i partition name or UUID on that system.
        settings: Nonempty canonical settings; None preserves, False/0 are explicit.
        ownership_override: Bypass ownership rejection after operator approval.
        profile: Configured HMC connection profile, or environment default.
    """
    return with_client(
        lambda hmc: configure_lpar_settings(
            hmc,
            lpar_name_or_uuid,
            settings,
            system_name_or_uuid=system_name_or_uuid,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.get_settings", target_kind="lpar")
def hmc_get_lpar_settings(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile: str | None = None,
) -> LparSettings:
    """Read the five C01 partition settings after proving system containment.

    Args:
        system_name_or_uuid: Containing managed system name or UUID.
        lpar_name_or_uuid: AIX/Linux or IBM i partition name or UUID on that system.
        profile: Configured HMC connection profile, or environment default.
    """
    return with_client(
        lambda hmc: read_lpar_settings(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        ),
        profile=profile,
    )


@tool(effect="mutate", operation="lpar.configure_reporting", target_kind="lpar")
def hmc_configure_lpar_reporting(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    settings: LparReportingSettings,
    ownership_override: bool = False,
    profile: str | None = None,
) -> LparReportingSettingsResult:
    """Configure partition reporting once and verify applicable readback.

    IPL source (a/b/c/d), restricted I/O and electronic reporting require IBM i.
    Work group accepts integer or "none"; omitted values are preserved.
    Redundant error-path reporting is creation-only and is refused here.
    Readback proves configuration, not guest IPL or dynamic runtime behavior.

    Args:
        system_name_or_uuid: Containing managed system name or UUID.
        lpar_name_or_uuid: Partition name or UUID on that system.
        settings: Typed C02 update fields; bool false, zero and "none" are explicit.
        ownership_override: Explicitly bypass ownership rejection.
        profile: TOML connection profile, or environment-default HMC when omitted.
    """
    return with_client(
        lambda hmc: configure_lpar_reporting_settings(
            hmc,
            lpar_name_or_uuid,
            settings,
            system_name_or_uuid=system_name_or_uuid,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.get_reporting_settings", target_kind="lpar")
def hmc_get_lpar_reporting_settings(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile: str | None = None,
) -> LparReportingSettings:
    """Read applicable C02 update settings after proving partition containment.

    IBM i-only fields are omitted (null) for AIX/Linux; redundant reporting is
    creation-only and is not requested by this update-settings reader.

    Args:
        system_name_or_uuid: Containing managed system name or UUID.
        lpar_name_or_uuid: Partition name or UUID on that system.
        profile: TOML connection profile, or environment-default HMC when omitted.
    """
    return with_client(
        lambda hmc: read_lpar_reporting_settings(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.get_vtpm_settings", target_kind="lpar")
def hmc_get_lpar_vtpm_settings(
    system_name_or_uuid: str, lpar_name_or_uuid: str, profile: str | None = None
) -> VtpmState:
    """Read one contained AIX/Linux partition's vTPM configuration.

    Args:
        system_name_or_uuid: Selected managed system name or UUID.
        lpar_name_or_uuid: Contained AIX/Linux partition name or UUID.
        profile: Optional configured HMC connection profile.
    """
    return with_client(
        lambda hmc: read_lpar_vtpm(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        ),
        profile=profile,
    )


@tool(effect="destructive", operation="lpar.configure_vtpm", target_kind="lpar")
def hmc_configure_lpar_vtpm(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    settings: LparVtpmSettings,
    acknowledge_data_loss: StrictBool = False,
    ownership_override: bool = False,
    profile: str | None = None,
) -> VtpmResult:
    """Configure an inactive AIX/Linux partition's advertised vTPM settings.

    No implicit reset, replacement, power, profile or key-management operation.
    Acknowledgement is required before disabling an enabled instance; keys and
    stored TPM data can be lost. Inspect uncertain outcomes before retrying.

    Args:
        system_name_or_uuid: Selected managed system name or UUID.
        lpar_name_or_uuid: Contained AIX/Linux partition name or UUID.
        settings: Three typed optional fields; omitted values are preserved.
        acknowledge_data_loss: Explicit consent to losing keys/data on enabled disable.
        ownership_override: Operator-approved ownership bypass, separate from data-loss consent.
        profile: Optional configured HMC connection profile.
    """
    return with_client(
        lambda hmc: configure_lpar_vtpm(
            hmc,
            lpar_name_or_uuid,
            settings,
            system_name_or_uuid=system_name_or_uuid,
            acknowledge_data_loss=acknowledge_data_loss,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )


@tool(effect="read", operation="lpar.get_security_policy", target_kind="lpar")
def hmc_get_lpar_security_policy(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile: str | None = None,
) -> SecurityPolicyState:
    """Read contained partition pending policy and separate current secure boot.

    Configuration proves no guest OS, runtime enforcement or key readiness.

    Args:
        system_name_or_uuid: Containing managed system name or UUID.
        lpar_name_or_uuid: Contained LPAR partition name or UUID.
        profile: HMC connection profile, distinct from a logical-partition profile.
    """
    return with_client(
        lambda hmc: read_lpar_security_policy(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        ),
        profile=profile,
    )


@tool(
    effect="destructive", operation="lpar.configure_security_policy", target_kind="lpar"
)
def hmc_configure_lpar_security_policy(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    settings: LparSecurityPolicy,
    ownership_override: bool = False,
    profile: str | None = None,
) -> SecurityPolicyResult:
    """Configure security policy once; verify pending policy separately from current boot.

    WARNING: Secure boot can be weakened. Signed-update/Linux enables are irreversible;
    store disable requires HMC emptiness checks and nonzero shrink requires Power11.
    Enabling/increasing a store or enabling dynamic features requires inactivity.
    Bypass provisions PK without authenticating against the old PK. Linux field selection
    declares intent only. Configuration proves no guest OS, enforcement or key readiness.
    Inspect uncertain outcomes before retrying; no power, profile, key action or replay.

    Args:
        system_name_or_uuid: Containing managed system name or UUID.
        lpar_name_or_uuid: Contained LPAR partition name or UUID.
        settings: Nonempty strict policy; omitted fields preserve, false/zero are explicit.
        ownership_override: Operator-approved ownership bypass.
        profile: HMC connection profile, distinct from a logical-partition profile.
    """
    return with_client(
        lambda hmc: configure_lpar_security_policy(
            hmc,
            lpar_name_or_uuid,
            settings,
            system_name_or_uuid=system_name_or_uuid,
            ownership_override=ownership_override,
        ),
        profile=profile,
    )
