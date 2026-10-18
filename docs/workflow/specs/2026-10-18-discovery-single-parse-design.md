# Single-pass discovery classification

Interactive full-spec M denominator250; user approval item3. Root owns task.
Existing client_parse owns parsing/error tagging. Outcome: exactly one defused XML
classification parse per HTTP200 discovery response, preserving all existing results and errors.
No new dependencies, authorization, transport/cache/schema or public wire changes.

## Interface and behavior
Add narrow private _parse_discovery_names implementation in client_parse.py and
expose its
_tag_parse_errors-wrapped callable _discovery_names(xml_text,context,name_tag,
container_tag,member_tag)->tuple[list[str]|None,bool]. The boolean says container
exists; returned names are populated values in document order including duplicates,
None means at least a name/member element exists but there are no usable names,
[] means neither exists. Parse once with DET.fromstring(xml_text.encode('utf-8')),
use existing xmlutil.localname over root.iter once; collect stripped text and two
presence booleans. No custom XML parser or generic classification framework.
Both quick and search endpoint methods call this parser after existing HTTP gates.
If populated names exist return regardless of container, preserving existing order
of checks. Otherwise absent container raises the exact current HMCError with
status/body and endpoint wording; otherwise return parsed [] or None and header.
204 never parses; non200 status refusal unchanged. Remove repeated endpoint text
scans and redundant comments; keep explanatory boundary semantics where needed.
Keep _find_all_text if other client call sites still use it; no gratuitous API move.

## Failure model
Malformed XML, existing defusedxml entity/external-reference rejection and
excessive nesting retain tagged errors and causes; preserve default DTD handling
(entity-free DTDs remain accepted). Missing container remains error, not empty or unknown. Blank
Nickname/ParameterName and unnamed members remain unknown, empty container is fact.
Names outside container remain accepted as today. No cache expansion or extra I/O.

## Verification inventory
Mode focused-test: both real endpoint HTTP200 empty responses parse once; old code
parses3times. Count actual defusedxml.fromstring calls at parser boundary, not mocked
classification. Include names, names-without-container, blank names, unnamed members,
empty collection and missing-container cases with original messages/status/body.
Mode focused-test: malicious/malformed XML retains error tagging/no entities;204/non200
have zero discovery parses. Existing HMCError diagnostic extraction still parses
non200 bodies once and missing-container bodies once more: total parse counts
are 0 for 204, 1 for non200, and 2 for missing-container errors.
Existing discovery validation/cache/version tests pass. Run affected
client tests discovered by rg plus just lint/format-check/typecheck, all exit0.
Prove an extra parse fault fails parse-count case. Commit spec+implementation with
actual14hooks; root final assembled full verify and pinned hooks. No live hardware.
