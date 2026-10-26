"""Creation-only huge-page, page-table and KVM reservation values."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from decimal import Decimal

from pydantic import ConfigDict, StrictInt, StrictStr

_HUGE_PAGE_FIELDS = (
    "min_num_huge_pages",
    "desired_num_huge_pages",
    "max_num_huge_pages",
)


def _reservation(value: object) -> Decimal:
    if type(value) not in {Decimal, int, float}:
        raise ValueError("kvm_reserved_mem_percent must be a finite numeric value")
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        raise ValueError("kvm_reserved_mem_percent must be finite")
    digits = number.as_tuple().digits
    exponent = number.as_tuple().exponent
    # normalize() rounds through the ambient Decimal context before dropping zeros.
    if isinstance(exponent, int) and exponent < -2:
        discarded_places = -exponent - 2
        if any(digits[-discarded_places:]):
            raise ValueError(
                "kvm_reserved_mem_percent accepts at most two decimal places"
            )
    return _canonical_reservation(number)


def _canonical_reservation(number: Decimal) -> Decimal:
    """Bound fixed-point output before expansion, without Decimal context rounding."""
    sign, digits, exponent = number.as_tuple()
    assert isinstance(exponent, int)  # The caller has rejected nonfinite values.
    if not any(digits):
        return Decimal((sign, (0,), 0))
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
    exponent += len(digits) - end
    digits = digits[:end]
    if exponent >= 0:
        length = sign + len(digits) + exponent
    else:
        length = sign + max(len(digits), -exponent) + 1
        length += len(digits) + exponent <= 0
    if length > 65536:
        raise ValueError(
            "kvm_reserved_mem_percent canonical encoding exceeds 65536 ASCII bytes"
        )
    return Decimal((sign, digits, exponent))


@dataclass(frozen=True)
class LparMemoryLayout:
    """Six optional creation fields; omission preserves native defaults."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    min_num_huge_pages: StrictInt | None = field(
        default=None,
        metadata={"description": "Minimum huge-page count; AIX/Linux only."},
    )
    desired_num_huge_pages: StrictInt | None = field(
        default=None,
        metadata={"description": "Desired huge-page count; AIX/Linux only."},
    )
    max_num_huge_pages: StrictInt | None = field(
        default=None,
        metadata={"description": "Maximum huge-page count; AIX/Linux only."},
    )
    hpt_ratio: StrictStr | None = field(
        default=None,
        metadata={"description": "HPT ratio token advertised by the selected system."},
    )
    ppt_ratio: StrictStr | None = field(
        default=None,
        metadata={"description": "PPT ratio token advertised by the selected system."},
    )
    kvm_reserved_mem_percent: Decimal | None = field(
        default=None,
        metadata={
            "description": "Finite reservation with at most two fractional decimal places; "
            "canonical encoding is limited to 65536 ASCII bytes. Selection including zero declares "
            "Linux intent. HMC decides undocumented numeric limits."
        },
    )

    def __post_init__(self) -> None:
        supplied = []
        for name in _HUGE_PAGE_FIELDS:
            value = getattr(self, name)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError(f"{name} must be a nonnegative integer")
                supplied.append(value)
        if supplied != sorted(supplied):
            raise ValueError(
                "Huge-page counts must satisfy minimum <= desired <= maximum"
            )
        for name in ("hpt_ratio", "ppt_ratio"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not str
                or re.fullmatch(r"[A-Za-z0-9_.:/+-]+", value) is None
                or value in {"null", "none"}
            ):
                raise ValueError(f"{name} must be a nonempty advertised scalar token")
        if self.kvm_reserved_mem_percent is not None:
            object.__setattr__(
                self,
                "kvm_reserved_mem_percent",
                _reservation(self.kvm_reserved_mem_percent),
            )

    def pairs(self) -> list[tuple[str, object]]:
        """Return canonical native pairs without losing zero or decimal precision."""
        result = []
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None:
                if isinstance(value, Decimal):
                    text = format(value, "f")
                    value = text.rstrip("0").rstrip(".") if "." in text else text
                result.append((item.name, value))
        return result

    def validate_creation(self, partition_type: str) -> None:
        """Keep source-specific restrictions separate from unrestricted ratios."""
        if partition_type != "AIX/Linux" and any(
            getattr(self, name) is not None for name in _HUGE_PAGE_FIELDS
        ):
            raise ValueError("Huge-page creation fields require AIX/Linux")
        if self.kvm_reserved_mem_percent is not None and partition_type != "AIX/Linux":
            raise ValueError(
                "kvm_reserved_mem_percent requires Linux intent within AIX/Linux"
            )
