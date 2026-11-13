# Discover valid search parameters via /search — design

Issue [#789](https://github.com/randomparity/hmc-mcp/issues/789), under epic
[#785](https://github.com/randomparity/hmc-mcp/issues/785). Decision record:
[ADR 0142](../../adr/0142-search-parameter-discovery-parses-one-named-element.md). Prior records
read: [ADR 0118](../../adr/0118-core-library-facade.md),
[ADR 0139](../../adr/0139-discovery-reads-return-feed-and-schema-version.md),
[ADR 0140](../../adr/0140-quick-property-discovery-reads-atom-nicknames.md),
[ADR 0141](../../adr/0141-quick-property-name-validation-is-opt-in.md). Prior solution record read:
[fixtures invented for an endpoint never spoken](../../solutions/2026-09-14-fixtures-invented-for-an-endpoint-never-spoken.md).

## Problem

`HMCClient.search_uom` (`src/hmc_mcp/client/core.py:859`) interpolates `property_name` into
`/rest/api/uom/{R}/search/({P}=={V})` and sends it, so an unsupported property is discovered only as
an error status from the HMC — captured as HTTP 500; see Success 3. The type-anchored
`/rest/api/uom/{R}/search` anchor, which answers what properties a search may use, has no reader
anywhere under `src/`.

#789 is the union of two already-merged siblings applied to `/search`: the discovery read #788
landed for `/quick` (PR #803, ADR 0140) and the opt-in validation wiring #799 landed for
`get_quick_property` (PR #805, ADR 0141). Both shapes are reused rather than re-decided.

**The response shape was unknown when this was designed, and a live capture has since settled it.**
The vendored reference corpus describes the anchor — path grammar for both forms, and per-type
prose pointing at it — but never its response: no content type, no body example, no element
vocabulary. ADR 0142 carries the citations and the searches that establish those three absences.
The design was therefore built on an inferred element name, the fixtures were constructed, and the
solution record above named #789 as one of three sibling reads that would hit exactly this wall.

It did hit it. The operator's capture at `V1_17_0` and `V1_20_0` (PR #807) found both halves of the
inference wrong — the container and the name-bearing element — and the design was corrected against
it before merge. The fixtures are now **reconstructed from that capture's shape report**. ADR 0142
records the correction and what the inference cost; the entries below are revised against the
capture and mark what it changed.

## Scope

One unit: a new discovery read, a per-client name cache, and an opt-in validation branch in
`src/hmc_mcp/client/core.py`, plus the record and changelog entry facade movement requires.

**Ownership.** `core.py` already owns `search_uom`, both sibling discovery reads
(`list_operations`, `list_quick_properties`) and the client's per-instance state, so this is a clean
extension of the current owner: no responsibility moves, no caller migrates, no path becomes
obsolete, and no compatibility path is retained. The six production call sites
(`client_lpars.py:60`, `client_systems.py:179,251`, `operations/vios/core.py:42`,
`operations/systems/core.py:44`, `operations/lpar/core.py:96`) are untouched — the new behaviour is
unreachable without passing `validate=True`, which none of them does. The two `search_uom`
declarations in `client_contracts.py` (lines 89, 319) are also untouched: a keyword-only parameter
with a default widens the implementation without narrowing the protocol, so both declarations stay
satisfied. `hmc_mcp.api`'s six exports (ADR 0118) are a protected contract and stay as they are.

**Changes.** Two module-level constants carry the captured shape:
`_SEARCH_PARAMETER_NAME_ELEMENT = "ParameterName"` and
`_SEARCH_PARAMETER_CONTAINER_ELEMENT = "SearchParameterSet"`, the second consulted only when no
name was found, to separate a type defining none from a body of another shape entirely.
`list_search_parameters(resource_type)` reads `/rest/api/uom/{R}/search`, sends `Accept: */*`,
and returns `(names, schema_version)` — the texts of that element read document-wide via
`_find_all_text`, paired with the response's `X-HMC-Schema-Version`. ~~A 204 returns
`([], schema_version)`; a non-200 raises `HMCError` carrying the status; a 200 yielding no name
returns `([], schema_version)` when it carries the container and raises `HMCError` when it does
not.~~ ADR 0144 is where those empty answers were separated: a 204 returns `(None, schema_version)`,
and a 200 yielding no name returns `([], schema_version)` only when the container holds nothing this
parse could have named. The non-200 clause is unchanged. `HMCClient.__init__` gains
`self._search_parameter_names: dict[str, frozenset[str] | None] = {}` and
`self._search_parameter_names_lock = asyncio.Lock()`. A private
`_defined_search_parameter_names(resource_type)` reads the root anchor once per type per client,
storing `frozenset(names)` when it yields names and `None` otherwise. `search_uom` gains
keyword-only `validate: bool = False`; when true it consults that helper and raises `ValueError`
before building the path if the set is non-`None` and lacks `property_name`. `CHANGELOG.md` gains
one `## [Unreleased] / ### Added` entry.

**Out of scope, with owners.** MCP and CLI exposure of the discovery calls (#792); capability-ledger
rows (#794); the XSD anchor (#790) and job feeds (#791); live `/operations` reconciliation (#793).
Live-firmware confirmation of the response shape was the operator's, on the live-test host, after
this PR was pushed; it has since run and this design is revised against it. No parent-anchored
validation — `search_uom` reads a root-anchored path,
so parent arguments would describe a resource it does not address, the same ground ADR 0141 gave.

## Failure model

**Actors and deployments.** A local operator running the `hmc-mcp` CLI; an MCP client calling a
tool, where `_app.with_client` opens and closes one `HMCClient` per call; a Python caller holding an
`HMCClient` across several reads, including one issuing validated calls concurrently on that client
— the only actor for whom the cache saves a request and the only one whose cache can go stale; CI,
which exercises this code only against `respx` mocks and never against firmware.

**Invariants and assets at stake.** `search_uom`'s default behaviour, which ADR 0118's facade
exposes through `HMCClient`, and which six in-repo call sites depend on. The cost bound stated to
callers: at most one discovery request per resource type per client session, failures included,
holding for concurrent callers as well as sequential ones, and covering calls that run to
completion — a discovery read cancelled by `asyncio.CancelledError` caches nothing and is retried on
the next validated call, because `CancelledError` is a `BaseException` that the helper's
`except HMCError` does not catch. A *timeout* is not that case: `_request` converts
`httpx.TimeoutException` to `HMCTransportError` (`core.py:451-456`), which the helper does catch and
cache, exactly as accepted class 3 below states. First-time discovery for *different* types is
serialized by one client-wide lock: that costs latency, never a wrong answer. `search_uom`
continuing to work at levels where the discovery read fails, which is the pre-flight's own
degradation promise rather than any numbered criterion of #789. The six `hmc_mcp.api` exports.

**Accepted failure classes.**

- ~~**The parsed element name is unverified against firmware.**~~ **Closed by the capture, not
  accepted.** It was accepted for this branch on ADR 0142's bounding properties; the capture then
  found the name and the container both wrong, and the correction landed before merge. The child
  anchor is closed too, in the other direction: a control round established the form is not served,
  and the parent arguments were removed rather than left as an accepted failure class. Neither of
  these is an accepted class any more — both are settled facts, and the record says which way.
- A stale positive cache wrongly rejects a name a newer level added, for a client held across a
  firmware change. Accepted: unreachable for the per-call CLI and MCP actors, and the remaining
  actor's escape is `validate=False`, the default. Carried from ADR 0141.
- A transient discovery failure is cached as durably as a firmware-level one, so one connection
  blip leaves validation off for that type until a new client is constructed, with no signal.
  Accepted: the degraded state is today's unvalidated behaviour rather than an error, the client is
  per tool call in the CLI and MCP deployments so the window is one call, and re-reading instead
  would spend the per-call request the cost bound rules out. Stated in `search_uom`'s docstring.
- **The wrong-set class.** A discovery read that succeeds with the *wrong* names — fewer than the
  level serves, or the texts of an element that is not the one holding parameter names — makes
  `validate=True` reject a working property. Accepted: validation is opt-in and the `False` default
  is the escape, so no caller who has not asked for the check can be broken by it. No claim is made
  about whether any level answers short; this repository has not checked, and the acceptance does
  not rest on it. The *empty* answer is not accepted, because it is reachable — and the capture
  made it far more reachable than this entry originally said. It was written when a 204 was the
  only route to an empty set and a legitimately empty answer was believed impossible on firmware.
  **Six of the eleven captured types answer 200 with a `SearchParameterSet` and no parameters**:
  `ManagementConsole`, `VirtualSwitch`, `VirtualNetwork`, `NetworkBridge`, `LogicalUnit`,
  `SharedProcessorPool`. So the empty answer is the majority firmware case, not a protocol corner.
  ~~It is still degraded from rather than trusted, because an empty positive set would reject every
  name for the client's lifetime, and because on an unmeasured level an empty set may be a parse
  artefact rather than a fact about the type.~~ ADR 0144 is where that was taken the other way: an
  empty answer carrying the container and no name element at all is trusted as a fact about the
  type, and every other empty answer stays degraded from.
- ~~**`validate=True` performs no check on a type that defines nothing.**~~ A consequence of the
  entry above, stated separately because it is the one the reader will care about: for those six
  types the pre-flight is inert and the search still costs the HMC's 500, on precisely the types
  where a local refusal would be certain rather than probabilistic. Accepted for this change: the
  direction is fail-open to today's behaviour, no in-repo call site passes `validate=True`, and
  ~~the API surface discloses it — `search_uom`'s docstring and `CHANGELOG.md` both say a type
  defining none reads as unknown~~ (both now say it is refused locally instead).
  Making the check fire there needs a discriminated return separating
  "container present, no parameters" from a 204 or a failure, which buys local refusal on six of
  eleven captured types at the cost of a new way for `validate=True` to reject everything if a
  later level nests its parameters differently. Not taken here; it is a behaviour change beyond
  what this change was scoped to. **Taken in ADR 0144, not accepted:** that discriminated return
  is where the check was made to fire, at the cost this entry names.
- Cache growth is unbounded in the number of distinct `resource_type` values passed. Accepted:
  resource types come from literals in this repository and the dict dies with the client.
  **The per-entry size is bounded by `HMC_MAX_RESPONSE_BYTES`, not by the names being short** —
  the parse is captured at two levels, but no other level has been measured, and one answering the
  query-less root anchor with an instance feed would fill a single entry up to that ceiling.
  A level answering the query-less root anchor with an instance feed would fill one entry with
  per-instance data up to the configured response ceiling (ADR 0133). The entry still dies with the
  client; what is *not* accepted is rendering it unbounded. `search_uom`'s refusal message
  enumerates at most `_MAX_REPORTED_NAMES` names, each truncated to `_MAX_REPORTED_NAME_LENGTH`
  characters, and summarizes the rest as a count. **Both bounds are needed and neither makes the
  message private.** A count cap alone is not a byte bound — one element carrying a whole 32 MiB
  body renders in full, because one name is never twenty-one — which is why the length bound is
  there. And under a wrong parse the message still discloses up to twenty truncated operator
  instance names: that is less disclosure than the whole set, not none, and the accepted class is
  the residue.

**Covered elsewhere.** **Dot-segment** traversal in `property_name` or `resource_type`: `_reject_dot_segments` (`core.py:115`), which `_request` (`core.py:436`) applies
to every request, `_request` being the only site that builds or sends one. That guard is specific,
not general path handling: it refuses `.` and `..` segments in the raw and single-unquoted forms,
and it does **not** refuse `?` or `#`, so a type string carrying either retargets the GET within
`/rest/api/uom/` — verified not to permit SSRF, header injection, a different method, or an escape
above that prefix. The type segments are not percent-encoded, unlike `search_uom`'s
`property_name`/`property_value`. This is pre-existing and identical at every uom path
interpolation in this module — `rg -c 'f"/rest/api/uom/' src/hmc_mcp/client/core.py` returns 13 at
`a0d29ac7` (11 interpolating a caller-supplied type segment, 2 a job id). **This change extends
that class by exactly one site of the same shape rather than altering it:** the root anchor
`f"/rest/api/uom/{resource_type}/search"` in `list_search_parameters`. It is unreachable from MCP
or the CLI for this method; the shared decision is a follow-up candidate, recorded rather than
closed in this change.

  This entry has been wrong twice and is recorded rather than quietly reworded. It first said
  "identical at all six interpolation sites in this module, is not widened here" — the count wrong
  by a factor of two and the direction of change wrong outright. It then said the change adds
  **two** sites, which was true until the live capture removed the child anchor; it now adds one.
  The deferral itself is unaffected: the guard is specific, the pattern is pre-existing, and the
  encoding decision is shared.
Percent-encoding of the instance-search
grammar: `search_uom`'s existing `quote(..., safe="")` calls, pinned by
`tests/unit/test_client.py:824`. Response-body bounding: ADR 0133. MCP and CLI exposure: #792.

Not security-relevant, so no threat model: the change adds no entry point, no authn/authz or
tenancy logic, no secret, no new deserialization mechanism (`_find_all_text` already exists,
unchanged), no new non-literal in a constructed path beyond the ones `_reject_dot_segments` already
governs, no permission grant and no dependency. It narrows what reaches the transport. `$quest`
step 6 re-judges this against the actual diff.

## Success

1. `list_search_parameters(R)` reads `/rest/api/uom/{R}/search`, and no captured level serves a
   child-anchored form. (#789 criterion 1 — **knowingly partial**) The capture's
   control round found `/rest/api/uom/{P}/{U}/{C}/search` answering 400 `INVALID_URL` for two child
   types under a parent that served its plain child feed and its `/quick` anchor 200 in the same
   session. The parent arguments were removed on that evidence rather than shipped as surface whose
   only observed behaviour is `HMCError`; see ADR 0142.
2. `search_uom(..., validate=True)` raises `ValueError` without sending a request to
   `/rest/api/uom/{R}/search/({P}=={V})` when `{P}` is not among the names
   `list_search_parameters` returns for `{R}`. (#789 criterion 2)
3. `search_uom`'s docstring records what an unsupported search property yields from the HMC, so the
   reason the pre-flight exists survives. #789's criterion 3 named HTTP 400; **the captured status
   is 500** (`ReasonCode: Unknown internal error.`), and the docstring records the captured value
   and the correction. Both surface as `HMCError`, so only the recorded reason changes.
   (#789 criterion 3, amended by capture)
4. A resource type the HMC does not recognise surfaces `HMCError` carrying the HMC's status — from
   `list_search_parameters`' own non-200 raise, and on the `validate=True` path from `_get`'s
   non-200 raise for the instance search (`core.py:505-506`). (#789 criterion 4)
5. Across repeated `validate=True` calls on one `HMCClient` **that run to completion**, requests to
   `/rest/api/uom/{R}/search` number at most one per distinct `{R}`, whether the first read
   succeeded or failed; a fresh client reads again. A read cancelled by `asyncio.CancelledError`
   caches nothing and is retried, as the failure model states.
6. `list_search_parameters` sends `Accept: */*` on the one anchor, exactly. The captured content
   type is `application/atom+xml` and a second probe with `application/atom+xml; type=feed` also
   answered 200, but only those two values were ever sent, so `*/*` is kept as the one Accept that
   cannot fail negotiation on an unmeasured level.
7. When the discovery read yields no names — an `HMCError` from a 4xx or 5xx, an
   `HMCTransportError` from a connection failure, or ~~a 204 returning `([], version)`~~ —
   `validate=True` sends the search anyway and returns its result. ADR 0144 is where a 204 became
   `(None, version)`; it still validates nothing, and the container-present empty answer is the one
   that now refuses locally instead.
8. `validate` defaults to `False`; a call omitting it makes no discovery request and behaves
   exactly as at `a0d29ac7`. The decision is in ADR 0142 and the parameter in `CHANGELOG.md`.
9. `hmc_mcp.api` still exports exactly the six names ADR 0118 names.

## Validation

Every success criterion above is covered by a `focused-test` entry in the implementation plan's
Verification table
(the local plan `2026-09-15-discover-search-parameters.md`, *Task 1 — Verification*), which is the
inventory of record for the build. **It is pre-capture and was not revised**: it still prescribes
the disproven `<Nickname>`/`<SearchParameter_Collection>` parse and the removed parent arguments,
and three of its named tests no longer exist. A banner at its head says so, and ADR 0142 plus the
shipped test block are authoritative over it. Bodies in the test block are **reconstructed from the
capture's shape report**, with the block head enumerating which facts are live and which are
constructed.

Three contracts carry `Mode: task-test-not-applicable`, and the reason is a design judgment rather
than a plan mechanic, so it is recorded here:

- **The unsupported-property rationale in `search_uom`'s docstring** (Success 3, captured as 500
  rather than the 400 this was written against). Prose addressed to a human reader. No executable
  consumer validates it, and asserting its wording would snapshot prose.
- **The six facade exports are unchanged** (Success 9). Unchanged by this design, and
  `tests/unit/test_public_api.py` already fails on any drift; a second test would observe that
  test's subject rather than this change.
- **The `CHANGELOG.md` entry** (Success 8). `tests/unit/test_changelog.py` binds only the declared
  `pyproject.toml` version, which this change does not alter; no executable consumer validates an
  unreleased entry.

One contract was **not observable from this repository at all**: that
`_SEARCH_PARAMETER_NAME_ELEMENT` matches firmware (*Failure model*, entry 1). No firmware is
reachable from CI or a workstation, and a `respx` fixture asserts only against itself — a mutation
score over these tests measures whether they discriminate between implementations given the
fixture, and must not be reported as fixture validation. **That closing evidence has since
arrived.** The operator's capture at `V1_17_0` and `V1_20_0` ran during this change, found the
element name and its container both wrong, and the branch was corrected against it; what remains
unobservable is every level nobody has measured.
