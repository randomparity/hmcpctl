# Selected recovery checks
Full-spec M denominator250; scope is operator-approved correction5 in proposed-seven-finding-contracts.md.

## Charter
Interaction: interactive. Scope identity: recovery selected checker/input protocol.
Outcome: construct selected pairs once instead of forwarding seven parallel inputs.
Completion: C1–C4 and meaningful selected-check, aggregation and read-only evidence.
Provenance: user answered “Approve all five scopes” to correction5; exact proposal retained privately.
Exclusions/owners: saved-document models/decoders remain existing config/recovery owners;
read-only policy and served-client composition remain recovery/runner; no live execution,
framework, dependency, model or output-policy change. These are the approved preservation bounds.
Surface: scripts/live_test_recovery.py orchestration and its direct test callers.
Ambiguities: retain users as explicit keyword boolean, preserving its distinct error aggregation.

## Success
C1: main builds an immutable tuple of selected (checker,input) pairs after existing
input decoding, filtering only None. Order is check, check_test_partition,
check_vios_backup, check_network, check_lpar_config, check_lpar_power, check_round2.
Pairing ownership moves from check_run to main; all decoded models stay unchanged.
C2: _run_checks(checks, *, users=False) forwards to
check_run(call, checks, *, users=False), removing old parallel argument paths.
Use a private plain tuple type alias only if necessary; no class/registry/framework.
Callable[..., Awaitable[list[Finding]]] plus object is sufficient for heterogeneous inputs;
retain existing runtime checker behavior and do not introduce validation/coercion.
C3: users executes first. Its StateUnreadable contributes its message only, as today.
Each selected arm contributes successful findings, or StateUnreadable.findings plus its
message, continues siblings, then raises joined StateUnreadable if any unread message.
No selected arm is skipped because another failed. Other exceptions propagate unchanged.
C4: bootstrap iff users or selected pairs; connection composition, read-only wrapper,
MutatingCallRefused handling, CLI exits0/1/2, output, partial/unwitnessed and round2
error handling stay unchanged. Saved-document decoding remains before selection.

## Failure model
Actors: operator invoking saved-run recovery; empty/one/multiple arms, users,
unreadable arms with partial findings and attempted mutating checker calls.
Assets: conservative clean verdict, full known findings, deterministic ordering,
read-only enforcement and correct arm/input association. Accepted classes: none newly.
No generalized runtime checker registration or external callers promised.

## Validation
C1/C2 focused-test: direct check_run selected pairs and main captured selection/order,
input identity, omitted None and empty/users-only bootstrap behavior. Old parallel
signature fails selected-pair call before production edit.
C3 focused-test: users first, mixed success/unreadable arms with partial findings,
continued siblings and joined errors; preserve distinct users-only aggregation.
C4 focused-test: _run_checks actual read-only caller refuses mutation and served-client
composition remains; existing main exit/output/round2/recovery tests migrated without
weakening assertions. Controlled skipped-sibling fault must fail then restore.
Run uv run --no-sync pytest tests/scripts/test_live_test_recovery.py
tests/test_live_round2_recovery.py -q --no-cov plus affected callers discovered by rg.
Run lint/format/type/all14 hooks; root owns final full verification and review.
