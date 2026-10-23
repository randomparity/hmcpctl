"""ST51: inactive vTPM creation, acknowledged disable and explicit enable."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.vtpm import read_vtpm_capabilities

from .lpar_config import (
    NAME_PREFIX,
    RESOURCES,
    TOKEN_PREFIX,
    _compare,
    _read_pools,
    _Run,
    _teardown,
    scratch_partitions,
)
from .observation import Assertion, plain_data

if TYPE_CHECKING:
    from fastmcp import Client

    from .runner import RunState

GROUP = "lpar-vtpm"
SUBTASK = 51
SCENARIO = "st51-lpar-vtpm"


async def exercise_lpar_vtpm(client: Client, state: RunState) -> None:
    """One owned inactive fixture, two updates, no activation/profile/key effects."""
    if state.group != GROUP or state.hmc_config is None:
        state.skip(SUBTASK, GROUP, "requires opt-in group and HMC configuration")
        return
    try:
        async with HMCClient(state.hmc_config) as hmc:
            capabilities = await read_vtpm_capabilities(hmc, state.config.system_name)
    except (HMCError, ValueError) as error:
        state.skip(SUBTASK, GROUP, f"capability advertisement unavailable: {error}")
        return
    settings: dict[str, bool | str] = {"vtpm_enabled": True}
    if "1.2" in capabilities.versions:
        settings["vtpm_version"] = "1.2"
    elif (
        "2.0" in capabilities.versions
        and capabilities.version20_capable
        and capabilities.encryptions
    ):
        settings.update(vtpm_version="2.0", vtpm_encryption=capabilities.encryptions[0])
    else:
        state.skip(SUBTASK, GROUP, "no supported explicit advertised test variant")
        return
    baseline = await _read_pools(client, state, state.config.system_name)
    if baseline is None or scratch_partitions(baseline[0].names):
        state.skip(
            SUBTASK, GROUP, "baseline unavailable or stranded scratch; run recovery"
        )
        return
    suffix = uuid.uuid4().hex[:8]
    run = _Run(
        state.config.system_name,
        NAME_PREFIX + suffix,
        TOKEN_PREFIX + suffix,
        baseline[1],
        subtask=SUBTASK,
    )
    created_verified = False
    updates_verified = False
    read_verified = False
    try:
        run.create_attempted = True
        status, data = await state.call(
            client,
            "hmc_create_lpar",
            system_name_or_uuid=run.system,
            name=run.name,
            partition_type="AIX/Linux",
            resources=RESOURCES,
            caller_token=run.token,
            vtpm_settings=settings,
            apply_partition_profile=False,
        )
        data = plain_data(data)
        if status != "PASS" or not isinstance(data, dict):
            raise ValueError(
                f"vTPM creation did not return a structured success ({status}): {data!r}"
            )
        lpar = data.get("lpar")
        run.uuid = lpar.get("UUID") if isinstance(lpar, dict) else None
        run.created = bool(run.uuid)
        steps = data.get("steps")
        if not isinstance(steps, list):
            raise TypeError("vTPM creation returned malformed workflow steps")
        created_verified = bool(
            data.get("workflow_completed") is True
            and data.get("ownership_stamped") is True
            and run.created
            and any(
                isinstance(s, dict)
                and s.get("step") == "vtpm"
                and s.get("status") == "ok"
                for s in steps
            )
        )
        if not created_verified:
            raise ValueError(
                "vTPM creation identity, ownership or readback was not confirmed"
            )
        status, observed = await state.call(
            client,
            "hmc_get_lpar_vtpm_settings",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=run.uuid,
        )
        observed = plain_data(observed)
        actual = observed.get("settings") if isinstance(observed, dict) else None
        if (
            status != "PASS"
            or not isinstance(actual, dict)
            or observed.get("state") != "Not Activated"
            or any(actual.get(key) != value for key, value in settings.items())
        ):
            raise ValueError(
                "Independent typed vTPM read did not confirm inactive creation settings"
            )
        read_verified = True
        for patch, consent in [({"vtpm_enabled": False}, True), (settings, False)]:
            status, result = await state.call(
                client,
                "hmc_configure_lpar_vtpm",
                system_name_or_uuid=run.system,
                lpar_name_or_uuid=run.uuid,
                settings=patch,
                acknowledge_data_loss=consent,
            )
            result = plain_data(result)
            if (
                status != "PASS"
                or not isinstance(result, dict)
                or result.get("status") != "verified"
            ):
                raise ValueError(f"vTPM update readback was not verified: {result!r}")
        updates_verified = True
    except (HMCError, ValueError, TypeError, PermissionError) as error:
        state.record(SUBTASK, GROUP, "FAIL", str(error))
    finally:
        await _teardown(client, state, run, inactive_only=True)
    same = await _compare(client, state, run, baseline[0])
    cleanup = "passed" if run.deleted and same else "failed"
    state.record_verified(
        SUBTASK,
        "hmc_create_lpar",
        operation="lpar.create",
        scenario=SCENARIO,
        assertions=[Assertion("explicit-vtpm-creation-readback", created_verified)],
        cleanup=cleanup,
        data={"variant": settings, "vios": "not-run", "activation": "not-run"},
    )
    state.record_verified(
        SUBTASK,
        "hmc_configure_lpar_vtpm",
        operation="lpar.configure_vtpm",
        scenario=SCENARIO,
        assertions=[
            Assertion("acknowledged-disable-explicit-enable-readback", updates_verified)
        ],
        cleanup=cleanup,
        data={"variant": settings, "guest-tpm-objects": "not-run"},
    )
    state.record_verified(
        SUBTASK,
        "hmc_get_lpar_vtpm_settings",
        operation="lpar.get_vtpm_settings",
        scenario=SCENARIO,
        assertions=[Assertion("independent-inactive-vtpm-readback", read_verified)],
        cleanup=cleanup,
        data={"variant": settings},
    )
