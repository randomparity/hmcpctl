"""Static contracts between domain mixins and the composed HMC client."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol, get_args

# Element is a type contract only; client implementations parse inbound XML
# through defusedxml.
from xml.etree.ElementTree import Element  # nosec B405

import httpx

from ..config import HMCConfig
from ..errors import HMCError
from ..resource_identity import is_uuid

AuthenticationFilter = Literal["local", "ldap", "kerberos", "all"]
VALID_AUTHENTICATION_FILTERS = frozenset(get_args(AuthenticationFilter))

AdapterType = Literal[
    "ClientNetworkAdapter",
    "VirtualSCSIClientAdapter",
    "VirtualFibreChannelClientAdapter",
    "VirtualNICDedicated",
]
ADAPTER_TYPES = frozenset(get_args(AdapterType))


def _reject_non_uuid_path_argument(argument: str, value: str) -> None:
    """Refuse a path identity outside the canonical UUID shape."""
    if not is_uuid(value):
        raise ValueError(f"{argument} must be a UUID")


# The HMC's own type-name grammar. Every `/rest/api/uom/` type segment in the
# vendored V10 and V11 corpora, and every type name this client passes, matches
# it. Deliberately an allowlist: a denylist over a URL path segment has to
# discover `?`, `#`, `%`, `;`, `@`, `:`, and CRLF one incident at a time, while
# the type namespace is closed and documented (ADR 0143).
#
# Unanchored, because it is used with `fullmatch`. An `^...$` pattern with
# `.match` would accept "LogicalPartition\n" -- Python's `$` matches before a
# trailing newline -- which httpx puts straight into the Accept header.
_UOM_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9]*")

# The grammar bounds the character set; without this it bounds nothing else, so
# a megabyte of "A" is grammar-valid and gets built into a URL and an Accept
# header (ADR 0147). An *acceptance* bound, which is why it is not
# `_MAX_REPORTED_NAME_LENGTH`: that one truncates a name for display, where
# being wrong costs a shortened message, and this one refuses an operation.
# Too low is the expensive direction -- ADR 0143 accepts that no authoritative
# list of HMC type names exists -- so this is four times the longest type name
# in this repository (32, `VirtualFibreChannelClientAdapter`) rather than a
# tight fit. Against a megabyte every candidate performs the same.
_MAX_UOM_TYPE_LENGTH = 128


def _reject_unknown_uom_type(argument: str, value: str) -> None:
    """Refuse a type segment outside the HMC's own type-name grammar.

    Raised as ``ValueError`` because it reports a malformed caller argument,
    not a path this client declines to send -- the same family as
    ``_request_with_uuid_path_arguments``' UUID check and
    ``validate_adapter_type`` (ADR 0143). The message names the argument and the
    first offending character or the length only, never the whole value, which
    on the CLI and API paths can carry an operator's own strings.

    Length is checked before the character class: an over-long value that is
    otherwise grammar-valid has no offending character to name, so the
    character-class branch would report the first character and describe a rule
    the value did not break.
    """
    if len(value) > _MAX_UOM_TYPE_LENGTH:
        detail = (
            f"a value of {len(value)} characters, over the "
            f"{_MAX_UOM_TYPE_LENGTH}-character maximum"
        )
    elif _UOM_TYPE.fullmatch(value):
        return
    elif not value:
        detail = "an empty value"
    elif not (value[0].isascii() and value[0].isalpha()):
        detail = f"a value starting with {value[0]!r}"
    else:
        offending = next(
            (c for c in value if not (c.isascii() and c.isalnum())), value[0]
        )
        detail = f"a value containing {offending!r}"
    raise ValueError(
        f"{argument} must be an HMC resource type name: ASCII letters and "
        f"digits only, starting with a letter, at most {_MAX_UOM_TYPE_LENGTH} "
        f"characters. Got {detail}."
    )


# A caller-supplied value percent-encoded into a uom path segment. Encoding
# makes a value safe without making it small: `quote` emits three characters per
# UTF-8 byte and a character can be four bytes, so one accepted character
# becomes up to twelve on the wire. Derived from a wire budget rather than from
# the values observed here -- 3 KiB, under half of nginx's documented 8k
# request-line buffer, divided by that twelvefold worst case (ADR 0150).
#
# Deliberately not `_MAX_UOM_TYPE_LENGTH`: that bound is four times the longest
# name in a closed vendored namespace and is partly justified by the `Accept`
# header, which neither of these values reaches.
_MAX_UOM_PATH_VALUE_LENGTH = 256


def _reject_over_long_path_value(argument: str, value: str) -> None:
    """Refuse a caller-supplied uom path value longer than the bound.

    ``ValueError`` because it reports a malformed caller argument, the family
    ``_reject_unknown_uom_type`` and ``validate_adapter_type`` belong to
    (ADR 0143). ADR 0148 draws the line: a per-argument validator called where
    the segment is built raises ``ValueError``, while the guard running *at* the
    request waist raises ``HMCError`` because it holds a path it did not build.

    The message names the argument and both lengths, never the value, which on
    the CLI and API paths carries an operator's own resource names.
    """
    if len(value) > _MAX_UOM_PATH_VALUE_LENGTH:
        raise ValueError(
            f"{argument} is {len(value)} characters; "
            f"maximum is {_MAX_UOM_PATH_VALUE_LENGTH}"
        )


def validate_adapter_type(adapter_type: AdapterType) -> AdapterType:
    if adapter_type not in ADAPTER_TYPES:
        raise ValueError(
            f"Invalid adapter_type {adapter_type!r}. "
            f"Must be one of: {', '.join(sorted(ADAPTER_TYPES))}"
        )
    return adapter_type


class LparsClient(Protocol):
    """Host operations required by :class:`client_lpars.LparsMixin`."""

    async def _request_with_uuid_path_arguments(
        self,
        method: str,
        path: str,
        *,
        uuid_path_arguments: Mapping[str, str],
        **kwargs: Any,
    ) -> httpx.Response: ...

    def _uom_headers(
        self, resource_type: str | None, include_schema_version: bool = True
    ) -> dict[str, str]: ...

    async def _get(
        self,
        path: str,
        resource_type: str | None = None,
        include_schema_version: bool = True,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> str: ...

    async def _post(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> str: ...

    async def _put(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> str: ...

    async def _delete(
        self,
        path: str,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> None: ...

    async def list_logical_partitions(
        self, system_uuid: str
    ) -> list[dict[str, Any]]: ...

    async def list_uom(
        self, resource_type: str, group: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get_uom(
        self, resource_type: str, uuid: str, group: str | None = None
    ) -> dict[str, Any] | None: ...

    async def search_uom(
        self, resource_type: str, property_name: str, property_value: str
    ) -> list[dict[str, Any]]: ...

    async def list_system_summaries(self) -> list[Any]: ...

    async def get_managed_system(self, uuid: str) -> dict[str, Any] | None: ...

    async def read_logical_partition_document(
        self, lpar_uuid: str, *, require_etag: bool = False
    ) -> tuple[Element, str | None]: ...

    async def validate_logical_partition_update(
        self,
        lpar_uuid: str,
        updates: Callable[[Element], Mapping[str, str | None]],
        subject: str,
    ) -> None: ...

    async def update_logical_partition(
        self,
        lpar_uuid: str,
        updates: Callable[[Element], Mapping[str, str | None]],
        subject: str,
    ) -> dict[str, Any] | None: ...


class PcmClient(Protocol):
    """Host state and operations required by :class:`client_pcm.PcmMixin`."""

    config: HMCConfig
    _http: httpx.AsyncClient
    _rest_base_url: str

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response: ...

    async def _get(
        self,
        path: str,
        resource_type: str | None = None,
        include_schema_version: bool = True,
    ) -> str: ...

    async def raw_get(
        self, path: str, accept: str = "*/*"
    ) -> tuple[str, dict[str, str]]: ...

    async def _post_pcm(self, path: str, body: str) -> str: ...

    async def _metrics_links(
        self,
        category: str,
        resource_uuid: str,
        kind: str,
        start_ts: str,
        end_ts: str | None,
        no_of_samples: int | None,
        *,
        system_uuid: str | None = None,
    ) -> list[dict[str, str]]: ...

    async def get_metrics_feed(self, path: str) -> list[dict[str, str]]: ...


class UpdatesClient(Protocol):
    """Host operations required by :class:`client_updates.UpdatesMixin`."""

    async def _request_with_uuid_path_arguments(
        self,
        method: str,
        path: str,
        *,
        uuid_path_arguments: Mapping[str, str],
        **kwargs: Any,
    ) -> httpx.Response: ...


class StorageClient(Protocol):
    """Host state and operations required by :class:`client_storage.StorageMixin`."""

    config: HMCConfig
    _rest_base_url: str

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response: ...

    async def _request_with_uuid_path_arguments(
        self,
        method: str,
        path: str,
        *,
        uuid_path_arguments: Mapping[str, str],
        **kwargs: Any,
    ) -> httpx.Response: ...

    def _uom_headers(
        self, resource_type: str | None, include_schema_version: bool = True
    ) -> dict[str, str]: ...

    def _web_headers(self, extra: dict[str, str]) -> dict[str, str]: ...

    async def _get(
        self,
        path: str,
        resource_type: str | None = None,
        include_schema_version: bool = True,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> str: ...

    async def _post(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> str: ...

    async def _put(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> str: ...

    async def _delete(
        self,
        path: str,
        *,
        uuid_path_arguments: Mapping[str, str] | None = None,
    ) -> None: ...

    def get_lpar_link(self, system_uuid: str, lpar_uuid: str) -> str: ...

    async def _reconcile_storage_mutation(
        self,
        operation: str,
        snapshot: Callable[[], Awaitable[Any]],
        dispatch: Callable[[], Awaitable[Any]],
        note: str | Callable[[HMCError], str] = "",
    ) -> Any: ...

    async def list_volume_groups(self, vios_uuid: str) -> list[dict[str, Any]]: ...

    async def get_volume_group(
        self, vios_uuid: str, vg_uuid: str
    ) -> dict[str, Any] | None: ...

    async def list_storage_mappings(
        self, vios_uuid: str, lpar_uuid: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def _get_vg_raw_xml(
        self, vios_uuid: str, vg_uuid: str
    ) -> tuple[str | None, Element]: ...

    async def _post_vg_xml(
        self,
        vios_uuid: str,
        vg_uuid: str,
        vg_elem: Element,
        *,
        operation: str = ...,
        etag: str | None = None,
    ) -> dict[str, Any] | None: ...

    def _build_mr_element(self, size_mib: int) -> Element: ...

    def _insert_mr_at_correct_position(
        self, vg_elem: Element, mr_elem: Element
    ) -> None: ...

    def _find_vmlib(self, vg_elem: Element) -> Element | None: ...


class AdaptersClient(Protocol):
    """Host operations required by :class:`client_adapters.AdaptersMixin`."""

    def _uom_headers(
        self, resource_type: str | None, include_schema_version: bool = True
    ) -> dict[str, str]: ...

    async def _request_with_uuid_path_arguments(
        self,
        method: str,
        path: str,
        *,
        uuid_path_arguments: Mapping[str, str],
        **kwargs: Any,
    ) -> httpx.Response: ...

    async def _network_adapter_creation_feed(
        self, lpar_uuid: str
    ) -> list[dict[str, Any]]: ...

    async def list_child(
        self, parent_type: str, parent_uuid: str, child_type: str
    ) -> list[dict[str, Any]]: ...

    async def create_child(
        self, parent_type: str, parent_uuid: str, child_type: str, child_xml: str
    ) -> dict[str, Any] | None: ...

    async def delete_child(
        self, parent_type: str, parent_uuid: str, child_type: str, child_uuid: str
    ) -> None: ...


class JobClient(Protocol):
    """Host operation shared by mixins that submit HMC jobs."""

    async def submit_job(
        self, job_path: str, job_request_xml: str
    ) -> dict[str, Any] | None: ...


class ClusterClient(JobClient, Protocol):
    """Host operations required by :class:`client_cluster.ClusterMixin`."""

    async def list_uom(
        self, resource_type: str, group: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get_uom(
        self, resource_type: str, uuid: str, group: str | None = None
    ) -> dict[str, Any] | None: ...


class LpmClient(JobClient, Protocol):
    """Host operations required by :class:`client_lpm.LpmMixin`."""

    async def _lpar_job(
        self, lpar_uuid: str, operation: str, job_xml: str
    ) -> dict[str, Any] | None: ...


class NetworkClient(Protocol):
    """Host state and operations required by :class:`client_network.NetworkMixin`."""

    _rest_base_url: str

    async def _get(
        self,
        path: str,
        resource_type: str | None = None,
        include_schema_version: bool = True,
    ) -> str: ...

    async def _put(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
    ) -> str: ...

    async def _delete(self, path: str) -> None: ...


class SystemsClient(JobClient, Protocol):
    """Host operations required by :class:`client_systems.SystemsMixin`."""

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response: ...

    async def list_uom(
        self, resource_type: str, group: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get_uom(
        self, resource_type: str, uuid: str, group: str | None = None
    ) -> dict[str, Any] | None: ...

    async def search_uom(
        self, resource_type: str, property_name: str, property_value: str
    ) -> list[dict[str, Any]]: ...

    async def _get(
        self,
        path: str,
        resource_type: str | None = None,
        include_schema_version: bool = True,
    ) -> str: ...

    async def _post(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
    ) -> str: ...

    async def _put(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
    ) -> str: ...

    async def _delete(self, path: str) -> None: ...

    async def list_system_summaries(self) -> list[Any]: ...

    async def get_managed_system(self, uuid: str) -> dict[str, Any] | None: ...

    async def find_system_by_name(self, name: str) -> dict[str, Any] | None: ...

    async def _quick_all_system_names(self) -> dict[str, str]: ...

    async def list_vios(self, system_uuid: str) -> list[dict[str, Any]]: ...


class TemplatesClient(JobClient, Protocol):
    """Host state and operations required by :class:`client_templates.TemplatesMixin`."""

    TEMPLATES_ACCEPT: str
    _session_token: str | None

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response: ...

    async def _templates_get(self, path: str) -> str: ...


class UsersClient(Protocol):
    """Host operations required by :class:`client_users.UsersMixin`."""

    async def _get(
        self,
        path: str,
        resource_type: str | None = None,
        include_schema_version: bool = True,
    ) -> str: ...

    async def _post(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
    ) -> str: ...

    async def _put(
        self,
        path: str,
        body: str | bytes,
        resource_type: str | None = None,
        include_schema_version: bool = True,
    ) -> str: ...

    async def _delete(self, path: str) -> None: ...

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response: ...

    def _child_path(self, console_uuid: str, child_type: str) -> str: ...

    def _entries(self, xml_text: str, path: str) -> list[dict[str, Any]]: ...

    def _first_entry(self, xml_text: str, path: str) -> dict[str, Any] | None: ...
