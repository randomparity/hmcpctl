"""Offline contracts for the narrowly authorized system-profile source arm."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from live_test import system_profile_source as source

S = "00000001-0000-4000-8000-000000000001"
L = ["00000002-0000-4000-8000-000000000002", "00000003-0000-4000-8000-000000000003"]
P = ["00000004-0000-4000-8000-000000000004", "00000005-0000-4000-8000-000000000005"]


def packet():
    return {
        "configured_profile": "fixture",
        "context_host": "hmc.test",
        "system": {"system_uuid": S, "command": "lssyscfg -r sysprof -m system1"},
        "scratch_system_profile": "hmcpctl-source-q1725-d647765d",
        "members": [
            {
                "partition_uuid": L[i],
                "partition_name": f"lpar{i}",
                "partition_state": ["not activated", "running"][i],
                "partition_type": "AIX/Linux",
                "profile_uuid": P[i],
                "profile_name": f"profile{i}",
                "ownership": {"description": None},
            }
            for i in range(2)
        ],
    }


def client():
    h = AsyncMock()
    h.config.host = "hmc.test"
    h.config.port = 443
    h.get_managed_system.return_value = {
        "UUID": S,
        "Resource": {"SystemName": "system1"},
    }
    h.list_logical_partitions.return_value = [
        {
            "UUID": L[i],
            "Resource": {
                "PartitionName": f"lpar{i}",
                "PartitionState": ["not activated", "running"][i],
                "PartitionType": "AIX/Linux",
            },
        }
        for i in range(2)
    ]

    async def profiles(parent, uuid, child):
        i = L.index(uuid)
        return [
            {"UUID": P[i], "Resource": {"ProfileName": f"profile{i}", "Memory": "1024"}}
        ]

    h.list_child.side_effect = profiles
    h.submit_job.side_effect = [{"Resource": {"JobID": f"job{i}"}} for i in range(3)]
    h.wait_for_job_entry.return_value = {
        "Resource": {
            "Status": "COMPLETED_OK",
            "Results": {
                "JobParameter": [
                    {"ParameterName": "returnCode", "ParameterValue": "0"},
                    {
                        "ParameterName": "result",
                        "ParameterValue": "opaque native result",
                    },
                ]
            },
        }
    }

    async def wait(identity, **kwargs):
        terminal = json.loads(json.dumps(h.wait_for_job_entry.return_value))
        terminal["Resource"]["JobID"] = identity
        return terminal

    h.wait_for_job_entry.side_effect = wait
    return h


def completed_document(p, before, h):
    from datetime import UTC, datetime

    records = []
    for i, operation in enumerate(source.OPERATIONS):
        identity = f"own-{i}"
        terminal = json.loads(json.dumps(h.wait_for_job_entry.return_value))
        terminal["Resource"]["JobID"] = identity
        records.append(
            {
                "operation": operation,
                "id": identity,
                "href": f"/rest/api/uom/jobs/{identity}",
                "submission": {"Resource": {"JobID": identity}},
                "terminal": terminal,
                "own_job_deleted": True,
            }
        )
    return {
        "packet_sha256": source.digest(p),
        "before": before,
        "phase": "captured-and-reconciled",
        "jobs": records,
        "run": {
            "partial": False,
            "group": source.GROUP,
            "finished": datetime.now(UTC).isoformat(),
        },
    }


def grant(p):
    return {
        "grant_packet_sha256": source.digest(p),
        "references_approved": True,
        "exclusive_writer_window": True,
    }


@pytest.mark.asyncio
async def test_capture_only_three_jobs_and_independent_recovery(monkeypatch, tmp_path):
    p, h = packet(), client()
    scratch = p["scratch_system_profile"]
    monkeypatch.setattr(
        source,
        "run_hmc_command",
        AsyncMock(
            side_effect=["name\n", "name\n", f"name\n{scratch}\n", "name\n", "name\n"]
        ),
    )
    document = await source.capture(h, p, tmp_path / "private", **grant(p))
    assert not document["run"]["partial"]
    assert [c.args[0].rsplit("/", 1)[-1] for c in h.submit_job.call_args_list] == [
        "CreateSystemProfile",
        "GetSystemProfiles",
        "DeleteSystemProfile",
    ]
    assert [c.args[0] for c in h.delete_job.call_args_list] == ["job0", "job1", "job2"]
    assert await source.recover(h, p, document) == "clean"
    assert h.submit_job.await_count == 3
    assert (tmp_path / "private" / "results.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alter",
    [
        "grant",
        "reference",
        "window",
        "host",
        "foreign",
        "duplicate",
        "equal",
        "state",
        "owner",
    ],
)
async def test_guard_refuses_before_domain_effect(alter, monkeypatch, tmp_path):
    p, h = packet(), client()
    args = grant(p)
    if alter == "grant":
        args["grant_packet_sha256"] = "0" * 64
    if alter == "reference":
        args["references_approved"] = False
    if alter == "window":
        args["exclusive_writer_window"] = False
    if alter == "host":
        h.config.host = "other.test"
    if alter == "foreign":
        h.list_child.return_value = []
        h.list_child.side_effect = None
    if alter == "duplicate":
        p["members"][1]["partition_uuid"] = L[0]
        args = grant(p)
    if alter == "equal":
        p["members"][1]["profile_name"] = "profile0"
        args = grant(p)
    if alter == "state":
        h.list_logical_partitions.return_value[0]["Resource"]["PartitionState"] = (
            "running"
        )
    if alter == "owner":
        h.list_logical_partitions.return_value[0]["Resource"]["Description"] = "changed"
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    with pytest.raises(ValueError):
        await source.capture(h, p, tmp_path / "private", **args)
    h.submit_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "name\nhmcpctl-source-q1725-d647765d\n",
        "wrong-header\n",
        "name\nx,y\n",
        "name\nx\nx\n",
    ],
)
async def test_namespace_collision_and_malformed_output_refuse(
    raw, monkeypatch, tmp_path
):
    p, h = packet(), client()
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value=raw))
    with pytest.raises(ValueError):
        await source.capture(h, p, tmp_path / "private", **grant(p))
    h.submit_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["FAILED_BEFORE_COMPLETION", "RUNNING", "COMPLETED_WITH_WARNINGS"]
)
async def test_uncertain_create_never_replayed_or_deleted(
    status, monkeypatch, tmp_path
):
    p, h = packet(), client()
    h.wait_for_job_entry.return_value["Resource"]["Status"] = status
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    with pytest.raises(ValueError):
        await source.capture(h, p, tmp_path / "private", **grant(p))
    assert h.submit_job.await_count == 1
    h.delete_job.assert_not_awaited()
    document = json.loads((tmp_path / "private" / "results.json").read_text())
    assert document["run"]["partial"] and document["jobs"][0]["id"] == "job0"


@pytest.mark.asyncio
async def test_drift_after_create_prevents_cleanup(monkeypatch, tmp_path):
    p, h = packet(), client()
    scratch = p["scratch_system_profile"]
    monkeypatch.setattr(
        source,
        "run_hmc_command",
        AsyncMock(side_effect=["name\n", "name\n", f"name\n{scratch}\nforeign\n"]),
    )
    with pytest.raises(ValueError):
        await source.capture(h, p, tmp_path / "private", **grant(p))
    assert h.submit_job.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["partial", "wrong-binding", "member-drift", "namespace-drift", "unreadable"],
)
async def test_recovery_fails_closed(kind, monkeypatch):
    p, h = packet(), client()
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    before = await source.snapshot(h, p)
    document = completed_document(p, before, h)
    if kind == "partial":
        document["run"]["partial"] = True
    if kind == "wrong-binding":
        document["packet_sha256"] = "0" * 64
    if kind == "member-drift":
        h.list_logical_partitions.return_value[0]["Resource"]["Description"] = "changed"
    if kind == "namespace-drift":
        source.run_hmc_command.return_value = "name\nforeign\n"
    if kind == "unreadable":
        source.run_hmc_command.side_effect = OSError("unreadable")
    with pytest.raises((ValueError, OSError)):
        await source.recover(h, p, document)
    h.submit_job.assert_not_awaited()
    h.delete_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["timeout", "interrupt", "nonzero", "no-id", "oversize", "unreadable"]
)
async def test_ambiguous_or_unreadable_source_is_never_replayed(
    fault, monkeypatch, tmp_path
):
    p, h = packet(), client()
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    if fault == "timeout":
        h.wait_for_job_entry.side_effect = TimeoutError("deadline")
    elif fault == "interrupt":
        import asyncio

        h.wait_for_job_entry.side_effect = asyncio.CancelledError()
    elif fault == "nonzero":
        h.wait_for_job_entry.return_value["Resource"]["Results"]["JobParameter"][0][
            "ParameterValue"
        ] = "1"
    elif fault == "no-id":
        h.submit_job.side_effect = [None]
    elif fault == "oversize":
        source.run_hmc_command.return_value = "x" * (source.LIMIT + 1)
    else:
        source.run_hmc_command.side_effect = OSError("unreadable namespace")
    import asyncio

    with pytest.raises((ValueError, TimeoutError, OSError, asyncio.CancelledError)):
        await source.capture(h, p, tmp_path / "private", **grant(p))
    assert h.submit_job.await_count <= 1
    h.delete_job.assert_not_awaited()
    saved = json.loads((tmp_path / "private" / "results.json").read_text())
    assert saved["run"]["partial"]


@pytest.mark.asyncio
async def test_duplicate_job_handle_cannot_delete_a_prior_job(monkeypatch, tmp_path):
    p, h = packet(), client()
    h.submit_job.side_effect = [{"Resource": {"JobID": "job0"}}] * 2
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    with pytest.raises(ValueError):
        await source.capture(h, p, tmp_path / "private", **grant(p))
    assert [c.args[0] for c in h.delete_job.call_args_list] == ["job0"]
    assert h.submit_job.await_count == 2


def test_canonical_preflight_and_recovery_route_only_the_exact_arm(
    monkeypatch, tmp_path
):
    import live_test_preflight as preflight
    import live_test_recovery as recovery

    called = []
    monkeypatch.setattr(source, "main", lambda args: called.append(args) or 0)
    monkeypatch.setattr(
        preflight,
        "_check_configuration",
        lambda: pytest.fail(
            "default configuration must not replace the exact source profile"
        ),
    )
    packet_path, journal = tmp_path / "packet.json", tmp_path / "results.json"
    assert (
        preflight.main(
            [
                "--group",
                source.GROUP,
                "--packet",
                str(packet_path),
                "--packet-sha256",
                "digest",
            ]
        )
        == 0
    )
    assert called[-1][0] == "preflight"
    assert (
        recovery.main(
            [
                "--source-packet",
                str(packet_path),
                "--packet-sha256",
                "digest",
                "--results",
                str(journal),
            ]
        )
        == 0
    )
    assert called[-1][0] == "recover"
    with pytest.raises(SystemExit):
        preflight.main(["--group", source.GROUP, "--skip-hardware"])
    assert len(called) == 2


def test_packet_digest_and_explicit_grant_fail_before_connecting(monkeypatch, tmp_path):
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet()))
    monkeypatch.setattr(
        source, "load_profile", lambda _: pytest.fail("no connection allowed")
    )
    assert (
        source.main(["preflight", "--packet", str(path), "--packet-sha256", "0" * 64])
        == 2
    )
    raw_digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    assert (
        source.main(
            [
                "capture",
                "--packet",
                str(path),
                "--packet-sha256",
                raw_digest,
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 2
    )


@pytest.mark.asyncio
async def test_empty_get_result_retains_scratch_for_explicit_reconciliation(
    monkeypatch, tmp_path
):
    p, h = packet(), client()
    successful = h.wait_for_job_entry.return_value
    empty = json.loads(json.dumps(successful))
    empty["Resource"]["Results"]["JobParameter"][1]["ParameterValue"] = ""
    successful["Resource"]["JobID"] = "job0"
    empty["Resource"]["JobID"] = "job1"
    h.wait_for_job_entry.side_effect = [successful, empty]
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    with pytest.raises(ValueError, match="nonempty raw result"):
        await source.capture(h, p, tmp_path / "private", **grant(p))
    assert h.submit_job.await_count == 2
    assert [c.args[0] for c in h.delete_job.call_args_list] == ["job0"]
    document = json.loads((tmp_path / "private" / "results.json").read_text())
    assert document["run"]["partial"]
    assert document["jobs"][1]["id"] == "job1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "missing-profile-identity",
        "duplicate-profile-id",
        "duplicate-profile-name",
        "ambiguous-partition-name",
    ],
)
async def test_incomplete_or_ambiguous_member_inventory_blocks_create(
    kind, monkeypatch, tmp_path
):
    p, h = packet(), client()
    original = h.list_child.side_effect

    async def malformed(parent, uuid, child):
        rows = await original(parent, uuid, child)
        if kind == "missing-profile-identity":
            rows.append({"Resource": {"ProfileName": "unknown"}})
        elif kind == "duplicate-profile-id":
            rows.append({"UUID": rows[0]["UUID"], "Resource": {"ProfileName": "other"}})
        elif kind == "duplicate-profile-name":
            rows.append(
                {
                    "UUID": "00000009-0000-4000-8000-000000000009",
                    "Resource": {"ProfileName": rows[0]["Resource"]["ProfileName"]},
                }
            )
        return rows

    h.list_child.side_effect = malformed
    if kind == "ambiguous-partition-name":
        h.list_logical_partitions.return_value.append(
            {
                "UUID": "00000009-0000-4000-8000-000000000009",
                "Resource": {"PartitionName": "lpar0"},
            }
        )
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    with pytest.raises(ValueError):
        await source.capture(h, p, tmp_path / "private", **grant(p))
    h.submit_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_other_job_operation_is_reachable(tmp_path):
    h = client()
    with pytest.raises(ValueError, match="three named jobs"):
        await source._job(h, packet(), "ActivateSystemProfile", {}, {}, tmp_path)
    h.submit_job.assert_not_awaited()


def test_capture_output_cannot_put_private_native_results_in_checkout(
    monkeypatch, tmp_path
):
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet()))
    raw_digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        source, "load_profile", lambda _: pytest.fail("no connection allowed")
    )
    assert (
        source.main(
            [
                "capture",
                "--packet",
                str(path),
                "--packet-sha256",
                raw_digest,
                "--grant-packet-sha256",
                raw_digest,
                "--references-approved",
                "--exclusive-writer-window",
                "--output",
                str(Path(source.__file__).resolve().parents[2] / "raw"),
            ]
        )
        == 2
    )


@pytest.mark.parametrize("override", ["host", "port"])
def test_effective_ambient_host_is_rejected_before_client_authentication(
    monkeypatch, tmp_path, override
):
    from functools import partial

    from hmcpctl.config import load_profile

    configuration = tmp_path / "config.toml"
    configuration.write_text(
        '[profiles.fixture]\nhost="hmc.test"\nuser="fixture"\npassword="p"\n'
    )
    monkeypatch.setenv(
        "HMC_HOST", "wrong-destination.test" if override == "host" else "hmc.test"
    )
    monkeypatch.setenv("HMC_PORT", "443" if override == "host" else "12443")
    monkeypatch.setattr(
        source, "load_profile", partial(load_profile, config_path=configuration)
    )
    constructed = []

    def forbidden(config):
        constructed.append(config)
        raise AssertionError("authentication must not be attempted")

    monkeypatch.setattr(source, "HMCClient", forbidden)
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet()))
    raw_digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    assert (
        source.main(["preflight", "--packet", str(path), "--packet-sha256", raw_digest])
        == 2
    )
    assert not constructed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "submission-link",
        "namespace",
        "terminal-id",
        "terminal-link",
        "missing-terminal-id",
        "effective-repeat",
    ],
)
async def test_job_identity_ambiguity_stops_before_poll_or_cleanup(fault, tmp_path):
    h, p = client(), packet()
    submitted = {"Resource": {"JobID": "own-job"}}
    terminal = json.loads(json.dumps(h.wait_for_job_entry.return_value))
    terminal["Resource"]["JobID"] = "own-job"
    document = {"jobs": []}
    if fault == "submission-link":
        submitted["link"] = "/rest/api/uom/jobs/unrelated-job"
    if fault == "namespace":
        submitted["link"] = "/rest/api/templates/jobs/own-job"
    if fault == "terminal-id":
        terminal["Resource"]["JobID"] = "unrelated-job"
    if fault == "terminal-link":
        terminal["link"] = "/rest/api/uom/jobs/unrelated-job"
    if fault == "missing-terminal-id":
        terminal["Resource"].pop("JobID")
    if fault == "effective-repeat":
        submitted["Resource"]["JobID"] = "second-id"
        submitted["link"] = "/rest/api/uom/jobs/first-id"
        document["jobs"] = [{"id": "first-id", "href": "/rest/api/uom/jobs/first-id"}]
    h.submit_job.side_effect = [submitted]
    h.wait_for_job_entry.side_effect = None
    h.wait_for_job_entry.return_value = terminal
    with pytest.raises(ValueError):
        await source._job(h, p, "CreateSystemProfile", {}, document, tmp_path)
    h.delete_job.assert_not_awaited()
    if fault in {"submission-link", "namespace", "effective-repeat"}:
        h.wait_for_job_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "queued",
        "wrong-group",
        "unfinished",
        "empty-jobs",
        "scratch-baseline",
        "failed-job",
        "uncleaned-job",
        "wrong-sequence",
        "terminal-id",
    ],
)
async def test_forged_partial_false_cannot_claim_clean(fault, monkeypatch):
    p, h = packet(), client()
    scratch = p["scratch_system_profile"]
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    before = await source.snapshot(h, p)
    document = completed_document(p, before, h)
    records = document["jobs"]
    if fault == "queued":
        document["phase"] = "submitting-CreateSystemProfile"
    if fault == "wrong-group":
        document["run"]["group"] = "other"
    if fault == "unfinished":
        document["run"]["finished"] = None
    if fault == "empty-jobs":
        document["jobs"] = []
    if fault == "scratch-baseline":
        before["namespace"] = [scratch]
        source.run_hmc_command.return_value = f"name\n{scratch}\n"
    if fault == "failed-job":
        records[0]["terminal"]["Resource"]["Status"] = "FAILED_BEFORE_COMPLETION"
    if fault == "uncleaned-job":
        records[1]["own_job_deleted"] = False
    if fault == "wrong-sequence":
        records.reverse()
    if fault == "terminal-id":
        records[0]["terminal"]["Resource"]["JobID"] = "unrelated-job"
    with pytest.raises(ValueError):
        await source.recover(h, p, document)
    h.submit_job.assert_not_awaited()
    h.delete_job.assert_not_awaited()


@pytest.mark.parametrize("failure_kind", ["ssh", "rest"])
def test_cli_native_errors_are_sanitized(monkeypatch, tmp_path, capsys, failure_kind):
    from hmcpctl.config import HMCConfig
    from hmcpctl.errors import HMCError
    from hmcpctl.ssh.transport import HMCCLIError

    cfg = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "fixture", "password": "p"}
    )
    monkeypatch.setattr(source, "load_profile", lambda _: cfg)

    class FailedClient:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            print("native-host.test secret-sentinel", file=sys.stderr)
            if failure_kind == "ssh":
                raise HMCCLIError("native-host.test account-identifier secret-sentinel")
            raise HMCError(
                "REST failure",
                status_code=500,
                body="native-host.test account-identifier secret-sentinel",
            )

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(source, "HMCClient", FailedClient)
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet()))
    raw_digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    assert (
        source.main(["preflight", "--packet", str(path), "--packet-sha256", raw_digest])
        == 2
    )
    output = capsys.readouterr()
    assert "secret-sentinel" not in output.out + output.err
    assert "native-host.test" not in output.out + output.err
    assert "Traceback" not in output.out + output.err
    assert "STOPPED" in output.err


def test_provenance_uses_checkout_and_existing_renderer_clean_field(monkeypatch):
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return "a" * 40 + "\n" if "rev-parse" in args else ""

    monkeypatch.setattr(source.subprocess, "check_output", command)
    assert source._revision() == ("a" * 40, True)
    assert all(
        c[1].get("cwd") == Path(source.__file__).resolve().parents[2] for c in calls
    )


@pytest.mark.parametrize(
    "link",
    [
        None,
        "/rest/api/uom/jobs/own-1",
        "https://hmc.test:443/rest/api/uom/jobs/own-1/00000009-0000-4000-8000-000000000009",
    ],
)
def test_canonical_stable_job_identity_supports_real_read_self_links(link):
    job = {"Resource": {"JobID": "own-1"}}
    if link is not None:
        job["link"] = link
    assert source._identity(job, packet()) == ("own-1", "/rest/api/uom/jobs/own-1")


@pytest.mark.asyncio
async def test_recovery_requires_live_scratch_absence_with_a_valid_clean_baseline(
    monkeypatch,
):
    p, h = packet(), client()
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    document = completed_document(p, await source.snapshot(h, p), h)
    source.run_hmc_command.return_value = "name\n" + p["scratch_system_profile"] + "\n"
    with pytest.raises(ValueError):
        await source.recover(h, p, document)
    h.submit_job.assert_not_awaited()
    h.delete_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_failure_detail_is_saved_only_in_owned_private_journal(
    monkeypatch, tmp_path
):
    p, h = packet(), client()
    monkeypatch.setattr(source, "run_hmc_command", AsyncMock(return_value="name\n"))
    h.wait_for_job_entry.side_effect = OSError("native-diagnostic.test secret-sentinel")
    monkeypatch.setattr(source, "_revision", lambda: ("a" * 40, False))
    with pytest.raises(OSError):
        await source.capture(h, p, tmp_path / "private", **grant(p))
    journal = tmp_path / "private" / "results.json"
    document = json.loads(journal.read_text())
    assert document["failure"]["detail"] == "native-diagnostic.test secret-sentinel"
    assert journal.stat().st_mode & 0o777 == 0o600
    assert document["run"]["partial"] is True
    assert (
        document["run"]["tree_clean"] is False
    )  # Controlled dirty-tree provenance input.


@pytest.mark.asyncio
async def test_final_journal_save_failure_cannot_leave_a_completed_marker(
    monkeypatch, tmp_path
):
    p, h = packet(), client()
    scratch = p["scratch_system_profile"]
    monkeypatch.setattr(
        source,
        "run_hmc_command",
        AsyncMock(side_effect=["name\n", "name\n", f"name\n{scratch}\n", "name\n"]),
    )
    original = source.save
    failed = False

    def fail_final_once(directory, document):
        nonlocal failed
        if document.get("phase") == "captured-and-reconciled" and not failed:
            failed = True
            raise OSError("controlled final save failure")
        original(directory, document)

    monkeypatch.setattr(source, "save", fail_final_once)
    with pytest.raises(OSError):
        await source.capture(h, p, tmp_path / "private", **grant(p))
    document = json.loads((tmp_path / "private" / "results.json").read_text())
    assert document["run"]["partial"] is True
    with pytest.raises(ValueError):
        await source.recover(h, p, document)
    assert h.submit_job.await_count == 3


@pytest.mark.parametrize("port", [443, 12443])
def test_cli_pins_validated_port_before_authentication_transport_failure(
    monkeypatch, tmp_path, port
):
    from functools import partial

    import httpx
    import respx

    from hmcpctl.config import load_profile

    configuration = tmp_path / "config.toml"
    configuration.write_text(
        '[profiles.fixture]\nhost="hmc.test"\nuser="fixture"\npassword="p"\nverify_ssl=true\n'
        + (f"port={port}\n" if port != 443 else "")
    )
    monkeypatch.delenv("HMC_PORT", raising=False)
    monkeypatch.delenv("HMC_HOST", raising=False)
    monkeypatch.setattr(
        source, "load_profile", partial(load_profile, config_path=configuration)
    )
    binding = packet()
    binding["context_port"] = port
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(binding))
    raw_digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    with respx.mock(assert_all_called=False) as router:
        selected = router.put(
            url__regex=(
                r"https://hmc\.test/rest/api/web/Logon"
                if port == 443
                else r"https://hmc\.test:12443/rest/api/web/Logon"
            )
        ).mock(side_effect=httpx.ConnectError("selected transport failed"))
        other = router.put(
            url__regex=(
                r"https://hmc\.test:12443/rest/api/web/Logon"
                if port == 443
                else r"https://hmc\.test/rest/api/web/Logon"
            )
        ).mock(side_effect=httpx.ConnectError("unapproved credential destination"))
        assert (
            source.main(
                ["preflight", "--packet", str(path), "--packet-sha256", raw_digest]
            )
            == 2
        )
    assert selected.call_count == 1
    assert other.call_count == 0
