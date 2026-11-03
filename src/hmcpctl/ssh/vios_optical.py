"""The VIOS virtual optical load and unload commands over the HMC SSH CLI (ADR 0317)."""

from __future__ import annotations

import re
import shlex
from typing import Literal

OpticalAction = Literal["load", "unload"]

#: The repository medium rule; the name enters the VIOS shell unquoted.
MEDIA_NAME = re.compile(r"[A-Za-z0-9_.]{1,79}")
#: A conservative VIOS device-name rule; the name enters the VIOS shell unquoted.
DEVICE_NAME = re.compile(r"[A-Za-z0-9_]{1,15}")


def require_media_name(name: str) -> str:
    """Return *name*, or refuse one the VIOS shell could read as more than a name."""
    if not isinstance(name, str) or not MEDIA_NAME.fullmatch(name):
        raise ValueError(
            f"media_name {name!r} must be 1-79 letters, digits, '_' or '.', the "
            "repository rule; list media with hmc_list_optical_media"
        )
    return name


def require_device_name(name: str) -> str:
    """Return *name*, or refuse one the VIOS shell could read as more than a name."""
    if not isinstance(name, str) or not DEVICE_NAME.fullmatch(name):
        raise ValueError(
            f"target_device {name!r} must be 1-15 letters, digits or '_', a VIOS "
            "device name; list devices with hmc_list_storage_mappings"
        )
    return name


def optical_command(
    system_name: str,
    vios_id: int,
    action: OpticalAction,
    target_device: str,
    media_name: str | None = None,
    *,
    release: bool,
) -> str:
    """Return the ``viosvrcmd`` that loads or unloads one virtual optical device.

    The shapes are the captured ones (``cli-vios-loadopt``, ``cli-vios-unloadopt``);
    ``-release`` takes the position the VIOS usage text gives it.
    """
    if type(vios_id) is not int or vios_id <= 0:
        raise ValueError(f"VIOS partition id {vios_id!r} must be a positive integer")
    device = require_device_name(target_device)
    flag = " -release" if release else ""
    if action == "load":
        if media_name is None:
            raise ValueError("loading optical media requires media_name")
        inner = f"loadopt{flag} -disk {require_media_name(media_name)} -vtd {device}"
    elif action == "unload":
        inner = f"unloadopt{flag} -vtd {device}"
    else:
        raise ValueError(f"optical action {action!r} must be 'load' or 'unload'")
    return (
        f"viosvrcmd -m {shlex.quote(system_name)} --id {vios_id} "
        f"-c {shlex.quote(inner)}"
    )
