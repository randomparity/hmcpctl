"""Capture how the HMC and VIOS change the medium in a virtual optical device (#1284).

ST71 runs only in its own arm and answers the four questions #1285 depended on, on one
authorized window: whether a ``ViosSCSIMapping`` POST that replaces a mapping's
``Storage`` changes the loaded medium (Q1), how ``loadopt``/``unloadopt`` answer (Q2),
how they behave after an immediate power-off, without and with ``-release`` (Q3), and
what a create naming a VTD another mapping holds answers, and whether it leaves an
orphan server adapter (Q4). Since #1285, Q2 and Q3 go through ``hmc_load_optical_media``
and ``hmc_unload_optical_media`` (ADR 0317): each tool step is followed by an ``lsvopt``
read and a ``check`` row, and Q2 also records one load into the occupied device, through
the tool and as the raw command, to capture how a refusal is answered.

Every HMC call it makes goes to a git-ignored capture file for later tokenizing. Its rows
use ``state.record`` only: a capture promotes no maturity. It creates two blank media,
records each name before its create, and removes only those; it never removes an
adapter. Any residue it cannot clear is a ``MANUAL RECOVERY REQUIRED`` row.
"""

from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET  # nosec B405 - builds elements; parsing is defusedxml
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from defusedxml import ElementTree as DET
from fastmcp import Client

from hmcpctl.client.client_storage import _rmw_vios_mapping
from hmcpctl.client.core import HMCClient
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.storage import build_virtual_optical_mapping_document
from hmcpctl.errors import HMCError

from . import storage
from .capture import capture
from .vmedia import (
    _NO_REPOSITORY,
    _NOT_ACTIVATED,
    BLANK_PREFIX,
    _forget_run_media,
    _protected_reason,
    _run_tag,
    media_sizes,
    repository_fields,
)

if TYPE_CHECKING:
    from live_test.runner import RunState

SUBTASK = 71
GROUP = "vmedia-media-change"
SIZE_MIB = 1024
#: A VIOS device name. The HMC chooses it at mount, and it enters `viosvrcmd` commands.
VTD_NAME = re.compile(r"[A-Za-z0-9_]{1,15}")
_ROW = "vmedia media change"
#: The row naming each VTD ST71 uses, written before any command touches it; recovery
#: reports an empty optical device only when one of these names it.
VTD_ROW = "vtd"
_POST = "ViosSCSIMapping POST"
_CAPTURES = Path("hmc-captures")
_EARLIER_CAPTURE = (
    "media from an earlier capture are still recorded; run subtask 22 and the "
    "recovery check first"
)

Adapters = tuple[frozenset[str], frozenset[str]]


class _Stop(Exception):
    """A step left state the capture must not build on; cleanup still runs."""


def capture_path(tag: str) -> Path:
    """The git-ignored file one ST71 run records its HMC calls to."""
    return _CAPTURES / f"vmedia-media-change-{tag}.capture.jsonl"


def _optical_target(entry: object) -> tuple[str, str] | None:
    """An optical mapping's (medium, VTD name), or None when either is absent."""
    if not isinstance(entry, dict):
        return None
    storage_ = entry.get("Storage")
    media = storage_.get("VirtualOpticalMedia") if isinstance(storage_, dict) else None
    name = media.get("MediaName") if isinstance(media, dict) else None
    target = entry.get("TargetDevice")
    device = (
        target.get("VirtualOpticalTargetDevice") if isinstance(target, dict) else None
    )
    vtd = device.get("TargetName") if isinstance(device, dict) else None
    if not isinstance(name, str) or not isinstance(vtd, str):
        return None
    return name, vtd


def _unready(state: RunState) -> str | None:
    """Why ST71 cannot start from what ST16 resolved, or None."""
    artifacts, config = state.artifacts, state.config
    if not artifacts.vios_uuid or not artifacts.vmedia_vg_uuid:
        return _NO_REPOSITORY
    if type(artifacts.vios_partition_id) is not int:
        return "no VIOS partition id resolved in ST16"
    if not artifacts.lp3_uuid:
        return "no test-partition UUID resolved in ST16"
    if config.lp3_name in config.protected_lpar_names:
        return _protected_reason(config)
    if artifacts.vmedia_change_names:
        return _EARLIER_CAPTURE
    if state.hmc_config is None:
        return "no HMC configuration for the raw ViosSCSIMapping writes"
    return None


_NO_MEDIA = "No Media"


def _lsvopt_media(listing: object) -> str | None:
    """The medium ``lsvopt -vtd`` reads, ``No Media``, or None when unparsable.

    The captured shape is a ``Media  Size(mb)`` header and one row
    (cli-vios-lsvopt-after-storage-post, cli-vios-lsvopt-no-media).
    """
    if not isinstance(listing, str):
        return None
    rows = [line.split() for line in listing.splitlines() if line.strip()]
    if len(rows) != 2 or rows[0][:1] != ["Media"]:
        return None
    return _NO_MEDIA if rows[1][:2] == ["No", "Media"] else rows[1][0]


def _tag(name: str) -> str:
    return f"{{{UOM_NS}}}{name}"


class _Capture:
    """One ST71 run against the configured VIOS and test partition."""

    def __init__(self, client: Client, state: RunState) -> None:
        artifacts, config = state.artifacts, state.config
        self.client = client
        self.state = state
        self.system = config.system_name
        self.lpar = config.lp3_name
        self.vios = str(artifacts.vios_uuid)
        self.vios_id = int(artifacts.vios_partition_id or 0)
        self.vg = str(artifacts.vmedia_vg_uuid)
        self.lpar_uuid = str(artifacts.lp3_uuid)
        self.media = (f"{BLANK_PREFIX}{_run_tag()}", f"{BLANK_PREFIX}{_run_tag()}")
        self.vtd: str | None = None
        self.powered_on = False
        self.baseline: tuple[frozenset[tuple[object, ...]], Adapters] | None = None
        self.mounted_adapters: Adapters | None = None

    # -- recording ----------------------------------------------------------

    def note(self, tool: str, step: str, result: tuple[str, Any]) -> tuple[str, Any]:
        """Record one call's row as ``<tool> (<step>)`` (recovery reads it), hand it back."""
        self.state.record(SUBTASK, f"{tool} ({step})", *result)
        return result

    def manual(self, what: str, command: str) -> None:
        self.state.record(
            SUBTASK, _ROW, "FAIL", None, f"MANUAL RECOVERY REQUIRED: {what} ({command})"
        )

    def on_vios(self, command: str) -> str:
        return storage.vios_command(self.system, self.vios_id, command)

    async def optical_tool(
        self, step: str, action: str, media: str | None = None, *, release: bool = False
    ) -> tuple[str, Any]:
        """One ``hmc_load_optical_media``/``hmc_unload_optical_media`` call on V."""
        if action == "load":
            return self.note(
                "hmc_load_optical_media",
                step,
                await self.state.call(
                    self.client,
                    "hmc_load_optical_media",
                    vios_name_or_uuid=self.vios,
                    lpar_name_or_uuid=self.lpar,
                    media_name=media,
                    system_name_or_uuid=self.system,
                    target_device=self.vtd,
                    release=release,
                ),
            )
        return self.note(
            "hmc_unload_optical_media",
            step,
            await self.state.call(
                self.client,
                "hmc_unload_optical_media",
                vios_name_or_uuid=self.vios,
                lpar_name_or_uuid=self.lpar,
                system_name_or_uuid=self.system,
                target_device=self.vtd,
                release=release,
            ),
        )

    async def checked(
        self, step: str, action: str, media: str | None = None, *, release: bool = False
    ) -> bool:
        """A tool step, then ``lsvopt``; PASS only when the device reads as expected."""
        status, _ = await self.optical_tool(step, action, media, release=release)
        read, listing = await self.vios_run(f"{step} lsvopt", f"lsvopt -vtd {self.vtd}")
        held = _lsvopt_media(listing) if read == "PASS" else None
        expected = media if action == "load" else _NO_MEDIA
        ok = status == "PASS" and held == expected
        self.state.record(
            SUBTASK,
            f"{step} check",
            "PASS" if ok else "FAIL",
            {"tool": status, "lsvopt": held, "expected": expected},
        )
        return status == "PASS"

    async def vios_run(self, step: str, command: str) -> tuple[str, Any]:
        return self.note(
            "hmc_run_command",
            step,
            await self.state.call(
                self.client, "hmc_run_command", cmd=self.on_vios(command)
            ),
        )

    # -- reads --------------------------------------------------------------

    async def ready(self) -> bool:
        """The test partition is off and the repository has room for both media."""
        status, lpar_state = self.note(
            "hmc_get_lpar_state",
            "precondition",
            await self.state.call(
                self.client,
                "hmc_get_lpar_state",
                system_name_or_uuid=self.system,
                lpar_name_or_uuid=self.lpar,
            ),
        )
        if status != "PASS" or not isinstance(lpar_state, str):
            self.state.skip(SUBTASK, _ROW, "cannot read the test partition's state")
            return False
        if lpar_state.strip().lower() != _NOT_ACTIVATED:
            self.state.skip(SUBTASK, _ROW, f"the test partition is {lpar_state!r}")
            return False
        status, repository = self.note(
            "hmc_get_media_repository",
            "precondition",
            await self.state.call(
                self.client,
                "hmc_get_media_repository",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
            ),
        )
        size = repository_fields(repository)[1] if status == "PASS" else None
        status, listing = self.note(
            "hmc_list_optical_media",
            "precondition",
            await self.state.call(
                self.client,
                "hmc_list_optical_media",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
            ),
        )
        sizes = media_sizes(listing) if status == "PASS" else None
        if (
            size is None
            or sizes is None
            or any(not isinstance(s, int | float) for s in sizes.values())
        ):
            self.state.skip(SUBTASK, _ROW, "the repository's free space is unknown")
            return False
        free = size * 1024 - sum(Decimal(str(s)) for s in sizes.values())
        if free < 2 * SIZE_MIB:
            self.state.skip(
                SUBTASK, _ROW, f"{free} MiB free in the repository < {2 * SIZE_MIB} MiB"
            )
            return False
        return True

    async def mappings(self, step: str) -> frozenset[tuple[object, ...]] | None:
        status, data = self.note(
            "hmc_list_storage_mappings",
            step,
            await self.state.call(
                self.client, "hmc_list_storage_mappings", vios_name_or_uuid=self.vios
            ),
        )
        return storage.storage_rows(data) if status == "PASS" else None

    async def adapters(self, step: str) -> Adapters | None:
        rows = []
        for attribute, value in (("lpar_ids", self.vios_id), ("lpar_names", self.lpar)):
            status, listing = self.note(
                "hmc_run_command",
                f"{step} adapters {attribute}",
                await self.state.call(
                    self.client,
                    "hmc_run_command",
                    cmd=storage.scsi_adapter_listing(self.system, attribute, value),
                ),
            )
            parsed = storage.adapter_rows(listing) if status == "PASS" else None
            if parsed is None:
                return None
            rows.append(parsed)
        return rows[0], rows[1]

    async def optical(self, step: str) -> list[tuple[str, str]] | None:
        status, data = self.note(
            "hmc_list_optical_mappings",
            step,
            await self.state.call(
                self.client,
                "hmc_list_optical_mappings",
                vios_name_or_uuid=self.vios,
                lpar_name_or_uuid=self.lpar,
                system_name_or_uuid=self.system,
            ),
        )
        if status != "PASS" or not isinstance(data, list):
            return None
        return [target for target in map(_optical_target, data) if target]

    async def listed_media(self, step: str) -> dict[str, Any] | None:
        status, data = self.note(
            "hmc_list_optical_media",
            step,
            await self.state.call(
                self.client,
                "hmc_list_optical_media",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
            ),
        )
        return media_sizes(data) if status == "PASS" else None

    async def empty_target(self) -> bool | None:
        """Whether V is mapped with no medium; None when unreadable."""
        status, data = self.note(
            "hmc_list_storage_mappings",
            "cleanup empty target",
            await self.state.call(
                self.client,
                "hmc_list_storage_mappings",
                vios_name_or_uuid=self.vios,
                lpar_name_or_uuid=self.lpar,
                system_name_or_uuid=self.system,
            ),
        )
        if status != "PASS" or not isinstance(data, list):
            return None
        return any(
            isinstance(row, dict)
            and str(row.get("id", "")).endswith(f"/{self.vtd}")
            and "backing_kind" in row
            and row["backing_kind"] is None
            for row in data
        )

    # -- the sequence -------------------------------------------------------

    async def run(self) -> None:
        mappings, adapters = (
            await self.mappings("baseline"),
            await self.adapters("baseline"),
        )
        if mappings is None or adapters is None:
            self.state.skip(SUBTASK, _ROW, "the baseline could not be read")
            return
        self.baseline = (mappings, adapters)
        await self.create_media()
        await self.mount()
        await self.help()
        await self.collide()
        if await self.replace_storage():
            await self.cli_swap()
            await self.release_cycle()

    async def create_media(self) -> None:
        for name in self.media:
            self.state.artifacts.vmedia_change_names.append(name)
            self.note(
                "hmc_create_optical_media",
                "blank",
                await self.state.call(
                    self.client,
                    "hmc_create_optical_media",
                    vios_name_or_uuid=self.vios,
                    vg_uuid=self.vg,
                    media_name=name,
                    size_mib=SIZE_MIB,
                    system_name_or_uuid=self.system,
                ),
            )
        listed = await self.listed_media("post-create")
        if listed is None or any(name not in listed for name in self.media):
            raise _Stop("a created medium is not listed")

    async def mount(self) -> None:
        self.note(
            "hmc_mount_optical_media",
            "blank",
            await self.state.call(
                self.client,
                "hmc_mount_optical_media",
                vios_name_or_uuid=self.vios,
                media_name=self.media[0],
                lpar_name_or_uuid=self.lpar,
                system_name_or_uuid=self.system,
            ),
        )
        targets = await self.optical("post-mount")
        vtd = dict(targets or []).get(self.media[0])
        if vtd is None:
            raise _Stop("the mounted medium's VTD could not be read")
        self.use_vtd(vtd)
        self.mounted_adapters = await self.adapters("post-mount")
        if self.mounted_adapters is None or self.baseline is None:
            raise _Stop("the post-mount adapter listing could not be read")
        base = self.baseline[1]
        added = sorted(
            (self.mounted_adapters[0] - base[0]) | (self.mounted_adapters[1] - base[1])
        )
        self.state.record(SUBTASK, "mount adapter delta", "PASS", {"added": added})

    def use_vtd(self, vtd: str) -> None:
        if not VTD_NAME.fullmatch(vtd):
            raise _Stop(f"unusable VTD name {vtd!r}")
        self.vtd = vtd
        self.state.record(SUBTASK, VTD_ROW, "PASS", {"vtd": vtd})

    async def help(self) -> None:
        await self.vios_run("help loadopt", "help loadopt")
        await self.vios_run("help unloadopt", "help unloadopt")
        await self.vios_run("lsvopt", f"lsvopt -vtd {self.vtd}")

    async def raw(
        self, step: str, mutate: Callable[[HMCClient, ET.Element, str | None], None]
    ) -> tuple[str, Any]:
        """One read-modify-write of the VIOS mapping group; its answer is the row."""
        path = f"/rest/api/uom/VirtualIOServer/{self.vios}?group=ViosSCSIMapping"
        config = self.state.hmc_config
        if config is None:
            raise _Stop("no HMC configuration")
        try:
            async with HMCClient(config) as hmc:
                body = await _rmw_vios_mapping(
                    hmc,
                    f"capture {step}",
                    path,
                    {"vios_uuid": self.vios},
                    lambda mappings, system: mutate(hmc, mappings, system),
                )
        except HMCError as error:
            result = ("FAIL", {"status": error.status_code, "body": error.body})
        else:
            result = ("PASS", {"body": body})
        self.state.record(SUBTASK, f"{_POST} ({step})", *result)
        return result

    def _append_collision(
        self, hmc: HMCClient, mappings: ET.Element, system: str | None
    ) -> None:
        """Q4: append a mapping of medium B naming the VTD medium A already holds."""
        if system is None:
            raise _Stop("the VIOS names no managed system")
        document = build_virtual_optical_mapping_document(
            self.media[1], hmc.get_lpar_link(system, self.lpar_uuid), self.vtd
        )
        mapping = DET.fromstring(document).find(f".//{_tag('VirtualSCSIMapping')}")
        if mapping is None:
            raise _Stop("the mapping document holds no VirtualSCSIMapping")
        mappings.append(mapping)

    def _replace_storage(
        self, _hmc: HMCClient, mappings: ET.Element, _system: str | None
    ) -> None:
        """Q1: make V's mapping name medium B as its only optical storage."""
        for mapping in mappings.findall(_tag("VirtualSCSIMapping")):
            name = mapping.findtext(
                f"{_tag('TargetDevice')}/{_tag('VirtualOpticalTargetDevice')}"
                f"/{_tag('TargetName')}"
            )
            held = mapping.find(_tag("Storage"))
            if name == self.vtd and held is not None:
                for child in list(held):
                    held.remove(child)
                media = ET.SubElement(
                    held, _tag("VirtualOpticalMedia"), schemaVersion="V1_0"
                )
                ET.SubElement(ET.SubElement(media, _tag("Metadata")), _tag("Atom"))
                ET.SubElement(
                    media, _tag("MediaName"), kb="CUR", kxe="false"
                ).text = self.media[1]
                return
        raise _Stop(f"no mapping holds {self.vtd} with storage")

    async def collide(self) -> None:
        before = await self.mappings("pre-collision")
        await self.raw("q4 collision", self._append_collision)
        after = await self.mappings("post-collision")
        now = await self.adapters("post-collision")
        mounted = self.mounted_adapters
        if now is None or mounted is None:
            self.manual(
                "the adapter listing after the collision write could not be read",
                f"lshwres -r virtualio --rsubtype scsi -m {shlex.quote(self.system)} "
                "--level lpar",
            )
        else:
            vios_side = sorted(now[0] - mounted[0])
            lpar_side = sorted(now[1] - mounted[1])
            self.state.record(
                SUBTASK,
                "q4 orphan adapter",
                "PASS",
                {"vios": vios_side, "lpar": lpar_side},
            )
            for rows, where in (
                (vios_side, f"--id {self.vios_id}"),
                (lpar_side, f"-p {shlex.quote(self.lpar)}"),
            ):
                for row in rows:
                    self.manual(
                        f"an adapter the refused create added ({row})",
                        f"chhwres -r virtualio --rsubtype scsi -m "
                        f"{shlex.quote(self.system)} -o r {where} "
                        f"-s {shlex.quote(row.split(',', 1)[0])}",
                    )
        if before is None or after != before:
            raise _Stop("the collision write changed the mapping set, or it is unread")

    async def replace_storage(self) -> bool:
        """Q1, then re-derive V; False when no run medium is mapped any more."""
        await self.raw("q1 replace storage", self._replace_storage)
        await self.vios_run("q1 lsvopt", f"lsvopt -vtd {self.vtd}")
        targets = await self.optical("post-q1")
        if targets is None:
            raise _Stop("the optical mappings after q1 could not be read")
        held = [vtd for media, vtd in targets if media in self.media]
        if not held:
            self.state.skip(SUBTASK, _ROW, "no run medium is mapped after q1; q2, q3")
            return False
        if held[0] != self.vtd:
            self.state.record(SUBTASK, "q1 target renamed", "PASS", {"vtd": held[0]})
            self.use_vtd(held[0])
        return True

    async def cli_swap(self) -> None:
        """Q2: unload and load through the tools while the partition is off.

        Then load the other medium into the occupied device, through the tool and as
        the raw command, so the capture holds how ``viosvrcmd`` answers a refusal.
        """
        await self.checked("q2 unload", "unload")
        if not await self.checked("q2 load", "load", self.media[0]):
            return
        status, _ = await self.optical_tool("q2 occupied", "load", self.media[1])
        self.state.record(
            SUBTASK,
            "q2 occupied check",
            "PASS" if status == "FAIL" else "FAIL",
            {"tool": status},
        )
        await self.vios_run(
            "q2 occupied raw", f"loadopt -disk {self.media[1]} -vtd {self.vtd}"
        )
        await self.vios_run("q2 occupied lsvopt", f"lsvopt -vtd {self.vtd}")

    async def release_cycle(self) -> None:
        """Q3: the tools after an immediate power-off, without and with ``release``."""
        self.powered_on = True
        powered, _ = self.note(
            "hmc_power_on_lpar",
            "q3",
            await self.state.call(
                self.client,
                "hmc_power_on_lpar",
                lpar_name_or_uuid=self.lpar,
                system_name_or_uuid=self.system,
                wait=True,
                timeout_seconds=120,
            ),
        )
        if powered != "PASS":
            # No client opened the device, so a plain unloadopt would answer a
            # question Q3 does not ask.
            self.state.skip(SUBTASK, _ROW, f"q3 unanswered: power-on {powered}")
            return
        self.note(
            "hmc_power_off_lpar",
            "q3 immediate",
            await self.state.call(
                self.client,
                "hmc_power_off_lpar",
                lpar_name_or_uuid=self.lpar,
                system_name_or_uuid=self.system,
                immediate=True,
                wait=True,
            ),
        )
        if not await self.checked("q3 unload", "unload"):
            await self.checked("q3 unload release", "unload", release=True)
        await self.checked("q3 load release", "load", self.media[1], release=True)
        await self.checked("q3 unload release again", "unload", release=True)
        await self.checked("q3 load", "load", self.media[0])

    # -- cleanup ------------------------------------------------------------

    async def cleanup(self) -> None:
        """Leave the partition off and remove this run's mapping and media."""
        if self.powered_on:
            _, now = self.note(
                "hmc_get_lpar_state",
                "cleanup",
                await self.state.call(
                    self.client,
                    "hmc_get_lpar_state",
                    system_name_or_uuid=self.system,
                    lpar_name_or_uuid=self.lpar,
                ),
            )
            if not (isinstance(now, str) and now.strip().lower() == _NOT_ACTIVATED):
                off, _ = self.note(
                    "hmc_power_off_lpar",
                    "cleanup",
                    await self.state.call(
                        self.client,
                        "hmc_power_off_lpar",
                        lpar_name_or_uuid=self.lpar,
                        system_name_or_uuid=self.system,
                        immediate=True,
                        wait=True,
                    ),
                )
                if off != "PASS":
                    self.manual(
                        f"{self.lpar} may still be running",
                        f"chsysstate -m {shlex.quote(self.system)} -r lpar "
                        f"-n {shlex.quote(self.lpar)} -o shutdown --immed",
                    )
        if self.vtd:
            await self.reload_empty_target()
        targets = await self.optical("cleanup")
        if targets is None:
            self.manual(
                "the optical mappings could not be read",
                f"hmcpctl storage list-mappings {shlex.quote(self.vios)} "
                f"--lpar {shlex.quote(self.lpar)} --system {shlex.quote(self.system)}",
            )
        for media, _ in targets or []:
            if media in self.media:
                self.note(
                    "hmc_unmount_optical_media",
                    "cleanup",
                    await self.state.call(
                        self.client,
                        "hmc_unmount_optical_media",
                        vios_name_or_uuid=self.vios,
                        lpar_name_or_uuid=self.lpar,
                        media_name=media,
                        system_name_or_uuid=self.system,
                    ),
                )
        await self.delete_media()
        await self.compare_baseline()

    async def reload_empty_target(self) -> None:
        """Load medium A into an empty V so the tool can address the mapping."""
        empty = await self.empty_target()
        if empty is None:
            self.manual(
                f"whether {self.vtd} is mapped with no medium could not be read",
                self.on_vios(f"rmvdev -vtd {self.vtd}"),
            )
            return
        if not empty:
            return
        status, _ = await self.optical_tool("cleanup load", "load", self.media[0])
        if status != "PASS":
            await self.optical_tool(
                "cleanup load release", "load", self.media[0], release=True
            )
        if await self.empty_target() is not False:
            self.manual(
                f"{self.vtd} may be mapped with no medium",
                self.on_vios(f"rmvdev -vtd {self.vtd}"),
            )

    async def delete_media(self) -> None:
        created = [
            name
            for name in self.media
            if name in self.state.artifacts.vmedia_change_names
        ]
        if not created:
            return
        listed = await self.listed_media("cleanup")
        for name in created:
            if listed is not None and name in listed:
                self.note(
                    "hmc_delete_optical_media",
                    "cleanup",
                    await self.state.call(
                        self.client,
                        "hmc_delete_optical_media",
                        vios_name_or_uuid=self.vios,
                        vg_uuid=self.vg,
                        media_name=name,
                        system_name_or_uuid=self.system,
                    ),
                )
        after = await self.listed_media("post-cleanup")
        for name in created:
            if after is not None and name not in after:
                _forget_run_media(self.state, name)
            else:
                self.manual(
                    f"{name} may still be in the repository",
                    f"hmcpctl storage delete-media {shlex.quote(self.vios)} "
                    f"--vg {shlex.quote(self.vg)} --name {shlex.quote(name)}",
                )

    async def compare_baseline(self) -> None:
        if self.baseline is None:
            return
        mappings, adapters = self.baseline
        if await self.mappings("post-cleanup") != mappings:
            self.manual(
                "VIOS storage mappings differ from the baseline",
                f"hmcpctl storage list-mappings {shlex.quote(self.vios)} "
                f"--system {shlex.quote(self.system)}",
            )
        now = await self.adapters("post-cleanup")
        if now != adapters:
            added = (
                sorted((now[0] - adapters[0]) | (now[1] - adapters[1]))
                if now is not None
                else ["(adapter listing unreadable)"]
            )
            self.manual(
                f"vSCSI adapters differ from the baseline: {', '.join(added)}",
                f"chhwres -r virtualio --rsubtype scsi -m {shlex.quote(self.system)} "
                f"-o r --id <partition id> -s <slot>",
            )


async def vmedia_media_change(client: Client, state: RunState) -> None:
    print("\n=== ST71: Optical media change capture (#1284) ===")
    if state.group != GROUP:
        state.skip(SUBTASK, _ROW, f"runs only in the {GROUP} arm")
        return
    reason = _unready(state)
    if reason:
        state.skip(SUBTASK, _ROW, reason)
        return
    arm = _Capture(client, state)
    _CAPTURES.mkdir(mode=0o700, exist_ok=True)
    path = capture_path(_run_tag())
    state.record(SUBTASK, "capture", "PASS", {"path": str(path)})
    with capture(path):
        if not await arm.ready():
            return
        try:
            await arm.run()
        except _Stop as stop:
            state.record(SUBTASK, _ROW, "FAIL", None, f"stopped: {stop}")
        finally:
            await arm.cleanup()
