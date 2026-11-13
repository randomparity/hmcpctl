# HMC compatibility

[Documentation index](index.md)

## What 0.2.0 claims

hmcpctl 0.2.0 serves the logical LPAR workflow through the primary MCP catalog: the tools an
MCP client's `tools/list` carries ([MCP server guide](mcp-server.md#logical-lpar-workflow)).
Its release claim, [`docs/capabilities/releases/v0.2.0.json`](capabilities/releases/v0.2.0.json),
names exactly the nine operations below. The claim is about the MCP tools in the table and the
operations beneath them; the `hmcpctl lpars provision` and `hmcpctl lpars decommission` commands
that wrap two of them are not part of it.

| Operation | MCP tool |
|-----------|----------|
| `inventory.logical` | `hmc_inventory` |
| `lpar.plan` | `hmc_plan_lpar` |
| `provision.lpar` | `hmc_provision_lpar` (partial) |
| `lpar.reconfigure` | `hmc_reconfigure_lpar` (partial) |
| `lpar.power` | `hmc_power_lpar` |
| `lpar.inspect` | `hmc_inspect_lpar` |
| `lpar.prepare_host_handoff` | `hmc_prepare_host_handoff` |
| `lpar.decommission` | `hmc_decommission_lpar` |
| `tools.invoke` | `hmc_invoke_tool` |

A claimed operation is live-verified only while the verification ledger reads `current` for
it: a passing live observation of the code the release ships, no older than 90 days. The
release is tagged only when all nine read `current` at the tag commit
([evidence gate](development.md#evidence-gate)). Each live observation records the HMC release
and hardware family it ran on; another HMC level or machine model may answer differently.

"Partial" is the catalog's implementation state: the operation implements some of the vendor
operation's variants, and the ledger names the ones it does not. The claim covers what the
live run asserted, which the ledger lists for each observation.

`hmc_search_tools` and `hmc_operation_status`, the other two primary tools, and every
specialist tool ship in 0.2.0 without a live-verification claim. That includes the 15
system, LPAR, job and dedicated-PCIe operations
[0.1.0 claimed](https://github.com/randomparity/hmcpctl/blob/v0.1.0/docs/compatibility.md#what-010-supports):
0.2.0 does not claim them again. Some unclaimed operations may still read `current` in the
report; the release does not claim them.

### Check an operation's state

The [verification ledger](capabilities/README.md#release-snapshot-and-candidate-currency) is
the authority for each operation's state. On a checkout of the release tag, after
`just setup`, check the nine claimed operations:

```bash
uv run --no-sync python scripts/check_capability_inventory.py \
  --require-current docs/capabilities/releases/v0.2.0.json
```

It exits nonzero unless every claimed operation is `current`. For every operation, run:

```bash
just verification-report
```

It prints one line per operation: its implementation state and its derived verification
state, which is `current`, `stale`, `failed`, `unevidenced` or `unrecorded`. Only `current`
means a passing live observation still applies. The state is derived when the report runs,
and an observation older than 90 days reads `stale`, so the same tag reports fewer `current`
operations as time passes even though its code does not change. The ledger's data is in
[`docs/capabilities/`](capabilities/README.md).

An installed package carries a frozen snapshot instead, which never ages:
`hmcpctl capabilities` prints it, and the [generated tool reference](tools/index.md) shows
the same values.

## HMC version compatibility

The checked reference corpus covers retained POWER10 and POWER11 command and REST
documentation snapshots. It is an evidence inventory, not a blanket compatibility
promise for every operation, HMC release, or managed system. Consult the
[HMC reference capability ledger](capabilities/README.md) for the corpus scope and
per-operation evidence, and the [generated tool reference](tools/index.md) for the
maturity currently published by the installed code.

Three VIOS backup catalog tools have a narrower floor:
`hmc_list_vios_backups`, `hmc_backup_vios`, and `hmc_restore_vios` require
**HMC V10 or newer**. Their supported HMC commands do not exist in the V9.1.940
command inventory, so these tools have no runtime version probe or V8/V9
fallback.

**`HMC_SCHEMA_VERSION` — leave this unset for normal operation.** It is opt-in
and unset by default, because HMC V8/V9 targets do not need it and uom
documents already declare `schemaVersion=V1_0`. Set it only to pin schema
negotiation explicitly — for example while debugging a read path.

Where the `X-HMC-Schema-Version` header goes when it *is* set. The rule is per
call site, not per HTTP method — there is no method whose requests all carry it
and none whose requests all omit it:

- **UOM requests carry it unless their own call site opts out.** A call site
  opts out by passing `include_schema_version=False`, so
  `rg -n 'include_schema_version=False' src/hmcpctl/client/` is the current set
  and the only answer that cannot go stale. Opting out was a response to HTTP
  406 on specific endpoints, not a property of any HTTP method or resource
  type: `PUT`/`POST LogicalPartition`, `PUT VirtualNetwork` and child-resource
  adapter `PUT` opt out, and so do many — **not all** — of the VolumeGroup and
  VirtualIOServer storage paths, reads included. Do not read a sibling
  endpoint's omission as covering yours; check the flag at the call site you
  are debugging.
- **Every `/rest/api/web/` request carries it**, reads and writes alike,
  because some HMC releases require it there.
- **Requests that build their own headers never carry it**, whatever this
  variable is set to. That is the `Accept: */*` discovery reads — quick
  properties, and the operation, schema and search-parameter listings, where
  ADR 0139 records the omission deliberately — and every `do/{Operation}`
  job, which is the path LPAR power on/off and activation take.

Unset — or set to the empty string, which disables the header the same way —
is therefore the setting under which no request carries it at all.

A live run prints the resolved value in its run header, on stdout, and records
the same string (`(not set)` when unset) as `run.schema_version` in the
`test-results-*.json` document. Each observation it emits records the same
string as `schema_version`, so an observation cited as evidence says which of
those two request environments produced it; observations stored before maturity
format 4 read `unrecorded` ([ADR 0186](adr/0186-observation-schema-version-stamp.md)).
See [`docs/environment-variables.md`](environment-variables.md) for all
supported variables.

### Firmware write-path compatibility

HMC V10R3 answers a UOM write with HTTP 406 when its `Accept` header names a uom
media type, and with 415 when its `Content-Type` does not name the resource type.
Shared UOM writes therefore send `Accept: */*` with a typed `Content-Type`, and
shared deletes send `Accept: */*` ([ADR 0178](adr/0178-uom-writes-send-untyped-accept.md)).

LPAR creation (`hmc_create_lpar`, `hmc_provision_lpar`) uses the REST create,
which V10R3 accepts for a document that follows its XSD and gives the partition
`default_profile` and a current configuration. It falls back to `mksyscfg` over
SSH when the REST create is refused with a 406 or with a 400 `REST0001` payload
rejection, logging the HMC's message, and always for a create with no memory or
processor values. `HMC_PASSWORD` (or `HMC_SSH_KEY_FILE`) must be set for SSH
auth; the fallback is transparent to the caller.
