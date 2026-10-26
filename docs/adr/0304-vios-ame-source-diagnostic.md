# 0304: Registered source-only VIOS AME diagnostic

## Status

Proposed — #1768 diagnostic prerequisite; no implementation or hardware evidence.

## Context

The retained P10/P11 creation grammar permits dedicated memory and AME0 or a
positive factor in the partition-create attribute family. It does not resolve
the VIOS value matrix. The separate VIOS operation currently accepts neither
C07 input. The operator approved a registered source diagnostic to answer that
question before the product design continues. The diagnostic must not close
#1768, supply product maturity evidence or revise the original M250 scope.

## Decision

Add the separately named `vios-ame-source` diagnostic using the existing source
preflight/capture/recovery registration pattern, outside numbered product arms.
It makes at most two sequential fresh inactive VIOS attempts: dedicated memory
with AME0, then1.10. One native creation record carries the initial validated
owner/caller description. There is no post-create stamp, activation or pool write.

Bind exact source, target, private packet, current grant and finite budgets.
Validate the complete typed collection envelope at the counted GET boundary before
shared readers flatten it. Require the exact collection SELF path; hold on unknown
continuation, truncation or collection shape, with no follow-up fetch. Only the
retained empty memory-pool204 sentinel is admitted.

Read contained VIOS/profile state independently. Delete once by UUID only after
positive native completion, initial marker, fresh identity, inactive state and
foreign baseline agree. Ambiguous completion or ownership leaves a held residual;
recovery observes every phase and performs no mutation. Preserve source-only
attribution in the journal and renderer; do not invoke ordinary promotion.

## Consequences

Configuration acceptance is evidence for the observed HMC/firmware and input
only. It does not prove guest compression or universal product applicability.
An unobservable field, changed baseline or exhausted bound stops effects.
Same-record description does not make HMC creation transactional: cleanup can
remain unavailable. Root must bind a fresh window after design/code proof.
Existing product APIs, source arms and C07 artifacts retain their contracts.

## Considered & rejected

- **Instrument ST58.** verified: at1aad1376, `creation_memory_layout.py:553–603`
  runs an LPAR phase and records C06 scenarios; `config.py:647–767` closes its
  journal to C06 fields. The new diagnostic preserves truthful independent scope.
- **Use post-create stamping.** verified: `ssh/lpar.py:99–144` performs a separate
  best-effort description write and can return None after creation. That cannot
  establish the prerequisite's initial-marker cleanup predicate.
- **Add product C07 support first.** judgment: it would embed the unresolved
  VIOS applicability decision in the behavior the experiment is meant to inform.
- **Obtain only published evidence.** judgment: a valid alternative, but the
  operator selected this bounded diagnostic; its output still requires analysis.

- **Trust flattened collection lists.** verified: core._get/list_child erase empty200
  versus204, while xmlutil.parse_feed discards feed continuation metadata. The local
  physical response guard is necessary before these shared readers run.
