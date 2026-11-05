# Report utilization configuration classification

## Problem and authority

Issue #1891 and scope comment 6646209173 authorize the exhaustive 19/2 partition.
[ADR 0338](../../adr/0338-report-config-override-classification.md) records that choice.

## Design

Retain ownership in `cli_commands/report.py`; no caller migration is needed.
Replace its literal environment tuple with explicit `_PROFILE_FIELDS` and
`_GLOBAL_FIELDS` frozensets of model field names. Globals are exactly `timeout`
and `schema_version`. The nineteen refused fields are `host`, `port`, `user`,
`password`, `ssh_key_file`, `ssh_verify_host_key`, `verify_ssl`, `ca_bundle`,
`cert_fingerprint`, `trust_system_store`, `upload_timeout`, `max_response_bytes`,
`ssh_timeout`, `audit_memento`, `agent_id`, `authorize_power_operations`,
`iso_url_allowlist`, `iso_build_entry` and `iso_build_arguments`.
Derive `_PROFILE_OVERRIDES` as sorted uppercase `HMC_` names. Existing
`env_var_value` presence checks reject empty values and case variants before
profile selection/client construction. Error rendering stays names-only; the
separate global-option diagnostic remains #1892's work. General loading, schema
and other commands stay unchanged.

## Success and validation

C1/C2: disjoint union equals `HMCConfig.model_fields`; globals equal the exact
approved pair. A controlled added-model-field fault must fail that check.
C3: exercise each of the nineteen names in uppercase and lowercase with an
empty and a nonempty sentinel value; exit 2, named refusal, no value disclosure,
no survey/client loading and no output artifact. Removing a newly refused field
must make its behavioral case fail. Existing report tests retain their contracts.
C4: exercise real `load_profile` on three distinct TOML profiles with both global
exports; prove allowed execution, unchanged profile hosts, and both overridden
values on every opened configuration. Review CLI prose against the exact partition.
C5: focused report tests, `just verify`, pinned prek and amd64 Python 3.11–3.14 CI.
The report fixture clears model-field environment names locally after suite
fixtures; suite-wide security defaults remain intact. No live HMC is required.

## Failure model

- Actors and deployments: local CLI operators and mocked CLI tests on supported
  Python environments; maintained source releases whose tests run before shipping.
- Invariants/assets: each declared model field has one explicit classification;
  per-profile exports cannot retarget credentials, trust, attribution or bounds
  across report profiles; both approved globals continue working.
- Accepted failure classes: a process that mutates its own environment concurrently
  with the synchronous guard/load sequence is outside the CLI invocation contract;
  untested downstream edits can bypass the development-time drift gate.
- Covered elsewhere: general precedence/validation belongs to `config.py` and its
  tests; global-option diagnostic improvements belong to #1892; network behavior
  and output writing remain covered by existing report/client tests.

## Threat model

- Boundaries: existing operator-controlled environment to report admission, then
  TOML/environment to configuration and client construction; none added or widened.
- Actors: local operator/shell supplies exports, including mistaken credentials or
  trust overrides; the running process and installed source are trusted.
- Controls: explicit refused set, existing case-insensitive presence check, exit 2
  before profile loading; failure output lists names and does not echo values.
  Existing configuration validation and client TLS controls govern accepted inputs.
- Out of scope: malicious modification of installed code/process environment,
  general loader changes and other commands; excluded by the frozen charter.
