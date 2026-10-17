"""ADR 0190/0195 engine: identity, journal, crash windows, guard and bounds (#1218)."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from hmcpctl.operations.logical import engine, store
from hmcpctl.operations.logical.engine import (
    BodyResult,
    Classification,
    EffectNotApplied,
    OperationFailed,
    OperationRequest,
)
from hmcpctl.operations.logical.store import OperationRefused


def _request(
    request_id="r1",
    *,
    agent="agent-a",
    connection="<default>",
    host="hmc.test",
    **arguments,
):
    return OperationRequest(
        "hmc_test_tool", agent, connection, host, request_id, arguments or {"lpar": "x"}
    )


class Writer:
    """A fake HMC write that counts calls and returns or raises as told."""

    def __init__(self, *, identity=None, raises=None):
        self.calls = 0
        self.identity = identity or {"uuid": "u1"}
        self.raises = raises

    async def __call__(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.identity


def _body(*writers, outcome="completed", kinds=None):
    kinds = kinds or [f"test.k{i}" for i in range(len(writers))]

    async def body(ctx):
        identities = []
        for index, (writer, kind) in enumerate(zip(writers, kinds, strict=True)):
            ctx.phase(f"step{index}")
            identities.append(await ctx.effect(f"step{index}", kind, "lpar:x", writer))
        return BodyResult(outcome, {"identities": [dict(i) for i in identities]})

    return body


def _reason(fn, *args, **kwargs) -> str:
    with pytest.raises(OperationRefused) as info:
        fn(*args, **kwargs)
    return info.value.reason


def _submit(request=None, body=None, **kwargs):
    kwargs.setdefault("wait_seconds", 10)
    return engine.submit(request or _request(), body or _body(Writer()), **kwargs)


def _crashed(*, effects=("test.k0",), partition=None):
    """Leave a running record whose intents never got outcomes, as a dead process does."""
    request_json, digest = engine.canonical_request(_request())
    with store.session() as conn, store.write_transaction(conn):
        store.insert_operation(
            conn,
            operation_id="0" * 32,
            agent_id="agent-a",
            request_id="r1",
            tool="hmc_test_tool",
            connection="<default>",
            host="hmc.test",
            digest=digest,
            request_json=request_json,
        )
        if partition:
            store.set_partition(conn, "0" * 32, *partition)
        for index, kind in enumerate(effects):
            store.record_intent(conn, "0" * 32, f"step{index}", kind, "lpar:x")


def _classify(status, *, identity=None, reason=None, raises=None):
    async def classifier(ctx, effect):
        if raises is not None:
            raise raises
        return Classification(status, identity, reason)

    return classifier


def test_completes_and_journals_the_effect():
    record = _submit()
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert [(e.key, e.status, e.identity) for e in record.effects] == [
        ("step0", "applied", {"uuid": "u1"})
    ]
    kinds = [e.kind for e in record.events]
    assert kinds.index("intent") < kinds.index("outcome")
    assert record.result == {"identities": [{"uuid": "u1"}]}
    assert record.next_actions == ()


def test_repeat_with_same_digest_does_not_rerun():
    writer = Writer()
    first = _submit(body=_body(writer))
    again = _submit(body=_body(writer))
    assert again.operation_id == first.operation_id
    assert writer.calls == 1


def test_conflicting_reuse_is_refused():
    _submit()
    assert _reason(_submit, _request(lpar="y")) == "request_conflict"


def test_excluded_keys_do_not_change_the_digest():
    plain = engine.canonical_request(_request(lpar="x"))
    noisy = engine.canonical_request(
        _request(lpar="x", continuation="resume", wait_seconds=5, hold_id="h")
    )
    assert plain == noisy


def test_other_connection_is_refused():
    _submit()
    assert _reason(_submit, _request(connection="lab")) == "connection_mismatch"


def test_same_profile_on_another_hmc_is_refused():
    _submit()
    assert _reason(_submit, _request(host="other.hmc"), continuation="resume") == (
        "connection_mismatch"
    )


@pytest.mark.parametrize("other", [{"connection": "lab"}, {"host": "other.hmc"}])
def test_connection_mismatch_does_not_disclose_the_recorded_connection(other):
    _submit()
    with pytest.raises(OperationRefused) as info:
        _submit(_request(**other), continuation="resume")
    assert info.value.reason == "connection_mismatch"
    assert "<default>" not in str(info.value)
    assert "hmc.test" not in str(info.value)


def test_other_agent_sees_not_found():
    _submit()
    assert (
        _reason(_submit, _request(agent="agent-b"), continuation="resume")
        == "not_found"
    )


@pytest.mark.parametrize(
    ("request_id", "continuation", "wait", "reason"),
    [
        ("bad id", "none", 5, "invalid_request_id"),
        ("", "none", 5, "invalid_request_id"),
        ("x" * 65, "none", 5, "invalid_request_id"),
        ("r1", "redo", 5, "invalid_continuation"),
        ("r1", "none", 601, "invalid_wait_seconds"),
        ("r1", "none", -1, "invalid_wait_seconds"),
        ("r1", "none", True, "invalid_wait_seconds"),
    ],
)
def test_invalid_inputs_are_refused(request_id, continuation, wait, reason):
    assert (
        _reason(
            _submit, _request(request_id), continuation=continuation, wait_seconds=wait
        )
        == reason
    )


def test_request_too_large():
    assert _reason(_submit, _request(lpar="x" * 70_000)) == "request_too_large"


def test_wait_zero_returns_running_and_work_continues():
    release = threading.Event()

    async def write():
        await asyncio.to_thread(release.wait, 10)
        return {"uuid": "u1"}

    record = _submit(body=_body(write), wait_seconds=0)
    assert record.state == "running"
    assert _reason(_submit, continuation="resume") == "running"
    release.set()
    engine.join(record.operation_id, 10)
    later = store.operation_status(
        agent_id="agent-a", connection="<default>", host="hmc.test"
    ).operations[0]
    assert (later.state, later.outcome) == ("terminal", "completed")


def _hold_first_end(monkeypatch):
    """Hold the first worker after its end state commits, before its thread exits."""
    real_end, ended, hold, held = engine._end, threading.Event(), threading.Event(), []

    def held_end(*args, **kwargs):
        real_end(*args, **kwargs)
        if not held:
            held.append(threading.current_thread())
            ended.set()
            hold.wait(10)

    monkeypatch.setattr(engine, "_end", held_end)
    return ended, hold, held


def test_finishing_worker_keeps_its_successor_registered(monkeypatch):
    ended, hold, held = _hold_first_end(monkeypatch)
    release, calls, joined_after_release = threading.Event(), [], []

    async def write():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("timeout")
        await asyncio.to_thread(release.wait, 10)
        return {"uuid": "u1"}

    monkeypatch.setitem(engine.CLASSIFIERS, "test.k0", _classify("not_applied"))
    try:
        first = _submit(body=_body(write), wait_seconds=0)
        assert ended.wait(10)
        joiner = threading.Thread(
            target=lambda: (
                engine.join(first.operation_id, 10),
                joined_after_release.append(release.is_set()),
            )
        )
        joiner.start()
        resumed = _submit(body=_body(write), continuation="resume", wait_seconds=0)
        assert resumed.state == "running"
        hold.set()
        held[0].join(10)
        assert engine._has_worker(first.operation_id)
        assert _submit(body=_body(write), wait_seconds=0).state == "running"
    finally:
        hold.set()
        release.set()
    joiner.join(10)
    assert joined_after_release == [True]
    engine.join(first.operation_id, 10)
    later = store.operation_status(
        agent_id="agent-a", connection="<default>", host="hmc.test"
    ).operations[0]
    assert (later.state, later.outcome, len(calls)) == ("terminal", "completed", 2)


def test_lock_recovery_skips_a_live_worker():
    started, release = threading.Event(), threading.Event()

    async def write():
        started.set()
        await asyncio.to_thread(release.wait, 10)
        return {"uuid": "u1"}

    try:
        first = _submit(body=_body(write), wait_seconds=0)
        assert started.wait(10)
        (store.state_dir() / store.LOCK_NAME).unlink()
        _submit(_request("r2"))
        with store.session() as conn:
            live = store.read_record(conn, first.operation_id)
        assert live.state == "running"
        assert live.effects[0].status == "intended"
    finally:
        release.set()
    engine.join(first.operation_id, 10)
    later = store.operation_status(
        agent_id="agent-a", connection="<default>", host="hmc.test", request_id="r1"
    ).operations[0]
    assert (later.state, later.outcome) == ("terminal", "completed")


def test_effect_not_applied_fails_the_operation():
    record = _submit(body=_body(Writer(raises=EffectNotApplied("HMC refused"))))
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert record.effects[0].status == "not_applied"
    assert "HMC refused" in record.warnings[0]


def test_effect_with_unknown_outcome_needs_attention():
    record = _submit(body=_body(Writer(raises=RuntimeError("socket closed"))))
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert record.effects[0].status == "uncertain"
    assert "RuntimeError: socket closed" in record.warnings[0]
    assert record.next_actions == ("resume", "abandon")


def test_warning_text_is_bounded(monkeypatch):
    record = _submit(body=_body(Writer(raises=RuntimeError("x" * 2000))))
    assert record.warnings[0] == ("RuntimeError: " + "x" * 2000)[:512]
    monkeypatch.setitem(
        engine.CLASSIFIERS, "test.k0", _classify("needs_attention", reason="y" * 2000)
    )
    resumed = _submit(body=_body(Writer()), continuation="resume")
    assert resumed.warnings[0] == ("effect step0: " + "y" * 2000)[:512]
    monkeypatch.setitem(
        engine.CLASSIFIERS, "test.k0", _classify("applied", raises=OSError("z" * 2000))
    )
    again = _submit(body=_body(Writer()), continuation="resume")
    assert len(again.warnings[0]) == 512
    assert again.warnings[0].endswith("zz); retry resume")


def test_body_defect_before_any_effect_needs_attention():
    async def body(ctx):
        raise KeyError("missing")

    record = _submit(body=body)
    assert (record.state, record.outcome) == ("paused", "needs_attention")


def test_body_that_swallows_an_effect_error_needs_attention():
    async def body(ctx):
        try:
            await ctx.effect(
                "attach", "test.k", "lpar:x", Writer(raises=TimeoutError())
            )
        except TimeoutError:
            pass
        return BodyResult("completed")

    record = _submit(body=body)
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert "attach" in record.warnings[0]


def test_failed_end_write_is_recovered_in_the_same_process(monkeypatch):
    real = store.set_state
    failures = []

    def flaky(conn, operation_id, state, outcome, **kwargs):
        if state == "terminal" and not failures:
            failures.append(1)
            raise sqlite3.OperationalError("database or disk is full")
        return real(conn, operation_id, state, outcome, **kwargs)

    monkeypatch.setattr(store, "set_state", flaky)
    writer = Writer()
    stuck = _submit(body=_body(writer))
    assert stuck.state == "running"
    record = _submit(body=_body(writer), continuation="abandon")
    assert (record.state, record.outcome, writer.calls) == ("terminal", "abandoned", 1)


def test_intent_left_by_a_failed_settle_is_reconciled_on_resume(monkeypatch):
    real, failures = store.record_outcome, []

    def flaky(conn, operation_id, key, status, identity):
        if status == "uncertain" and not failures:
            failures.append(1)
            raise sqlite3.OperationalError("database or disk is full")
        return real(conn, operation_id, key, status, identity)

    monkeypatch.setattr(store, "record_outcome", flaky)
    writer = Writer(raises=RuntimeError("timeout"))
    stuck = _submit(body=_body(writer))
    assert (stuck.outcome, stuck.effects[0].status) == ("needs_attention", "intended")
    monkeypatch.setitem(engine.CLASSIFIERS, "test.k0", _classify("not_applied"))
    writer.raises = None
    record = _submit(body=_body(writer), continuation="resume")
    assert (record.state, record.outcome, writer.calls) == ("terminal", "completed", 2)


def test_recorded_exposes_the_journal():
    seen = []

    async def body(ctx):
        seen.append(ctx.recorded("step0"))
        await ctx.effect("step0", "test.k0", "lpar:x", Writer())
        seen.append(ctx.recorded("step0").status)
        return BodyResult("completed")

    _submit(body=body)
    assert seen == [None, "applied"]


def test_operation_failed_is_terminal():
    async def body(ctx):
        raise OperationFailed("precondition failed")

    record = _submit(body=body)
    assert (record.state, record.outcome) == ("terminal", "failed")


def test_resume_applied_classifier_skips_the_write(monkeypatch):
    _crashed()
    store.acquire_execution_lock()
    monkeypatch.setitem(
        engine.CLASSIFIERS, "test.k0", _classify("applied", identity={"uuid": "live"})
    )
    writer = Writer()
    record = _submit(body=_body(writer), continuation="resume")
    assert writer.calls == 0
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert record.result == {"identities": [{"uuid": "live"}]}


def test_resume_not_applied_rewrites_once(monkeypatch):
    _crashed()
    monkeypatch.setitem(engine.CLASSIFIERS, "test.k0", _classify("not_applied"))
    writer = Writer()
    record = _submit(body=_body(writer), continuation="resume")
    assert writer.calls == 1
    assert record.outcome == "completed"


@pytest.mark.parametrize(
    "classifier",
    [
        _classify("needs_attention", reason="owner changed to agent-z"),
        _classify("applied", raises=ConnectionError("HMC unreachable")),
        None,
    ],
)
def test_resume_that_cannot_classify_pauses_without_writing(monkeypatch, classifier):
    _crashed()
    if classifier is not None:
        monkeypatch.setitem(engine.CLASSIFIERS, "test.k0", classifier)
    writer = Writer()
    record = _submit(body=_body(writer), continuation="resume")
    assert writer.calls == 0
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert record.effects[0].status == "uncertain"
    assert "step0" in record.warnings[0]


def test_owner_change_is_reported_not_adopted(monkeypatch):
    _crashed()
    monkeypatch.setitem(
        engine.CLASSIFIERS,
        "test.k0",
        _classify("needs_attention", reason="owner changed"),
    )
    record = _submit(body=_body(Writer()), continuation="resume")
    assert "owner changed" in record.warnings[0]
    assert record.effects[0].identity is None


def test_crash_before_any_effect_replays_from_the_start():
    _crashed(effects=())
    writer = Writer()
    record = _submit(body=_body(writer), continuation="resume")
    assert writer.calls == 1
    assert record.outcome == "completed"


def test_applied_effect_is_not_rewritten_on_replay(monkeypatch):
    first, second = Writer(), Writer(raises=RuntimeError("timeout"))
    body = _body(first, second)
    assert _submit(body=body).outcome == "needs_attention"
    monkeypatch.setitem(engine.CLASSIFIERS, "test.k1", _classify("not_applied"))
    second.raises = None
    record = _submit(body=body, continuation="resume")
    assert (first.calls, second.calls) == (1, 2)
    assert record.outcome == "completed"


def test_abandon_writes_nothing_and_releases_the_guard():
    _crashed(partition=("sys", "lpar"))
    writer = Writer()
    record = _submit(body=_body(writer), continuation="abandon")
    assert (record.state, record.outcome, writer.calls) == ("terminal", "abandoned", 0)

    async def guarded(ctx):
        ctx.guard("sys", "lpar")
        return BodyResult("completed")

    assert _submit(_request("r2"), guarded).outcome == "completed"


def test_continuation_outside_the_table_is_refused():
    _crashed()
    assert _reason(_submit, continuation="boot") == "continuation_not_accepted"


def test_terminal_record_is_returned_for_any_continuation():
    done = _submit()
    again = _submit(continuation="resume")
    assert again == done


def test_boot_continues_a_ready_to_boot_operation():
    seen = []

    async def body(ctx):
        seen.append(ctx.continuation)
        return BodyResult(
            "boot_started" if ctx.continuation == "boot" else "ready_to_boot"
        )

    paused = _submit(body=body)
    assert (paused.state, paused.outcome, paused.next_actions) == (
        "paused",
        "ready_to_boot",
        ("boot", "abandon"),
    )
    assert (
        _reason(_submit, body=body, continuation="resume")
        == "continuation_not_accepted"
    )
    booted = _submit(body=body, continuation="boot")
    assert (booted.state, booted.outcome, seen) == (
        "terminal",
        "boot_started",
        ["none", "boot"],
    )


def test_guard_refuses_a_second_operation_on_the_partition():
    async def holder(ctx):
        ctx.guard("sys", "lpar")
        ctx.guard("sys", "lpar")
        return BodyResult("ready_to_boot")

    first = _submit(_request("r1"), holder)
    second = _submit(_request("r2"), holder)
    assert (second.state, second.outcome) == ("terminal", "failed")
    assert first.operation_id in second.warnings[0]


def test_guard_cannot_move_to_another_partition():
    async def body(ctx):
        ctx.guard("sys", "a")
        ctx.guard("sys", "b")
        return BodyResult("completed")

    record = _submit(body=body)
    assert record.outcome == "needs_attention"
    assert "already guards" in record.warnings[0]


def test_ledger_add_is_idempotent_across_replays(monkeypatch):
    calls = []

    async def body(ctx):
        ctx.ledger_add("disk", "disk", "sys", "lpar", {"name": "d1"})
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first run dies")
        return BodyResult("completed")

    _submit(body=body)
    _submit(body=body, continuation="resume")
    with store.session() as conn:
        assert conn.execute("SELECT count(*) FROM ledger").fetchone()[0] == 1


def test_effect_limit(monkeypatch):
    # Exercise the bound without hundreds of journal writes racing the submit wait.
    monkeypatch.setattr(store, "MAX_EFFECTS", 2)

    async def body(ctx):
        for n in range(store.MAX_EFFECTS + 1):
            await ctx.effect(f"e{n}", "test.k", "t", Writer())
        return BodyResult("completed")

    record = _submit(body=body)
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert len(record.effects) == store.MAX_EFFECTS
    assert "effect_limit" in record.warnings[0]


def test_result_too_large_needs_attention():
    async def body(ctx):
        return BodyResult("completed", {"blob": "x" * (store.MAX_RESULT_BYTES + 1)})

    assert _submit(body=body).outcome == "needs_attention"


@pytest.mark.parametrize(
    ("key", "kind", "target"),
    [("Bad Key", "test.k", "t"), ("k", "Bad-Kind", "t"), ("k", "test.k", "t" * 257)],
)
def test_malformed_effect_names_are_defects(key, kind, target):
    async def body(ctx):
        await ctx.effect(key, kind, target, Writer())
        return BodyResult("completed")

    record = _submit(body=body)
    assert record.outcome == "needs_attention"
    assert record.effects == ()


def test_oversized_identity_is_uncertain():
    record = _submit(body=_body(Writer(identity={"blob": "x" * 5_000})))
    assert record.effects[0].status == "uncertain"


def test_register_classifier_rejects_a_duplicate(monkeypatch):
    monkeypatch.setattr(engine, "CLASSIFIERS", {})
    engine.register_classifier("test.k", _classify("applied"))
    with pytest.raises(ValueError, match="already registered"):
        engine.register_classifier("test.k", _classify("applied"))


def test_a_lock_held_elsewhere_refuses_submit(monkeypatch):
    def held(live_operation_ids=()):
        raise OperationRefused(
            "execution_lock_held", "process 7 runs logical operations"
        )

    monkeypatch.setattr(store, "acquire_execution_lock", held)
    assert _reason(_submit) == "execution_lock_held"


def test_a_lost_store_refuses_continuations():
    _submit()
    (store.state_dir() / store.DB_NAME).unlink()
    assert _reason(_submit, continuation="resume") == "store_lost"


@pytest.mark.parametrize("continuation", ["resume", "abandon"])
def test_a_continuation_from_another_tool_is_refused(continuation):
    async def pauses(ctx):
        return BodyResult("needs_attention")

    _submit(body=pauses)
    writer = Writer()
    other = OperationRequest(
        "hmc_other_tool", "agent-a", "<default>", "hmc.test", "r1", {}
    )
    reason = _reason(_submit, other, _body(writer), continuation=continuation)
    assert reason == "request_conflict"
    assert writer.calls == 0
    record = store.operation_status(
        agent_id="agent-a", connection="<default>", host="hmc.test"
    ).operations[0]
    assert (record.state, record.outcome) == ("paused", "needs_attention")


def test_a_worker_that_fails_to_start_is_recoverable(monkeypatch):
    def refuse(self):
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patch:
        patch.setattr(engine.threading.Thread, "start", refuse)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            _submit()
    record = _submit(continuation="abandon")
    assert (record.state, record.outcome) == ("terminal", "abandoned")


def test_recorded_arguments_reads_without_creating_the_store():
    assert engine.recorded_arguments("agent-a", "r1", "hmc_test_tool") is None
    assert not store.state_dir().exists()


def test_recorded_arguments_returns_the_tools_own_record():
    _submit(_request(lpar="p1", action="stop"))
    assert engine.recorded_arguments("agent-a", "r1", "hmc_test_tool") == {
        "lpar": "p1",
        "action": "stop",
    }
    assert engine.recorded_arguments("agent-a", "r1", "hmc_other_tool") is None
    assert engine.recorded_arguments("agent-b", "r1", "hmc_test_tool") is None


def test_recorded_arguments_refuses_a_malformed_request_id():
    assert _reason(engine.recorded_arguments, "agent-a", "bad id", "t") == (
        "invalid_request_id"
    )
