# C12 IBM i explicit HMC-console creation proof

Follow [live-testing](../live-testing.md) and the root-approved exact candidate
window. Nothing in this procedure grants access. The opt-in `lpar-physical-io`
group is arm49 (`st49-lpar-physical-io`); arm48 is reserved independently.

Confirm the isolated deployed checkout's HEAD, interpreter, imported module path,
and regular private configuration copy. Keep credentials, identities, raw results
and comparison snapshots private. Require the existing `hmcpctl-live-lpar-` scratch
prefix absent and other writers quiescent. Capture an independent system baseline.

The arm calls MCP `hmc_create_lpar` exactly once for a run-unique inactive OS400
fixture with explicit 1024/2048/4096 MiB and 0.1/0.5/1.0 shared processor units,
1/1/2 virtual processors, `physical_io={"console_slot":"hmc"}`, caller token and
`apply_partition_profile=False`. It requires confirmed ownership and successful
native default-profile I/O readback. It never supplies an implicit console,
activates, applies a profile, assigns physical/SR-IOV resources or changes another
partition. Physical/SR-IOV/pool/OptiConnect positive cases remain not-run until
separately approved eligible-resource packets can exercise them. Three positive
virtual-slot forms without creation-time adapter linkage remain a #638 gap.

Set `WINDOW_PRIVATE` to a fresh physical mode0700 directory outside the checkout;
`sys-R1` is an anonymous placeholder for the approved system:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-physical-io
uv run --no-sync python scripts/live_test_baseline.py capture \
  --system sys-R1 --output "$WINDOW_PRIVATE/before.json"
uv run --no-sync python scripts/live_test_runner.py --group lpar-physical-io \
  --results-file "$WINDOW_PRIVATE/results.json"
uv run --no-sync python scripts/live_test_recovery.py \
  --results "$WINDOW_PRIVATE/results.json"
uv run --no-sync python scripts/live_test_baseline.py capture \
  --system sys-R1 --output "$WINDOW_PRIVATE/after.json"
uv run --no-sync python scripts/live_test_baseline.py compare \
  "$WINDOW_PRIVATE/before.json" "$WINDOW_PRIVATE/after.json"
```

Recovery and independent after comparison run on every outcome, including failed
or uncertain create. Teardown reuses the established matching-token inactive-only
helper; active, foreign or unreadable state holds for manual recovery and never
causes a power action. Recovery independently reports a stranded scratch prefix.
No automatic retry or rollback is permitted. Keep the target window held until
recovery is clean and the independent baseline matches, or the root accepts the
exact residual. Bound the arm to one fixture and 15 minutes, plus five minutes for
recovery; configured finite transport timeouts must fit the approved packet.

Use `scripts/live_test_evidence.py` for commit-stamped allowlisted evidence. No raw
results may be posted. An actual matching-commit pass proves only this inactive
HMC-console arm, not P10/P11 native execution, device allocation, activation or guest
boot; failed/skipped/unrun arms are not passing evidence.
