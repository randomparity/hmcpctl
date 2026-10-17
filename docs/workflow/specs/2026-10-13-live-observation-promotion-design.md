# Live observation promotion

## Problem and scope

Issue #1512 implements ADR 0212's offline conversion after a completed release window.
The runner emits bare observation/gap arrays; its companion results document carries
`run.partial`, `group`, `subtasks`, `tested_commit`, `tree_clean` and `finished`.
Reuse the inventory validator, release claim loader, snapshot renderer and regenerators.
The new script owns conversion and Git orchestration; existing owners need no migration.
Window execution, authorization and recovery are #1513; other exclusions remain in WORK:SCOPE.

## Interface and flow

`uv run --no-sync python scripts/promote_live_observations.py --claims FILE --branch NAME
--window-complete --recovery-confirmed RESULTS...` runs from a clean candidate checkout.
The two flags attest caller-owned completion and recovery; they grant no hardware authority.
RESULTS names completed runner outputs; sibling `<stem>-observations.json` files are derived.
Validate every supplied run before branch creation: non-partial, clean, HEAD commit,
known arm with its full declared subtasks, finished UTC time not in the future, matching
schema, and no detach probe. Reject missing, duplicate or extra arms relative to selected
nonempty validated release claims. This is the last-arm boundary, not a runner lock.
Read JSON with duplicate-key rejection and bounded files (16 MiB results, 1 MiB sidecars).
Never echo JSON, paths, validator payloads or subprocess output in failure diagnostics.
Require exact sidecar row keys, known operations, inventory-valid observations or confirmed
gaps, live channel, canonical timestamp no later than completion, current HEAD and closure.
Join each sidecar entry to a matching recorded result from this invocation by registered
operation, producer-derived identity/outcome (or declared gap reason) and recording interval.
Dispatcher completion comes from run/group metadata; result-row numbers are a separate
producer identity. The shared source-declaration reader binds each operation/result-row pair
to its declared scenario; unresolved or ambiguous declarations fail closed. Apply that check
to both registered tools and recording labels before matching identity/outcome/time.
For non-registry recording labels, reuse source-declared release routes
to validate operation/arm/scenario. Use the producer's second-resolution timestamp floor;
reject missing, stale or mismatched results without copying their raw payloads. Reject
stale/future evidence and duplicate ids within a sidecar; validate catalog-wide ids
after selection. Multiple arms can observe the same observation id (catalog-wide unique; a
re-produced id replaces its stored row even when its scenario changed): keep its latest
timestamp (ties with differing content fail).
Require each claim's newest operation/scenario observation in its named arm's supplied
sidecar to be passed evidence. Replace a stored live row only when the window produced its
id again, keeping every other stored live row, non-live evidence and implementation (ADR
0212, amended by #1658). The strict-subset refusal compares a passing observation with the
stored row of its id. Readers take an operation's state from its newest live row by
`(observed_at, id)`, the selection key; a newer failed or stale row from another arm or
scenario therefore decides a claimed operation's state, so promotion refuses a claim whose
merged newest live row is not passed or carries another closure, before branch creation.
After full validation and selection, omit unclaimed observations whose known
operation lacks a catalog record, reporting its operation ID without raw data. Preserve that
catalog absence; a missing claimed record still fails. Unknown operation IDs and malformed,
stale or mismatched incidental rows fail before omission. This handles the sparse catalog
without inventing implementation metadata.
Merge confirmed gaps only into existing matching missing-scope entries; unknown scopes fail
for manual catalog review. Revalidate the complete candidate catalog before writing.
Render the existing release snapshot from candidate states, regenerate the projection and
tool docs, then run the existing claimed-set gate and release-mode inventory check.

Create the requested previously absent `promotion/` branch before output writes. Validate
branch with Git; reject main/master, detached HEAD, existing branch and dirty tracked/untracked
or staged content. No branch/reset/commit operation targets the original branch.
The commit allowlist is exactly the catalog, snapshot, projection and currently tracked
regular generated Markdown pages under docs/tools/. Refuse symlink files or ancestor directories.
After regeneration reject any changed path outside that set, stage only explicit allowed
paths, check the index again, and commit with a constant message and ordinary hooks.
Inspect the resulting commit paths and clean status. Never push, tag, include raw outputs,
or silently reset failed work. Failure after branch creation leaves that branch and its
changes for operator inspection; report a safe phase-specific error and nonzero status.

## Success and validation

A fixture-clone CLI run creates a new branch and an allowlisted commit with validated catalog,
snapshot, projection and generated docs, while preserving the original branch commit.
Offline tests exercise real Git and the normal validators/regenerators; no HMC calls occur.
Negative tests cover malformed/oversized JSON, duplicate keys, incomplete arms/provenance,
wrong HEAD/fingerprint/scenario, gaps, dirty/index surprises, forbidden paths and symlinks.
Python 3.11–3.14 and existing project dependencies only; no new dependency or schema.
No repository live evidence is generated by implementation or tests.

## Failure model

- Actors and deployments: trusted local operator or #1513 caller, one writer per checkout;
  untrusted or accidentally stale sidecars and raw results in an otherwise trusted repository.
- Invariants and assets: release evidence correctness, source commit identity, private raw
  results, original branch and strict committed-path boundary.
- Accepted failure classes: crashes/disk/Git/hook failure may leave an unpushed promotion
  branch or partial allowed writes for inspection; no automatic rollback risks losing work.
  Concurrent malicious repository/hook mutation and forged operator attestations are outside
  the single trusted writer deployment; JSON validation does not authenticate hardware truth.
- Covered elsewhere: live authorization, last-arm scheduling and clean recovery #1513;
  source claim routes #1511; snapshot schema #1508; age/closure release gate #1478.

## Threat model

- Boundary inventory: new results/sidecar JSON to persisted public evidence; CLI branch/path
  arguments to local Git/filesystem; generated outputs and index to committed tree. No widened
  network or hardware boundary.
- Actor model: local operator trusted for provenance attestations and repository code/hooks;
  input files may be malformed, oversized, stale or contain private raw data.
- Controls: byte caps and duplicate-key rejection; existing closed schemas and source routes;
  timestamp/commit/closure matching; never copy raw result fields; generic errors; argv-only
  subprocesses; clean-tree gate, new-branch check and exact tracked-path allowlist before commit.
- Out of scope: cryptographic run attestation and hostile concurrent writers, as the failure
  model states; live device authorization and recovery remain caller-owned under #1513.
