"""Typed partition vTPM fields; omission never enables or resets a device."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from typing import Literal

from pydantic import ConfigDict, StrictBool, StrictStr


@dataclass(frozen=True)
class LparVtpmSettings:
    """Three optional HMC fields; versions/levels require runtime advertisement."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    vtpm_enabled: StrictBool | None = field(
        default=None,
        metadata={
            "description": "Enable vTPM; omitted preserves defaults/current configuration. Disabling an enabled instance can destroy TPM data and requires separate acknowledgement."
        },
    )
    vtpm_version: StrictStr | None = field(
        default=None,
        metadata={
            "description": "Version advertised by the selected system; omitted preserves defaults/current value. Version replacement while enabled is refused."
        },
    )
    vtpm_encryption: StrictStr | None = field(
        default=None,
        metadata={
            "description": "Selected system's advertised vTPM 2.0 encryption level; incompatible with 1.2. Omitted preserves defaults/current value."
        },
    )

    def __post_init__(self) -> None:
        if self.vtpm_enabled is not None and type(self.vtpm_enabled) is not bool:
            raise ValueError("vtpm_enabled must be a boolean")
        for name in ("vtpm_version", "vtpm_encryption"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not str
                or value in {"null", "none"}
                or re.fullmatch(r"[A-Za-z0-9_.+-]+", value) is None
            ):
                raise ValueError(
                    f"{name} must be a nonempty advertised scalar token; omit unavailable values"
                )

    def pairs(self) -> list[tuple[str, object]]:
        """Canonical fixed field order; False is the explicit native value zero."""
        return [
            (item.name, int(value) if type(value) is bool else value)
            for item in fields(self)
            if (value := getattr(self, item.name)) is not None
        ]


@dataclass(frozen=True)
class VtpmState:
    """Strict CLI response including the target's environment and current state."""

    environment: Literal["aixlinux", "vioserver"]
    state: str
    settings: LparVtpmSettings
