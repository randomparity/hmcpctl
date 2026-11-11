"""The installer boot in hmc_provision_lpar (ADR 0191 Decisions 1-2, ADR 0326)."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from test_provision_install import (
    DISK,
    LPAR,
    OPTICAL,
    REQUEST,
    SYSTEM,
    VIOS,
    FakeHMC,
    Writes,
    _scsi_mapping,
)

from hmcpctl.operations.logical import engine
from hmcpctl.operations.logical.engine import OperationRequest
from hmcpctl.operations.logical.models import EffectRecord
from hmcpctl.operations.logical.store import OperationRefused
from hmcpctl.operations.lpar.provisioning import boot as provision_boot
from hmcpctl.operations.lpar.provisioning import operation as provision_operation
from hmcpctl.operations.lpar.provisioning.boot import newer_rows, wait_for_boot
from hmcpctl.operations.lpar.provisioning.media import UNVERIFIED, media_name_for
from hmcpctl.operations.lpar.provisioning.operation import (
    EFFECT_KINDS,
    provision_body,
    provision_classifiers,
    result_from_record,
)
from hmcpctl.server_tools.catalog import TOOL_SECURITY
from hmcpctl.server_tools.lpar.provision import provision_handler
from hmcpctl.ssh.transport import HMCCLIError

IMMEDIATE = replace(REQUEST, boot="immediate")
OLD = {"lpar_name": "lpar-a", "time_stamp": "11/03/2026 10:00:00", "refcode": "OLD1"}
OTHER_VIOS = "00000000-0000-0000-0000-0000000000f6"


def _row(time_stamp: str, refcode: str) -> dict[str, Any]:
    return {"lpar_name": "lpar-a", "time_stamp": time_stamp, "refcode": refcode}


class BootHMC(FakeHMC):
    """A healthy partition, powered off with the media and root disk mapped."""

    def __init__(self) -> None:
        super().__init__()
        self.state = "not activated"
        self.extra_mappings: dict[str, list[dict[str, Any]]] = {}
        self.mac_hex = "020000000001"

    async def get_logical_partition(self, _lpar: str) -> dict[str, Any]:
        return {"Resource": {"PartitionState": self.state}}

    async def list_vios(self, _system: str) -> list[dict[str, Any]]:
        own = [*await self.list_storage_mappings(VIOS), *self.optical]
        other = self.extra_mappings.get(OTHER_VIOS, [])
        fibre = self.extra_mappings.get("fibre", [])
        return [
            {
                "UUID": VIOS.upper(),
                "Resource": {"VirtualSCSIMappings": {"VirtualSCSIMapping": own}},
            },
            {
                "UUID": OTHER_VIOS,
                "Resource": {
                    "VirtualSCSIMappings": {"VirtualSCSIMapping": other},
                    "VirtualFibreChannelMappings": {
                        "VirtualFibreChannelMapping": fibre
                    },
                },
            },
        ]

    async def list_adapters(self, _lpar: str, _kind: str) -> list[dict[str, Any]]:
        resource = {"PortVLANID": "100", "MACAddress": self.mac_hex}
        return [{"UUID": "adapter-1", "Resource": resource}]


class Boot(Writes):
    """Writes plus the boot's reads: refcodes, the boot order and the power-on."""

    def __init__(
        self, hmc: BootHMC, monkeypatch: pytest.MonkeyPatch, creation_mock
    ) -> None:
        super().__init__(hmc, monkeypatch, creation_mock)
        self.hmc: BootHMC = hmc
        self.refcodes: list[list[dict[str, Any]]] = [[OLD]]
        self.refcode_error: Exception | None = None
        self.counts: list[int] = []
        self.power_error: Exception | None = None
        po = provision_operation
        patch = monkeypatch.setattr
        patch(po, "list_lpar_refcodes", self.read_refcodes)
        patch(po, "resolve_ssh_names", self.names)
        patch(po, "_power_on", self.power)
        patch(po, "read_lpar_boot_order", self.read_boot)

    async def names(self, _hmc: Any, system: str, lpar: str) -> tuple[str, str]:
        assert (system, lpar) == (SYSTEM, LPAR)
        return "sys-R1", "lpar-a"

    async def read_refcodes(self, _config, system, lpar, count) -> list[dict]:
        assert (system, lpar) == ("sys-R1", "lpar-a")
        self.calls.append(f"refcodes:{count}")
        self.counts.append(count)
        if self.refcode_error is not None:
            raise self.refcode_error
        rows = self.refcodes[0] if len(self.refcodes) == 1 else self.refcodes.pop(0)
        return rows[:count]

    async def power(self, _hmc, _system, lpar, _assessment, **kw) -> dict:
        self.calls.append("power_on")
        assert lpar == LPAR and kw["partition_profile_uuid"] == "profile-1"
        if self.power_error is not None:
            raise self.power_error
        self.hmc.state = "starting"
        return {"job_id": "job-1"}

    async def read_boot(self, _hmc, _lpar, **_kw) -> dict[str, Any]:
        return {"pending_boot_string": " ".join(self.boot[-1]) if self.boot else ""}


_IDS = itertools.count()


def _submit(boot: Boot, request=IMMEDIATE, continuation="none", rid=None):
    rid = rid or f"boot-{next(_IDS)}"
    operation = OperationRequest(
        "hmc_provision_lpar", "agent", "<default>", "hmc.test", rid, {"r": rid}
    )
    hmc = boot.hmc
    record = engine.submit(
        operation,
        provision_body(SYSTEM, request, lambda _c, _host: hmc),
        classifiers=provision_classifiers(request, lambda _c, _host: hmc),
        continuation=continuation,
        wait_seconds=30,
    )
    hmc.media_name = media_name_for(record.operation_id)
    return record


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch, creation_mock) -> Boot:
    return Boot(BootHMC(), monkeypatch, creation_mock)


def _step(record, name: str):
    return next(s for s in result_from_record(record).steps if s.step == name)


def test_an_immediate_boot_powers_on_once_and_reports_the_codes_it_saw(boot):
    boot.refcodes = [[OLD], [_row("11/03/2026 10:00:05", "CA00E1F1"), OLD]]

    record = _submit(boot)

    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert boot.calls[-4:] == ["boot_order", "refcodes:1", "power_on", "refcodes:20"]
    effects = [(e.key, e.kind, e.status) for e in record.effects][-3:]
    assert effects == [
        ("boot_order", "lpar.boot_order", "applied"),
        ("refcode_baseline", "lpar.refcode_baseline", "applied"),
        ("power_on", "lpar.power_on", "applied"),
    ]
    baseline = next(e for e in record.effects if e.key == "refcode_baseline")
    assert baseline.identity == {"time_stamp": OLD["time_stamp"], "refcode": "OLD1"}
    wait = _step(record, "wait_boot_started")
    assert wait.status == "error"
    assert wait.result["observed"] == [
        {"time_stamp": "11/03/2026 10:00:05", "refcode": "CA00E1F1"}
    ]
    assert "before wait_seconds" in wait.result["reason"]
    assert _step(record, "revalidate_boot").status == "ok"
    assert result_from_record(record).unverified == UNVERIFIED
    assert record.next_actions == ("resume", "abandon")


def test_a_resume_after_the_wait_polls_again_and_never_powers_on_twice(boot):
    rid = f"boot-{next(_IDS)}"
    first = _submit(boot, rid=rid)
    assert first.outcome == "needs_attention"
    boot.hmc.state = "running"

    resumed = _submit(boot, continuation="resume", rid=rid)

    assert resumed.outcome == "needs_attention"
    assert boot.calls.count("power_on") == 1
    assert boot.calls.count("refcodes:1") == 1
    assert boot.calls.count("refcodes:20") == 2
    steps = [s.step for s in result_from_record(resumed).steps]
    assert "revalidate_boot" not in steps
    assert _step(resumed, "wait_boot_started").status == "error"


def test_a_code_in_the_boot_started_set_ends_boot_started(boot, monkeypatch):
    monkeypatch.setattr(provision_boot, "BOOT_STARTED_CODES", frozenset({"CA00E1F1"}))
    boot.refcodes = [[OLD], [_row("11/03/2026 10:00:05", "CA00E1F1"), OLD]]

    record = _submit(boot)

    assert (record.state, record.outcome) == ("terminal", "boot_started")
    result = result_from_record(record)
    assert result.workflow_completed is True
    assert _step(record, "wait_boot_started").result == {
        "time_stamp": "11/03/2026 10:00:05",
        "refcode": "CA00E1F1",
    }


def test_the_native_set_is_the_firmware_handoff_code_alone():
    """ADR 0328: both native windows ended at CA00E105 and posted nothing after it."""
    assert provision_boot.BOOT_STARTED_CODES == frozenset({"CA00E105"})


def test_a_resume_that_sees_the_handoff_code_ends_boot_started(boot):
    """Natively the handoff came ~130 s after power-on, past a short first wait."""
    rid = f"boot-{next(_IDS)}"
    early = _row("11/03/2026 10:00:05", "CA00E14D")
    handoff = _row("11/03/2026 10:00:09", "CA00E105")
    boot.refcodes = [[OLD], [early, OLD], [handoff, early, OLD]]
    first = _submit(boot, rid=rid)
    assert first.outcome == "needs_attention"
    boot.hmc.state = "running"

    resumed = _submit(boot, continuation="resume", rid=rid)

    assert (resumed.state, resumed.outcome) == ("terminal", "boot_started")
    assert boot.calls.count("power_on") == 1
    assert _step(resumed, "wait_boot_started").result == {
        "time_stamp": "11/03/2026 10:00:09",
        "refcode": "CA00E105",
    }


def test_a_handoff_code_older_than_the_baseline_never_counts(boot):
    """A previous boot's CA00E105 sits behind the baseline row and is not newer."""
    stale = _row("11/03/2026 09:00:00", "CA00E105")
    boot.refcodes = [[OLD, stale], [OLD, stale]]

    record = _submit(boot)

    assert record.outcome == "needs_attention"
    assert _step(record, "wait_boot_started").result["observed"] == []


def test_the_baseline_code_itself_never_counts_as_boot_started(boot, monkeypatch):
    monkeypatch.setattr(provision_boot, "BOOT_STARTED_CODES", frozenset({"OLD1"}))

    record = _submit(boot)

    assert record.outcome == "needs_attention"
    assert _step(record, "wait_boot_started").result["observed"] == []


def test_deferred_pauses_ready_to_boot_and_continuation_boot_powers_on(boot):
    rid = f"boot-{next(_IDS)}"
    first = _submit(boot, request=REQUEST, rid=rid)
    assert first.outcome == "ready_to_boot"
    assert "power_on" not in boot.calls

    booted = _submit(boot, request=REQUEST, continuation="boot", rid=rid)

    assert booted.outcome == "needs_attention"
    assert boot.calls.count("power_on") == 1
    assert _step(booted, "revalidate_boot").status == "ok"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda b: setattr(b.hmc, "state", "running"), "not powered off"),
        (lambda b: b.hmc.media.clear(), "no longer in the media repository"),
        (lambda b: b.hmc.mount(), "not mapped once"),
        (lambda b: b.hmc.optical.clear(), "not mapped once"),
        (lambda b: b.boot.append(["/other"]), "pending boot order differs"),
        (lambda b: setattr(b.hmc, "mac_hex", "020000000009"), "MAC the media binds"),
        (
            lambda b: b.hmc.extra_mappings.setdefault(OTHER_VIOS, []).append(
                _scsi_mapping(6, {"VirtualDisk": {"DiskName": "lv-b"}})
            ),
            "only disk",
        ),
        (
            lambda b: b.hmc.extra_mappings.setdefault("fibre", []).append(
                {
                    "AssociatedLogicalPartition": {
                        "href": f"https://hmc.test/rest/api/uom/LogicalPartition/{LPAR}"
                    }
                }
            ),
            "only disk",
        ),
    ],
)
def test_continuation_boot_revalidates_and_never_powers_on_a_changed_partition(
    boot, change, problem
):
    rid = f"boot-{next(_IDS)}"
    assert _submit(boot, request=REQUEST, rid=rid).outcome == "ready_to_boot"
    change(boot)

    record = _submit(boot, request=REQUEST, continuation="boot", rid=rid)

    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert "power_on" not in boot.calls
    assert not any(c.startswith("refcodes") for c in boot.calls)
    step = _step(record, "revalidate_boot")
    assert step.status == "error" and problem in step.result
    skipped = [
        s.step for s in result_from_record(record).steps if s.status == "skipped"
    ]
    assert skipped[-3:] == ["refcode_baseline", "power_on", "wait_boot_started"]


def test_an_optical_mapping_on_another_vios_is_not_a_disk(boot):
    boot.hmc.extra_mappings[OTHER_VIOS] = [
        _scsi_mapping(8, {"VirtualOpticalMedia": {"MediaName": "x.iso"}})
    ]

    assert _step(_submit(boot), "revalidate_boot").status == "ok"


def test_an_unread_baseline_powers_nothing_on_and_resumes_by_reading_again(boot):
    rid = f"boot-{next(_IDS)}"
    boot.refcode_error = HMCCLIError("lsrefcode failed")
    first = _submit(boot, rid=rid)

    assert (first.state, first.outcome) == ("paused", "needs_attention")
    assert "power_on" not in boot.calls
    baseline = next(e for e in first.effects if e.key == "refcode_baseline")
    assert baseline.status == "not_applied"

    boot.refcode_error = None
    resumed = _submit(boot, continuation="resume", rid=rid)
    assert boot.calls.count("power_on") == 1
    assert resumed.outcome == "needs_attention"


def test_a_failed_poll_read_ends_the_wait_naming_the_error(boot, monkeypatch):
    boot.refcodes = [[OLD]]
    original = boot.read_refcodes

    async def read(config, system, lpar, count):
        if count == 1:
            return await original(config, system, lpar, count)
        raise HMCCLIError("lsrefcode timed out")

    monkeypatch.setattr(provision_operation, "list_lpar_refcodes", read)
    record = _submit(boot)

    wait = _step(record, "wait_boot_started")
    assert wait.status == "error" and "lsrefcode timed out" in wait.result["reason"]
    assert boot.calls.count("power_on") == 1


def test_an_uncertain_power_on_pauses_and_is_classified_not_resubmitted(boot):
    rid = f"boot-{next(_IDS)}"
    boot.power_error = RuntimeError("connection reset")
    first = _submit(boot, rid=rid)
    power = next(e for e in first.effects if e.key == "power_on")
    assert (first.outcome, power.status) == ("needs_attention", "uncertain")

    _submit(boot, continuation="resume", rid=rid)

    assert boot.calls.count("power_on") == 1


def test_the_baseline_classifier_always_reads_again():
    effect = EffectRecord(
        "refcode_baseline",
        EFFECT_KINDS["refcode_baseline"],
        f"lpar:{LPAR}",
        "uncertain",
        None,
    )
    classifier = provision_classifiers(IMMEDIATE, lambda _c, _host: BootHMC())[
        effect.kind
    ]

    verdict = asyncio.run(classifier(None, effect))  # type: ignore[arg-type]

    assert verdict.status == "not_applied"


def test_a_partition_with_no_codes_counts_every_row_as_newer():
    rows = [_row("11/03/2026 10:00:05", "A"), _row("11/03/2026 10:00:01", "B")]

    newer = newer_rows(rows, {"time_stamp": None, "refcode": None})

    assert [r["refcode"] for r in newer] == ["A", "B"]


def test_rows_order_by_hmc_row_order_and_never_before_the_baseline_time():
    base = {"time_stamp": "11/03/2026 10:00:00", "refcode": "OLD1"}
    rows = [
        _row("11/03/2026 10:00:00", "SAME_SECOND"),
        _row("11/03/2026 09:59:00", "EARLIER"),
        _row("garbled", "UNPARSED"),
        OLD,
        _row("11/03/2026 10:30:00", "BELOW_BASELINE"),
    ]

    newer = newer_rows(rows, base)

    assert [r["refcode"] for r in newer] == ["SAME_SECOND", "UNPARSED"]


def test_a_baseline_pushed_out_of_the_read_keeps_the_timestamp_bound():
    base = {"time_stamp": "11/03/2026 10:00:00", "refcode": "OLD1"}
    rows = [_row("11/03/2026 10:00:09", "NEW"), _row("11/03/2026 09:00:00", "STALE")]

    assert [r["refcode"] for r in newer_rows(rows, base)] == ["NEW"]


def test_the_wait_reads_until_wait_seconds_then_stops():
    now = [0.0]
    reads: list[float] = []

    async def read():
        reads.append(now[0])
        return [OLD]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    wait = asyncio.run(wait_for_boot(read, OLD, 40, sleep=sleep, clock=lambda: now[0]))

    assert reads == [0.0, 15.0, 30.0]
    assert wait.started is None and wait.observed == []


def test_a_zero_second_wait_still_reads_once():
    reads = []

    async def read():
        reads.append(1)
        return []

    asyncio.run(wait_for_boot(read, OLD, 0))

    assert reads == [1]


def test_install_immediate_writes_the_boot_order_before_any_power_on(boot):
    _submit(boot)

    assert boot.boot == [[OPTICAL, DISK]]
    assert boot.calls.index("boot_order") < boot.calls.index("power_on")


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "u")
    monkeypatch.setenv("HMC_PASSWORD", "p")
    return provision_handler(TOOL_SECURITY, lambda _name: True, lambda *_args: None)


def test_continuation_boot_needs_the_writer_window(handler):
    with pytest.raises(ValueError, match="a boot asserts it again"):
        handler(request_id="boot-window", continuation="boot")


def test_continuation_boot_of_an_unknown_request_is_not_found(handler):
    with pytest.raises(OperationRefused, match="no operation has request_id"):
        handler(
            request_id="boot-unknown", continuation="boot", exclusive_writer_window=True
        )


def test_the_wait_ends_when_the_calls_own_wait_does(boot, monkeypatch):
    windows: list[float] = []
    ticks = iter([100.0, 120.0])

    async def wait(_read, _baseline, window, **_kw):
        windows.append(window)
        return provision_boot.BootWait(None, [], "no code")

    monkeypatch.setattr(provision_operation, "wait_for_boot", wait)
    monkeypatch.setattr(
        provision_operation, "time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    hmc = boot.hmc
    operation = OperationRequest(
        "hmc_provision_lpar", "agent", "<default>", "hmc.test", "boot-window", {}
    )
    engine.submit(
        operation,
        provision_body(SYSTEM, IMMEDIATE, lambda _c, _host: hmc, wait_seconds=30),
        classifiers=provision_classifiers(IMMEDIATE, lambda _c, _host: hmc),
        wait_seconds=30,
    )

    assert windows == [10.0]
