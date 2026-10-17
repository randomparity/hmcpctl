# Offline POWER9 progress-code definitions

Issue: #1690. Decision: [ADR 0225](../../adr/0225-offline-progress-code-definitions.md).
Authority: frozen WORK:SCOPE q1690-a73e9b21 on #1690; operator approved independently
written definitions and enrichment in the shared reader on 2026-10-16.

## Problem

Reference-code reads expose opaque values. Installed consumers need definitions without
network access or an operator's reference corpus.

## Scope

Package a JSON catalog under `src/hmcpctl/` and a `progress_codes` module using stdlib
`importlib.resources`. Each entry carries a source notation, independently written
informational definition, applicability section, and physical PDF page locator(s).
Catalog-level metadata identifies IBM's *Power Systems: Progress codes*, copyright
2018/2021, POWER9 applicability and public source URL:
https://public.dhe.ibm.com/systems/power/docs/hw/p9/p9eai_prg1.pdf.
Do not distribute source explanations or repair instructions.

`lookup_progress_code(code: str) -> ProgressCodeDefinition | None` normalizes case and
surrounding whitespace, recognizes a leading documented code with whitespace-separated
suffix data, and returns an immutable definition record or None. It never changes the
caller's original string. Invalid syntax and unknown values return None.

Match exact source notation first, then documented ranges and variable patterns by
specificity (fewest matching concrete codes), then source order for equal specificity.
Only notation documented as variable is variable: X, Y, T and S positions, with their
section-specific meanings retained. Ranges must be explicit inclusive bounds. Short
AIX indicators match the source's documented leading-zero aliases, not arbitrary
prefixes of longer codes. Hyphenated crash sequences remain intact.

The shared `list_lpar_refcodes` reader adds `definition: str | None` and
`definition_source: dict | None` (notation, applicability, public URL, PDF pages).
Its three raw fields remain byte-for-byte unchanged. Typed direct callers adjust to
these richer rows. No authorization, ordering, count, retrieval or lifecycle changes.
CLI readable output uses an explicit unknown-definition label; structured output uses
null. MCP and composite inspection consume the same enriched reader. No new tool or
stable-facade export is added.

Validate coverage against a development-only transcription supplied as an explicit
argument. Extract only factual code notation, section and page inventory; compare it
with the packaged catalog and a committed source-inventory digest/count. Validation
must reject omissions, duplicate identities, missing definitions/locators, and malformed
patterns. Runtime and ordinary tests require neither network nor transcription.

Exclusions: new inventory operations, LED controls, live hardware execution, hardware
coverage beyond the cited reference, and stable-facade changes. Future backlog owner:
maintainer. The current shared reader remains the owner; no caller migration beyond
its richer return type and rendering is needed.

## Success

The finite defined entries in the cited document are represented, including unheaded
entries, ranges and shorter indicators. CA00E105 describes handoff to the OS without
claiming boot success. Unknown/malformed codes remain retrievable and displayable.
Definitions are independently written factual summaries, not reproduced explanation
or repair prose. Every shipped entry has a locator and applicability.
All existing reference-code output paths carry definitions and explicit unknowns.
Wheel and sdist consumers can perform known and unknown lookups outside the checkout.

## Failure model

- Actors and deployments: Python library callers, CLI operators, MCP clients and
  composite inspection consumers; source checkout and installed Python >=3.11 packages.
- Invariants and assets: raw codes/suffixes, successful reads, output shape, offline
  resource availability, accurate bounded informational definitions and attribution.
- Accepted failure classes: codes outside this document are unknown (bounded coverage);
  no hardware-state inference from a definition (not diagnostic authority).
- Covered elsewhere: transport/auth/count guards remain existing reader responsibilities;
  broader inventory is #691; the existing unknown-partition doc correction is #1475.

## Validation

Focused tests cover exact/pattern/range/short-code precedence, ambiguous ties, malformed
and unknown inputs, whitespace/case and suffix preservation; finite inventory coverage,
source metadata and representative factual summaries; shared-reader retrieval failures
and enrichment; CLI text/JSON, MCP and composite output; isolated wheel/sdist lookup.
Validate the catalog against the supplied development transcription, regenerate tool
references, run `just verify` and `uv run --no-sync prek run --all-files`. Hardware
execution is explicitly excluded; use offline consumer and installed-artifact proof.
