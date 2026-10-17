# HMC reference capability ledger

This directory inventories the four IBM documentation snapshots named by issue
[#621](https://github.com/randomparity/hmcpctl/issues/621). It separates the
reference denominator from current implementation and verification maturity.

The artifacts use format version 1:

- `corpora.json` identifies the POWER10 and POWER11 command and REST snapshots,
  records archive and per-file hashes, classifies all 832 documents, and accounts
  for 14,311 structured source units. Source units cover command synopsis lines,
  option and attribute table rows, REST resources, methods and fields, and explicit
  version or capability statements.
- `rows.json` groups source units into 382 normalized command or REST capabilities.
  Each row preserves its POWER10/POWER11 presence. Coverage-child rows map to
  concrete children of epic #620. A missing page in either snapshot records only that delta;
  it does not claim that an HMC release lacks the capability.
- `operations.json` reconciles all 163 registered MCP operations to reference
  families or explains a repository-specific composite. It records the current
  handler signature, registered surface and relevant test paths. These links do
  not claim that a test passed or that hardware verification exists.

Run `just capability-inventory` for the offline structural and registry check.
The command succeeds when the ledger is internally sound even while coverage
children remain. Its second output line states whether implementation coverage is
complete; the initial ledger is deliberately incomplete until those children land.

To reproduce the source identity and source-unit extraction, provide the four
retained directories explicitly:

```text
uv run --no-sync python scripts/check_capability_inventory.py \
  --source commands-p10=<SOURCE_ROOT>/hmc-commands-p10 \
  --source commands-p11=<SOURCE_ROOT>/hmc-commands-p11 \
  --source rest-p10=<SOURCE_ROOT>/hmc-rest-api-p10 \
  --source rest-p11=<SOURCE_ROOT>/hmc-rest-api-p11
```

The validator checks only those named roots, rejects symlinks and unexpected files,
and compares file bytes and regenerated source units. The large third-party source
snapshots remain outside Git; their URLs, capture timestamps and hashes remain here.

`supported`, `coverage-child`, `proposed-exclusion` and `unknown` are accounting
states for this inventory. They are separate from the maturity and live-evidence
contract owned by #622 and from runtime eligibility. A proposed exclusion is not an
approved exclusion, and either it or an unknown blocks a complete-coverage claim.

## Maturity and evidence catalog

`maturity.json` is a sparse, format-versioned catalog keyed by the stable operation
IDs in `operations.json`. Format 3 ([ADR 0132](../adr/0132-confirmed-live-limitation-gaps.md))
added optional confirmations to missing scope; format 4
([ADR 0186](../adr/0186-observation-schema-version-stamp.md)) adds a required `schema_version`
to every observation. [ADR 0212](../adr/0212-release-scoped-live-verification.md) now
governs release-scoped currency and promotion; ADR 0127's observation shape and closure fingerprint remain.
Implementation — `absent`, `partial`, or `implemented`, with explicit implemented
and missing scope — is recorded independently from evidence.

Evidence retains one observation shape, with an exact key set:

```json
{
  "id": "st12-hmc-get-job",
  "channel": "live",
  "result": "passed",
  "scenario": "st12-job-inspection",
  "tested_commit": "<40 hex>",
  "observed_at": "2026-09-06T00:03:02Z",
  "hmc_release": "V10R3M1060",
  "hardware_family": "POWER10",
  "schema_version": "V1_0",
  "cleanup": "not-required",
  "closure_fingerprint": "<64 hex>",
  "assertions": ["job-found", "job-identity-matches", "job-status-successful"]
}
```

`result` is `passed` or `failed`; ADR 0126's `not-run` placeholder is gone, because an
operation with no observation already reports as `unevidenced`. `assertions` lists the
ids that **held**, in declaration order — so a `failed` observation is distinguishable
from a `passed` one on that field alone. `hmc_release` and `hardware_family` are the
only free text and each has a grammar (`V<n>R<n>[M<n>]` and `POWER<n>`) rather than a
permissive character class, so a hostname, serial, or location code cannot be written
there. The catalog grammar keeps the maintenance level optional so rows recorded before
#1335 stay valid; the runner requires it (see *Recording an observation*).
`schema_version` is the run's `HMC_SCHEMA_VERSION` as its header printed it — a
`V<n>_<n>[_<n>...]` token or `(not set)` — and observations stored before format 4 read
`unrecorded`, because nothing recorded the value for them; it attributes the observation and does not
affect currency or promotion. An operation carries one live observation per producing
observation id, and the newest by `(observed_at, id)` decides its state; re-validating
replaces that id's row, and the superseded record stays in `git log`.

### Confirmed limitation gaps

A declared, matching HMC limitation records `SKIP` and emits a separate
`{operation, missing_scope}` row, never an observation or promoting result.
`InvalidDispatch` remains a failure. Transient cleanup conditions (already absent
resources or already powered-off partitions) only skip; they do not confirm gaps.
Every declaration supplies a registered operation and a closed variant token.
Startup validates declarations and their tool associations before opening a client.

Copy an emitted `missing_scope` object into the operation's implementation record:

```json
{
  "variant": "managed-system-pcm",
  "parameters": [],
  "confirmation": {
    "tested_commit": "<40 hex>",
    "observed_at": "2026-09-10T00:00:00Z",
    "hmc_release": "V10R3M1060",
    "hardware_family": "POWER10",
    "closure_fingerprint": "<64 hex>"
  }
}
```

The placeholders above describe hash lengths, not valid catalog values. Review the
implementation state and scopes together: `absent` has only missing scope; `partial`
has both; `implemented` has no missing scope. Confirmations are forbidden on implemented
scope, require empty parameters and have no evidence fields. Existing unconfirmed,
parameter-constrained scope entries remain valid. Duplicate scope identities fail validation.

On the next run a validated confirmation skips the matching declared operation/variant
only when release, hardware family and current handler import-closure fingerprint match,
and its age is between zero and 90 days inclusive. A cached skip does not refresh the
timestamp or emit another confirmation. After 90 days, an environment/closure change,
or removal of the confirmation, the runner attempts the call again. Replace a confirmed
gap only with a fresh observed limitation; remove/revise missing scope when it is fixed.
Stale confirmations remain valid historical records; future timestamps are invalid.
Coarse environment labels can retain a repaired limitation for up to 90 days; remove
its confirmation when earlier revalidation is needed. Missing environment labels disable reuse.

### Release snapshot and candidate currency

`release-verification.json` freezes `verification`, `reason` and `observed_at` for the
last release, initially v0.1.0. The packaged `src/hmcpctl/_operation_maturity.json`
combines those fields with current catalog-derived operation IDs, `implementation`
and `runtime_eligibility`. `just capability-metadata` regenerates that projection;
`just tool-docs` renders its frozen verification without a wall-clock dependency.
Ordinary inventory checks compare verification to the snapshot, not development closures.
Installed CLI/MCP discovery retains this historical state without runtime age decay.

Candidate currency is derived separately from catalog observations. An observation is
stale when its operation's fingerprint differs from the current import closure, or it
exceeds the 90-day ceiling at gate time. The closure covers the handler and transitively
imported Python modules under `src/hmcpctl/`, not the CLI wrappers. Each operation reports:

| State | Condition |
|---|---|
| `unrecorded` | no maturity record |
| `unevidenced` | a record, but no live observation |
| `stale` | the closure changed, or the observation exceeded the age ceiling |
| `failed` | a failed observation that is not stale |
| `current` | a passed observation that is not stale; the only promoting state |

Ordinary development needs no live rerun or rerun issue when closures change. An
operator-authorized ad-hoc window may contribute catalog observations; it does not
change packaged verification between releases. Release eligibility still depends on
matching candidate closures and sufficiently recent evidence.

### Reporting

`just verification-report` prints candidate implementation and derived verification
states plus global counts. It is a diagnostic, not the installed historical state.
The former CI verification-report job, including weekly fail-on-stale, is removed.

Committed `releases/vX.Y.Z.json` files identify each claimed operation, arm and scenario.
The historical `v0.1.0.json` records 15 MCP claims and makes no CLI claim. Inventory
validation checks claim triples; the release gate checks selected operations:

```sh
uv run --no-sync python scripts/check_capability_inventory.py \
  --require-current docs/capabilities/releases/vX.Y.Z.json --release
```

Substitute the actual release claims filename. `--require-current` implies a selected
report, checks candidate closure and age, and fails on any selected non-current state.
It is mutually exclusive with global `--fail-on-stale`, which requires
`--verification-report`. Unclaimed non-current operations do not block the selected gate.
`--release` additionally requires the snapshot to match candidate closures/catalog,
without age decay; the two checks serve distinct purposes. See
[releasing](../development.md#evidence-gate) for the tag-time sequence.

### Recording an observation

The runner writes ignored results and `<stem>-observations.json` sidecars, not the
catalog. Observations require clean `src/` and `scripts/`, both environment labels in
`.env` (`LIVE_TEST_ENV_HMC_RELEASE`, canonical `V<n>R<n>M<n>` such as `V10R3M1060`, and
`LIVE_TEST_ENV_HARDWARE_FAMILY`), a valid or unset `HMC_SCHEMA_VERSION`, and an ignored
output destination. Assertions and cleanup attribution determine eligible evidence.
Raw results can contain private infrastructure identifiers and must not be published.

For a release, use [the authorized window command](../live-testing.md#2-run-the-authorized-window).
After its last successful arm, clean recovery and matching independent baselines,
it invokes `scripts/promote_live_observations.py` with the claims, complete result set,
new `promotion/` branch and explicit completion/recovery attestations. The promoter
validates result/sidecar provenance at the candidate and eligible observations offline,
then writes `maturity.json`, `release-verification.json`, the packaged projection and
regenerated `docs/tools/`. Its commit allowlist excludes raw output. It neither pushes
nor tags; review the local promotion branch in a PR before merging. A failed promotion
may leave that branch for inspection, never automatic rollback or a retry grant.

Promotion retains MCP evidence only. Bare-CEC's asserted CLI observations stay on raw
result rows, and the window's private summary reports CLI coverage or an explicit gap
for each claim. Three read-only wrapper checks do not verify every CLI wrapper or
retroactively change v0.1.0's MCP-only scope.

The validator proves shape and provenance consistency, not that hardware was exercised.
Review must tie the generated filtered matrices to the actual candidate, completed window
and clean recovery. An ad-hoc catalog update does not authorize writing a release snapshot;
only the reviewed release-evidence flow changes the frozen verification fields.

Scenario coverage remains hand-written until #706.
`uv run --no-sync python scripts/scenario_gap_report.py` reports, offline, the operations
and rows no registered live scenario exercises, dispatches in scenario functions `SUBTASKS`
no longer reaches, tools or operations the registry no longer has, and dispatches whose
argument names the served input schema rejects. It exits 0; `--fail-on-dispatch` exits 1
on an unregistered, mismatched or unreadable dispatch. `just scenario-gap`
runs it with that flag as a `static` member, so a dispatch finding fails `just verify`.

The catalog records no runtime eligibility. `existing-runtime-guards` neither grants
nor revokes admission: authorization, ownership, validation, capability, and safety
guards continue to control runtime behavior. Validate the catalog and its joins with
`just capability-inventory`.
