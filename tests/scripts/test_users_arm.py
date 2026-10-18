"""Behavioural tests for the users live arm, ST11 (issue #632)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import live_fixture

from hmcpctl.client.client_parse import _parse_feed

SCRIPTS_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
from live_test import (  # noqa: E402
    runner,
    users,
)
from live_test.observation import classify_failure  # noqa: E402

CONSOLE = "00000002-abcd-4ef0-8abc-000000000002"
#: A pre-existing user, parsed from the V10R3 capture so field shapes are real.
_CAPTURED = _parse_feed(live_fixture("rest-user-profile")["body"], "/x")[0]
_CONSOLE_ENTRY = _parse_feed(live_fixture("rest-management-console")["body"], "/x")[0]


def _profile(uuid: str, user_id: str, **fields: Any) -> dict[str, Any]:
    entry = copy.deepcopy(_CAPTURED)
    entry["UUID"] = uuid
    entry["Resource"]["UserID"]["text"] = user_id
    entry["Resource"]["UserProfileUUID"]["text"] = uuid
    for name, value in fields.items():
        entry["Resource"][name] = value
    return entry


class FakeHmc:
    """An in-memory UserProfile table answering the arm's tool calls."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {
            "uuid-1": _profile("uuid-1", "operator"),
            "uuid-2": _profile("uuid-2", "auditor"),
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, int] = {}
        self.roles = [{"Resource": {"TaskRoleName": {"text": "hmcviewer"}}}]

    async def call(self, _client, tool: str, **kwargs: Any):
        self.calls.append((tool, kwargs))
        if self.fail.get(tool):
            self.fail[tool] -= 1
            return "FAIL", classify_failure(RuntimeError(f"{tool} refused"))
        return "PASS", getattr(self, tool)(**kwargs)

    def hmc_get_console_info(self) -> dict[str, Any]:
        return {"uuid": CONSOLE}

    def hmc_list_users(self, console_uuid: str) -> list[dict[str, Any]]:
        assert console_uuid == CONSOLE
        return copy.deepcopy(list(self.users.values()))

    def hmc_list_task_roles(self, console_uuid: str) -> list[dict[str, Any]]:
        return self.roles

    def hmc_list_resource_roles(self, console_uuid: str) -> list[dict[str, Any]]:
        return []

    def hmc_get_remote_access(self, console_uuid: str) -> dict[str, Any]:
        return copy.deepcopy(_CONSOLE_ENTRY)

    def hmc_create_user(self, console_uuid: str, user_id: str, **fields: Any) -> None:
        self.users["uuid-new"] = _profile(
            "uuid-new",
            user_id,
            AssociatedTaskRole={"text": fields["associated_task_role"]},
            UserDescription={"text": fields["description"]},
            AllowWebRemoteAccess={
                "text": str(fields["allow_web_remote_access"]).lower()
            },
            AllowSSHRemoteAccess={
                "text": str(fields["allow_ssh_remote_access"]).lower()
            },
            VerifySessionTimeout={"text": str(fields["verify_session_timeout"])},
        )

    def hmc_get_user(self, console_uuid: str, user_profile_uuid: str) -> dict:
        return copy.deepcopy(self.users[user_profile_uuid])

    def hmc_modify_user(
        self,
        console_uuid: str,
        user_profile_uuid: str,
        description: str,
        verify_session_timeout: int | None = None,
    ) -> None:
        resource = self.users[user_profile_uuid]["Resource"]
        resource["UserDescription"] = {"text": description or "HMC User"}
        if verify_session_timeout is not None:
            resource["VerifySessionTimeout"] = {"text": str(verify_session_timeout)}

    def hmc_delete_user(self, console_uuid: str, user_profile_uuid: str) -> str:
        del self.users[user_profile_uuid]
        return "deleted"


@pytest.fixture
def hmc(monkeypatch) -> FakeHmc:
    fake = FakeHmc()
    monkeypatch.setattr(runner.RunState, "call", fake.call)
    return fake


def _state(group: str | None = "users") -> runner.RunState:
    return runner.RunState(group=group)


def _observations(state: runner.RunState) -> dict[str, dict[str, Any]]:
    return {item["operation"]: item["observation"] for item in state.observations}


@pytest.mark.asyncio
async def test_the_arm_skips_outside_its_own_group(hmc):
    state = _state("round2")

    await users.exercise_users(None, state)

    assert hmc.calls == []
    assert {row["status"] for row in state.results} == {"SKIP"}


@pytest.mark.asyncio
async def test_the_lifecycle_records_verified_postconditions(hmc):
    state = _state()

    await users.exercise_users(None, state)

    tools = [tool for tool, _ in hmc.calls]
    assert tools == [
        "hmc_get_console_info",
        "hmc_list_users",
        "hmc_list_task_roles",
        "hmc_list_resource_roles",
        "hmc_get_remote_access",
        "hmc_create_user",
        "hmc_list_users",
        "hmc_get_user",
        "hmc_modify_user",
        "hmc_get_user",
        "hmc_modify_user",
        "hmc_get_user",
        "hmc_delete_user",
        "hmc_list_users",
    ]
    create = hmc.calls[5][1]
    name = create["user_id"]
    assert name.startswith("hmcpctl-live-") and state.artifacts.test_user_name == name
    assert create["associated_task_role"] == "hmcviewer"
    assert create["allow_web_remote_access"] is False
    assert create["allow_ssh_remote_access"] is False
    assert create["verify_session_timeout"] == 15
    assert hmc.calls[12][1] == {
        "console_uuid": CONSOLE,
        "user_profile_uuid": "uuid-new",
    }
    modifies = [call[1] for call in hmc.calls if call[0] == "hmc_modify_user"]
    assert modifies[0]["verify_session_timeout"] == 5
    assert modifies[1]["description"] == ""
    assert modifies[1]["verify_session_timeout"] is None
    observed = _observations(state)
    assert sorted(observed) == sorted(
        [
            "user.list",
            "task_role.list",
            "resource_role.list",
            "remote_access.get",
            "user.create",
            "user.get",
            "user.modify",
            "user.delete",
        ]
    )
    assert {o["result"] for o in observed.values()} == {"passed"}, observed
    assert "description-cleared" in observed["user.modify"]["assertions"]
    assert len({o["id"] for o in observed.values()}) == 8
    assert "resource-roles-empty-branch" in observed["resource_role.list"]["assertions"]
    assert set(hmc.users) == {"uuid-1", "uuid-2"}
    password = create["password"]
    assert password not in json.dumps([state.results, state.observations], default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "readback",
    [
        None,
        {"ksv": "V1_17_0"},
        {"text": ""},
        {"text": "unexpected"},
        {"text": "hmcpctl live test user (modified)"},
    ],
    ids=["missing", "attribute-only", "empty", "unrelated", "unchanged"],
)
async def test_description_reset_requires_the_observed_default(
    hmc, monkeypatch, readback
):
    original = hmc.hmc_modify_user

    def incorrect_reset(console_uuid: str, user_profile_uuid: str, **changes: Any):
        original(console_uuid, user_profile_uuid, **changes)
        if changes["description"] == "":
            hmc.users[user_profile_uuid]["Resource"]["UserDescription"] = readback

    monkeypatch.setattr(hmc, "hmc_modify_user", incorrect_reset)
    state = _state()

    await users.exercise_users(None, state)

    observed = _observations(state)
    assert observed["user.modify"]["result"] == "failed"
    assert "description-cleared" not in observed["user.modify"]["assertions"]
    assert observed["user.delete"]["result"] == "passed"
    assert set(hmc.users) == {"uuid-1", "uuid-2"}


@pytest.mark.asyncio
async def test_a_disclosed_password_fails_the_listing(hmc):
    hmc.users["uuid-1"]["Resource"]["UserProfilePassword"] = {"text": "leaked"}
    state = _state()

    await users.exercise_users(None, state)

    listing = _observations(state)["user.list"]
    assert listing["result"] == "failed"
    assert "passwords-not-disclosed" not in listing["assertions"]
    assert "leaked" not in json.dumps(state.results)


@pytest.mark.asyncio
async def test_a_disclosed_bind_password_fails_and_is_not_recorded(hmc, monkeypatch):
    remote = copy.deepcopy(_CONSOLE_ENTRY)
    remote["Resource"]["LdapConfiguration"]["BindPassword"] = {"text": "bind-secret"}
    monkeypatch.setattr(hmc, "hmc_get_remote_access", lambda console_uuid: remote)
    state = _state()

    await users.exercise_users(None, state)

    read = _observations(state)["remote_access.get"]
    assert read["result"] == "failed"
    assert "bind-password-not-disclosed" not in read["assertions"]
    assert "bind-secret" not in json.dumps(state.results)


@pytest.mark.asyncio
async def test_a_failed_modify_still_deletes_the_scratch_user(hmc):
    hmc.fail["hmc_modify_user"] = 2
    state = _state()

    await users.exercise_users(None, state)

    assert "hmc_delete_user" in [tool for tool, _ in hmc.calls]
    assert set(hmc.users) == {"uuid-1", "uuid-2"}
    observed = _observations(state)
    assert observed["user.modify"]["result"] == "failed"
    assert observed["user.delete"]["result"] == "passed"


@pytest.mark.asyncio
async def test_an_ambiguous_create_is_found_and_deleted(hmc, monkeypatch):
    """A create that reports failure but committed is still deleted by UUID.

    The listing right after the create fails too, so only the final listing
    can resolve the minted name.
    """
    pending = {"list_failures": 0}

    async def scripted(_state, _client, tool, **kwargs):
        hmc.calls.append((tool, kwargs))
        if tool == "hmc_create_user":
            hmc.hmc_create_user(**kwargs)
            pending["list_failures"] = 1
            return "FAIL", classify_failure(RuntimeError("read timed out"))
        if tool == "hmc_list_users" and pending["list_failures"]:
            pending["list_failures"] -= 1
            return "FAIL", classify_failure(RuntimeError("listing failed"))
        return "PASS", getattr(hmc, tool)(**kwargs)

    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _state()

    await users.exercise_users(None, state)

    deletes = [kwargs for tool, kwargs in hmc.calls if tool == "hmc_delete_user"]
    assert deletes == [{"console_uuid": CONSOLE, "user_profile_uuid": "uuid-new"}]
    assert set(hmc.users) == {"uuid-1", "uuid-2"}
    assert _observations(state)["user.create"]["result"] == "failed"


@pytest.mark.asyncio
async def test_a_refused_create_that_echoes_the_password_records_none_of_it(
    hmc, monkeypatch
):
    async def echoing(_state, _client, tool, **kwargs):
        hmc.calls.append((tool, kwargs))
        if tool == "hmc_create_user":
            body = f"<UserProfilePassword>{kwargs['password']}</UserProfilePassword>"
            return "FAIL", classify_failure(RuntimeError(f"HTTP 400: {body}"))
        return "PASS", getattr(hmc, tool)(**kwargs)

    monkeypatch.setattr(runner.RunState, "call", echoing)
    state = _state()

    await users.exercise_users(None, state)

    password = next(k["password"] for t, k in hmc.calls if t == "hmc_create_user")
    assert password not in json.dumps([state.results, state.observations], default=str)
    created = _observations(state)["user.create"]
    assert "password-not-echoed" not in created["assertions"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda fake: setattr(fake, "roles", []), "task role hmcviewer is not listed"),
        (
            lambda fake: fake.users.update(
                {"uuid-9": _profile("uuid-9", "hmcpctl-live-0badf00d")}
            ),
            "a scratch user from an earlier run remains",
        ),
        (
            lambda fake: fake.fail.update({"hmc_list_users": 1}),
            "the user listing failed or could not be read",
        ),
        (
            lambda fake: fake.users["uuid-1"].pop("UUID"),
            "the user listing failed or could not be read",
        ),
    ],
    ids=["no-viewer-role", "residue", "listing-failed", "listing-unmappable"],
)
async def test_no_user_is_created_without_its_preconditions(hmc, setup, reason):
    setup(hmc)
    state = _state()

    await users.exercise_users(None, state)

    assert "hmc_create_user" not in [tool for tool, _ in hmc.calls]
    skipped = [row for row in state.results if row["tool"] in users._LIFECYCLE_TOOLS]
    assert len(skipped) == 4
    assert all(row["status"] == "SKIP" and reason in row["note"] for row in skipped)


def test_scrub_removes_the_password_from_failure_text():
    failure = classify_failure(RuntimeError("echo <UserProfilePassword>s3cret-pw<"))

    scrubbed = users._scrub(failure, "s3cret-pw")

    assert "s3cret-pw" not in scrubbed.message
    assert users._scrub({"a": ["s3cret-pw"]}, "s3cret-pw") == {"a": ["<redacted>"]}


def test_profile_rows_read_the_parser_shapes():
    assert users.profile_rows([_CAPTURED]) == {
        "00000606-abcd-4ef0-8abc-000000000606": "user-1"
    }
    assert users.is_empty(_CAPTURED["Resource"]["UserProfilePassword"])
    assert not users.is_empty(_CAPTURED["Resource"]["UserID"])


@pytest.mark.asyncio
async def test_a_refused_delete_records_the_hmc_error(hmc):
    """The verified row carries the final listing; the refusal needs its own row."""
    hmc.fail["hmc_delete_user"] = 1
    state = _state()

    await users.exercise_users(None, state)

    refused = [
        row for row in state.results if row["tool"] == "hmc_delete_user (refused)"
    ]
    assert [row["status"] for row in refused] == ["FAIL"]
    assert "hmc_delete_user refused" in json.dumps(refused)
    assert _observations(state)["user.delete"]["result"] == "failed"


@pytest.mark.asyncio
async def test_a_scratch_user_left_with_remote_access_fails_the_read(hmc, monkeypatch):
    """An HMC that ignored the disabled flags must not yield a passed observation."""
    original = hmc.hmc_create_user

    def ignoring_flags(console_uuid: str, user_id: str, **fields):
        original(console_uuid, user_id, **fields)
        resource = hmc.users["uuid-new"]["Resource"]
        resource["AllowSSHRemoteAccess"] = {"text": "true"}

    monkeypatch.setattr(hmc, "hmc_create_user", ignoring_flags)
    state = _state()

    await users.exercise_users(None, state)

    read = _observations(state)["user.get"]
    assert read["result"] == "failed"
    assert "remote-access-disabled" not in read["assertions"]


@pytest.mark.asyncio
async def test_a_modify_that_resets_remote_access_fails_the_modify(hmc, monkeypatch):
    """A partial POST that resets unsupplied fields must not promote user.modify."""
    original = hmc.hmc_modify_user

    def resetting(console_uuid: str, user_profile_uuid: str, **changes: Any):
        original(console_uuid, user_profile_uuid, **changes)
        hmc.users[user_profile_uuid]["Resource"]["AllowWebRemoteAccess"] = {
            "text": "true"
        }

    monkeypatch.setattr(hmc, "hmc_modify_user", resetting)
    state = _state()

    await users.exercise_users(None, state)

    modify = _observations(state)["user.modify"]
    assert modify["result"] == "failed"
    assert "remote-access-unchanged" not in modify["assertions"]


@pytest.mark.asyncio
async def test_a_refused_create_records_no_observation_for_steps_never_called(hmc):
    """Live 2026-10-06: the create was refused, so get, modify and delete never ran."""
    hmc.fail["hmc_create_user"] = 1
    state = _state()

    await users.exercise_users(None, state)

    observed = _observations(state)
    assert observed["user.create"]["result"] == "failed"
    assert observed["user.create"]["cleanup"] == "passed"
    assert not {"user.get", "user.modify", "user.delete"} & set(observed)
    skipped = {row["tool"] for row in state.results if row["status"] == "SKIP"}
    assert skipped == {"hmc_get_user", "hmc_modify_user", "hmc_delete_user"}
    assert "hmc_delete_user" not in [tool for tool, _ in hmc.calls]


@pytest.mark.asyncio
async def test_an_ignored_verify_session_timeout_fails_create_and_modify(
    hmc, monkeypatch
):
    """The minutes sent must read back: an HMC that keeps its default (the V10R3
    capture reads 0) promotes neither user.create nor user.modify (#1381)."""
    create, modify = hmc.hmc_create_user, hmc.hmc_modify_user

    def keeping_default(tool, *args: Any, **kwargs: Any) -> None:
        tool(*args, **kwargs)
        for user in hmc.users.values():
            user["Resource"]["VerifySessionTimeout"] = {"text": "0"}

    monkeypatch.setattr(
        hmc, "hmc_create_user", lambda *a, **k: keeping_default(create, *a, **k)
    )
    monkeypatch.setattr(
        hmc, "hmc_modify_user", lambda *a, **k: keeping_default(modify, *a, **k)
    )
    state = _state()

    await users.exercise_users(None, state)

    observed = _observations(state)
    assert observed["user.create"]["result"] == "failed"
    assert (
        "verify-session-timeout-read-back" not in observed["user.create"]["assertions"]
    )
    assert observed["user.modify"]["result"] == "failed"
    assert "verify-session-timeout-updated" not in observed["user.modify"]["assertions"]
    assert observed["user.get"]["result"] == "passed"
