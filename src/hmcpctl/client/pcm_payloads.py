"""Performance and Capacity Monitoring (PCM) helpers.

PCM is separate from the uom tree and returns *JSON* metric payloads, reached
via an Atom feed of links. Two request shapes:

  preferences:  GET/POST /rest/api/pcm/{Category}/{uuid}/preferences   (XML)
  metrics:      GET     /rest/api/pcm/{Category}/{uuid}/{Kind}[...]    (Atom feed
                of links) -> follow each link to a JSON document.

Categories: ManagementConsole, ManagedSystem, LogicalPartition,
VirtualIOServer, SharedStoragePool, Cluster.
Metric kinds: RawMetrics/LongTermMonitor, RawMetrics/ShortTermMonitor,
ProcessedMetrics, AggregatedMetrics.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TypedDict, Unpack
from urllib.parse import urlsplit

from defusedxml import ElementTree as ET

from ..xmlutil import ATOM_NS

PCM_NS = "http://www.ibm.com/xmlns/systems/power/firmware/pcm/mc/2012_10/"
ATOM = f"{{{ATOM_NS}}}"  # braced tag prefix: {http://www.w3.org/2005/Atom}

PREFERENCE_FIELDS = (
    "LongTermMonitorEnabled",
    "ShortTermMonitorEnabled",
    "AggregationEnabled",
    "ComputeLTMEnabled",
    "EnergyMonitorEnabled",
)


class PCMPreferenceFlags(TypedDict, total=False):
    """The five optional fields accepted by the PCM preferences endpoint."""

    LongTermMonitorEnabled: bool
    ShortTermMonitorEnabled: bool
    AggregationEnabled: bool
    ComputeLTMEnabled: bool
    EnergyMonitorEnabled: bool


def reject_unsupported_preference_fields(names: Iterable[str]) -> None:
    """Refuse a flag name the preferences document does not carry, before any I/O."""
    unsupported = sorted(set(names) - set(PREFERENCE_FIELDS))
    if unsupported:
        raise ValueError(f"Unsupported PCM preference fields: {', '.join(unsupported)}")


_PREFERENCE_ELEMENT = re.compile(
    r"<(?:(\w+):)?ManagedSystemPcmPreference\b.*?</(?:\1:)?ManagedSystemPcmPreference>",
    re.DOTALL,
)


def pcm_preferences_update(
    document_xml: str, **flags: Unpack[PCMPreferenceFlags]
) -> str:
    """Return the preferences element from a read, with the given flags changed.

    V10R3 answers a hand-built document carrying only the changed flags with
    HTTP 500 "Unexpected error during unmarshalling" (#634). IBM's PCM REST
    walkthrough posts the whole ``ManagedSystemPcmPreference`` element the GET
    returns, so this keeps every element of *document_xml* (the GET body, an
    Atom feed) and rewrites only the named flags' text.

    Raises:
        ValueError: If a flag name is unsupported, the read carries no
            preferences element or not exactly one element for a named flag,
            or the element is not well-formed outside the read (a namespace
            declared on the enclosing feed).
        ET.ParseError: If *document_xml* is malformed.
    """
    reject_unsupported_preference_fields(flags.keys())
    ET.fromstring(document_xml)  # refuse a malformed read before editing its text
    found = _PREFERENCE_ELEMENT.search(document_xml)
    if found is None:
        raise ValueError("the PCM preferences read has no ManagedSystemPcmPreference")
    element = found.group(0)
    for name, value in flags.items():
        element, count = re.subn(
            rf"(<{name}\b[^>]*>)\s*(?:true|false)\s*(</{name}>)",
            rf"\g<1>{'true' if value else 'false'}\g<2>",
            element,
        )
        if count != 1:
            raise ValueError(
                f"the PCM preferences read carries {count} {name} elements, not one"
            )
    try:
        ET.fromstring(element)
    except ET.ParseError as exc:
        raise ValueError(
            f"the PCM preferences element is not well-formed on its own: {exc}"
        ) from exc
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n{element}\n'


def pcm_preferences_to_dict(xml: str) -> dict[str, Any]:
    """Parse a PCM preferences GET response into {field: bool/str}.

    Raises:
        ET.ParseError: If *xml* is malformed — callers must not mistake a
            parse failure for "no preferences" (an empty dict is returned
            only for well-formed XML without recognized preference fields).
    """
    root = ET.fromstring(xml)
    out: dict[str, Any] = {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in PREFERENCE_FIELDS or tag in ("EnergyMonitoringCapable",):
            text = (el.text or "").strip()
            out[tag] = text.lower() in ("true", "1") if text else text
    return out


def metric_links(feed_xml: str) -> list[dict[str, str]]:
    """Extract metric JSON links from a PCM Atom feed.

    Returns a list of {link, updated, title} — 'link' is the absolute or
    relative URL to the JSON metrics document. An empty list means a
    well-formed feed with no entries (e.g. no metrics in the requested
    range), never a parse failure.

    Raises:
        ValueError: If an entry has no selected first link with a nonblank href.
        ET.ParseError: If *feed_xml* is malformed — propagated so callers
            surface the failure instead of mistaking it for "no metrics".
    """
    root = ET.fromstring(feed_xml)
    links = []
    for entry in root.iter(f"{ATOM}entry"):
        link = entry.find(f"{ATOM}link")
        updated = entry.find(f"{ATOM}updated")
        title = entry.find(f"{ATOM}title")
        href = link.get("href") if link is not None else None
        if href is None or not href.strip():
            raise ValueError("PCM entry requires a first link with a nonblank href")
        links.append(
            {
                "link": href,
                "updated": (updated.text or "").strip() if updated is not None else "",
                "title": (title.text or "").strip() if title is not None else "",
            }
        )
    return links


def newest_metric_link(links: list[dict[str, str]]) -> dict[str, str] | None:
    """Return the newest JSON document link, or ``None`` when the feed has none.

    The PCM feed does not guarantee entries are ordered by age, so picking the
    last row (``links[-1]``) could select a stale document. Compare each
    entry's ISO-8601 ``updated`` stamp instead; stamps that fail to parse sort
    as the earliest UTC instant so a real (even old) timestamp always wins.

    A managed system's feed also lists each partition's metric feed, stamped
    newer than the documents (V10R3, #634); only ``.json`` documents qualify.
    """

    def _key(link: dict[str, str]) -> datetime:
        updated = link.get("updated", "")
        try:
            dt = datetime.fromisoformat(updated)
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    documents = [
        link for link in links if urlsplit(link.get("link", "")).path.endswith(".json")
    ]
    return max(documents, key=_key, default=None)
