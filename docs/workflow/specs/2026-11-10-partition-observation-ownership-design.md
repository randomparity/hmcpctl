# Shared resource and partition observations

## Problem and ownership
The dictionary-only resource_container in inventory/composite imports fleet/capacity collectors
into specialist reads. Processor mutation/move imports memory.current for shared contained
partition context and inherited numeric snapshots. Keep policies in their domain owners and
move existing read infrastructure to the nearest neutral layers.

- operations/resource_reading.py owns the exact resource_container(resource:dict[str,Any],
  name:str)->dict[str,Any] body: value=resource.get(name); return value if isinstance(value,dict)
  else{}. No collector, client or operation import is needed.
- operations/lpar/observation.py owns MEMORY_FIELDS, MemoryQuantities, PartitionSnapshot,
  integer/boolean field readers, processor assignment vector, partition_snapshot(entry,uuid)
  and contained_partition(hmc,system_uuid,selector). These are relocated definitions currently
  named _MEMORY_FIELDS/_MemoryQuantities/_MemorySnapshot/_integer/_boolean/_cpu_vector/_snapshot/
  _contained_partition in memory/current.py. Use clear names at their real owner, no old aliases.
- memory/current keeps _MemoryTargets, target/admission/capacity/pool/postcondition/result/change
  logic and memory-specific coupled-reset _require_cpu_vector. Memory move/statistics import
  shared observations directly while retaining their current memory-policy calls.
- processors extends PartitionSnapshot and uses shared field mappings/readers directly;
  processor_move uses contained_partition. Remove both memory.current module dependencies.
  Preserve processors' _bounded_memory_snapshot numeric prevalidation before inherited snapshot.

## Caller migration
resource_container direct callers: inventory/composite.py, inventory/logical.py,
lpar/{inspect,accelerator,processors}.py, lpar/memory/{current,move,statistics}.py,
and scripts/live_test/arms/accelerator_qos.py. Split mixed imports, preserving real collector
imports that other functions need. No wrapper or old module re-export only for compatibility.
Snapshot/containment callers: memory/{current,move,statistics}.py, processors.py,
processor_move.py and scripts/live_test/arms/processor_move.py's memory field map.
Update tests/lpar/test_processor_quantities.py's inherited snapshot patch to the actual new
owner and every other direct patch/import seam discovered by the bounded owner search.

## Contracts and failure model
Actors are current native memory/processor operations and offline/live arm operators. Existing
API/result/journal shapes, numeric spelling bounds, minimum/current/maximum consistency,
boolean/entitlement semantics, identity containment/ambiguity, LPAR guard context, authorization,
TLS fatality and exact remote call/recheck order are invariant. Keep historical Memory error
wording unchanged even in the generic owner; do not add a parser policy or normalize outputs.
Existing already captured source packets become stale when owner paths/commit change, as their
current source-binding model requires. Add real relocated executable owners to applicable
selected source witnesses; preserve field/version/limit/closed-set checks and actual newcaptures.
No new hostile-actor transaction, remote fallback, mutation or hardware verification is promised.

## Verification
focused-test: extend existing isolated-import tests to import processors/processor_move while a
MetaPathFinder first forbids inventory.composite, then forbids memory.current after the first
green milestone. Each baseline fails on its actual old dependency and the completed transition
passes; keep each committed milestone green and preserve other legitimate imports.
focused-test: run memory quantities/move/statistics, processor quantities/move, accelerator,
inspection/inventory, app/tool and live offline caller suites; numeric-before-inherited snapshot
cases retain their assertion. Existing behavior tests are the preservation proof.
Audit moved AST bodies after only declared symbol/import/type-owner normalization, retaining
all strings/numbers/conditions. Import/patch/source closure maps are explicit direct migrations.
Compare actual207MCPschemas/meta to baseline, real scenario associations and selected source
binding tests. Run lint/types/format/static and normal14hooks before each milestone commit.
No artificial prose or function-placement snapshots; pure relocated definitions need no new
behavioral fault. The meaningful import boundary uses the genuine red/green subprocess case.
