"""Builders for HMC ``JobRequest`` XML documents.

Request serialization is kept separate from job outcomes and polling helpers so
changes to a ``do/*`` payload do not require editing lifecycle code.
"""

from __future__ import annotations

from typing import Literal, get_args

from ..xmlutil import WEB_NS, escapes_string_arguments

LuType = Literal["THIN", "THICK"]
DeviceType = Literal["VirtualIO_Disk", "VirtualIO_Image"]
RemoteRestartOperation = Literal["validate", "recover", "restart", "cleanup", "cancel"]
BootMode = Literal["norm", "dd", "ds", "of", "sms"]
PowerOnOperationType = Literal["activate"]
# The PowerOn job's own keylock spelling, not the creation-time ``Keylock``
# vocabulary in ``documents/lpar.py`` (``normal``/``manual``/``auto``).
PowerOnKeylock = Literal["manual", "norm"]
PowerOffOperation = Literal["shutdown", "osshutdown", "dumprestart"]
REMOTE_RESTART_OPERATIONS = frozenset(get_args(RemoteRestartOperation))
LU_TYPES = frozenset(get_args(LuType))
DEVICE_TYPES = frozenset(get_args(DeviceType))
BOOT_MODES = frozenset(get_args(BootMode))
POWER_ON_OPERATION_TYPES = frozenset(get_args(PowerOnOperationType))
POWER_ON_KEYLOCKS = frozenset(get_args(PowerOnKeylock))
POWER_OFF_OPERATIONS = frozenset(get_args(PowerOffOperation))

_JOB_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<JobRequest xmlns="{ns}" xmlns:JobRequest="{ns}" schemaVersion="V1_0">
  <Metadata><Atom/></Metadata>
  <RequestedOperation kb="CUR" kxe="false" schemaVersion="V1_0">
    <Metadata><Atom/></Metadata>
    <OperationName kb="ROR" kxe="false">{operation}</OperationName>
    <GroupName kb="ROR" kxe="false">{group}</GroupName>
    <ProgressType kb="ROR" kxe="false">DISCRETE</ProgressType>
  </RequestedOperation>
  <JobParameters kb="CUR" kxe="false" schemaVersion="V1_0">
    <Metadata><Atom/></Metadata>
{parameters}
  </JobParameters>
</JobRequest>
"""

_PARAM_TEMPLATE = """    <JobParameter schemaVersion="V1_0">
      <Metadata><Atom/></Metadata>
      <ParameterName kb="ROR" kxe="false">{name}</ParameterName>
      <ParameterValue kb="CUR" kxe="false">{value}</ParameterValue>
    </JobParameter>"""


@escapes_string_arguments
def build_job_request(
    operation: str,
    group: str,
    parameters: dict[str, str] | None = None,
) -> str:
    """Build and XML-escape a JobRequest document for a ``do/*`` operation."""
    params_xml = ""
    if parameters:
        params_xml = "\n".join(
            _PARAM_TEMPLATE.format(name=name, value=value)
            for name, value in parameters.items()
        )
    return _JOB_TEMPLATE.format(
        ns=WEB_NS, operation=operation, group=group, parameters=params_xml
    )


def validate_power_on_activation(
    bootmode: BootMode,
    operation_type: PowerOnOperationType | None,
    keylock: PowerOnKeylock | None,
) -> None:
    """Validate the PowerOn activation vocabularies for direct callers.

    Exposed the way ``validate_logical_unit_types`` is, because the builder is
    not the only place that must refuse: ``submit_power_on_lpar`` returns early when the
    partition is already running, reaching an activation-consuming exit without
    ever building a document. Both sites call this, so the permitted sets and
    the wording live in one place.
    """
    if bootmode not in BOOT_MODES:
        allowed = ", ".join(sorted(BOOT_MODES))
        raise ValueError(f"PowerOn boot mode must be one of: {allowed}")
    if operation_type is not None and operation_type not in POWER_ON_OPERATION_TYPES:
        allowed = ", ".join(sorted(POWER_ON_OPERATION_TYPES))
        raise ValueError(f"PowerOn operation type must be one of: {allowed}")
    if keylock is not None and keylock not in POWER_ON_KEYLOCKS:
        allowed = ", ".join(sorted(POWER_ON_KEYLOCKS))
        raise ValueError(f"PowerOn keylock must be one of: {allowed}")


def power_on_lpar_job(
    profile_uuid: str | None = None,
    bootmode: BootMode = "norm",
    operation_type: PowerOnOperationType | None = None,
    keylock: PowerOnKeylock | None = None,
) -> str:
    """Build a PowerOn request for one logical partition.

    ``bootmode``, ``operation_type`` and ``keylock`` are the job's own closed
    vocabularies and are refused here, before any XML is built. ``profile_uuid``
    is the UUID of the partition profile to activate against, not a connection
    profile; it has no vocabulary to check against, so an empty value is omitted
    rather than refused. An omitted ``keylock`` sends no parameter, leaving the
    keylock position to the HMC. A call passing none of the four emits the
    document this builder has always emitted.

    ``operation_type`` is validated but never sent: ``activate``, its only
    value, is the job's default, and a V10R3 HMC refuses the ``OperationType``
    parameter with "Parameter 'OperationType' is not allowed for this Job."
    (#1392, ADR 0161). Stating it emits the document omitting it emits.
    """
    validate_power_on_activation(bootmode, operation_type, keylock)
    parameters = {"force": "false", "novsi": "true", "bootmode": bootmode}
    if profile_uuid:
        parameters["LogicalPartitionProfile"] = profile_uuid
    if keylock:
        parameters["keylock"] = keylock
    return build_job_request("PowerOn", "LogicalPartition", parameters)


def validate_power_off_operation(
    operation: PowerOffOperation,
    allow_dump_restart: bool,
    *,
    immediate: bool,
    restart: bool,
) -> None:
    """Validate the PowerOff shutdown vocabulary and its gated combinations.

    Exposed the way ``validate_power_on_activation`` is: ``power_off_lpar`` reaches the
    ADR 0092 ownership leg — which can write an audited override record — before it
    reaches a builder, so it calls this first. ``dumprestart`` crashes the partition
    and takes a platform dump, and no layer below the CLI asks for confirmation, so
    it is refused unless the caller opts in by name. ``shutdown`` with ``restart``
    and without ``immediate`` is the same dump restart under another spelling
    (operator panel function 22), so it is refused outright (ADR 0164 amendment).
    """
    if operation not in POWER_OFF_OPERATIONS:
        allowed = ", ".join(sorted(POWER_OFF_OPERATIONS))
        raise ValueError(f"PowerOff operation must be one of: {allowed}")
    if operation == "dumprestart" and not allow_dump_restart:
        raise ValueError(
            "PowerOff operation 'dumprestart' crashes the partition and takes a "
            "platform dump; pass allow_dump_restart=True (CLI: --allow-dump-restart) "
            "to request it."
        )
    if operation == "shutdown" and restart and not immediate:
        raise ValueError(
            "PowerOff operation 'shutdown' with restart=true and immediate=false is a "
            "dump restart, which crashes the partition and takes a platform dump. "
            "Pass immediate=true (CLI: --immediate) for an immediate restart, use "
            "operation osshutdown for an operating-system restart, or use "
            "hmc_dump_restart_lpar to request the dump."
        )


def power_off_lpar_job(
    immediate: bool = False,
    restart: bool = False,
    operation: PowerOffOperation = "shutdown",
    allow_dump_restart: bool = False,
) -> str:
    """Build a PowerOff request for one logical partition.

    ``operation`` is the job's own closed vocabulary and is refused here, before any
    XML is built; the vendor's fourth member ``dumpretry`` is documented but not
    admitted (ADR 0164). ``restart`` turns the power off into a restart, which is
    what kdive's ``cycle`` and ``reset`` map to. A call passing none of the three
    emits the document this builder has always emitted.
    """
    validate_power_off_operation(
        operation, allow_dump_restart, immediate=immediate, restart=restart
    )
    return build_job_request(
        "PowerOff",
        "LogicalPartition",
        {
            "immediate": "true" if immediate else "false",
            "restart": "true" if restart else "false",
            "operation": operation,
        },
    )


def power_on_system_job() -> str:
    return build_job_request("PowerOn", "ManagedSystem")


def power_off_system_job(immediate: bool = False) -> str:
    parameters = {"immediate": "true"} if immediate else None
    return build_job_request("PowerOff", "ManagedSystem", parameters)


def power_on_vios_job() -> str:
    return build_job_request("PowerOn", "VirtualIOServer")


def power_off_vios_job(immediate: bool = False) -> str:
    parameters = {"immediate": "true"} if immediate else None
    return build_job_request("PowerOff", "VirtualIOServer", parameters)


def validate_logical_unit_types(
    lu_type: LuType, device_type: DeviceType
) -> tuple[LuType, DeviceType]:
    """Validate logical-unit serialization vocabularies for direct callers."""
    if lu_type not in LU_TYPES:
        raise ValueError(
            f"Invalid lu_type {lu_type!r}. Must be one of: {', '.join(sorted(LU_TYPES))}"
        )
    if device_type not in DEVICE_TYPES:
        raise ValueError(
            f"Invalid device_type {device_type!r}. "
            f"Must be one of: {', '.join(sorted(DEVICE_TYPES))}"
        )
    return lu_type, device_type


def create_logical_unit_job(
    lu_name: str,
    lu_size_gib: int,
    lu_type: LuType = "THIN",
    device_type: DeviceType = "VirtualIO_Disk",
    cloned_from: str | None = None,
) -> str:
    """Build a CreateLogicalUnit request against a Cluster/SSP."""
    validate_logical_unit_types(lu_type, device_type)
    parameters: dict[str, str] = {
        "TierUDID": "",
        "LUName": lu_name,
        "LUSize": str(lu_size_gib),
        "LUType": lu_type,
        "DeviceType": device_type,
    }
    if cloned_from:
        parameters["ClonedFrom"] = cloned_from
    return build_job_request("CreateLogicalUnit", "Cluster", parameters)


def delete_logical_unit_job(lu_udid: str) -> str:
    """Build a DeleteLogicalUnit request against a Cluster/SSP."""
    return build_job_request(
        "DeleteLogicalUnit", "Cluster", {"LogicalUnitUDID": lu_udid}
    )


def _migrate_job(
    operation: str,
    target_system: str,
    target_profile_name: str | None = None,
    destination_lpar_id: str | None = None,
    shared_proc_pool_id: str | None = None,
    wait_time: int | None = None,
) -> str:
    parameters = {"TargetManagedSystemName": target_system}
    if target_profile_name:
        parameters["TargetProfileName"] = target_profile_name
    if destination_lpar_id:
        parameters["DestinationLparID"] = destination_lpar_id
    if shared_proc_pool_id:
        parameters["SharedProcPoolID"] = shared_proc_pool_id
    if wait_time is not None:
        parameters["WaitTime"] = str(wait_time)
    return build_job_request(operation, "LogicalPartition", parameters)


def migrate_lpar_job(
    target_system: str,
    target_profile_name: str | None = None,
    destination_lpar_id: str | None = None,
    shared_proc_pool_id: str | None = None,
    wait_time: int | None = None,
) -> str:
    """Build an LPAR migration request."""
    return _migrate_job(
        "Migrate",
        target_system,
        target_profile_name,
        destination_lpar_id,
        shared_proc_pool_id,
        wait_time,
    )


def migrate_validate_lpar_job(
    target_system: str,
    target_profile_name: str | None = None,
    destination_lpar_id: str | None = None,
    shared_proc_pool_id: str | None = None,
    wait_time: int | None = None,
) -> str:
    """Build an LPAR migration validation request."""
    return _migrate_job(
        "MigrateValidate",
        target_system,
        target_profile_name,
        destination_lpar_id,
        shared_proc_pool_id,
        wait_time,
    )


def migrate_abort_lpar_job() -> str:
    """Build an LPAR migration abort request."""
    return build_job_request("MigrateAbort", "LogicalPartition")


def migrate_recover_lpar_job() -> str:
    """Build an LPAR migration recovery request."""
    return build_job_request("MigrateRecover", "LogicalPartition")


def remote_restart_lpar_job(
    operation: RemoteRestartOperation,
    managed_system: str,
    logical_partition_uuid: str,
    *,
    target_managed_system: str | None = None,
    target_managed_system_uuid: str | None = None,
    use_current_data: bool = False,
    retain_devices: bool = False,
) -> str:
    """Build a RemoteRestart request using its dedicated parameter vocabulary."""
    _validate_remote_restart(
        operation,
        target_managed_system,
        target_managed_system_uuid,
        use_current_data,
        retain_devices,
    )
    parameters = {
        "Operation": operation,
        "managedSystem": managed_system,
        "logicalPartitionUuid": logical_partition_uuid,
    }
    if target_managed_system:
        parameters["targetManagedSystem"] = target_managed_system
    if target_managed_system_uuid:
        parameters["targetManagedSystemUUID"] = target_managed_system_uuid
    if use_current_data:
        parameters["usecurrdata"] = "true"
    if retain_devices:
        parameters["retaindev"] = "true"
    return build_job_request("RemoteRestart", "LogicalPartition", parameters)


def _validate_remote_restart(
    operation: RemoteRestartOperation,
    target_managed_system: str | None,
    target_managed_system_uuid: str | None,
    use_current_data: bool,
    retain_devices: bool,
) -> None:
    """Validate the operation-specific RemoteRestart parameter vocabulary."""
    if operation not in REMOTE_RESTART_OPERATIONS:
        allowed = ", ".join(sorted(REMOTE_RESTART_OPERATIONS))
        raise ValueError(f"RemoteRestart operation must be one of: {allowed}")
    if operation != "cleanup" and not (
        target_managed_system or target_managed_system_uuid
    ):
        raise ValueError(
            f"RemoteRestart {operation!r} requires a target managed system"
        )
    if target_managed_system and target_managed_system_uuid:
        raise ValueError("Specify a target managed-system name or UUID, not both")
    if use_current_data and operation != "restart":
        raise ValueError("use_current_data is valid only for RemoteRestart 'restart'")
    if retain_devices and operation != "cleanup":
        raise ValueError("retain_devices is valid only for RemoteRestart 'cleanup'")


def deploy_partition_template_job(
    draft_template_uuid: str, target_system_uuid: str, memento: str
) -> str:
    """Build a PartitionTemplate deployment request."""
    return build_job_request(
        "Deploy",
        "PartitionTemplate",
        {
            "K_X_API_SESSION_MEMENTO": memento,
            "TargetUuid": target_system_uuid,
            "TemplateUuid": draft_template_uuid,
        },
    )
