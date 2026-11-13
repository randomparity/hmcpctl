# ADR 0377: Forward SRR-plus-keystore creation to the HMC's system-key rule

## Status

Accepted (2026-11-13), under the operator-confirmed campaign dispatch for #1829.
Amends [ADR 0277](0277-lpar-secure-boot-keystore.md): its pre-effect refusal of
creation combining simplified remote restart (SRR) with a nonzero keystore is
replaced. ADR 0277's other decisions are unchanged.

## Context

ADR 0277 refused SRR with `keystore_kbytes>0` before effects until hmcpctl could
positively classify a user-defined trusted system key. No published source
enumerates the key statuses. A read-only capture on 2026-11-13 (HMC V10R3 M1060,
system `sys-R1`) showed `lstskey` reporting `key_status=default_key`, a REST
`VirtualTrustedPlatformModuleKeyStatus` integer whose mapping one observation
cannot establish, and `mksyscfg` refusing the combined record with exit 1 and
`HSCL0DCE The simplified remote restart and keystore capabilities for a
partition cannot both be enabled at the same time when the trusted system key
status is not User Key.` No partition existed afterwards. SRR alone and the
keystore alone were each accepted. The user-key path was not captured, and
changing the system key to capture it is excluded.

## Decision

Remove the pre-effect refusal and submit the combination in the existing single
`mksyscfg` record. The HMC is the classifier: hmcpctl reads no key status.
`create_lpar_via_cli` keeps the remote `HSCLxxxx` refusal code on the error it
re-raises. When a creation requesting the combination fails with `HSCL0DCE`,
the creation owner runs one scoped lookup by name: no partition yields a
contained `HMCCLIError` stating the HMC's rule and that nothing was created; a
found partition yields the existing partial outcome; a failed lookup yields an
unknown outcome. No replay, stamp, apply or rollback follows any of them. An
accepted combination continues through the unchanged readback and verification.
The accepted path has no live evidence; under ADR 0212 it stays unevidenced.

## Consequences

A request on a default-key system now reaches the HMC and costs one refused
submission plus one read, instead of a local refusal. The refusal names the HMC's
rule rather than a hmcpctl prerequisite. Other codes, and the combination on
creation-memory, memory-layout, serial or start routes, keep their existing
reconciliation. On an HMC release that does not enforce the rule, hmcpctl creates
and verifies whatever that HMC accepts; it no longer second-guesses the HMC's own
admission. Update paths are unchanged.

## Considered & rejected

- **Read the key status before submission and admit only a user key.** verified:
  the 2026-11-13 capture (HMC V10R3 M1060) observed only `default_key`; the
  user-key literal and the REST integer mapping are unobserved, so the predicate
  would be a guess, which #1829 excludes.
- **Refuse only an observed `default_key`, admit everything else.** judgment:
  treats unknown statuses as positive, which #1829 forbids.
- **Keep the blanket refusal.** verified: the observed HMC (V10R3 M1060, the
  2026-11-13 capture) enforced the rule itself with `HSCL0DCE` and created
  nothing; judgment: refusing locally blocks every user-key system to guard a
  rule the HMC already applies.
- **Trust `HSCL0DCE` without a lookup.** judgment: one observation of no effect
  does not prove every release creates nothing on that code; the scoped lookup
  is the same bound the memory-layout route already uses.
