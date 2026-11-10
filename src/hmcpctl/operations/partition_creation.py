"""Invocation-local native partition feature admission and evidence coordination."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparResources, LparSettings
from hmcpctl.documents.lpar_affinity_group import AffinityGroupID
from hmcpctl.documents.lpar_creation_memory import LparCreationMemory
from hmcpctl.documents.lpar_hardware_memory import hardware_memory_pairs
from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter
from hmcpctl.documents.lpar_start import LparStartSettings
from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.operations.lpar.affinity_group import created_affinity_group_step
from hmcpctl.operations.lpar.linux_roles import (
    read_creation_authority,
    read_role_capabilities,
)
from hmcpctl.operations.lpar.memory.bounds import require_memory_within_system
from hmcpctl.operations.lpar.memory.creation import (
    CreationMemoryAdmission,
    admit_creation_memory,
    confirm_creation_memory_identity,
    creation_memory_step,
)
from hmcpctl.operations.lpar.memory.hardware import (
    admit_hardware_memory,
    created_hardware_memory_step,
)
from hmcpctl.operations.lpar.security_policy import (
    admit_security_policy,
    read_lpar_security_policy,
    read_security_policy_capabilities,
)
from hmcpctl.operations.lpar.serial import (
    SerialAdmission,
    admit_serial_creation,
    confirm_serial_created,
    serial_creation_step,
)
from hmcpctl.operations.lpar.start import (
    StartAdmission,
    admit_start_creation,
    confirm_start_created,
    created_start_step,
)
from hmcpctl.operations.lpar.vtpm import admit_vtpm, read_vtpm_capabilities
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.lpar import complete_create_resources, resolve_system_cli_name
from hmcpctl.ssh.lpar_memory_layout import admit_memory_layout, get_lpar_memory_layout
from hmcpctl.ssh.lpar_vtpm import get_lpar_vtpm_state

if TYPE_CHECKING:
    from hmcpctl.operations.lpar.core import LparCreation


@dataclass
class _CreationFeatureCoordinator:
    hmc: HMCClient
    system_uuid: str
    system: str
    name: str
    environment: Literal["AIX/Linux", "OS400", "Virtual IO Server"]
    resources: LparResources
    partition_id: int | None = None
    max_virtual_slots: int | None = None
    virtual_serial_adapters: tuple[VirtualSerialAdapter, ...] = ()
    creation_memory: LparCreationMemory = field(default_factory=LparCreationMemory)
    start_settings: LparStartSettings = field(default_factory=LparStartSettings)
    memory_layout: LparMemoryLayout = field(default_factory=LparMemoryLayout)
    vtpm_settings: LparVtpmSettings = field(default_factory=LparVtpmSettings)
    security_policy: LparSecurityPolicy = field(default_factory=LparSecurityPolicy)
    hardware_mem_encryption: bool | None = None
    hardware_mem_expansion: bool | None = None
    affinity_group_id: AffinityGroupID | None = None
    controller_mode: bool = False
    serial_admission: SerialAdmission | None = field(default=None, init=False)
    memory_admission: CreationMemoryAdmission | None = field(default=None, init=False)
    start_admission: StartAdmission | None = field(default=None, init=False)
    entry: dict[str, Any] | None = field(default=None, init=False)
    facts: dict[str, WorkflowStep | None] = field(default_factory=dict, init=False)
    active_step: str | None = field(default=None, init=False)
    attempted_feature: str | None = field(default=None, init=False)

    @property
    def vios(self) -> bool:
        return self.environment == "Virtual IO Server"

    @property
    def kind(self) -> Literal["VirtualIOServer", "LogicalPartition"]:
        return "VirtualIOServer" if self.vios else "LogicalPartition"

    @property
    def serial_requested(self) -> bool:
        return bool(self.virtual_serial_adapters)

    @property
    def memory_requested(self) -> bool:
        return bool(self.creation_memory.pairs())

    @property
    def start_requested(self) -> bool:
        return self.start_settings.requested()

    @property
    def layout_requested(self) -> bool:
        return bool(self.memory_layout.pairs())

    @property
    def affinity_requested(self) -> bool:
        return self.affinity_group_id is not None

    @property
    def vtpm_requested(self) -> bool:
        return bool(self.vtpm_settings.pairs())

    @property
    def policy_requested(self) -> bool:
        return bool(self.security_policy.pairs())

    @property
    def native_features_requested(self) -> bool:
        return (
            bool(
                hardware_memory_pairs(
                    self.hardware_mem_encryption, self.hardware_mem_expansion
                )
            )
            or self.layout_requested
            or self.serial_requested
            or self.memory_requested
            or self.start_requested
            or self.affinity_requested
        )

    async def observe(
        self,
        field_name: str,
        observation: Awaitable[WorkflowStep | None] | WorkflowStep | None,
    ) -> WorkflowStep | None:
        self.active_step = field_name
        self.attempted_feature = field_name
        step = (
            observation
            if observation is None or isinstance(observation, WorkflowStep)
            else await observation
        )
        self.facts[field_name] = step
        self.active_step = None
        return step

    async def discover_mode(self, settings: LparSettings | None = None):
        if settings is not None:
            caps = await read_role_capabilities(
                self.hmc,
                self.system_uuid,
                need_kvm=settings.kvm_capable is not None,
                need_management=settings.powervm_mgmt_capable is not None,
            )
            self.controller_mode = caps.controller_mode
            return caps
        if self.affinity_requested or self.layout_requested:
            _, self.controller_mode = await read_creation_authority(
                self.hmc,
                self.system_uuid,
                need_management=False,
                context="Affinity group"
                if self.affinity_requested
                else "Memory layout",
            )
        elif self.serial_requested or self.memory_requested or self.start_requested:
            self.controller_mode = (
                await read_role_capabilities(
                    self.hmc, self.system_uuid, need_kvm=False, need_management=False
                )
            ).controller_mode
        return None

    async def admit_host_features(self) -> None:
        if self.vtpm_requested:
            self.attempted_feature = "vtpm_admission"
            admit_vtpm(
                self.vtpm_settings,
                await read_vtpm_capabilities(self.hmc, self.system_uuid),
            )
        if self.policy_requested:
            self.attempted_feature = "security_policy_admission"
            admit_security_policy(
                self.security_policy,
                await read_security_policy_capabilities(
                    self.hmc,
                    self.system_uuid,
                    **(
                        {
                            "need_power11": self.environment == "OS400"
                            and self.security_policy.secure_boot is not None
                        }
                        if not self.vios
                        else {}
                    ),
                ),
                environment=self.environment,
            )
        self.attempted_feature = "hardware_memory_admission"
        await admit_hardware_memory(
            self.hmc,
            self.system_uuid,
            self.hardware_mem_encryption,
            self.hardware_mem_expansion,
        )

    async def admit_partition_features(self) -> None:
        if self.vios and self.layout_requested:
            await admit_memory_layout(self.hmc.config, self.system, self.memory_layout)
        if self.serial_requested:
            self.attempted_feature = "serial_admission"
            self.serial_admission = await admit_serial_creation(
                self.hmc,
                self.system_uuid,
                name=self.name,
                partition_id=self.partition_id,
                maximum=self.max_virtual_slots,
                adapters=self.virtual_serial_adapters,
                kind=self.kind,
            )
        if not self.vios and self.start_requested:
            self.attempted_feature = "start_admission"
            self.start_admission = await admit_start_creation(
                self.hmc, self.system_uuid, self.system, self.start_settings
            )
        if not self.vios:
            resources = complete_create_resources(self.resources)
            if (
                (
                    self.policy_requested
                    or bool(
                        hardware_memory_pairs(
                            self.hardware_mem_encryption, self.hardware_mem_expansion
                        )
                    )
                    or self.layout_requested
                    or self.serial_requested
                    or self.memory_requested
                    or self.start_requested
                )
                and resources is not None
                and self.creation_memory.mem_mode != "shared"
            ):
                await require_memory_within_system(
                    self.hmc, self.system_uuid, resources
                )
            if self.memory_requested:
                self.system = await resolve_system_cli_name(
                    self.hmc.config, self.system_uuid
                )
        if self.memory_requested:
            self.attempted_feature = "creation_memory_admission"
            self.memory_admission = await admit_creation_memory(
                self.hmc,
                self.system_uuid,
                self.system,
                self.creation_memory,
                complete_create_resources(self.resources) or self.resources,
                kind=self.kind,
                environment=self.environment,
                name=self.name,
                **({"partition_id": self.partition_id} if not self.vios else {}),
            )
        if self.vios and self.start_requested:
            self.attempted_feature = "start_admission"
            self.start_admission = await admit_start_creation(
                self.hmc, self.system_uuid, self.system, self.start_settings
            )

    async def confirm_identity(
        self, created_lpar: dict[str, Any] | None = None
    ) -> None:
        if self.vios:
            self.attempted_feature = "created_identity"
            try:
                self.entry = await self.hmc.find_vios_by_name(
                    self.name, system_uuid=self.system_uuid
                )
            except HMCTLSFingerprintError:
                raise
            except Exception:
                if not self.native_features_requested:
                    raise
                # Native creation succeeded; discard unavailable lookup diagnostics.
                self.entry = None
            if (
                self.entry is None
                or (self.layout_requested and not isinstance(self.entry, dict))
                or not self.entry.get("UUID")
            ):
                raise ValueError("Created VIOS identity was not confirmed")
            if (self.policy_requested or self.native_features_requested) and (
                not isinstance(self.entry.get("UUID"), str)
                or not is_uuid(self.entry["UUID"])
                or not isinstance(self.entry.get("Resource"), dict)
                or self.entry["Resource"].get("PartitionName") != self.name
            ):
                raise ValueError("Created VIOS identity unconfirmed")
            if self.start_admission is not None:
                self.attempted_feature = "start_identity"
                self.entry = await confirm_start_created(
                    self.hmc,
                    self.start_admission,
                    self.entry,
                    self.name,
                    "Virtual IO Server",
                )
        elif created_lpar is not None:
            self.entry = created_lpar
            identity_ok = True
            confirmed_lpar = created_lpar
            if self.serial_admission is not None:
                try:
                    self.attempted_feature = "serial_identity"
                    confirmed_lpar = await confirm_serial_created(
                        self.hmc, self.serial_admission, created_lpar
                    )
                    self.entry = confirmed_lpar
                except HMCTLSFingerprintError:
                    raise
                except Exception:  # noqa: BLE001 — uncertain identity forbids stamping.
                    identity_ok = False
            if self.memory_admission is not None:
                try:
                    self.attempted_feature = "memory_identity"
                    confirmed_lpar = await confirm_creation_memory_identity(
                        self.hmc, self.memory_admission, created_lpar
                    )
                    self.entry = confirmed_lpar
                except HMCTLSFingerprintError:
                    raise
                except Exception:  # noqa: BLE001 — uncertain identity forbids stamping.
                    identity_ok = False
            if self.start_admission is not None:
                try:
                    self.attempted_feature = "start_identity"
                    confirmed_lpar = await confirm_start_created(
                        self.hmc,
                        self.start_admission,
                        created_lpar,
                        self.name,
                        self.environment,
                    )
                    self.entry = confirmed_lpar
                except HMCTLSFingerprintError:
                    raise
                except Exception:  # noqa: BLE001 — unknown identity forbids ownership stamping.
                    identity_ok = False
            self.entry = confirmed_lpar if identity_ok else None
        else:
            self.entry = None

    async def observe_before_followup(
        self, creation: LparCreation | None = None, cli_system: str | None = None
    ) -> None:
        if self.vios:
            await self._observe_vios()
            return
        assert creation is not None
        await self.observe(
            "serial_step",
            serial_creation_step(
                self.hmc,
                self.serial_admission,
                self.entry,
                controller_mode=self.controller_mode,
            )
            if self.serial_admission is not None
            else None,
        )
        await self.observe(
            "creation_memory_step",
            creation_memory_step(
                self.hmc,
                self.memory_admission,
                self.entry,
                controller_mode=self.controller_mode,
            )
            if self.memory_admission is not None
            else None,
        )
        await self.observe(
            "memory_layout_step",
            _created_memory_layout_step(
                self.hmc, cli_system, creation, controller_mode=self.controller_mode
            ),
        )
        await self.observe(
            "affinity_group_step",
            created_affinity_group_step(
                self.hmc,
                self.system_uuid,
                self.entry,
                creation.name,
                creation.partition_type,
                creation.affinity_group_id,
                controller_mode=self.controller_mode,
            ),
        )
        await self.observe(
            "hardware_memory_step",
            created_hardware_memory_step(
                self.hmc,
                self.system_uuid,
                self.entry,
                creation.name,
                creation.partition_type,
                creation.hardware_mem_encryption,
                creation.hardware_mem_expansion,
            ),
        )

    async def _observe_vios(self) -> None:
        verification_failed = False
        try:
            await self.confirm_identity()
            if self.memory_admission is not None:
                self.attempted_feature = "memory_identity"
                self.entry = await confirm_creation_memory_identity(
                    self.hmc, self.memory_admission, self.entry
                )
                memory_step = await self.observe(
                    "creation_memory_step",
                    creation_memory_step(
                        self.hmc,
                        self.memory_admission,
                        self.entry,
                        controller_mode=self.controller_mode,
                    ),
                )
                assert memory_step is not None
                if memory_step.status != "ok":
                    raise ValueError("Creation memory observation incomplete")
            if self.serial_admission is not None:
                self.attempted_feature = "serial_identity"
                self.entry = await confirm_serial_created(
                    self.hmc, self.serial_admission, self.entry
                )
                serial_step = await self.observe(
                    "serial_step",
                    serial_creation_step(
                        self.hmc,
                        self.serial_admission,
                        self.entry,
                        controller_mode=self.controller_mode,
                    ),
                )
                assert serial_step is not None
                if serial_step.status != "ok":
                    raise ValueError("Serial creation observation incomplete")
            if self.layout_requested:
                if self.controller_mode:
                    raise ValueError(
                        "Controller current ratio mappings unavailable; no named profile was read"
                    )
                self.attempted_feature = "memory_layout_step"
                observed_layout = await get_lpar_memory_layout(
                    self.hmc.config, self.system, self.name
                )
                mismatch = any(
                    getattr(observed_layout, field) != value
                    for field, value in self.memory_layout.pairs()
                )
                self.facts["memory_layout_step"] = WorkflowStep(
                    "memory_layout", "error" if mismatch else "ok", observed_layout
                )
                if mismatch:
                    raise ValueError(
                        "Created VIOS memory ratios unavailable or mismatched"
                    )
            if self.vtpm_settings is not None and self.vtpm_requested:
                self.attempted_feature = "vtpm_step"
                observed = await get_lpar_vtpm_state(
                    self.hmc.config, self.system, self.name, vios=True
                )
                mismatch = any(
                    getattr(observed.settings, field)
                    != getattr(self.vtpm_settings, field)
                    for field, _ in self.vtpm_settings.pairs()
                )
                self.facts["vtpm_step"] = WorkflowStep(
                    "vtpm", "error" if mismatch else "ok", observed
                )
                if mismatch:
                    raise ValueError(
                        "Created VIOS vTPM readback differs from the requested fields"
                    )
            if self.security_policy is not None and self.policy_requested:
                assert self.entry is not None
                self.attempted_feature = "security_policy_step"
                policy = await read_lpar_security_policy(
                    self.hmc,
                    self.entry["UUID"],
                    system_name_or_uuid=self.system_uuid,
                    vios=True,
                )
                mismatch = any(
                    getattr(policy.settings, field)
                    != getattr(self.security_policy, field)
                    for field, _ in self.security_policy.pairs()
                )
                self.facts["security_policy_step"] = WorkflowStep(
                    "security_policy", "error" if mismatch else "ok", policy
                )
                if mismatch:
                    raise ValueError(
                        "Created VIOS policy readback differs from requested fields"
                    )
        except HMCTLSFingerprintError as error:
            error.add_note(
                "VIOS exists or may exist after creation submission. Inspect before "
                "retrying; no replay or rollback performed."
            )
            raise
        except Exception as error:
            if (
                not self.serial_requested
                and not self.memory_requested
                and not self.start_requested
                and not isinstance(error, (HMCError, ValueError, PermissionError))
            ):
                raise
            if self.native_features_requested:
                verification_failed = True
            elif self.policy_requested:
                raise HMCError(
                    "VIOS exists or may exist; creation verification unavailable or mismatched. Inspect before retrying. No second create or rollback performed"
                ) from error
            else:
                raise HMCError(
                    f"VIOS creation returned, but verification is unavailable or mismatched: {error}. The VIOS exists or may exist; inspect before retrying. No second create or rollback performed"
                ) from error
        if verification_failed:
            raise HMCError(
                "VIOS exists or may exist; creation verification unavailable or mismatched. Inspect before retrying. No second create or rollback performed"
                if self.layout_requested
                or self.serial_requested
                or self.memory_requested
                or self.start_requested
                else f"VIOS exists or may exist; {'affinity group' if self.affinity_requested else 'hardware memory access'} readback-unavailable. Inspect before retrying. No second create or rollback performed"
            )
        assert (
            self.entry is not None
        )  # Successful identity verification above requires an entry.
        hardware_step = await self.observe(
            "hardware_memory_step",
            created_hardware_memory_step(
                self.hmc,
                self.system_uuid,
                self.entry,
                self.name,
                "Virtual IO Server",
                self.hardware_mem_encryption,
                self.hardware_mem_expansion,
                vios=True,
            ),
        )
        if hardware_step is not None and hardware_step.status != "ok":
            raise HMCError(
                f"VIOS exists or may exist; hardware memory access {hardware_step.result['status']}. "
                "Inspect before retrying. No second create or rollback performed"
            )
        affinity_step = await self.observe(
            "affinity_group_step",
            created_affinity_group_step(
                self.hmc,
                self.system_uuid,
                self.entry,
                self.name,
                "Virtual IO Server",
                self.affinity_group_id,
                controller_mode=self.controller_mode,
                vios=True,
            ),
        )
        if affinity_step is not None and affinity_step.status != "ok":
            raise HMCError(
                f"VIOS exists or may exist; affinity group {affinity_step.result['status']}. "
                f"Observed {affinity_step.result['observed']}; "
                f"observation status {affinity_step.result['observation_status']}. "
                "Inspect before retrying. No second create or rollback performed"
            ) from None

    async def observe_start(self) -> WorkflowStep | None:
        if self.start_admission is None:
            return None
        step = await self.observe(
            "start_step" if self.vios else "settings_step",
            created_start_step(
                self.hmc,
                self.start_admission,
                self.entry,
                self.name,
                self.environment,
                controller_mode=self.controller_mode,
            ),
        )
        assert step is not None
        if self.vios and step.status != "ok":
            raise HMCError(
                "VIOS exists or may exist; start configuration unverified. Inspect before retrying; no rollback performed"
            )
        return step

    async def observe_after_followup(
        self,
        creation: LparCreation,
        cli_system: str | None,
        *,
        verify_before_apply: bool,
    ) -> None:
        await self.observe(
            "vtpm_step",
            _created_vtpm_step(self.hmc, cli_system, creation)
            if verify_before_apply
            else None,
        )
        assert self.entry is not None
        await self.observe(
            "security_policy_step",
            _created_security_policy_step(
                self.hmc, self.system_uuid, creation, self.entry
            ),
        )

    async def observe_after_apply(self, apply_step: WorkflowStep | None) -> None:
        if (
            self.memory_admission is not None
            and apply_step is not None
            and apply_step.status == "ok"
        ):
            memory_step = self.facts["creation_memory_step"]
            assert memory_step is not None
            await self.observe(
                "creation_memory_step",
                creation_memory_step(
                    self.hmc,
                    self.memory_admission,
                    self.entry,
                    controller_mode=self.controller_mode,
                    current=True,
                    saved=memory_step.result.saved,
                ),
            )


async def _created_memory_layout_step(
    hmc: HMCClient,
    cli_system: str | None,
    creation: LparCreation,
    *,
    controller_mode: bool = False,
) -> WorkflowStep | None:
    from hmcpctl.operations.lpar.core import MemoryLayoutVerification

    requested = creation.memory_layout
    names = tuple(name for name, _ in requested.pairs())
    if not names:
        return None
    observed: dict[str, object] = {}
    warnings: tuple[str, ...] = ()
    if controller_mode:
        warnings = (
            "Controller current configured-value mappings are not established; no named profile was read",
        )
    elif cli_system is None:
        warnings = (
            "Created native identity or CLI system unavailable; layout unverified",
        )
    else:
        try:
            layout = await get_lpar_memory_layout(hmc.config, cli_system, creation.name)
            observed = {
                name: getattr(layout, name)
                for name in names
                if getattr(layout, name) is not None
            }
        except HMCTLSFingerprintError:
            raise
        except (HMCError, OSError, ValueError, PermissionError):
            warnings = (
                "Saved memory layout readback unavailable; inspect before retrying",
            )
    unavailable = tuple(name for name in names if name not in observed)
    mismatch = any(
        value != getattr(requested, name) for name, value in observed.items()
    )
    status = "mismatch" if mismatch else "unavailable" if unavailable else "verified"
    verification = MemoryLayoutVerification(
        status, requested, observed, unavailable, warnings
    )
    return WorkflowStep(
        "memory_layout", "ok" if status == "verified" else "error", verification
    )


async def _created_security_policy_step(
    hmc: HMCClient,
    system_uuid: str,
    creation: LparCreation,
    created_lpar: dict[str, Any],
) -> WorkflowStep | None:
    if not creation.security_policy.pairs():
        return None
    try:
        uuid = created_lpar.get("UUID")
        if (
            not isinstance(uuid, str)
            or not is_uuid(uuid)
            or (created_lpar.get("Resource") or {}).get("PartitionName")
            != creation.name
        ):
            raise ValueError("Created partition identity unconfirmed")
        observed = await read_lpar_security_policy(
            hmc, uuid, system_name_or_uuid=system_uuid
        )
        matches = observed.environment == creation.partition_type and all(
            getattr(observed.settings, name) == getattr(creation.security_policy, name)
            for name, _ in creation.security_policy.pairs()
        )
    except HMCTLSFingerprintError:
        raise
    except (HMCError, ValueError, PermissionError):
        return WorkflowStep(
            "security_policy",
            "error",
            "Partition exists or may exist; policy unverified. Inspect before retrying",
        )
    return WorkflowStep("security_policy", "ok" if matches else "error", observed)


async def _created_vtpm_step(
    hmc: HMCClient, cli_system: str | None, creation: LparCreation
) -> WorkflowStep | None:
    if not creation.vtpm_settings.pairs():
        return None
    try:
        if cli_system is None:
            raise ValueError("Native vTPM creation did not retain its system identity")
        observed = await get_lpar_vtpm_state(hmc.config, cli_system, creation.name)
    except HMCTLSFingerprintError:
        raise
    except (HMCError, ValueError, PermissionError) as error:
        return WorkflowStep(
            "vtpm", "error", f"Partition exists; vTPM unverified: {error}"
        )
    matches = all(
        getattr(observed.settings, name) == getattr(creation.vtpm_settings, name)
        for name, _ in creation.vtpm_settings.pairs()
    )
    return WorkflowStep("vtpm", "ok" if matches else "error", observed)
