# Update a shared processor pool

## Problem and scope

Operators need to rename a pool and set its maximum or reserved processing units (#1976).
The pool ID and UUID from the existing system-scoped projection identify the same slot across
a rename. The client, CLI and MCP retain the existing distinction between an integer ID and
an exact name. #641 delegates partition reassignment to #1977.

## Operation

Resolve the selector against the entire feed, including unconfigured slots. Refuse the
default slot, an empty change, an ambiguous name, and a name used by another slot.
Validate the new name as printable nonblank HMC text up to 14 characters, allowing interior
spaces. Encode the name as a single native attribute value. Use bounded_processor_number
for finite nonnegative values, selecting whole units for maximum (an integral decimal
spelling is valid) and exact hundredths for reservations. IBM SG24-8535 specifies whole
maximum capacity in docs/refs/intro-to-powervm-redbook/03-planning-for-ibm-powervm.md:206. The IBM PowerVM
Virtualization Introduction and Configuration Redbook (SG24-7940) gives the name limit.

Use the current target maximum and pending reservation for omitted quantities. Establish
member entitlements and available system capacity before capacity changes. Read both system-scoped LogicalPartition and VirtualIOServer feeds
(client/client_lpars.py:102 and client/client_systems.py:329). Each shared partition's
CurrentSharedProcessorConfiguration supplies CurrentSharedProcessorPoolID and
CurrentProcessingUnits; CurrentHasDedicatedProcessors discriminates dedicated partitions
(operations/lpar/processors/current.py:208 and the rest-lpar-entry and rest-vios-create
fixtures). Sum each UUID once. A completed scan of both feeds establishes empty membership
when no current shared partition points to the slot. Cross-check reported AssignedPartitions
with the computed members when present. Read CurrentAvailableSystemProcessorUnits from
AssociatedSystemProcessorConfiguration (operations/inventory/capacity.py:76). Incomplete, ambiguous or malformed
capacity evidence stops a write. Require enough maximum for committed member entitlements
and the pending reservation together. Check a requested reservation against available
shared units. Preserve fields absent from the request in the submitted patch.

The native update candidate binds to docs/refs/hmc-commands-p10/commands/chhwres.md:160,
:220–221, :238 and :425–426. The REST reference describes PUT as creation and POST as
update (docs/refs/hmc-rest-api-p10/managed-system/153-shared-processor-pool.md:45–46).
Confirm the native candidate with the bounded live scenario before selecting it for the
public operation. Native submission performs one fixed command addressed by numeric pool
ID. Its result records the native route and that HTTP If-Match is inapplicable.

Capture the before projection. After submission, including ambiguous transport errors,
independently fetch the slot by ID under the resolved system and verify its UUID. Return
the observed name, maximum, pending and current reservations. Report success only when the
submitted fields match and the submit succeeded. Distinguish a mismatch, failed submit
after a possible effect, and missing readback. Never automatically replay a write.

## User surfaces and validation

Extend the existing systems processor-pools CLI with update and expose
hmc_update_shared_processor_pool through the classified managed-system write path.
Both take the same selector and optional fields and render the same result.
Exercise MCP through the in-process client and the CLI through its registered command.
Test pool zero by name and ID, decimal and name boundaries (including a refused fractional maximum), duplicate names, combined
capacity limits, missing or incomplete evidence, stale identity, pending/current
divergence, partial writes, and independent post-submit read failures.

## Live verification

One named, opt-in arm selects an unconfigured nonzero placeholder slot with zero capacities
and independently confirmed empty membership on the authorized managed system. It records
the exact original slot, capacity fields and intended change in a durable private results
checkpoint before submission. A failed checkpoint stops the write. It requests a unique
temporary name, a one-unit maximum and a small hundredth-unit reservation,
compares REST readback, and restores the original fields. Before restoration verify the
same slot identity and ownership of the changed values; ambiguous ownership requires
operator recovery. The recovery command independently reads the original slot and compares
its identity, name, maximum and both reservation values against the baseline. Preflight,
run and recovery are separate. Publish only commit-stamped, filtered evidence.

## Failure model

- Actors and deployments. An operator uses the installed CLI or a policy-authorized MCP
  client, with REST and SSH access to a supported HMC.
- Invariants. Protect the default pool and other slots. Capacity validation depends on
  complete observed membership and system capacity. Distinguish a pending request from
  current allocation. Keep raw lab identity and credential data private.
- Accepted failures. Native writes lack a conditional precondition. Concurrent independent
  writers can change state between reads; report observed results and do not promise atomic
  compare-and-swap. Firmware rejection or delayed readback can leave the outcome unverified.
- Covered elsewhere. #639 owns ResourceGroup pool URIs, #680 system processor configuration,
  #1977 partition assignment, #638 non-pool DLPAR and #642 shared memory pools.

## Threat model

The tool adds a managed-system write capability through the existing authorization and
audit layer. Treat caller names, numbers and HMC-returned names as untrusted data. Bound
number rendering, validate selectors and names, encode native attributes and shell tokens
separately, and submit only fixed command and option names. Use existing transport output
bounds. Test refusal before effects and preservation of structured argument boundaries.
Existing profile and credential management governs HMC authentication. The live runner
keeps identifying results private and uses the repository evidence exporter for publication.
