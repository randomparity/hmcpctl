"""HMCClient templates mixin.

The full client is assembled in :mod:`hmcpctl.client` by inheriting every
domain mixin; this module only defines methods for templates.
"""

from __future__ import annotations

from typing import Any

from ..errors import HMCError
from ..jobs import deploy_partition_template_job
from .client_contracts import TemplatesClient
from .client_parse import _parse_feed


class TemplatesMixin:
    # Template Library (/rest/api/templates/). V10R3 answers the typed
    # `templates+xml; type=PartitionTemplate` Accept with HTTP 406 and serves the
    # library feed for `application/atom+xml` (#1202).
    TEMPLATES_ACCEPT = "application/atom+xml"

    async def _templates_get(self: TemplatesClient, path: str) -> str:
        resp = await self._request(
            "GET", path, headers={"Accept": self.TEMPLATES_ACCEPT}
        )
        if resp.status_code == 204:
            return ""
        if resp.status_code != 200:
            raise HMCError(f"GET {path} failed", resp.status_code, resp.text)
        return resp.text

    async def list_partition_templates(self: TemplatesClient) -> list[dict[str, Any]]:
        """List all partition templates in the template library."""
        xml = await self._templates_get("/rest/api/templates/PartitionTemplate")
        return _parse_feed(xml, "/rest/api/templates/PartitionTemplate") if xml else []

    async def get_partition_template(
        self: TemplatesClient, template_uuid: str
    ) -> dict[str, Any] | None:
        """Get one partition template by UUID.

        Returns None for an empty HTTP 200 body, HTTP 204, or no parsed entries.
        HTTP 404 and every other status outside 200/204 raise HMCError.
        """
        xml = await self._templates_get(
            f"/rest/api/templates/PartitionTemplate/{template_uuid}"
        )
        if not xml:
            return None
        entries = _parse_feed(
            xml, f"/rest/api/templates/PartitionTemplate/{template_uuid}"
        )
        return entries[0] if entries else None

    async def deploy_partition_template(
        self: TemplatesClient, draft_template_uuid: str, target_system_uuid: str
    ) -> dict[str, Any] | None:
        """Deploy a partition from a *draft* template onto a managed system.

        draft_template_uuid is the transformed/replica template UUID (produced
        by capture/transform); target_system_uuid is the managed system to
        create the partition on. Submits the Deploy job and returns the Job.
        """

        memento = self._session_token or ""
        xml = deploy_partition_template_job(
            draft_template_uuid, target_system_uuid, memento
        )
        return await self.submit_job(
            f"/rest/api/templates/PartitionTemplate/{draft_template_uuid}/do/deploy",
            xml,
        )
