# Paging observation reconciliation

## Decision
Keep observe_paging responsible for its existing sequential reads and local fact extraction.
Move the five cross-plane checks (device names, pool/path pair, current outside pair,
current reader disagreement, configured/runtime pair) into _reconcile(observation, device_names).
The helper is synchronous/pure: no reads, calls to HMC/native collectors or fallback policy.
It receives the existing PagingObservation and already-collected UOM device names list orNone;
the latter is not part of the publicrecord and must not become a newfield/model.
Retain the exact original guard before extracting UOMnames (device andpaths), at its original
point before unique-device-id/service-state extraction. Preserve all collection order andlabels.
Construct existing PagingObservation with all19 keywordarguments in original field/evaluation
order. Reconciliation copies its current invalidtuple, appends existing five tags in original
order, and uses dataclasses.replace to retain every otherfield, including unavailable.
All per-planeinvalid checks stay at collection. No collector class/framework or helperlayers.

## Failure model
Required: unknown native/configured/UOM evidence stays independent, not repaired by another
plane; unavailable and invalid may coexist; no extra/missing requests, weaker containment,
identity, pool/provider, dedication/redundancy/readers checks, labelreordering or changedTLSstop.
Keep unknown/False/None distinctions, sorted provider/dedication/path bindings, original system
UUID normalization and finite positive timeout. Existingmodel shape and downstream classification
stay exact. Accepted: this offline control-flow refactor makes no new hardware capability claim.

## Validation
Task-test-not-applicable: no new externalbehavior or independentstructuralcontract; existing
real paging observations/mutations/error/TLS tests protect the retained semantics. Do not add
privatehelper-name or code-shape mirror tests. Run baseline existing paging suite before edits.
Capture actual observation results/exceptions plus transparent existingread-boundary traces
before/after (record-only wrappers, no fake logic), and compare complete JSONrecords andorder.
Audit unchanged19fielddeclaration and originalpositional-to-keywordbinding/evaluation equivalence,
rawexisting comparisonconditions/messages after only observed-field binding relocation.
Prove current-readers-disagree and independent invalid/unavailable cases bite with a controlled
runtime skipped-reconciliation fault, then restore andrerun; no trackedsource mutationduringtests.
Run tests/lpar/test_paging_observation.py and test_paging_switchover.py, existing SSH/presentation/
offlinearm/wrapper/runner/recovery consumers. Lint/type/format/diff/smoke andnormal14hooks before
conventionalcommit; rootfullverify/pinnedhooks/wholebranchreview remain. Nohardware.
