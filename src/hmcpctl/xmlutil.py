"""Parsing and escaping helpers for the HMC uom (Atom/XML) payloads.

The HMC REST API returns Atom feeds where each <entry> wraps a single uom
resource in its own namespace, e.g.::

    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>urn:uuid:...</id>
        <title>ManagedSystem:7042-CR8*212345A</title>
        <link rel="SELF" href="https://hmc:12443/rest/api/uom/ManagedSystem/<uuid>"/>
        <content type="application/vnd.ibm.powervm.uom+xml">
          <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/" ...>
            <SystemName>server1</SystemName>
            ...
          </ManagedSystem>
        </content>
      </entry>
    </feed>

We parse with defusedxml and flatten each entry into a plain dict whose keys
are the (namespace-stripped) element names. Lists are produced for repeated
element names.

Outbound documents are rendered from string templates in ``documents.py`` and
``jobs.py``. ``escape_xml`` and ``escapes_string_arguments`` below are the
package's only encoding boundary for those templates; see ADR 0042.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import Callable, Mapping
from typing import Any, ParamSpec, cast

# Element is used only as a type annotation; all XML parsing uses defusedxml.
from xml.etree.ElementTree import Element  # nosec B405

# saxutils.escape only serializes; it parses nothing, so the xml.sax parser
# advisory does not apply. All parsing in this module goes through defusedxml.
from xml.sax.saxutils import escape  # nosec B406

from defusedxml import ElementTree as DET

ATOM_NS = "http://www.w3.org/2005/Atom"

# web/mc namespace: Logon documents.
WEB_NS = "http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"

# HMC bookkeeping attributes carried on nearly every uom element; they are
# noise for consumers, so we drop them during flattening.
_IGNORED_ATTRS = {"kb", "kxe", "kbo", "kb-cur", "schemaVersion", "lsb"}


def localname(tag: str) -> str:
    """Strip an XML namespace from a tag: '{ns}Name' -> 'Name'."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


# Outbound escaping (ADR 0042)

# The two metacharacters saxutils.escape does not handle by default. Escaping
# all five means one escaped form is safe as character data and inside a
# single- or double-quoted attribute value alike, so a builder never has to
# pick an encoding per interpolation site.
_ATTRIBUTE_ENTITIES = {'"': "&quot;", "'": "&apos;"}

# The complement of the XML 1.0 Char production. Escaping cannot rescue these:
# a NUL or a lone surrogate in a value produces a document the HMC rejects, or
# raises UnicodeEncodeError from the transport layer with nothing naming the
# cause. They are refused at the boundary instead, which keeps the contract
# every builder parameter meets down to "escape or reject".
_ILLEGAL_XML_CHARACTERS = re.compile(
    "[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


class _EscapedXmlText(str):
    """A string already escaped for XML output.

    Carrying the fact in the type is what makes ``escape_xml`` idempotent, so
    a builder that delegates to another builder (``build_vios_document`` ->
    ``build_lpar_document``) cannot double-escape its caller's value.

    The marker survives assignment and nothing else: every string operation
    (``str()``, ``strip()``, concatenation, an f-string) yields a plain ``str``
    and loses it. A builder may therefore forward a caller's value to another
    builder only untouched. Deriving a new string from one and passing that on
    would escape it twice.
    """

    __slots__ = ()


def escape_xml(value: str) -> str:
    """Escape a caller-supplied value for interpolation into an XML template.

    The result is safe as element character data and as a quoted attribute
    value. Escaping an already-escaped value returns it unchanged.

    A value carrying a character XML 1.0 cannot represent — a control
    character other than tab, newline, or carriage return, or a lone
    surrogate — is rejected rather than escaped, because no encoding of it
    parses.

    The round trip through a parser is exact for every value this accepts
    except whitespace: a carriage return in element text reads back as a
    newline, and a tab or newline inside an attribute value reads back as a
    space. That is XML's own normalization, not this function's.
    """
    if isinstance(value, _EscapedXmlText):
        return value
    illegal = _ILLEGAL_XML_CHARACTERS.search(value)
    if illegal is not None:
        raise ValueError(
            f"Cannot build an XML document from a value containing "
            f"U+{ord(illegal.group()):04X} at offset {illegal.start()}: XML 1.0 "
            f"has no representation for it. Remove the character from the value."
        )
    return _EscapedXmlText(escape(value, _ATTRIBUTE_ENTITIES))


# Argument types that carry no string and are rendered by str()/format().
_OPAQUE_ARGUMENT_TYPES = (bool, int, float, type(None))


def _escape_argument(value: object) -> object:
    """Escape every string reachable from one builder argument.

    Lists, dicts, and dataclass instances are the composite shapes the builders
    accept — ``physical_volumes``, the job-parameter mapping, and
    ``LparResources`` — and each recurses, so
    nesting is covered rather than assumed away.

    Any other shape is refused. Passing it through would be the one silent
    failure this boundary exists to prevent: its strings would reach a template
    unescaped, and a shape nobody modelled here is a shape the escaping harness
    cannot synthesize either, so it would arrive with no test to catch it.
    """
    if isinstance(value, str):
        return escape_xml(value)
    if isinstance(value, _OPAQUE_ARGUMENT_TYPES):
        return value
    if isinstance(value, list):
        return [_escape_argument(item) for item in value]
    if isinstance(value, dict):
        return {
            _escape_argument(key): _escape_argument(item) for key, item in value.items()
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _escape_dataclass(value)
    raise TypeError(
        f"Cannot build an XML document from an argument of type "
        f"{type(value).__name__!r}: "
        f"the escaping boundary models str, bool, int, float, None, list, dict, "
        f"and dataclass values. Give _escape_argument a branch for this shape, "
        f"and the escaping harness a case for it, before a builder accepts it."
    )


def _escape_dataclass(value: Any) -> Any:
    """Rebuild a dataclass instance with every string field escaped."""
    replacements = {}
    for member_field in dataclasses.fields(value):
        member = getattr(value, member_field.name)
        escaped = _escape_argument(member)
        if escaped is not member:
            replacements[member_field.name] = escaped
    return dataclasses.replace(value, **replacements) if replacements else value


_P = ParamSpec("_P")


def escapes_string_arguments(func: Callable[_P, str]) -> Callable[_P, str]:
    """Escape every string a caller passes, before the builder interpolates it.

    Escaping at the boundary rather than at each interpolation site is what
    makes the document builders correct by construction: the function body
    never holds an unescaped caller value, so the number of times it
    interpolates one does not matter. Strings nested recursively in lists,
    dictionary keys and values, or dataclass fields are escaped too. Booleans,
    integers, floats, and None pass through unchanged; unsupported argument
    shapes raise TypeError before the builder runs.

    A closed-vocabulary argument is escaped before the builder validates it,
    so a rejection message quotes the escaped form of an illegal value.
    """

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> str:
        escaped_args = cast("_P.args", tuple(_escape_argument(value) for value in args))
        escaped_kwargs = cast(
            "_P.kwargs",
            {name: _escape_argument(value) for name, value in kwargs.items()},
        )
        return func(*escaped_args, **escaped_kwargs)

    # The marker the escaping harness reads to prove every builder is wrapped;
    # a type checker cannot model an attribute set on a function object.
    wrapper.__xml_escaped_arguments__ = True  # ty: ignore[unresolved-attribute]
    return wrapper


def element_to_dict(el: Element) -> dict[str, Any] | str:
    """Recursively convert an ElementTree element to plain Python data.

    - Leaf elements become their text (or their attribute dict if they only
      carry attributes, e.g. HMC's kb/kxe/kb-cur metadata attributes).
    - Repeated child element names are collected into a list.
    - Attributes are preserved under an "@attrs" key when an element also has
      children or meaningful text.

    Returns a dict for elements with children or attributes, otherwise the
    element's text as a string.
    """
    children = list(el)
    attrs = {
        localname(k): v
        for k, v in el.attrib.items()
        if localname(k) not in _IGNORED_ATTRS
    }
    text = (el.text or "").strip()

    if not children:
        if attrs and not text:
            return attrs
        if attrs:
            return {"@attrs": attrs, "text": text}
        return text

    result: dict[str, Any] = {}
    if attrs:
        result["@attrs"] = attrs

    for child in children:
        key = localname(child.tag)
        value = element_to_dict(child)
        if key in result:
            existing = result[key]
            if not isinstance(existing, list):
                result[key] = [existing]
            result[key].append(value)
        else:
            result[key] = value

    return result


def _is_rooted(href: str | None) -> bool:
    """Whether *href* is an absolute URL or an absolute path.

    A job read carries a malformed relative SELF link beside the real one —
    ``nulljobs/{JobID}``, "null" where a base URL belonged (live capture at
    2281afd2, issue #1160) — so a relative SELF link never displaces a rooted one,
    whichever order the HMC writes them in.
    """
    return isinstance(href, str) and (href.startswith("/") or "://" in href)


def _parse_entry(entry: Element) -> dict[str, Any]:
    """Flatten one Atom entry and its wrapped HMC resource."""
    result: dict[str, Any] = {
        "UUID": None,
        "title": None,
        "link": None,
        "ResourceType": None,
        "Resource": {},
    }
    for child in entry:
        name = localname(child.tag)
        if name == "id":
            result["UUID"] = (child.text or "").strip().removeprefix("urn:uuid:")
        elif name == "title":
            result["title"] = (child.text or "").strip()
        elif name == "link" and child.attrib.get("rel", "SELF").upper() == "SELF":
            href = child.attrib.get("href")
            if _is_rooted(href) or not _is_rooted(result["link"]):
                result["link"] = href
        elif name == "content":
            resource = next(iter(child), None)
            if resource is not None:
                result["ResourceType"] = localname(resource.tag)
                result["Resource"] = element_to_dict(resource)
    return result


def leaf_text(value: object) -> object:
    """Return the text of a leaf that ``element_to_dict`` wrapped with attributes.

    A leaf carrying a non-ignored attribute (``ksv`` on V10R3 ``Description``) parses
    as ``{"@attrs": ..., "text": ...}`` rather than a string. Any other value,
    including a mapping without string text, is returned unchanged so callers that
    fail closed on a non-string still see it.
    """
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return value


def mtms_parts(resource: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return (machine type, model, serial) from a managed system resource.

    V10R3 serves ``MachineTypeModelAndSerialNumber`` as a nested element. Returns
    ``None`` unless all three children are nonblank strings.
    """
    mtms = resource.get("MachineTypeModelAndSerialNumber")
    if not isinstance(mtms, Mapping):
        return None
    machine_type, model, serial = (
        leaf_text(mtms.get(name)) for name in ("MachineType", "Model", "SerialNumber")
    )
    if all(isinstance(p, str) and p.strip() for p in (machine_type, model, serial)):
        return cast("str", machine_type), cast("str", model), cast("str", serial)
    return None


def console_version(resource: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """Return (version, release, service pack) from a management console resource.

    V10R3 nests the level as ``VersionInfo/{Version,Release,ServicePackName}``, so
    HMC V10R3 M1060 reads ``(10, 3, 1060)``. Returns ``None`` unless all three are
    one to four digits.
    """
    info = resource.get("VersionInfo")
    if not isinstance(info, Mapping):
        return None
    parts = [
        leaf_text(info.get(name)) for name in ("Version", "Release", "ServicePackName")
    ]
    if not all(
        isinstance(part, str) and re.fullmatch(r"[0-9]{1,4}", part.strip())
        for part in parts
    ):
        return None
    version, release, service_pack = (int(cast("str", part)) for part in parts)
    return version, release, service_pack


def render_mtms(resource: Mapping[str, Any]) -> str | None:
    """Render ``type-model*serial``, or ``None`` when the MTMS is absent or partial."""
    parts = mtms_parts(resource)
    return None if parts is None else "{}-{}*{}".format(*parts)


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """Parse an HMC Atom feed into flattened resource dictionaries."""
    root = DET.fromstring(xml_text.encode("utf-8"))
    root_type = localname(root.tag)
    if root_type == "feed":
        return [
            _parse_entry(entry) for entry in root if localname(entry.tag) == "entry"
        ]
    if root_type == "entry":
        return [_parse_entry(root)]
    return [
        {
            "UUID": None,
            "title": None,
            "link": None,
            "ResourceType": root_type,
            "Resource": element_to_dict(root),
        }
    ]


def find_text(xml_text: str, *names: str) -> str | None:
    """Return the text of the first element whose local name is in `names`."""
    root = DET.fromstring(xml_text.encode("utf-8"))
    wanted = set(names)
    for el in root.iter():
        if localname(el.tag) in wanted:
            return (el.text or "").strip()
    return None


def find_all_text(xml_text: str, *names: str) -> list[str]:
    """Return the text of every element whose local name is in `names`.

    The document-order sibling of `find_text`, which stops at the first match.
    A matching element with no text contributes an empty string rather than
    being skipped, so the result's length is the number of matching elements;
    callers that want only populated values filter them out.
    """
    root = DET.fromstring(xml_text.encode("utf-8"))
    wanted = set(names)
    return [
        (el.text or "").strip() for el in root.iter() if localname(el.tag) in wanted
    ]
