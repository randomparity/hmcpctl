"""The shared ``lpar.profile_write`` effect kind and its resume classifier (ADR 0314).

A logical tool journals one saved-profile write under this kind, targeting
``lpar:<partition uuid>/profile:<profile uuid>``. Resume reads the profile back: the
expected values mean the write landed, the recorded ``before`` values mean it did not,
and anything else needs a human. Each tool supplies its own reader of those two
patches, so provision (#1225) reuses the kind with its own request shape.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ...client.client_profiles import read_native_profile
from ...client.core import HMCClient
from ...documents.partition_profile import (
    ProfilePatch,
    apply_profile_patch,
    requested_profile_values,
)
from ..logical import engine
from ..logical.engine import Classification, OperationContext
from ..logical.models import EffectRecord

if TYPE_CHECKING:
    import xml.etree.ElementTree as ET  # nosec B405 - type name only

KIND = "lpar.profile_write"
Values = Callable[[OperationContext, EffectRecord], tuple[ProfilePatch, ProfilePatch]]
"""Return the (expected, before) patches for an open ``lpar.profile_write`` effect."""


def target(lpar_uuid: str, profile_uuid: str) -> str:
    return f"lpar:{lpar_uuid}/profile:{profile_uuid}"


def parse_target(text: str) -> tuple[str, str]:
    """The (partition UUID, profile UUID) a profile-write target names."""
    lpar, _, profile = text.partition("/")
    lpar_prefix, _, lpar_uuid = lpar.partition(":")
    profile_prefix, _, profile_uuid = profile.partition(":")
    if (lpar_prefix, profile_prefix) != ("lpar", "profile") or not (
        lpar_uuid and profile_uuid
    ):
        raise ValueError(f"profile-write target {text!r} is malformed")
    return lpar_uuid, profile_uuid


def _holds(element: ET.Element, patch: ProfilePatch) -> bool:
    """Whether every field *patch* sets already reads its value in *element*."""
    patched = apply_profile_patch(element, patch)
    return requested_profile_values(element, patch) == requested_profile_values(
        patched, patch
    )


def profile_write_classifier(
    values: Values,
    open_client: Callable[[str], HMCClient],
) -> engine.Classifier:
    """Classify an open profile write by reading the profile back (ADR 0314 Decision 5)."""

    async def classify(ctx: OperationContext, effect: EffectRecord) -> Classification:
        if ctx.partition is None:
            raise ValueError("a profile write needs the operation's guarded partition")
        lpar_uuid, profile_uuid = parse_target(effect.target)
        expected, before = values(ctx, effect)
        async with open_client(ctx.connection) as hmc:
            profile = await read_native_profile(
                hmc, "LogicalPartition", lpar_uuid, profile_uuid, ctx.partition[0]
            )
        if _holds(profile.element, expected):
            return Classification(
                "applied", {"profile_uuid": profile_uuid, "after_etag": profile.etag}
            )
        if _holds(profile.element, before):
            return Classification("not_applied")
        return Classification(
            "needs_attention",
            reason=(
                f"profile {profile_uuid} holds neither the requested nor the earlier "
                "values; read it with hmc_list_partition_profiles, then abandon"
            ),
        )

    return classify
