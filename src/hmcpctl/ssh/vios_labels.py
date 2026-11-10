"""Bounded VIOS Fibre Channel label commands over the HMC SSH CLI."""

from __future__ import annotations

import csv
import shlex
from collections.abc import Sequence
from io import StringIO
from typing import Literal

from ..config import HMCConfig
from ..documents.native_records import build_attribute_record, build_filter
from .commands import HMC_NO_RESULTS
from .transport import HMCCLIError, run_hmc_command

ViosGroupUpdateAction = Literal["rename", "add-members", "remove-members"]
_MAX_GROUP_MEMBERS = 1024
_MAX_GROUP_MEMBER_BYTES = 16 * 1024
# HSCLC3A4 on V10R3 is the only evidence for this cap; the command reference
# states no limit. Whether the HMC counts characters or bytes for non-ASCII
# names is unevidenced, so this counts characters.
_MAX_GROUP_LABEL_CHARACTERS = 16


def _nonblank(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"VIOS label operation requires a nonblank {field}")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(
            f"VIOS label operation {field} {value!r} contains a control character"
        )
    return value


def _group_label_name(value: str, field: str) -> str:
    name = _nonblank(value, field)
    if len(name) > _MAX_GROUP_LABEL_CHARACTERS:
        raise ValueError(
            f"VIOS vFC group {field} {name!r} has {len(name)} characters; "
            f"the HMC accepts at most {_MAX_GROUP_LABEL_CHARACTERS}, so choose a "
            "shorter name"
        )
    return name


def _single_vios_selector(
    vios_name: str | None,
    vios_id: int | None,
    *,
    required: bool,
) -> tuple[str, str | int] | None:
    if (vios_name is None) == (vios_id is None):
        expectation = "exactly one" if required else "at most one"
        raise ValueError(
            f"VIOS label operation accepts {expectation} of vios_name or vios_id"
        )
    if vios_name is not None:
        return "vios_names", _nonblank(vios_name, "vios_name")
    if vios_id is None:
        raise ValueError(
            "VIOS label operation requires vios_id when vios_name is absent"
        )
    if vios_id <= 0:
        raise ValueError("VIOS label operation vios_id must be positive")
    return "vios_ids", vios_id


def _optional_vios_selector(
    vios_name: str | None, vios_id: int | None
) -> tuple[str, str | int] | None:
    if vios_name is None and vios_id is None:
        return None
    return _single_vios_selector(vios_name, vios_id, required=False)


def _member_selector(
    vios_names: Sequence[str] | None,
    vios_ids: Sequence[int] | None,
) -> tuple[str, list[str] | list[int]]:
    if (vios_names is None) == (vios_ids is None):
        raise ValueError(
            "VIOS group label operation requires exactly one of vios_names or vios_ids"
        )
    if vios_names is not None:
        if len(vios_names) > _MAX_GROUP_MEMBERS:
            raise ValueError(
                f"VIOS group label vios_names accepts at most {_MAX_GROUP_MEMBERS} members"
            )
        members = list(vios_names)
        if not members:
            raise ValueError("VIOS group label vios_names must not be empty")
        validated = [_nonblank(name, "vios_names member") for name in members]
        if len(set(validated)) != len(validated):
            raise ValueError("VIOS group label vios_names must not contain duplicates")
        for name in validated:
            build_attribute_record([("vios_names", name)])
        _require_bounded_member_bytes("vios_names", validated)
        return "vios_names", validated
    if vios_ids is None:
        raise ValueError(
            "VIOS group label operation requires vios_ids when names are absent"
        )
    if len(vios_ids) > _MAX_GROUP_MEMBERS:
        raise ValueError(
            f"VIOS group label vios_ids accepts at most {_MAX_GROUP_MEMBERS} members"
        )
    identifiers = list(vios_ids)
    if not identifiers:
        raise ValueError("VIOS group label vios_ids must not be empty")
    if any(identifier <= 0 for identifier in identifiers):
        raise ValueError("VIOS group label vios_ids must all be positive")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("VIOS group label vios_ids must not contain duplicates")
    _require_bounded_member_bytes("vios_ids", identifiers)
    return "vios_ids", identifiers


def _require_bounded_member_bytes(attribute: str, members: Sequence[str | int]) -> None:
    remaining = _MAX_GROUP_MEMBER_BYTES
    for index, member in enumerate(members):
        if index:
            remaining -= 1
        try:
            text = member if isinstance(member, str) else str(member)
        except ValueError as error:
            raise ValueError(
                f"VIOS group label {attribute} member cannot be encoded as text: {error}"
            ) from error
        if remaining < 0 or len(text) > remaining:
            _raise_member_payload_too_large(attribute)
        try:
            remaining -= len(text.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError(
                f"VIOS group label {attribute} members must be valid UTF-8 text"
            ) from error
        if remaining < 0:
            _raise_member_payload_too_large(attribute)


def _raise_member_payload_too_large(attribute: str) -> None:
    raise ValueError(
        f"VIOS group label {attribute} accepts at most "
        f"{_MAX_GROUP_MEMBER_BYTES} bytes including separators"
    )


def _parse_label_rows(output: str, operation: str) -> list[dict[str, str]]:
    # Not parse_hmc_result_rows: these reads take whatever attributes the HMC
    # prints, so there is no requested field tuple for the header to match.
    if not output.strip() or output.strip() == HMC_NO_RESULTS:
        return []
    try:
        records = list(csv.reader(StringIO(output, newline=""), strict=True))
    except csv.Error as error:
        raise HMCCLIError(f"{operation} returned malformed CSV: {error}") from error
    records = [record for record in records if record]
    if not records:
        return []
    header = records[0]
    if any(not name.strip() for name in header):
        raise HMCCLIError(f"{operation} returned a blank header name")
    if len(set(header)) != len(header):
        raise HMCCLIError(f"{operation} returned duplicate header names")
    rows: list[dict[str, str]] = []
    for number, values in enumerate(records[1:], start=2):
        if len(values) != len(header):
            raise HMCCLIError(
                f"{operation} row {number} has {len(values)} columns; "
                f"expected {len(header)}"
            )
        rows.append(dict(zip(header, values, strict=True)))
    return rows


def _receipt(operation: str, system_name: str, **values: object) -> dict[str, object]:
    return {"operation": operation, "system_name": system_name, **values}


async def _run_mutation(
    config: HMCConfig,
    command: str,
    operation: str,
    system_name: str,
    **values: object,
) -> dict[str, object]:
    output = await run_hmc_command(config, command)
    return _receipt(operation, system_name, **values, output=output.strip())


async def list_vios_fc_port_labels(
    config: HMCConfig,
    system_name: str,
    *,
    vios_name: str | None = None,
    vios_id: int | None = None,
) -> list[dict[str, str]]:
    system = _nonblank(system_name, "system_name")
    selector = _optional_vios_selector(vios_name, vios_id)
    command = f"lslabelvios -r fcport -m {shlex.quote(system)}"
    if selector is not None:
        command += f" --filter {shlex.quote(build_filter([selector]))}"
    command += " -F --header"
    return _parse_label_rows(
        await run_hmc_command(config, command), "list VIOS FC-port labels"
    )


async def set_vios_fc_port_label(
    config: HMCConfig,
    system_name: str,
    label: str,
    port_name: str,
    *,
    vios_name: str | None = None,
    vios_id: int | None = None,
) -> dict[str, object]:
    system = _nonblank(system_name, "system_name")
    selected = _single_vios_selector(vios_name, vios_id, required=True)
    if selected is None:
        raise ValueError("VIOS label operation requires a VIOS selector")
    label_value = _nonblank(label, "label")
    port = _nonblank(port_name, "port_name")
    record = build_attribute_record(
        [("resource", "fcport"), ("port_name", port), selected]
    )
    command = (
        f"labelvios -m {shlex.quote(system)} -o s -l {shlex.quote(label_value)} "
        f"-i {shlex.quote(record)}"
    )
    return await _run_mutation(
        config,
        command,
        "set-vios-fc-port-label",
        system,
        label=label_value,
        port_name=port,
        **{selected[0][:-1]: selected[1]},
    )


async def remove_vios_fc_port_label(
    config: HMCConfig,
    system_name: str,
    port_name: str,
    *,
    vios_name: str | None = None,
    vios_id: int | None = None,
) -> dict[str, object]:
    system = _nonblank(system_name, "system_name")
    selected = _single_vios_selector(vios_name, vios_id, required=True)
    if selected is None:
        raise ValueError("VIOS label operation requires a VIOS selector")
    port = _nonblank(port_name, "port_name")
    record = build_attribute_record(
        [("resource", "fcport"), ("port_name", port), selected]
    )
    command = f"labelvios -m {shlex.quote(system)} -o r -i {shlex.quote(record)}"
    return await _run_mutation(
        config,
        command,
        "remove-vios-fc-port-label",
        system,
        port_name=port,
        **{selected[0][:-1]: selected[1]},
    )


async def list_vios_vfc_group_labels(
    config: HMCConfig, system_name: str
) -> list[dict[str, str]]:
    system = _nonblank(system_name, "system_name")
    filter_value = build_filter([("resources", "vfc")])
    command = (
        f"lslabelvios -r group -m {shlex.quote(system)} "
        f"--filter {shlex.quote(filter_value)} -F --header"
    )
    return _parse_label_rows(
        await run_hmc_command(config, command), "list VIOS vFC group labels"
    )


async def create_vios_vfc_group_label(
    config: HMCConfig,
    system_name: str,
    label: str,
    *,
    vios_names: Sequence[str] | None = None,
    vios_ids: Sequence[int] | None = None,
) -> dict[str, object]:
    system = _nonblank(system_name, "system_name")
    label_value = _group_label_name(label, "label")
    attribute, members = _member_selector(vios_names, vios_ids)
    record = build_attribute_record(
        [("resource", "vfc"), (attribute, ",".join(map(str, members)))],
        quoted={attribute},
    )
    command = (
        f"labelvios -m {shlex.quote(system)} -o a -l {shlex.quote(label_value)} "
        f"-i {shlex.quote(record)}"
    )
    return await _run_mutation(
        config,
        command,
        "create-vios-vfc-group-label",
        system,
        label=label_value,
        **{attribute: members},
    )


async def update_vios_vfc_group_label(
    config: HMCConfig,
    system_name: str,
    label: str,
    action: ViosGroupUpdateAction,
    *,
    new_name: str | None = None,
    vios_names: Sequence[str] | None = None,
    vios_ids: Sequence[int] | None = None,
) -> dict[str, object]:
    system = _nonblank(system_name, "system_name")
    label_value = _nonblank(label, "label")
    if action == "rename":
        if vios_names is not None or vios_ids is not None or new_name is None:
            raise ValueError("rename requires only new_name")
        renamed = _group_label_name(new_name, "new_name")
        record = build_attribute_record([("new_name", renamed)])
        values: dict[str, object] = {"label": label_value, "new_name": renamed}
    elif action in {"add-members", "remove-members"}:
        if new_name is not None:
            raise ValueError(f"{action} does not accept new_name")
        attribute, members = _member_selector(vios_names, vios_ids)
        operator = "+" if action == "add-members" else "-"
        operated_attribute = f"{attribute}{operator}"
        record = build_attribute_record(
            [(operated_attribute, ",".join(map(str, members)))],
            quoted={operated_attribute},
        )
        values = {"label": label_value, attribute: members}
    else:
        raise ValueError(f"unsupported VIOS group label update action {action!r}")
    command = (
        f"labelvios -m {shlex.quote(system)} -o s -l {shlex.quote(label_value)} "
        f"-i {shlex.quote(record)}"
    )
    return await _run_mutation(
        config, command, f"{action}-vios-vfc-group-label", system, **values
    )


async def remove_vios_vfc_group_label(
    config: HMCConfig, system_name: str, label: str
) -> dict[str, object]:
    system = _nonblank(system_name, "system_name")
    label_value = _nonblank(label, "label")
    command = f"labelvios -m {shlex.quote(system)} -o r -l {shlex.quote(label_value)}"
    return await _run_mutation(
        config,
        command,
        "remove-vios-vfc-group-label",
        system,
        label=label_value,
    )
