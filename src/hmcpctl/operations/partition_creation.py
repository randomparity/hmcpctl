"""Invocation-local native partition feature admission and evidence coordination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from functools import partial
from types import MappingProxyType
from typing import Any, Literal, cast

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparReportingSettings, LparResources, LparSettings
from hmcpctl.documents.lpar_affinity_group import AffinityGroupID
from hmcpctl.documents.lpar_creation_memory import LparCreationMemory
from hmcpctl.documents.lpar_hardware_memory import hardware_memory_pairs
from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter
from hmcpctl.documents.lpar_start import LparStartSettings
from hmcpctl.documents.lpar_vtpm import LparVtpmSettings
from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.operations.lpar.affinity_group import created_affinity_group_step
from hmcpctl.operations.lpar.creation.serial import (
    SerialAdmission,
    admit_serial_creation,
    confirm_serial_created,
    serial_creation_step,
)
from hmcpctl.operations.lpar.creation.start import (
    StartAdmission,
    admit_start_creation,
    confirm_start_created,
    created_start_step,
)
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
from hmcpctl.operations.lpar.vtpm import admit_vtpm, read_vtpm_capabilities
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.lpar import complete_create_resources, resolve_system_cli_name
from hmcpctl.ssh.lpar_memory_layout import admit_memory_layout, get_lpar_memory_layout
from hmcpctl.ssh.lpar_vtpm import get_lpar_vtpm_state


@dataclass(frozen=True)
class MemoryLayoutVerification:
    """Requested creation values and an honest saved-configuration observation."""

    status: Literal["verified", "mismatch", "unavailable"]
    requested: LparMemoryLayout
    observed: dict[str, object]
    unavailable: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _CreationObservation:
    label: str
    requested: bool
    completed: bool = False
    step: WorkflowStep | None = None


@dataclass(frozen=True)
class _CreationFeatureInputs:
    """Normalized native feature requests shared by admission and observation."""

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
    settings: LparSettings = field(default_factory=LparSettings)
    reporting_settings: LparReportingSettings = field(
        default_factory=LparReportingSettings
    )
    physical_io: LparPhysicalIO = field(default_factory=LparPhysicalIO)
    apply_profile: bool | None = None

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
    def hardware_requested(self) -> bool:
        return bool(
            hardware_memory_pairs(
                self.hardware_mem_encryption, self.hardware_mem_expansion
            )
        )

    @property
    def profile_features_requested(self) -> bool:
        return (
            self.layout_requested
            or self.serial_requested
            or self.memory_requested
            or self.start_requested
            or self.affinity_requested
        )

    @property
    def native_features_requested(self) -> bool:
        return self.hardware_requested or self.profile_features_requested


@dataclass(frozen=True)
class _CreationObservations:
    """One definition of selected and completed creation evidence, including start."""

    settings_step: _CreationObservation
    reporting_step: _CreationObservation
    vtpm_step: _CreationObservation
    physical_io_step: _CreationObservation
    security_policy_step: _CreationObservation
    hardware_memory_step: _CreationObservation
    memory_layout_step: _CreationObservation
    serial_step: _CreationObservation
    creation_memory_step: _CreationObservation
    affinity_group_step: _CreationObservation
    apply_step: _CreationObservation
    start_step: _CreationObservation

    @classmethod
    def selected(cls, inputs: _CreationFeatureInputs) -> _CreationObservations:
        return cls(
            settings_step=_CreationObservation(
                "settings", bool(inputs.settings.pairs()) or inputs.start_requested
            ),
            reporting_step=_CreationObservation(
                "reporting_settings", bool(inputs.reporting_settings.pairs())
            ),
            vtpm_step=_CreationObservation("vtpm", inputs.vtpm_requested),
            physical_io_step=_CreationObservation(
                "physical_io", inputs.physical_io.requested
            ),
            security_policy_step=_CreationObservation(
                "security_policy", inputs.policy_requested
            ),
            hardware_memory_step=_CreationObservation(
                "hardware_memory", inputs.hardware_requested
            ),
            memory_layout_step=_CreationObservation(
                "memory_layout", inputs.layout_requested
            ),
            serial_step=_CreationObservation(
                "virtual_serial_adapters", inputs.serial_requested
            ),
            creation_memory_step=_CreationObservation(
                "creation_memory", inputs.memory_requested
            ),
            affinity_group_step=_CreationObservation(
                "affinity_group", inputs.affinity_requested
            ),
            apply_step=_CreationObservation(
                "apply_profile", inputs.apply_profile is not None
            ),
            start_step=_CreationObservation("settings", inputs.start_requested),
        )

    def _items(self) -> tuple[tuple[str, _CreationObservation], ...]:
        return tuple(
            (item.name, cast(_CreationObservation, getattr(self, item.name)))
            for item in fields(self)
        )

    def record(self, name: str, step: WorkflowStep | None) -> _CreationObservations:
        observation = dict(self._items())[name]
        return replace(self, **{name: replace(observation, completed=True, step=step)})

    def public_items(self) -> tuple[tuple[str, _CreationObservation], ...]:
        return tuple(
            (name, value) for name, value in self._items() if name != "start_step"
        )

    def public_steps(self) -> Mapping[str, WorkflowStep | None]:
        return MappingProxyType(
            {
                name: observation.step
                for name, observation in self.public_items()
                if observation.completed
            }
        )

    @property
    def verified(self) -> bool:
        return all(
            not observation.requested
            or (
                observation.completed
                and observation.step is not None
                and observation.step.status == "ok"
            )
            for name, observation in self.public_items()
            if name != "apply_step"
        )

    def with_public_steps(
        self, values: Mapping[str, WorkflowStep | None]
    ) -> _CreationObservations:
        return replace(
            self,
            **{
                name: replace(observation, completed=True, step=values.get(name))
                for name, observation in self.public_items()
            },
        )

    def interrupted(
        self, active_step: str | None, message: str
    ) -> _CreationObservations:
        updates: dict[str, _CreationObservation] = {}
        for name, observation in self.public_items():
            previous = observation.step
            if name == active_step:
                step = (
                    replace(previous, status="error")
                    if previous is not None
                    else WorkflowStep(observation.label, "error", message)
                )
            elif observation.requested and previous is None:
                step = WorkflowStep(
                    observation.label,
                    "skipped" if name == "apply_step" else "error",
                    message,
                )
            else:
                continue
            updates[name] = replace(observation, completed=True, step=step)
        if not any(
            value.step is not None and value.step.status == "error"
            for value in updates.values()
        ):
            updates["settings_step"] = replace(
                self.settings_step,
                completed=True,
                step=WorkflowStep("settings", "error", message),
            )
        return replace(self, **updates)


@dataclass(frozen=True)
class _CreationObservationSnapshot:
    """Immutable evidence and phase; payload identity is retained."""

    observations: _CreationObservations
    active_step: str | None
    attempted_feature: str | None

    @property
    def facts(self) -> Mapping[str, WorkflowStep | None]:
        return self.observations.public_steps()


@dataclass
class _CreationFeatureCoordinator:
    hmc: HMCClient
    system_uuid: str
    system_selector: str
    inputs: _CreationFeatureInputs
    _cli_system: str | None = field(default=None, init=False)
    controller_mode: bool = False
    serial_admission: SerialAdmission | None = field(default=None, init=False)
    memory_admission: CreationMemoryAdmission | None = field(default=None, init=False)
    start_admission: StartAdmission | None = field(default=None, init=False)
    _entry: dict[str, Any] | None = field(default=None, init=False)
    _observations: _CreationObservations = field(init=False)
    _active_step: str | None = field(default=None, init=False)
    _attempted_feature: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._observations = _CreationObservations.selected(self.inputs)

    @property
    def system(self) -> str:
        return (
            self._cli_system if self._cli_system is not None else self.system_selector
        )

    def snapshot(self) -> _CreationObservationSnapshot:
        return _CreationObservationSnapshot(
            self._observations,
            self._active_step,
            self._attempted_feature,
        )

    @property
    def entry(self) -> dict[str, Any] | None:
        return self._entry

    def retain(self, name: str, step: WorkflowStep | None) -> None:
        self._observations = self._observations.record(name, step)

    async def observe(
        self,
        field_name: str,
        observation: Awaitable[WorkflowStep | None] | WorkflowStep | None,
        *,
        active_step: str | None = None,
    ) -> WorkflowStep | None:
        self._active_step = active_step or field_name
        self._attempted_feature = self._active_step
        step = (
            observation
            if observation is None or isinstance(observation, WorkflowStep)
            else await observation
        )
        self.retain(field_name, step)
        self._active_step = None
        return step

    async def discover_mode(
        self, settings: LparSettings | None = None, *, cli_system: str | None = None
    ):
        if cli_system is not None:
            self._cli_system = cli_system
        if settings is not None:
            caps = await read_role_capabilities(
                self.hmc,
                self.system_uuid,
                need_kvm=settings.kvm_capable is not None,
                need_management=settings.powervm_mgmt_capable is not None,
            )
            self.controller_mode = caps.controller_mode
            return caps
        if self.inputs.affinity_requested or self.inputs.layout_requested:
            _, self.controller_mode = await read_creation_authority(
                self.hmc,
                self.system_uuid,
                need_management=False,
                context="Affinity group"
                if self.inputs.affinity_requested
                else "Memory layout",
            )
        elif (
            self.inputs.serial_requested
            or self.inputs.memory_requested
            or self.inputs.start_requested
        ):
            self.controller_mode = (
                await read_role_capabilities(
                    self.hmc, self.system_uuid, need_kvm=False, need_management=False
                )
            ).controller_mode
        return None

    async def admit_host_features(self) -> None:
        if self.inputs.vtpm_requested:
            self._attempted_feature = "vtpm_admission"
            admit_vtpm(
                self.inputs.vtpm_settings,
                await read_vtpm_capabilities(self.hmc, self.system_uuid),
            )
        if self.inputs.policy_requested:
            self._attempted_feature = "security_policy_admission"
            admit_security_policy(
                self.inputs.security_policy,
                await read_security_policy_capabilities(
                    self.hmc,
                    self.system_uuid,
                    **(
                        {
                            "need_power11": self.inputs.environment == "OS400"
                            and self.inputs.security_policy.secure_boot is not None
                        }
                        if not self.inputs.vios
                        else {}
                    ),
                ),
                environment=self.inputs.environment,
            )
        self._attempted_feature = "hardware_memory_admission"
        await admit_hardware_memory(
            self.hmc,
            self.system_uuid,
            self.inputs.hardware_mem_encryption,
            self.inputs.hardware_mem_expansion,
        )

    async def admit_partition_features(self) -> None:
        if self.inputs.vios and self.inputs.layout_requested:
            await admit_memory_layout(
                self.hmc.config, self.system, self.inputs.memory_layout
            )
        if self.inputs.serial_requested:
            self._attempted_feature = "serial_admission"
            self.serial_admission = await admit_serial_creation(
                self.hmc,
                self.system_uuid,
                name=self.inputs.name,
                partition_id=self.inputs.partition_id,
                maximum=self.inputs.max_virtual_slots,
                adapters=self.inputs.virtual_serial_adapters,
                kind=self.inputs.kind,
            )
        if not self.inputs.vios and self.inputs.start_requested:
            self._attempted_feature = "start_admission"
            self.start_admission = await admit_start_creation(
                self.hmc, self.system_uuid, self.system, self.inputs.start_settings
            )
        if not self.inputs.vios:
            resources = complete_create_resources(self.inputs.resources)
            if (
                (
                    self.inputs.policy_requested
                    or self.inputs.hardware_requested
                    or self.inputs.layout_requested
                    or self.inputs.serial_requested
                    or self.inputs.memory_requested
                    or self.inputs.start_requested
                )
                and resources is not None
                and self.inputs.creation_memory.mem_mode != "shared"
            ):
                await require_memory_within_system(
                    self.hmc, self.system_uuid, resources
                )
            if self.inputs.memory_requested:
                self._cli_system = await resolve_system_cli_name(
                    self.hmc.config, self.system_uuid
                )
        if self.inputs.memory_requested:
            self._attempted_feature = "creation_memory_admission"
            self.memory_admission = await admit_creation_memory(
                self.hmc,
                self.system_uuid,
                self.system,
                self.inputs.creation_memory,
                complete_create_resources(self.inputs.resources)
                or self.inputs.resources,
                kind=self.inputs.kind,
                environment=self.inputs.environment,
                name=self.inputs.name,
                **(
                    {"partition_id": self.inputs.partition_id}
                    if not self.inputs.vios
                    else {}
                ),
            )
        if self.inputs.vios and self.inputs.start_requested:
            self._attempted_feature = "start_admission"
            self.start_admission = await admit_start_creation(
                self.hmc, self.system_uuid, self.system, self.inputs.start_settings
            )

    async def confirm_identity(
        self,
        created_lpar: dict[str, Any] | None = None,
        *,
        readback_error: HMCError | None = None,
    ) -> dict[str, Any] | None:
        self._entry = created_lpar
        if isinstance(readback_error, HMCTLSFingerprintError):
            raise readback_error
        if self.inputs.vios:
            self._attempted_feature = "created_identity"
            try:
                self._entry = await self.hmc.find_vios_by_name(
                    self.inputs.name, system_uuid=self.system_uuid
                )
            except HMCTLSFingerprintError:
                raise
            except Exception:
                if not self.inputs.native_features_requested:
                    raise
                # Native creation succeeded; discard unavailable lookup diagnostics.
                self._entry = None
            if (
                self.entry is None
                or (self.inputs.layout_requested and not isinstance(self.entry, dict))
                or not self.entry.get("UUID")
            ):
                raise ValueError("Created VIOS identity was not confirmed")
            if (
                self.inputs.policy_requested or self.inputs.native_features_requested
            ) and (
                not isinstance(self.entry.get("UUID"), str)
                or not is_uuid(self.entry["UUID"])
                or not isinstance(self.entry.get("Resource"), dict)
                or self.entry["Resource"].get("PartitionName") != self.inputs.name
            ):
                raise ValueError("Created VIOS identity unconfirmed")
            if self.start_admission is not None:
                self._attempted_feature = "start_identity"
                self._entry = await confirm_start_created(
                    self.hmc,
                    self.start_admission,
                    self.entry,
                    self.inputs.name,
                    "Virtual IO Server",
                )
        elif created_lpar is not None:
            self._entry = created_lpar
            identity_ok = True
            confirmed_lpar = created_lpar
            confirmations: list[
                tuple[str, Callable[[], Awaitable[dict[str, Any]]]]
            ] = []
            if self.serial_admission is not None:
                confirmations.append(
                    (
                        "serial_identity",
                        partial(
                            confirm_serial_created,
                            self.hmc,
                            self.serial_admission,
                            created_lpar,
                        ),
                    )
                )
            if self.memory_admission is not None:
                confirmations.append(
                    (
                        "memory_identity",
                        partial(
                            confirm_creation_memory_identity,
                            self.hmc,
                            self.memory_admission,
                            created_lpar,
                        ),
                    )
                )
            if self.start_admission is not None:
                confirmations.append(
                    (
                        "start_identity",
                        partial(
                            confirm_start_created,
                            self.hmc,
                            self.start_admission,
                            created_lpar,
                            self.inputs.name,
                            self.inputs.environment,
                        ),
                    )
                )
            for feature, confirm in confirmations:
                self._attempted_feature = feature
                try:
                    confirmed_lpar = await confirm()
                    self._entry = confirmed_lpar
                except HMCTLSFingerprintError:
                    raise
                except Exception:  # noqa: BLE001 — uncertain identity forbids stamping.
                    identity_ok = False
            self._entry = confirmed_lpar if identity_ok else None
        else:
            self._entry = None
        if not self.inputs.vios:
            await self.observe(
                "serial_step",
                serial_creation_step(
                    self.hmc,
                    self.serial_admission,
                    self.entry,
                    controller_mode=self.controller_mode,
                )
                if self.serial_admission is not None and self.entry is None
                else None,
            )

        return self._entry

    async def observe_unavailable_identity(self, *, missing: bool) -> None:
        """Retain the existing distinct absent and unconfirmed observation orders."""
        if missing:
            await self.observe(
                "creation_memory_step",
                creation_memory_step(
                    self.hmc,
                    self.memory_admission,
                    None,
                    controller_mode=self.controller_mode,
                )
                if self.memory_admission is not None
                else None,
            )
        else:
            await self.observe(
                "memory_layout_step",
                _created_memory_layout_step(
                    self.hmc, None, self.inputs, controller_mode=self.controller_mode
                ),
            )
            await self.observe(
                "security_policy_step",
                WorkflowStep(
                    "security_policy",
                    "error",
                    "Created partition identity unconfirmed; inspect before retrying",
                )
                if self.inputs.policy_requested
                else None,
            )
        await self.observe(
            "affinity_group_step",
            created_affinity_group_step(
                self.hmc,
                self.system_uuid,
                None,
                self.inputs.name,
                self.inputs.environment,
                self.inputs.affinity_group_id,
                controller_mode=self.controller_mode,
            ),
        )
        if missing:
            await self.observe(
                "memory_layout_step",
                _created_memory_layout_step(
                    self.hmc, None, self.inputs, controller_mode=self.controller_mode
                ),
            )
        await self.observe(
            "hardware_memory_step",
            created_hardware_memory_step(
                self.hmc,
                self.system_uuid,
                self.entry,
                self.inputs.name,
                self.inputs.environment,
                self.inputs.hardware_mem_encryption,
                self.inputs.hardware_mem_expansion,
            ),
        )
        if missing:
            await self.observe(
                "security_policy_step",
                WorkflowStep(
                    "security_policy",
                    "error",
                    "Created partition identity unavailable; policy unverified",
                )
                if self.inputs.policy_requested
                else None,
            )
            await self.observe(
                "vtpm_step",
                WorkflowStep(
                    "vtpm", "error", "Created identity unavailable; vTPM unverified"
                )
                if self.inputs.vtpm_requested
                else None,
            )
        else:
            await self.observe(
                "creation_memory_step",
                creation_memory_step(
                    self.hmc,
                    self.memory_admission,
                    None,
                    controller_mode=self.controller_mode,
                )
                if self.memory_admission is not None
                else None,
            )

    async def observe_before_followup(self, cli_system: str | None = None) -> None:
        if self.inputs.vios:
            await self._observe_vios()
            return
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
                self.hmc, cli_system, self.inputs, controller_mode=self.controller_mode
            ),
        )
        await self.observe(
            "affinity_group_step",
            created_affinity_group_step(
                self.hmc,
                self.system_uuid,
                self.entry,
                self.inputs.name,
                self.inputs.environment,
                self.inputs.affinity_group_id,
                controller_mode=self.controller_mode,
            ),
        )
        await self.observe(
            "hardware_memory_step",
            created_hardware_memory_step(
                self.hmc,
                self.system_uuid,
                self.entry,
                self.inputs.name,
                self.inputs.environment,
                self.inputs.hardware_mem_encryption,
                self.inputs.hardware_mem_expansion,
            ),
        )

    async def _observe_vios_settings(self) -> None:
        if self.inputs.layout_requested:
            if self.controller_mode:
                raise ValueError(
                    "Controller current ratio mappings unavailable; no named profile was read"
                )
            self._attempted_feature = "memory_layout_step"
            observed_layout = await get_lpar_memory_layout(
                self.hmc.config, self.system, self.inputs.name
            )
            mismatch = any(
                getattr(observed_layout, field) != value
                for field, value in self.inputs.memory_layout.pairs()
            )
            await self.observe(
                "memory_layout_step",
                WorkflowStep(
                    "memory_layout", "error" if mismatch else "ok", observed_layout
                ),
            )
            if mismatch:
                raise ValueError("Created VIOS memory ratios unavailable or mismatched")
        if self.inputs.vtpm_settings is not None and self.inputs.vtpm_requested:
            self._attempted_feature = "vtpm_step"
            observed = await get_lpar_vtpm_state(
                self.hmc.config, self.system, self.inputs.name, vios=True
            )
            mismatch = any(
                getattr(observed.settings, field)
                != getattr(self.inputs.vtpm_settings, field)
                for field, _ in self.inputs.vtpm_settings.pairs()
            )
            await self.observe(
                "vtpm_step",
                WorkflowStep("vtpm", "error" if mismatch else "ok", observed),
            )
            if mismatch:
                raise ValueError(
                    "Created VIOS vTPM readback differs from the requested fields"
                )
        if self.inputs.security_policy is not None and self.inputs.policy_requested:
            assert self.entry is not None
            self._attempted_feature = "security_policy_step"
            policy = await read_lpar_security_policy(
                self.hmc,
                self.entry["UUID"],
                system_name_or_uuid=self.system_uuid,
                vios=True,
            )
            mismatch = any(
                getattr(policy.settings, field)
                != getattr(self.inputs.security_policy, field)
                for field, _ in self.inputs.security_policy.pairs()
            )
            await self.observe(
                "security_policy_step",
                WorkflowStep("security_policy", "error" if mismatch else "ok", policy),
            )
            if mismatch:
                raise ValueError(
                    "Created VIOS policy readback differs from requested fields"
                )

    async def _observe_vios(self) -> None:
        verification_unavailable = "VIOS exists or may exist; creation verification unavailable or mismatched. Inspect before retrying. No second create or rollback performed"
        try:
            await self.confirm_identity()
            if self.memory_admission is not None:
                self._attempted_feature = "memory_identity"
                self._entry = await confirm_creation_memory_identity(
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
                self._attempted_feature = "serial_identity"
                self._entry = await confirm_serial_created(
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
            await self._observe_vios_settings()
        except HMCTLSFingerprintError as error:
            error.add_note(
                "VIOS exists or may exist after creation submission. Inspect before "
                "retrying; no replay or rollback performed."
            )
            raise
        except Exception as error:
            if (
                not self.inputs.serial_requested
                and not self.inputs.memory_requested
                and not self.inputs.start_requested
                and not isinstance(error, (HMCError, ValueError, PermissionError))
            ):
                raise
            if not self.inputs.native_features_requested:
                if self.inputs.policy_requested:
                    raise HMCError(verification_unavailable) from error
                raise HMCError(
                    f"VIOS creation returned, but verification is unavailable or mismatched: {error}. The VIOS exists or may exist; inspect before retrying. No second create or rollback performed"
                ) from error
        else:
            assert (
                self.entry is not None
            )  # Successful identity verification above requires an entry.
            hardware_step = await self.observe(
                "hardware_memory_step",
                created_hardware_memory_step(
                    self.hmc,
                    self.system_uuid,
                    self.entry,
                    self.inputs.name,
                    "Virtual IO Server",
                    self.inputs.hardware_mem_encryption,
                    self.inputs.hardware_mem_expansion,
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
                    self.inputs.name,
                    "Virtual IO Server",
                    self.inputs.affinity_group_id,
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
            return
        if (
            self.inputs.layout_requested
            or self.inputs.serial_requested
            or self.inputs.memory_requested
            or self.inputs.start_requested
        ):
            raise HMCError(verification_unavailable)
        feature = (
            "affinity group"
            if self.inputs.affinity_requested
            else "hardware memory access"
        )
        raise HMCError(
            f"VIOS exists or may exist; {feature} readback-unavailable. "
            "Inspect before retrying. No second create or rollback performed"
        )

    async def observe_start(self, entry: dict[str, Any] | None) -> WorkflowStep | None:
        if self.start_admission is None:
            return None
        step = await self.observe(
            "start_step",
            created_start_step(
                self.hmc,
                self.start_admission,
                entry,
                self.inputs.name,
                self.inputs.environment,
                controller_mode=self.controller_mode,
            ),
            active_step="start_step" if self.inputs.vios else "settings_step",
        )
        assert step is not None
        if self.inputs.vios and step.status != "ok":
            raise HMCError(
                "VIOS exists or may exist; start configuration unverified. Inspect before retrying; no rollback performed"
            )
        return step

    async def observe_after_followup(
        self,
        cli_system: str | None,
        *,
        verify_before_apply: bool,
    ) -> None:
        if verify_before_apply:
            await self.observe_vtpm(cli_system)
        assert self.entry is not None
        await self.observe(
            "security_policy_step",
            _created_security_policy_step(
                self.hmc, self.system_uuid, self.inputs, self.entry
            ),
        )

    async def observe_vtpm(self, cli_system: str | None) -> None:
        await self.observe(
            "vtpm_step", _created_vtpm_step(self.hmc, cli_system, self.inputs)
        )

    async def observe_after_apply(self, apply_step: WorkflowStep | None) -> None:
        if (
            self.memory_admission is not None
            and apply_step is not None
            and apply_step.status == "ok"
        ):
            memory_step = self._observations.creation_memory_step.step
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
    inputs: _CreationFeatureInputs,
    *,
    controller_mode: bool = False,
) -> WorkflowStep | None:
    requested = inputs.memory_layout
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
            layout = await get_lpar_memory_layout(hmc.config, cli_system, inputs.name)
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
    inputs: _CreationFeatureInputs,
    created_lpar: dict[str, Any],
) -> WorkflowStep | None:
    if not inputs.security_policy.pairs():
        return None
    try:
        uuid = created_lpar.get("UUID")
        if (
            not isinstance(uuid, str)
            or not is_uuid(uuid)
            or (created_lpar.get("Resource") or {}).get("PartitionName") != inputs.name
        ):
            raise ValueError("Created partition identity unconfirmed")
        observed = await read_lpar_security_policy(
            hmc, uuid, system_name_or_uuid=system_uuid
        )
        matches = observed.environment == inputs.environment and all(
            getattr(observed.settings, name) == getattr(inputs.security_policy, name)
            for name, _ in inputs.security_policy.pairs()
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
    hmc: HMCClient, cli_system: str | None, inputs: _CreationFeatureInputs
) -> WorkflowStep | None:
    if not inputs.vtpm_settings.pairs():
        return None
    try:
        if cli_system is None:
            raise ValueError("Native vTPM creation did not retain its system identity")
        observed = await get_lpar_vtpm_state(hmc.config, cli_system, inputs.name)
    except HMCTLSFingerprintError:
        raise
    except (HMCError, ValueError, PermissionError) as error:
        return WorkflowStep(
            "vtpm", "error", f"Partition exists; vTPM unverified: {error}"
        )
    matches = all(
        getattr(observed.settings, name) == getattr(inputs.vtpm_settings, name)
        for name, _ in inputs.vtpm_settings.pairs()
    )
    return WorkflowStep("vtpm", "ok" if matches else "error", observed)
