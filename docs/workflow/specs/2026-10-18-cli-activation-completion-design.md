# Shared CLI activation completion
Full-spec S denominator100, bounded one CLI adapter and existing tests. Runtime/output
contract hazard requires full lane. Expected implementation100–220 lines including tests.

## Charter
Interaction: interactive. Scope identity: current finding activation_adapter_bypasses_completion_policy.
Outcome: CLI PowerOn uses the same activation completion owner as MCP.
Completion: C1–C4. Provenance: user Approved to the explicit CLI shared-operation and
normalized LparPowerOnOutcome JSON proposal, then stop after current four findings.
Exclusions/owners: PowerOff keeps its existing operation/output; operation activation,
authorization, boot/profile/force/wait policy and persistence remain existing owners.
No new options, dependencies, live run or compatibility JSON alias.
Surface: cli_commands/lpar/lifecycle.py, affected CLI tests and maintained CLI docs.
Ambiguities: human submitted-target text may use supplied selector since normalized
outcome intentionally has no lpar_uuid; do not expand the domain result model.

## Success
C1: lpars_power_on directly calls existing power_on_lpar with exactly the same
arguments/options currently sent to submit_power_on_lpar. The shared function owns
waited require_activated and already-activated normalization. No second validation,
job submission or local interpretation of ephemeral raw markers.
C2: render existing LparPowerOnOutcome as a JSON OBJECT using stdlib asdict, as sibling
CLI modules already do; print_json(dataclass) alone currently stringifies it.
Keep message/no-submission and warnings visible, with terminal_field at human-text
boundaries. Submitted outcome retains raw job inside job and all five existing fields.
No invented activation exit policy: use existing operation exceptions/status semantics.
C3: PowerOff operation/options/output remain unchanged. Remove the now-unused
PowerOn marker branch from its old renderer; separate small renderers may express
the two existing result shapes without union dispatch or new abstraction.
C4: CLI --wait with COMPLETED_OK but partition error/not activated exits nonzero
via existing HMCError; completed job with activated readback succeeds. Already-active
states submit no job and expose true/null job, unwaited/forced and all boot/profile/
ownership options retain their exact existing behavior.

## Failure model
Actors: CLI users, inactive/activated partitions, misleading successful HMC job,
unreadable readback and ownership denial. Assets: truthful completion, no duplicate
PowerOn, existing authorization and option forwarding, normalized machine output.
Accepted class: approved pre-release CLI JSON shape replacement; external raw-job
readers migrate, no alias. Missing/unreadable state semantics remain shared operation,
not a new CLI-only stricter policy. Durable operation/journal APIs unchanged.

## Validation
C1/C4 Mode focused-test: actual Typer CLI with existing fake/respx HMC boundary,
waited completed job+error/notactivated state fails old adapter, trueactivated passes;
already-active/no-force and force/no-wait options remain covered. Mutation back to
submit_power_on_lpar must fail completion test then restore.
C2 Mode focused-test: parse actual CLI JSON as object, exact five fields/new marker,
raw job nested/submitted and null job/no-submission, warnings/messages. Old renderer
fails normalized shape; avoid mocked operation logic as sole proof.
C3 Mode focused-test: existing PowerOff CLI and authorization regressions unchanged.
Run relevant test_cli_commands/test_power/test_power_ownership_guard and CLI security
cases --no-cov; lint/format/types/all14 pinned hooks and normal commit hooks, no bypass.
Root final four-task integration/review; no live hardware or further scoring cycle.
