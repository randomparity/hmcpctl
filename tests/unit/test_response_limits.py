"""Response limits apply before materialization through the real HTTP boundary."""

import asyncio

import httpx
import pytest
import pytest_asyncio

from hmcpctl.client import core
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError, HMCTransportError

PATH = "/rest/api/uom/ManagedSystem"
UUID = "11111111-1111-4111-8111-111111111111"
pytestmark = pytest.mark.asyncio


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, read_error=None, close_error=None):
        self.chunks = chunks
        self.read_error = read_error
        self.close_error = close_error
        self.read_gate = None
        self.close_gate = None
        self.read_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.yielded = 0
        self.closed = False
        self.close_calls = 0

    async def __aiter__(self):
        self.read_started.set()
        if self.read_gate is not None:
            await self.read_gate.wait()
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        if self.read_error is not None:
            raise self.read_error

    async def aclose(self):
        self.close_calls += 1
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@pytest_asyncio.fixture
async def make_client():
    clients = []

    async def make(stream, *, headers=None, status=200, max_response_bytes=8):
        client = core.HMCClient(
            HMCConfig.from_mapping(
                {
                    "host": "hmc.test",
                    "user": "test",
                    "verify_ssl": True,
                    "max_response_bytes": max_response_bytes,
                    "password": "test",  # pragma: allowlist secret — MockTransport fixture only.
                }
            )
        )
        await client._http.aclose()

        def handle(request):
            stream.accept_encoding = request.headers["Accept-Encoding"]
            return httpx.Response(status, headers=headers, stream=stream)

        client._http = httpx.AsyncClient(
            base_url="https://hmc.test",
            transport=httpx.MockTransport(handle),
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client._http.aclose()


@pytest.mark.parametrize(
    "declared",
    ["9", "00009", "100000000", "9" * 5000],
    ids=["small", "zero-padded", "large", "huge-decimal"],
)
async def test_declared_overflow_does_not_read_body(make_client, declared):
    stream = ObservedStream([b"unread"])
    client = await make_client(stream, headers={"Content-Length": declared})
    with pytest.raises(HMCError, match="declared.*limit 8 bytes") as error:
        await client._get(PATH)
    assert declared.lstrip("0")[:64] in str(error.value)
    assert len(str(error.value)) < 200
    assert stream.yielded == 0
    assert stream.closed


@pytest.mark.parametrize("declared", [None, "invalid", "-9", "1, 9", "1", "8", ""])
async def test_streamed_overflow_stops_at_crossing_chunk(make_client, declared):
    stream = ObservedStream([b"12345678", b"9", b"never read"])
    headers = {} if declared is None else {"Content-Length": declared}
    client = await make_client(stream, headers=headers)
    with pytest.raises(HMCError, match="observed size 9 bytes.*limit 8 bytes"):
        await client._get(PATH)
    assert stream.yielded == 2
    assert stream.closed


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "0008"}])
async def test_exact_boundary_preserves_text_and_headers(make_client, headers):
    stream = ObservedStream([b"1234", b"5678"])
    client = await make_client(stream, headers={**headers, "X-Test": "retained"})
    text, returned_headers = await client.raw_get(PATH)
    assert text == "12345678"
    assert returned_headers["x-test"] == "retained"
    assert stream.closed
    assert stream.close_calls == 1


@pytest.mark.parametrize("status", [200, 204])
async def test_empty_response_is_closed(make_client, status):
    stream = ObservedStream()
    client = await make_client(stream, status=status)
    assert await client._get(PATH) == ""
    assert stream.closed


async def test_success_inside_callers_exception_handler_stays_successful(make_client):
    stream = ObservedStream([b"ok"])
    client = await make_client(stream)
    try:
        raise HMCError("earlier request failed")
    except HMCError:
        assert await client._get(PATH) == "ok"
    assert stream.closed


async def test_exact_boundary_json_decodes(make_client):
    stream = ObservedStream([b'{"a": 1}'])
    client = await make_client(stream)
    assert await client.fetch_json(PATH) == {"a": 1}
    assert stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
async def test_encoded_response_is_rejected_before_read(make_client, encoding):
    stream = ObservedStream([b"not decoded"])
    client = await make_client(stream, headers={"Content-Encoding": encoding})
    with pytest.raises(HMCError, match="encoding.*identity"):
        await client._request("GET", PATH, headers={"Accept-Encoding": "gzip"})
    assert stream.yielded == 0
    assert stream.closed


@pytest.mark.parametrize("encoding", ["", "identity", "IDENTITY"])
async def test_identity_encoding_is_accepted(make_client, encoding):
    stream = ObservedStream([b"ok"])
    client = await make_client(stream, headers={"Content-Encoding": encoding})
    assert await client._get(PATH) == "ok"
    assert stream.accept_encoding == "identity"


async def test_identity_negotiation_overrides_caller(make_client):
    stream = ObservedStream([b"ok"])
    client = await make_client(stream)
    assert (
        await client._request("GET", PATH, headers={"accept-encoding": "gzip"})
    ).text == "ok"
    assert stream.accept_encoding == "identity"


async def test_upload_reply_is_bounded(make_client):
    stream = ObservedStream([b"unread"])
    client = await make_client(stream, headers={"Content-Length": "9"})

    async def content():
        yield b"iso"

    with pytest.raises(HMCError, match="declared.*limit 8 bytes"):
        await client._web_file_upload(UUID, content(), 3)
    assert stream.yielded == 0
    assert stream.closed


@pytest.mark.parametrize(
    "body",
    ["x" * 9000, "🙂" * 2000, "<Message>" + "x" * 5000 + "</Message>"],
    ids=["plain", "utf8", "xml"],
)
async def test_error_diagnostics_are_independently_bounded(body):
    error = HMCError("failed", 500, body)
    assert error.status_code == 500
    assert len(error.body.encode("utf-8")) <= 4096
    assert len(str(error).encode("utf-8")) <= 4150


async def test_http_error_body_has_separate_cap(make_client):
    stream = ObservedStream([b"<E><Message>" + b"m" * 9000 + b"</Message></E>"])
    client = await make_client(stream, status=500, max_response_bytes=20000)
    with pytest.raises(HMCError) as error:
        await client._get(PATH)
    assert error.value.status_code == 500
    assert (
        error.value.body
        == f"<HttpErrorResponse><Message>{'m' * 500}</Message></HttpErrorResponse>"
    )
    assert stream.closed


@pytest.mark.parametrize("declared", [False, True])
async def test_clients_use_independent_configured_limits(make_client, declared):
    headers = {"Content-Length": "9"} if declared else {}
    small_stream = ObservedStream([b"123456789"])
    small = await make_client(small_stream, headers=headers, max_response_bytes=1)
    large_stream = ObservedStream([b"123456789"])
    large = await make_client(large_stream, headers=headers, max_response_bytes=9)
    with pytest.raises(HMCError, match="size 9 bytes.*limit 1 bytes"):
        await small._get(PATH)
    assert await large._get(PATH) == "123456789"
    assert small_stream.yielded == (0 if declared else 1)
    assert small_stream.closed and large_stream.closed


async def test_oversized_http_error_is_rejected(make_client):
    stream = ObservedStream([b"123456789", b"unread"])
    client = await make_client(stream, status=500)
    with pytest.raises(HMCError, match="observed size 9 bytes.*limit 8 bytes") as error:
        await client._get(PATH)
    assert error.value.status_code == 500
    assert error.value.body is None
    assert stream.yielded == 1
    assert stream.closed


@pytest.mark.parametrize(
    "failure", [httpx.ReadError("read failed"), httpx.ReadTimeout("timed out")]
)
async def test_read_failure_closes_response(make_client, failure):
    stream = ObservedStream([b"small"], read_error=failure)
    client = await make_client(stream)
    with pytest.raises(HMCTransportError) as error:
        await client._get(PATH)
    assert error.value.__cause__ is failure
    assert stream.closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_close_failure_preserves_primary_error(make_client, cancel):
    stream = ObservedStream([b"123456789"], close_error=httpx.ReadError("close failed"))
    if cancel:
        stream.read_gate = asyncio.Event()
    client = await make_client(stream)
    task = asyncio.create_task(client._get(PATH))
    await asyncio.wait_for(stream.read_started.wait(), 1)
    if cancel:
        task.cancel()
    expected = asyncio.CancelledError if cancel else HMCError
    with pytest.raises(expected) as error:
        await task
    assert "close failed" in " ".join(error.value.__notes__)
    assert task.cancelled() == cancel
    if not cancel:
        assert "limit 8 bytes" in str(error.value)


async def test_repeated_cancellation_waits_for_response_close(make_client):
    stream = ObservedStream()
    stream.read_gate = asyncio.Event()
    stream.close_gate = asyncio.Event()
    client = await make_client(stream)
    task = asyncio.create_task(client._get(PATH))
    try:
        await asyncio.wait_for(stream.read_started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(stream.close_started.wait(), 1)
        task.cancel()
        stream.close_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        assert stream.close_calls == 1
    finally:
        stream.read_gate.set()
        stream.close_gate.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_close_failure_without_primary_is_reported(make_client):
    failure = httpx.ReadError("close failed")
    stream = ObservedStream([b"ok"], close_error=failure)
    client = await make_client(stream)
    with pytest.raises(HMCTransportError) as error:
        await client._get(PATH)
    assert error.value.__cause__ is failure
    assert stream.close_calls == 1


async def test_cancellation_during_successful_close_is_preserved(make_client):
    stream = ObservedStream([b"ok"])
    stream.close_gate = asyncio.Event()
    client = await make_client(stream)
    task = asyncio.create_task(client._get(PATH))
    try:
        await asyncio.wait_for(stream.close_started.wait(), 1)
        task.cancel()
        stream.close_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
    finally:
        stream.close_gate.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.parametrize(
    "method,args",
    [
        ("_get", (PATH,)),
        ("_post", (PATH, "")),
        ("_put", (PATH, "")),
        ("_delete", (PATH,)),
        ("_web_get", (PATH,)),
        ("_web_post", (PATH, "")),
        ("_web_delete", (PATH,)),
        ("raw_get", (PATH,)),
        ("raw_post", (PATH, "")),
        ("submit_job", (PATH, "")),
        ("_templates_get", (PATH,)),
        ("_web_file_create", (UUID, "test.iso", 1)),
        ("_web_file_delete", (UUID,)),
        ("_post_pcm", (PATH, "")),
        ("fetch_json", (PATH,)),
        ("submit_platform_update", (UUID, {})),
        ("_logon_once", ("",)),
        ("logoff", ()),
    ],
)
async def test_consumers_share_declared_limit(make_client, method, args):
    stream = ObservedStream([b"unread"])
    client = await make_client(stream, headers={"Content-Length": "9"})
    client._session_token = "test-session"
    with pytest.raises(HMCError, match="declared.*limit 8 bytes"):
        await getattr(client, method)(*args)
    assert stream.yielded == 0
    assert stream.closed
