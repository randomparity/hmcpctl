# Release-scoped verification runbooks

## Problem

Issue #1514's three runbooks still describe per-commit currency and hand-run
promotion. Accepted [ADR 0212](../../adr/0212-release-scoped-live-verification.md)
and the landed release scripts now define a release-scoped workflow.

## Scope

One coordinated prose update in `docs/development.md`, `docs/live-testing.md`,
and `docs/capabilities/README.md`, with an Unreleased changelog entry. Existing
runbooks retain ownership; replace obsolete instructions rather than add a
parallel procedure. No implementation ownership transition or new ADR is needed.
The approved exclusions remain generator #1509, CI #1510, snapshot #1508,
claims #1511, gate #1478, baseline #1479, promotion #1512, window #1513;
live execution, tags and releases belong to the operator.

### Failure model

- Actors and deployments: contributors reading these runbooks; authorized release
  operators running installed commands from a configured candidate worktree.
- Invariants and assets at stake: claimed-closure freeze, explicit hardware and
  VIOS authorization, clean recovery, private raw evidence, truthful release claims.
- Accepted failure classes: no hardware execution proof from this prose change;
  it neither runs hardware nor creates a release, as the approved exclusions require.
- Covered elsewhere: script validation and execution belong to the landed dependency
  issues above; hardware access and tag authorization belong to the operator.

## Success

The three runbooks distinguish frozen packaged verification from candidate
currency, explain the committed claim triples and 90-day gate, and document
release-mode inventory validation at tag steps 2–3. The release procedure keeps
claimed closures frozen and uses the actual release-window invocation, independent
baseline, per-arm recovery, evidence generation and reviewed allowlisted promotion.
The live guide preserves named-arm recovery details and identifies CLI coverage
and explicit gaps without claiming all wrappers were verified. Ordinary development
requires no live rerun or rerun issue; ad-hoc windows remain operator-authorized.

## Validation

- `task-test-not-applicable`: the three runbooks and changelog change human-facing
  prose only, with no new executable or machine-consumed contract. Wording snapshots
  would not establish command correctness. Read the actual parsers and execution
  paths, check documented examples using offline help/dry-run where applicable,
  and review links, phase ordering, authorization and failure instructions.
- Run `just verify` and `UV_NO_SYNC=1 uv run --no-sync prek run --all-files` on the
  final candidate, then require its CI checks. Do not contact an HMC for validation.
