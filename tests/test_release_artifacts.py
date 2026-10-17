import base64
import copy
import csv
import hashlib
import io
import os
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import pytest
import validate_release_artifacts as validator
from validate_release_artifacts import _read_bounded, main

ROOT = Path(__file__).parents[1]


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.pop("VIRTUAL_ENV", None)
    return subprocess.run(
        args,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _clean_project(project: Path) -> Path:
    project.mkdir(exist_ok=True)
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    for relative in filter(None, tracked):
        source = ROOT / relative
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    # The build reads no Git state (ADR 0033), so the fixture needs no commit. It
    # still needs a repository: `just setup` runs `prek install`, which requires one.
    # The repository-local hooks path keeps that install working on machines whose
    # global config sets core.hooksPath, which prek otherwise refuses to install over.
    for command in (
        ("git", "init", "--quiet", "--initial-branch=main"),
        ("git", "config", "--local", "core.hooksPath", str(project / ".git" / "hooks")),
    ):
        result = _run(*command, cwd=project)
        assert result.returncode == 0, result.stderr
    return project


@pytest.fixture(scope="module")
def built_project(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    project = _clean_project(tmp_path_factory.mktemp("release-project"))

    artifacts = project / "dist"
    result = _run(
        "uv",
        "build",
        "--wheel",
        "--sdist",
        "--out-dir",
        str(artifacts),
        ".",
        cwd=project,
    )
    assert result.returncode == 0, result.stderr
    return artifacts, project


def test_validates_clean_wheel_and_sdist(built_project: tuple[Path, Path]) -> None:
    artifacts, project = built_project

    assert main([str(artifacts), str(project)]) == 0


def test_built_artifacts_ship_the_pep_561_marker(
    built_project: tuple[Path, Path],
) -> None:
    """PEP 561: the marker is only meaningful where a consumer reads it — the
    built distribution, not the source checkout."""
    artifacts, _ = built_project

    wheel = validator._read_wheel(next(artifacts.glob("*.whl")))
    _, sdist = validator._read_sdist(next(artifacts.glob("*.tar.gz")))

    assert wheel["hmcpctl/py.typed"] == b""
    assert sdist["src/hmcpctl/py.typed"] == b""


def test_built_artifacts_ship_the_operation_maturity_projection(
    built_project: tuple[Path, Path],
) -> None:
    artifacts, project = built_project
    resource = (project / "src" / "hmcpctl" / "_operation_maturity.json").read_bytes()

    wheel = validator._read_wheel(next(artifacts.glob("*.whl")))
    _, sdist = validator._read_sdist(next(artifacts.glob("*.tar.gz")))

    assert wheel["hmcpctl/_operation_maturity.json"] == resource
    assert sdist["src/hmcpctl/_operation_maturity.json"] == resource


def _artifact_copy(
    tmp_path: Path,
    built_project: tuple[Path, Path],
) -> tuple[Path, Path]:
    artifacts, project = built_project
    copied = tmp_path / "dist"
    shutil.copytree(artifacts, copied)
    return copied, project


def _record_bytes(members: dict[str, bytes]) -> bytes:
    record = next(name for name in members if name.endswith(".dist-info/RECORD"))
    rows: list[list[str]] = []
    for name, data in members.items():
        if name == record:
            rows.append([name, "", ""])
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        rows.append([name, f"sha256={digest.decode()}", str(len(data))])
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return stream.getvalue().encode()


def _rewrite_wheel(
    path: Path,
    mutate: Callable[[dict[str, bytes]], None],
    *,
    repair_record: bool = True,
) -> None:
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    mutate(members)
    if repair_record:
        record = next(name for name in members if name.endswith(".dist-info/RECORD"))
        members[record] = _record_bytes(members)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _rewrite_wheel_mode(path: Path, suffix: str, mode: int) -> None:
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    record = next(name for name in members if name.endswith(".dist-info/RECORD"))
    members[record] = _record_bytes(members)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            if name.endswith(suffix):
                item = zipfile.ZipInfo(name)
                item.create_system = 3
                item.external_attr = mode << 16
                archive.writestr(item, data)
            else:
                archive.writestr(name, data)


def _tar_data(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    if not member.isfile():
        return b""
    extracted = archive.extractfile(member)
    assert extracted is not None
    return extracted.read()


def _rewrite_sdist(
    path: Path,
    mutate: Callable[[list[tuple[tarfile.TarInfo, bytes]]], None],
) -> None:
    with tarfile.open(path, "r:gz") as archive:
        entries = [
            (member, _tar_data(archive, member)) for member in archive.getmembers()
        ]
    mutate(entries)
    with tarfile.open(path, "w:gz") as archive:
        for member, data in entries:
            archive.addfile(member, io.BytesIO(data) if member.isfile() else None)


def _assert_invalid(
    artifacts: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
    invariant: str,
) -> None:
    assert main([str(artifacts), str(project)]) == 1
    assert invariant in capsys.readouterr().err


def test_rejects_wrong_cli_arity(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "ARTIFACT_DIR PROJECT_ROOT" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["artifacts", "project"])
def test_rejects_missing_input_directory(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    artifacts, project = built_project
    absent = tmp_path / "absent"
    arguments = (
        [str(absent), str(project)]
        if missing == "artifacts"
        else [str(artifacts), str(absent)]
    )

    assert main(arguments) == 1
    assert "does not exist" in capsys.readouterr().err


def test_rejects_unexpected_artifact(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    (artifacts / "unexpected.zip").write_bytes(b"not an artifact")

    _assert_invalid(artifacts, project, capsys, "exactly one wheel")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_rejects_missing_artifact(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    artifact: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    pattern = "*.whl" if artifact == "wheel" else "*.tar.gz"
    next(artifacts.glob(pattern)).rename(tmp_path / f"removed-{artifact}")

    _assert_invalid(artifacts, project, capsys, "exactly one wheel")


def test_rejects_duplicate_wheel(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    shutil.copy2(wheel, artifacts / f"duplicate-{wheel.name}")

    _assert_invalid(artifacts, project, capsys, "exactly one wheel")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_rejects_corrupt_archive(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    artifact: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    pattern = "*.whl" if artifact == "wheel" else "*.tar.gz"
    next(artifacts.glob(pattern)).write_bytes(b"not an archive")

    _assert_invalid(artifacts, project, capsys, f"{artifact} archive is malformed")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    ("limit", "invariant"),
    [
        ("input", "archive input exceeds 256 MiB"),
        ("members", "archive contains more than 4096 members"),
        ("member-size", "archive member exceeds 64 MiB uncompressed"),
        ("total-size", "archive exceeds 512 MiB total uncompressed"),
    ],
)
def test_rejects_declared_archive_limit_overrun(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
    limit: str,
    invariant: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    path = next(artifacts.glob("*.whl" if artifact == "wheel" else "*.tar.gz"))
    if limit == "input":
        monkeypatch.setattr(validator, "MAX_ARCHIVE_BYTES", path.stat().st_size - 1)
    elif limit == "members":
        monkeypatch.setattr(validator, "MAX_ARCHIVE_MEMBERS", 1)
    elif limit == "member-size":
        monkeypatch.setattr(validator, "MAX_MEMBER_BYTES", 1)
    else:
        monkeypatch.setattr(validator, "MAX_TOTAL_BYTES", 1)

    _assert_invalid(artifacts, project, capsys, invariant)


def _eocd_offset(payload: bytes | bytearray) -> int:
    offset = payload.rfind(b"PK\x05\x06")
    assert offset >= 0
    return offset


def test_rejects_excessive_zip_entry_count_before_member_enumeration(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, _ = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    payload = bytearray(wheel.read_bytes())
    eocd = _eocd_offset(payload)
    payload[eocd + 8 : eocd + 12] = struct.pack("<HH", 1, 1)
    wheel.write_bytes(payload)
    monkeypatch.setattr(validator, "MAX_ARCHIVE_MEMBERS", 1)

    def reject_enumeration(_archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        raise AssertionError("members enumerated before the directory was bounded")

    monkeypatch.setattr(zipfile.ZipFile, "infolist", reject_enumeration)

    with pytest.raises(
        validator.ValidationError, match="archive contains more than 4096 members"
    ):
        validator._read_wheel(wheel)


def test_preflight_and_member_enumeration_share_one_open_wheel(
    built_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, _ = built_project
    wheel = next(artifacts.glob("*.whl"))
    original_preflight = validator._preflight_zip_directory
    original_zip_file = zipfile.ZipFile
    preflight_source: object | None = None
    enumeration_source: object | None = None

    def capture_preflight(source: BinaryIO, artifact: str) -> None:
        nonlocal preflight_source
        preflight_source = source
        original_preflight(source, artifact)

    def capture_zip_file(source: BinaryIO) -> zipfile.ZipFile:
        nonlocal enumeration_source
        enumeration_source = source
        return original_zip_file(source)

    monkeypatch.setattr(validator, "_preflight_zip_directory", capture_preflight)
    monkeypatch.setattr(zipfile, "ZipFile", capture_zip_file)

    validator._read_wheel(wheel)

    assert preflight_source is enumeration_source
    assert not isinstance(preflight_source, Path)


def test_wheel_input_limit_uses_the_open_stream(
    built_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, _ = built_project
    wheel = next(artifacts.glob("*.whl"))

    def reject_path_size_check(_path: Path) -> None:
        raise AssertionError("wheel size checked through its replaceable path")

    monkeypatch.setattr(validator, "_check_archive_input", reject_path_size_check)

    validator._read_wheel(wheel)


def test_member_enumeration_uses_an_immutable_wheel_snapshot(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, _ = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    original_preflight = validator._preflight_zip_directory

    def mutate_source_after_preflight(source: BinaryIO, artifact: str) -> None:
        original_preflight(source, artifact)
        wheel.write_bytes(b"replaced after preflight")

    monkeypatch.setattr(
        validator, "_preflight_zip_directory", mutate_source_after_preflight
    )

    assert validator._read_wheel(wheel)


@pytest.mark.parametrize("field_offset", [12, 16])
def test_rejects_malformed_zip_directory_bounds(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    field_offset: int,
) -> None:
    artifacts, _ = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    payload = bytearray(wheel.read_bytes())
    eocd = _eocd_offset(payload)
    payload[eocd + field_offset : eocd + field_offset + 4] = struct.pack(
        "<L", len(payload)
    )
    wheel.write_bytes(payload)

    with pytest.raises(
        validator.ValidationError, match="wheel archive is malformed: central directory"
    ):
        validator._read_wheel(wheel)


def test_accepts_zip64_entry_count_at_exact_boundary(tmp_path: Path) -> None:
    directory_record = b"PK\x01\x02" + bytes(42)
    directory = directory_record * validator.MAX_ARCHIVE_MEMBERS
    zip64_record = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        validator.MAX_ARCHIVE_MEMBERS,
        validator.MAX_ARCHIVE_MEMBERS,
        len(directory),
        0,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, len(directory), 1)
    eocd = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )
    wheel = tmp_path / "boundary.whl"
    wheel.write_bytes(directory + zip64_record + locator + eocd)

    with wheel.open("rb") as stream:
        validator._preflight_zip_directory(stream, wheel.name)


def test_rejects_malformed_zip64_record_offset(tmp_path: Path) -> None:
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 1, 1)
    eocd = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )
    wheel = tmp_path / "malformed.whl"
    wheel.write_bytes(locator + eocd)

    with (
        pytest.raises(
            validator.ValidationError, match="wheel archive is malformed: ZIP64 record"
        ),
        wheel.open("rb") as stream,
    ):
        validator._preflight_zip_directory(stream, wheel.name)


@pytest.mark.parametrize(
    ("limit", "invariant"),
    [
        ("member", "member exceeds 64 MiB uncompressed"),
        ("total", "archive exceeds 512 MiB total uncompressed"),
    ],
)
def test_rejects_observed_overrun_during_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    invariant: str,
) -> None:
    monkeypatch.setattr(validator, "MAX_MEMBER_BYTES", 1 if limit == "member" else 10)
    monkeypatch.setattr(validator, "MAX_TOTAL_BYTES", 1)

    with pytest.raises(validator.ValidationError, match=invariant):
        _read_bounded(
            io.BytesIO(b"ab"), "artifact.whl", "member", declared_size=1, total=0
        )


def test_accepts_declared_archive_limits_at_exact_boundaries(
    built_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, project = built_project
    wheel = next(artifacts.glob("*.whl"))
    sdist = next(artifacts.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_sizes = [item.file_size for item in archive.infolist()]
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_sizes = [item.size for item in archive if item.isfile()]
    monkeypatch.setattr(
        validator, "MAX_ARCHIVE_BYTES", max(wheel.stat().st_size, sdist.stat().st_size)
    )
    monkeypatch.setattr(
        validator, "MAX_ARCHIVE_MEMBERS", max(len(wheel_sizes), len(sdist_sizes))
    )
    monkeypatch.setattr(validator, "MAX_MEMBER_BYTES", max(wheel_sizes + sdist_sizes))
    monkeypatch.setattr(
        validator, "MAX_TOTAL_BYTES", max(sum(wheel_sizes), sum(sdist_sizes))
    )

    assert main([str(artifacts), str(project)]) == 0


def test_rejects_hidden_pax_payload_before_tarfile_member_reads(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, _ = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))
    with tarfile.open(
        sdist,
        "w:gz",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": "x" * 1024},
    ) as archive:
        member = tarfile.TarInfo("hmcpctl-1.0/README.md")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    monkeypatch.setattr(validator, "MAX_MEMBER_BYTES", 100)

    def reject_tarfile_open(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("tarfile opened before hidden records were bounded")

    monkeypatch.setattr(validator.tarfile, "open", reject_tarfile_open)

    with pytest.raises(
        validator.ValidationError, match="archive member exceeds 64 MiB uncompressed"
    ):
        validator._read_sdist(sdist)


def test_rejects_unsupported_wheel_compression_actionably(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    payload = bytearray(wheel.read_bytes())
    for signature, compression_offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        position = 0
        while (position := payload.find(signature, position)) != -1:
            payload[
                position + compression_offset : position + compression_offset + 2
            ] = (99).to_bytes(2, "little")
            position += len(signature)
    wheel.write_bytes(payload)

    _assert_invalid(
        artifacts, project, capsys, "wheel archive is malformed: NotImplementedError"
    )


def test_rejects_corrupt_deflate_payload_actionably(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    payload = bytearray(wheel.read_bytes())
    with zipfile.ZipFile(wheel) as archive:
        item = next(
            entry
            for entry in archive.infolist()
            if entry.compress_type == zipfile.ZIP_DEFLATED
        )
    header = item.header_offset
    name_size = int.from_bytes(payload[header + 26 : header + 28], "little")
    extra_size = int.from_bytes(payload[header + 28 : header + 30], "little")
    compressed_start = header + 30 + name_size + extra_size
    payload[compressed_start] |= 0b00000110
    wheel.write_bytes(payload)

    _assert_invalid(artifacts, project, capsys, "wheel archive is malformed: error")


def test_rejects_duplicate_wheel_member(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        entries = [(item, archive.read(item)) for item in archive.infolist()]
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item, data in entries:
            archive.writestr(item, data)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(entries[0][0].filename, entries[0][1])

    _assert_invalid(artifacts, project, capsys, "duplicate archive member")


def test_rejects_duplicate_sdist_member(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))

    def duplicate(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        member, data = entries[0]
        entries.append((copy.copy(member), data))

    _rewrite_sdist(sdist, duplicate)
    _assert_invalid(artifacts, project, capsys, "sdist root or member is not unique")


@pytest.mark.parametrize(
    ("identity", "invariant"),
    [
        ("other", "wheel filename and .dist-info identity differ"),
        ("version", "wheel filename and .dist-info identity differ"),
    ],
)
def test_rejects_wheel_filename_identity_mismatch(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    identity: str,
    invariant: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    parts = wheel.name.split("-")
    parts[0 if identity == "other" else 1] = "other" if identity == "other" else "9.9.9"
    wheel.rename(artifacts / "-".join(parts))

    _assert_invalid(artifacts, project, capsys, invariant)


@pytest.mark.parametrize(
    ("location", "invariant"),
    [
        ("wheel-filename", "wheel filename and .dist-info identity differ"),
        ("wheel-root", "wheel filename and .dist-info identity differ"),
        ("wheel-metadata", "metadata version is inconsistent"),
        ("sdist-filename", "sdist filename and root identity differ"),
        ("sdist-root", "sdist filename and root identity differ"),
        ("sdist-metadata", "metadata version is inconsistent"),
    ],
)
def test_rejects_each_independent_version_location_mutation(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    location: str,
    invariant: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    sdist = next(artifacts.glob("*.tar.gz"))
    if location == "wheel-filename":
        parts = wheel.name.split("-")
        parts[1] = "9.9.9"
        wheel.rename(artifacts / "-".join(parts))
    elif location == "wheel-root":

        def change_wheel_root(members: dict[str, bytes]) -> None:
            for name in list(members):
                if ".dist-info/" in name:
                    members[name.replace(".dist-info/", ".changed.dist-info/")] = (
                        members.pop(name)
                    )

        _rewrite_wheel(wheel, change_wheel_root)
    elif location == "wheel-metadata":

        def change_wheel_metadata(members: dict[str, bytes]) -> None:
            name = next(
                item for item in members if item.endswith(".dist-info/METADATA")
            )
            members[name] = re.sub(
                rb"(?m)^Version: .+$", b"Version: 9.9.9", members[name]
            )

        _rewrite_wheel(wheel, change_wheel_metadata)
    elif location == "sdist-filename":
        sdist.rename(
            artifacts / re.sub(r"-[^-]+\.tar\.gz$", "-9.9.9.tar.gz", sdist.name)
        )
    elif location == "sdist-root":
        old_root = sdist.name.removesuffix(".tar.gz")

        def change_sdist_root(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
            for member, _ in entries:
                member.name = f"hmcpctl-9.9.9{member.name.removeprefix(old_root)}"

        _rewrite_sdist(sdist, change_sdist_root)
    else:

        def change_sdist_metadata(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
            for index, (member, data) in enumerate(entries):
                if member.name.endswith("/PKG-INFO"):
                    changed = re.sub(rb"(?m)^Version: .+$", b"Version: 9.9.9", data)
                    member.size = len(changed)
                    entries[index] = (member, changed)

        _rewrite_sdist(sdist, change_sdist_metadata)

    _assert_invalid(artifacts, project, capsys, invariant)


def test_rejects_synchronized_invalid_version_across_all_six_locations(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    wheel_parts = wheel.name.split("-")
    wheel_parts[1] = "1..0"

    def change_wheel(members: dict[str, bytes]) -> None:
        for name in list(members):
            changed_name = re.sub(r"(?<=hmcpctl-)[^/]+(?=\.dist-info/)", "1..0", name)
            data = members.pop(name)
            if changed_name.endswith(".dist-info/METADATA"):
                data = re.sub(rb"(?m)^Version: .+$", b"Version: 1..0", data)
            members[changed_name] = data

    _rewrite_wheel(wheel, change_wheel)
    wheel.rename(artifacts / "-".join(wheel_parts))

    sdist = next(artifacts.glob("*.tar.gz"))
    old_root = sdist.name.removesuffix(".tar.gz")
    new_root = "hmcpctl-1..0"

    def change_sdist(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        for index, (member, data) in enumerate(entries):
            member.name = new_root + member.name.removeprefix(old_root)
            if member.name.endswith("/PKG-INFO"):
                changed = re.sub(rb"(?m)^Version: .+$", b"Version: 1..0", data)
                member.size = len(changed)
                entries[index] = (member, changed)

    _rewrite_sdist(sdist, change_sdist)
    sdist.rename(artifacts / f"{new_root}.tar.gz")

    _assert_invalid(artifacts, project, capsys, "version must be valid PEP 440")


def test_rejects_record_digest_mismatch(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    def corrupt_record(members: dict[str, bytes]) -> None:
        record = next(name for name in members if name.endswith(".dist-info/RECORD"))
        members[record] = members[record].replace(b"sha256=", b"sha256=broken", 1)

    _rewrite_wheel(wheel, corrupt_record, repair_record=False)
    _assert_invalid(artifacts, project, capsys, "RECORD digest or size differs")


@pytest.mark.parametrize(
    ("mutation", "invariant"),
    [
        ("missing", "RECORD is missing or malformed"),
        ("extra", "RECORD member set is inconsistent"),
        ("duplicate", "RECORD rows must be unique triples"),
        ("size", "RECORD digest or size differs"),
    ],
)
def test_rejects_invalid_record_structure(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    mutation: str,
    invariant: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    def change_record(members: dict[str, bytes]) -> None:
        name = next(item for item in members if item.endswith(".dist-info/RECORD"))
        if mutation == "missing":
            del members[name]
            return
        rows = list(csv.reader(members[name].decode().splitlines()))
        if mutation == "extra":
            rows.append(["absent.py", "", ""])
        elif mutation == "duplicate":
            rows.append(rows[0])
        else:
            row = next(row for row in rows if row[0] != name)
            row[2] = str(int(row[2]) + 1)
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="\n").writerows(rows)
        members[name] = stream.getvalue().encode()

    _rewrite_wheel(wheel, change_record, repair_record=False)
    _assert_invalid(artifacts, project, capsys, invariant)


def test_rejects_byte_divergent_wheel_package_with_valid_record(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    def change_package(members: dict[str, bytes]) -> None:
        members["hmcpctl/__init__.py"] += b"\n# changed\n"

    _rewrite_wheel(wheel, change_package)
    _assert_invalid(artifacts, project, capsys, "package bytes differ")


def test_rejects_unexpected_wheel_payload_with_valid_record(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    _rewrite_wheel(
        wheel, lambda members: members.__setitem__("other/payload", b"payload")
    )

    _assert_invalid(artifacts, project, capsys, "wheel member set is not closed")


def test_rejects_wheel_that_drops_the_pep_561_marker(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A build-configuration regression that silently omits the marker must fail
    validation rather than ship an untyped distribution."""
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    _rewrite_wheel(wheel, lambda members: members.pop("hmcpctl/py.typed"))

    _assert_invalid(artifacts, project, capsys, "wheel member set is not closed")


def test_rejects_checkout_without_the_pep_561_marker(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    stripped = tmp_path / "stripped"
    shutil.copytree(project, stripped, ignore=shutil.ignore_patterns("dist"))
    (stripped / "src" / "hmcpctl" / "py.typed").unlink()

    _assert_invalid(
        artifacts, stripped, capsys, "missing package sentinel: hmcpctl/py.typed"
    )


@pytest.mark.parametrize(
    "member_name",
    [
        "../escape.py",
        "C:/drive.py",
        "back\\slash.py",
        "empty//component.py",
        "dot/./component.py",
        "unicode/cafe\u0301.py",
    ],
)
def test_rejects_noncanonical_wheel_path(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    member_name: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))
    _rewrite_wheel(wheel, lambda members: members.__setitem__(member_name, b"payload"))

    _assert_invalid(artifacts, project, capsys, "archive member path")


@pytest.mark.parametrize(
    ("suffix", "mode"),
    [
        ("hmcpctl/__init__.py", stat.S_IFLNK | 0o777),
        (".dist-info/WHEEL", stat.S_IFIFO | 0o644),
    ],
)
def test_rejects_non_regular_wheel_member(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    suffix: str,
    mode: int,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    _rewrite_wheel_mode(wheel, suffix, mode)

    _assert_invalid(artifacts, project, capsys, "wheel member must be a regular file")


@pytest.mark.parametrize(
    ("mutation", "invariant"),
    [
        ("missing", "WHEEL must contain exactly one Wheel-Version"),
        ("missing_tag", "WHEEL tags differ"),
        ("duplicate_version", "WHEEL must contain exactly one Wheel-Version"),
        ("version", "WHEEL version or purelib flag is unsupported"),
        ("purelib", "WHEEL version or purelib flag is unsupported"),
        ("tag", "WHEEL tags differ"),
        ("duplicate_tag", "WHEEL tags differ"),
    ],
)
def test_rejects_invalid_wheel_contract_with_valid_record(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    mutation: str,
    invariant: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    def change_wheel(members: dict[str, bytes]) -> None:
        name = next(item for item in members if item.endswith(".dist-info/WHEEL"))
        if mutation == "missing":
            del members[name]
        elif mutation == "missing_tag":
            members[name] = members[name].replace(b"Tag: py3-none-any\n", b"")
        elif mutation == "duplicate_version":
            members[name] += b"Wheel-Version: 1.0\n"
        elif mutation == "version":
            members[name] = members[name].replace(
                b"Wheel-Version: 1.0", b"Wheel-Version: 2.0"
            )
        elif mutation == "purelib":
            members[name] = members[name].replace(
                b"Root-Is-Purelib: true", b"Root-Is-Purelib: false"
            )
        elif mutation == "duplicate_tag":
            members[name] += b"Tag: py3-none-any\n"
        else:
            members[name] = members[name].replace(
                b"Tag: py3-none-any", b"Tag: cp311-none-any"
            )

    _rewrite_wheel(wheel, change_wheel)
    _assert_invalid(artifacts, project, capsys, invariant)


@pytest.mark.parametrize("member_suffix", ["METADATA", "PKG-INFO"])
def test_rejects_duplicate_core_metadata_singleton(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    member_suffix: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    if member_suffix == "METADATA":
        wheel = next(artifacts.glob("*.whl"))

        def duplicate_name(members: dict[str, bytes]) -> None:
            name = next(
                item for item in members if item.endswith(".dist-info/METADATA")
            )
            members[name] = b"Name: conflicting\n" + members[name]

        _rewrite_wheel(wheel, duplicate_name)
    else:
        sdist = next(artifacts.glob("*.tar.gz"))

        def duplicate_name(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
            for index, (member, data) in enumerate(entries):
                if member.name.endswith("/PKG-INFO"):
                    member.size += len(b"Name: conflicting\n")
                    entries[index] = (member, b"Name: conflicting\n" + data)

        _rewrite_sdist(sdist, duplicate_name)
    _assert_invalid(artifacts, project, capsys, "exactly one Name")


@pytest.mark.parametrize(
    "field",
    ["Name", "Version", "Requires-Python", "License-Expression"],
)
def test_rejects_missing_core_metadata_singleton(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    def remove_field(members: dict[str, bytes]) -> None:
        name = next(item for item in members if item.endswith(".dist-info/METADATA"))
        lines = members[name].splitlines(keepends=True)
        members[name] = b"".join(
            line for line in lines if not line.startswith(f"{field}:".encode())
        )

    _rewrite_wheel(wheel, remove_field)
    _assert_invalid(artifacts, project, capsys, f"exactly one {field}")


@pytest.mark.parametrize("member_suffix", ["METADATA", "PKG-INFO"])
def test_rejects_unsupported_core_metadata_version(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    member_suffix: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    if member_suffix == "METADATA":
        wheel = next(artifacts.glob("*.whl"))

        def change_version(members: dict[str, bytes]) -> None:
            name = next(
                item for item in members if item.endswith(".dist-info/METADATA")
            )
            members[name] = members[name].replace(
                b"Metadata-Version: 2.5", b"Metadata-Version: 1.0"
            )

        _rewrite_wheel(wheel, change_version)
    else:
        sdist = next(artifacts.glob("*.tar.gz"))

        def change_version(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
            for index, (member, data) in enumerate(entries):
                if member.name.endswith("/PKG-INFO"):
                    changed = data.replace(
                        b"Metadata-Version: 2.5", b"Metadata-Version: 1.0"
                    )
                    member.size = len(changed)
                    entries[index] = (member, changed)

        _rewrite_sdist(sdist, change_version)
    _assert_invalid(artifacts, project, capsys, "Metadata-Version must be 2.5")


@pytest.mark.parametrize(
    ("field", "old", "new", "invariant"),
    [
        ("Name", b"Name: hmcpctl", b"Name: other", "project name is inconsistent"),
        (
            "Requires-Python",
            b"Requires-Python: >=3.11",
            b"Requires-Python: >=3.12",
            "Requires-Python differs",
        ),
        (
            "License-Expression",
            b"License-Expression: MIT",
            b"License-Expression: Apache-2.0",
            "license expression differs",
        ),
        (
            "Requires-Dist",
            b"Requires-Dist: asyncssh<3,>=2.24.1",
            b"Requires-Dist: asyncssh<3,>=2.24.2",
            "runtime dependencies differ",
        ),
        (
            "Provides-Extra",
            b"Provides-Extra: app",
            b"Provides-Extra: other",
            "optional extras differ",
        ),
    ],
)
def test_rejects_wheel_metadata_mismatch_with_valid_record(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    field: str,
    old: bytes,
    new: bytes,
    invariant: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    def change_metadata(members: dict[str, bytes]) -> None:
        name = next(item for item in members if item.endswith(".dist-info/METADATA"))
        assert old in members[name], field
        members[name] = members[name].replace(old, new, 1)

    _rewrite_wheel(wheel, change_metadata)
    _assert_invalid(artifacts, project, capsys, invariant)


def test_rejects_wheel_entry_point_mismatch_with_valid_record(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    def change_entry_point(members: dict[str, bytes]) -> None:
        name = next(
            item for item in members if item.endswith(".dist-info/entry_points.txt")
        )
        members[name] = members[name].replace(b"hmcpctl:main", b"hmcpctl:missing")

    _rewrite_wheel(wheel, change_entry_point)
    _assert_invalid(artifacts, project, capsys, "console scripts differ")


@pytest.mark.parametrize("mutation", ["case", "extra-group"])
def test_rejects_nonexact_wheel_entry_points_with_valid_record(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    wheel = next(artifacts.glob("*.whl"))

    def change_entry_points(members: dict[str, bytes]) -> None:
        name = next(
            item for item in members if item.endswith(".dist-info/entry_points.txt")
        )
        if mutation == "case":
            members[name] = members[name].replace(b"hmcpctl =", b"HMC-MCP =")
        else:
            members[name] += b"\n[other]\ncommand = hmcpctl:main\n"

    _rewrite_wheel(wheel, change_entry_points)
    expected = "console scripts differ" if mutation == "case" else "undeclared groups"
    _assert_invalid(artifacts, project, capsys, expected)


@pytest.mark.parametrize(
    "member_type",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.DIRTYPE,
    ],
)
def test_rejects_non_regular_sdist_member(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    member_type: bytes,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))

    def add_link(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        root = entries[0][0].name.split("/", 1)[0]
        member = tarfile.TarInfo(f"{root}/unsafe-member")
        member.type = member_type
        if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
            member.linkname = "../../outside"
        entries.append((member, b""))

    _rewrite_sdist(sdist, add_link)
    expected = (
        "archive member path is not canonical"
        if member_type
        in {
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
        }
        else "sdist member must be a regular file"
    )
    _assert_invalid(artifacts, project, capsys, expected)


def test_rejects_sdist_link_with_safe_target(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))

    def add_link(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        root = entries[0][0].name.split("/", 1)[0]
        member = tarfile.TarInfo(f"{root}/safe-looking-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "README.md"
        entries.append((member, b""))

    _rewrite_sdist(sdist, add_link)
    _assert_invalid(artifacts, project, capsys, "sdist links are forbidden")


def test_rejects_byte_divergent_sdist_package(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))

    def change_package(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        for index, (member, data) in enumerate(entries):
            if member.name.endswith("/src/hmcpctl/__init__.py"):
                changed = data + b"\n# changed\n"
                member.size = len(changed)
                entries[index] = (member, changed)

    _rewrite_sdist(sdist, change_package)
    _assert_invalid(artifacts, project, capsys, "package bytes differ")


@pytest.mark.parametrize("mutation", ["missing", "bytes", "nonregular"])
def test_rejects_invalid_required_sdist_input(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))

    def change_input(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        index = next(
            index
            for index, (member, _) in enumerate(entries)
            if member.name.endswith("/README.md")
        )
        member, data = entries[index]
        if mutation == "missing":
            entries.pop(index)
        elif mutation == "bytes":
            changed = data + b"\nchanged\n"
            member.size = len(changed)
            entries[index] = (member, changed)
        else:
            member.type = tarfile.FIFOTYPE
            member.size = 0
            entries[index] = (member, b"")

    _rewrite_sdist(sdist, change_input)
    expected = {
        "missing": "sdist member set is not closed",
        "bytes": "sdist input bytes differ",
        "nonregular": "sdist member must be a regular file",
    }[mutation]
    _assert_invalid(artifacts, project, capsys, expected)


def test_rejects_sdist_entry_point_mismatch(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))

    def change_pyproject(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        for index, (member, data) in enumerate(entries):
            if member.name.endswith("/pyproject.toml"):
                changed = data.replace(
                    b'hmcpctl = "hmcpctl:main"', b'hmcpctl = "hmcpctl:missing"'
                )
                member.size = len(changed)
                entries[index] = (member, changed)

    _rewrite_sdist(sdist, change_pyproject)
    _assert_invalid(artifacts, project, capsys, "sdist input bytes differ")


def test_rejects_sdist_filename_root_version_mismatch(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))
    sdist.rename(artifacts / re.sub(r"-[^-]+\.tar\.gz$", "-9.9.9.tar.gz", sdist.name))

    _assert_invalid(
        artifacts, project, capsys, "sdist filename and root identity differ"
    )


def test_rejects_cross_artifact_version_mismatch(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))
    old_root = sdist.name.removesuffix(".tar.gz")
    new_root = re.sub(r"-[^-]+$", "-9.9.9", old_root)

    def synchronize_sdist_version(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        for index, (member, data) in enumerate(entries):
            member.name = new_root + member.name.removeprefix(old_root)
            if member.name.endswith("/PKG-INFO"):
                changed = re.sub(rb"(?m)^Version: .+$", b"Version: 9.9.9", data)
                member.size = len(changed)
                entries[index] = (member, changed)

    _rewrite_sdist(sdist, synchronize_sdist_version)
    sdist.rename(artifacts / f"{new_root}.tar.gz")
    _assert_invalid(
        artifacts, project, capsys, "wheel and sdist identity or version differs"
    )


def test_rejects_sdist_dependency_mismatch(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))

    def change_dependency(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        for index, (member, data) in enumerate(entries):
            if member.name.endswith("/PKG-INFO"):
                changed = data.replace(
                    b"Requires-Dist: asyncssh<3,>=2.24.1",
                    b"Requires-Dist: asyncssh<3,>=2.24.2",
                )
                member.size = len(changed)
                entries[index] = (member, changed)

    _rewrite_sdist(sdist, change_dependency)
    _assert_invalid(artifacts, project, capsys, "runtime dependencies differ")


def test_validation_does_not_invoke_subprocess(
    built_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts, project = built_project

    def reject_subprocess(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("artifact validation invoked a subprocess")

    monkeypatch.setattr(subprocess, "run", reject_subprocess)
    assert main([str(artifacts), str(project)]) == 0


@pytest.mark.parametrize(
    ("mutation", "detail"),
    [
        ("missing-name", "project.name must be a non-empty string"),
        ("missing-scripts", "project.scripts must be a non-empty string mapping"),
        ("dependencies-type", "project.dependencies must be a list of strings"),
        (
            "optional-dependencies-type",
            "project.optional-dependencies must map extras to lists of strings",
        ),
        ("bad-requirement", "project dependencies contain an invalid requirement"),
    ],
)
def test_rejects_malformed_project_configuration_actionably(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    mutation: str,
    detail: str,
) -> None:
    artifacts, project = built_project
    malformed = tmp_path / "project"
    shutil.copytree(
        project, malformed, ignore=shutil.ignore_patterns(".git", ".venv", "dist")
    )
    pyproject = malformed / "pyproject.toml"
    content = pyproject.read_text()
    if mutation == "missing-name":
        content = content.replace('name = "hmcpctl"', 'renamed = "hmcpctl"')
    elif mutation == "missing-scripts":
        content = content.replace('[project.scripts]\nhmcpctl = "hmcpctl:main"\n', "")
    elif mutation == "dependencies-type":
        content = re.sub(
            r"dependencies = \[\n.*?\n\]",
            'dependencies = "wrong"',
            content,
            count=1,
            flags=re.DOTALL,
        )
    elif mutation == "optional-dependencies-type":
        content = content.replace(
            "[project.optional-dependencies]", "optional-dependencies = []"
        )
    else:
        content = content.replace(
            '"asyncssh>=2.24.1,<3"', '"not a valid requirement !!!"'
        )
    pyproject.write_text(content)

    assert main([str(artifacts), str(malformed)]) == 1
    error = capsys.readouterr().err
    assert detail in error
    assert "Traceback" not in error


def test_rejects_unexpected_regular_sdist_member(
    tmp_path: Path,
    built_project: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    sdist = next(artifacts.glob("*.tar.gz"))

    def add_file(entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        root = entries[0][0].name.split("/", 1)[0]
        member = tarfile.TarInfo(f"{root}/unexpected.txt")
        member.size = 7
        entries.append((member, b"payload"))

    _rewrite_sdist(sdist, add_file)
    _assert_invalid(artifacts, project, capsys, "sdist member set is not closed")


def test_clean_checkout_runs_canonical_artifact_commands(tmp_path: Path) -> None:
    project = _clean_project(tmp_path / "project")

    for command in (
        ("just", "setup"),
        ("just", "build"),
        ("just", "verify-artifacts"),
    ):
        result = _run(*command, cwd=project)
        assert result.returncode == 0, result.stderr
    assert len(list((project / "dist").glob("*.whl"))) == 1
    assert len(list((project / "dist").glob("*.tar.gz"))) == 1


@pytest.mark.parametrize("returncode", [0, 1])
def test_release_validation_is_opt_in(built_project, monkeypatch, capsys, returncode):
    artifacts, project = built_project
    calls = []

    def invoke(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, returncode, "", "snapshot drift")

    monkeypatch.setattr(subprocess, "run", invoke)
    assert main([str(artifacts), str(project)]) == 0
    assert calls == []
    assert main([str(artifacts), str(project), "--release"]) == returncode
    assert calls[0][0] == [
        validator.sys.executable,
        str(project / "scripts/check_capability_inventory.py"),
        "--release",
    ]
    assert calls[0][1]["cwd"] == project
    assert calls[0][1]["env"]["PYTHONPATH"] == str(project / "src")
    if returncode:
        assert "snapshot drift" in capsys.readouterr().err


def test_release_artifacts_recompute_candidate_verification(
    built_project, tmp_path, capsys
):
    artifacts, original = built_project
    project = tmp_path / "checkout"
    shutil.copytree(original, project, ignore=shutil.ignore_patterns(".git", "dist"))
    (project / "docs/capabilities/release-verification.json").write_text(
        '{"format_version": 1, "operations": []}\n'
    )
    assert main([str(artifacts), str(project)]) == 0
    assert main([str(artifacts), str(project), "--release"]) == 1
    assert "release verification snapshot differs" in capsys.readouterr().err


@pytest.mark.parametrize("extension", ["*.whl", "*.tar.gz"])
def test_installed_artifact_has_offline_progress_code_lookup(
    built_project: tuple[Path, Path], tmp_path: Path, extension: str
) -> None:
    import sys

    artifacts, _ = built_project
    artifact = next(artifacts.glob(extension))
    installed = tmp_path / "installed"
    result = _run(
        "uv",
        "pip",
        "install",
        "--offline",
        "--no-deps",
        "--no-build-isolation",
        "--python",
        sys.executable,
        "--target",
        str(installed),
        str(artifact),
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    proof = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from pathlib import Path; from hmcpctl import progress_codes as p; "
        "assert Path(p.__file__).is_relative_to(sys.argv[1]); "
        "r=p.lookup_progress_code('ca00e105 LP=00002'); "
        "assert r.notation == 'CA00E105' and r.pdf_pages == (100,); "
        "assert 'operating system' in r.definition.lower(); "
        "assert p.lookup_progress_code('B2001230') is None"
    )
    result = _run(sys.executable, "-I", "-c", proof, str(installed), cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_rejects_checkout_without_progress_catalog(
    tmp_path: Path, built_project: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts, project = _artifact_copy(tmp_path, built_project)
    stripped = tmp_path / "stripped"
    shutil.copytree(project, stripped, ignore=shutil.ignore_patterns("dist"))
    (stripped / "src/hmcpctl/_progress_codes.json").unlink()
    _assert_invalid(
        artifacts,
        stripped,
        capsys,
        "missing package sentinel: hmcpctl/_progress_codes.json",
    )
