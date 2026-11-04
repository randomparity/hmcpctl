# Public five-field vNIC policy updater

Design for #1858, bound by `WORK:SCOPE` token `q1858-20c67c4e`. The decision is
recorded in [ADR 0332](../../adr/0332-public-vnic-policy-updater.md); the native
evidence it rests on is in [ADR 0312](../../adr/0312-vnic-policy-updates.md) and
`docs/live-testing.md` (#1856, #1857).

## Problem

An existing vNIC's adapter policy cannot be changed through hmcpctl. The only vNIC
mutations are add (`chhwres -o a`) and remove (`-o r`). #1857 qualified a small set
of `chhwres -o s` modes on HMC V10R3 M1060; nothing public offers them.

## Interface

One operation, `vnic.update_policy`, reachable three ways:

- `update_vnic_policy(hmc, lpar_name_or_uuid, slot_num, *, system_name_or_uuid,
  ownership_override=False, port_vlan_id=None, pvid_priority=None,
  auto_priority_failover=None, add_allowed_vlan_ids=None,
  add_allowed_os_mac_addrs=None) -> VnicPolicyUpdateResult` in
  `operations/virtualization/vnic.py`;
- MCP tool `hmc_update_vnic_policy`, `effect="mutate"`, `target_kind="lpar"`, with
  the same keyword inputs (strict int/bool via pydantic);
- CLI `hmcpctl network update-vnic-policy SYSTEM LPAR SLOT` with
  `--port-vlan-id`, `--pvid-priority`,
  `--auto-priority-failover/--no-auto-priority-failover`,
  `--add-allowed-vlan-ids 10,20`, `--add-allowed-os-mac-addrs <hex>,<hex>`,
  `--ownership-override` and `--yes`; it prints the result JSON and exits 1 on a
  partial result, as `add-vnic` does.

### Input validation (before any HMC read)

- Exactly one input is given. Combined inputs are refused: #1857 qualified only
  single-change records, and W10 shows a multi-pair record succeeds or fails whole.
- `pvid_priority`: strict int 0..7. `auto_priority_failover`: strict bool.
- `port_vlan_id`: strict int 1..4094. Zero is refused: only 0→nonzero is qualified.
- Each add list: nonempty, unique, canonicalized as at creation (VLAN ints
  1..4094; MACs 12 hex, lowercased).
- `slot_num` passes `require_command_safe_text`.

### Preflight (reads, no effect)

1. `resolve_and_authorize_lpar_names` (ownership; `ownership_override`).
2. `require_admitted_environment` (HMC V10R3 M1060, model 8375-42A).
3. `list_vnic_rows` for the partition, retained raw as `R1`. Rows are parsed with
   `_parse_vnic_snapshots`; a parse failure, including duplicate slot rows,
   propagates as `ValueError`. No row with `slot_num` → `VnicCapabilityError`.

### Derivation

Baseline `B1` is the selected snapshot. The input converges first:

- a scalar equal to its baseline is satisfied;
- a list add whose members are all in the baseline is satisfied; otherwise only
  the members not yet present remain.

Refuse with `VnicCapabilityError`, before any effect, when the unsatisfied input:

- is `port_vlan_id` and the baseline `port_vlan_id` is not 0 or the baseline
  `allowed_vlan_ids` is not `none` (HSCLAB12, unqualified transitions);
- is a list add whose baseline list is `all` or `none`;
- is a list add whose baseline list is unobservable (`None` in `observed_policy`);
- or when the baseline `auto_priority_failover` is unobservable and the input is
  not `auto_priority_failover` itself.

If the input is satisfied: return `mutation_dispatched=False`, `changed=False`,
`submission="not_submitted"`, readback verified against `B1`, preservation
`not_applicable`, no record.

Otherwise the record is one of exactly five shapes, the changed pair first and the
explicit `auto_priority_failover` (requested value, else the baseline) after it,
except that a list pair goes last:

- `port_vlan_id=V,auto_priority_failover=X`
- `pvid_priority=N,auto_priority_failover=X`
- `auto_priority_failover=X`
- `auto_priority_failover=X,allowed_vlan_ids+=<sorted members>`
- `auto_priority_failover=X,allowed_os_mac_addrs+=<sorted members>`

`build_vnic_update_attributes` in `ssh/vnic.py` renders it with
`build_attribute_record(quoted=("allowed_vlan_ids+", "allowed_os_mac_addrs+"))`;
a multi-member list becomes the trailing quoted pair. The expected value per
submitted field is the set value (for the preserving pair, the baseline), or the
sorted union for an add.

### Submission

`update_vnic_slot_policy(config, system, lpar, slot, record)` runs
`chhwres -r virtualio --rsubtype vnic -o s -m S -p L -s SLOT -a RECORD` once, every
token `shlex.quote`d. The outcome:

- returns → `completed`;
- `HMCCLIError` with `remote_nonzero_completed` or a `remote_refusal_code` →
  `rejected`, with `refusal_code`;
- any other exception → `unknown`.

### Readback and preservation

One `list_vnic_rows` after submission, whatever the outcome, retained raw as `R2`.

- **Readback** is a `VnicPolicyVerification` computed in the operation (no change
  to `VnicPolicy`, `observed_policy` or `verify_policy`): `checked_fields` are the
  submitted fields. Per field, `unavailable` when the read failed, the slot row is
  absent, or the cell does not parse (`port_vlan_id` must be all digits; the
  others via `observed_policy` on the raw row); `mismatched` when it differs from
  expected; else verified. Lists compare canonically. `vnic_after` is the parsed
  snapshot, or `None` when `R2` does not parse.
- **Preservation** compares raw rows, every `_VNIC_FIELDS` cell, keyed by
  `slot_num`: `unavailable` if the read failed. Otherwise copy `R2`, put `R1`'s raw
  cells back into the submitted fields of the selected slot, and compare with
  `R1`. Equal → `unchanged`; else `drifted` with `drifted_fields` as sorted
  `"<slot>:<field>"` strings (a slot added or removed reads as `"<slot>:*"`).
- **changed**: `True` when completed, verified and unchanged; `False` when the
  read succeeded and every submitted field still equals its baseline; else `None`.

Any result other than `completed`/`verified`/`unchanged` raises
`VnicPartialError(message, result)`, chained from the submission or read error.

### Result model

`VnicPolicyUpdateResult` (frozen dataclass): `operation="update_policy"`,
`slot_num`, `mutation_dispatched`, `changed`, `record`, `submission`,
`refusal_code`, `vnic_before: VnicSnapshot`, `vnic_after: VnicSnapshot | None`,
`readback: VnicPolicyVerification`, `preservation`, `drifted_fields`, `output`,
`errors`. `VnicPartialError.result` widens to
`VnicChangeResult | VnicPolicyUpdateResult`.

### Registration

- `tool_mirrors.py`: `"network update-vnic-policy": "hmc_update_vnic_policy"`.
- Operation ledgers (`_operation_maturity.json`, `docs/capabilities/*.json`):
  `vnic.update_policy`, implementation `partial` (implemented scope: the qualified
  modes; missing: replace/remove/clear, sentinels, two-list records, other port
  VLAN transitions), verification `unevidenced`. No promotion.
- `docs/tools/` regenerated; `docs/operations.md` names the tool; `CHANGELOG.md`.

## Native qualification

A procedure section in `docs/live-testing.md`, run only under a separate operator
grant, reuses the #1856/#1857 fixture lifecycle: preflight and prepare one owned
inactive fixture; create each vNIC with `hmcpctl network add-vnic`; run
`hmcpctl network update-vnic-policy`; read with `hmcpctl network list-vnics`;
remove each vNIC with `hmcpctl network remove-vnic`; delete the fixture with the
ownership-checked inactive-only `hmcpctl lpars delete`; witness preflight and
recovery. By completion criterion 7 the direct CLI invocation is the path under
test; each update's result JSON, kept private, is its journal. Cases run in order
on two vNICs (failover baseline 0, so a reset would show) and cover every
offered record shape, bare and quoted:

| # | Baseline | Input | Record exercised |
|---|---|---|---|
| U1 | vNIC 1: VLAN {A}, MAC {M1} | `pvid_priority` 5 | `pvid_priority=5,auto_priority_failover=0` |
| U2 | U1's result | add VLAN {B, C} | `auto_priority_failover=0,"allowed_vlan_ids+=B,C"` |
| U3 | U2's result | add VLAN {D} | `auto_priority_failover=0,allowed_vlan_ids+=D` |
| U4 | U3's result | add MAC {M2, M3} | `auto_priority_failover=0,"allowed_os_mac_addrs+=M2,M3"` |
| U5 | U4's result | add MAC {M4} | `auto_priority_failover=0,allowed_os_mac_addrs+=M4` |
| U6 | U5's result | `auto_priority_failover` true | `auto_priority_failover=1` |
| U7 | vNIC 2: VLAN `none`, MAC {M1} | `port_vlan_id` V | `port_vlan_id=V,auto_priority_failover=0` |

Any result other than completed/verified/unchanged stops the window. On a stop,
record the result, take a read-only `list-vnics`, and leave further effects to a
separate grant: removing the case's vNICs and the fixture through the
ownership-checked commands, then witness preflight and recovery, as #1856 and
#1857 did. Outcomes are recorded as returned; a hypothesis is never a result.

## Failure model

- **Actors and deployments:**
  - an MCP client or CLI operator authorized for the `lpar` target and owning it;
  - offline CI and local tests through mocked SSH;
  - one operator-granted native window on the established test HMC.
- **Invariants and assets:**
  - foreign vNIC, backing and partition configuration;
  - the selected vNIC's untargeted cells, including `auto_priority_failover`;
  - one submission per request; truthful axes (missing evidence never reads as
    success).
- **Accepted failure classes:**
  - A concurrent writer between the baseline read and the submission: its
    `auto_priority_failover` change can be overwritten; preservation reports drift
    elsewhere. Writer coordination is the operator's, as for add/remove.
  - A volatile cell (backing operational state) changing on its own reads as
    `drifted`. Over-reporting is the safe direction.
  - An `unknown` submission is not retried; the caller reads back and decides.
  - Qualified modes hold for the admitted environment only, which
    `require_admitted_environment` already restricts.
- **Covered elsewhere:**
  - backing updates and failover #669; system defaults #680; multi-backing #638;
    profile CRUD #1724/#637; maturity/release promotion (out of scope).

## Threat model

- **Boundaries:** widened — new MCP tool and CLI command accepting five policy
  inputs and a slot; a new remote command built from them.
- **Actors:** an authenticated MCP client or local operator; the HMC is trusted.
- **Controls:** tool-registry target authorization (`lpar`) and the mutate effect
  gate; LPAR ownership via `resolve_and_authorize_lpar_names`; strict typed
  validation before any read; `require_command_safe_text` for the slot;
  `build_attribute_record` for record grammar; `shlex.quote` on every token;
  error text passes through the transport's existing redaction.
- **Out of scope:** authorization of the VIOS or backing (no backing changes);
  denial of service by repeated calls (one attempt each, no retry).

## Success

1. The five inputs, validation and pre-effect refusals above are enforced, each by
   a focused test.
2. A qualified request submits exactly one record of the stated shape and returns
   completed/verified/unchanged.
3. Rejected, unknown, read-failure, mismatch and drift outcomes each produce the
   stated axes and raise `VnicPartialError` with the result.
4. The CLI and MCP surfaces reach the operation; `docs/tools/`, ledgers and
   `CHANGELOG.md` are updated; `just verify` and prek pass.
5. U1–U7 run natively under a grant and their outcomes are recorded publicly
   without lab identifiers.
