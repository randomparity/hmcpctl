"""ST72: provision's installer boot on one scratch partition (ADR 0326, #1849).

One `hmcpctl-live-pwr-<8 hex>-p` partition is provisioned with prepared media built
from the configured ISO and `boot: immediate`. The arm records whether the mount was
accepted on the provision-created partition (REST0269, ADR 0319), whether the saved
profile carried the optical adapter, whether the activation came from that profile
and kept the optical adapter and mapping, and the reference codes the boot wait saw.
The lpar-power arm's names, teardown, comparison and recovery witness are reused, so
`live_test_recovery.py` finds what a stopped run leaves behind.

The native boot-started groups (#1850, ADR 0328) run the same subtask with producer-built
launcher media for one installation profile, a pinned MAC and the producer's network, enough
memory for the launcher, and a recorder: refcodes and partition state every 15 s until the
window ends, bracketed by HMC clock reads, plus the VIOS level.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
import shlex
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hmcpctl.operations.lpar.provisioning.boot import BOOT_STARTED_CODES
from hmcpctl.ssh.commands import build_filter

from . import lpar_config, lpar_power
from .bare_cec import _power_operations_authorized
from .observation import Assertion
from .results import field as result_field
from .vmedia import _start_iso_server

if TYPE_CHECKING:
    from fastmcp import Client

    from .config import LiveTestConfig
    from .runner import RunState

GROUP = "provision-boot"
SUBTASK = 72
SCENARIO = "st72-provision-boot"
#: Within the H1 maximum, and past the ~130 s from power-on to CA00E105 seen natively
#: (ADR 0328); the call may return first, and the operation is polled.
WAIT_SECONDS = 300
_PROFILE = ("ubuntu", "26.04.1")
# Documentation addresses (RFC 5737): the guest never reaches its installer source here.
_NETWORK: dict[str, Any] = {
    "address": "192.0.2.10/24",
    "routes": [{"destination": "0.0.0.0/0", "gateway": "192.0.2.1"}],
    "dns": [],
}
_POLLS = 20
_POLL_DELAY_S = 15.0
_PAUSED_OR_DONE = frozenset({"paused", "terminal"})
#: The native boot-started groups and the installation profile each boots (ADR 0194 D3).
NATIVE_GROUPS: dict[str, tuple[str, str]] = {
    "native-ubuntu-boot-started": ("ubuntu", "26.04.1"),
    "native-rocky-boot-started": ("rocky", "9.8"),
}
#: The producer's Rocky guidance (6,656 MiB), the larger of the two launcher floors.
NATIVE_MEMORY_MIB = 6656
#: How long a native run keeps reading after its first provision call: the operator's
#: powered-on bound, since power-on comes after that call starts.
NATIVE_WINDOW_S = 900.0
_RECORD_DELAY_S = 15.0
_REFCODE_COUNT = 100
_ASSERTIONS = (
    "mount-accepted",
    "saved-profile-optical-slot",
    "revalidated-before-power-on",
    "installer-powered-on",
    "activated-from-saved-profile",
    "optical-adapter-after-activation",
    "optical-mapping-after-activation",
    "boot-wait-ended-truthfully",
)
_NATIVE_ASSERTIONS = (
    "vios-level-read",
    "hmc-clock-read",
    "refcodes-read-while-running",
)


@dataclass(frozen=True)
class Media:
    """The medium a run boots, what its producer result says, and the guest it binds."""

    profile: tuple[str, str]
    iso: Path
    mac: str
    network: dict[str, Any]
    result: dict[str, Any]
    resources: dict[str, Any]


def run_mac(suffix: str) -> str:
    """A locally administered unicast MAC unique to the run's 8-hex suffix."""
    return ":".join(["02", "00", *(suffix[i : i + 2] for i in range(0, 8, 2))])


def _describe(iso: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with iso.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return {"iso_sha256": digest.hexdigest(), "iso_size": iso.stat().st_size}


def producer_result(iso: Path, mac: str) -> dict[str, Any]:
    """A prepared `iso-chain-media-v1` result describing *iso* as it is on disk."""
    return {
        "format": "iso-chain-media-v1",
        **_describe(iso),
        # Declared only: hmcpctl binds it to nothing it can check.
        "manifest_sha256": "0" * 64,
        "distribution": _PROFILE[0],
        "release": _PROFILE[1],
        "architecture": "ppc64le",
        "mac": mac,
        "network": _NETWORK,
    }


def native_media(config: LiveTestConfig, group: str) -> Media | str:
    """The producer-built medium a native group boots, or why it cannot run.

    The producer's own result is sent unchanged; it must describe the configured
    ISO's bytes, the group's profile and the configured MAC.
    """
    profile = NATIVE_GROUPS[group]
    distro = profile[0]
    key = f"LIVE_TEST_NATIVE_{distro.upper()}"
    iso = Path(getattr(config, f"native_{distro}_iso_path"))
    described = getattr(config, f"native_{distro}_media_result")
    mac = config.native_boot_mac.lower()
    if not (mac and described and iso.is_file()):
        return (
            f"{key}_ISO_PATH (a readable file), {key}_MEDIA_RESULT and "
            "LIVE_TEST_NATIVE_BOOT_MAC must all be set"
        )
    try:
        result = json.loads(Path(described).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"{key}_MEDIA_RESULT did not read: {type(exc).__name__}"
    expected = {
        "format": "iso-chain-media-v1",
        "distribution": profile[0],
        "release": profile[1],
        "architecture": "ppc64le",
        "mac": mac,
        **_describe(iso),
    }
    if not isinstance(result, dict) or not isinstance(result.get("network"), dict):
        return f"{key}_MEDIA_RESULT is not a media result with a network"
    wrong = sorted(
        name for name, value in expected.items() if result.get(name) != value
    )
    if wrong:
        return f"{key}_MEDIA_RESULT does not describe the configured medium: {wrong}"
    memory = {"desired_memory": NATIVE_MEMORY_MIB, "max_memory": NATIVE_MEMORY_MIB}
    return Media(
        profile, iso, mac, result["network"], result, {**lpar_power.RESOURCES, **memory}
    )


def _fixture_media(config: LiveTestConfig, run: lpar_power.Run) -> Media:
    """ST72's own: the configured ISO described here, on documentation addresses."""
    iso, mac = Path(config.iso_path), run_mac(run.suffix)
    return Media(
        _PROFILE, iso, mac, _NETWORK, producer_result(iso, mac), lpar_power.RESOURCES
    )


def steps_by_name(data: object) -> dict[str, Mapping[str, Any]]:
    """The result's steps by name; the last of a repeated name wins."""
    # The whole result, so served steps (nested dataclasses) become dicts too.
    plain = lpar_power._plain(data)
    steps = plain.get("steps") if isinstance(plain, Mapping) else None
    items = steps if isinstance(steps, list) else []
    return {str(step.get("step")): step for step in items if isinstance(step, Mapping)}


def _boot_wait_truthful(outcome: object, wait: Mapping[str, Any] | None) -> bool:
    """boot_started on a boot-started code, or needs_attention with what it saw."""
    result = (wait or {}).get("result")
    if not isinstance(result, Mapping):
        return False
    if outcome == "boot_started":
        return result.get("refcode") in BOOT_STARTED_CODES
    observed = result.get("observed")
    return (
        outcome == "needs_attention"
        and isinstance(observed, list)
        and not any(
            isinstance(row, Mapping) and row.get("refcode") in BOOT_STARTED_CODES
            for row in observed
        )
    )


def _ok(steps: Mapping[str, Mapping[str, Any]], name: str) -> bool:
    return (steps.get(name) or {}).get("status") == "ok"


async def _provision(
    client: Client, state: RunState, run: lpar_power.Run, media: Media
) -> tuple[str, Any]:
    """The provision call, repeated until its operation is paused or terminal."""
    vios = run.vios
    assert vios is not None
    request_id = f"{run.token}-boot"

    async def call() -> tuple[str, Any]:
        # A repeat with the same arguments returns the same operation (ADR 0190).
        return await state.call(
            client,
            "hmc_provision_lpar",
            request_id=request_id,
            wait_seconds=WAIT_SECONDS,
            system_name_or_uuid=run.system,
            name=run.p_name,
            caller_token=run.token,
            adapters={"port_vlan_id": run.vlan, "mac": media.mac},
            storage={
                "vios_uuid": vios.uuid,
                "storage_name": run.volume,
                "kind": "VirtualDisk",
                "vg_uuid": vios.group_uuid,
                "capacity_mib": lpar_power.VOLUME_MIB,
            },
            resources=media.resources,
            install={
                "profile": "-".join(media.profile),
                "network": media.network,
                "media": {
                    "mode": "prepared",
                    "url": state.config.iso_url,
                    "producer_result": media.result,
                },
            },
            boot="immediate",
            exclusive_writer_window=True,
        )

    run.volume_attempted = True
    # Recorded first, so recovery reads the repository even if the response is lost.
    recorded = {"vios_uuid": vios.uuid, "vg_uuid": None, "media_name": None}
    state.artifacts.provision_boot_media = recorded
    st, data = await call()
    for _ in range(_POLLS):
        operation = lpar_power._plain(result_field(data, "operation"))
        if st != "PASS" or not isinstance(operation, Mapping):
            break
        run.provisions.add(request_id)
        run.requests[request_id] = operation.get("state") == "terminal"
        if operation.get("state") in _PAUSED_OR_DONE:
            break
        await asyncio.sleep(_POLL_DELAY_S)
        st, data = await call()
    state.record(SUBTASK, "hmc_provision_lpar (install, boot immediate)", st, data)
    if not run.provisions and not await _recorded(client, state, request_id):
        state.artifacts.provision_boot_media = {}  # refused before anything was recorded
    elif not run.provisions:
        run.provisions.add(request_id)  # a lost response: teardown abandons it
        run.requests[request_id] = False
    steps = steps_by_name(data)
    for step, key in (("bind_media", "media_name"), ("upload_media", "vg_uuid")):
        result = (steps.get(step) or {}).get("result")
        if isinstance(result, Mapping) and isinstance(result.get(key), str):
            recorded[key] = result[key]
    found = result_field(data, "lpar_uuid") if st == "PASS" else None
    run.p_uuid = found if isinstance(found, str) else None
    if run.p_uuid is None:
        run.p_uuid = await lpar_power._adopt(client, state, run, run.p_name)
    return st, data


async def _recorded(client: Client, state: RunState, request_id: str) -> bool:
    """Whether an operation exists for *request_id*; True when that cannot be read."""
    st, page = await state.call(client, "hmc_operation_status", request_id=request_id)
    if st != "PASS":
        return True
    return any(
        item.get("request_id") == request_id
        for item in lpar_power._items(page, "operations")
    )


async def _activated_from_saved(
    client: Client, state: RunState, run: lpar_power.Run, operation_id: str
) -> bool:
    saved = f"hmcpctl_{operation_id[:12]}"
    row = await lpar_config._cli(
        client,
        state,
        f"lssyscfg -r lpar -m {shlex.quote(run.system)} "
        f"--filter {shlex.quote(build_filter([('lpar_names', run.p_name)]))} "
        "-F curr_profile,state",
    )
    run.data["activation"] = row
    profile, _, partition_state = (row or "").partition(",")
    return profile == saved and partition_state.lower() != "not activated"


async def _optical_slot_current(
    client: Client, state: RunState, run: lpar_power.Run, slot: object
) -> bool:
    """Whether the running partition's current configuration has the optical slot."""
    listing = await lpar_config._cli(
        client,
        state,
        f"lshwres -r virtualio --rsubtype scsi -m {shlex.quote(run.system)} "
        f"--level lpar --filter {shlex.quote(build_filter([('lpar_names', run.p_name)]))} "
        "-F slot_num",
    )
    run.data["client_scsi_slots"] = listing
    return str(slot) in (listing or "").split()


async def _media_mapped(
    client: Client, state: RunState, run: lpar_power.Run, media: str
) -> bool | None:
    """Whether *media* is mapped to the run partition; None when unreadable."""
    assert run.vios is not None
    st, data = await state.call(
        client,
        "hmc_list_optical_mappings",
        vios_name_or_uuid=run.vios.uuid,
        lpar_name_or_uuid=run.p_uuid,
        system_name_or_uuid=run.system,
    )
    return media in repr(data) if st == "PASS" else None


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def _clock(client: Client, state: RunState) -> dict[str, Any]:
    """The HMC's own clock between two runner UTC reads; HMC times are never compared."""
    before = _utc()
    hmc = await lpar_config._cli(client, state, "date")
    return {"utc_before": before, "hmc": hmc, "utc_after": _utc()}


def _rows(data: object) -> list[dict[str, Any]] | None:
    rows = lpar_power._plain(data)
    if not isinstance(rows, list):
        return None
    return [
        {"time_stamp": row.get("time_stamp"), "refcode": row.get("refcode")}
        for row in rows
        if isinstance(row, Mapping)
    ]


async def _record_window(
    client: Client, state: RunState, run: lpar_power.Run, deadline: float
) -> None:
    """Refcodes and partition state every 15 s until *deadline* (ADR 0328).

    Every read is kept whole and newest first; one read holds at most
    ``_REFCODE_COUNT`` rows, and the HMC's timestamps are recorded, never compared.
    """
    assert run.vios is not None and run.p_uuid is not None
    vios = build_filter([("lpar_ids", str(run.vios.partition_id))])
    run.data["vios_level"] = await lpar_config._cli(
        client,
        state,
        f"lssyscfg -r lpar -m {shlex.quote(run.system)} "
        f"--filter {shlex.quote(vios)} -F os_version",
    )
    clocks = [await _clock(client, state)]
    reads: list[dict[str, Any]] = []
    while True:
        st, rows = await state.call(
            client,
            "hmc_read_lpar_refcodes",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=run.p_name,
            count=_REFCODE_COUNT,
        )
        partition = await lpar_power._lpar_state(client, state, run, run.p_uuid)
        reads.append(
            {
                "utc": _utc(),
                "state": partition,
                "rows": _rows(rows) if st == "PASS" else None,
            }
        )
        if time.monotonic() + _RECORD_DELAY_S > deadline:
            break
        await asyncio.sleep(_RECORD_DELAY_S)
    clocks.append(await _clock(client, state))
    run.data.update(hmc_clock=clocks, refcode_reads=reads)
    level = run.data["vios_level"]
    run.hold(
        "provision.lpar",
        "vios-level-read",
        isinstance(level, str) and re.fullmatch(r"VIOS \d+(\.\d+)+", level) is not None,
    )
    run.hold("provision.lpar", "hmc-clock-read", all(c["hmc"] for c in clocks))
    run.hold(
        "provision.lpar",
        "refcodes-read-while-running",
        any(read["state"] == "running" and read["rows"] for read in reads),
    )


async def _boot(
    client: Client,
    state: RunState,
    run: lpar_power.Run,
    media: Media,
    deadline: float,
) -> None:
    st, data = await _provision(client, state, run, media)
    run.data["answered"] = st == "PASS"
    steps = steps_by_name(data)
    operation = lpar_power._plain(result_field(data, "operation"))
    operation = operation if isinstance(operation, Mapping) else {}
    mount = steps.get("mount_media") or {}
    run.data.update(
        outcome=operation.get("outcome"),
        mount=mount,
        wait=steps.get("wait_boot_started"),
        upload=steps.get("upload_media"),
    )
    run.hold("provision.lpar", "mount-accepted", _ok(steps, "mount_media"))
    run.hold(
        "provision.lpar", "saved-profile-optical-slot", _ok(steps, "profile_readback")
    )
    run.hold(
        "provision.lpar", "revalidated-before-power-on", _ok(steps, "revalidate_boot")
    )
    run.hold("provision.lpar", "installer-powered-on", _ok(steps, "power_on"))
    run.hold(
        "provision.lpar",
        "boot-wait-ended-truthfully",
        _boot_wait_truthful(operation.get("outcome"), steps.get("wait_boot_started")),
    )
    bound = (steps.get("bind_media") or {}).get("result")
    name = bound.get("media_name") if isinstance(bound, Mapping) else None
    run.data["media_name"] = name
    if st != "PASS" or run.p_uuid is None or not _ok(steps, "power_on"):
        return
    result = mount.get("result")
    slot = result.get("client_slot") if isinstance(result, Mapping) else None
    run.hold(
        "provision.lpar",
        "activated-from-saved-profile",
        await _activated_from_saved(
            client, state, run, str(operation.get("operation_id") or "")
        ),
    )
    run.hold(
        "provision.lpar",
        "optical-adapter-after-activation",
        slot is not None and await _optical_slot_current(client, state, run, slot),
    )
    run.hold(
        "provision.lpar",
        "optical-mapping-after-activation",
        isinstance(name, str) and await _media_mapped(client, state, run, name) is True,
    )
    if state.group in NATIVE_GROUPS:
        await _record_window(client, state, run, deadline)


async def _unmount(
    client: Client, state: RunState, run: lpar_power.Run, name: str
) -> bool:
    """Power the partition off and unmount *name*; whether it is no longer mapped."""
    assert run.vios is not None and run.p_uuid is not None
    off = frozenset({lpar_power._NOT_ACTIVATED})
    if await lpar_power._lpar_state(client, state, run, run.p_uuid) not in off:
        st, data = await lpar_power._power_off(
            client, state, run, run.p_uuid, immediate=True
        )
        state.record(SUBTASK, "hmc_power_off_lpar (teardown)", st, data)
        await lpar_power._wait_for_state(client, state, run, run.p_uuid, off)
    st, data = await state.call(
        client,
        "hmc_unmount_optical_media",
        vios_name_or_uuid=run.vios.uuid,
        lpar_name_or_uuid=run.p_uuid,
        media_name=name,
        system_name_or_uuid=run.system,
    )
    state.record(SUBTASK, "hmc_unmount_optical_media (teardown)", st, data)
    return await _media_mapped(client, state, run, name) is False


async def _teardown_media(
    client: Client, state: RunState, run: lpar_power.Run
) -> tuple[bool, bool]:
    """Abandon, power off, unmount and delete the run's medium.

    Returns whether the medium is unmapped, and whether it is gone. A partition is
    never deleted while the medium is mapped: its server adapter would be stranded.
    """
    await lpar_power._abandon(client, state, run)
    run.requests = dict.fromkeys(run.requests, True)
    media = state.artifacts.provision_boot_media
    name, group = media.get("media_name"), media.get("vg_uuid")
    if not media or run.vios is None:
        return True, True
    if not isinstance(name, str):
        # An answered provision that bound no medium created none; a lost answer
        # leaves the partition for recovery rather than guess.
        answered = run.data.get("answered") is True
        return answered or run.p_uuid is None, answered
    unmapped = run.p_uuid is None or await _unmount(client, state, run, name)
    if not unmapped or not isinstance(group, str):
        return unmapped, False
    st, data = await state.call(
        client,
        "hmc_delete_optical_media",
        vios_name_or_uuid=run.vios.uuid,
        vg_uuid=group,
        media_name=name,
        system_name_or_uuid=run.system,
    )
    state.record(SUBTASK, "hmc_delete_optical_media (teardown)", st, data)
    st, data = await state.call(
        client,
        "hmc_list_optical_media",
        vios_name_or_uuid=run.vios.uuid,
        vg_uuid=group,
        system_name_or_uuid=run.system,
    )
    return True, st == "PASS" and name not in repr(data)


def _media(state: RunState, run: lpar_power.Run) -> Media | str:
    """The medium this group boots, or why the arm cannot run here."""
    if state.group in NATIVE_GROUPS:
        return native_media(state.config, state.group)
    if state.group != GROUP:
        groups = sorted([GROUP, *NATIVE_GROUPS])
        return f"powers on a partition with installer media: runs only in {groups}"
    if not Path(state.config.iso_path).is_file():
        return "LIVE_TEST_ISO_PATH names no readable file"
    return _fixture_media(state.config, run)


async def _preconditions(
    client: Client, state: RunState, run: lpar_power.Run
) -> lpar_power.Baseline | str:
    if not state.config.partition_profile_writer_window:
        return lpar_power.PROVISION_WINDOW_SKIP
    if not _power_operations_authorized():
        return "HMC_AUTHORIZE_POWER_OPERATIONS is off"
    if state.group in NATIVE_GROUPS:
        # The guest takes the address the test partition normally holds (ADR 0328).
        owner = await lpar_power._lpar_state(client, state, run, state.config.lp3_name)
        if owner != lpar_power._NOT_ACTIVATED:
            return (
                f"the test partition {state.config.lp3_name!r} reads {owner!r}, not "
                "Not Activated: the native guest would share its address"
            )
    why = await lpar_power._find_vios(client, state, run) or (
        await lpar_power._vlan_refusal(client, state, run)
    )
    if why is not None:
        return why
    # After the VIOS: the baseline then covers its mappings, adapters and volumes.
    baseline = await lpar_power._read_baseline(client, state, run)
    if baseline is None:
        return "the system baseline did not read"
    stranded = lpar_power.scratch_partitions(lpar_power._names(baseline.partitions))
    stranded += lpar_power.scratch_volumes(baseline.volumes or ())
    if stranded:
        return f"{stranded} already exist: run live_test_recovery.py first"
    return baseline


async def exercise_provision_boot(client: Client, state: RunState) -> None:
    print("\n=== ST72: provision installer boot (issues #1849, #1850) ===")
    run = lpar_power.Run(state.config.system_name, uuid.uuid4().hex[:8])
    group = state.group or GROUP
    media = _media(state, run)
    if isinstance(media, str):
        state.skip(SUBTASK, group, media)
        return
    ready = await _preconditions(client, state, run)
    if isinstance(ready, str):
        state.skip(SUBTASK, group, ready)
        return
    # The run's ISO server publishes the medium this group boots.
    state.config = dataclasses.replace(state.config, iso_path=str(media.iso))
    why = _start_iso_server(state, SUBTASK)
    if why is not None:
        state.skip(SUBTASK, group, why)
        return
    media_gone = False
    try:
        deadline = time.monotonic() + NATIVE_WINDOW_S
        await _boot(client, state, run, media, deadline)
    finally:
        unmapped, media_gone = await _teardown_media(client, state, run)
        if unmapped:
            await lpar_power._teardown(client, state, run)
        else:
            state.record(
                SUBTASK,
                "run medium teardown",
                "FAIL",
                f"partition {run.p_name!r}, its volume and medium were kept: the medium "
                "could not be shown unmapped. MANUAL RECOVERY REQUIRED: unmount it with "
                "hmc_unmount_optical_media, then run live_test_recovery.py for the "
                "remaining commands.",
            )
    same = await lpar_power._compare(client, state, run, ready)
    cleanup = "passed" if run.clean and media_gone and same else "failed"
    state.record_verified(
        SUBTASK,
        "hmc_provision_lpar",
        operation="provision.lpar",
        scenario=SCENARIO,
        assertions=[
            Assertion(name, run.holds("provision.lpar", name))
            for name in (
                _ASSERTIONS + _NATIVE_ASSERTIONS
                if group in NATIVE_GROUPS
                else _ASSERTIONS
            )
        ],
        cleanup=cleanup,
        data=run.data,
    )
