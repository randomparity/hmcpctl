# Separate LPAR inventory and activation ownership
Full-spec L denominator1000; exact user approval item1, private proposal ownership
round. Root owns cleanup. Interactive; no stable facade, auth, persisted identity,
job/XML, external output or dependency changes. Replace internal paths, no aliases.

## Transition inventory
Current core.py owns reads, compatibility vocabulary, activation and creation.
Move list_lpars/get_lpar/get_lpar_state to new lpar/inventory.py, unchanged signatures
and bodies, with only required imports. Move ProcessorCompatibilityMode and matching
PROCESSOR_COMPATIBILITY_MODES to existing configuration.py; eliminate its dependency
on core for this vocabulary. No duplicated vocabulary or compatibility reexports.
Move ACTIVATED_STATES, _FAILED_ACTIVATION_STATES, LparPowerResult,
LparPowerOnOutcome, power_on_outcome, activation_allows_assessment, require_activated,
power_on_lpar, _require_contained_partition_profile, _unapplied_activation_clause,
_resolve_power_target, submit_power_on_lpar, power_off_lpar and submit_power_off
into new lpar/activation.py. Keep raw submission and post-activation/affinity wrapper
semantics separate. Existing durable lpar/power.py imports policy/submission from
activation; it remains the logical-engine adapter and is not merged into activation.
Core retains creation/stamping models and helpers, delete/rename (new target-first
required-system signatures from preceding task). Delete moved definitions and all
obsolete imports in core; creation cannot import unrelated activation policy.

Update all direct consumers found by AST/name anchor searches, including CLI/MCP,
provision/decommission/inspection/workflow callers, scripts and tests. Review monkey-
patch targets against the new actual owner instead of adding reexports to satisfy
mocks. Update maintained current API inventories/doc snippets; historical ADR bodies
remain historical. The six-name hmcpctl.api facade remains exactly unchanged.

## Failure model and validation
No behavior changes intended. Keep function/class ASTs equivalent except import
owner references and intentional prior signature changes. Preserve annotations,
state sets, validation ordering, profile containment, scoped ownership audit,
already-activated warnings, waits/readback and affinity failure classification.
No import cycle: activation and inventory depend on lower layers, not core; durable
power depends on activation. Configuration owns vocabulary without depending oncore.
Mode focused-test: meaningful boundary tests ensure configuration/inventory/activation
can import with core import blocked, and actual consumers call the intended owner;
controlled core dependency restoration must fail. Check facade exact inventory.
Mode focused-test: existing inventory/creation/power/profile/affinity/provision/
authorization/CLI/MCP tests exercise behavior unchanged; compare moved ASTs (no
mirrored snapshot-only tests). CLI16groups and actual MCP handshake load. Run
focused suites --no-cov, just lint/format-check/typecheck, regenerate tool-docs if
registry rendering changes. Actual14hooks before commit; root fullverify and pinned
hooks at assembled candidate. No live HMC or external writes.
