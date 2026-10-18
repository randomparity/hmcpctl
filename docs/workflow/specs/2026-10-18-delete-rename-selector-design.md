# Target-first deletion and rename
Full-spec S denominator100, user approval item1 exact two API signatures; root owner.
Interactive; no change to required explicit system scope, authorization, deletion
state refusal, XML validation, error translation, return values or external output.

## Interface and migration
In existing lpar/core.py change delete_lpar(hmc,lpar_name_or_uuid,*,
system_name_or_uuid:str,ownership_override=False)->str, and rename_lpar(hmc,
lpar_name_or_uuid,new_name,*,system_name_or_uuid:str,ownership_override=False)
->tuple[str,dict[str,Any]|None]. Retain existing annotations on hmc/return/options.
System is a required keyword with no default, unlike optional-scope power APIs.
Bodies unchanged. Migrate all direct repository callers and fixtures; no aliases
or shim, no facade exports. Keep historical decisions; update maintained API
inventories if they assert current signatures or snippets.

## Failure model and validation
Mode focused-test: extend existing selector signature tests for required keyword
system, target-first order and missing-scope rejection before client access; old
signature must fail. Real operation tests must preserve system and target values
through authorization and deletion/rename using names/UUIDs; retain non-off refusal,
XML invalid-name refusal, ownership override and translated HMC failures.
Run uv run --no-sync pytest tests/lpar tests/app/test_bare_cec_recipe.py tests/unit/test_destructive_scope.py --no-cov -q
plus exact CLI/MCP consumers identified by rg, just lint/format-check/typecheck.
No new tests for prose wording. Re-read diff and commit with actual14hooks. Root
owns final assembled verify/pinnedhooks. No dependency/persistence/live/HMC writes.
