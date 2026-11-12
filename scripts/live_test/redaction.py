"""Bounded redaction of failed live-test diagnostics and echoed session values."""

from __future__ import annotations

import html
import json
import re
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import quote, unquote

_SESSION_FIELD_RE = re.compile(
    r"(?i)(?:RequestHeaders|Cookie|Set-Cookie|sessionCookie|X-API-Session)"
)
_SESSION_XML_TOKEN_RE = re.compile(
    r"(?s)<!\[CDATA\[.*?(?:\]\]>|\Z)|<!--.*?(?:-->|\Z)|<\?.*?(?:\?>|\Z)|"
    r"<(?P<closing>/)?(?P<tag>[\w.:-]+)(?=[\s/>])"
    r"(?:[^'\"><]|'[^']*'|\"[^\"]*\"|'[^'<]*\Z|\"[^\"<]*\Z)*(?:>|\Z)"
)
_SESSION_XML_ATTR_RE = re.compile(
    r"""([\w.:-]+)\s*=\s*(?:"([^"]*)(?:"|\Z)|'([^']*)(?:'|\Z))"""
)
_SESSION_HEADER_RE = re.compile(
    r"(?i)(?<![\w-])(?:Cookie|Set-Cookie|sessionCookie|X-API-Session)['\"]?\s*[:=][^\r\n]*"
    r"(?:\r?\n[ \t]+[^\r\n]*)*"
)
_SESSION_ENTITY_RE = re.compile(r"(?i)&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);")
_SESSION_HEADER_VALUE_RE = re.compile(
    r"(?i)(?<![\w-])(?P<name>Cookie|Set-Cookie|sessionCookie|X-API-Session)['\"]?\s*[:=][ \t]*"
    r"(?P<value>[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*)"
)
_SESSION_VALUE_RE = re.compile(
    r"(?i)(?=(?<![\w-])(?P<name>Cookie|Set-Cookie|sessionCookie|X-API-Session)['\"]?\s*[:=][ \t]*"
    r"""(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:&(?:\#\d+|\#x[0-9a-f]+|[a-z][a-z0-9]+);|[^\s,;{}])+))"""
)


_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?P<name>password|passwd|token|secret|api[_-]?key)"
    r"(?P<separator>\s*(?:=|:)\s*)(?P<quote>['\"]?)(?P<value>[^\s,;'\"&]+)"
    r"(?P=quote)"
)
_URL_USERINFO_RE = re.compile(r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s@]+@")
#: Labels admit `_` (#927). It is a word character, so without it `\b` cannot fire
#: inside an underscore label: the match began at the next dot-separated label and
#: left every label up to the underscore one readable, or missed a two-label name.
_HOSTNAME_RE = re.compile(
    r"(?i)\b(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z]{2,63}\b"
)
#: A single-dot `name.ext` with one of these extensions is a filename, not a
#: host (#914). None is an IANA TLD — `.py`, `.md`, `.sh` and `.zip` are, so
#: they stay out; a multi-label name is redacted whatever its last label.
_FILENAME_EXTENSIONS = frozenset(
    {
        "cfg",
        "conf",
        "csv",
        "ini",
        "iso",
        "json",
        "log",
        "toml",
        "txt",
        "xml",
        "yaml",
        "yml",
    }
)
_ABSOLUTE_PATH_RE = re.compile(r"(?<![:\w])/(?:[^\s/]+/)*[^\s,;:'\")]+")


def _redact_hostname(match: re.Match[str]) -> str:
    stem, _, extension = match.group().rpartition(".")
    if "." not in stem and extension.lower() in _FILENAME_EXTENSIONS:
        return match.group()
    return "<REDACTED-HOST>"


class _FailureRedactionLimit(Exception):
    """Stop collection before its bounded diagnostic work can be exceeded."""


def _remember_session(value: str, secrets: set[str], *, cookie: bool) -> None:
    def retain(value: str, *, encoded_quotes: bool) -> str:
        raw = value.strip()
        inner = raw
        quote_char = raw[:1]
        if len(raw) > 1 and quote_char in {'"', "'"} and raw[-1] == quote_char:
            inner = raw[1:-1]
        else:
            first = _SESSION_ENTITY_RE.match(raw) if encoded_quotes else None
            last = raw.rfind("&")
            quote_char = html.unescape(first.group()) if first else ""
            if (
                first
                and last >= first.end()
                and quote_char in {'"', "'"}
                and html.unescape(raw[last:]) == quote_char
            ):
                inner = raw[first.end() : last]
            else:
                quote_char = ""
        value = raw
        if quote_char:
            try:
                value = json.loads(quote_char + inner + quote_char)
            except json.JSONDecodeError:
                value = re.sub(r"\\(.)", r"\1", inner)
        if value:
            secrets.update((raw, inner, value))
            if len(secrets) > 256:
                raise _FailureRedactionLimit
        return value

    raw_value = retain(value, encoded_quotes=True)
    decoded_value = retain(html.unescape(raw_value), encoded_quotes=False)
    if not cookie:
        return
    # Decode named source entities once, without decoding its component values again.
    for source, encoded_quotes in {raw_value: True, decoded_value: False}.items():
        parsed = SimpleCookie()
        try:
            # Entity semicolons are value data; preserve literal percent bytes too.
            protected = _SESSION_ENTITY_RE.sub(
                lambda match: quote(match.group(), safe=""), source.replace("%", "%25")
            )
            parsed.load(protected)
        except CookieError:
            continue  # Raw named values still protect exact echoes of malformed cookies.
        for item in parsed.values():
            if item.value:
                retain(unquote(item.value), encoded_quotes=encoded_quotes)
                retain(unquote(item.coded_value), encoded_quotes=encoded_quotes)


def _raw_session_header_value(fragment: str, match: re.Match[str]) -> str:
    start, end = match.span("value")
    raw_start, raw_end = start, end
    removed = 0
    # Each ampersand-delimited chunk can undergo only one stdlib entity replacement.
    for part in re.finditer(r"&[^&]*", fragment):
        raw = part.group()
        decoded = html.unescape(raw)
        difference = len(raw) - len(decoded)
        if not difference:
            continue
        suffix = 0
        while suffix < len(decoded) and raw[-suffix - 1] == decoded[-suffix - 1]:
            suffix += 1
        decoded_start = part.start() - removed
        decoded_end = decoded_start + len(decoded) - suffix
        if start > decoded_end or (
            start == decoded_end and decoded_start < decoded_end
        ):
            raw_start += difference
        elif start > decoded_start:
            raw_start = part.start()
        if end >= decoded_end:
            raw_end += difference
        elif end > decoded_start:
            raw_end = part.end() - suffix
        removed += difference
    return fragment[raw_start:raw_end]


def _session_xml_text(match: re.Match[str]) -> str:
    token = match.group()
    if not token.startswith("<![CDATA["):
        return ""
    return token[9:-3] if token.endswith("]]>") else token[9:]


def _redact_session_xml(value: str, secrets: set[str]) -> str:
    parts: list[str] = []
    stack: list[tuple[str, int, str]] = []
    cursor = 0

    def remember_region(start: int, end: int, field: str) -> None:
        if (
            start < 0
            or field == "requestheaders"
            or not _SESSION_FIELD_RE.fullmatch(field)
        ):
            return
        content = _SESSION_XML_TOKEN_RE.sub(_session_xml_text, value[start:end])
        _remember_session(content, secrets, cookie=field in {"cookie", "set-cookie"})

    for match in _SESSION_XML_TOKEN_RE.finditer(value):
        tag = match["tag"]
        if tag is None:
            continue
        tag = tag.lower()
        field = tag.rsplit(":", 1)[-1]
        if field == "header":
            attrs = [
                (name.rsplit(":", 1)[-1].lower(), double or single)
                for name, double, single in _SESSION_XML_ATTR_RE.findall(match.group())
            ]
            fields = {
                html.unescape(value).lower()
                for name, value in attrs
                if name == "name" and _SESSION_FIELD_RE.fullmatch(html.unescape(value))
            }
            cookie = bool(fields & {"cookie", "set-cookie"})
            field = (
                "cookie"
                if cookie
                else next(
                    iter(sorted(fields - {"requestheaders"})),
                    "requestheaders" if fields else "",
                )
            )
            if match["closing"] and stack and stack[-1][0] == tag:
                field = stack[-1][2]
            if fields:
                for name, attribute in attrs:
                    if name == "value":
                        _remember_session(attribute, secrets, cookie=cookie)
        # Ordinary nested Header elements balance without splitting the outer value.
        sensitive = bool(_SESSION_FIELD_RE.fullmatch(field))
        if not sensitive and not (tag.rsplit(":", 1)[-1] == "header" and stack):
            continue
        if match["closing"]:
            if not stack or stack[-1][0] != tag:
                continue
            _, start, field = stack.pop()
            remember_region(start, match.start(), field)
        else:
            if not stack:
                parts.append(value[cursor : match.start()])
            if sensitive:
                for index in range(len(stack) - 1, -1, -1):
                    parent, start, parent_field = stack[index]
                    if _SESSION_FIELD_RE.fullmatch(parent_field):
                        remember_region(start, match.start(), parent_field)
                        stack[index] = (parent, -1, parent_field)
                        break
            if not match.group().endswith("/>"):
                if len(stack) >= 32:
                    raise _FailureRedactionLimit
                stack.append((tag, match.end(), field))
                continue
        if sensitive:
            for index in range(len(stack) - 1, -1, -1):
                parent, start, parent_field = stack[index]
                if _SESSION_FIELD_RE.fullmatch(parent_field):
                    stack[index] = (parent, match.end(), parent_field)
                    break
        if not stack:
            parts.append("<REDACTED-SECRET>")
            cursor = match.end()
    for _, start, field in stack:
        remember_region(start, len(value), field)
    parts.append("<REDACTED-SECRET>" if stack else value[cursor:])
    return "".join(parts)


def redact_failure_data(data: Any) -> Any:
    secrets: set[str] = set()
    remaining_chars = 65_536
    remaining_nodes = 4096
    remaining_source_work = 1_000_000
    prepared_chars = 0

    # None is outside a named source; False learns exact values, True also parses cookies.
    def prepare(value: Any, depth: int = 0, cookie: bool | None = None) -> Any:
        nonlocal remaining_chars, remaining_nodes, prepared_chars, remaining_source_work
        remaining_nodes -= 1
        if depth > 32 or remaining_nodes < 0:
            raise _FailureRedactionLimit
        if isinstance(value, dict):
            prepared = {}
            for key, child in value.items():
                remaining_chars -= len(str(key))
                if remaining_chars < 0:
                    raise _FailureRedactionLimit
                named = bool(_SESSION_FIELD_RE.fullmatch(str(key)))
                child_cookie = cookie
                if named and str(key).lower() != "requestheaders":
                    child_cookie = bool(cookie) or str(key).lower() in {
                        "cookie",
                        "set-cookie",
                    }
                safe_child = prepare(child, depth + 1, child_cookie)
                if named:
                    safe_child = "<REDACTED-SECRET>"
                    prepared_chars += len(safe_child)
                prepared[key] = safe_child
            return prepared
        if isinstance(value, list):
            return [prepare(child, depth + 1, cookie) for child in value]
        text = str(value)
        remaining_chars -= len(text)
        if remaining_chars < 0:
            raise _FailureRedactionLimit
        if cookie is not None and isinstance(value, str):
            _remember_session(value, secrets, cookie=cookie)
        fragments = [text]
        cursor = 0
        for token in _SESSION_XML_TOKEN_RE.finditer(text):
            fragments.append(text[cursor : token.start()])
            if token.group().startswith("<![CDATA["):
                fragments.append(_session_xml_text(token))
            elif token.group().startswith("<!--"):
                fragments.append(token.group()[4:].removesuffix("-->"))
            elif token.group().startswith("<?"):
                fragments.append(token.group()[2:].removesuffix("?>"))
            cursor = token.end()
        fragments.append(text[cursor:])
        for fragment in fragments:
            for source in {fragment, html.unescape(fragment)}:
                for matcher in (_SESSION_VALUE_RE, _SESSION_HEADER_VALUE_RE):
                    for match in matcher.finditer(source):
                        value = match["value"]
                        if source != fragment:
                            remaining_source_work -= len(fragment)
                            if remaining_source_work < 0:
                                raise _FailureRedactionLimit
                            value = _raw_session_header_value(fragment, match)
                        _remember_session(
                            value,
                            secrets,
                            cookie=match["name"].lower() in {"cookie", "set-cookie"},
                        )
        text = _redact_session_xml(text, secrets)
        text = _SESSION_HEADER_RE.sub("<REDACTED-SECRET>", text)
        prepared_chars += len(text)
        return text

    try:
        prepared = prepare(data)
        if sum(map(len, secrets)) > 65_536 or len(secrets) * prepared_chars > 1_000_000:
            raise _FailureRedactionLimit
    except _FailureRedactionLimit:
        # Discard the complete object: an uncollected value may have earlier echoes.
        return "<REDACTED-FAILURE-DATA>"
    pattern = re.compile(
        "|".join(re.escape(s) for s in sorted(secrets, key=len, reverse=True))
        or r"(?!)"
    )

    def finish(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: finish(child) for key, child in value.items()}
        if isinstance(value, list):
            return [finish(child) for child in value]
        value = pattern.sub("<REDACTED-SECRET>", value)
        value = _SECRET_VALUE_RE.sub(r"\g<name>\g<separator><REDACTED-SECRET>", value)
        value = _URL_USERINFO_RE.sub(r"\g<scheme><REDACTED-URL-USERINFO>@", value)
        value = _HOSTNAME_RE.sub(_redact_hostname, value)
        return _ABSOLUTE_PATH_RE.sub("<REDACTED-PATH>", value)

    return finish(prepared)


def redact_failure_text(value: str) -> str:
    return redact_failure_data(value)
