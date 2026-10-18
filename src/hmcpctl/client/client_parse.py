"""Parse helpers that tag XML failures with the HMC call that returned them."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from ..errors import HMCError
from ..xmlutil import find_all_text, find_text, localname, parse_feed
from .pcm_payloads import metric_links, pcm_preferences_to_dict

_T = TypeVar("_T")


def _tag_parse_errors(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Wrap an XML parse callable so a ParseError becomes an HMCError.

    The wrapper takes the same arguments as *fn* with a *context* string
    inserted second; on a parse failure it raises ``HMCError(f"Failed to
    parse {context} response")`` so the error names the HMC call that
    returned the malformed XML.
    """

    @functools.wraps(fn)
    def wrapper(xml_text: str, context: str, *args: Any) -> _T:
        try:
            return fn(xml_text, *args)
        except (DET.ParseError, DefusedXmlException) as exc:
            raise HMCError(
                f"Failed to parse {context} response: {str(exc)[:500]}"
            ) from exc
        except RecursionError as exc:
            # element_to_dict recurses per nesting level of the parsed
            # document, so a deeply nested response exhausts the stack;
            # RecursionError carries no message, hence the fixed clause.
            raise HMCError(
                f"Failed to parse {context} response: document nesting is too deep"
            ) from exc

    return wrapper


def _parse_discovery_names(
    xml_text: str, name_tag: str, container_tag: str, member_tag: str
) -> tuple[list[str] | None, bool]:
    """Classify document-wide discovery names without losing empty/unknown facts."""
    root = DET.fromstring(xml_text.encode("utf-8"))
    names = []
    container_present = False
    item_present = False
    for element in root.iter():
        tag = localname(element.tag)
        if tag == container_tag:
            container_present = True
        if tag == member_tag or tag == name_tag:
            item_present = True
        if tag == name_tag:
            text = (element.text or "").strip()
            if text:
                names.append(text)
    return (names if names or not item_present else None), container_present


_discovery_names = _tag_parse_errors(_parse_discovery_names)

_parse_feed = _tag_parse_errors(parse_feed)
_find_text = _tag_parse_errors(find_text)
_find_all_text = _tag_parse_errors(find_all_text)
_metric_links = _tag_parse_errors(metric_links)
_pcm_preferences = _tag_parse_errors(pcm_preferences_to_dict)
