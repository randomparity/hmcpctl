"""Tool-layer tests for the partition-template library MCP tools.

The client methods and job builder are covered in test_templates_api.py;
these tests call the actual ``@mcp.tool`` functions in ``server_tools.templates.core``
against the respx ``mock_hmc`` router so the argument->URL mapping in the
tool bodies is exercised.
"""

from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
from conftest import (
    COMPLETED_JOB_ENTRY,
    JOB_ENTRY,
    JOB_ID,
    live_fixture,
    live_response,
    mock_system_summaries,
)

from hmcpctl.errors import HMCError
from hmcpctl.server_tools.templates.core import (
    hmc_deploy_partition_template,
    hmc_get_partition_template,
    hmc_list_partition_templates,
)

TEMPLATE_UUID = "tmpl-uuid-1"
TARGET_SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"
TEMPLATE_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:tmpl-uuid-1</id>
    <title>PartitionTemplate:aix-gold</title>
    <content type="application/vnd.ibm.powervm.templates+xml">
      <PartitionTemplate xmlns="http://www.ibm.com/xmlns/systems/power/firmware/templates/mc/2012_10/">
        <templateName>aix-gold</templateName>
      </PartitionTemplate>
    </content>
  </entry>
</feed>
"""


def _not_acceptable() -> httpx.Response:
    """V10R3's answer to an Accept it cannot serve: an empty HTTP 406 (#1202)."""
    capture = live_fixture("rest-templates-typed-406")
    return httpx.Response(capture["status"], text=capture["body"])


def _hmc_env(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def test_partition_templates_lists_all(monkeypatch, mock_hmc):
    """hmc_list_partition_templates() GETs the captured template library feed."""
    _hmc_env(monkeypatch)
    path, response = live_response("rest-templates-feed")
    mock_hmc.get(path).mock(return_value=response)
    result = hmc_list_partition_templates()
    assert result[0]["UUID"] == "000005da-abcd-4ef0-8abc-0000000005da"
    assert result[0]["Resource"]["partitionTemplateName"] == "QuickStart_lpar_rpa_1"


def test_partition_templates_with_uuid_gets_one(monkeypatch, mock_hmc):
    """hmc_get_partition_template GETs one template by UUID."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/templates/PartitionTemplate/{TEMPLATE_UUID}").mock(
        return_value=httpx.Response(200, text=TEMPLATE_FEED)
    )
    result = hmc_get_partition_template(TEMPLATE_UUID)
    assert result["Resource"]["templateName"] == "aix-gold"


def test_partition_templates_with_uuid_error_propagates(monkeypatch, mock_hmc):
    """A non-200 template GET surfaces as HMCError."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/templates/PartitionTemplate/{TEMPLATE_UUID}").mock(
        return_value=httpx.Response(404, text="<error>not found</error>")
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_get_partition_template(TEMPLATE_UUID)
    assert exc_info.value.status_code == 404


def test_partition_templates_list_http_406_names_media_type(monkeypatch, mock_hmc):
    """hmc_list_partition_templates() explains an HTTP 406 as media-type refusal."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/templates/PartitionTemplate").mock(
        return_value=_not_acceptable()
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_list_partition_templates()
    assert exc_info.value.status_code == 406
    error_msg = str(exc_info.value)
    # The message should be actionable and mention templates specifically, not raw HTTP error
    assert "refused the media type" in error_msg.lower()
    assert "not licensing" in error_msg.lower()
    assert "partition templates" in error_msg.lower()


def test_partition_templates_get_http_406_names_media_type(monkeypatch, mock_hmc):
    """A single template lookup explains an HTTP 406 as media-type refusal."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/templates/PartitionTemplate/{TEMPLATE_UUID}").mock(
        return_value=_not_acceptable()
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_get_partition_template(TEMPLATE_UUID)
    assert exc_info.value.status_code == 406
    error_msg = str(exc_info.value)
    assert "refused the media type" in error_msg.lower()
    assert "not licensing" in error_msg.lower()
    assert "partition templates" in error_msg.lower()


def test_deploy_partition_template_http_406_names_media_type(monkeypatch, mock_hmc):
    """hmc_deploy_partition_template explains an HTTP 406 as media-type refusal."""
    _hmc_env(monkeypatch)
    mock_hmc.put("/rest/api/templates/PartitionTemplate/draft-uuid/do/deploy").mock(
        return_value=_not_acceptable()
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_deploy_partition_template("draft-uuid", TARGET_SYSTEM_UUID)
    assert exc_info.value.status_code == 406
    error_msg = str(exc_info.value)
    assert "refused the media type" in error_msg.lower()
    assert "not licensing" in error_msg.lower()
    assert "partition templates" in error_msg.lower()


def test_deploy_partition_template_submits_job(monkeypatch, mock_hmc):
    """hmc_deploy_partition_template PUTs a Deploy job to the draft template."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        "/rest/api/templates/PartitionTemplate/draft-uuid/do/deploy"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    result = hmc_deploy_partition_template("draft-uuid", TARGET_SYSTEM_UUID)
    body = route.calls.last.request.content.decode()
    assert "Deploy</OperationName>" in body
    assert (
        f'<ParameterName kb="ROR" kxe="false">TargetUuid</ParameterName>\n'
        f'      <ParameterValue kb="CUR" kxe="false">{TARGET_SYSTEM_UUID}'
        "</ParameterValue>" in body
    )
    assert (
        '<ParameterName kb="ROR" kxe="false">TemplateUuid</ParameterName>\n'
        '      <ParameterValue kb="CUR" kxe="false">draft-uuid</ParameterValue>' in body
    )
    assert "K_X_API_SESSION_MEMENTO" in body
    assert set(result) == {"job", "ownership_stamped", "warnings"}
    assert result["job"]["Resource"]["JobID"] == JOB_ID
    assert result["ownership_stamped"] is None
    assert result["warnings"] == [
        (
            "ownership stamp not attempted: template deployment does not identify and stamp "
            "the new LPAR; list partitions to identify it, then set its description"
        )
    ]


def test_deploy_partition_template_resolves_target_system_name(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.put("/rest/api/templates/PartitionTemplate/draft-uuid/do/deploy").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    resolver = AsyncMock(return_value=TARGET_SYSTEM_UUID)

    with patch("hmcpctl.operations.templates.core.resolve_system_uuid", new=resolver):
        hmc_deploy_partition_template("draft-uuid", "system-prod")

    resolver.assert_awaited_once_with(ANY, "system-prod")


# ---------------------------------------------------------------------- #
# wait=True path: deploy blocks until job reaches terminal state
# ---------------------------------------------------------------------- #

# A finished job reads COMPLETED_OK: IBM documents a template deploy job ending
# Completed_OK (docs/refs/hmc-rest-api-p10/template-library/196-template-rest-job-api.md:541),
# the documented job statuses have no bare COMPLETED
# (docs/refs/hmc-rest-api-p10/016-job-status.md:16-27), and V10R3 answers so (#1161).


def test_deploy_partition_template_wait_true_polls_to_completion(monkeypatch, mock_hmc):
    """hmc_deploy_partition_template(wait=True) submits then polls until COMPLETED_OK."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{TARGET_SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(200, text=_lpar_feed()))
    # An empty partition feed is trusted only from an operating system (#1301).
    mock_system_summaries(mock_hmc, (TARGET_SYSTEM_UUID, "sys-R1", "operating"))
    submit_route = mock_hmc.put(
        "/rest/api/templates/PartitionTemplate/draft-uuid/do/deploy"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    poll_route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    result = hmc_deploy_partition_template(
        "draft-uuid", TARGET_SYSTEM_UUID, wait=True, timeout_seconds=60, poll_interval=1
    )
    assert submit_route.called
    assert poll_route.called
    assert set(result) == {"job", "ownership_stamped", "warnings"}
    assert result["job"]["Resource"]["Status"] == "COMPLETED_OK"
    assert result["ownership_stamped"] is None


def test_deploy_partition_template_wait_true_polls_template_job_link(
    monkeypatch, mock_hmc
):
    """hmc_deploy_partition_template polls /rest/api/templates/jobs/ with atom+xml (#110)."""
    _hmc_env(monkeypatch)
    template_job_submit = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:submit-job-1</id>
  <title>JobResponse</title>
  <link rel="SELF" href="https://hmc.test/rest/api/templates/jobs/{JOB_ID}"/>
  <content type="application/vnd.ibm.powervm.web+xml; type=JobResponse">
    <JobResponse:JobResponse xmlns:JobResponse="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/">
      <JobID>{JOB_ID}</JobID>
      <Status>NOT_STARTED</Status>
    </JobResponse:JobResponse>
  </content>
</entry>
"""
    template_job_completed = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:completed-job-1</id>
  <title>JobResponse</title>
  <link rel="SELF" href="https://hmc.test/rest/api/templates/jobs/{JOB_ID}"/>
  <content type="application/vnd.ibm.powervm.web+xml; type=JobResponse">
    <JobResponse:JobResponse xmlns:JobResponse="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/">
      <JobID>{JOB_ID}</JobID>
      <Status>COMPLETED_OK</Status>
    </JobResponse:JobResponse>
  </content>
</entry>
"""
    mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{TARGET_SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(200, text=_lpar_feed()))
    mock_system_summaries(mock_hmc, (TARGET_SYSTEM_UUID, "sys-R1", "operating"))
    submit_route = mock_hmc.put(
        "/rest/api/templates/PartitionTemplate/draft-uuid/do/deploy"
    ).mock(return_value=httpx.Response(202, text=template_job_submit))
    poll_route = mock_hmc.get(f"/rest/api/templates/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=template_job_completed)
    )
    result = hmc_deploy_partition_template(
        "draft-uuid", TARGET_SYSTEM_UUID, wait=True, timeout_seconds=60, poll_interval=1
    )
    assert submit_route.called
    assert poll_route.called
    assert poll_route.calls.last.request.headers.get("Accept") == "application/atom+xml"
    assert set(result) == {"job", "ownership_stamped", "warnings"}
    assert result["job"]["Resource"]["Status"] == "COMPLETED_OK"


def _lpar_feed(*entries: tuple[str, str]) -> str:
    body = "".join(
        f"""
  <entry>
    <id>urn:uuid:{uuid}</id>
    <title>LogicalPartition:{name}</title>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>{name}</PartitionName>
      </LogicalPartition>
    </content>
  </entry>"""
        for uuid, name in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">{body}
</feed>
"""


def test_deploy_partition_template_completed_stamps_the_new_lpar(monkeypatch, mock_hmc):
    """wait=True stamps the sole UUID added during a completed deployment."""
    _hmc_env(monkeypatch)
    old = ("old-uuid", "old-lpar")
    created = ("new-uuid", "new-lpar")
    mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{TARGET_SYSTEM_UUID}/LogicalPartition"
    ).mock(
        side_effect=[
            httpx.Response(200, text=_lpar_feed(old)),
            httpx.Response(200, text=_lpar_feed(old, created)),
        ]
    )
    mock_hmc.put("/rest/api/templates/PartitionTemplate/draft-uuid/do/deploy").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    stamp = AsyncMock(return_value=(True, []))
    with patch(
        "hmcpctl.operations.templates.core.stamp_created_lpar_ownership", new=stamp
    ):
        result = hmc_deploy_partition_template(
            "draft-uuid",
            TARGET_SYSTEM_UUID,
            wait=True,
            timeout_seconds=60,
            poll_interval=1,
        )

    assert set(result) == {"job", "ownership_stamped", "warnings"}
    assert result["ownership_stamped"] is True
    assert result["warnings"] == []
    assert stamp.await_args.args[3]["Resource"]["PartitionName"] == "new-lpar"
