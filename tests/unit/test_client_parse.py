"""Tests for client_parse's error-tagging wrappers.

The helpers under test exist specifically so a ParseError surfaces as an
HMCError naming the HMC call that returned the malformed XML (the same
behavior covered end-to-end in tests/unit/test_client.py for the feed and PCM
preferences paths); these unit tests pin the tagging contract directly.
"""

import sys

import pytest
from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from hmcpctl.client.client_parse import (
    _discovery_names,
    _find_text,
    _metric_links,
    _parse_feed,
)
from hmcpctl.errors import HMCError


def test_find_text_parse_error_tags_context():
    with pytest.raises(HMCError) as exc_info:
        _find_text("<feed><entry>", "test context")
    assert "Failed to parse test context response" in str(exc_info.value)
    assert "no element found" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, DET.ParseError)


def test_metric_links_parse_error_tags_context():
    with pytest.raises(HMCError) as exc_info:
        _metric_links("<ManagementConsolePcmPreference><unclosed>", "test context")
    assert "Failed to parse test context response" in str(exc_info.value)
    assert "no element found" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, DET.ParseError)


def test_find_text_entity_error_tags_context():
    body = (
        "<!DOCTYPE feed [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><feed>&xxe;</feed>"
    )

    with pytest.raises(HMCError) as exc_info:
        _find_text(body, "test context")

    assert "Failed to parse test context response" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, DefusedXmlException)


def test_discovery_names_parse_error_tags_context():
    with pytest.raises(HMCError) as exc_info:
        _discovery_names(
            "<feed><entry>",
            "test context",
            "Nickname",
            "QuickProperty_Collection",
            "QuickProperty",
        )
    assert "Failed to parse test context response" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, DET.ParseError)


def test_parse_feed_recursion_error_tags_context():
    """A deeply nested wrapped resource overflows element_to_dict's per-level

    recursion; RecursionError is not a ValueError or ParseError subclass, so
    it needs its own clause to reach HMCError instead of escaping the guard.
    The recursion limit is lowered only so a modest nesting depth reproduces
    the failure quickly and deterministically; the same guard also fires at
    the default limit against a deeper body (verified manually, not asserted
    here to keep the test fast).
    """
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        nested = "<a>" * 500 + "leaf" + "</a>" * 500
        body = f"<feed><entry><content><Resource>{nested}</Resource></content></entry></feed>"
        with pytest.raises(HMCError) as exc_info:
            _parse_feed(body, "test context")
    finally:
        sys.setrecursionlimit(old_limit)
    assert "Failed to parse test context response" in str(exc_info.value)
    assert "nesting is too deep" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, RecursionError)
