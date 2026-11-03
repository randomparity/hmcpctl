"""ST72: provision's installer boot on one scratch partition (ADR 0326, #1849).

One `hmcpctl-live-pwr-<8 hex>-p` partition is provisioned with prepared media built
from the configured ISO and `boot: immediate`. The arm records whether the mount was
accepted on the provision-created partition (REST0269, ADR 0319), whether the saved
profile carried the optical adapter, whether the activation came from that profile
and kept the optical adapter and mapping, and the reference codes the boot wait saw.
The lpar-power arm's names, teardown, comparison and recovery witness are reused, so
`live_test_recovery.py` finds what a stopped run leaves behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hmcpctl.ssh.commands import build_filter

from . import lpar_config, lpar_power
from .bare_cec import _power_operations_authorized
from .observation import Assertion
from .results import field as result_field
from .vmedia import _start_iso_server

if TYPE_CHECKING:
    from fastmcp import Client

    from .runner import RunState

GROUP = "provision-boot"
SUBTASK = 72
SCENARIO = "st72-provision-boot"
#: Within the H1 maximum; the call may return first, and the operation is polled.
WAIT_SECONDS = 120
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


def run_mac(suffix: str) -> str:
    """A locally administered unicast MAC unique to the run's 8-hex suffix."""
    return ":".join(["02", "00", *(suffix[i : i + 2] for i in range(0, 8, 2))])


def producer_result(iso: Path, mac: str) -> dict[str, Any]:
    """A prepared `iso-chain-media-v1` result describing *iso* as it is on disk."""
    digest = hashlib.sha256()
    with iso.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return {
        "format": "iso-chain-media-v1",
        "iso_sha256": digest.hexdigest(),
        # Declared only: hmcpctl binds it to nothing it can check.
        "manifest_sha256": "0" * 64,
        "iso_size": iso.stat().st_size,
        "distribution": _PROFILE[0],
        "release": _PROFILE[1],
        "architecture": "ppc64le",
        "mac": mac,
        "network": _NETWORK,
    }


def steps_by_name(data: object) -> dict[str, Mapping[str, Any]]:
    """The result's steps by name; the last of a repeated name wins."""
    steps = lpar_power._plain(result_field(data, "steps"))
    items = steps if isinstance(steps, list) else []
    return {str(step.get("step")): step for step in items if isinstance(step, Mapping)}


def _ok(steps: Mapping[str, Mapping[str, Any]], name: str) -> bool:
    return (steps.get(name) or {}).get("status") == "ok"


async def _provision(
    client: Client, state: RunState, run: lpar_power.Run, iso: Path
) -> tuple[str, Any]:
    """The provision call, repeated until its operation is paused or terminal."""
    vios = run.vios
    assert vios is not None
    mac = run_mac(run.suffix)
    request_id = f"{run.token}-boot"
    result = producer_result(iso, mac)

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
            adapters={"port_vlan_id": run.vlan, "mac": mac},
            storage={
                "vios_uuid": vios.uuid,
                "storage_name": run.volume,
                "kind": "VirtualDisk",
                "vg_uuid": vios.group_uuid,
                "capacity_mib": lpar_power.VOLUME_MIB,
            },
            resources=lpar_power.RESOURCES,
            install={
                "profile": "-".join(_PROFILE),
                "network": _NETWORK,
                "media": {
                    "mode": "prepared",
                    "url": state.config.iso_url,
                    "producer_result": result,
                },
            },
            boot="immediate",
            exclusive_writer_window=True,
        )

    run.volume_attempted = True
    # Recorded first, so recovery reads the repository even if the response is lost.
    media = {"vios_uuid": vios.uuid, "vg_uuid": None, "media_name": None}
    state.artifacts.provision_boot_media = media
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
            media[key] = result[key]
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


async def _boot(
    client: Client, state: RunState, run: lpar_power.Run, iso: Path
) -> None:
    st, data = await _provision(client, state, run, iso)
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
    wait = steps.get("wait_boot_started") or {}
    run.hold(
        "provision.lpar",
        "refcodes-observed-without-boot-started-claim",
        operation.get("outcome") == "needs_attention"
        and isinstance(wait.get("result"), Mapping)
        and "observed" in wait["result"],
    )
    media = (steps.get("bind_media") or {}).get("result")
    name = media.get("media_name") if isinstance(media, Mapping) else None
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
        return run.p_uuid is None, False
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


async def _preconditions(
    client: Client, state: RunState, run: lpar_power.Run
) -> lpar_power.Baseline | str:
    if state.group != GROUP:
        return f"powers on a partition with installer media: runs only in {GROUP!r}"
    if not state.config.partition_profile_writer_window:
        return lpar_power.PROVISION_WINDOW_SKIP
    if not _power_operations_authorized():
        return "HMC_AUTHORIZE_POWER_OPERATIONS is off"
    if not Path(state.config.iso_path).is_file():
        return "LIVE_TEST_ISO_PATH names no readable file"
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
    print("\n=== ST72: provision installer boot (issue #1849) ===")
    run = lpar_power.Run(state.config.system_name, uuid.uuid4().hex[:8])
    ready = await _preconditions(client, state, run)
    if isinstance(ready, str):
        state.skip(SUBTASK, GROUP, ready)
        return
    why = _start_iso_server(state, SUBTASK)
    if why is not None:
        state.skip(SUBTASK, GROUP, why)
        return
    media_gone = False
    try:
        await _boot(client, state, run, Path(state.config.iso_path))
    finally:
        unmapped, media_gone = await _teardown_media(client, state, run)
        if unmapped:
            await lpar_power._teardown(client, state, run)
        else:
            lpar_power._manual(
                state,
                "run medium teardown",
                f"partition {run.p_name!r}, its volume and medium were kept: the medium "
                "could not be shown unmapped. Unmount it with hmc_unmount_optical_media, "
                "then run live_test_recovery.py for the remaining commands.",
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
                "mount-accepted",
                "saved-profile-optical-slot",
                "revalidated-before-power-on",
                "installer-powered-on",
                "activated-from-saved-profile",
                "optical-adapter-after-activation",
                "optical-mapping-after-activation",
                "refcodes-observed-without-boot-started-claim",
            )
        ],
        cleanup=cleanup,
        data=run.data,
    )
