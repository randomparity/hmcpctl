"""ST42: one explicitly released physical volume's scratch group (#1370).

In the `vg-if-match` group (#1850, ADR 0328) the run also probes, between create and
cleanup, whether the HMC enforces `If-Match` on a VolumeGroup write: it POSTs the run
group's own body back under an `If-Match` that never matched.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.xmlutil import find_text

from .observation import Assertion
from .results import resource
from .storage import vios_command, volume_group_names
from .storage_lifecycle import volume_names

if TYPE_CHECKING:
    from fastmcp import Client

    from hmcpctl.config import HMCConfig
    from live_test.runner import RunState

SUBTASK = 42
SCENARIO = "st42-scratch-volume-group"
GROUPS = frozenset({"storage", "vg-if-match"})
IF_MATCH_GROUP = "vg-if-match"
_ERROR_CODES = re.compile(r"\b(?:REST|HSCL)[0-9A-Z]{4}\b")
_PV = re.compile(r"hdisk[0-9]+")
_RUN_GROUP = re.compile(r"hpvg[0-9a-f]{8}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_PVID = re.compile(r"[0-9a-fA-F]{16}|none")
_SNAPSHOT_COMMANDS = (
    "lspv -field pvname pvid vgname -fmt :",
    "lspv -free -field pvname pvid size -fmt :",
    "lsvg",
)


def is_run_group(name: object) -> bool:
    return isinstance(name, str) and _RUN_GROUP.fullmatch(name) is not None


def configured_scratch(pv: str, group: str) -> bool:
    return bool(_PV.fullmatch(pv) and is_run_group(group))


def physical_volumes(
    listing: object, *, free: bool = False
) -> dict[str, tuple[str, str]] | None:
    """Parse explicit pvname:pvid:vgname or pvname:pvid:size fields; fail closed."""
    if not isinstance(listing, str):
        return None
    if free is True and listing == ":":
        return {}
    rows = {}
    for line in listing.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3:
            return None
        name, pvid, value = parts
        if not _PV.fullmatch(name) or not _PVID.fullmatch(pvid) or name in rows:
            return None
        if free:
            if not value.isascii() or not value.isdecimal() or int(value) <= 0:
                return None
        elif value != "None" and not _NAME.fullmatch(value):
            return None
        rows[name] = (pvid, value)
    return rows


def rest_group_names(data: object) -> frozenset[str] | None:
    """A complete, unambiguous REST group listing, never filtered malformed rows."""
    if not isinstance(data, list):
        return None
    names = []
    for entry in data:
        if not isinstance(entry, Mapping):
            return None
        name = entry.get("name") or resource(entry).get("GroupName")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
            return None
        names.append(name)
    return frozenset(names)


async def if_match_probe(config: HMCConfig, vios: str, group: str) -> dict[str, Any]:
    """POST *group*'s own body back under an `If-Match` no ETag ever had (#1850).

    Goes through hmcpctl's own VolumeGroup write. Records the HTTP status, and from an
    error body only its `<Message>` and REST/HSCL codes, never the body itself.
    """
    stale = f'"stale-{secrets.token_hex(8)}"'
    async with HMCClient(config) as hmc:
        listed = [
            entry.get("UUID")
            for entry in await hmc.list_volume_groups(vios)
            if resource(entry).get("GroupName") == group
        ]
        if len(listed) != 1 or not isinstance(listed[0], str):
            return {"status": None, "reason": f"{group} is not listed exactly once"}
        _, element, _raw = await hmc._get_vg_raw_xml(vios, listed[0])
        try:
            await hmc._post_vg_xml(
                vios, listed[0], element, operation="if_match_probe", etag=stale
            )
        except HMCError as exc:
            body = exc.body or ""
            try:
                message = find_text(body, "Message") or ""
            except Exception:  # noqa: BLE001 - a non-XML body keeps only its codes
                message = ""
            return {
                "if_match": "never-matched",
                "status": exc.status_code,
                "message": message[:300],
                "codes": sorted(set(_ERROR_CODES.findall(body))),
            }
    return {"if_match": "never-matched", "status": "2xx"}


def create_guard(pv: str, group: str, inventory, free, groups) -> bool:
    return bool(
        configured_scratch(pv, group)
        and inventory is not None
        and free is not None
        and groups is not None
        and group not in groups
        and pv in inventory
        and pv in free
        and inventory[pv][0] != "none"
        and inventory[pv] == (free[pv][0], "None")
    )


def cleanup_guard(pv: str, group: str, baseline, current, members, volumes) -> bool:
    if (
        not configured_scratch(pv, group)
        or baseline is None
        or pv not in baseline
        or baseline[pv][1] != "None"
    ):
        return False
    expected = dict(baseline)
    expected[pv] = (baseline[pv][0], group)
    return current == expected and members == frozenset({pv}) and volumes == frozenset()


@dataclass(frozen=True)
class _Snapshot:
    raw: tuple[str, str, str]
    inventory: dict[str, tuple[str, str]]
    free: dict[str, tuple[str, str]]
    groups: frozenset[str]
    rest: frozenset[str]


@dataclass
class _Scenario:
    client: Client
    state: RunState
    vios: str
    vios_id: int
    pv: str
    group: str

    async def read(self, command: str, label: str) -> object | None:
        result = await self.state.call(
            self.client,
            "hmc_run_command",
            cmd=vios_command(self.state.config.system_name, self.vios_id, command),
        )
        self.state.record(SUBTASK, f"hmc_run_command ({label})", *result)
        return result[1] if result[0] == "PASS" else None

    async def snapshot(self, label: str) -> _Snapshot | None:
        status, data = await self.state.call(
            self.client,
            "hmc_list_volume_groups",
            vios_name_or_uuid=self.vios,
            system_name_or_uuid=self.state.config.system_name,
        )
        self.state.record(SUBTASK, f"hmc_list_volume_groups ({label})", status, data)
        rest = rest_group_names(data) if status == "PASS" else None
        raw = [await self.read(command, label) for command in _SNAPSHOT_COMMANDS]
        inventory = physical_volumes(raw[0])
        free = physical_volumes(raw[1], free=True)
        groups = volume_group_names(raw[2])
        if inventory is None or free is None or groups is None or rest is None:
            self.state.record(
                SUBTASK,
                f"scratch snapshot ({label})",
                "FAIL",
                None,
                "authoritative physical-volume or group snapshot could not be validated",
            )
            return None
        return _Snapshot(
            (str(raw[0]), str(raw[1]), str(raw[2])), inventory, free, groups, rest
        )

    def manual(self, reason: str) -> None:
        self.state.record(
            SUBTASK,
            "scratch volume group",
            "FAIL",
            None,
            f"MANUAL RECOVERY REQUIRED: {reason}; inspect with scripts/live_test_recovery.py; "
            "do not retry create or remove an unverified group",
        )

    async def cleanup(
        self, baseline: _Snapshot, current: _Snapshot
    ) -> tuple[bool, bool]:
        members = volume_group_names(
            await self.read(
                f"lsvg -pv {self.group} -field pvname -fmt :", "cleanup membership"
            )
        )
        volumes = volume_names(
            await self.read(f"lsvg -lv {self.group}", "cleanup volumes")
        )
        if not cleanup_guard(
            self.pv, self.group, baseline.inventory, current.inventory, members, volumes
        ):
            self.manual(
                "scratch group membership, identity or empty-volume guard refused cleanup"
            )
            return False, False
        await self.read(f"reducevg {self.group} {self.pv}", "scratch group cleanup")
        after = await self.snapshot("after cleanup")
        restored = after == baseline
        if not restored:
            self.manual(
                "scratch group absence or baseline restoration could not be confirmed"
            )
        return restored, True

    async def probe(self) -> dict[str, Any]:
        """The If-Match probe; a failure to ask is recorded and never stops cleanup."""
        config = self.state.hmc_config
        try:
            if config is None:
                raise ValueError("no HMC configuration for a direct REST client")
            probe = await if_match_probe(config, self.vios, self.group)
        except Exception as exc:  # noqa: BLE001 - any failure must still reach cleanup
            # Not BaseException: an interrupt still stops the run, as everywhere else.
            self.state.record(
                SUBTASK, "If-Match probe", "FAIL", None, f"{type(exc).__name__}"
            )
            return {}
        # Only a 412 or a success says whether If-Match is enforced.
        answered = probe.get("status") in (412, "2xx")
        self.state.record(
            SUBTASK, "If-Match probe", "PASS" if answered else "FAIL", probe
        )
        return probe if answered else {}

    async def run(self, baseline: _Snapshot) -> None:
        self.state.artifacts.storage_volume_group_name = self.group
        accepted = rest_listed = vios_listed = selected_only = restored = False
        data: Any = None
        probe: dict[str, Any] = {}
        try:
            status, data = await self.state.call(
                self.client,
                "hmc_create_volume_group",
                vios_name_or_uuid=self.vios,
                name=self.group,
                physical_volumes=[self.pv],
                system_name_or_uuid=self.state.config.system_name,
            )
            self.state.record(SUBTASK, "hmc_create_volume_group", status, data)
            accepted = status == "PASS"
            current = await self.snapshot("after create")
            if current is None:
                self.manual("cannot read scratch group post-create state")
                return
            rest_listed = self.group in current.rest
            vios_listed = self.group in current.groups
            if not rest_listed and not vios_listed:
                restored = current == baseline
                if not restored:
                    self.manual("create left physical-volume or group snapshot drift")
                return
            if self.state.group == IF_MATCH_GROUP:
                probe = await self.probe()
            restored, selected_only = await self.cleanup(baseline, current)
        finally:
            if restored:
                self.state.artifacts.storage_volume_group_name = None
            self.state.record_verified(
                SUBTASK,
                "hmc_create_volume_group",
                operation="storage.create_volume_group",
                scenario=SCENARIO,
                assertions=[
                    Assertion("create-accepted", accepted),
                    Assertion("rest-group-listed", rest_listed),
                    Assertion("vios-group-listed", vios_listed),
                    Assertion("selected-pv-only", selected_only),
                    *(
                        [Assertion("if-match-probe-decisive", "status" in probe)]
                        if self.state.group == IF_MATCH_GROUP
                        else []
                    ),
                ],
                cleanup="passed" if restored else "failed",
                data=data,
            )


async def exercise_volume_group(client: Client, state: RunState) -> None:
    """Create exactly one guarded scratch group; disabled configuration only SKIPs."""
    config = state.config
    pv, group = config.scratch_pv_name, config.scratch_vg_name
    vios, vios_id = state.artifacts.vios_uuid, state.artifacts.vios_partition_id
    if state.group not in GROUPS or not configured_scratch(pv, group):
        state.skip(
            SUBTASK,
            "scratch volume group",
            "storage or vg-if-match arm and both valid scratch PV/VG settings required",
        )
        return
    if (
        not vios
        or type(vios_id) is not int
        or vios_id <= 0
        or not _NAME.fullmatch(config.system_name)
    ):
        state.skip(
            SUBTASK,
            "scratch volume group",
            "resolved VIOS and plain managed-system name required",
        )
        return
    if state.artifacts.storage_volume_group_name is not None:
        state.skip(
            SUBTASK,
            "scratch volume group",
            "an earlier scratch group needs recovery before a new create",
        )
        return
    scenario = _Scenario(client, state, vios, vios_id, pv, group)
    baseline = await scenario.snapshot("before create")
    if (
        baseline is None
        or baseline.rest != baseline.groups
        or not create_guard(
            pv, group, baseline.inventory, baseline.free, baseline.groups
        )
    ):
        state.skip(
            SUBTASK,
            "scratch volume group",
            "named PV must be free, identified, in no VG; scratch VG must be absent in REST and VIOS",
        )
        return
    await scenario.run(baseline)
