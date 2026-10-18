"""User-administration scenarios for the live HMC test harness."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from hmcpctl.xmlutil import leaf_text

from .observation import Assertion, CallFailure
from .results import entries, resource

if TYPE_CHECKING:
    from live_test.runner import RunState

#: Every scratch user the users arm creates starts with this, and nothing else
#: may: recovery reports any listed user carrying it as stranded.
SCRATCH_PREFIX = "hmcpctl-live-"
#: The predefined read-only task role the scratch user is given.
VIEWER_TASK_ROLE = "hmcviewer"
#: The group whose dispatch is the operator's authorization to create a user.
USERS_GROUP = "users"

_DESCRIPTION = "hmcpctl live test user"
#: VerifySessionTimeout minutes the scratch user is created with, then modified to:
#: the reference's sample value, then a different one so the read-back tells them apart.
_CREATED_TIMEOUT = 15
_MODIFIED_TIMEOUT = 5


def is_empty(value: object) -> bool:
    """Whether a parsed field carries no value.

    An empty element with attributes parses to an attribute-only mapping, so a
    mapping without a ``text`` key is empty too.
    """
    return (
        value is None
        or value == ""
        or (isinstance(value, Mapping) and "text" not in value)
    )


def profile_rows(data: object) -> dict[str, str]:
    """Map each listed UserProfile's UUID to its UserID."""
    rows: dict[str, str] = {}
    for entry in entries(data):
        uuid = entry.get("UUID")
        user_id = leaf_text(resource(entry).get("UserID"))
        if isinstance(uuid, str) and isinstance(user_id, str):
            rows[uuid] = user_id
    return rows


def scratch_users(rows: Mapping[str, str]) -> list[str]:
    """The listed UserIDs that only the users arm may have created."""
    return sorted(name for name in rows.values() if name.startswith(SCRATCH_PREFIX))


def _passwords_hidden(data: object) -> bool:
    return all(
        is_empty(resource(entry).get("UserProfilePassword")) for entry in entries(data)
    )


def _field(data: object, name: str) -> object:
    return leaf_text(resource(data).get(name)) if isinstance(data, Mapping) else None


def _scrub(data: Any, password: str) -> Any:
    """Remove the minted password from anything about to be recorded.

    A refused create can echo its request body, password included, in the
    failure text the runner persists and prints.
    """
    if isinstance(data, CallFailure):
        return replace(data, message=data.message.replace(password, "<redacted>"))
    if isinstance(data, (dict, list, str)):
        text = json.dumps(data)
        if password in text:
            return json.loads(text.replace(password, "<redacted>"))
    return data


def _without_secrets(data: Any) -> Any:
    """Copy *data* with every password field's value replaced.

    The disclosure assertions read the raw value; what is recorded must not
    carry it, and the runner's redaction only sees ``name=value`` text.
    """
    if isinstance(data, Mapping):
        return {
            key: "<redacted>"
            if key in {"UserProfilePassword", "BindPassword"} and not is_empty(value)
            else _without_secrets(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_without_secrets(item) for item in data]
    return data


# ---------------------------------------------------------------------------
# ST11 — the users arm: role and remote-access reads, then one scratch user's
# create, read, modify, clear and delete (issue #632)
# ---------------------------------------------------------------------------

_LIFECYCLE_TOOLS = (
    "hmc_create_user",
    "hmc_get_user",
    "hmc_modify_user",
    "hmc_delete_user",
)


async def _read_only_checks(
    client: Client, state: RunState, console_uuid: str
) -> tuple[str, Any, list[str]]:
    """Record the four reads; return the before listing and the task-role names."""
    st, before = await state.call(client, "hmc_list_users", console_uuid=console_uuid)
    state.record_verified(
        11,
        "hmc_list_users",
        operation="user.list",
        scenario="st11-user-reads",
        assertions=[
            Assertion("profiles-listed", st == "PASS" and bool(entries(before))),
            Assertion(
                "profiles-carry-uuid-and-user-id",
                st == "PASS" and len(profile_rows(before)) == len(entries(before)),
            ),
            Assertion(
                "passwords-not-disclosed", st == "PASS" and _passwords_hidden(before)
            ),
        ],
        cleanup="not-required",
        data=_without_secrets(before),
    )

    rst, roles = await state.call(
        client, "hmc_list_task_roles", console_uuid=console_uuid
    )
    role_names = [
        name
        for entry in entries(roles)
        if isinstance(name := leaf_text(resource(entry).get("TaskRoleName")), str)
    ]
    state.record_verified(
        11,
        "hmc_list_task_roles",
        operation="task_role.list",
        scenario="st11-user-reads",
        assertions=[
            Assertion("task-roles-listed", rst == "PASS" and bool(role_names)),
            Assertion("viewer-role-present", VIEWER_TASK_ROLE in role_names),
        ],
        cleanup="not-required",
        data=roles,
    )

    rrst, resource_roles = await state.call(
        client, "hmc_list_resource_roles", console_uuid=console_uuid
    )
    listed = entries(resource_roles)
    named = all(
        isinstance(leaf_text(resource(entry).get("ResourceRoleName")), str)
        for entry in listed
    )
    state.record_verified(
        11,
        "hmc_list_resource_roles",
        operation="resource_role.list",
        scenario="st11-user-reads",
        assertions=[
            Assertion("resource-role-rows-named", rrst == "PASS" and named)
            if listed
            else Assertion("resource-roles-empty-branch", rrst == "PASS")
        ],
        cleanup="not-required",
        data=resource_roles,
    )

    ast_, remote = await state.call(
        client, "hmc_get_remote_access", console_uuid=console_uuid
    )
    fields = resource(remote) if isinstance(remote, Mapping) else {}
    ldap = fields.get("LdapConfiguration")
    state.record_verified(
        11,
        "hmc_get_remote_access",
        operation="remote_access.get",
        scenario="st11-user-reads",
        assertions=[
            Assertion(
                "remote-access-group-read",
                ast_ == "PASS"
                and (
                    "LdapConfiguration" in fields or "KerberosConfiguration" in fields
                ),
            ),
            Assertion(
                "bind-password-not-disclosed",
                ast_ == "PASS"
                and (
                    not isinstance(ldap, Mapping) or is_empty(ldap.get("BindPassword"))
                ),
            ),
        ],
        cleanup="not-required",
        data=_without_secrets(remote),
    )
    return st, before, role_names


@dataclass
class _Lifecycle:
    """What one scratch user's lifecycle returned, step by step."""

    name: str
    uuid: str | None = None
    create: tuple[str, Any] = ("SKIP", None)
    reads: list[tuple[str, Any]] = field(default_factory=list)
    modifies: list[tuple[str, Any]] = field(default_factory=list)
    delete: tuple[str, Any] = ("SKIP", "no user profile UUID resolved for the name")
    final: tuple[str, Any] = ("SKIP", None)
    echoed: bool = False

    def uuid_in(self, listing: object) -> str | None:
        rows = profile_rows(listing)
        return next((uuid for uuid, user in rows.items() if user == self.name), None)

    def read(self, index: int, name: str) -> object:
        if index >= len(self.reads) or self.reads[index][0] != "PASS":
            return None
        return _field(self.reads[index][1], name)

    def read_empty(self, index: int, name: str) -> bool:
        if index >= len(self.reads) or self.reads[index][0] != "PASS":
            return False
        data = self.reads[index][1]
        return isinstance(data, Mapping) and is_empty(resource(data).get(name))

    def modified(self, index: int) -> bool:
        return index < len(self.modifies) and self.modifies[index][0] == "PASS"


async def _lifecycle(
    client: Client,
    state: RunState,
    console_uuid: str,
    before: Mapping[str, str],
) -> None:
    """Create, read, modify, clear and delete one scratch user, then record it."""
    run = _Lifecycle(name=f"{SCRATCH_PREFIX}{secrets.token_hex(4)}")
    password = f"Aa1-{secrets.token_hex(8)}"
    # Written before the create so an interrupted run's document names the user.
    state.artifacts.test_user_name = run.name
    responses: list[Any] = []

    def kept(tool: str, result: tuple[str, Any]) -> tuple[str, Any]:
        responses.append(result[1])
        scrubbed = result[0], _scrub(_without_secrets(result[1]), password)
        if scrubbed[0] == "FAIL":
            # The verified rows below carry one step's data each; a refused step's
            # HMC error is recorded here so a failed observation can be diagnosed.
            state.record(11, f"{tool} (refused)", *scrubbed)
        return scrubbed

    try:
        run.create = kept(
            "hmc_create_user",
            await state.call(
                client,
                "hmc_create_user",
                console_uuid=console_uuid,
                user_id=run.name,
                password=password,
                associated_task_role=VIEWER_TASK_ROLE,
                description=_DESCRIPTION,
                verify_session_timeout=_CREATED_TIMEOUT,
                allow_web_remote_access=False,
                allow_ssh_remote_access=False,
            ),
        )
        # Listed whatever the create returned: a timed-out create may still exist.
        listed = kept(
            "hmc_list_users",
            await state.call(client, "hmc_list_users", console_uuid=console_uuid),
        )
        run.uuid = run.uuid_in(listed[1])
        for description, timeout in (
            (f"{_DESCRIPTION} (modified)", _MODIFIED_TIMEOUT),
            ("", None),
            (None, None),
        ):
            if run.uuid is None:
                break
            run.reads.append(
                kept(
                    "hmc_get_user",
                    await state.call(
                        client,
                        "hmc_get_user",
                        console_uuid=console_uuid,
                        user_profile_uuid=run.uuid,
                    ),
                )
            )
            if description is not None:
                run.modifies.append(
                    kept(
                        "hmc_modify_user",
                        await state.call(
                            client,
                            "hmc_modify_user",
                            console_uuid=console_uuid,
                            user_profile_uuid=run.uuid,
                            description=description,
                            verify_session_timeout=timeout,
                        ),
                    )
                )
    finally:
        await _delete_scratch_user(client, state, console_uuid, run, kept)
    run.echoed = any(password in json.dumps(item, default=str) for item in responses)
    _record_lifecycle(state, run, before)


async def _delete_scratch_user(
    client: Client,
    state: RunState,
    console_uuid: str,
    run: _Lifecycle,
    kept: Callable[[str, tuple[str, Any]], tuple[str, Any]],
) -> None:
    """Delete only this run's user, by the UUID listed for its minted name."""
    target = run.uuid
    if target is None:
        listing = kept(
            "hmc_list_users",
            await state.call(client, "hmc_list_users", console_uuid=console_uuid),
        )
        target = run.uuid_in(listing[1])
    if target is not None:
        run.delete = kept(
            "hmc_delete_user",
            await state.call(
                client,
                "hmc_delete_user",
                console_uuid=console_uuid,
                user_profile_uuid=target,
            ),
        )
    run.final = kept(
        "hmc_list_users",
        await state.call(client, "hmc_list_users", console_uuid=console_uuid),
    )


def _record_lifecycle(
    state: RunState, run: _Lifecycle, before: Mapping[str, str]
) -> None:
    after = profile_rows(run.final[1])
    listed = run.final[0] == "PASS"
    gone = listed and not scratch_users(after)
    untouched = listed and all(after.get(uuid) == user for uuid, user in before.items())
    cleanup = "passed" if gone and untouched else "failed"
    state.record_verified(
        11,
        "hmc_create_user",
        operation="user.create",
        scenario="st11-user-lifecycle",
        assertions=[
            Assertion("create-accepted", run.create[0] == "PASS"),
            Assertion("scratch-profile-listed", run.uuid is not None),
            Assertion("password-not-echoed", not run.echoed),
            Assertion(
                "verify-session-timeout-read-back",
                run.read(0, "VerifySessionTimeout") == str(_CREATED_TIMEOUT),
            ),
        ],
        cleanup=cleanup,
        data=run.create[1],
    )
    # An operation the run never called has no observation: a failed one would
    # read as the HMC refusing it. The create's cleanup already says whether a
    # user was left behind.
    if not run.reads:
        for tool in ("hmc_get_user", "hmc_modify_user"):
            state.skip(11, tool, "not reached: no user profile was listed")
    else:
        _record_reads_and_modifies(state, run, cleanup)
    if run.delete[0] == "SKIP":
        state.skip(11, "hmc_delete_user", "not reached: there was no user to delete")
        return
    state.record_verified(
        11,
        "hmc_delete_user",
        operation="user.delete",
        scenario="st11-user-lifecycle",
        assertions=[
            Assertion("scratch-profile-absent", run.delete[0] == "PASS" and gone),
            Assertion("pre-existing-profiles-unchanged", untouched),
        ],
        cleanup=cleanup,
        data=run.final[1],
    )


def _record_reads_and_modifies(state: RunState, run: _Lifecycle, cleanup: str) -> None:
    state.record_verified(
        11,
        "hmc_get_user",
        operation="user.get",
        scenario="st11-user-lifecycle",
        assertions=[
            Assertion("user-id-matches", run.read(0, "UserID") == run.name),
            Assertion(
                "task-role-is-viewer",
                run.read(0, "AssociatedTaskRole") == VIEWER_TASK_ROLE,
            ),
            Assertion(
                "password-not-disclosed", run.read_empty(0, "UserProfilePassword")
            ),
            Assertion("not-predefined", run.read(0, "IsPredefinedUser") == "false"),
            Assertion(
                "remote-access-disabled",
                run.read(0, "AllowWebRemoteAccess") == "false"
                and run.read(0, "AllowSSHRemoteAccess") == "false",
            ),
        ],
        cleanup=cleanup,
        data=run.reads[0][1] if run.reads else None,
    )
    state.record_verified(
        11,
        "hmc_modify_user",
        operation="user.modify",
        scenario="st11-user-lifecycle",
        assertions=[
            Assertion(
                "description-updated",
                run.modified(0)
                and run.read(1, "UserDescription") == f"{_DESCRIPTION} (modified)",
            ),
            Assertion(
                "verify-session-timeout-updated",
                run.modified(0)
                and run.read(1, "VerifySessionTimeout") == str(_MODIFIED_TIMEOUT),
            ),
            Assertion(
                "description-cleared",
                # V10R3 resets an empty description to this default (#1409).
                run.modified(1) and run.read(2, "UserDescription") == "HMC User",
            ),
            Assertion("user-id-unchanged", run.read(2, "UserID") == run.name),
            Assertion(
                "profile-uuid-unchanged",
                run.uuid is not None and run.read(2, "UserProfileUUID") == run.uuid,
            ),
            Assertion(
                "task-role-unchanged",
                run.read(2, "AssociatedTaskRole") == VIEWER_TASK_ROLE,
            ),
            Assertion(
                "remote-access-unchanged",
                run.read(2, "AllowWebRemoteAccess") == "false"
                and run.read(2, "AllowSSHRemoteAccess") == "false",
            ),
        ],
        cleanup=cleanup,
        data=run.modifies[-1][1] if run.modifies else None,
    )


async def exercise_users(client: Client, state: RunState) -> None:
    print("\n=== ST11: Users, roles and remote access ===")
    reads = (
        "hmc_list_users",
        "hmc_list_task_roles",
        "hmc_list_resource_roles",
        "hmc_get_remote_access",
    )
    if state.group != USERS_GROUP:
        for tool in reads + _LIFECYCLE_TOOLS:
            state.skip(11, tool, "creates an HMC user: runs only in the users arm")
        return

    # This run's console, never one restored from an earlier results document.
    st, console = await state.call(client, "hmc_get_console_info")
    state.record(11, "hmc_get_console_info", st, console)
    console_uuid = (
        console.get("uuid") or console.get("UUID")
        if st == "PASS" and isinstance(console, dict)
        else None
    )
    if not isinstance(console_uuid, str):
        for tool in reads + _LIFECYCLE_TOOLS:
            state.skip(11, tool, "no console UUID")
        return

    before_status, before, role_names = await _read_only_checks(
        client, state, console_uuid
    )
    rows = profile_rows(before)
    reason = None
    if before_status != "PASS" or not rows or len(rows) != len(entries(before)):
        # Without every UUID and UserID the arm could neither find its own user
        # to delete nor tell that every other user survived.
        reason = "the user listing failed or could not be read"
    elif VIEWER_TASK_ROLE not in role_names:
        reason = f"task role {VIEWER_TASK_ROLE} is not listed"
    elif scratch_users(rows):
        reason = "a scratch user from an earlier run remains: run live_test_recovery.py"
    if reason is not None:
        for tool in _LIFECYCLE_TOOLS:
            state.skip(11, tool, reason)
        return
    await _lifecycle(client, state, console_uuid, rows)


# ---------------------------------------------------------------------------
# ST6 — User Inventory
# ---------------------------------------------------------------------------


async def inventory_users(client: Client, state: RunState) -> None:
    print("\n=== ST6: User Inventory ===")
    artifacts = state.artifacts

    if not artifacts.console_uuid:
        state.skip(
            6, "hmc_list_users", "no console UUID captured (ST1 may have failed)"
        )
        return

    st, data = await state.call(
        client, "hmc_list_users", console_uuid=artifacts.console_uuid
    )
    state.record(6, "hmc_list_users", st, data)
