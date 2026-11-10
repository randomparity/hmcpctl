"""The force-save persistence leg and its classifier (ADR 0322 Decision 3).

The leg overwrites a partition's named profile with its current configuration through
#637's helper; the helper's verified completion, or a readback showing every current
client adapter in the profile, decides that it landed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.operations.logical.engine import EffectNotApplied
from hmcpctl.operations.logical.models import EffectRecord
from hmcpctl.operations.lpar.profiles import persist as profile_persist

SYS = "0000000a-0000-4000-8000-000000000000"
LPAR = "0000000a-0000-4000-b000-000000000000"


def _profile(name: str, *slots: str) -> dict:
    adapters = [{"VirtualSlotNumber": slot} for slot in slots]
    return {
        "UUID": f"uuid-{name}",
        "Resource": {
            "ProfileName": name,
            "IOConfigurationInstance": {
                "ProfileVirtualIOAdapters": {
                    "ProfileVirtualIOAdapterSubclass": {
                        "ProfileVirtualSCSIClientAdapter": adapters
                    }
                }
            },
        },
    }


class FakeHMC:
    """A partition with current client adapters and a profile feed a save rewrites."""

    def __init__(self, current=("2", "3"), saved=("2",)):
        self.current = list(current)
        self.network: list[str] = []
        self.profiles = [_profile("default", *saved), _profile("hmcprec-00ff")]
        self.save_lands = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def list_child(self, parent, uuid, child):
        assert (parent, uuid) == ("LogicalPartition", LPAR)
        if child == "LogicalPartitionProfile":
            return self.profiles
        if child == "ClientNetworkAdapter":
            return [{"Resource": {"VirtualSlotNumber": slot}} for slot in self.network]
        if child != "VirtualSCSIClientAdapter":
            return []
        return [{"Resource": {"VirtualSlotNumber": slot}} for slot in self.current]


@pytest.fixture
def hmc(monkeypatch):
    fake = FakeHMC()
    fake.saves = []

    async def read(hmc, lpar_name_or_uuid, profile_selector, *, system_name_or_uuid):
        assert (system_name_or_uuid, lpar_name_or_uuid, profile_selector) == (
            SYS,
            LPAR,
            "default",
        )
        return SimpleNamespace(etag="E7")

    async def save(hmc, lpar_name_or_uuid, name, *, system_name_or_uuid, **kwargs):
        fake.saves.append(kwargs)
        if fake.save_lands:
            fake.profiles[0] = _profile("default", *fake.current)
        return fake.outcome

    fake.outcome = SimpleNamespace(
        execution="unknown",
        requested_change="unverified",
        job=object(),
        recovery_verified=True,
        recovery_profile=SimpleNamespace(name="hmcprec-00ff"),
        error=None,
        diagnostics=(),
    )
    monkeypatch.setattr(profile_persist, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(profile_persist, "POLL_SECONDS", 0)
    monkeypatch.setattr(profile_persist, "read_partition_profile", read)
    monkeypatch.setattr(profile_persist, "save_current_partition_profile", save)
    return fake


async def _save(hmc, window=True):
    return await profile_persist.save_current_over(
        hmc, SYS, LPAR, "default", exclusive_writer_window=window
    )


@pytest.mark.asyncio
async def test_a_save_the_readback_shows_is_applied_and_names_recovery_copies(hmc):
    identity = await _save(hmc)
    assert identity == {
        "profile_name": "default",
        "recovery_profiles": ["hmcprec-00ff"],
        "confirmed_by": "first_read",
    }
    assert hmc.saves == [
        {"overwrite": True, "expected_etag": "E7", "exclusive_writer_window": True}
    ]


@pytest.mark.asyncio
async def test_the_callers_window_assertion_reaches_the_helper(hmc):
    await _save(hmc, window=False)
    assert hmc.saves[0]["exclusive_writer_window"] is False


@pytest.mark.asyncio
async def test_a_verified_completion_decides_without_the_readback(hmc):
    hmc.save_lands = False
    hmc.outcome.execution, hmc.outcome.requested_change = "completed", "verified"
    assert (await _save(hmc))["profile_name"] == "default"


@pytest.mark.asyncio
async def test_a_save_never_submitted_is_not_applied_and_names_the_copy(hmc):
    hmc.save_lands = False
    hmc.outcome.job = None
    with pytest.raises(EffectNotApplied, match="hmcprec-00ff"):
        await _save(hmc)


@pytest.mark.asyncio
async def test_a_helper_failure_with_adapters_missing_is_uncertain(hmc, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise HMCError("job read failed")

    monkeypatch.setattr(profile_persist, "save_current_partition_profile", boom)
    with pytest.raises(HMCError, match="unconfirmed.*hmcprec-00ff"):
        await _save(hmc)


@pytest.mark.asyncio
async def test_a_helper_failure_the_readback_shows_landed_is_applied(hmc, monkeypatch):
    async def landed_then_raised(*_args, **_kwargs):
        hmc.profiles[0] = _profile("default", *hmc.current)
        raise HMCError("job status misread")

    monkeypatch.setattr(
        profile_persist, "save_current_partition_profile", landed_then_raised
    )
    assert (await _save(hmc))["profile_name"] == "default"


@pytest.mark.asyncio
async def test_an_empty_adapter_feed_does_not_vouch_for_an_empty_profile(hmc):
    hmc.current, hmc.save_lands = [], False
    hmc.profiles[0] = _profile("default")
    missing, _ = await profile_persist.persisted(hmc, LPAR, "default")
    assert missing == ["any virtual adapter"]


@pytest.mark.asyncio
async def test_an_unconfirmed_save_names_what_the_helper_reported(hmc):
    hmc.save_lands = False
    with pytest.raises(profile_persist.SaveUnconfirmed) as raised:
        await _save(hmc)
    assert "execution=unknown" in str(raised.value)
    assert "step=save" in str(raised.value) and "job=submitted" in str(raised.value)
    assert "1 adapter(s) missing" in str(raised.value)


@pytest.mark.asyncio
async def test_a_helper_refusal_before_any_call_is_named(hmc, monkeypatch):
    async def refused(*_args, **_kwargs):
        raise ValueError("expected_etag must match a fresh contained profile ETag")

    monkeypatch.setattr(profile_persist, "save_current_partition_profile", refused)
    with pytest.raises(profile_persist.SaveUnconfirmed, match="expected_etag must"):
        await _save(hmc)


@pytest.mark.asyncio
async def test_a_foreign_error_shows_only_its_status_and_hmc_codes(hmc, monkeypatch):
    text = (
        "save failed: Set-Cookie: JSESSIONID=abc123secret; REST0126 [HSCL2957] "
        "X-API-Session: tok-xyz REST0126"
    )

    async def refused(*_args, **_kwargs):
        raise HMCError(text, 409)

    monkeypatch.setattr(profile_persist, "save_current_partition_profile", refused)
    monkeypatch.setattr(profile_persist, "SETTLE_SECONDS", 0)
    with pytest.raises(profile_persist.SaveUnconfirmed) as raised:
        await _save(hmc)
    shown = str(raised.value)
    assert "HMCError, HTTP 409, codes=REST0126,HSCL2957" in shown
    for leaked in ("abc123secret", "tok-xyz", "JSESSIONID", "save failed"):
        assert leaked not in shown


@pytest.mark.asyncio
async def test_a_helper_error_string_shows_only_its_codes(hmc, monkeypatch):
    hmc.save_lands = False
    hmc.outcome.error = "boom: X-API-Session: secret-token HSCL0001"
    monkeypatch.setattr(profile_persist, "SETTLE_SECONDS", 0)
    with pytest.raises(profile_persist.SaveUnconfirmed) as raised:
        await _save(hmc)
    assert "error_codes=HSCL0001" in str(raised.value)
    assert "secret-token" not in str(raised.value)


@pytest.mark.asyncio
async def test_a_save_that_lands_late_is_confirmed_by_a_repoll(hmc, monkeypatch):
    hmc.save_lands = False
    monkeypatch.setattr(profile_persist, "SETTLE_SECONDS", 60)
    reads = []
    real = profile_persist.persisted

    async def late(*args, **kwargs):
        reads.append(1)
        if len(reads) == 3:
            hmc.profiles[0] = _profile("default", *hmc.current)
        return await real(*args, **kwargs)

    monkeypatch.setattr(profile_persist, "persisted", late)
    assert (await _save(hmc))["confirmed_by"] == "repoll"
    assert len(reads) == 3


@pytest.mark.asyncio
async def test_an_unreadable_profile_before_the_save_is_not_applied(hmc, monkeypatch):
    async def gone(*_args, **_kwargs):
        raise HMCError("profile read failed", 500)

    monkeypatch.setattr(profile_persist, "read_partition_profile", gone)
    with pytest.raises(EffectNotApplied, match="step=list, HMCError, HTTP 500"):
        await _save(hmc)
    assert hmc.saves == []


@pytest.mark.asyncio
async def test_a_failed_readback_is_unconfirmed_and_named(hmc, monkeypatch):
    async def unreadable(*_args, **_kwargs):
        raise HMCError("feed read failed", 503)

    monkeypatch.setattr(profile_persist, "persisted", unreadable)
    with pytest.raises(profile_persist.SaveUnconfirmed, match="step=readback"):
        await _save(hmc)


@pytest.mark.asyncio
async def test_a_missing_profile_is_an_error(hmc):
    hmc.profiles = []
    with pytest.raises(HMCError, match="no profile"):
        await profile_persist.persisted(hmc, LPAR, "default")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("saved", "status"), [(("2", "3"), "applied"), (("2",), "not_applied")]
)
async def test_classifier_reads_the_profile_back(saved, status):
    fake = FakeHMC(saved=saved)
    classify = profile_persist.profile_save_classifier(lambda _connection, _host: fake)
    ctx = SimpleNamespace(host="hmc.test", connection="<default>")
    effect = EffectRecord(
        "profile_save",
        profile_persist.KIND,
        profile_persist.target(LPAR, "default"),
        "uncertain",
        None,
    )
    verdict = await classify(ctx, effect)
    assert verdict.status == status
    if status == "applied":
        assert verdict.identity == {
            "profile_name": "default",
            "recovery_profiles": ["hmcprec-00ff"],
        }


def test_target_round_trips():
    assert profile_persist.parse_target(profile_persist.target(LPAR, "a b")) == (
        LPAR,
        "a b",
    )
    with pytest.raises(ValueError, match="malformed"):
        profile_persist.parse_target(f"lpar:{LPAR}/profile:x")


# ADR 0325: the removed-slot readback, an empty profile, and client network adapters.


@pytest.mark.asyncio
async def test_a_removed_slot_still_in_the_profile_is_unsaved():
    fake = FakeHMC(current=("2",), saved=("2", "5"))
    missing, _ = await profile_persist.persisted(
        fake, LPAR, "default", removed_slots=("5",)
    )
    assert missing == ["removed adapter in virtual slot 5 still in the profile"]
    assert (await profile_persist.persisted(fake, LPAR, "default"))[0] == []


@pytest.mark.asyncio
async def test_a_removed_slot_a_current_adapter_holds_is_saved():
    fake = FakeHMC(current=("2", "5"), saved=("2", "5"))
    missing, _ = await profile_persist.persisted(
        fake, LPAR, "default", removed_slots=("5",)
    )
    assert missing == []


@pytest.mark.asyncio
async def test_a_removal_only_save_may_leave_an_empty_profile():
    fake = FakeHMC(current=(), saved=())
    assert (await profile_persist.persisted(fake, LPAR, "default"))[0] == [
        "any virtual adapter"
    ]
    missing, _ = await profile_persist.persisted(
        fake, LPAR, "default", removed_slots=("5",), require_adapter=False
    )
    assert missing == []


@pytest.mark.asyncio
async def test_client_network_adapters_are_read_as_a_child_collection():
    fake = FakeHMC(current=(), saved=("4",))
    fake.network = ["4", "6"]
    missing, _ = await profile_persist.persisted(fake, LPAR, "default")
    assert missing == ["ClientNetworkAdapter in virtual slot 6"]


@pytest.mark.asyncio
async def test_the_save_passes_its_readback_options(hmc):
    hmc.current, hmc.save_lands = [], False
    hmc.profiles[0] = _profile("default", "5")
    with pytest.raises(profile_persist.SaveUnconfirmed, match="1 adapter"):
        await profile_persist.save_current_over(
            hmc,
            SYS,
            LPAR,
            "default",
            exclusive_writer_window=True,
            removed_slots=("5",),
            require_adapter=False,
        )


@pytest.mark.asyncio
async def test_classifier_applies_the_operations_readback_options():
    fake = FakeHMC(current=("2",), saved=("2", "5"))
    effect = EffectRecord(
        "profile_save",
        profile_persist.KIND,
        profile_persist.target(LPAR, "default"),
        "uncertain",
        None,
    )
    ctx = SimpleNamespace(host="hmc.test", connection="<default>")
    plain = profile_persist.profile_save_classifier(lambda _connection, _host: fake)
    removal = profile_persist.profile_save_classifier(
        lambda _connection, _host: fake, lambda _ctx: (("5",), False)
    )
    assert (await plain(ctx, effect)).status == "applied"
    assert (await removal(ctx, effect)).status == "not_applied"


@pytest.mark.asyncio
async def test_fingerprint_failure_from_save_stops_independent_readback(
    hmc, monkeypatch
):
    from unittest.mock import AsyncMock

    failure = HMCTLSFingerprintError("private-pin")
    save = AsyncMock(side_effect=failure)
    readback = AsyncMock()
    monkeypatch.setattr(profile_persist, "save_current_partition_profile", save)
    monkeypatch.setattr(profile_persist, "persisted", readback)
    with pytest.raises(HMCTLSFingerprintError) as caught:
        await _save(hmc)
    assert caught.value is failure
    readback.assert_not_awaited()


@pytest.mark.asyncio
async def test_fingerprint_outcome_retains_recovery_and_stops_readback(
    hmc, monkeypatch
):
    from unittest.mock import AsyncMock

    hmc.outcome.diagnostics = ("tls-identity-verification-failed",)
    readback = AsyncMock()
    monkeypatch.setattr(profile_persist, "persisted", readback)
    with pytest.raises(profile_persist.SaveUnconfirmed) as caught:
        await _save(hmc)
    assert "TLS" in str(caught.value)
    assert "hmcprec-00ff" in str(caught.value)
    assert "job=submitted" in str(caught.value)
    readback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["initial", "verified_readback"])
async def test_profile_save_does_not_hide_fingerprint_failure(hmc, monkeypatch, stage):
    from unittest.mock import AsyncMock

    failure = HMCTLSFingerprintError("private-pin")
    if stage == "initial":
        monkeypatch.setattr(
            profile_persist, "read_partition_profile", AsyncMock(side_effect=failure)
        )
    else:
        hmc.outcome.execution = "completed"
        hmc.outcome.requested_change = "verified"
        monkeypatch.setattr(
            profile_persist, "persisted", AsyncMock(side_effect=failure)
        )
    with pytest.raises(HMCTLSFingerprintError) as caught:
        await _save(hmc)
    assert caught.value is failure
    if stage == "initial":
        assert hmc.saves == []
