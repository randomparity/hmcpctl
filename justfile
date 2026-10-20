# hmcpctl — canonical commands
#
# These give the workspace a single entry point for "suite green":
#   just verify   # full ad-hoc verification (tests + MCP handshake + CLI)
#   just test             # quiet suite summary
#   just test-verbose     # pytest diagnostics and missing-lines coverage
#   just smoke             # MCP handshake / tool count
#   just smoke-verbose     # MCP handshake / exposed tool names
#   just tool-docs         # regenerate docs/tools/ from the registry
#   just tool-docs-check   # fail when docs/tools/ has fallen behind the registry
#   just doc-freshness     # fail when any generated document has fallen behind

# synchronize locked dependencies and install repository hooks
setup:
    uv sync --locked --extra app --link-mode copy
    uv run --no-sync prek install
    uv run --no-sync python scripts/link_reference_corpus.py

# Python lint
lint:
    uv run --no-sync ruff check .

# fail when any Python file differs from ruff format's output (Markdown excluded)
format-check:
    uv run --no-sync ruff format --check .

# type-check the explicit clean production-module boundary
typecheck:
    uv run --no-sync ty check

# scan every tracked file against the reviewed fixture baseline
secrets:
    git ls-files -z | xargs -0 uv run --no-sync detect-secrets-hook \
        --baseline .secrets.baseline --no-verify --

# audit GitHub Actions without depending on mutable remote state
workflow-security:
    uv run --no-sync zizmor -qq --no-online-audits .github/workflows/

# verify every HMC_* env var in HMCConfig is documented
env-vars:
    uv run --no-sync python scripts/check_env_vars.py

# verify the committed config fixture has a well-formed nicknames table
nicknames:
    uv run --no-sync python scripts/check_nicknames.py

# reject conftest files that shadow the canonical test configuration
test-layout:
    uv run --no-sync python scripts/check_test_layout.py

# verify the capability ledger remains structurally valid and reconciles the registry
capability-inventory:
    uv run --no-sync python scripts/check_capability_inventory.py

# regenerate the packaged operation-maturity projection
capability-metadata:
    uv run --no-sync python scripts/check_capability_inventory.py \
        --write-runtime-projection src/hmcpctl/_operation_maturity.json

# report each operation's derived live-verification state (ADR 0127)
verification-report *ARGS:
    uv run --no-sync python scripts/check_capability_inventory.py --verification-report {{ARGS}}

# regenerate docs/tools/ from the MCP tool registry
tool-docs:
    uv run --no-sync python scripts/gen_tool_reference.py

# verify the committed docs/tools/ still matches the registry
tool-docs-check:
    uv run --no-sync python scripts/gen_tool_reference.py --check

# regenerate scripts/live_test/scenario_definitions.json from the capability ledger
scenario-definitions:
    uv run --no-sync python scripts/gen_scenario_definitions.py

# verify every decision record in docs/adr/ carries a unique number
adr-numbering:
    uv run --no-sync python scripts/check_adr_numbering.py

# verify every generated document still matches its declared regeneration command
doc-freshness:
    uv run --no-sync python scripts/check_generated_docs.py

# verify HMC-shaped literals and read paths in src/ and tests/ match captured vocabularies
live-vocabulary:
    uv run --no-sync python scripts/check_live_vocabulary.py

# fail when a live-scenario dispatch is unregistered, mismatched, or unreadable (offline)
scenario-gap:
    uv run --no-sync python scripts/scenario_gap_report.py --fail-on-dispatch

# local and hosted static-analysis gate
static: lint format-check typecheck secrets workflow-security env-vars nicknames test-layout \
        capability-inventory tool-docs-check adr-numbering doc-freshness live-vocabulary \
        scenario-gap

# run the full pytest suite with one semantic summary
test:
    uv run --no-sync python scripts/run_tests.py

# retain successful pytest diagnostics and the 30 slowest test phases
test-timings:
    uv run --no-sync python scripts/run_tests.py --timings

# time each recipe body in the canonical graph and retain pytest durations
verify-timings:
    HMCPCTL_TEST_TIMINGS=1 just --time verify

# run the full pytest suite with native diagnostics
test-verbose:
    uv run --no-sync pytest -q --cov-report=term-missing

# MCP stdio handshake (reports exposed tool count)
smoke:
    uv run --no-sync python scripts/smoke_mcp.py

# MCP stdio handshake with the full exposed tool registry
smoke-verbose:
    uv run --no-sync python scripts/smoke_mcp.py --verbose

# construct a fresh wheel and source distribution
build:
    uv build --clear --wheel --sdist --out-dir dist .

# validate the retained distributions without rebuilding them
verify-artifacts:
    uv run --no-sync python tests/validate_release_artifacts.py dist .

# full standalone local verification
verify: static verify-runtime

# runtime checks; CI covers static gates through the real hooks first
# The root help goes through the installed console script, so the entry point is
# covered; the group helps are derived from the Typer app rather than listed here.
verify-runtime: test smoke build verify-artifacts
    uv run --no-sync hmcpctl --help >/dev/null
    uv run --no-sync python scripts/smoke_cli_groups.py
    @echo "verify: all groups load OK"
