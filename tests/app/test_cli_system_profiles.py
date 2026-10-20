"""CLI system-profile pair parsing and truthful lifecycle exits."""

from dataclasses import replace
from unittest.mock import AsyncMock

from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.operations.systems.profiles import SystemProfileOutcome


def test_profiles_cli(monkeypatch):
    from hmcpctl.cli_commands.systems import profiles

    outcome = SystemProfileOutcome(
        "system",
        "name",
        "create",
        "rest-job",
        "completed",
        None,
        "0",
        None,
        None,
        "verified",
        (),
        (),
        None,
    )
    call = AsyncMock(return_value=outcome)
    monkeypatch.setattr(profiles, "create_system_profile", call)
    import asyncio

    monkeypatch.setattr(profiles, "with_client", lambda f: asyncio.run(f("hmc")))
    argv = [
        "systems",
        "profile-create",
        "system",
        "name",
        "--member",
        '{"partition":"member","profile":"contained"}',
        "--exclusive-writer-window",
        "--json",
    ]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert call.await_args.args[3][0].profile == "contained"
    call.return_value = replace(outcome, state="failed")
    assert CliRunner().invoke(app, argv).exit_code == 1
    call.return_value = replace(outcome, state="uncertain")
    assert CliRunner().invoke(app, argv).exit_code == 2
    before = call.await_count
    bad = argv.copy()
    bad[bad.index("--member") + 1] = '{"partition":"x","profile":"y","extra":1}'
    assert CliRunner().invoke(app, bad).exit_code != 0
    assert call.await_count == before


def test_profiles_cli_other_readers(monkeypatch):
    import asyncio

    from hmcpctl.cli_commands.systems import profiles
    from hmcpctl.operations.systems.profiles import SystemProfileInventory

    monkeypatch.setattr(profiles, "with_client", lambda f: asyncio.run(f("hmc")))
    for command, method, tail, result in [
        (
            "profile-list",
            "list_system_profiles",
            ["--name", "name"],
            SystemProfileInventory(
                "system", (), "ssh-command", None, "completed", None
            ),
        ),
        (
            "profile-update",
            "update_system_profile",
            ["name", "--new-name", "renamed"],
            SystemProfileOutcome(
                "system",
                "renamed",
                "update",
                "rest-job",
                "completed",
                None,
                "0",
                None,
                None,
                "verified",
                (),
                (),
                None,
            ),
        ),
        (
            "profile-delete",
            "delete_system_profile",
            ["name", "--exclusive-writer-window"],
            SystemProfileOutcome(
                "system",
                "name",
                "delete",
                "ssh-command",
                "completed",
                None,
                None,
                "completed",
                None,
                "verified",
                (),
                (),
                None,
            ),
        ),
        (
            "profile-activate",
            "activate_system_profile",
            ["name", "--action", "validate"],
            SystemProfileOutcome(
                "system",
                "name",
                "validate",
                "ssh-command",
                "completed",
                None,
                None,
                "completed",
                None,
                "not-applicable",
                (),
                (),
                None,
            ),
        ),
    ]:
        call = AsyncMock(return_value=result)
        monkeypatch.setattr(profiles, method, call)
        actual = CliRunner().invoke(
            app, ["systems", command, "system", *tail, "--json"]
        )
        assert actual.exit_code == 0, actual.output
        assert call.await_count == 1
