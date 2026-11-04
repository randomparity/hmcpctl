# Host handoff: `hmc_prepare_host_handoff` and the kdive hold contract

**Issue:** #1852 (piece 2 of #1231, epic #1215) · **Decisions:**
[ADR 0193](../../adr/0193-host-handoff-external-management-hold.md),
[ADR 0327](../../adr/0327-hold-store-schema-and-hold-id-plumbing.md),
[ADR 0329](../../adr/0329-host-handoff-document-sources.md) · **Contract:**
[H1 spec](2026-10-01-logical-lpar-workflows-design.md), *Host handoff* · **Built on:** #1851's
`operations/logical/holds.py`

## Problem

`hmc_prepare_host_handoff` is named in the primary catalog (`server_tools/catalog.py`) but not
registered, so nothing places or releases an ADR 0193 hold, and `docs/kdive-tier-a-contract.md`
says nothing about holds.

## Scope

In: the tool, its operation module, its handoff document, the kdive contract text and test, the
interlock tests and the registration footprint. Out: the hold store, hook and `hmcpctl holds`
CLI (#1851, merged); console leasing (kdive, ADR 0172); hold expiry; guest checks (kdive
doctor/adopt); fencing the library, other hosts, the HMC GUI or direct HMC commands (ADR 0193 D6).
No CLI command mirrors the tool.

**Tool.** `hmc_prepare_host_handoff(action, lpar_name_or_uuid, system_name_or_uuid, hold=False,
consumer_label=None, hold_id=None, profile=None) -> HandoffResult`; `ToolSecurity(effect="mutate",
operation="lpar.prepare_host_handoff", target_kind="lpar")`, both selectors required, not
exhaustive. It is in `HOLD_EXEMPT_TOOLS` already, so it is not hold-guarded and its own `hold_id`
is the handler's parameter, not the registry's injected one. Inputs:

- `action="prepare"`: `consumer_label` is required with `hold=True` and refused without it;
  `hold_id` is refused.
- `action="release"`: `hold`, `consumer_label` refused; `hold_id` optional.

**Result.** `HandoffResult{action, hold: HoldView | None, document: HandoffDocument | None}`.
`HoldView{consumer_label, agent_id, created_at, hold_id}`; `hold_id` is set only when the
calling agent id placed the hold, and always null on `release`. `document` is null on `release`.

**Prepare** (ADR 0193 D1, ADR 0329):

1. Authorize as `hmc_get_lpar` and `hmc_list_lpar_ownership` (refuse when the policy withholds
   either); resolve the system and the partition from the system's partition feed.
2. With `hold=True`, `place_hold(system, partition, consumer_label, agent_id, connection)`;
   its `partition_guarded` / `partition_held` refusals reach the caller unchanged. Without it,
   `find_hold(partition)` reports any existing hold.
3. Read the document's sections, each reported with a `SectionSource` (`ok` / `unavailable` /
   `denied`) and never refusing the call:
   - `ownership`: the ADR 0011 stamp parsed from the partition read in step 1;
   - `storage`: `inspect.read_resources` — `hmc_list_vios`, then `hmc_get_vios_storage_detail`
     per VIOS, each admitted as that tool (ADR 0200's per-section rule);
   - `adapters`: the client network adapter feed, admitted as `hmc_list_adapters`; MAC, VLAN,
     slot per adapter.

**Document.** `identity` (`connection`, `system_uuid`, `partition_uuid`, `name`,
`partition_id`); `references` (`connection`, `agent_id`; never a secret); `observed` (`state`,
`rmc_state`, the resource figures, `ownership`, `storage`, `adapters`); `declared`; `unverified`;
`hold`; `unfenced`.

- `declared` (ADR 0329 D2): from this host's store. The partition's `partition` ledger entry names
  the provision operation; its recorded request yields `install_profile`, `network` (`address`,
  `routes`, `dns`) and, for `built` media only, `login_user` and `ssh_key_fingerprints`
  (`SHA256:` base64 of each key's blob; keys never echoed). `source` is `ok`, `none` (no ledger
  entry: not provisioned by hmcpctl on this host) or `unavailable` (operation pruned or its
  request unreadable), with `detail`.
- `unverified`: fixed items `os`, `kernel`, `bootloader`, `kdump_fadump`, `ssh_reachability`,
  `host_identity`, each stating that kdive's doctor/adopt checks it. Nothing claims guest
  readiness or trust.
- `unfenced`: ADR 0193 D6's surfaces (the hmcpctl library, other hosts, the HMC GUI, direct HMC
  commands) and ADR 0327 D5's tools the hold hook does not check (`hmc_restore_lpar_profiles`,
  system power and system profile activation, `hmc_run_command`, `hmcpctl raw`).

**Release.** Resolve the partition as prepare step 1 does, authorized as the tool alone (H1
row), then `release_hold(system, partition, agent_id, hold_id)`. `hold_mismatch` reaches the
caller; an unheld partition returns `hold: null`. Nothing is written to the HMC. A partition that
no longer resolves is released with `hmcpctl holds release` (ADR 0193 D7).

**Footprint.** `server.py` registers it beside `hmc_inspect_lpar`; `catalog.py` adds its security;
`check_capability_inventory.py`'s factory map, `docs/capabilities/operations.json`/`README.md`,
`maturity.json` (`implemented`, no evidence), `docs/tools/` (regenerated), the count tests, and
`test_hold_coverage`'s schema test (the tool publishes its own `hold_id`). Two private helpers become shared by
rename, with their one caller each: `operations/lpar/inspect.py`'s `_resources` →
`read_resources`, `server_tools/lpar/inspect.py`'s `_admitter` → `admitter`; `store.py` gains
the read-only `ledger_request`. The H1 spec's handoff
row is amended to ADR 0329's delegated set. `docs/kdive-tier-a-contract.md` gains a *Holds*
section: an MCP power call (`hmc_power_on_lpar`, `hmc_power_off_lpar`, `hmc_dump_restart_lpar`,
`hmc_power_lpar`) on a held partition needs `hold_id`; library calls are not checked.

## Failure model

1. **Actors and deployments**
   - An MCP client agent on a server with a served access policy and one state directory (the
     H1 deployments: one long-lived server, or stdio sessions and CLI on one host).
   - kdive, receiving the `hold_id` from that agent and calling MCP power tools or the library.
   - The local operator with `hmcpctl holds`.
2. **Invariants and assets at stake**
   - The hold's `hold_id`: returned only to its placing agent, never in `document`, a refusal or a
     listing.
   - The meaning of `observed`: only facts read during the call; a failed read is `unavailable`,
     never a stale or guessed value.
   - Policy-withheld authority: a withheld delegated read yields `denied` and no data.
   - No HMC write by either action; `release` never deletes the partition.
3. **Accepted failure classes**
   - *Facts change after the call.* Accepted: the document is a point-in-time read; kdive
     re-checks (H1 *Failure model*).
   - *A hold placed and then a section read fails.* Accepted: the hold stands; the section says
     `unavailable`, and a repeat with the same label returns the hold.
   - *`declared` absent for partitions provisioned elsewhere or pruned after 30 days.* Accepted:
     reported as `none`/`unavailable`, never inferred (ADR 0329 D2).
   - *Placing a hold on a partition another agent's ADR 0011 stamp names.* Accepted: ownership is
     advisory and reported in `observed.ownership` (ADR 0329 D4).
   - *A partition removed out of band keeps its hold.* Accepted: operator release (ADR 0193 D7).
4. **Covered elsewhere**
   - Hold storage, the hook, `hold_id` plumbing, store refusal: #1851, ADR 0327.
   - Unfenced writers and system-scoped tools: ADR 0193 D6, ADR 0327 D5 (stated in `unfenced`).
   - Guest checks and console leasing: kdive, ADR 0172.

### Threat model

- **Boundaries.** Added: MCP caller → hold placement and release in the store. Widened: the
  store's provision request → a caller of this tool (`declared`).
- **Actors.** An authenticated MCP client holding a narrower grant than the server; a local user
  who can write the state directory (trusted as in ADR 0190).
- **Controls.** The tool's own grant and target scope (`authorized()`); `hmc_get_lpar` and
  `hmc_list_lpar_ownership` admitted for the partition before any read; each section admitted as
  its tool; `consumer_label` validated by `place_hold`; `hold_id` compared in constant time by
  `release_hold`; `declared` emits fingerprints, never keys, URLs or producer results; errors name
  the failed check without echoing arguments.
- **Out of scope.** Fencing independent HMC writers; `hold_id` secrecy against the local operator
  (ADR 0193 Consequences); a compromised state directory.

## Success

| Criterion | Proof |
| --- | --- |
| C1 prepare/release shape and D1 rules | unit tests on the operation; app test through `create_mcp` |
| C2 document groups, no readiness claim, `unfenced` list, no secrets | unit tests on `prepare` |
| C3 delegated authorization | app tests: withheld `hmc_get_lpar` refuses; withheld section tool → `denied` |
| C4 kdive contract | `test_kdive_tier_a_contract` resolves the new tokens |
| C5 interlocks | app tests: after `prepare(hold=True)`, `hmc_power_lpar`, `hmc_reconfigure_lpar`, `hmc_decommission_lpar` refuse `partition_held` and pass with `hold_id`; after `release` they pass; `prepare(hold=True)` refuses `partition_guarded` while a provision operation guards the partition |
| C6 ledgers and guardrails | `just capability-inventory`, `just tool-docs-check`, the split `just verify` |
| C7 no live proof | none run; `maturity.json` carries no evidence row |
