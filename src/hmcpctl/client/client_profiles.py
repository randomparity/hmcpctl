"""Contained native saved-profile reads and fixed-route writes (ADR 0242)."""

from __future__ import annotations

import xml.etree.ElementTree as ET  # nosec B405 - serializes defusedxml-parsed content
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from defusedxml import ElementTree as DET

from ..documents.partition_profile import (
    ATOM,
    UOM,
    N,
    compare_profiles,
    one,
    parse_profile,
)
from ..errors import HMCError
from .client_contracts import _reject_non_uuid_path_argument
from .client_parse import _parse_feed
from .core import HMCClient

PartitionKind = Literal["LogicalPartition", "VirtualIOServer"]


@dataclass(frozen=True)
class NativeProfile:
    uuid: str
    name: str
    setting_id: str
    etag: str | None
    element: ET.Element
    raw: str
    resource: dict[str, Any]


def profile_path(kind: PartitionKind, parent: str, uuid: str | None = None) -> str:
    if kind not in {"LogicalPartition", "VirtualIOServer"}:
        raise ValueError("partition_type must be LogicalPartition or VirtualIOServer")
    _reject_non_uuid_path_argument("partition_uuid", parent)
    path = f"/rest/api/uom/{kind}/{parent}/LogicalPartitionProfile"
    if uuid is not None:
        _reject_non_uuid_path_argument("profile_uuid", uuid)
        path += "/" + uuid
    return path


def _validate_parent(
    hmc: HMCClient, element: ET.Element, kind: PartitionKind, parent: str, system: str
) -> None:
    association = one(element, "AssociatedPartition")
    links = list(association)
    if links:
        if len(links) != 1 or links[0].tag != N + "link":
            raise ValueError("profile partition association is malformed")
        href = links[0].get("href")
        if association.get("href") not in {None, href}:
            raise ValueError("conflicting profile partition associations")
    else:
        href = association.get("href")
    if not href or not href.isprintable() or any(c.isspace() for c in href):
        raise ValueError("profile parent association is unavailable or malformed")
    parsed = urlsplit(href)
    if (
        parsed.scheme != "https"
        or parsed.hostname != hmc.config.host
        or (parsed.port or 443) != hmc.config.port
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
        not in {
            f"/rest/api/uom/{kind}/{parent}",
            f"/rest/api/uom/ManagedSystem/{system}/{kind}/{parent}",
        }
    ):
        raise ValueError("profile belongs to a different partition or HMC")


async def read_native_profile(
    hmc: HMCClient, kind: PartitionKind, parent: str, uuid: str, system: str
) -> NativeProfile:
    path = profile_path(kind, parent, uuid)
    response = await hmc._request_with_uuid_path_arguments(
        "GET",
        path,
        uuid_path_arguments={"partition_uuid": parent, "profile_uuid": uuid},
        headers={"Accept": "*/*"},
    )
    if response.status_code != 200:
        raise HMCError("contained profile GET failed", response.status_code)
    element = parse_profile(response.text)
    entries = _parse_feed(response.text, path)
    if len(entries) != 1 or entries[0].get("UUID") != uuid:
        raise ValueError(
            "profile readback identity differs from the contained selector"
        )
    _validate_parent(hmc, element, kind, parent, system)
    name = one(element, "ProfileName")
    setting = one(element, "SettingID")
    name_text, setting_text = name.text or "", setting.text or ""
    if len(name) or len(setting) or not name_text.strip() or not setting_text.strip():
        raise ValueError("profile name or SettingID is unavailable/malformed")
    for key in ("AtomID", "AtomCreated"):
        field = one(element, "Metadata/Atom/" + key)
        if len(field) or not field.text:
            raise ValueError("profile root identity scalar is unavailable/malformed")
    return NativeProfile(
        uuid,
        name_text,
        setting_text,
        response.headers.get("ETag"),
        element,
        response.text,
        entries[0].get("Resource") or {},
    )


def require_fresh_profile(before: NativeProfile, after: NativeProfile) -> None:
    if not before.etag or before.etag != after.etag or before.uuid != after.uuid:
        raise ValueError("profile changed or has no ETag; refresh before retrying")
    left, right = DET.fromstring(before.raw), DET.fromstring(after.raw)
    # Only the direct outer entry publication time was observed to change on GET.
    for root in (left, right):
        if root.tag == "{" + ATOM + "}entry":
            publication = root.findall("{" + ATOM + "}published")
            if len(publication) > 1:
                raise ValueError("outer profile publication is ambiguous")
            for field in publication:
                if len(field):
                    raise ValueError("outer profile publication scalar is malformed")
                field.text = "<outer-publication>"
    comparison = compare_profiles(left, right)
    if not comparison.equal or not comparison.complete:
        raise ValueError("profile content/identity changed; refresh before retrying")


async def write_native_profile(
    hmc: HMCClient,
    kind: PartitionKind,
    parent: str,
    element: ET.Element,
    before: NativeProfile | None = None,
) -> int:
    path = profile_path(kind, parent, before.uuid if before else None)
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/vnd.ibm.powervm.uom+xml; type=LogicalPartitionProfile",
    }
    if before:
        if not before.etag:
            raise ValueError("profile update requires an opaque ETag")
        headers["If-Match"] = before.etag
    ET.register_namespace("", UOM)
    ET.register_namespace("atom", ATOM)
    response = await hmc._request_with_uuid_path_arguments(
        "POST" if before else "PUT",
        path,
        uuid_path_arguments={
            "partition_uuid": parent,
            **({"profile_uuid": before.uuid} if before else {}),
        },
        content=ET.tostring(element, encoding="unicode"),
        headers=headers,
    )
    if response.status_code not in {200, 201, 202, 204}:
        raise HMCError(
            "native profile write failed; reconcile before retrying",
            response.status_code,
        )

    return response.status_code
