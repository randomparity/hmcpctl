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
    return h


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
    document = {
        "packet_sha256": source.digest(p),
        "before": before,
        "run": {"partial": False},
    }
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
    with pytest.raises(ValueError, match="digest"):
        source.main(["preflight", "--packet", str(path), "--packet-sha256", "0" * 64])
    raw_digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="grant|assertions"):
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


@pytest.mark.asyncio
async def test_empty_get_result_retains_scratch_for_explicit_reconciliation(
    monkeypatch, tmp_path
):
    p, h = packet(), client()
    successful = h.wait_for_job_entry.return_value
    empty = json.loads(json.dumps(successful))
    empty["Resource"]["Results"]["JobParameter"][1]["ParameterValue"] = ""
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
    with pytest.raises(ValueError, match="outside the checkout"):
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
