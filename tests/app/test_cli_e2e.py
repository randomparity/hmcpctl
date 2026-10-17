"""End-to-end CLI tests: the real client against a mocked HMC over HTTPS.

The unit tests mock at the client boundary (``client_from_env`` / respx), so
the full config → HMCClient → httpx → TLS → XML-parse → CLI-output stack is
only exercised here, against a threaded mock HMC speaking TLS with a
throwaway self-signed cert.

This replaces the old ``scripts/manual_e2e.py`` harness, which CI never ran and
which had rotted: the client forces ``https://`` (``HMCConfig.base_url``) but
the mock spoke plain HTTP, so every invocation failed with an SSL error.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import shlex
import ssl
import struct
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from click import unstyle
from conftest import live_fixture
from typer.testing import CliRunner

from hmcpctl import cli

RUNNER = CliRunner()

LOGON = b"""<?xml version="1.0"?>
<LogonResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/">
  <X-API-Session>tok</X-API-Session>
</LogonResponse>"""

SYSTEMS = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:33333333-3333-4333-8333-333333333333</id>
    <title>ManagedSystem:9179-MHD*06064FV</title>
    <link rel="SELF" href="https://hmc/rest/api/uom/ManagedSystem/33333333-3333-4333-8333-333333333333"/>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <SystemName>server1</SystemName>
        <State>operating</State>
        <MachineTypeModelAndSerialNumber>
          <MachineType>9179</MachineType>
          <Model>MHD</Model>
          <SerialNumber>06064FV</SerialNumber>
        </MachineTypeModelAndSerialNumber>
        <IPAddress>10.0.0.11</IPAddress>
      </ManagedSystem>
    </content>
  </entry>
</feed>"""

SYSTEM_SUMMARIES = (
    b'[{"UUID":"33333333-3333-4333-8333-333333333333","SystemName":"server1",'
    b'"State":"operating","StateDetail":""}]'
)

LPARS = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:11111111-1111-1111-1111-111111111111</id>
    <title>LogicalPartition</title>
    <link rel="SELF" href="https://hmc/rest/api/uom/LogicalPartition/11111111-1111-1111-1111-111111111111"/>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>aixprod</PartitionName>
        <PartitionID>3</PartitionID>
        <PartitionState>running</PartitionState>
        <PartitionType>AIX/Linux</PartitionType>
        <OperatingSystemVersion>AIX 7.3</OperatingSystemVersion>
        <ResourceMonitoringControlState>active</ResourceMonitoringControlState>
      </LogicalPartition>
    </content>
  </entry>
  <entry>
    <id>urn:uuid:22222222-2222-2222-2222-222222222222</id>
    <title>LogicalPartition</title>
    <link rel="SELF" href="https://hmc/rest/api/uom/LogicalPartition/22222222-2222-2222-2222-222222222222"/>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>linuxdev</PartitionName>
        <PartitionID>4</PartitionID>
        <PartitionState>not activated</PartitionState>
        <PartitionType>AIX/Linux</PartitionType>
      </LogicalPartition>
    </content>
  </entry>
</feed>"""

# The captured V10R3 answer to a PowerOn submission (#1161).
JOB_ENTRY = live_fixture("rest-poweron-submit")["body"].encode()


class _MockHMC(BaseHTTPRequestHandler):
    """Minimal HMC REST stand-in: logon, quick/All, one system and its LPARs, logoff.

    ``protocol_version`` is HTTP/1.1 and every response carries an explicit
    ``Content-Length``: httpx (the real client) needs the length to know where
    the body ends, and the request body must be drained before the next request
    on the keep-alive connection.  Without these the TLS read fails with
    ``httpx.ReadError``.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _drain_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_PUT(self):
        body = self._drain_body()
        request_log = self.server.request_log
        request_log.append(
            {
                "path": self.path,
                "session": self.headers.get("X-API-Session"),
                "content_type": self.headers.get("Content-Type"),
                "accept": self.headers.get("Accept"),
                "body": body,
            }
        )
        if self.path == "/rest/api/web/Logon":
            self._send(LOGON)
        elif self.path == (
            "/rest/api/uom/LogicalPartition/"
            "11111111-1111-1111-1111-111111111111/do/PowerOn"
        ):
            self._send(JOB_ENTRY)
        else:
            self._send(b"", 404)

    def do_DELETE(self):
        self._send(b"", 204)

    def do_GET(self):
        # Strip query string for routing — the client may append ?group=...
        path = self.path.split("?")[0]
        if path == "/rest/api/uom/ManagedSystem/33333333-3333-4333-8333-333333333333":
            self._send(SYSTEMS)
        elif path == "/rest/api/uom/ManagedSystem/quick/All":
            self._send(SYSTEM_SUMMARIES)
        elif (
            path
            == "/rest/api/uom/ManagedSystem/33333333-3333-4333-8333-333333333333/LogicalPartition"
        ):
            self._send(LPARS)
        else:
            self._send(b"", 404)

    def _send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


def _self_signed_cert(tmp_path: Path) -> tuple[str, str]:
    """Generate a throwaway self-signed cert for 127.0.0.1 (mock HMC TLS)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
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
    certfile = tmp_path / "mock_hmc_cert.pem"
    keyfile = tmp_path / "mock_hmc_key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(certfile), str(keyfile)


@pytest.fixture
def mock_hmc(tmp_path, request):
    """A threaded HTTPS mock HMC and its trusted certificate path."""
    certfile, keyfile = _self_signed_cert(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), getattr(request, "param", _MockHMC))
    httpd.request_log = []
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, certfile
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _env(port: int, certificate_path: str) -> dict[str, str]:
    return {
        "HMC_HOST": "127.0.0.1",
        "HMC_PORT": str(port),
        "HMC_USER": "hscroot",
        "HMC_PASSWORD": secrets.token_hex(8),  # the mock HMC accepts any logon
        "HMC_VERIFY_SSL": "true",
        "SSL_CERT_FILE": certificate_path,
    }


def test_systems_list_e2e(mock_hmc):
    server, certificate_path = mock_hmc
    result = RUNNER.invoke(
        cli.app,
        ["systems", "list"],
        env=_env(server.server_address[1], certificate_path),
    )

    assert result.exit_code == 0
    assert "server1" in result.stdout
    assert "operating" in result.stdout
    assert "9179-MHD*06064FV" in result.stdout
    assert "10.0.0.11" in result.stdout


def test_lpars_list_e2e(mock_hmc):
    server, certificate_path = mock_hmc
    result = RUNNER.invoke(
        cli.app, ["lpars", "list"], env=_env(server.server_address[1], certificate_path)
    )

    assert result.exit_code == 0
    assert "aixprod" in result.stdout
    assert "linuxdev" in result.stdout
    assert "running" in result.stdout


def test_lpars_list_json_e2e(mock_hmc):
    server, certificate_path = mock_hmc
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "list", "--json"],
        env=_env(server.server_address[1], certificate_path),
    )

    assert result.exit_code == 0
    assert "11111111-1111-1111-1111-111111111111" in result.stdout
    assert "22222222-2222-2222-2222-222222222222" in result.stdout


def test_lpar_power_on_e2e(mock_hmc):
    server, certificate_path = mock_hmc
    lpar_uuid = "11111111-1111-1111-1111-111111111111"
    result = RUNNER.invoke(
        cli.app,
        ["lpars", "power-on", lpar_uuid, "--force", "--yes"],
        env=_env(server.server_address[1], certificate_path),
    )

    assert result.exit_code == 0
    assert lpar_uuid in result.stdout
    power_on_request = next(
        request
        for request in server.request_log
        if request["path"].endswith("/do/PowerOn")
    )
    assert power_on_request["path"] == (
        f"/rest/api/uom/LogicalPartition/{lpar_uuid}/do/PowerOn"
    )
    assert power_on_request["session"] == "tok"
    assert power_on_request["content_type"] == (
        "application/vnd.ibm.powervm.web+xml; type=JobRequest"
    )
    assert power_on_request["accept"] == "application/atom+xml"
    body = power_on_request["body"].decode()
    assert "<OperationName" in body and ">PowerOn<" in body
    assert "<GroupName" in body and ">LogicalPartition<" in body


# TLS trust (#1595): with HMC_VERIFY_SSL=true the client trusts its bundled roots
# plus SSL_CERT_FILE / SSL_CERT_DIR, and nothing else. docs/configuration.md
# ("TLS trust") documents exactly these four behaviours.


def _systems_list(server, certificate_path, **overrides):
    env = _env(server.server_address[1], certificate_path)
    env.update(overrides)
    return RUNNER.invoke(cli.app, ["systems", "list"], env=env)


def test_ssl_cert_file_trust_verifies_hmc_e2e(mock_hmc):
    server, certificate_path = mock_hmc

    result = _systems_list(server, certificate_path)

    assert result.exit_code == 0
    assert "server1" in result.stdout


def test_verify_ssl_without_trusted_ca_fails_e2e(mock_hmc, monkeypatch):
    server, certificate_path = mock_hmc
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    env = _env(server.server_address[1], certificate_path)
    del env["SSL_CERT_FILE"]
    # Disable the system store so the test cert (self-signed, not in OS store)
    # is not trusted.  ADR 0222: trust_system_store=false falls back to certifi.
    env["HMC_TRUST_SYSTEM_STORE"] = "false"

    result = RUNNER.invoke(cli.app, ["systems", "list"], env=env)

    assert result.exit_code != 0
    assert "CERTIFICATE_VERIFY_FAILED" in result.output


def test_requests_ca_bundle_alone_does_not_trust_e2e(mock_hmc, monkeypatch):
    server, certificate_path = mock_hmc
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    env = _env(server.server_address[1], certificate_path)
    del env["SSL_CERT_FILE"]
    env["REQUESTS_CA_BUNDLE"] = certificate_path
    # Disable the system store so REQUESTS_CA_BUNDLE alone is provably ignored.
    # ADR 0222: trust_system_store=false falls back to certifi only.
    env["HMC_TRUST_SYSTEM_STORE"] = "false"

    result = RUNNER.invoke(cli.app, ["systems", "list"], env=env)

    assert result.exit_code != 0
    assert "CERTIFICATE_VERIFY_FAILED" in result.output


def test_unterminated_pem_bundle_fails_to_load_e2e(mock_hmc, tmp_path):
    server, certificate_path = mock_hmc
    pem = Path(certificate_path).read_text().rstrip("\n")
    joined = tmp_path / "joined.pem"
    joined.write_text(pem + "\n" + pem + "\n")
    unterminated = tmp_path / "unterminated.pem"
    unterminated.write_text(pem + pem)

    good = _systems_list(server, certificate_path, SSL_CERT_FILE=str(joined))
    bad = _systems_list(server, certificate_path, SSL_CERT_FILE=str(unterminated))

    assert good.exit_code == 0
    assert bad.exit_code != 0
    assert "PEM lib" in bad.output


# SSL_CERT_DIR (#1607): httpx passes capath=SSL_CERT_DIR to ssl.create_default_context,
# which uses OpenSSL's hash-directory format.  Each cert must be accessible under the
# name "<subject-hash>.0" where the hash is the first four bytes of SHA-1(subject DER)
# interpreted as a little-endian uint32 and formatted as 8 lowercase hex digits.


def _ssl_cert_dir_hash(cert_path: str) -> str:
    """Return the OpenSSL subject-hash filename stem for a PEM certificate.

    Algorithm: SHA-1 of the *inner* content of the DER-encoded subject Name
    (i.e. the SEQUENCE content bytes, with the outer tag and length stripped);
    first four bytes reinterpreted as a little-endian uint32; formatted as 8
    lowercase hex digits.  This matches ``openssl x509 -subject_hash -noout``
    and what ``ssl.create_default_context(capath=…)`` expects.
    """
    from cryptography.x509 import load_pem_x509_certificate

    cert = load_pem_x509_certificate(Path(cert_path).read_bytes())
    full_der = cert.subject.public_bytes()
    # Strip the outer SEQUENCE tag (0x30) and its length to get the inner bytes.
    # The length field is one byte when < 0x80 (short form), otherwise the first
    # byte encodes how many subsequent bytes hold the actual length.
    length_byte = full_der[1]
    inner = full_der[2:] if length_byte < 0x80 else full_der[2 + (length_byte & 0x7F) :]
    digest = hashlib.sha1(inner).digest()
    return f"{struct.unpack_from('<I', digest[:4])[0]:08x}"


def test_ssl_cert_dir_hashed_trusts_hmc_e2e(mock_hmc, tmp_path):
    """A hash-named cert in SSL_CERT_DIR lets the client verify the mock HMC."""
    server, certificate_path = mock_hmc
    ssl_dir = tmp_path / "ssl_dir"
    ssl_dir.mkdir()
    hash_name = _ssl_cert_dir_hash(certificate_path)
    (ssl_dir / f"{hash_name}.0").write_bytes(Path(certificate_path).read_bytes())

    # Pass None to remove SSL_CERT_FILE so httpx falls through to SSL_CERT_DIR.
    result = _systems_list(
        server,
        certificate_path,
        SSL_CERT_FILE=None,
        SSL_CERT_DIR=str(ssl_dir),
    )

    assert result.exit_code == 0
    assert "server1" in result.output


def test_ssl_cert_dir_unhashed_trusts_nothing_e2e(mock_hmc, tmp_path):
    """A directory with a bare .pem file (not hash-named) trusts nothing."""
    server, certificate_path = mock_hmc
    ssl_dir = tmp_path / "ssl_dir"
    ssl_dir.mkdir()
    (ssl_dir / "mock_hmc_cert.pem").write_bytes(Path(certificate_path).read_bytes())

    # Pass None to remove SSL_CERT_FILE so httpx falls through to SSL_CERT_DIR.
    result = _systems_list(
        server,
        certificate_path,
        SSL_CERT_FILE=None,
        SSL_CERT_DIR=str(ssl_dir),
    )

    assert result.exit_code != 0
    assert "CERTIFICATE_VERIFY_FAILED" in result.output


def test_ssl_cert_file_wins_over_ssl_cert_dir_e2e(mock_hmc, tmp_path):
    """When both SSL_CERT_FILE and SSL_CERT_DIR are set, SSL_CERT_FILE is used.

    Proof: SSL_CERT_DIR points to an unhashed directory that would fail on its
    own — if SSL_CERT_DIR were consulted the connection would fail.  SSL_CERT_FILE
    points to the valid cert: success proves SSL_CERT_FILE was used, not
    SSL_CERT_DIR.
    """
    server, certificate_path = mock_hmc
    # Unhashed dir: present but not consulted when SSL_CERT_FILE is also set.
    unhashed_dir = tmp_path / "unhashed"
    unhashed_dir.mkdir()
    (unhashed_dir / "mock_hmc_cert.pem").write_bytes(
        Path(certificate_path).read_bytes()
    )

    result = _systems_list(
        server,
        certificate_path,
        SSL_CERT_FILE=certificate_path,
        SSL_CERT_DIR=str(unhashed_dir),
    )

    assert result.exit_code == 0
    assert "server1" in result.output


# Constructed journey state, using the captured storage fixtures' XML vocabulary.
# This is offline composition evidence, not a capture or a hardware verification.
_UOM = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
_SYSTEM = "33333333-3333-4333-8333-333333333333"
_OTHER_SYSTEM = "44444444-4444-4444-8444-444444444444"
_LPAR = "11111111-1111-4111-8111-111111111111"
_OTHER_LPAR = "22222222-2222-4222-8222-222222222222"
_VIOS = "55555555-5555-4555-8555-555555555555"
_VG = "66666666-6666-4666-8666-666666666666"
_VIOS_PATH = f"/rest/api/uom/VirtualIOServer/{_VIOS}"
_VG_PATH = f"{_VIOS_PATH}/VolumeGroup/{_VG}"
_DISK = "cleanup-[disk]-雪-" + "long-" * 15
_ISO = "cleanup-[image]-雪.iso"


def _entry(kind, uuid, body):
    return (
        f'<entry xmlns="http://www.w3.org/2005/Atom"><id>urn:uuid:{uuid}</id>'
        f'<content><{kind} xmlns="{_UOM}">{body}</{kind}></content></entry>'
    ).encode()


def _feed(*entries):
    return (
        b'<feed xmlns="http://www.w3.org/2005/Atom">' + b"".join(entries) + b"</feed>"
    )


def _mapping(name, kind, field, target, lpar=_LPAR):
    device = (
        "VirtualOpticalTargetDevice"
        if kind == "VirtualOpticalMedia"
        else "LogicalVolumeVirtualTargetDevice"
    )
    link = (
        f'<AssociatedLogicalPartition href="/rest/api/uom/LogicalPartition/{lpar}"/>'
        if lpar
        else ""
    )
    return (
        f"<VirtualSCSIMapping>{link}<ServerAdapter><AdapterName>vhost0</AdapterName>"
        f"</ServerAdapter><TargetDevice><{device}>"
        f"<TargetName>{target}</TargetName></{device}>"
        f"</TargetDevice><Storage><{kind}><{field}>{name}</{field}></{kind}>"
        "</Storage></VirtualSCSIMapping>"
    )


def _cleanup_state():
    mappings = (
        _mapping(_DISK, "VirtualDisk", "DiskName", "vtscsi0")
        + _mapping(_ISO, "VirtualOpticalMedia", "MediaName", "vtscsi1")
        + _mapping("keep-disk", "VirtualDisk", "DiskName", "vtscsi2", _OTHER_LPAR)
    )
    vios = _entry(
        "VirtualIOServer",
        _VIOS,
        f"<Metadata><Atom><AtomID>{_VIOS}</AtomID></Atom></Metadata>"
        "<PartitionName>vios-a</PartitionName><PartitionID>10</PartitionID>"
        f'<AssociatedManagedSystem href="/rest/api/uom/ManagedSystem/{_SYSTEM}"/>'
        f"<VirtualSCSIMappings>{mappings}</VirtualSCSIMappings>"
        "<VirtualFibreChannelMappings/>",
    )
    disks = "".join(
        f"<VirtualDisk><DiskName>{name}</DiskName><DiskCapacity>1</DiskCapacity>"
        "</VirtualDisk>"
        for name in (_DISK, "keep-disk")
    )
    media = "".join(
        f"<VirtualOpticalMedia><MediaName>{name}</MediaName><Size>1</Size>"
        "</VirtualOpticalMedia>"
        for name in (_ISO, "keep.iso")
    )
    vg = _entry(
        "VolumeGroup",
        _VG,
        "<GroupName>datavg</GroupName><GroupCapacity>20</GroupCapacity>"
        f"<FreeSpace>16</FreeSpace><VirtualDisks>{disks}</VirtualDisks>"
        "<MediaRepositories><VirtualMediaRepository><RepositoryName>VMLibrary</RepositoryName>"
        f"<OpticalMedia>{media}</OpticalMedia></VirtualMediaRepository></MediaRepositories>",
    )
    lpars = {
        uuid: _entry(
            "LogicalPartition",
            uuid,
            f"<PartitionName>{name}</PartitionName><PartitionID>{index}</PartitionID>"
            "<PartitionState>not activated</PartitionState><PartitionType>AIX/Linux</PartitionType>",
        )
        for index, (uuid, name) in enumerate(
            ((_LPAR, "cleanup"), (_OTHER_LPAR, "keep")), 1
        )
    }
    return {
        "vios": vios,
        "vg": vg,
        "lpars": lpars,
        "fail_delete": False,
        "fail_mapping": False,
    }


class _CleanupHMC(_MockHMC):
    def _send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"journey-v1"')
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        path = self.path.split("?")[0]
        state = self.server.journey
        self.server.request_log.append(("GET", self.path, b""))
        routes = {
            f"{_VIOS_PATH}/VolumeGroup": _feed(state["vg"]),
            _VG_PATH: state["vg"],
            _VIOS_PATH: state["vios"],
            f"/rest/api/uom/ManagedSystem/{_SYSTEM}/VirtualIOServer": _feed(
                state["vios"]
            ),
            f"/rest/api/uom/ManagedSystem/{_OTHER_SYSTEM}/VirtualIOServer": _feed(),
            f"/rest/api/uom/ManagedSystem/{_SYSTEM}/LogicalPartition": _feed(
                *state["lpars"].values()
            ),
            f"/rest/api/uom/ManagedSystem/{_OTHER_SYSTEM}/LogicalPartition": _feed(),
        }
        summaries = []
        for uuid, name in ((_SYSTEM, "frame-a"), (_OTHER_SYSTEM, "frame-b")):
            summaries.append({"UUID": uuid, "SystemName": name, "State": "operating"})
            routes[f"/rest/api/uom/ManagedSystem/{uuid}"] = _entry(
                "ManagedSystem",
                uuid,
                f"<SystemName>{name}</SystemName><State>operating</State>",
            )
        routes["/rest/api/uom/ManagedSystem/quick/All"] = json.dumps(summaries).encode()
        for uuid, entry in state["lpars"].items():
            routes[f"/rest/api/uom/LogicalPartition/{uuid}"] = entry
            routes[f"/rest/api/uom/LogicalPartition/{uuid}/quick/PartitionState"] = (
                b"not activated"
            )
            for kind in (
                "ClientNetworkAdapter",
                "VirtualSCSIClientAdapter",
                "VirtualFibreChannelClientAdapter",
                "VirtualNICDedicated",
            ):
                routes[f"/rest/api/uom/LogicalPartition/{uuid}/{kind}"] = _feed()
        self._send(routes.get(path, b""), 200 if path in routes else 404)

    def do_POST(self):
        body = self._drain_body()
        self.server.request_log.append(("POST", self.path, body))
        path = self.path.split("?")[0]
        if self.headers.get("If-Match") != '"journey-v1"':
            self._send(b"", 412)
            return
        posts = [
            item
            for item in self.server.request_log
            if isinstance(item, tuple) and item[0] == "POST"
        ]
        if (
            path == _VIOS_PATH
            and self.server.journey["fail_mapping"]
            and len(posts) == 2
        ):
            self._send(b"", 412)
            return
        key, uuid = ("vg", _VG) if path == _VG_PATH else ("vios", _VIOS)
        if path not in (_VG_PATH, _VIOS_PATH):
            self._send(b"", 404)
            return
        wrapper = ET.fromstring(_entry("Placeholder", uuid, ""))
        content = wrapper.find("{http://www.w3.org/2005/Atom}content")
        content.remove(content[0])
        content.append(ET.fromstring(body))
        self.server.journey[key] = ET.tostring(wrapper)
        self._send(self.server.journey[key])

    def do_DELETE(self):
        self._drain_body()
        self.server.request_log.append(("DELETE", self.path, b""))
        if self.path == f"/rest/api/uom/LogicalPartition/{_LPAR}":
            if self.server.journey["fail_delete"]:
                self._send(b"<Message>injected delete failure</Message>", 500)
                return
            del self.server.journey["lpars"][_LPAR]
        elif self.path != "/rest/api/web/Logon":
            self._send(b"", 404)
            return
        self._send(b"", 204)


# Run the installed console script after replacing only the SSH command transport.
# The subprocess keeps real selectors, ownership parsing, operations and rendering.
_CLEANUP_LAUNCH = r"""
import runpy, shlex, sys
from hmcpctl.ssh import profiles
async def command(config, cmd):
    tokens = shlex.split(cmd)
    assert tokens == ["lssyscfg", "-r", "lpar", "-m", "frame-a", "--filter",
                      "lpar_names=cleanup", "-F", "description"], tokens
    return "[hmcpctl owner:journey created:2026-10-17]"
profiles.run_hmc_command = command
script = sys.argv.pop(1)
sys.argv[0] = script
runpy.run_path(script, run_name="__main__")
"""


@pytest.fixture
def cleanup_cli(mock_hmc, tmp_path):
    from test_entry_point_equivalence import _child_env, _launchers

    server, cert = mock_hmc
    server.journey = _cleanup_state()
    executable = _launchers()[0][0]
    env = {
        k: v
        for k, v in _child_env(tmp_path).items()
        if not k.upper().startswith(("HMC_", "HMCPCTL_"))
    }
    env.update(_env(server.server_address[1], cert))
    env.update(
        HMC_AGENT_ID="journey",
        XDG_CONFIG_HOME=str(tmp_path),
        HMCPCTL_STATE_DIR=str(tmp_path / "state"),
        NO_PROXY="127.0.0.1",
        COLUMNS="40",
        TERM="xterm",
        GITHUB_ACTIONS="1",
    )

    def invoke(*args, code=0, width=40, input_text=""):
        result = subprocess.run(
            [sys.executable, "-c", _CLEANUP_LAUNCH, executable, *args],
            input=input_text,
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env={**env, "COLUMNS": str(width)},
            timeout=30,
            check=False,
        )
        assert result.returncode == code, f"{args}\n{result.stdout}\n{result.stderr}"
        # CI styles help despite NO_COLOR; machine output must remain byte-clean.
        bounded = unstyle(result.stdout) if "--help" in args else result.stdout
        assert len(bounded) < 20000, args
        assert len(result.stderr) < 20000, args
        return result

    return invoke, server


def _mutations(server):
    return [
        item
        for item in server.request_log
        if isinstance(item, tuple)
        and item[0] in ("POST", "DELETE")
        and item[1] != "/rest/api/web/Logon"
    ]


@pytest.mark.parametrize("mock_hmc", [_CleanupHMC], indirect=True)
def test_cleanup_journey(cleanup_cli):
    run, server = cleanup_cli
    for group in ("systems", "lpars", "vios", "storage"):
        assert "Usage:" in unstyle(run(group, "--help").stdout)
    assert not server.request_log
    systems = json.loads(run("systems", "list", "--json").stdout)
    system = next(
        row["UUID"] for row in systems if row["Resource"]["SystemName"] == "frame-a"
    )
    lpars = json.loads(run("lpars", "list", "--system", system, "--json").stdout)
    lpar = next(
        row["UUID"]
        for row in lpars["entries"]
        if row["Resource"]["PartitionName"] == "cleanup"
    )
    vios = json.loads(run("vios", "list", "--system", system, "--json").stdout)[
        "entries"
    ][0]["UUID"]
    vg = json.loads(
        run("storage", "list-vgs", vios, "--system", system, "--json").stdout
    )[0]
    media = json.loads(
        run("storage", "list-optical-media", vios, "--vg", vg["name"], "--json").stdout
    )
    iso = next(row["name"] for row in media if row["name"] != "keep.iso")
    shown = json.loads(run("lpars", "show", lpar, "--system", system, "--json").stdout)
    assert shown["UUID"] == lpar
    preview = json.loads(
        run(
            "lpars", "decommission", lpar, "--system", system, "--dry-run", "--json"
        ).stdout
    )
    assert preview["dry_run"] and not preview["resource_deleted"]
    evidence = preview["blast_radius"]["mapping_evidence"]
    disk = next(
        row["backing_device"] for row in evidence if row["backing_device"] == _DISK
    )
    assert {
        row["disposition"] for row in evidence if row["backing_device"] in (disk, iso)
    } == {"planned detach"}
    assert not _mutations(server)
    for operation, name, message in (
        ("delete-disk", disk, "mapped"),
        ("delete-media", iso, "mounted"),
    ):
        args = ("storage", operation, vios, "--vg", vg["uuid"], "--name", name)
        refused = run(*args, "--json", code=2, input_text="y\n")
        assert "requires --yes" in refused.stderr
        guarded = run(*args, "--yes", code=1)
        assert message in guarded.stderr
        assert not _mutations(server)
    completed = json.loads(
        run("lpars", "decommission", lpar, "--system", system, "--yes", "--json").stdout
    )
    assert completed["workflow_completed"] and completed["resource_deleted"]
    assert (
        _LPAR not in server.journey["lpars"] and _OTHER_LPAR in server.journey["lpars"]
    )
    assert (
        disk.encode() in server.journey["vg"] and iso.encode() in server.journey["vg"]
    )
    assert [item[0] for item in _mutations(server)] == ["POST", "POST", "DELETE"]
    for operation, name in (("delete-disk", disk), ("delete-media", iso)):
        result = run(
            "storage", operation, vios, "--vg", vg["name"], "--name", name, "--yes"
        )
        assert len(result.stdout) < 1200
        assert name in result.stdout and "GroupCapacity" not in result.stdout
    remaining = ET.fromstring(server.journey["vg"])
    assert [e.text for e in remaining.iter(f"{{{_UOM}}}DiskName")] == ["keep-disk"]
    assert [e.text for e in remaining.iter(f"{{{_UOM}}}MediaName")] == ["keep.iso"]
    assert b"keep-disk" in server.journey["vios"]
    assert len(_mutations(server)) == 5


@pytest.mark.parametrize("mock_hmc", [_CleanupHMC], indirect=True)
def test_cleanup_refuses_wrong_system_and_duplicate_name(cleanup_cli):
    run, server = cleanup_cli
    systems = json.loads(run("systems", "list", "--json").stdout)
    wrong_system = next(
        row["UUID"] for row in systems if row["Resource"]["SystemName"] == "frame-b"
    )
    lpars = json.loads(run("lpars", "list", "--system", "frame-a", "--json").stdout)
    target = next(
        row for row in lpars["entries"] if row["Resource"]["PartitionName"] == "cleanup"
    )
    for command, flags in (("show", ()), ("decommission", ("--yes",))):
        refused = run(
            "lpars", command, target["UUID"], "--system", wrong_system, *flags, code=1
        )
        assert target["UUID"] in refused.stderr and wrong_system in refused.stderr
        assert not _mutations(server)
    server.journey["lpars"][_OTHER_LPAR] = server.journey["lpars"][_OTHER_LPAR].replace(
        b">keep<", b">cleanup<"
    )
    duplicate = run(
        "lpars",
        "decommission",
        target["Resource"]["PartitionName"],
        "--system",
        "frame-a",
        "--yes",
        code=1,
    )
    assert "ambiguous" in duplicate.stderr.lower()
    assert _LPAR in duplicate.stderr and _OTHER_LPAR in duplicate.stderr
    assert not _mutations(server)


@pytest.mark.parametrize("mock_hmc", [_CleanupHMC], indirect=True)
def test_cleanup_unresolved_mapping_inspection_and_refusal(cleanup_cli):
    run, server = cleanup_cli
    unknown = _mapping("unproven-disk", "VirtualDisk", "DiskName", "vtscsi9", lpar=None)
    server.journey["vios"] = server.journey["vios"].replace(
        b"</VirtualSCSIMappings>", unknown.encode() + b"</VirtualSCSIMappings>"
    )
    preview = run(
        "lpars", "decommission", "cleanup", "--system", "frame-a", "--dry-run", "--json"
    )
    data = json.loads(preview.stdout)
    row = next(
        row
        for row in data["blast_radius"]["mapping_evidence"]
        if row["backing_device"] == "unproven-disk"
    )
    assert (
        row["disposition"] == "unproven"
        and "AssociatedLogicalPartition" in row["missing_identity"]
    )
    inspection = next(
        line.removeprefix("Inspect: ")
        for line in preview.stderr.splitlines()
        if line.startswith("Inspect: ")
    )
    argv = shlex.split(inspection)
    assert argv[0] == "hmcpctl" and "--lpar" not in argv
    assert row["vios_uuid"] in argv
    inspected = run(*argv[1:])
    assert "unproven-disk" in inspected.stdout
    assert "Uncertainty" in preview.stderr
    failed = json.loads(
        run(
            "lpars",
            "decommission",
            data["lpar_uuid"],
            "--system",
            "frame-a",
            "--yes",
            "--json",
            code=1,
        ).stdout
    )
    assert not failed["workflow_completed"] and not failed["resource_deleted"]
    assert {step["step"]: step["status"] for step in failed["steps"]} == {
        "power_off": "skipped",
        "detach_storage_mappings": "error",
        "detach_adapters": "skipped",
        "delete_lpar": "skipped",
    }
    assert not _mutations(server)
    assert _LPAR in server.journey["lpars"]
    assert b"unproven-disk" in server.journey["vios"]


@pytest.mark.parametrize("mock_hmc", [_CleanupHMC], indirect=True)
@pytest.mark.parametrize("failure", ["mapping", "delete"])
def test_cleanup_partial_failure_preserves_backing_and_prior_effects(
    cleanup_cli, failure
):
    run, server = cleanup_cli
    server.journey[f"fail_{failure}"] = True
    initial_vg = server.journey["vg"]
    preview = json.loads(
        run(
            "lpars",
            "decommission",
            "cleanup",
            "--system",
            "frame-a",
            "--dry-run",
            "--json",
        ).stdout
    )
    result = json.loads(
        run(
            "lpars",
            "decommission",
            preview["lpar_uuid"],
            "--system",
            "frame-a",
            "--yes",
            "--json",
            code=1,
        ).stdout
    )
    assert not result["resource_deleted"] and not result["workflow_completed"]
    steps = {step["step"]: step for step in result["steps"]}
    remaining = json.loads(run("storage", "list-mappings", _VIOS, "--json").stdout)
    dispositions = {
        row["backing_device"]: row["disposition"]
        for row in result["blast_radius"]["mapping_evidence"]
    }
    assert dispositions[_DISK] == "detached"
    if failure == "mapping":
        assert steps["detach_storage_mappings"]["status"] == "error"
        assert (
            steps["detach_adapters"]["status"]
            == steps["delete_lpar"]["status"]
            == "skipped"
        )
        assert [item[0] for item in _mutations(server)] == ["POST", "POST"]
        assert [row["backing_name"] for row in remaining] == [_ISO, "keep-disk"]
        assert dispositions[_ISO] == "failed"
    else:
        assert steps["detach_storage_mappings"]["status"] == "ok"
        assert steps["delete_lpar"]["status"] == "error"
        assert "injected delete failure" in steps["delete_lpar"]["result"]
        assert [item[0] for item in _mutations(server)] == ["POST", "POST", "DELETE"]
        assert [row["backing_name"] for row in remaining] == ["keep-disk"]
        assert dispositions[_ISO] == "detached"
    assert all(item[1] != _VG_PATH for item in _mutations(server))
    assert _LPAR in server.journey["lpars"] and server.journey["vg"] == initial_vg


@pytest.mark.parametrize("mock_hmc", [_CleanupHMC], indirect=True)
@pytest.mark.parametrize("width", [40, 80, 200])
def test_cleanup_redirected_identifiers_round_trip(cleanup_cli, width):
    run, server = cleanup_cli
    systems = run("systems", "list", width=width).stdout.splitlines()
    headings = systems[0].split("\t")
    system = dict(zip(headings, systems[1].split("\t"), strict=True))["UUID"]
    assert system == _SYSTEM
    vios_rows = run("vios", "list", "--system", system, width=width).stdout.splitlines()
    vios = dict(zip(vios_rows[0].split("\t"), vios_rows[1].split("\t"), strict=True))[
        "UUID"
    ]
    mappings = run(
        "storage", "list-mappings", vios, "--system", system, width=width
    ).stdout
    assert _DISK in mappings and _ISO in mappings and "…" not in mappings
    assert not _mutations(server)
