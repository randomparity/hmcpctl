# Paging-VIOS switchover: one forward attempt and separate restoration

This procedure exercises ST66, group `lpar-paging-switchover`, scenario
`st66-lpar-paging-switchover`. It requires a separately authorized hardware window.
Preparing or merging the feature grants no hardware access. CI uses boundary mocks.

Use an existing running shared-memory consumer with positive AMS capability,
configured primary/secondary paging VIOS identities and independently observed
current redundant assignment to that same unordered pair. Both paging providers
must be dedicated-memory VIOS partitions contained in the selected managed system.
The consumer and both providers require positive matching ownership stamps and
must be outside the protected inventory. This arm creates, activates and allocates
nothing; it changes no pool or device configuration. Optional runtime fields that
are unavailable prevent admission. No release floor, RMC matrix or device-state
usability rule is inferred from schema vocabulary. HMC decides undocumented final
eligibility limits after the required client checks.

Configure the authoritative private `.env` with the selected connection, system,
`LIVE_TEST_LPAR_NAME`, `LIVE_TEST_PAGING_EXPECTED_CURRENT_VIOS` and
`LIVE_TEST_PAGING_WRITER_WINDOW=true`. The last setting acknowledges a coordinated
window excluding other writers; neither an ownership stamp nor `--yes` supplies it.
The discovered provider requires MCP all-targets authority as well as connection
policy. There is no ownership override.

Before the window, use a clean committed checkout with `just setup` completed.
The runner serves the local application in process and checks its source path,
commit and tree against that checkout. Choose a new canonical absolute results
path in a mode0700 directory outside every repository. Raw results contain private
system identities; files are written mode0600. Example directory setup:

```sh
RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/hmcpctl-paging.XXXXXXXX")
chmod 700 "$RUN_DIR"
```

Run the configuration/hardware preflight, then the named wrapper only after the
operator's authorization and the exclusive writer window are in effect:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-paging-switchover
uv run --no-sync python scripts/live_lpar_paging_switchover.py --results-file "$RUN_DIR/results.json"
```

Record each exit status. Before the effect, the arm durably checkpoints the original
configured/runtime vector, current provider, expected-current intent, destination
and clean deployed commit/tree. It dispatches exactly one forward tool attempt.
An acknowledgement cannot establish completion; independent after facts determine
the product outcome. Failure, timeout or interruption never authorizes replay.
If the child remains alive, terminate and account for it before recovery; an
in-flight native command can still complete. Do not start another attempt to
resolve uncertainty.

After child termination, always run the independent read-only recovery command,
including after an apparently successful forward attempt:

```sh
uv run --no-sync python scripts/live_test_recovery.py --paging-switchover --results "$RUN_DIR/results.json"
```

Exit0 confirms the original vector/current provider at the readback instant for a
complete bound record. Exit1 means observed drift, including the expected forward
change of current provider. Exit2 means unverified or unreadable evidence; an
interrupted record cannot establish a completed attempt or absence of late effects.
Missing baseline/provenance or unavailable runtime facts never mean clean.
Recovery imports no writer and cannot reverse the operation. It compares against
the original baseline without editing the results. Preserve its output privately
as separate evidence.

A successful forward attempt deliberately remains held because the current provider
changed. Restoration requires **separate explicit operator intent**, a new exclusive
writer window, fresh authorization and current observations. Use the ordinary
`hmcpctl lpars paging-switchover` command with the original consumer/system and the
**now-current** provider supplied by `--expected-current-paging-vios`, together with
`--exclusive-writer-window`. Retain this separately authorized attempt's output and
exit status in its own private journal. The arm, recovery and evidence exporter
never perform this reverse automatically. If restoration is refused or uncertain,
hold the fixture and escalate through the operator's recovery process.

After the separate restoration child terminates, rerun read-only recovery against
the **original** forward results. Keep the original forward failure/drift history
unchanged. A later confirmation is separate evidence, not a rewritten passing run.
Unresolved restoration cannot promote live maturity. This arm records the forward
facts but does not mark its held scenario passed.

Render the original results with the existing filtered exporter when sharing an
account of the attempt:

```sh
uv run --no-sync python scripts/live_test_evidence.py "$RUN_DIR/results.json"
```

The exporter stamps the tested commit and excludes raw identities. Read its full
output before public use. A matrix applies only to the commit it exercised; it
cannot establish unrun contexts or later restoration, and raw results are never
public artifacts.
