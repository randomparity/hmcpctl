"""Behavioural tests for the dedicated PCIe live-assignment arm (issue #217)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult

LIVE_TEST_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(LIVE_TEST_ROOT))
from live_test import dedicated_pcie  # noqa: E402
from live_test.config import LiveTestArtifacts, LiveTestConfig  # noqa: E402
from live_test.observation import CLEANUP, Assertion, CallFailure  # noqa: E402
from live_test_runner import RunState  # noqa: E402

#: The arm's own settings. Everything else on `LiveTestConfig` keeps its
#: declared default: this arm reads only these four, and reads them from the
#: validated configuration rather than the environment (ADR 0115).
_CONFIG = {
    "dedicated_pcie_system_name": "sys-one",
    "dedicated_pcie_lpar_prefix": "live-",
}
_DRC = "21010020"
_ASSIGNED = f"{_DRC}/none/0"
#: The one profile read ADR 0165 Decision 1 admits, as a literal so a builder
#: that drifts from it fails here rather than agreeing with itself.
_PROFILE_READ = "lssyscfg -r prof -m sys-one -F lpar_name,name,io_slots --header"
#: What the HMC answers for a partition name it does not have
#: (`scripts/live_test_recovery.py:172-173`, ADR 0162).
_NOT_FOUND = CallFailure(
    "HMCCLIError",
    "HMCCLIError: HSCL8012 The partition named live-x-createtime was not found.",
    "",
    None,
    False,
)

_CONNECTION_LOST = CallFailure(
    "HMCCLIError", "HMCCLIError: connection lost", "", None, False
)
#: Same wording as HSCL8012, but IBM's only recovery for it is rebuilding the
#: managed system, so it is not read as "no such partition".
_SIBLING_NOT_FOUND = CallFailure(
    "HMCCLIError",
    "HMCCLIError: HSCL7002 The partition named live-x-createtime was not found.",
    "",
    None,
    False,
)


@pytest.mark.parametrize(
    ("status", "data", "expected"),
    [
        ("FAIL", _NOT_FOUND, True),
        # Only a failed call is the HMC's "no such partition" answer.
        ("PASS", _NOT_FOUND, False),
        ("FAIL", _CONNECTION_LOST, False),
        ("FAIL", _SIBLING_NOT_FOUND, False),
        ("FAIL", None, False),
        ("PASS", "description text", False),
    ],
)
def test_partition_not_found_reads_only_a_failed_hscl8012(status, data, expected):
    """HSCL8012 is the one "no such partition" answer; nothing else counts."""
    assert dedicated_pcie.partition_not_found(status, data) is expected


class ScenarioState:
    """State seam recording every tool call in order, with per-call outcomes."""

    def __init__(
        self,
        responses: dict[str, Any],
        statuses: dict[str, Any] | None = None,
        config: LiveTestConfig | None = None,
    ) -> None:
        self.responses = responses
        self.statuses = statuses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: list[tuple[int, str, str, Any]] = []
        self.context = SimpleNamespace(system_name="unused", lp3_name="unused")
        self.config = config if config is not None else LiveTestConfig(**_CONFIG)
        self.gaps: list[dict[str, Any]] = []
        # The production dataclass, not a stand-in: the arm records what it
        # created here and `live_test_recovery.py` reads those exact fields.
        self.artifacts = LiveTestArtifacts()
        self.tool_counts: dict[str, int] = {}
        self.cleanup_start: int | None = None
        self.observations: list[dict[str, Any]] = []
        self.notes: dict[str, str] = {}

    async def call(self, _client: object, tool: str, **kwargs: Any) -> tuple[str, Any]:
        index = self.tool_counts.get(tool, 0)
        self.tool_counts[tool] = index + 1
        self.calls.append((tool, kwargs))
        response = self.responses.get(tool)
        if callable(response):
            response = response(kwargs, index)
        # A response that is a failure is what the tool raised, so it is a FAIL.
        status = self.statuses.get(
            tool, "FAIL" if isinstance(response, CallFailure) else "PASS"
        )
        if callable(status):
            status = status(tool, kwargs, index)
        return status, response

    def record(
        self,
        subtask: int,
        tool: str,
        status: str,
        data: Any,
        note: str = "",
        **_kwargs: Any,
    ) -> None:
        # `RunState.record` carries the note in `note` and the payload in
        # `data`; keeping whichever is populated lets one assertion read both.
        self.results.append((subtask, tool, status, data if data is not None else note))
        self.notes[tool] = note

    def skip(self, subtask: int, tool: str, reason: str) -> None:
        self.results.append((subtask, tool, "SKIP", reason))

    # The production classifier, not a re-implementation of it. It reads only
    # `skip`, `record` and `gaps`, all of which this seam provides — so the
    # tests exercise the real declared-limitation matching rather than a copy
    # that can agree with a broken arm.
    record_with_expected = RunState.record_with_expected
    # Likewise the production observation writer, so the tests pin the exact
    # objects `_emit_observations` would write.
    record_verified = RunState.record_verified

    # -- views -------------------------------------------------------------

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    def commands(self) -> list[str]:
        return [k["cmd"] for t, k in self.calls if t == "hmc_run_command"]

    def cleanup_calls(self) -> list[tuple[str, dict[str, Any]]]:
        assert self.cleanup_start is not None, "cleanup never ran"
        return self.calls[self.cleanup_start :]

    def cleanup_commands(self) -> list[str]:
        return [k["cmd"] for t, k in self.cleanup_calls() if t == "hmc_run_command"]

    def cleanup_tools(self) -> list[str]:
        return [tool for tool, _ in self.cleanup_calls()]

    def row(self, needle: str) -> tuple[int, str, str, Any] | None:
        """The first recorded row whose tool label contains *needle*."""
        return next((r for r in self.results if needle in r[1]), None)


# ---------------------------------------------------------------------------
# Response / status helpers
# ---------------------------------------------------------------------------

_ADMITTED_VERSION = "Version: 10\nRelease: 3\nService Pack: 1060"
_ADMITTED_MODEL = "8375-42A"
#: A refused create-time probe as `RunState.call` delivers it: a classified
#: `CallFailure` that created nothing.
_REFUSAL = CallFailure(
    exception_type="PcieAssignmentUnavailableError",
    message="PcieAssignmentUnavailableError: refused before creation",
    traceback_text="",
    http_status=None,
    denied=False,
)


def _slot_inventory(owner: str = "") -> dict[str, Any]:
    return {
        "capability": "available",
        "items": [
            {"drc_index": _DRC, "description": "PCIe adapter", "owner_lpar": owner}
        ],
    }


def _is_probe(kwargs: dict[str, Any]) -> bool:
    return str(kwargs.get("name", "")).endswith("-createtime")


def _is_probe_name(kwargs: dict[str, Any]) -> bool:
    return str(kwargs.get("lpar_name_or_uuid", "")).endswith("-createtime")


def _probe_refused(_tool: str, kwargs: dict[str, Any], _index: int) -> str:
    """The default: the create-time probe creates nothing, the fixture create succeeds.

    Tests of the create-time path itself opt into a probe that exists.
    """
    return "FAIL" if _is_probe(kwargs) else "PASS"


def _happy_responses(
    marker_holder: dict[str, str],
    *,
    description: str | None = None,
    descriptions: list[str] | None = None,
    probe_description: Any = None,
    uuid_value: str | None = "fixture-uuid",
    uuids: list[str | None] | None = None,
    ownership_stamped: bool | None = True,
    hmc_version: str = _ADMITTED_VERSION,
    system_model: str = _ADMITTED_MODEL,
    inventory_owner: str = "",
    run_command: Any = None,
    probe_exists: bool = False,
) -> dict[str, Any]:
    """Responses for a run in which every step succeeds.

    ``descriptions`` and ``uuids``, when given, are consumed one per call so a
    test can make an identity drift *between* two reads — which is how Guard C
    is reached with a value Guard A already accepted. ``run_command`` replaces
    the default command model outright, for the two tests whose fault is a
    response property rather than a status.
    """
    slots = {"io_slots": "none"}

    def default_run_command(kwargs: dict[str, Any], _index: int) -> str:
        cmd = kwargs["cmd"]
        if cmd == "lshmc -V":
            return hmc_version
        if "-r sys " in cmd and "type_model" in cmd:
            return system_model
        if "io_slots+" in cmd:
            slots["io_slots"] = _ASSIGNED
            return ""
        if "io_slots-" in cmd:
            slots["io_slots"] = "none"
            return ""
        return slots["io_slots"]

    def get_description(kwargs: dict[str, Any], index: int) -> Any:
        # Keyed on which partition is being asked about, so a test can give the
        # probe a foreign token while the fixture keeps this run's — the only
        # way `_cleanup_probe_partition`'s comparison can be made to refuse.
        if _is_probe_name(kwargs):
            if probe_description is not None:
                return probe_description
            if not probe_exists:
                # The probe create was refused, so this partition does not
                # exist. Answering with the fixture's own stamp would tell the
                # arm's lost-response readback that a refused create had in
                # fact created something, which is the opposite of the truth.
                return _NOT_FOUND
        if descriptions is not None:
            return descriptions[min(index, len(descriptions) - 1)]
        if description is not None:
            return description
        token = marker_holder.get("marker", "")
        return f"[hmcpctl owner:hmcpctl created:2026-09-02] [caller {token}]"

    def get_lpar(_kwargs: dict[str, Any], index: int) -> dict[str, Any] | None:
        value = uuids[min(index, len(uuids) - 1)] if uuids is not None else uuid_value
        return {"UUID": value} if value else None

    def create_lpar(kwargs: dict[str, Any], _index: int) -> Any:
        if _is_probe(kwargs):
            return _REFUSAL
        return {
            "resource_created": True,
            "lpar": {"UUID": uuid_value} if uuid_value else None,
            "ownership_stamped": ownership_stamped,
            "warnings": [],
        }

    command_model = run_command or default_run_command

    def assign_slot(kwargs: dict[str, Any], index: int) -> None:
        # The operation issues the documented grammar itself (ADR 0166), so the
        # same profile model sees the write the arm's raw reads later observe.
        command_model({"cmd": _io_slots_plus(kwargs["lpar_name_or_uuid"])}, index)

    return {
        "hmc_list_dedicated_pcie_slots": lambda _k, _n: _slot_inventory(
            inventory_owner
        ),
        "hmc_create_lpar": create_lpar,
        "hmc_get_lpar": get_lpar,
        "hmc_get_lpar_description": get_description,
        "hmc_run_command": command_model,
        "hmc_delete_lpar": lambda _k, _n: "deleted",
        "hmc_assign_dedicated_pcie_slot": assign_slot,
    }


def _io_slots_plus(lpar_name: str) -> str:
    return f"chsyscfg -r prof -m sys-one -i name=default_profile,io_slots+={_DRC}//0,lpar_name={lpar_name}"


def _probe_create_succeeds(
    responses: dict[str, Any], *, assignment_lands: bool = True
) -> None:
    """Make the create-time probe create its partition, as the lifted gate does."""
    command_model = responses["hmc_run_command"]

    def create_lpar(kwargs: dict[str, Any], index: int) -> Any:
        probe = _is_probe(kwargs)
        if probe and assignment_lands:
            command_model({"cmd": _io_slots_plus(str(kwargs["name"]))}, index)
        return {
            "resource_created": True,
            "lpar": {"UUID": "probe-uuid" if probe else "fixture-uuid"},
            "ownership_stamped": True,
            "warnings": [],
        }

    responses["hmc_create_lpar"] = create_lpar


def _command_fails(needle: str) -> Any:
    """Status seam: fail every `hmc_run_command` whose text contains *needle*."""

    def status(_tool: str, kwargs: dict[str, Any], _index: int) -> str:
        return "FAIL" if needle in kwargs.get("cmd", "") else "PASS"

    return status


def _nth_matching_command_fails(needle: str, first: int) -> Any:
    """Status seam: fail matching commands from the *first*-th occurrence on.

    Counts matches itself rather than using the per-tool index, because
    `hmc_run_command` also carries the environment reads and the mutations.
    """
    seen = {"n": 0}

    def status(_tool: str, kwargs: dict[str, Any], _index: int) -> str:
        if needle not in kwargs.get("cmd", ""):
            return "PASS"
        seen["n"] += 1
        return "FAIL" if seen["n"] >= first else "PASS"

    return status


def _admitted_readback(io_slots: str, lpar_name: str) -> str:
    """The admitted table: every profile on the system, not only the arm's.

    The fixture and its create-time probe share one modelled value. The rows the
    arm must not read list the slot, so selecting one of them instead reads a
    foreign assignment and the guards notice.
    """
    rows = [
        (lpar_name, dedicated_pcie._DEFAULT_DEDICATED_PROFILE, io_slots),
        (
            f"{lpar_name}-createtime",
            dedicated_pcie._DEFAULT_DEDICATED_PROFILE,
            io_slots,
        ),
        (lpar_name, "other_profile", f"{_DRC}/none/1"),
        (
            "vios-1",
            dedicated_pcie._DEFAULT_DEDICATED_PROFILE,
            f"{_DRC}/none/1,21030030/none/0",
        ),
    ]
    lines = ["lpar_name,name,io_slots"]
    lines += [f'{lpar},{name},"{value}"' for lpar, name, value in rows]
    return "\n".join(lines) + "\n"


#: The system's profiles when ST29 selects a slot, before any fixture exists:
#: another partition's profile lists a slot, but not the one the inventory offers.
_SELECTION_READBACK = (
    'lpar_name,name,io_slots\nvios-1,default_profile,"21030030/none/0"\n'
)


def _rendering_profile_reads(
    responses: dict[str, Any],
    marker_holder: dict[str, str],
    prefix: str,
    selection_readback: Any,
) -> dict[str, Any]:
    """Answer each admitted profile read with the table around the modelled value.

    A read before the run marker exists is ST29's slot selection, which sees the
    system as it stands rather than the fixture's modelled profile.
    """
    command_model = responses.get("hmc_run_command")
    if not callable(command_model):
        return responses

    def run_command(kwargs: dict[str, Any], index: int) -> Any:
        if kwargs["cmd"] == _PROFILE_READ and "marker" not in marker_holder:
            return selection_readback
        value = command_model(kwargs, index)
        if kwargs["cmd"] != _PROFILE_READ or not isinstance(value, str):
            return value
        return _admitted_readback(value, f"{prefix}{marker_holder['marker']}")

    return {**responses, "hmc_run_command": run_command}


async def _run_arm(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, Any],
    marker_holder: dict[str, str],
    statuses: dict[str, Any] | None = None,
    config: dict[str, str] | None = None,
    selection_readback: Any = _SELECTION_READBACK,
    inventory: bool = False,
) -> ScenarioState:
    live = LiveTestConfig(**(config if config is not None else _CONFIG))
    if not inventory:
        # The #630 read phase has its own tests below; the assignment tests here
        # pin the arm's mutation path and would otherwise each script its reads.
        async def no_reads(*_args: Any) -> None:
            return None

        monkeypatch.setattr(dedicated_pcie, "record_inventory_reads", no_reads)
        monkeypatch.setattr(
            dedicated_pcie, "record_dedicated_listing", lambda *_args: None
        )
    responses = _rendering_profile_reads(
        responses, marker_holder, live.dedicated_pcie_lpar_prefix, selection_readback
    )

    real_marker = dedicated_pcie._new_run_marker

    def capture() -> str:
        marker = real_marker()
        marker_holder["marker"] = marker
        return marker

    monkeypatch.setattr(dedicated_pcie, "_new_run_marker", capture)
    monkeypatch.setattr(dedicated_pcie, "_ABSENCE_REREAD_DELAY_S", 0)

    # The cleanup phase boundary: the arm reaches cleanup through this module
    # global, so wrapping it records where cleanup's calls begin.
    real_cleanup = dedicated_pcie.cleanup_dedicated

    async def marking_cleanup(client: Any, st: Any, fixture: Any) -> str | None:
        st.cleanup_start = len(st.calls)
        return await real_cleanup(client, st, fixture)

    monkeypatch.setattr(dedicated_pcie, "cleanup_dedicated", marking_cleanup)

    merged: dict[str, Any] = {"hmc_create_lpar": _probe_refused}
    merged.update(statuses or {})
    state = ScenarioState(responses, merged, config=live)
    await dedicated_pcie.exercise_dedicated_pcie_assignment(None, state)
    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_configuration_skips_without_any_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    state = await _run_arm(monkeypatch, {}, holder, config={})
    assert state.calls == []
    assert [status for _, _, status, _ in state.results] == ["SKIP"]


@pytest.mark.asyncio
async def test_delimiter_bearing_config_skips_without_any_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comma in the profile name would rewrite the HMC record grammar.

    The SKIP must happen before anything is created — a `build_filter` raise
    off a path where a partition already exists is what this test prevents.
    """
    holder: dict[str, str] = {}
    state = await _run_arm(
        monkeypatch,
        {},
        holder,
        config={**_CONFIG, "dedicated_pcie_profile_name": "prof,x"},
    )
    assert state.calls == []


@pytest.mark.asyncio
async def test_environment_outside_envelope_skips_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, system_model="9080-M9S")
    state = await _run_arm(monkeypatch, responses, holder)
    assert state.cleanup_start is None  # cleanup never ran (fixture not created)
    assert any(r[2] == "SKIP" for r in state.results)
    assert "hmc_list_dedicated_pcie_slots" not in state.tools()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hmc_version", "admitted"),
    [
        pytest.param(_ADMITTED_VERSION, True, id="admitted"),
        pytest.param(
            "Version: 10\nRelease: 3\nService Pack: 10600",
            False,
            id="service-pack-10600",
        ),
        pytest.param(
            "Version: 10\nRelease: 3\nService Pack: 1061\nMH01999 - HMC V10R3 M1060 iFix",
            False,
            id="later-pack-listing-m1060-ifix",
        ),
    ],
)
async def test_environment_gate_matches_release_fields_exactly(
    monkeypatch: pytest.MonkeyPatch, hmc_version: str, admitted: bool
) -> None:
    """ADR 0166 §3: the arm admits only what the product's exact predicate admits."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, hmc_version=hmc_version)
    state = await _run_arm(monkeypatch, responses, holder)
    assert ("hmc_list_dedicated_pcie_slots" in state.tools()) is admitted
    envelope = [r for r in state.results if r[1] == "dedicated admitted environment"]
    assert [r[2] for r in envelope] == ["PASS" if admitted else "SKIP"]


@pytest.mark.asyncio
async def test_no_unassigned_slot_skips_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, inventory_owner="someone")
    state = await _run_arm(monkeypatch, responses, holder)
    assert state.cleanup_start is None
    skip_rows = [r for r in state.results if r[2] == "SKIP"]
    assert any("slot" in r[1].lower() or "slot" in str(r[3]).lower() for r in skip_rows)


_LISTED_DRC = "21010021"


def _two_slot_inventory(_kwargs: dict[str, Any], _index: int) -> dict[str, Any]:
    """Two unowned slots; the first is the one a profile lists in the tests below."""
    return {
        "capability": "available",
        "items": [
            {"drc_index": _LISTED_DRC, "description": "listed", "owner_lpar": "null"},
            {"drc_index": _DRC, "description": "PCIe adapter", "owner_lpar": ""},
        ],
    }


def _selection_table(io_slots: str) -> str:
    return f'lpar_name,name,io_slots\nvios-1,default_profile,"{io_slots}"\n'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vios_io_slots",
    [
        pytest.param(f"{_LISTED_DRC}/none/0", id="listed"),
        pytest.param(f"21030030/none/0,{_LISTED_DRC}/none/1", id="listed-required"),
        # The #882 holder check refuses a slot a row it cannot parse mentions,
        # so selection must not pick that slot either.
        pytest.param(f"{_LISTED_DRC}//0", id="unparseable-mention"),
    ],
)
async def test_auto_selection_skips_a_slot_a_profile_lists(
    monkeypatch: pytest.MonkeyPatch, vios_io_slots: str
) -> None:
    """Unowned in inventory is not free: a profile can still list the slot (#916)."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    responses["hmc_list_dedicated_pcie_slots"] = _two_slot_inventory
    state = await _run_arm(
        monkeypatch,
        responses,
        holder,
        selection_readback=_selection_table(vios_io_slots),
    )
    row = state.row("dedicated slot selection")
    assert row is not None and row[2] == "PASS"
    assert f"selected drc_index={_DRC!r}" in str(row[3])
    assert not any(_LISTED_DRC in cmd for cmd in state.commands())


@pytest.mark.asyncio
async def test_unrelated_unparseable_profile_does_not_block_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only rows naming a slot decide it, as the holder check reads them."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    state = await _run_arm(
        monkeypatch,
        responses,
        holder,
        selection_readback=_selection_table("21030030//0"),
    )
    row = state.row("dedicated slot selection")
    assert row is not None and row[2] == "PASS"


@pytest.mark.asyncio
async def test_every_unowned_slot_listed_by_a_profile_skips_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    state = await _run_arm(
        monkeypatch, responses, holder, selection_readback=_selection_table(_ASSIGNED)
    )
    assert state.cleanup_start is None
    assert "hmc_create_lpar" not in state.tools()
    row = state.row("dedicated slot selection")
    assert row is not None and row[2] == "SKIP"
    assert "listed by a partition profile" in str(row[3])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "readback"),
    [
        pytest.param(
            {"hmc_run_command": _command_fails("-r prof")},
            _CONNECTION_LOST,
            id="failed",
        ),
        pytest.param(None, "not the admitted table\n", id="unadmitted"),
    ],
)
async def test_unreadable_profiles_skip_auto_selection(
    monkeypatch: pytest.MonkeyPatch, statuses: Any, readback: Any
) -> None:
    """Without the profile table, no slot can be shown to be listed by none."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    state = await _run_arm(
        monkeypatch, responses, holder, statuses=statuses, selection_readback=readback
    )
    assert "hmc_create_lpar" not in state.tools()
    row = state.row("dedicated slot selection")
    assert row is not None and row[2] == "SKIP"
    # The failure is a `CallFailure`, whose message alone `RunState.record`
    # persists, redacted: a lost connection reads apart from an unadmitted table
    # without a re-run, and raw HMC output never reaches the results file.
    failure = row[3]
    assert isinstance(failure, CallFailure)
    if isinstance(readback, CallFailure):
        assert failure is readback
    else:
        assert "profile io_slots readback response did not match" in failure.message
        assert readback.strip() not in failure.message


@pytest.mark.asyncio
async def test_pinned_slot_is_not_checked_against_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned slot keeps ADR 0166's contract: a profile listing it fails ST30/ST31."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    state = await _run_arm(
        monkeypatch,
        responses,
        holder,
        config={**_CONFIG, "dedicated_pcie_drc_index": _DRC},
        selection_readback=_selection_table(_ASSIGNED),
    )
    row = state.row("dedicated slot selection")
    assert row is not None and row[2] == "PASS"


@pytest.mark.asyncio
async def test_configured_drc_absent_from_inventory_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    state = await _run_arm(
        monkeypatch,
        responses,
        holder,
        config={**_CONFIG, "dedicated_pcie_drc_index": "999"},
    )
    assert state.cleanup_start is None
    assert any(
        "drc_index" in str(r[3]).lower() for r in state.results if r[2] == "SKIP"
    )


@pytest.mark.asyncio
async def test_refused_probe_create_is_a_fail_row_and_the_fixture_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    state = await _run_arm(monkeypatch, responses, holder)

    # No SKIP is declared for the create-time path any more (ADR 0166): a
    # refusal inside the envelope is a finding.
    probe_row = state.row("create-time dedicated assignment")
    assert probe_row is not None and probe_row[2] == "FAIL"
    # The readback answers HSCL8012 for the probe, so its absence is confirmed.
    assert state.row("create-time probe partition not confirmed absent") is None
    creates = [k for t, k in state.calls if t == "hmc_create_lpar"]
    assert any(not _is_probe(k) for k in creates), (
        "fixture create must have been called"
    )
    assert not any(
        str(k.get("lpar_name_or_uuid", "")).endswith("-createtime")
        for t, k in state.calls
        if t == "hmc_delete_lpar"
    )


@pytest.mark.asyncio
async def test_probe_create_with_failed_apply_step_is_recorded_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe create whose apply_profile step errored must not read as PASS (#997)."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, probe_exists=True)
    _probe_create_succeeds(responses, assignment_lands=False)
    base_create = responses["hmc_create_lpar"]

    def create_lpar(kwargs: dict[str, Any], index: int) -> Any:
        result = base_create(kwargs, index)
        if _is_probe(kwargs) and isinstance(result, dict):
            result = {
                **result,
                "workflow_completed": False,
                "steps": [
                    {"step": "create", "status": "ok"},
                    {
                        "step": "apply_profile",
                        "status": "error",
                        "result": "HMCCLIError: refused",
                    },
                ],
            }
        return result

    responses["hmc_create_lpar"] = create_lpar
    state = await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "PASS"}
    )

    probe_row = state.row("create-time dedicated assignment")
    assert probe_row is not None and probe_row[2] == "FAIL"
    # The call itself still reported PASS, so the probe is still treated as
    # created: no false "not confirmed absent" recovery row, and the arm
    # still proceeds to create the fixture and clean the probe up.
    assert state.row("create-time probe partition not confirmed absent") is None
    assert any(
        t == "hmc_delete_lpar" and k["lpar_name_or_uuid"] == "probe-uuid"
        for t, k in state.calls
    )
    assert any(t == "hmc_create_lpar" and not _is_probe(k) for t, k in state.calls)


@pytest.mark.asyncio
async def test_probe_uuid_is_read_from_a_dataclass_create_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed create result is a dataclass, not a dict; its UUID still names the probe (#1410)."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, probe_exists=True)
    _probe_create_succeeds(responses)
    base_create = responses["hmc_create_lpar"]

    def create_lpar(kwargs: dict[str, Any], index: int) -> Any:
        result = base_create(kwargs, index)
        return LparPcieWorkflowResult(
            resource_created=True,
            workflow_completed=True,
            lpar=result["lpar"],
            ownership_stamped=True,
            steps=(),
            warnings=(),
        )

    responses["hmc_create_lpar"] = create_lpar
    state = await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "PASS"}
    )

    assert any(
        t == "hmc_delete_lpar" and k["lpar_name_or_uuid"] == "probe-uuid"
        for t, k in state.calls
    )


@pytest.mark.asyncio
async def test_fixture_create_with_failed_apply_step_is_recorded_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixture create whose apply_profile step errored must not read as PASS (#997)."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    base_create = responses["hmc_create_lpar"]

    def create_lpar(kwargs: dict[str, Any], index: int) -> Any:
        result = base_create(kwargs, index)
        if not _is_probe(kwargs) and isinstance(result, dict):
            result = {
                **result,
                "workflow_completed": False,
                "steps": [
                    {"step": "create", "status": "ok"},
                    {
                        "step": "apply_profile",
                        "status": "error",
                        "result": "HMCCLIError: refused",
                    },
                ],
            }
        return result

    responses["hmc_create_lpar"] = create_lpar
    state = await _run_arm(monkeypatch, responses, holder)

    fixture_row = state.row("hmc_create_lpar (fixture)")
    assert fixture_row is not None and fixture_row[2] == "FAIL"
    # The call itself still reported PASS, so the arm still treats the
    # partition as created — ownership stamping still runs and cleanup
    # deletes it, rather than the arm being SKIPped as if nothing exists.
    stamp_row = state.row("fixture ownership stamp")
    assert stamp_row is not None and stamp_row[2] == "PASS"
    assert any(
        t == "hmc_delete_lpar" and k["lpar_name_or_uuid"] == "fixture-uuid"
        for t, k in state.cleanup_calls()
    )


def _index_of(state: ScenarioState, predicate: Any) -> int:
    return next(i for i, (t, k) in enumerate(state.calls) if predicate(t, k))


_LOOKUP_LOST = CallFailure(
    "HMCCLIError", "HMCCLIError: connection lost", "", None, False
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "probe_description",
    [pytest.param(_LOOKUP_LOST, id="unread"), pytest.param("", id="unstamped")],
)
async def test_probe_absence_that_cannot_be_confirmed_is_a_recovery_row(
    monkeypatch: pytest.MonkeyPatch, probe_description: Any
) -> None:
    """A failed or unstamped readback cannot rule out a partition the create made."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, probe_description=probe_description)
    state = await _run_arm(monkeypatch, responses, holder)

    check_row = state.row("create-time probe partition not confirmed absent")
    assert check_row is not None and check_row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(check_row[3])
    assert "-createtime" in str(check_row[3])
    # Final cleanup never retries this probe, so the row must say so (#906).
    assert "will not retry cleanup" in str(check_row[3])
    creates = [k for t, k in state.calls if t == "hmc_create_lpar"]
    assert any(not _is_probe(k) for k in creates), (
        "fixture create must have been called"
    )


@pytest.mark.asyncio
async def test_create_time_assignment_is_verified_and_removed_before_the_fixture_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, probe_exists=True)
    _probe_create_succeeds(responses)

    state = await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "PASS"}
    )

    row = state.row("create-time assignment profile readback")
    assert row is not None and row[2] == "PASS"
    probe_removal = _index_of(
        state,
        lambda t, k: (
            t == "hmc_run_command"
            and "io_slots-" in k["cmd"]
            and "-createtime" in k["cmd"]
        ),
    )
    probe_delete = _index_of(
        state,
        lambda t, k: t == "hmc_delete_lpar" and k["lpar_name_or_uuid"] == "probe-uuid",
    )
    fixture_create = _index_of(
        state, lambda t, k: t == "hmc_create_lpar" and not _is_probe(k)
    )
    assert probe_removal < probe_delete < fixture_create
    # Final cleanup has only the fixture left to remove.
    deletes = [k for t, k in state.cleanup_calls() if t == "hmc_delete_lpar"]
    assert [k["lpar_name_or_uuid"] for k in deletes] == ["fixture-uuid"]


@pytest.mark.asyncio
async def test_probe_whose_assignment_did_not_land_is_deleted_without_a_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, probe_exists=True)
    _probe_create_succeeds(responses, assignment_lands=False)

    state = await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "PASS"}
    )

    row = state.row("create-time assignment profile readback")
    assert row is not None and row[2] == "FAIL"
    assert state.notes[_CREATE_TIME_JUDGED] == "unmet: profile-lists-slot"
    assert not [c for c in state.commands() if "io_slots-" in c and "-createtime" in c]
    assert any(
        t == "hmc_delete_lpar" and k["lpar_name_or_uuid"] == "probe-uuid"
        for t, k in state.calls
    )
    assert any(t == "hmc_create_lpar" and not _is_probe(k) for t, k in state.calls)


@pytest.mark.asyncio
async def test_unfinished_probe_cleanup_blocks_the_fixture_and_is_retried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe's profile is unreadable at its ST30 cleanup, readable after.

    The fixture must not be created while the probe may still list the slot,
    and `probe_created` must stay set so the final cleanup retries the probe.
    """
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, probe_exists=True)
    _probe_create_succeeds(responses)
    probe_reads = {"n": 0}

    def status(_tool: str, kwargs: dict[str, Any], _index: int) -> str:
        # The admitted read names no partition, but the fixture is never
        # created here, so every profile read after ST29's slot selection and
        # before cleanup is the probe's.
        if kwargs.get("cmd", "") == _PROFILE_READ:
            probe_reads["n"] += 1
            return "FAIL" if probe_reads["n"] == 3 else "PASS"
        return "PASS"

    state = await _run_arm(
        monkeypatch,
        responses,
        holder,
        statuses={"hmc_create_lpar": "PASS", "hmc_run_command": status},
    )

    row = state.row("probe profile unreadable")
    assert row is not None and row[2] == "FAIL"
    judged = state.row(_CREATE_TIME_JUDGED)
    assert judged is not None and judged[2] == "FAIL"
    assert "cleanup failed" in state.notes[_CREATE_TIME_JUDGED]
    assert not any(t == "hmc_create_lpar" and not _is_probe(k) for t, k in state.calls)
    deletes = [k for t, k in state.cleanup_calls() if t == "hmc_delete_lpar"]
    assert [k["lpar_name_or_uuid"] for k in deletes] == ["probe-uuid"]
    assert [c for c in state.cleanup_commands() if "io_slots-" in c]


@pytest.mark.asyncio
async def test_probe_carrying_a_foreign_token_is_never_deleted_and_blocks_the_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(
        holder,
        probe_description="[hmcpctl owner:hmcpctl created:2026-09-02] [caller someone-else]",
    )
    _probe_create_succeeds(responses)

    state = await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "PASS"}
    )

    rows = [r for r in state.results if "probe run-marker mismatch" in r[1]]
    assert [r[2] for r in rows] == ["FAIL", "FAIL"], "ST30 attempt plus one retry"
    assert "MANUAL RECOVERY REQUIRED" in str(rows[0][3])
    assert "hmc_delete_lpar" not in state.tools()
    assert not [c for c in state.commands() if "io_slots-" in c]
    assert not any(t == "hmc_create_lpar" and not _is_probe(k) for t, k in state.calls)


def _fixture_absent_after_delete(responses: dict[str, Any]) -> None:
    """Answer HSCL8012 for the fixture once its delete has been issued."""
    describe = responses["hmc_get_lpar_description"]
    deleted: set[str] = set()

    def delete(kwargs: dict[str, Any], _index: int) -> str:
        deleted.add(str(kwargs["lpar_name_or_uuid"]))
        return "deleted"

    def get_description(kwargs: dict[str, Any], index: int) -> Any:
        if "fixture-uuid" in deleted and not _is_probe_name(kwargs):
            return _NOT_FOUND
        return describe(kwargs, index)

    responses["hmc_delete_lpar"] = delete
    responses["hmc_get_lpar_description"] = get_description


#: The judged-but-unobserved rows the dedicated arm records for the
#: create-time probe and the fixture delete.
_CREATE_TIME_JUDGED = "hmc_create_lpar (create-time judged)"
_DELETE_JUDGED = "hmc_delete_lpar (judged)"


def _emitted(state: ScenarioState) -> dict[str, tuple[str, str, str, str, list[str]]]:
    """Each observation by id: operation, scenario, result, cleanup, assertion ids."""
    emitted = {
        item["observation"]["id"]: (
            item["operation"],
            item["observation"]["scenario"],
            item["observation"]["result"],
            item["observation"]["cleanup"],
            sorted(item["observation"]["assertions"]),
        )
        for item in state.observations
    }
    assert len(emitted) == len(state.observations), (
        "a duplicate id discards the document"
    )
    return emitted


async def _run_dedicated_with_probe(
    monkeypatch: pytest.MonkeyPatch, *, fixture_absent: bool = True
) -> ScenarioState:
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, probe_exists=True)
    _probe_create_succeeds(responses)
    if fixture_absent:
        _fixture_absent_after_delete(responses)
    return await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "PASS"}
    )


@pytest.mark.asyncio
async def test_dedicated_arm_emits_verified_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = await _run_dedicated_with_probe(monkeypatch)

    scenario = "st29-dedicated-pcie"
    assert _emitted(state) == {
        "st31-hmc-assign-dedicated-pcie-slot": (
            "pcie.assign_dedicated_slot",
            scenario,
            "passed",
            "passed",
            ["assign-call-succeeded", "profile-lists-slot"],
        ),
        "st33-chsyscfg-io-slots-remove": (
            "command.run",
            scenario,
            "passed",
            "not-required",
            ["profile-restored-to-baseline", "remove-command-succeeded"],
        ),
        "st33-chsyscfg-io-slots-add": (
            "command.run",
            scenario,
            "passed",
            "passed",
            ["add-command-succeeded", "profile-lists-slot"],
        ),
    }


@pytest.mark.asyncio
async def test_dedicated_arm_judges_create_and_delete_without_observing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lpar-power arm owns both operations' observations (#1389)."""
    state = await _run_dedicated_with_probe(monkeypatch)

    operations = {item["operation"] for item in state.observations}
    assert not operations & {"lpar.create", "lpar.delete"}
    for label in (_CREATE_TIME_JUDGED, _DELETE_JUDGED):
        row = state.row(label)
        assert row is not None and row[2] == "PASS", label


@pytest.mark.asyncio
async def test_a_fixture_that_survives_its_delete_fails_the_additions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An assignment whose partition is still there was not undone by this run."""
    state = await _run_dedicated_with_probe(monkeypatch, fixture_absent=False)

    emitted = _emitted(state)
    delete = state.row(_DELETE_JUDGED)
    assert delete is not None and delete[2] == "FAIL"
    assert state.notes[_DELETE_JUDGED] == "unmet: lpar-name-absent"
    for key in ("st31-hmc-assign-dedicated-pcie-slot", "st33-chsyscfg-io-slots-add"):
        assert emitted[key][2:4] == ("failed", "failed")


@pytest.mark.asyncio
async def test_st31_assigns_through_the_operation_and_issues_no_raw_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    state = await _run_arm(monkeypatch, _happy_responses(holder), holder)

    calls = [k for t, k in state.calls if t == "hmc_assign_dedicated_pcie_slot"]
    assert len(calls) == 1
    assert calls[0]["drc_index"] == _DRC
    assert calls[0]["lpar_name_or_uuid"].startswith("live-")
    op_row = state.row("hmc_assign_dedicated_pcie_slot")
    assert op_row is not None and op_row[2] == "PASS"
    readback = state.row("profile io_slots readback (post-assign)")
    assert readback is not None and readback[2] == "PASS"
    # Only ST33's reassign issues the raw grammar.
    assert len([c for c in state.commands() if "io_slots+" in c]) == 1


@pytest.mark.asyncio
async def test_happy_path_removes_slot_then_deletes_by_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    state = await _run_arm(monkeypatch, _happy_responses(holder), holder)

    order = [
        index
        for index, (tool, kwargs) in enumerate(state.cleanup_calls())
        if tool == "hmc_delete_lpar"
        or (tool == "hmc_run_command" and "io_slots-" in kwargs["cmd"])
    ]
    assert order, "expected a cleanup removal and a delete"
    assert state.cleanup_calls()[order[-1]][0] == "hmc_delete_lpar"

    deletes = [k for t, k in state.calls if t == "hmc_delete_lpar"]
    assert len(deletes) == 1, (
        "the probe created nothing, so only the fixture is deleted"
    )
    assert deletes[0]["lpar_name_or_uuid"] == "fixture-uuid"
    assert "ownership_override" not in deletes[0]


@pytest.mark.asyncio
async def test_foreign_caller_token_blocks_every_cleanup_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(
        holder,
        description="[hmcpctl owner:hmcpctl created:2026-09-02] [caller someone-else]",
    )
    state = await _run_arm(monkeypatch, responses, holder)

    assert "hmc_delete_lpar" not in state.cleanup_tools()
    assert not [c for c in state.cleanup_commands() if "io_slots-" in c]
    row = state.row("run-marker mismatch")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])


@pytest.mark.asyncio
async def test_unreadable_identity_blocks_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard A: caller_token is None when the description read fails."""
    holder: dict[str, str] = {}
    state = await _run_arm(
        monkeypatch,
        _happy_responses(holder),
        holder,
        statuses={"hmc_get_lpar_description": "FAIL"},
    )

    assert "hmc_delete_lpar" not in state.cleanup_tools()
    row = state.row("run-marker mismatch")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])


@pytest.mark.asyncio
async def test_uuid_drift_between_reads_blocks_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard A: UUID changes between fixture create and cleanup read."""
    holder: dict[str, str] = {}
    # The fixture is created with fixture-uuid; the second hmc_get_lpar call
    # (Guard A's read inside _read_dedicated_state) returns other-uuid.
    responses = _happy_responses(holder, uuids=["fixture-uuid", "other-uuid"])
    state = await _run_arm(monkeypatch, responses, holder)

    assert "hmc_delete_lpar" not in state.cleanup_tools()
    row = state.row("uuid mismatch")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])


@pytest.mark.asyncio
async def test_no_uuid_resolved_blocks_arm_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST30: no UUID means no hardware mutation and no stranded slot."""
    holder: dict[str, str] = {}
    state = await _run_arm(
        monkeypatch, _happy_responses(holder, uuid_value=None), holder
    )

    # No io_slots mutations were issued
    assert not any("io_slots+" in cmd for cmd in state.commands())
    # The fixture was still created and cleaned up (Guard A refuses the delete)
    assert state.cleanup_start is not None


@pytest.mark.asyncio
async def test_ownership_stamp_not_landed_blocks_hardware_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST30: ownership_stamped=False means the caller segment was also lost.

    Guard A reads None from hmc_get_lpar_description and refuses the delete.
    No io_slots mutations must occur.
    """
    holder: dict[str, str] = {}
    # ownership_stamped=False → the caller segment is lost too (per lifecycle.py)
    responses = _happy_responses(
        holder,
        ownership_stamped=False,
        description="[hmcpctl owner:hmcpctl created:2026-09-02]",  # no caller segment
    )
    state = await _run_arm(monkeypatch, responses, holder)

    # No chsyscfg mutations (no io_slots+ or io_slots-)
    assert not any(
        ("io_slots+" in cmd or "io_slots-" in cmd) for cmd in state.commands()
    )

    # Guard A refuses the delete, with a recovery row
    assert "hmc_delete_lpar" not in state.cleanup_tools()
    row = state.row("run-marker mismatch")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])


@pytest.mark.asyncio
async def test_profile_drift_to_unknown_value_blocks_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard B: io_slots is neither the baseline nor the value this run applied."""
    holder: dict[str, str] = {}

    # Full run: assign+unassign+reassign completes. applied_io_slots=_ASSIGNED.
    # Guard A's profile read returns a third value (neither baseline "none" nor
    # applied _ASSIGNED). Guard B must emit "profile drift" FAIL and refuse the
    # delete. The key: apply the slots model normally (so applied_io_slots gets
    # set), but switch to a third value starting from profile read #6 (Guard A).
    profile_read_count = {"n": 0}
    slots = {"io_slots": "none"}

    def run_cmd_third_value(kwargs: dict[str, Any], _index: int) -> str:
        cmd = kwargs["cmd"]
        if cmd == "lshmc -V":
            return _ADMITTED_VERSION
        if "-r sys " in cmd and "type_model" in cmd:
            return _ADMITTED_MODEL
        if "io_slots+" in cmd:
            slots["io_slots"] = _ASSIGNED
            return ""
        if "io_slots-" in cmd:
            slots["io_slots"] = "none"
            return ""
        # Profile reads: return normally for reads 1–5, then "99999999//0" from #6 on.
        # Read #6 is Guard A's profile read in the full happy path.
        profile_read_count["n"] += 1
        if profile_read_count["n"] >= 6:
            return "99999999//0"
        return slots["io_slots"]

    responses = _happy_responses(holder, run_command=run_cmd_third_value)
    state = await _run_arm(monkeypatch, responses, holder)

    assert "hmc_delete_lpar" not in state.cleanup_tools()
    row = state.row("profile drift")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])


@pytest.mark.asyncio
async def test_removal_does_not_restore_baseline_blocks_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard B: slot is removed but post-removal read does not equal baseline."""
    holder: dict[str, str] = {}

    # The io_slots- command returns PASS but the model stays at _ASSIGNED
    slots = {"io_slots": "none"}

    def run_cmd_stuck(kwargs: dict[str, Any], _index: int) -> str:
        cmd = kwargs["cmd"]
        if cmd == "lshmc -V":
            return _ADMITTED_VERSION
        if "-r sys " in cmd and "type_model" in cmd:
            return _ADMITTED_MODEL
        if "io_slots+" in cmd:
            slots["io_slots"] = _ASSIGNED
            return ""
        # io_slots- does NOT clear: the removal appears to succeed but the
        # HMC did not actually change the profile
        if "io_slots-" in cmd:
            return ""
        return slots["io_slots"]

    responses = _happy_responses(holder, run_command=run_cmd_stuck)
    state = await _run_arm(monkeypatch, responses, holder)

    assert "hmc_delete_lpar" not in state.cleanup_tools()
    row = state.row("baseline not restored")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])


@pytest.mark.asyncio
async def test_cleanup_removes_slot_whose_assign_response_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operation's write applied and then reported FAIL.

    `assign_dedicated_slot` must read back anyway, so `applied_io_slots`
    records what the profile holds. Restore the early `return` before that
    readback and cleanup can no longer prove the deviation is its own: it
    refuses the delete, and this test goes red.
    """
    holder: dict[str, str] = {}
    state = await _run_arm(
        monkeypatch,
        _happy_responses(holder),
        holder,
        statuses={"hmc_assign_dedicated_pcie_slot": "FAIL"},
    )

    assert [c for c in state.cleanup_commands() if "io_slots-" in c]
    assert "hmc_delete_lpar" in state.cleanup_tools()
    order = [
        index
        for index, (tool, kwargs) in enumerate(state.cleanup_calls())
        if tool == "hmc_delete_lpar"
        or (tool == "hmc_run_command" and "io_slots-" in kwargs["cmd"])
    ]
    assert state.cleanup_calls()[order[-1]][0] == "hmc_delete_lpar"


@pytest.mark.asyncio
async def test_cleanup_refuses_when_confirming_read_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST29's selection and ST30's baseline reads succeed; every profile read after fails.

    So the assign applies, `applied_io_slots` is never set, and the live
    `io_slots` at cleanup is unreadable. Guard B must enter on the baseline
    comparison and refuse — a Guard B gated on `applied_io_slots is not None`
    would skip the branch and delete, and this test goes red.
    """
    holder: dict[str, str] = {}
    state = await _run_arm(
        monkeypatch,
        _happy_responses(holder),
        holder,
        statuses={"hmc_run_command": _nth_matching_command_fails(_PROFILE_READ, 3)},
    )

    assert "hmc_delete_lpar" not in state.cleanup_tools()
    assert not [c for c in state.cleanup_commands() if "io_slots-" in c]
    row = state.row("profile drift")
    assert row is not None and row[2] == "FAIL"


@pytest.mark.asyncio
async def test_identity_drift_before_delete_blocks_guard_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard C: caller token changes between slot removal and deletion.

    The happy path makes exactly three description reads *of the fixture*:
      - ST32's _read_dedicated_state (verify_dedicated_assigned)
      - Guard A's _read_dedicated_state
      - Guard C's _read_dedicated_state
    The foreign stamp must be at index 2 so Guard A sees the correct token
    and Guard C sees the foreign one. Reads of the *probe* name are the
    refused-create absence check and are counted separately, so adding or
    removing one cannot silently shift which read gets the foreign stamp.
    """
    holder: dict[str, str] = {}

    def make_description() -> list[str]:
        # Two matching stamps, then the foreign one on the third read.
        # The index clamps at the last entry, so read 3 onward is the foreign one.
        base = "[hmcpctl owner:hmcpctl created:2026-09-02] [caller {token}]"
        foreign = "[hmcpctl owner:hmcpctl created:2026-09-02] [caller someone-else]"
        return [base, base, foreign]

    descriptions = make_description()
    fixture_reads = {"n": 0}

    def get_description(kwargs: dict[str, Any], _index: int) -> Any:
        if _is_probe_name(kwargs):
            return _NOT_FOUND
        token = holder.get("marker", "")
        entry = descriptions[min(fixture_reads["n"], len(descriptions) - 1)]
        fixture_reads["n"] += 1
        return entry.format(token=token)

    responses = _happy_responses(holder)
    responses["hmc_get_lpar_description"] = get_description

    state = await _run_arm(monkeypatch, responses, holder)

    assert "hmc_delete_lpar" not in state.cleanup_tools()
    row = state.row("identity changed before delete")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])


@pytest.mark.asyncio
async def test_profile_drift_before_delete_blocks_guard_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard C: profile io_slots changes after Guard A and Guard B pass.

    A foreign value only on the last profile read means Guard C's comparison
    fails even though Guard A saw the baseline.
    """
    holder: dict[str, str] = {}
    # In the full happy-path run, Guard C's _read_dedicated_state calls
    # _read_profile_io_slots on profile read #9 (1-indexed). Returning a foreign
    # value there while Guard A (#11) and Guard B's confirming read (#12) both see
    # the baseline means only Guard C's comparison fails.
    profile_read_count = {"n": 0}
    slots = {"io_slots": "none"}

    def run_cmd_guard_c_drift(kwargs: dict[str, Any], _index: int) -> str:
        cmd = kwargs["cmd"]
        if cmd == "lshmc -V":
            return _ADMITTED_VERSION
        if "-r sys " in cmd and "type_model" in cmd:
            return _ADMITTED_MODEL
        if "io_slots+" in cmd:
            slots["io_slots"] = _ASSIGNED
            return ""
        if "io_slots-" in cmd:
            slots["io_slots"] = "none"
            return ""
        profile_read_count["n"] += 1
        # Profile read #9 is Guard C's in the full happy-path run:
        # #1 ST30-baseline, #2 ST31-post-assign, #3 ST32-verify,
        # #4 ST33-unassign-confirm, #5 ST33-post-reassign, #6 ST36 spare-slot
        # selection (one slot inventoried, so the scenario SKIPs), #7 Guard-A,
        # #8 Guard-B-confirm, #9 Guard-C.
        if profile_read_count["n"] == 9:
            return "21030030/none/0"
        return slots["io_slots"]

    responses = _happy_responses(holder, run_command=run_cmd_guard_c_drift)
    state = await _run_arm(monkeypatch, responses, holder)

    assert "hmc_delete_lpar" not in state.cleanup_tools()
    row = state.row("profile changed before delete")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])


@pytest.mark.asyncio
async def test_never_assigned_no_cleanup_slot_removal_delete_happens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When io_slots+ is a no-op the profile stays at baseline.

    Guard B must NOT issue an io_slots- (the baseline comparison is satisfied),
    and the delete must still happen.
    """
    holder: dict[str, str] = {}

    def run_cmd_noop_assign(kwargs: dict[str, Any], _index: int) -> str:
        cmd = kwargs["cmd"]
        if cmd == "lshmc -V":
            return _ADMITTED_VERSION
        if "-r sys " in cmd and "type_model" in cmd:
            return _ADMITTED_MODEL
        # io_slots+ does nothing — profile stays "none"
        return "none"

    responses = _happy_responses(holder, run_command=run_cmd_noop_assign)
    state = await _run_arm(monkeypatch, responses, holder)

    # No cleanup slot removal (the profile was never drifted)
    assert not [c for c in state.cleanup_commands() if "io_slots-" in c]
    # The fixture is still deleted
    assert "hmc_delete_lpar" in state.cleanup_tools()


@pytest.mark.asyncio
async def test_run_marker_uniqueness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls to _new_run_marker must return distinct values."""
    m1 = dedicated_pcie._new_run_marker()
    m2 = dedicated_pcie._new_run_marker()
    assert m1 != m2
    assert m1.startswith("pcie-")
    assert len(m1) == len("pcie-") + 8


@pytest.mark.asyncio
async def test_fixture_lpar_name_starts_with_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    state = await _run_arm(monkeypatch, _happy_responses(holder), holder)
    creates = [k for t, k in state.calls if t == "hmc_create_lpar" and not _is_probe(k)]
    assert creates
    assert creates[0]["name"].startswith("live-")


@pytest.mark.asyncio
async def test_arm_records_what_it_created_into_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery reads these four from the results document, not from prose."""
    holder: dict[str, str] = {}
    state = await _run_arm(monkeypatch, _happy_responses(holder), holder)

    assert state.artifacts.pcie_run_marker == holder["marker"]
    assert state.artifacts.pcie_fixture_lpar == f"live-{holder['marker']}"
    assert state.artifacts.pcie_drc_index == _DRC
    assert state.artifacts.pcie_baseline_io_slots is not None


@pytest.mark.asyncio
async def test_a_fixture_abandoned_before_the_baseline_is_still_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run that ends between ST29 and ST30 is the one recovery is for.

    The partition exists and cleanup could not confirm it away, so the marker
    and name have to reach the document even though no baseline was captured.
    """
    holder: dict[str, str] = {}
    state = await _run_arm(
        monkeypatch, _happy_responses(holder, uuid_value=None), holder
    )

    assert state.artifacts.pcie_run_marker == holder["marker"]
    assert state.artifacts.pcie_fixture_lpar == f"live-{holder['marker']}"
    assert state.artifacts.pcie_drc_index == _DRC
    assert state.artifacts.pcie_baseline_io_slots is None


@pytest.mark.asyncio
async def test_a_skipped_arm_records_no_pcie_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was created, so recovery must not be told to look for one."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, system_model="9080-M9S")
    state = await _run_arm(monkeypatch, responses, holder)

    assert state.artifacts.pcie_run_marker is None
    assert state.artifacts.pcie_fixture_lpar is None


# ---------------------------------------------------------------------------
# A create whose response was lost has still created the partition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("io_slots", "drc", "expected"),
    [
        ("none", "21010020", False),
        ("21010020/none/0", "21010020", True),
        ("21010020/none/0,21030030/none/1", "21030030", True),
        # The substring traps: a shorter index inside a listed one, and a run
        # of characters spanning the `/` and `,` joins.
        ("21010020/none/0", "2101002", False),
        ("21010020/none/0", "none", False),
        ("21010020/none/0,21030030/none/1", "0,2103", False),
    ],
)
def test_io_slots_membership_is_by_entry_not_substring(
    io_slots: str, drc: str, expected: bool
) -> None:
    """A DRC index counts as present only as a whole first field of an entry."""
    assert dedicated_pcie._io_slots_contains(io_slots, drc) is expected


@pytest.mark.parametrize(
    "io_slots", ["21010020//0", "210100201/none/0", "21010020/none", ""]
)
def test_io_slots_membership_refuses_an_unadmitted_rendering(io_slots: str) -> None:
    """The documented input grammar's empty pool is not the read rendering (ADR 0165)."""
    with pytest.raises(
        dedicated_pcie.HMCCLIError, match="unadmitted io_slots rendering"
    ):
        dedicated_pcie._io_slots_contains(io_slots, "21010020")


@pytest.mark.asyncio
async def test_every_profile_read_is_the_admitted_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0165 Decision 1: three fields, `--header`, and no `--filter`."""
    holder: dict[str, str] = {}
    state = await _run_arm(monkeypatch, _happy_responses(holder), holder)

    reads = [c for c in state.commands() if c.startswith("lssyscfg -r prof")]
    assert reads
    assert set(reads) == {_PROFILE_READ}
    assert "hmc_delete_lpar" in state.cleanup_tools()


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        pytest.param(_admitted_readback(_ASSIGNED, "live-x"), _ASSIGNED, id="assigned"),
        pytest.param(_admitted_readback("none", "live-x"), "none", id="no-slots"),
        pytest.param(
            "lpar_name,name,io_slots\nvios-1,default_profile,none\n",
            None,
            id="no-row-for-the-profile",
        ),
        pytest.param(
            "lpar_name,name,io_slots\nlive-x,default_profile,none\n"
            "live-x,default_profile,none\n",
            None,
            id="two-rows-for-the-profile",
        ),
        pytest.param("live-x,default_profile,none\n", None, id="headerless"),
        pytest.param("", None, id="empty"),
        pytest.param(
            f"lpar_name,name,io_slots\nlive-x,default_profile,{_DRC}//0\n",
            None,
            id="empty-pool",
        ),
    ],
)
@pytest.mark.asyncio
async def test_profile_read_selects_exactly_the_arms_row(
    output: str, expected: str | None
) -> None:
    """The value of the one `lpar_name`/`name` row, or unreadable."""
    state = ScenarioState({"hmc_run_command": output})
    arm = dedicated_pcie._DedicatedConfig(
        "sys-one", "live-", dedicated_pcie._DEFAULT_DEDICATED_PROFILE, _DRC
    )
    fixture = dedicated_pcie._DedicatedFixture(arm, "x", "live-x", "live-x-createtime")

    assert await dedicated_pcie._read_profile_io_slots(None, state, fixture) == expected
    assert state.commands() == [_PROFILE_READ]


@pytest.mark.asyncio
async def test_fixture_create_reporting_failure_but_creating_is_cleaned_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out fixture create that in fact created must not orphan it.

    The SSH transport raises when its timeout expires, after the HMC has
    already made the partition. Believing a failed create created nothing is
    the belief that leaves a partition behind with no manual-recovery row.
    """
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    state = await _run_arm(
        monkeypatch,
        responses,
        holder,
        # Both creates report failure; the description read still finds the
        # fixture carrying this run's marker.
        statuses={"hmc_create_lpar": "FAIL"},
    )
    row = state.row("created a partition")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])
    # The arm still SKIPs, but cleanup ran and deleted what was created.
    assert state.cleanup_start is not None
    assert "hmc_delete_lpar" in state.cleanup_tools()


@pytest.mark.asyncio
async def test_fixture_create_failure_with_no_partition_skips_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readback's other answer: nothing was created, so nothing is cleaned."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    # The HMC has no partition of either name.
    responses["hmc_get_lpar_description"] = lambda _k, _n: _NOT_FOUND
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(dedicated_pcie.asyncio, "sleep", record_sleep)
    state = await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "FAIL"}
    )
    assert state.cleanup_start is None
    assert "hmc_delete_lpar" not in [t for t, _ in state.calls]
    skip = state.row("dedicated fixture create")
    assert skip is not None and skip[2] == "SKIP"
    assert "nothing to clean up" in str(skip[3])
    # Each failed create waits once, then re-reads once: absence takes two
    # HSCL8012 answers, and never a third lookup (#906).
    lookups = [
        k["lpar_name_or_uuid"]
        for t, k in state.calls
        if t == "hmc_get_lpar_description"
    ]
    assert len(lookups) == 4 and len(set(lookups)) == 2
    assert all(lookups.count(name) == 2 for name in lookups)
    assert delays == [dedicated_pcie._ABSENCE_REREAD_DELAY_S] * 2


def _not_found_first(responses: dict[str, Any], probe: bool) -> None:
    """The partition's first lookup answers HSCL8012; later ones see it.

    A create still in flight when the readback runs, or a CLI view lagging
    REST, answers HSCL8012 for a partition that then appears (#906).
    """
    get_description = responses["hmc_get_lpar_description"]
    seen = {"n": 0}

    def description(kwargs: dict[str, Any], index: int) -> Any:
        if _is_probe_name(kwargs) is probe:
            seen["n"] += 1
            if seen["n"] == 1:
                return _NOT_FOUND
        return get_description(kwargs, index)

    responses["hmc_get_lpar_description"] = description


@pytest.mark.asyncio
async def test_probe_in_flight_on_first_lookup_is_found_by_the_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One HSCL8012 is not absence: the delayed re-read finds the probe."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder, probe_exists=True)
    _not_found_first(responses, probe=True)
    state = await _run_arm(monkeypatch, responses, holder)
    row = state.row("create-time probe created a partition despite")
    assert row is not None and row[2] == "FAIL"
    deletes = [k for t, k in state.calls if t == "hmc_delete_lpar"]
    assert any(
        str(k.get("lpar_name_or_uuid", "")).endswith("-createtime") for k in deletes
    ), "the probe partition the re-read found must be deleted"


@pytest.mark.asyncio
async def test_fixture_in_flight_on_first_lookup_is_found_by_the_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixture's failed create gets the same re-read as the probe's."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    _not_found_first(responses, probe=False)
    state = await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "FAIL"}
    )
    row = state.row("fixture create reported failure but created a partition")
    assert row is not None and row[2] == "FAIL"
    assert "nothing to clean up" not in " ".join(str(r[3]) for r in state.results)
    assert "hmc_delete_lpar" in state.cleanup_tools()


@pytest.mark.asyncio
async def test_fixture_create_failure_with_unconfirmed_absence_is_a_recovery_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lookup that failed some other way cannot say nothing was created."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    responses["hmc_get_lpar_description"] = lambda _k, _n: _LOOKUP_LOST
    state = await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "FAIL"}
    )
    row = state.row("fixture partition not confirmed absent")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])
    assert "live-" in str(row[3])
    assert "nothing to clean up" not in " ".join(str(r[3]) for r in state.results)
    assert "hmc_delete_lpar" not in [t for t, _ in state.calls]


@pytest.mark.asyncio
async def test_probe_create_reporting_failure_but_creating_is_cleaned_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe may hold the dedicated slot, so a lost response is worse there."""
    holder: dict[str, str] = {}
    # The probe exists despite its create reporting failure.
    responses = _happy_responses(holder, probe_exists=True)
    state = await _run_arm(monkeypatch, responses, holder)
    row = state.row("create-time probe created a partition despite")
    assert row is not None and row[2] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in str(row[3])
    deletes = [k for t, k in state.calls if t == "hmc_delete_lpar"]
    assert any(
        str(k.get("lpar_name_or_uuid", "")).endswith("-createtime") for k in deletes
    ), "the probe partition must be deleted, not just the fixture"


@pytest.mark.asyncio
async def test_foreign_partition_of_the_same_name_is_never_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readback claims a partition only when it carries this run's marker."""
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    responses["hmc_get_lpar_description"] = lambda _k, _n: (
        "[hmcpctl owner:hmcpctl created:2026-09-02] [caller someone-else]"
    )
    state = await _run_arm(
        monkeypatch, responses, holder, statuses={"hmc_create_lpar": "FAIL"}
    )
    assert state.cleanup_start is None
    assert "hmc_delete_lpar" not in [t for t, _ in state.calls]
    # A partition of that name held by someone else rules out one of this run's.
    assert state.row("create-time probe partition not confirmed absent") is None


# ---------------------------------------------------------------------------
# ST36 — io_slots scenario (#912)
# ---------------------------------------------------------------------------

_SLOT_B = "21010021"
_SLOT_C = "21010022"
_IO_SLOTS_IDS = {
    "st36-hmc-assign-dedicated-pcie-slot",
    "st36-hmc-unassign-dedicated-pcie-slot",
    "st36-chsyscfg-io-slots-remove-required",
    "st36-chsyscfg-io-slots-remove-last",
}


def _io_slots_world(
    holder: dict[str, str], *, c_suffix: str = "0"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A three-slot system whose fixture profile is a list of rendered elements.

    `io_slots+=X//R` appends `X/none/R`; `io_slots-=X//R` removes X's element
    whatever its `is_required`, which is the answer the #879 window observed.
    *c_suffix* renders the operation's add of C, so a test can make the HMC
    store it differently from what the operation wrote.
    """
    model: dict[str, Any] = {"elements": [], "unreadable": False}

    def change(cmd: str) -> None:
        match = re.search(r"io_slots([+-])=(\w+)//(\d)", cmd)
        assert match is not None, cmd
        sign, drc, required = match.groups()
        if sign == "+":
            model["elements"].append(f"{drc}/none/{required}")
        else:
            model["elements"] = [
                e for e in model["elements"] if not e.startswith(f"{drc}/")
            ]

    def run_command(kwargs: dict[str, Any], _index: int) -> str:
        cmd = kwargs["cmd"]
        if cmd == "lshmc -V":
            return _ADMITTED_VERSION
        if "-r sys " in cmd and "type_model" in cmd:
            return _ADMITTED_MODEL
        if "io_slots" in cmd and cmd.startswith("chsyscfg"):
            change(cmd)
            return ""
        if model["unreadable"]:
            return "not the admitted table"
        return ",".join(model["elements"]) or "none"

    def assign(kwargs: dict[str, Any], _index: int) -> None:
        suffix = c_suffix if kwargs["drc_index"] == _SLOT_C else "0"
        change(f"chsyscfg io_slots+={kwargs['drc_index']}//{suffix}")

    def unassign(kwargs: dict[str, Any], _index: int) -> None:
        change(f"chsyscfg io_slots-={kwargs['drc_index']}//0")

    responses = _happy_responses(holder, run_command=run_command)
    responses["hmc_list_dedicated_pcie_slots"] = lambda _k, _n: {
        "capability": "available",
        "items": [
            {"drc_index": drc, "description": "PCIe adapter", "owner_lpar": ""}
            for drc in (_DRC, _SLOT_B, _SLOT_C)
        ],
    }
    responses["hmc_assign_dedicated_pcie_slot"] = assign
    responses["hmc_unassign_dedicated_pcie_slot"] = unassign
    _fixture_absent_after_delete(responses)
    return responses, model


def _unreadable_rendering(responses: dict[str, Any], model: dict[str, Any]) -> None:
    """Keep "not the admitted table" out of `_rendering_profile_reads`' table."""
    inner = responses["hmc_run_command"]

    def run_command(kwargs: dict[str, Any], index: int) -> Any:
        value = inner(kwargs, index)
        return _LOOKUP_LOST if value == "not the admitted table" else value

    responses["hmc_run_command"] = run_command


@pytest.mark.asyncio
async def test_io_slots_scenario_emits_verified_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses, model = _io_slots_world(holder)
    state = await _run_arm(monkeypatch, responses, holder)

    emitted = {k: v for k, v in _emitted(state).items() if k.startswith("st36-")}
    scenario = "st36-io-slots"
    assert emitted == {
        "st36-hmc-assign-dedicated-pcie-slot": (
            "pcie.assign_dedicated_slot",
            scenario,
            "passed",
            "passed",
            [
                "added-slot-renders-none-pool",
                "other-slots-stable-on-add",
                "zero-suffix-add-accepted",
            ],
        ),
        "st36-hmc-unassign-dedicated-pcie-slot": (
            "pcie.unassign_dedicated_slot",
            scenario,
            "passed",
            "not-required",
            ["other-slots-stable-on-remove", "zero-suffix-remove-accepted"],
        ),
        "st36-chsyscfg-io-slots-remove-required": (
            "command.run",
            scenario,
            "passed",
            "not-required",
            ["remaining-slot-stable", "required-slot-removed-by-zero-suffix"],
        ),
        "st36-chsyscfg-io-slots-remove-last": (
            "command.run",
            scenario,
            "passed",
            "not-required",
            ["empty-profile-reads-none", "remove-command-succeeded"],
        ),
    }
    assert f"io_slots+={_SLOT_B}//1" in " ".join(state.commands())
    assert model["elements"] == []
    # Step 5 left the profile at its baseline, so cleanup deletes without a removal.
    assert not [c for c in state.cleanup_commands() if "io_slots-" in c]
    assert "hmc_delete_lpar" in state.cleanup_tools()


@pytest.mark.asyncio
async def test_io_slots_rendering_fault_fails_the_add_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C stored as `C/none/1` is not the `//0` rendering the operation relies on."""
    holder: dict[str, str] = {}
    responses, model = _io_slots_world(holder, c_suffix="1")
    state = await _run_arm(monkeypatch, responses, holder)

    emitted = _emitted(state)
    add = emitted["st36-hmc-assign-dedicated-pcie-slot"]
    assert add[2] == "failed"
    assert "added-slot-renders-none-pool" not in add[4]
    assert not _IO_SLOTS_IDS - {"st36-hmc-assign-dedicated-pcie-slot"} & set(emitted)
    # The restore removed C and B with the suffix each renders.
    restores = [c for c in state.commands() if "io_slots-" in c and _DRC not in c]
    assert [re.search(r"io_slots-=(\S+?),", c).group(1) for c in restores] == [
        f"{_SLOT_C}//1",
        f"{_SLOT_B}//1",
    ]
    assert model["elements"] == []


@pytest.mark.asyncio
async def test_io_slots_unreadable_after_a_failed_remove_restores_both_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses, model = _io_slots_world(holder)
    unassign = responses["hmc_unassign_dedicated_pcie_slot"]

    def lost_unassign(kwargs: dict[str, Any], index: int) -> Any:
        unassign(kwargs, index)
        model["unreadable"] = True
        return _LOOKUP_LOST

    responses["hmc_unassign_dedicated_pcie_slot"] = lost_unassign
    _unreadable_rendering(responses, model)
    state = await _run_arm(monkeypatch, responses, holder)

    emitted = _emitted(state)
    assert emitted["st36-hmc-unassign-dedicated-pcie-slot"][2] == "failed"
    assert "st36-chsyscfg-io-slots-remove-required" not in emitted
    restores = [c for c in state.commands() if "io_slots-" in c and _DRC not in c]
    assert [re.search(r"io_slots-=(\S+?),", c).group(1) for c in restores][:2] == [
        f"{_SLOT_C}//0",
        f"{_SLOT_B}//1",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "inventory"),
    [
        pytest.param({**_CONFIG, "dedicated_pcie_drc_index": _DRC}, 3, id="pinned"),
        pytest.param(_CONFIG, 2, id="one-spare"),
    ],
)
async def test_io_slots_scenario_skips_without_two_selectable_spares(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, str], inventory: int
) -> None:
    holder: dict[str, str] = {}
    responses, _model = _io_slots_world(holder)
    slots = (_DRC, _SLOT_B, _SLOT_C)[:inventory]
    responses["hmc_list_dedicated_pcie_slots"] = lambda _k, _n: {
        "capability": "available",
        "items": [
            {"drc_index": d, "description": "x", "owner_lpar": ""} for d in slots
        ],
    }
    state = await _run_arm(monkeypatch, responses, holder, config=config)

    row = state.row("io_slots scenario")
    assert row is not None and row[2] == "SKIP"
    assert not [c for c in state.commands() if _SLOT_B in c or _SLOT_C in c]
    assert not _IO_SLOTS_IDS & set(_emitted(state))


# ---------------------------------------------------------------------------
# Read-only inventory phase — st29-pcie-inventory (#630)
# ---------------------------------------------------------------------------

_SLOT_ETH = "21010030"
_ARM = dedicated_pcie._DedicatedConfig("sys-one", "live-", "default_profile", None)


def _dedicated_listing(owner: str = "") -> dict[str, Any]:
    return {
        "capability": "available",
        "items": [
            {"drc_index": _DRC, "description": "x", "owner_lpar": owner},
            {"drc_index": _SLOT_ETH, "description": "y", "owner_lpar": "vios-1"},
        ],
    }


def _inventory_world() -> dict[str, Any]:
    """Every inventory read as the served tools return it, on the JSON wire.

    Decimals arrive as strings, never as `Decimal`, so a parse defect shows here.
    """
    io_rows = [
        {"drc_index": _DRC, "pci_class": "0104"},
        {"drc_index": _SLOT_ETH, "pci_class": "0200"},
    ]

    def io_slots(kwargs: dict[str, Any], _n: int) -> list[dict[str, Any]]:
        if kwargs.get("pci_class") == "eth":
            return [row for row in io_rows if row["pci_class"] == "0200"]
        return io_rows

    adapters = [
        {"adapter_id": "1", "mode": "sriov", "availability": "1"},
        {"adapter_id": None, "mode": "dedicated", "availability": "1"},
    ]

    def sriov_adapters(kwargs: dict[str, Any], _n: int) -> dict[str, Any]:
        selected = kwargs.get("adapter_id")
        items = [a for a in adapters if selected is None or a["adapter_id"] == selected]
        return {"capability": "available", "items": items}

    def physical_ports(kwargs: dict[str, Any], _n: int) -> Any:
        if kwargs.get("adapter_id") is None:
            return CallFailure(
                "ValueError",
                "ValueError: adapter_id is required for SR-IOV physical-port inventory",
                "",
                None,
                False,
            )
        return {
            "capability": "available",
            "items": [
                {
                    "adapter_id": "1",
                    "physical_port_id": "0",
                    "minimum_capacity_granularity_percent": "2.0",
                },
                {
                    "adapter_id": "1",
                    "physical_port_id": "1",
                    "minimum_capacity_granularity_percent": None,
                },
            ],
        }

    logical_ports = {
        "capability": "available",
        "items": [
            {
                "adapter_id": "1",
                "physical_port_id": "0",
                "availability": "1",
                "capacity_percent": "2.0",
                "maximum_capacity_percent": "100.0",
            },
            {
                "adapter_id": "1",
                "physical_port_id": "1",
                "availability": "unconfigured",
                "capacity_percent": None,
                "maximum_capacity_percent": None,
            },
        ],
    }
    return {
        "hmc_list_io_slots": io_slots,
        "hmc_list_sriov_adapters": sriov_adapters,
        "hmc_list_sriov_physical_ports": physical_ports,
        "hmc_list_sriov_logical_ports": lambda _k, _n: logical_ports,
        "hmc_run_command": lambda _k, _n: "lpar-v\nlpar-v\n",
        "hmc_list_vnics": lambda kwargs, _n: [
            {"lpar_name": kwargs["lpar_name_or_uuid"], "slot_num": "6"}
        ],
    }


async def _read_inventory(
    responses: dict[str, Any], listing: Any = None
) -> ScenarioState:
    state = ScenarioState(responses)
    data = _dedicated_listing() if listing is None else listing
    dedicated_pcie.record_dedicated_listing(state, "PASS", data)
    await dedicated_pcie.record_inventory_reads(None, state, _ARM, data)
    return state


_INVENTORY = "st29-pcie-inventory"


@pytest.mark.asyncio
async def test_inventory_reads_emit_verified_observations() -> None:
    state = await _read_inventory(_inventory_world())

    assert _emitted(state) == {
        "st29-hmc-list-dedicated-pcie-slots": (
            "pcie.list_dedicated_slots",
            _INVENTORY,
            "passed",
            "not-required",
            ["owners-normalized", "slot-rows-identified"],
        ),
        "st29-hmc-list-io-slots": (
            "io_slot.list",
            _INVENTORY,
            "passed",
            "not-required",
            [
                "class-filter-exact",
                "matches-dedicated-inventory",
                "slot-rows-identified",
            ],
        ),
        "st29-hmc-list-sriov-adapters": (
            "pcie.list_sriov_adapters",
            _INVENTORY,
            "passed",
            "not-required",
            [
                "adapter-filter-selects-one",
                "adapter-rows-parsed",
                "capability-available",
            ],
        ),
        "st29-hmc-list-sriov-physical-ports": (
            "pcie.list_sriov_physical_ports",
            _INVENTORY,
            "passed",
            "not-required",
            [
                "adapter-required-refused",
                "capability-available",
                "granularity-positive",
                "ports-listed",
            ],
        ),
        "st29-hmc-list-sriov-logical-ports": (
            "pcie.list_sriov_logical_ports",
            _INVENTORY,
            "passed",
            "not-required",
            [
                "capability-available",
                "configured-capacity-bounded",
                "parents-are-listed-ports",
                "ports-belong-to-adapter",
            ],
        ),
        "st29-hmc-list-vnics": (
            "vnic.list",
            _INVENTORY,
            "passed",
            "not-required",
            ["vnic-rows-parsed"],
        ),
    }
    vnic_calls = [k for t, k in state.calls if t == "hmc_list_vnics"]
    assert vnic_calls == [
        {"system_name_or_uuid": "sys-one", "lpar_name_or_uuid": "lpar-v"}
    ]
    assert state.commands() == [
        "lshwres -r virtualio --rsubtype vnic --level lpar -m sys-one -F lpar_name"
    ]


@pytest.mark.asyncio
async def test_inventory_reads_issue_no_mutating_tool() -> None:
    state = await _read_inventory(_inventory_world())

    assert set(state.tools()) == {
        "hmc_list_io_slots",
        "hmc_list_sriov_adapters",
        "hmc_list_sriov_physical_ports",
        "hmc_list_sriov_logical_ports",
        "hmc_run_command",
        "hmc_list_vnics",
    }
    assert all(cmd.startswith("lshwres ") for cmd in state.commands())


def _replace(tool: str, fault: Any) -> Any:
    def build() -> dict[str, Any]:
        world = _inventory_world()
        world[tool] = fault(world[tool])
        return world

    return build


def _items_edit(edit: Any) -> Any:
    """Wrap a response callable so its `items` pass through *edit*."""

    def wrap(original: Any) -> Any:
        def respond(kwargs: dict[str, Any], n: int) -> Any:
            value = original(kwargs, n)
            if isinstance(value, dict):
                return {**value, "items": edit(kwargs, value["items"])}
            return value

        return respond

    return wrap


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("world", "listing", "observation", "unmet"),
    [
        pytest.param(
            _inventory_world,
            _dedicated_listing(owner="null"),
            "st29-hmc-list-dedicated-pcie-slots",
            "owners-normalized",
            id="owner-null",
        ),
        pytest.param(
            _inventory_world,
            {
                "capability": "available",
                "items": [{"drc_index": _DRC}, {"drc_index": _DRC}],
            },
            "st29-hmc-list-dedicated-pcie-slots",
            "slot-rows-identified",
            id="duplicate-drc",
        ),
        pytest.param(
            _replace(
                "hmc_list_io_slots",
                lambda orig: lambda k, n: orig(k, n)[:1],
            ),
            None,
            "st29-hmc-list-io-slots",
            "matches-dedicated-inventory",
            id="io-slot-set-differs",
        ),
        pytest.param(
            _replace(
                "hmc_list_io_slots",
                lambda orig: (
                    lambda k, n: (
                        orig({}, n) if k.get("pci_class") == "eth" else orig(k, n)
                    )
                ),
            ),
            None,
            "st29-hmc-list-io-slots",
            "class-filter-exact",
            id="eth-filter-unfiltered",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_adapters",
                _items_edit(lambda _k, items: [*items, {"adapter_id": "1"}]),
            ),
            None,
            "st29-hmc-list-sriov-adapters",
            "adapter-filter-selects-one",
            id="filter-returns-two",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_adapters",
                _items_edit(
                    lambda _k, items: [
                        *items,
                        {"adapter_id": "3", "mode": "dedicated"},
                    ]
                ),
            ),
            None,
            "st29-hmc-list-sriov-adapters",
            "adapter-rows-parsed",
            id="dedicated-adapter-with-id",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_adapters",
                _items_edit(
                    lambda _k, items: [*items, {"adapter_id": None, "mode": "sriov"}]
                ),
            ),
            None,
            "st29-hmc-list-sriov-adapters",
            "adapter-rows-parsed",
            id="sriov-adapter-without-id",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_physical_ports",
                lambda orig: lambda k, n: orig({**k, "adapter_id": "1"}, n),
            ),
            None,
            "st29-hmc-list-sriov-physical-ports",
            "adapter-required-refused",
            id="bare-port-read-accepted",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_physical_ports",
                _items_edit(
                    lambda _k, items: [
                        {**i, "minimum_capacity_granularity_percent": "0"}
                        for i in items
                    ]
                ),
            ),
            None,
            "st29-hmc-list-sriov-physical-ports",
            "granularity-positive",
            id="granularity-zero",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_physical_ports",
                _items_edit(
                    lambda _k, items: [
                        {**i, "minimum_capacity_granularity_percent": None}
                        for i in items
                    ]
                ),
            ),
            None,
            "st29-hmc-list-sriov-physical-ports",
            "granularity-positive",
            id="granularity-never-carried",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_logical_ports",
                _items_edit(lambda _k, items: [{**items[0], "physical_port_id": "7"}]),
            ),
            None,
            "st29-hmc-list-sriov-logical-ports",
            "parents-are-listed-ports",
            id="unlisted-parent",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_logical_ports",
                _items_edit(lambda _k, items: [{**items[0], "adapter_id": "2"}]),
            ),
            None,
            "st29-hmc-list-sriov-logical-ports",
            "ports-belong-to-adapter",
            id="foreign-adapter",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_logical_ports",
                _items_edit(
                    lambda _k, items: [{**items[0], "capacity_percent": "100.5"}]
                ),
            ),
            None,
            "st29-hmc-list-sriov-logical-ports",
            "configured-capacity-bounded",
            id="capacity-over-maximum",
        ),
        pytest.param(
            _replace(
                "hmc_list_sriov_logical_ports",
                _items_edit(
                    lambda _k, items: [{**items[1], "capacity_percent": "null"}]
                ),
            ),
            None,
            "st29-hmc-list-sriov-logical-ports",
            "configured-capacity-bounded",
            id="unconfigured-with-capacity",
        ),
        pytest.param(
            _replace(
                "hmc_list_vnics",
                lambda _orig: (
                    lambda k, _n: [
                        {"lpar_name": k["lpar_name_or_uuid"], "slot_num": "null"}
                    ]
                ),
            ),
            None,
            "st29-hmc-list-vnics",
            "vnic-rows-parsed",
            id="vnic-slot-unparsed",
        ),
    ],
)
async def test_each_inventory_fault_fails_its_assertion(
    world: Any, listing: Any, observation: str, unmet: str
) -> None:
    state = await _read_inventory(world(), listing)

    emitted = _emitted(state)[observation]
    assert emitted[2] == "failed"
    assert unmet not in emitted[4]


@pytest.mark.asyncio
async def test_failed_inventory_reads_are_failed_observations_not_skips() -> None:
    """A read that fails, or answers in an unexpected shape, never raises or SKIPs."""
    world = {
        "hmc_list_io_slots": lambda _k, _n: _CONNECTION_LOST,
        "hmc_list_sriov_adapters": lambda _k, _n: {"capability": "available"},
        "hmc_run_command": lambda _k, _n: "lpar-v\n",
        "hmc_list_vnics": lambda _k, _n: _CONNECTION_LOST,
    }
    state = await _read_inventory(world)

    emitted = _emitted(state)
    for observation in (
        "st29-hmc-list-io-slots",
        "st29-hmc-list-sriov-adapters",
        "st29-hmc-list-vnics",
    ):
        assert emitted[observation][2:] == ("failed", "not-required", [])
    assert "hmc_list_sriov_physical_ports" not in state.tools()


@pytest.mark.asyncio
async def test_a_failed_vnic_discovery_is_not_a_vnic_list_observation() -> None:
    """The harness's own discovery read failing says nothing about `vnic.list`."""
    world = _inventory_world()
    world["hmc_run_command"] = lambda _k, _n: _CONNECTION_LOST
    state = await _read_inventory(world)

    assert "st29-hmc-list-vnics" not in _emitted(state)
    assert "hmc_list_vnics" not in state.tools()
    row = state.row("vNIC discovery")
    assert row is not None and row[2] == "FAIL"


@pytest.mark.asyncio
async def test_failed_dedicated_listing_is_observed_before_the_arm_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    responses = _happy_responses(holder)
    responses["hmc_list_dedicated_pcie_slots"] = lambda _k, _n: _CONNECTION_LOST
    state = await _run_arm(monkeypatch, responses, holder, inventory=True)

    assert _emitted(state) == {
        "st29-hmc-list-dedicated-pcie-slots": (
            "pcie.list_dedicated_slots",
            _INVENTORY,
            "failed",
            "not-required",
            [],
        )
    }
    row = state.row("dedicated slot inventory")
    assert row is not None and row[2] == "SKIP"


@pytest.mark.asyncio
async def test_empty_listings_record_no_observation() -> None:
    """An empty listing proves no row shape: an `(empty)` row, never an observation."""
    world = _inventory_world()
    world["hmc_list_sriov_adapters"] = lambda _k, _n: {
        "capability": "available",
        "items": [],
    }
    world["hmc_run_command"] = lambda _k, _n: "No results were found.\n"
    state = await _read_inventory(world)

    emitted = _emitted(state)
    assert "st29-hmc-list-sriov-adapters" not in emitted
    assert "st29-hmc-list-vnics" not in emitted
    assert state.row("hmc_list_sriov_adapters (empty)") is not None
    assert state.row("hmc_list_vnics (empty)") is not None
    assert "hmc_list_vnics" not in state.tools()
    row = state.row("SR-IOV port inventory")
    assert row is not None and row[2] == "SKIP"


@pytest.mark.asyncio
async def test_no_sriov_mode_adapter_skips_the_port_reads() -> None:
    world = _inventory_world()
    world["hmc_list_sriov_adapters"] = lambda _k, _n: {
        "capability": "available",
        "items": [{"adapter_id": None, "mode": "dedicated"}],
    }
    world["hmc_list_io_slots"] = lambda k, _n: (
        []
        if k.get("pci_class") == "eth"
        else [{"drc_index": _DRC}, {"drc_index": _SLOT_ETH}]
    )
    state = await _read_inventory(world)

    emitted = _emitted(state)
    assert emitted["st29-hmc-list-sriov-adapters"] == (
        "pcie.list_sriov_adapters",
        _INVENTORY,
        "passed",
        "not-required",
        ["adapter-rows-parsed", "capability-available"],
    )
    assert not {
        "hmc_list_sriov_physical_ports",
        "hmc_list_sriov_logical_ports",
    } & set(state.tools())
    row = state.row("SR-IOV port inventory")
    assert (
        row is not None and row[2] == "SKIP" and "no adapter in SR-IOV mode" in row[3]
    )
    # No Ethernet-class slot: the filter must then select nothing, and it does.
    assert emitted["st29-hmc-list-io-slots"][2] == "passed"
    assert "class-filter-exact" in emitted["st29-hmc-list-io-slots"][4]


@pytest.mark.asyncio
async def test_eth_filter_selecting_rows_with_no_eth_slot_fails() -> None:
    world = _inventory_world()
    world["hmc_list_io_slots"] = lambda _k, _n: [
        {"drc_index": _DRC},
        {"drc_index": _SLOT_ETH},
    ]
    state = await _read_inventory(world)

    emitted = _emitted(state)["st29-hmc-list-io-slots"]
    assert emitted[2] == "failed" and "class-filter-exact" not in emitted[4]


@pytest.mark.asyncio
async def test_a_transport_failure_is_not_the_selector_refusal() -> None:
    """Only the inventory's own "adapter_id is required" refusal holds the negative."""
    world = _inventory_world()
    ports = world["hmc_list_sriov_physical_ports"]
    world["hmc_list_sriov_physical_ports"] = lambda k, n: (
        _CONNECTION_LOST if k.get("adapter_id") is None else ports(k, n)
    )
    state = await _read_inventory(world)

    emitted = _emitted(state)["st29-hmc-list-sriov-physical-ports"]
    assert emitted[2] == "failed" and "adapter-required-refused" not in emitted[4]


@pytest.mark.asyncio
async def test_unhashable_ids_fail_their_assertions_without_raising() -> None:
    world = _inventory_world()
    world["hmc_list_io_slots"] = lambda _k, _n: [
        {"drc_index": {"x": 1}, "pci_class": "0200"}
    ]
    logical = world["hmc_list_sriov_logical_ports"]
    world["hmc_list_sriov_logical_ports"] = lambda k, n: {
        **logical(k, n),
        "items": [{"adapter_id": ["1"], "physical_port_id": {"p": 0}}],
    }
    listing = {"capability": "available", "items": [{"drc_index": {"x": 1}}]}
    state = await _read_inventory(world, listing)

    emitted = _emitted(state)
    for observation in (
        "st29-hmc-list-dedicated-pcie-slots",
        "st29-hmc-list-io-slots",
        "st29-hmc-list-sriov-logical-ports",
    ):
        assert emitted[observation][2] == "failed"


@pytest.mark.asyncio
async def test_failed_adapter_or_port_reads_skip_what_depends_on_them() -> None:
    world = _inventory_world()
    world["hmc_list_sriov_adapters"] = lambda _k, _n: _CONNECTION_LOST
    state = await _read_inventory(world)
    row = state.row("SR-IOV port inventory")
    assert row is not None and "could not be read" in row[3]
    assert _emitted(state)["st29-hmc-list-sriov-adapters"][2] == "failed"

    world = _inventory_world()
    world["hmc_list_sriov_physical_ports"] = lambda _k, _n: _CONNECTION_LOST
    state = await _read_inventory(world)
    row = state.row("SR-IOV port inventory")
    assert row is not None and "logical-port read" in row[3]
    assert "hmc_list_sriov_logical_ports" not in state.tools()
    assert "st29-hmc-list-sriov-logical-ports" not in _emitted(state)


@pytest.mark.asyncio
async def test_inventory_phase_runs_before_selection_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reads precede any creation, and failing every one leaves the arm intact."""
    holder: dict[str, str] = {}
    with_reads = await _run_arm(
        monkeypatch, _happy_responses(holder), holder, inventory=True
    )
    holder2: dict[str, str] = {}
    without = await _run_arm(monkeypatch, _happy_responses(holder2), holder2)

    tools = with_reads.tools()
    assert tools.index("hmc_list_sriov_adapters") < tools.index("hmc_create_lpar")
    vnic_read = dedicated_pcie._vnic_command("sys-one")
    reads = {"hmc_list_io_slots", "hmc_list_sriov_adapters", "hmc_list_vnics"}
    remaining = [
        tool
        for tool, kwargs in with_reads.calls
        if tool not in reads and kwargs.get("cmd") != vnic_read
    ]
    assert remaining == without.tools()
    assert with_reads.artifacts.pcie_drc_index == without.artifacts.pcie_drc_index
    marker = re.compile(r"pcie-[0-9a-f]{8}")
    assert [marker.sub("M", c) for c in with_reads.cleanup_commands()] == [
        marker.sub("M", c) for c in without.cleanup_commands()
    ]


def test_record_held_refuses_an_unknown_cleanup_value() -> None:
    recorded: list[Any] = []
    state: Any = SimpleNamespace(record=lambda *args: recorded.append(args))

    with pytest.raises(ValueError, match="cleanup disposition is not a known value"):
        dedicated_pcie._record_held(
            state,
            34,
            "hmc_delete_lpar (judged)",
            assertions=[Assertion("delete-call-succeeded", True)],
            cleanup="fialed",
            data="",
        )

    assert recorded == []


@pytest.mark.parametrize("cleanup", sorted(CLEANUP))
def test_record_held_judges_cleanup_as_record_verified_does(cleanup: str) -> None:
    recorded: list[Any] = []
    state: Any = SimpleNamespace(record=lambda *args: recorded.append(args))

    dedicated_pcie._record_held(
        state,
        34,
        "hmc_delete_lpar (judged)",
        assertions=[Assertion("delete-call-succeeded", True)],
        cleanup=cleanup,
        data="",
    )

    expected = "PASS" if cleanup in {"passed", "not-required"} else "FAIL"
    assert [args[-3] for args in recorded] == [expected]


@pytest.mark.asyncio
async def test_fixture_text_result_preserves_identity_and_ownership(monkeypatch):
    holder: dict[str, str] = {}
    uuid = "11111111-1111-1111-1111-111111111111"
    responses = _happy_responses(holder, uuid_value=uuid)
    create = responses["hmc_create_lpar"]

    def text_result(kwargs, index):
        result = create(kwargs, index)
        return result if _is_probe(kwargs) else repr(SimpleNamespace(**result))

    responses["hmc_create_lpar"] = text_result
    state = await _run_arm(monkeypatch, responses, holder)

    stamp = state.row("fixture ownership stamp")
    baseline = state.row("fixture profile io_slots (baseline)")
    assert stamp is not None and stamp[2] == "PASS"
    assert "parsed from response text" in stamp[3]
    assert baseline is not None and baseline[2] == "PASS"
    assert uuid in baseline[3]
