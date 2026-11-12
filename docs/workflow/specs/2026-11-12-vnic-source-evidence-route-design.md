# vNIC source recovery evidence route (#1934)

Decision record: [ADR 0371](../../adr/0371-render-vnic-source-recovery-evidence.md).

## Problem

`scripts/live_test_evidence.py` refuses every `vnic-creation-policy` document,
because those documents carry `expected_commit` rather than `run.tested_commit`.
So the #1856/#1857 source-case outcomes exist publicly only as hand-written prose.

## Scope

In scope (piece A of #1934):

1. `scripts/live_test/arms/vnic_creation_policy.py` gains one public function,
   `validate_source_recovery(document: dict) -> None`. It raises `ValueError`
   unless all of these hold:
   - `group == GROUP`, `stage == "recover"`, and `version` is the int `2`;
   - `purpose == "update-mode-source"` and `evidence_class == "source-only"`;
   - `case` is in `_SOURCE_CASES` or equals `_FINITE`;
   - `expected_commit` is a str matching `[0-9a-f]{40}`, and `packet_sha256` is a
     str matching `[0-9a-f]{64}`;
   - `complete is True`, and `status` is `"CLEAN"` or `"MANUAL_RECOVERY"`;
   - `session == {"logged_on": True, "logged_off": True}`;
   - `intents == []` and there is no `effect_commands` key.

   Run behaviour is unchanged.
2. `scripts/live_test_evidence.py` gains `_render_vnic_source(document)`. `render()`
   calls it first whenever `document.get("group") == "vnic-creation-policy"`. It
   calls the validator and returns `None` on `ValueError`, `TypeError`, `KeyError`
   or `AttributeError`. On success it returns:

   ```text
   SOURCE ONLY — vNIC update-mode observation on `<commit>`

   | Case | Submission | Readback | Preservation | Independent recovery |
   | --- | --- | --- | --- | --- |
   | <case> | completed | verified | unchanged | CLEAN |

   One owned-fixture case. Finite-update operands, snapshots and HMC text stay private.
   No public updater, traffic or failover claim.
   ```

   For `MANUAL_RECOVERY`, the row is
   `| <case> | not established | not established | not established | HELD |`.
   The interpolated values are `case`, which is drawn from the fixed set, and the
   validated commit. Nothing else from the document is printed.
3. `docs/live-testing.md`: the #1856 sentence ("does not apply to them") and the
   #1857 sentence ("has no route for them") are replaced. The new text says that
   recovery records render through the script, and that raw journals, capture
   records and fixture records stay private.

No ownership transition is needed: the record's vocabulary stays with its writer,
and the evidence script keeps owning the public allowlist.

Out of scope, with owners: piece B, which covers stamping or routing the
`update-vnic-policy` CLI JSON (operator); fixture documents and raw snapshots,
excluded permanently; promoting the ADR 0332 ledger row (#1858/operator);
version-1 creation records, for which no criterion asks.

## Failure model

1. **Actors and deployments:** an operator on a workstation renders a private
   record and pastes the output into a PR, issue or ADR.
2. **Invariants and assets at stake:**
   - No LPAR, system or fixture name, slot, VLAN ID, MAC address, HMC host,
     snapshot or HMC message text reaches the output.
   - No record outside the accepted shape renders; in particular, fixture records
     never render.
   - A rendered commit is 40 lowercase hex.
3. **Accepted failure classes:**
   - A hand-edited record can claim `CLEAN`. This is accepted because every route
     in this script has the same unsigned-JSON trust model (ADR 0371).
   - A HELD row hides why recovery held. This is accepted because the reason lives
     in private text.
4. **Covered elsewhere:** the truth of `CLEAN` is held by the owner's
   `_source_history`. Publishing hygiene beyond this script is held by
   `docs/live-testing.md`'s private-evidence rules.

## Threat model

- **Boundary:** one existing boundary is widened. A private JSON record, written
  by the runner from HMC data, now crosses into public Markdown through a new route.
- **Actor:** the local operator, who is trusted to choose which file to render but
  not to scrub it by hand. HMC-derived strings inside the record are the untrusted
  content.
- **Control:** a closed allowlist. Only one fixed-set token and one regex-validated
  commit are interpolated. A validation failure prints nothing and exits 1 through
  the existing `main()` path.
- **Out of scope:** forged records (see the accepted classes above), and the
  updater journals (piece B).

## Success

1. A synthetic complete `CLEAN` recovery record renders the qualified row. A
   complete `MANUAL_RECOVERY` record renders the HELD row.
2. A record that seeds a sentinel into the LPAR/system names, slot, VLAN, MAC,
   baseline snapshot, `error` and `reason` renders without that sentinel.
3. Each of these refuses (`render` returns `None`, and `main` exits 1 with no
   stdout), and none falls through to the generic matrix: stage `preflight` or
   `capture`; `complete` false; an open session; non-empty `intents`; a
   bad commit; an unknown case; an unknown status; version 1; purpose
   `vnic-source-fixture`; and a record that adds a `run.tested_commit` block.
4. `docs/live-testing.md` no longer says the script has no route.

## Validation

- Owner validator: `focused-test`. The cases are parametrised in
  `tests/scripts/test_live_test_evidence.py`, which exercises the validator through
  `render`. Before the route exists, the CLEAN case fails because `render` returns
  `None`. Green command:
  `uv run --no-sync pytest tests/scripts/test_live_test_evidence.py -q`.
- Renderer and dispatch: `focused-test`. Success items 1–3 run in the same file.
  Leak sentinels assert absence. The fall-through guard uses a document that also
  carries a valid `run.tested_commit` block and expects `None`.
- Docs edit: `task-test-not-applicable`. The changed surface is prose in
  `docs/live-testing.md`, and no executable consumer reads those sentences.
