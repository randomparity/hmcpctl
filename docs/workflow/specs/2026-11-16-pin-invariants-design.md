# Version-independent CI pin checks

## Problem

Issue [#2005](https://github.com/randomparity/hmcpctl/issues/2005) requires normal
Dependabot pin updates to pass without editing literal copies in CI policy tests.

## Scope and design

Keep policy ownership in `tests/test_ci_pipeline.py`. Replace `TOOL_PINS`,
`ACTION_PINS`, and `SCORECARD_ACTION_PINS` with required name sets. Two local
assertion helpers serve the existing quality-tool, CI, and Scorecard tests.
No caller migration, runtime interface, dependency, or workflow edit is needed.
A shared requirements utility would widen ownership for one policy consumer;
removing pin validation would lose the issue's required security checks.
The issue already selects invariant validation over literal copies; no new ADR
is needed. Reserved ADR 0384 remains unused.

The tool helper extracts requirement names, normalizes package-name spelling,
and requires each named tool exactly once with a plain `name==version` pin.
The version token follows the existing exact-requirement grammar in
`tests/test_supply_chain.py`; no particular version is authoritative here.
The action helper preserves the previous broad discovery of literal `uses:`
occurrences in active YAML and the commented ppc64le template. Each discovered
value must match the complete same-line pin/comment grammar; values outside it
fail closed. Names must equal the workflow-specific required set;
each occurrence must use a lowercase 40-hex ref and end with two spaces plus
`# vN` or `# vN.N.N`. Major-only comments preserve the existing setup-just pin.
Repeated uses of one action remain valid, with each occurrence checked.

## Success

Version/SHA-only edits to the named tools/actions pass without changing tests.
Missing, duplicate, or non-exact required tool pins fail. Missing/extra action
names, tag/short/nonhex refs, and malformed/missing version comments fail.
Isolated regression cases prove both permissive updates and rejected regressions.
The full local gates and pinned prek pass; CI supplies its declared Python legs.

## Failure model

- Actors and deployments: contributors and Dependabot editing the current dev
  requirements and the existing plain `uses:` spelling in CI/Scorecard workflows;
  offline local and hosted tests, including inline flow mappings of that spelling.
- Invariants and assets: exact required-name membership, unique tool requirements,
  exact tool pins, full action SHA pins, and human-readable version-comment syntax.
- Accepted failure classes: none for discovered literal `uses:` values outside
  the required name/ref/comment grammar; those fail closed. Other YAML key
  spellings remain outside the scanner grammar; no general YAML parser is introduced.
- Covered elsewhere: upstream SHA/version correspondence belongs to the maintainer's
  separate network-backed verification proposal, as approved by the operator.

## Threat model

- Boundary inventory: repository-edited requirements and workflow text enter offline
  policy assertions; no new entry point, permission, secret, or network call.
- Actor model: contributors/Dependabot can change these files; reviewed policy name
  sets remain the explicit trust anchor.
- Controls: check names independently from versions, validate each pin/comment,
  reject discovered values outside the pin/comment grammar with assertion
  evidence; report source text only locally.
- Out of scope: upstream action contents and SHA/comment correspondence, owned by
  the separate maintainer verification proposal; workflow execution security remains
  with the existing workflow-security gate.

## Validation

The tightly coupled Cast plan inventories tool and action contracts separately.
Run focused positive/negative policy cases, controlled source mutations, `just test`,
`just verify`, and `uv run --no-sync prek run --all-files`. No HMC arm runs.
