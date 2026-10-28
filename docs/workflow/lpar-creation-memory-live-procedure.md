# Inactive creation memory — ST65

The named `creation-memory` group runs exactly one selected case. It is excluded
from default and `all`. Registration and offline tests supply no native evidence.
The [C07 design](specs/2026-10-26-lpar-shared-memory-creation-design.md) and
[ADR 0299](../../adr/0299-lpar-shared-memory-creation.md) distinguish saved profiles,
current configured values and runtime. This arm never activates or applies a profile.

Root must separately approve the exact clean deployed candidate, selected system,
case and exclusive writer window, conditional fresh-UUID cleanup permission and
measured finite request, absolute deadline and session/reaping reserves. Verify
that the imported package and executing build match that candidate. An operator
must retain the private independent before/after baseline and review actual
capabilities, pool/provider topology and policy. Fixture dimensions do not promise
admission or free paging capacity. No live call is authorized by this procedure.

Set these optional keys in the local `.env`; ambient exports cannot arm the case:

| Key | Meaning |
|---|---|
| `LIVE_TEST_CREATION_MEMORY_CASE` | Empty disables; select exactly one case below |
| `LIVE_TEST_CREATION_MEMORY_PRIMARY_VIOS_NAME` | Exact primary name for LPAR cases |
| `LIVE_TEST_CREATION_MEMORY_SECONDARY_VIOS_NAME` | Exact distinct secondary name for redundant case; otherwise empty |
| `LIVE_TEST_CREATION_MEMORY_FRESH_DELETE_AUTHORIZED` | Literal `true` only with a current separate conditional fresh UUID grant; defaults `false` |

| Case | Fixed request and evidence |
|---|---|
| `shared-lpar` | Shared, automatic entitlement, weight zero, explicit primary, explicit no secondary; saved default profile |
| `redundant-lpar` | Same scalars, explicit distinct primary and secondary; saved default profile |
| `ame-zero-vios` | Dedicated memory, AME zero, no provider selectors; saved disabled state only |
| `ame-positive-vios` | Dedicated memory, AME 1.10, no provider selectors; saved enabled factor only |
| `controller-shared-lpar` | Shared single-provider request; independently observed controller, current configured scalars, unavailable designated-pair proof; incomplete, never promoted |

Classic cases require independently observed classic management. Both VIOS cases
leave paging selectors empty. Requests use 1024/2048/4096 MiB memory and
0.1/0.5/1.0 shared processing units with 1/1/2 virtual processors. AME is a JSON
number. LPAR sends a fresh caller token and explicit profile application false;
VIOS adds neither ownership stamp nor profile application.

Create an operator-owned mode-0700 private directory outside the checkout. Set
`RESULT` to a fresh absolute file in it and use `umask 077`. Both the results and
observations paths must be unused. Run each command separately and inspect its
exit status. Failed preflight stops execution; any execution attempt still
requires recovery, including success, interruption, cancellation or timeout:

```sh
uv run --no-sync python scripts/live_test_preflight.py --group creation-memory
uv run --no-sync python scripts/live_creation_memory.py --results-file "$RESULT"
uv run --no-sync python scripts/live_test_recovery.py --results "$RESULT"
uv run --no-sync python scripts/live_test_evidence.py "$RESULT"
```

Maximum effects per invocation are one create, at most one LPAR stamp (zero VIOS
stamps) inside the existing creation owner, and one conditional exact UUID delete.
No existing provider, pool, storage, role or hardware state is changed. Request
counts include additional reads and are not an estimated transport budget. Root
must measure and approve that budget before granting the window.

The arm checkpoints closed intent before mutations, binding candidate, invocation,
HMC identity, selected system, fixed request and complete independent baseline.
It selects one `default_profile` from the contained profile inventory and reads
its native XML, then independently parses literal `ProfileMemory` leaves and
re-resolves designated provider links. It does not trust its product verification
payload. Missing, ambiguous, foreign or oversized evidence is unavailable. AME
zero checks disabled state independently of any stale unused factor. Controller
current paging is never substituted for saved primary/secondary designation.

Cleanup requires a successful returned UUID and complete independent classic
configured equality, baseline exclusion, fresh containment/name/kind/inactive
identity, current grant and destructive policy, plus the exact LPAR caller token.
The arm deletes only that UUID, confirms absence and compares partition, free
CPU/memory and VIOS baselines before recording verified evidence. Mismatch,
unavailable evidence, controller incomplete observations, uncertain acknowledgement
or failed checkpoint stops effects and preserves residue. Diagnostic identities
never authorize adoption, stamping, deletion, another fixture or retry.

Recovery is mandatory and read-only. A complete single unarmed SKIP can report
`SKIPPED` without connecting, and proves no baseline/configuration/runtime fact.
Missing, partial, malformed or contradictory journals cannot attest clean state.
Other phases require candidate/HMC binding, selected-system identity, exact
baseline and terminal fixture absence. Saved grants cannot authorize later cleanup.
Root must independently reconcile interrupted intent and obtain separate authority
for any manual remediation; source rollback never removes HMC state.

Generate any public matrix only with `live_test_evidence.py`, retaining the exact
tested commit and scanning the complete filtered output for private identifiers.
Keep raw results/observations private. No controller designation, reserved paging
capacity, activation, guest compression, VIOS service/failover or runtime proof
follows from this arm. IBM i, other releases, mixed families and active guest
contexts retain their separate evidence gaps until separately exercised.
