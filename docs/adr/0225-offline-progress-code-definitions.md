# 0225: Offline progress-code definitions in the shared reader

## Status

Accepted (2026-10-16), under issue #1690's operator-approved scope and design.

## Context

CLI, MCP and composite inspection already call `ssh.refcodes.list_lpar_refcodes`.
The package must supply POWER9 code definitions offline and retain original code values.
The operator selected independently written definitions rather than publication prose.

## Decision

Ship one JSON catalog with independently written factual definitions and public IBM
page/code provenance. A stdlib resource-based module owns lookup and deterministic
matching. Enrich the existing shared reader with nullable definition and source fields;
presenters render unknowns explicitly. Keep the six-name stable facade unchanged.
Definitions carry no readiness or lifecycle meaning. Source transcription is an explicit
development validation input, never a runtime dependency or tracked provenance URL.

## Consequences

Reference-code rows gain fields; direct type annotations and documentation must agree.
Source-inventory validation and installed-artifact tests protect completeness and offline
availability. Catalog updates require factual review and coverage validation.

## Considered & rejected

- Separate presenter lookups. judgment: duplicate policy and miss composite consumers.
- Runtime downloads. judgment: violate the required offline installed-package behavior.
- Package IBM explanation prose. verified: the publication's Terms and conditions,
  physical PDF page 146, restrict redistribution; the operator selected independently
  written definitions in the issue #1690 working session.
- Leave raw-only codes. judgment: does not meet the requested lookup/display outcome.
