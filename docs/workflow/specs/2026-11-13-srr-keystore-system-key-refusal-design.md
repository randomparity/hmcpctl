# SRR plus keystore creation through the HMC system-key rule (#1829)

Decision: [ADR 0377](../../adr/0377-forward-srr-keystore-creation-to-hmc-key-rule.md),
amending ADR 0277.

## Problem

`validate_lpar_creation` refuses creation with
`simplified_remote_restart_capable=True` and `keystore_kbytes>0` before effects,
pending a positive user-defined system-key classifier that no source supports.
The HMC enforces the rule itself (`HSCL0DCE`), so the refusal blocks user-key
systems and its stated prerequisite cannot be met without guessing.

## Scope

Owner: `src/hmcpctl/operations/lpar/creation/core.py` (creation admission and
native-failure reconciliation). `src/hmcpctl/ssh/lpar.py` changes only so
`create_lpar_via_cli` keeps the refusal code. No key read, no new parameter, no
update-path change. No ownership transition is needed.

1. Remove the combination refusal from `validate_lpar_creation`. A predicate
   `_srr_keystore_requested(creation) -> bool` keeps its exact condition.
2. `create_lpar_via_cli`: both re-raised `HMCCLIError`s in its generic and
   hardware-memory/affinity branches carry `remote_refusal_code` from the
   provider error (a matched `HSCL[A-Z0-9]{4}` token or `None`). Messages are
   unchanged. The handler reads the code once before branching, so its branch
   predicates are restated without changing which route raises what.
3. `_submit_native_creation`, first statement of its `except HMCError` handler
   (before the policy branch rewraps every error as "outcome is unknown"): when
   `_srr_keystore_requested(creation)` and the error is an `HMCCLIError` whose
   `remote_refusal_code == "HSCL0DCE"`, await
   `_raise_system_key_refusal(hmc, features.system_uuid, creation)` after
   leaving the handler, so the provider error is neither cause nor context.
   Serial,
   start, memory and layout routes convert their errors earlier and are unchanged.
4. `_raise_system_key_refusal` runs one
   `hmc.find_partition_by_name(name, system_uuid=...)` and raises:
   - `None`: `HMCCLIError(..., remote_refusal_code="HSCL0DCE")` with message
     `HMC refused creation combining simplified remote restart with a nonzero
     keystore (HSCL0DCE): the managed system's trusted system key status is not
     User Key. A scoped lookup observed no partition; nothing was created.
     Select only one of these features, or set a user-defined system key first.`
   - a matching identity: `HMCError` `... outcome partial; partition exists (UUID
     <uuid>). Inspect scoped identity before retrying; no retry, ownership stamp
     or rollback performed.`
   - `HMCTLSFingerprintError`: re-raised with an added note; any other lookup
     exception: `HMCError` `... outcome unknown; scoped identity readback
     unavailable ...`; a found entry that is not an identifiable match:
     `HMCError` `... outcome unknown. ...`.
   The lookup/identity logic is shared with `_raise_layout_create_failure`
   through one helper rather than copied.
5. ADR 0277 Status gains an amendment line; `docs/capabilities/lpar-allocation.md`
   C04 paragraph states the new behaviour and the unevidenced positive path.

## Failure model

- Actors and deployments: a local operator or agent calling LPAR creation
  through the CLI, MCP tool or library against a P10/P11 HMC.
- Invariants and assets: no false "nothing was created"; no partition left
  unreported; key state never inferred; unknown stays unknown.
- Accepted failure classes:
  - the user-key accepted path is unevidenced live (ADR 0212 records it so);
    the unchanged readback verifies whatever the HMC accepted.
  - `HSCL0DCE` on memory, layout, serial or start routes: memory and layout keep
    their reconcilers; serial and start keep their "outcome unknown" errors.
    Honest, bounded, and not required by a criterion.
  - a partition created between the refusal and the lookup by another actor is
    reported as partial, never as refused.
  - an HMC release that accepts the combination on a non-user-key system: the
    HMC owns that admission; hmcpctl verifies what was created (ADR 0377).
    Only V10R3 M1060 behaviour is observed.
- Covered elsewhere: system-key management (excluded by #1829); update-path
  rules (ADR 0277); remote-output redaction (ADR 0369, `run_hmc_command`).

## Threat model

- Boundaries: remote `mksyscfg` output parsed for a refusal code (existing, now
  retained one frame further); the lookup response (existing).
- Actors: the HMC is trusted for its own refusal; the operator is authenticated.
- Controls: the code comes from the existing anchored `HSCL[A-Z0-9]{4}` match on a
  completed nonzero process; only the exact literal `HSCL0DCE` plus the requested
  combination selects the refusal; no provider text enters the message.
- Out of scope: a hostile HMC forging codes (it already controls every outcome).

## Success

1. The combination is no longer refused before effects; the record carries both
   fields once.
2. `HSCL0DCE` + no partition found: contained refusal, no stamp, apply or retry.
3. `HSCL0DCE` + partition found: partial; lookup failure: unknown; TLS identity
   failure re-raised.
4. Other codes, or `HSCL0DCE` without the combination: prior diagnostics unchanged.
5. Accepted combination: SRR and keystore readback steps run as for each alone.

## Validation

- Criteria 1, 5 — focused-test: `test_creation_submits_srr_with_keystore`.
- Criterion 2 — focused-test: `test_srr_keystore_hscl0dce_is_contained_refusal`.
- Criterion 3 — focused-test: `test_srr_keystore_hscl0dce_reconciles_outcome`
  (partial, unidentifiable, lookup failure, TLS).
- Criterion 4 — focused-test:
  `test_hscl0dce_outside_combination_keeps_policy_unknown` (keystore only;
  other code with the combination).
- Code retention — focused-test in `tests/lpar/test_hardware_memory_creation.py`:
  `test_cli_creation_error_keeps_refusal_code`.
- Layout reconciler outcome words unchanged — existing
  `tests/lpar/test_memory_layout.py`.
- ADR/capability prose — task-test-not-applicable: no executable consumer reads
  this prose beyond `just adr-numbering` (name and H1 only).
