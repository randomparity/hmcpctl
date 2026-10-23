"""Strict native partition security policy; omitted fields preserve HMC policy."""

from dataclasses import dataclass as standard_dataclass
from dataclasses import field, fields
from typing import Any, Literal

from pydantic import (
    ConfigDict,
    ModelWrapValidatorHandler,
    StrictBool,
    StrictInt,
    ValidationError,
    model_validator,
)
from pydantic.dataclasses import dataclass


@dataclass(
    frozen=True,
    slots=True,
    config=ConfigDict(extra="forbid", hide_input_in_errors=True),
)
class LparSecurityPolicy:
    """Five finite settings; configuration does not prove guest or key readiness."""

    secure_boot: StrictInt | None = field(
        default=None,
        metadata={
            "description": "Pending secure boot: 0 off, 1 log, 2 enforce; 3–9 require OS-specific operator knowledge. Does not prove runtime enforcement."
        },
    )
    keystore_kbytes: StrictInt | None = field(
        default=None,
        metadata={
            "description": "Keystore size in KB within selected-system bounds. Zero disables only an empty store; HMC enforces emptiness. Enable/increase requires inactive state; nonzero shrink requires Power11."
        },
    )
    keystore_signed_updates: StrictBool | None = field(
        default=None,
        metadata={
            "description": "Irreversibly enable signed keystore updates; requires nonzero store and inactive partition. Omission preserves policy."
        },
    )
    linux_dynamic_key_secure_boot: StrictBool | None = field(
        default=None,
        metadata={
            "description": "Irreversible Linux dynamic-key enablement requires signed infrastructure, nonzero store and inactive state. Selecting this field declares Linux intent on AIX/Linux, without proving the guest OS."
        },
    )
    keystore_signed_updates_without_verification: StrictBool | None = field(
        default=None,
        metadata={
            "description": "Update-only PK provisioning bypasses authentication against the old PK; enabling requires signed infrastructure and nonzero store."
        },
    )

    @model_validator(mode="wrap")
    @classmethod
    def _private_validation(
        cls, value: Any, handler: ModelWrapValidatorHandler["LparSecurityPolicy"]
    ) -> "LparSecurityPolicy":
        """Outer MCP adapters must receive bounded, input-free error details."""
        try:
            return handler(value)
        except ValidationError as error:
            known = {item.name for item in fields(cls)}
            sanitized: list[Any] = []
            unknown = False
            for detail in error.errors(include_url=False):
                location = detail["loc"]
                if location and location[0] not in known:
                    unknown = True
                    continue
                code = detail["type"]
                clean: Any = {"type": code, "loc": location, "input": None}
                if code == "value_error":
                    message = str(detail.get("ctx", {}).get("error", ""))
                    clean["loc"] = tuple(
                        name for name in known if message.startswith(name + " ")
                    )
                    clean["ctx"] = {
                        "error": ValueError("Invalid security policy range")
                    }
                elif code == "dataclass_type":
                    clean["ctx"] = {"class_name": "LparSecurityPolicy"}
                sanitized.append(clean)
            if unknown:
                sanitized.append(
                    {
                        "type": "extra_forbidden",
                        "loc": ("unknown_field",),
                        "input": None,
                    }
                )
            raise ValidationError.from_exception_data(cls.__name__, sanitized) from None

    def __post_init__(self) -> None:
        for name, maximum in (("secure_boot", 9), ("keystore_kbytes", None)):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not int
                or value < 0
                or (maximum is not None and value > maximum)
            ):
                raise ValueError(
                    f"{name} requires a nonnegative integer"
                    + (" from 0 to 9" if maximum else "")
                )
        for item in fields(self)[2:]:
            if (value := getattr(self, item.name)) is not None and type(
                value
            ) is not bool:
                raise ValueError(f"{item.name} requires a boolean")

    def pairs(self) -> list[tuple[str, object]]:
        """Canonical native order; explicit False and zero are retained."""
        return [
            (item.name, int(value) if type(value) is bool else value)
            for item in fields(self)
            if (value := getattr(self, item.name)) is not None
        ]

    def validate_creation(self, partition_type: str) -> None:
        """Reject update-only bypass, including explicit False, before creation."""
        if self.keystore_signed_updates_without_verification is not None:
            raise ValueError(
                "keystore_signed_updates_without_verification is update-only"
            )
        if self.pairs() and partition_type not in {
            "AIX/Linux",
            "Virtual IO Server",
            "OS400",
        }:
            raise ValueError(
                "Security policy requires a supported partition environment"
            )
        if (
            self.linux_dynamic_key_secure_boot is not None
            and partition_type != "AIX/Linux"
        ):
            raise ValueError(
                "linux_dynamic_key_secure_boot requires AIX/Linux and declares Linux intent only"
            )


@standard_dataclass(frozen=True, slots=True)
class SecurityPolicyState:
    """Observed pending policy, independent current secure boot and target state."""

    environment: str
    state: str
    settings: LparSecurityPolicy
    current_secure_boot: int | None


@standard_dataclass(frozen=True, slots=True)
class SecurityPolicyResult:
    """Configuration evidence from one submission; uncertainty requires inspection."""

    status: Literal[
        "verified", "mismatch", "readback-unavailable", "write-outcome-unknown"
    ]
    requested: LparSecurityPolicy
    observed: SecurityPolicyState | None
    changed_fields: tuple[str, ...]
    warnings: tuple[str, ...]
