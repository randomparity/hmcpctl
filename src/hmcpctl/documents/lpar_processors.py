"""Typed native non-pool processor quantities (ADR 0289)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)


class LparProcessorChange(BaseModel):
    """Change current whole CPUs/vCPUs and shared entitlement, without rounding."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    operation: Literal["add", "remove", "set"]
    processors: Annotated[StrictInt, Field(ge=0)] | None = Field(
        default=None,
        description="Whole dedicated processors or shared virtual processors.",
    )
    processing_units: Decimal | None = Field(
        default=None,
        description="Shared processor entitlement, exactly representable in hundredths.",
    )
    force: StrictBool = Field(
        default=False,
        description="Allow running AIX/Linux/VIOS changes without RMC; runtime effect requires restart.",
    )

    @field_validator("processing_units", mode="before")
    @classmethod
    def exact_units(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
            raise ValueError("processing_units must be a decimal quantity")  # noqa: TRY004 — Pydantic validation error
        try:
            units = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError("processing_units must be a decimal quantity") from error
        if not units.is_finite() or units < 0:
            raise ValueError("processing_units must be finite and nonnegative")
        digits = units.as_tuple()
        fractional = -int(digits.exponent) - 2
        if fractional > 0 and any(digits.digits[-fractional:]):
            raise ValueError(
                "processing_units must be exactly representable in hundredths"
            )
        return units

    @model_validator(mode="after")
    def require_quantity(self) -> Self:
        if self.processors is None and self.processing_units is None:
            raise ValueError(
                "Pass processors or processing_units; nothing was requested"
            )
        return self
