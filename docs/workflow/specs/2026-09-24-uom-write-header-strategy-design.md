# One header strategy for UOM writes (#935)

Decision record: [ADR 0178](../../adr/0178-uom-writes-send-untyped-accept.md).

## Problem

`_write_uom` (`src/hmcpctl/client/core.py`) copies a typed `Accept` into `Content-Type` for
every `_put`/`_post`, and `_delete` sends a generic uom `Accept`. A V10R3 HMC answers both with
HTTP 406 before mutating anything. The 2026-09-23 probe in the #935 comment shows V10R3 wants an
untyped `Accept` (`*/*`) and a typed `Content-Type`; with them the vSCSI adapter PUT returned 200
and its DELETE 204. The VIOS mapping and VolumeGroup writes already send that shape by hand
(ADR 0169, ADR 0171); the shared helpers do not.

LPAR create depends on the 406: `create_and_stamp_lpar` (`src/hmcpctl/operations/lpar/creation/core.py`)
falls back to `mksyscfg` only on a 406. With the header fixed, V10R3 answers 400 `REST0001`: on
`PartitionMemoryConfiguration` in the 2026-09-23 probe (fixed since by #961), and on
`PartitionProcessorConfiguration` in a 2026-09-24 check with this design's headers, which created
nothing. That 400 escapes as an unhandled error.

## Scope

1. `_write_uom` sends `Accept: */*` and `Content-Type: <uom media>; type=<T>` (generic when no
   type is given), keeps the `X-HMC-Schema-Version` rule, and sends once. The
   `fallback_to_generic_uom_on_406` parameter is deleted from `_put`, `_post`, `_write_uom`, the
   `client_contracts` protocol, and its one caller, `create_volume_group`.
2. `_delete` sends `Accept: */*`, keeping the `X-HMC-Schema-Version` rule.
3. `create_and_stamp_lpar` falls back to `mksyscfg` on a 406 as today, and also on a 400 whose
   body contains `REST0001`. Any other status still raises. The processor-wrapper builder is left
   as #961 left it, so V10R3 keeps the `mksyscfg` path and its profile apply (#939).
4. GETs are unchanged. Hand-built writes (mapping, VolumeGroup, partition RMW, web API) are
   unchanged.
5. ADR 0178 records the strategy; CHANGELOG gets one `Fixed` line. Prose that names the 406 as
   the fallback trigger or blames a too-generic media type is corrected where it describes these
   paths (`docs/compatibility.md`, `docs/cli.md`, the create tool parameter, the two 406
   translations).

No ownership transition: the shared transport keeps header negotiation and the operation keeps
the create fallback decision.

## Failure model

1. Actors and deployments:
   - an operator or MCP client driving `hmcpctl` against a V10R3 HMC (the lab level) or an
     older HMC level; CI runs the unit tests offline.
2. Invariants and assets at stake:
   - a rejected write must not mutate; a create must not both succeed through REST and run
     `mksyscfg` (a 406/400 creates nothing, so the fallback cannot duplicate a partition);
   - GET negotiation, which works today, must not change.
3. Accepted failure classes:
   - an older HMC level that required a typed `Accept` on writes. Accepted: no captured
     evidence shows one; `*/*` admits every media type by definition, and the live probe is
     the only negotiating HMC in evidence.
   - a 400 `REST0001` caused by a caller-supplied value rather than the builder now runs
     `mksyscfg`, whose own validation reports the value. The cost is a different error text.
   - an HMC level that accepts the REST create takes the REST path, which applies no profile.
     Not reachable on V10R3: the 2026-09-24 check got 400 `REST0001` with the current builder.
     Unchanged by this design on other levels; raised as a follow-up candidate.
4. Covered elsewhere:
   - builder `kb`/attribute conformance: #961 (closed);
   - mapping and VolumeGroup RMW writes: ADR 0169, ADR 0171;
   - the provision/attach-disk vSCSI step: #1030.

## Success

1. A `_put` and a `_post` with a resource type send exactly `Accept: */*` and the typed
   `Content-Type`, one request, and a 406 is raised without a retry.
2. A `_delete` sends `Accept: */*`.
3. `create_child` sends the same headers and no `X-HMC-Schema-Version`.
4. `create_and_stamp_lpar` runs `mksyscfg` after a 400 carrying `REST0001`, and re-raises a 400
   without it.
5. `rg fallback_to_generic_uom_on_406 src tests` returns nothing.
6. Live, on the authorized lab partition: an adapter PUT and its DELETE succeed with the new
   headers, and an LPAR create check leaves no new partition behind, confirmed by read-back.

## Validation

- Success 1-3: `Mode: focused-test`, `tests/unit/test_transport_contracts.py` (replaces the
  mirror and retry tests) and `tests/unit/test_client.py` (`create_child`).
- Success 4: `Mode: focused-test`, `tests/lpar/test_lpar_http406.py`, a 400-`REST0001` case and a
  plain-400 case beside the 406 case.
- Success 5: `Mode: focused-test`, the `rg` command above exits 1.
- Success 6: `Mode: task-test-not-applicable`: needs the lab HMC. Direct `hmcpctl` commands under
  the campaign live lock, with HMC CLI baselines before and after; no runner arm, because every
  arm mutates beyond the operator's authorization for this issue.
