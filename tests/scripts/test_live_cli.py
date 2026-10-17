"""Offline CLI arm evidence; only process and transport boundaries are doubled."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_test_runner as runner
from live_test import cli
from live_test.observation import Assertion

from hmcpctl.config import HMCConfig


def state():
    return runner.RunState(
        hmc_config=HMCConfig.from_mapping({"host": "hmc.example.test"})
    )


def test_cli_record_is_separate_from_mcp():
    run = state()
    run.record_verified(
        35,
        "CLI lpars state",
        operation="lpar.get_state",
        scenario="st35-bare-cec-cli",
        assertions=[Assertion("state-matches", True)],
        cleanup="not-required",
        data={},
        surface="cli",
    )
    assert not run.observations
    assert run.results[0]["cli_observation"]["operation"] == "lpar.get_state"
    assert run.results[0]["result"] == "passed"


@pytest.mark.parametrize(
    "text,passed", [("open firmware\n", True), ("running", False), ("", False)]
)
def test_state_assertion(monkeypatch, text, passed):
    async def read(*args):
        return text

    monkeypatch.setattr(cli, "execute", read)
    run = state()
    asyncio.run(cli.observe_state(run, "fixture-uuid", "open firmware"))
    assert (run.results[-1]["result"] == "passed") is passed


def test_process_environment_pins_config(monkeypatch):
    monkeypatch.setenv("hmc_host", "wrong.example.test")
    monkeypatch.setenv("HMC_PROFILE", "wrong")
    env = cli.environment(state().hmc_config)
    assert env["HMC_HOST"] == "hmc.example.test"
    assert "hmc_host" not in env and "HMC_PROFILE" not in env
    assert env["NO_COLOR"] == "1"


def test_missing_config_fails_without_launch(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("missing config launched a child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(cli.CLIError):
        asyncio.run(cli.execute(runner.RunState(), ["lpars", "state", "fixture"]))


@pytest.mark.parametrize(
    "payload,passed",
    [
        ([{"lpar_name": "fixture"}], True),
        ([], False),
        ([{"lpar_name": "foreign"}], False),
        ([{"lpar_name": "fixture"}] * 6, False),
        ({"lpar_name": "fixture"}, False),
    ],
)
def test_refcode_identity_and_count(monkeypatch, payload, passed):
    async def read(*args):
        return json.dumps(payload)

    monkeypatch.setattr(cli, "execute", read)
    run = state()
    asyncio.run(cli.observe_refcodes(run, "system", "fixture"))
    assert (run.results[-1]["result"] == "passed") is passed


@pytest.mark.parametrize(
    "payload,passed",
    [
        ({"job_id": "job1", "found": True, "status": "COMPLETED_OK"}, True),
        ({"job_id": "job2", "found": True, "status": "COMPLETED_OK"}, False),
        ({"job_id": "job1", "found": False, "status": "COMPLETED_OK"}, False),
        ({"job_id": "job1", "found": True, "status": "RUNNING"}, False),
        ([], False),
    ],
)
def test_job_identity_and_success(monkeypatch, payload, passed):
    async def read(*args):
        return json.dumps(payload)

    monkeypatch.setattr(cli, "execute", read)
    run = state()
    asyncio.run(cli.observe_job(run, "job1"))
    assert (run.results[-1]["result"] == "passed") is passed


def test_bad_json_and_process_error_never_record_success(monkeypatch):
    async def read(*args):
        return "private invalid JSON"

    monkeypatch.setattr(cli, "execute", read)
    run = state()
    asyncio.run(cli.observe_job(run, "job1"))
    assert all(row["result"] == "failed" for row in run.results)
    assert "private invalid" not in json.dumps(run.results)


@pytest.mark.parametrize(
    "program,match",
    [
        ("import sys; sys.exit(7)", "unsuccessfully"),
        ("import time; time.sleep(10)", "deadline"),
        (
            "import sys; sys.stdout.write('x'*10000000); sys.stdout.flush()",
            "byte limit",
        ),
        ("import os; os.write(1, b'\\xff')", "failed"),
    ],
)
def test_process_failures_reap_child(monkeypatch, program, match):
    original = asyncio.create_subprocess_exec
    children = []

    async def launch(*arguments, **kwargs):
        assert arguments[:4] == (sys.executable, "-I", "-m", "hmcpctl")
        assert not kwargs.get("start_new_session")
        child = await original(sys.executable, "-c", program, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(cli, "TIMEOUT", 2)
    with pytest.raises(cli.CLIError, match=match):
        asyncio.run(cli.execute(state(), ["jobs", "show", "job1"]))
    assert children[0].returncode is not None


def test_process_reads_all_chunks_and_cancellation_reaps(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []
    program = (
        "import sys,time; print('first',flush=True); time.sleep(.02); print('second')"
    )

    async def launch(*arguments, **kwargs):
        child = await original(sys.executable, "-c", program, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    assert (
        asyncio.run(cli.execute(state(), ["jobs", "show", "job1"])) == "first\nsecond\n"
    )
    program = "import time; time.sleep(10)"

    async def cancel():
        task = asyncio.create_task(cli.execute(state(), ["jobs", "show", "job1"]))
        while len(children) < 2:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert children[-1].returncode is not None


def test_matrix_labels_expose_surface_without_targets():
    import live_test_evidence as evidence

    run = state()
    run.record_verified(
        35,
        "CLI lpars state",
        operation="lpar.get_state",
        scenario=cli.SCENARIO,
        assertions=[Assertion("cli-state-matches", True)],
        cleanup="not-required",
        data={"private": "fixture-target"},
        surface="cli",
    )
    row = evidence.allowlisted_row(run.results[0])
    assert row["tool"] == "CLI lpars state"
    assert "fixture-target" not in json.dumps(row)


TRANSPORT_CLI = r"""
import json, runpy, sys
from pathlib import Path
import httpx, respx
from hmcpctl.ssh import refcodes
async def ssh(config, command):
    assert config.host == "hmc.example.test"
    assert command == "lsrefcode -r lpar -m system --filter lpar_names=fixture -n 5 -F lpar_name,time_stamp,refcode --header"
    return "lpar_name,time_stamp,refcode\nfixture,2026-09-21 10:00:00,C2001150\n"
refcodes.run_hmc_command = ssh
fixture = json.loads(Path(sys.argv[1]).read_text())
sys.argv = ["hmcpctl", *sys.argv[2:]]
with respx.mock(base_url="https://hmc.example.test:443", assert_all_called=False) as router:
    router.put("/rest/api/web/Logon").respond(200, text='<LogonResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"><X-API-Session>fixture</X-API-Session></LogonResponse>')
    router.delete("/rest/api/web/Logon").respond(204)
    router.get("/rest/api/uom/LogicalPartition/00000001-ABCD-4EF0-8ABC-000000000001/quick/PartitionState").respond(200, text="open firmware")
    router.get(fixture["path"]).respond(200, text=fixture["body"])
    runpy.run_module("hmcpctl", run_name="__main__")
"""


def test_installed_cli_paths_with_only_transports_doubled(monkeypatch):
    original = asyncio.create_subprocess_exec
    fixture = Path(__file__).parents[1] / "fixtures/live/rest-job-completed-ok.json"
    calls = []

    async def launch(*arguments, **kwargs):
        assert arguments[:4] == (sys.executable, "-I", "-m", "hmcpctl")
        calls.append(arguments[4:])
        return await original(
            sys.executable,
            "-I",
            "-c",
            TRANSPORT_CLI,
            str(fixture),
            *arguments[4:],
            **kwargs,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    run = runner.RunState(
        hmc_config=HMCConfig.from_mapping(
            {
                "host": "hmc.example.test",
                "user": "fixture",
                "password": "fixture",  # pragma: allowlist secret — offline transport fixture
            }
        )
    )

    async def exercise():
        await cli.observe_state(
            run, "00000001-ABCD-4EF0-8ABC-000000000001", "open firmware"
        )
        await cli.observe_refcodes(run, "system", "fixture")
        await cli.observe_job(run, "1787837921266")

    asyncio.run(exercise())
    assert len(calls) == 3
    assert len(run.results) == 3
    assert all(row["result"] == "passed" for row in run.results), run.results
    assert not run.observations


def test_cancellation_during_spawn_retains_and_reaps_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []

    async def exercise():
        spawned = asyncio.Event()
        release = asyncio.Event()

        async def launch(*arguments, **kwargs):
            child = await original(
                sys.executable, "-c", "import time; time.sleep(10)", **kwargs
            )
            children.append(child)
            spawned.set()
            await release.wait()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        task = asyncio.create_task(cli.execute(state(), ["jobs", "show", "job1"]))
        await spawned.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert children[0].returncode is not None
