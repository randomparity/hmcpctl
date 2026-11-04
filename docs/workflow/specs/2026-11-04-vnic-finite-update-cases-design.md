# Scalar and finite list-mode vNIC update cases

Design for #1857, bound by `WORK:SCOPE` token `q1857-ce7176a2`. It extends the
version-2 source owner described in the
[source qualification design](2026-11-01-vnic-update-source-design.md) and recorded
in [ADR 0312](../../adr/0312-vnic-policy-updates.md). That design's packet, budgets,
lifecycle, recovery and removal rules hold unless this document changes them.

## Problem

The six version-2 cases tested only the `all`/`none` sentinels and removal of the
last member. #1856 ran them on HMC V10R3 M1060. On an existing vNIC the sentinels
were rejected, and removing the last VLAN or MAC member was refused (`HSCLAB1F`,
`HSCLAB20`). Creation with a nonzero port VLAN ID and a finite VLAN list was
refused (`HSCL12E9`). Nothing has tested an update of `port_vlan_id`,
`pvid_priority` or `auto_priority_failover`, a finite replacement, a finite add
(`+=`) or remove (`-=`), or two lists in one submission. #1858's updater may offer
only modes that a native run qualified.

## Decision

Add one version-2 case, `finite-update`. Its packet carries one extra key,
`update`, which lists the changes to make. The harness derives the native record
and the oracle from the packet. The caller supplies no command, record or oracle.
The six existing cases keep their schema and behaviour exactly, and they reject an
`update` key.

### Packet

A `finite-update` packet has the 20 version-2 keys plus `update`. Version-2 checks
apply as before, except for the `policy` list rule below.

`policy` is the creation baseline. `pvid_priority` and `auto_priority_failover`
must be explicit. Each list is a finite nonempty list or `"all"`. `"none"` is
refused, and so is `mac_addr`.

`update` is a list of one to five actions, with at most one action per field. Each
action is exactly `{"field", "operation", "value"}`:

| Field | Operations | Value |
| --- | --- | --- |
| `port_vlan_id` | `set` | strict integer 0..4094 |
| `pvid_priority` | `set` | strict integer 0..7 |
| `auto_priority_failover` | `set` | strict bool |
| `allowed_vlan_ids`, `allowed_os_mac_addrs` | `set`, `add`, `remove` | finite nonempty list, canonicalized as at creation |

The packet is refused before any effect in each of these cases:

- `set` gives the field's baseline value. A no-op cannot be told apart from an
  ignored update.
- `add` or `remove` targets a baseline of `"all"`. This keeps `all` minus finite
  members, and every other sentinel delta, unqualified.
- `add` repeats a baseline member.
- `remove` names a member the baseline lacks, or names every member. Removing the
  last member is already known to be refused.
- A value is `"all"` or `"none"`. The sentinels are creation-only on the observed
  HMC.

Starting a `set` from an `"all"` baseline is allowed. That is a replacement, not a
delta.

### Native record

The record has one pair per action, in fixed field order: `port_vlan_id`,
`pvid_priority`, `auto_priority_failover`, `allowed_vlan_ids`,
`allowed_os_mac_addrs`. The attribute is the field name, with `+` appended for
`add` and `-` for `remove`. List values are the sorted canonical members joined by
commas. A bool renders as `0` or `1`.

The record is built with `build_attribute_record`. The list attributes, with and
without the suffixes, are quotable, and `allow_nontrailing_quoted=True` is passed.
The retained command reference shows this exact form for an existing vNIC,
`"allowed_vlan_ids+=10,20,30",pvid_priority=5`
(`docs/refs/hmc-commands-p11/commands/chhwres.md:292`). vNIC creation already
sends quoted lists that are not last (ADR 0311). The shared builder and every
other caller are unchanged. The record stays inside the existing update token and
shell quoting.

### Oracle and axes

The expected value for each action field is:

- `set`: the value;
- `add`: the sorted union of the baseline and the value;
- `remove`: the sorted baseline minus the value.

`source.expected` stores this as a mapping from field to expected value. The six
old cases keep their `{field, value}` shape.

`policy_verification` reads only the action fields in B2's selected row:

- `port_vlan_id` must be all digits and equal the expected integer;
- every other field is compared with its canonical `observed_policy` value.

If any action field is absent or unparseable, the result is `unavailable`.
Otherwise any difference is `mismatched`, and full agreement is `verified`.
Comparing canonical lists is deliberate. #1856 established no raw ordering or
casing for lists with several members, so a reordered but equal readback is a
match. The raw rows are retained.

`preservation` puts B1's raw cells back in every action field of B2, then requires
B2 to equal B1 exactly. A side effect on a field outside the actions therefore
reads as `drifted`.

Submission, qualification and lifecycle values are unchanged. A completed
submission whose readback is verified and unchanged is `qualified`. A structured
HSCL rejection with B2 equal to B1 is `refused`. Anything else is `unresolved`.

### Cleanup and recovery

Only a qualified case can arm removal, as before. The expected cleanup object is
the packet with every action field replaced by its expected value. Removal goes
through the same `_cleanup_candidate`, the removal guard and the before-state
proofs. A refused or unresolved case leaves its vNIC behind for the operator.

Recovery runs the same derivation again from the packet: the record, the expected
mapping, the comparisons, the cleanup proof and the ordered effect commands. A
history labelled `finite-update` and edited by hand cannot produce `CLEAN`.

Physical budgets and suffix reserves are unchanged, because the case still makes
one create, one update and one remove. The fixture's limit of six source histories
is unchanged. A sequence of more than six cases, or one that contains a non-CLEAN
history, removes the fixture through the ownership-checked inactive-only
`hmcpctl lpars delete`, as #1856 did.

### Live case set

Each row is one packet and one admitted window, run in this order. Every baseline
has `port_vlan_id=0` and explicit scalars. VLAN IDs `A`..`C` and MAC addresses
`M1`..`M3` are private operator values.

| # | Baseline lists | Actions | Hypothesis |
| --- | --- | --- | --- |
| 1 | VLAN {A}, MAC {M1} | `pvid_priority` set to a different value | qualified |
| 2 | VLAN {A}, MAC {M1} | `auto_priority_failover` set to the opposite | qualified |
| 3 | VLAN `all`, MAC {M1} | `port_vlan_id` set to a nonzero value | qualified |
| 4 | VLAN {A}, MAC {M1} | VLAN set to {B, C} | qualified |
| 5 | VLAN {A}, MAC {M1} | VLAN add {B} | qualified |
| 6 | VLAN {A, B}, MAC {M1} | VLAN remove {B} | qualified |
| 7 | VLAN {A}, MAC {M1} | MAC set to {M2, M3} | qualified |
| 8 | VLAN {A}, MAC {M1} | MAC add {M2} | qualified |
| 9 | VLAN {A}, MAC {M1, M2} | MAC remove {M2} | qualified |
| 10 | VLAN {A}, MAC {M1, M2} | VLAN add {B} and MAC remove {M2}, in one record | qualified |
| 11 | VLAN {A}, MAC {M1} | `port_vlan_id` set to a nonzero value | refused, like creation's `HSCL12E9` |

Case 11 runs last because its expected residue disarms the fixture cleanup. The
outcome recorded is whatever the HMC returns. A hypothesis is never a result.

## Failure model

- **Actors and deployments:**
  - a local authorized operator running one root-admitted window on the
    established test HMC and system;
  - offline CI and local tests through synthetic transports.
- **Invariants and assets:**
  - the owned fixture and its captured vNIC;
  - foreign vNIC, backing and logical-port configuration;
  - one attempt per effect;
  - removal only after qualification;
  - truthful, private, bounded evidence.
- **Accepted failure classes:**
  - Remote uncertainty, a timeout, a refusal or a drifted readback leaves residue
    under `MANUAL_RECOVERY`. Bounded fail-stop plus the operator's per-case removal
    is the approved outcome, as in #1856.
  - An uncoordinated writer during a window. Writer acknowledgement is
    coordination, not a lock.
  - Canonical list equality accepts a reordered readback. It says nothing about
    how the HMC orders lists.
  - A qualified case proves only the stored policy for that exact case and
    environment. It does not prove traffic, failover or general cross-products.
- **Covered elsewhere:**
  - the public updater #1858;
  - backing updates and failover #669;
  - system defaults #680;
  - multi-backing #638;
  - profile CRUD #1724/#637;
  - the evidence-script route for source journals, a follow-up already reported
    by #1856.

## Threat model

- **Boundaries:**
  - Widened: the version-2 packet parser gains the `update` list.
  - Widened: a derived native record now carries several pairs and quoted lists
    that are not last.
  - No new listener, credential or file path.
- **Actors:**
  - the local operator, who writes the packet;
  - HMC responses, which may be malformed or stale.
  - Code and the operator are trusted. Observations confer no mutation authority.
- **Controls:**
  - exact keys, strict types and canonical members per action;
  - the refusals listed under Packet, applied before effects;
  - values rendered through `build_attribute_record`'s grammar guard, then
    `shlex.quote`;
  - the existing one-shot update token, intent journal and budgets;
  - recovery recomputing the command from the packet.
- **Out of scope:** same-user tampering with code or journals, a hostile HMC, and
  an atomic HMC compare-and-swap, as in the source design.

## Success

- A `finite-update` packet that breaks any rule under Packet is refused before
  any transport call.
- For each action shape in the case table, offline capture produces the exact
  derived record and reaches `CLEAN`, and recovery reaches `CLEAN`.
- Offline, a refused, unknown, mismatched, unavailable or drifted outcome never
  arms removal. A forged `finite-update` history never recovers as `CLEAN`.
- The six existing cases, version 1 and version 3 keep their behaviour, and their
  existing tests pass unchanged.
- The native case set runs under separate grants. Its public-safe outcomes are
  recorded in `docs/live-testing.md` and ADR 0312.

## Validation

| Contract | Mode | Evidence |
| --- | --- | --- |
| Packet rules | focused-test | parametrized refusals in `tests/test_live_vnic_creation_policy_arm.py` |
| Record derivation | focused-test | exact `-a` record per action shape, including the two-list record that is not last |
| Oracle and axes | focused-test | lifecycle per shape, reordered readback, side-effect drift, a mismatch per field |
| Cleanup and recovery | focused-test | qualified `CLEAN`; refusal and unknown hold; forged history refused |
| Unchanged cases | focused-test | the existing source, version-1 and fixture tests, unmodified |
| Native outcomes | task-test-not-applicable | hardware results are evidence recorded by the operator; no offline test can observe the HMC |
