"""Tokenize private HMC captures and derive the committed vocabularies (issue #1202).

Three subcommands, all offline:

    python scripts/live_capture_export.py tokenize RAW.jsonl... --out corpus.json \\
        [--private REGEX]...
    python scripts/live_capture_export.py vocabulary corpus.json --enums enums.json \\
        --firmware v10r3-p9 --source TEXT [--source TEXT]... --out vocabulary.json
    python scripts/live_capture_export.py enums corpus.json --firmware v10r3 --out enums.json

`tokenize` reads the records `scripts/live_capture_sweep.py` writes (the capture
harness's `rest`/`ssh` records and the sweep's `tool`/`skip` records) and writes a
tokenized corpus. It is fail-closed: when a collected name, a URL host, an IP address,
a location code, a session value, an SSH key or a `--private` match survives, it exits 1
and writes nothing. The corpus stays private; only `vocabulary` and `enums` output is
committed, and both run the same scan over what they write.

The rules are in docs/workflow/specs/2026-09-30-live-capture-pipeline-design.md.
"""

from __future__ import annotations

import argparse
import collections
import csv
import ipaddress
import json
import os
import re
import shlex
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

UUID = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)
#: A word for whole-word name replacement: dots and hyphens join, edges are word chars.
WORD = re.compile(r"\w[\w.-]*\w|\w")
#: Namespace and schema hosts that are public and structural, never a lab host.
PRESERVED_HOSTS = ("www.ibm.com", "www.w3.org")
PLACEHOLDER_IP = "192.0.2.1"
HMC_HOST = "hmc.test"

#: XML elements whose text is a name, and the token kind each becomes.
NAME_ELEMENTS = {
    "SystemName": "sys",
    "PartitionName": "lpar",
    "UserName": "user",
    "UserID": "user",
    "BMCConnectionUserName": "user",
    "UserDescription": "desc",
    "ManagementConsoleName": "hmc",
    "ProfileName": "prof",
    "PartitionProfileName": "prof",
    "LastActivatedProfile": "prof",
    "HostName": "host",
    "VolumeGroupName": "vg",
    "MediaName": "media",
    "DeviceName": "dev",
    "TargetName": "dev",
    "BackingDeviceName": "dev",
    "DiskName": "dev",
    "VolumeName": "dev",
    "AdapterName": "dev",
    "ClusterName": "cluster",
    "StoragePoolName": "pool",
    "NetworkName": "net",
    "SwitchName": "vswitch",
    "PartitionTemplateName": "tmpl",
    "templateName": "tmpl",
    "LogicalUnitName": "lu",
    "Description": "desc",
}
#: CLI output fields whose value is a name, and the token kind each becomes.
NAME_FIELDS = {
    "name": "lpar",
    "lpar_name": "lpar",
    "curr_lpar_names": "lpar",
    "lpar_names": "lpar",
    "vios_name": "lpar",
    "profile_name": "prof",
    "curr_profile": "prof",
    "default_profile": "prof",
    "label": "label",
    "group_name": "label",
    "resource_group_name": "label",
    "description": "desc",
    "pool_name": "pool",
    "backup_name": "bk",
}
TOKEN_KINDS = sorted(set(NAME_ELEMENTS.values()) | set(NAME_FIELDS.values()))
#: Values that look like names but are words every HMC prints.
_NOT_NAMES = {"null", "none", "default", "default_profile", "true", "false"}
#: Accounts every HMC ships with. They identify no one, and `root` also appears in HMC
#: messages ("Unrecognized root REST type"), which replacing it would corrupt.
BUILT_IN_ACCOUNTS = {"root", "hscroot", "hscpe", "admin"}

Replacement = str | Callable[[re.Match[str]], str]

#: A 16-hex WWN, other than a LUN address: two digits and fourteen zeros. Its start is
#: supplied by each scrub rule or survivor guard.
_WWN_16 = r"(?![0-9a-fA-F]{2}0{14}\b)[0-9a-fA-F]{16}\b"

#: Elements whose whole text is a device identifier.
_DEVICE_ID_ELEMENTS = (
    "VolumeUniqueID|UniqueDeviceID|MediaUDID|DescriptorPage83|GroupSerialID|UDID|UniqueID"
    "|SerialNumberOfDisk|DeviceSerialNumber|WorldWidePortName|WWPN|PortWWPN"
    "|WorldWideNodeName|WWNN|WWPNPrefix"
)
_ADDRESS_ELEMENTS = "NetworkAddress|IPAddress|PrimaryIPAddress|IPv6Address"
#: The rest of an opening tag, up to a `>` that does not close it as `/>`. A self-closing
#: element has no value, and `[^<]+` would otherwise take the whitespace after it.
#: Quoted runs are skipped whole, so a `/>` inside an attribute value cannot end the tag;
#: a run is any `"`...`"`, which also spans the `\"`-escaped quotes of a JSON-encoded body.
#: A malformed tag (an unbalanced quote, a `<` in a value) falls back to the first `>`,
#: even after a `/`, so its value is still scrubbed rather than exported — unless the
#: quote-aware run does end in `/>`. Then the first `>` closes the tag only when no `/`
#: precedes it; one inside a quoted value (`kb=">"/>`) breaks the body, and the export
#: stops. The quote-aware branch must come first. The possessive `[^>]*+` stops the
#: fallback backtracking; each unclosed opening still scans to the next `>`.
_ATTRS = r"""(?:[^>"'<]|"[^"<]*"|'[^'<]*')*"""
_TAG_TAIL = rf"(?:{_ATTRS}(?<!/)>|(?!{_ATTRS}/>)[^>]*+>|[^>]*+(?<!/)>)"
#: What ends a namespace prefix inside a tag: XML's own spaces, the tag and attribute
#: delimiters, and the `:` after each prefix. Any other character is taken, so a
#: malformed prefix is scrubbed rather than exported (#1623): an allowlist of XML name
#: characters let `<a!b:SerialNumber>` or a non-ASCII space hide the element from the
#: scrub rule and the backstop alike.
_PREFIX_ENDS = "\t\n\r \"'/:<=>"
_PREFIX_END_CLASS = "".join(f"\\x{ord(c):02x}" for c in _PREFIX_ENDS)
#: A backslash run, as `json.dumps` writes one at any depth, is taken unless a `\u`
#: escape of a `_PREFIX_ENDS` character follows it: `\u0020` or `\u003e` ends a prefix
#: as a space or `>` would. Every other character after the run is read as itself, so
#: `\"` ends one, and `\n`, `\r` and `\t` do not: in a raw body a backslash is just a
#: character. So the literal text `\u0020` in a raw body ends a prefix too, as it does in
#: `json.dumps` text, where the two cannot be told apart.
_NAME_ESCAPE = r"\\++(?!u(?i:{}))".format(
    "|".join(f"{ord(c):04x}" for c in _PREFIX_ENDS)
)
#: Any number of namespace prefixes, `a:b:` and an empty `:` too. A prefix is given
#: back when the element name does not follow it: `<SerialNumber:x>` is still a
#: `SerialNumber`. A scrub rule takes the prefix and name as one atomic group: the tag
#: tail after the name ends at the same `>` whichever prefix matched, and retrying each
#: one would scan the tail again per segment of `<SerialNumber:SerialNumber:...`. The
#: two branches of a segment start with different characters, and each segment ends at
#: the first `:`, so a prefix matches one way only. A prefix never starts with `!` or
#: `?`: a comment, CDATA section or processing instruction opens no element.
_NS_PREFIX = rf"(?![!?])(?:(?:[^{_PREFIX_END_CLASS}\\]|{_NAME_ESCAPE})*+:)*"
#: The namespace prefixes of a closing tag. It holds no attributes, so anything but a tag
#: delimiter is taken, XML's spaces too: a prefix with a space, a quote, `=`, a `\u0020`
#: or a leading `!` or `?` still ends a value the backstops read (#1660). A comment, CDATA
#: section or processing instruction never starts with `</`; text that is not XML but
#: holds `</`, free text and `x:SerialNumber` is refused, never exported. Each segment
#: ends at its first literal or encoded colon (#1679). Encoded colons are accepted only
#: here, so a malformed opening tag still reaches refusal through the leak backstop.
#: Prefix characters cannot consume a separator, and backslash runs are possessive:
#: JSON escaping at any depth costs one scan. Before the bare name, XML whitespace,
#: `!` and `?` need no `:` (#1678); JSON whitespace escapes count too.
_ENCODED_COLON = r"(?:\\++u003[aA]|&#(?:58|x3[aA]);)"
_CLOSING_PREFIX = (
    rf"(?:(?:[^:<>&\\]|&(?!#(?:58|x3[aA]);)|\\++(?!u003[aA]))*+"
    rf"(?::|{_ENCODED_COLON}))*"
    r"(?:[ \t\n\r!?]|\\++[nrt])*+"
)
#: Elements whose whole text a rule replaces: the element name pattern, the text the
#: rule writes and, optionally, the keywords it keeps as evidence. The same entry builds
#: the scrub rule and its `leaks()` backstop. Names match in any case, after any
#: namespace prefix: neither is significant to whoever reads the value, so neither may
#: hide it. A keyword is kept only as the element's whole text, in its documented case.
_SESSION_ELEMENT = ("(?i:X-API-Session)", "<REDACTED-SESSION>")
_SSH_KEY_ELEMENT = (
    "(?i:PublicSSHKeyValue|AuthorizedKeysValue)",
    "<REDACTED-SSHKEY>",
)
_ADDRESS_ELEMENT = (f"(?i:{_ADDRESS_ELEMENTS})", PLACEHOLDER_IP)
_DEVICE_ID_ELEMENT = (f"(?i:{_DEVICE_ID_ELEMENTS})", "<REDACTED-DEVID>")
#: A virtual serial number is assigned to a partition, which otherwise holds `none`, or
#: `auto` while the HMC assigns one (#1593). Neither keyword is any serial, so the other
#: serial elements keep them too.
_SERIAL_ELEMENT = (
    "(?i:(?:Logical|Virtual)?SerialNumber)",
    "<REDACTED-SERIAL>",
    "none|auto",
)

#: `virtual_serial_num=` holds the same serial, or the same keywords, in `lssyscfg -r
#: lpar` output (#1618), as do `logical_serial_num=` there and `cluster_serial_num=` and
#: `peer_collective_serial_num=` in `lssyscfg -r sys` output (#1637). `\bserial_num` has
#: no boundary inside any of them to match at, so each is named. A keyword is kept only
#: as the field's whole value, in its documented case. A value ends at a field separator,
#: a line feed, a carriage return before one, a closing tag or the end of the text,
#: `json.dumps`'s `\n` and `\r` escapes on the same terms; any other carriage return is
#: in the value (#1641). It also ends at a quote, raw or escaped, that one of those
#: follows; a raw quote also before a JSON `}` or `]`, and `\"` before the quote that
#: closes a JSON string. A quote with anything else after it is in the value, and the
#: unquoted rule and the backstop read it so even where the quoted rule kept the keyword
#: before it. The unquoted rule reads no escape, so in `json.dumps` text a value runs on
#: over a `\n`: over-scrubbing, never a leak. The quoted rule keeps the backslashes
#: before its closing quote, so `json.dumps` text stays valid JSON (#1641).
_SERIAL_NAMES = (
    "SerialNumber",
    "LogicalSerialNumber",
    "VirtualSerialNumber",
    "serial",
    "serial_num",
    "virtual_serial_num",
    "logical_serial_num",
    "cluster_serial_num",
    "peer_collective_serial_num",
)
_KEYWORD_SERIAL_KEY = (
    "(?:"
    + "|".join(
        name.removesuffix("_serial_num")
        for name in _SERIAL_NAMES
        if name.endswith("_serial_num")
    )
    + ")_serial_num"
)
#: The other keys the key=value rules scrub, which keep no keyword.
_DEVICE_ID_KEY = "(?:unique_id|udid|wwpns?|wwnn|serial_num)"
_SERIAL_KEPT_RE = "(?-i:none|auto)"
_VALUE_END = r"(?:[,\n]|\r(?=\n)|</[\w.:-]++>|\\n|\\r(?=\\n)|$)"
_FIELD_END = rf'(?={_VALUE_END}|"(?:{_VALUE_END}|[}}\]])|\\"(?:{_VALUE_END}|"))'
#: The token the key=value rules write, in a body or an XML body.
_KEY_VALUE_TOKEN = "(?:<REDACTED-DEVID>|&lt;REDACTED-DEVID&gt;)"
#: The backslashes the quoted rule keeps before its closing quote, all but the last:
#: that one is read as the `\` of an escaped quote or escape, as `_FIELD_END` reads it
#: (#1641).
_KEPT_BACKSLASHES = r"(?:\\(?=\\))*+"
#: A quoted field's closing quote, then a space or tab between fields or XML attributes,
#: or the end of a tag: the quoted rule scrubbed up to that quote (#1641). The backstops
#: scan `json.dumps` text, where a quote inside a string is `\"` and a raw one opens or
#: closes the string, so both quotes must be escaped: a field at the start of a string
#: has no quote of its own. Any other text after the quote is read as part of the value,
#: and so is the quote after a kept backslash: that one was in the value.
_QUOTE_END = r'\\"(?: |\\t|/?>|\?>)'


def _key_value_leak(start: str, key: str, kept: str = "") -> re.Pattern[str]:
    """A *key* value the key=value rules missed, quoted too, after *start*.

    The token they write passes before a field end, and before a `_QUOTE_END` when an
    escaped quote opened the field; so does a *kept* keyword before a field end.
    """
    keyword = f"|{kept}{_FIELD_END}" if kept else ""
    field_end = rf"(?:{_KEPT_BACKSLASHES}{_FIELD_END}|{_QUOTE_END})"
    quoted = rf"(?!{_KEY_VALUE_TOKEN}{field_end}{keyword})"
    unquoted = rf"(?!{_KEY_VALUE_TOKEN}{_KEPT_BACKSLASHES}{_FIELD_END}{keyword})"
    return re.compile(
        rf'(?i){start}(?:(?<=\\"){key}={quoted}|(?<!\\"){key}={unquoted})"?[^,\n"]+'
    )


def _element_rule(
    element: str, value: str, keep: str | None = None
) -> tuple[re.Pattern[str], str]:
    """Replace the text of each *element* with *value*, unless it is a *keep* keyword.

    The tag tail is atomic: once it has ended the tag, a kept keyword after it cannot
    send the match back to end the tag at a `>` inside a quoted attribute instead.
    """
    kept = rf"(?!(?:{keep})<)" if keep else ""
    return (
        re.compile(rf"(<(?>{_NS_PREFIX}{element}\b)(?>{_TAG_TAIL})){kept}[^<]+"),
        rf"\g<1>{value}",
    )


def _element_leak(element: str, value: str, keep: str | None = None) -> re.Pattern[str]:
    """Any text that ends at a closing *element* tag and is not *value* as written.

    An XML body escapes a `<REDACTED-...>` token, so the text allowed is *value* with
    `<` and `>` escaped. Unescaped, the token is a tag, and the text between it and
    the closing tag is empty; an address placeholder is the same text either way.
    The closing tag is found, not the opening one, so a malformed opening tag the
    scrub rule could not end does not hide the value. Only what ends at the
    closing tag is checked: a CDATA section there is a leak whatever it holds, but
    text or CDATA followed by a child element, comment or processing instruction is
    not seen. A run starts only after a `<` or `>` and never backtracks, so the scan
    stays linear in the corpus.

    A *keep* keyword is matched as the `kept` group, which `leaks()` does not report,
    only right after a well-formed opening tag: its quoted runs closed and no `<` in
    it. Anywhere else, after a child element or a malformed tag the scrub rule ended
    at a later `>`, it is element text like any other. Matching an opening tag scans
    it to its end, as the scrub rule does.
    """
    written = re.escape(value.replace("<", "&lt;").replace(">", "&gt;"))
    # Nested JSON text retains its slash escape; later serialization doubles backslashes.
    closing = rf"(?=<\\*+/{_CLOSING_PREFIX}{element}\b)"
    kept = (
        rf"(?P<kept><(?>{_NS_PREFIX}{element}\b){_ATTRS}(?<!/)>(?:{keep}){closing})|"
        if keep
        else ""
    )
    return re.compile(rf"{kept}(?:(?<![^<>])(?!{written}<)[^<>]++|\]\]>){closing}")


#: The JSON escapes a value can follow: `\\`, `\n` or `\u00e9`. A lookbehind must be
#: fixed width, so each gets its own.
_JSON_ESCAPES = (r"\\\\", r"\\[bfnrt]", r"\\u[0-9A-Fa-f]{4}")
#: A run starts after a JSON escape, as after a separator, but never on the escape's
#: own letter or digits.
_AFTER_ESCAPE = "|".join(f"(?<={escape})" for escape in _JSON_ESCAPES)
#: Where an unquoted key=value key starts: after a non-word character or a JSON escape,
#: whose letter or digits `\b` reads as part of the key, as in `\nserial_num` (#1638);
#: never inside a longer key such as `virtual_serial_num`. A literal backslash before
#: `n` or `u0041` reads the same, and so does an escape of a word character such as
#: `\u00e9`: over-scrubbing, never a leak.
_KEY_START = rf"(?:(?<!\w)|{_AFTER_ESCAPE})"
#: Where a free-text MAC, WWN or IPv4 address the scrub rules replace starts: after a
#: character that is neither a word character nor a backslash, or a backslash run from its
#: first backslash, so pairs are escaped backslashes (#1669). The run is pairs alone,
#: pairs and a JSON escape, or pairs and a lone backslash before anything but an
#: escape letter, as in raw text. It is the `escape` group, which the rule writes back.
#: The letter of a `\f` or `\b` escape is never read as the value's first hex digit and
#: dropped; after an escaped backslash, an `n` is a letter the value is joined to. The
#: run is matched only from its first backslash, so it is scanned once.
_SCRUB_START = (
    r"(?:(?<![\w\\])|(?<!\\)(?P<escape>(?:\\\\)++"
    r"|(?:\\\\)*+\\(?:[bfnrt]|u[0-9A-Fa-f]{4}|(?![bfnrtu]))))"
)
#: Where a backstop reads a free-text MAC, WWN or IPv4 address as starting: as
#: `_SCRUB_START`, but after any `_AFTER_ESCAPE`, even an escaped backslash and an `n`, so a value the scrub
#: rules read as joined to that letter fails closed (#1669).
_LEAK_START = rf"(?:(?<![\w\\])|{_AFTER_ESCAPE})"
#: Free-text MACs in each separator style: a guard, the run, and the run zeroed whole as
#: the scrub rules write it. A longer hyphen or dotted run, such as a hyphenated WWPN, is
#: zeroed whole, not left with live groups after a MAC's worth. Each group ends at a word
#: boundary, so a word after the run, as in `...-5f-backup`, ends it rather than hiding
#: it. The guard refuses a start after a whole group and its separator, so a run is
#: matched from its first group only and scanned once; a group joined to a word, as in
#: `node-0a-1b-...` or `vlan_10-0a-...`, is not a whole one. A backstop checks the guard
#: before the zeroed run, or each group would scan the rest of the run. A longer colon
#: run is the IPv6 rule's.
_MAC_FORMS = (
    ("", r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", r"(?:00:){5}00\b"),
    (
        r"(?<!(?<!\w)[0-9A-Fa-f]{2}-)",
        r"[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}\b){5,}+",
        r"00(?:-00\b){5,}+(?!-[0-9A-Fa-f]{2}\b)",
    ),
    (
        r"(?<!(?<!\w)[0-9A-Fa-f]{4}\.)",
        r"[0-9A-Fa-f]{4}(?:\.[0-9A-Fa-f]{4}\b){2,}+",
        r"0000(?:\.0000\b){2,}+(?!\.[0-9A-Fa-f]{4}\b)",
    ),
)
#: A MAC separator and the hex digit after it.
_MAC_SEPARATOR = r"[-.:][0-9A-Fa-f]"
#: The digits after a `\\u` where a separated MAC's first octets stay: fewer than an
#: escape holds, or four before a separator that are not the `0030` or `0000` the MAC
#: element rule writes there. Each check reads at most six characters ahead. Four
#: digits before any other separator, or at the end of the value, are a leak only after
#: a literal `\\u`, which `_LITERAL_U` reports: elsewhere they read as an escape such as
#: `\\u00e9`. In a REST body, a non-ASCII character before a separator and a hex digit is
#: a leak, as the export writes it as such an escape.
_AFTER_LITERAL_U = (
    r"(?<=\\u)(?:(?=[0-9A-Fa-f]{1,3}(?![0-9A-Za-z]))0*+[1-9A-Fa-f]"
    rf"|(?!00[03]0)(?=[0-9A-Fa-f]{{4}}{_MAC_SEPARATOR}))"
)
#: A `u` after an even count of backslashes in JSON text, where each pair is one escaped
#: backslash, so the `u` is literal: the MAC element rule reads raw XML's `\\u0A1B` as an
#: escape and keeps its digits before a space, comma, slash or tab, or at the end of the
#: value (#1642). Digits with a non-zero one are a leak where no ASCII letter or digit
#: follows them, or where six or more are joined to a word, as `_LIVE_HEX_RUN` counts a
#: run. Only the bounded alternative exempts the `0030` or `0000` the rule writes;
#: the joined alternative skips that prefix without backtracking into it. A literal `\\f` or
#: `\\b` needs no such branch: `_AFTER_ESCAPE` starts a run after any `\\\\`. In a REST
#: body that is JSON text, the scan reads a real escape as a literal one, so that body
#: fails closed. The backslashes are matched from the first, so a run of them is scanned
#: once.
_LITERAL_U = (
    r"(?<!\\)(?:\\\\)++u"
    r"(?:(?=[0-9A-Fa-f]{6})(?:00[03]0)?+0*+[1-9A-Fa-f]"
    r"|(?!00[03]0)0*+[1-9A-Fa-f][0-9A-Fa-f]*+(?![0-9A-Za-z]))"
)
#: A whole hex run directly before a MAC separator and hex, or directly after hex and a
#: separator: an octet, even when a word character adjoins its other end, as in
#: `G0A:1B` or `:5FG` (#1640). A word character between the run and the separator, as
#: in `0AG:1B`, is not covered. Each starts only where a run does, so a run is scanned
#: once.
_JOINED_OCTET = (
    rf"(?<![0-9A-Fa-f])[0-9A-Fa-f]++(?={_MAC_SEPARATOR})"
    r"|(?<=[0-9A-Fa-f][-.:])[0-9A-Fa-f]++"
)
#: A `_JOINED_OCTET` with a non-zero digit. The first never starts on an escape's letter
#: or digits; a run right after an escape ends at the separator, so the word-bounded
#: branch of `_LIVE_HEX_RUN` already reports it.
_LIVE_JOINED_OCTET = (
    r"(?<![0-9A-Fa-f\\])(?<!\\u)0*+[1-9A-Fa-f][0-9A-Fa-f]*+"
    rf"(?={_MAC_SEPARATOR})|(?<=[0-9A-Fa-f][-.:])0*+[1-9A-Fa-f]"
)
#: A hex run with a non-zero digit: one no ASCII letter or digit adjoins, as the MAC
#: element rule zeroes a word, or six or more digits joined to a word, as in
#: `0x0A1B2C3D4E5F` or after a JSON escape, which `\b` reads as part of the value's
#: word. A shorter run inside a word, such as the `A` of `ALL`, is not one. Both err
#: towards failing closed: `TYPE_A` and a hex-letter word such as `FEEDBACK` count.
#: So do the first octets of a separated MAC after a `\\u` (#1622), a joined octet
#: (#1640), and the digits after a literal `\\u` (#1642).
_LIVE_HEX_RUN = (
    rf"(?:(?:(?<![0-9A-Za-z\\])|{_AFTER_ESCAPE})0*+[1-9A-Fa-f][0-9A-Fa-f]*+(?![0-9A-Za-z])"
    rf"|(?:(?<![0-9A-Fa-f\\])(?<!\\u)|{_AFTER_ESCAPE})(?=[0-9A-Fa-f]{{6}})0*+[1-9A-Fa-f]"
    rf"|{_LIVE_JOINED_OCTET}|{_AFTER_LITERAL_U}|{_LITERAL_U})"
)
#: Text that ends at a closing MAC element tag and still holds a live hex run, or a CDATA
#: section there, whitespace aside. Found as `_element_leak` finds a value, so a malformed
#: opening tag the scrub rule could not match does not hide it, and text followed by a
#: child element is not seen; zeroed runs and keywords such as `ALL` are what the rule
#: keeps. The closing tag is checked before the run is searched, and neither check
#: backtracks out of the text run, so the scan stays linear.
_MAC_LEAK = re.compile(
    rf"(?:(?<![^<>])(?=[^<>]*+<\\*+/{_CLOSING_PREFIX}(?i:\w*MACAddress))(?=[^<>]*?{_LIVE_HEX_RUN})"
    rf"[^<>]++|\]\]>(?=(?:\s|\\[nrt])*+<\\*+/{_CLOSING_PREFIX}(?i:\w*MACAddress)))"
)


#: Share the MAC scrubber's exact value spans with the raw-text backstop: an actual
#: Unicode escape before zeroed MAC digits can otherwise look like a live identifier.
_MAC_ELEMENT = re.compile(
    rf"(<(?>{_NS_PREFIX}(?i:\w*MACAddress\w*+)){_TAG_TAIL})([^<]+)"
)
_RAW_IDENTIFIER_VALUE = (
    rf"(?:{_WWN_16}|"
    + "|".join(rf"{guard}(?!{zeroed}){run}" for guard, run, zeroed in _MAC_FORMS[1:])
    + ")"
)
_RAW_IDENTIFIER_LEAK = re.compile(rf"(?<=\\u){_RAW_IDENTIFIER_VALUE}")
#: Only an even slash run is a literal backslash-u in JSON produced by the caller.
_JSON_RAW_IDENTIFIER_LEAK = re.compile(rf"(?<!\\)(?:\\\\)++u{_RAW_IDENTIFIER_VALUE}")


#: An IPv6 address, or a colon-separated HMC record `_ipv6` keeps, up to a word boundary.
_IPV6_CANDIDATE = r"(?:fe80|[0-9a-f]{1,4})(?::[0-9a-f]{0,4}){2,7}\b"
#: A JSON escape and the candidate directly after it, or a candidate at a word boundary.
#: The escape is matched first, so a candidate never starts on its letter: the `b` of
#: `\b0A:1B:...` is not rewritten into the invalid escape `\2001:db8::1` (#1643), and
#: an address after `\n`, which no word boundary precedes, is still read as one. The
#: escape may be written after an escaped backslash: `json.dumps` text of a value that
#: is itself JSON writes the value's `\f` as `\\f`. Backslashes pair off from the left,
#: so the letter after `\\\\` and the escape after `\\` in `\\\f` are matched too. After
#: `\b` or `\f` the candidate may open with a colon, as `_ipv6` also reads the letter.
_IPV6 = re.compile(
    r"(?i)(?P<escape>\\\\?(?:[bfnrt]|u[0-9a-f]{4})|\\\\)"
    rf"(?P<address>{_IPV6_CANDIDATE}|(?<=\\[bf])(?::[0-9a-f]{{0,4}}){{2,7}}\b)?"
    rf"|\b{_IPV6_CANDIDATE}"
)


def _is_ipv6(text: str) -> bool:
    """Whether *text* is an IPv6 address, not a colon-separated HMC record `1:0:1:3`.

    An address has a `::`, a group of three or more digits, a hex letter, or the
    link-local prefix; SR-IOV and slot records are short decimal fields only.
    """
    return (
        "::" in text
        or text.lower().startswith("fe80")
        or any(
            len(g) >= 3 or re.search("[a-f]", g, re.IGNORECASE) for g in text.split(":")
        )
    )


def _ipv6(match: re.Match[str]) -> str:
    """Redact an IPv6 address, keeping a JSON escape before it.

    The letter of `\\b` or `\\f` is a hex digit, and where the backslash is literal, as
    in raw XML or `\\\\b` in JSON text, it opens the address: so the candidate is an
    address when it is one with the letter too. Not when it holds only zeros, as a MAC
    the MAC rules zeroed does: the letter, kept as it is, is all that is left of it.
    """
    escape, text = match.group("escape", "address")
    if escape is None:
        escape, text = "", match.group(0)
    if text is None:
        return escape
    hex_letter = escape[-2:].lower() in (r"\b", r"\f") and text.strip("0:")
    if _is_ipv6(text) or (hex_letter and _is_ipv6(escape[-1] + text)):
        return escape + "2001:db8::1"
    return match.group(0)


#: Consume escapes before hex text, so their digits or letters never join an address.
#: Maximal possessive runs prevent retrying every suffix of oversized input.
# A vocabulary CLI key begins with a command and its structural ` :: ` separator.
# Consume only that prefix; the field name and every value are still scanned.
_IPV6_LEAK_TOKEN = re.compile(
    r'(?i)"ls[a-z0-9_-]+(?: [^":\\\r\n ]++)*+ :: (?=[^"\\\r\n]*+"\s*:)'
    r'|\\{1,2}(?:u[0-9a-f]{4}|[bfnrt])|\\[\\"/]|[0-9a-z_.:]++'
)


def _ipv6_leaks(text: str) -> list[str]:
    """Find addresses independently of the scrub rule, excluding HMC records and MACs."""
    found = []
    for match in _IPV6_LEAK_TOKEN.finditer(text):
        token = match.group()
        # A non-hex label can precede an address without whitespace (`IPv6:::1`).
        # Find its final non-address character once, then consume one label colon.
        label_end = max(
            (m.end() for m in re.finditer(r"(?i)[^0-9a-f:.]", token)), default=0
        )
        label_colon = token.find(":", label_end) if label_end else -1
        if label_end and label_colon < 0:
            continue
        value = token[label_colon + 1 :].strip(".")
        if not 2 <= len(value) <= 45 or ":" not in value or value == "2001:db8::1":
            continue
        if re.fullmatch(r"(?i)(?:[0-9a-f]{2}:){5,}[0-9a-f]{2}", value):
            continue
        if not _is_ipv6(value):
            # A literal backslash can precede an address opening with a hex letter.
            if label_end or text[
                max(0, match.start() - 2) : match.start()
            ].lower() not in (
                r"\b",
                r"\f",
            ):
                continue
            value = text[match.start() - 1] + value
        try:
            ipaddress.IPv6Address(value)
        except ipaddress.AddressValueError:
            continue
        found.append(value)
    return found


def _email(match: re.Match[str]) -> str:
    """Redact an e-mail address; a run of local-part characters with no `@` stays.

    The rule takes the whole run, so a run with no address is scanned once rather than
    again from each of its positions, which were bound to fail the same way. An escape
    is consumed first, even without a following run, so its letter stays intact.
    """
    if match.group("domain"):
        return (match.group("escape") or "") + "user@example.test"
    return match.group(0)


#: A JSON escape and the hex run it directly precedes, or a hex run at a word boundary.
#: The escape is matched first, so a run never starts on its letter: the `f` of
#: `\fA1B2C3D4E5F6` is not zeroed into the invalid escape `\0`. Scanning left to
#: right, a run follows only an escape the scan itself matched, never the `\u` of a
#: literal `\\u` before a MAC. A surrogate pair is one escape, so replacing the digits
#: of its low half never strands the high one. An escape before a MAC separator takes an
#: empty run, so its digits are zeroed: in raw XML they are a separated MAC's first
#: octets. So are the digits after a `\\u` the scan did not match as an escape (#1622),
#: however many: in JSON text five or more are the MAC's, as the escape and the run
#: after it are in raw XML (#1642). So is a joined octet (#1640).
_MAC_ELEMENT_RUN = re.compile(
    r"(?P<escape>\\u[dD][89abAB][0-9A-Fa-f]{2}\\u[dD][c-fC-F][0-9A-Fa-f]{2}"
    rf"|{'|'.join(_JSON_ESCAPES)})(?P<run>[0-9A-Fa-f]+\b|(?={_MAC_SEPARATOR}))?"
    r"|(?<=\\u)[0-9A-Fa-f]+\b|\b[0-9A-Fa-f]+\b"
    rf"|{_JOINED_OCTET}"
)
_ESCAPE_DIGITS = re.compile(r"(?<=\\u)[0-9A-Fa-f]{4}")


def _zero_run(run: re.Match[str]) -> str:
    """Zero *run*'s hex digits, keeping an escape before them.

    Raw XML has no escapes, so a `\\u` there is literal and the four digits after it
    may be the MAC's own: they become `\\u0030`, an escape for the digit `0`. Not
    `\\u0000`: the NUL it decodes to is not a legal XML 1.0 character.
    """
    escape, value = run.group("escape", "run")
    if escape is None:
        return "0" * len(run.group(0))
    if value is None:
        return escape
    return _ESCAPE_DIGITS.sub("0030", escape) + "0" * len(value)


def _zero_value(match: re.Match[str]) -> str:
    """Zero a free-text MAC's hex digits, keeping its separators and the escape before it."""
    escape = match.group("escape") or ""
    return escape + re.sub("[0-9A-Fa-f]", "0", match.group(0)[len(escape) :])


def _mac_element(match: re.Match[str]) -> str:
    """Zero each hex run in a MAC element's text; keywords such as `ALL` stay as evidence."""
    text = _MAC_ELEMENT_RUN.sub(_zero_run, match.group(2))
    return match.group(1) + text


# Replacements write `\g<1>`, never `\1` before a digit: `\1000000000000` is the
# octal escape for `@` and deleted the opening <MACAddress> tag 959 times.
SECRET_RULES: tuple[tuple[re.Pattern[str], Replacement], ...] = (
    # Both key elements are redacted whole; an SSH key in free text loses its body
    # and its comment, which is conventionally user@host, up to the next `<`.
    _element_rule(*_SSH_KEY_ELEMENT),
    (
        re.compile(
            r"\b(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-[\w-]+|sk-[\w@.-]+)"
            r"\s+[A-Za-z0-9+/=]{16,}(?:[ \t]+[^\s<\"',}\]]+)?"
        ),
        r"\g<1> <REDACTED-SSHKEY>",
    ),
    (re.compile(r"(?i)\b(x-api-session=)[^,}\s]+"), r"\g<1><REDACTED-SESSION>"),
    _element_rule(*_SESSION_ELEMENT),
    (
        re.compile(r"(?i)\b(JSESSIONID|CCFWSESSION|LtpaToken2)=[^;,}\s]+"),
        r"\g<1>=<REDACTED-COOKIE>",
    ),
    (re.compile(r"(?i)\b(cookie=)[^,}\n]+"), r"\g<1><REDACTED-COOKIE>"),
)
IDENTIFIER_RULES: tuple[tuple[re.Pattern[str], Replacement], ...] = (
    (
        re.compile(
            rf"(?i)(?:(?P<escape>{'|'.join(_JSON_ESCAPES)})|\b)"
            r"[\w.+-]++(?P<domain>@[a-z0-9-]+(?:\.[a-z0-9-]+)+\b)?"
            rf"|(?:{'|'.join(_JSON_ESCAPES)})"
        ),
        _email,
    ),
    (
        re.compile(
            r"(?<![\w.-])(?!(?:www\.ibm\.com|www\.w3\.org)\b)"
            r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:ibm\.com|internal|local|lab|lan|corp)\b"
        ),
        HMC_HOST,
    ),
    _element_rule(*_ADDRESS_ELEMENT),
    (
        re.compile(rf"{_SCRUB_START}(?:\d{{1,3}}\.){{3}}\d{{1,3}}\b"),
        rf"\g<escape>{PLACEHOLDER_IP}",
    ),
    # Anywhere, even inside `1eU8375.42A.XXXXXXX-V100-C3`; the slot suffix after the
    # prefix is generic and code joins on it, so it stays.
    (re.compile(r"U[0-9A-Za-z]{4}\.[0-9A-Za-z]{3}\.[0-9A-Za-z]{7}"), "<REDACTED-LOC>"),
    # A `-L<hex>` segment of a location suffix is a disk WWN or RAID array id; `-L0`
    # and other short logical-unit segments stay.
    (re.compile(r"(?<=-)L[0-9A-F]{8,}(?=-L|\b)"), "L<REDACTED-DEVID>"),
    _element_rule(*_DEVICE_ID_ELEMENT),
    # A quoted CSV field holds every value up to its closing quote: "wwpns=a,b". The
    # backslashes before that quote stay, so `json.dumps`'s `\"` keeps its escape and an
    # escaped backslash its pair. Each run is read once, possessively.
    (
        re.compile(
            rf'(?i)"({_DEVICE_ID_KEY}|{_KEYWORD_SERIAL_KEY}'
            rf'(?!={_SERIAL_KEPT_RE}\\?"))=(?:[^"\n\\]++|\\++(?!"))*+(\\*+)"'
        ),
        r'"\g<1>=<REDACTED-DEVID>\g<2>"',
    ),
    (
        re.compile(
            rf"(?i){_KEY_START}({_DEVICE_ID_KEY}|{_KEYWORD_SERIAL_KEY}"
            rf"(?!={_SERIAL_KEPT_RE}{_FIELD_END}))=(?:[^,\n\r\"<]++|\r(?!\n))++"
        ),
        r"\g<1>=<REDACTED-DEVID>",
    ),
    _element_rule(*_SERIAL_ELEMENT),
    (re.compile(r"\b(\d{4}-[0-9A-Z]{3}\*)[0-9A-Z]{7}\b"), r"\g<1><REDACTED-SERIAL>"),
    # Every element naming a MAC address, `MACAddressPrefix` and `MacAddressValue` too, in
    # any case and after any namespace prefix. A self-closing `<MACAddress/>` has no
    # value: text after it belongs to its parent. This rule leaves it, and `_MAC_LEAK`
    # reports it only when it ends at a closing MAC tag.
    (
        _MAC_ELEMENT,
        _mac_element,
    ),
    *(
        (re.compile(rf"{_SCRUB_START}{guard}{run}"), _zero_value)
        for guard, run, _ in _MAC_FORMS
    ),
    (re.compile(r"(?i)(mac_addr=)[0-9a-f]{12}"), r"\g<1>000000000000"),
    (_IPV6, _ipv6),
    # Any other 16-hex token is a WWN: a `disk@<target WWPN>` path, an
    # `AIX-VDASD-<hex>` descriptor. A LUN address (`81` and fourteen zeros) is not.
    (re.compile(rf"{_SCRUB_START}{_WWN_16}"), r"\g<escape><REDACTED-DEVID>"),
)
#: Shapes that may never appear in a tokenized output, whatever was collected.
ALWAYS_LEAKS = (
    re.compile(
        rf"{_LEAK_START}(?!{re.escape(PLACEHOLDER_IP)}\b)(?:\d{{1,3}}\.){{3}}\d{{1,3}}\b"
    ),
    re.compile(r"U[0-9A-Za-z]{4}\.[0-9A-Za-z]{3}\.[0-9A-Za-z]{7}"),
    re.compile(r"-L[0-9A-F]{8,}"),
    re.compile(rf"{_LEAK_START}{_WWN_16}"),
    # A free-text MAC the scrub rules missed; only the zeroed form they write is kept.
    *(
        re.compile(rf"{_LEAK_START}{guard}(?!{zeroed}){run}")
        for guard, run, zeroed in _MAC_FORMS
    ),
    re.compile(r"(?i)x-api-session=(?!<REDACTED)\w"),
    re.compile(r"\b(?:ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-[\w-]+) +AAAA"),
    # A private token joined to a word left part of that word (a lab name's suffix)
    # behind. Location and device tokens keep a generic suffix on purpose.
    re.compile(
        r"(?:<|&lt;)REDACTED-PRIVATE(?:>|&gt;)[\w.-]|[\w.-](?:<|&lt;)REDACTED-PRIVATE"
    ),
    # A long upper-case hex run is a volume id or WWN; no UUID, etag or count is one.
    re.compile(r"(?<![0-9A-Za-z])[0-9A-F]{20,}"),
    # Element text a scrub rule missed: a malformed opening tag, a `<` in the value.
    *(
        _element_leak(*element)
        for element in (
            _SESSION_ELEMENT,
            _SSH_KEY_ELEMENT,
            _ADDRESS_ELEMENT,
            _DEVICE_ID_ELEMENT,
            _SERIAL_ELEMENT,
        )
    ),
    _MAC_LEAK,
    # A device id or serial the key=value rules missed, where they start.
    _key_value_leak(_KEY_START, _DEVICE_ID_KEY),
    # A keyword-holding serial the key=value rules missed: no word boundary is needed,
    # and `json.dumps` text hides one behind a `\n` escape.
    _key_value_leak("", _KEYWORD_SERIAL_KEY, _SERIAL_KEPT_RE),
)
#: Elements whose values are fixed HMC vocabulary (task names such as `View HMC Logs`).
#: A candidate name found inside one is HMC wording, never a private name.
BUILT_IN_VOCABULARY_ELEMENTS = ("ManagedTaskName", "ManagedTaskType", "ManagedTaskKey")
#: HMC sentinels and messages: data the tests need verbatim, never a name.
SENTINELS = {
    "No results were found",
    "null",
    "none",
    "None",
    "N/A",
    "unavailable",
    "Unknown",
}


class ExportError(RuntimeError):
    """The export is unsafe or corrupt; nothing was written."""


class LeakError(ExportError):
    """A tokenized output still carries something private."""


class BrokenBodyError(ExportError):
    """Tokenizing turned XML or JSON text that parsed into text that does not."""


# --- collection -------------------------------------------------------------


def structural_identifiers(texts: Iterable[str]) -> set[str]:
    """Element, attribute, JSON-key and CLI-field names: structure, never a name."""
    found: set[str] = set()
    for text in texts:
        found.update(re.findall(r"</?(?:[\w.-]+:)?([A-Za-z_][\w.-]*)", text))
        # Texts are JSON-encoded records, so an attribute's quote arrives escaped.
        found.update(re.findall(r"\s(?:[\w.-]+:)?([A-Za-z_][\w.-]*)=\\?[\"']", text))
        found.update(re.findall(r"\"([A-Za-z_][\w-]*)\"\s*:", text))
        found.update(re.findall(r"(?:^|[,\"])([a-z_][a-z0-9_]*)=", text, re.MULTILINE))
    return found


def schema_enum_values(records: Iterable[dict[str, Any]]) -> set[str]:
    """Every enumeration value in any captured XSD body."""
    values: set[str] = set()
    for record in records:
        body = record.get("body") or ""
        if "enumeration" in body and "schema" in body:
            values.update(
                re.findall(r"<(?:\w+:)?enumeration\s+value=\"([^\"]*)\"", body)
            )
    return values


def _cli_fields(command: str) -> list[str] | None:
    """The `-F` field list of *command*, or None when it prints native output."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if "-F" not in words:
        return None
    index = words.index("-F") + 1
    if index >= len(words) or words[index].startswith("-"):
        return []
    return words[index].split(",")


def cli_rows(command: str, stdout: str) -> list[dict[str, str]]:
    """Parse `ls*` output into rows of field -> value, `-F` lists and native k=v both."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    fields = _cli_fields(command)
    if fields is not None and ("--header" in command or not fields):
        if not lines:
            return []
        fields = next(csv.reader([lines[0]]))
        lines = lines[1:]
    rows = []
    for line in lines:
        values = next(csv.reader([line]))
        if fields:
            rows.append(dict(zip(fields, values, strict=False)))
        else:
            pairs = (value.split("=", 1) for value in values if "=" in value)
            rows.append({key: value for key, value in pairs})
    return rows


class NameCollector:
    """Maps each name found in the captures to a stable token."""

    def __init__(self, excluded: set[str], phrases: Iterable[str] = ()) -> None:
        self.names: dict[str, str] = {}
        self._excluded = excluded
        self._phrases = tuple(phrases)
        self._counts: collections.Counter[str] = collections.Counter()

    def add(self, kind: str, value: str | None) -> None:
        value = (value or "").strip().strip('"')
        if (
            len(value) < 3
            or value in self.names
            or value in self._excluded
            or value.lower() in _NOT_NAMES
            or value.rstrip(".") in SENTINELS
            or value in BUILT_IN_ACCOUNTS
            or value.startswith(("HSCL", "No results"))
            or re.fullmatch(r"[0-9.]+", value)
            or UUID.fullmatch(value)
            or any(value in phrase for phrase in self._phrases)
        ):
            return
        self._counts[kind] += 1
        n = self._counts[kind]
        self.names[value] = f"sys-R{n}" if kind == "sys" else f"{kind}-{n}"

    def finish(self) -> dict[str, str]:
        """Give a partition named after its system the system's token plus its suffix."""
        systems = {n: t for n, t in self.names.items() if t.startswith("sys-R")}
        for name in list(self.names):
            for system, token in systems.items():
                if name != system and name.startswith(system + "-"):
                    self.names[name] = token + name[len(system) :]
        return self.names


def collect_names(records: Sequence[dict[str, Any]]) -> dict[str, str]:
    texts = [_text_of(record) for record in records]
    excluded = structural_identifiers(texts) | schema_enum_values(records)
    blob = "\n".join(texts)
    phrases = {
        value.strip()
        for element in BUILT_IN_VOCABULARY_ELEMENTS
        for value in re.findall(
            rf"<(?:\w+:)?{element}(?:\s[^>]*)?>([^<]+)</(?:\w+:)?{element}>", blob
        )
    }
    collector = NameCollector(excluded, phrases)
    for element, kind in NAME_ELEMENTS.items():
        pattern = rf"<(?:\w+:)?{element}(?:\s[^>]*)?>([^<]+)</(?:\w+:)?{element}>"
        for value in re.findall(pattern, blob):
            collector.add(kind, value)
    for record in records:
        if record.get("kind") != "ssh" or not record.get("stdout"):
            continue
        for row in cli_rows(record.get("command") or "", record["stdout"]):
            for field, value in row.items():
                if field in NAME_FIELDS:
                    for part in value.split(","):
                        collector.add(NAME_FIELDS[field], part)
    return collector.finish()


def url_hosts(texts: Iterable[str]) -> set[str]:
    """Hosts of every `https://` URL, the HMC's own address among them."""
    hosts = set()
    for text in texts:
        hosts.update(re.findall(r"https://([A-Za-z0-9.-]+)", text))
    return {h for h in hosts if h not in PRESERVED_HOSTS and h != HMC_HOST}


def _text_of(record: dict[str, Any]) -> str:
    return json.dumps(record, default=str)


# --- tokenizing --------------------------------------------------------------


class Tokenizer:
    def __init__(
        self, names: dict[str, str], hosts: set[str], private: Sequence[str] = ()
    ) -> None:
        self.names = names
        self.hosts = hosts
        self.private = [re.compile(p) for p in private]
        # A match replaces the whole word it sits in, never only the matched fragment.
        self._private_words = [re.compile(rf"[\w.-]*(?:{p})[\w.-]*") for p in private]
        self._uuids: dict[str, str] = {}
        spaced = sorted(
            (n for n in names if not WORD.fullmatch(n)), key=len, reverse=True
        )
        self._spaced = (
            re.compile("|".join(rf"(?<!\w){re.escape(n)}(?!\w)" for n in spaced))
            if spaced
            else None
        )
        host_alternation = "|".join(
            re.escape(h) for h in sorted(hosts, key=len, reverse=True)
        )
        self._hosts = (
            re.compile(rf"(?<![\w.-])(?:{host_alternation})(?::\d+)?(?![\w-])")
            if hosts
            else None
        )

    def _uuid(self, match: re.Match[str]) -> str:
        original = match.group(0)
        key = original.lower()
        if key not in self._uuids:
            n = len(self._uuids) + 1
            self._uuids[key] = f"{n:08x}-abcd-4ef0-8abc-{n:012x}"
        token = self._uuids[key]
        return token.upper() if original == original.upper() else token

    def _word(self, match: re.Match[str]) -> str:
        word = match.group(0)
        if word in self.names:
            return self.names[word]
        # A trailing dot belongs to the sentence, not the name.
        if word.endswith(".") and word[:-1] in self.names:
            return self.names[word[:-1]] + "."
        return word

    def tokenize(self, text: str | None, *, json_encoded: bool = False) -> str | None:
        """Scrub raw text; json_encoded opts in only for JSON made by the caller."""
        if text is None:
            return None
        xml = text.lstrip().startswith("<")
        text = UUID.sub(self._uuid, text)
        for pattern, replacement in SECRET_RULES:
            text = pattern.sub(replacement, text)
        if self._hosts:
            text = self._hosts.sub(f"{HMC_HOST}:443", text)
        for pattern, replacement in IDENTIFIER_RULES:
            text = pattern.sub(replacement, text)
        # Names first: a private pattern run earlier would cut the lab prefix out of
        # `acmesys9-lp3`, the name would no longer match, and its suffix would leak.
        if self._spaced:
            text = self._spaced.sub(lambda m: self.names[m.group(0)], text)
        text = WORD.sub(self._word, text)
        for pattern in self._private_words:
            text = pattern.sub("<REDACTED-PRIVATE>", text)
        if xml:
            # The token must not open an element, or the body would stop parsing.
            text = re.sub(r"<(REDACTED-[A-Z]+)>", r"&lt;\1&gt;", text)
        # Check this call's exact representation before fields are serialized together.
        mac_values = iter(_MAC_ELEMENT.finditer(text))
        mac_value = next(mac_values, None)
        raw_rule = _JSON_RAW_IDENTIFIER_LEAK if json_encoded else _RAW_IDENTIFIER_LEAK
        for match in raw_rule.finditer(text):
            while mac_value is not None and mac_value.end(2) <= match.start():
                mac_value = next(mac_values, None)
            if mac_value is None or match.start() < mac_value.start(2):
                raise LeakError("a raw identifier survived tokenizing; nothing written")
        return text

    def leaks(self, text: str) -> list[str]:
        """Everything private still present in *text*."""
        words = set(WORD.findall(text))
        found = [
            name
            for name in self.names
            if len(name) >= 4
            and (name in words if WORD.fullmatch(name) else name in text)
        ]
        found += [host for host in self.hosts if host in text]
        for pattern in ALWAYS_LEAKS:
            found += [
                m.group(0)
                for m in pattern.finditer(text)
                if not m.groupdict().get("kept")
            ]
        found += _ipv6_leaks(text)
        for pattern in self.private:
            found += [m.group(0) for m in pattern.finditer(text)]
        return found


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


#: Tool output keys that hold a device id or serial, in any case or separator style,
#: so `VolumeUniqueID`, `volume_unique_id` and `UniqueDeviceID` all match. The text
#: rules see only XML elements and `key=value` fields, never this JSON form.
_DEVICE_ID_KEYS = {_key(n) for n in _DEVICE_ID_ELEMENTS.split("|")} | {
    "wwpns",
    "logicalunitudid",
    "luudid",
    "tierudid",
}
_SERIAL_KEYS = {_key(name) for name in _SERIAL_NAMES}
#: The keywords a serial element keeps (#1593), kept here on the same terms: only as the
#: whole value, in their documented case.
_SERIAL_KEPT = frozenset(_SERIAL_ELEMENT[2].split("|"))


def _replace_value(value: Any, token: str, keep: frozenset[str] = frozenset()) -> Any:
    """Replace text by *token*, retaining ordinary attributes and *keep* values."""
    if isinstance(value, dict):
        return {
            k: redact_identifier_keys(v)
            if k == "@attrs"
            else _replace_value(v, token, keep)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_replace_value(v, token, keep) for v in value]
    return value if value is None or value in keep else token


def redact_identifier_keys(value: Any) -> Any:
    """Redact device ids and serials held under their key names anywhere in *value*."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _key(key) in _DEVICE_ID_KEYS:
                out[key] = _replace_value(item, "<REDACTED-DEVID>")
            elif _key(key) in _SERIAL_KEYS or "serial" in _key(key):
                out[key] = _replace_value(item, "<REDACTED-SERIAL>", _SERIAL_KEPT)
            else:
                out[key] = redact_identifier_keys(item)
        return out
    if isinstance(value, list):
        return [redact_identifier_keys(v) for v in value]
    return value


def _has_raw_identifier(data: Any) -> bool:
    """Check actual unsanitized strings and keys without scrub-span exemptions."""
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value)
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
        elif isinstance(value, str) and _RAW_IDENTIFIER_LEAK.search(value):
            return True
    return False


def _tokenize_record(tok: Tokenizer, record: dict[str, Any]) -> dict[str, Any]:
    kind = record.get("kind")
    out: dict[str, Any] = {
        "capture": record["_src"],
        "step": record.get("step"),
        "kind": kind,
    }
    if kind == "rest":
        headers = {
            k.lower(): v for k, v in (record.get("response_headers") or {}).items()
        }
        # Reserve the scrubbed field's position to preserve serialized output order.
        out.update(
            method=record.get("method"),
            path=None,
            accept=record.get("accept"),
            status=record.get("status"),
            content_type=headers.get("content-type"),
        )
    elif kind == "ssh":
        out.update(command=None, exit_status=record.get("exit_status"))
    if _has_raw_identifier(out):
        raise LeakError("a raw identifier is in capture metadata; nothing written")
    if kind == "rest":
        out.update(
            path=tok.tokenize(record.get("path")),
            body=tok.tokenize(record.get("body")),
            exception=tok.tokenize(record.get("exception")),
        )
    elif kind == "ssh":
        out.update(
            command=tok.tokenize(record.get("command")),
            stdout=tok.tokenize(record.get("stdout")),
            stderr=tok.tokenize(record.get("stderr")),
            exception=tok.tokenize(record.get("exception")),
        )
    else:
        rest = {
            k: v for k, v in record.items() if k not in ("_src", "step", "kind", "t")
        }
        rest = redact_identifier_keys(rest)
        try:
            out["record"] = json.loads(
                tok.tokenize(json.dumps(rest, default=str), json_encoded=True) or "{}"
            )
        except json.JSONDecodeError as exc:
            raise BrokenBodyError(
                f"tokenizing broke the JSON of tool record {record['_src']} "
                f"({exc.msg}); nothing written"
            ) from exc
    return out


def tokenize_records(
    records: Sequence[dict[str, Any]], private: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Tokenize *records*; raise LeakError when anything private survives."""
    tok = Tokenizer(collect_names(records), url_hosts(map(_text_of, records)), private)
    corpus = [_tokenize_record(tok, record) for record in records]
    leaks = tok.leaks(json.dumps(corpus))
    if leaks:
        raise LeakError(
            f"{len(leaks)} private value(s) survived tokenizing; nothing written"
        )
    broken = [
        out["capture"]
        for raw, out in zip(records, corpus, strict=True)
        if _parses(raw.get("body")) and not _parses(out.get("body"))
    ]
    if broken:
        raise BrokenBodyError(
            f"tokenizing broke the XML of {len(broken)} body(ies), first {broken[0]}; "
            "nothing written"
        )
    return corpus


def _parses(body: Any) -> bool:
    """Whether *body* is an XML document (feed, entry or schema) that parses."""
    if not isinstance(body, str) or not body.lstrip().startswith("<"):
        return False
    try:
        ET.fromstring(body.encode("utf-8"))
    except ET.ParseError:
        return False
    return True


def load_raw(paths: Sequence[Path]) -> list[dict[str, Any]]:
    records = []
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if line.strip():
                    record = json.loads(line)
                    record["_src"] = f"{path.name}#{number}"
                    records.append(record)
    return records


# --- vocabulary --------------------------------------------------------------

LEAF = re.compile(
    r"<(?:[\w.-]+:)?([A-Za-z][\w.]*)(?:\s[^>]*)?>([^<]*)</(?:[\w.-]+:)?\1>"
)
#: Elements and fields whose values identify something; only their shape is kept.
NAME_BEARING = re.compile(
    r"(?i)name|description|serial|mac|host|address|location|loc$|wwpn|^ip|_ip|ip_|uuid"
    r"|id$|_ids?$|link|key|session|user|date|time|href|label|path|url|drc|vpd|unique"
    r"|profile$|role$"
)
LITERAL = re.compile(r"[A-Za-z][A-Za-z _/+-]{0,40}")
TOKEN = re.compile(
    rf"\b(?:sys-R\d+(?:-[\w]+)?|(?:{'|'.join(TOKEN_KINDS)})-\d+)\b|&lt;REDACTED-\w+&gt;|<REDACTED-\w+>"
)
_STRUCTURAL_SEGMENT = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:\.xsd)?")


def shape(value: str) -> str:
    v = value.strip()
    if v == "":
        return "<empty>"
    if re.fullmatch(r"-?\d+", v):
        return "<int>"
    if re.fullmatch(r"-?\d+\.\d+", v):
        return "<float>"
    if UUID.fullmatch(v):
        if v == v.lower():
            return "<uuid-lower>"
        return "<uuid-upper>" if v == v.upper() else "<uuid-mixed>"
    if v in ("true", "false") or (LITERAL.fullmatch(v) and not TOKEN.search(v)):
        return v
    return "<text>"


def observed(field: str, value: str) -> str:
    kept = shape(value)
    if kept.startswith("<") or kept in ("true", "false"):
        return kept
    return "<text>" if NAME_BEARING.search(field) else kept


def _enum_members(element: str, enums: dict[str, Any]) -> set[str]:
    """The values of the enums that apply to *element*.

    The enum a captured schema binds it to, else every enum named for it as `_bound_enum`
    names one: `State` is shared by objects with different enums, so it binds to none.
    """
    types = enums.get("types", {})
    bound = enums.get("elements", {}).get(element)
    if bound:
        return set(types.get(bound, ()))
    return {
        value
        for name, values in types.items()
        if _named_for(name.removesuffix(".Enum"), element)
        for value in values
    }


def kept(element: str, value: str, members: set[str]) -> str:
    """`observed`, except that an enum member it would lose as `<text>` is kept (#1292).

    `LITERAL` caps a kept literal at 41 characters; an enum member is a constant the HMC
    publishes, whatever its length. A name-bearing element keeps its shape class.
    """
    shaped = observed(element, value)
    if (
        shaped == "<text>"
        and value.strip() in members
        and not NAME_BEARING.search(element)
    ):
        return value.strip()
    return shaped


def path_template(path: str) -> str:
    """A captured path with identifiers templated; structure and `group=` kept.

    An absolute href (a link the HMC returned, fetched as given) keeps only its path.
    """
    href = urllib.parse.urlsplit(path)
    if href.scheme in ("http", "https") and href.netloc:
        path = urllib.parse.urlunsplit(("", "", href.path, href.query, ""))
    base, _, query = path.partition("?")
    base = UUID.sub("{uuid}", base)
    base = re.sub(r"\(([A-Za-z]+)==[^)]*\)", r"(\1=={value})", base)
    segments = [
        s
        if not s or s.startswith(("{", "(")) or _STRUCTURAL_SEGMENT.fullmatch(s)
        else "{value}"
        for s in base.split("/")
    ]
    templated = "/".join(segments)
    if not query:
        return templated
    params = []
    for param in query.split("&"):
        key = param.partition("=")[0]
        params.append(param if key == "group" else f"{key}={{value}}")
    return templated + "?" + "&".join(params)


#: Option flags whose value identifies something; templated as `{}`.
_VALUE_FLAGS = {"-m", "-p", "--id", "--ip", "-e"}
#: Option flags whose value selects what the command reads; part of a command's key.
KEY_FLAGS = ("-r", "--rsubtype", "--level", "-o")


def command_template(command: str) -> str:
    words = shlex.split(command)
    out: list[str] = []
    previous = ""
    for word in words:
        if previous in _VALUE_FLAGS:
            word = "{}"
        elif previous == "--filter":
            word = ",".join(f"{p.split('=', 1)[0]}={{}}" for p in word.split(","))
        out.append(word)
        previous = word if word.startswith("-") else ""
    return " ".join(out)


def command_key(command: str) -> str:
    """Command name plus its selecting flags, e.g. `lshwres -r sriov --rsubtype adapter`."""
    words = shlex.split(command)
    key = [words[0]] if words else []
    for flag in KEY_FLAGS:
        if flag in words[:-1]:
            key += [flag, words[words.index(flag) + 1]]
    return " ".join(key)


def _template_message(text: str) -> str:
    text = UUID.sub("{uuid}", text)
    return TOKEN.sub("{name}", text).strip()[:300]


def _error_form(record: dict[str, Any]) -> dict[str, Any]:
    body = record.get("body") or ""
    title = re.search(r"<title>([^<]*)</title>", body)
    reason = re.search(r"<(?:\w+:)?ReasonCode\b[^>]*>([^<]*)<", body)
    message = re.search(r"<(?:\w+:)?Message\b[^>]*>([^<]*)<", body)
    return {
        "method": record.get("method"),
        "path": path_template(record.get("path") or ""),
        "status": record.get("status"),
        "content_type": record.get("content_type"),
        "form": title.group(1).strip()
        if title
        else ("<empty>" if not body.strip() else "<text>"),
        "reason_code": _template_message(reason.group(1)) if reason else None,
        "message": _template_message(message.group(1)) if message else None,
    }


def _sentinel(command: str, stdout: str) -> str | None:
    """A one-line fixed message such as `No results were found.`, kept verbatim.

    Only from a command whose output fields name nothing, so a one-line description
    or partition name is never mistaken for a message.
    """
    line = stdout.strip()
    fields = _cli_fields(command) or []
    if (
        "\n" in line
        or "=" in line
        or not re.fullmatch(r"[A-Za-z][A-Za-z ,.'()-]{0,120}", line)
        or TOKEN.search(line)
        or any(NAME_BEARING.search(field) for field in fields)
    ):
        return None
    return line


def _bound_enum(
    element: str, literals: set[str], types: dict[str, list[str]]
) -> str | None:
    """The one enum type named for *element* whose values hold every literal observed for it.

    A same-named enum binds on one literal. A name-related one (`_named_for`) needs two,
    because one value is weak evidence: `Status` observed only as `OPERATIONAL` would
    otherwise bind to a VNIC enum and then judge every job status against it. An enum
    the name does not relate to never binds, however many literals it holds (#1498).
    """
    if not literals:
        return None
    candidates = [t for t, values in types.items() if literals <= set(values)]
    rules = [lambda base: base == element]
    if len(literals) >= 2:
        rules.append(lambda base: _named_for(base, element))
    for rule in rules:
        matches = [t for t in candidates if rule(t.removesuffix(".Enum"))]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
    return None


def _named_for(base: str, element: str) -> bool:
    return base.endswith(element) or element.endswith(base)


def _accept(record: dict[str, Any]) -> str | None:
    # The 2026-09-30 corpus predates the tracked harness and records request headers.
    return record.get("accept") or (record.get("req_headers") or {}).get("Accept")


def _is_schema(record: dict[str, Any]) -> bool:
    path = record.get("path") or ""
    return path.endswith(".xsd") or "/schema" in path


def build_vocabulary(
    corpus: Sequence[dict[str, Any]],
    enums: dict[str, Any],
    firmware: str,
    sources: Sequence[str],
) -> dict[str, Any]:
    """The committed vocabulary of one firmware and hardware pair.

    *sources* names each raw capture the corpus was tokenized from.
    """
    endpoints: set[tuple[Any, ...]] = set()
    errors: list[dict[str, Any]] = []
    values: dict[str, set[str]] = collections.defaultdict(set)
    commands: dict[str, dict[str, set[Any]]] = collections.defaultdict(
        lambda: {
            "exit_status": set(),
            "codes": set(),
            "outputs": set(),
            "streams": set(),
        }
    )
    cli_values: dict[str, set[str]] = collections.defaultdict(set)
    members: dict[str, set[str]] = {}

    def rest_value(element: str, value: str) -> str:
        if element not in members:
            members[element] = _enum_members(element, enums)
        return kept(element, value, members[element])

    for record in corpus:
        if record.get("kind") == "rest" and record.get("status") is not None:
            template = path_template(record.get("path") or "")
            endpoints.add(
                (
                    record["method"],
                    template,
                    _accept(record),
                    record["status"],
                    record.get("content_type"),
                )
            )
            if record["status"] >= 400:
                error = _error_form(record)
                if error not in errors:
                    errors.append(error)
            elif not _is_schema(record):
                for element, value in LEAF.findall(record.get("body") or ""):
                    values[element].add(rest_value(element, value))
        elif record.get("kind") == "ssh" and record.get("command"):
            command = record["command"]
            entry = commands[command_template(command)]
            entry["exit_status"].add(record.get("exit_status"))
            output = (record.get("stdout") or "") + (record.get("stderr") or "")
            entry["codes"].update(re.findall(r"\bHSCL\w{4}\b", output))
            if record.get("exit_status") not in (0, None):
                # V10R3 prints an HSCL error on stdout and leaves stderr empty.
                entry["streams"].update(
                    name
                    for name in ("stdout", "stderr")
                    if (record.get(name) or "").strip()
                )
            sentinel = _sentinel(command, record.get("stdout") or "")
            if sentinel:
                entry["outputs"].add(sentinel)
            if record.get("exit_status") == 0:
                key = command_key(command)
                for row in cli_rows(command, record.get("stdout") or ""):
                    for field, value in row.items():
                        cli_values[f"{key} :: {field}"].add(observed(field, value))
    types = enums.get("types", {})
    bindings = dict(enums.get("elements", {}))
    for element, seen in values.items():
        if element not in bindings:
            literals = {
                v for v in seen if not v.startswith("<") and v not in ("true", "false")
            }
            bound = _bound_enum(element, literals, types)
            if bound:
                bindings[element] = bound
    return {
        "firmware": firmware,
        "sources": list(sources),
        "rest": {
            "endpoints": [
                dict(
                    zip(
                        ("method", "path", "accept", "status", "content_type"),
                        e,
                        strict=True,
                    )
                )
                for e in sorted(endpoints, key=lambda e: tuple(str(x) for x in e))
            ],
            "errors": sorted(errors, key=lambda e: json.dumps(e, sort_keys=True)),
            "values": {k: sorted(v) for k, v in sorted(values.items())},
            "element_enums": {k: bindings[k] for k in sorted(bindings) if k in values},
        },
        "cli": {
            "commands": [
                {
                    "command": template,
                    "exit_status": sorted(e["exit_status"], key=str),
                    "codes": sorted(e["codes"]),
                    "outputs": sorted(e["outputs"]),
                    "error_streams": sorted(e["streams"]),
                }
                for template, e in sorted(commands.items())
            ],
            "values": {k: sorted(v) for k, v in sorted(cli_values.items())},
        },
    }


# --- schema enums ------------------------------------------------------------

_XSD = "{http://www.w3.org/2001/XMLSchema}"


def derive_enums(corpus: Sequence[dict[str, Any]], firmware: str) -> dict[str, Any]:
    """Enum types from the captured `Enumerations.xsd`, bindings from any captured XSD."""
    types: dict[str, list[str]] = {}
    elements: dict[str, str] = {}
    for record in corpus:
        body = record.get("body") or ""
        if (
            record.get("kind") != "rest"
            or record.get("status") != 200
            or not _is_schema(record)
        ):
            continue
        try:
            root = ET.fromstring(body.encode("utf-8"))
        except ET.ParseError:
            continue
        for simple in root.iter(f"{_XSD}simpleType"):
            name = simple.get("name") or ""
            if name.endswith(".Enum"):
                types[name] = [
                    e.get("value") or "" for e in simple.iter(f"{_XSD}enumeration")
                ]
        for element in root.iter(f"{_XSD}element"):
            name = element.get("name")
            bound = element.get("type") or next(
                (ext.get("base") for ext in element.iter(f"{_XSD}extension")), None
            )
            if name and bound and bound.endswith(".Enum"):
                elements[name] = bound
    if not types:
        raise ValueError("no Enumerations.xsd record with enum types in the corpus")
    return {
        "firmware": firmware,
        "types": dict(sorted(types.items())),
        "elements": dict(sorted(elements.items())),
    }


# --- entry point -------------------------------------------------------------


def _write_scanned(data: Any, out: Path, private: Sequence[str]) -> None:
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    leaks = Tokenizer({}, set(), private).leaks(text)
    if leaks:
        raise LeakError(
            f"{len(leaks)} private value(s) in the derived output; nothing written"
        )
    if _has_raw_identifier(data):
        raise LeakError("a raw identifier is in the derived output; nothing written")
    out.write_text(text, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    tok = sub.add_parser(
        "tokenize", help="tokenize raw capture JSONL into a private corpus"
    )
    tok.add_argument("raw", nargs="+", type=Path)
    vocab = sub.add_parser("vocabulary", help="derive a committed vocabulary")
    vocab.add_argument("corpus", type=Path)
    vocab.add_argument("--enums", type=Path, required=True)
    vocab.add_argument("--firmware", required=True)
    vocab.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="TEXT",
        help="where one raw capture in the corpus came from (repeatable)",
    )
    enum = sub.add_parser("enums", help="derive the committed schema enum lists")
    enum.add_argument("corpus", type=Path)
    enum.add_argument("--firmware", required=True)
    for p in (tok, vocab, enum):
        p.add_argument("--out", type=Path, required=True)
        p.add_argument(
            "--private",
            action="append",
            default=[],
            metavar="REGEX",
            help="a lab name or host pattern that must not survive (repeatable)",
        )
    args = parser.parse_args(argv)
    try:
        if args.command == "tokenize":
            corpus = tokenize_records(load_raw(args.raw), args.private)
            fd = os.open(
                args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(corpus, handle, indent=1)
            print(f"{len(corpus)} records tokenized -> {args.out}")
            return 0
        corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
        if args.command == "enums":
            data = derive_enums(corpus, args.firmware)
        else:
            enums = json.loads(args.enums.read_text(encoding="utf-8"))
            data = build_vocabulary(corpus, enums, args.firmware, args.source)
        _write_scanned(data, args.out, args.private)
        print(f"wrote {args.out}")
        return 0
    except (ExportError, ValueError, OSError) as exc:
        print(f"live_capture_export: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
