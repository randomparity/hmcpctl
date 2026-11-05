# ADR 0338: Report utilization classifies configuration overrides explicitly

## Status

Accepted for #1891 by the operator's “Approve 19 refused / 2 global” decision.

## Context

The report's eight-name refusal tuple omitted eleven configuration fields.
Only `timeout` and `schema_version` were documented as report-global.

## Decision

Keep explicit disjoint field sets in `cli_commands/report.py`: `timeout` and
`schema_version` are global; the other nineteen current fields are per-profile.
Derive refused names from that set, retaining case-insensitive presence refusal
before profile loading. Compare the partition with `HMCConfig.model_fields` in
a test so future fields require a decision. Document the partition in `docs/cli.md`.

## Consequences

Per-profile exports stop the report even when empty or unused by this survey,
including SSH settings, attribution, response limits and mutation settings.
Operators must unset those exports and keep settings in individual profiles.
The two globals retain environment-over-profile precedence; general loading is unchanged.

## Considered & rejected

- **Keep the tuple alone.** verified: main `c07423d2` declares 21 fields but the
  eight refusals and two documented globals account for only ten.
- **Implicit globals.** judgment: contradicts the approved explicit partition.
- **Change loader/model metadata.** judgment: report policy belongs at admission.
