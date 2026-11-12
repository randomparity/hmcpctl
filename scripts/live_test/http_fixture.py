"""Invocation-owned one-file HTTP fixture for live ISO uploads."""

from __future__ import annotations

import functools
import http.server
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from hmcpctl.config import env_var_value

if TYPE_CHECKING:
    from .config import LiveTestConfig


class _OneFileHandler(http.server.SimpleHTTPRequestHandler):
    """Serves only the configured ISO; its directory may hold private build files."""

    served = ""

    def send_head(self) -> Any:
        if unquote(urlparse(self.path).path) != "/" + self.served:
            self.send_error(404)
            return None
        return super().send_head()


class IsoHttpServer:
    """Invocation-owned HTTP fixture for virtual-media ISO uploads."""

    def __init__(self) -> None:
        self._server: http.server.HTTPServer | None = None

    def start(self, config: LiveTestConfig) -> None:
        """Start serving the configured ISO, and nothing beside it, once per invocation."""
        _allow_iso_host(config)
        if self._server is not None:
            return
        handler = functools.partial(
            type("_IsoHandler", (_OneFileHandler,), {"served": config.iso_filename}),
            directory=str(Path(config.iso_path).parent),
        )
        startup_decided = threading.Event()
        serve_allowed = False
        server = http.server.HTTPServer(
            (config.iso_bind_host, config.iso_http_port), handler
        )

        def serve() -> None:
            startup_decided.wait()
            if serve_allowed:
                server.serve_forever()

        try:
            threading.Thread(target=serve, daemon=True).start()
        except BaseException:
            # A native worker may start after Thread.start is interrupted.
            startup_decided.set()
            server.server_close()
            raise
        self._server = server
        serve_allowed = True
        startup_decided.set()

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
