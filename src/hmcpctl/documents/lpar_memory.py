"""Typed native non-pool memory quantity requests (ADR 0285)."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)


class LparMemoryChange(BaseModel):
    """One native current-memory quantity operation, with MiB amounts.

    Entitlement is I/O entitled memory for a shared-memory partition. Numeric
    entitlement selects manual management; ``auto`` requests automatic management.
    Removing memory below a shutdown partition's minimum resets memory and CPU
    assignments to zero. Target-specific admission runs before submission.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    operation: Literal["add", "remove", "set"]
    quantity_mib: Annotated[StrictInt, Field(ge=0)] | None = None
    entitled_mib: Annotated[StrictInt, Field(gt=0)] | Literal["auto"] | None = None
    force: StrictBool = False

    @model_validator(mode="after")
    def require_quantity(self) -> Self:
        if self.quantity_mib is None and self.entitled_mib is None:
            raise ValueError("Pass quantity_mib or entitled_mib; nothing was requested")
        return self
