"""Safe dedicated PCIe assignment contract tests (ADR 0055, 0165, 0166)."""

import asyncio
import shlex
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.operations.virtualization.pcie import (
    PcieAssignmentPartialError,
    PcieAssignmentUnavailableError,
    assign_dedicated_pcie_slot,
    unassign_dedicated_pcie_slot,
)
from hmcpctl.server_tools.lpar.profiles import tool_security
from hmcpctl.ssh.profiles import (
    ProfileIoSlot,
    assign_profile_io_slot,
    parse_profile_io_slots,
    read_profile_io_slot_rows,
    unassign_profile_io_slot,
)
from hmcpctl.ssh.transport import HMCCLIError

_DRC = "21010020"
_ADMITTED_VERSION = "version= Version: 10\n Release: 3\n Service Pack: 1060\n"
_READ = "lssyscfg -r prof -m sys -F lpar_name,name,io_slots --header"


def _config() -> HMCConfig:
    return HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"})


class _FakeHmc:
    """An HMC CLI double holding profile `io_slots` in the admitted read rendering.

    `chsyscfg` applies `io_slots+=<drc>//0` as `<drc>/none/0` and `io_slots-=<drc>//0`
    by removing that entry, which is the behaviour ADR 0166 assumes and verifies. What
    `io_slots-=<drc>//0` does to the DRC stored in another form (`<drc>/none/1`) is
    unknown, so `other_form_removal` models each possibility: remove it, leave it
    (no-op), or fail the command.
    """

    def __init__(
        self,
        io_slots: str = "none",
        *,
        model: str = "8375-42A",
        version: str = _ADMITTED_VERSION,
        applies: bool = True,
        chsyscfg_error: bool = False,
        rows: list[tuple[str, str, str]] | None = None,
        after_write: Callable[[str], str] | None = None,
        fail_reads_after_write: bool = False,
        state: str = "Not Activated",
        other_form_removal: str = "remove",
        before_write: Callable[[], None] | None = None,
    ) -> None:
        self.rows = rows if rows is not None else [("lpar", "prof", io_slots)]
        self.model = model
        self.version = version
        self.applies = applies
        self.chsyscfg_error = chsyscfg_error
        self.after_write = after_write
        self.fail_reads_after_write = fail_reads_after_write
        self.state = state
        self.other_form_removal = other_form_removal
        self.before_write = before_write
        self.written = False
        self.commands: list[str] = []

    async def run(self, _config: HMCConfig, command: str) -> str:
        self.commands.append(command)
        if command == "lshmc -V":
            return self.version
        if command.startswith("lssyscfg -r sys"):
            return self.model
        if command.startswith("lssyscfg -r lpar"):
            return f"name,lpar_id,state,rmc_state\nlpar,3,{self.state},inactive\n"
        if command.startswith("lssyscfg -r prof"):
            if self.written and self.fail_reads_after_write:
                raise HMCCLIError("connection lost")
            lines = ["lpar_name,name,io_slots"]
            lines += [f'{lpar},{prof},"{value}"' for lpar, prof, value in self.rows]
            return "\n".join(lines) + "\n"
        if command.startswith("chsyscfg -r prof"):
            self.written = True
            if self.before_write is not None:
                self.before_write()
            if self.applies:
                self._apply(command)
            if self.chsyscfg_error:
                raise HMCCLIError("response lost")
            return ""
        raise AssertionError(f"unexpected command {command!r}")

    def _apply(self, command: str) -> None:
        record = shlex.split(command)[-1]
        fields = dict(part.split("=", 1) for part in record.split(","))
        lpar, prof = fields["lpar_name"], fields["name"]
        for index, (row_lpar, row_prof, value) in enumerate(self.rows):
            if (row_lpar, row_prof) != (lpar, prof):
                continue
            entries = [] if value == "none" else value.split(",")
            if "io_slots+" in fields:
                entries.append(fields["io_slots+"].replace("//", "/none/"))
            else:
                entries = self._remove(
                    entries, fields["io_slots-"].replace("//", "/none/")
                )
            new = ",".join(entries) or "none"
            if self.after_write is not None:
                new = self.after_write(new)
            self.rows[index] = (row_lpar, row_prof, new)

    def _remove(self, entries: list[str], written: str) -> list[str]:
        if written in entries:
            return [entry for entry in entries if entry != written]
        if self.other_form_removal == "error":
            raise HMCCLIError("An invalid I/O slot was specified")
        if self.other_form_removal == "remove":
            drc_index = written.split("/")[0]
            return [entry for entry in entries if entry.split("/")[0] != drc_index]
        return entries

    def mutations(self) -> list[str]:
        return [command for command in self.commands if command.startswith("chsyscfg")]

    def profile_reads(self) -> list[str]:
        return [
            command
            for command in self.commands
            if command.startswith("lssyscfg -r prof")
        ]

    def value(self) -> str:
        return self.rows[0][2]


@pytest.fixture
def hmc(monkeypatch) -> AsyncMock:
    client = AsyncMock()
    client.config = _config()
    authorize = AsyncMock(return_value=("sys", "lpar"))
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.pcie.resolve_and_authorize_lpar_names",
        authorize,
    )
    client.authorize = authorize
    return client


def _install(monkeypatch, fake: _FakeHmc) -> _FakeHmc:
    monkeypatch.setattr("hmcpctl.ssh.profiles.run_hmc_command", fake.run)
    monkeypatch.setattr("hmcpctl.ssh.sriov.run_hmc_command", fake.run)
    return fake


def _assign(hmc, drc: str = _DRC, profile: str = "prof") -> None:
    asyncio.run(
        assign_dedicated_pcie_slot(hmc, "lpar", profile, drc, system_name_or_uuid="sys")
    )


def _unassign(hmc, drc: str = _DRC, profile: str = "prof") -> None:
    asyncio.run(
        unassign_dedicated_pcie_slot(
            hmc, "lpar", profile, drc, system_name_or_uuid="sys"
        )
    )


@pytest.mark.parametrize(
    ("operation", "token"),
    [(assign_profile_io_slot, "io_slots+="), (unassign_profile_io_slot, "io_slots-=")],
)
def test_profile_commands_are_symmetric_and_never_force(monkeypatch, operation, token):
    command = AsyncMock(return_value="ok")
    monkeypatch.setattr("hmcpctl.ssh.profiles.run_hmc_command", command)

    assert asyncio.run(operation(_config(), "sys", "lpar", "profile", _DRC)) == "ok"
    built = command.await_args.args[1]
    assert token in built
    assert f"{_DRC}//0" in built
    assert "--force" not in built


def test_profile_io_slot_read_is_the_admitted_command(monkeypatch):
    fake = _install(monkeypatch, _FakeHmc(f"{_DRC}/none/0"))

    rows = asyncio.run(read_profile_io_slot_rows(_config(), "sys"))

    assert fake.commands == [_READ]
    assert rows == [{"lpar_name": "lpar", "name": "prof", "io_slots": f"{_DRC}/none/0"}]


def test_profile_io_slot_read_refuses_a_headerless_answer(monkeypatch):
    monkeypatch.setattr(
        "hmcpctl.ssh.profiles.run_hmc_command",
        AsyncMock(return_value="lpar,prof,none\n"),
    )
    with pytest.raises(
        HMCCLIError,
        match="profile io_slots readback response did not match the expected "
        "lpar_name,name,io_slots fields",
    ):
        asyncio.run(read_profile_io_slot_rows(_config(), "sys"))


def test_profile_io_slot_read_treats_hmc_empty_result_as_no_profiles(monkeypatch):
    monkeypatch.setattr(
        "hmcpctl.ssh.profiles.run_hmc_command",
        AsyncMock(return_value="No results were found.\n"),
    )
    assert asyncio.run(read_profile_io_slot_rows(_config(), "sys")) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("none", ()),
        (f"{_DRC}/none/0", (ProfileIoSlot(_DRC, None, False),)),
        (
            "21020013/none/1,21040015/none/1,21010020/none/0",
            (
                ProfileIoSlot("21020013", None, True),
                ProfileIoSlot("21040015", None, True),
                ProfileIoSlot("21010020", None, False),
            ),
        ),
        ("2105001B/3/1", (ProfileIoSlot("2105001B", "3", True),)),
    ],
)
def test_parse_profile_io_slots_accepts_the_admitted_rendering(value, expected):
    assert parse_profile_io_slots(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        f"{_DRC}//0",
        f"{_DRC}/none",
        f"{_DRC}/none/2",
        f"{_DRC}/none/0/1",
        f"{_DRC}/none/0,{_DRC}/none/1",
        "2105001b/none/0",
        f" {_DRC}/none/0",
        "2101002/none/0",
        f"{_DRC}/none/0,",
    ],
)
def test_parse_profile_io_slots_refuses_an_unadmitted_rendering(value):
    with pytest.raises(HMCCLIError, match="unadmitted"):
        parse_profile_io_slots(value)


@pytest.mark.parametrize(
    ("profile", "drc"),
    [
        (" ", _DRC),
        ("prof", " "),
        ("prof", "2101002a"),
        ("prof", "2101002"),
        ("prof", "210100200"),
        ("prof", "553713664"),
        ("prof", "2101/020"),
        ("prof,x", _DRC),
        ('prof"', _DRC),
    ],
)
@pytest.mark.parametrize("operation", [_assign, _unassign])
def test_unsafe_selectors_are_refused_before_any_hmc_call(
    monkeypatch, hmc, operation, profile, drc
):
    fake = _install(monkeypatch, _FakeHmc())

    with pytest.raises(ValueError):
        operation(hmc, drc=drc, profile=profile)

    hmc.authorize.assert_not_awaited()
    assert fake.commands == []


@pytest.mark.parametrize("operation", [_assign, _unassign])
def test_foreign_owner_is_refused_before_any_ssh_command(monkeypatch, hmc, operation):
    fake = _install(monkeypatch, _FakeHmc())
    hmc.authorize.side_effect = PermissionError("owned by another caller")

    with pytest.raises(PermissionError):
        operation(hmc)

    assert fake.commands == []


@pytest.mark.parametrize(
    ("model", "version"),
    [
        ("9009-42A", _ADMITTED_VERSION),
        ("8375-42A", "version= Version: 10\n Release: 2\n Service Pack: 1060\n"),
        # A later service pack that still lists an M1060 fix line is not M1060.
        (
            "8375-42A",
            "version= Version: 10\n Release: 3\n Service Pack: 1061\nMF71689 - HMC V10R3 M1060\n",
        ),
        ("8375-42A", "HMC V10R3 M1060\n"),
        ("8375-42A", _ADMITTED_VERSION + " Service Pack: 1060\n"),
    ],
)
@pytest.mark.parametrize("operation", [_assign, _unassign])
def test_outside_the_envelope_is_capability_unavailable_before_any_profile_command(
    monkeypatch, hmc, operation, model, version
):
    fake = _install(
        monkeypatch, _FakeHmc(f"{_DRC}/none/0", model=model, version=version)
    )

    with pytest.raises(PcieAssignmentUnavailableError, match="V10R3 M1060.*8375-42A"):
        operation(hmc)

    assert fake.profile_reads() == []
    assert fake.mutations() == []


@pytest.mark.parametrize("state", ["Running", "Open Firmware", "Not Available"])
@pytest.mark.parametrize("operation", [_assign, _unassign])
def test_an_lpar_that_is_not_activated_is_required_before_any_profile_command(
    monkeypatch, hmc, operation, state
):
    fake = _install(monkeypatch, _FakeHmc(f"{_DRC}/none/0", state=state))

    with pytest.raises(ValueError, match="requires a Not Activated LPAR"):
        operation(hmc)

    assert fake.profile_reads() == []
    assert fake.mutations() == []


def test_assign_refuses_a_slot_another_lpars_profile_lists(monkeypatch, hmc):
    fake = _install(
        monkeypatch,
        _FakeHmc(
            rows=[
                ("lpar", "prof", "none"),
                ("other", "p2", f"21020013/none/1,{_DRC}/none/0"),
            ]
        ),
    )

    with pytest.raises(ValueError, match="already listed by a profile of LPAR other"):
        _assign(hmc)

    assert fake.mutations() == []


def test_assign_refuses_an_already_doubled_listing(monkeypatch, hmc):
    fake = _install(
        monkeypatch,
        _FakeHmc(
            rows=[("lpar", "prof", f"{_DRC}/none/0"), ("other", "p2", f"{_DRC}/none/0")]
        ),
    )

    with pytest.raises(ValueError, match="already listed by a profile of LPAR other"):
        _assign(hmc)

    assert fake.mutations() == []


def test_a_concurrent_assign_to_another_lpar_is_a_partial_error(monkeypatch, hmc):
    def racing_writer(value: str) -> str:
        fake.rows.append(("other", "p2", f"{_DRC}/none/0"))
        return value

    fake = _install(monkeypatch, _FakeHmc(after_write=racing_writer))

    with pytest.raises(
        PcieAssignmentPartialError, match="also listed by a profile of LPAR other"
    ):
        _assign(hmc)

    assert len(fake.mutations()) == 1


def test_assign_ignores_the_same_lpars_other_profiles_and_unrelated_rows(
    monkeypatch, hmc
):
    rows = [
        ("lpar", "prof", "none"),
        ("lpar", "backup", f"{_DRC}/none/0"),
        ("other", "p2", "not-an-admitted-rendering"),
    ]
    fake = _install(monkeypatch, _FakeHmc(rows=rows))

    _assign(hmc)

    assert len(fake.mutations()) == 1
    assert fake.value() == f"{_DRC}/none/0"


def test_assign_writes_the_documented_grammar_and_verifies(monkeypatch, hmc):
    fake = _install(monkeypatch, _FakeHmc())

    _assign(hmc)

    hmc.authorize.assert_awaited_once_with(
        hmc, "lpar", system_name_or_uuid="sys", ownership_override=False
    )
    assert fake.mutations() == [
        "chsyscfg -r prof -m sys -i name=prof,io_slots+=21010020//0,lpar_name=lpar"
    ]
    assert fake.value() == f"{_DRC}/none/0"
    assert len(fake.profile_reads()) == 2


def test_unassign_removes_only_the_written_form_and_verifies(monkeypatch, hmc):
    fake = _install(monkeypatch, _FakeHmc(f"21020013/none/1,{_DRC}/none/0"))

    asyncio.run(
        unassign_dedicated_pcie_slot(
            hmc,
            "lpar",
            "prof",
            _DRC,
            ownership_override=True,
            system_name_or_uuid="sys",
        )
    )

    hmc.authorize.assert_awaited_once_with(
        hmc, "lpar", system_name_or_uuid="sys", ownership_override=True
    )
    assert fake.mutations() == [
        "chsyscfg -r prof -m sys -i name=prof,io_slots-=21010020//0,lpar_name=lpar"
    ]
    assert fake.value() == "21020013/none/1"


def test_unassigning_the_last_slot_reads_back_none(monkeypatch, hmc):
    fake = _install(monkeypatch, _FakeHmc(f"{_DRC}/none/0"))

    _unassign(hmc)

    assert fake.value() == "none"


def test_a_reordered_readback_still_verifies(monkeypatch, hmc):
    def reverse(value: str) -> str:
        return ",".join(reversed(value.split(",")))

    fake = _install(monkeypatch, _FakeHmc("21020013/none/1", after_write=reverse))

    _assign(hmc)

    assert fake.value() == f"{_DRC}/none/0,21020013/none/1"


@pytest.mark.parametrize(
    ("operation", "io_slots"),
    [(_assign, f"{_DRC}/none/0"), (_unassign, "21020013/none/1")],
)
def test_idempotent_retry_issues_no_mutation(monkeypatch, hmc, operation, io_slots):
    fake = _install(monkeypatch, _FakeHmc(io_slots))

    operation(hmc)

    assert fake.mutations() == []
    assert fake.value() == io_slots


@pytest.mark.parametrize("io_slots", [f"{_DRC}/none/1", f"{_DRC}/3/0"])
@pytest.mark.parametrize("operation", [_assign, _unassign])
def test_a_slot_in_another_form_is_refused_before_mutation(
    monkeypatch, hmc, operation, io_slots
):
    fake = _install(monkeypatch, _FakeHmc(io_slots))

    with pytest.raises(ValueError, match="only .*/none/0"):
        operation(hmc)

    assert fake.mutations() == []


_REMOVAL_BRANCHES = ["remove", "noop", "error"]


@pytest.mark.parametrize("branch", _REMOVAL_BRANCHES)
@pytest.mark.parametrize("operation", [_assign, _unassign])
def test_a_required_slot_is_refused_before_any_write_whatever_removal_would_do(
    monkeypatch, hmc, operation, branch
):
    """ADR 0166: `io_slots-=<drc>//0` against `<drc>/none/1` is uncharacterized.

    The captured VIOS slots are stored with `is_required=1`. Whether removing
    `<drc>//0` would delete such an element, leave it, or fail is unknown, so the
    operation never sends it: each branch the HMC might take stays unreached.
    """
    fake = _install(
        monkeypatch,
        _FakeHmc(f"21020013/none/1,{_DRC}/none/1", other_form_removal=branch),
    )

    with pytest.raises(ValueError, match="only .*/none/0"):
        operation(hmc)

    assert fake.mutations() == []
    assert fake.value() == f"21020013/none/1,{_DRC}/none/1"


@pytest.mark.parametrize(
    ("branch", "outcome"),
    [
        ("remove", None),
        ("noop", PcieAssignmentPartialError),
        ("error", PcieAssignmentPartialError),
    ],
)
def test_a_slot_turned_required_between_read_and_write(
    monkeypatch, hmc, branch, outcome
):
    """The concurrent-writer case (failure model class 3), pinned per branch.

    Only a removal that leaves the DRC absent reads back as the requested state;
    a no-op or a refused command reads back as neither state and fails closed.
    """

    def turn_required() -> None:
        fake.rows[0] = ("lpar", "prof", f"{_DRC}/none/1")

    fake = _install(
        monkeypatch,
        _FakeHmc(
            f"{_DRC}/none/0", other_form_removal=branch, before_write=turn_required
        ),
    )

    if outcome is None:
        _unassign(hmc)
        assert fake.value() == "none"
    else:
        with pytest.raises(outcome, match="could not be verified"):
            _unassign(hmc)


def _partial_error_message(operation, hmc) -> str:
    """Run *operation* to its partial error and check the advice issues no change command.

    Rounds 1 and 2 of #882's review each found a concurrent state in which a named
    reversal command was wrong, so the advice names none (ADR 0166).
    """
    with pytest.raises(PcieAssignmentPartialError) as caught:
        operation(hmc)
    message = str(caught.value)
    for command in ("io_slots+=", "io_slots-=", "chsyscfg"):
        assert command not in message
    assert "ProfileIoSlot(" not in message
    return message


def test_a_partial_error_says_what_the_profile_may_hold_and_how_to_inspect_it(
    monkeypatch, hmc
):
    _install(monkeypatch, _FakeHmc(applies=False))

    message = _partial_error_message(_assign, hmc)

    assert (
        "may hold the change, none of it, or a form this operation refuses" in message
    )
    assert "`lssyscfg -r prof -m sys -F lpar_name,name,io_slots --header`" in message
    assert "Never write the read value back as `io_slots=` input" in message


def _extra_slot(value: str) -> str:
    return ",".join(entry for entry in (value, "21040015/none/0") if entry != "none")


@pytest.mark.parametrize(
    ("operation", "io_slots", "rendering"),
    [
        pytest.param(_assign, "none", "absent", id="assign-no-op"),
        pytest.param(
            _unassign, f"{_DRC}/none/0", f"{_DRC}/none/0", id="unassign-no-op"
        ),
    ],
)
def test_a_readback_in_the_before_state_needs_no_reversal(
    monkeypatch, hmc, operation, io_slots, rendering
):
    _install(monkeypatch, _FakeHmc(io_slots, applies=False))

    message = _partial_error_message(operation, hmc)

    assert (
        f"The readback lists slot {_DRC} of profile 'prof' of LPAR 'lpar' as before "
        f"({rendering}), so no reversal is needed." in message
    )
    assert "HMC UI" not in message


@pytest.mark.parametrize(
    ("operation", "io_slots", "rendering"),
    [
        pytest.param(_assign, "none", f"{_DRC}/none/0", id="assign-landed"),
        pytest.param(_unassign, f"{_DRC}/none/0", "absent", id="unassign-landed"),
    ],
)
def test_a_slot_read_as_requested_puts_the_difference_elsewhere(
    monkeypatch, hmc, operation, io_slots, rendering
):
    _install(monkeypatch, _FakeHmc(io_slots, after_write=_extra_slot))

    message = _partial_error_message(operation, hmc)

    assert (
        f"The readback lists slot {_DRC} of profile 'prof' of LPAR 'lpar' as requested "
        f"({rendering}), so the difference is elsewhere in the profile" in message
    )
    assert f"do not undo slot {_DRC}" in message
    assert "through the HMC UI" in message
    assert "also lists the slot" not in message


def test_a_slot_read_in_another_form_advises_comparing_and_the_ui(monkeypatch, hmc):
    def turn_required() -> None:
        fake.rows[0] = ("lpar", "prof", f"{_DRC}/none/1")

    fake = _install(
        monkeypatch,
        _FakeHmc(
            f"{_DRC}/none/0", other_form_removal="noop", before_write=turn_required
        ),
    )

    message = _partial_error_message(_unassign, hmc)

    assert "Compare the read value with the before value" in message
    assert (
        f"make any reversal of slot {_DRC} of profile 'prof' of LPAR 'lpar'" in message
    )
    assert "through the HMC UI" in message
    assert "no reversal is needed" not in message
    assert "as requested" not in message


@pytest.mark.parametrize(
    ("operation", "io_slots", "fake_options"),
    [
        pytest.param(
            _assign,
            "none",
            {"after_write": lambda _value: f"{_DRC}//0"},
            id="assign-unparsed",
        ),
        pytest.param(
            _unassign,
            f"{_DRC}/none/0",
            {"after_write": lambda _value: ""},
            id="unassign-empty",
        ),
        pytest.param(
            _assign, "none", {"fail_reads_after_write": True}, id="assign-unread"
        ),
        pytest.param(
            _unassign,
            f"{_DRC}/none/0",
            {"fail_reads_after_write": True},
            id="unassign-unread",
        ),
    ],
)
def test_a_readback_that_does_not_parse_advises_the_ui_only(
    monkeypatch, hmc, operation, io_slots, fake_options
):
    _install(monkeypatch, _FakeHmc(io_slots, **fake_options))

    message = _partial_error_message(operation, hmc)

    assert "could not be read or parsed" in message
    assert "inspect it with the read command above" in message
    assert "through the HMC UI" in message
    assert "no reversal is needed" not in message


def test_the_read_command_quotes_the_system_name(monkeypatch, hmc):
    hmc.authorize.return_value = ("my sys", "lpar")
    _install(monkeypatch, _FakeHmc(applies=False))

    message = _partial_error_message(_assign, hmc)

    assert (
        "`lssyscfg -r prof -m 'my sys' -F lpar_name,name,io_slots --header`" in message
    )


_HOLDER_ADVICE = (
    "The profile of LPAR other also lists the slot; do not edit that profile without its "
    "owner"
)


def test_a_lost_response_with_a_new_holder_names_both_causes(monkeypatch, hmc):
    def racing_writer(value: str) -> str:
        fake.rows.append(("other", "p2", f"{_DRC}/none/0"))
        return value

    fake = _install(
        monkeypatch, _FakeHmc(chsyscfg_error=True, after_write=racing_writer)
    )

    message = _partial_error_message(_assign, hmc)

    assert "response lost; slot is also listed by a profile of LPAR other" in message
    assert "through the HMC UI" in message
    assert _HOLDER_ADVICE in message
    assert "resolve that conflict there" not in message


def test_a_holder_beside_an_unchanged_slot_asks_for_no_reversal(monkeypatch, hmc):
    def racing_writer() -> None:
        fake.rows.append(("other", "p2", f"{_DRC}/none/0"))

    fake = _install(monkeypatch, _FakeHmc(applies=False, before_write=racing_writer))

    message = _partial_error_message(_assign, hmc)

    assert "as before (absent), so no reversal is needed" in message
    assert _HOLDER_ADVICE in message
    assert "reverse" not in message.lower().replace("no reversal is needed", "")


def test_an_unassign_whose_slot_another_lpar_took_names_no_add_back(monkeypatch, hmc):
    """Round 2 of #882: adding the slot back here would list it in two profiles."""

    def racing_writer(value: str) -> str:
        fake.rows.append(("other", "p2", f"{_DRC}/none/0"))
        return _extra_slot(value)

    fake = _install(monkeypatch, _FakeHmc(f"{_DRC}/none/0", after_write=racing_writer))

    message = _partial_error_message(_unassign, hmc)

    assert "as requested (absent), so the difference is elsewhere" in message
    assert f"do not undo slot {_DRC}" in message
    assert _HOLDER_ADVICE in message
    assert "slot is also listed by" not in message


def test_an_assign_beside_a_new_holder_keeps_the_reversal_open(monkeypatch, hmc):
    def racing_writer(value: str) -> str:
        fake.rows.append(("other", "p2", f"{_DRC}/none/0"))
        return _extra_slot(value)

    fake = _install(monkeypatch, _FakeHmc(after_write=racing_writer))

    message = _partial_error_message(_assign, hmc)

    assert "Compare the read value with the before value" in message
    assert _HOLDER_ADVICE in message
    assert "do not undo" not in message


def test_an_assign_beside_an_unreadable_holder_keeps_the_reversal_open(
    monkeypatch, hmc
):
    def racing_writer(value: str) -> str:
        fake.rows.append(("third", "p3", f"{_DRC}//0"))
        return value

    fake = _install(monkeypatch, _FakeHmc(after_write=racing_writer))

    message = _partial_error_message(_assign, hmc)

    assert "could not be verified: unadmitted io_slots rendering" in message
    assert "Compare the read value with the before value" in message
    assert "Whether another LPAR's profile lists the slot could not be read" in message
    assert "do not undo" not in message


def test_a_refused_assign_beside_an_unreadable_holder_is_a_partial_error(
    monkeypatch, hmc
):
    def racing_writer() -> None:
        fake.rows.append(("third", "p3", f"{_DRC}//0"))

    fake = _install(
        monkeypatch,
        _FakeHmc(applies=False, chsyscfg_error=True, before_write=racing_writer),
    )

    message = _partial_error_message(_assign, hmc)

    assert "could not be verified: response lost" in message


def test_an_unassign_that_landed_succeeds_whatever_other_profiles_list(
    monkeypatch, hmc
):
    fake = _install(
        monkeypatch,
        _FakeHmc(
            rows=[
                ("lpar", "prof", f"{_DRC}/none/0"),
                ("other", "p2", f"{_DRC}/none/0"),
                ("third", "p3", f"{_DRC}//0"),
            ]
        ),
    )

    _unassign(hmc)

    assert fake.value() == "none"


def test_an_unreadable_holder_is_reported_after_an_unassign(monkeypatch, hmc):
    def racing_writer(value: str) -> str:
        fake.rows.append(("third", "p3", f"{_DRC}//0"))
        return _extra_slot(value)

    fake = _install(monkeypatch, _FakeHmc(f"{_DRC}/none/0", after_write=racing_writer))

    message = _partial_error_message(_unassign, hmc)

    assert "as requested (absent)" in message
    assert "Whether another LPAR's profile lists the slot could not be read" in message
    assert "unadmitted io_slots rendering" in message


def test_a_write_the_readback_does_not_show_is_a_partial_error(monkeypatch, hmc):
    fake = _install(monkeypatch, _FakeHmc(applies=False))

    with pytest.raises(PcieAssignmentPartialError, match="readback mismatch") as caught:
        _assign(hmc)

    assert "before='none'" in str(caught.value)
    assert "after='none'" in str(caught.value)
    assert len(fake.mutations()) == 1


def test_a_re_rendered_foreign_slot_is_a_partial_error(monkeypatch, hmc):
    fake = _install(
        monkeypatch,
        _FakeHmc(
            "21020013/none/1",
            after_write=lambda value: value.replace(
                "21020013/none/1", "21020013/none/0"
            ),
        ),
    )

    with pytest.raises(PcieAssignmentPartialError, match="readback mismatch"):
        _assign(hmc)

    assert len(fake.mutations()) == 1


def test_a_failed_readback_after_the_write_is_a_partial_error(monkeypatch, hmc):
    _install(monkeypatch, _FakeHmc(fail_reads_after_write=True))

    with pytest.raises(PcieAssignmentPartialError, match="connection lost") as caught:
        _assign(hmc)

    assert isinstance(caught.value.__cause__, HMCCLIError)


def test_a_refused_write_that_changed_nothing_re_raises_the_command_error(
    monkeypatch, hmc
):
    fake = _install(monkeypatch, _FakeHmc(applies=False, chsyscfg_error=True))

    with pytest.raises(HMCCLIError, match="response lost"):
        _assign(hmc)

    assert len(fake.profile_reads()) == 2


def test_a_lost_response_whose_change_landed_is_verified_by_readback(monkeypatch, hmc):
    fake = _install(monkeypatch, _FakeHmc(chsyscfg_error=True))

    _assign(hmc)

    assert fake.value() == f"{_DRC}/none/0"
    assert len(fake.profile_reads()) == 2


def test_a_missing_profile_row_is_refused_before_mutation(monkeypatch, hmc):
    fake = _install(monkeypatch, _FakeHmc(rows=[("other", "prof", "none")]))

    with pytest.raises(ValueError, match="not found"):
        _assign(hmc)

    assert fake.mutations() == []


def test_duplicate_profile_rows_are_refused_before_mutation(monkeypatch, hmc):
    fake = _install(
        monkeypatch, _FakeHmc(rows=[("lpar", "prof", "none"), ("lpar", "prof", "none")])
    )

    with pytest.raises(HMCCLIError, match="more than one"):
        _assign(hmc)

    assert fake.mutations() == []


def test_mcp_contract_replaces_the_unsafe_profile_tool():
    security = tool_security()
    assert "hmc_assign_profile_io_slot" not in security
    assert security["hmc_assign_dedicated_pcie_slot"].operation == (
        "pcie.assign_dedicated_slot"
    )
    assert security["hmc_unassign_dedicated_pcie_slot"].operation == (
        "pcie.unassign_dedicated_slot"
    )
    assert security["hmc_assign_dedicated_pcie_slot"].effect == "mutate"
    assert security["hmc_unassign_dedicated_pcie_slot"].target_kind == "lpar"
