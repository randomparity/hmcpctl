"""Persist a partition's current configuration into a named profile by force-save.

A REST adapter or mapping write lands in the current configuration only, and a profile
activation drops an adapter the profile lacks (``profile_sync`` module note). This leg
overwrites the named profile with the current configuration through #637's
``save_current_partition_profile`` (ADR 0242: a verified ``hmcprec-`` recovery copy is
kept first). It is journaled as ``lpar.profile_save`` (ADR 0322 Decision 3) with target
``lpar:<partition uuid>/profile-name:<name>``; logical tools reuse it for any change
that adds a client adapter.

The helper can misread a landed save (#1867), so its ``completed``/``verified`` decides
first and otherwise a readback does: the profile must lack no current client adapter.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Collection
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import TLS_IDENTITY_FAILURE, HMCError, HMCTLSFingerprintError
from hmcpctl.operations.logical import engine
from hmcpctl.operations.logical.engine import (
    Classification,
    EffectNotApplied,
    OperationContext,
)
from hmcpctl.operations.logical.models import EffectRecord
from hmcpctl.operations.lpar.profiles.core import (
    read_partition_profile,
    save_current_partition_profile,
)
from hmcpctl.operations.lpar.profiles.sync import (
    PROFILE_CHECKED_ADAPTER_TYPES,
    _profile_slots,
    adapters_missing_from_profile,
)
from hmcpctl.xmlutil import leaf_text

# Persisted contract (ADR 0195 Consequences).
KIND = "lpar.profile_save"
RECOVERY_PREFIX = "hmcprec-"
# A submitted save whose job status the helper misread (#1867) may land after it
# returns, so the readback repeats for a bounded time; the identity records which read
# confirmed it, to tell that cause from an immediate one.
SETTLE_SECONDS = 120
POLL_SECONDS = 5
# Only HMC message codes leave an exception's text: a client path can fall back to a
# raw body slice, and a body can echo the session cookie.
_HMC_CODE = re.compile(r"\b(?:REST\d{4}|HSCL[0-9A-F]{4})\b")


class SaveUnconfirmed(HMCError):
    """A save the readback could not confirm; its text holds no raw HMC body."""


def target(lpar_uuid: str, profile_name: str) -> str:
    return f"lpar:{lpar_uuid}/profile-name:{profile_name}"


def parse_target(text: str) -> tuple[str, str]:
    """The (partition UUID, profile name) a ``lpar.profile_save`` target names."""
    lpar, _, profile = text.partition("/")
    lpar_prefix, _, lpar_uuid = lpar.partition(":")
    profile_prefix, _, name = profile.partition(":")
    if (lpar_prefix, profile_prefix) != ("lpar", "profile-name") or not (
        lpar_uuid and name
    ):
        raise ValueError(f"profile-save target {text!r} is malformed")
    return lpar_uuid, name


def _name(entry: dict[str, Any]) -> str:
    value = leaf_text((entry.get("Resource") or {}).get("ProfileName"))
    return value.strip() if isinstance(value, str) else ""


async def _held_slots(hmc: HMCClient, lpar_uuid: str) -> set[str]:
    """The virtual slots the partition's current client adapters hold."""
    held = set()
    for adapter_type in PROFILE_CHECKED_ADAPTER_TYPES:
        for adapter in await hmc.list_child(
            "LogicalPartition", lpar_uuid, adapter_type
        ):
            slot = leaf_text((adapter.get("Resource") or {}).get("VirtualSlotNumber"))
            if isinstance(slot, str):
                held.add(slot.strip())
    return held


async def persisted(
    hmc: HMCClient,
    lpar_uuid: str,
    profile_name: str,
    *,
    removed_slots: Collection[str] = (),
    require_adapter: bool = True,
) -> tuple[list[str], list[str]]:
    """What *profile_name* still lacks or still holds, and the recovery copies kept.

    The profile lacks a current client adapter whose slot it does not list, and still
    holds a removed adapter whose slot in *removed_slots* it lists while no current
    client adapter does (ADR 0325 Decision 4). Under *require_adapter*, a profile listing
    no virtual adapter at all counts as lacking one: a caller that persisted an added
    adapter must not let an empty current-adapter read make an unsaved profile look
    complete.
    """
    feed = await hmc.list_child(
        "LogicalPartition", lpar_uuid, "LogicalPartitionProfile"
    )
    entries = [entry for entry in feed if isinstance(entry, dict)]
    found = [entry for entry in entries if _name(entry) == profile_name]
    if len(found) != 1:
        raise HMCError(
            f"LPAR {lpar_uuid} has no profile, or several, of the saved name"
        )
    recovery = sorted(
        name for entry in entries if (name := _name(entry)).startswith(RECOVERY_PREFIX)
    )
    saved = _profile_slots(found[0])
    missing = await adapters_missing_from_profile(hmc, lpar_uuid, found[0])
    if removed_slots:
        held = await _held_slots(hmc, lpar_uuid)
        missing += [
            f"removed adapter in virtual slot {slot} still in the profile"
            for slot in removed_slots
            if slot in saved and slot not in held
        ]
    if require_adapter and not saved:
        missing = missing or ["any virtual adapter"]
    return missing, recovery


def _copies(recovery: list[str]) -> str:
    return ", ".join(recovery) if recovery else "none"


async def save_current_over(
    hmc: HMCClient,
    system_uuid: str,
    lpar_uuid: str,
    profile_name: str,
    *,
    exclusive_writer_window: bool,
    removed_slots: Collection[str] = (),
    require_adapter: bool = True,
) -> dict[str, Any]:
    """Force-save the current configuration over *profile_name*; the effect's write.

    Returns ``{profile_name, recovery_profiles}``. Raises ``EffectNotApplied`` when no
    save job was submitted, and ``SaveUnconfirmed`` otherwise when the save cannot be
    confirmed, so the classifier decides it on resume. Either names the step that
    failed and what it reported. *removed_slots* and *require_adapter* pass to the
    readback (``persisted``).
    """
    try:
        record = await read_partition_profile(hmc, system_uuid, lpar_uuid, profile_name)
    except HMCTLSFingerprintError:
        raise
    except (HMCError, ValueError, TypeError, LookupError) as exc:
        raise EffectNotApplied(
            f"the profile of LPAR {lpar_uuid} to save over could not be read "
            f"(step=list, {_said(exc)}); nothing was submitted"
        ) from exc
    outcome = failure = None
    try:
        outcome = await save_current_partition_profile(
            hmc,
            system_uuid,
            lpar_uuid,
            profile_name,
            overwrite=True,
            expected_etag=record.etag,
            exclusive_writer_window=exclusive_writer_window,
        )
    except HMCTLSFingerprintError:
        raise
    except Exception as exc:  # noqa: BLE001 - the readback below decides the outcome
        failure = exc
    if outcome is not None and TLS_IDENTITY_FAILURE in outcome.diagnostics:
        recovery = [outcome.recovery_profile.name] if outcome.recovery_profile else []
        raise SaveUnconfirmed(
            "TLS certificate identity verification failed; profile readback stopped "
            f"({_diagnosis(outcome, failure)}); known recovery profiles: {_copies(recovery)}. "
            "Reconcile profile and job state before retrying."
        )
    verified = outcome is not None and (
        outcome.execution,
        outcome.requested_change,
    ) == ("completed", "verified")
    submitted = outcome is None or outcome.job is not None
    deadline = time.monotonic() + (SETTLE_SECONDS if submitted else 0)
    reads = 0
    while True:
        reads += 1
        try:
            missing, recovery = await persisted(
                hmc,
                lpar_uuid,
                profile_name,
                removed_slots=removed_slots,
                require_adapter=require_adapter,
            )
        except HMCTLSFingerprintError:
            raise
        except (HMCError, ValueError, TypeError) as exc:
            if verified:
                return {"profile_name": profile_name, "recovery_profiles": []}
            raise SaveUnconfirmed(
                f"the force-save of LPAR {lpar_uuid}'s profile is unconfirmed "
                f"(step=readback, {_said(exc)}; {_diagnosis(outcome, failure)})"
            ) from exc
        if verified or not missing:
            return {
                "profile_name": profile_name,
                "recovery_profiles": recovery,
                "confirmed_by": _confirmed_by(verified, reads),
            }
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(POLL_SECONDS)
    detail = (
        f"{_diagnosis(outcome, failure)}; {len(missing)} adapter(s) missing after "
        f"{reads} read(s)"
    )
    copies = f"recovery profiles on the partition: {_copies(recovery)}"
    if outcome is not None and outcome.job is None:
        raise EffectNotApplied(
            f"the force-save of LPAR {lpar_uuid}'s profile was not submitted "
            f"({detail}); {copies}"
        ) from failure
    raise SaveUnconfirmed(
        f"the force-save of LPAR {lpar_uuid}'s profile is unconfirmed ({detail}); "
        f"{copies}"
    ) from failure


def _confirmed_by(verified: bool, reads: int) -> str:
    if verified:
        return "helper"
    return "first_read" if reads == 1 else "repoll"


def _codes(text: str) -> str:
    """The HMC message codes in *text*, deduplicated, at most five; no other text."""
    found = list(dict.fromkeys(_HMC_CODE.findall(text)))[:5]
    return ",".join(found) if found else "none"


def _said(exc: BaseException) -> str:
    """An exception's type, HTTP status and HMC message codes; never its text.

    The save helper's own pre-call refusals (``ValueError``/``TypeError``) are text
    hmcpctl writes and are shown; every other exception may hold an HMC body.
    """
    name = type(exc).__name__
    if isinstance(exc, (ValueError, TypeError)) and not isinstance(exc, HMCError):
        return f"{name}: {str(exc)[:200]}"
    status = getattr(exc, "status_code", None)
    shown = f", HTTP {status}" if status is not None else ""
    return f"{name}{shown}, codes={_codes(str(exc))}"


def _diagnosis(outcome: Any, failure: Exception | None) -> str:
    """What the save helper reported: its outcome fields, or what it raised."""
    if outcome is None:
        return (
            f"step=save, the helper raised {_said(failure) if failure else 'nothing'}"
        )
    # The helper keeps ``str(HMCError)``, so only the codes in it are shown.
    error = f", error_codes={_codes(outcome.error)}" if outcome.error else ""
    return (
        f"step=save, execution={outcome.execution}, "
        f"requested_change={outcome.requested_change}, "
        f"job={'submitted' if outcome.job is not None else 'none'}, "
        f"recovery_verified={outcome.recovery_verified}{error}"
    )


ReadbackOptions = Callable[[OperationContext], tuple[Collection[str], bool]]


def profile_save_classifier(
    open_client: Callable[[str, str], HMCClient],
    options: ReadbackOptions = lambda _ctx: ((), True),
) -> engine.Classifier:
    """Classify an open save by the readback the write itself uses.

    *options* gives the readback's ``(removed_slots, require_adapter)`` for the
    operation, as its write passed them.
    """

    async def classify(ctx: OperationContext, effect: EffectRecord) -> Classification:
        lpar_uuid, name = parse_target(effect.target)
        removed, required = options(ctx)
        async with open_client(ctx.connection, ctx.host) as hmc:
            missing, recovery = await persisted(
                hmc,
                lpar_uuid,
                name,
                removed_slots=removed,
                require_adapter=required,
            )
        if missing:
            return Classification("not_applied")
        return Classification(
            "applied", {"profile_name": name, "recovery_profiles": recovery}
        )

    return classify
