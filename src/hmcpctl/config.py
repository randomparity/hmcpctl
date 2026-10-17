"""Configuration for hmcpctl.

Settings are resolved in priority order:
  1. CLI options / explicit constructor args
  2. Environment variables (HMC_*)
  3. TOML profile (~/.config/hmcpctl/config.toml or platform equivalent)

Checkout-local .env files are NOT loaded: HMCConfig declares no ``env_file``, so
no dotenv source is configured. Passing ``_env_file=None`` is therefore inert
here, and it never suppressed environment variables in any case.

Use load_profile() to load a named profile from the platform-native config file.
Use HMCConfig(...) directly for explicit construction that should still honour
HMC_* — the CLI and MCP server paths.
Use HMCConfig.from_mapping(...) when a value must come from the mapping or the
field default and never from the ambient environment (ADR 0096); a library
consumer building one config per HMC wants this one.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_logger = logging.getLogger(__name__)


#: Whether :meth:`HMCConfig._warn_audit_memento_override` has reported in this
#: process. Changing the values does not change the diagnostic's information:
#: ``agent_id`` still overrides the configured memento.
_reported_memento_override = False

#: Serialises the check-then-act on :data:`_reported_memento_override`. Config
#: construction is not confined to the event-loop thread — the permissions path
#: resolves its guards in a worker thread, one config per granted connection — so
#: without this two racers both miss the membership test and both emit. Held only
#: across the flag check and update; logging is outside it, so a slow handler cannot
#: block a config being built on another thread.
_override_report_lock = threading.Lock()


def validate_agent_id(agent_id: str) -> None:
    """Validate an agent identifier used in audit and ownership tokens."""
    if not agent_id:
        raise ValueError("agent_id must not be empty")
    if agent_id == "hmcpctl":
        raise ValueError("agent_id 'hmcpctl' is reserved; choose a distinct identifier")
    if len(agent_id) > 64:
        raise ValueError(f"agent_id is {len(agent_id)} characters; maximum is 64")
    if not agent_id.isascii() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in agent_id
    ):
        raise ValueError(
            "agent_id contains non-ASCII or non-printable characters; "
            "only printable ASCII is accepted"
        )
    forbidden = {
        ",": "commas corrupt the HMC CLI -i parser",
        "=": "equals signs corrupt the HMC CLI -i parser",
        '"': "double quotes are the HMC CLI -i record escape",
        "[": "brackets break the ownership token format",
        "]": "brackets break the ownership token format",
        "/": "the HMC REST API rejects '/' in X-Audit-Memento",
        ":": "colons make audit and ownership token formats ambiguous",
        "\\": "backslash behaviour inside an HMC CLI -i record is unverified "
        "(ADR 0045)",
        " ": "spaces corrupt the ownership token in the CLI -i parser",
    }
    for character, reason in forbidden.items():
        if character in agent_id:
            raise ValueError(f"agent_id contains {character!r}; {reason}")


ISO_URL_ALLOWLIST_HELP = (
    "Set HMC_ISO_URL_ALLOWLIST (or iso_url_allowlist in the TOML profile) to a "
    "comma-separated list of hosts, each written as 'host' or 'host:port' with "
    "no scheme and no path — for example "
    "HMC_ISO_URL_ALLOWLIST=iso.example.internal,localhost:18765"
)


def parse_iso_url_allowlist(value: str) -> tuple[tuple[str, int | None], ...]:
    """Parse the ISO download allowlist into ``(host, port_or_None)`` pairs.

    *value* is a comma-separated list of ``host`` or ``host:port`` entries;
    empty entries are dropped, so a trailing comma is not an error. An entry
    without a port permits any port on that host.

    Each entry is parsed as a URL authority rather than split by hand, so
    bracketed IPv6 literals (``[::1]:18765``) and out-of-range ports are handled
    by the standard library. Anything that is not a bare authority — a scheme, a
    path, credentials, a query — raises ``ValueError`` naming the entry: an
    operator who writes ``https://iso.example.internal/isos/`` would otherwise
    get an allowlist that silently matches nothing.
    """
    entries: list[tuple[str, int | None]] = []
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        parts = urlsplit(f"//{entry}")
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError(
                f"iso_url_allowlist entry {entry!r} has an unusable port: {exc}. "
                + ISO_URL_ALLOWLIST_HELP
            ) from exc
        if port == 0:
            # Port 0 is not a destination, and it is falsy: an entry carrying it
            # would compare unequal to every URL's port and silently match
            # nothing.
            raise ValueError(
                f"iso_url_allowlist entry {entry!r} has an unusable port: port "
                "must be between 1 and 65535. " + ISO_URL_ALLOWLIST_HELP
            )
        if not parts.hostname or parts.username is not None:
            raise ValueError(
                f"iso_url_allowlist entry {entry!r} is not a host or host:port. "
                + ISO_URL_ALLOWLIST_HELP
            )
        if parts.path or parts.query or parts.fragment:
            raise ValueError(
                f"iso_url_allowlist entry {entry!r} carries a scheme, path, or "
                "query; the allowlist matches hosts, not URL prefixes. "
                + ISO_URL_ALLOWLIST_HELP
            )
        entries.append((parts.hostname, port))
    return tuple(entries)


class HMCConfig(BaseSettings):
    """Connection settings for an IBM HMC."""

    model_config = SettingsConfigDict(
        env_prefix="HMC_",
        extra="ignore",
        # A rejected value can be the password; keep it out of every error text.
        hide_input_in_errors=True,
    )

    host: str = Field(default="", description="HMC hostname or IP address")
    port: int = Field(default=443, description="HMC REST API port")
    user: str = Field(default="", description="HMC user name")
    password: str = Field(default="", description="HMC password")
    ssh_key_file: str | None = Field(
        default=None, description="Path to SSH private key file (HMC_SSH_KEY_FILE)"
    )
    ssh_verify_host_key: bool = Field(
        default=True, description="Verify SSH host keys against ~/.ssh/known_hosts"
    )
    verify_ssl: bool = Field(
        default=False, description="Verify the HMC TLS certificate"
    )
    ca_bundle: str | None = Field(
        default=None,
        description=(
            "Path to a PEM file containing the CA certificate(s) to trust for the HMC "
            "TLS connection (HMC_CA_BUNDLE). When set and verify_ssl is true, this path "
            "is passed directly to the HTTP client and takes precedence over SSL_CERT_FILE, "
            "SSL_CERT_DIR, and HMC_TRUST_SYSTEM_STORE. Ignored when verify_ssl is false."
        ),
    )
    trust_system_store: bool = Field(
        default=True,
        description=(
            "When true and verify_ssl is true, use the operating-system trust store "
            "via truststore.SSLContext instead of only the certifi roots. Set false "
            "to rely on SSL_CERT_FILE / SSL_CERT_DIR and the bundled roots only "
            "(HMC_TRUST_SYSTEM_STORE). Ignored when verify_ssl is false."
        ),
    )
    timeout: float = Field(default=180.0, description="HTTP timeout in seconds")
    upload_timeout: float = Field(
        default=600.0,
        gt=0,
        description="Seconds the ISO upload waits for the HMC's response after the last byte "
        "(HMC_UPLOAD_TIMEOUT); the wait is never shorter than HMC_TIMEOUT",
    )
    max_response_bytes: int = Field(
        default=32 * 1024 * 1024,
        gt=0,
        description="Maximum HMC REST response size in bytes (HMC_MAX_RESPONSE_BYTES)",
    )
    ssh_timeout: float = Field(
        default=300.0,
        description="SSH command timeout in seconds (HMC CLI ops are slower "
        "than REST calls, e.g. bkprofdata/rstprofdata; 60s is too tight)",
    )
    audit_memento: str = Field(
        default="hmcpctl",
        description=(
            "Value sent in the X-Audit-Memento header (shows up in HMC audit "
            "logs). Must be printable ASCII (U+0020 through U+007E); control "
            "characters and non-ASCII values are refused at construction."
        ),
    )
    schema_version: str = Field(
        default="",
        description=(
            "Schema version sent as X-HMC-Schema-Version request header "
            "(e.g. 'V1_0'). Empty string disables the header (default). "
            "Must be printable ASCII (U+0020 through U+007E) when set; "
            "control characters and non-ASCII values are refused at "
            "construction. HMC V8/V9 targets do not need this; uom documents "
            "already declare schemaVersion=V1_0. Set it only to pin "
            "negotiation explicitly."
        ),
    )
    agent_id: str | None = Field(
        default=None,
        description=(
            "Per-agent identifier folded into the X-Audit-Memento header as "
            "hmcpctl:<agent_id>. Used for multi-agent LPAR ownership attribution. "
            "Must be 1–64 printable ASCII characters with no commas, = signs, "
            "square brackets, double quotes, or backslashes — any of those would "
            "corrupt the ownership stamp in the HMC CLI -i parser or the "
            "description grammar. (HMC_AGENT_ID)"
        ),
    )

    authorize_power_operations: bool = Field(
        default=False,
        description=(
            "Enforce the ADR 0011 ownership guard on LPAR power operations "
            "(ADR 0092 §4). Off by default: the guard adds an SSH login and extra "
            "REST reads to every call that does not carry an ownership "
            "override, and power is the one mutation class whose inverse is a "
            "single call. When on, power_lpar derives the owning managed system when "
            "the selector is omitted and refuses to power a partition another "
            "agent owns unless the caller passes ownership_override. "
            "(HMC_AUTHORIZE_POWER_OPERATIONS)"
        ),
    )

    iso_url_allowlist: str = Field(
        default="",
        description=(
            "Comma-separated hosts that hmc_upload_iso may download an ISO from, "
            "each written as 'host' or 'host:port'. An entry without a port "
            "permits any port on that host. Empty (the default) refuses every "
            "URL: the tool fetches from the MCP server's network position, so "
            "there is no safe default destination. (HMC_ISO_URL_ALLOWLIST)"
        ),
    )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> Self:
        """Build a config from *values* alone, reading no environment and no dotenv.

        The ordinary constructor resolves every field left unset from the ambient
        ``HMC_*`` environment. That is what an operator running the CLI or the MCP
        server wants. It is not what a process building one config per HMC from
        database rows wants: a stray ``HMC_HOST`` in the deployment environment
        points a backend at a different HMC than its row names, a stray
        ``HMC_SSH_KEY_FILE`` offers the wrong private key, and a stray
        ``HMC_AGENT_ID`` corrupts ownership attribution on every LPAR the process
        stamps — none of which raises.

        Here, a key in *values* naming a field is applied and every field *values*
        omits takes its declared field default. Nothing else can supply a value:
        every field is passed as an explicit constructor argument, and constructor
        arguments are pydantic-settings' highest-priority source. Note that
        ``_env_file=None`` would *not* achieve this — it suppresses a dotenv
        source and never touches the environment (see
        ``docs/environment-variables.md``).

        Validation is unchanged: field validators and the model validator run
        exactly as they do for ``HMCConfig(...)``. Keys naming no field are
        ignored, matching the ``extra="ignore"`` in ``model_config``. A key whose
        value is ``None`` is applied like any other — a nullable database column
        arriving as ``None`` is a validation error for every field but
        ``ssh_key_file`` and ``agent_id``, so omit the key rather than passing
        ``None`` when the intent is "use the default".

        ``model_fields_set`` reports the keys *values* supplied, not the full
        field set, so ``model_dump(exclude_unset=True)`` round-trips and the
        ``verify_ssl`` provenance in the TLS audit record stays accurate.

        A field whose default comes from a factory that takes ``validated_data``
        is not supported here and raises pydantic's own error; ``HMCConfig``
        declares no such field, and a subclass that does should not inherit this
        method.

        Raises:
            ValueError: When *values* omits a field that has no default. The
                omission does not leak — the field is still passed explicitly,
                so the environment is still shut out — but pydantic would report
                it as a type error about ``PydanticUndefined``. This names the
                field and says where to supply it instead.
        """
        missing = sorted(
            name
            for name, field in cls.model_fields.items()
            if field.is_required() and name not in values
        )
        if missing:
            raise ValueError(
                f"{cls.__name__}.from_mapping is missing required settings: "
                + ", ".join(missing)
                + " — supply them in the mapping; from_mapping reads no "
                "environment variables"
            )
        explicit = {
            name: values[name]
            if name in values
            else field.get_default(call_default_factory=True)
            for name, field in cls.model_fields.items()
        }
        config = cls(**explicit)
        # Passing every field explicitly is what closes the leak, but it would
        # also report every field as caller-set. ``model_fields_set`` is a
        # consumer-visible fact: ``model_dump(exclude_unset=True)`` reads it, and
        # ``client._verify_ssl_source`` uses it to name where ``verify_ssl`` came
        # from in the ``tls-verification-disabled`` audit record (#379), which
        # would otherwise say ``explicit-argument`` for a value that came from the
        # field default. Restore it to the keys the caller actually supplied.
        object.__setattr__(
            config, "__pydantic_fields_set__", set(values) & set(cls.model_fields)
        )
        return config

    @field_validator("iso_url_allowlist")
    @classmethod
    def _validate_iso_url_allowlist(cls, v: str) -> str:
        parse_iso_url_allowlist(v)
        return v

    @property
    def iso_url_allowlist_entries(self) -> tuple[tuple[str, int | None], ...]:
        """The allowlist as ``(host, port_or_None)`` pairs; empty when unset."""
        return parse_iso_url_allowlist(self.iso_url_allowlist)

    @field_validator("schema_version", "audit_memento")
    @classmethod
    def _validate_header_value(cls, v: str, info: ValidationInfo) -> str:
        """Both fields reach HTTP request headers: reject non-printable ASCII
        at construction instead of h11's later header refusal, which httpx
        classifies as a retryable ``TransportError`` (issue #839, ADR 0153)."""
        if not v.isascii() or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in v
        ):
            raise ValueError(
                f"{info.field_name} contains non-ASCII or non-printable characters; "
                "use only printable ASCII (U+0020 through U+007E)"
            )
        return v

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id_field(cls, v: str | None) -> str | None:
        if v is not None and v != "":
            validate_agent_id(v)
        return v

    @model_validator(mode="after")
    def _warn_audit_memento_override(self) -> HMCConfig:
        """Warn once per process when HMC_AGENT_ID overrides a custom audit memento."""
        if not (self.agent_id and self.audit_memento != "hmcpctl"):
            return self
        global _reported_memento_override
        msg = (
            f"HMC_AGENT_ID is set ({self.agent_id!r}); the custom "
            f"HMC_AUDIT_MEMENTO value ({self.audit_memento!r}) will be "
            "ignored — X-Audit-Memento is always sent as "
            f"hmcpctl:{self.agent_id}"
        )
        with _override_report_lock:
            if _reported_memento_override:
                _logger.debug(msg)
                return self
            _reported_memento_override = True
        _logger.warning(msg)
        return self

    @property
    def effective_audit_memento(self) -> str:
        """Audit memento value sent in the X-Audit-Memento header.

        Returns ``hmcpctl:<agent_id>`` when ``agent_id`` is set and non-empty;
        otherwise returns ``audit_memento`` (default ``"hmcpctl"``).

        Note: when ``agent_id`` is set, ``audit_memento`` is ignored — the prefix
        is always ``hmcpctl``.  An operator who has customised ``HMC_AUDIT_MEMENTO``
        and then sets ``HMC_AGENT_ID`` will see the audit prefix revert to
        ``hmcpctl``.
        """
        if self.agent_id:
            return f"hmcpctl:{self.agent_id}"
        return self.audit_memento

    @property
    def base_url(self) -> str:
        host = self.host.removeprefix("https://").removeprefix("http://").rstrip("/")
        return f"https://{host}:{self.port}"

    def validate_credentials(self, require_password: bool = True) -> None:
        """Raise ValueError naming any missing connection settings.

        ``require_password`` is False for key-based SSH auth, where a private
        key replaces the password; the REST path always requires it.
        """
        missing = []
        if not self.host:
            missing.append("host (HMC_HOST / --host)")
        if not self.user:
            missing.append("user (HMC_USER / --user)")
        if require_password and not self.password:
            missing.append("password (HMC_PASSWORD / --password)")
        if missing:
            raise ValueError("Missing HMC configuration: " + ", ".join(missing))


class ConfigError(ValueError):
    """Raised when hmcpctl/config.toml is invalid or a profile cannot be selected."""


class NoProfileSelectedError(ConfigError):
    """Raised when no argument, environment variable, or default selects a profile."""


class ConfigFileNotFoundError(ConfigError):
    """Raised when the config file :func:`load_profile` was told to read is absent.

    Distinct from :class:`NoProfileSelectedError`: a missing ``default_profile``
    key is a fact about a file that was actually read, and reporting it for a
    file that was never there sends the operator looking for the wrong problem
    (#915).
    """


@dataclass(frozen=True)
class ConfigDocument:
    """Package-internal snapshot of a resolved path and parsed config document."""

    path: Path | None
    data: dict[str, Any]


def resolve_config_path() -> Path | None:
    """Return the platform-native config.toml path, or None when absent.

    Platform resolution:
    - Linux/other POSIX: $XDG_CONFIG_HOME/hmcpctl/config.toml
      (fallback: ~/.config/hmcpctl/config.toml)
    - macOS:  ~/Library/Application Support/hmcpctl/config.toml
    - Windows: %APPDATA%/hmcpctl/config.toml
      (fallback: ~/.config/hmcpctl/config.toml)
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) if appdata else Path.home() / ".config"
    else:
        # Linux / other POSIX: honour XDG_CONFIG_HOME
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        base = Path(xdg) if xdg else Path.home() / ".config"

    p = base / "hmcpctl" / "config.toml"
    return p if p.exists() else None


def config_dir() -> Path:
    """Return the platform-native hmcpctl/ config directory (no existence check).

    Same platform resolution as resolve_config_path() but never checks whether
    the directory or file exists. Used by ``config init`` to compute the target
    path.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) if appdata else Path.home() / ".config"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "hmcpctl"


def _selected_config_path(config_path: Path | None) -> Path | None:
    """Return *config_path*, or the platform-native path when it is None.

    Raises ConfigError when the platform-native path cannot be resolved:
    :func:`resolve_config_path` reaches ``Path.home()``, which raises
    RuntimeError under a uid with no passwd entry and no HOME — a container or a
    systemd unit. ``access_policy.load_access_policy`` guards the same case.
    """
    if config_path is not None:
        return config_path
    try:
        return resolve_config_path()
    except (RuntimeError, ValueError) as exc:
        raise ConfigError(f"cannot resolve the config path: {exc}") from exc


def _read_config_document(path: Path, *, missing_ok: bool = True) -> dict[str, Any]:
    """Read and parse *path*, converting every failure into a ConfigError.

    Returns ``{}`` when the file is absent and *missing_ok* is true (the
    default): an absent config file is an empty configuration everywhere it is
    read. There is deliberately no ``exists()`` pre-check — that is a TOCTOU,
    and the absent case is the FileNotFoundError arm below.

    When *missing_ok* is false, a missing file raises
    :class:`ConfigFileNotFoundError` naming *path* instead — for a caller that
    already knows there is nothing to select a profile from and would
    otherwise report a missing ``default_profile`` in a file that was never
    there (#915).

    Every other failure is a ConfigError naming *path*, so the callers that
    document ConfigError as their failure type tell the truth and a
    ``try/except ConfigError`` around one of them actually catches. This is the
    single read-and-parse for config.toml; see
    ``access_policy.load_access_policy`` for the same conversion over the
    access-policy file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if missing_ok:
            return {}
        raise ConfigFileNotFoundError(f"{path}: config file not found") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: is not valid UTF-8: {exc}") from exc
    except (OSError, ValueError) as exc:
        # A directory at the path or an unreadable mode lands in OSError;
        # ValueError covers an unusable path string, such as an embedded null
        # byte, which read_text raises before it reaches the filesystem.
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML parse error: {exc}") from exc
    except RecursionError as exc:
        # tomllib recurses on nested arrays and inline tables, so a deeply nested
        # document exhausts the stack before it can report a syntax error. A
        # RecursionError carries no message, hence the fixed clause.
        raise ConfigError(
            f"{path}: TOML parse error: document nesting is too deep"
        ) from exc


def load_config_document() -> ConfigDocument:
    """Resolve and read one fresh package-internal configuration snapshot."""
    path = resolve_config_path()
    data = {} if path is None else _read_config_document(path)
    return ConfigDocument(path, data)


def _coerce_profiles(raw: Any, path: str | Path | None) -> dict[str, Any]:
    """Validate and return the ``profiles`` table as ``dict[str, Any]``.

    ``raw`` is the parsed value of the top-level ``profiles`` key (or None when
    absent). Mirrors :func:`_coerce_nicknames` for the other half of profile
    selection: without it ``doc.get("profiles", {}).keys()`` raises an
    AttributeError from the middle of a dict access when the key is not a table,
    and ``name not in profiles`` quietly degrades into a substring test.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: 'profiles' must be a table of profile name to settings, "
            f"got {type(raw).__name__}"
        )
    # TOML keys are always strings; str() states that for the type checker, which
    # sees only the untyped mapping the isinstance check above narrowed to.
    return {str(name): entry for name, entry in raw.items()}


def _coerce_default_profile(raw: Any, path: str | Path | None) -> str | None:
    """Validate and return the optional default profile name."""
    if raw is not None and not isinstance(raw, str):
        raise ConfigError(f"{path}: 'default_profile' must be a profile-name string")
    return raw


def list_profiles_with_default(
    config_path: Path | None = None,
) -> tuple[list[str], str | None]:
    """Return (profile_names, default_profile_or_none) from one TOML read.

    Never resolves secrets — safe for diagnostics.
    Returns ([], None) when the file is absent or path is None.
    Raises ConfigError on every read, decode, parse, or structure failure.
    """
    path = _selected_config_path(config_path)
    if path is None:
        return [], None
    doc = _read_config_document(path)
    return list(_coerce_profiles(doc.get("profiles"), path)), _coerce_default_profile(
        doc.get("default_profile"), path
    )


def list_profiles(config_path: Path | None = None) -> list[str]:
    """Return profile names from the config file; empty list when absent.

    Never resolves secrets — safe for tab-completion and diagnostics.
    Raises ConfigError on every read, decode, parse, or structure failure.
    """
    return list_profiles_with_default(config_path=config_path)[0]


def _coerce_nicknames(raw: Any, path: str | Path | None) -> dict[str, str]:
    """Validate and return the ``nicknames`` table as ``dict[str, str]``.

    ``raw`` is the parsed value of the top-level ``nicknames`` key (or None
    when absent). Raises ``ConfigError`` naming ``path`` when the table is not
    a mapping or when any target value is not a string, so a malformed table
    fails loudly instead of silently shadowing profile selection.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: 'nicknames' must be a table mapping a friendly name to a "
            f"profile key, got {type(raw).__name__}"
        )
    for key, target in raw.items():
        if not isinstance(target, str):
            raise ConfigError(
                f"{path}: nickname {key!r} must map to a profile-key string, "
                f"got {type(target).__name__}"
            )
    return dict(raw)


def list_nicknames(config_path: Path | None = None) -> dict[str, str]:
    """Return the nicknames table as ``dict[str, str]``; empty when absent.

    Never resolves secrets - safe for diagnostics and display.
    Returns ``{}`` when the file is absent or path is None.
    Raises ConfigError on every read, decode, parse, or structure failure.
    """
    path = _selected_config_path(config_path)
    if path is None:
        return {}
    return _coerce_nicknames(_read_config_document(path).get("nicknames"), path)


def list_profiles_and_nicknames(
    config_path: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Return (profile_names, nicknames) from one TOML read.

    Never resolves secrets — safe for diagnostics and for authorization decisions
    that must mirror :func:`load_profile`'s selection order. One read rather than
    ``list_profiles()`` plus ``list_nicknames()`` so that the two halves of a
    single decision cannot be taken from two different versions of the file.

    Returns ``([], {})`` when the file is absent or *config_path* is None.
    Every other failure is a ConfigError: this feeds ADR 0038's authorization
    decision, whose denial message must interpolate no path and no raw exception
    text, so an unreadable file, a non-UTF-8 one, and a malformed table must all
    arrive as one recognizable type rather than as an OSError or an
    AttributeError from the middle of a dict access.
    """
    path = _selected_config_path(config_path)
    if path is None:
        return [], {}
    doc = _read_config_document(path)
    return list(_coerce_profiles(doc.get("profiles"), path)), _coerce_nicknames(
        doc.get("nicknames"), path
    )


def env_var_value(name: str) -> str | None:
    """*name*'s value from the environment, matched the way ``HMCConfig`` matches it.

    ``HMCConfig`` matches environment names case-insensitively, and this helper
    mirrors that behavior for callers that inspect effective settings.

    Returns ``None`` only when no casing of *name* is set. When several casings
    are set, the **last** one in ``os.environ`` order wins — the exact spelling
    gets no precedence. That is not a tie-break chosen here: pydantic-settings'
    ``parse_env_vars`` folds the whole environment into ``{key.lower(): value}``
    in ``os.environ`` order, so the last match is the one that reaches the field.
    Preferring the exact spelling instead would leave ``HMC_HOST=""`` beside a
    non-empty ``hmc_host`` reading as an unset host to the ADR 0038 gates below
    while the config resolved to the exported one — the fail-open this function
    exists to close.

    Keys are snapshotted before lookup so concurrent environment changes cannot
    raise ``KeyError`` on the authorization path.
    """
    wanted = name.lower()
    found: str | None = None
    for key in list(os.environ):
        if key.lower() == wanted:
            found = os.environ.get(key, found)
    return found


@dataclass(frozen=True)
class _ProfileSelection:
    name: str
    nickname: str | None = None


def _select_profile(
    profiles: Mapping[str, Any],
    nicknames: Mapping[str, str],
    default_profile: Any,
    path: Path | None,
    requested_profile: str | None,
) -> _ProfileSelection:
    """Select one profile and record the nickname that resolved to it."""
    default_profile = _coerce_default_profile(default_profile, path)
    requested = requested_profile or os.environ.get("HMC_PROFILE") or default_profile
    if requested is None:
        raise NoProfileSelectedError(
            f"{path or 'config.toml'}: no default_profile set and no "
            "--profile / HMC_PROFILE supplied"
        )
    if requested in profiles:
        return _ProfileSelection(requested)
    if requested in nicknames:
        target = nicknames[requested]
        if target in profiles:
            return _ProfileSelection(target, requested)
        profile_names = ", ".join(sorted(profiles)) or "(none)"
        nickname_names = ", ".join(sorted(nicknames)) or "(none)"
        raise ConfigError(
            f"nickname {requested!r} targets missing profile {target!r} "
            f"in {path}; available profiles: {profile_names}; "
            f"available nicknames: {nickname_names}"
        )
    profile_names = ", ".join(sorted(profiles)) or "(none)"
    nickname_names = ", ".join(sorted(nicknames)) or "(none)"
    raise ConfigError(
        f"profile {requested!r} not found in {path}; "
        f"available profiles: {profile_names}; "
        f"available nicknames: {nickname_names}"
    )


def _load_profile_from_document(
    doc: dict[str, Any],
    path: Path | None,
    profile: str | None = None,
) -> HMCConfig:
    """Build an HMCConfig for *profile* from an already-parsed *doc*.

    Shared by :func:`load_profile` and callers that already hold the parsed
    document for this invocation. *path* is used only for
    error messages; it is not re-read here.
    """
    profiles = _coerce_profiles(doc.get("profiles"), path)

    # Validate the nicknames table structure whenever the key is
    # present, so a malformed table is a ConfigError regardless of
    # which profile is selected (ADR 0030). No existing config
    # carries a nicknames key, so this cannot break current users.
    nicknames = _coerce_nicknames(doc.get("nicknames"), path)
    selection = _select_profile(
        profiles, nicknames, doc.get("default_profile"), path, profile
    )
    name = selection.name

    selected = profiles[name]
    if not isinstance(selected, dict):
        # Without this, dict() on a non-table raises a bare ValueError whose
        # message is about update sequences, not about the config file.
        raise ConfigError(
            f"{path}: profile {name!r} must be a table of settings, "
            f"got {type(selected).__name__}"
        )
    entry = dict(selected)

    # Validate and resolve secret fields
    if "password" in entry and "password_env" in entry:
        raise ConfigError(
            f"{path}: profile {name!r}: set password or password_env, not both"
        )
    if "password_env" in entry:
        var = entry.pop("password_env")
        if var not in os.environ:
            raise ConfigError(
                f"{path}: profile {name!r}: password_env={var!r} is not set"
            )
        entry["password"] = os.environ[var]

    # Build HMCConfig with correct precedence:
    #   explicit constructor args > HMC_* env vars > TOML profile values
    # pydantic-settings gives init-kwargs the highest priority, so we must NOT
    # pass TOML values as init-kwargs when a matching HMC_* env var is set —
    # otherwise the TOML value would win over the env var.
    #
    # Env-over-TOML is deliberate on this path: an operator overriding a
    # committed profile with HMC_HOST for one invocation is the documented CLI
    # behaviour. A field this profile omits therefore still resolves from HMC_*,
    # and _env_file=None below does not change that — it suppresses a dotenv
    # source (which HMCConfig does not configure at all) and never the
    # environment. HMCConfig.from_mapping is the isolated path; see ADR 0096.
    #
    # The membership test matches the loader's own casing rule via
    # env_var_value: an exact-case test would leave the TOML value in the init
    # kwargs for a lower- or mixed-case export that pydantic-settings does read,
    # and init kwargs outrank every environment source.
    env_prefix = "HMC_"
    filtered_entry = {
        k: v for k, v in entry.items() if env_var_value(env_prefix + k.upper()) is None
    }
    return HMCConfig(_env_file=None, **filtered_entry)


def _inventory_field(path: Path, name: str, entry: dict[str, Any], field: str) -> Any:
    """Return *entry*'s *field* as ``HMCConfig`` would coerce it, else raise ConfigError."""
    info = HMCConfig.model_fields[field]
    assert info.annotation is not None, f"HMCConfig field {field!r} has no annotation"
    value = entry.get(field, info.default)
    try:
        return TypeAdapter(info.annotation).validate_python(value)
    except ValidationError:
        raise ConfigError(
            f"{path}: profile {name!r}: {field} must be "
            f"{'an integer' if field == 'port' else 'a boolean'}, got {value!r}"
        ) from None


def config_inventory(
    config_path: Path | None = None,
    *,
    selected_profile: str | None = None,
    include_selected: bool = False,
) -> dict[str, Any]:
    """Read validated, secret-free profile metadata from one TOML snapshot.

    ``include_selected`` adds the effective non-secret configuration for
    ``selected_profile`` (or the normal environment/default selection). Raw
    passwords, password environment-variable names, and SSH key paths are
    never returned.
    """
    path = _selected_config_path(config_path)
    if path is None:
        return {"profiles": [], "config_file": None}
    doc = _read_config_document(path)
    profiles = _coerce_profiles(doc.get("profiles"), path)
    nicknames = _coerce_nicknames(doc.get("nicknames"), path)
    default_profile = doc.get("default_profile")

    profile_entries: list[dict[str, Any]] = []
    for name, entry in profiles.items():
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{path}: profile {name!r} must be a TOML table, "
                f"got {type(entry).__name__}"
            )
        profile_entries.append(
            {
                "name": name,
                "host": entry.get("host", ""),
                "user": entry.get("user", ""),
                "port": _inventory_field(path, name, entry, "port"),
                "verify_ssl": _inventory_field(path, name, entry, "verify_ssl"),
                "is_default": name == default_profile,
                "has_password": "password" in entry  # pragma: allowlist secret
                or "password_env" in entry,  # pragma: allowlist secret
                "has_ssh_key": "ssh_key_file" in entry,
            }
        )
    profile_names = set(profiles)
    result: dict[str, Any] = {
        "profiles": profile_entries,
        "nicknames": [
            {"name": name, "target": target, "target_exists": target in profile_names}
            for name, target in nicknames.items()
        ],
        "config_file": str(path),
    }
    if not include_selected:
        return result

    selection = _select_profile(
        profiles, nicknames, default_profile, path, selected_profile
    )
    raw_profile = profiles[selection.name]
    cfg = _load_profile_from_document(doc, path, selected_profile)
    result["selected"] = {
        "profile": selection.name,
        "resolved_from": selection.nickname,
        "host": cfg.host,
        "port": cfg.port,
        "user": cfg.user,
        "verify_ssl": cfg.verify_ssl,
        "timeout": cfg.timeout,
        "audit_memento": cfg.audit_memento,
        "schema_version": cfg.schema_version or "(not set)",
        "authorize_power_operations": cfg.authorize_power_operations,
        "password_configured": bool(
            isinstance(raw_profile, dict)
            and (raw_profile.get("password") or raw_profile.get("password_env"))
        ),
        "ssh_key_configured": bool(
            isinstance(raw_profile, dict) and raw_profile.get("ssh_key_file")
        ),
    }
    return result


def load_profile(
    profile: str | None = None,
    config_path: Path | None = None,
) -> HMCConfig:
    """Load and return an HMCConfig for the selected profile.

    Profile selection order:
      1. explicit ``profile`` argument
      2. ``HMC_PROFILE`` environment variable
      3. ``default_profile`` key in the TOML file
      4. ConfigError

    Precedence (highest to lowest):
      explicit HMCConfig constructor args > HMC_* env vars > TOML profile values

    Checkout-local .env files are NOT loaded.

    Args:
        profile: Profile name to select, or None to use env/TOML default.
        config_path: Override the config file path (for testing).

    Returns:
        HMCConfig populated from the selected profile with env-var overrides.

    Raises:
        ConfigFileNotFoundError: When *config_path* is given and does not
            exist, or when the platform-native config file is absent and no
            profile is requested (no explicit ``profile`` and no
            ``HMC_PROFILE``) to select from it.
        ConfigError: When the file cannot be read, decoded, or parsed, when a
            table it needs is malformed, when no profile is selected, when the
            selected profile is absent, or when secret config is invalid.
    """
    if config_path is not None:
        doc = _read_config_document(config_path, missing_ok=False)
        return _load_profile_from_document(doc, config_path, profile)

    requested = profile or os.environ.get("HMC_PROFILE")
    path = _selected_config_path(None)
    doc: dict[str, Any]
    if requested is None:
        # Nothing would select a profile even if the file existed, so read
        # with missing_ok=False: a platform file resolve_config_path() never
        # found, or one it found but that vanished before this read, is
        # reported as missing by path rather than as a misleading
        # no-default_profile message (#915). config_dir() stands in for
        # *path* only when resolve_config_path() never had one to report.
        doc = _read_config_document(
            path or config_dir() / "config.toml", missing_ok=False
        )
    else:
        doc = {} if path is None else _read_config_document(path)
    return _load_profile_from_document(doc, path, profile)


def build_config(
    profile: str | None = None,
    *,
    document: ConfigDocument | None = None,
    **overrides: Any,
) -> HMCConfig:
    """Build configuration from CLI options, environment, and a TOML profile.

    Environment-only construction is used when nothing selects a profile. Errors
    reading or validating an authored configuration are propagated unchanged.
    """
    filtered = {key: value for key, value in overrides.items() if value is not None}

    explicit_host = filtered.get("host")
    if not explicit_host and not env_var_value("HMC_HOST"):
        config_path = document.path if document is not None else resolve_config_path()
        if config_path is not None or profile or os.environ.get("HMC_PROFILE"):
            try:
                base = (
                    _load_profile_from_document(document.data, document.path, profile)
                    if document is not None
                    else load_profile(profile=profile)
                )
                if filtered:
                    merged = {key: getattr(base, key) for key in base.model_fields_set}
                    merged.update(filtered)
                    base = HMCConfig(**merged)
                return base
            except NoProfileSelectedError:
                pass

    return HMCConfig(**filtered)
