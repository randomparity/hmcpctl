"""The request path must address the resource the caller named.

Every REST path in this package is built by interpolating caller-supplied
identifiers into an f-string, and httpx resolves RFC 3986 dot-segments when
merging a path onto ``base_url``. That combination lets an undeclared
sub-resource argument steer a request off the resource its declared selector
names — which defeats ADR 0039's target constraints, and applies equally to the
CLI and ``api`` paths that no access policy bounds.

See docs/adr/0039-dispatch-time-target-scope.md, "Sub-resources reached through
a declared selector".
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import cast
from urllib.parse import quote, unquote

import httpx
import pytest

from hmcpctl.client.client_contracts import (
    _MAX_UOM_PATH_VALUE_LENGTH,
    _MAX_UOM_TYPE_LENGTH,
    ADAPTER_TYPES,
    _reject_over_long_path_value,
    _reject_unknown_uom_type,
)
from hmcpctl.client.core import (
    MEDIA_UOM,
    HMCClient,
    _reject_dot_segments,
    _reject_non_job_path,
)
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError, HMCTransportError

UUID_A = "12345678-1234-1234-1234-1234567890ab"
UUID_B = "ABCDEFAB-CDEF-CDEF-CDEF-ABCDEFABCDEF"


def _client() -> HMCClient:
    return HMCClient(
        HMCConfig(host="hmc.test", user="u", password="p")  # pragma: allowlist secret
    )


def _recording_client() -> tuple[HMCClient, list[str]]:
    """A client whose transport records paths instead of sending them."""
    client = _client()
    requested: list[str] = []

    def _record(method, path, **kwargs):
        requested.append(path)
        return httpx.Request(method, f"https://hmc.test:12443{path}")

    client._http.build_request = _record  # type: ignore[method-assign]

    async def _send(request, **kwargs):
        return httpx.Response(204, request=request)

    client._http.send = _send  # type: ignore[method-assign]
    return client, requested


# Each newly guarded request builder, including both delete-network identities.
# Arguments marked UUID_B are varied independently; payload names stay ordinary.
_NEW_UUID_PATH_CALLS = (
    (
        "create_logical_unit",
        (UUID_B, "disk", 1),
        "cluster_uuid",
        0,
        f"Cluster/{UUID_B}/do/CreateLogicalUnit",
    ),
    (
        "delete_logical_unit",
        (UUID_B, "disk"),
        "cluster_uuid",
        0,
        f"Cluster/{UUID_B}/do/DeleteLogicalUnit",
    ),
    (
        "list_logical_partitions",
        (UUID_B,),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/LogicalPartition",
    ),
    (
        "create_logical_partition",
        (UUID_B, "<LogicalPartition/>"),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/LogicalPartition",
    ),
    (
        "delete_logical_partition",
        (UUID_B,),
        "lpar_uuid",
        0,
        f"LogicalPartition/{UUID_B}",
    ),
    (
        "_lpar_job",
        (UUID_B, "Migrate", "<JobRequest/>"),
        "lpar_uuid",
        0,
        f"LogicalPartition/{UUID_B}/do/Migrate",
    ),
    (
        "list_virtual_switches",
        (UUID_B,),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/VirtualSwitch",
    ),
    (
        "list_virtual_networks",
        (UUID_B,),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/VirtualNetwork",
    ),
    (
        "list_network_bridges",
        (UUID_B,),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/NetworkBridge",
    ),
    (
        "create_virtual_network",
        (UUID_B, "network", 2, 0, "switch"),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/VirtualNetwork",
    ),
    (
        "delete_virtual_network",
        (UUID_B, UUID_A),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/VirtualNetwork/{UUID_A}",
    ),
    (
        "delete_virtual_network",
        (UUID_A, UUID_B),
        "network_uuid",
        1,
        f"ManagedSystem/{UUID_A}/VirtualNetwork/{UUID_B}",
    ),
    (
        "modify_managed_system",
        (UUID_B, "<ManagedSystem/>"),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}",
    ),
    (
        "power_on_system",
        (UUID_B,),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/do/PowerOn",
    ),
    (
        "power_off_system",
        (UUID_B,),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/do/PowerOff",
    ),
    (
        "create_vios",
        (UUID_B, "<VirtualIOServer/>"),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/VirtualIOServer",
    ),
    (
        "delete_vios",
        (UUID_B,),
        "vios_uuid",
        0,
        f"VirtualIOServer/{UUID_B}",
    ),
    (
        "power_on_vios",
        (UUID_B,),
        "vios_uuid",
        0,
        f"VirtualIOServer/{UUID_B}/do/PowerOn",
    ),
    (
        "power_off_vios",
        (UUID_B,),
        "vios_uuid",
        0,
        f"VirtualIOServer/{UUID_B}/do/PowerOff",
    ),
    (
        "list_vios",
        (UUID_B,),
        "system_uuid",
        0,
        f"ManagedSystem/{UUID_B}/VirtualIOServer",
    ),
    (
        "get_vios_storage_detail",
        (UUID_B,),
        "vios_uuid",
        0,
        f"VirtualIOServer/{UUID_B}?group=ViosSCSIMapping,ViosFCMapping",
    ),
)


@pytest.mark.parametrize("method,args,argument,position,suffix", _NEW_UUID_PATH_CALLS)
def test_new_uuid_builders_refuse_ordinary_names_before_io(
    method, args, argument, position, suffix
):
    client, requested = _recording_client()
    invalid = "ordinary-resource-name"
    arguments = list(args)
    arguments[position] = invalid
    with pytest.raises(ValueError) as error:
        asyncio.run(getattr(client, method)(*arguments))
    assert argument in str(error.value)
    assert invalid not in str(error.value)
    assert requested == []


@pytest.mark.parametrize("method,args,argument,position,suffix", _NEW_UUID_PATH_CALLS)
def test_new_uuid_builders_preserve_mixed_case_paths(
    method, args, argument, position, suffix, monkeypatch
):
    client = _client()
    requested = []
    mixed = "aBcDeFaB-cDeF-CdEf-cDEF-AbCdEfABCdef"
    arguments = list(args)
    arguments[position] = mixed

    async def record(path, *args, **kwargs):
        requested.append(path)
        return ""

    async def operating():
        return [{"UUID": mixed.lower(), "State": "operating"}]

    for helper in ("_get", "_put", "_post", "_delete", "submit_job"):
        monkeypatch.setattr(client, helper, record)
    # An empty scoped feed reads the system's state from quick/All (#1301,
    # #1500), matching the UUID in any case; keep that off the recorded paths.
    monkeypatch.setattr(client, "list_system_summaries", operating)
    asyncio.run(getattr(client, method)(*arguments))
    assert requested == ["/rest/api/uom/" + suffix.replace(UUID_B, mixed)]


def test_lpar_document_link_refuses_an_ordinary_name_without_io():
    client, requested = _recording_client()
    with pytest.raises(ValueError) as error:
        client.get_lpar_link(UUID_A, "ordinary-partition-name")
    assert "lpar_uuid" in str(error.value)
    assert "ordinary-partition-name" not in str(error.value)
    assert requested == []


def test_lpar_document_link_refuses_an_ordinary_system_name():
    client, requested = _recording_client()
    with pytest.raises(ValueError) as error:
        client.get_lpar_link("ordinary-system-name", UUID_A)
    assert "system_uuid" in str(error.value)
    assert "ordinary-system-name" not in str(error.value)
    assert requested == []


def test_lpar_document_link_preserves_mixed_case():
    client = _client()
    mixed = "aBcDeFaB-cDeF-CdEf-cDEF-AbCdEfABCdef"
    assert client.get_lpar_link(UUID_A, mixed) == (
        f"{client._rest_base_url}/rest/api/uom/ManagedSystem/{UUID_A}/LogicalPartition/{mixed}"
    )


@pytest.mark.parametrize("method", ["list_logical_partitions", "list_vios"])
def test_an_empty_system_scope_never_reads_the_hmc_wide_feed(method, monkeypatch):
    """The HMC-wide feeds time out while one system has no connection (#1293)."""
    client = _client()
    requested = []

    async def record(path, *args, **kwargs):
        requested.append(path)
        return ""

    monkeypatch.setattr(client, "_get", record)
    with pytest.raises(ValueError, match="^system_uuid must be a UUID$"):
        asyncio.run(getattr(client, method)(""))
    assert requested == []


def test_lpm_operation_refusal_precedes_uuid_refusal():
    client, requested = _recording_client()
    with pytest.raises(ValueError, match="^LPM job operation must be one of: "):
        asyncio.run(
            client._lpar_job("ordinary-partition-name", "Unlisted", "<JobRequest/>")
        )
    assert requested == []


# ---------------------------------------------------------------------------
# The property that makes the guard necessary
# ---------------------------------------------------------------------------


def test_httpx_leaves_percent_encoded_dot_segments_untouched():
    """The other half of the empirical fact, and the half that is *not* a reason
    to allow them.

    httpx normalizes literal dot-segments and leaves encoded ones alone. An
    earlier version of the guard concluded from that they could pass, because
    they would "address nothing" — a claim about whether the HMC decodes before
    routing, which nothing here can establish. This pins the library behaviour
    only; the guard refuses both forms regardless.
    """
    client = httpx.AsyncClient(base_url="https://hmc.test:12443")
    encoded = client.build_request(
        "DELETE", "/rest/api/uom/VirtualIOServer/v1/VolumeGroup/%2e%2e/%2e%2e/x"
    )
    assert "%2e%2e" in str(encoded.url)


def test_httpx_resolves_dot_segments_against_the_base_url():
    """The empirical fact the guard exists for, pinned against the real library.

    If a future httpx stops normalizing, this test tells us the guard became
    belt-and-braces rather than load-bearing — and if it starts normalizing
    something new, the guard needs to grow. Either way the assumption is not
    left implicit.
    """
    client = httpx.AsyncClient(base_url="https://hmc.test:12443")
    built = client.build_request(
        "DELETE",
        "/rest/api/uom/VirtualIOServer/vios-1/VolumeGroup/../../../LogicalPartition/prod",
    )
    assert str(built.url) == "https://hmc.test:12443/rest/api/uom/LogicalPartition/prod"


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/rest/api/uom/VirtualIOServer/v1/VolumeGroup/../../../LogicalPartition/prod",
        "/rest/api/uom/VirtualIOServer/v1/VirtualSCSIMapping/../../web/HmcUser/root",
        "/rest/api/uom/LogicalPartition/./x",
        "..",
        "https://hmc.test:12443/rest/api/uom/jobs/../HmcUser/root",
        # Percent-encoded, and mixed. httpx resolves only the raw form, so an
        # earlier version of this guard let these through on the reasoning that
        # they "address nothing" — an assumption about the HMC's own decoding
        # that cannot be tested from here, and the wrong way round for a
        # fail-closed check.
        "/rest/api/uom/Job/%2e%2e/%2e%2e/web/HmcUser/root",
        "/rest/api/uom/Job/..%2f..%2fweb%2fHmcUser%2froot",
        "/rest/api/uom/VirtualIOServer/v1/VolumeGroup/%2E%2E/%2E%2E/LogicalPartition/x",
        # Mixed encoding: httpx leaves this alone, but a literal "../" survives
        # in it, so the *raw* arm should already catch it. The case a
        # hand-written guard usually misses, pinned so it cannot regress to
        # being caught only by the decoded arm.
        "/rest/api/uom/VirtualIOServer/v1/VolumeGroup/..%2f../LogicalPartition/x",
    ],
)
def test_a_dot_segment_is_refused(path):
    with pytest.raises(HMCError, match="refused"):
        _reject_dot_segments("DELETE", path)


@pytest.mark.parametrize(
    "path",
    [
        "/rest/api/uom/LogicalPartition/prod",
        "/rest/api/uom/VirtualIOServer/v1/VolumeGroup/vg-1",
        # Characters, not segments: a resource may legitimately be named this.
        "/rest/api/uom/VirtualIOServer/v1/VolumeGroup/a..b",
        "/rest/api/uom/VirtualIOServer/v1/VolumeGroup/..log",
        "/rest/api/uom/ManagedSystem/s1/LogicalPartition?group=None",
    ],
)
def test_an_ordinary_path_is_not_refused(path):
    assert _reject_dot_segments("GET", path) is None


def test_the_guard_runs_before_anything_leaves_the_process():
    """Refusal is not merely a different error: no request is built at all."""
    client = _client()
    sent: list[str] = []

    def _forbidden(*args, **kwargs):
        sent.append("request")
        raise AssertionError("a refused path reached the transport")

    client._http.build_request = _forbidden  # type: ignore[method-assign]

    with pytest.raises(HMCError, match="refused"):
        asyncio.run(
            client._request(
                "DELETE",
                "/rest/api/uom/VirtualIOServer/v1/VolumeGroup/../../LogicalPartition/x",
            )
        )
    assert sent == []


def test_a_uuid_only_path_argument_is_refused_before_transport():
    client = _client()
    sent: list[str] = []

    def _forbidden(*args, **kwargs):
        sent.append("request")
        raise AssertionError("a refused UUID reached the transport")

    client._http.build_request = _forbidden  # type: ignore[method-assign]

    with pytest.raises(ValueError, match=r"^vg_uuid must be a UUID$") as error:
        asyncio.run(
            client._request_with_uuid_path_arguments(
                "GET",
                "/rest/api/uom/VirtualIOServer/vios/VolumeGroup/not-a-uuid",
                uuid_path_arguments={"vg_uuid": "not-a-uuid"},
            )
        )

    assert "hmc.test" not in str(error.value)
    assert "not-a-uuid" not in str(error.value)
    assert sent == []


def test_canonical_mixed_case_uuid_path_arguments_reach_the_request_boundary():
    client = _client()
    requested: list[tuple[str, str]] = []

    async def _record(method, path, **kwargs):
        requested.append((method, path))
        return httpx.Response(204)

    client._request = _record  # type: ignore[method-assign]
    path = f"/rest/api/uom/VirtualIOServer/{UUID_A}/VolumeGroup/{UUID_B}"

    asyncio.run(
        client._request_with_uuid_path_arguments(
            "GET",
            path,
            uuid_path_arguments={"vios_uuid": UUID_A, "vg_uuid": UUID_B},
        )
    )

    assert requested == [("GET", path)]


_UUID_PATH_BUILDERS = {
    "HMCClient": {
        "get_uom": ("uuid",),
        "get_quick_property": ("uuid",),
        "list_child": ("parent_uuid",),
        "create_child": ("parent_uuid",),
        "delete_child": ("parent_uuid", "child_uuid"),
        "update_logical_partition": ("lpar_uuid",),
    },
    "StorageMixin": {
        "list_volume_groups": ("vios_uuid",),
        "get_volume_group": ("vios_uuid", "vg_uuid"),
        "create_volume_group": ("vios_uuid",),
        "map_storage_to_lpar": ("vios_uuid",),
        "list_storage_mappings": ("vios_uuid",),
        "delete_storage_mapping": ("vios_uuid",),
        "_get_vg_raw_xml": ("vios_uuid", "vg_uuid"),
        "_post_vg_xml": ("vios_uuid", "vg_uuid"),
        "get_media_repository": ("vios_uuid", "vg_uuid"),
        "list_optical_media": ("vios_uuid", "vg_uuid"),
        "list_optical_mappings": ("vios_uuid",),
        "create_optical_mapping": ("vios_uuid",),
        "_web_file_upload": ("file_uuid",),
        "_web_file_delete": ("file_uuid",),
    },
    "UpdatesMixin": {"submit_platform_update": ("system_uuid",)},
}


@pytest.mark.parametrize(
    "owner, method, arguments",
    [
        (owner, method, arguments)
        for owner, methods in _UUID_PATH_BUILDERS.items()
        for method, arguments in methods.items()
    ],
)
def test_uuid_only_path_builder_inventory_uses_explicit_metadata(
    owner, method, arguments
):
    from hmcpctl.client.client_storage import StorageMixin
    from hmcpctl.client.client_updates import UpdatesMixin

    owner_type = {
        "HMCClient": HMCClient,
        "StorageMixin": StorageMixin,
        "UpdatesMixin": UpdatesMixin,
    }[owner]
    source = inspect.getsource(getattr(owner_type, method))
    for argument in arguments:
        assert f'"{argument}": {argument}' in source


def test_platform_update_rejects_a_non_uuid_system_before_transport():
    client = _client()
    sent: list[str] = []

    def _forbidden(*args, **kwargs):
        sent.append("request")
        raise AssertionError("an invalid system UUID reached the transport")

    client._http.build_request = _forbidden  # type: ignore[method-assign]

    with pytest.raises(ValueError, match=r"^system_uuid must be a UUID$"):
        asyncio.run(client.submit_platform_update("not-a-uuid", {}))

    assert sent == []


# ---------------------------------------------------------------------------
# job_href addresses a job, or nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/rest/api/uom/jobs/j-1",
        "/rest/api/uom/jobs/j%2D1",
        # The captured submission's SELF link path (#1161).
        "/rest/api/uom/jobs/1787837921266",
        # Template deployment job paths (#110).
        "/rest/api/templates/jobs/j-1",
        "/rest/api/templates/jobs/j%2D1",
        "/rest/api/templates/jobs/1787837835560",
    ],
)
def test_a_job_link_is_accepted(path):
    assert _reject_non_job_path(path, "job_href") is None


@pytest.mark.parametrize(
    "path",
    [
        "/rest/api/web/HmcUser/root",
        "/rest/api/uom/LogicalPartition/prod",
        "/rest/api/uom/ManagedSystem/s1",
        "",
        # Contains the word but does not address a job — the case that made
        # segment membership too weak a test to rely on.
        "/rest/api/web/HmcUser/jobs",
        "/rest/api/web/HmcUser/root/Job",
        # Job link forms no HMC capture or reference shows (#1202): the legacy
        # uom Job type, a relative jobs path, and the per-operation link.
        "/rest/api/uom/Job/abcd-1234",
        "/jobs/j-1",
        "/rest/api/uom/LogicalPartition/lpar-uuid/do/PowerOn/Job/job-uuid-999",
        # Trailing content after the identifier.
        "/rest/api/uom/Job/j-1/../../web/HmcUser/root",
        "/rest/api/uom/Job/%2e%2e/web/HmcUser/root",
    ],
)
def test_a_non_job_link_is_refused(path):
    """`_web_get` sends the same Accept header `get_hmc_user` uses, so without
    this an href of `/rest/api/web/HmcUser/root` returns the root account record
    through a tool classified `read` on target kind `job`."""
    with pytest.raises(HMCError, match="does not address a job"):
        _reject_non_job_path(path, "job_href")


# ---------------------------------------------------------------------------
# The guard is site-independent, and that is the point of where it lives
# ---------------------------------------------------------------------------


# Every client method that interpolates a caller-supplied sub-resource identifier
# into a path, named by symbol rather than by line: `client.py` gains and loses
# lines, and this list must outlive that. Four independent sweeps enumerated
# these; two of them produced lists that were each missing sites the other had,
# which is the argument for guarding the waist rather than the call sites.
async def _empty_stream():
    return
    yield b""


_SUB_RESOURCE_CALLS = (
    ("delete_child", ("LogicalPartition", "AUTH", "ClientNetworkAdapter", "{X}")),
    ("create_virtual_disk", ("AUTH", "{X}", "disk", 1)),
    ("delete_virtual_disk", ("AUTH", "{X}", "disk")),
    ("_get_vg_raw_xml", ("AUTH", "{X}")),
    ("get_volume_group", ("AUTH", "{X}")),
    ("list_optical_media", ("AUTH", "{X}")),
    ("delete_optical_media", ("AUTH", "{X}", "media")),
    ("_web_file_upload", ("{X}", _empty_stream(), 0)),
    ("_web_file_delete", ("{X}",)),
    ("delete_virtual_network", ("AUTH", "{X}")),
)

TRAVERSAL = "../../../LogicalPartition/VICTIM"


@pytest.mark.parametrize(
    "method, args", _SUB_RESOURCE_CALLS, ids=[name for name, _ in _SUB_RESOURCE_CALLS]
)
def test_no_unsafe_sub_resource_identifier_reaches_transport(method, args):
    """One guard at the waist, not ten checks at the call sites.

    Each of these builds `/{Parent}/{authorized}/{Child}/{caller-supplied}` by
    f-string. A dot-segment in the trailing identifier resolves the authorized
    parent away — so the access policy authorizes one resource and the request
    addresses another, with nothing denied and every "a denied call makes no
    outbound attempt" test still green.

    Parametrized by symbol rather than by file:line deliberately. The
    enumerations of this pattern that circulated during review each missed sites
    the others caught, which is precisely why the check belongs somewhere no
    enumeration has to be complete.
    """
    client = _client()
    call = getattr(client, method)
    with pytest.raises((HMCError, ValueError), match=r"refused|must be a UUID"):
        asyncio.run(
            call(
                *[
                    a.replace("{X}", TRAVERSAL) if isinstance(a, str) else a
                    for a in args
                ]
            )
        )


def test_the_guard_is_reached_by_every_transport_helper():
    """`_request` is the only place any of them can send from.

    If a future helper bypasses `_request` — calling `self._http` directly — the
    guard above stops covering it. This asserts the property the placement
    depends on, rather than trusting that no such helper appears.
    """
    from hmcpctl.client import core as client_module

    source = inspect.getsource(client_module)
    tree = ast.parse(source)
    direct: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        expression = ast.unparse(node.func.value)
        if expression != "self._http" or node.func.attr in {"aclose"}:
            continue
        owner = next(
            (
                parent.name
                for parent in ast.walk(tree)
                if isinstance(parent, ast.AsyncFunctionDef)
                and any(child is node for child in ast.walk(parent))
            ),
            None,
        )
        if owner != "_request":
            direct.append(ast.unparse(node.func))
    assert not direct, f"a transport call bypassing _request: {direct}"


# ---------------------------------------------------------------------------
# The type-segment grammar (ADR 0143)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "LogicalPartition",
        "ManagedSystem",
        "VirtualIOServer",
        "Cluster",
        "SharedStoragePool",
        "UserProfile",
        "TaskRole",
        "ResourceRole",
        "VirtualSwitch",
        "VirtualNetwork",
        "NetworkBridge",
        "VolumeGroup",
        "Job",
        # Lowercase and digit-bearing type names the HMC serves. The grammar
        # constrains the character set, not the casing convention.
        "jobs",
        "SRIOVAdapter",
        *sorted(ADAPTER_TYPES),
    ],
)
def test_a_well_formed_type_is_accepted(value):
    assert _reject_unknown_uom_type("resource_type", value) is None


@pytest.mark.parametrize(
    "value",
    [
        # The two characters issue #809 reproduced: each retargets the GET
        # inside /rest/api/uom/ without tripping the dot-segment guard.
        "LogicalPartition?group=None",
        "LogicalPartition#x",
        # The dot segments the waist guard already refuses, which now fail the
        # grammar first.
        "..",
        ".",
        "%2e%2e",
        "../web/Logon",
        # Separators and whitespace.
        "Logical Partition",
        "Logical/Partition",
        "Logical-Partition",
        "Logical_Partition",
        "Logical.Partition",
        # A leading digit: the HMC's type names are XML element names.
        "1LogicalPartition",
        "",
        # Header-shaped input. httpx carries this into the Accept value and only
        # h11 refuses it, at send time.
        "LogicalPartition\r\nEvil: 1",
        # The pair an `^...$` grammar accepts, because Python's `$` matches
        # before a trailing newline. This is why the predicate uses fullmatch.
        "LogicalPartition\n",
        "LogicalPartition\r",
        # Non-ASCII that str.isalnum() would call alphanumeric.
        "LogicalPartitioñ",
        "Ⅴ",
    ],
)
def test_a_malformed_type_is_refused(value):
    with pytest.raises(ValueError, match="must be an HMC resource type name") as error:
        _reject_unknown_uom_type("resource_type", value)

    message = str(error.value)
    assert message.startswith("resource_type must be")
    # The leak rule _reject_dot_segments already follows: the message names the
    # argument and one character, never the caller's whole string or the host.
    assert value not in message or len(value) <= 1
    assert "hmc.test" not in message
    # A CR or LF reaches the message only through !r, so a refusal cannot inject
    # a line break into a log.
    assert "\n" not in message and "\r" not in message


# Every client method that interpolates a caller-supplied type segment, named by
# symbol with the argument position under test. `RETARGET` is the reproduction
# issue #809 carries: `?` splits a query string and `#` truncates the path at a
# fragment, both retargeting inside /rest/api/uom/ without tripping the
# dot-segment guard.
RETARGET = "LogicalPartition?group=None"

_TYPE_SEGMENT_CALLS = (
    ("list_uom", (RETARGET,), {}),
    ("get_uom", (RETARGET, UUID_A), {}),
    ("get_quick_property", (RETARGET, UUID_A, "PartitionState"), {}),
    ("list_quick_properties", (RETARGET,), {}),
    (
        "list_quick_properties",
        ("LogicalPartition",),
        {"parent_type": RETARGET, "parent_uuid": UUID_A},
    ),
    ("search_uom", (RETARGET, "PartitionName", "prod"), {}),
    ("list_search_parameters", (RETARGET,), {}),
    ("list_operations", (RETARGET,), {}),
    (
        "list_operations",
        ("LogicalPartition",),
        {"parent_type": RETARGET, "parent_uuid": UUID_A},
    ),
    ("list_child", (RETARGET, UUID_A, "ClientNetworkAdapter"), {}),
    ("list_child", ("LogicalPartition", UUID_A, RETARGET), {}),
    ("create_child", (RETARGET, UUID_A, "ClientNetworkAdapter", "<x/>"), {}),
    ("create_child", ("LogicalPartition", UUID_A, RETARGET, "<x/>"), {}),
    ("delete_child", (RETARGET, UUID_A, "ClientNetworkAdapter", UUID_B), {}),
    ("delete_child", ("LogicalPartition", UUID_A, RETARGET, UUID_B), {}),
)


@pytest.mark.parametrize(
    "method, args, kwargs",
    _TYPE_SEGMENT_CALLS,
    ids=[f"{name}-{index}" for index, (name, _, _) in enumerate(_TYPE_SEGMENT_CALLS)],
)
def test_no_unsafe_type_segment_reaches_transport(method, args, kwargs):
    """Refused at the boundary, before anything is built (ADR 0143).

    Asserted against the transport rather than against the exception alone: a
    refusal that still built a request would leave the retargeted path in the
    HMC's audit log even though the caller saw an error.
    """
    client = _client()
    sent: list[str] = []

    def _forbidden(*call_args, **call_kwargs):
        sent.append("request")
        raise AssertionError("a refused type segment reached the transport")

    client._http.build_request = _forbidden  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="must be an HMC resource type name"):
        asyncio.run(getattr(client, method)(*args, **kwargs))
    assert sent == []


def test_uom_headers_refuses_a_malformed_type():
    """The second destination: the same value reaches the Accept parameter.

    `_uom_headers` is where `get_uom_path` -- which builds no uom f-string of
    its own and has no caller inside this package -- puts a caller-supplied
    type. Without this, the Accept destination's only control is h11's
    send-time header validation: it does refuse this value, but it belongs to a
    transitive dependency this repository neither owns, pins for that property,
    nor tests, and it refuses at send time rather than at the boundary
    (ADR 0143).
    """
    client = _client()
    with pytest.raises(ValueError, match="must be an HMC resource type name"):
        client._uom_headers("LogicalPartition\n")


@pytest.mark.parametrize(
    "resource_type, expected",
    [
        ("LogicalPartition", f"{MEDIA_UOM}; type=LogicalPartition"),
        # Falsy values keep the generic Accept they always produced: no type=
        # parameter is emitted, so nothing reaches the destination to check.
        ("", MEDIA_UOM),
        (None, MEDIA_UOM),
    ],
)
def test_uom_headers_passes_a_valid_type_through(resource_type, expected):
    client = _client()
    assert client._uom_headers(resource_type)["Accept"] == expected


# ---------------------------------------------------------------------------
# The group query value (ADR 0145)
# ---------------------------------------------------------------------------


# The values `list_uom` and `get_uom` may put after `?group=`. The first three
# are every group name this repository passes, and `quote(g, safe="")` is the
# identity function on each — so for those rows the expected path below pins the
# wire form as unchanged. The rest are the characters ADR 0145 governs: `&` and
# `=` append a parameter this client did not name, `#` truncates the value, and
# CR/LF raise `httpx.InvalidURL`, which `_request` refuses as `HMCError`
# (ADR 0148) — asserted under *The unbuildable URL* at the end of this module.
_GROUP_VALUES = (
    "RemoteAccess",
    "ViosSCSIMapping",
    "ViosFCMapping",
    "None&foo=bar",
    "None=x",
    "None?x=1",
    "None#/rest/api/web/HmcUser/root",
    "a/b",
    "a b",
    "None\rX-Evil: 1",
    "None\nX-Evil: 1",
)

_GROUP_CALLS = (
    ("list_uom", ("LogicalPartition",), "/rest/api/uom/LogicalPartition"),
    (
        "get_uom",
        ("LogicalPartition", UUID_A),
        f"/rest/api/uom/LogicalPartition/{UUID_A}",
    ),
)


@pytest.mark.parametrize(
    "method, args, prefix", _GROUP_CALLS, ids=[name for name, _, _ in _GROUP_CALLS]
)
@pytest.mark.parametrize("group", _GROUP_VALUES)
def test_a_group_value_reaches_the_query_string_percent_encoded(
    method, args, prefix, group
):
    """One `group` parameter, holding exactly what the caller passed (ADR 0145).

    Asserted twice over, because the two halves fail differently. The path
    equality pins the encoding itself — and for the three names this repository
    passes, `quote` is the identity function, so the same assertion pins that
    their wire form did not change. The parsed-query assertion is what the
    injection characters trip: raw, `None&foo=bar` reaches the HMC as two
    parameters and `None#...` reaches it truncated, and neither shows up as a
    difference in the *number* of characters this client sent.
    """
    client = _client()
    requested: list[str] = []

    async def _record(method_name, path, **kwargs):
        requested.append(path)
        return httpx.Response(204)

    client._request = _record  # type: ignore[method-assign]

    asyncio.run(getattr(client, method)(*args, group=group))

    assert requested == [f"{prefix}?group={quote(group, safe='')}"]
    # Raw, a CR or LF here raises httpx.InvalidURL rather than returning a URL;
    # ADR 0148 translates that at the waist, asserted at the end of this module.
    params = httpx.URL(f"https://hmc.test:12443{requested[0]}").params
    assert params.get_list("group") == [group]
    assert len(params) == 1


def test_a_falsy_group_appends_no_query_string():
    """The `if group:` guard is unchanged: nothing is encoded and nothing is sent."""
    client = _client()
    requested: list[str] = []

    async def _record(method_name, path, **kwargs):
        requested.append(path)
        return httpx.Response(204)

    client._request = _record  # type: ignore[method-assign]

    asyncio.run(client.list_uom("LogicalPartition", group=""))
    asyncio.run(client.list_uom("LogicalPartition", group=None))

    assert requested == ["/rest/api/uom/LogicalPartition"] * 2


def test_a_group_whose_decoded_form_holds_a_dot_segment_is_still_refused():
    """Encoding does not smuggle a dot segment past the waist.

    `_reject_dot_segments` checks `unquote(candidate)` as well as the raw form,
    so `quote("a/../../x", safe="")` — `a%2F..%2F..%2Fx` — is still read as
    carrying `..` segments and still refused before transport. This is a pin,
    not a red test: the raw path was refused too, and what ADR 0145 records is
    that the refusal does *not* move. A `group` the caller percent-encoded
    itself is the one value that stops tripping it, which that record accepts.
    """
    client = _client()
    sent: list[str] = []

    def _forbidden(*args, **kwargs):
        sent.append("request")
        raise AssertionError("a refused group value reached the transport")

    client._http.build_request = _forbidden  # type: ignore[method-assign]

    with pytest.raises(HMCError, match="refused"):
        asyncio.run(
            client.list_uom("LogicalPartition", group="a/../../web/HmcUser/root")
        )
    assert sent == []


def test_a_caller_percent_encoded_group_now_reaches_the_transport_as_data():
    """The one refusal this change does move, pinned rather than left to the record.

    A `group` the caller already percent-encoded is double-encoded here, so the
    waist's single decode resolves `x%252F..%252Fy` to `x%2F..%2Fy` rather than
    to a dot segment. At `4d823cbb` the raw value decoded straight to `x/../y`
    and was refused, so this is red against the unfixed code. Nothing is
    retargeted: the value sits after the `?`, where no path resolution applies
    (ADR 0145, "one narrow residual opens").
    """
    client, requested = _recording_client()

    asyncio.run(client.list_uom("LogicalPartition", group="x%2F..%2Fy"))

    assert requested == ["/rest/api/uom/LogicalPartition?group=x%252F..%252Fy"]


def _is_quote_binding(node: ast.AST) -> str | None:
    """The name a literal `<target> = quote(<arg>, safe="")` statement binds, or `None`.

    The declaration form for a query value, as `_is_boundary_check` is the
    declaration form for a type segment: the assignment says at the call site
    which rule governs the name the f-string below it interpolates. Unlike
    `_is_boundary_check`, this returns the *target* name without requiring it
    to equal `<arg>` — a query value's encoded form is routinely bound to a
    new name (e.g. `encoded_value = quote(property_value, safe="")`), so the
    declaration form for a query value cannot require the same identifier on
    both sides the way the type-segment form does.
    """
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    call = node.value
    if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
        return None
    if not isinstance(call.func, ast.Name) or call.func.id != "quote":
        return None
    if len(call.args) != 1 or not isinstance(call.args[0], ast.Name):
        return None
    safe = [k for k in call.keywords if k.arg == "safe"]
    if len(safe) != 1 or not isinstance(safe[0].value, ast.Constant):
        return None
    if safe[0].value.value != "":
        return None
    return target.id


def test_every_group_query_interpolation_is_encoded():
    """A `?group=` f-string in `core.py` may only interpolate an encoded name.

    The companion the existing uom inventory cannot be: `_uom_path_sites` matches
    an f-string whose *first* part begins `/rest/api/uom/`, and these sites build
    `f"?group={...}"` onto a path already assembled — so `group` is invisible to
    both checks above, which is how it stayed unexamined while the type segment
    beside it was decided twice (ADR 0145).

    **What this does not cover, stated rather than implied.** It matches the
    `path += f"?group={name}"` idiom and the literal `quote(name, safe="")`
    assignment, and only where the f-string interpolates a bare name.
    Concatenation, `.format`, an interpolated attribute or subscript
    (`f"?group={self.g}"`), a differently-spelled encoder, and a name rebound
    between the assignment and the f-string are all invisible here, exactly as
    concatenation and `.format` are to the type-segment walk. This raises the
    cost of adding an unencoded site in the idiom the module uses; it is not a
    proof that none can exist.
    """
    from hmcpctl.client import core as client_module

    tree = ast.parse(inspect.getsource(client_module))
    unencoded: list[tuple[str, str]] = []
    for owner in ast.walk(tree):
        if not isinstance(owner, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        encoded = {
            name
            for node in ast.walk(owner)
            if (name := _is_quote_binding(node)) is not None
        }
        for node in ast.walk(owner):
            if not isinstance(node, ast.JoinedStr) or not node.values:
                continue
            head = node.values[0]
            if not isinstance(head, ast.Constant) or not str(head.value).startswith(
                "?group="
            ):
                continue
            unencoded.extend(
                (owner.name, part.value.id)
                for part in node.values
                if isinstance(part, ast.FormattedValue)
                and isinstance(part.value, ast.Name)
                and part.value.id not in encoded
            )
    assert not unencoded, f"group query values interpolated unencoded: {unencoded}"


# ---------------------------------------------------------------------------
# The guard is site-independent, and no site may join without it
# ---------------------------------------------------------------------------


# The arguments a `/rest/api/uom/` f-string in `core.py` may interpolate, and
# which rule governs each. A name outside this table is a segment nobody has
# decided a rule for, which is what the second assertion below refuses.
_TYPE_SEGMENT_ARGUMENTS = frozenset({"resource_type", "parent_type", "child_type"})

# Segments carrying *data* rather than a schema identifier, governed by
# percent-encoding at the site that builds them: `search_uom`'s two search values
# and `get_quick_property`'s name (ADR 0146). Membership is not self-certifying —
# `test_every_encoded_uom_segment_is_quote_bound` holds each of these to a literal
# `quote(..., safe="")` binding in its own function, so a name is in this class
# because of what its site does and not because of what it is called. That is the
# defect issue #818 found: `property_name` sat in the inventory below with no rule
# behind it, which made the unclassified-segment assertion pass for it.
_ENCODED_SEGMENT_ARGUMENTS = frozenset({"encoded_property", "encoded_value"})

_KNOWN_UOM_SEGMENT_ARGUMENTS = (
    _TYPE_SEGMENT_ARGUMENTS
    | _ENCODED_SEGMENT_ARGUMENTS
    | {
        "uuid",
        "parent_uuid",
        "child_uuid",
        "job_id",
    }
)


# ADR 0160: the seven formerly residual classes have explicit rules. Site
# membership below is inventory only; evidence is checked at each construction.
_UOM_SEGMENT_POLICIES = {
    "cluster_uuid": "uuid",
    "lpar_uuid": "uuid",
    "network_uuid": "uuid",
    "system_uuid": "uuid",
    "vg_uuid": "uuid",
    "vios_uuid": "uuid",
    "console_path_id": "bounded-quoted-console",
}
_UUID_UOM_REQUEST_SITES = {
    "client_cluster.ClusterMixin.create_logical_unit": "cluster_uuid",
    "client_cluster.ClusterMixin.delete_logical_unit": "cluster_uuid",
    "client_lpars.LparsMixin.create_logical_partition": "system_uuid",
    "client_lpars.LparsMixin.delete_logical_partition": "lpar_uuid",
    "client_lpars.LparsMixin.list_logical_partitions": "system_uuid",
    "client_lpars.LparsMixin.update_logical_partition": "lpar_uuid",
    "client_lpm.LpmMixin._lpar_job": "lpar_uuid",
    "client_network.NetworkMixin.create_virtual_network": "system_uuid",
    "client_network.NetworkMixin.delete_virtual_network": "system_uuid network_uuid",
    "client_network.NetworkMixin.list_network_bridges": "system_uuid",
    "client_network.NetworkMixin.list_virtual_networks": "system_uuid",
    "client_network.NetworkMixin.list_virtual_switches": "system_uuid",
    "client_storage.StorageMixin._get_vg_raw_xml": "vios_uuid vg_uuid",
    "client_storage.StorageMixin._post_vg_xml": "vios_uuid vg_uuid",
    "client_storage.StorageMixin.create_optical_mapping": "vios_uuid",
    "client_storage.StorageMixin.create_volume_group": "vios_uuid",
    "client_storage.StorageMixin.delete_storage_mapping": "system_uuid vios_uuid",
    "client_storage.StorageMixin.get_media_repository": "vios_uuid vg_uuid",
    "client_storage.StorageMixin.get_volume_group": "vios_uuid vg_uuid",
    "client_storage.StorageMixin.list_optical_mappings": "vios_uuid",
    "client_storage.StorageMixin.list_optical_media": "vios_uuid vg_uuid",
    "client_storage.StorageMixin.list_storage_mappings": "vios_uuid",
    "client_storage.StorageMixin.list_volume_groups": "vios_uuid",
    "client_storage.StorageMixin.map_storage_to_lpar": "vios_uuid",
    "client_systems.SystemsMixin.create_vios": "system_uuid",
    "client_systems.SystemsMixin.delete_vios": "vios_uuid",
    "client_systems.SystemsMixin.get_vios_storage_detail": "vios_uuid",
    "client_systems.SystemsMixin.list_vios": "system_uuid",
    "client_systems.SystemsMixin.modify_managed_system": "system_uuid",
    "client_systems.SystemsMixin.power_off_system": "system_uuid",
    "client_systems.SystemsMixin.power_off_vios": "vios_uuid",
    "client_systems.SystemsMixin.power_on_system": "system_uuid",
    "client_systems.SystemsMixin.power_on_vios": "vios_uuid",
    "client_updates.UpdatesMixin.submit_platform_update": "system_uuid",
}
_CONSOLE_UOM_REQUEST_SITES = {
    "client_users.UsersMixin._child_path": "console_path_id",
    "client_users.UsersMixin.configure_remote_access": "console_path_id",
    "client_users.UsersMixin.get_remote_access": "console_path_id",
}
_UUID_UOM_SITES = {
    (*owner.split(".", 1), name, "request")
    for owner, names in _UUID_UOM_REQUEST_SITES.items()
    for name in names.split()
} | {
    (
        "client_network",
        "NetworkMixin.create_virtual_network",
        "system_uuid",
        "document-link",
    ),
    ("client_storage", "StorageMixin.get_lpar_link", "lpar_uuid", "document-link"),
    ("client_storage", "StorageMixin.get_lpar_link", "system_uuid", "document-link"),
}
_CLASSIFIED_UOM_SITES = (
    _UUID_UOM_SITES
    | {
        (*owner.split(".", 1), name, "request")
        for owner, name in _CONSOLE_UOM_REQUEST_SITES.items()
    }
    | {
        # These document/comparison identities are classified, not UUID-governed.
        (
            "client_network",
            "NetworkMixin.create_virtual_network",
            "switch_uuid",
            "document-link",
        ),
        (
            "client_storage",
            "StorageMixin.list_storage_mappings",
            "lpar_uuid",
            "comparison",
        ),
        ("client_storage", "_filter_optical_mappings", "lpar_uuid", "comparison"),
        # Existing enforcement remains separately asserted: ADRs 0143, 0151, 0157.
        ("client_lpm", "LpmMixin._lpar_job", "operation", "request"),
        ("client_users", "UsersMixin._child_path", "child_type", "request"),
        ("client_users", "UsersMixin.get_hmc_user", "profile_path_id", "request"),
        ("client_users", "UsersMixin.modify_hmc_user", "profile_path_id", "request"),
        ("client_users", "UsersMixin.delete_hmc_user", "profile_path_id", "request"),
    }
)


def _is_boundary_check(node: ast.AST) -> bool:
    """A literal `_reject_unknown_uom_type("x", x)` call — the declaration form."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_reject_unknown_uom_type"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[1], ast.Name)
        and node.args[0].value == node.args[1].id
    )


def _uom_function_scopes(
    root: ast.AST, prefix: str = ""
) -> list[tuple[str, list[ast.AST]]]:
    """Keep checks in their lexical function, never in a nested function's caller."""
    scopes: list[tuple[str, list[ast.AST]]] = []
    for child in ast.iter_child_nodes(root):
        if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            qualified = f"{prefix}.{child.name}" if prefix else child.name
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                nodes: list[ast.AST] = []
                pending: list[ast.AST] = list(child.body)
                while pending:
                    node = pending.pop()
                    if isinstance(
                        node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
                    ):
                        continue
                    nodes.append(node)
                    pending.extend(ast.iter_child_nodes(node))
                scopes.append((qualified, nodes))
            scopes.extend(_uom_function_scopes(child, qualified))
        else:
            scopes.extend(_uom_function_scopes(child, prefix))
    return scopes


def _uom_segment_expressions(node: ast.JoinedStr) -> list[ast.expr]:
    """Recognize literal, absolute-base and console-child prefixes used here."""
    start = None
    for index, part in enumerate(node.values):
        if (
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and part.value.startswith("/rest/api/uom/")
        ):
            if index == 0 or (
                index == 1
                and isinstance(node.values[0], ast.FormattedValue)
                and ast.unparse(node.values[0].value) == "self._rest_base_url"
            ):
                start = index + 1
            break
    if start is None and node.values:
        first = node.values[0]
        if (
            isinstance(first, ast.FormattedValue)
            and isinstance(first.value, ast.Call)
            and isinstance(first.value.func, ast.Attribute)
            and ast.unparse(first.value.func) == "self._child_path"
        ):
            start = 1
    if start is None:
        return []
    return [
        part.value
        for part in node.values[start:]
        if isinstance(part, ast.FormattedValue)
    ]


def _uom_path_sites() -> tuple[
    list[tuple[str, str, str, str]],
    dict[tuple[str, str], set[str]],
    dict[tuple[str, str], set[str]],
    set[int],
]:
    """Inventory current f-string forms without importing discovered modules.

    Usage labels describe construction sites, not whole-program dataflow.
    Aliases, arbitrary helper returns, percent/format strings and guard dominance
    remain outside this bounded inventory (ADR 0159).
    """
    from hmcpctl.client import core as client_module

    package = Path(client_module.__file__).parent
    interpolations: list[tuple[str, str, str, str]] = []
    guarded: dict[tuple[str, str], set[str]] = {}
    quote_bound: dict[tuple[str, str], set[str]] = {}
    # Inline quote evidence belongs to the one interpolation it wraps. A raw
    # occurrence of the same name stays unbound even in the same function.
    inline_encoded: set[int] = set()
    for source in sorted(package.rglob("*.py")):
        module = source.relative_to(package).with_suffix("").as_posix()
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for function, nodes in _uom_function_scopes(tree):
            owner = (module, function)
            usages = {}
            for node in nodes:
                if isinstance(node, ast.Assign) and isinstance(
                    node.value, ast.JoinedStr
                ):
                    targets = {ast.unparse(target) for target in node.targets}
                    if module == "client_storage" and "expected_link" in targets:
                        usages[id(node.value)] = "comparison"
                    elif module == "client_network" and "switch_link" in targets:
                        usages[id(node.value)] = "document-link"
                if _is_boundary_check(node):
                    guarded.setdefault(owner, set()).add(
                        ast.unparse(cast(ast.Call, node).args[1])
                    )
                if (bound := _is_quote_binding(node)) is not None:
                    quote_bound.setdefault(owner, set()).add(bound)
            for node in nodes:
                if not isinstance(node, ast.JoinedStr):
                    continue
                usage = usages.get(id(node), "request")
                if owner == ("client_storage", "StorageMixin.get_lpar_link"):
                    usage = "document-link"
                for expression in _uom_segment_expressions(node):
                    # Inline encoding is per-expression evidence; it never
                    # certifies a separate raw interpolation of the same name.
                    binding = ast.Assign(
                        targets=[ast.Name(id="inline")], value=expression
                    )
                    if (
                        isinstance(expression, ast.Call)
                        and _is_quote_binding(binding) is not None
                    ):
                        inline_encoded.add(len(interpolations))
                        name = ast.unparse(expression.args[0])
                    else:
                        name = ast.unparse(expression)
                    interpolations.append((module, function, name, usage))
    return interpolations, guarded, quote_bound, inline_encoded


def _matching_argument_check(node: ast.AST, predicate: str, argument: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == predicate
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == argument
        and isinstance(node.args[1], ast.Name)
        and node.args[1].id == argument
    )


def _mapped_path_arguments(
    node: ast.AST, path: ast.JoinedStr, bindings: set[str]
) -> set[str]:
    """Only literal metadata on an existing request helper receiving this path."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return set()
    helpers = {
        "_get": 0,
        "_put": 0,
        "_post": 0,
        "_delete": 0,
        "_request_with_uuid_path_arguments": 1,
    }
    position = helpers.get(node.func.attr)
    if (
        ast.unparse(node.func.value) != "self"
        or position is None
        or len(node.args) <= position
    ):
        return set()
    destination = node.args[position]
    if destination is not path and not (
        isinstance(destination, ast.Name) and destination.id in bindings
    ):
        return set()
    return {
        key.value
        for keyword in node.keywords
        if keyword.arg == "uuid_path_arguments" and isinstance(keyword.value, ast.Dict)
        for key, value in zip(keyword.value.keys, keyword.value.values)
        if isinstance(key, ast.Constant)
        and isinstance(value, ast.Name)
        and key.value == value.id
    }


def test_every_governed_uom_segment_has_policy_evidence():
    """Classification never certifies a missing UUID check or console bound.

    Recognizes current direct declarations, literal request metadata and the two
    concrete storage closures; not aliases, dataflow or guard dominance.
    """
    from hmcpctl.client import core as client_module

    sites, _, _, _ = _uom_path_sites()
    required = {
        (module, function, name)
        for module, function, name, usage in sites
        if name in _UOM_SEGMENT_POLICIES and usage != "comparison"
    }
    # This dispatch closure captures one parent-built path. Never lend its
    # direct checks, or another nested function's metadata, to the parent.
    # (delete_storage_mapping's detach RMW moved into the shared module-level
    # `_rmw_vios_mapping` helper, ADR 0169 follow-up #1037, so it no longer
    # builds its own dispatch closure or `post_path` here.)
    storage_dispatch_paths = {
        "StorageMixin._post_vg_xml": "path",
    }
    missing = []
    package = Path(client_module.__file__).parent
    for source in sorted(package.rglob("*.py")):
        module = source.relative_to(package).with_suffix("").as_posix()
        scopes = dict(
            _uom_function_scopes(ast.parse(source.read_text(encoding="utf-8")))
        )
        for function, nodes in scopes.items():
            for path in nodes:
                if not isinstance(path, ast.JoinedStr):
                    continue
                bindings = {
                    target.id
                    for node in nodes
                    if isinstance(node, ast.Assign) and node.value is path
                    for target in node.targets
                    if isinstance(target, ast.Name)
                }
                mapped = set().union(
                    *(_mapped_path_arguments(node, path, bindings) for node in nodes)
                )
                capture = (
                    storage_dispatch_paths.get(function)
                    if module == "client_storage"
                    else None
                )
                if capture in bindings:
                    mapped.update(
                        set().union(
                            *(
                                _mapped_path_arguments(node, path, {capture})
                                for node in scopes.get(f"{function}.dispatch", [])
                            )
                        )
                    )
                for expression in _uom_segment_expressions(path):
                    # A canonical identity is never encoded data: an
                    # inline-quoted UUID segment is unclassifiable, and the
                    # failure must name its plain argument, not the call.
                    binding = ast.Assign(
                        targets=[ast.Name(id="inline")], value=expression
                    )
                    if (
                        isinstance(expression, ast.Call)
                        and _is_quote_binding(binding) is not None
                    ):
                        name = ast.unparse(expression.args[0])
                    else:
                        name = ast.unparse(expression)
                    site = (module, function, name)
                    if site not in required:
                        continue
                    if _UOM_SEGMENT_POLICIES[name] == "uuid":
                        enforced = name in mapped or any(
                            _matching_argument_check(
                                node, "_reject_non_uuid_path_argument", name
                            )
                            for node in nodes
                        )
                    else:
                        enforced = any(
                            _is_quote_binding(node) == name
                            and ast.unparse(
                                cast(ast.Call, cast(ast.Assign, node).value).args[0]
                            )
                            == "console_uuid"
                            for node in nodes
                        ) and any(
                            _matching_argument_check(
                                node, "_reject_over_long_path_value", "console_uuid"
                            )
                            for node in nodes
                        )
                    if not enforced:
                        missing.append(site)
    assert not missing, f"uom segments missing policy evidence: {sorted(set(missing))}"


@pytest.mark.parametrize(
    "evidence",
    [
        '_reject_non_uuid_path_argument("system_uuid", system_uuid)',
        'self._get(path, "ManagedSystem", uuid_path_arguments={"system_uuid": system_uuid})',
    ],
    ids=["direct", "mapped"],
)
def test_uuid_inventory_detects_missing_site_evidence(tmp_path, monkeypatch, evidence):
    from hmcpctl.client import core as client_module

    core_path = tmp_path / "core.py"
    core_path.write_text("", encoding="utf-8")
    source = tmp_path / "client_extra.py"
    prefix = (
        "def example(self, system_uuid):\n"
        '    path = f"/rest/api/uom/ManagedSystem/{system_uuid}"\n'
    )
    source.write_text(prefix + f"    {evidence}\n", encoding="utf-8")
    monkeypatch.setattr(client_module, "__file__", str(core_path))
    test_every_governed_uom_segment_has_policy_evidence()
    # The same name in another function or a nested function cannot certify it.
    source.write_text(
        prefix + "    def nested():\n" + f"        {evidence}\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="client_extra.*example.*system_uuid"):
        test_every_governed_uom_segment_has_policy_evidence()


@pytest.mark.parametrize(
    "evidence",
    [
        '_reject_non_uuid_path_argument("other_uuid", system_uuid)',
        '_reject_non_uuid_path_argument("system_uuid", other_uuid)',
        'self._get(path, "ManagedSystem", uuid_path_arguments={"system_uuid": other_uuid})',
        'self._get(path, "ManagedSystem", uuid_path_arguments=metadata)',
        'self._get(other_path, "ManagedSystem", uuid_path_arguments={"system_uuid": system_uuid})',
    ],
)
def test_uuid_inventory_requires_matching_argument_and_destination(
    tmp_path, monkeypatch, evidence
):
    from hmcpctl.client import core as client_module

    source = tmp_path / "core.py"
    source.write_text(
        "def example(self, system_uuid, other_uuid, other_path, metadata):\n"
        '    path = f"/rest/api/uom/ManagedSystem/{system_uuid}"\n'
        f"    {evidence}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(client_module, "__file__", str(source))
    with pytest.raises(AssertionError, match="core.*example.*system_uuid"):
        test_every_governed_uom_segment_has_policy_evidence()


@pytest.mark.parametrize("missing", ["bound", "quote"])
def test_console_inventory_requires_its_own_bound_and_quote(
    tmp_path, monkeypatch, missing
):
    from hmcpctl.client import core as client_module

    source = tmp_path / "core.py"
    evidence = {
        "bound": '    _reject_over_long_path_value("console_uuid", console_uuid)\n',
        "quote": '    console_path_id = quote(console_uuid, safe="")\n',
    }
    prefix = "def example(console_uuid, console_path_id):\n"
    path = '    return f"/rest/api/uom/ManagementConsole/{console_path_id}"\n'
    source.write_text(prefix + "".join(evidence.values()) + path, encoding="utf-8")
    monkeypatch.setattr(client_module, "__file__", str(source))
    test_every_governed_uom_segment_has_policy_evidence()
    source.write_text(
        prefix
        + "".join(value for key, value in evidence.items() if key != missing)
        + "    def nested():\n    "
        + evidence[missing]
        + path,
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="core.*example.*console_path_id"):
        test_every_governed_uom_segment_has_policy_evidence()


@pytest.mark.parametrize(
    "function, binding",
    [
        ("_post_vg_xml", "path"),
    ],
)
def test_storage_dispatch_metadata_certifies_only_its_captured_path(
    tmp_path, monkeypatch, function, binding
):
    from hmcpctl.client import core as client_module

    core_path = tmp_path / "core.py"
    core_path.write_text("", encoding="utf-8")
    source = tmp_path / "client_storage.py"
    prefix = (
        "class StorageMixin:\n"
        f"    def {function}(self, vios_uuid):\n"
        f'        {binding} = f"/rest/api/uom/VirtualIOServer/{{vios_uuid}}"\n'
        "        async def dispatch():\n"
    )
    metadata = (
        f'            await self._request_with_uuid_path_arguments("POST", {binding}, '
        'uuid_path_arguments={"vios_uuid": vios_uuid})\n'
    )
    source.write_text(prefix + metadata, encoding="utf-8")
    monkeypatch.setattr(client_module, "__file__", str(core_path))
    test_every_governed_uom_segment_has_policy_evidence()
    source.write_text(prefix + "            pass\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=f"client_storage.*{function}.*vios_uuid"):
        test_every_governed_uom_segment_has_policy_evidence()


def test_an_inline_quoted_uuid_identity_still_requires_its_check(tmp_path, monkeypatch):
    """Encoding a canonical identity does not satisfy its UUID policy.

    The inventory normalizes inline quote forms to their argument, so this
    synthetic site is classified as `system_uuid` and must still carry the
    canonical check — encoding is not a supported identity rule (ADR 0160).
    """
    from hmcpctl.client import core as client_module

    source = tmp_path / "core.py"
    source.write_text(
        "def example(self, system_uuid):\n"
        "    path = f\"/rest/api/uom/ManagedSystem/{quote(system_uuid, safe='')}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(client_module, "__file__", str(source))
    with pytest.raises(AssertionError, match="core.*example.*system_uuid"):
        test_every_governed_uom_segment_has_policy_evidence()


def test_every_uom_type_interpolation_is_guarded():
    """A type segment must carry its check in the same module and lexical scope."""
    interpolations, guarded, _, _ = _uom_path_sites()
    unguarded = sorted(
        {
            (module, function, name)
            for module, function, name, _ in interpolations
            if name in _TYPE_SEGMENT_ARGUMENTS
            and name not in guarded.get((module, function), set())
        }
    )
    assert not unguarded, f"uom type segments interpolated without a check: {unguarded}"


def test_every_uom_path_interpolation_is_a_known_argument():
    """A new *kind* of segment fails until someone decides which rule governs it.

    The companion to the check above, and not a substitute for it: this one
    catches a segment nobody has classified, that one catches a classified
    segment nobody guarded.
    """
    interpolations, _, _, _ = _uom_path_sites()
    unknown = sorted(
        (module, function, name, usage)
        for module, function, name, usage in interpolations
        if not (
            module == "core"
            and name in _KNOWN_UOM_SEGMENT_ARGUMENTS
            or (module, function, name, usage) in _CLASSIFIED_UOM_SITES
        )
    )
    assert not unknown, f"unclassified uom path segment arguments: {unknown}"


def test_uom_path_inventory_detects_an_unclassified_module(tmp_path, monkeypatch):
    from hmcpctl.client import core as client_module

    core_path = tmp_path / "core.py"
    core_path.write_text("# Synthetic package root.\n", encoding="utf-8")
    (tmp_path / "client_extra.py").write_text(
        "async def example(new_segment):\n"
        '    return f"/rest/api/uom/Example/{new_segment}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(client_module, "__file__", str(core_path))
    with pytest.raises(AssertionError, match="client_extra.*example.*new_segment"):
        test_every_uom_path_interpolation_is_a_known_argument()


def test_uom_inventory_keeps_prefixed_composed_and_inline_segments(
    tmp_path, monkeypatch
):
    from hmcpctl.client import core as client_module

    core_path = tmp_path / "core.py"
    core_path.write_text("", encoding="utf-8")
    (tmp_path / "client_network.py").write_text(
        "class NetworkMixin:\n"
        "    def create_virtual_network(self, system_uuid, switch_uuid):\n"
        '        switch_link = (f"{self._rest_base_url}/rest/api/uom/ManagedSystem/"\n'
        '                       f"{system_uuid}/VirtualSwitch/{switch_uuid}")\n',
        encoding="utf-8",
    )
    (tmp_path / "client_users.py").write_text(
        "class UsersMixin:\n"
        "    def get_hmc_user(self, console_uuid, user_profile_uuid):\n"
        '        profile_path_id = quote(user_profile_uuid, safe="")\n'
        "        return f\"{self._child_path(console_uuid, 'UserProfile')}/{profile_path_id}\"\n",
        encoding="utf-8",
    )
    (tmp_path / "client_storage.py").write_text(
        "def _filter_optical_mappings(lpar_uuid):\n"
        '    expected_link = f"/rest/api/uom/LogicalPartition/{lpar_uuid}"\n',
        encoding="utf-8",
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "inline.py").write_text(
        "def example(system_uuid, data):\n"
        "    return f\"/rest/api/uom/ManagedSystem/{quote(system_uuid, safe='')}/{data.id}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(client_module, "__file__", str(core_path))
    sites, _, _, encoded = _uom_path_sites()
    assert set(sites) == {
        (
            "client_network",
            "NetworkMixin.create_virtual_network",
            "system_uuid",
            "document-link",
        ),
        (
            "client_network",
            "NetworkMixin.create_virtual_network",
            "switch_uuid",
            "document-link",
        ),
        ("client_users", "UsersMixin.get_hmc_user", "profile_path_id", "request"),
        ("client_storage", "_filter_optical_mappings", "lpar_uuid", "comparison"),
        ("nested/inline", "example", "system_uuid", "request"),
        ("nested/inline", "example", "data.id", "request"),
    }
    assert {sites[index] for index in encoded} == {
        ("nested/inline", "example", "system_uuid", "request")
    }
    test_every_encoded_uom_segment_is_quote_bound()


def test_uom_inventory_cannot_borrow_another_functions_guards(tmp_path, monkeypatch):
    from hmcpctl.client import core as client_module

    core_path = tmp_path / "core.py"
    core_path.write_text(
        "def same(resource_type):\n"
        '    _reject_unknown_uom_type("resource_type", resource_type)\n'
        '    return f"/rest/api/uom/{resource_type}"\n',
        encoding="utf-8",
    )
    (tmp_path / "client_extra.py").write_text(
        "class Guarded:\n"
        "    def same(resource_type, value):\n"
        '        _reject_unknown_uom_type("resource_type", resource_type)\n'
        '        encoded_value = quote(value, safe="")\n'
        '        return f"/rest/api/uom/{resource_type}/{encoded_value}"\n'
        "class Unguarded:\n"
        "    def same(resource_type, encoded_value):\n"
        "        def nested():\n"
        '            _reject_unknown_uom_type("resource_type", resource_type)\n'
        '            encoded_value = quote(resource_type, safe="")\n'
        '        return f"/rest/api/uom/{resource_type}/{encoded_value}"\n'
        "def same(resource_type):\n"
        '    return f"/rest/api/uom/{resource_type}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(client_module, "__file__", str(core_path))
    with pytest.raises(
        AssertionError, match="client_extra.*Unguarded.same.*resource_type"
    ):
        test_every_uom_type_interpolation_is_guarded()
    with pytest.raises(
        AssertionError, match="client_extra.*Unguarded.same.*encoded_value"
    ):
        test_every_encoded_uom_segment_is_quote_bound()


def test_inline_encoding_cannot_certify_a_raw_sibling(tmp_path, monkeypatch):
    from hmcpctl.client import core as client_module

    source = tmp_path / "core.py"
    inline = (
        "def example(encoded_value):\n"
        "    first = f\"/rest/api/uom/Example/{quote(encoded_value, safe='')}\"\n"
    )
    source.write_text(inline, encoding="utf-8")
    monkeypatch.setattr(client_module, "__file__", str(source))
    test_every_encoded_uom_segment_is_quote_bound()
    source.write_text(
        inline + '    second = f"/rest/api/uom/Example/{encoded_value}"\n',
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="core.*example.*encoded_value"):
        test_every_encoded_uom_segment_is_quote_bound()


def test_every_encoded_uom_segment_is_quote_bound():
    """Encoded segments retain literal quote binding in their own lexical scope.

    This checks the existing binding idiom, not dominance or later rebinding.
    Inline calls are recognized by the same quote predicate as assignments.
    """
    interpolations, _, quote_bound, inline_encoded = _uom_path_sites()
    unbound = sorted(
        {
            (module, function, name)
            for index, (module, function, name, _) in enumerate(interpolations)
            if name
            in _ENCODED_SEGMENT_ARGUMENTS | {"console_path_id", "profile_path_id"}
            and name not in quote_bound.get((module, function), set())
            and index not in inline_encoded
        }
    )
    assert not unbound, f"encoded uom segments interpolated unbound: {unbound}"


# ---------------------------------------------------------------------------
# The type-segment length bound (ADR 0147)
# ---------------------------------------------------------------------------


def test_a_type_at_the_length_bound_is_accepted():
    """The bound is inclusive: exactly `_MAX_UOM_TYPE_LENGTH` is still a type."""
    value = "A" + "b" * (_MAX_UOM_TYPE_LENGTH - 1)
    assert len(value) == _MAX_UOM_TYPE_LENGTH
    assert _reject_unknown_uom_type("resource_type", value) is None


@pytest.mark.parametrize(
    "length",
    [
        _MAX_UOM_TYPE_LENGTH + 1,
        # The reproduction issue #820 carries: a megabyte of grammar-valid
        # characters built a megabyte-long URL and Accept header and sent them.
        1024 * 1024,
    ],
)
def test_a_grammar_valid_type_over_the_bound_is_refused(length):
    value = "A" * length
    with pytest.raises(ValueError, match="must be an HMC resource type name") as error:
        _reject_unknown_uom_type("resource_type", value)

    message = str(error.value)
    assert message.startswith("resource_type must be")
    # The length branch, not the character branch: an all-alphanumeric value has
    # no offending character, so naming one would describe a rule it did not
    # break.
    assert f"a value of {length} characters" in message
    assert f"{_MAX_UOM_TYPE_LENGTH}-character maximum" in message
    # The same leak rule the character branch follows: the length, never the
    # value, which on the CLI and API paths carries an operator's own strings.
    assert value not in message


def test_the_length_branch_precedes_the_character_branch():
    """An over-long value that also breaks the character class reports length.

    Checking length first is what keeps a megabyte from being scanned character
    by character before it is refused, and it is the ordering the message's
    detail depends on.
    """
    value = "A" * (_MAX_UOM_TYPE_LENGTH + 1) + "?group=None"
    with pytest.raises(ValueError) as error:
        _reject_unknown_uom_type("resource_type", value)

    assert f"a value of {len(value)} characters" in str(error.value)


# ---------------------------------------------------------------------------
# The quick-property name segment (ADR 0146)
# ---------------------------------------------------------------------------

# The six quick-property names this repository passes. `rg -n -o
# "quick/[A-Za-z0-9_.%-]*" src/ tests/ docs/` returns exactly these, beside a
# bare `quick/` and two prose artifacts (`quick/All.`, `quick/PartitionState.`).
_QUICK_PROPERTY_NAMES = (
    "PartitionState",
    "PartitionID",
    "SystemType",
    "NoSuchProperty",
    "all",
    "All",
)

_QUICK_PREFIX = f"/rest/api/uom/LogicalPartition/{UUID_A}/quick/"


def _quick_property_path(property_name: str) -> str:
    """The path `get_quick_property` hands the transport for *property_name*.

    Records at `build_request` rather than after `send`, because a value that
    re-points the request does so while the URL is being built.
    """
    client, requested = _recording_client()

    assert (
        asyncio.run(
            client.get_quick_property("LogicalPartition", UUID_A, property_name)
        )
        is None
    )
    assert len(requested) == 1
    return requested[0]


@pytest.mark.parametrize(
    "property_name",
    [
        # The `?` and `#` reproduction issue #818 carries, on the same line
        # ADR 0143 hardened for the type segment beside it.
        "PartitionState?group=None",
        "PartitionState#/rest/api/web/HmcUser/root",
        # A second path segment: not a retarget out of the resource, but not the
        # property the caller named either.
        "PartitionState/extra",
        "Partition State",
        "Partitioñ",
        # Raw, this raises httpx.InvalidURL; ADR 0148 translates it to HMCError
        # at the waist, which encoding here keeps unreached.
        "PartitionState\r\nX-Evil: 1",
    ],
)
def test_a_quick_property_name_cannot_re_point_the_request(property_name):
    """The whole name stays inside the last path segment (ADR 0146).

    Not asserted by comparing against `quote(...)` alone, which would only say
    the client called the function this test expects. The two assertions below
    are the property itself: nothing structural survives into the segment, and
    the segment still decodes to exactly what the caller passed — so neither a
    truncation nor an addition can pass.
    """
    path = _quick_property_path(property_name)

    assert path.startswith(_QUICK_PREFIX)
    segment = path[len(_QUICK_PREFIX) :]
    assert not set(segment) & set("?#/\r\n")
    assert unquote(segment) == property_name
    # Every case here holds a character outside RFC 3986's unreserved set, so
    # each must actually be rewritten. Without this the space and non-ASCII
    # cases assert nothing that can fail: httpx encodes both while building the
    # URL, so their two assertions above hold whether or not the client encoded
    # anything, and dropping the binding would leave them green.
    assert segment != property_name


@pytest.mark.parametrize("property_name", _QUICK_PROPERTY_NAMES)
def test_encoding_is_a_no_op_on_the_quick_property_names_this_client_passes(
    property_name,
):
    """No wire-format change for any name this repository passes (ADR 0146).

    Asserted against the built path rather than `quote(n, safe="") == n`, which
    imports no client code at all. What the built-path form adds is coverage of
    the path *template* — a segment reordered or a literal changed reddens this.
    It is deliberately blind to the binding's removal, because `quote` is the
    identity on all six names; `test_a_quick_property_name_cannot_re_point_the_request`
    is what fails when the binding goes.
    """
    assert _quick_property_path(property_name) == _QUICK_PREFIX + property_name


@pytest.mark.parametrize("property_name", ["..", ".", "../../x"])
def test_a_dot_segment_quick_property_name_is_still_refused(property_name):
    """The refusal identity that does *not* move (ADR 0146).

    `quote` leaves `.` alone and turns `../../x` into `..%2F..%2Fx`, which the
    waist guard's percent-decoding arm still reads as dot segments. Asserted
    against the transport as well as the exception: a refusal that still built a
    request would leave the path in the HMC's audit log.
    """
    client = _client()

    def _forbidden(*args, **kwargs):
        raise AssertionError("a refused quick-property name reached the transport")

    client._http.build_request = _forbidden  # type: ignore[method-assign]

    with pytest.raises(HMCError, match="refused"):
        asyncio.run(
            client.get_quick_property("LogicalPartition", UUID_A, property_name)
        )


@pytest.mark.parametrize(
    "property_name",
    [
        "..%2f..%2fweb%2fHmcUser%2froot",
        "%2e%2e",
        "%2E%2E",
        "..%2F..%2Fx",
        # Shaped like a URL. `_reject_dot_segments` takes a *path*, and its first
        # statement is `urlparse(path).path if "://" in path else path` — handed a
        # bare segment carrying `://`, that branch parses `x` as a scheme and
        # `%2e%2e` as a netloc, leaving an empty path for both arms to scan. The
        # segment is therefore handed to the predicate as the path it forms.
        "x://%2e%2e",
        "x://%2E%2E",
        "x://%2e%2e/y",
        # And the other half of the same hazard: a name starting with `/` makes
        # the prefixed value start `//`, which `urlparse` reads as a netloc
        # rather than a path. Both halves are why the value is handed over as
        # the path it actually occupies, with a non-empty first segment.
        "/..%2f://",
        "/..%2fx://",
        "/..%2F://",
    ],
)
def test_a_caller_percent_encoded_dot_segment_name_is_refused_too(property_name):
    """Encoding must not buy a dot segment passage past the waist (ADR 0146).

    This is the case the site guard exists for. Percent-encoding a name the
    caller had already encoded double-encodes it, so `..%2f..` reaches the wire
    as `..%252f..` and neither of the waist guard's two arms reads it as a dot
    segment — it would be sent. Calling `_reject_dot_segments` on the *argument*,
    before encoding, is what keeps it refused.

    Reasoning that the double-encoded form "addresses nothing" is exactly the
    argument `_reject_dot_segments`' own body records having removed: how many
    times the HMC's web stack decodes a path is untestable from here, and
    guessing low is the wrong direction for a fail-closed check.
    """
    client = _client()

    def _forbidden(*args, **kwargs):
        raise AssertionError("a pre-encoded dot segment reached the transport")

    client._http.build_request = _forbidden  # type: ignore[method-assign]

    with pytest.raises(HMCError, match="refused"):
        asyncio.run(
            client.get_quick_property("LogicalPartition", UUID_A, property_name)
        )


# ---------------------------------------------------------------------------
# The unbuildable URL (ADR 0148)
# ---------------------------------------------------------------------------

# Exactly the raw values the encoding tests above describe in prose and, before
# ADR 0148, could only describe: each carries a character httpx refuses.
_UNBUILDABLE_PATHS = (
    "/rest/api/uom/LogicalPartition?group=None\rX-Evil: 1",
    "/rest/api/uom/LogicalPartition?group=None\nX-Evil: 1",
    f"/rest/api/uom/LogicalPartition/{UUID_A}/quick/PartitionState\r\nX-Evil: 1",
    f"/rest/api/uom/LogicalPartition/{UUID_A}\tX-Evil: 1",
)

# The waist, and the public method ADR 0145 and ADR 0146 recorded as still
# reaching it: `get_uom_path` hands its path to `_get` unchanged.
_UNBUILDABLE_CALLS = (
    ("_request", lambda c, p: c._request("GET", p)),
    ("get_uom_path", lambda c, p: c.get_uom_path(p, "LogicalPartition")),
)


@pytest.mark.parametrize(
    "call", [c for _, c in _UNBUILDABLE_CALLS], ids=[n for n, _ in _UNBUILDABLE_CALLS]
)
@pytest.mark.parametrize("path", _UNBUILDABLE_PATHS)
def test_a_url_httpx_refuses_to_build_is_refused_as_an_hmc_error(call, path):
    """The waist's exception contract holds for a URL httpx will not build."""
    client = _client()

    with pytest.raises(HMCError) as error:
        asyncio.run(call(client, path))

    # `HMCTransportError` subclasses `HMCError`, so `pytest.raises` alone would
    # accept the classification ADR 0148 rejects. httpx's reason is carried,
    # and the cause chain with it, without pinning httpx's wording; the path is
    # not, because this message reaches logs and the path holds the control
    # character httpx rejected.
    assert not isinstance(error.value, HMCTransportError)
    message = str(error.value)
    assert message.startswith("GET refused:")
    assert str(error.value.__cause__) in message
    assert path not in message
    assert not set(message) & set("\r\n\t")


# ---------------------------------------------------------------------------
# The encoded path-value length bound (ADR 0150)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("validate", [False, True])
def test_over_long_search_property_name_is_refused_before_io(validate):
    client, requested = _recording_client()
    name = "A" * (_MAX_UOM_PATH_VALUE_LENGTH + 1)

    with pytest.raises(ValueError) as error:
        asyncio.run(
            client.search_uom("ManagedSystem", name, "value", validate=validate)
        )

    assert requested == []
    assert "property_name" in str(error.value)
    assert name not in str(error.value)


@pytest.mark.parametrize("character", ["A", "\U0001f600"])
def test_search_property_name_at_length_bound_is_accepted(character):
    client, requested = _recording_client()
    name = character * _MAX_UOM_PATH_VALUE_LENGTH

    asyncio.run(client.search_uom("ManagedSystem", name, "value"))

    assert requested == [
        f"/rest/api/uom/ManagedSystem/search/({quote(name, safe='')}==value)"
    ]


def test_a_search_value_at_the_length_bound_is_accepted():
    """The bound is inclusive: exactly `_MAX_UOM_PATH_VALUE_LENGTH` is still sent."""
    client, requested = _recording_client()
    value = "A" * _MAX_UOM_PATH_VALUE_LENGTH

    asyncio.run(client.search_uom("ManagedSystem", "SystemName", value))

    assert requested == [f"/rest/api/uom/ManagedSystem/search/(SystemName=={value})"]


def test_a_console_uuid_at_the_length_bound_is_accepted():
    """The same inclusive edge on the other argument."""
    client, requested = _recording_client()
    value = "A" * _MAX_UOM_PATH_VALUE_LENGTH

    asyncio.run(client.list_hmc_users(value))

    assert requested == [f"/rest/api/uom/ManagementConsole/{value}/UserProfile"]


@pytest.mark.parametrize(
    "length",
    [
        _MAX_UOM_PATH_VALUE_LENGTH + 1,
        # The reproduction issue #827 carries, and the length verified at
        # `101f2117` to build and send a 65,065-character URL.
        65_000,
    ],
)
def test_an_over_long_search_value_is_refused_before_any_request(length):
    client, requested = _recording_client()

    with pytest.raises(ValueError) as error:
        asyncio.run(client.search_uom("ManagedSystem", "SystemName", "A" * length))

    assert requested == []
    assert str(error.value).startswith("property_value is ")


@pytest.mark.parametrize("length", [_MAX_UOM_PATH_VALUE_LENGTH + 1, 65_000])
def test_an_over_long_console_uuid_is_refused_before_any_request(length):
    client, requested = _recording_client()

    with pytest.raises(ValueError) as error:
        asyncio.run(client.list_hmc_users("A" * length))

    assert requested == []
    assert str(error.value).startswith("console_uuid is ")


def test_an_over_long_search_value_is_refused_before_the_validation_request():
    """The check sits at the top of `search_uom`, ahead of the `validate=True`
    discovery read, so an over-long value never pays a network round trip
    (ADR 0150). With the check at the encoding site instead, this request is
    sent before the refusal.
    """
    client, requested = _recording_client()

    with pytest.raises(ValueError):
        asyncio.run(
            client.search_uom(
                "ManagedSystem",
                "SystemName",
                "A" * (_MAX_UOM_PATH_VALUE_LENGTH + 1),
                validate=True,
            )
        )

    assert requested == []


def test_the_refusal_names_the_argument_and_both_lengths_but_not_the_value():
    """The same leak rule `_reject_unknown_uom_type` follows: these values carry
    an operator's own resource names on the CLI and API paths.
    """
    value = "secret-system-name" * 40
    assert len(value) > _MAX_UOM_PATH_VALUE_LENGTH

    with pytest.raises(ValueError) as error:
        _reject_over_long_path_value("property_value", value)

    message = str(error.value)
    assert message == (
        f"property_value is {len(value)} characters; "
        f"maximum is {_MAX_UOM_PATH_VALUE_LENGTH}"
    )
    assert value not in message


def test_the_bound_caps_the_encoded_contribution_whatever_was_passed():
    """The property the number is chosen for: `quote` emits three characters per
    UTF-8 byte and a character can be four bytes, so an accepted value
    contributes at most twelve characters per character to the URL. Without the
    bound that ceiling is httpx's 65536 and the accepted *input* length varies
    twelvefold with the caller's characters (ADR 0150).
    """
    worst_case = "\U0001f600" * _MAX_UOM_PATH_VALUE_LENGTH
    assert _reject_over_long_path_value("property_value", worst_case) is None
    assert len(quote(worst_case, safe="")) == 12 * _MAX_UOM_PATH_VALUE_LENGTH

    over = "\U0001f600" * (_MAX_UOM_PATH_VALUE_LENGTH + 1)
    with pytest.raises(ValueError):
        _reject_over_long_path_value("property_value", over)


def test_the_bound_is_its_wire_budget_divided_by_the_worst_case_expansion():
    """The number itself, pinned against ADR 0150's derivation rather than
    restated beside it: a 3 KiB share of the request line, divided by the twelve
    characters one input character can become. Every other test here floats with
    the constant, so without this one the bound could be changed to any value
    and the suite would still pass -- and the derivation is the decision.

    One assertion, not two: the equality below already admits exactly 256, so a
    second `== 256` beside it could never fail on its own.
    """
    assert _MAX_UOM_PATH_VALUE_LENGTH * 12 == 3 * 1024


# ---------------------------------------------------------------------------
# The LPM job operation segment (ADR 0151)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        # The `?`/`#` retargeting pair issue #809 carries, a second segment, a
        # traversal, the case a character grammar would have accepted (`PowerOff`
        # is real and not submitted here), casing, empty, and a CRLF value httpx
        # will not build a URL from (ADR 0148) -- refused here first, and so as a
        # `ValueError` rather than the waist's `HMCError`.
        "Migrate?group=None",
        "Migrate#/rest/api/web/HmcUser/root",
        "Migrate/extra",
        "../../web/HmcUser/root",
        "PowerOff",
        "migrate",
        "",
        "Migrate\r\nX-Evil: 1",
    ],
)
def test_an_unlisted_lpm_operation_is_refused_before_any_request(operation):
    """Refused by membership, before `submit_job` builds anything. Asserted
    against the transport too: a refusal that still built a request would leave
    the retargeted path in the HMC's audit log."""
    client = _client()
    sent: list[str] = []

    def _forbidden(*args, **kwargs):
        sent.append("request")
        raise AssertionError("a refused LPM operation reached the transport")

    client._http.build_request = _forbidden  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="^LPM job operation must be one of: "):
        asyncio.run(client._lpar_job(UUID_A, operation, "<JobRequest/>"))
    assert sent == []


def test_the_lpm_operation_refusal_names_the_permitted_set_not_the_value():
    """Equality, not a substring: dropping an operation from the set without
    dropping its call site cannot pass here. The transport is patched because
    unfixed this call would otherwise attempt a real request (ADR 0151)."""
    client = _client()

    def _forbidden(*args, **kwargs):
        raise AssertionError("a refused LPM operation reached the transport")

    client._http.build_request = _forbidden  # type: ignore[method-assign]

    with pytest.raises(ValueError) as error:
        asyncio.run(client._lpar_job(UUID_A, "secret-partition-name", "<x/>"))

    message = str(error.value)
    assert message == (
        "LPM job operation must be one of: Migrate, MigrateAbort, "
        "MigrateRecover, MigrateValidate, RemoteRestart"
    )
    assert "secret-partition-name" not in message
