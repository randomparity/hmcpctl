"""Load and unload media in an existing virtual optical device (#1285, ADR 0317)."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncssh
import httpx
import pytest
from conftest import live_fixture, make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.storage.resources import (
    load_optical_media,
    unload_optical_media,
)
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.ssh.vios_optical import optical_command

# The captures tokenize the VTD and media names (`dev-14`, `media-2`), and those tokens
# fall outside the name rules; the live names (`vtopt0`, `hmcpctl_live_<hex>`) do not.
# Substituting rule-conformant names keeps the command's shape from the capture.
_TOKENS = {"dev-14": "vtopt1", "media-2": "media_2", "media-3": "media_3"}


def _captured_command(name: str) -> str:
    command = live_fixture(name)["command"]
    for token, name_ in _TOKENS.items():
        command = command.replace(token, name_)
    return command


@pytest.mark.parametrize(
    ("fixture", "action", "media"),
    [
        ("cli-vios-loadopt", "load", "media_2"),
        ("cli-vios-unloadopt", "unload", None),
        ("cli-vios-loadopt-after-power-off", "load", "media_3"),
        ("cli-vios-unloadopt-after-power-off", "unload", None),
    ],
)
def test_optical_command_matches_the_captured_shapes(fixture, action, media):
    assert optical_command(
        "sys-R1", 100, action, "vtopt1", media, release=False
    ) == _captured_command(fixture)


def test_release_follows_the_captured_usage():
    # cli-vios-help-loadopt: `loadopt [-f] [-release] -disk FileName -vtd ...`;
    # cli-vios-help-unloadopt: `unloadopt [-release] -vtd ...`.
    assert "-release" in live_fixture("cli-vios-help-loadopt")["stdout"]
    assert optical_command("sys-R1", 100, "load", "vtopt1", "m.iso", release=True) == (
        "viosvrcmd -m sys-R1 --id 100 -c 'loadopt -release -disk m.iso -vtd vtopt1'"
    )
    assert optical_command("sys-R1", 100, "unload", "vtopt1", release=True) == (
        "viosvrcmd -m sys-R1 --id 100 -c 'unloadopt -release -vtd vtopt1'"
    )


def test_the_system_name_is_quoted_for_the_hmc_shell():
    assert optical_command("sys R1", 7, "unload", "vtopt1", release=False) == (
        "viosvrcmd -m 'sys R1' --id 7 -c 'unloadopt -vtd vtopt1'"
    )


@pytest.mark.parametrize(
    "device", ["dev-14", "", "a b", "-x", "a;b", "v" * 16, "vt$(x)"]
)
def test_a_device_outside_the_rule_is_refused(device):
    with pytest.raises(ValueError, match="target_device"):
        optical_command("sys-R1", 100, "unload", device, release=False)


@pytest.mark.parametrize("media", ["media-2", "", "a b", "a;b", "m" * 80, "a'b"])
def test_a_medium_outside_the_rule_is_refused(media):
    with pytest.raises(ValueError, match="media_name"):
        optical_command("sys-R1", 100, "load", "vtopt1", media, release=False)


@pytest.mark.parametrize("vios_id", [0, -1, True, "100", 1.0])
def test_the_vios_id_must_be_a_positive_integer(vios_id):
    with pytest.raises(ValueError, match="partition id"):
        optical_command("sys-R1", vios_id, "unload", "vtopt1", release=False)


def test_load_requires_a_medium_and_the_action_is_closed():
    with pytest.raises(ValueError, match="requires media_name"):
        optical_command("sys-R1", 100, "load", "vtopt1", release=False)
    with pytest.raises(ValueError, match="'load' or 'unload'"):
        optical_command("sys-R1", 100, "swap", "vtopt1", release=False)  # type: ignore[arg-type]


# -- the operations ---------------------------------------------------------------

EMPTY = "rest-vios-scsi-mapping-unmapped-adapter"  # one empty optical VTD, one disk
LOADED = "rest-vios-scsi-mapping-replace-storage"  # two loaded optical VTDs, one disk
_DEVICES = {
    "dev-11": "vtopt0",
    "dev-12": "vtscsi2",
    "dev-13": "vtopt3",
    "dev-14": "vtopt4",
}
_VIOS = {
    EMPTY: "00000001-ABCD-4EF0-8ABC-000000000001",
    LOADED: "0000001C-ABCD-4EF0-8ABC-00000000001C",
}
_LPAR = {
    EMPTY: "00000002-ABCD-4EF0-8ABC-000000000002",
    LOADED: "00000014-ABCD-4EF0-8ABC-000000000014",
}
_SYSTEM = {
    EMPTY: "00000006-abcd-4ef0-8abc-000000000006",
    LOADED: "00000001-abcd-4ef0-8abc-000000000001",
}


def _vios_body(fixture: str, devices: dict[str, str] | None = None) -> str:
    body = live_fixture(fixture)["body"]
    for token, name in (_DEVICES if devices is None else devices).items():
        body = body.replace(f">{token}<", f">{name}<")
    return body


class _Fake:
    """The SSH, ownership and system-name boundaries the operations cross."""

    def __init__(
        self,
        monkeypatch,
        fixture: str,
        answer: object = ":",
        *,
        transport: bool = False,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self.answer = answer
        lpar = _LPAR[fixture]
        system = _SYSTEM[fixture]

        async def authorize(hmc, selector, *, ownership_override, system_name_or_uuid):
            self.calls.append(("authorize", (selector, ownership_override)))
            if isinstance(self.answer, PermissionError):
                raise self.answer
            return lpar

        async def system_name(hmc, value):
            assert value == system
            return "sys-R1"

        async def run(config, command, **kwargs):
            self.calls.append(("ssh", command))
            assert kwargs == {"max_output_bytes": 65536}
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        def released(**fields):
            self.calls.append(("audit", fields))

        target = "hmcpctl.operations.storage.resources"
        monkeypatch.setattr(f"{target}.resolve_and_authorize_lpar_mutation", authorize)
        monkeypatch.setattr(f"{target}.resolve_system_name", system_name)
        if not transport:
            monkeypatch.setattr(f"{target}.run_hmc_command", run)
        monkeypatch.setattr(f"{target}.audit.record_optical_release", released)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    def ssh(self) -> list[object]:
        return [value for kind, value in self.calls if kind == "ssh"]


def _serve(mock_hmc, fixture: str, body: str | None = None) -> None:
    mock_hmc.get(f"/rest/api/uom/VirtualIOServer/{_VIOS[fixture]}").mock(
        return_value=httpx.Response(
            200,
            text=_vios_body(fixture) if body is None else body,
            headers={"Content-Type": "application/atom+xml"},
        )
    )


async def _call(operation, fixture: str, **kwargs):
    async with HMCClient(make_config()) as hmc:
        return await operation(hmc, _VIOS[fixture], "lpar-a", **kwargs)


@pytest.mark.asyncio
async def test_load_finds_the_lpars_empty_device_and_submits_the_captured_shape(
    mock_hmc, monkeypatch
):
    fake = _Fake(monkeypatch, EMPTY)
    _serve(mock_hmc, EMPTY)

    result = await _call(load_optical_media, EMPTY, media_name="media_2")

    assert fake.ssh() == [
        _captured_command("cli-vios-loadopt").replace("vtopt1", "vtopt0")
    ]
    assert result.raw == {
        "action": "load",
        "vios_uuid": _VIOS[EMPTY],
        "lpar_uuid": _LPAR[EMPTY],
        "target_device": "vtopt0",
        "media_name": "media_2",
        "release": False,
    }
    assert result.lpar_uuid == _LPAR[EMPTY]
    assert result.target_device == "vtopt0"
    assert result.change_location is None
    assert "audit" not in fake.kinds()


@pytest.mark.asyncio
async def test_unload_names_one_of_several_devices_explicitly(mock_hmc, monkeypatch):
    fake = _Fake(monkeypatch, LOADED)
    _serve(mock_hmc, LOADED)

    result = await _call(unload_optical_media, LOADED, target_device="vtopt4")

    assert fake.ssh() == [
        _captured_command("cli-vios-unloadopt").replace("vtopt1", "vtopt4")
    ]
    assert result.raw["media_name"] is None
    assert result.raw["action"] == "unload"


@pytest.mark.asyncio
async def test_several_devices_and_no_target_is_refused_without_a_command(
    mock_hmc, monkeypatch
):
    fake = _Fake(monkeypatch, LOADED)
    _serve(mock_hmc, LOADED)

    with pytest.raises(HMCError, match=r"has 2 virtual optical devices.*target_device"):
        await _call(unload_optical_media, LOADED)
    assert fake.ssh() == []


@pytest.mark.asyncio
async def test_a_device_the_lpar_does_not_own_is_refused(mock_hmc, monkeypatch):
    fake = _Fake(monkeypatch, LOADED)
    _serve(mock_hmc, LOADED)

    # vtscsi2 is the partition's disk VTD, not an optical one.
    for device in ("vtopt9", "vtscsi2"):
        with pytest.raises(HMCError, match="is not mapped to LPAR"):
            await _call(unload_optical_media, LOADED, target_device=device)
    assert fake.ssh() == []


@pytest.mark.asyncio
async def test_an_lpar_with_no_optical_device_is_refused(mock_hmc, monkeypatch):
    fake = _Fake(monkeypatch, EMPTY)
    body = _vios_body(EMPTY).replace(
        _LPAR[EMPTY], "00000009-ABCD-4EF0-8ABC-000000000009"
    )
    _serve(mock_hmc, EMPTY, body)

    with pytest.raises(HMCError, match=r"has 0 virtual optical devices.*none"):
        await _call(load_optical_media, EMPTY, media_name="media_2")
    assert fake.ssh() == []


@pytest.mark.asyncio
async def test_a_mapped_device_outside_the_name_rule_is_refused_by_its_name(
    mock_hmc, monkeypatch
):
    fake = _Fake(monkeypatch, EMPTY)
    _serve(mock_hmc, EMPTY, _vios_body(EMPTY, {}))  # the raw token dev-11

    with pytest.raises(HMCError, match="Mapped virtual optical device 'dev-11'"):
        await _call(unload_optical_media, EMPTY)
    assert fake.ssh() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "replacement"),
    [
        (r"(<PartitionID[^>]*>)[^<]*<", r"\g<1>0<"),
        (r"\bPartitionID\b", "NoPartitionID"),
        (r"\bAssociatedManagedSystem\b", "OtherLink"),
    ],
)
async def test_an_unusable_vios_identity_is_refused(
    mock_hmc, monkeypatch, pattern, replacement
):
    fake = _Fake(monkeypatch, EMPTY)
    body = re.sub(pattern, replacement, _vios_body(EMPTY))
    _serve(mock_hmc, EMPTY, body)

    with pytest.raises(HMCError, match="no usable PartitionID"):
        await _call(unload_optical_media, EMPTY)
    assert fake.ssh() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "kwargs"),
    [
        (load_optical_media, {"media_name": "bad name"}),
        (load_optical_media, {"media_name": "media_2", "target_device": "vt-1"}),
        (unload_optical_media, {"target_device": "a;b"}),
    ],
)
async def test_a_bad_name_is_refused_before_any_request(
    mock_hmc, monkeypatch, operation, kwargs
):
    fake = _Fake(monkeypatch, EMPTY)
    with pytest.raises(ValueError):
        await _call(operation, EMPTY, **kwargs)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_an_ownership_refusal_submits_nothing(mock_hmc, monkeypatch):
    fake = _Fake(monkeypatch, EMPTY, answer=PermissionError("foreign owner"))
    _serve(mock_hmc, EMPTY)

    with pytest.raises(PermissionError):
        await _call(unload_optical_media, EMPTY, ownership_override=False)
    assert fake.ssh() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["load", "unload"])
async def test_release_is_audited_before_the_command(mock_hmc, monkeypatch, action):
    fake = _Fake(monkeypatch, EMPTY)
    _serve(mock_hmc, EMPTY)
    operation = load_optical_media if action == "load" else unload_optical_media
    media = {"media_name": "media_2"} if action == "load" else {}

    result = await _call(operation, EMPTY, release=True, **media)

    assert fake.kinds()[-2:] == ["audit", "ssh"]
    assert fake.calls[-2][1] == {
        "action": action,
        "system": "sys-R1",
        "vios": _VIOS[EMPTY],
        "lpar": _LPAR[EMPTY],
        "target_device": "vtopt0",
        "media": media.get("media_name"),
        "host": "hmc.test",
        "agent_id": "hmcpctl",
    }
    assert " -release " in str(fake.ssh()[0])
    assert result.raw["release"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (HMCCLIError("rc 1: refused", remote_nonzero_completed=True), "was refused"),
        (HMCCLIError("timed out"), "completion unknown"),
        ("Some VIOS text\n", "completion unknown, unexpected answer"),
        ("", "completion unknown, unexpected answer"),
    ],
)
async def test_every_answer_but_the_captured_one_is_not_success(
    mock_hmc, monkeypatch, answer, message
):
    fake = _Fake(monkeypatch, EMPTY, answer=answer)
    _serve(mock_hmc, EMPTY)

    with pytest.raises(HMCError, match=message) as caught:
        await _call(load_optical_media, EMPTY, media_name="media_2")
    text = str(caught.value)
    assert "Optical load of vtopt0 on VIOS" in text
    assert "Nothing was retried" in text
    assert len(fake.ssh()) == 1


@pytest.mark.asyncio
async def test_the_captured_answer_with_surrounding_whitespace_is_success(
    mock_hmc, monkeypatch
):
    _Fake(monkeypatch, EMPTY, answer=" :\n")
    _serve(mock_hmc, EMPTY)

    result = await _call(unload_optical_media, EMPTY)
    assert result.raw["target_device"] == "vtopt0"


def _captured_ssh(monkeypatch, fixture: str) -> list[str]:
    """Answer the real SSH transport with one captured ``viosvrcmd`` result."""
    capture = live_fixture(fixture)
    commands: list[str] = []

    def reader(data: str) -> asyncio.StreamReader:
        stream = asyncio.StreamReader()
        stream.feed_data(data.encode())
        stream.feed_eof()
        return stream

    async def create_process(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            stdout=reader(capture["stdout"]),
            stderr=reader(capture["stderr"]),
            env={},
            command=command,
            subsystem=None,
            exit_status=capture["exit_status"],
            exit_signal=None,
            returncode=capture["exit_status"],
            close=MagicMock(),
            wait_closed=AsyncMock(),
        )

    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.create_process.side_effect = create_process
    connection.abort = MagicMock()
    monkeypatch.setattr(asyncssh, "connect", MagicMock(return_value=connection))
    return commands


@pytest.mark.asyncio
async def test_the_captured_success_is_success_through_the_transport(
    mock_hmc, monkeypatch
):
    _Fake(monkeypatch, EMPTY, transport=True)
    _serve(mock_hmc, EMPTY)
    commands = _captured_ssh(monkeypatch, "cli-vios-loadopt")

    result = await _call(load_optical_media, EMPTY, media_name="media_2")

    assert result.raw["target_device"] == "vtopt0"
    assert len(commands) == 1


@pytest.mark.asyncio
async def test_the_captured_occupied_device_refusal_is_reported_as_refused(
    mock_hmc, monkeypatch
):
    """#1285 live: a load into a loaded device exits 1 with HSCL2970."""
    _Fake(monkeypatch, EMPTY, transport=True)
    _serve(mock_hmc, EMPTY)
    _captured_ssh(monkeypatch, "cli-vios-loadopt-occupied")

    with pytest.raises(HMCError, match="was refused") as caught:
        await _call(load_optical_media, EMPTY, media_name="media_3")
    assert "HSCL2970" in str(caught.value)
    assert "already contains a loaded virtual disk" in str(caught.value)
