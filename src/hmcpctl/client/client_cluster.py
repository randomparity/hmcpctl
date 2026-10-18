"""HMCClient cluster mixin.

The full client is assembled in :mod:`hmcpctl.client` by inheriting every
domain mixin; this module only defines methods for cluster.
"""

from __future__ import annotations

from typing import Any

from ..jobs import DeviceType, LuType, create_logical_unit_job, delete_logical_unit_job
from .client_contracts import ClusterClient, _reject_non_uuid_path_argument


class ClusterMixin:
    # Cluster / Shared Storage Pool (SSP)
    async def list_clusters(self: ClusterClient) -> list[dict[str, Any]]:
        return await self.list_uom("Cluster")

    async def get_cluster(
        self: ClusterClient, cluster_uuid: str
    ) -> dict[str, Any] | None:
        """Get one Cluster using the ``get_uom`` empty-success/error contract.

        None means an empty successful response or no parsed entries; HTTP 404
        and other statuses outside 200/204 raise HMCError.
        """
        return await self.get_uom("Cluster", cluster_uuid)

    async def list_shared_storage_pools(self: ClusterClient) -> list[dict[str, Any]]:
        return await self.list_uom("SharedStoragePool")

    async def get_shared_storage_pool(
        self: ClusterClient, ssp_uuid: str
    ) -> dict[str, Any] | None:
        """Get one SharedStoragePool using the ``get_uom`` empty-success/error contract.

        None means an empty successful response or no parsed entries; HTTP 404
        and other statuses outside 200/204 raise HMCError.
        """
        return await self.get_uom("SharedStoragePool", ssp_uuid)

    async def create_logical_unit(
        self: ClusterClient,
        cluster_uuid: str,
        lu_name: str,
        lu_size_gib: int,
        *,
        lu_type: LuType = "THIN",
        device_type: DeviceType = "VirtualIO_Disk",
        cloned_from: str | None = None,
    ) -> dict[str, Any] | None:
        """Submit a CreateLogicalUnit job against a Cluster/SSP.

        Returns the Job resource (poll get_job_entry for status; the job result
        contains the new LU's UDID in LUCreated).
        """

        _reject_non_uuid_path_argument("cluster_uuid", cluster_uuid)
        job_xml = create_logical_unit_job(
            lu_name, lu_size_gib, lu_type, device_type, cloned_from
        )
        return await self.submit_job(
            f"/rest/api/uom/Cluster/{cluster_uuid}/do/CreateLogicalUnit", job_xml
        )

    async def delete_logical_unit(
        self: ClusterClient, cluster_uuid: str, lu_udid: str
    ) -> dict[str, Any] | None:
        """Submit a DeleteLogicalUnit job against a Cluster/SSP."""

        _reject_non_uuid_path_argument("cluster_uuid", cluster_uuid)
        job_xml = delete_logical_unit_job(lu_udid)
        return await self.submit_job(
            f"/rest/api/uom/Cluster/{cluster_uuid}/do/DeleteLogicalUnit", job_xml
        )
