"""HMCClient systems mixin.

The full client is assembled in :mod:`hmcpctl.client` by inheriting every
domain mixin; this module only defines methods for systems.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import HMCError
from ..jobs import (
    power_off_system_job,
    power_off_vios_job,
    power_on_system_job,
    power_on_vios_job,
)
from .client_contracts import SystemsClient, _reject_non_uuid_path_argument
from .client_parse import _parse_feed
from .client_resolution import (
    ambiguity_candidate_ids,
    ambiguous_parent_details,
    require_absence_provable,
    require_operating_system,
    summary_for,
)

_logger = logging.getLogger(__name__)


class SystemsMixin:
    # -- Convenience wrappers for the common resources ----------------- #
    async def get_console_info(self: SystemsClient) -> dict[str, Any] | None:
        """ManagementConsole: HMC version, network info, links to systems."""
        # The unfiltered ManagementConsole feed has returned HTTP 500 over a
        # null nested property (observed: Session/SessionId/Value, #880).
        # The same 500 on the ManagedSystem feed is reproduced by the
        # X-HMC-Schema-Version: V1_0 pin alone (ADR 0138); that this console
        # 500 shares the cause is likely but unverified. Translate the known
        # response into an actionable error rather than making it
        # indistinguishable from an empty feed. The marker is the
        # live-confirmed text the two managed-system guards below also match
        # (ADR 0138), but it is tested against the body rather than their
        # str(exc): the rendered detail carries only the first
        # Message/msg/error element of an XML body, or its first 500
        # characters when the body is not XML (errors.py), while the body is
        # the response text as received, truncated to MAX_ERROR_BODY_BYTES --
        # a wider window either way. No raw body for this endpoint has been
        # captured, and no observed body needs the wider window: every error
        # recorded for this defect class puts the marker in the first 500
        # characters. The wider surface is here so that whatever shape a live
        # capture shows, the guard already covers it; the XML case its test
        # carries is hypothetical. The message names no particular property
        # because the marker does not establish one; the HMC's own detail
        # still reaches the operator through the body.
        try:
            entries = await self.list_uom("ManagementConsole")
            return entries[0] if entries else None
        except HMCError as exc:
            if exc.status_code == 500 and (
                "Nested path contains null property" in (exc.body or "")
            ):
                raise HMCError(
                    "Management-console inventory is unavailable because the HMC "
                    "could not serialize a null property; the likely cause is an "
                    "X-HMC-Schema-Version pin (HMC_SCHEMA_VERSION): unset "
                    "HMC_SCHEMA_VERSION and any profile schema_version, and retry",
                    status_code=500,
                    body=exc.body,
                ) from exc
            raise

    async def list_system_summaries(self: SystemsClient) -> list[Any]:
        """Every managed system's summary from GET .../ManagedSystem/quick/All.

        Not documented in this repo's vendored HMC REST API reference (only
        the per-UUID quick/{Property} form is); evidenced by IBM's public
        project-pim repository (ADR 0138), and captured on V10R3 as a JSON
        array of objects keyed ``UUID`` (lower case), ``SystemName``,
        ``State`` and ``StateDetail``. No typed Accept header, matching
        get_quick_property's precedent (core.py) that a uom+xml header 406s
        on quick/ endpoints, and project-pim's own quick/All calls, which
        send none either. It answers while a system is ``No Connection``,
        where the full ManagedSystem feed times out (#1309). Items are
        returned unvalidated; each caller decides what an unreadable one means.
        """
        resp = await self._request(
            "GET",
            "/rest/api/uom/ManagedSystem/quick/All",
            headers={"Accept": "*/*"},
        )
        if resp.status_code != 200:
            raise HMCError(
                "GET /rest/api/uom/ManagedSystem/quick/All failed",
                resp.status_code,
                resp.text,
            )
        try:
            summaries = resp.json()
        except ValueError as exc:
            raise HMCError(
                "GET /rest/api/uom/ManagedSystem/quick/All returned invalid "
                f"JSON: {str(exc)[:500]}"
            ) from exc
        except RecursionError as exc:
            # json.loads recurses per nesting level; RecursionError carries
            # no message, hence the fixed clause.
            raise HMCError(
                "GET /rest/api/uom/ManagedSystem/quick/All returned invalid "
                "JSON: document nesting is too deep"
            ) from exc
        if not isinstance(summaries, list):
            raise HMCError(
                "GET /rest/api/uom/ManagedSystem/quick/All returned a JSON "
                f"{type(summaries).__name__}; expected an array"
            )
        return summaries

    async def _quick_all_system_names(self: SystemsClient) -> dict[str, str]:
        """UUID -> SystemName map from quick/All, keyed by the lower-cased UUID.

        Used only as a fallback when a direct entry read trips the
        null-property serialization bug, so entries missing UUID or
        SystemName are skipped rather than raised.
        """
        return {
            entry["UUID"].lower(): entry["SystemName"]
            for entry in await self.list_system_summaries()
            if isinstance(entry, dict)
            and isinstance(entry.get("UUID"), str)
            and "SystemName" in entry
        }

    async def get_managed_system(
        self: SystemsClient, uuid: str
    ) -> dict[str, Any] | None:
        # A direct UUID fetch can 500 over a null hardware property; the
        # observed trigger is an X-HMC-Schema-Version: V1_0 pin (ADR 0138).
        # quick/All supplies the missing name so find_system_by_name (a
        # different, working path) can resolve it.
        try:
            return await self.get_uom("ManagedSystem", uuid)
        except HMCError as exc:
            if not (
                exc.status_code == 500
                and "Nested path contains null property" in str(exc)
            ):
                raise
            entry = None
            fallback_exc: Exception | None = None
            try:
                names = await self._quick_all_system_names()
                name = names.get(uuid.lower())
                if name:
                    entry = await self.find_system_by_name(name)
            except (HMCError, ValueError) as fb_exc:
                fallback_exc = fb_exc
            if entry is None:
                raise HMCError(
                    f"Managed system {uuid} is unavailable because the HMC "
                    "could not serialize a null hardware property, and it "
                    "could not be resolved from the managed-system summary; "
                    "the likely cause is an X-HMC-Schema-Version pin "
                    "(HMC_SCHEMA_VERSION): unset HMC_SCHEMA_VERSION and any "
                    "profile schema_version, and retry, or query the system by "
                    "name with systems show",
                    status_code=500,
                    body=exc.body,
                ) from (fallback_exc or exc)
            return entry

    async def find_system_by_name(
        self: SystemsClient, name: str
    ) -> dict[str, Any] | None:
        """Find a managed system by its SystemName (exact match)."""
        results = await self.search_uom("ManagedSystem", "SystemName", name)
        if len(results) > 1:
            ambiguity_candidate_ids(results, "managed-system", name)
            details = ", ".join(
                f"{(entry.get('Resource') or {}).get('SystemName')!r} ({entry.get('UUID')})"
                for entry in sorted(results, key=lambda item: str(item.get("UUID")))
            )
            raise ValueError(f"Ambiguous managed-system name {name!r}: {details}")
        return results[0] if results else None

    async def modify_managed_system(
        self: SystemsClient, system_uuid: str, system_xml: str
    ) -> dict[str, Any] | None:
        """Modify a managed system's properties (POST a partial ManagedSystem doc).

        Supported fields include system name, power-off policy, LPAR start
        policy, pending memory region size, huge pages, and mirroring mode.
        See documents.system.build_managed_system_document for the document builder.
        """
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        path = f"/rest/api/uom/ManagedSystem/{system_uuid}"
        xml = await self._post(path, system_xml, resource_type="ManagedSystem")
        entries = _parse_feed(xml, path) if xml else []
        return entries[0] if entries else None

    # Managed-system / VIOS power jobs
    async def power_on_system(
        self: SystemsClient, system_uuid: str
    ) -> dict[str, Any] | None:
        """Power on a managed system (PowerOn job)."""

        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        return await self.submit_job(
            f"/rest/api/uom/ManagedSystem/{system_uuid}/do/PowerOn",
            power_on_system_job(),
        )

    async def power_off_system(
        self: SystemsClient, system_uuid: str, immediate: bool = False
    ) -> dict[str, Any] | None:
        """Power off a managed system (PowerOff job; immediate skips graceful shutdown)."""

        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        return await self.submit_job(
            f"/rest/api/uom/ManagedSystem/{system_uuid}/do/PowerOff",
            power_off_system_job(immediate),
        )

    async def find_vios_by_name(
        self: SystemsClient, name: str, system_uuid: str | None = None
    ) -> dict[str, Any] | None:
        """Find a Virtual I/O Server by its PartitionName (exact match)."""
        if system_uuid:
            entries = await self.list_vios(system_uuid)
            results = [
                entry
                for entry in entries
                if (entry.get("Resource") or {}).get("PartitionName") == name
            ]
            if len(results) > 1:
                ambiguity_candidate_ids(results, "VIOS", name)
                system_name = summary_for(
                    await self.list_system_summaries(), system_uuid
                ).get("SystemName")
                if not isinstance(system_name, str) or not system_name:
                    raise ValueError(
                        f"Cannot resolve ambiguous VIOS name {name!r}: cannot "
                        f"identify managed system {system_uuid}"
                    )
                details = ", ".join(
                    f"{entry.get('UUID')} on {system_name!r} ({system_uuid})"
                    for entry in sorted(results, key=lambda item: str(item.get("UUID")))
                )
                raise ValueError(f"Ambiguous VIOS name {name!r}: {details}")
            return results[0] if results else None

        results = await self.search_uom("VirtualIOServer", "PartitionName", name)
        if not results:
            await require_absence_provable(self.list_system_summaries, "VIOS", name)
            return None
        if len(results) == 1:
            return results[0]

        details = await ambiguous_parent_details(
            results,
            await self.list_system_summaries(),
            "VIOS",
            name,
            self.list_vios,
        )
        raise ValueError(f"Ambiguous VIOS name {name!r}: {details}")

    async def power_on_vios(
        self: SystemsClient, vios_uuid: str
    ) -> dict[str, Any] | None:
        """Power on a VIOS (PowerOn job)."""

        _reject_non_uuid_path_argument("vios_uuid", vios_uuid)
        return await self.submit_job(
            f"/rest/api/uom/VirtualIOServer/{vios_uuid}/do/PowerOn", power_on_vios_job()
        )

    async def power_off_vios(
        self: SystemsClient, vios_uuid: str, immediate: bool = False
    ) -> dict[str, Any] | None:
        """Power off a VIOS (PowerOff job; immediate skips graceful shutdown)."""

        _reject_non_uuid_path_argument("vios_uuid", vios_uuid)
        return await self.submit_job(
            f"/rest/api/uom/VirtualIOServer/{vios_uuid}/do/PowerOff",
            power_off_vios_job(immediate),
        )

    async def list_vios(self: SystemsClient, system_uuid: str) -> list[dict[str, Any]]:
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        path = f"/rest/api/uom/ManagedSystem/{system_uuid}/VirtualIOServer"
        xml = await self._get(path, "VirtualIOServer")
        entries = _parse_feed(xml, path) if xml else []
        if not entries:
            await require_operating_system(
                self.list_system_summaries, system_uuid, "VIOSes"
            )
        return entries

    async def get_vios(self: SystemsClient, vios_uuid: str) -> dict[str, Any] | None:
        """GET one VIOS entry.

        A VIOS answers only under ``VirtualIOServer``: V10R3 returns 404 for
        ``LogicalPartition/{vios_uuid}`` and its quick properties (#1202).
        """
        _reject_non_uuid_path_argument("vios_uuid", vios_uuid)
        return await self.get_uom("VirtualIOServer", vios_uuid)

    async def create_vios(
        self: SystemsClient, system_uuid: str, vios_xml: str
    ) -> dict[str, Any] | None:
        """Create a VIOS on a managed system and return its entry.

        PUTs a VirtualIOServer document (see documents.lpar.build_vios_document) to
        the system's ``VirtualIOServer`` collection: V10R3 answers a VIOS PUT to
        ``LogicalPartition`` with 500 REST0140 (#1214). Omits the
        X-HMC-Schema-Version header; the write headers follow ADR 0178.
        """
        _reject_non_uuid_path_argument("system_uuid", system_uuid)
        path = f"/rest/api/uom/ManagedSystem/{system_uuid}/VirtualIOServer"
        xml = await self._put(
            path,
            vios_xml,
            resource_type="VirtualIOServer",
            include_schema_version=False,
        )
        entries = _parse_feed(xml, path) if xml else []
        return entries[0] if entries else None

    async def delete_vios(self: SystemsClient, vios_uuid: str) -> None:
        """Delete a VIOS partition. It must be powered off first."""
        _reject_non_uuid_path_argument("vios_uuid", vios_uuid)
        await self._delete(f"/rest/api/uom/VirtualIOServer/{vios_uuid}")

    async def get_vios_storage_detail(
        self: SystemsClient, vios_uuid: str
    ) -> dict[str, Any] | None:
        """GET VirtualIOServer device mappings.

        Requests the documented ViosSCSIMapping and ViosFCMapping groups and
        returns the parsed entry with both mapping collections populated. The
        groups go in one comma-separated value: V10R3 answers a repeated
        ``group`` parameter with the first group only (#1202).
        """
        _reject_non_uuid_path_argument("vios_uuid", vios_uuid)
        path = (
            f"/rest/api/uom/VirtualIOServer/{vios_uuid}"
            "?group=ViosSCSIMapping,ViosFCMapping"
        )
        xml = await self._get(path, "VirtualIOServer")
        if not xml:
            return None
        entries = _parse_feed(xml, path)
        return entries[0] if entries else None
