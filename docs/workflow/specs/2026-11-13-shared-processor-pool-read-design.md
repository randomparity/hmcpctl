# Read shared processor pools (#1975)

Decision record: [ADR 0375](../../adr/0375-classify-configured-shared-processor-pool-slots.md).

## Problem

Nothing reads a managed system's shared processor pools. `SharedProcessorPool` is a
`ManagedSystem` child, so the root lister cannot reach it, and its feed returns every
firmware slot (64 on the captured POWER9) where `lshwres -r procpool` lists only the
configured pools. Pool update (#1976) and partition assignment (#1977) need a typed,
evidence-backed read to validate against.

## Design

One owner, `src/hmcpctl/operations/systems/processor_pools.py`, holds the projection, the
ADR 0375 classification, and both operations. Presentation layers only serialize it.
#1976 and #1977 import the projection from there. The issue proposed a separate
`documents/` module; `documents/` holds outbound request bodies and imports neither
client nor operation code, so a read projection belongs beside its operations.

**Client.** `SystemsMixin.list_shared_processor_pools(system_uuid) -> list[dict]` reads
`GET /rest/api/uom/ManagedSystem/{uuid}/SharedProcessorPool` with resource type
`SharedProcessorPool`, the media type IBM documents for this GET
(`docs/refs/hmc-rest-api-p10/managed-system/153-shared-processor-pool.md`), and parses
the feed with `_parse_feed`, as `list_virtual_switches` does. 204 returns `[]`.

**Projection.** `SharedProcessorPool` (frozen dataclass): `uuid`, `pool_id` (int),
`name`, `configured` (bool), `maximum_processing_units`, `reserved_processing_units`
(from `CurrentReservedProcessingUnits`), `pending_reserved_processing_units`,
`available_processing_units` (each `float | None`), and `assigned_partition_uuids`
(`tuple[str, ...] | None`).

- An absent unit element is `None`, never 0. A present one must parse as a finite
  decimal ≥ 0. PoolID must be decimal digits; PoolName must be a non-empty string; the
  entry must carry a `UUID`.
- `AssignedPartitions` absent is `None` (not reported). `_parse_feed` renders a present,
  childless element as `""`; that, or a mapping with no `link`, is `()`. Each `link` (one
  mapping or a list) must carry an href ending `/LogicalPartition/<uuid>` or
  `/VirtualIOServer/<uuid>` (a module-local `_partition_uuid(href)`, since
  `lpar_uuid_from_href` matches only the first); a VIOS in shared mode is a partition too,
  though no capture shows one. The UUID keeps the HMC's case. A present, empty unit element (`""`) is
  malformed, not `None`: only absence means not applicable.
- Any violation raises `HMCError` naming the field and the pool ID (or the entry UUID when
  the ID itself is bad). It never projects a partial pool.
- `configured` applies ADR 0375; `CONFIGURED_RULE =
  "default-or-nonzero-units-or-renamed"`.

**Operations.**

- `list_shared_processor_pools(hmc, system_name_or_uuid, *, include_unconfigured=False)
  -> SharedProcessorPoolListing(system_uuid, configured_rule, slot_count,
  unconfigured_omitted, pools)`. Pools sort by `pool_id`; `slot_count` is the feed's entry
  count; `unconfigured_omitted` is 0 when `include_unconfigured` is set. A repeated PoolID
  in the feed raises `HMCError`.
- `get_shared_processor_pool(hmc, system_name_or_uuid, pool: int | str)
  -> SharedProcessorPoolResult(system_uuid, configured_rule, pool)`. An `int` (never a
  `bool`) selects by PoolID, 0 included; a `str` selects by exact, case-sensitive PoolName.
  An empty name or a negative ID raises `ValueError`; a `bool` or another type raises
  `TypeError`. The selector is one
  required argument, so no call carries two selectors or none, and an MCP client's `""` for
  an unset optional string (ADR 0094) cannot arise. It searches every slot. No match raises
  `ResourceNotFoundError("shared processor pool", ...)` with the neutral advice "List the
  system's shared processor pools for valid IDs and names." More than one name match
  raises `ValueError`.
- Both operations read through one `_read_pools`, which validates the feed (repeated
  PoolID raises `HMCError`), so get and list never disagree.
- The system resolves through `resolve_system_uuid`, so names and UUIDs both work.

**MCP tools** (`server_tools/systems/processor_pools.py`, `effect="read"`,
`target_kind="managed_system"`): `hmc_list_shared_processor_pools(system_name_or_uuid,
include_unconfigured=False, profile=None)` as `processor_pool.list`, and
`hmc_get_shared_processor_pool(system_name_or_uuid, pool, profile=None)` as
`processor_pool.get`. Because `pool` is required and not a target selector, the scenario
generator reports `processor_pool.get` as hand-written rather than deriving a selectorless
definition; ST4 is its scenario. The list docstring states that the default pool's
`available_processing_units` is reported as the HMC gives it (0 on the capture) and is not
free capacity. Each returns `serialize_tool_result(...)`.

**CLI** (`cli_commands/systems/processor_pools.py`, top-level `processor-pools` group,
mirrored to the tools): `list SYSTEM [--all] [--json] [--format]` and
`get SYSTEM (--id N | --name NAME) [--json]` (exactly one option, enforced by the CLI). The table renders `None` as `n/a` for units
and `not reported` for partitions, and prints the rule and the omitted count.

**Capability ledgers.** Both operations go in `docs/capabilities/{maturity,operations}.json`
as implemented and `unevidenced`, each bound to row `rest:managed-system/shared-processor-pool`.
`_operation_maturity.json` is regenerated by `just capability-metadata`; the scenario
definitions and `docs/tools/` by their generators.

**Live vocabulary.** `just live-vocabulary` refuses a REST read path with no captured
endpoint. A new focused vocabulary, `tests/fixtures/live/vocabulary/v10r3-8375-42a-shared-processor-pool-source.json`,
records the captured `GET /rest/api/uom/ManagedSystem/{uuid}/SharedProcessorPool` (200) and
`lshwres -r procpool -m {}` (exit 0), in the shape of the existing `*-source.json` files,
with the captured `accept` and `content_type`. The gate reads only the GET path. The capture
requested `application/atom+xml`; the client's typed `type=SharedProcessorPool` request is
evidenced by ST4's pool reads, not by this capture.

**Live scenario.** ST4's read-only profile reads gain `_processor_pool_reads`, beside the
memory-pool read (`scripts/live_test/arms/profiles.py`). It needs only the system. It
lists pools (`processor_pool.list`) and reads `lshwres -r procpool -m <system>` through
`hmc_run_command`, in its default `name=value` form: the captured rows omit
`max_pool_proc_units` on the default pool, and `-F` would print an uncaptured rendering
there. The rows parse with `ssh.commands._parse_lshwres_output`. It asserts four things:
the configured pool IDs equal the CLI's; each pool's name matches; each unit the CLI row
carries (`max_pool_proc_units`, `curr_reserved_pool_proc_units`,
`pend_reserved_pool_proc_units`) equals the REST value; and the `lpar_ids` count (`none`
is 0; otherwise the comma-separated count) equals the `assigned_partition_uuids` length,
a `null` membership counting as 0, so a pool the CLI shows with members and REST reports
without them fails the assertion and captures the uncaptured case. Units compare as `Decimal(cli) == Decimal(str(rest))`.
Assertion ids: `pool-ids-match`, `pool-names-match`, `pool-units-match`,
`pool-membership-match`; the two gets record one `processor_pool.get` observation holding
`default-pool-by-id` and `default-pool-by-name`, because an observation id derives from
the subtask and tool and the evidence emitter refuses a run whose ids repeat.
A failed tool call, a failed `hmc_run_command`, or CLI output `_parse_lshwres_output`
refuses (`HMCCLIError`) records the read as failed (`_READ_FAILED`) and lets ST4's later
reads run. It then gets the
default pool by ID and by name (`processor_pool.get`). Nothing is written.

## Failure model

1. **Actors and deployments** — an MCP client or CLI operator with HMC read credentials;
   the live runner on the operator's lab host. The design targets HMC V10R3+/V11 REST.
2. **Invariants and assets at stake** — the `configured` flag and null-versus-zero
   semantics are a published contract #1976/#1977 will act on. A pool misread as
   unconfigured, or a missing maximum read as 0, would mislead a later write.
3. **Accepted failure classes**
   - Configured pool with maximum 0 and a placeholder name reads as unconfigured: no
     captured or documented case (ADR 0375); the live comparison catches one.
   - Feed size is unbounded by this code: firmware fixes the slot count (64 captured), and
     `_parse_feed` already bounds parsing for every UOM read.
   - Non-default pool membership in REST is uncaptured: reported as `None`, not `()`.
   - A partition link of another shape fails the whole read with `HMCError` rather than
     dropping a member silently; the only captured shape is `LogicalPartition`.
   - The default pool's `available_processing_units` is the HMC's value (0 captured), not
     free capacity; the tool docstring says so. System processor capacity is #680.
4. **Covered elsewhere** — ResourceGroup pool URIs (#639); managed-system processor
   configuration (#680); non-pool processor DLPAR (#638); shared memory pools (#642);
   pool update (#1976); partition assignment (#1977). Authorization and connection policy:
   the tool registry's existing `read` gate.

### Threat model

- **Boundaries** — added: two read tools and two CLI commands. They widen nothing.
  Input crosses as a system selector, a pool ID or a pool name, and the HMC's XML feed.
- **Actors** — a caller holding an existing read grant, and the HMC as the data source.
  The HMC is trusted for content but not for shape.
- **Controls** — the system selector goes through `resolve_system_uuid`; the client
  rejects a non-UUID path argument. Pool ID and name are compared in memory and never
  reach a path or a command. Feed XML parses with defusedxml (`_parse_feed`). Malformed
  fields raise `HMCError`. The live arm's `lshwres` command quotes the system with
  `shlex.quote`.
- **Out of scope** — new authorization rules: the read class and target kind are the
  existing ones.

## Success

1. `hmc_list_shared_processor_pools` and `processor-pools list` return, on the captured
   feed, pools {0, 1} with `unconfigured_omitted == 62`. With `include_unconfigured` they
   return all 64 slots, each carrying `configured`, and name `CONFIGURED_RULE`.
2. `hmc_get_shared_processor_pool` and `processor-pools get` return pool 0 by ID 0, pool 1
   by ID 1 and by name `SharedPool01`, and an unconfigured slot by ID with
   `configured: false`.
3. Pool 0 projects maximum/reserved/pending as `None` and available as 0.0. Its partition
   list is the one captured UUID. Pool 1 reports partitions as `None`.
4. A malformed unit (non-numeric, negative, non-finite, empty), PoolID or partition href
   raises `HMCError`; an empty `AssignedPartitions` projects as `()`. An unknown ID or name
   raises `ResourceNotFoundError`; an empty name or negative ID raises `ValueError`, a `bool` `TypeError`.
5. ST4 records `processor_pool.list` and `processor_pool.get` against `lshwres`, read-only.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Client child read | focused-test | respx serves the fixture; the path and the `type=SharedProcessorPool` Accept are asserted |
| Projection, null semantics, malformed fields | focused-test | `tests/system/test_processor_pools.py` over the redacted feed fixture |
| ADR 0375 rule on the 64-slot feed | focused-test | the configured set equals the CLI fixture's IDs |
| List/get selectors, not-found, ambiguity | focused-test | same module |
| MCP tools, CLI, mirrors | focused-test | tool and `test_cli_commands.py` cases; existing mirror/registry guards |
| Ledgers, docs, scenario definitions | focused-test | `just static` sub-gates (`tool-docs-check`, `doc-freshness`, capability inventory) |
| Live vocabulary entry | focused-test | `just live-vocabulary` passes with the client method present |
| ST4 pool reads | focused-test | arm test with a stubbed client: each of the four assertions failing alone, both gets, failed tool read, failed and malformed `lshwres` |
