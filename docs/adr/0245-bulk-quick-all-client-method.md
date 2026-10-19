# ADR 0245: One generic client method for the bulk `quick/All` listing

## Status
Accepted

## Context
Issue #244. The Atom path for listing LPARs or VIOSes costs one request per managed
system plus the full `Resource` block of every partition. A read-only probe on a V10R3
HMC (SP1060, 16 systems, 77 LPARs, 15 VIOSes) found that
`GET /rest/api/uom/{Type}/quick/All`, and the child form
`/rest/api/uom/ManagedSystem/{uuid}/{Child}/quick/All`, answer the same listing in one
request as a bare JSON array. `list_system_summaries` already calls the
`ManagedSystem` instance of this endpoint (ADR 0138). This record fixes the shape of the
client method that exposes the other types and the child form. It migrates no caller:
the payload lacks the Atom `ManagedSystem`/`link`/`title` fields and the `Resource` block
that `FleetListing` carries (#1731).

## Decision
`HMCClient.list_quick_all(resource_type, *, parent_type=None, parent_uuid=None)` in
`client/core.py`, beside `list_quick_properties`, whose parent arguments it copies.

- The path is `/rest/api/uom/{resource_type}/quick/All`, or
  `/rest/api/uom/{parent_type}/{parent_uuid}/{resource_type}/quick/All` when both parent
  arguments are given; exactly one of them is a `ValueError`. The suffix is exactly
  `quick/All`.
- Type segments pass `_reject_unknown_uom_type`; `parent_uuid` goes through
  `_request_with_uuid_path_arguments`, as in `list_quick_properties`.
- It sends `Accept: */*` and returns the parsed JSON array unvalidated, as `list[Any]`.
  Any status other than 200, invalid JSON, nesting too deep for the parser, and a
  non-array document each raise `HMCError` naming the path.
- An empty array is a successful empty listing.
- `list_system_summaries` is left as it is.

## Consequences
- A later caller migration, MCP/CLI exposure and other-firmware verification are separate
  records (#1731, #1732, #1733).
- The ~25 lines of JSON handling now exist twice (`list_system_summaries` and here).
  Delegating the former is a behaviour-preserving cleanup that this change does not take.
- Which types answer is firmware-dependent. An unsupported type surfaces as the HMC's own
  status in `HMCError`, not as a local refusal.

## Considered & rejected
- **Lowercase `quick/all`.** verified: GET on V10R3 SP1060 returned HTTP 400 `REST000B`
  (`INVALID_URL`) for all five forms probed on 2026-10-19; `quick/All` returned 200 on the
  same five. ADR 0138 recorded the same split for `ManagedSystem`.
- **Keep the Atom feed.** verified: on that HMC, listing 77 LPARs took 16 requests in about
  20 s, against one request in about 0.5 s for `quick/All`; the VIOS listing took 18
  requests in about 23 s, against one in about 0.4 s.
- **One method per type** (`list_lpar_quick_all`, ...). judgment: the type is the only thing
  that varies, and `list_quick_properties` already takes it as an argument.
- **A typed `uom+xml` Accept header.** judgment: `get_quick_property` and
  `list_system_summaries` document that quick endpoints take `*/*`; the typed form was not
  probed here.
- **Validate or project each item.** judgment: field presence varies by type and firmware,
  `list_system_summaries` leaves item meaning to the caller, and no caller exists yet.
- **Delegate `list_system_summaries` to the new method now.** judgment: the operator
  excluded that method from this change; the saving is small.
