"""hmc_reconfigure_lpar's planner, body and classifiers on the ADR 0195 engine (#1226).

ADR 0314 governs: every change is classified before any write, the default profile is
written first, then the current configuration, then the rename, and the result is read
back before the operation completes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from lpar.test_partition_profiles import U
from lpar.test_partition_profiles import profile as profile_document

from hmcpctl.config import HMCConfig
from hmcpctl.documents.partition_profile import ProfilePatch, apply_profile_patch, one
from hmcpctl.errors import HMCError, LparUpdateError
from hmcpctl.operations.logical import engine, profile_write
from hmcpctl.operations.logical.engine import OperationFailed, OperationRequest
from hmcpctl.operations.lpar import reconfigure
from hmcpctl.operations.lpar.reconfigure import ReconfigurePatch, plan_changes
from hmcpctl.xmlutil import element_to_dict

SYS = "0000000a-0000-4000-8000-000000000000"
LPAR = "0000000a-0000-4000-b000-000000000000"
PROFILE = "0000000a-0000-4000-c000-000000000000"
OTHER = "0000000b-0000-4000-b000-000000000000"
ARGS = {"lpar_name_or_uuid": "web1", "system_name_or_uuid": "sysA"}


def _view(state="running", rmc="active", sync="disabled", **values):
    view = {
        "name": "web1",
        "state": state,
        "rmc": rmc,
        "sync": sync,
        "default_profile": "default",
        "last_profile": "default",
        "dedicated": False,
        "memory": [512, 768, 1024],
        "processors": [1, 2, 4],
        "units": [0.05, 0.1, 1.0],
    }
    return view | values


def _saved(**values):
    saved = {
        "dedicated": False,
        "memory": [512, 768, 1024],
        "processors": [1, 2, 4],
        "units": [0.05, 0.1, 1.0],
    }
    return saved | values


def _plan(patch, view=None, saved=None, allow_disruption=False):
    return plan_changes(
        ReconfigurePatch(**patch), view or _view(), saved or _saved(), allow_disruption
    )


def _applied(plan):
    return {change["field"]: change["applied"] for change in plan["changes"]}


# ---------------------------------------------------------------------------
# Planner (ADR 0314 Decision 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("view", "patch", "allow", "applied", "current"),
    [
        (
            _view(state="not activated"),
            {"memory_mib": 1024},
            False,
            "profile",
            {"desired_memory": 1024},
        ),
        (_view(), {"memory_mib": 1024}, False, "live", {"desired_memory": 1024}),
        (_view(), {"processors": 3}, False, "live", {"desired_vcpus": 3}),
        (
            _view(rmc="inactive"),
            {"memory_mib": 1024},
            False,
            "pending_activation",
            None,
        ),
        (
            _view(state="open firmware", rmc="inactive"),
            {"memory_mib": 1024},
            False,
            "pending_activation",
            None,
        ),
        (
            _view(state="starting"),
            {"memory_mib": 1024},
            False,
            "pending_activation",
            None,
        ),
        (
            _view(memory=[512, 768, 900]),
            {"memory_mib": 1000},
            False,
            "pending_activation",
            None,
        ),
        (_view(), {"memory_mib": 600}, False, "pending_activation", None),
        (_view(), {"memory_mib": 600}, True, "live", {"desired_memory": 600}),
        (_view(), {"memory_mib": 2048}, False, "pending_activation", None),
    ],
)
def test_classification(view, patch, allow, applied, current):
    plan = _plan(patch, view, allow_disruption=allow)
    field = next(iter(patch))
    assert _applied(plan) == {field: applied}
    assert plan["current"] == current
    assert plan["profile_patch"]


def test_a_value_beyond_the_profile_maximum_widens_it_and_is_pending():
    plan = _plan({"memory_mib": 2048})
    assert plan["profile_patch"] == {"desired_memory": 2048, "max_memory": 2048}
    assert plan["profile_before"] == {"desired_memory": 768, "max_memory": 1024}
    assert plan["changes"][0]["reason"] == "outside the profile's minimum or maximum"


def test_a_widened_bound_on_an_inactive_partition_writes_both_and_is_pending():
    plan = _plan({"memory_mib": 2048}, _view(state="not activated"))
    assert _applied(plan) == {"memory_mib": "pending_activation"}
    assert plan["current"] == {"desired_memory": 2048, "max_memory": 2048}


def test_drifted_profile_is_written_even_when_the_current_value_matches():
    plan = _plan({"memory_mib": 1024}, _view(memory=[512, 1024, 1024]))
    assert plan["profile_patch"] == {"desired_memory": 1024}
    assert plan["current"] is None
    assert plan["changes"][0]["before"] == {"profile": 768, "current": 1024}


def test_equal_values_change_nothing():
    plan = _plan({"memory_mib": 768, "processors": 2})
    assert (plan["changes"], plan["profile_patch"], plan["current"]) == ([], {}, None)


def test_omitted_fields_are_never_written():
    plan = _plan({"processors": 3})
    assert plan["profile_patch"] == {"desired_processors": 3}
    assert [change["field"] for change in plan["changes"]] == ["processors"]


def test_shared_virtual_processors_below_units_are_refused():
    with pytest.raises(OperationFailed, match="processing units"):
        _plan({"processors": 1}, saved=_saved(units=[0.05, 1.5, 2.0]))


def test_an_empty_state_is_refused():
    with pytest.raises(OperationFailed, match="state is unavailable"):
        _plan({"memory_mib": 1024}, _view(state=""))


def test_sync_on_refuses_a_mixed_pending_and_live_plan():
    with pytest.raises(OperationFailed, match="hmc_sync_lpar_profile"):
        _plan({"memory_mib": 600, "processors": 3}, _view(sync="on"))


def test_sync_on_refuses_pending_plus_rename():
    with pytest.raises(OperationFailed, match="split the patch"):
        _plan({"memory_mib": 600, "name": "web2"}, _view(sync="on"))


def test_sync_on_allows_pending_alone():
    assert _applied(_plan({"memory_mib": 600}, _view(sync="on"))) == {
        "memory_mib": "pending_activation"
    }


def test_rename_is_live_and_needs_no_profile():
    plan = plan_changes(ReconfigurePatch(name="web2"), _view(), None, False)
    assert _applied(plan) == {"name": "live"}
    assert plan["rename"] == {"before": "web1", "after": "web2"}
    assert plan["profile_patch"] == {}


def test_patch_needs_a_field():
    with pytest.raises(ValueError, match="at least one"):
        ReconfigurePatch()


# ---------------------------------------------------------------------------
# The operation body against a fake HMC
# ---------------------------------------------------------------------------


def _partition_element(dedicated: bool) -> ET.Element:
    root = ET.Element(f"{{{U}}}LogicalPartition")
    cpu = ET.SubElement(root, f"{{{U}}}PartitionProcessorConfiguration")
    ET.SubElement(cpu, f"{{{U}}}HasDedicatedProcessors").text = str(dedicated).lower()
    return root


class FakeHMC:
    """One partition and its default profile; writes change both as the HMC would."""

    def __init__(self, state="running", rmc="active", sync="Disabled"):
        self.config = HMCConfig.from_mapping({"host": "hmc.test"})
        self.state, self.rmc, self.sync = state, rmc, sync
        self.name = "web1"
        self.memory = [512, 768, 1024]
        self.vcpus = [1, 2, 4]
        self.profile = profile_document()
        self.etag = 1
        self.calls: list[str] = []
        self.partition_error: Exception | None = None
        self.lag = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def list_logical_partitions(self, system_uuid):
        assert system_uuid == SYS
        return [
            {"UUID": LPAR, "Resource": {"PartitionName": self.name}},
            {"UUID": OTHER, "Resource": {"PartitionName": "taken"}},
        ]

    async def get_logical_partition(self, uuid):
        assert uuid == LPAR
        current = self.state != "not activated"
        p = "Current" if current else ""
        memory = dict(
            zip(
                (
                    ("CurrentMinimumMemory", "CurrentMemory", "CurrentMaximumMemory")
                    if current
                    else ("MinimumMemory", "DesiredMemory", "MaximumMemory")
                ),
                map(str, self.memory),
                strict=True,
            )
        )
        names = (
            (
                "CurrentMinimumVirtualProcessors",
                "AllocatedVirtualProcessors",
                "CurrentMaximumVirtualProcessors",
            )
            if current
            else (
                "MinimumVirtualProcessors",
                "DesiredVirtualProcessors",
                "MaximumVirtualProcessors",
            )
        )
        units = (
            (
                "CurrentMinimumProcessingUnits",
                "CurrentProcessingUnits",
                "CurrentMaximumProcessingUnits",
            )
            if current
            else (
                "MinimumProcessingUnits",
                "DesiredProcessingUnits",
                "MaximumProcessingUnits",
            )
        )
        shared = dict(zip(names, map(str, self.vcpus), strict=True))
        shared |= dict(zip(units, ("0.05", "0.1", "1.0"), strict=True))
        return {
            "UUID": LPAR,
            "Resource": {
                "PartitionName": self.name,
                "PartitionState": self.state,
                "ResourceMonitoringControlState": self.rmc,
                "CurrentProfileSync": self.sync,
                "DefaultProfileName": "default",
                "LastActivatedProfile": "default",
                "PartitionMemoryConfiguration": memory,
                "PartitionProcessorConfiguration": {
                    f"{p}HasDedicatedProcessors": "false",
                    f"{p}SharedProcessorConfiguration": shared,
                },
            },
        }

    async def update_logical_partition(self, uuid, updates, subject):
        self.calls.append(subject)
        if self.partition_error is not None:
            raise self.partition_error
        changes = updates(_partition_element(False))
        levels = {"Minimum": 0, "Desired": 1, "Maximum": 2}
        for path, text in changes.items():
            leaf = path.rsplit("/", 1)[-1]
            if leaf == "PartitionName":
                self.name = text
            elif leaf.endswith("Memory") and not self.lag:
                self.memory[levels[leaf.removesuffix("Memory")]] = int(text)
            elif leaf.endswith("VirtualProcessors") and not self.lag:
                self.vcpus[levels[leaf.removesuffix("VirtualProcessors")]] = int(text)

    # Seams for operations.lpar.profiles, monkeypatched into the reconfigure module.

    async def read_partition_profile(self, hmc, system, lpar, name):
        assert (system, lpar, name) == (SYS, LPAR, "default")
        fields = element_to_dict(self.profile)
        return SimpleNamespace(
            uuid=PROFILE, name="default", etag=f"E{self.etag}", fields=fields
        )

    async def update_partition_profile(
        self, hmc, system, lpar, selector, etag, patch, *, exclusive_writer_window
    ):
        assert exclusive_writer_window is True
        self.calls.append("profile")
        if etag != f"E{self.etag}":
            raise ValueError("expected_etag must match a fresh contained profile ETag")
        self.profile = apply_profile_patch(self.profile, patch)
        self.etag += 1
        return SimpleNamespace(
            execution="completed",
            requested_change="verified",
            after_etag=f"E{self.etag}",
            preservation="verified",
        )


@pytest.fixture
def hmc(monkeypatch):
    fake = FakeHMC()

    async def system_uuid(_hmc, value):
        assert value in {"sysA", SYS}
        return SYS

    async def authorize(_hmc, lpar, *, ownership_override, system_name_or_uuid):
        assert ownership_override is False and system_name_or_uuid == SYS
        fake.calls.append("authorize")
        if fake.owner_error is not None:
            raise fake.owner_error
        assert lpar in {"web1", LPAR}
        return LPAR

    async def within(_hmc, _system, _resources):
        return None

    fake.owner_error = None
    monkeypatch.setattr(reconfigure, "open_client", lambda _connection, _host: fake)
    monkeypatch.setattr(reconfigure, "resolve_system_uuid", system_uuid)
    monkeypatch.setattr(reconfigure, "resolve_and_authorize_lpar_mutation", authorize)
    monkeypatch.setattr(reconfigure, "require_memory_within_selected_system", within)

    async def system_entry(_hmc, system):
        assert system == SYS
        return {"Resource": {"AssociatedSystemProcessorConfiguration": fake.limits}}

    fake.limits = {
        "MaximumAllowedVirtualProcessorsPerPartition": "64",
        "MinimumProcessorUnitsPerVirtualProcessor": "0.05",
    }
    monkeypatch.setattr(reconfigure, "read_system_entry", system_entry)
    monkeypatch.setattr(
        reconfigure, "read_partition_profile", fake.read_partition_profile
    )
    monkeypatch.setattr(
        reconfigure, "update_partition_profile", fake.update_partition_profile
    )
    monkeypatch.setattr(reconfigure, "POLL_SECONDS", 0)
    monkeypatch.setattr(reconfigure, "SETTLE_SECONDS", 0)

    async def native(_hmc, _kind, lpar, profile, system):
        assert (lpar, profile, system) == (LPAR, PROFILE, SYS)
        return SimpleNamespace(element=fake.profile, etag=f"E{fake.etag}")

    monkeypatch.setattr(profile_write, "read_native_profile", native)
    return fake


def _run(patch, *, continuation="none", request_id="r1", allow_disruption=False):
    model = ReconfigurePatch(**patch)
    arguments = {**ARGS, "patch": patch, "allow_disruption": allow_disruption}
    request = OperationRequest(
        "hmc_reconfigure_lpar",
        "agent-a",
        "<default>",
        "hmc.test",
        request_id,
        arguments,
    )
    return engine.submit(
        request,
        reconfigure.reconfigure_body(model, allow_disruption, "sysA", "web1"),
        continuation=continuation,
        wait_seconds=10,
        classifiers=reconfigure.RECONFIGURE_CLASSIFIERS,
    )


def _effects(record):
    return [(e.key, e.kind, e.status) for e in record.effects]


def _saved_memory(fake):
    return int(one(fake.profile, "ProfileMemory/DesiredMemory").text)


def test_live_change_writes_profile_then_current_and_verifies(hmc):
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome, record.phase) == (
        "terminal",
        "completed",
        "applying",
    )
    assert [c for c in hmc.calls if c != "authorize"] == [
        "profile",
        "the current configuration",
    ]
    assert _effects(record) == [
        ("plan", "lpar.reconfigure_plan", "applied"),
        ("profile", "lpar.profile_write", "applied"),
        ("current", "lpar.current_config", "applied"),
    ]
    assert (_saved_memory(hmc), hmc.memory[1]) == (1024, 1024)
    assert record.result["changes"][0]["applied"] == "live"
    assert record.result["activation"] is None
    assert (record.system_uuid, record.partition_uuid) == (SYS, LPAR)


def test_owner_and_target_are_rechecked_before_every_write(hmc):
    _run({"memory_mib": 1024})
    assert hmc.calls == [
        "authorize",
        "authorize",
        "profile",
        "authorize",
        "the current configuration",
    ]


def test_pending_activation_writes_profile_only(hmc):
    hmc.rmc = "inactive"
    record = _run({"memory_mib": 1024})
    assert record.outcome == "completed"
    assert "the current configuration" not in hmc.calls
    assert (_saved_memory(hmc), hmc.memory[1]) == (1024, 768)
    assert record.result["activation"] == "default"
    assert record.result["changes"][0]["reason"] == "RMC is not active"


def test_not_activated_writes_profile_and_current_configuration(hmc):
    hmc.state = "not activated"
    record = _run({"memory_mib": 1024, "processors": 1})
    assert record.outcome == "completed"
    assert (_saved_memory(hmc), hmc.memory[1], hmc.vcpus[1]) == (1024, 1024, 1)
    assert {c["applied"] for c in record.result["changes"]} == {"profile"}


def test_rename_is_written_last_and_verified(hmc):
    record = _run({"memory_mib": 1024, "name": "web2"})
    assert record.outcome == "completed"
    assert [c for c in hmc.calls if c != "authorize"][-1] == "the partition name"
    assert hmc.name == "web2"
    assert record.result["observed"]["name"] == "web2"


def test_a_name_another_partition_has_fails_before_writing(hmc):
    record = _run({"name": "taken"})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "the partition name" not in hmc.calls
    assert record.effects == ()


def test_owner_refusal_fails_before_write(hmc):
    hmc.owner_error = PermissionError("LPAR 'web1' is owned by someone else")
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "profile" not in hmc.calls
    assert "web1" not in record.warnings[0]


def test_stale_profile_fails_without_writing(hmc, monkeypatch):
    real = hmc.read_partition_profile

    async def stale(*args):
        record = await real(*args)
        hmc.etag += 1  # another writer changed the profile after this read
        return record

    monkeypatch.setattr(reconfigure, "read_partition_profile", stale)
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert ("profile", "lpar.profile_write", "not_applied") in _effects(record)
    assert hmc.memory[1] == 768


def test_state_changed_since_plan_fails_before_first_write(hmc, monkeypatch):
    real = reconfigure._make_plan

    async def then_activate(*args):
        plan = await real(*args)
        hmc.rmc = "inactive"
        return plan

    monkeypatch.setattr(reconfigure, "_make_plan", then_activate)
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "changed since the plan" in record.warnings[0]
    assert "profile" not in hmc.calls


def test_partial_apply_pauses_and_resume_keeps_it_paused(hmc):
    hmc.partition_error = LparUpdateError("POST failed", "unknown", 500)
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert ("profile", "lpar.profile_write", "applied") in _effects(record)
    assert ("current", "lpar.current_config", "uncertain") in _effects(record)
    assert record.result["changes"][0]["applied"] == "live"
    assert "HMCError while reconfiguring" in record.result["warnings"][-1]
    record = _run({"memory_mib": 1024}, continuation="resume")
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert "has no live check" in record.warnings[0]


@pytest.mark.parametrize(
    "error",
    [LparUpdateError("412", "rejected", 412), HMCError("read failed", 503)],
    ids=["rejected", "before-the-post"],
)
def test_a_refused_write_after_an_applied_one_pauses_and_resumes(hmc, error):
    hmc.partition_error = error
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert ("profile", "lpar.profile_write", "applied") in _effects(record)
    assert ("current", "lpar.current_config", "not_applied") in _effects(record)
    assert record.result["changes"][0]["applied"] == "live"
    hmc.partition_error = None
    record = _run({"memory_mib": 1024}, continuation="resume")
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert hmc.calls.count("profile") == 1
    assert hmc.memory[1] == 1024


def test_a_refused_first_write_fails(hmc):
    hmc.partition_error = LparUpdateError("412", "rejected", 412)
    record = _run({"name": "web2"})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert _effects(record)[-1] == ("rename", "lpar.rename", "not_applied")


def test_a_read_failure_during_verification_pauses_and_resume_verifies(
    hmc, monkeypatch
):
    real = hmc.get_logical_partition
    reads = []

    async def flaky(uuid):
        reads.append(uuid)
        if len(reads) == 4:  # plan, before profile, before current, then verify
            raise HMCError("GET failed", 503)
        return await real(uuid)

    monkeypatch.setattr(hmc, "get_logical_partition", flaky)
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert "could not be read" in record.result["warnings"][-1]
    record = _run({"memory_mib": 1024}, continuation="resume")
    assert (record.state, record.outcome) == ("terminal", "completed")


def test_basis_is_rechecked_before_every_write(hmc, monkeypatch):
    real = hmc.update_partition_profile

    async def then_rmc_drops(*args, **kwargs):
        outcome = await real(*args, **kwargs)
        hmc.rmc = "inactive"
        return outcome

    monkeypatch.setattr(reconfigure, "update_partition_profile", then_rmc_drops)
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert "changed since the plan" in record.result["warnings"][-1]
    assert "the current configuration" not in hmc.calls


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"MaximumAllowedVirtualProcessorsPerPartition": "2"}, "per-partition"),
        ({"MinimumProcessorUnitsPerVirtualProcessor": "0.1"}, "units-per-virtual"),
    ],
)
def test_system_processor_limits_refuse_before_writing(hmc, limits, message):
    hmc.limits = limits
    record = _run({"processors": 3})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert message in record.warnings[0]
    assert record.effects == ()


def test_profile_rejection_is_not_applied(hmc, monkeypatch):
    async def rejected(*_args, **_kwargs):
        return SimpleNamespace(execution="rejected", requested_change="unverified")

    monkeypatch.setattr(reconfigure, "update_partition_profile", rejected)
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert _effects(record)[-1] == ("profile", "lpar.profile_write", "not_applied")


def test_unknown_profile_outcome_is_uncertain_and_resumes_by_readback(hmc, monkeypatch):
    real = hmc.update_partition_profile

    async def unknown(*args, **kwargs):
        await real(*args, **kwargs)
        return SimpleNamespace(execution="unknown", requested_change="unverified")

    monkeypatch.setattr(reconfigure, "update_partition_profile", unknown)
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    monkeypatch.setattr(reconfigure, "update_partition_profile", real)
    record = _run({"memory_mib": 1024}, continuation="resume")
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert ("profile", "lpar.profile_write", "applied") in _effects(record)
    assert hmc.calls.count("profile") == 1


def test_resume_after_verification_mismatch_reverifies(hmc):
    hmc.lag = True
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("paused", "needs_attention")
    assert record.result["observed"]["current"] == {"memory:1": 768}
    hmc.lag = False
    hmc.memory[1] = 1024  # the DLPAR finished
    record = _run({"memory_mib": 1024}, continuation="resume")
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert hmc.calls.count("the current configuration") == 1


def test_resume_after_profile_applied_continues(hmc, monkeypatch):
    real = reconfigure._resolve
    calls = []

    async def dies_before_the_current_write(*args):
        calls.append(args)
        if len(calls) == 3:  # validating, before profile, before current
            raise RuntimeError("process died")
        return await real(*args)

    monkeypatch.setattr(reconfigure, "_resolve", dies_before_the_current_write)
    record = _run({"memory_mib": 1024})
    assert record.outcome == "needs_attention"
    assert _effects(record)[-1] == ("profile", "lpar.profile_write", "applied")
    record = _run({"memory_mib": 1024}, continuation="resume")
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert hmc.calls.count("profile") == 1
    assert hmc.memory[1] == 1024


@pytest.mark.parametrize(
    "later_keys", [("networks",), ("boot", "media", "disks", "networks", "save")]
)
def test_resume_preserves_plans_recorded_before_later_legs(
    hmc, monkeypatch, later_keys
):
    make_plan, resolve = reconfigure._make_plan, reconfigure._resolve
    calls = 0

    async def older_plan(*args):
        plan = await make_plan(*args)
        for key in later_keys:
            plan.pop(key)
        return plan

    async def pause_after_profile(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("process died before current write")
        return await resolve(*args)

    monkeypatch.setattr(reconfigure, "_make_plan", older_plan)
    monkeypatch.setattr(reconfigure, "_resolve", pause_after_profile)
    first = _run({"memory_mib": 1024})
    assert first.outcome == "needs_attention"
    original = next(e.identity for e in first.effects if e.key == reconfigure.PLAN_KEY)
    assert all(key not in original for key in later_keys)
    resumed = _run({"memory_mib": 1024}, continuation="resume")
    assert (resumed.state, resumed.outcome) == ("terminal", "completed")
    recorded = next(
        e.identity for e in resumed.effects if e.key == reconfigure.PLAN_KEY
    )
    assert recorded == original
    assert hmc.calls.count("profile") == 1
    assert hmc.calls.count("the current configuration") == 1
    assert hmc.memory[1] == 1024


@pytest.mark.parametrize("state", ["running", "not activated"])
def test_resume_after_the_current_write_renames(hmc, monkeypatch, state):
    hmc.state = state
    real = reconfigure._resolve
    calls = []

    async def dies_before_the_rename(*args):
        calls.append(args)
        if len(calls) == 4:  # validating, profile, current, then the rename
            raise RuntimeError("process died")
        return await real(*args)

    monkeypatch.setattr(reconfigure, "_resolve", dies_before_the_rename)
    record = _run({"memory_mib": 1024, "name": "web2"})
    assert record.outcome == "needs_attention"
    assert _effects(record)[-1] == ("current", "lpar.current_config", "applied")
    record = _run({"memory_mib": 1024, "name": "web2"}, continuation="resume")
    assert (record.state, record.outcome) == ("terminal", "completed")
    assert hmc.calls.count("the current configuration") == 1
    assert hmc.name == "web2"


@pytest.mark.parametrize(
    ("refusal", "message"),
    [
        ("last_profile", "last activated a profile other than its default"),
        ("no_default", "has no default profile"),
        ("mode", "processor mode differs"),
        ("memory_bound", "configurable memory"),
    ],
)
def test_planning_refusals_write_nothing(hmc, monkeypatch, refusal, message):
    real = hmc.get_logical_partition

    async def entry(uuid):
        found = await real(uuid)
        resource = found["Resource"]
        if refusal == "last_profile":
            resource["LastActivatedProfile"] = "other"
        elif refusal == "no_default":
            resource["DefaultProfileName"] = ""
        elif refusal == "mode":
            resource["PartitionProcessorConfiguration"] = {
                "CurrentHasDedicatedProcessors": "true",
                "CurrentDedicatedProcessorConfiguration": {
                    "CurrentMinimumProcessors": "1",
                    "CurrentProcessors": "1",
                    "CurrentMaximumProcessors": "2",
                },
            }
        return found

    async def too_large(*_args):
        raise ValueError("desired_memory exceeds ConfigurableSystemMemory")

    monkeypatch.setattr(hmc, "get_logical_partition", entry)
    if refusal == "memory_bound":
        monkeypatch.setattr(
            reconfigure, "require_memory_within_selected_system", too_large
        )
    record = _run({"memory_mib": 1024})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert message in record.warnings[0]
    assert record.effects == ()
    assert [c for c in hmc.calls if c != "authorize"] == []


def test_result_warns_of_preservation_drift_and_sync(hmc, monkeypatch):
    hmc.rmc, hmc.sync = "inactive", "On"
    real = hmc.update_partition_profile

    async def drifted(*args, **kwargs):
        outcome = await real(*args, **kwargs)
        outcome.preservation = "drifted"
        return outcome

    monkeypatch.setattr(reconfigure, "update_partition_profile", drifted)
    record = _run({"memory_mib": 1024})
    assert record.outcome == "completed"
    warnings = record.result["warnings"]
    assert any("preservation drift" in w for w in warnings)
    assert any("profile sync is on" in w for w in warnings)


def test_a_name_too_long_for_the_plan_is_refused():
    with pytest.raises(ValueError, match="at most 256"):
        ReconfigurePatch(name="a" * 257)


def test_a_plan_too_large_to_record_fails_before_any_effect(hmc):
    record = _run({"name": "\U0001f600" * 256})
    assert (record.state, record.outcome) == ("terminal", "failed")
    assert "effect identity bound" in record.warnings[0]
    assert record.effects == ()


def test_open_plan_effect_is_recomputed():
    verdict = _await(reconfigure._classify_plan(None, None))
    assert verdict.status == "not_applied"


def _await(coroutine):
    import asyncio

    return asyncio.run(coroutine)


# ---------------------------------------------------------------------------
# The shared profile-write classifier (ADR 0314 Decision 5)
# ---------------------------------------------------------------------------


def _classify(fake, expected, before):
    ctx = SimpleNamespace(
        partition=(SYS, LPAR), host="hmc.test", connection="<default>"
    )
    effect = SimpleNamespace(target=profile_write.target(LPAR, PROFILE))
    classify = profile_write.profile_write_classifier(
        lambda _ctx, _effect: (expected, before), lambda _connection, _host: fake
    )
    return _await(classify(ctx, effect))


@pytest.mark.parametrize(
    ("saved", "status"),
    [(1024, "applied"), (768, "not_applied"), (900, "needs_attention")],
)
def test_profile_write_classifier(hmc, saved, status):

    hmc.profile = apply_profile_patch(hmc.profile, ProfilePatch(desired_memory=saved))
    verdict = _classify(
        hmc, ProfilePatch(desired_memory=1024), ProfilePatch(desired_memory=768)
    )
    assert verdict.status == status


def test_profile_write_classifier_read_error_raises(hmc, monkeypatch):

    async def broken(*_args):
        raise HMCError("GET failed", 503)

    monkeypatch.setattr(profile_write, "read_native_profile", broken)
    with pytest.raises(HMCError):
        _classify(
            hmc, ProfilePatch(desired_memory=1024), ProfilePatch(desired_memory=768)
        )


def test_profile_write_target_round_trips():
    assert profile_write.parse_target(profile_write.target(LPAR, PROFILE)) == (
        LPAR,
        PROFILE,
    )
    with pytest.raises(ValueError, match="malformed"):
        profile_write.parse_target(f"lpar:{LPAR}")


def test_rename_classifier(hmc):
    hmc.name = "web2"
    ctx = SimpleNamespace(
        host="hmc.test",
        connection="<default>",
        recorded=lambda _key: SimpleNamespace(
            status="applied", identity={"rename": {"before": "web1", "after": "web2"}}
        ),
    )
    effect = SimpleNamespace(target=f"lpar:{LPAR}")
    assert _await(reconfigure._classify_rename(ctx, effect)).status == "applied"
    hmc.name = "web1"
    assert _await(reconfigure._classify_rename(ctx, effect)).status == "not_applied"
    hmc.name = "other"
    assert _await(reconfigure._classify_rename(ctx, effect)).status == "needs_attention"


# ---------------------------------------------------------------------------
# The name-addressed profile-write target (ADR 0313 Decision 12)
# ---------------------------------------------------------------------------


def _classify_named(monkeypatch, found):

    async def by_name(_hmc, lpar, system, name):
        assert (lpar, system, name) == (LPAR, SYS, "hmcpctl_abc")
        return found

    monkeypatch.setattr(profile_write, "profile_by_name", by_name)
    ctx = SimpleNamespace(
        partition=(SYS, LPAR), host="hmc.test", connection="<default>"
    )
    effect = SimpleNamespace(target=profile_write.target_by_name(LPAR, "hmcpctl_abc"))
    classify = profile_write.profile_write_classifier(
        lambda _ctx, _effect: (ProfilePatch(desired_memory=1024),) * 2,
        lambda _connection, _host: FakeHMC(),
    )
    return _await(classify(ctx, effect))


def test_a_named_profile_that_is_absent_was_not_written(monkeypatch):
    assert _classify_named(monkeypatch, None).status == "not_applied"


@pytest.mark.parametrize(
    ("saved", "status"), [(1024, "applied"), (900, "needs_attention")]
)
def test_a_named_profile_classifies_by_its_values(monkeypatch, saved, status):

    element = apply_profile_patch(
        profile_document(), ProfilePatch(desired_memory=saved)
    )
    found = SimpleNamespace(uuid=PROFILE, element=element)
    verdict = _classify_named(monkeypatch, found)
    assert verdict.status == status
    if status == "applied":
        assert verdict.identity == {
            "profile_uuid": PROFILE,
            "profile_name": "hmcpctl_abc",
        }


def test_the_uuid_parser_refuses_a_named_target():
    with pytest.raises(ValueError, match="malformed"):
        profile_write.parse_target(profile_write.target_by_name(LPAR, "p"))


def test_profile_by_name_reads_each_profile_once(monkeypatch):
    names = {"u1": "default_profile", "u2": "hmcpctl_abc"}

    class Feed:
        async def list_child(self, kind, parent, child):
            assert (kind, parent, child) == (
                "LogicalPartition",
                LPAR,
                "LogicalPartitionProfile",
            )
            return [{"UUID": uuid} for uuid in names]

    async def read(_hmc, kind, parent, uuid, system):
        assert (kind, parent, system) == ("LogicalPartition", LPAR, SYS)
        return SimpleNamespace(uuid=uuid, name=names[uuid])

    monkeypatch.setattr(profile_write, "read_native_profile", read)
    found = _await(profile_write.profile_by_name(Feed(), LPAR, SYS, "hmcpctl_abc"))
    assert found.uuid == "u2"
    assert _await(profile_write.profile_by_name(Feed(), LPAR, SYS, "absent")) is None


def test_holds_compares_a_profile_name_as_text():
    element = profile_document()
    one(element, "ProfileName").text = "07"

    assert not profile_write.holds(element, ProfilePatch(name="7"))
    assert profile_write.holds(element, ProfilePatch(name="07"))
