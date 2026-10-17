"""Configuration subgroup commands for hmcpctl.

hmcpctl config init                — create the platform-native config file
hmcpctl config list                — list configured profile names
hmcpctl config show                — show non-secret connection metadata for a profile
hmcpctl config init-access-policy  — generate a legacy-equivalent server access policy
hmcpctl config diff-access-policy  — diff a deployed policy against what would generate now

Two different files live under this group, and they are not two spellings of one
thing. ``config.toml`` holds **HMC connection profiles**: which consoles this
installation can reach, and how. ``access-policy.toml`` holds **server access
policies**: what an MCP server composed from this package may do with them. Separate
files, separate lifecycles; a policy grant's ``connections`` entries are profile
*keys* from the first file, never profile contents.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Final

import typer

from ..authorization.access_policy import AccessPolicyError
from ..config import (
    ConfigError,
    config_dir,
    config_inventory,
    resolve_config_path,
)
from . import runtime as cli_runtime
from .legacy_policy import GENERATED_SOURCE, LEGACY_POLICY_NAME
from .output import console, err_console, fail, print_json, terminal_field
from .serve import _policy_file

_STARTER_TOML = """\
# hmcpctl configuration — see docs/configuration.md for profile setup
# default_profile = "prod"

[profiles.example]
host = "hmc.example.com"
user = "admin"
password_env = "HMC_PASSWORD"  # preferred: secret stays out of the file  # pragma: allowlist secret
# password = "..."             # alternative: literal password (less secure)
# verify_ssl = false

# Optional friendly nicknames map a memorable name to an existing profile key.
# A nickname works anywhere a profile name does (--profile / HMC_PROFILE /
# default_profile). A profile key always wins on a name collision; resolution
# is one level deep (no chained nicknames); matching is case-sensitive.
# nicknames = { "big-iron" = "example" }
"""


def config_init() -> None:
    """Create the platform-native config file with a starter profile.

    Creates parent directories as needed. Refuses to overwrite an existing
    file. On POSIX systems, the new file is created with mode 0o600.
    """
    target = config_dir() / "config.toml"

    # Use resolve_config_path() as the authoritative existence check —
    # do not call os.path.exists() separately (would re-open TOCTOU window).
    # resolve_config_path() returns non-None only when the file exists.
    if resolve_config_path() is not None:
        fail(FileExistsError(f"Config file already exists: {target}"))

    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        if sys.platform != "win32":
            # O_CREAT|O_EXCL|mode=0o600: atomic exclusive create with restrictive
            # permissions — no create-then-chmod window.  The mode is set before
            # any other process can open the file descriptor.
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_STARTER_TOML)
        else:
            # Windows: os.open mode bits are no-ops; use plain open() with O_EXCL
            # semantics via 'x' mode.  The file inherits the user-account ACL from
            # %APPDATA%, which is the accepted Windows security posture (ADR-0007).
            with open(target, "x", encoding="utf-8") as fh:
                fh.write(_STARTER_TOML)
    except FileExistsError:
        fail(FileExistsError(f"Config file already exists: {target}"))

    # Same treatment as `init-access-policy` below, and for the same two reasons: this
    # line is the command's machine-readable output, and the path comes from
    # Path.home()/XDG_CONFIG_HOME/APPDATA, any of which may legally contain brackets.
    console.print(terminal_field(target), soft_wrap=True, markup=False)


def config_list() -> None:
    """List configured profile names and indicate the default profile."""
    config_path = resolve_config_path()

    if config_path is None:
        # Compute what the path *would* be for the helpful message.
        would_be = config_dir() / "config.toml"
        console.print(
            f"No config file found at {terminal_field(would_be)}", markup=False
        )
        return

    # Parse once so profile names, the default marker, and nicknames all derive
    # from the same document.
    try:
        inventory = config_inventory(config_path)
    except ConfigError as exc:
        fail(exc)
    profiles = inventory["profiles"]

    if not profiles:
        console.print("No profiles defined in config file.")
        return

    for entry in profiles:
        marker = "  (default)" if entry["is_default"] else ""
        console.print(
            f"{terminal_field(entry['name'])}{terminal_field(marker)}", markup=False
        )

    # Surface nicknames (secret-free): each maps to a profile key, flagged if
    # its target does not exist.
    for entry in inventory["nicknames"]:
        status = "" if entry["target_exists"] else "  (no such profile)"
        console.print(
            (
                f"{terminal_field(entry['name'])} -> {terminal_field(entry['target'])}"
                f"{terminal_field(status)}"
            ),
            markup=False,
        )


def config_show(
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Profile name to show (overrides global --profile)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON"),
) -> None:
    """Show non-secret connection metadata for a profile.

    Reports host, port, user, and connection settings. Never emits literal
    passwords or resolves password_env. Reports only whether a password or
    SSH key credential is configured.
    """
    # Command --profile takes precedence over the invocation's root option.
    effective_profile = profile or cli_runtime.current_options().profile

    config_path = resolve_config_path()
    if config_path is None:
        fail(ConfigError(f"No config file found at {config_dir() / 'config.toml'}"))

    # config_inventory owns the single read, nickname resolution, and safe
    # credential-presence metadata. An unreadable, non-UTF-8, or malformed file
    # reaches fail as a ConfigError rather than a traceback.
    try:
        inventory = config_inventory(
            config_path, selected_profile=effective_profile, include_selected=True
        )
    except ConfigError as exc:
        fail(exc)
    data: dict[str, Any] = inventory["selected"]

    if as_json:
        print_json(data)
    else:
        width = max(len(k) for k in data)
        for key, value in data.items():
            console.print(
                f"{terminal_field(f'{key:<{width}}')}  {terminal_field(value)}",
                markup=False,
            )


def _write_exclusive(target: Path, text: str) -> None:
    """Create *target* with *text*, refusing to overwrite, leaving no partial file.

    ``O_EXCL`` gives "no partial file" only for a failure at ``open``. A write or close
    that fails afterwards — ENOSPC, EDQUOT, EIO — leaves a truncated file, and a
    truncated file here is worse than elsewhere: it exists, so this command's own
    no-overwrite rule refuses to regenerate over it, and it does not compile, so
    ``serve`` refuses too. So the descriptor's contents are the unit of work: if
    anything after the create fails, the destination is unlinked before the error is
    reported.

    The contents are ``fsync``-ed before the descriptor closes. Closing alone flushes
    only to the page cache, and on a delayed-allocation filesystem the metadata — the
    file exists — commits ahead of the data more often than the reverse, so a power
    loss after this command *reported success* could leave exactly the truncated file
    the unlink above exists to prevent, with no visible interruption to explain it.

    Two residuals, named rather than implied closed. The parent directory is not
    synced, so the new directory entry itself is not durable against the same event.
    And a signal or an OOM kill between the create and the flush leaves a partial file
    with no handler to run. docs/mcp-server.md carries the recovery for both:
    preserve the file, generate a scratch copy, and restore reviewed decisions.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        # O_CREAT|O_EXCL|0o600: atomic exclusive create with restrictive permissions,
        # the same shape `config init` uses — no create-then-chmod window.
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    else:
        # Windows: os.open mode bits are no-ops; 'x' gives the same O_EXCL semantics,
        # and the file inherits the user-account ACL from %APPDATA% (ADR 0007).
        try:
            with open(target, "x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError:
            raise
        except BaseException:
            target.unlink(missing_ok=True)
            raise


def config_init_access_policy(
    output: str | None = typer.Option(
        None,
        "--output",
        metavar="PATH",
        help="Write to PATH instead of the platform-native access-policy.toml. This "
        "is how you regenerate: the command never overwrites, so generate to a "
        "scratch path, diff it against the deployed policy, and merge by hand.",
    ),
) -> None:
    """Generate a legacy-equivalent server access policy for review.

    Writes the policy an existing deployment needs after an access policy became
    mandatory: every ordinary tool, every configured connection, no target
    restriction. It is a migration aid, not a recommended posture — a new deployment
    should start from the read-only example in the README.

    It activates nothing. Review the file, then pass its name to
    ``hmcpctl serve --access-policy legacy-equivalent``.

    Never overwrites an existing file. Run it as the identity and with the environment
    ``serve`` runs under: both resolve the file through the same config directory, and
    the connection list is read from that identity's ``config.toml``.
    """
    if output is not None:
        target = Path(output)
    else:
        resolved = _policy_file()
        if resolved is None:
            fail(
                RuntimeError(
                    "cannot resolve the access-policy path: no home directory. Set "
                    "HOME or XDG_CONFIG_HOME for the identity this runs as, or pass "
                    "--output PATH."
                )
            )
        target = Path(resolved[0])

    try:
        text = _render_validated_legacy_policy()
    except ConfigError as exc:
        fail(exc)
    except AccessPolicyError as exc:
        fail(exc, guidance=_PROFILE_KEY_GUIDANCE)

    try:
        _write_exclusive(target, text)
    except FileExistsError:
        # `output is not None` is the discriminator, not a comparison of `target`
        # against the platform-native default: an operator can point --output at
        # that exact path, and the message must still speak to the flag they passed
        # rather than assume this is the reviewed policy.
        if output is not None:
            fail(
                FileExistsError(
                    f"Output path already exists: {target}. This command never "
                    "overwrites an existing file. Delete it, or pass a different "
                    "--output PATH."
                )
            )
        fail(
            FileExistsError(
                f"Access policy file already exists: {target}. This command never "
                "overwrites a reviewed policy. To regenerate, write to a scratch path "
                "with --output PATH, diff it against this file, and merge by hand."
            )
        )
    except OSError as exc:
        fail(exc)

    # Markup off: these render through a markup-enabled rich Console, and under
    # --output the path is the operator's own. A bracketed path would print with the
    # bracketed segment silently deleted — so the operator copies a path that does not
    # exist — and a `[/x]`-shaped one would raise MarkupError in place of the success
    # line.
    # `soft_wrap=True` because this line is the command's machine-readable output: a
    # rich Console hard-folds at 80 columns on a non-tty, so
    # `hmcpctl config init-access-policy > path.txt` would otherwise capture a path
    # broken across lines. Markup off for the same reason as above.
    console.print(terminal_field(target), soft_wrap=True, markup=False)
    if output is None:
        err_console.print(
            (
                f"Review it, then start the server with: hmcpctl serve --access-policy "
                f"{terminal_field(LEGACY_POLICY_NAME, err=True)}"
            ),
            markup=False,
        )
    else:
        # `--access-policy` selects a NAME inside the platform-native file, and no
        # option takes a path — so this document is not one `serve` can be pointed at.
        # Printing the activation line here would end the regenerate-and-diff flow by
        # telling the operator to start the server on the *old* deployed policy, which
        # would start cleanly and report the same policy name.
        err_console.print(
            "Note: serve reads only the platform-native access-policy.toml, so this "
            "file is not the one it will load. Diff it against the deployed policy and "
            "merge by hand."
        )


DIFF_IDENTICAL: Final = 0
DIFF_DIFFERS: Final = 1
_PROFILE_KEY_GUIDANCE = (
    "\n\nThat entry came from a profile key in config.toml. "
    "Remove the padding from the profile key."
)

DEPLOYED_UNREADABLE: Final = 3
GENERATION_FAILED: Final = 4


def _render_validated_legacy_policy() -> str:
    """Render and validate the generated legacy-equivalent policy once."""
    from ..server_tools.catalog import TOOL_SECURITY
    from .legacy_policy import (
        compile_rendered_policy,
        legacy_connections,
        render_legacy_policy,
    )

    connections = legacy_connections()
    text = render_legacy_policy(TOOL_SECURITY, connections)
    compile_rendered_policy(text, TOOL_SECURITY)
    return text


def config_diff_access_policy(
    deployed: str = typer.Argument(
        metavar="PATH",
        help="Path to the deployed access-policy.toml document to compare against.",
    ),
) -> None:
    """Diff a deployed access policy against what this build would generate now.

    Renders the legacy-equivalent policy exactly as ``config init-access-policy``
    would — every ordinary tool in this build's registry, every profile key in
    the current ``config.toml`` — and prints a unified diff against the deployed
    document. That surfaces both drift arms ADR 0041 records: a tool a later
    release added, and a profile added to ``config.toml`` after generation. The
    non-zero difference exit makes it usable as a CI gate or health check.

    Exit codes:

    \b
      0  identical — nothing to do
      1  different — the unified diff went to stdout
      2  usage error
      3  the deployed policy could not be read
      4  generation failed

    Run it as the identity, and with the environment, that ``serve`` runs under,
    for the reason ``config init-access-policy`` gives: the connection list is
    read from that identity's ``config.toml``, through the same config-directory
    resolution.
    """
    import difflib

    path = Path(deployed)

    try:
        text = _render_validated_legacy_policy()
    except ConfigError as exc:
        fail(exc, code=GENERATION_FAILED)
    except AccessPolicyError as exc:
        fail(exc, code=GENERATION_FAILED, guidance=_PROFILE_KEY_GUIDANCE)

    try:
        deployed_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(
            RuntimeError(
                f"No deployed access policy at {path}. Pass the path of the "
                "access-policy.toml the server loads; if none exists yet, create "
                "one with `hmcpctl config init-access-policy` first."
            ),
            code=DEPLOYED_UNREADABLE,
        )
    except UnicodeDecodeError:
        fail(
            RuntimeError(
                f"{path} is not UTF-8 text, so it is not a TOML policy document. "
                "Pass the path of the access-policy.toml the server loads."
            ),
            code=DEPLOYED_UNREADABLE,
        )
    except OSError as exc:
        fail(exc, code=DEPLOYED_UNREADABLE)

    diff = list(
        difflib.unified_diff(
            deployed_text.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=f"{path} (deployed)",
            tofile=GENERATED_SOURCE,
        )
    )
    if not diff:
        # The all-clear goes to stderr so stdout stays machine-readable on the green
        # path: whatever captures the diff captures nothing at all when current.
        # `soft_wrap=True`: like init-access-policy's success line, this carries the
        # operator's own path, and an 80-column fold on a non-tty would break it.
        err_console.print(
            (
                f"No differences: {terminal_field(path, err=True)}"
                f" matches what this build and the current config.toml generate."
            ),
            soft_wrap=True,
            markup=False,
        )
        return

    # Escaped like every other operator-controlled path through a markup-enabled
    # Console; soft-wrapped because the diff IS the command's machine-readable
    # output and a hard fold at 80 columns would corrupt its lines.
    for line in diff:
        console.print(
            terminal_field(line.rstrip("\n")),
            soft_wrap=True,
            highlight=False,
            markup=False,
        )
    raise typer.Exit(code=DIFF_DIFFERS)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("init")(config_init)
    group.command("list")(config_list)
    group.command("show")(config_show)
    group.command("init-access-policy")(config_init_access_policy)
    group.command("diff-access-policy")(config_diff_access_policy)
