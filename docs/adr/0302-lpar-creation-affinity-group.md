# ADR 0302: Conventional affinity group at partition creation

## Status

Private proposed amendment only; not adopted, implemented or independently reviewed.
Prior D3/A3/Party1/P4/S3 cover their historical bytes, not this changed criterion.
User call_6f289dcf9ef4484b99a26406c941d29c/0 approved “Approve saved-configuration
criterion”: prepare this design update only. ADR0302 reservation remains unchanged.

## Context

C08 owns two partition-create occurrences, P10 mksyscfg:41 and P11 mksyscfg:49.
At candidate433c7ea6, ST57 requested no apply: positive1 returned profile1 and
partition none; all-plane equality truthfully stopped follow-ups. The failed
source-only experiment did not establish the original effective-assignment outcome.

## Decision

Success means independently verified saved affinity configuration. Classic creation
requires fresh contained identity and exact default-profile identity/value equality.
Keep native-partition observations separate, including mismatch/unavailability;
current, pending, runtime and physical-placement semantics remain unestablished.
A valid different native value alone no longer defeats verified saved configuration.
Controller creation remains profileless; without a supported saved-proof source it
returns incomplete, even when its native partition value matches. Keep all contexts.
Retain strict scalar inputs, single native record, ownership/identity uncertainty,
C05/C06/C07/C10/C11 checks and existing optional-apply owner. Unknown saved proof
or failed/missing requested-apply acknowledgement blocks later effects.
No setter, profile CRUD, retry, rollback, extra apply or activation is introduced.

## Consequences

LPAR exposes scoped saved status and separate native observations. VIOS retains
entry-or-exception, proposing bounded affinity_verification metadata on success.
Existing failed/none-only source evidence stays failed/source-only; no maturity credit.
ST57 remains reserved and inactive, with no execution authority from this amendment.

## Considered & rejected

- **Keep effective/all-plane success.** verified: candidate433c7ea6 affinity_group.py:115–132 and the retained readback investigation show that equality neither proves effective assignment nor admits the captured profile1/partition-none pair.
- **Infer current/runtime from profile or XML zero.** verified: P10/P11 lssyscfg:34–35 supply grammar, not that equivalence; retained field catalogs establish vocabulary only.
- **Apply or activate to obtain equality.** judgment: outside the approved saved criterion; the source-only apply proposal is unadopted and no experiment is authorized.
- **Drop controller/VIOS or reuse ResourceGroup.** verified: frozen C08 has empty exclusions and separate owners; source unavailability requires honest incompleteness.
