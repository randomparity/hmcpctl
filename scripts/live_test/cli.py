"""Bounded read-only CLI observations of the admitted bare-CEC fixture."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys

from hmcpctl.jobs import SUCCESSFUL_JOB_STATUSES

from .observation import Assertion

LIMIT = 64 * 1024
TIMEOUT = 120
SCENARIO = "st35-bare-cec-cli"
LABELS = {
    "lpar.get_state": "CLI lpars state",
    "lpar.list_refcodes": "CLI lpars refcodes",
    "job.get": "CLI jobs show",
}


class CLIError(ValueError):
    """Safe diagnostic for a CLI read which cannot support an observation."""


def environment(config):
    if config is None or not config.host:
        raise CLIError("CLI read requires the runner's resolved HMC configuration")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("HMC_")
    }
    env.update(
        {
            f"HMC_{key.upper()}": str(value)
            for key, value in config.model_dump().items()
            if value is not None
        }
    )
    env.update(NO_COLOR="1", TERM="dumb")
    return env


async def execute(state, arguments):
    env = environment(state.hmc_config)
    process = None
    try:
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "hmcpctl",
                *arguments,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            raise

        async def read():
            assert process.stdout is not None
            output = bytearray()
            while chunk := await process.stdout.read(LIMIT + 1 - len(output)):
                output.extend(chunk)
                if len(output) > LIMIT:
                    raise CLIError("CLI output exceeded its byte limit")
            if await process.wait() != 0:
                raise CLIError("CLI command exited unsuccessfully")
            return output.decode("utf-8")

        return await asyncio.wait_for(read(), TIMEOUT)
    except (OSError, UnicodeError, TimeoutError) as error:
        raise CLIError("CLI read failed or exceeded its deadline") from error
    finally:
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.communicate()


async def _read(state, operation, arguments, *, structured=False):
    try:
        output = await execute(state, arguments)
        return json.loads(output) if structured else output.strip()
    except ValueError:
        # Child output may contain credentials or target identifiers, including on error.
        state.record(
            35, LABELS[operation], "FAIL", "CLI read failed; no verified evidence"
        )
        return None


async def observe_state(state, uuid, expected):
    actual = await _read(state, "lpar.get_state", ["lpars", "state", uuid])
    state.record_verified(
        35,
        LABELS["lpar.get_state"],
        operation="lpar.get_state",
        scenario=SCENARIO,
        assertions=[
            Assertion(
                "cli-state-matches",
                actual == expected and expected in {"open firmware", "running"},
            )
        ],
        cleanup="not-required",
        data={},
        surface="cli",
    )


async def observe_refcodes(state, system, name):
    rows = await _read(
        state,
        "lpar.list_refcodes",
        ["lpars", "refcodes", system, name, "--count", "5", "--json"],
        structured=True,
    )
    matches = (
        isinstance(rows, list)
        and 0 < len(rows) <= 5
        and all(isinstance(row, dict) and row.get("lpar_name") == name for row in rows)
    )
    state.record_verified(
        35,
        LABELS["lpar.list_refcodes"],
        operation="lpar.list_refcodes",
        scenario=SCENARIO,
        assertions=[Assertion("cli-refcodes-match-fixture", matches)],
        cleanup="not-required",
        data={},
        surface="cli",
    )


async def observe_job(state, job_id):
    job = await _read(state, "job.get", ["jobs", "show", job_id], structured=True)
    matches = (
        isinstance(job, dict)
        and job.get("job_id") == job_id
        and job.get("found") is True
        and job.get("status") in SUCCESSFUL_JOB_STATUSES
    )
    state.record_verified(
        35,
        LABELS["job.get"],
        operation="job.get",
        scenario=SCENARIO,
        assertions=[Assertion("cli-job-matches-completed-activation", matches)],
        cleanup="not-required",
        data={},
        surface="cli",
    )
