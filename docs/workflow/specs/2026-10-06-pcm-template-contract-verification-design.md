# Verify existing PCM and partition-template contracts (V10)

Issue #634 (epic #620, entry V10; child epic #1243).

## Problem

Nine registered operations in this slice have no maturity record: `pcm.get_preferences`,
`pcm.set_preferences`, `metrics.processed_links`, `metrics.processed`,
`metrics.aggregated_links`, `metrics.aggregated`, `template.list`, `template.get` and
`template.deploy`. Their `row_ids` in `docs/capabilities/operations.json` bind every PCM row
(thresholds, energy, LTM, `chpcm`/`lspcm`), not the endpoints the handlers request.

The live harness exercises them only through non-promoting paths, and it treats outcomes that
are not limitations as limitations:

- **Stale "unlicensed" declarations.** ST5 and ST12 declare `406`, `403` and the bare token
  `PCM` as "PCM not licensed", and `406` as "templates not licensed". A 406 is a media-type
  refusal, which `operations/error_translation.py` already reports as an hmcpctl defect
  (#1202). A 403 is missing PCM authority on the connecting user, a prerequisite rather than a
  licence. The bare `PCM` token matches any message that mentions PCM, including a real
  failure.
- **Undisclosed system-wide toggle.** ST12, on round2's default path, flips the managed
  system's `LongTermMonitorEnabled` every run, records it with `state.record` (no assertion),
  and restores only that one flag. Preflight does not mention it. The harness also reads the
  flag as `long_term_monitor` first, a key the tool never returns.
- **Metric feeds answer 406 (defect).** On the boundary system (V10R3 M1060), preferences read
  `LongTermMonitorEnabled=true`, `AggregationEnabled=true`, `EnergyMonitorEnabled=true`, yet
  `hmcpctl metrics show ManagedSystem <sys>` fails with hmcpctl's own "refused the media type …
  (HTTP 406)". `PcmMixin.get_metrics_feed` issues the ProcessedMetrics / AggregatedMetrics /
  LongTermMonitor feed GET through `_get`, which sends the generic UOM Accept
  (`application/vnd.ibm.powervm.uom+xml`) and, when configured, `X-HMC-Schema-Version`. The
  captured 403 fixture `tests/fixtures/live/rest-pcm-metrics-403.json` records that exact Accept
  on a metrics request. The reference documents `application/xml` for these feeds
  (`docs/refs/hmc-rest-api-p10/performance-and-capacity-monitoring/170-processed-metrics-for-managed-system.md:40`,
  `159-aggregated-metrics-for-managed-system.md:46`), and #1202 captured V10R3 answering
  `application/xml` and the UOM type with 406 on the sibling preferences endpoint while
  serving `*/*`. The preferences GET was moved to `raw_get` (`Accept: */*`) then; the feed GET
  was not.

## Operations in scope

| Operation | Request issued | Rows bound | Implemented variant |
|---|---|---|---|
| `pcm.get_preferences` | `GET /rest/api/pcm/ManagedSystem/{uuid}/preferences` | `rest:…/managed-system-pcm-preferences` | `managed-system` |
| `pcm.set_preferences` | `POST` same path | same | `managed-system` |
| `metrics.processed_links` | `GET …/ProcessedMetrics?StartTS…` | `rest:…/processed-metrics-for-managed-system`, `rest:…/processed-metrics-for-logical-partition` | `managed-system`, `logical-partition` |
| `metrics.processed` | feed GET, then the newest JSON link | same | same |
| `metrics.aggregated_links` | `GET …/AggregatedMetrics?StartTS…` | `rest:…/aggregated-metrics-for-managed-system`, `rest:…/aggregated-metrics-for-logical-partition` | same |
| `metrics.aggregated` | feed GET, then the newest JSON link | same | same |
| `template.list` | `GET /rest/api/templates/PartitionTemplate` | `rest:template-library` | `library` |
| `template.get` | `GET …/PartitionTemplate/{uuid}` | `rest:template-library` | `by-uuid` |
| `template.deploy` | `POST …/PartitionTemplate/{uuid}/do/deploy` | `rest:template-library`, `rest:template-library/template-rest-job-api` | `draft-deploy` (unevidenced) |

Excluded, with owners: template capture/check/transform/delete (#644); raw LTM/STM, energy,
SSP and threshold rows (#649–#652); LPAR metrics dynamic parameters (#638); `template.deploy`
positive path (#644, recorded as a gap); editing #108/#110/#111 (maintainer); PCM
authority/role changes (operator).

## Design

1. **PCM request fixes (defects).** `PcmMixin.get_metrics_feed` fetches through
   `raw_get(path)` (`Accept: */*`, no schema-version header), exactly as
   `get_pcm_preferences` does; one client method covers the processed, aggregated and LTM
   feeds. The live run then found two more defects behind the data fetch, fixed the same way:
   `fetch_json` sent `Accept: application/json` (406; `*/*` serves
   `application/vnd.ibm.powervm.pcm.json`), and `newest_metric_link` chose a partition
   sub-feed entry, stamped newer than any document, so it now considers only `.json` links.
   ST38 then found every `set_pcm_preferences` POST answered with HTTP 500 ("Unexpected error
   during unmarshalling"): the update now reads the preferences and posts the HMC's own
   `ManagedSystemPcmPreference` element back with only the requested flags changed, as IBM's
   PCM REST walkthrough does. Each fix has a unit test that failed first.
2. **Declarations.** The three "unlicensed" PCM declarations become module-level authority
   declarations, one per declared operation (`error_codes={"403"}`, variant `pcm-authority`,
   reason "the connecting user lacks PCM authority (HTTP 403)"). The template declaration is
   removed: its `406` is the media-type defect #1202 fixed, and its bare `template`/`templates`
   tokens matched any message naming templates; a template 406 or 403 now records FAIL.
   The runner's startup validator (`_validate_declared_outcomes`) requires each
   `expected=[NAME]` to be a literal list of module-level names and each declared call to be
   recorded by `record_with_expected(5, tool, st, data, [NAME])` in the same async function;
   only the verified branch may go through a helper.
3. **ST5 (round2, read-only)** records through `record_verified` when the call returned, and
   through `record_with_expected` otherwise (refusals stay non-promoting). Each row's label is
   the tool's own name, so observation ids stay unique (`_observation_id` drops a ` (…)` suffix):
   - `pcm.get_preferences` — `five-flags-boolean` (all five fields present and boolean).
   - `metrics.processed_links` / `metrics.aggregated_links` over the last two hours —
     `links-name-system` (each href names the system UUID, case-insensitively; the reference
     file name is `<Category>_<uuid>_…json`). An empty list is a SKIP naming the prerequisite
     (`AggregationEnabled`/`LongTermMonitorEnabled` and collection time), and the matching data
     tool is then a SKIP with the same reason — never a promoting or failed observation.
   - `metrics.processed` / `metrics.aggregated` over the same window, only after links were
     returned — `document-names-system` (`systemUtil.utilInfo.uuid` equals the system UUID),
     `samples-present` (`utilSamples` non-empty), both from the reference JSON specification.
     An empty `{}` result (sample aged out) is a SKIP with the prerequisite.
   - `template.list` — `entries-are-template-summaries` (non-empty; each entry's
     `ResourceType` is `PartitionTemplateSummary`, as `tests/fixtures/live/rest-templates-feed.json`
     captures, and carries a `UUID`). `template.get` on the first listed UUID —
     `template-identity-matches` (the returned entry's `UUID` equals it, case-insensitively).
     No template → SKIP. Unit tests build the list from that capture.
   - The system UUID is `artifacts.system_uuid` when ST1 set it; otherwise one
     `hmc_get_system` read, recorded as a non-promoting row, supplies it. No UUID → the
     metric rows SKIP.
4. **ST12** keeps job inspection only; its PCM toggle is removed from round2.
5. **ST38, arm `pcm` (opt-in).** Group `pcm: [38]`. A bare run selects every `SUBTASKS` key and
   `live_test_runner.py 38` selects it positionally, so ST38 itself SKIPs ("runs only in the
   pcm arm") unless `state.group == "pcm"`, as the profiles arm gates its system-wide steps
   (`scripts/live_test/arms/lpar.py`). Dispatched by
   `scripts/live_pcm.py` (tested by `tests/scripts/test_live_pcm.py`), disclosed by a
   `_pcm_verdict` in preflight: "PCM preferences on managed system X: each of the five
   collection flags toggled, then all five restored to the pre-run read". The scenario:
   1. read the snapshot; all five flags must be boolean, otherwise SKIP with nothing changed;
      the snapshot read is recorded as a results row before the first write, so a finished or
      interrupted `test-results-pcm.json` carries the original values;
   2. for each flag: set it to the opposite value; read back; then restore **all five** to the
      snapshot in one call (the HMC couples flags — enabling aggregation enables LTM) and read
      back;
   3. a final read must equal the snapshot.
   One `record_verified` observation for `pcm.set_preferences` with one assertion per flag
   and `snapshot-restored`. While the snapshot has aggregation on, long-term monitoring (and
   energy monitoring when capable and on) is held by the documented coupling, so its
   assertion is `<flag>-held-by-aggregation` (the write accepted and read back unchanged); every other flag's is
   `<flag>-toggled` (orchestrator ruling 2026-10-06, after the live run held both). `cleanup` is
   `passed` only when the final read equals the snapshot, otherwise `failed` and a
   `MANUAL RECOVERY REQUIRED` row naming the five original values. Intermediate reads and
   writes are non-promoting `state.record` rows.
6. **Catalog.** Rebind rows as tabled. Add nine maturity records: implementation state and
   scope as tabled (`template.deploy` `implemented`, unevidenced: its positive path is #644's),
   evidence copied from the run's observations file, gaps from its gap file.
   Regenerate `src/hmcpctl/_operation_maturity.json` and `docs/tools/`.
7. **Docs.** `docs/live-testing.md` gains the `pcm` arm row and a section: before the run, save
   `hmcpctl metrics prefs ManagedSystem <system>` outside the repo (a hang-up writes no
   results document); after an interrupted or failed run, restore from that read or the
   snapshot row with `hmcpctl metrics set-prefs ManagedSystem <system> --ltm/--no-ltm
   --aggregation/--no-aggregation --stm/--no-stm --compute-ltm/--no-compute-ltm
   --energy/--no-energy --yes`, and do not run the arm again until the flags match (the next
   run overwrites the document and takes the current flags as its baseline). Recovery does not
   witness ST38 (exit 2 expected). The dispatch-range sentence names 38. `CHANGELOG.md`
   records the feed Accept fix (processed, aggregated and LTM feeds share the method; LTM gets
   no maturity claim here — #649) and the arm.

## Failure model

1. **Actors and deployments**
   - CI and developer workstations run the unit suite (no HMC).
   - A local operator runs the live arms against one lab HMC per `docs/live-testing.md`.
   - `hmcpctl` / MCP users of the metrics tools against V10R3 and V11R2 HMCs.
2. **Invariants and assets at stake**
   - The managed system's five PCM collection preferences (system-wide, shared with every
     other PCM consumer): they must equal the pre-run read when ST38 ends.
   - Catalog truth: no observation promotes without assertions over returned data; a refusal
     or prerequisite never promotes.
   - Generated docs and the runtime projection stay in step with the catalog.
   - Public evidence carries no lab identifiers.
3. **Accepted failure classes**
   - A toggle the documented coupling holds is expected and asserted as held; any other
     refused or coupled toggle fails its assertion and the observation records `failed`. The
     restore writes all five snapshot values and the final read decides cleanup.
   - An interrupted ST38 leaving a flag changed: accepted at bounded cost — the snapshot row
     precedes the first write, the runbook's saved pre-read covers a hang-up, and the runbook
     names the restore command and forbids a re-run until the flags match.
   - A V11R2 or other firmware answering `*/*` differently from V10R3: accepted; the live
     check runs on the boundary system only and other levels surface as FAIL rows.
   - Empty aggregated/processed data in the window: SKIP with the prerequisite, never a pass.
4. **Covered elsewhere**
   - Raw LTM/STM, energy, SSP and threshold endpoints — #649–#652.
   - Template capture/transform/deploy positive path — #644.
   - LPAR metric dynamic parameters — #638.
   - Observation staleness after a shared `src/` change — ADR 0127 (derived, reported).

## Verification

- Unit: feed Accept `*/*` (red first); declarations no longer match 406 or bare `PCM`, and
  `_validate_declared_outcomes()` passes over the real modules; ST5 assertion paths (pass,
  empty links → SKIP, empty data → SKIP, 403 → gap, unique observation ids); ST12 makes no
  `hmc_set_pcm_preferences` call; ST38 SKIPs outside the pcm group, records the snapshot row
  before any write, toggles each flag, restores all five, fails cleanup on a mismatched final
  read, SKIPs on a non-boolean snapshot; preflight lists the PCM mutation; wrapper dispatches
  `--group pcm`.
- Live (boundary system, V10R3): preflight; `live_test_runner.py 5`; `live_pcm.py`; recovery
  for each; a five-flag read before and after.
