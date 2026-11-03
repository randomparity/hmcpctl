# Load and unload media in an existing virtual optical device (#1285)

Decision record: [ADR 0317](../../adr/0317-optical-media-load-through-viosvrcmd.md).
Evidence: the #1284 capture, `tests/fixtures/live/README.md` (prefix `2026-11-02-q1284/`).

## Problem

hmcpctl can create an optical mapping (`mount_optical_media`) and remove one
(`unmount_optical_media`), but cannot change the medium in a mapped file-backed virtual optical
device. Operators run `viosvrcmd` `loadopt`/`unloadopt` by hand, sometimes with `-release`.

## Scope

Two operations in `src/hmcpctl/operations/storage/resources.py`, each with an MCP tool and a CLI
command beside mount and unmount:

| Operation | MCP tool (effect) | CLI | VIOS command |
|---|---|---|---|
| `load_optical_media` | `hmc_load_optical_media` (`destructive`, `media.load`) | `storage load-optical-media` | `loadopt [-release] -disk M -vtd V` |
| `unload_optical_media` | `hmc_unload_optical_media` (`destructive`, `media.unload`) | `storage unload-optical-media` | `unloadopt [-release] -vtd V` |

Inputs: `vios_name_or_uuid`, `lpar_name_or_uuid`, `media_name` (load only), optional
`target_device`, `release=False`, `ownership_override=False`, optional `system_name_or_uuid`,
and the MCP `profile`.

Sequence, each step refusing before any later one runs:

1. Validate `media_name` against `[A-Za-z0-9_.]{1,79}` (the rule `upload_iso` enforces) and a
   supplied `target_device` against `[A-Za-z0-9_]{1,15}`, before any HMC request.
2. Resolve the VIOS (`resolve_vios_uuid`) and authorize the client LPAR with
   `resolve_and_authorize_lpar_mutation` (ADR 0092 rule, as mount).
3. Read the VIOS mapping group (`hmc.list_storage_mappings(vios_uuid, lpar_uuid)`) and collect the
   `TargetName` of each mapping whose `TargetDevice` holds a `VirtualOpticalTargetDevice`, with or
   without `Storage`. A supplied `target_device` must be in that set; an omitted one requires the
   set to hold exactly one name. Otherwise raise `HMCError` naming the candidates (or none).
   A selected device whose name is outside the device-name rule is refused with a message
   naming the mapped device (not the caller's argument).
4. Read the VIOS (`hmc.get_vios`): `PartitionID` must be a positive integer and
   `AssociatedManagedSystem` must link a system UUID, resolved to its `SystemName`. Either missing
   raises `HMCError` before any command.
5. When `release` is true, emit the `optical-release` audit record.
6. Submit `viosvrcmd -m <system> --id <id> -c '<inner>'` through `run_hmc_command` with a 64 KiB
   output bound, and classify the answer (ADR 0317 decision 5):
   - exit 0 and output `:` after stripping whitespace → success;
   - `HMCCLIError` with `remote_nonzero_completed` → `HMCError` "refused", carrying the HMC's text;
   - any other `HMCError`, or exit 0 with any other output → `HMCError` "completion unknown; read
     the device (`hmc_list_storage_mappings`) before retrying", carrying the text.
   Each names the action, VTD and VIOS and states that nothing was retried.

Result (`StorageMutationResult.from_raw`): `action`, `vios_uuid`, `lpar_uuid`, `target_device`,
`media_name` (load) and `release`. No `change_location`: the device's medium is VIOS state, not
partition configuration. The MCP tools return that dict; the CLI prints it after a confirmation
prompt that, when declined, submits nothing.

Audit: `event: "optical-release"` with `action` (`load`/`unload`), `system`, `vios`, `lpar`,
`target_device`, `media` (null on unload), `host` and `attribution`, emitted at warning level,
documented in `docs/authorization-audit.md`. A call refused in steps 1–4 emits none.

Registries and docs: capability `operations.json`/`maturity.json` rows; ADR 0092 §3 rows for both
operations, with `load_`/`unload_` added to `_MUTATOR_PREFIXES` in
`tests/unit/test_adr_0092_citations.py` so the guard-reachability check covers them;
`_VMEDIA_MUTATIONS` in `scripts/live_test_recovery.py`; the storage mutation list in
`docs/cli.md`; `docs/tools/` (regenerated). The unmount and unload docstrings say an unloaded
mapping is listed only by `hmc_list_storage_mappings` and must be loaded again before unmount
can remove it.

Live arm: ST71 (`scripts/live_test/vmedia_media_change.py`) replaces its raw `viosvrcmd`
`unloadopt`/`loadopt` calls (Q2, Q3 and the cleanup reload) with the two tools. After the immediate
power-off, Q3 runs unload, load with `release=True`, unload with `release=True`, then load, each
followed by the existing `lsvopt` read. Each tool step is a PASS row only when the tool succeeded
and the following `lsvopt` reads the expected medium (or `No Media`). One negative step loads
the second medium into the occupied device through the tool and records the tool's answer; the
raw `viosvrcmd` refusal is captured once as a fixture for the classification test. Q1 and Q4 are
unchanged.

Not in scope: `-f`, retrying after a failure, verifying the medium by a readback, and the
exclusions in the issue's `WORK:SCOPE`.

## Failure model

1. **Actors and deployments**
   - an operator through the CLI, or an MCP client agent, against one configured HMC;
   - the live arm on the operator host.
2. **Invariants and assets at stake**
   - a client partition's loaded medium (an installer or boot image);
   - the ownership rule: a device not bound to the authorized LPAR at the binding read is not
     changed;
   - an audit record for every `release` request submitted to the VIOS.
3. **Accepted failure classes**
   - a failed command leaves the device in whatever state the VIOS left it; the error says
     nothing was retried and the operator inspects (`hmc_list_storage_mappings`). Cost bounded to
     one device;
   - a mapping change between step 3 and step 6 is not detected; ADR 0079 puts serializing VIOS
     mapping changes on the caller;
   - a medium or device name outside the rules is refused although the VIOS might accept it,
     including a device pinned at mount (ADR 0317);
   - an unloaded mapping is invisible to `hmc_list_optical_mappings` and not removable by
     `hmc_unmount_optical_media` until reloaded; documented in both tools.
4. **Covered elsewhere**
   - retry after a 5xx: ADR 0169; concurrent VIOS writes: ADR 0079;
   - repository creation and ISO upload: ADR 0031/0049; physical optical devices: out of scope.

## Threat model

1. **Boundary inventory.** Adds: caller-supplied `media_name` and `target_device` entering an HMC
   shell command and, inside it, a VIOS shell command. Widens: none.
2. **Actor model.** An MCP client agent or CLI operator holding a connection grant; trust is
   placed in the HMC and VIOS responses (device names, partition id, system link).
3. **Control per boundary.** Both names are refused unless they match the fixed character sets
   above, so neither shell can see a metacharacter, whitespace or leading `-`; the outer `-c`
   argument and system name are `shlex.quote`d; the partition id is an `int`. Ownership:
   ADR 0092 guard plus the mapping binding of step 3. Errors carry the HMC's text, never
   credentials (`run_hmc_command` already redacts declared secrets; none are sent).
4. **Out of scope.** A compromised HMC or VIOS returning hostile device names (step 3 still
   applies the device-name rule to them).

## Success

- Load and unload reach the VIOS as the captured command shapes for the selected device, with
  `-release` only when requested.
- Refusals happen before any write for: a bad name, a foreign or unowned LPAR, a device not owned
  by the LPAR's mapping, zero or several candidate devices, and an unusable VIOS identity.
- A `release=True` call that passes steps 1–4 emits exactly one `optical-release` record before
  the command; a refused call emits none. (The sink drops a record it cannot render, ADR 0040.)
- A refusal, an unknown completion, and an unexpected exit-0 answer are each reported as such,
  never as success.
- Live: the tools load, unload and run the release form on the test partition (ST71).

## Validation

- `tests/storage/test_optical_load.py` (focused-test): command text matched against
  `cli-vios-loadopt`/`cli-vios-unloadopt` after substituting their redaction tokens (`dev-14`,
  `media-2`) with rule-conformant names, plus a test that the unsubstituted tokens are refused;
  `-release` placement from the help fixtures; binding driven through `list_storage_mappings`
  over `rest-vios-scsi-mapping-unmapped-adapter.json` (empty device) and
  `rest-vios-scsi-mapping-replace-storage.json` (loaded devices), tokens substituted the same way;
  every refusal above makes no SSH call; the three outcome classes, the refusal one from the
  captured refused `loadopt` once the live proof commits it; audit record only with `release`.
- `tests/unit/test_audit.py`: the `optical-release` record shape.
- `tests/storage/test_storage_tools.py`, `tests/app/test_cli_commands.py`: wrappers forward
  arguments; a declined CLI prompt does not call the operation.
- `tests/test_live_vmedia_media_change_arm.py`: Q2/Q3 call the tools in the stated order.
  ST71 stays a capture arm: its `check` rows are the live proof for this change and promote
  no maturity, so `media.load`/`media.unload` remain `unevidenced` in the ledger.
- Registry and documentation gates: `just static` (capability inventory, tool docs, ADR numbering).
