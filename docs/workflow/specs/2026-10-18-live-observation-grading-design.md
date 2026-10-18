# Share live observation grading

Full-spec M denominator250, user explicitly approved item2 in ownership-round:
consistent empty assertion refusal and combined assertion/cleanup diagnostics.
Interactive; root owns implementation and final verification. No promotion-owner,
scenario/surface vocabulary, persistence schema, dependency or live execution changes.

## Outcome
Existing observation.py owns grade_assertions(assertions:Sequence[Assertion],
cleanup:str)->tuple[str,str]. It rejects empty assertions with existing promoting
message, validates cleanup against CLEANUP with existing message, gathers failed
assertion IDs in input order, appends cleanup disposition for failed/not-run and
returns PASS/FAIL plus empty note or 'unmet: ' joined failures. Three real callers
are runner.RunState.record_verified, bare_cec._record_held and dedicated_pcie._record_held.
Use exactly one grader; remove each duplicate computation. Runner keeps surface and
scenario validation, sets passed/failed result from verdict, and owns observation
emission. Held callers only state.record; never promote. Reorder validation only
as necessary to reuse helper; no broad runner refactor or new model.

## Failure model
Reject empty input before any record or emission in all callers. Invalid cleanup
never records. One or more failed assertions plus unsuccessful cleanup must expose
both failures, not hide cleanup. Passed/not-required are successful cleanup states;
failed/not-run fail even when all assertions hold. Preserve held result shape and
runner passed/failed semantics, valid assertion IDs, operation and scenario metadata.

## Validation
Mode focused-test: actual three recording paths parameterized empty assertions
must raise and leave records/observations unchanged; old held code fails.
Mode focused-test: promoting failed assertion plus failed/not-run cleanup includes
both failures, old runner lacks cleanup. Cover all four cleanup states and invalid
cleanup through actual callers, with held observations absent and promoting metadata
unchanged. Existing suites tests/test_live_runner.py, tests/scripts/test_live_bare_cec.py,
relevant dedicated PCIe tests and promote-live-observations pass --no-cov. Test external
boundaries only; no live HMC. Controlled empty-input or dropped-cleanup fault must
fail targeted cases. just lint/format-check/typecheck and14actual hooks before commit.
