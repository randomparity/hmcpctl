# Virtual SCSI client adapters at partition creation

Issue #1980 (part of #1830); claim `q1980-6f21b9bd`. Authority: the issue's
[`WORK:SCOPE` charter](https://github.com/randomparity/hmcpctl/issues/1980#issuecomment-6854819748).
Lane full-spec; complexity M; fixed denominator 250. Decision:
[ADR 0376](../../adr/0376-lpar-vscsi-client-creation.md).

## Global Constraints

Python 3.11 local floor; CI amd64 Ubuntu 24.04 on Python 3.11–3.14. No dependency,
toolchain-floor or `hmcpctl.api` facade change. Bootstrap with `just setup`; every `uv run`
passes `--no-sync`. `just verify` and `uv run --no-sync prek run --all-files` before push.
Synthetic names only in tests and docs (`sys-R1`, `vios-R1`, `example-lpar`).

## Problem and outcome

Creation can declare virtual serial adapters but no vSCSI client adapter, so an IBM i
virtual load source (#1981) has no adapter in the partition's own creation record. Add an
optional `virtual_scsi_adapters` collection of typed client records to LPAR creation
(`AIX/Linux` and `OS400`), rendered into the one `mksyscfg` record, admitted before any
effect, and compared against the created `default_profile` afterwards.

Live capture (2026-11-13, V10R3, `sys-R1`): `mksyscfg` for an `os400` partition accepted
`virtual_scsi_adapters=2/client/<vios-id>/<vios-name>/20/0` with no server slot 20 on the
VIOS; the profile readback echoed the record; the HMC created no VIOS server adapter and
no mapping. A client record therefore does not establish pairing.

## Input

`VirtualScsiClientAdapter` (new `documents/lpar_vscsi.py`), frozen, `extra="forbid"`:
`local_slot: StrictInt`, `remote_slot: StrictInt`, `is_required: StrictBool`,
`remote_lpar_id: StrictInt | None`, `remote_lpar_name: StrictStr | None`.

- There is no role field: every record is a client; a `role` or other extra key is refused
  (criterion 3, "server-role records are not accepted").
- `local_slot >= 2`, `remote_slot >= 2`, `remote_lpar_id >= 1`; exactly one of ID and name.
  Names are checked with the serial grammar guard (`serial_name`: no `/ , = " \` or controls).
- `native()` renders `slot/client/<id>/<name>/<remote-slot>/<0|1>`, the absent selector empty.
- `validate_vscsi_input(adapters, maximum, serial)` requires an explicit
  `max_virtual_slots` when the collection is non-empty, every local slot below it, and local
  slots unique across the vSCSI collection *and* the serial collection's local slots
  (criterion 2). The encoded collection is bounded to 64 KiB, as serial. Empty returns `""`.
- Explicit `keylock` with vSCSI records is refused (native creation cannot carry keylock).

Presentations: `hmc_create_lpar(virtual_scsi_adapters=...)` and
`hmcpctl lpar create --virtual-scsi-adapters '<inline JSON array>'`. The VIOS creation owner
is untouched (criterion 5 names AIX/Linux and OS400 only). `hmc_create_lpar` already
declares `exhaustive_targets=False`, so the nested VIOS target needs no access-policy change.

## Admission (before the create)

`admit_vscsi_creation` (new `operations/lpar/creation/vscsi.py`) reuses the serial owner's
contained inventory (LPAR and VIOS feeds of the selected system, duplicate-identity refusal)
and exact resource read. For each record:

1. The selector resolves to exactly one inventory entry; an unresolved selector is refused.
2. That entry must be of kind `VirtualIOServer`; an LPAR (or the pending partition) is
   refused as "not a VIOS on the selected system".
3. The VIOS is read fresh, its identity must agree with the inventory, and its description
   passes `authorize_lpar_ownership_description(..., operation="lpar-mutation")` — the same
   ownership policy serial peers use (unowned passes, foreign or malformed owner refuses).
4. `remote_slot` must be below the VIOS's `MaximumVirtualIOSlots`.

Any refusal raises before `mksyscfg`; nothing is created.

## Render

`create_lpar_via_cli(..., virtual_scsi_adapters=())` validates with the serial collection and
appends `,"virtual_scsi_adapters=<value>"` exactly as serial does. Selecting vSCSI routes
creation through the native path, defers profile apply until verification, and gates
follow-up assignments, like serial (`profile_features_requested`).

## Observation and comparison

`vscsi_creation_step` produces a `WorkflowStep("virtual_scsi_adapters", ...)` whose result is:

```
{"configuration": "verified" | "unverified", "plane": "saved-default-profile" | "current-unavailable",
 "vios_server_adapters": "not-created", "vios_mappings": "not-created", "pairing": "unproven",
 "records": [{"slot": n, "status": "verified" | "mismatch" | "unverified"}]}
```

- Controller mode has no named profile: status `error`, plane `current-unavailable`.
- Otherwise confirm the created identity with the serial owner's confirmation, read
  `default_profile` through `read_lpar_profile_record`, bind it to name/ID/profile, and parse
  `virtual_scsi_adapters` (`none`/empty = no records; six components each; numeric slots).
- The observed slot set must equal the requested set, and each record must be `client`, carry
  the requested remote slot and required flag, and point at the admitted VIOS: at least one of
  ID/name present, each present one equal to the admitted VIOS's ID/name.
- Status `ok` only when every record is verified. Any read/parse failure is `error` with
  `observation_error` naming the exception type only.
- The three fixed statements are true by construction: this change never writes the VIOS.

The serial observation's "no other virtual family" exclusivity guard would otherwise fail a
creation that declares both. `parse_serial_profile`/`read_serial_profile` gain a `declared`
family set excluded from that guard, and serial admission carries it (only
`virtual_scsi_adapters`, only when vSCSI is requested). The vSCSI step owns that family.

## Failure model

1. **Actors and deployments** — an MCP client or CLI operator holding a `lpar.create` grant,
   against one HMC-managed system with at least one VIOS.
2. **Invariants and assets** — no effect on the VIOS (no server adapter, no mapping, no profile
   change); no create when admission refuses; ownership of the named VIOS honoured; the result
   never claims pairing or a usable disk.
3. **Accepted failure classes**
   - TOCTOU between admission and create (VIOS renamed, re-owned or its maximum lowered):
     accepted, bounded — the HMC stores the record regardless and the readback compares
     against the admitted identity; same contract as serial (ADR 0303).
   - An ambiguous `mksyscfg` outcome propagates the existing plain-create error path; no retry
     or rollback, as for every native creation.
   - The remote server slot may already exist, be occupied or be absent: accepted — pairing is
     reported `unproven`, never checked.
   - A deferred profile apply after a verified comparison (default `apply_partition_profile`)
     runs against a client whose server slot may be absent; its outcome is reported as-is,
     with no retry or rollback. Arm74 records that apply step's status.
4. **Covered elsewhere** — VIOS server adapters and mappings (#680, #663); profile lifecycle
   (#1724); IBM i installation (operator); load-source / alternate-restart slot binding to a
   declared client slot (#1981, which references `local_slot`).

### Threat model

- **Boundaries** — added: a caller-supplied partition name/ID for a VIOS crosses into an
  `mksyscfg` record and into an ownership decision. Widened: none.
- **Actor** — an authenticated but possibly over-reaching MCP/CLI caller naming a VIOS it does
  not own, or injecting record delimiters through the name.
- **Controls** — strict types and `serial_name` delimiter refusal; the shared
  `build_attribute_record`/`shlex.quote` layering already used for serial; VIOS resolution by
  contained inventory plus ownership authorization (audited denial); 64 KiB input bound.
- **Out of scope** — a VIOS administrator racing the create (accepted above); access-policy
  target extraction for nested VIOS names (already non-exhaustive for `hmc_create_lpar`).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Record grammar and refusals | focused-test | `tests/lpar/test_vscsi_creation.py`: both/neither selector, slot < 2, extra `role` key, delimiter in name, native rendering |
| Slot rules | focused-test | same file: duplicate slot, slot >= max, missing max, serial/vSCSI collision |
| Admission | focused-test | same file: non-VIOS remote, unresolved remote, foreign-owned VIOS, remote slot >= VIOS max, success |
| Render | focused-test | same file: `create_lpar_via_cli` command carries the quoted attribute |
| Observation | focused-test | same file: verified, readback mismatch, missing record, controller mode, unavailable read; three fixed statements |
| Serial coexistence | focused-test | `tests/lpar/test_serial_input.py`: declared family excluded from exclusivity |
| MCP/CLI wiring | focused-test | `tests/app/test_lpar_vscsi.py`: MCP schema forbids extras; CLI inline JSON decode and refusal |
| Live scenario | live | arm74 (`scripts/live_test_runner.py --group lpar-vscsi-client` per `docs/live-testing.md`); its stub-client unit test is not evidence for the criterion, and without a granted lab run criterion 6's live half is reported unmet |
| Generated docs and ledger | focused-test | `just tool-docs-check`, `just doc-freshness`, `just capability-inventory` |
