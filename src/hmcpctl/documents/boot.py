"""Boot-order input validation for ``BootListInformation/PendingBootString``."""

from __future__ import annotations


def _is_joinable(path: str) -> bool:
    """A ``/``-rooted path of printable, non-space ASCII.

    Single-space joining can then neither split nor merge it, and every
    character is legal XML text.
    """
    return path.startswith("/") and all("!" <= ch <= "~" for ch in path)


def join_boot_device_paths(paths: list[str]) -> str:
    """Join Open Firmware device paths, first to last, into a ``PendingBootString`` value.

    Not a ``build_*`` document builder: the value becomes element text through
    ElementTree, which is its only escaping point.
    """
    if not paths:
        raise ValueError(
            "Boot order must contain at least one Open Firmware device path"
        )
    for path in paths:
        if not _is_joinable(path):
            raise ValueError(
                f"Invalid boot device path: {path!r}. Give an Open Firmware device path "
                "such as '/vdevice/v-scsi@30000002/disk@8100000000000000', as "
                "read-boot-order reports, of printable ASCII with no whitespace"
            )
    return " ".join(paths)


def validate_raw_boot_string(text: str) -> str:
    """Validate the bounded raw path without normalizing stored text."""
    if (
        not isinstance(text, str)
        or not text
        or not text.isprintable()
        or not text.strip()
    ):
        raise ValueError("Raw boot string must contain printable non-whitespace text")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise ValueError("Raw boot string must be valid UTF-8") from None
    if size > 16384:
        raise ValueError("Raw boot string exceeds the 16384-byte application budget")
    return text


def select_boot_string(devices: list[str] | None, boot_string: str | None) -> str:
    """Keep legacy paths and raw text as mutually exclusive input forms."""
    if (devices is None) == (boot_string is None):
        raise ValueError("Supply exactly one of devices or boot_string")
    if boot_string is not None:
        return validate_raw_boot_string(boot_string)
    assert devices is not None
    return join_boot_device_paths(devices)
