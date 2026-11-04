"""HMCClient never reuses a keep-alive connection the HMC has idled out (#1926).

The fake HMC models the race observed live: the HMC's idle timer starts before
the client's, so a request can reach a connection the HMC is already closing
before the client has seen the FIN. It closes such a connection without a
response, which httpx reports as ``Server disconnected``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCTransportError

# Below every gap the stale-connection case waits, so an unfixed client reuses it.
_HMC_IDLE_CLOSE_SECONDS = 1.1
# Longer than the HMC's idle close here; shorter than httpx's 5 s default expiry.
_STALE_GAP_SECONDS = 1.3
_PATH = "/rest/api/uom/ManagementConsole"


class _IdleClosingHMC:
    """HTTP/1.1 server that drops a request arriving on an idled-out connection."""

    def __init__(self) -> None:
        self.received: list[int] = []
        self.drop_reused = False
        self._connections = 0

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        loop = asyncio.get_running_loop()
        self._connections += 1
        connection = self._connections
        last_response: float | None = None
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                self.received.append(connection)
                idle = None if last_response is None else loop.time() - last_response
                if idle is not None and (
                    self.drop_reused or idle >= _HMC_IDLE_CLOSE_SECONDS
                ):
                    return
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                last_response = loop.time()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


def _self_signed_cert(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certfile = directory / "cert.pem"
    keyfile = directory / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return _self_signed_cert(tmp_path_factory.mktemp("keepalive-tls"))


@pytest_asyncio.fixture
async def hmc(
    tls_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[_IdleClosingHMC, HMCClient]]:
    certfile, keyfile = tls_files
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    fake = _IdleClosingHMC()
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    config = HMCConfig.from_mapping(
        {
            "host": "127.0.0.1",
            "port": port,
            "user": "u",
            "password": "p",
            "verify_ssl": True,
        }
    )
    client = HMCClient(config)
    try:
        yield fake, client
    finally:
        await client._http.aclose()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_request_after_idle_gap_opens_fresh_connection(hmc) -> None:
    fake, client = hmc
    await client._request("GET", _PATH)
    await asyncio.sleep(_STALE_GAP_SECONDS)

    response = await client._request("GET", _PATH)

    assert response.status_code == 200
    assert fake.received == [1, 2]


@pytest.mark.asyncio
async def test_back_to_back_requests_reuse_one_connection(hmc) -> None:
    fake, client = hmc
    await client._request("GET", _PATH)
    await asyncio.sleep(0.1)
    await client._request("GET", _PATH)

    assert fake.received == [1, 1]


@pytest.mark.asyncio
async def test_dropped_request_is_sent_once(hmc) -> None:
    fake, client = hmc
    fake.drop_reused = True
    await client._request("GET", _PATH)

    with pytest.raises(HMCTransportError, match="Server disconnected"):
        await client._request("GET", _PATH)

    assert fake.received == [1, 1]


@pytest.mark.asyncio
async def test_pool_keeps_httpx_default_caps(hmc) -> None:
    _fake, client = hmc
    pool = client._http._transport._pool  # type: ignore[attr-defined]

    assert (pool._max_connections, pool._max_keepalive_connections) == (100, 20)
