# ADR 0212: Scope live verification to releases

## Status

Accepted (2026-10-13). Amended 2026-10-15 by #1658 — see *Amendment* below.

Supersedes the currency, catalog-promotion, and scheduled-report policy in
[ADR 0127](0127-derived-live-verification-staleness.md), and the per-commit
verification freshness and runtime age decay in
[ADR 0131](0131-package-operation-maturity-projection.md). Their other contracts
remain in force. Issue #1507 records this decision; #1506 owns its implementation.
This record does not make the planned commands or artifacts available.

## Context

ADR 0127 rejected fingerprinting at release-tag granularity because it moved
re-validation into the release schedule. On 2026-10-13 the operator reversed
that choice, conditioned on a repeatable, explicitly authorized release window.
The operator also approved automatic promotion onto a reviewed branch and
frozen installed verification, replacing manual catalog copying and runtime decay.

## Decision

Live verification is a release stage: select a candidate, run an authorized
window for its claimed operations, record evidence, and validate at the tag commit.
Ad-hoc development windows remain permitted. Their observations enter the catalog,
but affect release eligibility only when the operation's computed closure
fingerprint still matches the candidate and the evidence meets the gate's age
ceiling. They do not change packaged verification between releases or create
tracked re-run obligations during development.

Retain closed-shape observations, clean-tree collection, asserted postconditions
through `record_verified`, cleanup attribution, and operation-keyed evidence.
Only eligible passed evidence promotes to current. After the last arm and clean
recovery, automatic promotion converts observation sidecars into catalog records
on a new branch for pull-request review. This replaces ADR 0127's “never writes
into the catalog — a human copies them in”. Limit that commit to the catalog,
release snapshot, packaged projection and generated `docs/tools/`; exclude raw
results. Promotion neither commits to `main` nor pushes or tags. Each live window
still needs explicit operator authorization naming arms, mutation boundary and
VIOS access when needed; repeatability never supplies that authorization.

Split the packaged projection by authority:

- The operation set, `implementation` and `runtime_eligibility` regenerate from
  the catalog in ordinary pull requests and remain exactly checked against it.
- `verification`, `reason` and `observed_at` come from the current committed
  `docs/capabilities/release-verification.json`, written by release evidence
  promotion. Git retains snapshot history. Seed it from v0.1.0's tagged state.
  Between releases, projection checks compare these fields to that snapshot,
  not changing development closures. Missing snapshot rows preserve the existing
  unevidenced/unrecorded distinction; they do not acquire invented evidence.
- At the tag commit, an explicit release-mode inventory check recomputes the
  verification projection from the catalog and candidate closures and requires
  equality with the snapshot. Release artifact validation opts into that same
  closure/snapshot check; ordinary artifact validation keeps source/package-byte
  checks without requiring release-mode equality during development.

Retain committed versioned claimed sets under `docs/capabilities/releases/`,
starting with `v0.1.0.json`. Each claim identifies an operation and its evidencing
arm and scenario, with their relationships validated. The release gate requires
current evidence for the claimed set only, applying the 90-day ceiling when the
gate is read. Unclaimed stale or unevidenced operations do not block the release.
The claimed operations' closures stay frozen through evidence promotion and tag;
a changed closure requires matching evidence before that operation can pass again.

Installed CLI and MCP discovery retain the tagged verification state without
runtime 90-day decay. Untagged development builds present the last release's
state. Generated tool documentation uses this frozen state without wall-clock
decay, so time alone cannot change its Verification column. This is historical
release evidence, not a claim of current fitness for a particular environment;
authorization and runtime capability checks remain authoritative.

Remove the `verification-report` CI job, including its weekly fail-on-stale step.
Keep the workflow schedule and `python-support-drift` job. Candidate release
gating supplies the age check instead of a development-time clock forcing function.
Replay-contract fixtures derived from live captures are a future direction under
#1506, not implemented or required by this decision.

## Consequences

Ordinary development changes no longer demand live re-runs merely to preserve
presentation currency. Release verification and its age ceiling become a release
responsibility; a pinned install intentionally keeps historical verification even
after 90 days. Ad-hoc observations can reduce release work only while they remain
eligible. The release-mode check preserves correspondence between release evidence
and shipped source, while ordinary checks still detect catalog/projection drift.
Implementation is staged through #1508–#1514; until then the existing code retains
its behavior. This ADR authorizes no live run or tag.

## Amendment (2026-10-15, #1658)

Operation-keyed evidence holds one live row per producing observation, keyed by
its catalog-wide unique observation id, not one row per operation; a
re-produced id replaces its stored row even when its scenario changed. Promotion replaces a stored live row only when the window
produced that observation again; every other stored row is kept until a
reviewed commit retires it. The strict-subset refusal compares a passing
observation with the stored row it replaces. A claimed operation still needs a
passed observation from its declared arm and scenario in the window.

Every reader takes an operation's state from its newest live row, ordered by
`(observed_at, id)`: the release snapshot, `--require-current`,
`verification-report` and the packaged projection derived from the snapshot.
Promotion selects with the same key, so a timestamp tie between two producers
resolves identically everywhere. When a retained row is newer than the claimed
observation — another arm or scenario observed the operation later — readers
report that row, failed or stale included, because it is the newest evidence;
promotion refuses such a claim before creating its branch rather than leaving
the release gate to fail on the promoted catalog.
Two producers that record the same second are ordered by id, not by their
position in the catalog.

## Considered & rejected

- **Keep per-commit currency and weekly stale failures.** judgment: the operator
  chose release-stage verification to avoid recurring development re-run work.
- **Keep runtime age decay in installed releases.** judgment: a fixed release
  should describe its tagged evidence; the release gate owns the age ceiling.
- **Drop closure matching for ad-hoc evidence.** judgment: evidence for changed
  implementation cannot substantiate the candidate merely by being recent.
- **Keep manual catalog promotion.** judgment: the operator chose a repeatable
  window with reviewed branch promotion, retaining review without hand-copying.
- **Keep one live row per operation (#1658).** judgment: an operation with two
  producers in one scenario, such as `lpar.modify`'s shared and dedicated
  paths, lost one producer's evidence on every promotion.
