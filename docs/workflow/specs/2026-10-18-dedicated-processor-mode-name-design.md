# Name the dedicated processor mode as a boolean

Charter: interactive; owner root; user approved final three corrections including
JSON/MCP field rename. S100/full-spec (observed result-model/wire change).

Rename dedicated_procs to has_dedicated_processors in processor_figures, shared
projection type, logical inventory and inspection models and all consumers.
Preserve true/false/null values, numeric processor figures and authorization.
Update current docs and generated tool docs with their recipe. No duplicate old
field or compatibility aliases; do not rewrite unrelated historical decisions.
No durable store schema, result identity or workflow data migration involved.

Failure model: missing/shared/dedicated processor configurations retain current
three-valued mode semantics; unknown must not become false. Serialized inventory/
inspection fields must use new name consistently, with old name absent.
Verification: update meaningful three-mode/result serialization tests, observe
new name failing before rename, retain actual MCP inventory/inspect behavior
tests; regenerate docs, run focused tests/lint/types/format/hooks. One commit.
