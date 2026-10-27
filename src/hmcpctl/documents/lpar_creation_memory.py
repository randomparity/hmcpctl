"""Finite creation-only shared-memory and saved AME values."""

from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, StrictInt, StrictStr

from ..ssh.commands import build_attribute_record
from ..ssh.transport import HMCCLIError

_INTEGER_LIMIT = 10**4096
_SHARED_FIELDS = (
    "desired_io_entitled_mem",
    "mem_weight",
    "primary_paging_vios_name",
    "primary_paging_vios_id",
    "secondary_paging_vios_name",
    "secondary_paging_vios_id",
)


def _expansion(value: object) -> Decimal:
    if type(value) not in {int, float, Decimal}:
        raise ValueError("mem_expansion must be a finite numeric value")
    if type(value) is int and not 0 <= value <= 10:
        raise ValueError("mem_expansion must be zero or between 1 and 10 inclusive")
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite() or (number != 0 and not 1 <= number <= 10):
        raise ValueError("mem_expansion must be zero or between 1 and 10 inclusive")
    if number == 0:
        return Decimal(0)
    sign, digits, exponent = number.as_tuple()
    assert isinstance(exponent, int)  # Nonfinite values have already been rejected.
    # Decimal.normalize() would round through the caller's ambient context.
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
    exponent += len(digits) - end
    digits = digits[:end]
    length = (
        len(digits) + exponent if exponent >= 0 else max(len(digits), -exponent) + 1
    )
    if length > 65536:
        raise ValueError("mem_expansion canonical encoding exceeds 65536 ASCII bytes")
    return Decimal((sign, digits, exponent))


ExpansionFactor = Annotated[
    Decimal, BeforeValidator(_expansion, json_schema_input_type=int | float)
]


@dataclass(frozen=True)
class LparCreationMemory:
    """Eight optional native fields; omission preserves HMC creation defaults."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    mem_mode: Literal["ded", "shared"] | None = None
    desired_io_entitled_mem: StrictInt | Literal["auto"] | None = None
    mem_weight: StrictInt | None = None
    primary_paging_vios_name: StrictStr | None = None
    primary_paging_vios_id: StrictInt | None = None
    secondary_paging_vios_name: StrictStr | None = None
    secondary_paging_vios_id: StrictInt | Literal["none"] | None = None
    mem_expansion: ExpansionFactor | None = None

    def __post_init__(self) -> None:
        if self.mem_mode is not None and (
            type(self.mem_mode) is not str or self.mem_mode not in {"ded", "shared"}
        ):
            raise ValueError("mem_mode must be ded or shared")
        if self.mem_mode != "shared" and any(
            getattr(self, name) is not None for name in _SHARED_FIELDS
        ):
            raise ValueError(
                "Entitlement, weight and paging selectors require explicit shared mem_mode"
            )
        for name in (
            "desired_io_entitled_mem",
            "mem_weight",
            "primary_paging_vios_id",
            "secondary_paging_vios_id",
        ):
            value = getattr(self, name)
            if value is None or (
                type(value) is str
                and (
                    (name == "desired_io_entitled_mem" and value == "auto")
                    or (name == "secondary_paging_vios_id" and value == "none")
                )
            ):
                continue
            minimum = 0 if name == "mem_weight" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
            if value >= _INTEGER_LIMIT:
                raise ValueError(
                    f"{name} exceeds the 4096 decimal digit encoding limit"
                )
            if name == "mem_weight" and value > 255:
                raise ValueError("mem_weight must be between 0 and 255 inclusive")
        for role in ("primary", "secondary"):
            name = getattr(self, f"{role}_paging_vios_name")
            if name is not None:
                if type(name) is not str or not name:
                    raise ValueError(
                        f"{role}_paging_vios_name must be a nonempty string"
                    )
                if len(name) > 65536 or len(name.encode("utf-8")) > 65536:
                    raise ValueError(
                        f"{role}_paging_vios_name exceeds 65536 UTF-8 bytes"
                    )
                if getattr(self, f"{role}_paging_vios_id") is not None:
                    raise ValueError(f"{role} paging VIOS name and ID conflict")
        if self.mem_expansion is not None:
            object.__setattr__(self, "mem_expansion", _expansion(self.mem_expansion))
        pairs = self.pairs()
        try:
            record = build_attribute_record(pairs) if pairs else ""
        except HMCCLIError:
            raise ValueError(
                "Creation memory fields must use native scalar grammar"
            ) from None
        if len(record.encode("utf-8")) > 65536:
            raise ValueError("Creation memory pair record exceeds 65536 UTF-8 bytes")

    def pairs(self) -> list[tuple[str, object]]:
        """Return fixed native pairs, retaining zero and canonical decimal precision."""
        return [
            (item.name, format(value, "f") if isinstance(value, Decimal) else value)
            for item in fields(self)
            if (value := getattr(self, item.name)) is not None
        ]
