"""Installer media for ``hmc_provision_lpar``: inputs, checks and producer binding.

The install input types and their field checks are shared with ``hmc_plan_lpar``.
``bind_prepared`` and ``bind_built`` check an iso-chain-loader ``iso-chain-media-v1``
producer result against the request (ADR 0191 Decision 3, ADR 0324); the media steps
that use it run in ``provision_operation`` (ADR 0319). Nothing here writes to the HMC, and no message
echoes a key, URL or producer-result value.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from ...client.core import HMCClient
from ...errors import HMCError
from ...xmlutil import leaf_text
from ..storage.resources import require_allowlisted_iso_url

InstallProfile = Literal["ubuntu-26.04.1", "rocky-9.8"]
INSTALL_PROFILES: tuple[str, ...] = ("ubuntu-26.04.1", "rocky-9.8")
Boot = Literal["immediate", "deferred"]

MAX_ROUTES = 16
MAX_DNS = 3
MAX_KEYS = 16
MAX_KEY_LENGTH = 8192
MAX_PRODUCER_RESULT_BYTES = 64 * 1024
_DEFAULT_ROUTE = ipaddress.IPv4Network("0.0.0.0/0")
_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
_LOGIN_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
# The producer's ``lpar`` identifier, which becomes the installer hostname (ADR 0324 D6).
_PRODUCER_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


@dataclass(frozen=True)
class InstallRoute:
    """One IPv4 route the installed guest gets."""

    destination: str = field(
        metadata={"description": "IPv4 network in CIDR form; 0.0.0.0/0 is default."}
    )
    gateway: str = field(
        metadata={"description": "IPv4 gateway inside the address's network."}
    )


@dataclass(frozen=True)
class InstallNetwork:
    """The installed guest's static IPv4 configuration."""

    address: str = field(
        metadata={"description": "IPv4 interface address in CIDR form."}
    )
    routes: tuple[InstallRoute, ...] = field(
        metadata={"description": "1 to 16 routes, exactly one of them default."}
    )
    dns: tuple[str, ...] = field(
        default=(), metadata={"description": "0 to 3 IPv4 DNS servers."}
    )


@dataclass(frozen=True)
class InstallMedia:
    """Where the installer media comes from (ADR 0191)."""

    mode: Literal["built", "prepared"] = field(
        metadata={"description": "built by hmcpctl, or prepared by a producer."}
    )
    url: str | None = field(
        default=None,
        metadata={"description": "Prepared media's http(s) URL; prepared only."},
    )
    producer_result: dict[str, Any] | None = field(
        default=None,
        metadata={
            "description": "The producer's JSON result, at most 64 KiB; prepared only."
        },
    )


@dataclass(frozen=True)
class LparInstall:
    """The operating-system installation provisioning would start (ADR 0194)."""

    profile: InstallProfile = field(metadata={"description": "Install profile."})
    network: InstallNetwork = field(
        metadata={"description": "Static network configuration."}
    )
    media: InstallMedia = field(metadata={"description": "Installer media."})
    ssh_authorized_keys: tuple[str, ...] = field(
        default=(),
        metadata={"description": "1 to 16 single-line keys; built media only."},
    )
    login_user: str | None = field(
        default=None, metadata={"description": "Guest login user; built media only."}
    )


def _ipv4_address(text: str, label: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        raise ValueError(f"{label}: must be an IPv4 address") from None


def _check_network(network: InstallNetwork) -> None:
    label = "install.network"
    try:
        interface = ipaddress.IPv4Interface(network.address)
    except ValueError:
        interface = None
    if (
        interface is None
        or "/" not in network.address
        or not 1 <= interface.network.prefixlen <= 32
    ):
        raise ValueError(
            f"{label}.address: must be an IPv4 interface in CIDR form, prefix 1 to 32"
        )
    _check_routes(network, interface.network)
    if len(network.dns) > MAX_DNS:
        raise ValueError(f"{label}.dns: pass at most {MAX_DNS} servers")
    for index, server in enumerate(network.dns):
        _ipv4_address(server, f"{label}.dns[{index}]")


def _check_routes(network: InstallNetwork, subnet: ipaddress.IPv4Network) -> None:
    label = "install.network"
    if not 1 <= len(network.routes) <= MAX_ROUTES:
        raise ValueError(f"{label}.routes: pass 1 to {MAX_ROUTES} routes")
    defaults = 0
    for index, route in enumerate(network.routes):
        try:
            destination = ipaddress.IPv4Network(route.destination, strict=True)
        except ValueError:
            raise ValueError(
                f"{label}.routes[{index}].destination: must be an IPv4 network in "
                "CIDR form"
            ) from None
        defaults += destination == _DEFAULT_ROUTE
        gateway = _ipv4_address(route.gateway, f"{label}.routes[{index}].gateway")
        if gateway not in subnet:
            raise ValueError(
                f"{label}.routes[{index}].gateway: must be inside "
                f"{network.address}'s network"
            )
    if defaults != 1:
        raise ValueError(f"{label}.routes: exactly one route must be 0.0.0.0/0")


def _check_login(install: LparInstall) -> None:
    keys = install.ssh_authorized_keys
    if not 1 <= len(keys) <= MAX_KEYS:
        raise ValueError(
            f"install.ssh_authorized_keys: built media needs 1 to {MAX_KEYS} keys"
        )
    for index, key in enumerate(keys):
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= MAX_KEY_LENGTH
            # Rejects every line break (U+2028 and NEL included) and control character.
            or not key.isprintable()
        ):
            raise ValueError(
                f"install.ssh_authorized_keys[{index}]: must be one line of 1 to "
                f"{MAX_KEY_LENGTH} characters"
            )
    if install.login_user is None or not _LOGIN_USER.fullmatch(install.login_user):
        raise ValueError(
            "install.login_user: built media needs 1 to 32 characters matching "
            "[a-z_][a-z0-9_-]*"
        )


def _check_prepared(install: LparInstall) -> None:
    if install.ssh_authorized_keys:
        raise ValueError(
            "install.ssh_authorized_keys: prepared media carries its own keys; omit it"
        )
    if install.login_user is not None:
        raise ValueError(
            "install.login_user: prepared media carries its own user; omit it"
        )
    media = install.media
    try:
        scheme = urlparse(media.url).scheme if media.url is not None else None
    except ValueError:  # its message quotes the URL's host
        scheme = None
    if scheme not in ("http", "https"):
        raise ValueError("install.media.url: prepared media needs an http or https URL")
    result = media.producer_result
    if result is None:
        raise ValueError(
            "install.media.producer_result: prepared media needs the producer's "
            "JSON object"
        )
    size = len(json.dumps(result, separators=(",", ":")).encode())
    if size > MAX_PRODUCER_RESULT_BYTES:
        raise ValueError(
            f"install.media.producer_result: {size} bytes exceeds "
            f"{MAX_PRODUCER_RESULT_BYTES}"
        )


def _check_media(install: LparInstall) -> None:
    if install.media.mode == "built":
        if install.media.url is not None or install.media.producer_result is not None:
            raise ValueError(
                "install.media.url: built media takes no url or producer_result"
            )
        _check_login(install)
    elif install.media.mode == "prepared":
        _check_prepared(install)
    else:
        raise ValueError("install.media.mode: must be built or prepared")


def check_install(install: LparInstall) -> None:
    """Refuse an install whose profile, network or media fields are malformed."""
    if install.profile not in INSTALL_PROFILES:
        raise ValueError(f"install.profile: must be one of {list(INSTALL_PROFILES)}")
    _check_network(install.network)
    _check_media(install)


def check_mac(mac: str | None, install: LparInstall | None) -> None:
    """Refuse a pinned MAC without prepared media, or prepared media without one."""
    prepared = install is not None and install.media.mode == "prepared"
    if mac is None:
        if prepared:
            raise ValueError("adapters.mac: prepared media requires the adapter MAC")
        return
    if not prepared:
        raise ValueError("adapters.mac: accepted only with prepared install media")
    if not _MAC.fullmatch(mac) or int(mac[:2], 16) & 1:
        raise ValueError(
            "adapters.mac: must be lower-case colon form, such as "
            "02:00:00:00:00:01, with no group bit"
        )


def check_built_name(name: str, install: LparInstall | None) -> None:
    """Refuse built media for a partition name the producer cannot use as ``lpar``."""
    if install is None or install.media.mode != "built":
        return
    if not _PRODUCER_NAME.fullmatch(name) or name.endswith("-"):
        raise ValueError(
            "name: built media needs a partition name of 1 to 32 characters matching "
            "[a-z][a-z0-9-]*, not ending in -, because it becomes the installer "
            "hostname; rename the partition or pass prepared media"
        )


PRODUCER_FORMAT = "iso-chain-media-v1"
TARGET_FORMAT = "iso-chain-target-v1"
MAX_MEDIA_URL = 2048
_PRODUCER_KEYS = frozenset(
    {
        "format",
        "iso_sha256",
        "manifest_sha256",
        "iso_size",
        "distribution",
        "release",
        "architecture",
        "mac",
        "network",
    }
)
# Present only in a built result (ADR 0191 D3).
_BUILT_KEYS = frozenset({"operation_binding", "url"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEY_NAME = re.compile(r"[a-z_]{1,32}")
_LUN = re.compile(r"0x([0-9a-fA-F]{1,16})")
# What the VIOS copy and the guest are not checked for (ADR 0191 D5, H1 spec).
UNVERIFIED: tuple[str, ...] = (
    "that the VIOS copy matches the uploaded image: the media is not read back",
    "that the guest can reach the installer source",
)


class MediaMismatch(ValueError):
    """A producer result does not bind the request; names the field, never its value."""


@dataclass(frozen=True)
class MediaBinding:
    """What a bound producer result fixes for the upload and the adapter."""

    url: str
    iso_sha256: str
    iso_size: int
    mac: str


def _mismatch(name: str, why: str) -> MediaMismatch:
    return MediaMismatch(f"install.media.producer_result.{name}: {why}")


def _key_label(key: str) -> str:
    """A result key as a message may name it: a builder chooses unknown keys."""
    return key if _KEY_NAME.fullmatch(key) else "<unnamed>"


def _check_result(
    result: Mapping[str, Any],
    install: LparInstall,
    mac: str | None,
    allowed: frozenset[str],
) -> None:
    """Raise ``MediaMismatch`` naming the first field of *result* that does not bind."""
    keys = set(result)
    if unknown := sorted(keys - allowed):
        raise _mismatch(_key_label(unknown[0]), f"is not a {PRODUCER_FORMAT} field")
    if missing := sorted(allowed - keys):
        raise _mismatch(missing[0], "is missing")
    if result["format"] != PRODUCER_FORMAT:
        raise _mismatch("format", f"must be {PRODUCER_FORMAT}")
    for name in ("iso_sha256", "manifest_sha256"):
        value = result[name]
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise _mismatch(name, "must be 64 lower-case hex digits")
    size = result["iso_size"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise _mismatch("iso_size", "must be a positive integer of bytes")
    if f"{result['distribution']}-{result['release']}" != install.profile:
        raise _mismatch(
            "distribution", "distribution and release differ from install.profile"
        )
    if result["architecture"] != "ppc64le":
        raise _mismatch("architecture", "must be ppc64le")
    if mac is None or result["mac"] != mac:
        raise _mismatch("mac", "differs from adapters.mac")
    if result["network"] != json.loads(json.dumps(asdict(install.network))):
        raise _mismatch("network", "differs from install.network")


def bind_prepared(install: LparInstall, mac: str | None) -> MediaBinding:
    """Check a prepared producer result field by field against the request.

    Raises ``MediaMismatch`` naming the first field that does not bind.
    """
    media = install.media
    result = media.producer_result
    if media.mode != "prepared" or media.url is None or not isinstance(result, dict):
        raise _mismatch("format", f"prepared media needs a {PRODUCER_FORMAT} object")
    _check_result(result, install, mac, _PRODUCER_KEYS)
    assert mac is not None  # _check_result refuses a None mac
    return MediaBinding(media.url, result["iso_sha256"], result["iso_size"], mac)


def target_request(
    install: LparInstall, name: str, mac: str, operation_id: str
) -> bytes:
    """The ``iso-chain-target-v1`` request the build entry reads (ADR 0324 D2)."""
    request = {
        "format": TARGET_FORMAT,
        "profile": install.profile,
        "lpar": name,
        "mac": mac,
        "network": asdict(install.network),
        "operation_binding": operation_id,
        "ssh_authorized_keys": list(install.ssh_authorized_keys),
        "login_user": install.login_user,
    }
    return json.dumps(request, sort_keys=True, separators=(",", ":")).encode()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _mismatch(_key_label(key), "appears twice")
        result[key] = value
    return result


def parse_result(output: bytes) -> dict[str, Any]:
    """The one JSON object a build entry printed, duplicate keys refused (ADR 0324 D4)."""
    if len(output) > MAX_PRODUCER_RESULT_BYTES:
        raise _mismatch("format", f"exceeds {MAX_PRODUCER_RESULT_BYTES} bytes")
    try:
        result = json.loads(output.decode("utf-8"), object_pairs_hook=_unique)
    except MediaMismatch:
        raise
    except (ValueError, RecursionError):
        raise _mismatch("format", "is not one UTF-8 JSON object") from None
    if not isinstance(result, dict):
        raise _mismatch("format", f"must be one {PRODUCER_FORMAT} object")
    return result


def _check_url(url: object, allowlist: tuple[tuple[str, int | None], ...]) -> str:
    # Bounded as journaled (JSON-escaped), so the build identity fits its 4 KiB cap.
    if not isinstance(url, str) or not 1 <= len(json.dumps(url)) - 2 <= MAX_MEDIA_URL:
        raise _mismatch(
            "url", f"must be a string of 1 to {MAX_MEDIA_URL} JSON-escaped characters"
        )
    try:
        scheme = urlparse(url).scheme
    except ValueError:  # its message quotes the URL's host
        scheme = None
    if scheme not in ("http", "https"):
        raise _mismatch("url", "must be an http or https URL")
    try:
        require_allowlisted_iso_url(url, allowlist)
    except (ValueError, HMCError):
        raise _mismatch(
            "url", "its origin (host and port) is not on HMC_ISO_URL_ALLOWLIST"
        ) from None
    return url


def bind_built(
    install: LparInstall,
    result: Mapping[str, Any],
    mac: str,
    operation_id: str,
    allowlist: tuple[tuple[str, int | None], ...],
) -> MediaBinding:
    """Check a build entry's result against the request, the adapter's actual MAC
    and the operation (ADR 0191 D3, ADR 0324 D4).

    Its URL is checked here as well as before the upload, so the build effect never
    records an unlisted URL. Raises ``MediaMismatch`` naming the first field that
    does not bind.
    """
    _check_result(result, install, mac, _PRODUCER_KEYS | _BUILT_KEYS)
    if result["operation_binding"] != operation_id:
        raise _mismatch("operation_binding", "differs from this operation")
    url = _check_url(result["url"], allowlist)
    return MediaBinding(url, result["iso_sha256"], result["iso_size"], mac)


def media_name_for(operation_id: str) -> str:
    """The repository name an operation's media gets (H1 spec)."""
    return f"hmcpctl_{operation_id[:12]}.iso"


def _child(node: object, name: str) -> object:
    return node.get(name) if isinstance(node, Mapping) else None


def device_path(mapping: Mapping[str, Any]) -> str | None:
    """The Open Firmware path of a vSCSI mapping's client device, or None.

    ``/vdevice/v-scsi@<0x30000000 + client slot>/disk@<LUN>``; the firmware names a
    virtual optical device ``disk@`` too (docs/recipes/lpar-iso-install.md, section 5).
    """
    resource = mapping.get("Resource", mapping)
    slot_text = leaf_text(
        _child(_child(resource, "ClientAdapter"), "VirtualSlotNumber")
    )
    target = _child(resource, "TargetDevice")
    luns = [
        leaf_text(_child(device, "LogicalUnitAddress"))
        for device in (target.values() if isinstance(target, Mapping) else ())
        if isinstance(device, Mapping)
    ]
    lun = (
        _LUN.fullmatch(luns[0].strip())
        if len(luns) == 1 and isinstance(luns[0], str)
        else None
    )
    if not isinstance(slot_text, str) or not slot_text.strip().isdigit() or lun is None:
        return None
    slot = int(slot_text.strip())
    return f"/vdevice/v-scsi@{0x30000000 + slot:x}/disk@{lun.group(1).lower()}"


def client_slot(path: str) -> int:
    """The client virtual slot a ``device_path`` names."""
    return int(path.split("@", 2)[1].split("/", 1)[0], 16) - 0x30000000


async def repository_group(hmc: HMCClient, vios_uuid: str) -> str:
    """The UUID of the one volume group on *vios_uuid* holding a media repository."""
    groups = [
        str(group.get("UUID") or "").lower()
        for group in await hmc.list_volume_groups(vios_uuid)
        if _child(
            _child(group.get("Resource"), "MediaRepositories"), "VirtualMediaRepository"
        )
        is not None
    ]
    if len(groups) != 1 or not groups[0]:
        raise ValueError(
            f"VIOS {vios_uuid} must hold exactly one media repository for install media; "
            f"it holds {len(groups)}. hmcpctl does not create one: create it with "
            "create-media-repo, or name a VIOS that has one"
        )
    return groups[0]
