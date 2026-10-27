"""Strict optional native affinity group selection for partition creation."""

import re
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    StrictInt,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
)


def _private_group(
    value: object, handler: ValidatorFunctionWrapHandler
) -> int | Literal["none"]:
    try:
        return handler(value)
    except ValidationError:
        details: list[Any] = [
            {
                "type": "value_error",
                "loc": (),
                "input": None,
                "ctx": {
                    "error": ValueError(
                        "affinity_group_id must be none or an integer from 1 to 255"
                    )
                },
            }
        ]
    raise ValidationError.from_exception_data("AffinityGroupID", details)


AffinityGroupID = Annotated[
    Annotated[
        StrictInt,
        Field(ge=1, le=255, description="Affinity group integer from 1 to 255"),
    ]
    | Literal["none"],
    WrapValidator(_private_group),
]


def validate_affinity_group_id(value: object) -> int | Literal["none"] | None:
    """Reject coercions and record delimiters before any I/O."""
    if value is None or (type(value) is str and value == "none"):
        return value
    if type(value) is int and 1 <= value <= 255:
        return value
    raise ValueError("affinity_group_id must be none or an integer from 1 to 255")


def parse_affinity_group_cli(value: str | None) -> int | Literal["none"] | None:
    """Parse the CLI's explicit textual representation without whitespace coercion."""
    if value is not None and re.fullmatch(r"[0-9]{1,3}", value):
        return validate_affinity_group_id(int(value))
    return validate_affinity_group_id(value)
