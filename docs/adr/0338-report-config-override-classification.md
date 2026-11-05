# ADR 0338: Report utilization classifies configuration overrides explicitly

## Status

Accepted for #1891 by the operator's “Approve 19 refused / 2 global” decision.

## Context

The report loads multiple profiles through environment-over-TOML precedence.
Its eight-name refusal tuple omitted eleven of the 21 configuration fields.
Only `timeout` and `schema_version` were documented as report-global.

## Decision

Keep report admission policy in `cli_commands/report.py`: explicit disjoint
field sets classify `timeout` and `schema_version` as global and the other
nineteen current `HMCConfig` fields as per-profile. Derive refused environment
names from the per-profile set. A test compares the partition with
`HMCConfig.model_fields`, so a newly added field requires an explicit decision.
Use the existing case-insensitive, presence-based refusal before profile loading.
The exact partition is documented in `docs/cli.md`; general loading is unchanged.

## Consequences

An exported per-profile field now stops the report even if this read-only survey
does not use it, and even when its value is empty. This includes SSH settings,
audit attribution, response limits and mutation-only settings. Operators must
unset those exports for the report and keep their settings in individual profiles.
The two existing globals retain environment-over-profile precedence.

## Considered & rejected

- **Keep the tuple alone.** verified: at main `c07423d2`, comparing the declared
  fields with the eight refusals and two documented globals leaves eleven fields.
- **Treat every other field as implicitly global.** judgment: contradicts the
  operator-approved explicit partition and would silently allow future fields.
- **Change configuration loading or add model metadata.** judgment: report-only
  policy belongs at report admission; general precedence and schema are excluded.
