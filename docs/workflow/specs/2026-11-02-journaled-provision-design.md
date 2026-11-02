# Journaled `hmc_provision_lpar`

**Issue:** #1839 (piece 1 of #1225, epic #1215) · **Decision:**
[ADR 0313](../../adr/0313-provision-is-a-journaled-operation.md) · **Contract:**
[H1 spec](2026-10-01-logical-lpar-workflows-design.md) · **Branch:**
`feat/journaled-provision-1839` · **Guardrails:** `just verify`;
`uv run --no-sync prek run --all-files`

## Problem

`provision_lpar` writes directly to the HMC. A repeated call duplicates a partition, an
interrupted call cannot be reconciled, and a failed ownership stamp is a warning.

## Scope

In: the MCP tool and CLI mirror run provision through the logical engine as ADR 0313 decides.
No profile write, `install`, `boot`, `adapters.mac` or `exclusive_writer_window` (#1840,
#1842, #1849); the dedicated-slot refusal stays (#1841).

**Components.**

- `operations/lpar/provision.py` keeps the request types, preconditions and leg helpers.
  `ProvisionStorage` gains `capacity_mib`; `ProvisionResult` gains `operation`. The direct
  `provision_lpar` workflow is replaced by `provision_body(system, request, client_factory)`
  and `provision_classifiers(request, client_factory)` in a new
  `operations/lpar/provision_operation.py`; no caller keeps the old path.
- `server_tools/lpar/provision.py` becomes a factory handler, as `hmc_power_lpar`'s is:
  `provision_handler(tool_security, permits, authorize)` and `register_provision_tool`, listed
  in `catalog.py`, `server.py` and `scripts/check_capability_inventory.py`. Security: `mutate`,
  target kind `console`, selectors `system_name_or_uuid` and nested `storage.vios_uuid`, both
  optional, not exhaustive.
- The handler: validate inputs; on a continuation read the recorded arguments; authorize the
  delegated tools (ADR 0313 Decision 8); for a new request with `expected_plan_digest`, run the
  plan check; `dry_run` returns `ProvisionResult(dry_run=True, operation=None)`; otherwise
  `engine.submit` with the classifiers and builds the result from the record.
- Arguments are recorded as JSON through a pydantic `TypeAdapter` and rebuilt the same way on
  a continuation.
- CLI `hmcpctl lpars provision` takes a required `--request-id` and `--capacity-mib`, submits
  with a client factory built from its own connection options, waits until the worker ends
  (`engine.join` gains `timeout=None`), renders the result plus operation id, state and
  outcome, and exits 1 unless the outcome is `completed`.
- Live scripts pass a run-unique `request_id` and `wait_seconds=600`; their arms are
  otherwise unchanged.

**Body.** Phase `validating`: when `create` is not applied, run the existing preconditions,
plus "no disk of that name in the volume group" with `capacity_mib`. Once `create` is
applied the body holds the guard on its system and partition UUID. Phase `creating`: the effects in ADR 0313's
order, each skipped when its request field is absent; ledger writes after `create` and `disk`
are applied; the change-location read after `network`. Messages name UUIDs and exception
types, never request arguments (engine module note).

## Failure model

1. **Actors and deployments:** an MCP agent on a policy-bounded server with one store; an
   operator running the CLI on the same host; other HMC writers.
2. **Invariants and assets:** no duplicate partition or disk per `request_id`; no ledgered
   storage the run did not create; a stamped partition or a `failed` run; withheld specialist
   authority stays withheld; persisted effect and ledger kinds.
3. **Accepted:** a concurrent writer creating the same name between a refused create and its
   lookup (not a fence, ADR 0190); an interrupted affinity-policy or assignment write needs a
   human (no classifier, H1 *Resume*); a resumed create skips a `mksyscfg` apply (ADR 0313);
   the plan check races provisioning (the body re-runs the preconditions); a CLI killed
   mid-run leaves an interrupted operation for `resume` or `operations abandon`.
4. **Covered elsewhere:** profile persistence #1840; VIOS writer window #1840/#1842; owned
   cleanup #1847/#1848; installer power-on #1849.

## Threat model

- **Boundaries widened:** the provision tool now admits continuation calls that name no
  target, and its writes are authorized as specialists.
- **Actor:** an MCP client whose grant covers provision but not every specialist, or not the
  targets a recorded operation names.
- **Controls:** delegated permit plus per-target authorization against the effective (recorded
  plus given) selectors before `submit`; the store's agent-id and connection binding; the plan
  digest check uses the plan's own delegated reads.
- **Out of scope:** fencing other HMC writers; a local user who can write the state directory
  (ADR 0190's private-mode check).

## Success

1. Missing `request_id` is a schema error on MCP and a usage error on the CLI.
2. A repeated request returns the same operation and writes once.
3. Each ADR 0313 effect is journaled with its key and kind; an open one resumes through its
   classifier; `network` identity carries the lower-case colon MAC.
4. A stamp that does not land ends `failed`.
5. A mismatched `expected_plan_digest` refuses with no record.
6. `dry_run` records nothing and still rejects a malformed `request_id`.
7. With `capacity_mib`, the disk is created, mapped and ledgered as `virtual_disk`.
8. A withheld delegated tool refuses before anything is recorded.

## Validation

Focused tests, rewritten from `tests/lpar/test_provision_tool.py` onto the handler against a
temporary store, cover Success 1–8 and the classifiers. `tests/app` schema, security, gateway
and CLI tests follow the new signature. `just tool-docs`, `just capability-metadata`, the
maturity record and `CHANGELOG.md` are regenerated or edited; `just verify` is the gate.
