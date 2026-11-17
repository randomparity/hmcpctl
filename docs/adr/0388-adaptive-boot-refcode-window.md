# 0388 — Widen saturated boot refcode windows and report incomplete history

## Status

Accepted (2026-11-16), issue #1911. Supplements ADR0326 Decisions5–6's polling
window and observation bound; its baseline identity and activation policy remain.

## Context

A boot-started code can leave provision's newest20 rows between polls. Source at
main51bd8ce56 uses a20-row poll while the shared SSH adapter caps requests at100.
The HMC reference's `lsrefcode -n` definition supplies ordering and short-result
semantics, but no rate or retention bound. Strict parsing rejects malformed rows;
metadata enrichment does not discard records.

## Decision

Inspect20-row polls for qualifying evidence first and return success immediately.
Only without success, when saturated and lacking the baseline, reread up to the existing
100-row cap. Search for newer boot evidence before diagnosing a gap. A full maximum
window lacking both baseline and qualifying evidence ends `needs_attention` with an
explicit incomplete-history reason. Short reads follow the existing timeout behavior.
Null baselines use the same saturation rule. Keep these choices in the boot-wait
owner; its private read callable takes the requested count.

## Consequences

A poll can perform two SSH reads and retain up to100 observed identities. This
recovers evidence beyond row20 within the wider read, without claiming100 covers
an undocumented worst case. HMC retention or arrivals during/before rereads can
still prevent reconstruction; the saturated-gap result states that uncertainty.
No persisted or public interface changes and no additional activation occurs.

## Considered & rejected

- **Do nothing.** verified: issue #1911 identifies the newest20-row polling gap in
  `provisioning/boot.py` at main51bd8ce56; the criterion requires widening or adequacy.
- **Always read100 and claim sufficient coverage.** verified: the adapter at
  `src/hmcpctl/ssh/refcodes.py:15` caps100; the HMC reference supplies no emission-rate
  bound. Judgment: a conditional read preserves normal read cost and names uncertainty.
- **Remove the shared cap or page indefinitely.** judgment: expands a shared read
  contract without a documented history/offset protocol; bounded widening meets the
  requested detection alternative and keeps failure explicit.
