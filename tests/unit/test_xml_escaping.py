"""Metacharacter round-trip harness for every HMC XML request builder.

The builders are discovered by reflection over the request-building modules
rather than listed by hand, so a builder added later is covered without anyone
remembering to extend this file.

A builder parameter whose annotation this harness does not model raises
``UnsupportedAnnotation`` while the cases are built, which is a collection
error; one it models but cannot build a value for fails its own case. Neither
is a silent skip, and that is the property the whole file rests on: a guard
that quietly declines to cover a new builder is worse than no guard.

For each string-carrying parameter the harness asserts one of two outcomes:

* the builder rejects the value with ``ValueError`` (closed vocabularies keep
  their existing validation), or
* the document is well-formed, the value parses back exactly, and the document
  has the same element/attribute structure as one built from a benign value —
  which is what makes element injection impossible rather than merely unlikely.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pathlib
import pkgutil
import re
import sys
import types
import typing
from typing import Any, Literal, get_args, get_origin, get_type_hints

import pytest
from defusedxml import ElementTree as DET

from hmcpctl import documents, jobs
from hmcpctl.documents.access import build_hmc_user_document
from hmcpctl.documents.logon import build_logon_request_document
from hmcpctl.documents.lpar import build_lpar_document
from hmcpctl.documents.remote_access import build_remote_access_document
from hmcpctl.documents.storage import (
    build_vscsi_mapping_document,
    build_web_file_document,
)
from hmcpctl.jobs import requests as jobs_requests
from hmcpctl.operations.updates import models as update_jobs
from hmcpctl.xmlutil import escape_xml, escapes_string_arguments, localname

# A value an operator could plausibly type that carries all five XML
# metacharacters at once. No tab, newline, or carriage return: an XML parser
# normalizes those inside an attribute value, so they would not round-trip
# through the builders that emit Atom hrefs.
PAYLOAD = "R&D <a> \"b\" 'c'"
BENIGN = "benign"

# Each builder lives in its owning `hmcpctl.documents` submodule; the package
# re-exports nothing (#1495), so the submodules are enumerated rather than named.
DOCUMENT_MODULES = tuple(
    importlib.import_module(f"{documents.__name__}.{info.name}")
    for info in pkgutil.iter_modules(documents.__path__)
)
BUILDER_MODULES = (*DOCUMENT_MODULES, jobs, jobs_requests, update_jobs)

ADR_0042 = (
    pathlib.Path(__file__).resolve().parents[2]
    / "docs"
    / "adr"
    / "0042-outbound-xml-escaping-at-the-builder-boundary.md"
)


class UnsupportedAnnotation(TypeError):
    """A builder parameter this harness cannot synthesize a value for."""


# --------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------- #


def _is_builder(name: str, obj: object, module: types.ModuleType) -> bool:
    """A public function in *module* that renders an XML request document."""
    return (
        inspect.isfunction(obj)
        and not inspect.iscoroutinefunction(obj)
        and not name.startswith("_")
        and (name.startswith("build_") or name.endswith("_job"))
        and (
            getattr(obj, "__module__", None) == module.__name__
            or getattr(obj, "__module__", "").startswith(f"{module.__name__}.")
        )
        and get_type_hints(obj).get("return") is str
    )


def _builders() -> list[tuple[types.ModuleType, str, Any]]:
    found = []
    for module in BUILDER_MODULES:
        for name, obj in vars(module).items():
            if _is_builder(name, obj, module):
                found.append((module, name, obj))
    return found


# --------------------------------------------------------------------- #
# Annotation inspection
# --------------------------------------------------------------------- #


def _unwrap(annotation: Any) -> Any:
    """Strip Optional from an annotation.

    ``get_type_hints`` already resolves string annotations and strips
    ``Annotated`` and ``NotRequired``, so a union is all that is left.
    """
    if get_origin(annotation) in (typing.Union, types.UnionType):
        members = [a for a in get_args(annotation) if a is not type(None)]
        if len(members) == 1:
            return _unwrap(members[0])
    return annotation


def _is_typed_dict(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, dict)


def _member_hints(annotation: Any) -> dict[str, Any]:
    """Field-name -> annotation for a dataclass or TypedDict."""
    return get_type_hints(annotation)


_SCALAR_ANNOTATIONS = (str, bool, int, float, type(None))


def _carries_string(annotation: Any) -> bool:
    """Whether a caller can put a free-form string into this annotation.

    Refuses to answer for an annotation it does not model, rather than saying
    "no". A shape this cannot classify — fixed-length tuples, ``set[str]``,
    ``Any`` — is one whose strings the harness would never probe, so guessing
    here is how a vulnerable builder would pass CI. Raising makes it a
    collection error instead.
    """
    annotation = _unwrap(annotation)
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        member_results = [_carries_string(member) for member in get_args(annotation)]
        return any(member_results)
    if origin is Literal:
        return False
    if annotation in _SCALAR_ANNOTATIONS:
        return annotation is str
    if (
        origin is tuple
        and len(get_args(annotation)) == 2
        and get_args(annotation)[1] is Ellipsis
    ):
        return _carries_string(get_args(annotation)[0])
    if origin in (list, dict):
        return any(_carries_string(arg) for arg in get_args(annotation))
    if dataclasses.is_dataclass(annotation) or _is_typed_dict(annotation):
        return any(_carries_string(h) for h in _member_hints(annotation).values())
    raise UnsupportedAnnotation(annotation)


def _is_closed_vocabulary(annotation: Any) -> bool:
    return get_origin(_unwrap(annotation)) is Literal


def _sample(annotation: Any, *, tainted: bool) -> Any:
    """Build a call value for *annotation*, carrying PAYLOAD when tainted."""
    annotation = _unwrap(annotation)
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        return _sample(get_args(annotation)[0], tainted=tainted)
    if origin is Literal:
        return get_args(annotation)[0]
    if annotation is str:
        return PAYLOAD if tainted else BENIGN
    if annotation is bool:
        return True
    if annotation is int:
        return 4
    if annotation is float:
        return 1.0
    if (
        origin is tuple
        and len(get_args(annotation)) == 2
        and get_args(annotation)[1] is Ellipsis
    ):
        return (_sample(get_args(annotation)[0], tainted=tainted),)
    if origin is list:
        (item,) = get_args(annotation)
        return [_sample(item, tainted=tainted)]
    if origin is dict:
        key, value = get_args(annotation)
        return {_sample(key, tainted=tainted): _sample(value, tainted=tainted)}
    if dataclasses.is_dataclass(annotation) or _is_typed_dict(annotation):
        members = {
            name: _sample(hint, tainted=tainted)
            for name, hint in _member_hints(annotation).items()
            if _carries_string(hint) or _is_closed_vocabulary(hint)
        }
        return annotation(**members)
    raise UnsupportedAnnotation(annotation)


def _call_kwargs(
    builder: Any,
    tainted_parameter: str | None,
    tainted_annotation: Any | None = None,
) -> dict[str, Any]:
    """Benign values for every required parameter, plus one tainted parameter."""
    kwargs: dict[str, Any] = {}
    hints = get_type_hints(builder)
    for name, parameter in inspect.signature(builder).parameters.items():
        annotation = hints[name]
        if name == tainted_parameter:
            kwargs[name] = _sample(tainted_annotation or annotation, tainted=True)
        elif parameter.default is inspect.Parameter.empty:
            kwargs[name] = _sample(annotation, tainted=False)
    return kwargs


def _parameter_cases(predicate: Any) -> list[tuple[str, Any, str]]:
    """(test id, builder, parameter name) for every parameter *predicate* selects."""
    cases = []
    for module, name, builder in _builders():
        hints = get_type_hints(builder)
        module_name = module.__name__.rsplit(".", 1)[-1]
        for parameter in inspect.signature(builder).parameters:
            if predicate(hints[parameter]):
                cases.append((f"{module_name}.{name}.{parameter}", builder, parameter))
    return cases


def _string_parameter_cases() -> list[tuple[str, Any, str, Any]]:
    """Expand union parameters so every string-carrying member is exercised."""
    cases = []
    for module, name, builder in _builders():
        hints = get_type_hints(builder)
        module_name = module.__name__.rsplit(".", 1)[-1]
        for parameter in inspect.signature(builder).parameters:
            annotation = _unwrap(hints[parameter])
            members = (
                get_args(annotation)
                if get_origin(annotation) in (typing.Union, types.UnionType)
                else (annotation,)
            )
            member_results = [_carries_string(member) for member in members]
            for member, carries_string in zip(members, member_results, strict=True):
                if carries_string:
                    suffix = getattr(member, "__name__", "value")
                    case_id = f"{module_name}.{name}.{parameter}.{suffix}"
                    cases.append((case_id, builder, parameter, member))
    return cases


STRING_CASES = _string_parameter_cases()
VOCABULARY_CASES = _parameter_cases(_is_closed_vocabulary)


# --------------------------------------------------------------------- #
# Document inspection
# --------------------------------------------------------------------- #


def _structure(xml: str) -> list[tuple[str, tuple[str, ...]]]:
    """Element path and attribute names for every element, in document order."""
    signature: list[tuple[str, tuple[str, ...]]] = []

    def walk(element: Any, path: str) -> None:
        here = f"{path}/{localname(element.tag)}"
        signature.append((here, tuple(sorted(localname(k) for k in element.attrib))))
        for child in element:
            walk(child, here)

    walk(DET.fromstring(xml.encode("utf-8")), "")
    return signature


def _values(xml: str) -> list[str]:
    """Every element text and attribute value in a document, as parsed."""
    parsed = []
    for element in DET.fromstring(xml.encode("utf-8")).iter():
        if element.text:
            parsed.append(element.text)
        parsed.extend(element.attrib.values())
    return parsed


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #


def test_builders_are_discovered():
    """The harness is worthless if reflection quietly finds nothing."""
    names = {name for _, name, _ in _builders()}
    assert {
        "build_hmc_user_document",
        "build_remote_access_document",
        "build_vscsi_mapping_document",
        "build_job_request",
        "migrate_lpar_job",
        "update_hmc_job",
        # The three client.py request bodies, moved here so the harness above
        # covers them the same way it covers every other builder (#284).
        "build_logon_request_document",
        "build_web_file_document",
    } <= names
    assert len(STRING_CASES) >= 40


@pytest.mark.parametrize(
    ("builder", "parameter", "annotation"),
    [
        (builder, parameter, annotation)
        for _, builder, parameter, annotation in STRING_CASES
    ],
    ids=[case_id for case_id, _, _, _ in STRING_CASES],
)
def test_metacharacters_round_trip_without_changing_structure(
    builder, parameter, annotation
):
    try:
        tainted = builder(**_call_kwargs(builder, parameter, annotation))
    except ValueError as rejected:
        # A closed vocabulary refused the value outright: no document was
        # produced, so neither malformed output nor injection is reachable.
        # The payload carries no character XML 1.0 lacks, so this can only be
        # the builder's own validation, never the encoding boundary's refusal.
        assert "XML 1.0" not in str(rejected)
        return

    assert PAYLOAD in _values(tainted), (
        f"{builder.__name__}({parameter}=...) did not round-trip the value"
    )

    benign_kwargs = _call_kwargs(builder, None)
    benign_kwargs[parameter] = _sample(annotation, tainted=False)
    assert _structure(tainted) == _structure(builder(**benign_kwargs)), (
        f"{builder.__name__}({parameter}=...) changed the document structure"
    )


@pytest.mark.parametrize(
    ("builder", "parameter"),
    [(builder, parameter) for _, builder, parameter in VOCABULARY_CASES],
    ids=[case_id for case_id, _, _ in VOCABULARY_CASES],
)
def test_closed_vocabularies_reject_metacharacters(builder, parameter):
    kwargs = _call_kwargs(builder, None)
    kwargs[parameter] = PAYLOAD
    with pytest.raises(ValueError):
        builder(**kwargs)


def test_every_documents_builder_escapes_its_arguments():
    """documents.py has no single rendering choke point, so each builder wraps."""
    unprotected = [
        name
        for module, name, builder in _builders()
        if module in DOCUMENT_MODULES
        and not getattr(builder, "__xml_escaped_arguments__", False)
    ]
    assert unprotected == []


def _renders_through_job_request(func: Any, seen: frozenset[str]) -> bool:
    """Whether *func* reaches the canonical renderer, directly or via a helper."""
    module = sys.modules[func.__module__]
    for name in func.__code__.co_names:
        target = getattr(module, name, None)
        if target is jobs.build_job_request:
            return True
        if (
            inspect.isfunction(target)
            and name not in seen
            and _renders_through_job_request(target, seen | {name})
        ):
            return True
    return False


def test_job_request_is_the_jobs_module_choke_point():
    """One decorator covers jobs.py only while nothing renders around it."""
    assert getattr(jobs.build_job_request, "__xml_escaped_arguments__", False)

    bypassing = [
        name
        for module, name, builder in _builders()
        if module is jobs
        and builder is not jobs.build_job_request
        and not _renders_through_job_request(builder, frozenset({name}))
    ]
    assert bypassing == []


# --------------------------------------------------------------------- #
# The concrete reproductions recorded on issue #263
# --------------------------------------------------------------------- #


def test_ldap_search_filter_accepts_an_ordinary_conjunction():
    search_filter = "(&(objectClass=person)(uid=*))"
    xml = build_remote_access_document({"SearchFilter": search_filter})

    parsed = DET.fromstring(xml.encode("utf-8"))
    found = [el.text for el in parsed.iter() if localname(el.tag) == "SearchFilter"]
    assert found == [search_filter]


def test_hmc_user_description_cannot_add_a_second_user_id():
    xml = build_hmc_user_document(
        user_id="alice",
        description="</Description><UserID>root</UserID><Description>x",
    )

    parsed = DET.fromstring(xml.encode("utf-8"))
    user_ids = [el.text for el in parsed.iter() if localname(el.tag) == "UserID"]
    assert user_ids == ["alice"]


def test_vscsi_target_device_cannot_add_a_second_lpar_href():
    xml = build_vscsi_mapping_document(
        storage_kind="VirtualDisk",
        storage_name="disk1",
        lpar_link="https://h/LP/AUTH",
        target_device="x</TargetDevice><AssociatedLogicalPartition "
        'xmlns="http://www.w3.org/2005/Atom" rel="related" href="https://h/LP/EVIL"/>'
        "<TargetDevice>y",
    )

    parsed = DET.fromstring(xml.encode("utf-8"))
    hrefs = [
        el.attrib.get("href")
        for el in parsed.iter()
        if localname(el.tag) == "AssociatedLogicalPartition"
    ]
    assert hrefs == ["https://h/LP/AUTH"]


def test_job_parameter_value_cannot_add_a_target_managed_system():
    xml = jobs.migrate_lpar_job(
        target_system="AUTHORIZED",
        target_profile_name="p</ParameterValue></JobParameter>"
        "<JobParameter><ParameterName>TargetManagedSystemName</ParameterName>"
        "<ParameterValue>EVIL",
    )

    parsed = DET.fromstring(xml.encode("utf-8"))
    names = [el.text for el in parsed.iter() if localname(el.tag) == "ParameterName"]
    assert names.count("TargetManagedSystemName") == 1


def test_repository_password_with_an_ampersand_stays_well_formed():
    xml = update_jobs.update_hmc_job(
        {
            "MediaType": "SFTP",
            "ServerHostOrIP": "h",
            "Password": "a&b<c",  # pragma: allowlist secret
        }
    )

    values = [
        el.text
        for el in DET.fromstring(xml.encode("utf-8")).iter()
        if localname(el.tag) == "ParameterValue"
    ]
    assert "a&b<c" in values


# --------------------------------------------------------------------- #
# The concrete reproductions recorded on issue #284 (client.py's three
# request bodies)
# --------------------------------------------------------------------- #


def test_logon_password_with_an_ampersand_stays_well_formed():
    """The reported defect: an ordinary password made logon impossible."""
    value = "pa&ss"
    xml = build_logon_request_document(user="hscroot", password=value)

    parsed = DET.fromstring(xml.encode("utf-8"))
    found = [el.text for el in parsed.iter() if localname(el.tag) == "Password"]
    assert found == [value]


def test_logon_user_cannot_add_a_second_user_id():
    """Unescaped, this produced a *well-formed* body with two identities."""
    username = "a</UserID><UserID>root"
    xml = build_logon_request_document(user=username, password="p")

    parsed = DET.fromstring(xml.encode("utf-8"))
    user_ids = [el.text for el in parsed.iter() if localname(el.tag) == "UserID"]
    assert user_ids == [username]


def test_web_file_filename_cannot_add_a_sibling_element():
    filename = "a.iso</Filename><Filename>evil.iso"
    xml = build_web_file_document(
        filename=filename,
        size_bytes=1,
        vios_uuid="00000000-0000-0000-0000-000000000001",
    )

    parsed = DET.fromstring(xml.encode("utf-8"))
    names = [el.text for el in parsed.iter() if localname(el.tag) == "Filename"]
    assert names == [filename]


@pytest.mark.parametrize(
    ("value", "codepoint"),
    [("unleakable\x00", "U+0000"), ("unleakable\ud800", "U+D800")],
)
def test_logon_rejects_characters_xml_cannot_carry_without_quoting_them(
    value, codepoint
):
    """Reject at the boundary, and name the codepoint rather than the value."""
    with pytest.raises(ValueError, match=re.escape(codepoint)) as raised:
        build_logon_request_document(user="hscroot", password=value)

    assert value not in str(raised.value)
    assert "unleakable" not in str(raised.value)


@pytest.mark.parametrize(
    "value",
    [
        "lpar1",
        "hdisk5",
        "dc=example,dc=com",
        "https://hmc:12443/rest/api/uom/LogicalPartition/1",
        "7042-CR8*212345A",
        "",
    ],
)
def test_escaping_is_the_identity_for_ordinary_values(value):
    """Byte-for-byte-unchanged documents rest on this, not on spot checks."""
    assert escape_xml(value) == value


@pytest.mark.parametrize(
    ("value", "codepoint"),
    [
        ("a\x00b", "U+0000"),
        ("a\x0bb", "U+000B"),
        ("a\x1fb", "U+001F"),
        ("a\ud800b", "U+D800"),
        ("a\ufffeb", "U+FFFE"),
    ],
)
def test_characters_xml_cannot_carry_are_rejected(value, codepoint):
    """Escaping cannot rescue these, so the boundary refuses them by name."""
    with pytest.raises(ValueError, match=re.escape(codepoint)):
        build_hmc_user_document(user_id=value)


@pytest.mark.parametrize("value", ["a\tb", "a\nb", "a\rb", "caf\u00e9", "\U0001f600"])
def test_characters_xml_can_carry_are_accepted(value):
    xml = build_hmc_user_document(user_id=value)

    assert DET.fromstring(xml.encode("utf-8")) is not None


def test_dataclass_members_are_escaped_too():
    """Dataclass values passed through builders are escaped recursively."""

    @dataclasses.dataclass(frozen=True)
    class Fixture:
        label: str = ""
        count: int = 0

    @escapes_string_arguments
    def build(fixture: Fixture) -> str:
        return f"<x n={fixture.count}>{fixture.label}</x>"

    assert build(Fixture(label="a&b", count=1)) == "<x n=1>a&amp;b</x>"


def test_escaping_an_escaped_value_is_a_no_op():
    """build_vios_document delegates to build_lpar_document; both are wrapped."""
    once = escape_xml(PAYLOAD)

    assert escape_xml(once) is once
    assert "&amp;amp;" not in once


@pytest.mark.parametrize(
    "builder",
    [builder for _, _, builder in _builders()],
    ids=[
        f"{module.__name__.rsplit('.', 1)[-1]}.{name}"
        for module, name, _ in _builders()
    ],
)
def test_benign_input_produces_no_entity_at_all(builder):
    """The byte-for-byte claim, asserted for every builder rather than two."""
    try:
        xml = builder(**_call_kwargs(builder, None))
    except ValueError:
        return

    assert "&" not in xml


def test_valid_input_is_unchanged_by_escaping():
    """Escaping must be invisible to every caller that was already correct."""
    assert '<PartitionName kb="CUR" kxe="false">lpar1</PartitionName>' in (
        build_lpar_document(name="lpar1")
    )
    assert (
        '<ParameterValue kb="CUR" kxe="false">false</ParameterValue>'
        in jobs.power_on_lpar_job()
    )


def test_variadic_tuple_annotation_probes_strings_and_refuses_unknown_shapes():
    assert _carries_string(tuple[str, ...])
    assert _sample(tuple[str, ...], tainted=True) == (PAYLOAD,)
    assert not _carries_string(tuple[int, ...])
    assert _sample(tuple[int, ...], tainted=False) == (4,)
    for annotation in (tuple[str, int], tuple[str], set[str], Any):
        with pytest.raises(UnsupportedAnnotation):
            _carries_string(annotation)
        with pytest.raises(UnsupportedAnnotation):
            _sample(annotation, tainted=True)
