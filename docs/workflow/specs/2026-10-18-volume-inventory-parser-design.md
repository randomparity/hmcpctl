# Preserve unreadable volume inventory across live arms

Charter: interactive; owner root; user approved all three final corrections.
Outcome one existing header-validating parser across power arm and recovery.
S100/full-spec (live safety/admission semantics). No hardware execution.

Delete lpar_power._volume_names and call storage_lifecycle.volume_names at its
producer and recovery witness. Parser remains in its current owning module.
None represents unreadable/nonlisting/empty response; only an LV NAME table
with no rows represents an empty volume set. Inspect consumers so None cannot
become absence evidence or authorize cleanup. Existing valid observations,
identity constraints, resource recovery and cleanup ordering remain unchanged.

Failure model: empty or malformed PASS command output must refuse mutation and
retain uncertain resources. A valid empty table permits the existing normal
flow; failed command status remains unreadable. No speculative parser framework.
Verification: meaningful malformed/empty/header-only/real-volume tests against
shared parser and actual power/recovery user paths, red on permissive old parser;
retain ownership/baseline/recovery assertions and relevant offline live tests.
Run lint/types/format/vocabulary/scenario guards, one commit including this spec.
