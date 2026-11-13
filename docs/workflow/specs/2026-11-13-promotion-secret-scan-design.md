# Promotion commits pass secret scanning without hand edits (#1970)

Decision record: [ADR 0381](../../adr/0381-secret-scan-evidence-hex-line-filter.md).

## Problem

`scripts/promote_live_observations.py` commits new `tested_commit` and
`closure_fingerprint` hex values in `docs/capabilities/maturity.json`. The
`secrets` pre-commit hook (`just secrets`) reports each new value. When existing
baselined findings move to other lines, the hook rewrites `.secrets.baseline` and
exits 3. The promotion commit is refused, and v0.2.0 needed a hand-built
baseline and `BASELINED_FINDINGS` edit (#1968).

## Scope

- `.secrets.baseline`: add one `detect_secrets.filters.regex.should_exclude_line`
  filter with exactly the two ADR 0381 patterns. Insert it in sorted path
  position, which is the form `detect-secrets scan --exclude-lines` writes. Delete the
  `docs/capabilities/maturity.json` results. No other change: plugins, the other
  filters, the other results and `version` stay byte-identical.
- `tests/test_ci_pipeline.py`: drop the `maturity.json` `BASELINED_FINDINGS` key. Pin
  the exact `filters_used` list, the two patterns included. Add the controlled-fault
  scan test.
- `tests/scripts/test_promote_live_observations.py`: an end-to-end promotion in
  the existing `candidate` clone, with a `pre-commit` hook that runs the `secrets`
  recipe's scanner command (`uv run --no-sync detect-secrets-hook --baseline
  .secrets.baseline --no-verify --`) over the staged files. Scanning only staged files
  keeps the test about 50 s faster than a full-repository `just secrets`, and the
  promotion's new and moved findings are all in the staged `maturity.json`.
- No change to `promote_live_observations.py`, its commit allowlist, the
  detect-secrets version or the `maturity.json` format (approved exclusions).

## Failure model

1. Actors and deployments: an operator running promotion on a workstation; CI
   (`just secrets` through prek, and `just test`) on hosted runners; contributors
   committing any tracked file.
2. Invariants and assets: a real credential committed to the public repository
   must still fail `just secrets`. Promotion commits only allowlisted artifacts.
3. Accepted failure classes:
   - A secret that is exactly 40 lowercase hex characters under `"tested_commit"`,
     or exactly 64 under `"closure_fingerprint"`, alone on the scanner's processed line (trailing whitespace stripped, YAML
     re-rendered as `key: "value"`), goes unflagged.
     Accepted per ADR 0381: those keys hold Git and SHA-256 digests by contract.
   - The filter applies repository-wide. Accepted: matching is limited to those
     two whole-line shapes.
4. Covered elsewhere: who may edit the baseline is ADR 0002's review rule.
   Evidence validity is checked by `scripts/check_capability_inventory.py`.

### Threat model

- Boundary widened: the secret-scan gate between contributor commits and the
  public repository. It now skips lines of the two shapes above.
- Actor: a contributor or agent who commits a credential by mistake. No
  adversarial committer is modelled, because a committer can already edit the baseline.
- Control: patterns anchored at both ends, with a fixed key and exact length and
  alphabet. A pinned test fails on any filter change. A controlled fault proves a
  credential is still reported when it differs from the shape in any one way:
  a different key, a value one character longer, uppercase hex, trailing content
  on the line, or a non-hex (AWS-shaped) value under an evidence key.
- Out of scope: a hex secret deliberately disguised as one of those digests (the
  accepted class above).

## Success

1. `just secrets` exits 0 on the branch, and the baseline holds no
   `maturity.json` results.
2. Run in the `candidate` fixture clone with that `pre-commit` hook, promotion commits a new `tested_commit` and
   `closure_fingerprint`, with no edit to the baseline or the pin.
3. Each fault case in the threat model is reported by `detect-secrets-hook`
   using this baseline. The two evidence shapes are not reported.
4. `test_secret_baseline_is_an_exact_reviewed_allowlist` passes, and the filter
   list is pinned exactly.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Success 1, 4: baseline and pin agree | focused-test | `test_secret_baseline_is_an_exact_reviewed_allowlist`; `just secrets` |
| Success 3: filter is narrow | focused-test | new `test_secret_baseline_line_filter_is_narrow`. Red: dropping the `$` anchor lets the trailing-content case through. |
| Success 2: promotion passes the hook | focused-test | new `test_promotion_commit_passes_secret_hook`. Red on the old baseline (hook exit 1). |
| ADR and spec prose | task-test-not-applicable | `just adr-numbering` checks only the name and H1. No consumer parses the body. |
