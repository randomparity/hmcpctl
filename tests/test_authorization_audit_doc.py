"""The audit documents must agree with the vocabularies they mirror.

#486. `audit.REASONS`, `audit.EVENTS`, `audit.State`, `tool_registry.EFFECTS` and
`client.VERIFY_SSL_SOURCES` are closed vocabularies, each derived from a `Literal` so
"a checker and a test can consult" it. `docs/authorization-audit.md` restates all five —
as a reason-code table, as one `### event:` section apiece, as the `effect` row of the
authorization field table, as the sentence enumerating the `connection.state` arms, and
as the clause naming the TLS record's `source` values — and nothing read it, so the two
sides could drift apart silently. They had.

`audit.DECISIONS` is the sixth, restated as the `decision` row of the same field table
and as the reason-code table's middle column (#518). It was the one vocabulary with no
alias to derive from — its `Literal` was written inline in `record_authorization`'s own
signature — so it has been lifted to `audit.Decision` beside its siblings rather than
read back off the signature here. Every `Literal`-annotated parameter of that builder, and
the same two on the `dispatch_scope` closure that calls it, are now held to naming their
alias — read from source, because a resolved hint cannot tell an alias from an inline
`Literal` — and each alias is held to the frozenset the checks below compare against. So
re-inlining one reddens instead of leaving those checks describing a vocabulary the
builder has stopped accepting.

`docs/environment-variables.md`'s `HMC_VERIFY_SSL` note describes the same TLS record, so
its restatement of the `source` values is held to the same set and neither document can
drift alone (#497). Two further restatements were in code — `audit`'s record builder and
its test — and are gone: each names `client.VerifySSLSource` instead, so the last check
below holds a pointer rather than comparing a vocabulary, there being none left in them
to compare (#504). Its residuals are in the ledger.

Every equality check below is a set comparison, so it fails on an orphan (documented,
not defined) and on a dangling entry (defined, not documented) alike — with one bounded
exception in the ledger, for a member appended to a run-based list with "and". Each is
paired with a mutation test that feeds the extractor a deliberately drifted copy of the
real document and asserts it notices, so the check cannot pass by extracting nothing.

The audit document's sample records are read by parsing the fenced JSON rather than by
backticking the literals inside them, which would stop them being JSON at all: a sample is
the one place a consumer copies a log query or an alert rule from, so it has to stay valid
and copy-pasteable (#506). Parsing reads them structurally, which is also what separates
the TLS record's top-level `source` from the `attribution.source` beside it — the same key
naming a different vocabulary. Only `event` is held in both directions there, because one
section and at least one sample apiece makes coverage checkable; one sample record shows
one `reason`, one `effect`, one `state`, one `kind` and one `decision`, so those are held
to naming nothing undefined. For `reason`, `effect`, `state` and `decision` the other half
is kept by the reason-code table, the `effect` and `decision` rows and the state sentence,
which are checked above; `kind` restates no such passage, so it is held in the orphan
direction alone and the ledger records it.
Coverage reads `event` at the top level and the orphan half reads it at any depth, so a
nested value cannot stand in for a missing record. Coverage is checked twice: over the
document, and over the `## The records` section alone, so an event sampled in two places
cannot lose the copy under its own heading. The price is an editing constraint: every
`json` fence in that document must open one of these records, written at the start of its
line. A fence that opens something else fails quoting the block and stating the rule; one
the reader cannot take fails naming every opener it found, by line.
`docs/environment-variables.md` carries no JSON block and so has no arm of this check.

The counts stay out of the assertions on purpose: pinning one here would recreate the
stale literal this guard exists to remove, the same anti-pattern
`test_project_metadata.test_generated_policy_guidance_does_not_pin_a_stale_tool_count`
already guards in the README. Two patterns hold the document to the same rule — one for
counts of reason codes, one for counts of records or events, which is the class that had
actually gone stale ("The two records", written when there were two).

Restated *constants* are checked where the constant is exported and nothing else.

What this does not reach, so a green run is not read as more coverage than it is:

- an enumeration written out in prose rather than as a table or a heading, unless it is
  one of the two `source` clauses below — the guard compares vocabularies, so a sentence
  listing the members by name goes stale silently. The documents avoid them where they
  can and state the rule instead;
- a pinned count of the `source` vocabulary, in either document. The count patterns below
  anchor on a noun, and "source" is ordinary English this document already uses for
  something else — "the two sources" of an ownership-override record names two code paths,
  not two `source` values — so an alternation for it would fail on prose that is correct.
  Both documents are held to the membership of that vocabulary and not to its size;
- a pinned count anywhere in `docs/environment-variables.md`. The count patterns run over
  the audit document, whose subject is these vocabularies; turning them loose on a
  document about twenty settings would read its ordinary prose ("one SSH login plus two
  REST GETs") the same wrong way. Only the `HMC_VERIFY_SSL` note's membership is guarded;
- a member appended to a run-based list with "and" rather than a comma or "or".
  `FIELD_VALUE_LIST` and `SOURCE_LIST` end the run at the first separator they do not
  recognise, which is what lets the negative controls treat a trailing clarification as
  prose — and the same tradeoff means `..., or `field-default`, and `config-file`` reads as
  three values plus prose. The dangling direction is unaffected; only the orphan half has
  the hole, and only for those two extractors;
- the *mapping* the reason-code table's middle column states — which decision each reason
  code is emitted with. Every cell there is held to naming a defined decision, and the
  `decision` field row above holds the other half of that vocabulary, so neither side of
  the set can drift. The pairing is not held. It is fixed in code, at
  `dispatch_scope.dispatch_authorizer`'s four `record(...)` call sites — `permitted` with
  `allow`, every other reason with `deny` — and nothing compares the column to them, so a
  row whose decision stops matching its emitter goes stale silently. Reading those call
  sites with `ast`, as `_annotation_source` reads a signature, would reach three of the
  four; the fourth passes `denial_reason(...)` rather than a literal. So a scrape returns
  a subset of the mapping that shrinks silently as call sites are refactored, and holding
  it to a size means pinning a count of them — the stale literal this module refuses to
  write anywhere else, and the reason this was weighed and declined rather than costed.
  The column is held in the orphan direction instead, which is a claim about the
  vocabulary rather than the mapping; equality there would be wrong on its own terms,
  because a decision arm that no reason code yields belongs in the field row and
  legitimately not in this table.
  What the column does carry is an editing constraint, and that one fails loud:
  `REASON_ROW` reads both cells at once, so a decision cell widened into prose takes its
  whole row out of the reason vocabulary and reddens the equality check above, rather than
  quietly ceasing to be read;
- a `Literal` re-inlined in any signature but the two held below. `record_authorization`
  and `dispatch_scope.record` are read from source and must name their aliases, and the
  first is held to covering every `Literal` parameter it has — but a fourth restatement
  written somewhere else entirely is reached by nothing here;
- a closed vocabulary the builder carries as a plain `str`. `effect` is one today: the
  values are `tool_registry.EFFECTS`, but `audit` imports nothing from `hmcpctl` — the
  module docstring's own rule, since `target_scope` imports back — so there is no alias
  for its signature to name and no `Literal` for the parameter check above to see. The
  next closed vocabulary reaching this builder from another module will look the same.
  Reaching it means scanning the signature's `str` parameters and holding the leftovers
  against a named exclusion list, which is the second unenforced list the sample-keys
  bullet below already declines. What holds `effect` instead is the field row: the
  document is compared to `EFFECTS` even though the builder is not;
- the sample records' `policy`, `tool` and `targets[].argument` values, none of which is a
  closed vocabulary. `policy` is an example name; `tool` names a registry entry rather
  than a `Literal`, and deriving that set means importing the server, which this module
  does not do; and `argument` is a plain `str` on `TargetSelector`, drawn from
  `REQUIRED_TARGET_ARGUMENTS`'s keys *and* from whatever a tool's own `extra_targets`
  declares, so that mapping is a subset of the field's range rather than its vocabulary and
  is not read here at all. `kind` is the closed half of that pair, held to `TARGET_KINDS`;
- whether the sample keys held are *all* of them. `SAMPLE_VOCABULARIES` lists six, and
  `source` is a seventh held by hand below — that is the set as of this commit, and nothing
  enforces that an eighth gets added: a new closed vocabulary on a record, or a `Literal`
  swapped in for one of the plain `str` fields above, joins the samples unread. Enforcing
  it means scanning `audit`, `client` and `tool_registry` for `Literal`s and holding the
  leftovers against a named exclusion list, which is a second unenforced list in place of
  this sentence;
- a record written outside a `json` fence — fenced as ```text, or in prose. The fence rule
  reaches what claims to be JSON; nothing makes a record claim it. One fence carries one
  object, which is every sample the document has, so a fence holding several records on
  several lines would fail as malformed rather than being read line by line;
- the dangling direction for the samples' `kind`. The document restates no `kind`
  vocabulary anywhere, so it has no second passage of the kind that lets `effect`,
  `decision` and the connection states be checked for coverage. Add an arm to
  `TARGET_KINDS` and the samples stay silently right, showing one of however many there
  now are;
- the sample's `attribution.source` value on the `ownership-override` record,
  `config:agent_id`, which restates no exported constant. Its counterpart on the
  `authorization` record is held to `audit.ATTRIBUTION_ENV` in the sample itself, because
  the constant check below is satisfied by the prose under the record and would stay green
  while the sample drifted;
- a `source` written anywhere in a sample but the top level of the TLS record. The
  extractor is scoped there because `attribution.source` under the same key names where an
  identity claim came from instead, and holding it to the TLS vocabulary would redden on a
  document that is right. A future event carrying a top-level `source` from some third
  vocabulary would go unread for the same reason;
- the sample records' editing constraint, which unlike the TLS passage's carries no editor
  marker in the document. It needs none in the same way: the failure names the offending
  block and quotes it, so an editor who adds a non-record `json` fence is told what is
  wrong rather than pointed at a regex whose name means nothing to them;
- the editing constraint this guard puts on the two documents. Both TLS passages must keep
  the `SOURCE_CLAUSE` wording in order, with or without `effective` — the
  environment-variable note writes no `source` identifier to anchor on, so ordinary prose
  is the only anchor available — and that note must stay one `- **` bullet. The regex is
  the authority; this bullet pins no count, for the reason the paragraph above gives. Line
  breaks within the clause do not matter, because `_tls_passage` collapses whitespace, so
  re-wrapping either paragraph is safe. Every violation fails loud, but it fails naming a
  regex, and an editor of a settings document has no reason to open a test named for a
  different one. Both documents now carry an editor marker saying so —
  `docs/authorization-audit.md`'s since #497, the settings document's since #504 — and
  each marker's quoted phrase is derived from `SOURCE_CLAUSE`, so it cannot be deleted,
  moved away from its passage, or left behind by a rewording without reddening a check.
  The marker is a pointer, not a guard: it tells an editor where the rule lives;
- the module half of the two code pointers. `_alias_name` finds the alias wherever in
  `client` it is bound, so a rename reddens — but both docstrings write the dotted path
  `hmcpctl.client.core.VerifySSLSource` in prose, and moving the alias to another module would
  leave that prefix wrong behind a green run;
- a third pointer, the `:data:` reference in `client._verify_ssl_source`'s own docstring,
  which nothing here reads. A rename forces an edit to that function's return annotation
  two lines away, so the rename is not silent — but the docstring is prose beside it and
  can be left behind, exactly as the two above could before #504;
- a *paraphrase* of the vocabulary in either code docstring. The check below compares
  against the members themselves, so "the explicit argument, the environment variable or
  the field default" passes it. What it closes is the restatement that goes stale on a
  rename; a description of the same shape in ordinary words is not reached, for the same
  reason the first bullet gives about the documents.

The environment-variable restatement is kept rather than replaced by a cross-reference,
which would have deleted half this machinery. It is where an operator deciding whether to
leave verification off actually reads, and #497's fourth criterion asked for it to be
covered. Guarding it is the price of leaving it there, and that price is this ledger.

Its check is filed here rather than in `tests/test_env_var_guard.py`, which is the module
an editor of that document already opens. Weighed and chosen: this is one vocabulary with
one extractor, and splitting the two arms across modules would put half a set comparison
in each and leave the ledger describing coverage that lives somewhere else. The
discoverability cost is what that document's editor marker pays down.
"""

import ast
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Literal, get_args, get_origin, get_type_hints

import pytest

from hmcpctl import tool_registry
from hmcpctl.audit import records as audit
from hmcpctl.client import core as client

ROOT = Path(__file__).parents[1]
DOCUMENT = ROOT / "docs" / "authorization-audit.md"
ENVIRONMENT_DOCUMENT = ROOT / "docs" / "environment-variables.md"
AUDIT_MODULE = ROOT / "src" / "hmcpctl" / "audit" / "records.py"
AUDIT_TEST = ROOT / "tests" / "unit" / "test_audit.py"
DISPATCH_MODULE = ROOT / "src" / "hmcpctl" / "authorization" / "dispatch_scope.py"

#: One reason-code table row: the code it names and the decision that code yields. Both
#: cells are read by one pattern, so a row whose decision cell stops being a bare token
#: drops out of the reason vocabulary too and fails loud rather than going unread (#518).
REASON_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([a-z][a-z-]*)\s*\|", re.MULTILINE)
EVENT_HEADING = re.compile(r'^### `event: "([^"]+)"`\s*$', re.MULTILINE)

#: The field-table rows that restate a closed vocabulary, keyed by the field each names.
#: Every such cell lists its members individually backticked, so one extractor serves them
#: all rather than a copy per field, and a vocabulary joining a table needs no new regex
#: (#518). `effect` and `decision` sit in the `authorization` table; `operation` and
#: `denial` are the `ownership-denied` record's own (#467, ADR 0100 §5). `FIELD_ROW`
#: scans the whole document and `_field_row_cell` requires exactly one match, so a field
#: name reused as a row in a second table would fail loud rather than pick one.
FIELD_ROW_VOCABULARIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("effect", tool_registry.EFFECTS),
    ("decision", audit.DECISIONS),
    ("operation", audit.OWNERSHIP_OPERATIONS),
    ("denial", audit.OWNERSHIP_DENIALS),
)
FIELD_ROW_FIELDS = [field for field, _ in FIELD_ROW_VOCABULARIES]
FIELD_ROW = {
    field: re.compile(rf"^\|\s*`{re.escape(field)}`\s*\|([^|]*)\|", re.MULTILINE)
    for field, _ in FIELD_ROW_VOCABULARIES
}
#: One member of such a cell. Quoted or bare, because the `decision` row writes its
#: members as the JSON literals `"allow"` and `"deny"` while the `effect` row writes bare
#: names; holding both spellings is what lets one extractor read either row unedited. The
#: quotes must balance — an optional one on each end would read `"allow` as a member and
#: leave a cell with a dropped quote green.
FIELD_VALUE = r'`(?:"[a-z][a-z-]*"|[a-z][a-z-]*)`'
#: The comma-and-`or` run at the head of that cell, so a clarification appended after it
#: is prose rather than another member. Same reason as STATE_ARM below. A bare `or` with
#: no comma is a separator too — a two-member run is written "`x` or `y`" — while bare
#: whitespace is not, which is what keeps a backticked term following the run outside it.
FIELD_VALUE_LIST = re.compile(
    rf"{FIELD_VALUE}(?:(?:,\s*(?:or\s+)?|\s+or\s+){FIELD_VALUE})*"
)
STATE_SENTENCE = re.compile(r"`connection\.state` is ([^.]*)\.")
#: One arm of that sentence. Anchored on the `x` when … shape rather than taking every
#: backticked token, so an unrelated term added to the sentence is not read as a state.
STATE_ARM = re.compile(r"`([a-z-]+)`\s+when\b")
BACKTICKED = re.compile(r"`([^`]+)`")

#: Each document's editor marker is one of these and quotes the anchor phrase, so
#: comments come out of a passage before the clause is read.
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
TLS_HEADING = '### `event: "tls-verification-disabled"`'
TLS_BULLET = "- **TLS verification** (`HMC_VERIFY_SSL`):"
#: The TLS record's own event name, taken from the heading above rather than spelled a
#: second time — the same reason `_alias_name` finds its name instead of writing it. A
#: heading reworded until `EVENT_HEADING` no longer reads it fails at import, and
#: `test_the_tls_event_name_is_one_the_audit_module_defines` holds the other end.
TLS_EVENT = EVENT_HEADING.findall(TLS_HEADING)[0]

#: One fenced JSON sample record, as the audit document writes them: a ```json fence
#: around a single object. The block's own text is what gets parsed, so the check reads
#: exactly the characters a consumer copies. An info string after the language is allowed
#: — several renderers take one — while `jsonc` and the like are not this fence, and a
#: document whose fences are all renamed out of reach fails rather than reading nothing.
JSON_SAMPLE = re.compile(r"^```json\b[^\n]*\n(.*?)^```$", re.MULTILINE | re.DOTALL)
#: Anything that opens a JSON block, wherever and however it is written — indented, inside
#: a blockquote, tilde-fenced, over-long, capitalised, or retagged to a `json` dialect.
#: `JSON_SAMPLE` reads only the plain column-0 triple-backtick form the document uses;
#: counting both is what stops a sample written any other way from being skipped in silence
#: instead of held to the checks below. Horizontal whitespace only: `\s` would match the
#: newline before the fence and put the reported line number one line early.
JSON_FENCE = re.compile(
    r"^[ \t]*(?:>[ \t]*)*(?:`{3,}|~{3,})[ \t]*json[a-z0-9]*\b",
    re.MULTILINE | re.IGNORECASE,
)

#: The record builder's `Literal`-annotated parameters, and the frozenset each of those
#: vocabularies is derived from. Re-inlining a `Literal` in either signature — which is
#: how `decision` was written before #518 — would leave the frozenset, and every check
#: that reads it, describing a vocabulary the builder no longer accepts.
BUILDER_VOCABULARIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("decision", audit.DECISIONS),
    ("reason", audit.REASONS),
)
BUILDER_PARAMETERS = [parameter for parameter, _ in BUILDER_VOCABULARIES]

#: The `Literal`-derived vocabularies a sample record draws its own values from, keyed by
#: the JSON key each sits at. Read by key at any depth, because `state` is nested twice —
#: under `connection` and under every `targets` entry — and `kind` once, under `targets`.
#: `source` is deliberately absent: the key names one vocabulary at the top level of the
#: TLS record and another inside `attribution`, so it is scoped by hand below.
SAMPLE_VOCABULARIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("event", audit.EVENTS),
    ("reason", audit.REASONS),
    ("effect", tool_registry.EFFECTS),
    ("state", frozenset(get_args(audit.State))),
    ("kind", tool_registry.TARGET_KINDS),
    ("decision", audit.DECISIONS),
)
SAMPLE_KEYS = [key for key, _ in SAMPLE_VOCABULARIES]

#: The passage describing the TLS record in each document that restates its `source`
#: values: the event's own section in the audit document, the `HMC_VERIFY_SSL` note in the
#: environment-variable one. Anchored on the record's own name, as every sibling extractor
#: anchors on something structurally unique to its vocabulary — the clause below is
#: ordinary English about settings, and `docs/environment-variables.md` documents nothing
#: but settings, so scanning that document whole would let an unrelated note collide.
TLS_PASSAGE = {
    "authorization-audit.md": re.compile(
        rf"^{re.escape(TLS_HEADING)}$.*?(?=^#{{2,6}} )",
        re.MULTILINE | re.DOTALL,
    ),
    "environment-variables.md": re.compile(
        rf"^{re.escape(TLS_BULLET)}.*?(?=^- \*\*|\Z)",
        re.MULTILINE | re.DOTALL,
    ),
}
#: What each document's editor marker sits immediately above: the record's own heading in
#: the audit document, the `HMC_VERIFY_SSL` note's bullet in the environment-variable one.
#: The same string the passage above is anchored on, so a marker cannot end up adjacent to
#: something other than the passage it warns about.
TLS_ANCHOR = {
    "authorization-audit.md": TLS_HEADING,
    "environment-variables.md": TLS_BULLET,
}
#: The clause both passages introduce the `source` values with. Anchored on the wording
#: rather than the punctuation, which differs between them: em dashes in the audit
#: document, parentheses in the environment-variable note. Single spaces are enough
#: because `_tls_passage` has already collapsed the passage's line breaks; both documents
#: are hard-wrapped prose, so where a break lands inside the clause is an accident of the
#: surrounding paragraph rather than anything an editor chose.
SOURCE_CLAUSE = re.compile(r"where the (?:effective )?setting came from\b")
#: One `source` value: a lowercase hyphenated name, optionally suffixed with the
#: environment variable it names, as `environment:HMC_VERIFY_SSL` is.
SOURCE_VALUE = r"[a-z][a-z-]*(?::[A-Z][A-Z0-9_]*)?"
#: The comma-and-`or` run of them, as FIELD_VALUE_LIST is for a field row's cell — so a
#: clarification appended after the run is prose rather than a fourth source. The two
#: differ in one place: this one takes `or` only after a comma, because the clause it
#: reads always lists more than two.
SOURCE_LIST = re.compile(rf"`{SOURCE_VALUE}`(?:,\s*(?:or\s+)?`{SOURCE_VALUE}`)*")

#: A count — spelled or numeric, bold or plain — pinned to the reason-code
#: vocabulary. One filler word is allowed between ("seven reason codes"); two are
#: not, so the countless phrasing "one of the codes below" reads clean.
COUNT_WORD = (
    r"(?:\*{2})?(?:\d[\d,]*|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|eleven|twelve)(?:\*{2})?"
)
FIXED_REASON_COUNT = re.compile(
    rf"\b{COUNT_WORD}\s+(?:[\w-]+\s+)?codes?\b", re.IGNORECASE
)

#: The same rule for the record and event vocabulary. Only plural counts, because
#: "one record per client construction" is a rate and stays true however many kinds
#: of record exist, while "the two records" and "both records" are the enumeration
#: that goes stale the next time ``audit.Event`` grows — as it did.
PLURAL_COUNT = (
    r"(?:\*{2})?(?:both|[2-9]|\d\d[\d,]*|two|three|four|five|six|seven|eight|nine"
    r"|ten|eleven|twelve)(?:\*{2})?"
)
FIXED_KIND_COUNT = re.compile(
    rf"\b{PLURAL_COUNT}\s+(?:[\w-]+\s+)?(?:records?|events?)\b", re.IGNORECASE
)

#: The same count in pronoun form, which names no noun for the pattern above to anchor
#: on. "Both are one physical line of ASCII JSON" is how the other half of this
#: document's drift was written, and it is why this is a separate pattern: it is safe
#: only in the lead paragraph that introduces the record kinds, where the whole subject
#: is how many there are.
PRONOUN_COUNT = re.compile(r"\b(?:both|neither|either)\b", re.IGNORECASE)


def _document() -> str:
    return DOCUMENT.read_text()


def _section(document: str, heading: str) -> str:
    """The body under a `##` heading, up to the next one."""
    marker = f"\n{heading}\n"
    assert marker in document, f"missing section: {heading}"
    return document.split(marker, 1)[1].split("\n## ", 1)[0]


def _reason_rows(document: str) -> list[tuple[str, str]]:
    """Every reason-code table row as `(code, decision)`, minus its separator row."""
    return REASON_ROW.findall(_section(document, "## Reason codes"))


def _documented_reasons(document: str) -> frozenset[str]:
    """The first column of the reason-code table."""
    return frozenset(code for code, _ in _reason_rows(document))


def _documented_reason_decisions(document: str) -> frozenset[str]:
    """The decision each documented reason code is written as yielding."""
    return frozenset(decision for _, decision in _reason_rows(document))


def _documented_events(document: str) -> frozenset[str]:
    """Scoped to the section the document's own intro calls the full set."""
    return frozenset(EVENT_HEADING.findall(_section(document, "## The records")))


def _field_row_cell(document: str, field: str) -> str:
    """The value cell of the authorization field table's *field* row."""
    rows = FIELD_ROW[field].findall(document)
    assert len(rows) == 1, f"expected one `{field}` field row, found {len(rows)}"
    return rows[0]


def _documented_field_values(document: str, field: str) -> frozenset[str]:
    """That cell's members, which are backticked individually.

    Quotes come off, so the `decision` row's JSON spelling `"allow"` compares against the
    vocabulary member `allow` — the members themselves never contain one.
    """
    cell = _field_row_cell(document, field)
    listing = FIELD_VALUE_LIST.search(cell)
    assert listing is not None, f"no `{field}` list in cell: {cell!r}"
    return frozenset(value.strip('"') for value in BACKTICKED.findall(listing.group(0)))


def _records_lead(document: str) -> str:
    """The paragraph introducing the record kinds, before the first one's section."""
    return _section(document, "## The records").split("\n### ", 1)[0]


def _tls_marker(document: str, name: str) -> str:
    """The editor marker: the HTML comment immediately above *document*'s TLS anchor.

    Identified by where it sits rather than by document order. Its adjacency to the
    passage is the whole mechanism — a marker relocated to the top of the document warns
    nobody — so an unrelated comment elsewhere neither stands in for it nor is mistaken
    for it.
    """
    anchor = TLS_ANCHOR[name]
    head, separator, _ = document.partition(f"\n{anchor}")
    assert separator, f"missing anchor in {name}: {anchor}"
    preceding = head.rstrip()
    assert preceding.endswith("-->"), f"no editor marker above {anchor} in {name}"
    return preceding[preceding.rindex("<!--") :]


def _tls_passage(document: str, name: str) -> str:
    """The passage of *document* describing the TLS record, where the `source` clause lives.

    HTML comments are dropped first: the editor marker quotes the anchor phrase, so a
    marker moved inside the section it describes — which is where its own wording points —
    would otherwise be read as a second clause. Then whitespace is collapsed, so no
    pattern below can depend on where a hard wrap happens to fall.
    """
    match = TLS_PASSAGE[name].search(document)
    assert match is not None, f"no TLS record passage in {name}"
    return re.sub(r"\s+", " ", HTML_COMMENT.sub("", match.group(0)))


def _source_sentence(passage: str) -> str:
    """The sentence naming the TLS record's `source` values.

    Bounded to that one sentence, so a passage carrying the clause but no list fails
    rather than reaching forward to unrelated backticks. No `source` value contains a
    period, which is what makes the sentence end a safe bound.
    """
    clauses = SOURCE_CLAUSE.findall(passage)
    assert len(clauses) == 1, f"expected one `source` clause, found {len(clauses)}"
    clause = SOURCE_CLAUSE.search(passage)
    assert clause is not None
    return passage[clause.end() :].split(".", 1)[0]


def _source_list(passage: str) -> str:
    """The comma-and-`or` run within that sentence."""
    sentence = _source_sentence(passage)
    listing = SOURCE_LIST.search(sentence)
    assert listing is not None, f"no source list in clause: {sentence!r}"
    return listing.group(0)


def _documented_sources(passage: str) -> frozenset[str]:
    return frozenset(BACKTICKED.findall(_source_list(passage)))


def _documented_states(document: str) -> frozenset[str]:
    """The `connection.state` arms, which one sentence enumerates."""
    sentences = STATE_SENTENCE.findall(document)
    assert len(sentences) == 1, f"expected one state sentence, found {len(sentences)}"
    return frozenset(STATE_ARM.findall(sentences[0]))


def _json_fences(document: str) -> list[str]:
    """Every JSON-block opener in *document*, as `line N: <the whole line>`.

    The whole line rather than the matched prefix, because what makes a fence unreadable —
    a capital, a tilde, a fourth backtick, a `json5` tag — is the part an editor has to
    see. This is the only report the fence rule gives, there being no editor marker.
    """
    fences = []
    for match in JSON_FENCE.finditer(document):
        number = document.count("\n", 0, match.start()) + 1
        line = document[match.start() :].split("\n", 1)[0]
        fences.append(f"line {number}: {line}")
    return fences


def _sample_records(document: str) -> tuple[dict[str, object], ...]:
    """Every fenced JSON sample in *document*, parsed.

    Each block must parse and must carry an `event`. Both are the point rather than
    incidental strictness: a sample that stopped being valid JSON has stopped being
    copy-pasteable, and a block with no `event` is a sample every check below would
    silently skip. A `json` fence added for something other than a record fails here,
    quoting the block, which is the editing constraint the ledger records.
    """
    rule = (
        "every ```json fence in the document must open one audit record, written at the "
        "start of its line; fence anything else as ```text"
    )
    blocks = JSON_SAMPLE.findall(document)
    assert blocks, f"no fenced JSON sample records in the document — {rule}"
    opened = _json_fences(document)
    assert len(opened) == len(blocks), (
        f"{len(opened)} JSON fences open but {len(blocks)} were read — {rule}. "
        f"Fences found: {opened}"
    )

    records = []
    for block in blocks:
        try:
            record = json.loads(block)
        except json.JSONDecodeError as error:
            raise AssertionError(
                f"sample is not valid JSON — {rule}: {block!r}"
            ) from error
        assert isinstance(record, dict), (
            f"sample is not a JSON object — {rule}: {block!r}"
        )
        assert "event" in record, f"sample carries no `event` — {rule}: {block!r}"
        records.append(record)
    return tuple(records)


def _values_at(node: object, key: str) -> Iterator[str]:
    """Every string *node* carries under *key*, at any depth."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key and isinstance(value, str):
                yield value
            yield from _values_at(value, key)
    elif isinstance(node, list):
        for item in node:
            yield from _values_at(item, key)


def _sampled_values(document: str, key: str) -> frozenset[str]:
    """What the sample records name under *key*, across every block, at any depth."""
    return frozenset(
        value
        for record in _sample_records(document)
        for value in _values_at(record, key)
    )


def _sampled_events(document: str) -> frozenset[str]:
    """The `event` each sample record names, read at the top level only.

    Coverage is a claim about records, so it reads the key `_sample_records` has already
    made every record carry. `_sampled_values` reads `event` at any depth, which is right
    for the orphan direction — a nested one must still name something defined — and wrong
    here, where a nested value would stand in for the record that is missing.
    """
    return frozenset(str(record["event"]) for record in _sample_records(document))


def _sampled_tls_sources(document: str) -> frozenset[str]:
    """The top-level `source` of every TLS sample record.

    Top-level and scoped to that event on purpose — see the ledger: `attribution.source`
    is the same key naming a different vocabulary, so reading `source` wherever it appears
    would fail on a document that is correct.
    """
    records = [r for r in _sample_records(document) if r.get("event") == TLS_EVENT]
    assert records, f"no sample record for `{TLS_EVENT}`"

    sources = []
    for record in records:
        source = record.get("source")
        assert isinstance(source, str), f"sample carries no `source`: {record}"
        sources.append(source)
    return frozenset(sources)


def test_documented_reason_codes_are_exactly_the_audit_vocabulary() -> None:
    #: The message names the second way this fails, which the comparison cannot show: a
    #: row is read only when its code is backticked *and* its decision cell is a bare
    #: lowercase token, so a cosmetic edit to that cell arrives here as a missing code.
    assert _documented_reasons(_document()) == audit.REASONS, (
        "reason-code table drift, or a row `REASON_ROW` could not read — a decision "
        "cell widened into prose takes its whole row out of this comparison"
    )


def test_reason_code_drift_is_caught_in_both_directions() -> None:
    document = _document()
    dangling = min(audit.REASONS)

    undocumented = document.replace(f"| `{dangling}` |", "| removed |", 1)
    assert undocumented != document
    assert audit.REASONS - _documented_reasons(undocumented) == {dangling}

    orphaned = document.replace(
        "## Reason codes\n",
        "## Reason codes\n\n| `retired-code` | deny | no longer defined |\n",
        1,
    )
    assert orphaned != document
    assert _documented_reasons(orphaned) - audit.REASONS == {"retired-code"}


def test_reason_code_table_is_scoped_to_its_own_section() -> None:
    """Field-table rows are backticked too; the extractor must not collect them."""
    document = _document()

    assert "| `time` |" in document
    assert "time" not in _documented_reasons(document)
    assert "code" not in _documented_reasons(document)


def test_documented_event_sections_are_exactly_the_audit_vocabulary() -> None:
    assert _documented_events(_document()) == audit.EVENTS


def test_event_drift_is_caught_in_both_directions() -> None:
    document = _document()
    dangling = min(audit.EVENTS)

    undocumented = document.replace(f'### `event: "{dangling}"`', "### removed", 1)
    assert undocumented != document
    assert audit.EVENTS - _documented_events(undocumented) == {dangling}

    orphaned = document.replace(
        "## The records\n", '## The records\n\n### `event: "retired-event"`\n', 1
    )
    assert orphaned != document
    assert _documented_events(orphaned) - audit.EVENTS == {"retired-event"}


def test_an_event_section_outside_the_records_section_is_not_counted() -> None:
    """The intro calls that section the full set; the guard holds it to that."""
    document = _document()
    stray = document + '\n### `event: "elsewhere"`\n'

    assert stray != document
    assert "elsewhere" not in _documented_events(stray)


def test_event_names_are_read_from_headings_only() -> None:
    """Every event name is also a table cell and a JSON value; only headings count."""
    document = _document()
    headings = EVENT_HEADING.findall(document)

    assert len(headings) == len(set(headings)), f"duplicate headings: {headings}"

    in_json = document.replace('"event":"authorization"', '"event":"not-a-heading"', 1)
    assert in_json != document
    assert "not-a-heading" not in _documented_events(in_json)

    in_table = document.replace(
        '| `event` | `"authorization"` |', '| `event` | `"not-a-row"` |', 1
    )
    assert in_table != document
    assert "not-a-row" not in _documented_events(in_table)


@pytest.mark.parametrize(
    ("field", "vocabulary"), FIELD_ROW_VOCABULARIES, ids=FIELD_ROW_FIELDS
)
def test_documented_field_row_values_are_exactly_their_vocabulary(
    field: str, vocabulary: frozenset[str]
) -> None:
    assert _documented_field_values(_document(), field) == vocabulary


@pytest.mark.parametrize(
    ("field", "vocabulary"), FIELD_ROW_VOCABULARIES, ids=FIELD_ROW_FIELDS
)
def test_field_row_drift_is_caught_in_both_directions(
    field: str, vocabulary: frozenset[str]
) -> None:
    document = _document()
    cell = _field_row_cell(document, field)
    dangling = min(vocabulary)

    #: Un-backticked in place, whichever spelling the cell uses — the run ends at the
    #: first member it no longer recognises, so the rest of the cell still reads.
    drifted, replaced = re.subn(
        rf'`("?){re.escape(dangling)}\1`', dangling, cell, count=1
    )
    assert replaced == 1
    undocumented = document.replace(cell, drifted, 1)
    assert undocumented != document
    assert vocabulary - _documented_field_values(undocumented, field) == {dangling}

    #: Inserted after the first member rather than appended to the cell, so the mutation
    #: lands inside the run wherever the run ends. Appending would be read as prose on a
    #: cell that already carries a trailing clarification — which is a spelling the
    #: extractor accepts, per the control below.
    first = re.search(FIELD_VALUE, cell)
    assert first is not None
    orphaned_cell = cell.replace(
        first.group(0), f"{first.group(0)}, `retired-{field}`", 1
    )
    orphaned = document.replace(cell, orphaned_cell, 1)
    assert orphaned != document
    expected = {f"retired-{field}"}
    assert _documented_field_values(orphaned, field) - vocabulary == expected


@pytest.mark.parametrize(
    ("field", "vocabulary"), FIELD_ROW_VOCABULARIES, ids=FIELD_ROW_FIELDS
)
def test_field_row_values_survive_an_unrelated_backticked_term(
    field: str, vocabulary: frozenset[str]
) -> None:
    """As for the state arms: a clarification in the cell is prose, not a member."""
    document = _document()
    cell = _field_row_cell(document, field)

    reworded = document.replace(cell, f"{cell.rstrip()} (`{field}` names it) ", 1)
    assert reworded != document
    assert _documented_field_values(reworded, field) == vocabulary


@pytest.mark.parametrize(
    ("field", "vocabulary"), FIELD_ROW_VOCABULARIES, ids=FIELD_ROW_FIELDS
)
def test_field_row_values_survive_an_unrelated_edit_elsewhere(
    field: str, vocabulary: frozenset[str]
) -> None:
    """The control the two mutation rows above are measured against.

    Without it they show only that *something* moves the extractor, not that the cell is
    what moves it.
    """
    document = _document()

    reworded = document.replace("\n\n", "\n\nAn unrelated new paragraph.\n\n", 1)
    assert reworded != document
    assert _documented_field_values(reworded, field) == vocabulary


def test_the_reason_table_names_no_undefined_decision() -> None:
    """The orphan direction for the reason table's middle column.

    Only that direction — see the ledger. The field-table row above holds the other half,
    and requiring every decision to appear in this column would pin a mapping the code
    does not make.
    """
    decisions = _documented_reason_decisions(_document())

    assert decisions, "the reason table names no decision, so this proves nothing"
    assert decisions <= audit.DECISIONS


def _reason_row_text(document: str, index: int = 0) -> tuple[str, str, str]:
    """One reason-code row as written, with the code and decision it was read as.

    Located with the extractor's own tolerance for padding rather than by reconstructing
    the row, so the mutations below cannot silently edit nothing and do not pin the
    table's alignment. The whole matched text comes back, because that is what they
    rewrite.
    """
    code, decision = _reason_rows(document)[index]
    written = re.search(
        rf"^\|\s*`{re.escape(code)}`\s*\|\s*{re.escape(decision)}\s*\|",
        document,
        re.MULTILINE,
    )
    assert written is not None, f"reason row not found for `{code}`"
    return code, decision, written.group(0)


def test_a_drifted_reason_table_decision_is_caught() -> None:
    document = _document()
    code, _decision, written = _reason_row_text(document)

    drifted = document.replace(written, f"| `{code}` | retired-decision |", 1)
    assert drifted != document
    expected = {"retired-decision"}
    assert _documented_reason_decisions(drifted) - audit.DECISIONS == expected


def test_a_reason_row_whose_decision_cell_is_prose_fails_loud() -> None:
    """Why one pattern reads both cells: the middle column carries no backticks of its
    own to anchor on, so a cell widened into prose would simply stop being read. Reading
    it alongside the code takes the whole row out of the reason vocabulary instead, which
    the equality check above fails on.
    """
    document = _document()
    code, decision, written = _reason_row_text(document)

    reworded = document.replace(written, f"| `{code}` | {decision} (see below) |", 1)
    assert reworded != document
    assert audit.REASONS - _documented_reasons(reworded) == {code}


def test_documented_connection_states_are_exactly_the_audit_vocabulary() -> None:
    assert _documented_states(_document()) == frozenset(get_args(audit.State))


def test_connection_state_drift_is_caught_in_both_directions() -> None:
    document = _document()
    states = frozenset(get_args(audit.State))
    dangling = min(states)
    sentence = STATE_SENTENCE.search(document)
    assert sentence is not None

    undocumented = document.replace(
        sentence.group(1), sentence.group(1).replace(f"`{dangling}`", dangling, 1), 1
    )
    assert undocumented != document
    assert states - _documented_states(undocumented) == {dangling}

    orphaned = document.replace(
        sentence.group(1),
        f"{sentence.group(1)}, and `retired-state` when nothing else applies",
        1,
    )
    assert orphaned != document
    assert _documented_states(orphaned) - states == {"retired-state"}


def test_state_arms_survive_an_unrelated_backticked_term() -> None:
    """The arms are read structurally, so editing the sentence's prose is safe."""
    document = _document()
    sentence = STATE_SENTENCE.search(document)
    assert sentence is not None

    reworded = document.replace(
        sentence.group(1), f"{sentence.group(1)}, such as `bytes`", 1
    )
    assert reworded != document
    assert _documented_states(reworded) == frozenset(get_args(audit.State))


#: Both restatements of the TLS record's `source` field. The environment-variable note
#: describes the same record, so it is held to the same set rather than left to drift.
SOURCE_DOCUMENTS = (DOCUMENT, ENVIRONMENT_DOCUMENT)


@pytest.mark.parametrize("path", SOURCE_DOCUMENTS, ids=lambda path: path.name)
def test_documented_tls_sources_are_exactly_the_client_vocabulary(path: Path) -> None:
    assert (
        _documented_sources(_tls_passage(path.read_text(), path.name))
        == client.VERIFY_SSL_SOURCES
    )


@pytest.mark.parametrize("path", SOURCE_DOCUMENTS, ids=lambda path: path.name)
def test_tls_source_drift_is_caught_in_both_directions(path: Path) -> None:
    passage = _tls_passage(path.read_text(), path.name)
    listing = _source_list(passage)
    #: The first value listed, which is never the last one — so dropping it and the
    #: separator that follows leaves a run the extractor still reads.
    dangling = BACKTICKED.findall(listing)[0]

    undocumented = re.sub(rf"`{re.escape(dangling)}`,\s*", "", passage, count=1)
    assert undocumented != passage
    assert client.VERIFY_SSL_SOURCES - _documented_sources(undocumented) == {dangling}

    orphaned = passage.replace(listing, f"{listing}, or `retired-source`", 1)
    assert orphaned != passage
    expected = {"retired-source"}
    assert _documented_sources(orphaned) - client.VERIFY_SSL_SOURCES == expected


@pytest.mark.parametrize("path", SOURCE_DOCUMENTS, ids=lambda path: path.name)
def test_tls_sources_survive_an_unrelated_backticked_term(path: Path) -> None:
    """As for the effects and the state arms: a term after the run is prose, not a source.

    The added term is itself list-shaped, so what excludes it is the run's own
    comma-and-`or` anchoring rather than the value pattern.
    """
    passage = _tls_passage(path.read_text(), path.name)
    sentence = _source_sentence(passage)

    reworded = passage.replace(
        sentence, f"{sentence}, and `verify-ssl` is the field it names", 1
    )
    assert reworded != passage
    assert _documented_sources(reworded) == client.VERIFY_SSL_SOURCES


@pytest.mark.parametrize("path", SOURCE_DOCUMENTS, ids=lambda path: path.name)
def test_the_editor_marker_quotes_the_anchor_it_names(path: Path) -> None:
    """The marker is the only warning an editor of the document gets, so it is derived.

    Deleting it, moving it away from the passage it warns about, or letting its quoted
    phrase drift from `SOURCE_CLAUSE` reddens here, rather than leaving the guard's one
    piece of documentation silently wrong or somewhere nobody meets it. Both documents
    carry one, because the environment-variable note is the more likely of the two to be
    edited by somebody who has never opened this module (#504).
    """
    quoted = re.findall(r'"([^"]+)"', _tls_marker(path.read_text(), path.name))

    assert quoted, f"the editor marker in {path.name} quotes no phrase"
    assert any(SOURCE_CLAUSE.fullmatch(phrase) for phrase in quoted), quoted


@pytest.mark.parametrize("path", SOURCE_DOCUMENTS, ids=lambda path: path.name)
def test_a_marker_moved_inside_the_passage_is_not_read_as_a_clause(path: Path) -> None:
    """Its own wording says "the values below", so inside the passage is where it lands."""
    document = path.read_text()
    marker = _tls_marker(document, path.name)
    anchor = TLS_ANCHOR[path.name]

    moved = document.replace(f"{marker}\n{anchor}", f"{anchor}\n{marker}", 1)
    assert moved != document
    assert _documented_sources(_tls_passage(moved, path.name)) == (
        client.VERIFY_SSL_SOURCES
    )


@pytest.mark.parametrize("path", SOURCE_DOCUMENTS, ids=lambda path: path.name)
def test_an_unrelated_comment_does_not_stand_in_for_the_marker(path: Path) -> None:
    """Adjacency is the mechanism, so a comment anywhere else is not the marker."""
    document = path.read_text()
    marker = _tls_marker(document, path.name)

    elsewhere = f"<!-- an unrelated note -->\n{document}"
    assert _tls_marker(elsewhere, path.name) == marker

    removed = document.replace(f"{marker}\n", "", 1)
    assert removed != document
    with pytest.raises(AssertionError, match="no editor marker"):
        _tls_marker(f"<!-- an unrelated note -->\n{removed}", path.name)


def test_the_tls_event_name_is_one_the_audit_module_defines() -> None:
    """`TLS_EVENT` is derived from the heading constant; this holds its other end."""
    assert TLS_EVENT in audit.EVENTS


def test_every_event_has_a_sample_record_and_the_samples_name_no_other() -> None:
    """Equality, unlike the vocabularies below: one section per event and at least one
    sample apiece, which is what makes coverage checkable. More than one is fine — the
    `records-dropped` record is sampled again later in the document — because this
    compares sets.
    """
    assert _sampled_events(_document()) == audit.EVENTS


def test_every_event_section_carries_a_sample_of_its_own() -> None:
    """Scoped to the section the intro calls the full set, as the heading check is.

    Document-wide coverage alone would let an event sampled twice lose the sample in its
    own section and stay covered by the other copy — which is the sample a reader of that
    section actually meets.
    """
    assert _sampled_events(_section(_document(), "## The records")) == audit.EVENTS


@pytest.mark.parametrize(("key", "vocabulary"), SAMPLE_VOCABULARIES, ids=SAMPLE_KEYS)
def test_sample_record_values_are_drawn_from_their_vocabulary(
    key: str, vocabulary: frozenset[str]
) -> None:
    """The orphan direction. One record shows one `reason`, one `effect`, one `state`, so
    coverage is not checkable here and is what the table, the row and the sentence above
    are for. `event` is the exception and is held both ways just above.
    """
    assert _sampled_values(_document(), key) <= vocabulary


@pytest.mark.parametrize(("key", "vocabulary"), SAMPLE_VOCABULARIES, ids=SAMPLE_KEYS)
def test_a_drifted_sample_value_is_caught(key: str, vocabulary: frozenset[str]) -> None:
    """Rename a vocabulary member without touching the sample and this is what happens."""
    document = _document()
    sampled = sorted(_sampled_values(document, key))
    assert sampled, f"no sample record names a `{key}`, so this row proves nothing"
    real = sampled[0]

    drifted, replaced = re.subn(
        rf'"{key}"\s*:\s*"{re.escape(real)}"',
        f'"{key}": "not-a-{key}"',
        document,
        count=1,
    )
    assert replaced == 1
    assert _sampled_values(drifted, key) - vocabulary == {f"not-a-{key}"}


def test_an_event_with_no_sample_record_is_caught() -> None:
    """The dangling direction: add an event and its section, forget its sample.

    Retargeted at an event that already has one rather than deleted, so the document under
    test still parses and still names nothing undefined — the coverage half fails alone.
    """
    document = _document()
    sampled = Counter(str(record["event"]) for record in _sample_records(document))
    once = sorted(name for name, count in sampled.items() if count == 1)
    assert once, f"every event is sampled more than once: {sampled}"
    dangling = str(once[0])
    others = sorted(str(name) for name in sampled if name != dangling)
    assert others, "only one event is sampled, so there is nothing to retarget at"

    uncovered, replaced = re.subn(
        rf'"event"\s*:\s*"{re.escape(dangling)}"',
        f'"event": "{others[0]}"',
        document,
        count=1,
    )
    assert replaced == 1
    assert audit.EVENTS - _sampled_events(uncovered) == {dangling}


def test_a_nested_event_does_not_cover_for_a_missing_sample_record() -> None:
    """Why coverage reads the top level: at any depth this mutation would pass.

    The same shape as the TLS `source` control — the real document carries no nested
    `event` to catch a widened extractor out, so the control has to plant one.
    """
    document = _document()
    sampled = Counter(str(record["event"]) for record in _sample_records(document))
    once = sorted(name for name, count in sampled.items() if count == 1)
    assert once, f"every event is sampled more than once: {sampled}"
    dangling = once[0]
    others = sorted(str(name) for name in sampled if name != dangling)
    assert others, "only one event is sampled, so there is nothing to retarget at"

    hidden, replaced = re.subn(
        rf'"event"\s*:\s*"{re.escape(dangling)}"',
        f'"event": "{others[0]}", "nested": {{"event": "{dangling}"}}',
        document,
        count=1,
    )
    assert replaced == 1
    #: The plant has to survive `json.loads` for the assertion below to mean anything —
    #: under a key of its own, because a duplicate one is dropped for the last wins.
    assert dangling in _sampled_values(hidden, "event")
    assert audit.EVENTS - _sampled_events(hidden) == {dangling}


def test_documented_sample_tls_sources_are_drawn_from_the_client_vocabulary() -> None:
    """Membership, not equality: one record carries one `source`. The clause above holds
    the other direction, so between them the vocabulary is covered and the sample cannot
    drift from it.
    """
    assert _sampled_tls_sources(_document()) <= client.VERIFY_SSL_SOURCES


def test_a_drifted_sample_tls_source_is_caught() -> None:
    document = _document()
    real = min(_sampled_tls_sources(document))

    drifted, replaced = re.subn(
        rf'"source"\s*:\s*"{re.escape(real)}"',
        '"source": "retired-source"',
        document,
        count=1,
    )
    assert replaced == 1
    expected = {"retired-source"}
    assert _sampled_tls_sources(drifted) - client.VERIFY_SSL_SOURCES == expected


def test_a_tls_sample_that_lost_its_source_is_caught() -> None:
    document = _document()
    real = min(_sampled_tls_sources(document))

    stripped, replaced = re.subn(
        rf',\s*"source"\s*:\s*"{re.escape(real)}"', "", document, count=1
    )
    assert replaced == 1
    with pytest.raises(AssertionError, match="carries no `source`"):
        _sampled_tls_sources(stripped)


def test_an_attribution_source_is_not_read_as_a_tls_source() -> None:
    """The control that lets the rows above mean anything.

    `attribution.source` sits under the same key and names a different vocabulary, so an
    extractor reading `source` at any depth would fail on the real document. That it does
    not is asserted against values checked to be outside the TLS vocabulary, so the
    control cannot pass by comparing two empty sets.
    """
    document = _document()
    nested = frozenset(
        source
        for record in _sample_records(document)
        for source in _values_at(record.get("attribution"), "source")
    )

    assert nested, "no attribution sources in the samples to control against"
    assert not nested & client.VERIFY_SSL_SOURCES, nested

    victim = min(nested)
    reworded, replaced = re.subn(
        rf'"source"\s*:\s*"{re.escape(victim)}"',
        '"source": "retired-source"',
        document,
        count=1,
    )
    assert replaced == 1
    assert _sampled_tls_sources(reworded) == _sampled_tls_sources(document)


def test_a_source_nested_inside_the_tls_sample_is_not_read() -> None:
    """The other half of the scoping rule, which the control above does not reach.

    That one holds "not the `attribution` record"; this holds "not at any depth", the part
    that would still pass if the extractor were widened, because the TLS sample carries no
    nested `source` of its own to catch it out.
    """
    document = _document()
    real = min(_sampled_tls_sources(document))

    nested, replaced = re.subn(
        rf'"source"\s*:\s*"{re.escape(real)}"',
        f'"source": "{real}", "nested": {{"source": "retired-source"}}',
        document,
        count=1,
    )
    assert replaced == 1
    #: As in the event control: under a key of its own, so the plant survives `json.loads`
    #: rather than being dropped as a duplicate, and the assertion below is not vacuous.
    assert "retired-source" in _sampled_values(nested, "source")
    assert _sampled_tls_sources(nested) == _sampled_tls_sources(document)


def test_renaming_every_fence_fails_rather_than_extracting_nothing() -> None:
    """The one assertion standing between this whole family and two empty sets."""
    document = _document()

    renamed = document.replace("```json", "```jsonc")
    assert renamed != document
    with pytest.raises(AssertionError, match="no fenced JSON sample"):
        _sample_records(renamed)


def test_a_fence_carrying_an_info_string_is_still_read() -> None:
    """Several renderers take one, and a sample behind it is still a sample."""
    document = _document()

    annotated = document.replace("```json\n", '```json title="sample"\n', 1)
    assert annotated != document
    assert _sampled_events(annotated) == _sampled_events(document)


@pytest.mark.parametrize(
    "fence", ["````json", "~~~json", "  ```json", "> ```json", "```JSON", "```json5"]
)
def test_a_sample_written_behind_an_unreadable_fence_is_caught(fence: str) -> None:
    """A sample the extractor cannot read must fail, not vanish from the count.

    Every one of these is a legal way to open a JSON block that `JSON_SAMPLE` does not
    take, and `records-dropped` is sampled twice — so retagging one of its blocks would
    otherwise leave the vocabulary checks green with a sample nothing had looked at.
    """
    document = _document()

    hidden = document.replace("```json", fence, 1)
    assert hidden != document
    with pytest.raises(AssertionError, match="JSON fences open"):
        _sample_records(hidden)


def test_the_fence_report_locates_every_fence_it_found() -> None:
    """The rule ships with no editor marker because the failure says where to look."""
    document = _document()
    fences = _json_fences(document)

    assert len(fences) == len(JSON_SAMPLE.findall(document))
    for fence in fences:
        number = int(fence.removeprefix("line ").split(":", 1)[0])
        line = document.splitlines()[number - 1]
        #: That the reported line is itself an opener is what bites. Equality alone passes
        #: under an off-by-one that points at a blank line, the empty string being a
        #: substring of everything and `f"line 59: "` matching a report of exactly that.
        assert JSON_FENCE.match(line), fence
        assert fence == f"line {number}: {line}"


def test_the_sampled_attribution_source_is_the_exported_environment_variable() -> None:
    """The one `attribution.source` value with a source of truth to restate.

    `test_restated_constants_are_the_exported_ones` holds the same string somewhere in the
    document, which the prose beneath the record satisfies on its own — so without this the
    sample's copy could drift while that check stayed green. The other one,
    `config:agent_id`, restates no constant and is in the ledger.
    """
    document = _document()

    assert f"environment:{audit.ATTRIBUTION_ENV}" in _sampled_values(document, "source")


def test_a_sample_that_stopped_being_valid_json_is_caught() -> None:
    """A sample is copied into a log query or an alert rule, so it has to parse."""
    document = _document()
    block = JSON_SAMPLE.findall(document)[0]

    broken = document.replace(block, f"{block.rstrip()[:-1]}\n", 1)
    assert broken != document
    with pytest.raises(AssertionError, match="not valid JSON"):
        _sample_records(broken)


def test_a_sample_that_lost_its_event_is_caught() -> None:
    """Otherwise dropping the key would exempt a record from every check above."""
    document = _document()

    dropped, replaced = re.subn(
        r'"event"\s*:\s*"[a-z-]+"\s*,\s*', "", document, count=1
    )
    assert replaced == 1
    with pytest.raises(AssertionError, match="carries no `event`"):
        _sample_records(dropped)


def test_sample_values_are_read_from_json_fences_only() -> None:
    """The counterpart to the heading rule: a record-shaped string in prose is prose."""
    document = _document()
    stray = (
        f'{document}\n{{"event":"in-prose","source":"in-prose"}}\n\n'
        '```text\n{"event":"wrong-fence","source":"wrong-fence"}\n```\n'
    )

    assert stray != document
    assert _sampled_values(stray, "event") == _sampled_values(document, "event")
    assert _sampled_tls_sources(stray) == _sampled_tls_sources(document)


def test_the_samples_survive_an_unrelated_edit_to_the_document() -> None:
    """The control the mutation rows above are measured against."""
    document = _document()

    reworded = document.replace("\n\n", "\n\nAn unrelated new paragraph.\n\n", 1)
    assert reworded != document
    for key in SAMPLE_KEYS:
        assert _sampled_values(reworded, key) == _sampled_values(document, key)
    assert _sampled_tls_sources(reworded) == _sampled_tls_sources(document)


def test_the_prose_pins_no_literal_vocabulary_count() -> None:
    document = _document()

    assert not FIXED_REASON_COUNT.search(document)
    assert not FIXED_KIND_COUNT.search(document)
    assert not PRONOUN_COUNT.search(_records_lead(document))


def test_a_pinned_reason_count_would_be_caught() -> None:
    document = _document()

    for pinned in ("seven codes", "**7** codes", "seven reason codes"):
        stale = document.replace("codes below", f"{pinned} below", 1)
        assert stale != document
        assert FIXED_REASON_COUNT.search(stale)

    assert not FIXED_REASON_COUNT.search("one of the codes below")
    assert not FIXED_REASON_COUNT.search("Treat null as not recorded for any code")


def test_a_pinned_record_count_would_be_caught() -> None:
    """The stale literal this document actually carried, and its near misses."""
    document = _document()

    for pinned in ("## The two records", "## The **4** records", "## Three events"):
        stale = document.replace("## The records", pinned, 1)
        assert stale != document
        assert FIXED_KIND_COUNT.search(stale)

    assert FIXED_KIND_COUNT.search("`DEBUG` and `INFO` keep both records")
    assert not FIXED_KIND_COUNT.search("One record per client construction")
    assert not FIXED_KIND_COUNT.search("Two other things produce no record, by design")


def test_a_pinned_count_in_pronoun_form_would_be_caught() -> None:
    """The other stale sentence named no noun: `Both are one physical line`."""
    document = _document()

    stale = document.replace(
        "Each is one physical line", "Both are one physical line", 1
    )
    assert stale != document
    assert not FIXED_KIND_COUNT.search(stale)
    assert PRONOUN_COUNT.search(_records_lead(stale))


def test_restated_constants_are_the_exported_ones() -> None:
    """Only where a source of truth exists to restate — see the module docstring."""
    document = _document()

    assert f"**{audit.MAX_VALUE_LENGTH} characters**" in document
    assert f"`{audit.DEFAULT_RENDERING}`" in document
    assert f"`{audit.UNRESOLVED_RENDERING}`" in document
    assert f'"environment:{audit.ATTRIBUTION_ENV}"' in document


#: The two `source` restatements that lived in code rather than in a document. Neither
#: spells the values any more; each names the alias instead, so there is a pointer to hold
#: rather than a vocabulary to compare (#504). `audit` still cannot import the alias — it
#: imports nothing from `hmcpctl` — which is why the pointer is prose and needs a check.
POINTER_DOCSTRINGS = (
    (AUDIT_MODULE, "record_tls_verification_disabled"),
    (AUDIT_TEST, "test_the_tls_record_carries_host_and_source"),
)


def _alias_name(module: ModuleType, vocabulary: frozenset[str]) -> str:
    """The name *module* binds *vocabulary*'s `Literal` under, found rather than spelled.

    Renaming an alias moves the name its dependents must quote or name, so the rename
    reddens the checks that read it instead of leaving them pointing at something that is
    gone.
    """
    names = [
        name
        for name, value in vars(module).items()
        if get_origin(value) is Literal and frozenset(get_args(value)) == vocabulary
    ]
    assert len(names) == 1, (
        f"expected one alias for {sorted(vocabulary)} in {module.__name__}: {names}"
    )
    return names[0]


def _annotation_source(path: Path, function: str, parameter: str) -> str:
    """How *function*'s signature writes *parameter*'s annotation, as source text.

    Read from the file for the same reason `_docstring` is, and because that is the only
    place the distinction lives: `audit` imports `annotations` from `__future__`, and a
    resolved type hint is the same object whether the signature named an alias or spelled
    a `Literal` out inline.

    Ambiguity is an error rather than a first match: one of the two names read here is
    `record`, a nested closure, and a second function so named would otherwise retarget
    the check in silence.
    """
    defined = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == function
    ]
    assert len(defined) == 1, (
        f"expected one {function} in {path.name}, found {len(defined)}"
    )

    for argument in defined[0].args.args + defined[0].args.kwonlyargs:
        if argument.arg == parameter:
            assert argument.annotation is not None, (
                f"{parameter} on {function} is unannotated"
            )
            return ast.unparse(argument.annotation)
    raise AssertionError(f"no {parameter} parameter on {function} in {path.name}")


def _docstring(path: Path, function: str) -> str:
    """The docstring of *function*, read from the file rather than imported.

    `tests/unit/` is not an importable package from here, and reading both the same way
    keeps the two halves of this check symmetric.
    """
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            documentation = ast.get_docstring(node)
            assert documentation, f"{function} in {path.name} has no docstring"
            return documentation
    raise AssertionError(f"no {function} in {path.name}")


@pytest.mark.parametrize(
    ("parameter", "vocabulary"), BUILDER_VOCABULARIES, ids=BUILDER_PARAMETERS
)
def test_the_record_builder_binds_the_derived_alias(
    parameter: str, vocabulary: frozenset[str]
) -> None:
    """The code end of every check that reads one of these frozensets.

    Two halves, and neither holds alone. The signature must *name* the alias — read from
    the source, because a resolved hint cannot tell an alias from an inline `Literal` —
    and the alias must resolve to the frozenset every check above compares against. Inline
    a `Literal` in the signature instead, which is how `decision` was written until #518,
    and the frozenset goes on describing a vocabulary the builder may no longer accept,
    behind a green run of every document check in this module.

    The alias name is found rather than spelled, as `client`'s is, so renaming it reddens
    only where the rename is wrong.
    """
    annotation = _annotation_source(AUDIT_MODULE, "record_authorization", parameter)

    assert annotation == _alias_name(audit, vocabulary)
    resolved = get_type_hints(audit.record_authorization)[parameter]
    assert frozenset(get_args(resolved)) == vocabulary


def test_every_literal_parameter_of_the_builder_is_held() -> None:
    """`BUILDER_VOCABULARIES` is a list, and this is what stops it going stale.

    A `Literal` parameter added to the builder would otherwise join the signature unread,
    exactly as `decision` sat unread until #518. What it holds is that list against the
    `Literal`s, which needs no exclusion list — not against every closed vocabulary the
    record carries, which is a larger set: see the ledger on `effect`.
    """
    annotated = get_type_hints(audit.record_authorization)
    literals = {
        parameter
        for parameter, hint in annotated.items()
        if get_origin(hint) is Literal
    }

    assert literals == set(BUILDER_PARAMETERS)


@pytest.mark.parametrize(
    ("parameter", "vocabulary"), BUILDER_VOCABULARIES, ids=BUILDER_PARAMETERS
)
def test_the_dispatch_closure_names_the_alias_too(
    parameter: str, vocabulary: frozenset[str]
) -> None:
    """The third restatement #518 removed, and what keeps it removed.

    `dispatch_scope.record` is where these two vocabularies are enforced on the way in,
    and `decision` was spelled out inline there on the line above `reason: audit.Reason`
    — for the same reason it was inline in the builder, there being no alias to name.
    Held from source like the builder's own signature, and dotted because that module
    imports `audit` rather than the aliases.
    """
    annotation = _annotation_source(DISPATCH_MODULE, "record", parameter)

    assert annotation == f"audit.{_alias_name(audit, vocabulary)}"


@pytest.mark.parametrize(
    ("path", "function"),
    POINTER_DOCSTRINGS,
    ids=[path.name for path, _ in POINTER_DOCSTRINGS],
)
def test_the_code_restatements_name_the_alias_instead_of_the_values(
    path: Path, function: str
) -> None:
    """Neither docstring may spell the vocabulary out, and both must name where it lives.

    These are the two restatements the ledger above used to record as out of reach. They
    are reachable now because there is no vocabulary left in them to drift: re-adding a
    value reddens the second assertion, and renaming the alias reddens the first.
    """
    documentation = _docstring(path, function)

    alias = _alias_name(client, client.VERIFY_SSL_SOURCES)
    assert alias in documentation, f"{function} names no `source` alias"

    spelled = sorted(v for v in client.VERIFY_SSL_SOURCES if v in documentation)
    assert not spelled, f"{function} spells the `source` values out: {spelled}"
