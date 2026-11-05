# Report utilization option diagnostics

## Problem

Issue #1892: global TLS options are refused, but the error lists five unrelated
options and the guide omits TLS options. The existing refusal is correct.

## Scope

One unit: replace report.py's fixed diagnostic list with sorted canonical names
from `current_options().command_line_options`, hyphenating underscores as serve.py
does. Keep report as the diagnostic owner; no shared helper or caller migration.
Update `docs/cli.md` and `tests/app/test_report_cli.py`; no ADR is needed.
The operator excludes acceptance behavior/config policy (owner: outside issue
operator). Preserve #1891's 19 refused and two global environment settings,
validation order, exit status, profile loading and survey behavior.

### Failure model

- Actors and deployments: local CLI operators and CI CLI tests on supported Python versions.
- Invariants and assets: name only supplied global option identities; disclose no
  argument values; refusal still precedes profile loading and output creation.
- Accepted failure classes: original alias spelling is not retained by the existing
  canonical option set; report canonical long names, including positive names for
  negative boolean switches, following serve.py. Earlier validation errors remain first.
- Covered elsewhere: CLI parsing and option-source classification belong to app.py;
  environment classification and configuration policy remain outside this issue.

## Success

- C1: When execution reaches the global-option refusal, its diagnostic lists exactly
  the supplied members of the eight-option set, sorted and hyphenated, without values.
  That set is host, user, password, verify_ssl, trust_system_store, ca_bundle,
  cert_fingerprint and profile. Repeated flags appear once; aliases use canonical names.
- C2: The guide lists those eight global options, the `-u`/`-p` aliases and both boolean
  polarities; distinguishes the accepted report-local `--profile` from global `--profile`.
- C3: Existing refusal and accepted report behavior remain unchanged.

## Validation

- C1/C3, Mode: focused-test. Parameterize the public `cli.app` runner in
  `tests/app/test_report_cli.py` over each global option, aliases and negative booleans;
  include mixed and repeated flags, assert exact canonical list and absence of values,
  exit 2, no profile inventory/load/survey and no output. Existing report-local profile
  and no-global success tests cover accepted paths. Red: TLS-only input omits its name
  and includes unrelated names. Green command:
  `uv run --no-sync pytest --no-cov tests/app/test_report_cli.py -q`.
  Verify tests bite by temporarily restoring the old fixed list, then restore the fix.
- C2, Mode: task-test-not-applicable. Guide prose has no executable consumer for its
  option list; manually compare it with app.py's eight callback parameters and aliases,
  without adding a prose snapshot test.
- Integration: `just verify` and `uv run --no-sync prek run --all-files`; CI checks the
  supported Python matrix. No live HMC operation is needed. Revert this isolated diff
  to roll back; the command changes no data format or persistent state.
