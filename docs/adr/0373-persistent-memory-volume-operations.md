# ADR 0373: Persistent-memory volume operations over REST, guarded per partition

## Status

Accepted (2026-11-13), under the operator-confirmed campaign dispatch for #643.

## Context

#643 binds the reference row `rest:managed-system/virtual-persistent-memory-volume`
(POWER10 and POWER11 REST references): list, list per partition (`?target=`),
read, create-and-assign to an inactive AIX/Linux partition, rename, and delete.
The HMC CLI offers the same create/delete through `chhwres -r pmem`. A volume
lives in the managed system's collection but is assigned to one partition, and
deleting it destroys its data. ADR 0092 requires every operation that changes an
existing partition's device attachments to check its ownership token.

## Decision

1. **REST is the only transport.** The CLI `-r pmem` path is not wrapped.
2. **Mutations name the partition.** Create, rename and delete each take the
   partition selector, resolve it inside the selected system, and run the ADR 0092
   guard on it. Rename and delete also require the volume's
   `AssociatedPartitionId` to equal that partition's `PartitionID`. A volume with
   no associated partition is refused.
3. **Classification under ADR 0092 §3.** `create_persistent_memory_volume` and
   `rename_persistent_memory_volume` are Reconfiguring; `delete_persistent_memory_volume`
   is Destructive. All three are guarded unconditionally, with the standard audited
   `ownership_override`.
4. **Delete needs separate consent.** `acknowledge_data_loss=True` is required in
   addition to the MCP `destructive` effect, as for vTPM disable (ADR 0276), and the
   partition must read `not activated`.
5. **Admission reads advertised values.** Create requires `PersistentMemoryCapable`,
   `dram` among `SupportedPersistentMemoryDeviceTypes`, a size that is a positive
   multiple of `DramPersistentMemoryVolumeBlockSize`, and spare
   `MaximumDramPersistentMemoryVolumes` on the partition. No value is assumed from a
   processor generation.
6. **One submission, honest outcome.** No retry, no rollback; the result reports
   `verified`, `mismatch`, `readback-unavailable` or `write-outcome-unknown` from an
   independent readback.

## Consequences

- A volume left unassigned (for example by a profile restore that retains volumes)
  cannot be renamed or deleted through this package; the operator uses the HMC.
- Rename and delete cost one SSH ownership read, as every guarded operation does.
- The MCP access policy binds all three mutations on the `lpar` target, so a
  targets table can grant them per partition.
- Rename is not refused on an active partition; the reference states no such rule.

## Considered & rejected

- **Wrap `chhwres -r pmem` as well, or instead.** judgment: epic #620's non-goals
  exclude repeating equivalent semantics in a second transport, and the REST row is
  the bound row.
- **Address rename/delete by volume alone, authorizing the associated partition
  read from the volume.** judgment: the policy's target table could not see the
  partition, so a system grant would reach every partition's volumes; naming the
  partition keeps the target explicit.
- **Classify rename as Destructive.** judgment: a volume name is not the identity
  other consumers key ownership on, unlike a partition name (ADR 0092 §3.1).
- **Rely on the HMC for every precondition.** judgment: a client-side refusal names
  the failed precondition and sends nothing, while an HMC refusal arrives as an
  error body the caller must interpret after a write was attempted. The HMC still
  enforces every rule, so the checks only add clarity, never authority.
