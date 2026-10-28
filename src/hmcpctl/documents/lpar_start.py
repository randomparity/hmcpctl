"""Creation-only partition boot/start settings and source applicability."""

from dataclasses import dataclass

from pydantic import ConfigDict, StrictBool, StrictInt, StrictStr

from ..jobs.requests import BOOT_MODES, BootMode


@dataclass(frozen=True)
class LparStartSettings:
    """Optional values preserve native defaults; selectors are resolved before create."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    auto_start: StrictBool | None = None
    boot_mode: BootMode | None = None
    power_ctrl_lpar_ids: tuple[StrictInt, ...] | None = None
    power_ctrl_lpar_names: tuple[StrictStr, ...] | None = None
    conn_monitoring: StrictBool | None = None

    def __post_init__(self) -> None:
        for name in ("auto_start", "conn_monitoring"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name} must be a Boolean or omitted")
        if self.boot_mode is not None and (
            type(self.boot_mode) is not str or self.boot_mode not in BOOT_MODES
        ):
            raise ValueError("boot_mode must be norm, dd, ds, of or sms")
        if (
            self.power_ctrl_lpar_ids is not None
            and self.power_ctrl_lpar_names is not None
        ):
            raise ValueError("Select power controllers by IDs or names, not both")
        for name in ("power_ctrl_lpar_ids", "power_ctrl_lpar_names"):
            values = getattr(self, name)
            if values is None:
                continue
            if type(values) is not tuple or not values:
                raise ValueError(f"{name} must be a nonempty tuple")
            if name == "power_ctrl_lpar_ids":
                valid = all(type(v) is int and v > 0 for v in values)
            else:
                valid = all(
                    type(v) is str and bool(v.strip()) and v.isprintable()
                    for v in values
                )
            if not valid or len(set(values)) != len(values):
                raise ValueError(f"{name} must contain distinct valid selectors")

    def validate_creation(self, environment: str) -> None:
        if environment not in {"AIX/Linux", "OS400", "Virtual IO Server"}:
            raise ValueError("Unsupported partition type")
        if self.boot_mode is not None and environment == "OS400":
            raise ValueError("boot_mode creation is unavailable for OS400")

    def requested(self) -> bool:
        return (
            bool(self.pairs())
            or self.power_ctrl_lpar_ids is not None
            or self.power_ctrl_lpar_names is not None
        )

    def pairs(self) -> list[tuple[str, object]]:
        """Only scalars here; the resolved controller list follows every I/O suffix."""
        result: list[tuple[str, object]] = []
        if self.auto_start is not None:
            result.append(("auto_start", int(self.auto_start)))
        if self.boot_mode is not None:
            result.append(("boot_mode", self.boot_mode))
        if self.conn_monitoring is not None:
            result.append(("conn_monitoring", int(self.conn_monitoring)))
        return result
