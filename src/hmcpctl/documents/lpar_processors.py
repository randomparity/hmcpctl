"""Typed native non-pool processor quantities (ADR 0296)."""

from __future__ import annotations

import sys
from decimal import Context, Decimal, Inexact, InvalidOperation
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

# Two maximal shared snapshots (30 numeric values each) plus two expected quantities.
PROCESSOR_NUMERIC_TOKEN_BYTES = 65536 // 62


def bounded_processor_number(value: object, *, whole: bool = False) -> Decimal | int:
    """Bound encoded numeric values before conversion, normalization or rendering."""
    budget = PROCESSOR_NUMERIC_TOKEN_BYTES
    integer_limit = sys.get_int_max_str_digits()
    digits = min(budget, integer_limit or budget) if whole else budget - 5
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError("Processor quantity must be numeric")  # noqa: TRY004 — Pydantic validation error
    if isinstance(value, str) and (
        len(value) > budget or len(value.encode("utf-8")) > budget
    ):
        raise ValueError("Processor numeric token exceeds the encoded byte budget")
    if isinstance(value, int) and (value < 0 or value >= 10**digits):
        raise ValueError("Processor integer exceeds the numeric representation budget")
    try:
        number = (
            Decimal(str(value)) if isinstance(value, (str, float)) else Decimal(value)
        )
        if not number.is_finite() or number < 0:
            raise ValueError("Processor quantity must be finite and nonnegative")
        if number.is_zero():
            return 0 if whole else Decimal(0)
        if not (0 if whole else -2) <= number.adjusted() < digits:
            raise ValueError(
                "Processor quantity exceeds representation or precision bounds"
            )
        number = number.quantize(
            Decimal(1) if whole else Decimal("0.01"),
            context=Context(prec=budget, traps=[Inexact, InvalidOperation]),
        )
    except (InvalidOperation, Inexact) as error:
        raise ValueError(
            "Processor quantity must have exact integral or hundredth precision"
        ) from error
    return int(number) if whole else number


class LparProcessorChange(BaseModel):
    """Change current whole CPUs/vCPUs and shared entitlement, without rounding."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    operation: Literal["add", "remove", "set"] = Field(
        description="Add/remove a current quantity, or set its absolute value."
    )
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
        return cast("Decimal", bounded_processor_number(value))

    @field_validator("processors")
    @classmethod
    def bounded_processors(cls, value: int | None) -> int | None:
        if value is not None:
            bounded_processor_number(value, whole=True)
        return value

    @model_validator(mode="after")
    def require_quantity(self) -> Self:
        if self.processors is None and self.processing_units is None:
            raise ValueError(
                "Pass processors or processing_units; nothing was requested"
            )
        return self


class LparProcessorMove(BaseModel):
    """Native two-partition transfer; mode applicability is resolved at admission."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    processors: Annotated[StrictInt, Field(ge=0)] | None = Field(
        default=None,
        description="Whole dedicated processors or shared virtual processors to move, when applicable.",
    )
    processing_units: Decimal | None = Field(
        default=None,
        description="Shared entitlement to move in exact hundredths; integral for a dedicated destination.",
    )
    force: StrictBool = Field(
        default=False,
        description="Allow running AIX/Linux/VIOS changes without RMC; runtime effect requires restart.",
    )

    @field_validator("processing_units", mode="before")
    @classmethod
    def exact_units(cls, value: object) -> Decimal | None:
        return (
            None if value is None else cast("Decimal", bounded_processor_number(value))
        )

    @field_validator("processors")
    @classmethod
    def bounded_processors(cls, value: int | None) -> int | None:
        if value is not None:
            bounded_processor_number(value, whole=True)
        return value

    @model_validator(mode="after")
    def require_quantity(self) -> Self:
        if self.processors is None and self.processing_units is None:
            raise ValueError(
                "Pass processors or processing_units; nothing was requested"
            )
        return self
