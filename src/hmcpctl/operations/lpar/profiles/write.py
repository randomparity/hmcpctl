"""The shared ``lpar.profile_write`` effect kind and its resume classifier (ADR 0314).

A logical tool journals one saved-profile write under this kind, targeting
``lpar:<partition uuid>/profile:<profile uuid>``. Resume reads the profile back: the
expected values mean the write landed, the recorded ``before`` values mean it did not,
and anything else needs a human. Each tool supplies its own reader of those two
patches, so provision (#1225) reuses the kind with its own request shape.

A write that creates a profile has no UUID yet, so it targets
``lpar:<partition uuid>/profile-name:<name>`` instead (ADR 0313 Decision 12, with recovery revised by ADR 0346): resume
reads the profile by name; absence stays uncertain because a submitted job may land later.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from ....client.client_profiles import NativeProfile, read_native_profile
from ....client.core import HMCClient
from ....documents.partition_profile import (
    ProfilePatch,
    apply_profile_patch,
    requested_profile_values,
)
from ...logical import engine
from ...logical.engine import Classification, OperationContext
from ...logical.models import EffectRecord

if TYPE_CHECKING:
    import xml.etree.ElementTree as ET  # nosec B405 - type name only

KIND = "lpar.profile_write"
Values = Callable[[OperationContext, EffectRecord], tuple[ProfilePatch, ProfilePatch]]
"""Return the (expected, before) patches for an open ``lpar.profile_write`` effect."""


def target(lpar_uuid: str, profile_uuid: str) -> str:
    return f"lpar:{lpar_uuid}/profile:{profile_uuid}"


def target_by_name(lpar_uuid: str, profile_name: str) -> str:
    """The target of a write that creates profile *profile_name* (ADR 0313)."""
    return f"lpar:{lpar_uuid}/profile-name:{profile_name}"


def _split(text: str) -> tuple[str, str, str]:
    lpar, _, profile = text.partition("/")
    lpar_prefix, _, lpar_uuid = lpar.partition(":")
    profile_prefix, _, value = profile.partition(":")
    if lpar_prefix != "lpar" or profile_prefix not in ("profile", "profile-name"):
        raise ValueError(f"profile-write target {text!r} is malformed")
    if not (lpar_uuid and value):
        raise ValueError(f"profile-write target {text!r} is malformed")
    return lpar_uuid, profile_prefix, value


def parse_target(text: str) -> tuple[str, str]:
    """The (partition UUID, profile UUID) a UUID-form profile-write target names."""
    lpar_uuid, form, profile_uuid = _split(text)
    if form != "profile":
        raise ValueError(f"profile-write target {text!r} is malformed")
    return lpar_uuid, profile_uuid


async def profile_by_name(
    hmc: HMCClient, lpar_uuid: str, system_uuid: str, name: str
) -> NativeProfile | None:
    """The partition's saved profile called *name*, or None when it has none."""
    feed = await hmc.list_child(
        "LogicalPartition", lpar_uuid, "LogicalPartitionProfile"
    )
    found = []
    for entry in feed if isinstance(feed, list) else []:
        uuid = entry.get("UUID")
        if not isinstance(uuid, str) or not uuid:
            raise ValueError("the profile feed has an entry with no UUID")
        profile = await read_native_profile(
            hmc, "LogicalPartition", lpar_uuid, uuid, system_uuid
        )
        if profile.name == name:
            found.append(profile)
    if len(found) > 1:
        raise ValueError(f"more than one profile of partition {lpar_uuid} has the name")
    return found[0] if found else None


_NUMERIC_PATHS = ("Memory", "Processors", "ProcessingUnits")


def _value(path: str, text: str | None) -> Decimal | str | None:
    # The HMC renders whole processing units as "1" where the patch writes "1.0".
    if text is None or not path.endswith(_NUMERIC_PATHS):
        return text
    try:
        return Decimal(text)
    except InvalidOperation:
        return text


def holds(element: ET.Element, patch: ProfilePatch) -> bool:
    """Whether every field *patch* sets already reads its value in *element*."""
    try:
        patched = apply_profile_patch(element, patch)
        current = requested_profile_values(element, patch)
        wanted = requested_profile_values(patched, patch)
    except ValueError:
        # A profile that cannot take the requested values does not hold them.
        return False
    return {k: _value(k, v) for k, v in current.items()} == {
        k: _value(k, v) for k, v in wanted.items()
    }


async def classify_named_profile(
    hmc: HMCClient,
    lpar_uuid: str,
    system_uuid: str,
    name: str,
    expected: ProfilePatch,
) -> Classification:
    """Observe a name-addressed save without treating absence as submission proof."""
    profile = await profile_by_name(hmc, lpar_uuid, system_uuid, name)
    if profile is None:
        return Classification(
            "needs_attention",
            reason="the saved profile is not yet visible; reconcile the original save/job",
        )
    if holds(profile.element, expected):
        return Classification(
            "applied", {"profile_uuid": profile.uuid, "profile_name": name}
        )
    return Classification(
        "needs_attention",
        reason=(
            f"profile {profile.uuid} exists without the requested values; read it "
            "with hmc_list_partition_profiles, then abandon"
        ),
    )


def profile_write_classifier(
    values: Values,
    open_client: Callable[[str, str], HMCClient],
) -> engine.Classifier:
    """Classify an open profile write by reading the profile back (ADR 0314 Decision 5)."""

    async def classify(ctx: OperationContext, effect: EffectRecord) -> Classification:
        if ctx.partition is None:
            raise ValueError("a profile write needs the operation's guarded partition")
        lpar_uuid, form, value = _split(effect.target)
        expected, before = values(ctx, effect)
        if form == "profile-name":
            async with open_client(ctx.connection, ctx.host) as hmc:
                return await classify_named_profile(
                    hmc, lpar_uuid, ctx.partition[0], value, expected
                )
        profile_uuid = value
        async with open_client(ctx.connection, ctx.host) as hmc:
            profile = await read_native_profile(
                hmc, "LogicalPartition", lpar_uuid, profile_uuid, ctx.partition[0]
            )
        if holds(profile.element, expected):
            return Classification(
                "applied", {"profile_uuid": profile_uuid, "after_etag": profile.etag}
            )
        if holds(profile.element, before):
            return Classification("not_applied")
        return Classification(
            "needs_attention",
            reason=(
                f"profile {profile_uuid} holds neither the requested nor the earlier "
                "values; read it with hmc_list_partition_profiles, then abandon"
            ),
        )

    return classify
