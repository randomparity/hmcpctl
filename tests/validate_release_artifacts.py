import base64
import configparser
import csv
import gzip
import hashlib
import os
import re
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Never, Protocol

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

PROJECT_NAME = "hmcpctl"
PACKAGE_NAME = "hmcpctl"
# PEP 561: without this marker in the distribution a type-checker reads every
# exported model, alias, and signature as Any, so it is a shipped-artifact
# invariant rather than a source-tree convenience.
TYPE_MARKER = "py.typed"
PACKAGE_RESOURCES = (TYPE_MARKER, "_operation_maturity.json", "_progress_codes.json")
PACKAGE_SENTINELS = ("__init__.py", "server.py", *PACKAGE_RESOURCES)
CORE_METADATA_VERSION = "2.5"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
ZIP_EOCD_SIZE = 22
ZIP_EOCD_SEARCH_BYTES = ZIP_EOCD_SIZE + 0xFFFF
ZIP64_LOCATOR_SIZE = 20
ZIP64_EOCD_MIN_SIZE = 56
ZIP_DIRECTORY_HEADER_SIZE = 46
SINGLETON_METADATA = (
    "Metadata-Version",
    "Name",
    "Version",
    "Requires-Python",
    "License-Expression",
)
SDIST_INPUTS = (
    ".gitignore",
    "pyproject.toml",
    "README.md",
    "LICENSE",
)


class ValidationError(ValueError):
    pass


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _CaseConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


@dataclass(frozen=True)
class Distribution:
    name: str
    version: str
    metadata: Message
    members: dict[str, bytes]


@dataclass(frozen=True)
class ProjectConfiguration:
    name: str
    requires_python: str
    license_expression: str
    dependencies: set[str]
    extras: set[str]
    scripts: dict[str, str]


def _fail(artifact: str, invariant: str) -> Never:
    raise ValidationError(f"{artifact}: {invariant}")


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _valid_version(value: str, artifact: str) -> str:
    try:
        return str(Version(value))
    except InvalidVersion:
        _fail(artifact, "version must be valid PEP 440")


def _validate_member_name(name: str, artifact: str) -> str:
    if unicodedata.normalize("NFC", name) != name:
        _fail(artifact, f"archive member path is not NFC: {name!r}")
    if "\\" in name or name.startswith("/"):
        _fail(artifact, f"archive member path is not portable: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _fail(artifact, f"archive member path is not canonical: {name!r}")
    if ":" in parts[0]:
        _fail(artifact, f"archive member path is drive-like: {name!r}")
    return str(PurePosixPath(*parts))


def _check_archive_size(size: int, artifact: str) -> None:
    if size > MAX_ARCHIVE_BYTES:
        _fail(artifact, "archive input exceeds 256 MiB")


def _check_archive_input(path: Path) -> None:
    _check_archive_size(path.stat().st_size, path.name)


def _find_zip_eocd(tail: bytes, tail_offset: int, file_size: int, artifact: str) -> int:
    signature = b"PK\x05\x06"
    position = len(tail)
    while (position := tail.rfind(signature, 0, position)) >= 0:
        if position + ZIP_EOCD_SIZE <= len(tail):
            comment_size = struct.unpack_from("<H", tail, position + 20)[0]
            absolute = tail_offset + position
            if absolute + ZIP_EOCD_SIZE + comment_size == file_size:
                return position
    _fail(artifact, "wheel archive is malformed: end record")


def _read_zip64_directory(
    stream: BinaryIO,
    *,
    eocd_offset: int,
    artifact: str,
) -> tuple[int, int, int, int]:
    locator_offset = eocd_offset - ZIP64_LOCATOR_SIZE
    if locator_offset < 0:
        _fail(artifact, "wheel archive is malformed: ZIP64 locator")
    stream.seek(locator_offset)
    locator = stream.read(ZIP64_LOCATOR_SIZE)
    if len(locator) != ZIP64_LOCATOR_SIZE:
        _fail(artifact, "wheel archive is malformed: ZIP64 locator")
    signature, disk, record_offset, disks = struct.unpack("<4sLQL", locator)
    if signature != b"PK\x06\x07" or disk != 0 or disks != 1:
        _fail(artifact, "wheel archive is malformed: ZIP64 locator")
    if record_offset + ZIP64_EOCD_MIN_SIZE > locator_offset:
        _fail(artifact, "wheel archive is malformed: ZIP64 record")
    stream.seek(record_offset)
    record = stream.read(ZIP64_EOCD_MIN_SIZE)
    if len(record) != ZIP64_EOCD_MIN_SIZE:
        _fail(artifact, "wheel archive is malformed: ZIP64 record")
    values = struct.unpack("<4sQ2H2L4Q", record)
    signature, record_size = values[:2]
    disk, directory_disk, disk_entries, entries, size, offset = values[4:]
    if (
        signature != b"PK\x06\x06"
        or record_size < 44
        or record_offset + 12 + record_size > locator_offset
        or disk != 0
        or directory_disk != 0
        or disk_entries != entries
    ):
        _fail(artifact, "wheel archive is malformed: ZIP64 record")
    return entries, size, offset, record_offset


def _scan_zip_directory(
    stream: BinaryIO,
    *,
    offset: int,
    size: int,
    declared_entries: int,
    artifact: str,
) -> None:
    stream.seek(offset)
    consumed = 0
    entries = 0
    while consumed < size:
        remaining = size - consumed
        if remaining < ZIP_DIRECTORY_HEADER_SIZE:
            _fail(artifact, "wheel archive is malformed: central directory")
        header = stream.read(ZIP_DIRECTORY_HEADER_SIZE)
        if len(header) != ZIP_DIRECTORY_HEADER_SIZE or header[:4] != b"PK\x01\x02":
            _fail(artifact, "wheel archive is malformed: central directory")
        name_size, extra_size, comment_size = struct.unpack_from("<3H", header, 28)
        record_size = ZIP_DIRECTORY_HEADER_SIZE + name_size + extra_size + comment_size
        if record_size > remaining:
            _fail(artifact, "wheel archive is malformed: central directory")
        stream.seek(record_size - ZIP_DIRECTORY_HEADER_SIZE, 1)
        consumed += record_size
        entries += 1
        if entries > MAX_ARCHIVE_MEMBERS:
            _fail(artifact, "archive contains more than 4096 members")
    if entries != declared_entries:
        _fail(artifact, "wheel archive is malformed: central directory entry count")


def _preflight_zip_directory(stream: BinaryIO, artifact: str) -> None:
    stream.seek(0, 2)
    file_size = stream.tell()
    _check_archive_size(file_size, artifact)
    tail_size = min(file_size, ZIP_EOCD_SEARCH_BYTES)
    try:
        stream.seek(file_size - tail_size)
        tail = stream.read(tail_size)
        position = _find_zip_eocd(tail, file_size - tail_size, file_size, artifact)
        eocd_offset = file_size - tail_size + position
        values = struct.unpack_from("<4s4H2LH", tail, position)
        disk, directory_disk, disk_entries, entries, size, offset = values[1:7]
        if disk != 0 or directory_disk != 0 or disk_entries != entries:
            _fail(artifact, "wheel archive is malformed: central directory")
        if 0xFFFF in (disk_entries, entries) or 0xFFFFFFFF in (size, offset):
            entries, size, offset, directory_end = _read_zip64_directory(
                stream, eocd_offset=eocd_offset, artifact=artifact
            )
        else:
            directory_end = eocd_offset
        if entries > MAX_ARCHIVE_MEMBERS:
            _fail(artifact, "archive contains more than 4096 members")
        if offset > directory_end or size > directory_end - offset:
            _fail(artifact, "wheel archive is malformed: central directory")
        _scan_zip_directory(
            stream,
            offset=offset,
            size=size,
            declared_entries=entries,
            artifact=artifact,
        )
    except OSError as error:
        _fail(artifact, f"wheel archive is malformed: {type(error).__name__}")


def _read_bounded(
    stream: _Readable,
    artifact: str,
    member: str,
    *,
    declared_size: int,
    total: int,
) -> bytes:
    if declared_size > MAX_MEMBER_BYTES:
        _fail(artifact, f"archive member exceeds 64 MiB uncompressed: {member}")
    if total + declared_size > MAX_TOTAL_BYTES:
        _fail(artifact, "archive exceeds 512 MiB total uncompressed")
    data = bytearray()
    while chunk := stream.read(READ_CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > MAX_MEMBER_BYTES:
            _fail(artifact, f"archive member exceeds 64 MiB uncompressed: {member}")
        if total + len(data) > MAX_TOTAL_BYTES:
            _fail(artifact, "archive exceeds 512 MiB total uncompressed")
    return bytes(data)


def _read_wheel(path: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    declared_total = 0
    total = 0
    try:
        with path.open("rb") as source, tempfile.TemporaryFile("w+b") as wheel_stream:
            observed_size = 0
            while chunk := source.read(READ_CHUNK_BYTES):
                observed_size += len(chunk)
                _check_archive_size(observed_size, path.name)
                wheel_stream.write(chunk)
            wheel_stream.seek(0)
            _preflight_zip_directory(wheel_stream, path.name)
            wheel_stream.seek(0)
            with zipfile.ZipFile(wheel_stream) as archive:
                items = archive.infolist()
                if len(items) > MAX_ARCHIVE_MEMBERS:
                    _fail(path.name, "archive contains more than 4096 members")
                for item in items:
                    name = _validate_member_name(item.filename, path.name)
                    unix_type = (
                        stat.S_IFMT(item.external_attr >> 16)
                        if item.create_system == 3
                        else 0
                    )
                    if item.is_dir() or unix_type not in {0, stat.S_IFREG}:
                        _fail(path.name, f"wheel member must be a regular file: {name}")
                    if name in members:
                        _fail(path.name, f"duplicate archive member: {name}")
                    declared_total += item.file_size
                    if declared_total > MAX_TOTAL_BYTES:
                        _fail(path.name, "archive exceeds 512 MiB total uncompressed")
                    with archive.open(item) as stream:
                        members[name] = _read_bounded(
                            stream,
                            path.name,
                            name,
                            declared_size=item.file_size,
                            total=total,
                        )
                    total += len(members[name])
    except (
        OSError,
        zipfile.BadZipFile,
        RuntimeError,
        NotImplementedError,
        zlib.error,
    ) as error:
        _fail(path.name, f"wheel archive is malformed: {type(error).__name__}")
    return members


def _consume_tar_bytes(stream: _Readable, size: int, artifact: str) -> None:
    remaining = size
    while remaining:
        chunk = stream.read(min(READ_CHUNK_BYTES, remaining))
        if not chunk:
            _fail(artifact, "sdist archive is truncated")
        remaining -= len(chunk)


def _scan_sdist_limits(path: Path) -> None:
    count = 0
    declared_total = 0
    try:
        with gzip.open(path, "rb") as stream:
            while header := stream.read(tarfile.BLOCKSIZE):
                if len(header) != tarfile.BLOCKSIZE:
                    _fail(path.name, "sdist archive has a truncated header")
                if header == tarfile.NUL * tarfile.BLOCKSIZE:
                    break
                item = tarfile.TarInfo.frombuf(header, "utf-8", "surrogateescape")
                count += 1
                if count > MAX_ARCHIVE_MEMBERS:
                    _fail(path.name, "archive contains more than 4096 members")
                if item.size > MAX_MEMBER_BYTES:
                    _fail(
                        path.name,
                        f"archive member exceeds 64 MiB uncompressed: {item.name}",
                    )
                declared_total += item.size
                if declared_total > MAX_TOTAL_BYTES:
                    _fail(path.name, "archive exceeds 512 MiB total uncompressed")
                padded_size = (item.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
                _consume_tar_bytes(stream, padded_size * tarfile.BLOCKSIZE, path.name)
    except (
        OSError,
        EOFError,
        gzip.BadGzipFile,
        tarfile.HeaderError,
        zlib.error,
    ) as error:
        _fail(path.name, f"sdist archive is malformed: {type(error).__name__}")


def _read_sdist(path: Path) -> tuple[str, dict[str, bytes]]:
    members: dict[str, bytes] = {}
    root: str | None = None
    declared_total = 0
    total = 0
    try:
        _check_archive_input(path)
        _scan_sdist_limits(path)
        with tarfile.open(path, mode="r:gz") as archive:
            for count, item in enumerate(archive, start=1):
                if count > MAX_ARCHIVE_MEMBERS:
                    _fail(path.name, "archive contains more than 4096 members")
                name = _validate_member_name(item.name, path.name)
                if item.issym() or item.islnk():
                    _validate_member_name(item.linkname, path.name)
                    _fail(path.name, f"sdist links are forbidden: {name}")
                if not item.isfile():
                    _fail(path.name, f"sdist member must be a regular file: {name}")
                parts = name.split("/", 1)
                if len(parts) != 2:
                    _fail(
                        path.name, f"sdist member lacks one top-level directory: {name}"
                    )
                root = root or parts[0]
                if parts[0] != root or parts[1] in members:
                    _fail(path.name, f"sdist root or member is not unique: {name}")
                declared_total += item.size
                if declared_total > MAX_TOTAL_BYTES:
                    _fail(path.name, "archive exceeds 512 MiB total uncompressed")
                extracted = archive.extractfile(item)
                if extracted is None:
                    _fail(path.name, f"sdist member is unreadable: {name}")
                members[parts[1]] = _read_bounded(
                    extracted,
                    path.name,
                    name,
                    declared_size=item.size,
                    total=total,
                )
                total += len(members[parts[1]])
    except (OSError, tarfile.TarError) as error:
        _fail(path.name, f"sdist archive is malformed: {type(error).__name__}")
    if root is None:
        _fail(path.name, "sdist archive is empty")
    return root, members


def _parse_message(data: bytes, artifact: str, member: str) -> Message:
    try:
        message = BytesParser(policy=default).parsebytes(data)
    except Exception as error:  # noqa: BLE001 - any parse failure is reported through _fail with the artifact named
        _fail(artifact, f"{member} is malformed: {type(error).__name__}")
    if message.defects:
        _fail(artifact, f"{member} has parser defects")
    return message


def _metadata(data: bytes, artifact: str, member: str) -> Message:
    message = _parse_message(data, artifact, member)
    for field in SINGLETON_METADATA:
        if len(message.get_all(field, [])) != 1:
            _fail(artifact, f"{member} must contain exactly one {field}")
    if message["Metadata-Version"] != CORE_METADATA_VERSION:
        _fail(artifact, f"{member} Metadata-Version must be {CORE_METADATA_VERSION}")
    return message


def _requirements(message: Message, artifact: str) -> set[str]:
    try:
        return {
            str(Requirement(value)) for value in message.get_all("Requires-Dist", [])
        }
    except Exception as error:  # noqa: BLE001 - any parse failure is reported through _fail with the artifact named
        _fail(artifact, f"Requires-Dist is malformed: {type(error).__name__}")


def _project_configuration(root: Path) -> ProjectConfiguration:
    try:
        with (root / "pyproject.toml").open("rb") as file:
            document = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        _fail(
            "pyproject.toml", f"project metadata is unreadable: {type(error).__name__}"
        )
    project = document.get("project")
    if not isinstance(project, dict):
        _fail("pyproject.toml", "project must be a table")

    def required_string(field: str) -> str:
        value = project.get(field)
        if not isinstance(value, str) or not value:
            _fail("pyproject.toml", f"project.{field} must be a non-empty string")
        return value

    name = required_string("name")
    requires_python = required_string("requires-python")
    license_expression = required_string("license")
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list) or not all(
        isinstance(value, str) for value in dependencies
    ):
        _fail("pyproject.toml", "project.dependencies must be a list of strings")
    optional_dependencies = project.get("optional-dependencies", {})
    if not isinstance(optional_dependencies, dict) or not all(
        isinstance(extra, str)
        and extra
        and isinstance(requirements, list)
        and all(isinstance(value, str) for value in requirements)
        for extra, requirements in optional_dependencies.items()
    ):
        _fail(
            "pyproject.toml",
            "project.optional-dependencies must map extras to lists of strings",
        )
    scripts = project.get("scripts")
    if (
        not isinstance(scripts, dict)
        or not scripts
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in scripts.items()
        )
    ):
        _fail("pyproject.toml", "project.scripts must be a non-empty string mapping")
    try:
        normalized = {str(Requirement(value)) for value in dependencies}
        for extra, requirements in optional_dependencies.items():
            normalized.update(
                str(Requirement(f'{value}; extra == "{extra}"'))
                for value in requirements
            )
    except InvalidRequirement:
        _fail("pyproject.toml", "project dependencies contain an invalid requirement")
    return ProjectConfiguration(
        name,
        requires_python,
        license_expression,
        normalized,
        set(optional_dependencies),
        scripts,
    )


def _validate_metadata(
    distribution: Distribution,
    project: ProjectConfiguration,
    artifact: str,
) -> None:
    message = distribution.metadata
    if _canonical_name(message["Name"]) != PROJECT_NAME:
        _fail(artifact, "project name is inconsistent")
    if _valid_version(message["Version"], artifact) != distribution.version:
        _fail(artifact, "metadata version is inconsistent")
    expected_name = _canonical_name(project.name)
    if (
        expected_name != PROJECT_NAME
        or message["Requires-Python"] != project.requires_python
    ):
        _fail(artifact, "project name or Requires-Python differs from pyproject.toml")
    if message["License-Expression"] != project.license_expression:
        _fail(artifact, "license expression differs from pyproject.toml")
    if _requirements(message, artifact) != project.dependencies:
        _fail(artifact, "runtime dependencies differ from pyproject.toml")
    if set(message.get_all("Provides-Extra", [])) != project.extras:
        _fail(artifact, "optional extras differ from pyproject.toml")


def _parse_wheel_filename(path: Path) -> tuple[str, str, str]:
    match = re.fullmatch(
        r"(?P<name>[^-]+)-(?P<version>[^-]+)-(?P<tag>[^-]+-[^-]+-[^-]+)\.whl",
        path.name,
    )
    if match is None:
        _fail(path.name, "wheel filename is malformed")
    return (
        _canonical_name(match["name"]),
        _valid_version(match["version"], path.name),
        match["tag"],
    )


def _parse_sdist_filename(path: Path) -> tuple[str, str]:
    match = re.fullmatch(r"(?P<name>.+)-(?P<version>[^-]+)\.tar\.gz", path.name)
    if match is None:
        _fail(path.name, "sdist filename is malformed")
    return _canonical_name(match["name"]), _valid_version(match["version"], path.name)


def _dist_info(members: dict[str, bytes], artifact: str) -> str:
    roots = {name.split("/", 1)[0] for name in members if ".dist-info/" in name}
    if len(roots) != 1:
        _fail(artifact, "wheel must contain exactly one .dist-info directory")
    root = roots.pop()
    if not root.endswith(".dist-info"):
        _fail(artifact, "wheel metadata directory is malformed")
    return root


def _wheel_record(members: dict[str, bytes], root: str, artifact: str) -> None:
    record_name = f"{root}/RECORD"
    try:
        rows = list(csv.reader(members[record_name].decode().splitlines()))
    except (KeyError, UnicodeDecodeError, csv.Error) as error:
        _fail(artifact, f"RECORD is missing or malformed: {type(error).__name__}")
    if any(len(row) != 3 for row in rows) or len({row[0] for row in rows}) != len(rows):
        _fail(artifact, "RECORD rows must be unique triples")
    records = {row[0]: (row[1], row[2]) for row in rows}
    if set(records) != set(members):
        _fail(artifact, "RECORD member set is inconsistent")
    for name, data in members.items():
        digest, size = records[name]
        if name == record_name:
            if digest or size:
                _fail(artifact, "RECORD self-row must omit digest and size")
            continue
        encoded = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .rstrip(b"=")
            .decode()
        )
        if digest != f"sha256={encoded}" or size != str(len(data)):
            _fail(artifact, f"RECORD digest or size differs for {name}")


def _wheel_file(members: dict[str, bytes], root: str, tag: str, artifact: str) -> None:
    message = _parse_message(members.get(f"{root}/WHEEL", b""), artifact, "WHEEL")
    for field in ("Wheel-Version", "Root-Is-Purelib"):
        if len(message.get_all(field, [])) != 1:
            _fail(artifact, f"WHEEL must contain exactly one {field}")
    if message["Wheel-Version"] != "1.0" or message["Root-Is-Purelib"] != "true":
        _fail(artifact, "WHEEL version or purelib flag is unsupported")
    tags = message.get_all("Tag", [])
    if (
        not tags
        or len(tags) != len(set(tags))
        or set(tags) != {tag}
        or tag != "py3-none-any"
    ):
        _fail(artifact, "WHEEL tags differ from the pure-Python filename tag")


def _scripts_from_wheel(data: bytes, artifact: str) -> dict[str, str]:
    parser = _CaseConfigParser(interpolation=None)
    try:
        parser.read_string(data.decode())
        if parser.sections() != ["console_scripts"]:
            _fail(artifact, "entry_points.txt contains undeclared groups")
        return dict(parser["console_scripts"])
    except (UnicodeDecodeError, configparser.Error, KeyError) as error:
        _fail(artifact, f"entry_points.txt is malformed: {type(error).__name__}")


def _source_members(root: Path) -> dict[str, bytes]:
    package = root / "src" / PACKAGE_NAME
    members = {
        path.relative_to(root / "src").as_posix(): path.read_bytes()
        for path in (
            *package.rglob("*.py"),
            *(package / name for name in PACKAGE_RESOURCES),
        )
        if path.is_file()
    }
    for sentinel in PACKAGE_SENTINELS:
        name = f"{PACKAGE_NAME}/{sentinel}"
        if name not in members:
            _fail("source checkout", f"missing package sentinel: {name}")
    return members


def _validate_wheel(
    path: Path,
    project_root: Path,
    project: ProjectConfiguration,
) -> Distribution:
    name, version, tag = _parse_wheel_filename(path)
    members = _read_wheel(path)
    root = _dist_info(members, path.name)
    expected_root = f"{PACKAGE_NAME}-{version}.dist-info"
    if name != PROJECT_NAME or root != expected_root:
        _fail(path.name, "wheel filename and .dist-info identity differ")
    metadata = _metadata(members.get(f"{root}/METADATA", b""), path.name, "METADATA")
    distribution = Distribution(name, version, metadata, members)
    _validate_metadata(distribution, project, path.name)
    _wheel_file(members, root, tag, path.name)
    _wheel_record(members, root, path.name)
    source = _source_members(project_root)
    expected = set(source) | {
        f"{root}/METADATA",
        f"{root}/WHEEL",
        f"{root}/entry_points.txt",
        f"{root}/licenses/LICENSE",
        f"{root}/RECORD",
    }
    if set(members) != expected:
        _fail(path.name, "wheel member set is not closed")
    for member, data in source.items():
        if members[member] != data:
            _fail(path.name, f"package bytes differ for {member}")
    if members[f"{root}/licenses/LICENSE"] != (project_root / "LICENSE").read_bytes():
        _fail(path.name, "wheel license bytes differ from checkout")
    if (
        _scripts_from_wheel(members[f"{root}/entry_points.txt"], path.name)
        != project.scripts
    ):
        _fail(path.name, "wheel console scripts differ from pyproject.toml")
    return distribution


def _validate_sdist(
    path: Path,
    project_root: Path,
    project: ProjectConfiguration,
) -> Distribution:
    name, version = _parse_sdist_filename(path)
    root, members = _read_sdist(path)
    expected_root = f"{PACKAGE_NAME}-{version}"
    if name != PROJECT_NAME or root != expected_root:
        _fail(path.name, "sdist filename and root identity differ")
    metadata = _metadata(members.get("PKG-INFO", b""), path.name, "PKG-INFO")
    distribution = Distribution(name, version, metadata, members)
    _validate_metadata(distribution, project, path.name)
    source = _source_members(project_root)
    sdist_source = {f"src/{name}": data for name, data in source.items()}
    expected = set(sdist_source) | set(SDIST_INPUTS) | {"PKG-INFO"}
    if set(members) != expected:
        _fail(path.name, "sdist member set is not closed")
    for member, data in sdist_source.items():
        if members[member] != data:
            _fail(path.name, f"package bytes differ for {member}")
    for member in SDIST_INPUTS:
        if members[member] != (project_root / member).read_bytes():
            _fail(path.name, f"sdist input bytes differ for {member}")
    try:
        embedded = tomllib.loads(members["pyproject.toml"].decode())
        scripts = embedded["project"]["scripts"]
    except (UnicodeDecodeError, KeyError, tomllib.TOMLDecodeError) as error:
        _fail(
            path.name, f"embedded pyproject.toml is malformed: {type(error).__name__}"
        )
    if scripts != project.scripts:
        _fail(path.name, "sdist console scripts differ from checkout")
    return distribution


def validate(artifact_dir: Path, project_root: Path, *, release: bool = False) -> None:
    if not artifact_dir.is_dir():
        _fail(str(artifact_dir), "artifact directory does not exist")
    if not project_root.is_dir():
        _fail(str(project_root), "project root does not exist")
    artifacts = [path for path in artifact_dir.iterdir() if path.name != ".gitignore"]
    wheels = [path for path in artifacts if path.name.endswith(".whl")]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(artifacts) != 2 or len(wheels) != 1 or len(sdists) != 1:
        _fail(str(artifact_dir), "expected exactly one wheel and one .tar.gz sdist")
    project = _project_configuration(project_root)
    wheel = _validate_wheel(wheels[0], project_root, project)
    sdist = _validate_sdist(sdists[0], project_root, project)
    if wheel.name != sdist.name or wheel.version != sdist.version:
        _fail(str(artifact_dir), "wheel and sdist identity or version differs")
    if release:
        project_root = project_root.resolve()
        result = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts/check_capability_inventory.py"),
                "--release",
            ],
            cwd=project_root,
            env={**os.environ, "PYTHONPATH": str(project_root / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            _fail(
                "release verification",
                result.stderr.strip()
                or result.stdout.strip()
                or "inventory check failed",
            )


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    release = len(args) == 3 and args[-1] == "--release"
    if len(args) != 2 and not release:
        print(
            "usage: validate_release_artifacts.py ARTIFACT_DIR PROJECT_ROOT [--release]",
            file=sys.stderr,
        )
        return 2
    try:
        validate(Path(args[0]), Path(args[1]), release=release)
    except (OSError, ValidationError) as error:
        print(f"artifact validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
