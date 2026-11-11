"""Prepare recorded continuation arguments before delegated authorization."""

from collections.abc import Mapping
from typing import Any

from ...operations.logical.store import OperationRefused


def continuation_arguments(
    recorded: Mapping[str, Any] | None,
    given: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """Require the scoped record and overlay supplied authorization arguments."""
    if recorded is None:
        # Refuse before authorization: submit must not admit a concurrently created
        # request whose recorded targets this call has never authorized.
        raise OperationRefused(
            "not_found",
            f"no operation has request_id {request_id}; omit continuation to start one",
        )
    return {**recorded, **given}
