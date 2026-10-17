# ADR 0094: Deriving the Managed System an Ownership Guard Needs

## Status

> **Superseded in part by [0226](0226-scoped-lpar-inspection.md)** (2026-10-16): supplied-scope UUID containment.

Accepted (2026-08-25)

## Context

Issue #365 extracted the DLPAR processor and memory workflows out of the
`hmc_dlpar_proc` / `hmc_dlpar_mem` tool bodies into
`operations_lpar.set_lpar_processors` and `operations_lpar.set_lpar_memory`, so
a consumer already on an event loop can call them at all (the tool bodies ran
`asyncio.run`, which raises inside a running loop). ADR 0092 §3.2 classifies both
as **Reconfiguring** and requires them to be guarded unconditionally, so each new
operation must call `authorize_lpar_mutation` before it writes.

That guard reads the ADR 0011 ownership token through
`ssh_commands.get_lpar_description`, which runs `lssyscfg -m <system> -r lpar
--filter lpar_names=<lpar>`. It is keyed by **CLI managed-system name plus
partition name**, not by partition UUID. Every operation that guards today —
`delete_lpar`, `rename_lpar`, `set_lpar_boot_order`, the PCIe and SR-IOV
operations — takes a **required** managed-system selector and feeds it through
`resolve_system_uuid` → `resolve_lpar_ownership_names` to produce that pair.

The two DLPAR entry points do not. ADR 0063 gave them
`system_name_or_uuid: str | None = None` and recorded "optional, not required" as
a deliberate decision: required parameters would break positional ADR 0029
callers, and ADR 0039 already denies an omitted optional selector under any
`targets` table, so an operator who wants the selector enforced gets it from the
policy rather than the signature. Issue #365 additionally requires that the tool
contract not break for MCP clients.

So the guard needs a system, the caller may legitimately not supply one, and
neither constraint can be dropped. The operation has to *derive* the owning
managed system in that case.

## Decision

**An LPAR-mutating operation whose managed-system selector is optional derives
the owning system by bounded parent discovery when the caller omits it.**

`operations_lpar._resolve_and_authorize_lpar` is the guarded resolve chain for
those operations. With a selector it is the chain `rename_lpar` already uses.
Without one it resolves the partition UUID fleet-wide, then calls
`_discover_owning_system`, which walks `list_managed_systems()` and each system's
`list_logical_partitions(<system_uuid>)` feed until it finds the system whose
partition list contains that UUID, returning that system's UUID **and its
`SystemName` from the inventory entry that matched**. From there the existing
`resolve_lpar_ownership_names` → `authorize_lpar_mutation` chain is unchanged.

Returning the name matters: `_system_name`'s degraded path falls back to the
selector string it was handed, so passing a UUID there would end in
`lssyscfg -m <uuid>`, which the HMC CLI cannot satisfy, and the operator would
see an opaque SSH failure instead of a diagnosis. The walk already read the name
from the same feed, so it carries it forward rather than re-deriving it.

That closes the shape on the discovery branch only. The selector-supplied branch
hands `_system_name` the caller's raw selector, exactly as `rename_lpar` and
every other guarded operation does, so a UUID selector can still reach
`lssyscfg -m <uuid>` in the doubly-degraded window where the REST system read and
the SSH name lookup both fail. That is `_system_name`'s pre-existing shape, is not
changed here, and fails closed; it is scoped rather than claimed away.

The walk applies the bounds `HMCClient.find_partition_by_name` applies to a
fleet-ambiguous partition name — `MAX_PARENT_DISCOVERY_SYSTEMS` and
`PARENT_DISCOVERY_TIMEOUT_SECONDS` — and names the same operator remedy, *supply
managed-system scope*. It does not reuse `bounded_parent_systems` itself, whose
message asserts an ambiguous partition name; nothing is ambiguous on this path,
because the UUID resolved uniquely before the walk began. It also diverges from
the sibling on unusable fleet entries — see the Consequences below, where the
reason for that divergence belongs.

**Both branches establish containment, and neither may skip it.** The guard reads
`lssyscfg -m <system> --filter lpar_names=<partition>`, so if the system it is
told about is not the system the partition lives on, it reads *some other*
partition's token — and on a cross-system name collision that token may approve
the mutation. The omitted-selector branch gets containment from discovery's UUID
match. The selector-supplied branch has none for free: `resolve_lpar_uuid` passes
a canonical UUID straight through without checking it against the selector, and
ADR 0039 actively recommends UUIDs in policy `lpar` allowlists, so "a partition
UUID paired with a managed-system selector" is the *recommended* input shape.
`_verify_partition_on_system` therefore reads the selected system's partition
feed and rejects a UUID that is not in it, before the guard runs. One REST read,
against an operation that already pays an SSH login — and only for a UUID: a
partition *name* was resolved through `find_partition_by_name` against that same
feed, so its containment is already established and re-reading the largest
payload in the guarded chain would answer a settled question. That read is the
one guarded lookup with no selector remedy to offer, since the selector was
supplied, so its failure names *retry* explicitly rather than propagating raw.

**A UUID is checked for existence before the walk, not after.** `is_uuid` is a
format check: nothing upstream of `_discover_owning_system` establishes that a
canonical UUID names a partition, or anything at all. Without a precheck a single
caller-supplied string that matches nothing drives the worst case by
construction — up to `MAX_PARENT_DISCOVERY_SYSTEMS` full partition feeds — which
is cheap, caller-controlled request amplification against a capacity-limited
management appliance, and reports "no managed system reports it; supply
managed-system scope" for what is really a stale or mistyped UUID. The chain
already reads that partition for its CLI name, so the read is *moved* ahead of the
walk rather than added, and the selector-less branch no longer calls
`resolve_lpar_ownership_names` at all — discovery already returned the system
name, so that branch now makes one REST call fewer than before.

`rename_lpar`, `delete_lpar` and the PCIe/SR-IOV resolve chain share the
unchecked shape. They are not changed here — this ADR governs the two operations
#365 introduces — and the sweep is tracked in #465.

**An approved override skips discovery entirely.** `ownership_override=True`
reads no token (ADR 0092 §5), so it needs none of the resolution the guard alone
requires: the chain resolves the partition UUID exactly as the pre-extraction tool
body did, audits the override, and writes. Without this short-circuit the
operator's exception would be *blocked* by discovery's failure modes — an
oversized fleet, a slow one, an unreachable owning frame — which are precisely
the degraded conditions that provoke an operator into using it, and ADR 0092 §4's
"that path pays nothing" would stop being true.

The override branch does still resolve the **partition name**, one REST read that
needs no system. The audit stream is the only durable record of a deliberate
bypass of the ownership control, and every other guarded operation writes a CLI
partition name into that field; a UUID there would leave `lpar` carrying two
vocabularies with nothing to distinguish them, so a query for overrides against a
named partition would silently miss the DLPAR ones. One GET on a path that is
about to POST is a proportionate price for a single vocabulary, and unlike
discovery it cannot be blocked by an unrelated frame's health.

The `system` field carries what the caller supplied, verbatim — empty when the
caller named no system, and a **UUID** when the caller selected by UUID. The two
fields are treated differently on purpose: `lpar` is the *subject* of the bypass,
the partition that was mutated, and has to be unambiguous and comparable across
operations; `system` is *context*, the scope the operator asserted, and recording
it as given is the faithful thing to record. Resolving it would cost exactly the
discovery this branch exists to skip.

Two consequences follow, recorded rather than mitigated. An override record from a
selector-less call cannot say which frame it touched. And an audit query keyed on
a managed-system *name* will miss a DLPAR override whose caller used a UUID
selector, where every other guarded operation's override record holds a CLI name —
such a query has to match UUIDs too. If one vocabulary is wanted later, the cheap
version is `resolve_system_name` on the override branch when the selector is a
UUID: one read of a system the caller *did* name, which unlike discovery cannot be
blocked by an unrelated frame's health.

A blank selector (`""` or whitespace) is read as absent on both branches. MCP
clients that serialise an unset optional string as `""` sent it to these tools
before the extraction, where `resolve_lpar_uuid` ignored it for a partition given
by UUID; treating it as a real selector would resolve a managed system named `""`
and break a call shape that worked.

**UUID comparison is case-insensitive.** `resolve_lpar_uuid` returns a
caller-supplied UUID verbatim and `is_uuid` admits upper-case hex, while the HMC
renders UUIDs lower-case. Both containment checks are the first place in the
package where a *user-supplied* UUID is equality-compared against HMC output, so
a case difference would otherwise fail closed on both branches at once and leave
`ownership_override=True` — bypassing the control this change adds — as the only
working call. The normalisation lives in the comparison rather than in
`resolve_lpar_uuid`, whose value also reaches URL path segments.

The cost lands only on the guarded, omitted-selector path: 1 + N REST reads, N
bounded by the cap. A caller that supplies the selector pays nothing new, any
caller under a `targets` table is already forced to supply it by ADR 0039, and an
override pays nothing at all.

## Consequences

`set_lpar_processors` and `set_lpar_memory` are guarded on every entry path — MCP
tool, `hmc_mcp.api`, and any future CLI command — while keeping the ADR 0063
signature. ADR 0092's Domain B loses its two DLPAR rows.

Both MCP tools gain `ownership_override: bool = False`, appended after the
existing parameters. ADR 0092 §5 requires every guarded operation to accept the
per-call operator override, and a tool that guards without exposing it would
leave an MCP client with a rejection and no approved way through — the position
every other guarded LPAR tool already avoids. Appending keeps every existing call
valid, positional callers included, so the tool contract is extended rather than
changed. Issue #365's "parameter lists are unchanged" is read as its stated
intent, *this must not be a breaking change for MCP clients*, which appending
satisfies.

A DLPAR call that omits the selector now makes REST reads proportional to fleet
size before it writes. On a large fleet that is visible latency, and the remedy
is the selector. A partition whose owning system is not in
`list_managed_systems()` — an unreachable or unmanaged frame — fails with a named
error rather than mutating unguarded.

**The fleet read itself is a new dependency for the default call shape.** A
selector-less DLPAR call previously resolved through a `LogicalPartition` search
and, for a partition given by UUID, read no inventory at all; it now reads the
unfiltered `ManagedSystem` feed — the one `list_managed_systems` already
documents as answered with HTTP 500 by some firmware builds. That failure is
translated to name `supply managed-system scope`, chaining the underlying
diagnosis, because the selector is precisely what fixes it: an operator pointed
at a firmware upgrade instead would be pointed away from a remedy they could
apply immediately.

**The walk crosses frames the caller never named, so it tolerates their
failures.** A frame whose partition feed errors, or whose inventory entry carries
no usable UUID or `SystemName`, is skipped and recorded rather than made fatal:
otherwise one unhealthy frame that happens to sort early in
`list_managed_systems()` takes DLPAR down for every partition in the fleet, and
whether a call works depends on inventory order. Skipping cannot widen what may
be mutated — the walk returns only on a positive UUID match, so a skipped owner
ends in the same raise as an absent one — and the raise names how many frames
went unread, so a degraded fleet reads as degraded instead of as a missing
partition. That evidence is bounded — a handful of frame UUIDs plus a count of
the rest — rather than one line per frame, so a wholly degraded fleet cannot turn
an error message into kilobytes of log and MCP-client context.

**The walk is sequential, and the budget is inherited.** One full
`LogicalPartition` feed per frame, in inventory order, under the 30-second
deadline `find_partition_by_name` uses. There, that budget covers a rare path — a
fleet-ambiguous partition *name*; here it covers the default MCP invocation, so a
rare-path shape has been promoted to a common one unchanged. On a fleet where the
owning frame sorts late, the deadline can expire well below the 100-frame cap,
and the call then reports the timeout and names the selector as the remedy. Fanning
the membership reads out concurrently would fit the same ceiling, and is
deliberately not done here: this package has no precedent for concurrent
per-frame REST fan-out against an HMC, the HMC publishes no concurrency limit, and
a serial walk is the conservative load profile to ship first. If measurement on a
real fleet shows the default shape timing out, bounded concurrency is a change
contained entirely within `_search_fleet_for_partition`.

On the success path the derived system is logged at `INFO` with the partition
UUID, the system UUID and its `SystemName`. Where the caller supplied the
selector, the guarded system is reconstructible from the request; where it was
derived, nothing else records which frame's token gated the write.

**The two DLPAR entry points now require SSH reachability to the HMC.** They were
REST-only; the guard reads the token over SSH (ADR 0092 §4), so an environment
where the HMC user has REST access but not remote command execution — port 22
filtered, or a task role that denies it — loses both tools unless the caller
passes `ownership_override=True`, which reads no token. This is disclosed rather
than mitigated: the guard's transport is ADR 0071's decision, not this one's. The
resulting failure is an unwrapped `HMCCLIError` that names neither the ownership
precheck nor the override, which is the same shape `delete_lpar` and `rename_lpar`
already produce; wrapping it belongs to the shared guard and to every operation
that calls it, so it is tracked in #459 rather than special-cased here.

This rule is stated generally because ADR 0092 tracks three more entry points
with the same shape (#441's `hmc_set_lpar_msp` and `hmc_set_lpar_proc_compat`,
#442's `hmc_modify_lpar`). Each will need a managed-system name for its guard and
each carries an optional selector; they should reuse
`_resolve_and_authorize_lpar` rather than invent a second derivation.

> **Amended by #1144** (2026-09-29): the function this record calls
> `_resolve_and_authorize_lpar` (in the Decision section and again in the closing
> paragraph above) is now
> `resolve_and_authorize_lpar_mutation`
> (`operations/lpar/ownership.py::resolve_and_authorize_lpar_mutation`). ADR 0092's #1135
> note records the same rename.

> **Amended by #1501** (2026-10-13): `_discover_owning_system` lists the fleet from
> `ManagedSystem/quick/All`, not `list_managed_systems()`. On V11R2 the unfiltered
> `ManagedSystem` feed timed out while a system was `No Connection` (#1290). The
> `SystemName` comes from the matching `quick/All` summary, and a row without a valid UUID
> is an unusable entry. The walk still reads every listed frame's partition feed, since a
> `Standby` frame can answer it; a frame whose feed refuses is counted among those it could
> not read, with its `State` when that is not `operating`. A failed `quick/All` read still
> names `supply managed-system scope`.

## Considered & rejected

**Make the selector required on the operation, and on the tool.** The simplest
code, and it is what every other guarded operation does. Rejected: ADR 0063
decided the opposite for these exact tools with reasons that still hold, and
issue #365 forbids breaking the tool contract.

**Make the selector required on the operation but keep the tool's optional and
raise when it is omitted.** The parameter list would be unchanged while the
behaviour was not: a fleet-wide call that works today would start failing. That
is a breaking change wearing a compatible signature, which is worse than an
honest one.

**Read the owning system from an `AssociatedManagedSystem` link on the
LogicalPartition resource.** One GET, and the operation already fetches the
partition inside `resolve_lpar_ownership_names`, so it would cost nothing extra.
Rejected on evidence: this repo has only ever observed that link on a
`VirtualIOServer` (`client_storage.py:70`), never on a `LogicalPartition`, and
there is no live-HMC survey for the latter the way #374 surveyed `Description`.
Betting the operation's correctness on an unobserved schema element would make it
fail on firmware where it works today, with no way to test the claim from this
repository. Bounded discovery uses only endpoints already exercised end to end.
If a survey later confirms the link across the supported firmware generations,
replacing the walk with it is a contained change behind
`_discover_owning_system_uuid` and should be recorded as its own decision.

*Update (#466):* the survey has since been run. Every sampled `LogicalPartition` on V10R3 and
V11R2 carried the link, in the single-entry, per-system and fleet-wide reads, so the rejection above no longer
rests on an unobserved element; see `docs/workflow/lpar-parent-system-survey-466.md`.
The walk stays until a decision replaces it.

**Move the guard's read from SSH to the REST `Description` element.** ADR 0071
established that the description is readable over REST and fully inlined in the
per-system list feed, which would make discovery and authorization one call.
Rejected as out of scope: ADR 0071 deliberately left the single-LPAR
authorization path on SSH and recorded that move as a separate decision, and ADR
0092 §4 costs the guard on that basis. Changing the guard's transport affects
every guarded operation, not these two.
