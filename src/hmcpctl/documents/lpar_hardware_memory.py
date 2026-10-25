"""Strict optional hardware memory access flags for partition creation."""

from typing import Annotated, Any

from pydantic import (
    StrictBool,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
)


def _private_flag(value: object, handler: ValidatorFunctionWrapHandler) -> bool:
    try:
        return handler(value)
    except ValidationError:
        details: list[Any] = [{"type": "bool_type", "loc": (), "input": None}]
    raise ValidationError.from_exception_data("HardwareMemoryFlag", details)


HardwareMemoryFlag = Annotated[StrictBool, WrapValidator(_private_flag)]


def hardware_memory_pairs(
    hardware_mem_encryption: bool | None,
    hardware_mem_expansion: bool | None,
) -> tuple[tuple[str, int], ...]:
    """Encode only the two fixed native keys, preserving explicit false."""
    pairs = []
    for name, value in (
        ("hardware_mem_encryption", hardware_mem_encryption),
        ("hardware_mem_expansion", hardware_mem_expansion),
    ):
        if value is None:
            continue
        if type(value) is not bool:
            raise TypeError(f"{name} must be a boolean or None")
        pairs.append((name, int(value)))
    return tuple(pairs)
