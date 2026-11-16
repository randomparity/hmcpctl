# IBM i virtual tagged I/O bound to declared vSCSI clients

Issue #1981 (part of #1830); claim `q1981-f4b18b42`. Authority: the issue's
[`WORK:SCOPE` charter](https://github.com/randomparity/hmcpctl/issues/1981#issuecomment-6858455946).
Lane full-spec; complexity M; fixed denominator 250. Decision:
[ADR 0382](../../adr/0382-ibmi-virtual-tagged-io-bound-to-declared-vscsi.md), amending
ADR 0274; builds on ADR 0376 (#1980).

## Global Constraints

Python 3.11 local floor; CI amd64 Ubuntu 24.04 on Python 3.11–3.14. No dependency,
toolchain-floor or `hmcpctl.api` facade change. Bootstrap with `just setup`; every `uv run`
passes `--no-sync`. `just verify` and `uv run --no-sync prek run --all-files` before push.
Synthetic names only in tests and docs (`sys-R1`, `vios-R1`, `example-lpar`).

## Problem and outcome

`LparPhysicalIO` refuses every positive virtual-slot form of the three IBM i tagged fields,
so a VIOS-hosted IBM i partition cannot be created with its load source. The HMC enforces
almost nothing here (ADR 0382 context), so hmcpctl binds the virtual form to a vSCSI client
record declared in the same creation request.

## Rules

A *virtual form* is an ASCII decimal string shorter than eight characters (a DRC is eight
uppercase hex digits; unchanged discriminator).

1. `LparPhysicalIO` (document): a virtual form of `load_source_slot` or
   `alt_restart_device_slot` must be canonical decimal (`"02"` refused) and is otherwise
   admitted by the document. A virtual form of `console_slot` is refused naming the missing
   console-family source; `alt_console_slot`/`op_console_slot` keep their physical-DRC rule.
   `hmc` and DRC forms are unchanged. New `virtual_tagged() -> tuple[tuple[str, int], ...]`.
2. Binding (criterion 1), `validate_tagged_vscsi(io, adapters)` in `documents/lpar_vscsi.py`:
   each virtual-tagged slot must equal a declared `VirtualScsiClientAdapter.local_slot`, else
   refuse with a message naming the rule and the refused kinds (undeclared, serial, HMC-default
   serial 0/1, ethernet — creation has no virtual ethernet input, so an ethernet slot is always
   undeclared). Called by `validate_lpar_creation` (before any read) and by
   `create_lpar_via_cli` (backstop at the only `mksyscfg -r lpar` render for LPARs).
3. Console (criterion 3), `require_ibmi_console(partition_type, physical_io)` in
   `ssh/lpar.py`: for `lpar_env=os400`, `physical_io.console_slot` must be set, else refuse
   ("supply physical_io.console_slot: hmc or a declared physical/SR-IOV DRC; no default is
   inserted"). Called in `_create_via_cli` before its effect-classifying `try` (so the start
   and serial outcome handlers never see it as an unknown outcome) and in
   `create_lpar_via_cli`, which `scripts/live_test/arms/lpar_serial_adapter.py` (ST63) calls
   directly; ST63's OS400 leg therefore passes `console_slot="hmc"`. Criterion 3 is bounded to
   native (`mksyscfg`) creation: the REST create path is unchanged, because keylock creation is
   REST-only and `admit_creation_io` refuses `physical_io` with keylock, so a universal console
   rule would remove IBM i keylock creation. The REST 406/REST0001 fallback reaches
   `_create_via_cli` and is refused there, after a refused REST write.
4. Admission: `admit_creation_io`, which the provisioning preflight also calls before any
   effect, runs rule 2 and, when physical I/O is requested, rule 3; it skips virtual forms in
   its per-device loop; its OS400 and fresh `IBMiCapable` checks still apply because
   `tagged()` is non-empty.

## Comparison (criterion 4)

- REST `compare_creation_io` skips virtual forms (REST rendering uncaptured, ADR 0382).
- `read_vscsi_profile` returns `VscsiProfile(records, attributes)`, `attributes` being the
  bound saved-profile attribute map it already parses.
- `VscsiAdmission` gains `roles: tuple[tuple[str, int], ...] = ()`; `admit_vscsi_creation`
  takes `roles` (passed `inputs.physical_io.virtual_tagged()` by the coordinator).
- `vscsi_creation_step`: a record whose slot carries roles adds `"roles": [names]`; it is
  `verified` only when its client record matches (unchanged rule) **and** each role's saved
  attribute equals `str(slot)`. Otherwise `mismatch`, the step errors, apply stays skipped.
- `console_slot=hmc` and physical forms keep their REST comparison.

## Presentation and docs

Field descriptions in `documents/lpar_io.py`; `hmc_create_lpar` and `hmc_provision_lpar`
docstrings drop "linkage gap #638"; regenerate `docs/tools/`. `docs/capabilities/lpar-allocation.md`
C12 paragraph, ADR 0274 status amendment line, CHANGELOG *Added*/*Changed*. Live arm
`lpar-vscsi-client` (ST74): `load_source_slot` and
`alt_restart_device_slot` set to the local slot, and asserts the record's `roles` verified.

## Failure model

1. **Actors and deployments** — local operator or agent calling `hmc_create_lpar`/CLI
   against an HMC it is configured for; live lab runs per `docs/live-testing.md`.
2. **Invariants and assets** — no HMC write before every rule refuses; a created partition is
   not reported verified when a role the request set as a virtual slot reads back as another
   value; the published MCP input
   contract (admits strictly more inputs; console-less IBM i native create now refused early).
3. **Accepted failure classes** — a load source whose VIOS server adapter or disk is absent
   (pairing unproven, ADR 0376); IBM i boot behaviour (no install or activation, exclusion);
   REST rendering of virtual tagged slots (uncompared); concurrent external profile edits
   between create and readback (ADR 0274 contention).
4. **Covered elsewhere** — server adapters and mappings → #680/#663; profile lifecycle →
   #1724; IBM i OS install → operator; vFC/SR-IOV virtual load sources → #1830.

## Validation

Focused tests in `tests/lpar/test_physical_io.py` and `tests/lpar/test_vscsi_creation.py`;
live ST74 on the operator's lab under a grant.
