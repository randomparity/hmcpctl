"""ST74: one IBM i create declaring a vSCSI client record, comparison and cleanup.

The client names the first VIOS of the selected system and is tagged as the load
source and alternate restart device (ADR 0382). Creation writes no VIOS server
adapter or mapping (ADR 0376), so the arm asserts the saved-profile record and
tagged-role comparison and that the result reports pairing as unproven.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hmcpctl.errors import HMCError
from hmcpctl.xmlutil import leaf_text

from ..observation import Assertion
from ..results import entries
from ..results import resource as get_resource
from .lpar_config import (
    NAME_PREFIX,
    RESOURCES,
    TOKEN_PREFIX,
    _compare,
    _prepare_scratch,
    _teardown,
)

if TYPE_CHECKING:
    from fastmcp import Client

    from ..runner import RunState

GROUP = "lpar-vscsi-client"
SUBTASK = 74
SCENARIO = "st74-lpar-vscsi-client"
MAX_VIRTUAL_SLOTS = 10
LOCAL_SLOT = 2


def _first_vios(data: object) -> tuple[int, int]:
    """Return the first listed VIOS native ID and its highest usable virtual slot."""
    for entry in entries(data):
        resource = get_resource(entry)
        native = leaf_text(resource.get("PartitionID"))
        io = resource.get("PartitionIOConfiguration")
        maximum = (
            leaf_text(io.get("MaximumVirtualIOSlots")) if isinstance(io, dict) else None
        )
        if (
            isinstance(native, str)
            and native.isdecimal()
            and isinstance(maximum, str)
            and maximum.isdecimal()
            and int(maximum) > 2
        ):
            return int(native), int(maximum) - 1
    raise ValueError("no VIOS with a readable partition ID and slot maximum")


ROLES = ["load_source_slot", "alt_restart_device_slot"]


def _step(steps: object, name: str) -> dict[str, object]:
    if not isinstance(steps, list):
        raise TypeError("vSCSI creation returned malformed workflow steps")
    for step in steps:
        if isinstance(step, dict) and step.get("step") == name:
            return step
    raise ValueError(f"vSCSI creation returned no {name} step")


async def exercise_lpar_vscsi_client(client: Client, state: RunState) -> None:
    """No activation, VIOS mutation, mapping or disk; inactive teardown only."""
    if state.group != GROUP or state.hmc_config is None:
        state.skip(SUBTASK, GROUP, "requires opt-in group and HMC configuration")
        return
    prepared = await _prepare_scratch(
        client,
        state,
        name_prefix=NAME_PREFIX,
        token_prefix=TOKEN_PREFIX,
        subtask=SUBTASK,
        group=GROUP,
    )
    if prepared is None:
        return
    run, baseline = prepared
    verified = False
    apply_status = "not-run"
    try:
        status, listing = await state.call(
            client, "hmc_list_vios", system_name_or_uuid=run.system
        )
        if status != "PASS":
            raise ValueError(f"VIOS listing unavailable ({status})")
        vios_id, remote_slot = _first_vios(listing)
        run.create_attempted = True
        status, data = await state.call(
            client,
            "hmc_create_lpar",
            system_name_or_uuid=run.system,
            name=run.name,
            partition_type="OS400",
            resources=RESOURCES,
            caller_token=run.token,
            physical_io={
                "console_slot": "hmc",
                "load_source_slot": str(LOCAL_SLOT),
                "alt_restart_device_slot": str(LOCAL_SLOT),
            },
            max_virtual_slots=MAX_VIRTUAL_SLOTS,
            virtual_scsi_adapters=[
                {
                    "local_slot": LOCAL_SLOT,
                    "remote_slot": remote_slot,
                    "is_required": False,
                    "remote_lpar_id": vios_id,
                }
            ],
            apply_partition_profile=True,
        )
        if not isinstance(data, dict):
            raise TypeError(f"vSCSI creation returned no structured result ({status})")
        lpar = data.get("lpar")
        run.uuid = lpar.get("UUID") if isinstance(lpar, dict) else None
        run.created = bool(run.uuid)
        step = _step(data.get("steps"), "virtual_scsi_adapters")
        io_step = _step(data.get("steps"), "physical_io")
        apply = next(
            (
                s
                for s in data.get("steps", [])
                if isinstance(s, dict) and s.get("step") == "apply_profile"
            ),
            None,
        )
        apply_status = str(apply.get("status")) if apply else "absent"
        result = step.get("result")
        verified = (
            status == "PASS"
            and run.created
            and data.get("ownership_stamped") is True
            and step.get("status") == "ok"
            and isinstance(result, dict)
            and result.get("pairing") == "unproven"
            and result.get("vios_server_adapters") == "not-created"
            and result.get("vios_mappings") == "not-created"
            and result.get("records")
            == [{"slot": LOCAL_SLOT, "status": "verified", "roles": ROLES}]
            and io_step.get("status") == "ok"
        )
        if not verified:
            raise ValueError(f"vSCSI client record comparison not confirmed: {data!r}")
        state.record(SUBTASK, "vSCSI client creation", "PASS", data)
    except (HMCError, ValueError, TypeError, PermissionError) as error:
        state.record(SUBTASK, GROUP, "FAIL", str(error))
    finally:
        await _teardown(client, state, run, inactive_only=True)
    same = await _compare(client, state, run, baseline)
    state.record_verified(
        SUBTASK,
        "hmc_create_lpar",
        operation="lpar.create",
        scenario=SCENARIO,
        assertions=[Assertion("vscsi-client-saved-profile-comparison", verified)],
        cleanup="passed" if run.deleted and same else "failed",
        data={
            "apply_profile": apply_status,
            "vios_mutation": "not-run",
            "activation": "not-run",
            "pairing": "unproven",
        },
    )
