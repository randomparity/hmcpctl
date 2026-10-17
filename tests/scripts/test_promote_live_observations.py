"""Offline contract tests for completed-window promotion; no live evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import check_capability_inventory as inventory
import live_test_runner as runner
import promote_live_observations as promotion

ROOT = Path(__file__).parents[2]


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def write_json(path, value):
    path.write_text(json.dumps(value) + "\n")


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "candidate"
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(ROOT), str(root)], check=True
    )
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "switch", "-c", "fixture-candidate")
    for name in ("promote_live_observations.py", "check_capability_inventory.py"):
        shutil.copyfile(ROOT / "scripts" / name, root / "scripts" / name)
    claims = root / "docs/capabilities/releases/v0.0.0.json"
    write_json(
        claims,
        {
            "format_version": 1,
            "operations": [
                {
                    "operation": "system.get",
                    "arm": "round2",
                    "scenario": "st1-system-inventory",
                }
            ],
        },
    )
    git(
        root,
        "add",
        "scripts/promote_live_observations.py",
        "scripts/check_capability_inventory.py",
        str(claims),
    )
    git(root, "commit", "-qm", "Prepare anonymous offline fixture")
    (root / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
    head = git(root, "rev-parse", "HEAD")
    now = datetime.now(UTC)
    observed = (now - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tool = next(x for x in inventory.discover_registry() if x.operation == "system.get")
    observation = {
        "id": "st1-hmc-get-system",
        "channel": "live",
        "result": "passed",
        "scenario": "st1-system-inventory",
        "tested_commit": head,
        "observed_at": observed,
        "hmc_release": "V10R3",
        "hardware_family": "POWER10",
        "schema_version": "V1_0",
        "cleanup": "not-required",
        "closure_fingerprint": inventory.closure_fingerprint(
            root, tool.handler.rsplit(".", 1)[0]
        ),
        "assertions": ["response-validated"],
    }
    results = root / "test-results-offline.json"
    run = {
        "tested_commit": head,
        "tree_clean": True,
        "group": "round2",
        "subtasks": runner.SUBTASK_GROUPS["round2"],
        "schema_version": "V1_0",
        "finished": now.isoformat(),
        "partial": False,
    }
    write_json(
        results,
        {
            "run": run,
            "config": {"private": "DO-NOT-COPY-RAW"},
            "results": [
                {
                    "subtask": 1,
                    "tool": "hmc_get_system",
                    "status": "PASS",
                    "result": "passed",
                    "timestamp": observed,
                    "note": "",
                    "data": {},
                }
            ],
        },
    )
    sidecar = results.with_name("test-results-offline-observations.json")
    write_json(sidecar, [{"operation": "system.get", "observation": observation}])
    return root, claims, results, sidecar, run, observation


def promote(candidate, **kwargs):
    root, claims, results, *_ = candidate
    return promotion.promote(
        root,
        claims,
        "promotion/offline",
        [results],
        window_complete=True,
        recovery_confirmed=True,
        **kwargs,
    )


def test_promote_completed_window(candidate):
    root, claims, results, _, _, observation = candidate
    original = git(root, "rev-parse", "HEAD")
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "UV_NO_SYNC": "1"}
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/promote_live_observations.py"),
            "--claims",
            str(claims),
            "--branch",
            "promotion/offline",
            "--window-complete",
            "--recovery-confirmed",
            str(results),
        ],
        cwd=root,
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, "branch", "--show-current") == "promotion/offline"
    assert git(root, "rev-parse", "fixture-candidate") == original
    assert git(root, "status", "--porcelain") == ""
    paths = set(
        git(
            root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
        ).splitlines()
    )
    assert {
        "docs/capabilities/maturity.json",
        "docs/capabilities/release-verification.json",
        "src/hmcpctl/_operation_maturity.json",
    } <= paths
    assert all(p in promotion.ARTIFACTS or p.startswith("docs/tools/") for p in paths)
    assert "DO-NOT-COPY-RAW" not in git(root, "show", "HEAD")
    catalog = inventory.load_json(root / "docs/capabilities/maturity.json")
    record = next(x for x in catalog["operations"] if x["operation"] == "system.get")
    assert [x for x in record["evidence"] if x["channel"] == "live"] == [observation]
    snapshot = inventory.load_release_verification(
        root / "docs/capabilities/release-verification.json"
    )
    assert snapshot["system.get"]["verification"] == "current"
    assert snapshot["system.get"]["observed_at"] == observation["observed_at"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("partial", True),
        ("partial", 0),
        ("tree_clean", False),
        ("tested_commit", "0" * 40),
        ("group", "users"),
        ("subtasks", [1]),
        ("finished", "bad"),
        ("finished", "2099-01-01T00:00:00+00:00"),
        ("detach_probe", True),
        ("schema_version", "private-value"),
    ],
)
def test_reject_invalid_run(candidate, field, value):
    root, _, results, _, run, _ = candidate
    run[field] = value
    write_json(results, {"run": run})
    with pytest.raises(promotion.PromotionError):
        promote(candidate)
    assert git(root, "branch", "--show-current") == "fixture-candidate"


@pytest.mark.parametrize(
    "field,value",
    [
        ("tested_commit", "0" * 40),
        ("closure_fingerprint", "0" * 64),
        ("scenario", "st1-other"),
        ("channel", "automated"),
        ("result", "failed"),
        ("cleanup", "failed"),
        ("assertions", []),
        ("private", "DO-NOT-COPY-RAW"),
        ("observed_at", "2000-01-01T00:00:00Z"),
        ("observed_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_reject_invalid_input(candidate, field, value):
    root, _, _, sidecar, _, observation = candidate
    observation[field] = value
    write_json(sidecar, [{"operation": "system.get", "observation": observation}])
    with pytest.raises(promotion.PromotionError):
        promote(candidate)
    assert git(root, "branch", "--show-current") == "fixture-candidate"


@pytest.mark.parametrize(
    "contents",
    ["{", "{}", "[]", '[{"operation":"a","operation":"b"}]', " " * (1024 * 1024 + 1)],
    ids=["invalid-json", "object", "empty", "duplicate-key", "oversized"],
)
def test_reject_malformed_sidecar(candidate, contents):
    candidate[3].write_text(contents)
    with pytest.raises(promotion.PromotionError):
        promote(candidate)


@pytest.mark.parametrize("staged", [False, True])
def test_reject_dirty_tree(candidate, staged):
    root = candidate[0]
    (root / "unrelated.txt").write_text("private")
    if staged:
        git(root, "add", "unrelated.txt")
    with pytest.raises(promotion.PromotionError, match="clean"):
        promote(candidate)
    assert git(root, "branch", "--show-current") == "fixture-candidate"


@pytest.mark.parametrize("completion,recovery", [(False, True), (True, False)])
def test_require_completion_attestations(candidate, completion, recovery):
    root, claims, results, *_ = candidate
    with pytest.raises(promotion.PromotionError):
        promotion.promote(
            root,
            claims,
            "promotion/offline",
            [results],
            window_complete=completion,
            recovery_confirmed=recovery,
        )


@pytest.mark.parametrize(
    "branch", ["main", "master", "fixture-candidate", "promotion/../bad"]
)
def test_reject_branch(candidate, branch):
    root, claims, results, *_ = candidate
    with pytest.raises(promotion.PromotionError):
        promotion.promote(
            root,
            claims,
            branch,
            [results],
            window_complete=True,
            recovery_confirmed=True,
        )


def test_reject_non_allowlisted_output(candidate, monkeypatch):
    root = candidate[0]
    original = promotion._regenerate

    def unexpected(*args):
        original(*args)
        (root / "unrelated.txt").write_text("DO-NOT-COPY-RAW")

    monkeypatch.setattr(promotion, "_regenerate", unexpected)
    head = git(root, "rev-parse", "HEAD")
    with pytest.raises(promotion.PromotionError, match="allowlist"):
        promote(candidate)
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "diff", "--cached", "--name-only") == ""


def test_reject_symlink_output(candidate, tmp_path):
    root = candidate[0]
    path = root / "docs/capabilities/release-verification.json"
    external = tmp_path / "external.json"
    external.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(external)
    git(root, "add", str(path))
    git(root, "commit", "-qm", "Use fixture symlink")
    with pytest.raises(promotion.PromotionError, match="symlink"):
        promote(candidate)
    assert external.read_bytes() == path.read_bytes()


def test_select_claimed_route_and_latest_unclaimed():
    older = {
        "id": "a",
        "observed_at": "2026-10-12T00:00:00Z",
        "scenario": "st1-first",
        "result": "passed",
    }
    newer = {
        **older,
        "id": "b",
        "observed_at": "2026-10-13T00:00:00Z",
        "scenario": "st2-second",
    }
    options = [("round2", older), ("dedicated", newer)]
    assert (
        promotion._select(options, {"arm": "round2", "scenario": "st1-first"}) == older
    )
    assert promotion._select(options, None) == newer
    with pytest.raises(promotion.PromotionError, match="conflicting"):
        promotion._select(
            [("round2", newer), ("dedicated", {**newer, "result": "failed"})], None
        )


def test_merge_confirmed_gap_preserves_reviewed_implementation():
    scope = {"variant": "feature", "parameters": []}
    catalog = {
        "operations": [
            {
                "operation": "system.get",
                "implementation": {"state": "partial", "missing_scope": [scope]},
                "evidence": [],
            }
        ]
    }
    confirmed = {**scope, "confirmation": {"observed_at": "2026-10-13T00:00:00Z"}}
    promotion._merge(catalog, {}, [("system.get", confirmed)], [])
    assert catalog["operations"][0]["implementation"] == {
        "state": "partial",
        "missing_scope": [confirmed],
    }
    with pytest.raises(promotion.PromotionError, match="matching reviewed"):
        promotion._merge(
            catalog, {}, [("system.get", {**confirmed, "variant": "unknown"})], []
        )


def _media_delete(evidence):
    return {
        "operations": [
            {
                "operation": "media.delete",
                "implementation": {"state": "implemented", "missing_scope": []},
                "evidence": evidence,
            }
        ]
    }


_HELD = ["refused-while-mounted", "delete-accepted", "media-absent"]
_STORED = {
    "id": "old",
    "channel": "live",
    "result": "passed",
    "scenario": "st19-optical-round-trip",
    "observed_at": "2026-10-13T00:00:00Z",
    "assertions": _HELD,
}


def _promote_media_delete(stored, assertions, **change):
    catalog = _media_delete([stored])
    new = {**_STORED, "assertions": assertions, **change}
    promotion._merge(catalog, {"media.delete": [("round2", new)]}, [], [])
    return catalog["operations"][0]["evidence"]


def test_refuse_passing_observation_with_strict_subset_of_assertions():
    catalog = _media_delete([_STORED])
    weaker = {**_STORED, "assertions": _HELD[1:]}
    with pytest.raises(
        promotion.PromotionError,
        match="media.delete lacks assertions .*: refused-while-mounted; rerun",
    ):
        promotion._merge(catalog, {"media.delete": [("round2", weaker)]}, [], [])
    assert catalog["operations"][0]["evidence"] == [_STORED]


@pytest.mark.parametrize(
    "assertions",
    [
        _HELD,
        [*_HELD, "media-equal-baseline"],
        # A set that is not a subset still replaces: alternative ids such as
        # lpar.py's sync-disable/sync-enable reads are disjoint.
        ["delete-accepted", "media-equal-baseline"],
        ["media-equal-baseline"],
    ],
)
def test_replace_with_equal_superset_or_non_subset_assertions(assertions):
    assert _promote_media_delete(_STORED, assertions) == [
        {**_STORED, "assertions": assertions}
    ]


@pytest.mark.parametrize(
    ("stored", "change"),
    [
        ({**_STORED, "result": "failed"}, {}),
        ({**_STORED, "channel": "automated"}, {}),
        (_STORED, {"result": "failed"}),
    ],
)
def test_subset_only_guards_passed_live_evidence(stored, change):
    evidence = _promote_media_delete(stored, _HELD[1:], **change)
    assert evidence[-1]["assertions"] == _HELD[1:]


def test_subset_guard_follows_an_observation_that_changed_scenario():
    catalog = _media_delete([_STORED])
    moved = {**_STORED, "scenario": "st19-other-scenario", "assertions": _HELD[1:]}
    with pytest.raises(promotion.PromotionError, match="refused-while-mounted"):
        promotion._merge(catalog, {"media.delete": [("round2", moved)]}, [], [])
    stronger = {**moved, "assertions": [*_HELD, "media-equal-baseline"]}
    promotion._merge(catalog, {"media.delete": [("round2", stronger)]}, [], [])
    assert catalog["operations"][0]["evidence"] == [stronger]


def test_subset_guard_ignores_stored_rows_of_other_observations():
    # The weaker row replaces nothing: the stronger one stays beside it.
    other = {**_STORED, "id": "other", "assertions": _HELD[1:]}
    catalog = _media_delete([_STORED])
    promotion._merge(catalog, {"media.delete": [("round2", other)]}, [], [])
    assert catalog["operations"][0]["evidence"] == [_STORED, other]


_SHARED = {
    "id": "st39-hmc-modify-lpar",
    "channel": "live",
    "result": "passed",
    "scenario": "st39-lpar-config",
    "observed_at": "2026-10-08T00:00:00Z",
    "closure_fingerprint": "c" * 64,
    "assertions": ["memory-read-back", "processors-read-back", "weight-read-back"],
}
_DEDICATED = {
    **_SHARED,
    "id": "st39-hmc-modify-lpar-dedicated",
    "assertions": ["assignment-workflow-completed", "dedicated-profile-read-back"],
}
_AUTOMATED = {**_SHARED, "id": "contract", "channel": "automated"}


def _lpar_modify(observations, claims=(), stored=(_AUTOMATED, _SHARED, _DEDICATED)):
    catalog = {
        "operations": [
            {
                "operation": "lpar.modify",
                "implementation": {"state": "implemented", "missing_scope": []},
                "evidence": list(stored),
            }
        ]
    }
    promotion._merge(catalog, {"lpar.modify": observations}, [], list(claims))
    return catalog["operations"][0]["evidence"]


def _later(row, **change):
    return {**row, "observed_at": "2026-10-15T00:00:00Z", **change}


_CLAIM = {"operation": "lpar.modify", "arm": "round2", "scenario": "st39-lpar-config"}


def test_two_producers_in_one_scenario_each_keep_their_row():
    both = [("round2", _later(_SHARED)), ("round2", _later(_DEDICATED))]
    assert _lpar_modify(both, [_CLAIM]) == [
        _AUTOMATED,
        _later(_SHARED),
        _later(_DEDICATED),
    ]


def test_window_replaces_only_the_observations_it_produced():
    dedicated = [("round2", _later(_DEDICATED))]
    assert _lpar_modify(dedicated) == [_AUTOMATED, _SHARED, _later(_DEDICATED)]


def test_one_observation_from_two_arms_keeps_the_newest():
    older = _later(_DEDICATED, observed_at="2026-10-14T00:00:00Z")
    rows = [("dedicated", _later(_DEDICATED)), ("round2", older)]
    assert _lpar_modify(rows) == [_AUTOMATED, _SHARED, _later(_DEDICATED)]
    with pytest.raises(promotion.PromotionError, match="conflicting"):
        _lpar_modify(
            [
                ("dedicated", _later(_DEDICATED)),
                ("round2", _later(_DEDICATED, result="failed")),
            ]
        )


@pytest.mark.parametrize(
    "newer",
    [
        (
            "dedicated",
            _later(_SHARED, observed_at="2026-10-15T00:00:01Z", result="failed"),
        ),
        (
            "dedicated",
            _later(_DEDICATED, observed_at="2026-10-15T00:00:01Z", result="failed"),
        ),
    ],
)
def test_refuse_claim_superseded_by_newer_evidence(newer):
    # Readers would report the newer row; promotion refuses before the branch.
    with pytest.raises(promotion.PromotionError, match="newer live observation"):
        _lpar_modify([("round2", _later(_SHARED)), newer], [_CLAIM])


def test_refuse_claim_tied_with_a_failed_row_of_greater_id():
    tied = ("dedicated", _later(_DEDICATED, result="failed"))
    with pytest.raises(promotion.PromotionError, match="newer live observation"):
        _lpar_modify([("round2", _later(_SHARED)), tied], [_CLAIM])


def test_newer_non_live_evidence_does_not_supersede_a_claim():
    automated = _later(_AUTOMATED, observed_at="2026-10-16T00:00:00Z", result="failed")
    assert _lpar_modify(
        [("round2", _later(_SHARED))], [_CLAIM], stored=(automated, _DEDICATED)
    ) == [automated, _DEDICATED, _later(_SHARED)]


def test_refuse_claim_superseded_by_a_newer_stored_row_of_another_closure():
    # Promoting an older window: the stored dedicated row is newer and its
    # closure differs from the candidate's.
    older = {
        **_SHARED,
        "observed_at": "2026-10-07T00:00:00Z",
        "closure_fingerprint": "d" * 64,
    }
    with pytest.raises(promotion.PromotionError, match="newer live observation"):
        _lpar_modify([("round2", older)], [_CLAIM])


def test_claim_still_needs_its_own_passed_observation():
    rows = [
        ("round2", _later(_SHARED, result="failed")),
        ("dedicated", _later(_DEDICATED)),
    ]
    with pytest.raises(promotion.PromotionError, match="no eligible passed"):
        _lpar_modify(rows, [_CLAIM])


def test_reject_missing_and_duplicate_arms(candidate):
    root, claims, results, *_ = candidate
    for inputs in ([], [results, results]):
        with pytest.raises(promotion.PromotionError):
            promotion.promote(
                root,
                claims,
                "promotion/offline",
                inputs,
                window_complete=True,
                recovery_confirmed=True,
            )


def test_reject_leftover_sidecar_after_failed_rerun(candidate):
    root, _, results, sidecar, _, observation = candidate
    document = json.loads(results.read_text())
    document["results"][0].update(status="FAIL", result="failed")
    write_json(results, document)
    observation["observed_at"] = (datetime.now(UTC) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    write_json(sidecar, [{"operation": "system.get", "observation": observation}])
    with pytest.raises(promotion.PromotionError, match="invocation"):
        promote(candidate)
    assert git(root, "branch", "--show-current") == "fixture-candidate"


@pytest.mark.parametrize(
    "change",
    [
        {"result": "failed", "status": "FAIL"},
        {"tool": "hmc_list_managed_systems"},
        {"subtask": True},
        {"timestamp": "invalid"},
        {"timestamp": "2026-01-01T00:00:00"},
    ],
)
def test_reject_mismatched_result(candidate, change):
    root, _, results, *_ = candidate
    document = json.loads(results.read_text())
    document["results"][0].update(change)
    write_json(results, document)
    with pytest.raises(promotion.PromotionError, match="invocation"):
        promote(candidate)
    assert git(root, "branch", "--show-current") == "fixture-candidate"


def test_confirmed_gap_matches_fresh_producer_result():
    outcome = next(
        value
        for module in runner._SCENARIO_MODULES
        for value in vars(module).values()
        if isinstance(value, runner.ExpectedOutcome) and not value.transient
    )
    tool = next(
        name
        for name, security in runner.TOOL_SECURITY.items()
        if security.operation == outcome.operation
    )
    state = runner.RunState()
    failure = runner.CallFailure(
        "FixtureFailure", next(iter(outcome.error_codes), ""), "", None, outcome.denial
    )
    state.record_with_expected(1, tool, "FAIL", failure, [outcome])
    run = {
        "results": state.results,
        "subtasks": [1],
        "finished_time": datetime.now(UTC),
    }
    promotion._invocation(state.gaps[0], run, {})
    state.results[0]["note"] = "known gap: " + outcome.reason
    with pytest.raises(promotion.PromotionError, match="invocation"):
        promotion._invocation(state.gaps[0], run, {})
    state.results[0]["note"] = outcome.reason
    state.gaps[0]["missing_scope"]["confirmation"]["observed_at"] = (
        datetime.now(UTC) - timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(promotion.PromotionError, match="invocation"):
        promotion._invocation(state.gaps[0], run, {})


def producer_states():
    from live_test import bare_cec, pcie

    dedicated = runner.RunState()
    pcie.record_dedicated_listing(
        dedicated,
        "PASS",
        {
            "capability": "available",
            "items": [{"drc_index": "1", "owner_lpar": None}],
        },
    )
    pcie._record_dedicated_additions(
        dedicated,
        SimpleNamespace(
            evidence={"assign": (True, True), "reassign": (True, True)},
            drc_index="1",
        ),
        True,
    )
    bare = runner.RunState()
    bare_cec._record_create_and_assign(
        bare,
        SimpleNamespace(
            fixture=SimpleNamespace(drc_index="1", applied_io_slots="1/none/0"),
            create_attempted=False,
            assign_holds=(True, True),
        ),
        True,
    )
    return [("dedicated", dedicated), ("bare-cec", bare)]


@pytest.mark.parametrize("arm", ["dedicated", "bare-cec"])
def test_accept_actual_producer_row_numbers(arm):
    state = dict(producer_states())[arm]
    run = {
        "group": arm,
        "subtasks": runner.SUBTASK_GROUPS[arm],
        "results": state.results,
        "finished_time": datetime.now(UTC),
    }
    for observation in state.observations:
        promotion._invocation(observation, run, inventory.discover_observation_sites())


def test_accept_actual_producer_recording_label():
    state = dict(producer_states())["dedicated"]
    observation = state.observations[-1]
    assert observation["operation"] == "command.run"
    assert state.results[-1]["tool"] == "chsyscfg-io-slots-add"
    # Isolate the label reader from the separate dispatcher/row-ID regression.
    run = {
        "group": "dedicated",
        "subtasks": [29, 31, 33],
        "results": state.results,
        "finished_time": datetime.now(UTC),
    }
    promotion._invocation(observation, run, inventory.discover_observation_sites())
    observation["observation"]["scenario"] = "st1-system-inventory"
    with pytest.raises(promotion.PromotionError, match="invocation"):
        promotion._invocation(observation, run, inventory.discover_observation_sites())


@pytest.fixture
def producer_candidate(candidate, request):
    root, claims, *_ = candidate
    write_json(
        claims,
        {
            "format_version": 1,
            "operations": [
                {
                    "operation": "pcie.list_dedicated_slots",
                    "arm": "dedicated",
                    "scenario": "st29-pcie-inventory",
                },
                {
                    "operation": "pcie.assign_dedicated_slot",
                    "arm": "bare-cec",
                    "scenario": "st35-bare-cec",
                },
            ],
        },
    )
    if getattr(request, "param", False):
        document = json.loads(claims.read_text())
        if request.param == "scenario":
            document["operations"][1].update(arm="dedicated", scenario="st36-io-slots")
        else:
            document["operations"].append(
                {
                    "operation": "command.run",
                    "arm": "dedicated",
                    "scenario": "st29-dedicated-pcie",
                }
            )
        write_json(claims, document)
    git(root, "add", str(claims))
    git(root, "commit", "-qm", "Select anonymous fixture arm claims")
    results = []
    for arm, state in producer_states():
        path = root / f"test-results-{arm}.json"
        run = runner._run_provenance(
            runner.SUBTASK_GROUPS[arm], arm, root, "V1_0", False
        )
        write_json(path, {"run": run, "results": state.results})
        assert runner._emit_observations(
            state,
            runner._observations_path(str(path)),
            ("V10R3M1060", "POWER10"),
            "V1_0",
            root,
        )
        results.append(path)
    if getattr(request, "param", False) == "scenario":
        results = results[:1]
    return root, claims, results


def test_promote_actual_dedicated_and_bare_producer_outputs(producer_candidate, capsys):
    root, claims, results = producer_candidate
    original = git(root, "rev-parse", "HEAD")
    catalog_path = root / "docs/capabilities/maturity.json"
    assert "command.run" not in {
        row["operation"] for row in json.loads(catalog_path.read_text())["operations"]
    }
    promoted = promotion.promote(
        root,
        claims,
        "promotion/producer",
        results,
        window_complete=True,
        recovery_confirmed=True,
    )
    assert promoted != original
    assert git(root, "rev-parse", "fixture-candidate") == original
    assert git(root, "status", "--porcelain") == ""
    snapshot = inventory.load_release_verification(
        root / "docs/capabilities/release-verification.json"
    )
    assert snapshot["pcie.list_dedicated_slots"]["verification"] == "current"
    assert snapshot["pcie.assign_dedicated_slot"]["verification"] == "current"
    catalog = json.loads(catalog_path.read_text())
    record = next(
        row
        for row in catalog["operations"]
        if row["operation"] == "pcie.assign_dedicated_slot"
    )
    assert record["evidence"][-1]["scenario"] == "st35-bare-cec"

    assert "command.run" not in {row["operation"] for row in catalog["operations"]}
    assert capsys.readouterr().err == (
        "promotion: omitted unclaimed observation for uncatalogued operation command.run\n"
    )


@pytest.mark.parametrize("producer_candidate", [True], indirect=True)
def test_reject_claimed_uncatalogued_operation(producer_candidate):
    root, claims, results = producer_candidate
    with pytest.raises(promotion.PromotionError, match="reviewed implementation"):
        promotion.promote(
            root,
            claims,
            "promotion/producer",
            results,
            window_complete=True,
            recovery_confirmed=True,
        )
    assert git(root, "branch", "--show-current") == "fixture-candidate"


@pytest.mark.parametrize("defect", ["unknown", "malformed", "stale", "mismatched"])
def test_reject_invalid_incidental_observation(producer_candidate, defect, capsys):
    root, claims, results = producer_candidate
    sidecar = runner._observations_path(str(results[0]))
    rows = json.loads(sidecar.read_text())
    row = next(row for row in rows if row["operation"] == "command.run")
    if defect == "unknown":
        row["operation"] = "fixture.unknown"
    elif defect == "malformed":
        row["private"] = "DO-NOT-COPY-RAW"
    elif defect == "stale":
        row["observation"]["observed_at"] = (
            datetime.now(UTC) - timedelta(days=91)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        row["observation"]["id"] = "st33-mismatched-label"
    write_json(sidecar, rows)
    with pytest.raises(promotion.PromotionError):
        promotion.promote(
            root,
            claims,
            "promotion/producer",
            results,
            window_complete=True,
            recovery_confirmed=True,
        )
    assert git(root, "branch", "--show-current") == "fixture-candidate"
    assert "omitted" not in capsys.readouterr().err


@pytest.mark.parametrize("producer_candidate", ["scenario"], indirect=True)
def test_reject_st31_relabelled_as_st36_before_branch_creation(producer_candidate):
    root, claims, results = producer_candidate
    sidecar = runner._observations_path(str(results[0]))
    rows = json.loads(sidecar.read_text())
    observation = next(
        row["observation"]
        for row in rows
        if row["operation"] == "pcie.assign_dedicated_slot"
    )
    assert observation["id"].startswith("st31-")
    observation["scenario"] = "st36-io-slots"
    write_json(sidecar, rows)
    with pytest.raises(promotion.PromotionError, match="invocation"):
        promotion.promote(
            root,
            claims,
            "promotion/producer",
            results,
            window_complete=True,
            recovery_confirmed=True,
        )
    assert git(root, "branch", "--show-current") == "fixture-candidate"


def test_accept_actual_st36_and_reject_relabelled_st31():
    from live_test import pcie

    state = runner.RunState()
    pcie._record_dedicated_additions(
        state,
        SimpleNamespace(
            evidence={"io-slots-add": (True, True, True)},
            drc_index="1",
        ),
        True,
    )
    observation = state.observations[0]
    assert state.results[0]["subtask"] == 36
    assert observation["observation"]["scenario"] == "st36-io-slots"
    run = {
        "group": "dedicated",
        "results": state.results,
        "finished_time": datetime.now(UTC),
    }
    sites = inventory.discover_observation_sites()
    promotion._invocation(observation, run, sites)
    observation["observation"]["scenario"] = "st29-dedicated-pcie"
    with pytest.raises(promotion.PromotionError, match="invocation"):
        promotion._invocation(observation, run, sites)


@pytest.mark.parametrize(
    "source",
    [
        "state.record_verified(31, 'label', operation=dynamic, scenario='st31-fixture')",
        "state.record_verified(dynamic, 'label', operation='command.run', scenario='st31-fixture')",
        "state.record_verified(31, 'label', operation='command.run', scenario=dynamic)",
    ],
)
def test_reject_unresolved_source_recording_identity(monkeypatch, source):
    monkeypatch.setattr(inventory.inspect, "getsource", lambda module: source)
    with pytest.raises(inventory.InventoryError):
        inventory._module_observation_sites(ModuleType("fixture"))


def test_reject_ambiguous_source_recording_identity(monkeypatch):
    module = ModuleType("fixture")
    monkeypatch.setattr(runner, "_SCENARIO_MODULES", [module])
    monkeypatch.setattr(
        inventory.inspect,
        "getsource",
        lambda module: (
            "state.record_verified(31, 'first', operation='command.run', scenario='st31-first')\n"
            "state.record_verified(31, 'second', operation='command.run', scenario='st31-second')\n"
        ),
    )
    with pytest.raises(inventory.InventoryError, match="ambiguous"):
        inventory.discover_observation_sites()
