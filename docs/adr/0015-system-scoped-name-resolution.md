# 0015 — Fail closed on ambiguous HMC names

## Status

> **Superseded in part by [0226](0226-scoped-lpar-inspection.md)** (2026-10-16): supplied-scope UUID containment.

Accepted (2026-08-14)

## Context

Partition names are unique only within a managed system, while the HMC search
feed spans every managed system. Existing convenience finders return the first
search result, so a destructive name-based operation can silently target a
different system. Failing on duplicates alone would make valid duplicate names
unusable because the resolvers currently accept no managed-system scope.

## Decision

Name finders return `None` for no match, return the entry for one match, and
raise `ValueError` for multiple matches. Ambiguity errors enumerate candidate
UUIDs; partition and VIOS errors also identify each candidate's managed system.

LPAR and VIOS finders accept an optional managed-system UUID. A scoped lookup
uses the managed system's child-resource collection and exact name matching.
The shared UUID resolvers accept an optional system name or UUID, resolve it
first, and pass its UUID to the finder. Destructive LPAR and VIOS tools expose
or reuse that selector, including LPAR delete and rename. UUID resource selectors remain pass-through values:
the system selector is ignored for UUID resources and is not a parent-system
validation or authorization boundary.

An ambiguous partition result is reported only after every candidate's parent
has been resolved. Parent-discovery failure propagates as an actionable lookup
failure; it never emits a partial ambiguity diagnosis or returns the first match.

## Consequences

Unscoped duplicate-name calls fail before mutation and explain how to
disambiguate. Scoped calls add a managed-system lookup and child-collection
request. Diagnosing an unscoped ambiguity may query managed-system child
collections to associate candidates with parents, capped at 100 systems per
request and a 30-second aggregate deadline; larger or slower inventories require
explicit system scope. Existing single-match and
no-match behavior remains unchanged. Operators must not treat the optional
system selector as a guard for resource UUIDs; it disambiguates names only.

## Considered & rejected

**Keep selecting the first result.** Feed order is not a safety boundary and
can direct a destructive operation at the wrong partition.

**Fail on duplicates without adding scope.** This is safe but makes legitimate
duplicate partition names impossible to address by name.

**Thread system scope through every name-based tool now.** Most tools become
safe by failing closed; widening every public signature exceeds issue #140.
