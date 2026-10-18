"""The audit record's rendering, its bounds, and its sink.

Covers docs/workflow/specs/2026-08-19-authorization-audit-events-design.md and
docs/workflow/specs/2026-08-19-non-blocking-stderr-diagnostics-design.md; the
decision records are docs/adr/0040-authorization-audit-events.md and
docs/adr/0043-non-blocking-stderr-diagnostics.md.

Logging isolation comes from the autouse ``isolate_audit_logging`` fixture in
``tests/conftest.py``. Nothing here may install a sink without it.

Spec test -> node id. This map is checked by
``test_every_spec_numbered_test_named_in_the_header_still_exists`` — keep it true.

  1   test_a_permitted_record_carries_every_field_in_order
  2   test_output_is_one_ascii_line_whatever_the_caller_sends
  3   test_a_long_selector_value_is_truncated_to_the_bound
  4   test_a_long_agent_id_is_truncated_to_the_bound
  5   test_a_non_string_connection_token_is_never_rendered
  6   test_targets_and_resolved_are_null_when_nothing_was_resolved
  6b  test_a_profile_named_unresolved_is_indistinguishable_from_the_sentinel
  7   test_attribution_is_unverified_and_sourced_when_the_env_is_unset
  8   test_reasons_matches_the_literal
  8a  test_only_audit_sink_resolves_the_audit_logger
  9   test_a_record_reaches_stderr_and_not_stdout
  10  test_the_sink_does_not_propagate_to_an_ancestor_handler
  11,12,13 test_a_stream_that_cannot_be_written_drops_and_says_so
           (parametrized over absent, closed, broken, and unforeseen)
  14  test_install_is_idempotent_and_defers_to_what_the_operator_set
  14a test_a_preattached_stdout_handler_is_deferred_to
  14b test_the_singletons_render_as_states_rather_than_raising
  15  test_the_line_equals_the_message_and_records_do_not_share_a_line
  15  test_the_handler_issues_one_write_per_record

#269 / ADR 0043, the sink's liveness contract. Each drives a real filled pipe:

  269a test_an_undrained_pipe_does_not_block_the_submitting_thread
  269b test_an_overflowing_queue_reports_what_it_lost
  269c test_the_drop_marker_is_one_ascii_line_of_the_same_grammar
  269f test_a_closed_sink_writes_nothing_more_and_still_counts_the_loss
  269g test_a_marker_that_cannot_be_written_is_still_owed
  269d test_shutdown_delivers_everything_queued_when_the_destination_is_read
  269e test_shutdown_returns_even_when_the_destination_never_drains

#323 / ADR 0051, a second producer sharing the sink:

  test_a_handler_without_a_formatter_renders_the_message_and_nothing_else
  test_a_handler_with_a_formatter_carries_the_traceback_to_the_sink
  test_a_foreign_rendering_cannot_forge_a_record_on_this_stream
  test_the_audit_records_own_grammar_is_untouched_by_that_formatter
  test_a_multi_line_rendering_reaches_the_stream_in_one_write
  test_two_producers_share_one_bound_and_the_count_still_adds_back

Not spec-numbered, each pinning something a review round found:

  test_resolved_connection_is_bound_to_the_sentinel_that_owns_it
  test_events_matches_the_literal_and_every_emitter_uses_it
  test_import_is_inert_until_sink_installation          (#272, fresh interpreter)
  test_an_unconfigured_logger_still_reaches_last_resort (#272's other half)
  test_the_override_record_carries_the_hmc_host       (#271)
  test_an_empty_override_host_renders_empty_and_is_bounded (#271)
  test_the_denial_record_names_both_halves_of_the_refusal (#467, ADR 0100)
  test_a_malformed_token_denial_records_a_null_owner   (#467)
  test_the_denial_record_is_bounded_and_escaped        (#467)
  test_a_foreign_writers_bad_record_does_not_raise_into_them
  test_the_install_record_names_the_target_and_the_log_path (#469, ADR 0102)
  test_the_install_record_is_emitted_at_warning        (#469)
  test_the_install_record_is_bounded_and_escaped       (#469)
  test_a_long_partition_records_a_log_path_that_does_not_exist (#469)
  test_the_tls_record_carries_host_and_source          (#379)
  test_an_empty_tls_host_renders_empty_and_is_bounded  (#379)
  test_a_long_tls_source_stays_bounded                 (#379)
  test_the_power_guard_record_carries_the_effective_value (#533, ADR 0107)
  test_the_power_guard_record_is_emitted_at_warning       (#533, ADR 0107)

#543, the attribution read's agreement with the loader that stamps the LPARs:

  543a test_a_case_variant_agent_id_reaches_the_record_and_the_stamp_alike
  543b test_the_last_agent_id_casing_in_the_environment_is_the_one_recorded
  543c test_the_audit_env_fold_agrees_with_the_configs
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import get_args

import pytest

from hmcpctl.audit import records as audit
from hmcpctl.audit import sink as audit_sink

SENTINEL = "SENTINEL-DO-NOT-LOG-9c1f"


def _capture() -> list[str]:
    """Attach a list-collecting handler and return the list it fills."""
    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.addHandler(_Collect())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return lines


def _flush() -> None:
    """Wait for the sink to settle. Delivery is asynchronous since ADR 0043.

    Every assertion that reads what reached the *stream* — as opposed to what
    reached the logger — has to wait for the writer thread, and has to fail rather
    than time out silently if it never lands.
    """
    assert audit_sink._sink().drain(audit_sink._DRAIN_TIMEOUT), (
        "the sink did not settle: a submitted line neither landed nor dropped"
    )


def _one(lines: list[str]) -> dict:
    assert len(lines) == 1, f"expected exactly one record, got {len(lines)}"
    return json.loads(lines[0])


def _authorization(**overrides) -> dict:
    """Emit one authorization record with sane defaults and return it parsed."""
    lines = _capture()
    kwargs = {
        "policy": "lab-only",
        "tool": "hmc_power_off_lpar",
        "effect": "destructive",
        "decision": "allow",
        "reason": "permitted",
        "token": "lab",
        "resolved": audit.resolved_connection("lab"),
        "targets": (
            audit.AuditTarget(
                kind="lpar",
                argument="lpar_name_or_uuid",
                state="present",
                value="db-01",
            ),
        ),
    }
    kwargs.update(overrides)
    audit.record_authorization(**kwargs)
    return _one(lines)


def test_every_spec_numbered_test_named_in_the_header_still_exists():
    """The header maps spec numbers to node ids; this makes that map load-bearing.

    Added because it was not. A slice replacement in `22ee201` — rewriting the
    span between two functions — silently swallowed three tests that sat between
    them, including the only pin on #272's import-time ``propagate = False``. The
    suite stayed green at 2011 passed, so deleting a security fix reddened
    nothing, and the loss reached a PR before the orchestrator caught it by
    mutation.

    A deletion is invisible to every other test by construction: nothing fails
    when an assertion simply stops existing. So the header is the inventory and
    this compares it against reality.
    """
    source = Path(__file__).read_text()
    header = source.split('"""')[1]
    named = set(re.findall(r"\b(test_[a-z_0-9]+)", header))
    defined = set(re.findall(r"^def (test_[a-z_0-9]+)", source, re.MULTILINE))
    assert named, "the header must map spec numbers to node ids"
    missing = named - defined
    assert not missing, f"named in the header but no longer defined: {sorted(missing)}"


def test_a_permitted_record_carries_every_field_in_order():
    """Spec 1."""
    lines = _capture()
    audit.record_authorization(
        policy="lab-only",
        tool="hmc_power_off_lpar",
        effect="destructive",
        decision="allow",
        reason="permitted",
        token="lab",
        resolved=audit.resolved_connection("lab"),
        targets=(
            audit.AuditTarget(
                kind="lpar",
                argument="lpar_name_or_uuid",
                state="present",
                value="db-01",
            ),
        ),
    )
    record = _one(lines)
    assert list(record) == [
        "time",
        "event",
        "policy",
        "tool",
        "effect",
        "decision",
        "reason",
        "connection",
        "targets",
        "attribution",
    ]
    assert record["event"] == "authorization"
    assert record["policy"] == "lab-only"
    assert record["tool"] == "hmc_power_off_lpar"
    assert record["effect"] == "destructive"
    assert record["decision"] == "allow"
    assert record["reason"] == "permitted"
    assert record["connection"] == {
        "state": "present",
        "selector": "lab",
        "resolved": "lab",
    }
    assert record["targets"] == [
        {
            "kind": "lpar",
            "argument": "lpar_name_or_uuid",
            "state": "present",
            "value": "db-01",
        }
    ]
    assert record["time"].endswith("+00:00")


def test_output_is_one_ascii_line_whatever_the_caller_sends():
    """Spec 2. A caller cannot forge a line, move a cursor, or reorder one."""
    hostile = "a\nb\rc\td\x1be‮f g"
    lines = _capture()
    audit.record_authorization(
        policy="p",
        tool="t",
        effect="mutate",
        decision="deny",
        reason="target-not-granted",
        token=hostile,
        resolved=audit.resolved_connection(""),
        targets=(
            audit.AuditTarget(
                kind="lpar",
                argument="lpar_name_or_uuid",
                state="present",
                value=hostile,
            ),
        ),
    )
    assert len(lines) == 1
    line = lines[0]
    assert "\n" not in line and "\r" not in line
    assert line.isascii(), "ensure_ascii=True must leave no non-ASCII byte"
    assert "‮" not in line and " " not in line
    record = json.loads(line)
    assert record["connection"]["selector"] == hostile


def test_a_long_selector_value_is_truncated_to_the_bound():
    """Spec 3. Both the connection token and a target value are bounded."""
    long = "A" * 500
    record = _authorization(
        token=long,
        targets=(
            audit.AuditTarget(
                kind="lpar", argument="lpar_name_or_uuid", state="present", value=long
            ),
        ),
    )
    assert len(record["connection"]["selector"]) == audit.MAX_VALUE_LENGTH == 128
    assert len(record["targets"][0]["value"]) == audit.MAX_VALUE_LENGTH
    assert not record["targets"][0]["value"].endswith("…"), "no truncation marker"


def test_a_long_agent_id_is_truncated_to_the_bound(monkeypatch):
    """Spec 4. The raw environment value, unvalidated but bounded."""
    monkeypatch.setenv(audit.ATTRIBUTION_ENV, "B" * 500)
    record = _authorization()
    assert len(record["attribution"]["claim"]) == audit.MAX_VALUE_LENGTH


def test_a_non_string_connection_token_is_never_rendered():
    """Spec 5. An arbitrary object's repr is not the caller's token."""

    class Hostile:
        def __repr__(self) -> str:  # pragma: no cover - must never be called
            return SENTINEL

    record = _authorization(token=Hostile())
    assert record["connection"]["state"] == "unreadable"
    assert record["connection"]["selector"] is None
    assert SENTINEL not in json.dumps(record)


def test_targets_and_resolved_are_null_when_nothing_was_resolved():
    """Spec 6. `null` means not evaluated; `[]` means the tool declares none."""
    unreadable = _authorization(
        reason="configuration-unreadable", decision="deny", resolved=None, targets=None
    )
    assert unreadable["targets"] is None
    assert unreadable["connection"]["resolved"] is None

    selectorless = _authorization(targets=())
    assert selectorless["targets"] == []

    for empty in (None, ""):
        assert _authorization(token=empty, resolved=audit.resolved_connection(None))[
            "connection"
        ] == {"state": "absent", "selector": None, "resolved": "<default>"}, (
            f"an absent token must render a null selector, got {empty!r}"
        )
    assert (
        _authorization(token="nope", resolved=audit.resolved_connection(""))[
            "connection"
        ]["resolved"]
        == "<unresolved>"
    )


def test_resolved_connection_is_bound_to_the_sentinel_that_owns_it():
    """`audit` restates `""` as a literal; `config` owns the value.

    `audit.py` may import nothing from the package, so the coupling cannot be an
    import — but this test may, and without it a change to `UNRESOLVED` would
    silently make every unresolved token render as a profile key instead. The
    target half avoids the same drift by putting `audit_state` in `target_scope`;
    this is the connection half's equivalent, paid for in a test rather than a
    dependency.
    """
    from hmcpctl.config import UNRESOLVED_CONNECTION

    assert audit.resolved_connection(UNRESOLVED_CONNECTION) == (
        audit.UNRESOLVED_RENDERING
    )
    assert audit.resolved_connection(None) == audit.DEFAULT_RENDERING
    assert audit.resolved_connection("lab") == "lab"


def test_a_profile_named_unresolved_is_indistinguishable_from_the_sentinel():
    """Spec 6b. A reserved rendering shares a string space with legal keys."""
    named = audit.resolved_connection(audit.UNRESOLVED_RENDERING)
    nothing = audit.resolved_connection("")
    assert named == nothing == audit.UNRESOLVED_RENDERING


def test_attribution_is_unverified_and_sourced_when_the_env_is_unset(monkeypatch):
    """Spec 7."""
    monkeypatch.delenv(audit.ATTRIBUTION_ENV, raising=False)
    record = _authorization()
    assert record["attribution"] == {
        "claim": None,
        "source": "environment:HMC_AGENT_ID",
        "verified": False,
    }


def test_a_case_variant_agent_id_reaches_the_record_and_the_stamp_alike(monkeypatch):
    """#543. One export, two halves of the trail, and they have to agree.

    ``HMCConfig`` leaves pydantic-settings' ``case_sensitive`` at its ``False``
    default, so ``hmc_agent_id=alice`` reaches ``config.agent_id``: the
    ``X-Audit-Memento`` header goes out as ``hmcpctl:alice`` and every LPAR the
    process creates carries the ADR 0011 ownership token for ``alice``. The
    authorization record read the same variable exact-case and saw nothing, so
    the records said nobody acted while the partitions said ``alice`` did.

    Both halves are driven here rather than one, because the defect was never
    visible in either alone.
    """
    from hmcpctl.config import HMCConfig

    for spelling in ("HMC_AGENT_ID", "hmc_agent_id", "Hmc_Agent_Id"):
        monkeypatch.delenv(spelling, raising=False)
    monkeypatch.setenv("hmc_agent_id", "alice")

    stamped = HMCConfig(host="h", user="u", password="p").agent_id
    record = _authorization()

    assert stamped == "alice"
    assert record["attribution"] == {
        "claim": stamped,
        "source": "environment:HMC_AGENT_ID",
        "verified": False,
    }


def test_the_last_agent_id_casing_in_the_environment_is_the_one_recorded(monkeypatch):
    """#543. Two casings at once resolve the way the loader resolves them.

    pydantic-settings folds the whole environment into one case-blind mapping in
    ``os.environ`` order, so the later entry overwrites the earlier and the exact
    spelling gets no precedence. Preferring the canonical spelling here would put
    an empty ``HMC_AGENT_ID`` in the record while ``hmc_agent_id`` stamped the
    partitions — the same divergence in the other direction.
    """
    from hmcpctl.config import HMCConfig

    for spelling in ("HMC_AGENT_ID", "hmc_agent_id", "Hmc_Agent_Id"):
        monkeypatch.delenv(spelling, raising=False)
    monkeypatch.setenv("HMC_AGENT_ID", "first")
    monkeypatch.setenv("hmc_agent_id", "second")

    stamped = HMCConfig(host="h", user="u", password="p").agent_id
    assert _authorization()["attribution"]["claim"] == stamped


@pytest.mark.parametrize(
    "spellings",
    [
        (),
        (("HMC_AGENT_ID", "canonical"),),
        (("hmc_agent_id", "lower"),),
        (("Hmc_Agent_Id", "mixed"),),
        (("HMC_AGENT_ID", ""), ("hmc_agent_id", "nonempty")),
        (("hmc_agent_id", "nonempty"), ("HMC_AGENT_ID", "")),
        # Dotless i: `str.lower()` leaves it alone and neither fold matches, so
        # both copies must return None. `str.upper()` turns it into `HMC_AGENT_ID`
        # and would return the value — a name the loader never reads, recorded as
        # a claimant the ownership stamp does not carry. Every ASCII case above
        # passes under either fold direction, so without this the parametrization
        # cannot see the half of the rule both docstrings call load-bearing.
        (("hmc_agent_ıd", "dotless-i"),),
    ],
)
def test_the_audit_env_fold_agrees_with_the_configs(monkeypatch, spellings):
    """#543. The two folds are one rule, and this is what keeps them one.

    ``audit`` imports nothing from ``hmcpctl`` by design, so it cannot call
    ``config.env_var_value`` and carries its own copy of the fold. A second
    mechanism for one job only stays honest if something compares them, and
    nothing else does — the copy is invisible from either side.
    """
    from hmcpctl.config import env_var_value

    for spelling in ("HMC_AGENT_ID", "hmc_agent_id", "Hmc_Agent_Id"):
        monkeypatch.delenv(spelling, raising=False)
    for name, value in spellings:
        monkeypatch.setenv(name, value)

    assert audit._env_var_value(audit.ATTRIBUTION_ENV) == env_var_value(
        audit.ATTRIBUTION_ENV
    )


def test_decisions_matches_the_literal():
    """The authorization outcome is closed, and a checker can see that (#518).

    Pinned here beside its siblings rather than only derived: `DECISIONS` is what the
    document guard compares the field table against, so without this a widened
    `Decision` would move the vocabulary and the guard's own expectation together.
    """
    assert audit.DECISIONS == frozenset(get_args(audit.Decision))
    assert audit.DECISIONS == {"allow", "deny"}


def test_reasons_matches_the_literal():
    """Spec 8. The vocabulary is closed."""
    assert audit.REASONS == frozenset(get_args(audit.Reason))
    assert audit.REASONS == {
        "permitted",
        "configuration-unreadable",
        "connection-not-granted",
        "target-selector-unreadable",
        "target-unboundable",
        "target-selector-absent",
        "target-not-granted",
    }


def test_events_matches_the_literal_and_every_emitter_uses_it():
    """The `event` vocabulary is restated here, and a checker can see the alias.

    ADR 0043 added `records-dropped`, which is the sink's own event rather than a
    decision, so it is emitted by the sink and not through the logger — which is
    why it is reached here through `_drop_marker` and the others are not.

    `EVENTS` is derived from the `Literal`, so the two assertions below cannot
    disagree by accident — but the restated set and the emitter walk are written
    by hand, which is what makes adding a member an edit here (ADR 0100 §1).
    """
    assert audit.EVENTS == frozenset(get_args(audit.Event))
    assert audit.EVENTS == {
        "authorization",
        "console-write",
        "install-attempted",
        "install-submitted",
        "lpar-power-off",
        "ownership-denied",
        "ownership-override",
        "power-ownership-guard",
        "records-dropped",
        "tls-verification-disabled",
    }

    lines = _capture()
    audit.record_ownership_override(system="s", lpar="l", host="hmc.test", agent_id="a")
    emitted = {json.loads(lines[0])["event"]}
    emitted.add(_authorization()["event"])
    lines = _capture()
    audit.record_ownership_denied(
        operation="lpar-mutation",
        denial="foreign-owner",
        system="s",
        lpar="l",
        owner="other",
        host="hmc.test",
        agent_id="a",
    )
    emitted.add(_one(lines)["event"])
    lines = _capture()
    audit.record_install_submitted(
        system="s", partition="p", pid=123, log_path="/l", host="hmc.test", agent_id="a"
    )
    emitted.add(_one(lines)["event"])
    lines = _capture()
    audit.record_tls_verification_disabled(host="hmc.test", source="field-default")
    emitted.add(_one(lines)["event"])
    lines = _capture()
    audit.record_install_attempted(
        system="s", partition="p", log_path="/l", host="hmc.test", agent_id="a"
    )
    emitted.add(_one(lines)["event"])
    lines = _capture()
    audit.record_power_ownership_guard(
        connection="lab",
        authorize_power_operations=True,
        source="profile",
        detail=None,
    )
    emitted.add(_one(lines)["event"])
    lines = _capture()
    audit.record_console_write(
        system="s",
        lpar="l",
        host="hmc.test",
        mode="shared",
        input_kind="raw",
        length=1,
        agent_id="a",
    )
    emitted.add(_one(lines)["event"])
    lines = _capture()
    audit.record_lpar_power_off(
        lpar="l",
        host="hmc.test",
        operation="dumprestart",
        immediate=False,
        restart=False,
        agent_id="a",
    )
    emitted.add(_one(lines)["event"])
    emitted.add(json.loads(audit_sink._drop_marker(1))["event"])
    assert emitted == audit.EVENTS, "every declared event must be reachable"


def test_the_console_write_record_carries_metadata_and_no_content():
    """ADR 0176 decision 6. A console write is recorded at WARNING before its bytes
    are queued, and the record names the target, the mode, the input kind and the
    length, but never the written bytes.
    """
    levels: list[int] = []

    class _Level(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            levels.append(record.levelno)

    lines = _capture()
    logging.getLogger(audit_sink.AUDIT_LOGGER_NAME).addHandler(_Level())
    audit.record_console_write(
        system="sys-a",
        lpar="lp-01",
        host="hmc.test",
        mode="exclusive",
        input_kind="sysrq",
        length=2,
        agent_id="agent-7",
    )
    record = _one(lines)
    assert list(record) == [
        "time",
        "event",
        "system",
        "lpar",
        "host",
        "mode",
        "input_kind",
        "length",
        "attribution",
    ]
    assert record["event"] == "console-write"
    assert (record["system"], record["lpar"], record["host"]) == (
        "sys-a",
        "lp-01",
        "hmc.test",
    )
    assert (record["mode"], record["input_kind"], record["length"]) == (
        "exclusive",
        "sysrq",
        2,
    )
    assert record["attribution"] == {
        "claim": "agent-7",
        "source": "config:agent_id",
        "verified": False,
    }
    assert levels == [logging.WARNING]


def test_the_power_off_record_bounds_its_text_and_keeps_its_booleans():
    """ADR 0180. Caller-reachable text takes the shared bound; the flags stay booleans."""
    lines = _capture()
    audit.record_lpar_power_off(
        lpar="x" * 300,
        host="hmc.test",
        operation="o" * 300,
        immediate=True,
        restart=False,
        agent_id="agent-7",
    )
    record = _one(lines)
    assert record["event"] == "lpar-power-off"
    assert len(record["lpar"]) == len(record["operation"]) == audit.MAX_VALUE_LENGTH
    assert (record["immediate"], record["restart"]) == (True, False)


def test_the_override_record_carries_the_hmc_host():
    """#271. The highest-consequence event names which HMC it applied to.

    `system` and `lpar` are names that repeat across a fleet; `host` is the
    ``HMCConfig.host`` of the client whose config supplied the recorded
    ``agent_id``, and it is its own field rather than a ``connection`` arm —
    a hostname is not an access-policy connection selector.
    """
    lines = _capture()
    audit.record_ownership_override(
        system="sys-a", lpar="db-01", host="hmc.test", agent_id="agent-7"
    )
    record = _one(lines)
    assert list(record) == [
        "time",
        "event",
        "system",
        "lpar",
        "host",
        "attribution",
    ]
    assert record["host"] == "hmc.test"


def test_an_empty_override_host_renders_empty_and_is_bounded():
    """`HMCConfig.host` defaults to "", which is what renders, and the
    caller-supplied bound applies to it like to every other field."""
    lines = _capture()
    audit.record_ownership_override(system="s", lpar="l", host="", agent_id="a")
    assert _one(lines)["host"] == ""

    lines = _capture()
    audit.record_ownership_override(system="s", lpar="l", host="H" * 500, agent_id="a")
    assert len(_one(lines)["host"]) == audit.MAX_VALUE_LENGTH


def test_the_denial_record_names_both_halves_of_the_refusal():
    """#467 / ADR 0100 §2. A refusal carries the comparison that failed.

    The override record knows only the actor, because it never read the token.
    A `foreign-owner` denial read one, so it carries the owner the LPAR claims
    beside the agent that was refused — which is what lets an operator count
    refusals per agent and per partition rather than only observing that some
    happened. `operation` and `denial` say which entry point refused and under
    which of the two rules.
    """
    lines = _capture()
    audit.record_ownership_denied(
        operation="lpar-mutation",
        denial="foreign-owner",
        system="sys-a",
        lpar="db-01",
        owner="agent-3",
        host="hmc.test",
        agent_id="agent-7",
    )
    record = _one(lines)
    assert list(record) == [
        "time",
        "event",
        "operation",
        "denial",
        "system",
        "lpar",
        "owner",
        "host",
        "attribution",
    ]
    assert record["event"] == "ownership-denied"
    assert record["operation"] == "lpar-mutation"
    assert record["denial"] == "foreign-owner"
    assert record["owner"] == "agent-3"
    assert record["attribution"] == {
        "claim": "agent-7",
        "source": "config:agent_id",
        "verified": False,
    }


def test_a_malformed_token_denial_records_a_null_owner():
    """ADR 0100 §2. Nothing parsed, so there is no claimed owner to name.

    `null` rather than an empty string, which is `_value`'s rendering of a value
    that *was* supplied and was empty — the same distinction the connection
    object's `selector` keeps.
    """
    lines = _capture()
    audit.record_ownership_denied(
        operation="lpar-decommission-snapshot",
        denial="malformed-token",
        system="sys-a",
        lpar="db-01",
        owner=None,
        host="hmc.test",
        agent_id="agent-7",
    )
    record = _one(lines)
    assert record["owner"] is None
    assert record["denial"] == "malformed-token"
    assert record["operation"] == "lpar-decommission-snapshot"


def test_profile_restore_is_a_distinct_ownership_operation():
    """A system-wide guard must not claim to be the single-LPAR mutation guard."""
    assert audit.OWNERSHIP_OPERATIONS == {
        "lpar-mutation",
        "lpar-decommission-snapshot",
        "lpar-profile-restore",
    }


def test_the_denial_record_is_bounded_and_escaped():
    """Every caller-supplied field on it takes the same bound as its siblings.

    `owner` is the one this record adds, and it is HMC-supplied text parsed out
    of an operator-authored description — so it is the field most worth pinning.
    """
    lines = _capture()
    audit.record_ownership_denied(
        operation="lpar-mutation",
        denial="foreign-owner",
        system="S" * 500,
        lpar="x\ny‮z",
        owner="O" * 500,
        host="H" * 500,
        agent_id="A" * 500,
    )
    assert len(lines) == 1
    assert lines[0].isascii() and "\n" not in lines[0]
    record = json.loads(lines[0])
    for field in ("system", "owner", "host"):
        assert len(record[field]) == audit.MAX_VALUE_LENGTH, field
    assert len(record["attribution"]["claim"]) == audit.MAX_VALUE_LENGTH


def test_an_empty_denial_host_renders_empty():
    """As on the override record: an unset `HMCConfig.host` is the empty string."""
    lines = _capture()
    audit.record_ownership_denied(
        operation="lpar-mutation",
        denial="foreign-owner",
        system="s",
        lpar="l",
        owner="o",
        host="",
        agent_id="a",
    )
    assert _one(lines)["host"] == ""


def test_the_tls_record_carries_host_and_source():
    """#379. The durable counterpart of the logon warning names the HMC and the knob.

    `source` is the operator-facing half of the record: it says which knob to turn
    to stop the exposure. Its closed vocabulary is `hmcpctl.client.core.VerifySSLSource`
    and the value below is one member of it, not a restatement of the set (#504).
    No credential, session token or request body travels — a construction-time
    event has none to carry.
    """
    lines = _capture()
    audit.record_tls_verification_disabled(host="hmc.test", source="field-default")
    record = _one(lines)
    assert list(record) == ["time", "event", "host", "source"]
    assert record["event"] == "tls-verification-disabled"
    assert record["host"] == "hmc.test"
    assert record["source"] == "field-default"


def test_an_empty_tls_host_renders_empty_and_is_bounded():
    """`HMCConfig.host` defaults to "", which is what renders, and the
    caller-supplied bound applies to it like to every other field."""
    lines = _capture()
    audit.record_tls_verification_disabled(host="", source="explicit-argument")
    assert _one(lines)["host"] == ""

    lines = _capture()
    audit.record_tls_verification_disabled(
        host="H" * 500, source="environment:HMC_VERIFY_SSL"
    )
    assert len(_one(lines)["host"]) == audit.MAX_VALUE_LENGTH


def test_a_long_tls_source_stays_bounded():
    """*source* is caller-supplied prose too, so it takes the same bound."""
    lines = _capture()
    audit.record_tls_verification_disabled(host="hmc.test", source="s" * 500)
    assert len(_one(lines)["source"]) == audit.MAX_VALUE_LENGTH


def test_the_power_guard_record_carries_the_effective_value():
    lines = _capture()
    audit.record_power_ownership_guard(
        connection="lab",
        authorize_power_operations=False,
        source="profile",
        detail=None,
    )
    record = _one(lines)
    assert list(record) == [
        "time",
        "event",
        "connection",
        "authorize_power_operations",
        "source",
        "detail",
    ]
    assert record == {
        "time": record["time"],
        "event": "power-ownership-guard",
        "connection": "lab",
        "authorize_power_operations": False,
        "source": "profile",
        "detail": None,
    }


def test_the_power_guard_record_is_emitted_at_warning():
    levels: list[int] = []
    messages: list[str] = []

    class _Level(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            levels.append(record.levelno)
            messages.append(record.getMessage())

    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.addHandler(_Level())
    logger.setLevel(logging.INFO)
    audit.record_power_ownership_guard(
        connection="c" * 500,
        authorize_power_operations=None,
        source="s" * 500,
        detail="d" * 500,
    )
    assert levels == [logging.WARNING]
    record = json.loads(messages[0])
    for field in ("connection", "source", "detail"):
        assert len(record[field]) == audit.MAX_VALUE_LENGTH


def test_the_install_record_names_the_target_and_the_log_path():
    """#469, ADR 0102. The only record the detached install path produces.

    `log_path` is the field a raised submission leaves an operator to read, and
    `system` and `host` are what make it locatable: the path is keyed on the
    partition name alone, so it collides across managed systems behind one HMC.
    """
    lines = _capture()
    audit.record_install_attempted(
        system="sys-a",
        partition="vios-01",
        log_path="/tmp/hmcpctl-installios-vios-01.log",
        host="hmc.test",
        agent_id="agent-7",
    )
    record = _one(lines)
    assert list(record) == [
        "time",
        "event",
        "system",
        "partition",
        "log_path",
        "host",
        "attribution",
    ]
    assert record["event"] == "install-attempted"
    assert record["system"] == "sys-a"
    assert record["partition"] == "vios-01"
    assert record["log_path"] == "/tmp/hmcpctl-installios-vios-01.log"
    assert record["host"] == "hmc.test"
    assert record["attribution"] == {
        "claim": "agent-7",
        "source": "config:agent_id",
        "verified": False,
    }


def test_the_install_record_is_emitted_at_warning():
    """ADR 0102 §3. `logging.lastResort` drops anything below `WARNING`, and on a
    bare `hmcpctl.api` consumer — which installs no sink — that is the whole
    delivery path. `INFO` would silence the record exactly where it is the only
    trace of an irreversible submission that exists."""
    levels: list[int] = []

    class _Level(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            levels.append(record.levelno)

    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.addHandler(_Level())
    logger.setLevel(logging.INFO)
    audit.record_install_attempted(
        system="s", partition="p", log_path="/l", host="hmc.test", agent_id="a"
    )
    audit.record_install_submitted(
        system="s", partition="p", pid=123, log_path="/l", host="hmc.test", agent_id="a"
    )
    assert levels == [logging.WARNING, logging.WARNING]


def test_the_install_submitted_record_carries_the_remote_pid():
    lines = _capture()
    audit.record_install_submitted(
        system="sys-a",
        partition="vios-01",
        pid=4321,
        log_path="/tmp/hmcpctl-installios-vios-01.log",
        host="hmc.test",
        agent_id="agent-7",
    )
    record = _one(lines)
    assert record["event"] == "install-submitted"
    assert record["pid"] == 4321
    assert record["system"] == "sys-a"
    assert record["partition"] == "vios-01"
    assert record["log_path"] == "/tmp/hmcpctl-installios-vios-01.log"


def test_the_install_record_is_bounded_and_escaped():
    """Every field is caller- or HMC-derived, so each takes the shared bound.

    `partition` is the one worth pinning: it is an HMC CLI name that the log path
    is composed from, and under ADR 0042's threat model it is not trusted text.
    """
    lines = _capture()
    audit.record_install_attempted(
        system="S" * 500,
        partition="x\ny‮z",
        log_path="L" * 500,
        host="H" * 500,
        agent_id="A" * 500,
    )
    assert len(lines) == 1
    assert lines[0].isascii() and "\n" not in lines[0]
    record = json.loads(lines[0])
    for field in ("system", "log_path", "host"):
        assert len(record[field]) == audit.MAX_VALUE_LENGTH, field
    assert len(record["attribution"]["claim"]) == audit.MAX_VALUE_LENGTH


@pytest.mark.parametrize(("length", "recoverable"), [(110, True), (200, False)])
def test_a_long_partition_records_a_log_path_that_does_not_exist(length, recoverable):
    """The bound applies to `log_path` too, and the document states the boundary.

    The template's fixed part is 28 characters, so a partition name past 100
    pushes `/tmp/hmcpctl-installios-<slug>.log` over the bound and the record
    carries a cut path with no marker. Whether the real path survives depends on
    `partition` beside it, which takes the same bound: at 110 it is whole and the
    path recomposes, at 200 it is cut too and nothing recovers it. Both are names
    `installios` would refuse, but the record precedes the submit.
    """
    partition = "p" * length
    real = f"/tmp/hmcpctl-installios-{partition}.log"
    lines = _capture()
    audit.record_install_attempted(
        system="s", partition=partition, log_path=real, host="h", agent_id="a"
    )
    record = _one(lines)
    assert len(real) > audit.MAX_VALUE_LENGTH
    assert record["log_path"] == real[: audit.MAX_VALUE_LENGTH]
    assert not record["log_path"].endswith(".log"), "a truncated path still looks whole"
    recomposed = f"/tmp/hmcpctl-installios-{record['partition']}.log"
    assert (recomposed == real) is recoverable


def test_only_audit_sink_resolves_the_audit_logger():
    """Spec 8a. The sink owns the logger and has no package dependencies."""
    package = Path(audit.__file__).parent.parent
    sink_path = Path(audit_sink.__file__).resolve()
    offenders = []
    for path in package.rglob("*.py"):
        if path.resolve() == sink_path:
            continue
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getLogger"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == audit_sink.AUDIT_LOGGER_NAME
            for node in ast.walk(tree)
        ):
            offenders.append(str(path.relative_to(package)))
    assert offenders == [], f"{offenders} name the reserved audit logger"

    source = Path(audit_sink.__file__).read_text()
    assert "from ." not in source and "from hmcpctl" not in source, (
        "audit/sink.py must import nothing from the package so records can depend "
        "on its emission boundary without a cycle"
    )


def test_a_record_reaches_stderr_and_not_stdout(capsys):
    """Spec 9."""
    audit_sink.install_audit_sink()
    audit.record_ownership_override(
        system="sys-a", lpar="db-01", host="hmc.test", agent_id="agent-7"
    )
    # Through the handler's own `flush`, which is what `logging.shutdown` calls at
    # interpreter exit — and the reason that call cannot become the hang #269 is
    # about, since it is the sink's bounded drain rather than an unbounded join.
    logging.getLogger(audit_sink.AUDIT_LOGGER_NAME).handlers[0].flush()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip())["event"] == "ownership-override"


def test_the_sink_does_not_propagate_to_an_ancestor_handler(capsys):
    """Spec 10. The in-process route to a root stdout handler is closed."""
    root_lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            root_lines.append(record.getMessage())

    collector = _Collect()
    logging.root.addHandler(collector)
    try:
        audit_sink.install_audit_sink()
        assert logging.getLogger(audit_sink.AUDIT_LOGGER_NAME).propagate is False
        audit.record_ownership_override(
            system="s", lpar="l", host="hmc.test", agent_id="a"
        )
        _flush()
        capsys.readouterr()
        assert root_lines == [], "a root handler must not receive audit records"
    finally:
        logging.root.removeHandler(collector)


def _private_sink(capacity: int = 8, drain_timeout: float = 2.0):
    """A sink of this test's own, so nothing here depends on process-global state.

    ``audit_sink._sink()`` is shared with every other test in the session and carries a
    live daemon thread; a test that wants to observe a drop counter, a shutdown, or
    a blocked writer needs an instance it alone owns.
    """
    sink = audit_sink._StderrSink(capacity, drain_timeout)
    try:
        yield sink
    finally:
        sink.close()


@pytest.fixture
def sink():
    yield from _private_sink()


class _Hostile(io.StringIO):
    """A stream that fails every write with the error it was built with."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def write(self, _data: str) -> int:
        raise self._error


@pytest.mark.parametrize(
    ("label", "stream"),
    [
        ("absent", None),
        ("closed", _Hostile(ValueError("I/O operation on closed file"))),
        ("broken", _Hostile(OSError("broken pipe"))),
        ("unforeseen", _Hostile(RuntimeError("something nobody predicted"))),
    ],
)
def test_a_stream_that_cannot_be_written_drops_and_says_so(
    monkeypatch, sink, label, stream
):
    """Spec 11, 12 and 13, and ADR 0043's closing of "a dropped record is silent".

    CPython sets ``sys.stderr`` to ``None`` when fd 2 is closed at interpreter
    start; a broken stream raises ``OSError`` and a closed one ``ValueError``. Each
    was silent before. Each is now a counted drop that the *next* successful write
    reports — asserted by pointing the sink at a working stream afterwards, which
    also proves the writer thread survived the failure rather than dying and
    stranding every later record.
    """
    monkeypatch.setattr(sys, "stderr", stream)
    sink.submit(f"lost-{label}\n")
    assert sink.drain(2.0), "a failing write must still settle"

    landed = io.StringIO()
    monkeypatch.setattr(sys, "stderr", landed)
    sink.submit('{"event":"probe"}\n')
    assert sink.drain(2.0)

    lines = landed.getvalue().splitlines()
    assert json.loads(lines[0]) == {
        "time": json.loads(lines[0])["time"],
        "event": "records-dropped",
        "count": 1,
    }
    assert json.loads(lines[1]) == {"event": "probe"}
    assert f"lost-{label}" not in landed.getvalue()


def test_import_is_inert_until_sink_installation(tmp_path):
    """Import leaves logging untouched; explicit installation owns mutation."""
    probe = (
        "import logging, hmcpctl.audit.sink as a; "
        "logger = logging.getLogger(a.AUDIT_LOGGER_NAME); "
        "print(logger.propagate, a._SINK is None); "
        "a.install_audit_sink(); "
        "print(logger.propagate, a._SINK is not None)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.splitlines() == ["True True", "False True"]


def test_an_unconfigured_logger_still_reaches_last_resort(capsys):
    """The other half of #272's fix: closing propagation must not cost a CLI user
    the record. `callHandlers` consults `lastResort` when the walk finds zero
    handlers, which `propagate` does not affect."""
    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False
    saved = list(logging.root.handlers)
    logging.root.handlers.clear()
    try:
        audit.record_ownership_override(
            system="s", lpar="l", host="hmc.test", agent_id="a"
        )
        captured = capsys.readouterr()
    finally:
        logging.root.handlers[:] = saved
    assert captured.out == ""
    assert json.loads(captured.err.strip())["event"] == "ownership-override"


def test_a_foreign_writers_bad_record_does_not_raise_into_them(capsys):
    """A stdlib handler never raises into its caller, and this is an attachment
    point the operator documentation invites others to use."""
    audit_sink.install_audit_sink()
    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("this record cannot be rendered")

    # Raised while the handler renders the message, not while audit builds it, so
    # audit_sink.emit's guard is not what saves the caller here.
    logger.warning("%s", Hostile())
    capsys.readouterr()


def test_install_is_idempotent_and_defers_to_what_the_operator_set():
    """Spec 14. Configured wins; unconfigured gets a default."""
    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    audit_sink.install_audit_sink()
    audit_sink.install_audit_sink()
    assert len(logger.handlers) == 1
    assert logger.level == logging.INFO

    logger.handlers.clear()
    logger.setLevel(logging.WARNING)
    audit_sink.install_audit_sink()
    assert logger.level == logging.WARNING, "an operator's level must survive"


def test_a_preattached_stdout_handler_is_deferred_to(capsys):
    """Spec 14a. The deferral is a chosen behaviour, not an accident."""
    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    audit_sink.install_audit_sink()
    assert len(logger.handlers) == 1, "install must not add a second handler"
    logger.setLevel(logging.INFO)
    audit.record_ownership_override(system="s", lpar="l", host="hmc.test", agent_id="a")
    assert json.loads(capsys.readouterr().out.strip())["event"] == "ownership-override"


def test_the_singletons_render_as_states_rather_than_raising():
    """Spec 14b. Asserted on record content: the guard swallows a raising build,
    so "did not raise" is true of correct and M7-mutated code alike."""
    record = _authorization(
        decision="deny",
        reason="target-selector-absent",
        targets=(
            audit.AuditTarget(
                kind="lpar", argument="lpar_name_or_uuid", state="absent", value=None
            ),
            audit.AuditTarget(
                kind="vios",
                argument="vios_partition_id",
                state="unreadable",
                value=None,
            ),
        ),
    )
    assert record["targets"] == [
        {
            "kind": "lpar",
            "argument": "lpar_name_or_uuid",
            "state": "absent",
            "value": None,
        },
        {
            "kind": "vios",
            "argument": "vios_partition_id",
            "state": "unreadable",
            "value": None,
        },
    ]


def test_the_line_equals_the_message_and_records_do_not_share_a_line(capsys):
    """Spec 15, and the StreamHandler.terminator premise it rests on.

    A custom ``logging.Handler`` inherits no ``terminator``, so the newline is
    written explicitly — and in the same ``write`` call, since nothing serialises
    this handler against the other writers still on fd 2: the interpreter's own
    exit traceback, and any handler an operator attached outside this sink.
    """
    assert not hasattr(logging.Handler, "terminator")
    assert logging.StreamHandler.terminator == "\n"

    audit_sink.install_audit_sink()
    audit.record_ownership_override(
        system="one", lpar="l", host="hmc.test", agent_id="a"
    )
    audit.record_ownership_override(
        system="two", lpar="l", host="hmc.test", agent_id="a"
    )
    _flush()
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert len(lines) == 2, "two records must not share a physical line"
    assert [json.loads(line)["system"] for line in lines] == ["one", "two"]

    handler = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME).handlers[0]
    assert handler.formatter is None, "no Formatter may wrap the record"


def test_the_handler_issues_one_write_per_record(monkeypatch):
    """Spec 15, second half. One call, so nothing can land between a record and
    its newline."""
    writes: list[str] = []

    class _Counting(io.StringIO):
        def write(self, data: str) -> int:
            writes.append(data)
            return len(data)

    audit_sink.install_audit_sink()
    monkeypatch.setattr(sys, "stderr", _Counting())
    audit.record_ownership_override(system="s", lpar="l", host="hmc.test", agent_id="a")
    _flush()
    assert len(writes) == 1
    assert writes[0].endswith("\n")


# --- #323 / ADR 0051: a second producer on the same sink -----------------------


def test_a_handler_without_a_formatter_renders_the_message_and_nothing_else(capsys):
    """ADR 0040's grammar survives the handler growing a formatted mode.

    ``logging.Handler.format`` falls back to a shared default ``Formatter`` when
    none is installed, which would append a traceback to a record carrying
    ``exc_info``. ``sink_handler`` must not take that fallback: the audit stream's
    contract is that the message *is* the record, one line of ASCII JSON.
    """
    handler = audit_sink.sink_handler()
    assert handler.formatter is None

    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.warning('{"event":"authorization"}', exc_info=True)
    _flush()

    err = capsys.readouterr().err
    assert err == '{"event":"authorization"}\n'
    assert "Traceback" not in err


def test_a_handler_with_a_formatter_carries_the_traceback_to_the_sink(capsys):
    """The other arm, which is what ADR 0051 attaches to the ``fastmcp`` logger."""
    handler = audit_sink.sink_handler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        raise RuntimeError("a handler bug")
    except RuntimeError:
        logger.exception("Error calling tool 'x'")
    _flush()

    err = capsys.readouterr().err
    assert err.startswith("ERROR: Error calling tool 'x'\n")
    assert "Traceback" in err
    assert "RuntimeError: a handler bug" in err


#: A record that parses. Injected through a *foreign* producer's rendering, which
#: is the only way anything but `audit` puts text on this stream.
FORGED = '{"time": "2026-01-01T00:00:00+00:00", "event": "authorization", "decision": "allow"}'


def _record_lines(text: str) -> list[dict]:
    """Every line of *text* a consumer of this stream would parse as a record."""
    parsed = []
    for line in text.splitlines():
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "event" in candidate:
            parsed.append(candidate)
    return parsed


def test_a_foreign_rendering_cannot_forge_a_record_on_this_stream(capsys):
    """#323: the grammar ADR 0040 defined, against the producer ADR 0051 added.

    A rendered exception carries the exception's ``str()``, and under ADR 0042's
    threat model that is HMC-returned text this package does not trust. Through
    the ``RichHandler`` ADR 0051 replaces, such text was indented into the message
    column and hard-wrapped, so column 0 was unreachable by accident. This asserts
    the rule that replaces the accident.
    """
    handler = audit_sink.sink_handler()
    handler.setFormatter(audit_sink.StreamSafeFormatter("%(message)s", "fastmcp: "))
    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    try:
        raise RuntimeError(f"hmc said: \n{FORGED}\n and \x1b[31m‮ too")
    except RuntimeError:
        logger.exception("Error calling tool 'hmc_migrate_validate_lpar'")
    _flush()

    err = capsys.readouterr().err
    assert FORGED in err.replace("\\u", ""), "the text must still be reported"
    assert _record_lines(err) == [], "a foreign rendering forged a parseable record"
    assert all(line.startswith("fastmcp: ") for line in err.splitlines())
    assert "\x1b" not in err, "a raw ESC reached the stream"
    assert "‮" not in err, "a raw bidirectional override reached the stream"
    assert "\\u001b" in err and "\\u202e" in err, "both must be reported, escaped"


def test_the_audit_records_own_grammar_is_untouched_by_that_formatter(capsys):
    """The other half: no prefix and no escaping on the records themselves.

    ``sink_handler`` installs no formatter, so ADR 0040's rendering is what lands
    — this fails if a later change gives the audit logger the marked one.
    """
    audit_sink.install_audit_sink()
    audit.record_ownership_override(system="s", lpar="l", host="hmc.test", agent_id="a")
    _flush()

    err = capsys.readouterr().err
    assert len(_record_lines(err)) == 1
    assert not err.startswith("fastmcp: ")


def test_a_multi_line_rendering_reaches_the_stream_in_one_write(monkeypatch):
    """Spec 15's premise, for the multi-line item ADR 0051 introduced.

    Written after the assertion it replaces was shown not to bite: an earlier
    version asserted that a traceback's *text* survived intact, which stayed green
    when `_StderrSink._write` was mutated to one `write()` plus `flush()` per
    physical line. Content preservation is not atomicity; the number of calls is.
    """
    writes: list[str] = []

    class _Counting(io.StringIO):
        def write(self, data: str) -> int:
            writes.append(data)
            return len(data)

    handler = audit_sink.sink_handler()
    handler.setFormatter(audit_sink.StreamSafeFormatter("%(message)s", "fastmcp: "))
    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    monkeypatch.setattr(sys, "stderr", _Counting())
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("Error calling tool 'x'")
    _flush()

    assert len(writes) == 1, "a traceback must be one write, so it cannot land torn"
    assert writes[0].count("\n") > 2, "the rendering must really have been multi-line"
    assert writes[0].endswith("\n")


# The overflow arm of this section lives below `_wedged`, which it needs:
#   test_two_producers_share_one_bound_and_the_count_still_adds_back


# --- #269 / ADR 0043: an undrained destination must not block the writer -------
#
# Every test below drives a *real* pipe filled to its buffer limit, not a stream
# object pretending to block. The `full_stderr_pipe` fixture in tests/conftest.py
# owns the arrangement and explains why clearing O_NONBLOCK leaves it blocking.


@contextlib.contextmanager
def _wedged(pipe, capacity: int = 64):
    """A private sink writing to a pipe nobody is draining.

    The redirection and the shutdown live here, in the test's own call frame,
    rather than in a fixture: `sys.stderr` restored by a fixture finalizer races
    the writer thread, which may not be scheduled at all until the test body has
    returned. Closing the sink *before* restoring `sys.stderr` is what guarantees
    every line the writer still owes goes to the pipe under test instead of to the
    console — and to whatever a later test is capturing.
    """
    sink = audit_sink._StderrSink(capacity, 2.0)
    saved, sys.stderr = sys.stderr, pipe.stream
    try:
        yield sink
    finally:
        pipe.read_available()
        sink.close()
        sys.stderr = saved


def _records(raw: bytes) -> list[dict]:
    """Every JSON line in what came off the pipe, in order.

    The pipe was filled with unterminated padding, so the first record is glued to
    the tail of that padding: each line is taken from its first ``{`` rather than
    being required to start with one.
    """
    found = []
    for line in raw.decode().splitlines():
        start = line.find("{")
        if start >= 0:
            found.append(json.loads(line[start:]))
    return found


def test_an_undrained_pipe_does_not_block_the_submitting_thread(full_stderr_pipe):
    """#269, the whole point. The first line blocks the writer thread; the rest
    fill the queue and then drop. None of it reaches the caller.

    The bound is wall clock: before ADR 0043 this call did not return at all, so a
    generous ceiling still separates the two behaviours completely.
    """
    assert full_stderr_pipe.capacity > 0, "the pipe must actually have filled"
    returned = threading.Event()
    with _wedged(full_stderr_pipe) as sink:

        def drive() -> None:
            for index in range(264):
                sink.submit(f'{{"n":{index}}}\n')
            returned.set()

        # Driven from a thread this test can abandon, so the pre-ADR-0043
        # behaviour *fails* here instead of hanging the suite: a synchronous
        # write to this pipe never returns, and an unbounded wait in the main
        # thread would take the whole run down with it.
        threading.Thread(target=drive, daemon=True).start()
        landed = returned.wait(10.0)
    assert landed, "submitting onto a wedged destination never returned — #269"


def test_an_overflowing_queue_reports_what_it_lost(full_stderr_pipe):
    """ADR 0043's drop marker, over a real overflow rather than a forced counter.

    264 lines onto a 64-line queue with the writer wedged: most are lost. How many
    exactly depends on when the writer thread got scheduled, so the assertion is
    the conservation law rather than a number — every submitted line is either on
    the stream or in a marker's count. An accounting bug shows up as a total that
    does not add back, which is what makes the silent loss #269 refuses detectable
    at all.
    """
    pipe = full_stderr_pipe
    submitted = 264
    with _wedged(pipe) as sink:
        for index in range(submitted):
            sink.submit(f'{{"n":{index}}}\n')
        # Freeing the writer also collects whatever it manages to write while we
        # read, so both halves are kept: `drain` then guarantees the rest landed.
        seen = pipe.read_available()
        assert sink.drain(5.0), "the sink never settled after the pipe was drained"
        sink.submit('{"n":"last"}\n')
        assert sink.drain(5.0)
        records = _records(seen + pipe.read_available())

    markers = [record for record in records if record.get("event")]
    written = [record for record in records if "n" in record]
    assert markers, "an overflow that reports nothing is the silent loss #269 refuses"
    assert all(marker["event"] == "records-dropped" for marker in markers)
    assert sum(marker["count"] for marker in markers) + len(written) == submitted + 1
    assert len(written) < submitted, "the queue must have overflowed for this to test"
    assert written[-1]["n"] == "last"
    assert records.index(markers[0]) < records.index(written[-1]), (
        "a marker reports lines missing *above* it, so it must precede them"
    )


def test_two_producers_share_one_bound_and_the_count_still_adds_back(full_stderr_pipe):
    """#323 / ADR 0051: what a second producer does to ADR 0043's accounting.

    A rendered traceback is many physical lines but **one** submitted item, so it
    takes one queue slot and costs one drop. What this asserts is the conservation
    law with both producers on the queue: every item is on the stream or inside a
    marker's ``count``. The field means what ADR 0043 already said — items lost,
    never a record count — so a reader reconciling the trail is not misled by the
    extra source, only by more of it.

    Atomicity is *not* asserted here. An earlier version claimed it by checking
    that the traceback text survived intact, which stayed green when
    ``_StderrSink._write`` was mutated to one write per physical line.
    ``test_a_multi_line_rendering_reaches_the_stream_in_one_write`` owns that
    property, by counting calls.
    """
    pipe = full_stderr_pipe
    rendered_traceback = "ERROR: boom\nTraceback (most recent call last):\n  frame\n"
    submitted = 264
    with _wedged(pipe) as sink:
        for index in range(submitted):
            sink.submit(rendered_traceback if index % 2 else f'{{"n":{index}}}\n')
        seen = pipe.read_available()
        assert sink.drain(5.0), "the sink never settled after the pipe was drained"
        sink.submit('{"n":"last"}\n')
        assert sink.drain(5.0)
        raw = seen + pipe.read_available()

    text = raw.decode()
    records = _records(raw)
    markers = [record for record in records if record.get("event")]
    written_records = [record for record in records if "n" in record]
    written_tracebacks = text.count(rendered_traceback)

    assert markers, "an overflow that reports nothing is the silent loss #269 refuses"
    assert all(marker["event"] == "records-dropped" for marker in markers)
    assert written_tracebacks, "no traceback survived, so nothing here was tested"
    assert (
        sum(marker["count"] for marker in markers)
        + len(written_records)
        + written_tracebacks
        == submitted + 1
    )


def test_a_closed_sink_writes_nothing_more_and_still_counts_the_loss(monkeypatch):
    """`atexit` closes the sink; anything after that is lost, and known to be.

    ADR 0043 states it as a limit rather than designing around it: there is no
    writer left to carry a marker, so the count is real and unreportable. Closing
    twice must also be safe — the second call has no writer to join.
    """
    landed = io.StringIO()
    monkeypatch.setattr(sys, "stderr", landed)
    sink = audit_sink._StderrSink(4, 1.0)
    sink.submit('{"n":0}\n')
    sink.close()
    assert landed.getvalue() == '{"n":0}\n', "close must deliver what was queued"

    sink.close()
    sink.submit('{"n":"after"}\n')
    assert sink.drain(1.0), "a closed sink has nothing left to wait for"
    assert landed.getvalue() == '{"n":0}\n', "a closed sink must write nothing more"
    with sink._state:
        assert sink._dropped == 1, "loss after close is counted, not ignored"


def test_a_marker_that_cannot_be_written_is_still_owed(monkeypatch):
    """The count survives its own failed report, and is not double-counted.

    The marker is written like any other line, so it can fail like any other line.
    Restoring what it was owed is what keeps the arithmetic honest across a
    destination that goes away and comes back.
    """
    hostile = _Hostile(OSError("broken pipe"))
    monkeypatch.setattr(sys, "stderr", hostile)
    sink = audit_sink._StderrSink(4, 1.0)
    try:
        sink.submit("lost-one\n")
        assert sink.drain(1.0)
        # The next line's marker write fails too, so one owed becomes two rather
        # than being forgotten or counted twice.
        sink.submit("lost-two\n")
        assert sink.drain(1.0)

        landed = io.StringIO()
        monkeypatch.setattr(sys, "stderr", landed)
        sink.submit('{"n":"ok"}\n')
        assert sink.drain(1.0)
    finally:
        sink.close()

    lines = landed.getvalue().splitlines()
    assert json.loads(lines[0]) == {
        "time": json.loads(lines[0])["time"],
        "event": "records-dropped",
        "count": 2,
    }
    assert json.loads(lines[1]) == {"n": "ok"}


def test_the_drop_marker_is_one_ascii_line_of_the_same_grammar():
    """A consumer of the record stream must be able to parse this like any other."""
    line = audit_sink._drop_marker(7)
    assert line.endswith("\n") and line.count("\n") == 1
    assert line.isascii()
    marker = json.loads(line)
    assert set(marker) == {"time", "event", "count"}
    assert marker["event"] == "records-dropped"
    assert marker["count"] == 7
    assert marker["event"] in audit.EVENTS


def test_shutdown_delivers_everything_queued_when_the_destination_is_read(
    monkeypatch,
):
    """Criterion 3, first half — including a queue that is full when close begins.

    The stop sentinel has a slot of its own, so nothing is evicted to make room
    for it; an eviction here would silently lose records at exit on a perfectly
    healthy destination.
    """
    landed = io.StringIO()
    monkeypatch.setattr(sys, "stderr", landed)
    sink = audit_sink._StderrSink(16, 2.0)
    for index in range(16):
        sink.submit(f'{{"n":{index}}}\n')
    sink.close()
    assert [json.loads(line)["n"] for line in landed.getvalue().splitlines()] == list(
        range(16)
    ), "shutdown must lose nothing, and must not reorder what it keeps"


def test_shutdown_returns_even_when_the_destination_never_drains(full_stderr_pipe):
    """Criterion 3, second half. `QueueListener.stop` joins unboundedly and would
    hang here; this join is bounded and the thread is a daemon."""
    with _wedged(full_stderr_pipe, capacity=16) as sink:
        for index in range(16):
            sink.submit(f'{{"n":{index}}}\n')
        start = time.monotonic()
        sink.close()
        elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"close() on a wedged destination took {elapsed}s"
