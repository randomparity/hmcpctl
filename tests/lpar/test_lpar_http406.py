"""Tool-layer tests for HTTP 406 behaviour on LPAR write tools.

hmc_create_lpar falls back to the CLI (mksyscfg over SSH) when REST returns
HTTP 406 or a 400 REST0001 schema rejection, rather than raising.  hmc_modify_lpar and the DLPAR tools have no
CLI fallback and still surface an actionable HMCError on 406.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import xml.etree.ElementTree as ET  # nosec B405 - types only; parsing uses defusedxml
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from conftest import (
    LPAR_RESOURCE_CONFIG,
    live_fixture,
    live_response,
    mock_system_summaries,
    mock_system_summary,
)
from defusedxml import ElementTree as DET

from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.lpar import LparResources, build_lpar_document
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.creation.core import LparCreation, create_and_stamp_lpar
from hmcpctl.operations.lpar.ownership import _resolve_system_name as _system_name
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.server_tools.lpar.lifecycle import (
    hmc_dlpar_mem,
    hmc_dlpar_proc,
    hmc_modify_lpar,
)
from hmcpctl.server_tools.lpar.lifecycle_create import hmc_create_lpar
from hmcpctl.ssh.lpar import (
    apply_lpar_profile_via_cli,
    complete_create_resources,
    create_lpar_via_cli,
)
from hmcpctl.ssh.transport import HMCCLIError

SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"
LPAR_UUID = "00000000-0000-0000-0000-000000000002"


@pytest.mark.asyncio
async def test_system_name_propagates_unexpected_rest_failure():
    hmc = AsyncMock()
    hmc.list_system_summaries.side_effect = TypeError("programming defect")

    with pytest.raises(TypeError, match="programming defect"):
        await _system_name(hmc, SYSTEM_UUID, "fallback")


@pytest.mark.asyncio
async def test_system_name_uses_fallback_only_for_expected_lookup_failures():
    hmc = AsyncMock()
    hmc.list_system_summaries.side_effect = HMCError("REST unavailable")

    with patch(
        "hmcpctl.operations.lpar.ownership.resolve_system_cli_name",
        new=AsyncMock(side_effect=HMCCLIError("SSH unavailable")),
    ):
        assert await _system_name(hmc, SYSTEM_UUID, "fallback") == "fallback"


@pytest.mark.asyncio
async def test_system_name_falls_back_on_whitespace_only_rest_name():
    """Issue #1026. A whitespace-only ``SystemName`` is truthy under the old
    ``if name:`` check, so it slipped through unrejected; the shared helper's
    ``.strip()`` treats it as absent and diverts to the SSH lookup instead."""
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": "   "}
    ]

    with patch(
        "hmcpctl.operations.lpar.ownership.resolve_system_cli_name",
        new=AsyncMock(return_value="cli-name"),
    ):
        assert await _system_name(hmc, SYSTEM_UUID, "fallback") == "cli-name"


EMPTY_FEED = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><feed xmlns="http://www.w3.org/2005/Atom"/>'

LPAR_ENTRY = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{LPAR_UUID}</id>
  <title>LogicalPartition</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <PartitionName>lpar1</PartitionName>
{LPAR_RESOURCE_CONFIG}\
      <PartitionState>not activated</PartitionState>
    </LogicalPartition>
  </content>
</entry>
"""

# The read-back filters the target system's feed by the created name.
CREATED_LPAR_ENTRY = LPAR_ENTRY.replace("lpar1", "new-lpar")

SYSTEM_ENTRY = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{SYSTEM_UUID}</id>
  <title>ManagedSystem:sys1</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <SystemName>sys1</SystemName>
      <State>operating</State>
    </ManagedSystem>
  </content>
</entry>
"""


def _hmc_env(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def _mock_dlpar_authorization(router) -> None:
    """The reads ADR 0092's guard and ADR 0094's containment check make."""
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(200, text=SYSTEM_ENTRY)
    )
    mock_system_summary(router, SYSTEM_UUID, "sys1")
    router.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY, headers={"ETag": "etag-1"})
    )
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=_partition_feed(LPAR_ENTRY))
    )


def _partition_feed(*entries: str) -> str:
    """Wrap rendered LPAR entries in the Atom feed envelope the client parses."""
    inner = "".join(
        entry.split("?>", 1)[1]
        .strip()
        .replace(' xmlns="http://www.w3.org/2005/Atom"', "", 1)
        for entry in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">' + inner + "</feed>"
    )


def _unowned_partition():
    """Patch the SSH ownership read to report a partition with no ADR 0011 stamp."""
    return patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=AsyncMock(return_value=""),
    )


# ---------------------------------------------------------------------- #
# hmc_create_lpar — HTTP 406 triggers CLI fallback
# ---------------------------------------------------------------------- #


# The 400 a V10R3 HMC returned for the LPAR create PUT once writes sent Accept */*
# (#935, 2026-09-24), as captured in window 2 of #1161.
_REST0001_BODY = live_fixture("rest-lpar-create-refused")["body"]
# Every V10R3 406 captured for a REST type the HMC serves came back with an empty
# body (template and PCM reads, 2026-09-30); the 406s here carry none (#1202).


def _mock_create_406(
    mock_hmc,
    order: list[str] | None = None,
    readback: httpx.Response | None = None,
    rejection: httpx.Response | None = None,
) -> None:
    """REST create is refused (406 by default); the scoped name read is empty before, found after."""
    lookup_responses = iter(
        [
            httpx.Response(200, text=EMPTY_FEED),
            readback or httpx.Response(200, text=CREATED_LPAR_ENTRY),
        ]
    )

    def _lookup(request):
        if order is not None:
            order.append("lookup")
        return next(lookup_responses)

    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        side_effect=_lookup
    )
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(200, text=SYSTEM_ENTRY)
    )
    mock_system_summary(mock_hmc, SYSTEM_UUID, "sys1")
    mock_system_summaries(mock_hmc, (SYSTEM_UUID, "sys1", "operating"))
    mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=rejection or httpx.Response(406)
    )


def _create_via_406(apply: AsyncMock, order: list[str] | None = None, **kwargs):
    """Run hmc_create_lpar down the mksyscfg fallback with SSH calls patched."""

    async def _mksyscfg(*args, **kw):
        if order is not None:
            order.append("mksyscfg")
        return ""

    with (
        patch(
            "hmcpctl.operations.lpar.creation.core.resolve_system_cli_name",
            new=AsyncMock(return_value="sys1"),
        ),
        patch(
            "hmcpctl.operations.lpar.creation.core.create_lpar_via_cli",
            new=AsyncMock(side_effect=_mksyscfg),
        ) as create_via_cli,
        patch(
            "hmcpctl.operations.lpar.creation.core.apply_lpar_profile_via_cli",
            new=apply,
        ),
        patch(
            "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership",
            new=AsyncMock(return_value="tok"),
        ),
    ):
        result = hmc_create_lpar(
            system_name_or_uuid=SYSTEM_UUID, name="new-lpar", **kwargs
        )
    return result, create_via_cli


def test_create_lpar_http_406_falls_back_to_cli(monkeypatch, mock_hmc):
    """hmc_create_lpar falls back to mksyscfg, applies the profile, then reads back.

    The read-back follows the apply, so the returned LPAR is the applied
    partition rather than the empty profile-only one (#939).
    """
    _hmc_env(monkeypatch)
    order: list[str] = []
    _mock_create_406(mock_hmc, order)

    async def _apply(*args, **kwargs):
        order.append("apply")
        return ""

    apply = AsyncMock(side_effect=_apply)
    result, create_via_cli = _create_via_406(apply, order)

    assert result.lpar.get("UUID") == LPAR_UUID
    create_via_cli.assert_awaited_once()
    resources = create_via_cli.await_args.kwargs["resources"]
    assert isinstance(resources, LparResources)
    assert resources.desired_memory == 4096
    assert resources.desired_vcpus == 1
    assert apply.await_args.args[1:] == ("sys1", "new-lpar")
    assert order == ["lookup", "mksyscfg", "apply", "lookup"]
    assert result.steps[1] == WorkflowStep("apply_profile", "ok", "default_profile")
    assert result.workflow_completed is True
    assert result.warnings == ()


def test_create_lpar_rest0001_schema_rejection_falls_back_to_cli(
    monkeypatch, mock_hmc, caplog
):
    """A 400 REST0001 created nothing, so the create still reaches mksyscfg (ADR 0178).

    The fallback logs the HMC's unmarshal message, so a defect in our own document is
    not hidden behind a successful mksyscfg create (#1164).
    """
    _hmc_env(monkeypatch)
    order: list[str] = []
    _mock_create_406(
        mock_hmc, order, rejection=httpx.Response(400, text=_REST0001_BODY)
    )
    apply = AsyncMock(return_value="")

    with caplog.at_level(
        logging.WARNING, logger="hmcpctl.operations.lpar.creation.core"
    ):
        result, create_via_cli = _create_via_406(apply, order)

    create_via_cli.assert_awaited_once()
    apply.assert_awaited_once()
    assert result.lpar.get("UUID") == LPAR_UUID
    assert order == ["lookup", "mksyscfg", "lookup"]
    assert any(
        "falling back to mksyscfg" in record.getMessage()
        and "must appear on element 'PartitionProcessorConfiguration'"
        in record.getMessage()
        for record in caplog.records
    )


def test_create_lpar_other_400_is_raised_without_cli_fallback(monkeypatch, mock_hmc):
    """A 400 that is not a schema rejection is the HMC's answer, not a reason to retry."""
    _hmc_env(monkeypatch)
    _mock_create_406(
        mock_hmc,
        rejection=live_response("rest-job-entry-uuid-refused")[1],
    )
    apply = AsyncMock(return_value="")

    with pytest.raises(HMCError, match="REST000B"):
        _create_via_406(apply)

    apply.assert_not_awaited()


def test_create_lpar_http_406_no_apply_leaves_profile_unapplied(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    _mock_create_406(mock_hmc)
    apply = AsyncMock(return_value="")

    result, _ = _create_via_406(apply, apply_partition_profile=False)

    apply.assert_not_awaited()
    assert result.steps[1] == WorkflowStep("apply_profile", "skipped")
    assert result.workflow_completed is True
    assert len(result.warnings) == 1
    assert "'default_profile' was not applied" in result.warnings[0]
    assert "no current configuration" in result.warnings[0]


@pytest.mark.parametrize("apply_partition_profile", [True, False])
def test_create_lpar_rest_success_reports_skipped_apply(
    monkeypatch, mock_hmc, apply_partition_profile
):
    """A REST create needs no apply; a requested apply is reported skipped (#1083, #1164).

    A V10R3 REST create writes default_profile and a current configuration, and the
    partition activates without an apply (#1161 P37), so the skip carries no warning.
    An explicit ``apply_partition_profile=False`` requested nothing, so it reports nothing.
    """
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=EMPTY_FEED)
    )
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(200, text=SYSTEM_ENTRY)
    )
    mock_system_summary(mock_hmc, SYSTEM_UUID, "sys1")
    mock_system_summaries(mock_hmc, (SYSTEM_UUID, "sys1", "operating"))
    mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY)
    )
    apply = AsyncMock(return_value="")

    with patch(
        "hmcpctl.operations.lpar.creation.core.apply_lpar_profile_via_cli", new=apply
    ):
        result = hmc_create_lpar(
            system_name_or_uuid=SYSTEM_UUID,
            name="new-lpar",
            apply_partition_profile=apply_partition_profile,
        )

    apply.assert_not_awaited()
    assert result.lpar.get("UUID") == LPAR_UUID
    if not apply_partition_profile:
        assert [s.step for s in result.steps] == ["create"]
        assert not any("not applied" in w for w in result.warnings)
        return
    assert result.steps[1].step == "apply_profile"
    assert result.steps[1].status == "skipped"
    assert "set the current configuration" in result.steps[1].result
    assert not any("not applied" in w or "not performed" in w for w in result.warnings)


def test_create_lpar_http_406_apply_error_stops_the_workflow(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    _mock_create_406(mock_hmc)
    apply = AsyncMock(side_effect=HMCCLIError("HSCL boom"))

    result, _ = _create_via_406(apply)

    assert result.resource_created is True
    assert result.lpar.get("UUID") == LPAR_UUID
    assert result.steps[1].step == "apply_profile"
    assert result.steps[1].status == "error"
    assert "HSCL boom" in result.steps[1].result
    assert result.workflow_completed is False
    assert "'default_profile' was not applied" in result.warnings[0]
    assert "redo any skipped steps" in result.warnings[0]


def test_create_lpar_http_406_readback_error_still_reports_the_create(
    monkeypatch, mock_hmc
):
    """A failed read-back after mksyscfg keeps the create and apply result (#1014)."""
    _hmc_env(monkeypatch)
    _mock_create_406(mock_hmc, readback=httpx.Response(500))
    apply = AsyncMock(return_value="")

    result, _ = _create_via_406(apply)

    assert result.resource_created is True
    assert result.lpar is None
    assert result.ownership_stamped is None
    assert result.steps[1] == WorkflowStep("apply_profile", "ok", "default_profile")
    assert len(result.warnings) == 1
    assert "read-back after mksyscfg failed" in result.warnings[0]
    assert "HTTP 500" in result.warnings[0]


def test_required_stamp_policy_raises_on_readback_error_after_mksyscfg():
    """Under 'required' a failed read-back raises, naming the created partition."""
    hmc = AsyncMock()
    hmc.find_partition_by_name.side_effect = [None, HMCError("boom", status_code=500)]
    hmc.create_logical_partition.side_effect = HMCError("nope", status_code=406)
    creation = LparCreation(
        "new-lpar", "AIX/Linux", LparResources(), stamp_policy="required"
    )

    with (
        patch(
            "hmcpctl.operations.lpar.creation.core.resolve_system_uuid",
            new=AsyncMock(return_value=SYSTEM_UUID),
        ),
        patch(
            "hmcpctl.operations.lpar.creation.core.resolve_system_cli_name",
            new=AsyncMock(return_value="sys1"),
        ),
        patch(
            "hmcpctl.operations.lpar.creation.core.create_lpar_via_cli", new=AsyncMock()
        ),
        pytest.raises(HMCError) as exc_info,
    ):
        asyncio.run(create_and_stamp_lpar(hmc, SYSTEM_UUID, creation))

    message = str(exc_info.value)
    assert "'new-lpar'" in message
    assert "mksyscfg" in message
    assert "still exists" in message
    assert "HTTP 500" in message
    assert isinstance(exc_info.value.__cause__, HMCError)


def test_required_stamp_policy_readback_error_includes_apply_warning():
    """A required-policy read-back-failed raise also reports a failed apply (#1020)."""
    hmc = AsyncMock()
    hmc.find_partition_by_name.side_effect = [None, HMCError("boom", status_code=500)]
    hmc.create_logical_partition.side_effect = HMCError("nope", status_code=406)
    creation = LparCreation(
        "new-lpar",
        "AIX/Linux",
        LparResources(),
        stamp_policy="required",
        apply_profile=True,
    )

    with (
        patch(
            "hmcpctl.operations.lpar.creation.core.resolve_system_uuid",
            new=AsyncMock(return_value=SYSTEM_UUID),
        ),
        patch(
            "hmcpctl.operations.lpar.creation.core.resolve_system_cli_name",
            new=AsyncMock(return_value="sys1"),
        ),
        patch(
            "hmcpctl.operations.lpar.creation.core.create_lpar_via_cli", new=AsyncMock()
        ),
        patch(
            "hmcpctl.operations.lpar.creation.core.apply_lpar_profile_via_cli",
            new=AsyncMock(side_effect=HMCCLIError("HSCL boom")),
        ),
        pytest.raises(HMCError) as exc_info,
    ):
        asyncio.run(create_and_stamp_lpar(hmc, SYSTEM_UUID, creation))

    message = str(exc_info.value)
    assert "'new-lpar'" in message
    assert "still exists" in message
    assert "'default_profile' was not applied" in message
    assert "redo any skipped steps" in message


def test_apply_profile_sends_verified_chsyscfg():
    """The #879-verified form: -p names the partition, -n the profile; no -f."""
    with patch(
        "hmcpctl.ssh.lpar.run_hmc_command", new=AsyncMock(return_value="")
    ) as run:
        asyncio.run(
            apply_lpar_profile_via_cli(HMCConfig(host="hmc.test"), "sys 1", "lp1")
        )
    assert run.await_args.args[1] == (
        "chsyscfg -r lpar -m 'sys 1' -o apply -p lp1 -n default_profile"
    )


# ---------------------------------------------------------------------- #
# hmc_modify_lpar — HTTP 406 actionable error
# ---------------------------------------------------------------------- #


def test_modify_lpar_http_406_actionable(monkeypatch, mock_hmc):
    """hmc_modify_lpar returns an actionable message on HTTP 406."""
    _hmc_env(monkeypatch)
    # The read-modify-write's read
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY, headers={"ETag": "etag-1"})
    )
    # modify returns 406
    mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(406)
    )

    with (
        patch(
            "hmcpctl.operations.lpar.dlpar.resolve_and_authorize_lpar_mutation",
            new=AsyncMock(return_value=LPAR_UUID),
        ),
        pytest.raises(HMCError) as exc_info,
    ):
        hmc_modify_lpar(
            lpar_name_or_uuid=LPAR_UUID,
            resources=LparResources(desired_memory=8192),
        )

    assert exc_info.value.status_code == 406
    msg = str(exc_info.value)
    assert "406" in msg
    assert "X-HMC-Schema-Version" in msg
    assert "HMC_SCHEMA_VERSION" not in msg
    assert exc_info.value.body == ""


# ---------------------------------------------------------------------- #
# hmc_dlpar_proc — HTTP 406 actionable error
# ---------------------------------------------------------------------- #


def test_dlpar_proc_http_406_actionable(monkeypatch, mock_hmc):
    """hmc_dlpar_proc returns an actionable message on HTTP 406."""
    _hmc_env(monkeypatch)
    _mock_dlpar_authorization(mock_hmc)
    # DLPAR POST returns 406
    mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(406)
    )

    with _unowned_partition(), pytest.raises(HMCError) as exc_info:
        hmc_dlpar_proc(
            lpar_name_or_uuid=LPAR_UUID,
            resources=LparResources(desired_procs=0.5),
            system_name_or_uuid=SYSTEM_UUID,
        )

    assert exc_info.value.status_code == 406
    msg = str(exc_info.value)
    assert "406" in msg
    assert "X-HMC-Schema-Version" in msg
    assert "HMC_SCHEMA_VERSION" not in msg


# ---------------------------------------------------------------------- #
# hmc_dlpar_mem — HTTP 406 actionable error
# ---------------------------------------------------------------------- #


def test_dlpar_mem_http_406_actionable(monkeypatch, mock_hmc):
    """hmc_dlpar_mem returns an actionable message on HTTP 406."""
    _hmc_env(monkeypatch)
    _mock_dlpar_authorization(mock_hmc)
    # DLPAR POST returns 406
    mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(406)
    )

    with _unowned_partition(), pytest.raises(HMCError) as exc_info:
        hmc_dlpar_mem(
            lpar_name_or_uuid=LPAR_UUID,
            resources=LparResources(desired_memory=8192),
            system_name_or_uuid=SYSTEM_UUID,
        )

    assert exc_info.value.status_code == 406
    msg = str(exc_info.value)
    assert "406" in msg
    assert "X-HMC-Schema-Version" in msg
    assert "HMC_SCHEMA_VERSION" not in msg


# ---------------------------------------------------------------------- #
# create_lpar_via_cli — omitted processing units vs. virtual processors (#938)
# ---------------------------------------------------------------------- #


def _cli_create(resources: LparResources) -> AsyncMock:
    """Run the CLI create with *resources*; return the patched command runner."""
    with patch(
        "hmcpctl.ssh.lpar.run_hmc_command", new=AsyncMock(return_value="")
    ) as run:
        asyncio.run(
            create_lpar_via_cli(
                HMCConfig(host="hmc.test"), "sys1", "lp1", resources=resources
            )
        )
    return run


@pytest.mark.parametrize(
    ("resources", "option"),
    [
        (LparResources(desired_vcpus=3, max_vcpus=6), "--procs"),
        (LparResources(min_vcpus=2, desired_vcpus=2, desired_procs=0.4), "--min-procs"),
        (LparResources(dedicated=False, desired_vcpus=3), "--procs"),
    ],
)
def test_cli_create_refuses_default_units_for_several_vcpus(resources, option):
    """The 0.1 unit default is never sent with more than one virtual processor."""
    with (
        patch(
            "hmcpctl.ssh.lpar.run_hmc_command", new=AsyncMock(return_value="")
        ) as run,
        pytest.raises(HMCCLIError, match=option),
    ):
        asyncio.run(
            create_lpar_via_cli(
                HMCConfig(host="hmc.test"), "sys1", "lp1", resources=resources
            )
        )
    run.assert_not_awaited()


def test_cli_create_sends_explicit_units_with_several_vcpus():
    run = _cli_create(
        LparResources(
            min_procs=0.1,
            desired_procs=0.3,
            max_procs=2.0,
            desired_vcpus=3,
            max_vcpus=6,
        )
    )
    command = run.await_args.args[1]
    assert "min_proc_units=0.1,desired_proc_units=0.3,max_proc_units=2.0" in command
    assert "min_procs=1,desired_procs=3,max_procs=6" in command


def test_cli_create_keeps_unit_defaults_for_one_vcpu():
    command = _cli_create(LparResources(desired_vcpus=1)).await_args.args[1]
    assert "min_proc_units=0.1,desired_proc_units=0.1,max_proc_units=2.0" in command


# ---------------------------------------------------------------------- #
# create_lpar_via_cli — max_proc_units default vs. --max-vcpus (#949)
# ---------------------------------------------------------------------- #


def test_cli_create_refuses_max_default_exceeding_max_vcpus():
    """A single max vCPU cannot use the max(desired, 2.0) processing-unit default."""
    with (
        patch(
            "hmcpctl.ssh.lpar.run_hmc_command", new=AsyncMock(return_value="")
        ) as run,
        pytest.raises(HMCCLIError, match="--max-procs"),
    ):
        asyncio.run(
            create_lpar_via_cli(
                HMCConfig(host="hmc.test"),
                "sys1",
                "lp1",
                resources=LparResources(max_vcpus=1),
            )
        )
    run.assert_not_awaited()


def test_cli_create_keeps_max_default_for_large_max_vcpus():
    """A large --max-vcpus keeps the 2.0-unit default; no minimum is derived (#949)."""
    command = _cli_create(LparResources(max_vcpus=100)).await_args.args[1]
    assert "max_proc_units=2.0" in command
    assert "max_procs=100" in command


# ---------------------------------------------------------------------- #
# create_lpar_via_cli — explicit processing units vs. virtual processors (#1034)
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("resources", "match"),
    [
        (
            LparResources(max_procs=4.0, max_vcpus=2),
            r"max_procs=4\.0 exceeds max_vcpus=2.*--max-procs",
        ),
        (
            LparResources(desired_procs=1.5),
            r"desired_procs=1\.5 exceeds desired_vcpus=1.*--vcpus",
        ),
        (
            LparResources(
                min_procs=1.5, desired_procs=2.0, desired_vcpus=2, max_vcpus=4
            ),
            r"min_procs=1\.5 exceeds min_vcpus=1.*--min-procs",
        ),
    ],
)
def test_cli_create_refuses_units_above_vcpus(resources, match):
    with (
        patch(
            "hmcpctl.ssh.lpar.run_hmc_command", new=AsyncMock(return_value="")
        ) as run,
        pytest.raises(HMCCLIError, match=match),
    ):
        asyncio.run(
            create_lpar_via_cli(
                HMCConfig(host="hmc.test"), "sys1", "lp1", resources=resources
            )
        )
    run.assert_not_awaited()


def test_cli_create_sends_units_equal_to_vcpus():
    command = _cli_create(
        LparResources(
            min_procs=1.0,
            desired_procs=2.0,
            max_procs=4.0,
            min_vcpus=1,
            desired_vcpus=2,
            max_vcpus=4,
        )
    ).await_args.args[1]
    assert "desired_proc_units=2.0" in command
    assert "desired_procs=2" in command


# ---------------------------------------------------------------------- #
# create_lpar_via_cli — processor mode of the mksyscfg record (#948)
# ---------------------------------------------------------------------- #


def _record(command: str) -> dict[str, str]:
    """Parse the ``-i`` record of a mksyscfg *command* into attribute pairs."""
    record = command.rsplit(" -i ", 1)[1].strip("'")
    return dict(pair.split("=", 1) for pair in record.split(","))


PROC_FIELDS = (
    "proc_mode",
    "sharing_mode",
    "min_proc_units",
    "desired_proc_units",
    "max_proc_units",
    "min_procs",
    "desired_procs",
    "max_procs",
)


def _proc_fields(resources: LparResources) -> dict[str, str]:
    record = _record(_cli_create(resources).await_args.args[1])
    return {key: value for key, value in record.items() if key in PROC_FIELDS}


def test_cli_create_sends_dedicated_record_for_dedicated_request():
    """CLI floats and default vcpus never leak units or vcpus into a ded record."""
    fields = _proc_fields(
        LparResources(
            desired_memory=4096,
            dedicated=True,
            min_procs=1.0,
            desired_procs=2.0,
            max_procs=4.0,
            desired_vcpus=1,
            max_vcpus=2,
        )
    )
    assert fields == {
        "proc_mode": "ded",
        "min_procs": "1",
        "desired_procs": "2",
        "max_procs": "4",
        "sharing_mode": "keep_idle_procs",
    }


def test_cli_create_defaults_omitted_dedicated_counts():
    fields = _proc_fields(
        LparResources(desired_memory=4096, dedicated=True, desired_procs=3.0)
    )
    # mksyscfg has no dedicated sharing_mode default (#1161 P38), so one is supplied.
    assert fields == {
        "proc_mode": "ded",
        "min_procs": "1",
        "desired_procs": "3",
        "max_procs": "3",
        "sharing_mode": "keep_idle_procs",
    }


@pytest.mark.parametrize("dedicated", [None, False])
def test_cli_create_sends_shared_record_otherwise(dedicated):
    fields = _proc_fields(
        LparResources(
            dedicated=dedicated,
            min_procs=0.1,
            desired_procs=0.5,
            max_procs=2.0,
            desired_vcpus=2,
            max_vcpus=4,
        )
    )
    assert fields == {
        "proc_mode": "shared",
        "sharing_mode": "uncap",
        "min_proc_units": "0.1",
        "desired_proc_units": "0.5",
        "max_proc_units": "2.0",
        "min_procs": "1",
        "desired_procs": "2",
        "max_procs": "4",
    }


@pytest.mark.parametrize(
    ("resources", "option"),
    [
        (
            LparResources(dedicated=True, min_procs=0.5, desired_procs=1.0),
            "--min-procs",
        ),
        (LparResources(dedicated=True, desired_procs=1.5), "--procs"),
        (
            LparResources(dedicated=True, desired_procs=1.0, max_procs=2.5),
            "--max-procs",
        ),
    ],
)
def test_cli_create_refuses_fractional_dedicated_counts(resources, option):
    """A fractional count is refused before mksyscfg, naming the option."""
    with (
        patch(
            "hmcpctl.ssh.lpar.run_hmc_command", new=AsyncMock(return_value="")
        ) as run,
        pytest.raises(HMCCLIError, match=option),
    ):
        asyncio.run(
            create_lpar_via_cli(
                HMCConfig(host="hmc.test"), "sys1", "lp1", resources=resources
            )
        )
    run.assert_not_awaited()


def test_cli_create_passes_dedicated_sharing_mode():
    fields = _proc_fields(
        LparResources(dedicated=True, desired_procs=1.0, sharing_mode="keep_idle_procs")
    )
    assert fields["sharing_mode"] == "keep_idle_procs"


@pytest.mark.parametrize("mode", ["capped", "uncapped"])
def test_cli_create_refuses_shared_sharing_mode_for_dedicated(mode):
    with (
        patch(
            "hmcpctl.ssh.lpar.run_hmc_command", new=AsyncMock(return_value="")
        ) as run,
        pytest.raises(HMCCLIError, match="sharing_mode"),
    ):
        asyncio.run(
            create_lpar_via_cli(
                HMCConfig(host="hmc.test"),
                "sys1",
                "lp1",
                resources=LparResources(
                    dedicated=True, desired_procs=1.0, sharing_mode=mode
                ),
            )
        )
    run.assert_not_awaited()


def test_cli_create_honors_capped_shared_request():
    """The record carries the requested capping, not a fixed ``uncap`` (#1164)."""
    fields = _proc_fields(LparResources(desired_procs=0.5, uncapped=False))
    assert fields["sharing_mode"] == "cap"


@pytest.mark.parametrize("mode", ["keep_idle_procs", "share_idle_procs"])
def test_cli_create_refuses_dedicated_sharing_mode_for_shared(mode):
    with (
        patch(
            "hmcpctl.ssh.lpar.run_hmc_command", new=AsyncMock(return_value="")
        ) as run,
        pytest.raises(HMCCLIError, match="dedicated processors only"),
    ):
        asyncio.run(
            create_lpar_via_cli(
                HMCConfig(host="hmc.test"),
                "sys1",
                "lp1",
                resources=LparResources(desired_procs=0.5, sharing_mode=mode),
            )
        )
    run.assert_not_awaited()


# ---------------------------------------------------------------------- #
# One defaults policy for both create paths (#1164)
# ---------------------------------------------------------------------- #


def _rest_create_body(mock_hmc, **kwargs) -> ET.Element:
    """Run hmc_create_lpar down a successful REST create and parse the PUT body."""
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=EMPTY_FEED)
    )
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(200, text=SYSTEM_ENTRY)
    )
    mock_system_summary(mock_hmc, SYSTEM_UUID, "sys1")
    mock_system_summaries(mock_hmc, (SYSTEM_UUID, "sys1", "operating"))
    put = mock_hmc.put(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(200, text=LPAR_ENTRY))
    with patch("hmcpctl.operations.lpar.creation.core.create_lpar_via_cli") as via_cli:
        hmc_create_lpar(system_name_or_uuid=SYSTEM_UUID, name="new-lpar", **kwargs)
    via_cli.assert_not_called()
    return DET.fromstring(put.calls.last.request.content)


def _uom_text(root: ET.Element, path: str) -> str | None:
    return root.findtext("/".join(f"{{{UOM_NS}}}{part}" for part in path.split("/")))


def test_rest_create_fills_the_values_v10r3_requires(monkeypatch, mock_hmc):
    """The tool's default resources name no processing units or minimum vCPUs.

    V10R3 refuses that document with 500 REST0126 naming min_procs, min_proc_units,
    max_proc_units and desired_proc_units (#1161 P38), so the REST create fills the
    defaults the mksyscfg record uses.
    """
    _hmc_env(monkeypatch)
    root = _rest_create_body(mock_hmc)

    ppc = "PartitionProcessorConfiguration"
    shared = f"{ppc}/SharedProcessorConfiguration"
    defaults = inspect.signature(hmc_create_lpar).parameters["resources"].default
    record = _record(_cli_create(defaults).await_args.args[1])
    assert {
        "HasDedicatedProcessors": _uom_text(root, f"{ppc}/HasDedicatedProcessors"),
        "SharingMode": _uom_text(root, f"{ppc}/SharingMode"),
        "min_proc_units": _uom_text(root, f"{shared}/MinimumProcessingUnits"),
        "desired_proc_units": _uom_text(root, f"{shared}/DesiredProcessingUnits"),
        "max_proc_units": _uom_text(root, f"{shared}/MaximumProcessingUnits"),
        "min_procs": _uom_text(root, f"{shared}/MinimumVirtualProcessors"),
        "desired_procs": _uom_text(root, f"{shared}/DesiredVirtualProcessors"),
        "max_procs": _uom_text(root, f"{shared}/MaximumVirtualProcessors"),
    } == {
        "HasDedicatedProcessors": "false",
        "SharingMode": "uncapped",
        "min_proc_units": "0.1",
        "desired_proc_units": "0.1",
        "max_proc_units": "2",
        "min_procs": record["min_procs"],
        "desired_procs": record["desired_procs"],
        "max_procs": record["max_procs"],
    }
    assert record["sharing_mode"] == "uncap"
    assert (
        float(record["min_proc_units"]),
        float(record["desired_proc_units"]),
        float(record["max_proc_units"]),
    ) == (0.1, 0.1, 2.0)


def test_rest_create_supplies_the_dedicated_sharing_mode(monkeypatch, mock_hmc):
    """V10R3 refuses a dedicated create without SharingMode (#1161 P30, P38)."""
    _hmc_env(monkeypatch)
    root = _rest_create_body(
        mock_hmc,
        resources=LparResources(desired_memory=2048, dedicated=True, desired_procs=1),
    )

    ppc = "PartitionProcessorConfiguration"
    assert _uom_text(root, f"{ppc}/HasDedicatedProcessors") == "true"
    assert _uom_text(root, f"{ppc}/SharingMode") == "keep idle procs"
    assert (
        _uom_text(root, f"{ppc}/DedicatedProcessorConfiguration/MinimumProcessors")
        == "1"
    )


def test_create_with_no_resource_values_goes_straight_to_mksyscfg(
    monkeypatch, mock_hmc
):
    """Only mksyscfg's all_resources=1 expresses a create with no resource values."""
    _hmc_env(monkeypatch)
    order: list[str] = []
    _mock_create_406(mock_hmc, order)

    result, create_via_cli = _create_via_406(
        AsyncMock(return_value=""), order, resources=LparResources()
    )

    assert not any(
        call.request.method == "PUT"
        and call.request.url.path.endswith("/LogicalPartition")
        for call in mock_hmc.calls
    )
    create_via_cli.assert_awaited_once()
    assert create_via_cli.await_args.kwargs["resources"] == LparResources()
    assert order == ["lookup", "mksyscfg", "lookup"]
    assert result.lpar.get("UUID") == LPAR_UUID


@pytest.mark.parametrize(
    ("resources", "cli_mode", "rest_mode", "weight"),
    [
        (LparResources(desired_procs=0.5, sharing_mode="capped"), "cap", "capped", "0"),
        (LparResources(desired_procs=0.5, uncapped=False), "cap", "capped", "0"),
        (LparResources(desired_procs=0.5), "uncap", "uncapped", None),
        (
            LparResources(desired_procs=0.5, uncapped=True, sharing_mode="capped"),
            "uncap",
            "uncapped",
            None,
        ),
    ],
)
def test_both_paths_take_the_same_capping(resources, cli_mode, rest_mode, weight):
    """``uncapped`` wins over ``sharing_mode``; either one alone decides (#1164)."""
    complete = complete_create_resources(resources)
    assert complete is not None
    root = DET.fromstring(build_lpar_document("p", resources=complete).encode())

    ppc = "PartitionProcessorConfiguration"
    assert _proc_fields(resources)["sharing_mode"] == cli_mode
    assert _uom_text(root, f"{ppc}/SharingMode") == rest_mode
    assert (
        _uom_text(root, f"{ppc}/SharedProcessorConfiguration/UncappedWeight") == weight
    )


def test_create_defaults_refuse_an_unknown_sharing_mode():
    with pytest.raises(ValueError, match="sharing_mode must be one of"):
        complete_create_resources(
            LparResources(desired_procs=0.5, sharing_mode="bogus")  # type: ignore[arg-type]
        )


def test_create_defaults_refuse_an_unhashable_sharing_mode():
    with pytest.raises(ValueError, match="sharing_mode must be one of"):
        complete_create_resources(
            LparResources(desired_procs=0.5, sharing_mode=["x"])  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_create_refuses_a_keylock_outside_the_schema_before_any_hmc_call():
    """V10R3's KeylockPosition.Enum has no ``auto``, and mksyscfg takes no keylock (#1164)."""
    hmc = AsyncMock()
    creation = LparCreation(
        "new-lpar", "AIX/Linux", LparResources(desired_memory=2048), keylock="auto"
    )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="keylock must be one of: normal, manual"):
        await create_and_stamp_lpar(hmc, SYSTEM_UUID, creation)

    assert hmc.mock_calls == []


@pytest.mark.asyncio
async def test_create_refuses_a_vios_partition_type_before_any_hmc_call():
    """V10R3 answers a LogicalPartition PUT typed Virtual IO Server with 500 REST0140."""
    hmc = AsyncMock()
    creation = LparCreation(
        "acmesys9-lp3", "Virtual IO Server", LparResources(desired_vcpus=1)
    )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="hmc_create_vios"):
        await create_and_stamp_lpar(hmc, SYSTEM_UUID, creation)

    assert hmc.mock_calls == []


def test_create_lpar_has_no_os_type():
    """OperatingSystemType is read-only; the HMC sets AIX/Linux itself (#1179)."""
    assert "os_type" not in inspect.signature(hmc_create_lpar).parameters
    assert "os_type" not in {f.name for f in dataclasses.fields(LparCreation)}


def test_create_tool_refuses_a_vios_type_before_logon(monkeypatch, mock_hmc):
    """A direct Python call bypasses the MCP schema; the tool still refuses first."""
    _hmc_env(monkeypatch)
    with pytest.raises(ValueError, match="hmc_create_vios"):
        hmc_create_lpar(
            system_name_or_uuid=SYSTEM_UUID,
            name="acmesys9-lp3",
            partition_type="Virtual IO Server",  # type: ignore[arg-type]
        )
    assert not mock_hmc.calls


def test_create_tool_passes_its_options_to_the_creation():
    captured: list[LparCreation] = []

    async def _create(hmc, system, creation, assignments):
        captured.append(creation)

    with (
        patch(
            "hmcpctl.server_tools.lpar.lifecycle_create.with_client",
            new=lambda fn, profile=None: asyncio.run(fn(None)),
        ),
        patch("hmcpctl.server_tools.lpar.lifecycle_create.create_lpar", new=_create),
    ):
        hmc_create_lpar(
            system_name_or_uuid="acmesys9",
            name="acmesys9-lp3",
            partition_type="OS400",
            partition_id=7,
            keylock="manual",
            max_virtual_slots=64,
            caller_token="t1",
            apply_partition_profile=False,
        )

    (creation,) = captured
    assert (
        creation.partition_type,
        creation.partition_id,
        creation.keylock,
        creation.max_virtual_slots,
        creation.caller_token,
        creation.apply_profile,
    ) == ("OS400", 7, "manual", 64, "t1", False)


@pytest.mark.asyncio
async def test_create_workflow_refuses_a_vios_type_before_prevalidation():
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.creation.workflow import create_lpar

    hmc = AsyncMock()
    creation = LparCreation(
        "acmesys9-lp3", "Virtual IO Server", LparResources(desired_vcpus=1)
    )  # type: ignore[arg-type]
    with (
        patch(
            "hmcpctl.operations.lpar.creation.workflow.prevalidate_lpar_pcie_assignments"
        ) as prevalidate,
        pytest.raises(ValueError, match="hmc_create_vios"),
    ):
        await create_lpar(hmc, "acmesys9", creation, LparPcieAssignments())
    prevalidate.assert_not_called()
    assert hmc.mock_calls == []
