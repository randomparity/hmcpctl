"""HMCError keeps and renders only sanitized HMC diagnostics (ADR 0369)."""

from __future__ import annotations

import pytest
from conftest import live_fixture

from hmcpctl.errors import MAX_ERROR_BODY_BYTES, HMCError
from hmcpctl.operations.virtualization.adapters import classified_add_refusal

COOKIE = "tok-cookie-A1b2C3d4"
SESSION = "tok-session-E5f6G7h8"


def _echoed(name: str, *, message: str | None = None) -> str:
    """A captured error body whose RequestHeaders carry tokens echoed in Message."""
    body = live_fixture(name)["body"]
    body = body.replace(
        "&lt;REDACTED-COOKIE&gt;", f'JSESSIONID={COOKIE}; CCFW="{SESSION}x"'
    )
    body = body.replace("&lt;REDACTED-SESSION&gt;", SESSION)
    if message is not None:
        start = body.index(">", body.index("<Message")) + 1
        body = body[:start] + message + body[body.index("</Message>") :]
    return body


def _leaks(error: HMCError) -> list[str]:
    shown = f"{error}\n{error.body}"
    return [
        s for s in ("tok-", "RequestHeaders", "RequestBody", "JSESSIONID") if s in shown
    ]


def test_echoed_header_fixture_keeps_no_credential_and_keeps_the_code() -> None:
    body = _echoed(
        "rest-vios-optical-mapping-vtd-in-use",
        message=f"VTD {SESSION} {COOKIE} name is already used in another mapping",
    )

    error = HMCError("POST x failed", 500, body)

    assert _leaks(error) == []
    assert "name is already used in another mapping" in error.body
    assert str(error) == (
        "POST x failed (HTTP 500): VTD <redacted> <redacted> name is already used in "
        "another mapping"
    )


@pytest.mark.parametrize(
    "name",
    [
        "rest-client-network-add-state-refused",
        "rest-client-network-add-vsi-rmc-refused",
        "rest-lpar-create-refused",
        "rest-vios-optical-mapping-vtd-in-use",
    ],
)
def test_captured_codes_survive_a_short_sibling_cookie(name: str) -> None:
    raw = live_fixture(name)["body"]
    body = raw.replace("&lt;REDACTED-COOKIE&gt;", f"JSESSIONID={COOKIE}; lb=0; e=r")

    error = HMCError("x", live_fixture(name)["status"], body)

    for code in ("REST0269", "HSCLB021", "REST0001", "name is already used"):
        assert (code in raw) == (code in error.body)
    assert _leaks(error) == []


def test_refusal_classifier_reads_the_sanitized_body() -> None:
    error = HMCError(
        "POST x failed", 500, _echoed("rest-client-network-add-state-refused")
    )

    assert classified_add_refusal(error, vsi_selected=False) is not None
    assert _leaks(error) == []


@pytest.mark.parametrize(
    "source",
    [
        f"<Cookie>{SESSION}</Cookie>",
        f'<Cookie value="{SESSION}"/>',
        f'<Header name="X-API-Session">{SESSION}</Header>',
        f'<H name="cookie" value="{SESSION}"/>',
        f"<Header><Name>X-API-Session</Name><Value>{SESSION}</Value></Header>",
        f'<RequestHeaders a="x-api-session={SESSION}"/>',
        f"<RequestHeaders>x-api-session={SESSION}</RequestHeaders><RequestBody>abc</RequestBody>",
        f'<Detail>{{"X-API-Session": "{SESSION}"}}</Detail>',
        f"<RequestHeaders>{{content-length=1, Cookie=a={SESSION}; b=c, host=h}}</RequestHeaders>",
        f"<RequestHeaders>{{x-api-session=[{SESSION}], accept=[x]}}</RequestHeaders>",
        f"<RequestHeaders>X-API-Session: {SESSION} Accept: */*</RequestHeaders>",
        f"<RequestHeaders>X-API-Session={SESSION}\tAccept=x</RequestHeaders>",
        f'<RequestHeaders Cookie="a={SESSION}" X-API-Session="x"/>',
        f"<RequestHeaders><Set-Cookie>{SESSION}</Set-Cookie></RequestHeaders>",
        f"<RequestHeaders><XAPISession>{SESSION}</XAPISession></RequestHeaders>",
        f"<RequestHeaders>{{cookie=[a=1, JSESSIONID={SESSION}], x=[y]}}</RequestHeaders>",
    ],
)
def test_each_identifiable_source_is_redacted_from_its_echo(source: str) -> None:
    body = f"<HttpErrorResponse><Message>REST0269 failed {SESSION}.</Message>{source}</HttpErrorResponse>"

    error = HMCError("x", 500, body)

    assert _leaks(error) == []
    assert error.body == (
        "<HttpErrorResponse><Message>REST0269 failed &lt;redacted&gt;.</Message></HttpErrorResponse>"
    )


def test_a_label_in_a_kept_field_cuts_the_field() -> None:
    body = "<E><Message>REST0001 bad header Cookie: s=abcdefghijk</Message></E>"

    error = HMCError("x", 400, body)

    assert str(error) == "x (HTTP 400): REST0001 bad header <redacted>"
    assert "abcdefghijk" not in error.body


@pytest.mark.parametrize(
    "body",
    [
        f"plain text Cookie: {SESSION}",
        f"<Error><Message>truncated {SESSION}",
        f"<!DOCTYPE E [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><E>&x;{SESSION}</E>",
        "<html><body><br></body></html>",
    ],
)
def test_unparseable_bodies_are_withheld(body: str) -> None:
    error = HMCError("GET x failed", 500, body)

    assert str(error) == (
        f"GET x failed (HTTP 500): unparseable {len(body)}-character response body withheld"
    )
    assert error.status_code == 500
    assert "tok-" not in error.body


def test_truncated_capture_is_withheld_keeping_label_and_status() -> None:
    body = _echoed("rest-vios-optical-mapping-vtd-in-use")[:500]

    error = HMCError("POST x failed", 500, body)

    assert str(error).startswith("POST x failed (HTTP 500): unparseable 500-character")
    assert _leaks(error) == []


def test_recognized_plaintext_marker_is_reported_by_literal() -> None:
    body = f"Nested path contains null property , currentProperty=SessionId {SESSION}"

    error = HMCError("GET x failed", 500, body)

    assert str(error).endswith(
        "withheld; recognized: Nested path contains null property"
    )
    assert "Nested path contains null property" in error.body
    assert "tok-" not in error.body


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("<E>" + "cookie=a; " * 65 + "</E>", "unsanitizable"),
        ("<E><Cookie>" + "a" * 4097 + "</Cookie></E>", "unsanitizable"),
        (
            f"<E><Cookie>{SESSION}</Cookie><Message>{f'{SESSION} ' * 257}</Message></E>",
            "unsanitizable",
        ),
        ("<E><Message>REST0269</Message></E>" + " " * 65537, "oversized"),
        (
            "<E>"
            + "".join(
                f"<Cookie>{'; '.join(f'{tag}{i}' for i in range(600))}</Cookie>"
                for tag in "ab"
            )
            + "</E>",
            "unsanitizable",
        ),
    ],
    ids=["assignments", "value-length", "echo-count", "body-length", "value-count"],
)
def test_out_of_bounds_credentials_withhold(body: str, reason: str) -> None:
    error = HMCError("x", 500, body)

    assert (
        str(error)
        == f"x (HTTP 500): {reason} {len(body)}-character response body withheld"
    )


def test_message_past_the_old_retention_cut_is_still_found() -> None:
    body = f"<Error><Padding>{'x' * 4200}</Padding><Message>schema violation</Message></Error>"

    error = HMCError("request failed", 500, body)

    assert str(error) == "request failed (HTTP 500): schema violation"
    assert (
        error.body
        == "<HttpErrorResponse><Message>schema violation</Message></HttpErrorResponse>"
    )


def test_retained_body_stays_under_the_byte_cap() -> None:
    body = f"<E><ReasonCode>{'&amp;' * 400}</ReasonCode><Message>{'&lt;' * 900}</Message></E>"

    assert len(HMCError("x", 500, body).body.encode()) <= MAX_ERROR_BODY_BYTES


def test_body_without_message_renders_a_fixed_detail() -> None:
    error = HMCError("x", 200, "<Fault><msg>guessed</msg></Fault>")

    assert str(error) == "x (HTTP 200): no Message in the response body"
    assert error.body == "<HttpErrorResponse></HttpErrorResponse>"


@pytest.mark.parametrize(
    "body",
    [
        _echoed("rest-vios-optical-mapping-vtd-in-use"),
        _echoed("rest-pcm-metrics-403"),
        "not xml",
        "Nested path contains null property",
        "<E><Message>" + "a" * 499 + " b</Message></E>",
        "<E><ReasonCode>" + "r" * 99 + " s</ReasonCode></E>",
    ],
)
def test_retained_body_is_a_fixed_point(body: str) -> None:
    error = HMCError("x", 500, body)

    rewrapped = HMCError("translated", 500, error.body)

    assert rewrapped.body == error.body
    assert str(rewrapped).split(": ", 1)[1] == str(error).split(": ", 1)[1]


def test_empty_and_missing_bodies_render_nothing() -> None:
    assert (str(HMCError("x", 500, None)), HMCError("x", 500, None).body) == (
        "x (HTTP 500)",
        None,
    )
    assert (str(HMCError("x", 500, "")), HMCError("x", 500, "").body) == (
        "x (HTTP 500)",
        "",
    )


def test_hmc_error_does_not_mask_unexpected_parser_failure(monkeypatch) -> None:
    def fail_unexpectedly(*_args: object) -> None:
        raise RuntimeError("parser defect")

    monkeypatch.setattr("hmcpctl.errors.DET.fromstring", fail_unexpectedly)

    with pytest.raises(RuntimeError, match="parser defect"):
        HMCError("request failed", 500, "<Error />")


def test_hmc_error_renders_the_captured_http_error_response_message() -> None:
    """V10R3 error bodies are an `HttpErrorResponse` whose text is `<Message>`."""
    capture = live_fixture("rest-lpar-not-found")

    error = HMCError("request failed", capture["status"], capture["body"])

    assert str(error) == (
        "request failed (HTTP 404): REST029B The URL presented to the Management "
        "Console REST Web Services is not valid. The supplied URI does not identify "
        "a known resource."
    )


def test_an_echo_split_by_markup_inside_message_is_redacted_whole() -> None:
    half = len(SESSION) // 2
    body = (
        f"<E><Message>bad {SESSION[:half]}<i/>{SESSION[half:]} REST0001</Message>"
        f"<RequestHeaders>{{x-api-session={SESSION}}}</RequestHeaders></E>"
    )

    error = HMCError("op", 400, body)

    assert str(error) == "op (HTTP 400): bad <redacted> REST0001"


def test_benign_digits_beside_a_short_sibling_cookie_do_not_withhold() -> None:
    raw = live_fixture("rest-client-network-add-vsi-rmc-refused")["body"]
    body = raw.replace("&lt;REDACTED-COOKIE&gt;", f"JSESSIONID={COOKIE}; lb=0")
    end = body.index("</Message>")
    body = body[:end] + " 00000000-0000-0000-0000-000000000000" * 20 + body[end:]

    error = HMCError("x", 500, body)

    assert "HSCLB021" in error.body


def test_echoes_shrunk_by_redaction_cannot_pull_a_cut_echo_into_the_field() -> None:
    token = "T" + "k" * 199
    message = (token * 41)[:8192] + token
    body = f"<E><Message>{message}</Message><R>{{x-api-session={token}}}</R></E>"

    error = HMCError("op", 500, body)

    assert "kkkkk" not in str(error)
    assert "kkkkk" not in error.body


@pytest.mark.parametrize(
    "source",
    [
        "Cookie: [JSESSIONID=ab]{token}X7, lb=0]",
        "{{cookie=[JSESSIONID=ab]{token}X7], x=[y]}}",
        "Cookie: [] JSESSIONID={token}X7",
    ],
    ids=["bracket-in-value", "bracket-in-map-list", "empty-leading-list"],
)
def test_a_bracketed_value_keeps_the_text_after_its_first_bracket(source: str) -> None:
    token = "Zq9SynthTok"
    body = f"<E><Message>see {token}X7 here</Message><R>{source.format(token=token)}</R></E>"

    error = HMCError("op", 500, body)

    assert str(error) == "op (HTTP 500): see <redacted> here"
    assert token not in error.body
