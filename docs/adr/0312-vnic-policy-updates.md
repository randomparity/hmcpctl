# ADR 0312: Qualify existing-vNIC policy modes through the registered owner

## Status

Source-only version 2 implemented and offline verified at `07d8a360`, 2026-11-01.
The maintainer subsequently approved the retained-fixture prerequisite through
design and implementation in issuecomment-6563667424; it was offline verified at
`9d1bbb43`. The subsequently approved fixed-fixture allocation and fresh-only
Description correction is implemented with offline boundary proof and independent
product/security review at `ebb5808e`. Native vNIC update qualification and the
public setter remain unfinished; hardware windows and publication need separate
admission. A separately approved bounded private submission-error extension is
implemented with offline boundary proof and independent product/security review at
`475e544b`. This record does not settle the complete updater design.
On 2026-11-04, #1856 amended the fixture's hypervisor-memory refusal policy
from one fixed value to a bound, as an operator-approved run-exposed correction.
On 2026-11-04, #1857 added the version-2 `finite-update` case for scalar and
finite list-mode updates; its native outcomes are to be recorded under Consequences.

## Context

#1828 retains five adapter-policy fields and one submission per complete update.
Creation evidence establishes neither existing-vNIC sentinel updates nor the
result of removing the last list member. The maintainer selected qualification
first and explicit no-members clear: acknowledged `=none` plus independent literal
`none`, never blank or unavailable readback.

At `4d500831b1a197204cbb2a47cc08526a4bb95fbf`, the registered
`scripts/live_test/vnic_creation_policy.py` owner provides contained ownership,
exact candidate/baseline binding, private intent persistence and physical budgets.
Its version-1 packet and recovery history permit creation followed by removal,
not an intervening update. Its cleanup equality still requires the creation policy.

## Decision

Extend that owner with a separately discriminated version-2 `update-mode-source`
packet and source-only history. Reuse its preflight, named capture and independent
read-only recovery entrypoints. Preserve version-1 schema, behavior and limits.

Each packet selects one of six fixed cases: replace-all, replace-none or remove-last
for either allowed-list field. Permit one fresh single-backing creation, one exact
derived update and one removal conditional on acknowledged expected policy and
bounded preservation. Rejection or uncertainty disarms removal. Cleanup compares
the captured object with precisely its selected cell changed to the fixed oracle.

LPAR preparation stays outside source-version-2 effects. The separately approved
fixture purpose extends this same owner with explicit prepare and cleanup stages,
required-stamp creation and inactive-only deletion. A retained identity handoff
does not run a source case or authorize a later stage. Cleanup requires original
preparation, declared complete source histories and fresh protected-state equality;
an explicit no-source declaration is distinct from missing history. Root owns
declaration completeness within the coordinated writer deployment. Configured
resources come from the unique contained default saved profile; inactive current
placeholders and unknown fields remain observations, not desired-value defaults.
For this fixed 512MiB/0.05-unit fixture only, require six observed allocation
changes: assigned memory +512MiB and current/pending available CPU -0.05 exactly;
hypervisor memory `h`, a non-negative multiple of the baseline logical memory
block of at most 64MiB; and current/pending available memory exactly -(512+`h`)MiB.
Stamping must not move any counter. Validate the baseline counter leaves against
the worst case before create and preserve their attributes and other observations.
These values are an explicit refusal policy, not universal HMC costs. #1856
amended the original fixed hypervisor +48MiB/available -560MiB after the same
tuple on the same system was observed at +48MiB in one window and +32MiB in a
later one; see the
[allocation bound design](../workflow/specs/2026-11-04-vnic-fixture-allocation-bound-design.md).
A missing REST Description is permitted only before stamping a proven fresh
created target with independently blank native text. Explicit empty remains
valid; null, malformed or nonempty values do not become absence. Stamped/owned
checks and failed-history cleanup rules remain strict.

Retain source submission exception type, a 64KiB UTF-8 message prefix with explicit
truncation, and existing native completion/refusal metadata in the private journal.
Absent or opaque native provenance remains unknown; diagnostic text is never an
oracle or cleanup authority. Preserve private-file/journal limits and public
exclusion. This does not recover an already discarded error or admit a retry.

#1857 adds one version-2 case, `finite-update`, which carries an `update` list of
one to five actions, at most one per field. `set` applies to all five fields;
`add` (`+=`) and `remove` (`-=`) apply to the finite lists only. The harness derives
the record and the oracle from the packet. It refuses before any effect:

- a no-op `set`;
- any sentinel value;
- a list action on an `all` or `none` baseline;
- an `add` that repeats a member;
- a `remove` that names a member not present, or every member;
- more than one list value containing a comma.

So `all` minus finite members stays unqualified. The record places any quoted
list last and keeps the shared builder's final-pair restriction (ADR 0311). List
oracles compare canonical members, not raw order. The six earlier cases
are unchanged. The case set tests the port VLAN constraint that #1856 met at
creation: `HSCL12E9 The SR-IOV physical port supports only the Deny all VLAN ID
restriction when a port VLAN ID is specified`. See the
[finite update design](../workflow/specs/2026-11-04-vnic-finite-update-cases-design.md).

No activation, reseed, alternative encoding, automatic retry, rollback or maturity
promotion follows. The public
five-field updater remains #1858 work.
The [source qualification design](../workflow/specs/2026-11-01-vnic-update-source-design.md)
defines the packet, evidence, proposed bounds and implementation admission gates.
The [retained-fixture design](../workflow/specs/2026-11-01-vnic-retained-fixture-design.md)
defines the separate prerequisite and its bounded lifecycle.

## Consequences

A refused or ambiguous experiment can deliberately leave its captured vNIC for
operator disposition. Recovery can report residue but cannot remove it or repair
uncertain history from current absence. A qualified source result establishes only
the exact stored-policy case and environment; it does not establish traffic,
failover, public updater support or completion of #1828.

#1856 ran all six fixed cases once on HMC V10R3 M1060. None qualified.

- Both remove-last cases were refused with codes `HSCLAB1F` and `HSCLAB20`.
- The four `all`/`none` replacements were rejected with the HMC's generic
  invalid-attribute-value text and no code. The harness therefore keeps them as
  `unknown`, without changing its oracle.
- Every independent readback was unchanged.
- The operator recorded those four cases as refusals evidenced by text.

So for this environment, these list sentinels are creation-only, and removing
the last member is refused. Later update modes must be designed around finite
lists. The outcome table is in the
[operator procedure](../live-testing.md#recorded-source-outcomes-1856).

## Considered & rejected

- **Infer update support from creation.** verified: the retained command reference
  `docs/refs/hmc-commands-p11/commands/chhwres.md:241` describes creation sentinel
  values separately from existing-vNIC list updates; it does not establish their
  update semantics. This is source interpretation, not a hardware result.
- **A separate general diagnostic driver.** judgment: duplicating the existing
  destination, ownership, journal and recovery owner adds unnecessary surface.
- **Borrow an existing inactive partition.** judgment: advisory ownership without
  this invocation's caller token cannot establish the required fresh identity.
- **Reuse a whole configuration/profile test arm as preparation.** verified:
  `scripts/live_test/lpar_config.py:1410,1529` and
  `scripts/live_test/profile_contract_run.py:455` at `07d8a360` perform their own
  experiments and teardown, without a successful retained-fixture handoff.
- **Reset between cases or clean up after refusal.** judgment: either requires an
  unqualified mutation or authority beyond the approved qualified-cleanup boundary.
- **Do nothing or implement finite modes first.** judgment: neither satisfies the
  maintainer's chosen qualification-first sequence; later modes cannot disappear.

- **Keep type-only diagnostics or print raw errors.** verified: at `6af8d409`,
  `_capture_source` stores only the caught exception class, while `main` emits only
  stage/status or exception type. The former loses the failure detail; widening
  public output would violate the approved private evidence boundary.
- **One fixed case name per finite shape (#1857).** judgment: about eleven
  near-identical case names would each need operand keys anyway. A validated action
  list with a derived oracle covers the same shapes with one parser.
- **Exact raw list literals as the finite oracle (#1857).** verified: #1856's
  recorded runs (`docs/live-testing.md`, "Recorded source outcomes") used only
  singleton finite baselines. No evidence fixes the order or case of a list
  with several members, so a raw-order oracle would be a guess.
- **Remove a refused case's vNIC inside the harness (#1857).** judgment: this
  record already rejects cleanup after a refusal. Per-case removal through the
  ownership-checked CLI stays the operator's separately granted action.
