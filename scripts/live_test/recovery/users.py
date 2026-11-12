"""Independent users recovery admission and read-only witnesses."""

from __future__ import annotations

from typing import Any

from live_test.arms.users import profile_rows, scratch_users
from live_test.recovery.common import Finding, StateUnreadable, recorded_calls
from live_test.results import entries

#: The users arm (#632). Its scratch users are found by their reserved prefix,
#: not by the document's name, so a run killed before it wrote its document, or
#: overwritten by a later run's, still has its user reported.
USERS_SUBTASK = 11


def users_witnessed(document: dict[str, Any], subtasks: list[int]) -> bool:
    """Whether to look for users-arm scratch users.

    Raises `ValueError` for a document written before #632: its ST11 created a
    configured user the prefix scan cannot recognise, so it cannot be witnessed.
    """
    if USERS_SUBTASK not in subtasks:
        return False
    artifacts = document.get("artifacts")
    if not (
        isinstance(artifacts, dict) and "test_user_name" in artifacts
    ) and recorded_calls(document, {USERS_SUBTASK}, {"hmc_create_user"}):
        raise ValueError(
            "this document's ST11 predates the users arm (#632) and created a "
            "configured user the check cannot recognise; confirm by hand that "
            "it is gone"
        )
    return True


async def check_users(call) -> list[Finding]:
    """One finding per HMC user carrying the users arm's reserved prefix."""
    status, console = await call("hmc_get_console_info")
    console_uuid = (
        console.get("uuid") or console.get("UUID")
        if status == "PASS" and isinstance(console, dict)
        else None
    )
    if not isinstance(console_uuid, str):
        raise StateUnreadable("the console UUID, to list HMC users")
    status, listing = await call("hmc_list_users", console_uuid=console_uuid)
    rows = profile_rows(listing) if status == "PASS" else {}
    if status != "PASS" or len(rows) != len(entries(listing)):
        raise StateUnreadable("the HMC user list")
    return [
        Finding(
            what=f"HMC user {name}",
            detail="a users-arm scratch user is still defined (viewer role)",
            remedy=f"rmhmcusr -u {name}",
        )
        for name in scratch_users(rows)
    ]
