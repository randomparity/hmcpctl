"""Load and unload media in an existing virtual optical device (#1285, ADR 0317)."""

import pytest
from conftest import live_fixture

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
