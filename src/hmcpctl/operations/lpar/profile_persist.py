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

from collections.abc import Callable
from typing import Any

from ...client.core import HMCClient
from ...errors import HMCError
from ...xmlutil import find_text, leaf_text
from ..logical import engine
from ..logical.engine import Classification, EffectNotApplied, OperationContext
from ..logical.models import EffectRecord
from .profile_sync import _profile_slots, adapters_missing_from_profile
from .profiles import read_partition_profile, save_current_partition_profile

# Persisted contract (ADR 0195 Consequences).
KIND = "lpar.profile_save"
RECOVERY_PREFIX = "hmcprec-"


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


async def persisted(
    hmc: HMCClient, lpar_uuid: str, profile_name: str
) -> tuple[list[str], list[str]]:
    """The current client adapters *profile_name* lacks, and the recovery copies kept.

    A profile listing no virtual adapter at all counts as lacking one: every caller
    persists at least one added adapter, so an empty current-adapter read must not make
    an unsaved profile look complete.
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
    missing = await adapters_missing_from_profile(hmc, lpar_uuid, found[0])
    if not _profile_slots(found[0]):
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
) -> dict[str, Any]:
    """Force-save the current configuration over *profile_name*; the effect's write.

    Returns ``{profile_name, recovery_profiles}``. Raises ``EffectNotApplied`` when no
    save job was submitted, and ``SaveUnconfirmed`` otherwise when the save cannot be
    confirmed, so the classifier decides it on resume. Either names the step that
    failed and what it reported.
    """
    try:
        record = await read_partition_profile(hmc, system_uuid, lpar_uuid, profile_name)
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
    except Exception as exc:  # noqa: BLE001 - the readback below decides the outcome
        failure = exc
    verified = outcome is not None and (
        outcome.execution,
        outcome.requested_change,
    ) == ("completed", "verified")
    try:
        missing, recovery = await persisted(hmc, lpar_uuid, profile_name)
    except (HMCError, ValueError, TypeError) as exc:
        if verified:
            return {"profile_name": profile_name, "recovery_profiles": []}
        raise SaveUnconfirmed(
            f"the force-save of LPAR {lpar_uuid}'s profile is unconfirmed "
            f"(step=readback, {_said(exc)}; {_diagnosis(outcome, failure)})"
        ) from exc
    if verified or not missing:
        return {"profile_name": profile_name, "recovery_profiles": recovery}
    detail = f"{_diagnosis(outcome, failure)}; {len(missing)} adapter(s) missing"
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


def _said(exc: BaseException) -> str:
    """An exception's type, status and message, never a raw HMC body.

    An ``HMCError`` built from a response keeps that body; only its ``<Message>`` is
    shown (a body can echo request headers). A message hmcpctl wrote itself is shown
    as it is. Any other exception is named by type only.
    """
    name = type(exc).__name__
    if isinstance(exc, HMCError):
        status = f", HTTP {exc.status_code}" if exc.status_code is not None else ""
        if exc.body:
            try:
                message = find_text(exc.body, "Message") or "no <Message>"
            except Exception:  # noqa: BLE001 - an unparsable body shows no text
                message = "no <Message>"
        else:
            message = str(exc.args[0]) if exc.args else ""
        return f"{name}{status}: {message[:200]}"
    if isinstance(exc, (ValueError, TypeError, LookupError)):
        return f"{name}: {str(exc)[:200]}"
    return name


def _diagnosis(outcome: Any, failure: Exception | None) -> str:
    """What the save helper reported: its outcome fields, or what it raised."""
    if outcome is None:
        return (
            f"step=save, the helper raised {_said(failure) if failure else 'nothing'}"
        )
    # The helper keeps ``str(HMCError)``, which falls back to a raw body slice when the
    # response had no <Message>; markup there is withheld, as a body can echo headers.
    text = outcome.error or ""
    shown = "<body withheld>" if "<" in text else text[:200]
    error = f", error={shown}" if text else ""
    return (
        f"step=save, execution={outcome.execution}, "
        f"requested_change={outcome.requested_change}, "
        f"job={'submitted' if outcome.job is not None else 'none'}, "
        f"recovery_verified={outcome.recovery_verified}{error}"
    )


def profile_save_classifier(
    open_client: Callable[[str], HMCClient],
) -> engine.Classifier:
    """Classify an open save by the readback the write itself uses."""

    async def classify(ctx: OperationContext, effect: EffectRecord) -> Classification:
        lpar_uuid, name = parse_target(effect.target)
        async with open_client(ctx.connection) as hmc:
            missing, recovery = await persisted(hmc, lpar_uuid, name)
        if missing:
            return Classification("not_applied")
        return Classification(
            "applied", {"profile_name": name, "recovery_profiles": recovery}
        )

    return classify
