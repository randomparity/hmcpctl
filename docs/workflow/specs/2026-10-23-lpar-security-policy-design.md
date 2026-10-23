# Partition secure-boot and keystore policy — #1765

## Authority and success

Frozen scope: #1765, token `q1765-9d5051e9`, C04, five fields/eighteen bindings,
explicit empty exclusions. [ADR 0277](../../adr/0277-lpar-secure-boot-keystore.md)
records the existing-owner extension. Success is typed creation/update coverage
with admission, honest readback and supported CLI/MCP presentations for this set.
The operator chose explicit settings plus existing confirmation/`--yes`, and
selection of the Linux field as Linux intent; neither introduces another flag.
Profile lifecycle remains with #1724. No owner migration or compatibility facade
change is needed; the existing six-name stable facade remains unchanged.

## Sources and admission

Local `docs/refs/hmc-commands-{p10,p11}/commands/mksyscfg.md` partition sections
(P10:41, P11:49) contain four fields; `chsyscfg.md:49` contains five in both
corpora. Tests retain the eighteen exact field/context bindings. Corpus generation
is not hardware support. Source values: `secure_boot` strict integer0–9 (0off,
1log,2enforce;3–9 OS-specific, without invented meanings), `keystore_kbytes`
strict nonnegative integer KB, and three strict booleans encoded0/1. None omits;
False and zero survive. `keystore_signed_updates_without_verification` is update
only, including explicitFalse being invalid at creation. Unknown keys and scalar
coercion are rejected without reflecting supplied values in validation errors.

[IBM advanced settings](https://www.ibm.com/docs/en/power11/9242-21T?topic=capabilities-changing-advanced-partition-settings)
and the equivalent Power10 page establish inactive store enable/increase,
nonzero store dependencies, irreversible HMC signed-update/Linux enablement,
and the meaning of bypass: PK provisioning without authenticating against the old
PK. Bypass requires effective signed-update infrastructure. Linux enablement also
requires it; the Linux field includingFalse is accepted only for AIX/Linux,
rejected for VIOS/OS400, and proves no guest OS. Store zero is subject to HMC
emptiness checks; this tool reads no key objects and claims no emptiness proof.
Power11 permits nonzero store shrink (`0 < requested < before`); older systems
reject that branch. Zero-disable is separate and does not require Power11; HMC
rejects it if the store is not empty, without this tool reading key objects.
Source-backed minimum PKS release is HMC9.2.950/FW950.
[IBM i enhancement details](https://www.ibm.com/support/pages/ibm-i-functional-enhancements-details)
establish IBM i7.6/Power11 secure boot with HMC11.1.1110/FW1110, including enforce.
Require POWER11 in the existing selected-system processor-mode advertisement and
those console/firmware floors for OS400 secure-boot settings and nonzero store shrink;
unknown/malformed release metadata refuses those branches. Warn that installed
IBM i7.6 and OS-specific3–9 semantics are operator prerequisites, not verified facts.
No claim of guest boot, key-object, migration or runtime enforcement follows.

Read selected ManagedSystem `Resource.AssociatedSystemCapabilities` for positive
`PartitionSecureBootCapable`, `PartitionKeyStoreCapable`, and
`PartitionDynamicKeySecureBootCapable` for their respective requested fields.
Read direct ManagedSystem.Resource children `MinimumKeyStoreSize`/`MaximumkeyStoreSize`
for nonzero stores; only the three capability flags belong to the associated object;
missing, malformed, nonpositive or inverted bounds refuse store requests. Dynamic
fields require positive dynamic capability even when explicitlyFalse; no implicit
unsupported-as-false substitution. The full capability reader may collect
console-version and selected-system SystemFirmware evidence regardless of field selection; only the existing processor-mode
query is conditional on `need_power11`. Apply release guards only to PKS, OS400
secure boot and nonzero store shrink as specified above; unknown required metadata refuses.

Update admission merges supplied values with the strict before snapshot. A
requested signed/Linux True requires effective nonzero store and effective signed
infrastructure for Linux; missing dependency values require explicit configuration
rather than guessed defaults. BypassTrue requires effective signed infrastructure
and nonzero store. Refuse observedTrue→requestedFalse for either irreversible flag.
An explicitFalse with unknown old irreversible value refuses before submission.
Store enable/increase and signed/Linux enable transitions require observed
`not activated`; other changes require a recognized stable REST state (`running` or
`not activated`), using the existing `operations/partition_state.py` vocabulary, without silently
powering off. Feed captured parsed states through admission tests. Creation is inactive and requires
explicit nonzero store/dependency settings when enabling dynamic features. Creation
has no before-state; omission leaves HMC defaults, subject to readback.

## Interfaces, transport and readback

Add immutable `LparSecurityPolicy`, `SecurityPolicyState`, `SecurityPolicyResult`
and selected-system capability evidence in dedicated model/operation modules.
Reuse `leaf_text` and real client XML parsing; do not add a parallel XML parser.
REST field mapping is fixed: PendingSecureBoot→secure_boot, CurrentSecureBoot
separately, KeyStoreSize→keystore_kbytes, KeyStoreSignedUpdatesEnabled,
LinuxDynamicKeySecureBootEnabled, KeyStoreSignedUpdatesWithoutVerificationAllowed.
Leaf absence yieldsNone; malformed present values raise a bounded read error.
Require one contained target of the correct LPAR/VIOS kind, name/environment/state
and immutable identity before mutation. Reuse existing LPAR and VIOS ownership
helpers and the current power/target authorization and audit decorators.

Extend `ssh/lpar.py` creation with `security_policy`; add one fixed `chsyscfg`
writer using `build_attribute_record` and final shell quoting. No caller attribute
names or arbitrary command text cross this boundary. `read_lpar_security_policy`
and `configure_lpar_security_policy` share implementations with explicit internal
`vios` selection; public LPAR/VIOS presenters retain distinct grant metadata.
`configure` takes settings plus existing system/target/ownership selectors only.
Do not reuse disproved native read aliases. Read REST once before and once after
one submission; refresh containment/identity with the before snapshot. No retries.

Results distinguish `verified`, `mismatch`, `readback-unavailable` and
`write-outcome-unknown`, carrying requested/observed policy and changed field names.
Verified requires requested pending policy and preserved observable omitted fields
to match, including observed current secure boot remaining unchanged after a
configuration-only update. Unknown preserved fields remain disclosed unknown;
a requested field that is absent cannot verify. Different pending/current values
produce a restart-required warning, not a false mismatch or runtime enforcement
claim. Uncertain submission receives no automatic replay, rollback or follow-up.
Raw command error/output, XML, key material and connection identifiers are excluded
from policy results/warnings; retain compiled field names and bounded error classes.
Creation has no preservation baseline and verifies supplied values only.

LPAR/VIOS creation composes policy with requested vTPM/C01/C02/I/O fields in one
existing native record. Preserve each current route when policy is empty/omitted,
including existing native LPAR and conditional VIOS vTPM creation. Keep resource
completion/capacity, duplicate, containment and ownership-stamp guards. Requested
LPAR explicit apply waits for identity/stamp and each requested domain's successful
verification; absent/failed policy step stops assignment/apply follow-ups. Existing
unrequested behavior is unchanged. VIOS retains its own creation owner and returns
an inspect-before-retry incomplete-creation error if policy cannot be verified.

CLI create/update accepts one inline `--security-policy` JSON object (64KiB cap),
using strict model parsing; it accepts no filename and does not echo invalid input.
Read/update commands are `get-security-policy`/`configure-security-policy` on
both existing target groups. Existing confirmation/`--yes` precedes mutation with
warnings for weakening, irreversibility, store shrink/disable and pending boot.
MCP adds the same strict typed optional creation field and target-specific read/
update tools. Inline field descriptions carry the consequences and no-OS-proof
limit. No extra consent or Linux-intent metadata is added.

## Global constraints

- Exactly five fields/eighteen bindings; bypass is update-only; omitted/empty preserves routes.
- Existing containment, separate target grants, ownership, audit and fixed encoders remain authoritative.
- No implicit power/profile/key action, arbitrary proxy, secret output, replay or rollback.
- Missing evidence is unknown; configuration proof is separate from runtime/guest/key proof.
- Explicit values plus existing confirmation are consent; Linux field selection declares intent only.

## Failure model

- Actors and deployments: authenticated local CLI operators and MCP callers using
  configured HMC accounts against positively admitted AIX/Linux, VIOS and supported
  OS400 partition contexts; mocked CI covers unavailable release variants.
- Invariants and assets: boot trust, persisted keystore policy, correct contained
  target, ownership, explicit effects, absence of secret material, truthful results.
- Accepted failure classes: concurrent external writers/state changes after reads
  lack an atomic HMC compare-and-swap; HMC performs final empty-store/fit checks.
  Guest OS/version, boot enforcement, key survival and migration are not established
  by configuration; operator prerequisites and bounded warnings disclose this.
- Covered elsewhere: profile lifecycle #1724; platform authentication/audit existing
  connection/tool owners; resource admission current LPAR/VIOS owners; arbitrary
  post-run resource recovery live preflight/recovery guards.

## Threat model

- Boundary inventory: widen existing CLI/MCP typed input→operation, operation→HMC
  native write, HMC XML→readback and result→operator boundaries; no new key endpoint.
- Actor model: malformed/untrusted authenticated tool inputs and HMC responses;
  trust configured HMC to enforce platform permissions and final store admission.
- Controls: strict finite keys/scalars and64KiB CLI bound; containment and target
  grants plus ownership; existing fixed CSV encoder/quoting; typed allowlisted
  XML scalars and sanitized results; one write with explicit uncertainty. Test each.
- Outside threats: compromised HMC/credentials and guest key lifecycle remain with
  platform/guest owners; an external writer racing reads is the accepted class above.

## MCP evaluation and verification

AI-SPEC: an authenticated tool caller supplies the strict policy/settings and
existing selectors; tools return typed configuration evidence from selected-system
REST and fixed native writes, with no guest/key claims. Unsupported/ambiguous input
refuses; uncertain writes return inspect-before-retry. No model calls/token cost;
one write and two partition policy snapshots bound update effects.

| Case / severity | Setup/input | Observable gate |
|---|---|---|
| happy /4 | admitted contained target, pending request/current unchanged | verified configuration, truthful pending warning; block otherwise |
| ambiguous /5 | absent dynamic dependency or requested readback | refuse or unavailable, no inventedFalse; block |
| unsafe /5 | irreversibleTrue→False or creation bypassFalse | no submission; block |
| stale /4 | captured malformed/absent capability or wrong association | no submission; block |
| permission/privacy /5 | foreign/wrong-kind target, sensitive invalid input | no write or sensitive reflection; block |
| bounds /5 | timeout/exception at submission, oversized JSON | one write maximum/no replay, input refusal; block |
| regression /4 | captured pending/current, absent booleans, invalid CLI read alias | real parser retains shape, REST read only; block |

Deterministic tests measure these tool schema/authorization/evidence contracts;
they make no claim about an LLM choosing tools. Reference-binding, resource-boundary,
creation composition/barrier and preservation tests supplement this matrix. Deliberate
fault injection must make high-risk negative tests fail before restoring the guard.
Register root-reserved arm52 `lpar-security-policy`: an inactive owned disposable
AIX/Linux fixture, bounded secure_boot change/readback without key infrastructure,
independent before/after baseline and guarded cleanup/recovery. Dynamic/capFalse,
OS400/Power11, bypass, irreversible enables and keystore variants stay unrun unless
a separately approved packet selects them. Live execution requires a new root grant,
exact deployed HEAD and the documented named preflight/scenario/recovery chain.
Generated docs, maturity mappings, changelog and live procedure accompany code;
no maturity promotion follows source-only reads. Final `just verify`, pinned hooks
and all CI Python legs precede root's merge decision.
