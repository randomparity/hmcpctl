"""Virtual-media scenarios and their local HTTP fixture (ST16-22, #1347).

ST16 finds the VIOS's one media repository, whoever created it, and creates one
only when the VIOS has none. ST18 uploads the configured ISO when it exists, and
ST19 round-trips a blank medium through create, mount, unmount and delete. Every
observation of a media or mapping operation is recorded through `record_verified`.

The arm removes only media this run created: each name carries a per-run tag and
is stored in the run's artifacts before the call that creates it. It never
removes an adapter: any adapter or mapping a round trip leaves different from its
baseline is a manual-recovery row.
"""

from __future__ import annotations

import functools
import http.server
import os
import re
import secrets
import shlex
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from fastmcp import Client

from hmcpctl.client.client_storage import storage_mapping_id
from hmcpctl.config import env_var_value
from hmcpctl.documents.boot import join_boot_device_paths
from hmcpctl.ssh.commands import HMC_NO_RESULTS, build_filter

from .observation import Assertion, CallFailure, ExpectedOutcome
from .results import entries
from .results import resource as get_resource
from .storage import configured_vg_uuid, resolve_configured_volume_group

_MEDIA_NAME_COLLISION = ExpectedOutcome(
    operation="media.upload_iso",
    variant="reupload-same-name",
    transient=True,
    reason="same-name re-upload refused as expected (upload_iso has no dedup status)",
    error_codes=frozenset({"already exists in repository"}),
)
_ALREADY_POWERED_OFF = ExpectedOutcome(
    operation="lpar.power_off",
    variant="pre-boot-power-off",
    transient=True,
    reason="lp3 already powered off (expected)",
    error_codes=frozenset({"already", "not activated", "powered off", "not running"}),
)
_REPOSITORY_ALREADY_GONE = ExpectedOutcome(
    operation="media.delete_repository",
    variant="repository-removal",
    transient=True,
    reason="repository already gone (expected on re-run)",
    error_codes=frozenset({"not found", "does not exist", "no repository", "no media"}),
)
_DELETE_REFUSED_WHILE_MOUNTED = ExpectedOutcome(
    operation="media.delete",
    variant="delete-while-mounted",
    transient=True,
    reason="delete refused while mounted (expected)",
    error_codes=frozenset({"is mounted on"}),
)

if TYPE_CHECKING:
    from live_test_runner import LiveTestConfig, RunState


REPOSITORY_SCENARIO = "st16-repository-read"
UPLOAD_SCENARIO = "st18-iso-upload"
ROUND_TRIP_SCENARIO = "st19-optical-round-trip"
MAPPINGS_SCENARIO = "st21-mapping-inventory"
#: The HMC media-name pattern is ``[A-Za-z0-9_.]``, so the prefix has no hyphen.
BLANK_PREFIX = "hmcpctl_live_"
BLANK_SIZE_MIB = 1024
_READ_FAILED = Assertion("read-failed", False)
_NOT_ACTIVATED = "not activated"

_NOT_OWNED = (
    "the run did not create the repository; the repository lifecycle needs a VIOS "
    "with no media repository (gap)"
)
_REPOSITORY_EXISTS = (
    "a media repository already exists and a VIOS holds one; create and delete "
    "need a VIOS with no media repository (gap)"
)
_NO_REPOSITORY = "no media repository resolved in ST16"
_EARLIER_MEDIUM = (
    "a medium from an earlier invocation is still recorded; run subtask 22 and the "
    "recovery check first"
)


def _owns_repository(state: RunState) -> bool:
    """Whether this run created the media repository in the configured volume group."""
    vg = configured_vg_uuid(state)
    return (
        state.artifacts.vmedia_repo_created
        and vg is not None
        and state.artifacts.vmedia_vg_uuid == vg
    )


def _run_tag() -> str:
    return secrets.token_hex(4)


def iso_media_name(config: LiveTestConfig) -> str:
    """The configured ISO name with a per-run tag before its suffix."""
    name = PurePosixPath(config.iso_media_name)
    return f"{name.stem}_{_run_tag()}{name.suffix}"


def _protected_reason(config: LiveTestConfig) -> str:
    return (
        f"the test partition {config.lp3_name!r} is protected by operator config "
        "(LIVE_TEST_PROTECTED_LPAR_NAMES); the blank-medium round trip and the boot "
        "need an unprotected test partition (gap)"
    )


def _no_iso_reason(config: LiveTestConfig) -> str:
    return (
        f"no ISO at LIVE_TEST_ISO_PATH ({config.iso_path}); the arm never fetches "
        "media (gap)"
    )


def is_run_media_name(name: object, iso_media_name: str) -> bool:
    """Whether *name* has the shape only this arm gives the media it creates.

    A restored document can name a medium the arm did not create: the arm before
    #1347 recorded the first listed medium as its ISO. Only a tagged name is
    ever unmounted or deleted.
    """
    if not isinstance(name, str):
        return False
    iso = PurePosixPath(iso_media_name)
    return bool(
        re.fullmatch(rf"{re.escape(BLANK_PREFIX)}[0-9a-f]{{8}}", name)
        or (
            iso.stem
            and re.fullmatch(
                rf"{re.escape(iso.stem)}_[0-9a-f]{{8}}{re.escape(iso.suffix)}", name
            )
        )
    )


def run_media_names(state: RunState) -> set[str]:
    """The media this run created and may remove."""
    artifacts = state.artifacts
    return {
        name
        for name in (artifacts.vmedia_blank_name, artifacts.vmedia_iso_name)
        if name is not None and is_run_media_name(name, state.config.iso_media_name)
    }


def repository_fields(data: object) -> tuple[str | None, Decimal | None]:
    """The repository's name and size in GiB from a ``hmc_get_media_repository`` entry."""
    group = get_resource(data) if isinstance(data, Mapping) else {}
    repositories = group.get("MediaRepositories")
    repository = (
        repositories.get("VirtualMediaRepository")
        if isinstance(repositories, Mapping)
        else None
    )
    if not isinstance(repository, Mapping):
        return None, None
    name = repository.get("RepositoryName")
    try:
        size = Decimal(str(repository.get("RepositorySize")))
    except InvalidOperation:
        size = None
    return (name if isinstance(name, str) and name else None), (
        size if size is not None and size.is_finite() else None
    )


def media_sizes(data: object) -> dict[str, Any] | None:
    """``hmc_list_optical_media`` as name to size in MiB; None when an entry has no name."""
    if not isinstance(data, list):
        return None
    sizes: dict[str, Any] = {}
    for entry in data:
        name = entry.get("name") if isinstance(entry, Mapping) else None
        if not isinstance(name, str) or not name:
            return None
        sizes[name] = entry.get("size_mib")
    return sizes


def _mapping_row(entry: object) -> tuple[object, ...] | None:
    if not isinstance(entry, Mapping):
        return None
    return tuple(
        entry.get(key) for key in ("id", "lpar_uuid", "backing_kind", "backing_name")
    )


def storage_rows(data: object) -> frozenset[tuple[object, ...]] | None:
    """``hmc_list_storage_mappings`` as comparable rows; None when malformed."""
    if not isinstance(data, list):
        return None
    rows = [_mapping_row(entry) for entry in data]
    if None in rows:
        return None
    return frozenset(row for row in rows if row is not None)


def _mapping_identity(mapping: object) -> tuple[str, str] | None:
    """Read the LPAR and media name that address one optical mapping.

    ``hmc_unmount_optical_media`` resolves a mapping from the pair, so an entry
    missing either half cannot be unmounted and is skipped rather than guessed at.
    """
    if not isinstance(mapping, dict):
        return None
    storage = mapping.get("Storage")
    optical = storage.get("VirtualOpticalMedia") if isinstance(storage, dict) else None
    media_name = optical.get("MediaName") if isinstance(optical, dict) else None
    partition = mapping.get("AssociatedLogicalPartition")
    href = partition.get("href") if isinstance(partition, dict) else None
    lpar = urlparse(href).path.rsplit("/", 1)[-1] if isinstance(href, str) else None
    if not isinstance(media_name, str) or not media_name or not lpar:
        return None
    return lpar, media_name


def scsi_adapter_listing(system_name: str, attribute: str, value: object) -> str:
    """The vSCSI adapter rows of the partitions the one filter pair selects (#1237)."""
    return (
        f"lshwres -r virtualio --rsubtype scsi -m {shlex.quote(system_name)} "
        f"--level lpar --filter {shlex.quote(build_filter([(attribute, value)]))} "
        "-F slot_num,remote_lpar_name,remote_slot_num"
    )


def adapter_rows(listing: object) -> frozenset[str] | None:
    """An adapter listing as a set of lines; None when it is not text."""
    if not isinstance(listing, str):
        return None
    text = listing.strip()
    if text == HMC_NO_RESULTS:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


async def _discover_vmedia_prerequisites(client: Client, state: RunState) -> bool:
    """Resolve the VIOS and test-partition identities required by ST16."""
    config = state.config
    artifacts = state.artifacts
    if not artifacts.vios_uuid:
        st, data = await state.call(
            client, "hmc_list_vios", system_name_or_uuid=config.system_name
        )
        state.record(16, "hmc_list_vios", st, data)
        if st == "PASS":
            for e in entries(data):
                resource = get_resource(e)
                uuid = e.get("UUID") or e.get("uuid")
                pid = resource.get("PartitionID") or resource.get("partition_id")
                if uuid:
                    artifacts.vios_uuid = cast(str, uuid)
                    artifacts.vios_partition_id = (
                        int(cast(str | int, pid)) if pid is not None else None
                    )
                    break
    else:
        print(f"  ℹ  vios_uuid already set: {artifacts.vios_uuid}")

    # lp3 UUID (needed by ST20 boot-order tools which require UUID not name)
    if not artifacts.lp3_uuid:
        st, data = await state.call(
            client, "hmc_get_lpar", lpar_name_or_uuid=config.lp3_name
        )
        state.record(16, "hmc_get_lpar (lp3 uuid)", st, data)
        if st == "PASS" and isinstance(data, dict):
            artifacts.lp3_uuid = data.get("uuid") or data.get("UUID")
    else:
        print(f"  ℹ  lp3_uuid already set: {artifacts.lp3_uuid}")

    if not artifacts.vios_uuid:
        for name in [
            "hmc_list_volume_groups",
            "hmc_create_media_repository",
            "hmc_get_media_repository",
        ]:
            state.skip(16, name, "no VIOS UUID resolved")
        return False

    return True


async def _repository_holders(
    client: Client, state: RunState, groups: list[Mapping[str, object]]
) -> list[tuple[str, object]] | None:
    """Every listed volume group holding a repository; None when a read failed."""
    holders: list[tuple[str, object]] = []
    for group in groups:
        uuid, name = group.get("uuid"), group.get("name")
        if not isinstance(uuid, str) or not uuid:
            continue
        st, data = await state.call(
            client,
            "hmc_get_media_repository",
            vios_name_or_uuid=state.artifacts.vios_uuid,
            vg_uuid=uuid,
        )
        if st != "PASS":
            state.record_verified(
                16,
                f"hmc_get_media_repository ({name})",
                operation="media.get_repository",
                scenario=REPOSITORY_SCENARIO,
                assertions=[_READ_FAILED],
                cleanup="not-required",
                data=data,
            )
            return None
        if data:
            holders.append((uuid, data))
        else:
            state.record(16, f"hmc_get_media_repository ({name}, none)", st, data)
    return holders


def _drop_restored_ownership(
    state: RunState, holders: list[tuple[str, object]]
) -> None:
    """Never claim an existing repository; say so when a document had claimed it."""
    artifacts = state.artifacts
    if artifacts.vmedia_repo_created:
        groups = ", ".join(uuid for uuid, _ in holders)
        state.record(
            16,
            "hmc_get_media_repository (restored ownership)",
            "FAIL",
            None,
            "MANUAL RECOVERY REQUIRED: a restored document recorded the repository "
            f"as this arm's; it is no longer treated as owned. If an earlier run "
            f"created it, delete it by hand (hmcpctl storage delete-media-repo "
            f"{shlex.quote(str(artifacts.vios_uuid))} --vg <group: {groups}> --system "
            f"{shlex.quote(state.config.system_name)})",
        )
    artifacts.vmedia_repo_created = False


def _record_repository_read(state: RunState, data: object) -> None:
    name, size = repository_fields(data)
    state.record_verified(
        16,
        "hmc_get_media_repository",
        operation="media.get_repository",
        scenario=REPOSITORY_SCENARIO,
        assertions=[
            Assertion("repository-named", name is not None),
            Assertion("repository-size-positive", size is not None and size > 0),
        ],
        cleanup="not-required",
        data=data,
    )


async def _create_vmedia_repository(
    client: Client, state: RunState, data: object, repo_size_mib: int
) -> None:
    """Create the repository in the configured group, which must have the space."""
    dependents = ("hmc_create_media_repository", "hmc_get_media_repository")
    group = resolve_configured_volume_group(state, 16, data, dependents)
    if group is None:
        return
    free_mib = group.free_space_mib
    print(f"  VG UUID: {group.uuid}  free space: {free_mib} MiB")
    if free_mib is not None and free_mib < repo_size_mib:
        for name in dependents:
            state.skip(
                16,
                name,
                f"insufficient free space: {free_mib} MiB < {repo_size_mib} MiB",
            )
        return
    artifacts = state.artifacts
    st, created = await state.call(
        client,
        "hmc_create_media_repository",
        vios_name_or_uuid=artifacts.vios_uuid,
        vg_uuid=group.uuid,
        size_mib=repo_size_mib,
    )
    state.record(16, "hmc_create_media_repository", st, created)
    st, read = await state.call(
        client,
        "hmc_get_media_repository",
        vios_name_or_uuid=artifacts.vios_uuid,
        vg_uuid=group.uuid,
    )
    state.record(16, "hmc_get_media_repository (post-create)", st, read)
    if st == "PASS" and read:
        # Owned once it reads back, whatever the create call returned.
        artifacts.vmedia_repo_created = True
        artifacts.vmedia_vg_uuid = group.uuid
        print("  ✅ Repository created — vmedia_repo_created=True")


async def vmedia_bootstrap_and_create_repo(client: Client, state: RunState) -> None:
    print("\n=== ST16: Media Repository Discovery (create only when absent) ===")
    if not await _discover_vmedia_prerequisites(client, state):
        return
    # Re-derived every run: a restored document's group may no longer hold it.
    state.artifacts.vmedia_vg_uuid = None
    st, data = await state.call(
        client, "hmc_list_volume_groups", vios_name_or_uuid=state.artifacts.vios_uuid
    )
    state.record(16, "hmc_list_volume_groups", st, data)
    if st != "PASS":
        for name in ("hmc_create_media_repository", "hmc_get_media_repository"):
            state.skip(16, name, "volume group listing failed")
        return
    holders = await _repository_holders(client, state, entries(data))
    if holders:
        # A repository found before this invocation's own create is never the run's,
        # whatever a restored document recorded.
        _drop_restored_ownership(state, holders)
    if holders is None:
        state.artifacts.vmedia_repo_created = False
        state.skip(16, "hmc_create_media_repository", "a repository read failed")
    elif len(holders) > 1:
        for name in ("hmc_create_media_repository", "hmc_get_media_repository"):
            state.skip(16, name, f"{len(holders)} groups report a repository")
    elif holders:
        uuid, repository = holders[0]
        _record_repository_read(state, repository)
        state.artifacts.vmedia_vg_uuid = uuid
        state.skip(16, "hmc_create_media_repository", _REPOSITORY_EXISTS)
    else:
        await _create_vmedia_repository(
            client, state, data, state.config.vmedia_repository_size_mib
        )


# ---------------------------------------------------------------------------
# ST17 — Short Repository Lifecycle (no ISO)
# ---------------------------------------------------------------------------


async def vmedia_short_repo_lifecycle(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts
    print("\n=== ST17: Short Repository Lifecycle (no ISO) ===")

    if not _owns_repository(state):
        for name in [
            "hmc_delete_media_repository (main)",
            "hmc_create_media_repository (small)",
            "hmc_get_media_repository (small)",
            "hmc_list_optical_media (empty)",
            "hmc_delete_media_repository (small)",
            "hmc_get_media_repository (confirm gone)",
            "hmc_create_media_repository (restore main)",
        ]:
            state.skip(17, name, _NOT_OWNED)
        return

    vios = artifacts.vios_uuid
    vg = configured_vg_uuid(state)

    # Step 2 — Delete the ST16 main repository
    st, data = await state.call(
        client,
        "hmc_delete_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
    )
    state.record(17, "hmc_delete_media_repository (main)", st, data)

    # Step 3 — Create small repository
    st, data = await state.call(
        client,
        "hmc_create_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
        size_mib=config.vmedia_short_repository_size_mib,
    )
    state.record(17, "hmc_create_media_repository (small)", st, data)

    # Step 4 — Verify small repository
    st, data = await state.call(
        client,
        "hmc_get_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
    )
    state.record(17, "hmc_get_media_repository (small)", st, data)

    # Step 5 — List optical media (must be empty)
    st, data = await state.call(
        client,
        "hmc_list_optical_media",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
    )
    state.record(17, "hmc_list_optical_media (empty)", st, data)

    # Step 6 — Delete small repository
    st, data = await state.call(
        client,
        "hmc_delete_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
    )
    state.record(17, "hmc_delete_media_repository (small)", st, data)

    # Step 7 — Confirm gone
    st, data = await state.call(
        client,
        "hmc_get_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
    )
    state.record(17, "hmc_get_media_repository (confirm gone)", st, data)

    # Step 8 — Re-create main repository for subsequent sub-tasks
    st, data = await state.call(
        client,
        "hmc_create_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
        size_mib=config.vmedia_repository_size_mib,
    )
    state.record(17, "hmc_create_media_repository (restore main)", st, data)
    if st == "PASS":
        artifacts.vmedia_repo_created = True
    else:
        artifacts.vmedia_repo_created = False
        artifacts.vmedia_vg_uuid = None
        print("  ⚠  Failed to restore main repository — ST18–ST22 may be skipped")


# ---------------------------------------------------------------------------
# ST18 — ISO Upload via HTTP
# ---------------------------------------------------------------------------


class IsoHttpServer:
    """Invocation-owned HTTP fixture for virtual-media ISO uploads."""

    def __init__(self) -> None:
        self._server: http.server.HTTPServer | None = None

    def start(self, config: LiveTestConfig) -> None:
        """Start serving the configured ISO directory once for this invocation."""
        _allow_iso_host(config)
        if self._server is not None:
            return
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler,
            directory=str(Path(config.iso_path).parent),
        )
        self._server = http.server.HTTPServer(
            (config.iso_bind_host, config.iso_http_port), handler
        )
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop serving and release the listening socket."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None


def _allow_iso_host(config: LiveTestConfig) -> None:
    """Put this runner's own ISO server on ``HMC_ISO_URL_ALLOWLIST``.

    ADR 0050 made ``hmc_upload_iso`` refuse every URL whose host an operator has
    not named, including when nothing is named at all, so the runner has to
    configure the allowlist for its own run exactly as an operator would. The
    entry is appended to whatever the environment or ``.env`` already carries
    rather than replacing it, and it names the port as well as the host, so this
    permits the one server started below and no other loopback service.
    """
    # env_var_value reads whatever casing the operator exported, because that is
    # the one `HMCConfig` will resolve — an exact-case read dropped a case
    # variant's entries from the merged allowlist (#543).
    name = "HMC_ISO_URL_ALLOWLIST"
    configured = env_var_value(name) or ""
    entries = [entry.strip() for entry in configured.split(",") if entry.strip()]
    if config.iso_host in entries:
        return
    entries.append(config.iso_host)
    # The merged value has to be the one that reaches the field, so every other
    # casing goes first. Assigning to a key that already exists updates it in
    # place rather than moving it, so a variant inserted after the canonical name
    # would stay last in `os.environ` order and stay the one pydantic-settings
    # folds onto `iso_url_allowlist` — the runner would print an allowlist
    # carrying its own host while ADR 0050 refused every one of its uploads.
    # Removing the variants makes the canonical spelling the only spelling, which
    # is also what lets the guard above short-circuit a second call.
    for variant in [k for k in os.environ if k.lower() == name.lower() and k != name]:
        del os.environ[variant]
    os.environ[name] = ",".join(entries)
    print(f"  ℹ  {name}={os.environ[name]}")


async def _list_media(
    client: Client, state: RunState, subtask: int, label: str
) -> dict[str, Any] | None:
    """List the repository's media, recording the row; None when unreadable."""
    st, data = await state.call(
        client,
        "hmc_list_optical_media",
        vios_name_or_uuid=state.artifacts.vios_uuid,
        vg_uuid=state.artifacts.vmedia_vg_uuid,
    )
    state.record(subtask, f"hmc_list_optical_media ({label})", st, data)
    return media_sizes(data) if st == "PASS" else None


def _start_iso_server(state: RunState, subtask: int) -> str | None:
    """Serve the ISO for this invocation; the failure text when it cannot bind."""
    config = state.config
    try:
        state.iso_http_server.start(config)
    except OSError as exc:
        state.record(
            subtask,
            "iso_http_server",
            "FAIL",
            str(exc),
            f"HTTP server could not bind to port {config.iso_http_port}",
        )
        return f"no HTTP server on port {config.iso_http_port}"
    return None


async def _upload_iso(
    client: Client, state: RunState, subtask: int, label: str, name: str
) -> tuple[str, Any]:
    """Upload the configured ISO as *name*, tracked as run-owned before the call."""
    state.artifacts.vmedia_iso_name = name
    print(f"  ⏳ Uploading ISO via HTTP ({state.config.iso_url}) — may take minutes…")
    st, data = await state.call(
        client,
        "hmc_upload_iso",
        vios_name_or_uuid=state.artifacts.vios_uuid,
        vg_uuid=state.artifacts.vmedia_vg_uuid,
        media_name=name,
        iso_source=state.config.iso_url,
    )
    state.record(subtask, f"hmc_upload_iso ({label})", st, data)
    return st, data


async def _delete_run_media(
    client: Client, state: RunState, subtask: int, name: str
) -> bool:
    """Delete one run-owned medium; True when a listing then shows it absent."""
    st, data = await state.call(
        client,
        "hmc_delete_optical_media",
        vios_name_or_uuid=state.artifacts.vios_uuid,
        vg_uuid=state.artifacts.vmedia_vg_uuid,
        media_name=name,
        system_name_or_uuid=state.config.system_name,
    )
    state.record(subtask, f"hmc_delete_optical_media ({name})", st, data)
    listed = await _list_media(client, state, subtask, f"after deleting {name}")
    absent = listed is not None and name not in listed
    if absent:
        artifacts = state.artifacts
        if artifacts.vmedia_blank_name == name:
            artifacts.vmedia_blank_name = None
        if artifacts.vmedia_iso_name == name:
            artifacts.vmedia_iso_name = None
    return absent


def _refused_as_collision(st: str, data: object) -> bool:
    return (
        st == "FAIL"
        and isinstance(data, CallFailure)
        and _MEDIA_NAME_COLLISION.matches(data)
    )


async def vmedia_upload_iso(client: Client, state: RunState) -> None:
    print("\n=== ST18: ISO Upload via HTTP ===")
    config = state.config
    label = "hmc_upload_iso (round trip)"
    if not state.artifacts.vios_uuid or not state.artifacts.vmedia_vg_uuid:
        state.skip(18, label, _NO_REPOSITORY)
        return
    if not Path(config.iso_path).is_file():
        state.skip(18, label, _no_iso_reason(config))
        return
    if state.artifacts.vmedia_iso_name in run_media_names(state):
        state.skip(18, label, _EARLIER_MEDIUM)
        return
    before = await _list_media(client, state, 18, "pre-upload")
    if before is None:
        state.record_verified(
            18,
            "hmc_list_optical_media",
            operation="media.list",
            scenario=UPLOAD_SCENARIO,
            assertions=[_READ_FAILED],
            cleanup="not-required",
            data=None,
        )
        return
    name = iso_media_name(config)
    if name in before:
        state.skip(18, label, f"{name} is already in the repository; never touched")
        return
    problem = _start_iso_server(state, 18)
    if problem:
        state.skip(18, label, problem)
        return

    uploaded, _ = await _upload_iso(client, state, 18, "http", name)
    after = await _list_media(client, state, 18, "post-upload")
    listed = after is not None and name in after
    refused = False
    if listed:
        st, data = await state.call(
            client,
            "hmc_upload_iso",
            vios_name_or_uuid=state.artifacts.vios_uuid,
            vg_uuid=state.artifacts.vmedia_vg_uuid,
            media_name=name,
            iso_source=config.iso_url,
        )
        state.record(18, "hmc_upload_iso (same-name reupload)", st, data)
        refused = _refused_as_collision(st, data)
    # Whatever the upload returned, a listed run-owned ISO is removed here.
    removed = await _delete_run_media(client, state, 18, name) if listed else True
    if not listed and after is not None:
        _forget_run_media(state, name)
    if after is None or not removed:
        removed = False
        state.record(
            18,
            "hmc_upload_iso (round trip)",
            "FAIL",
            None,
            f"MANUAL RECOVERY REQUIRED: {name} may be in the repository "
            f"(hmcpctl storage delete-media {shlex.quote(str(state.artifacts.vios_uuid))} "
            f"--vg {shlex.quote(str(state.artifacts.vmedia_vg_uuid))} --name {shlex.quote(name)} "
            f"--system {shlex.quote(config.system_name)})",
        )
    state.record_verified(
        18,
        label,
        operation="media.upload_iso",
        scenario=UPLOAD_SCENARIO,
        assertions=[
            Assertion("upload-accepted", uploaded == "PASS"),
            Assertion("media-listed", listed),
            Assertion("reupload-refused", refused),
        ],
        cleanup="passed" if removed else "failed",
        data={"media_name": name},
    )


# ---------------------------------------------------------------------------
# ST19 — Blank Medium: Create, Mount, Unmount, Delete
# ---------------------------------------------------------------------------


class _Stop(Exception):
    """A round-trip step left state the scenario must not build on."""


@dataclass(frozen=True)
class _Baseline:
    media: dict[str, Any]
    repository_size: Decimal | None
    mappings: frozenset[tuple[object, ...]]
    adapters: tuple[frozenset[str], frozenset[str]]


def _create_assertions(
    accepted: bool, listed: bool, size_ok: bool, kept: bool
) -> list[Assertion]:
    return [
        Assertion("create-accepted", accepted),
        Assertion("media-listed", listed),
        Assertion("size-matches", size_ok),
        Assertion("baseline-media-kept", kept),
    ]


def _mount_assertions(accepted: bool, listed: bool, kept: bool) -> list[Assertion]:
    return [
        Assertion("mount-accepted", accepted),
        Assertion("mapping-listed", listed),
        Assertion("baseline-mappings-kept", kept),
    ]


class _RoundTrip:
    """ST19's blank-medium round trip against the VIOS's one repository."""

    def __init__(self, client: Client, state: RunState) -> None:
        self.client = client
        self.state = state
        self.config = state.config
        self.artifacts = state.artifacts
        self.vios = str(state.artifacts.vios_uuid)
        self.vg = str(state.artifacts.vmedia_vg_uuid)
        self.name = f"{BLANK_PREFIX}{_run_tag()}"

    def note(self, tool: str, label: str, result: tuple[str, Any]) -> tuple[str, Any]:
        """Record one call's row and hand its result back."""
        self.state.record(19, f"{tool} ({label})", *result)
        return result

    def skip(self, reason: str) -> None:
        self.state.skip(19, "vmedia round trip", reason)

    def manual(self, what: str, command: str, data: object = None) -> None:
        self.state.record(
            19,
            "vmedia round trip",
            "FAIL",
            data,
            f"MANUAL RECOVERY REQUIRED: {what} ({command})",
        )

    async def preconditions(self) -> bool:
        config = self.config
        if config.lp3_name in config.protected_lpar_names:
            # A SKIP, not a raise: the reads and teardown after ST19 still run.
            self.skip(_protected_reason(config))
            return False
        if type(self.artifacts.vios_partition_id) is not int:
            self.skip("no VIOS partition id resolved in ST16")
            return False
        st, lpar_state = self.note(
            "hmc_get_lpar_state",
            "precondition",
            await self.state.call(
                self.client,
                "hmc_get_lpar_state",
                system_name_or_uuid=config.system_name,
                lpar_name_or_uuid=config.lp3_name,
            ),
        )
        if st != "PASS" or not isinstance(lpar_state, str):
            self.skip("cannot read the test partition's state")
            return False
        if lpar_state.strip().lower() != _NOT_ACTIVATED:
            self.skip(
                f"the test partition is {lpar_state!r}; a mount on a running "
                "partition is a dynamic reconfiguration this arm does not exercise"
            )
            return False
        return True

    async def adapters(
        self, label: str
    ) -> tuple[frozenset[str], frozenset[str]] | None:
        system = self.config.system_name
        rows = []
        for attribute, value in (
            ("lpar_ids", self.artifacts.vios_partition_id),
            ("lpar_names", self.config.lp3_name),
        ):
            st, listing = self.note(
                "hmc_run_command",
                f"{label} adapters {attribute}",
                await self.state.call(
                    self.client,
                    "hmc_run_command",
                    cmd=scsi_adapter_listing(system, attribute, value),
                ),
            )
            parsed = adapter_rows(listing) if st == "PASS" else None
            if parsed is None:
                return None
            rows.append(parsed)
        return rows[0], rows[1]

    async def mappings(self, label: str) -> frozenset[tuple[object, ...]] | None:
        st, data = self.note(
            "hmc_list_storage_mappings",
            label,
            await self.state.call(
                self.client, "hmc_list_storage_mappings", vios_name_or_uuid=self.vios
            ),
        )
        return storage_rows(data) if st == "PASS" else None

    async def repository_size(self, label: str) -> tuple[str, Any, Decimal | None]:
        st, data = self.note(
            "hmc_get_media_repository",
            label,
            await self.state.call(
                self.client,
                "hmc_get_media_repository",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
            ),
        )
        return st, data, repository_fields(data)[1] if st == "PASS" else None

    async def media(self, label: str) -> tuple[str, Any, dict[str, Any] | None]:
        st, data = self.note(
            "hmc_list_optical_media",
            label,
            await self.state.call(
                self.client,
                "hmc_list_optical_media",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
            ),
        )
        return st, data, media_sizes(data) if st == "PASS" else None

    async def baseline(self) -> _Baseline | None:
        st, data, size = await self.repository_size("baseline")
        if st != "PASS" or size is None:
            self.state.record_verified(
                19,
                "hmc_get_media_repository",
                operation="media.get_repository",
                scenario=ROUND_TRIP_SCENARIO,
                assertions=[_READ_FAILED],
                cleanup="not-required",
                data=data,
            )
            return None
        st, data, media = await self.media("baseline")
        if st != "PASS" or not isinstance(data, list):
            self.state.record_verified(
                19,
                "hmc_list_optical_media",
                operation="media.list",
                scenario=ROUND_TRIP_SCENARIO,
                assertions=[_READ_FAILED],
                cleanup="not-required",
                data=data,
            )
            return None
        if not data:
            # An empty listing proves no entry shape.
            self.state.record(19, "hmc_list_optical_media (empty)", st, data)
        else:
            self.state.record_verified(
                19,
                "hmc_list_optical_media",
                operation="media.list",
                scenario=ROUND_TRIP_SCENARIO,
                assertions=[Assertion("media-entries-named", media is not None)],
                cleanup="not-required",
                data=data,
            )
        if media is None:
            self.skip("a listed medium has no name, so no baseline can be compared")
            return None
        mappings = await self.mappings("baseline")
        if mappings is None:
            self.state.record_verified(
                19,
                "hmc_list_storage_mappings",
                operation="storage.list_mappings",
                scenario=ROUND_TRIP_SCENARIO,
                assertions=[_READ_FAILED],
                cleanup="not-required",
                data=None,
            )
            return None
        adapters = await self.adapters("baseline")
        if adapters is None:
            self.skip("cannot read the vSCSI adapter listings")
            return None
        return _Baseline(media, size, mappings, adapters)

    def has_room(self, baseline: _Baseline) -> bool:
        sizes = list(baseline.media.values())
        if baseline.repository_size is None or any(
            not isinstance(size, int | float) for size in sizes
        ):
            self.skip(
                "a media size is unknown, so the repository's free space is (gap)"
            )
            return False
        free = baseline.repository_size * 1024 - sum(Decimal(str(s)) for s in sizes)
        if free < BLANK_SIZE_MIB:
            self.skip(f"{free} MiB free in the repository < {BLANK_SIZE_MIB} MiB (gap)")
            return False
        return True

    def adapter_drift(
        self,
        baseline: _Baseline,
        now: tuple[frozenset[str], frozenset[str]] | None,
        when: str,
    ) -> bool:
        """Report adapters that differ from the baseline; True when they match."""
        if now == baseline.adapters:
            return True
        system = shlex.quote(self.config.system_name)
        added = (
            sorted(now[0] - baseline.adapters[0])
            + sorted(now[1] - baseline.adapters[1])
            if now is not None
            else ["(adapter listing unreadable)"]
        )
        self.manual(
            f"vSCSI adapters differ from the baseline {when}: {', '.join(added)}",
            f"lshwres -r virtualio --rsubtype scsi -m {system} --level lpar; remove "
            f"each adapter the run added with chhwres -r virtualio --rsubtype scsi -m "
            f"{system} -o r --id <partition id> -s <slot>",
        )
        return False

    async def mapped(self) -> bool | None:
        config = self.config
        st, data = self.note(
            "hmc_list_optical_mappings",
            "test partition",
            await self.state.call(
                self.client,
                "hmc_list_optical_mappings",
                vios_name_or_uuid=self.vios,
                lpar_name_or_uuid=config.lp3_name,
            ),
        )
        if st != "PASS" or not isinstance(data, list):
            return None
        return any(
            (identity := _mapping_identity(entry)) is not None
            and identity[1] == self.name
            for entry in data
        )

    async def run(self) -> None:
        if not await self.preconditions():
            return
        baseline = await self.baseline()
        if baseline is None or not self.has_room(baseline):
            return
        created = await self.create(baseline)
        if created is None:
            return
        try:
            mounted, refused = await self.mount_and_unmount(baseline)
        except _Stop:
            self.record_create(created, restored=False)
            raise
        await self.delete(baseline, created, mounted, refused)

    def record_create(self, created: tuple[bool, ...], *, restored: bool) -> None:
        self.state.record_verified(
            19,
            "hmc_create_optical_media",
            operation="media.create",
            scenario=ROUND_TRIP_SCENARIO,
            assertions=_create_assertions(*created),
            cleanup="passed" if restored else "failed",
            data={"media_name": self.name},
        )

    async def create(self, baseline: _Baseline) -> tuple[bool, ...] | None:
        self.artifacts.vmedia_blank_name = self.name
        st_c, _ = self.note(
            "hmc_create_optical_media",
            "blank",
            await self.state.call(
                self.client,
                "hmc_create_optical_media",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
                media_name=self.name,
                size_mib=BLANK_SIZE_MIB,
                system_name_or_uuid=self.config.system_name,
            ),
        )
        _, _, after = await self.media("post-create")
        listed = after is not None and self.name in after
        created = (
            st_c == "PASS",
            listed,
            after is not None and after.get(self.name) == BLANK_SIZE_MIB,
            after is not None
            and all(after.get(n) == s for n, s in baseline.media.items()),
        )
        if not listed:
            # Nothing listed to remove: restored exactly when the listing is readable.
            self.record_create(created, restored=after is not None)
            if after is None:
                self.manual(
                    f"cannot confirm whether {self.name} was created",
                    f"hmcpctl storage list-optical-media {shlex.quote(self.vios)} "
                    f"--vg {shlex.quote(self.vg)}",
                )
            else:
                self.artifacts.vmedia_blank_name = None
            return None
        return created

    async def mount_and_unmount(self, baseline: _Baseline) -> tuple[bool, bool | None]:
        """Mount, try the guarded delete, unmount; returns (mounted, delete refused).

        The refusal is None when the guard refused but the listing after it
        could not be read: unverified, not a guard that let the delete through.
        """
        config = self.config
        st_m, data_m = self.note(
            "hmc_mount_optical_media",
            "blank",
            await self.state.call(
                self.client,
                "hmc_mount_optical_media",
                vios_name_or_uuid=self.vios,
                media_name=self.name,
                lpar_name_or_uuid=config.lp3_name,
                system_name_or_uuid=config.system_name,
            ),
        )
        mapped = await self.mapped()
        after_mount = await self.mappings("post-mount")
        mounted = (
            st_m == "PASS",
            mapped is True,
            after_mount is not None and baseline.mappings <= after_mount,
        )
        if mapped is not True:
            restored = self.adapter_drift(
                baseline, await self.adapters("post-mount"), "after a failed mount"
            )
            self.record_mount(mounted, restored, data_m)
            if mapped is None:
                self.manual(
                    f"cannot confirm whether {self.name} is mounted",
                    self.unmount_command(),
                )
                raise _Stop("mount state unknown")
            return False, False

        st_d, data_d = await self.state.call(
            self.client,
            "hmc_delete_optical_media",
            vios_name_or_uuid=self.vios,
            vg_uuid=self.vg,
            media_name=self.name,
            system_name_or_uuid=self.config.system_name,
            expected=[_DELETE_REFUSED_WHILE_MOUNTED],
        )
        self.state.record_with_expected(
            19,
            "hmc_delete_optical_media (while mounted)",
            st_d,
            data_d,
            [_DELETE_REFUSED_WHILE_MOUNTED],
        )
        _, _, still = await self.media("after the guarded delete")
        refused: bool | None = False
        if isinstance(data_d, CallFailure) and _DELETE_REFUSED_WHILE_MOUNTED.matches(
            data_d
        ):
            refused = None if still is None else self.name in still
        if refused is None:
            self.state.skip(
                19,
                "hmc_delete_optical_media (refusal unverified)",
                f"the media listing after the guarded delete could not be read, so "
                f"{self.name} staying in the repository is unconfirmed",
            )

        st_u, data_u = self.note(
            "hmc_unmount_optical_media",
            "blank",
            await self.state.call(
                self.client,
                "hmc_unmount_optical_media",
                vios_name_or_uuid=self.vios,
                lpar_name_or_uuid=config.lp3_name,
                media_name=self.name,
                system_name_or_uuid=config.system_name,
            ),
        )
        still_mapped = await self.mapped()
        after_unmount = await self.mappings("post-unmount")
        adapters_ok = self.adapter_drift(
            baseline, await self.adapters("post-unmount"), "after the unmount"
        )
        mappings_ok = after_unmount == baseline.mappings
        if not mappings_ok:
            changed = (
                sorted(map(str, after_unmount ^ baseline.mappings))
                if after_unmount is not None
                else ["(mapping listing unreadable)"]
            )
            self.manual(
                f"VIOS storage mappings differ from the baseline after the unmount: "
                f"{', '.join(changed)}",
                f"hmcpctl storage list-mappings {shlex.quote(self.vios)} --system "
                f"{shlex.quote(self.config.system_name)}",
            )
        self.state.record_verified(
            19,
            "hmc_unmount_optical_media",
            operation="media.unmount",
            scenario=ROUND_TRIP_SCENARIO,
            assertions=[
                Assertion("unmount-accepted", st_u == "PASS"),
                Assertion("mapping-absent", still_mapped is False),
                Assertion("mappings-equal-baseline", mappings_ok),
                Assertion("adapters-equal-baseline", adapters_ok),
            ],
            cleanup="not-required",
            data=data_u,
        )
        self.record_mount(
            mounted,
            still_mapped is False and mappings_ok and adapters_ok,
            data_m,
        )
        if still_mapped is not False:
            self.manual(f"{self.name} may still be mounted", self.unmount_command())
            raise _Stop("unmount not confirmed")
        return True, refused

    def unmount_command(self) -> str:
        return (
            f"hmcpctl storage unmount-optical-media {shlex.quote(self.vios)} "
            f"--lpar {shlex.quote(self.config.lp3_name)} --name {shlex.quote(self.name)} --system "
            f"{shlex.quote(self.config.system_name)}"
        )

    def record_mount(
        self, mounted: tuple[bool, bool, bool], restored: bool, data: object
    ) -> None:
        self.state.record_verified(
            19,
            "hmc_mount_optical_media",
            operation="media.mount",
            scenario=ROUND_TRIP_SCENARIO,
            assertions=_mount_assertions(*mounted),
            cleanup="passed" if restored else "failed",
            data=data,
        )

    async def delete(
        self,
        baseline: _Baseline,
        created: tuple[bool, ...],
        mounted: bool,
        refused: bool | None,
    ) -> None:
        st_d, data_d = self.note(
            "hmc_delete_optical_media",
            "blank",
            await self.state.call(
                self.client,
                "hmc_delete_optical_media",
                vios_name_or_uuid=self.vios,
                vg_uuid=self.vg,
                media_name=self.name,
                system_name_or_uuid=self.config.system_name,
            ),
        )
        _, _, after = await self.media("post-delete")
        _, _, size = await self.repository_size("post-delete")
        absent = after is not None and self.name not in after
        equal = after == baseline.media and size == baseline.repository_size
        if absent:
            self.artifacts.vmedia_blank_name = None
        self.state.record_verified(
            19,
            "hmc_delete_optical_media",
            operation="media.delete",
            scenario=ROUND_TRIP_SCENARIO,
            assertions=[
                # Only a mounted medium can show the guard refusing, and only a
                # readable listing can confirm the refusal kept it.
                *(
                    [Assertion("refused-while-mounted", refused)]
                    if mounted and refused is not None
                    else []
                ),
                Assertion("delete-accepted", st_d == "PASS"),
                Assertion("media-absent", absent),
                Assertion("media-equal-baseline", equal),
            ],
            cleanup="not-required",
            data=data_d,
        )
        self.record_create(created, restored=equal)
        if not absent:
            self.manual(
                f"{self.name} is still in the repository",
                f"hmcpctl storage delete-media {shlex.quote(self.vios)} "
                f"--vg {shlex.quote(self.vg)} --name {shlex.quote(self.name)}",
            )


async def vmedia_mount_unmount(client: Client, state: RunState) -> None:
    print("\n=== ST19: Blank Medium — Create, Mount, Unmount, Delete ===")
    if not state.artifacts.vios_uuid or not state.artifacts.vmedia_vg_uuid:
        state.skip(19, "vmedia round trip", _NO_REPOSITORY)
        return
    if state.artifacts.vmedia_blank_name in run_media_names(state):
        state.skip(19, "vmedia round trip", _EARLIER_MEDIUM)
        return
    try:
        await _RoundTrip(client, state).run()
    except _Stop as stop:
        print(f"  ⚠  ST19 stopped: {stop}")


# ---------------------------------------------------------------------------
# ST20 — Boot Verification: Power Off → CD Boot → Power On → Verify → Restore
# ---------------------------------------------------------------------------


async def _prepare_boot_media(
    client: Client,
    state: RunState,
    vios_uuid: str,
    remaining_steps: list[str],
) -> bool:
    """Upload and mount the boot ISO after placing the test LPAR offline."""
    config = state.config
    artifacts = state.artifacts
    problem = _start_iso_server(state, 20)
    if problem:
        for name in remaining_steps:
            state.skip(20, name, problem)
        return False
    before = await _list_media(client, state, 20, "pre-upload")
    name = iso_media_name(config)
    if before is None or name in before:
        for step in remaining_steps:
            state.skip(20, step, "cannot confirm the ISO name is free")
        return False

    status, _ = await _upload_iso(client, state, 20, "re-upload for boot test", name)
    if status != "PASS":
        for step in remaining_steps[1:]:
            state.skip(20, step, "ISO re-upload failed")
        return False

    status, data = await state.call(
        client,
        "hmc_power_off_lpar",
        lpar_name_or_uuid=config.lp3_name,
        immediate=True,
        wait=True,
        expected=[_ALREADY_POWERED_OFF],
    )
    state.record_with_expected(
        20, "hmc_power_off_lpar (pre-boot)", status, data, [_ALREADY_POWERED_OFF]
    )

    status, data = await state.call(
        client,
        "hmc_mount_optical_media",
        vios_name_or_uuid=vios_uuid,
        media_name=name,
        lpar_name_or_uuid=config.lp3_name,
        system_name_or_uuid=config.system_name,
    )
    state.record(20, "hmc_mount_optical_media (boot test)", status, data)
    if status != "PASS":
        for step in remaining_steps[3:]:
            state.skip(20, step, "mount failed")
        return False
    if isinstance(data, dict):
        resource = data.get("Resource") or {}
        artifacts.vmedia_mapping_uuid = (
            data.get("ElementID")
            or data.get("UUID")
            or data.get("uuid")
            or data.get("mapping_uuid")
            or resource.get("ElementID")
            or resource.get("UUID")
        )
    return True


_SET_BOOT_ORDER_STEP = "hmc_set_lpar_boot_order (boot device list)"
#: The row whose `pending_boot_string` `live_test_recovery.py` compares against.
_BOOT_BASELINE_STEP = "hmc_read_lpar_boot_order (baseline)"


async def _configure_boot_order(
    client: Client, state: RunState, lpar_uuid: str
) -> None:
    """Capture the current boot order and set the firmware's own boot device list.

    The boot order takes Open Firmware device paths (#980). No path is reported for
    the virtual CD, so the step no longer forces a CD-first boot: it writes back the
    paths the HMC reports, which exercises the write. It writes only when the baseline
    pending boot order is non-empty, because only a set can restore it: no V10R3 value
    clears a pending boot string (#1048), so a write over an empty baseline would
    leave the write behind.
    """
    config = state.config
    artifacts = state.artifacts
    status, data = await state.call(
        client,
        "hmc_read_lpar_boot_order",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=lpar_uuid,
    )
    state.record(20, _BOOT_BASELINE_STEP, status, data)
    pending: list[str] = []
    boot_devices: list[str] = []
    if status == "PASS" and isinstance(data, dict):
        pending = cast(str, data.get("pending_boot_string") or "").split()
        boot_devices = cast(str, data.get("boot_device_list") or "").split()

    if not pending:
        state.skip(
            20,
            _SET_BOOT_ORDER_STEP,
            "baseline pending boot order is empty; no V10R3 value clears a pending boot "
            "string (#1048; the empty value is rejected with HTTP 500 REST0126)",
        )
        return
    if not boot_devices:
        state.skip(
            20,
            _SET_BOOT_ORDER_STEP,
            "no boot device list reported (a never-booted partition has none)",
        )
        return
    try:
        join_boot_device_paths(pending)
    except ValueError as exc:
        state.skip(
            20,
            _SET_BOOT_ORDER_STEP,
            f"baseline pending boot order cannot be restored: {exc}",
        )
        return
    artifacts.vmedia_orig_boot_order = pending
    status, data = await state.call(
        client,
        "hmc_set_lpar_boot_order",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=lpar_uuid,
        devices=boot_devices,
    )
    state.record(20, _SET_BOOT_ORDER_STEP, status, data)


async def _run_boot_probe(client: Client, state: RunState) -> None:
    """Boot the test partition, report its state, and power it off again."""
    config = state.config
    status, data = await state.call(
        client,
        "hmc_power_on_lpar",
        lpar_name_or_uuid=config.lp3_name,
        wait=True,
        timeout_seconds=120,
    )
    state.record(20, "hmc_power_on_lpar", status, data)

    status, data = await state.call(
        client, "hmc_lpar_summary", lpar_name_or_uuid=config.lp3_name
    )
    state.record(20, "hmc_lpar_summary (verify running)", status, data)
    if status == "PASS" and isinstance(data, dict):
        lpar_state = data.get("state") or ""
        icon = "✅" if lpar_state.lower() == "running" else "⚠️"
        print(f"  {icon} lp3 state: {lpar_state}")

    status, data = await state.call(
        client,
        "hmc_power_off_lpar",
        lpar_name_or_uuid=config.lp3_name,
        immediate=True,
        wait=True,
    )
    state.record(20, "hmc_power_off_lpar (post-boot)", status, data)


async def _restore_boot_configuration(
    client: Client, state: RunState, vios_uuid: str, lpar_uuid: str
) -> None:
    """Unmount the test ISO, restore boot order, and verify the restored state."""
    config = state.config
    artifacts = state.artifacts
    if artifacts.vmedia_mapping_uuid and artifacts.vmedia_iso_name:
        status, data = await state.call(
            client,
            "hmc_unmount_optical_media",
            vios_name_or_uuid=vios_uuid,
            lpar_name_or_uuid=config.lp3_name,
            media_name=artifacts.vmedia_iso_name,
            system_name_or_uuid=config.system_name,
        )
        state.record(20, "hmc_unmount_optical_media (boot test cleanup)", status, data)
        if status == "PASS":
            artifacts.vmedia_mapping_uuid = None
    else:
        state.skip(
            20,
            "hmc_unmount_optical_media (boot test cleanup)",
            "no mounted media to unmount",
        )

    if artifacts.vmedia_orig_boot_order:
        status, data = await state.call(
            client,
            "hmc_set_lpar_boot_order",
            system_name_or_uuid=config.system_name,
            lpar_name_or_uuid=lpar_uuid,
            devices=artifacts.vmedia_orig_boot_order,
        )
        state.record(20, "hmc_set_lpar_boot_order (restore)", status, data)
        if status == "PASS":
            artifacts.vmedia_orig_boot_order = []
    else:
        state.skip(20, "hmc_set_lpar_boot_order (restore)", "no boot order was set")

    status, data = await state.call(
        client,
        "hmc_read_lpar_boot_order",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=lpar_uuid,
    )
    state.record(20, "hmc_read_lpar_boot_order (verify restore)", status, data)


async def vmedia_boot_verification(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts
    print(
        "\n=== ST20: Boot Verification: Power Off → CD Boot → Power On → Verify → Restore ==="
    )

    _skip_names = [
        "hmc_upload_iso (re-upload for boot test)",
        "hmc_power_off_lpar (pre-boot)",
        "hmc_mount_optical_media (boot test)",
        _BOOT_BASELINE_STEP,
        _SET_BOOT_ORDER_STEP,
        "hmc_power_on_lpar",
        "hmc_lpar_summary (verify running)",
        "hmc_power_off_lpar (post-boot)",
        "hmc_unmount_optical_media (boot test cleanup)",
        "hmc_set_lpar_boot_order (restore)",
        "hmc_read_lpar_boot_order (verify restore)",
    ]

    reason = None
    if not artifacts.vios_uuid or not artifacts.vmedia_vg_uuid:
        reason = _NO_REPOSITORY
    elif not Path(config.iso_path).is_file():
        reason = _no_iso_reason(config)
    elif not artifacts.lp3_uuid:
        reason = "lp3_uuid not set (ST16 failed to capture it)"
    elif artifacts.vmedia_iso_name in run_media_names(state):
        reason = _EARLIER_MEDIUM
    elif config.lp3_name in config.protected_lpar_names:
        reason = _protected_reason(config)
    if reason:
        for name in _skip_names:
            state.skip(20, name, reason)
        return

    vios = str(artifacts.vios_uuid)
    lp3_uuid = str(artifacts.lp3_uuid)
    if not await _prepare_boot_media(client, state, vios, _skip_names):
        return
    await _configure_boot_order(client, state, lp3_uuid)
    await _run_boot_probe(client, state)
    await _restore_boot_configuration(client, state, vios, lp3_uuid)


# ---------------------------------------------------------------------------
# ST21 — List Storage Mappings Cross-Validation
# ---------------------------------------------------------------------------


def _optical_ids(data: list[Any]) -> list[str | None]:
    return [
        storage_mapping_id(entry) if isinstance(entry, Mapping) else None
        for entry in data
    ]


def _same_lpar(left: object, right: object) -> bool:
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and left.casefold() == right.casefold()
    )


async def vmedia_mapping_crossvalidation(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts
    print("\n=== ST21: List Storage Mappings Cross-Validation ===")

    if not artifacts.vios_uuid:
        for name in ["hmc_list_storage_mappings", "hmc_list_optical_mappings"]:
            state.skip(21, name, "no VIOS UUID in context")
        return

    vios, lp3 = artifacts.vios_uuid, config.lp3_name
    reads = {
        ("hmc_list_storage_mappings", "all"): await state.call(
            client, "hmc_list_storage_mappings", vios_name_or_uuid=vios
        ),
        ("hmc_list_storage_mappings", "lp3"): await state.call(
            client,
            "hmc_list_storage_mappings",
            vios_name_or_uuid=vios,
            lpar_name_or_uuid=lp3,
        ),
        ("hmc_list_optical_mappings", "all"): await state.call(
            client, "hmc_list_optical_mappings", vios_name_or_uuid=vios
        ),
        ("hmc_list_optical_mappings", "lp3"): await state.call(
            client,
            "hmc_list_optical_mappings",
            vios_name_or_uuid=vios,
            lpar_name_or_uuid=lp3,
        ),
    }
    for (tool, scope), (st, data) in reads.items():
        state.record(21, f"{tool} ({scope})", st, data)

    _verify_storage_mappings(state, reads)
    _verify_optical_mappings(state, reads)


def _scope_rows(
    state: RunState, tool: str, reads: dict[tuple[str, str], tuple[str, Any]]
) -> tuple[list[Any], list[Any]] | None:
    """Both scopes of *tool*; None after recording an empty listing.

    A failed read returns two empty lists, which the caller records as not
    holding: an empty listing proves no row shape and makes the subset check
    vacuous, so only a successful read can be empty.
    """
    (st_all, every), (st_lp3, scoped) = reads[tool, "all"], reads[tool, "lp3"]
    if st_all != "PASS" or st_lp3 != "PASS":
        return [], []
    if not isinstance(every, list) or not isinstance(scoped, list):
        return [], []
    if not every or not scoped:
        state.record(21, f"{tool} (empty)", "PASS", {"all": every, "lp3": scoped})
        return None
    return every, scoped


def _verify_storage_mappings(
    state: RunState, reads: dict[tuple[str, str], tuple[str, Any]]
) -> None:
    rows = _scope_rows(state, "hmc_list_storage_mappings", reads)
    if rows is None:
        return
    every, scoped = rows
    ids = [e.get("id") if isinstance(e, Mapping) else None for e in every]
    lp3 = state.artifacts.lp3_uuid
    _, optical = reads["hmc_list_optical_mappings", "all"]
    optical_ids = _optical_ids(optical) if isinstance(optical, list) else [None]
    backed = sorted(
        str(e.get("id"))
        for e in every
        if isinstance(e, Mapping) and e.get("backing_kind") == "VirtualOpticalMedia"
    )
    state.record_verified(
        21,
        "hmc_list_storage_mappings",
        operation="storage.list_mappings",
        scenario=MAPPINGS_SCENARIO,
        assertions=[
            Assertion(
                "mapping-ids-identified",
                all(isinstance(i, str) and i.count("/") == 1 for i in ids),
            ),
            Assertion(
                "lpar-scope-subset",
                all(
                    isinstance(e, Mapping)
                    and e.get("id") in ids
                    and _same_lpar(e.get("lpar_uuid"), lp3)
                    for e in scoped
                ),
            ),
            Assertion(
                "optical-backing-agrees",
                None not in optical_ids and sorted(map(str, optical_ids)) == backed,
            ),
        ]
        if every
        else [_READ_FAILED],
        cleanup="not-required",
        data=every,
    )


def _verify_optical_mappings(
    state: RunState, reads: dict[tuple[str, str], tuple[str, Any]]
) -> None:
    rows = _scope_rows(state, "hmc_list_optical_mappings", reads)
    if rows is None:
        return
    every, scoped = rows
    ids = _optical_ids(every)
    lp3 = state.artifacts.lp3_uuid
    state.record_verified(
        21,
        "hmc_list_optical_mappings",
        operation="media.list_mappings",
        scenario=MAPPINGS_SCENARIO,
        assertions=[
            Assertion(
                "optical-entries-named",
                None not in ids
                and all(_mapping_identity(entry) is not None for entry in every),
            ),
            Assertion(
                "lpar-scope-subset",
                all(
                    (identity := _mapping_identity(entry)) is not None
                    and _same_lpar(identity[0], lp3)
                    and (mapping_id := storage_mapping_id(entry)) is not None
                    and mapping_id in ids
                    for entry in scoped
                ),
            ),
        ]
        if every
        else [_READ_FAILED],
        cleanup="not-required",
        data=every,
    )


# ---------------------------------------------------------------------------
# ST22 — Teardown: Unmount Run Media → Delete Run Media → Delete Own Repository
# ---------------------------------------------------------------------------


async def _restore_teardown_boot_order(client: Client, state: RunState) -> None:
    """Restore a saved boot order when ST20 did not finish its own cleanup."""
    config = state.config
    artifacts = state.artifacts
    if artifacts.vmedia_orig_boot_order and artifacts.lp3_uuid:
        st, data = await state.call(
            client,
            "hmc_set_lpar_boot_order",
            system_name_or_uuid=config.system_name,
            lpar_name_or_uuid=artifacts.lp3_uuid,
            devices=artifacts.vmedia_orig_boot_order,
        )
        state.record(22, "hmc_set_lpar_boot_order (boot order restore guard)", st, data)
        if st == "PASS":
            artifacts.vmedia_orig_boot_order = []
    elif not artifacts.vmedia_orig_boot_order:
        state.skip(
            22,
            "hmc_set_lpar_boot_order (boot order restore guard)",
            "no saved boot order to restore",
        )
    else:
        state.skip(
            22,
            "hmc_set_lpar_boot_order (boot order restore guard)",
            "lp3_uuid not available",
        )


async def _remove_run_mappings(
    client: Client, state: RunState, vios: str, owned: set[str]
) -> None:
    """Unmount every optical mapping whose medium this run created; nothing else."""
    st, data = await state.call(
        client,
        "hmc_list_optical_mappings",
        vios_name_or_uuid=vios,
    )
    state.record(22, "hmc_list_optical_mappings (run media cleanup)", st, data)
    if st != "PASS":
        return
    identities = [
        _mapping_identity(m) for m in (data if isinstance(data, list) else [])
    ]
    targets = [i for i in identities if i is not None and i[1] in owned]
    if not targets:
        state.skip(22, "hmc_unmount_optical_media (run media)", "no run media mounted")
    for lpar, media_name in targets:
        st_u, data_u = await state.call(
            client,
            "hmc_unmount_optical_media",
            vios_name_or_uuid=vios,
            lpar_name_or_uuid=lpar,
            media_name=media_name,
            system_name_or_uuid=state.config.system_name,
        )
        state.record(
            22, f"hmc_unmount_optical_media (run media {media_name})", st_u, data_u
        )
        if st_u == "PASS" and media_name == state.artifacts.vmedia_iso_name:
            state.artifacts.vmedia_mapping_uuid = None


async def _remove_run_media(client: Client, state: RunState, owned: set[str]) -> None:
    """Delete every medium this run created that the repository still lists."""
    listed = await _list_media(client, state, 22, "run media cleanup")
    if listed is None:
        return
    for name in sorted(owned):
        if name in listed:
            await _delete_run_media(client, state, 22, name)
        else:
            _forget_run_media(state, name)


def _forget_run_media(state: RunState, name: str) -> None:
    artifacts = state.artifacts
    if artifacts.vmedia_blank_name == name:
        artifacts.vmedia_blank_name = None
    if artifacts.vmedia_iso_name == name:
        artifacts.vmedia_iso_name = None


async def _remove_repository(
    client: Client, state: RunState, vios: str, vg: str
) -> None:
    """Remove the repository this run created and confirm its absence."""
    st, data = await state.call(
        client,
        "hmc_delete_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
        expected=[_REPOSITORY_ALREADY_GONE],
    )
    state.record_with_expected(
        22,
        "hmc_delete_media_repository",
        st,
        data,
        [_REPOSITORY_ALREADY_GONE],
    )
    if st == "PASS":
        state.artifacts.vmedia_repo_created = False
    st, data = await state.call(
        client,
        "hmc_get_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
    )
    state.record(22, "hmc_get_media_repository (confirm gone)", st, data)


async def vmedia_teardown(client: Client, state: RunState) -> None:
    """Restore boot state and remove what this run created, in phase order."""
    artifacts = state.artifacts
    print("\n=== ST22: Teardown: Unmount Run Media → Delete Run Media → Repository ===")

    await _restore_teardown_boot_order(client, state)
    if not artifacts.vios_uuid:
        for name in [
            "hmc_list_optical_mappings (run media cleanup)",
            "hmc_list_optical_media (run media cleanup)",
            "hmc_delete_media_repository",
            "hmc_list_volume_groups (final audit)",
        ]:
            state.skip(22, name, "no VIOS UUID in context")
        return

    owned = run_media_names(state)
    for name in (artifacts.vmedia_blank_name, artifacts.vmedia_iso_name):
        if name and name not in owned:
            # Not a name this arm generates (an older arm recorded it): never removed.
            _forget_run_media(state, name)
    if not owned:
        state.skip(22, "hmc_list_optical_media (run media cleanup)", "no run media")
    else:
        # An unmount needs no volume group, so it runs even when ST16 found none.
        await _remove_run_mappings(client, state, artifacts.vios_uuid, owned)
        if artifacts.vmedia_vg_uuid:
            await _remove_run_media(client, state, owned)
        else:
            state.skip(22, "hmc_list_optical_media (run media cleanup)", _NO_REPOSITORY)

    vg = configured_vg_uuid(state)
    if _owns_repository(state) and vg is not None:
        await _remove_repository(client, state, artifacts.vios_uuid, vg)
    else:
        state.skip(22, "hmc_delete_media_repository", _NOT_OWNED)

    st, data = await state.call(
        client, "hmc_list_volume_groups", vios_name_or_uuid=artifacts.vios_uuid
    )
    state.record(22, "hmc_list_volume_groups (final audit)", st, data)
