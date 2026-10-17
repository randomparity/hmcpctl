# ADR 0138: Managed-system null-property fallback via quick/All

## Status
Accepted

## Context
Issue #784: on HMC firmware whose managed-system inventory feed contains a null
`VirtualPersistentMemoryVolume/Uuid` sub-field, both `get_managed_system(uuid)` and
`list_managed_systems()` fail with HTTP 500 ("Nested path contains null property").
`find_system_by_name()` (via `search_uom`) already works on the same firmware, but
neither failing path has a system name to resolve with — `get_managed_system` is
keyed by UUID, and `list_managed_systems` has no name until it can list something.

## Decision
On the exact null-property HTTP 500 pattern, both methods fall back to
`GET /rest/api/uom/ManagedSystem/quick/All`, a JSON endpoint returning
`{UUID, SystemName, ...}` summaries for every managed system, then resolve each
summary via the already-working `find_system_by_name()`. `get_managed_system`
looks up its one UUID in the summary and delegates to `find_system_by_name`; a
miss (UUID absent from the summary, or the name lookup also fails) raises the
same actionable `HMCError` shape `list_managed_systems` already uses for this
firmware class. `list_managed_systems` resolves every summary entry the same
way and returns the subset that succeeds, skipping any system that still 500s;
if none resolve, it raises the original actionable error unchanged.

`quick/All` is not present in this repo's vendored HMC REST API reference for
`ManagedSystem` (`docs/capabilities/corpora.json`, topic `rest-p11:managed-system`,
captured from `https://www.ibm.com/docs/en/power11/9824-22A?topic=apis-managed-system`
— it documents only the per-UUID `.../quick/{Property}` form; `quick/All` appears
in that corpus only under the `Cluster` resource). The only evidence it exists
for `ManagedSystem` is IBM's own public `project-pim` repository
(`github.com/IBM/project-pim`), which uses it extensively
(`cli/utils/command_util.py:164`, `examples/hmc-agent/app/hmc.py:70,159`,
and the same `.../quick/All` shape for VIOS/LPAR/VirtualNetwork/VirtualSwitch
collections) without an `Accept` header, decoding the response as JSON directly.
This repo's own sibling `get_quick_property` (`client/core.py`) independently
confirms the header constraint: its docstring records that a typed
`uom+xml` `Accept` header causes HTTP 406 on `quick/` endpoints, so the new
helper sends no typed `Accept` header either, matching both precedents.
This fix therefore carries the `verification:live-hmc` label and must be
confirmed against a live HMC before the fallback can be trusted in the field;
until then it is exercised only by respx-mocked unit tests.

**Confirmed live on 2026-09-15** (recorded on PR #800, FW950/P10, schema
`V1_0`), as a by-product of the `/quick` discovery check issue #788 required.
`GET /rest/api/uom/ManagedSystem/quick/All` answered 200 with
`Content-Type: application/json` holding one object per managed system with
property values populated — the per-instance shape assumed above, decoded as
JSON without a typed `Accept`. No `X-HMC-Schema-Version` was sent on that
response. The project-pim-derived assumption therefore stands on live evidence
rather than on a third-party repository alone.

The same run established what `quick/All` is *not*: the lowercase
`/rest/api/uom/ManagedSystem/quick/all` does not exist on that level (400), and
the bare `/rest/api/uom/ManagedSystem/quick` is a different endpoint returning
an Atom feed of defined property *names*. See ADR 0140.

## Consequences
Both methods gain one additional request path (`_quick_all_system_names`,
added to `SystemsClient` via `_request`) that only activates on the specific
firmware failure; unaffected firmware sees no behavior change. Unlike
`find_system_by_name`'s own contract, both fallbacks now catch its
ambiguous-name `ValueError` rather than propagate it — a `SystemName`
collision can't disambiguate the target, so it counts as a fallback miss
(skipped per-system in `list_managed_systems`, folded into the actionable
error in `get_managed_system`). The fallback trusts `quick/All`'s JSON shape
defensively — an entry missing `UUID` or `SystemName` is skipped rather than
raised. `list_managed_systems`'s fallback resolves each entry with its own
sequential `find_system_by_name` call, so N systems cost 1+N round trips
instead of one; accepted because it only activates on firmware already
broken today, with no evidence this firmware class hosts large enough
inventories to make the added latency material.

> **Amended by #1501** (2026-10-13): `list_managed_systems` is removed, and with it its
> fallback. Every fleet-wide reader now lists systems from `ManagedSystem/quick/All`, because
> on V11R2 the unfiltered `ManagedSystem` feed timed out while a system was `No Connection`
> (#1290). `quick/All` does not trip the null-property bug. A reader that needs the full
> entry then reads `ManagedSystem/{uuid}` for an operating system only, through
> `get_managed_system`, which keeps this record's fallback for a 500 on that read. The
> fallback still resolves the name through `find_system_by_name`, whose `SystemName` search
> stalled only for a system with no connection, and such a system is not read.

> **Amended by #1648** (2026-10-15): the observed trigger is the `X-HMC-Schema-Version: V1_0`
> pin, not a firmware class. The fallback is kept, re-scoped to that pin.
>
> - **Evidence.** #1646's read-only probe (one V10R3 SP1060 HMC, one session) read the full
>   `ManagedSystem` feed twice, seconds apart. Without the header it answered 200 at the HMC's
>   native schema (`V1_17_0`), with no `VirtualPersistentMemoryVolume` element. With
>   `X-HMC-Schema-Version: V1_0` it answered 500, "Nested path contains null property",
>   `nestedPath=VirtualPersistentMemoryVolume/Uuid/Value/Value`: this record's 500. #1531's
>   2026-10-14 sweep sent no header and saw no 500 on 12 HMCs: quick/All listed 130 systems, the
>   full feed answered on 10 HMCs, and 107 `ManagedSystem/{uuid}` reads returned no 500. This
>   record's own 2026-09-15 confirmation (PR #800) ran with schema `V1_0` configured; it read only
>   quick endpoints (no full feed, no per-UUID read) and saw no 500, so it is circumstantial, not a
>   reproduction.
> - **Decision.** Keep the quick/All fallback and its control flow unchanged, as a mitigation for a
>   pinned schema version rather than for broken firmware. `HMC_SCHEMA_VERSION` is empty by default,
>   which sends no header, and no header-less read has produced this 500. The fallback's error and
>   the `ManagementConsole` guard's error (`get_console_info`, #880) name the pin as the likely
>   cause and tell the operator to unset both `HMC_SCHEMA_VERSION` and any profile `schema_version`
>   (the variable overrides the profile), rather than update the HMC firmware, which would not help
>   a pinned operator.
> - **Unverified.** Whether `ManagedSystem/{uuid}` — the read this fallback guards — also 500s with
>   the pin, since #1646 read only the feed; whether other releases (V11R2, V9R2) behave the same;
>   whether any HMC 500s here without the header; whether the fallback's own `SystemName` search,
>   which also sends the pin and returns full `ManagedSystem` entries, succeeds under `V1_0`; what
>   schema setting #784's original report ran with, which it does not record; and whether the pin
>   also explains the `ManagementConsole` 500 on `Session/SessionId/Value` recorded in #880, which
>   #1531's sweeps never read. Until that is known, an operator who hits either 500 without a pin
>   gets advice that does not apply. Retiring the fallback waits on that evidence.

## Considered & rejected
- Fall back to `search_uom("ManagedSystem", "UUID", uuid)`. judgment: no
  evidence in this repo or `project-pim` that `UUID` is a supported
  `ManagedSystem` search field — speculative behavior shipped unverified.
- Only translate to a clearer error message, without attempting resolution.
  judgment: leaves both methods broken on affected firmware instead of
  usable, when a working resolution path is available.
- Enumerate `list_managed_systems` via `search_uom("ManagedSystem", "State", s)`
  over every known `State` value. judgment: needs an exhaustive, versioned
  state enumeration and still can't cover `get_managed_system`'s single-UUID
  case; `quick/All` covers both with one mechanism.
