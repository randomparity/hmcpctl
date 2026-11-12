"""Recorded argument admission shared by durable LPAR adapters."""

from types import MappingProxyType

import pytest

from hmcpctl.operations.logical.store import OperationRefused
from hmcpctl.server_tools.lpar import power, provision, reconfigure
from hmcpctl.server_tools.lpar.continuation import continuation_arguments


def test_missing_record_keeps_the_actionable_refusal():
    with pytest.raises(OperationRefused) as caught:
        continuation_arguments(None, {"name": "replacement"}, "request-7")
    assert caught.value.reason == "not_found"
    assert str(caught.value) == (
        "not_found: no operation has request_id request-7; omit continuation to start one"
    )


def test_empty_record_is_present_and_values_are_not_rebuilt():
    value = []
    supplied = MappingProxyType({"value": value, "enabled": False, "count": 0})
    result = continuation_arguments(MappingProxyType({}), supplied, "r")
    assert result == supplied
    assert result is not supplied
    assert result["value"] is value


def test_supplied_values_override_without_mutating_either_mapping():
    recorded = {"target": "recorded", "retained": []}
    supplied = {"target": "supplied"}
    result = continuation_arguments(recorded, supplied, "r")
    assert result == {"target": "supplied", "retained": []}
    assert result["retained"] is recorded["retained"]
    assert recorded == {"target": "recorded", "retained": []}
    assert supplied == {"target": "supplied"}


@pytest.mark.parametrize(
    ("adapter", "tool"),
    [
        (power, "hmc_power_lpar"),
        (reconfigure, "hmc_reconfigure_lpar"),
        (provision, "hmc_provision_lpar"),
    ],
)
@pytest.mark.parametrize("recorded", [None, {}, {"target": "recorded"}])
def test_each_adapter_keeps_one_scoped_continuation_lookup(
    monkeypatch, adapter, tool, recorded
):
    calls = []

    def read(agent, request, tool):
        calls.append((agent, request, tool))
        return recorded

    monkeypatch.setattr(adapter.engine, "recorded_arguments", read)
    given = {"target": "supplied"}
    if recorded is None:
        with pytest.raises(OperationRefused, match="^not_found:"):
            adapter._effective(given, "agent", "request", "resume")
    else:
        result = adapter._effective(given, "agent", "request", "resume")
        if adapter is provision:
            result, exists = result
            assert exists is True
        assert result == given
    assert calls == [("agent", "request", tool)]


@pytest.mark.parametrize(
    ("adapter", "given", "defaults"),
    [
        (
            power,
            {"lpar_name_or_uuid": "p", "system_name_or_uuid": "s", "action": "start"},
            {"mode": "graceful"},
        ),
        (
            reconfigure,
            {"lpar_name_or_uuid": "p", "system_name_or_uuid": "s", "patch": {}},
            {"allow_disruption": False, "exclusive_writer_window": False},
        ),
    ],
)
def test_new_power_and_reconfiguration_keep_defaults_without_store_reads(
    monkeypatch, adapter, given, defaults
):
    def forbidden(*args):
        pytest.fail("a new power/reconfiguration request must not read its record")

    monkeypatch.setattr(adapter.engine, "recorded_arguments", forbidden)
    assert adapter._effective(given, "agent", "request", "none") == {
        **defaults,
        **given,
    }


@pytest.mark.parametrize("recorded", [None, {}])
def test_new_provisioning_keeps_its_existence_read_and_install_power_default(
    monkeypatch, recorded
):
    calls = []

    def read(*args):
        calls.append(args)
        return recorded

    monkeypatch.setattr(provision.engine, "recorded_arguments", read)
    given = {
        "system_name_or_uuid": "s",
        "name": "p",
        "adapters": {},
        "storage": {},
        "install": {},
    }
    effective, exists = provision._effective(given, "agent", "request", "none")
    assert effective["power_on"] is False
    assert exists is (recorded is not None)
    assert all(effective[key] is value for key, value in given.items())
    assert calls == [("agent", "request", provision.PROVISION_TOOL_NAME)]
