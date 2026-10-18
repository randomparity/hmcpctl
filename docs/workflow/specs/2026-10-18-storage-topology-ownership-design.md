# Shared live storage topology helpers

## Problem
Disk and power scenarios import generic storage topology helpers from the virtual
media lifecycle. The operator approved moving these unchanged helpers into the
existing live_test/storage.py owner and updating consumers.

## Scope
Move _mapping_row, storage_rows, scsi_adapter_listing and adapter_rows unchanged
from vmedia.py into storage.py. Their shell quoting and build_filter grammar stay
intact. Update disk lifecycle, detach probe, LPAR power and recovery imports.
Virtual media consumes the helpers through the storage module; do not preserve
old helper globals as compatibility exports. Keep optical mapping identities,
repository, ISO server, mounting and media cleanup in vmedia.py. The detach probe's
stricter adapter_rows function is a different contract and remains unchanged.
Root owns this mechanical relocation; no request, output or safety behavior changes.

### Failure model
Malformed inputs retain the same None/empty-set outcomes. Command delimiters
remain guarded before shell transport. Approved old internal helper paths change;
missed consumers fail imports/tests rather than receiving copied implementations.
No hardware is invoked, and lifecycle/deletion decisions remain with their owners.

## Success
Four helpers have one storage owner and identical ASTs. Independent consumers
import that owner; virtual-media-specific helpers retain their existing ownership.

## Validation
focused-test: existing virtual media, disk lifecycle, detach, LPAR power, recovery
and HMC record-grammar tests cover malformed rows, quoting, cleanup and admission.
Compare all four moved ASTs. Run lint, format, types and live scenario guards.
task-test-not-applicable: no new behavior; retain existing behavioral coverage
rather than add source-spelling or duplicate algorithm tests.
