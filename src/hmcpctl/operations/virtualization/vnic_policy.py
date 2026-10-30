"""Canonical vNIC creation policy and independent inventory observations."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any, Literal


@dataclass(frozen=True)
class VnicPolicy:
    pvid_priority: int | None = None
    allowed_vlan_ids: tuple[int, ...] | Literal["all", "none"] | None = None
    allowed_os_mac_addrs: tuple[str, ...] | Literal["all", "none"] | None = None
    auto_priority_failover: bool | None = None
    mac_addr: str | None = None


@dataclass(frozen=True)
class VnicPolicyVerification:
    status: Literal["not_requested", "verified", "mismatched", "unavailable"] = (
        "not_requested"
    )
    checked_fields: tuple[str, ...] = ()
    mismatched_fields: tuple[str, ...] = ()
    unavailable_fields: tuple[str, ...] = ()


def _mac(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{12}", value):
        raise ValueError("MAC address must contain exactly 12 hexadecimal characters")
    return value.lower()


def _canonical(name: str, value: object) -> Any:
    if value is None:
        return None
    if name == "pvid_priority":
        if type(value) is not int or not 0 <= value <= 7:
            raise ValueError("pvid_priority must be an integer between 0 and 7")
        return value
    if name == "auto_priority_failover":
        if type(value) is not bool:
            raise ValueError("auto_priority_failover must be a boolean")
        return value
    if name == "mac_addr":
        return _mac(value)
    if isinstance(value, str) and value in ("all", "none"):
        return value
    if not isinstance(value, list) or not value:
        raise ValueError(
            f"{name} requires a nonempty list or all/none; use none for no members"
        )
    if name == "allowed_vlan_ids":
        if any(type(item) is not int or not 1 <= item <= 4094 for item in value):
            raise ValueError(
                "allowed_vlan_ids members must be integers between 1 and 4094"
            )
        members = value
    else:
        members = [_mac(item) for item in value]
    if len(set(members)) != len(members):
        raise ValueError(f"{name} must not contain duplicate members")
    return tuple(sorted(members))


def creation_policy(**values: Any) -> VnicPolicy:
    """Validate the five supplied fields without inventing omitted defaults."""
    return VnicPolicy(
        **{name: _canonical(name, value) for name, value in values.items()}
    )


def policy_attributes(policy: VnicPolicy) -> dict[str, str]:
    result = {}
    for item in fields(policy):
        value = getattr(policy, item.name)
        if value is None:
            continue
        if isinstance(value, tuple):
            encoded = ",".join(map(str, value))
        elif isinstance(value, bool):
            encoded = str(int(value))
        else:
            encoded = str(value)
        result[item.name] = encoded
    return result


def observed_policy(row: dict[str, str]) -> VnicPolicy:
    values = {}
    for item in fields(VnicPolicy):
        name = item.name
        raw = row.get(name)
        try:
            if not isinstance(raw, str) or not raw.strip():
                value = None
            elif name == "pvid_priority":
                value = int(raw) if re.fullmatch(r"[0-7]", raw) else None
            elif name == "auto_priority_failover":
                value = raw == "1" if raw in ("0", "1") else None
            elif name == "mac_addr":
                value = raw
            elif raw.lower() in ("all", "none"):
                value = raw.lower()
            elif name == "allowed_vlan_ids":
                parts = raw.split(",")
                value = (
                    [int(part) for part in parts]
                    if all(re.fullmatch(r"[0-9]+", part) for part in parts)
                    else None
                )
            else:
                value = raw.split(",")
            values[name] = _canonical(name, value)
        except (ValueError, TypeError):
            values[name] = None
    return VnicPolicy(**values)


def verify_policy(
    selected: VnicPolicy, observed: VnicPolicy | None
) -> VnicPolicyVerification:
    checked, mismatched, unavailable = [], [], []
    for item in fields(selected):
        expected = getattr(selected, item.name)
        if expected is None:
            continue
        checked.append(item.name)
        actual = getattr(observed, item.name) if observed is not None else None
        if actual is None:
            unavailable.append(item.name)
        elif actual != expected:
            mismatched.append(item.name)
    status: Literal["not_requested", "verified", "mismatched", "unavailable"]
    if not checked:
        status = "not_requested"
    elif mismatched:
        status = "mismatched"
    elif unavailable:
        status = "unavailable"
    else:
        status = "verified"
    return VnicPolicyVerification(
        status, tuple(checked), tuple(mismatched), tuple(unavailable)
    )
