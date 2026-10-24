---
name: hmc-review
description: >
  Repo-specific code review for hmc-mcp. Use when asked to review a diff,
  branch, or PR in this repository, or as the pre-merge review pass. Checks
  changes against the conventions actually enforced in this codebase
  (layering, tool registration, mutation safety, audit, test adequacy,
  optional-dependency boundary). Do NOT trigger for generic style questions
  or for repos other than hmc-mcp.
---

# hmc-mcp bespoke review

Review changed code against the conventions this repository actually enforces.
Every rule below is grounded in a source anchor (`file:line`, as of commit
`e847a23`). Anchors drift — before citing one in a finding, open the file and
confirm the rule still reads as described. If a rule and current source
disagree, the source wins; note the drift in your report.

## 1. Scope the review

1. Determine the diff: PR number → `gh pr diff <n>`; branch → `git --no-pager
   diff main...HEAD`; otherwise the working-tree diff. Never review `main`
   wholesale.
2. Classify each changed file into layers: `client*` / `operations_*` /
   `server_*` / `cli_*` / `tool_registry`, `_app`, `server` (composition) /
   `config`, `audit`, `ssh*`, `documents`, `xmlutil` (core support) /
   `tests/` / `scripts/`, `.github/`, `justfile` (guardrails).
3. Apply the matching checklists below. Sections for untouched layers are
   skipped, except §2 (layering) and §8 (tests), which apply to every change.

## 2. Layering and imports — always check

- Dependency direction: `cli_*`/`server_*` → `operations_*` → `client*`.
  `operations_*` never imports `server_*`; `client*` never imports
  `operations_*`. Presentation modules constructing `HMCClient` directly
  instead of calling an operations function is a smell (existing exceptions:
  `server_lpar_config.py`, `cli_app.py`, `cli_storage.py`).
- All HTTP goes through the `HMCClient._request` waist (`client.py:283`).
  The only sanctioned other `httpx` client is the ISO download in
  `operations_storage.py:353`. A new `httpx.AsyncClient` anywhere else is a
  finding.
- Relative imports (`from .x import`) are the convention. `operations_pcie.py`
  uses absolute imports — that is a known deviation, do not let new code copy
  it. Function-local imports are exceptional (only `server_lpars.py:526` area);
  flag new ones unless they exist to defer an `[app]`-extra import.
- Client mixins declare host requirements as `Protocol` classes in
  `client_contracts.py:29`, not concrete imports.
- `from __future__ import annotations` at module top; `X | None` unions, no
  `Optional[...]` in new code. Module docstring states the module's role in
  the layering (see `client.py:1`).

## 3. Optional-dependency boundary — check on any new import

- `fastmcp`, `mcp`, `rich`, `typer` may be imported ONLY by: `_app.py`,
  `server.py`, `server_command.py`, `server_permissions.py`,
  `tool_registry.py`, `cli_*.py`. All `client_*`, `operations_*`, `config`,
  `documents`, `audit`, `ssh*`, `*_scope`, `api` must stay importable without
  the `[app]` extra — `tests/test_optional_dependencies.py:29` blocks those
  imports with a meta-path finder, and the CI `library-wheel-smoke` job
  installs the bare wheel to prove it.
- `hmc_mcp.api` (`api.py`) is the only supported reusable import surface; its
  `__all__` is the compatibility manifest (ADR 0029). Additions there are a
  contract change — call that out explicitly.

## 4. Client layer (`client*.py`)

- Errors: raise `HMCError` (`errors.py:8`) or its subclasses
  (`HMCTransportError`, `HMCCLIError`) so one `except HMCError` covers both
  transports. Wrap transport/timeout exceptions `from exc`, name method+path,
  and state the remediation including the env var (`client.py:286-296`).
  Validation failures raise plain `ValueError`; authorization failures
  `PermissionError`/`TargetScopeError`. Never error dicts.
- Each HTTP verb asserts an explicit allowed status tuple and raises with
  `(message, status, text)` (`client.py:322`). No retries — single-send is
  load-bearing for streamed uploads (`client.py:426-437`); polling belongs in
  `jobs.py`, validated up front.
- XML parsing uses `defusedxml`; helpers in `client_parse.py` wrap
  `ParseError` into `HMCError` via `_tag_parse_errors` (`client_parse.py:18`)
  and take a `context` argument. Stdlib `xml.etree` is allowed only for type
  annotations and serialization, flagged `# nosec` with rationale
  (`xmlutil.py:38`). Known deviation: `client_storage.py:249,683,724,781`
  calls stdlib `ET.fromstring` on responses — do not let new code copy it,
  and flag any new unflagged stdlib parse.
- Namespace URIs are module constants passed as an `ns` dict;
  `ET.register_namespace` before read-modify-write serialization.
- Document builders escape via the `@escapes_string_arguments` decorator
  (`xmlutil.py:182`, ADR 0042) — never ad-hoc escaping at interpolation sites.
- Session lifecycle: credentials validated in `__init__`; `__aenter__` closes
  the transport if logon raises; `__aexit__` logs off then closes, attaching
  failures via `exc.add_note`; `logoff()` clears token and header in
  `finally`; disabled TLS verification always warns (`client.py:194`).
  Path refusals (`_reject_dot_segments`) live at the `_request` waist, not at
  call sites.

## 5. Operations layer (`operations_*.py`)

- Mutations use the readback pattern (`operations_pcie.py:400-470`): snapshot
  → attempt mutation catching the exception → re-read → build a change-result
  dataclass → raise a partial-error carrying that result if anything cannot
  be verified. Never swallow the mutation error; never report success without
  readback evidence.
- Capability admission fails closed: unadmitted environments/commands refuse
  WITHOUT mutating (`operations_pcie.py:551`, ADR 0053/0056 — only
  evidence-backed, version-labelled IBM-doc fixtures admit a capability).
- Booleans that change authorization semantics are keyword-only after `*`
  (e.g. `ownership_override`, `operations_lpar.py:135`).
- Result/input types are `@dataclass(frozen=True)`, not pydantic — pydantic
  is reserved for `HMCConfig`. Closed vocabularies are exported `Literal`
  aliases.
- Outbound fetches (ISO download): allowlist checked before DNS
  (`operations_storage.py:264`), empty allowlist refuses everything, every
  3xx refused, size cap enforced per chunk, temp file unlinked on any error.
- Error messages are operator-facing sentences: name the offending value with
  `!r`, say what was refused, state the remediation. Enforce this shape on
  every new `raise`.

## 6. SSH command construction (`ssh_commands.py`)

- Two defenses, both required: every interpolated value wrapped in
  `shlex.quote` (`ssh_commands.py:341`), AND attribute records built through
  `build_attribute_record` (`ssh_commands.py:99`), which rejects `"` and `,`
  — quoting is stripped before the HMC parses the record, so shlex alone is
  insufficient.
- Closed vocabularies validated against `Literal`/`get_args` before use.
- New SSH commands must stay inside the admitted command family for their
  capability (ADR 0056); check the exact-command contract test exists (§8).
- Known deviation: `ssh.py:24` sets `known_hosts=None` with no warning —
  do not extend this pattern to new connection paths without flagging it.

## 7. MCP tool surface (`server_*.py`, `tool_registry.py`, `_app.py`)

- Every `server_*.py` opens with `tool, register_tools, tool_security =
  tool_module()` (`tool_registry.py:369`); registration is closure-local and
  the decorator returns the handler unchanged so it stays directly testable.
- `@tool(...)` metadata is keyword-only: `effect` ∈ read / mutate /
  destructive / arbitrary-command; `operation` matches
  `^[a-z0-9_]+\.[a-z0-9_]+$` and is globally unique; target selectors are
  derived from parameter names. `ToolAnnotations` are DERIVED from effect
  (`tool_registry.py:124`, ADR 0035) — hand-written annotations are a finding.
- New domain modules must be added to the `server.py` import block,
  `TOOL_MODULES` (`server.py:260`), and the `X as X` re-export block.
- Naming: `hmc_<verb>_<resource>`; one consolidated tool per operation with
  optional selector, never list/get or update/upgrade pairs (ADR 0003/0004);
  a selector may narrow a collection, never switch its result shape (ADR 0012).
- Public parameter names follow ADR 0025 (`*_name_or_uuid` vs `*_uuid`).
- Docstrings are Google-style with a nonempty `Args:` line per parameter
  (ADR 0016; enforced by `tests/app/test_lifecycle_schema_descriptions.py`).
  Shape: imperative summary → paragraph naming units, defaults, and the exact
  HMC CLI/REST call → sibling-tool cross-references. Reuse the boilerplate
  `profile:` and `limit:` lines verbatim. Destructive tools carry an explicit
  in-prose WARNING and confirmation instruction.
- Returns: reads → `dict[str, Any] | None` or `list[dict[str, Any]]`;
  operation dataclasses converted at the tool boundary with
  `dataclasses.asdict`. Errors surface as raised `HMCError`/`ValueError`.
- Collection bounding: the ONLY limit path is `_run_limited_collection`
  (`_app.py:183`, ADR 0026) — full feed fetched, then sliced; every UOM-feed
  collection tool takes an optional non-negative `limit`.
- Unbounded string arguments need containment validators or membership in
  `UNBOUNDED_ARGUMENTS` (ADR 0044); mutating tools follow the ownership
  protocol with `ownership_override: bool = False` and an audit record on
  override (ADR 0011).
- Server instructions prose derives tool names by regex
  (`_app.py:116`) — never hand-list tool names in `INSTRUCTIONS`.
- Audit sinks are installed only in `server._serve_application`, never at
  import or in `create_mcp`.

## 8. CLI surface (`cli_*.py`)

- One root Typer + per-domain sub-Typers, all `no_args_is_help=True`,
  hyphenated group names. Registration by side-effect import in `cli.py` with
  the `# noqa: F401` comment.
- Connection options live ONLY on the root callback, snapshotted into frozen
  `GlobalOpts`; commands use `_client()` / `_ssh_config()` — never re-declare
  `--host/--user/--profile` per command.
- Output through `_output`/`_print_json` helpers, rich `Table` for humans,
  `err_console` for empties/errors, `--json` option named exactly `as_json`.
- Errors: `_fail` (exit 1), `_usage_error` (exit 2); `_run`/`_with_client`
  re-raise `typer.Abort`/`typer.Exit` before the catch-all.
- Mutating commands take `--yes/-y` and gate on `typer.confirm(...,
  abort=True)`; destructive workflows add `--dry-run` and exit 1 on
  incomplete workflows.
- CLI calls the operations layer directly — the same functions `server_*`
  uses — never MCP tools, and module placement follows resource-domain
  ownership (ADR 0013).

## 9. Config, audit, credentials

- `HMCConfig` fields: every `Field` carries a `description` naming its env
  var; every new `HMC_*` field must be documented in
  `docs/environment-variables.md` (`scripts/check_env_vars.py` gates this) and
  must not use an alias. Validators are private `@field_validator`s delegating
  to module-level free functions so they test without a model; derived values
  are `@property`, never stored fields.
- `password` is a plain `str`, not `SecretStr` — redaction is the caller's
  job. Any new code path that logs, audits, or serializes config must be
  checked for credential leakage.
- Audit: caller-supplied values pass `_value()` (truncated, never `repr()`);
  records are keyword-only, one line of ASCII JSON on the bounded non-blocking
  stderr sink; attribution is always `verified: False` (`HMC_AGENT_ID` is not
  authenticated); `StreamSafeFormatter` escapes control chars so foreign text
  cannot forge a record. The only sanctioned blanket `except Exception` sites
  live in audit (`audit.py:197,439,507`, each `# noqa: BLE001` with a
  rationale) — flag any new one elsewhere.

## 10. Test adequacy (`tests/`)

A new or changed tool/operation is adequately tested only with all four parts
(the SR-IOV precedent):

1. **Exact-command contract test** asserting the literal HMC CLI string and
   the absence of `--force` (`tests/unit/test_sriov_ssh_contract.py:38`).
2. **Operations-layer test** monkeypatching every resolver/authorizer,
   covering partial-failure and capability-error paths.
3. **Server-tool test through the real handler** with respx + fake SSH,
   asserting readback evidence and the returned message.
4. **Registration guards stay green**: every tool needs a `ToolSecurity`
   record (`tests/app/test_tool_security.py`, ADR 0035), capability hints
   (`tests/app/test_capabilities.py`), correct `server_*` module placement
   (`tests/unit/test_server_module_boundaries.py`), destructive scope, and
   public-API tests.

Conventions to enforce in test code:

- `HMCConfig(..., _env_file=None)` always (or `make_config()` from
  `tests/conftest.py:247`); `monkeypatch.delenv` for credential suppression is
  forbidden/redundant.
- HTTP mocking via the shared `mock_hmc` fixture (`tests/conftest.py:260`);
  canned XML as inline module-level string constants with real Atom+uom
  namespaces — never `.xml` files. `mock_uuid_resolution` is mandatory for
  SSH-passthrough tools. JSON fixtures only for HMC CLI field contracts under
  `tests/fixtures/`, each pinned with a doc-URL provenance entry (ADR 0053).
- Dry-run proofs use `assert_no_mutating_requests` /
  `assert_only_these_client_methods_used` — per-route `assert not
  route.called` is rejected.
- pytest-asyncio strict mode: `@pytest.mark.asyncio` on every async test; do
  NOT add it to sync handler-invocation tests. Async collaborators are
  `AsyncMock` patched against the importing module's namespace.
- Shared helpers go in `tests/conftest.py` (imported via `from conftest
  import ...`), never copied. Test names are behavior sentences; module
  docstrings say why the file exists and cite ADRs.
- Coverage config is FROZEN: `[tool.coverage.report]` is exactly
  `{fail_under=90, precision=2}`; no `omit`/`exclude`, no new `# pragma: no
  cover` in `src/hmc_mcp`, no coverage flags in addopts
  (`tests/test_ci_pipeline.py:781` area meta-enforces all of this). Focused
  runs pass `--no-cov` AND name a `tests` path.

## 11. Guardrails and CI

- Verification runs from the branch worktree: `just static` (lint, typecheck,
  secrets, workflow-security, env-vars, nicknames), `just test`, `just smoke`,
  `just verify` before push. Run `uv sync --locked --extra app --link-mode
  copy` first in a fresh worktree. Quiet recipes by default; run gates BARE
  (no piping) — the exit code is the truth.
- Prek hooks are an additional gate beyond `just verify`:
  `UV_NO_SYNC=1 uv run prek run --all-files`.
- CI facts a change must not break: 8-cell matrix (amd64 + arm64 × Python
  3.11–3.14); `library-wheel-smoke` proves the bare wheel imports without the
  app extra; all actions SHA-pinned with `persist-credentials: false`; the
  commented ppc64le job is deliberately retained — never delete it
  (`tests/test_ci_pipeline.py:494`).
- Pre-existing test failures found during review are fixed in the same PR,
  never documented around (AGENTS.md).

## 12. Report format

Produce findings ranked most-severe first. For each finding:

- `file:line` of the offending change (clickable form).
- The convention violated, with its source anchor from this skill (verified
  against current source, per the preamble).
- A concrete failure scenario or consequence.
- The recommended fix, matching the repo idiom.

Severity ordering: security/injection/credential issues → mutation-safety and
fail-closed violations → layering/boundary breaks → contract drift (tool
naming, return shapes, ADR conflicts) → test-adequacy gaps → style/idiom.

End with a verdict: **approve**, **approve with nits**, or **request
changes**, plus the list of guardrail commands you actually ran and their
results. A review with no guardrail run must say so. Findings that are real
but out of scope for the diff become `gh issue create` items, not silent
notes.

## Known deviations registry

Existing inconsistencies — do not propagate, flag any new copy of them:

- `operations_pcie.py` absolute imports (siblings use relative).
- `client_storage.py:249,683,724,781` stdlib `ET.fromstring` on HMC responses
  without defusedxml or `# nosec` rationale.
- `ssh.py:24` `known_hosts=None` without a warning (TLS path warns; SSH does
  not).
- `client_storage.py:23` mid-file aliased re-import (`import re as _re`).
- `server_lpars.py:526,581,624` function-local operations imports.
