# ADR 0294: Create-only hardware memory access flags

## Status

Proposed for #1771; implemented on the feature branch.
ST55 passed on candidate `534789755f64acea6b7831dce1192f5421f8e8f9` for an inactive
AIX/Linux LPAR with both flags true, including access readback and clean recovery.
No guest/runtime, VIOS, false/mixed-field or mixed-role proof is claimed.

## Context

The P10 and P11 `mksyscfg` partition-create references expose optional
`hardware_mem_encryption` and `hardware_mem_expansion` values 0/1
(`docs/refs/hmc-commands-p10/commands/mksyscfg.md:41`, P11 counterpart:49).
These grant hardware access; they do not establish guest encryption, transparent
physical-memory protection, or an active memory expansion factor.
Existing LPAR/VIOS creation owners already perform native creation and policy checks.
The existing security-policy model also serves update operations outside this scope.

## Decision

Extend existing creation inputs with two trailing optional strict booleans.
Omission preserves the old route; explicit false emits 0 and true emits 1.
Use the existing fixed attribute encoder, creation authorization, duplicate checks,
LPAR ownership stamping, and native submission owner. Add no update operation.
Require positive selected-system capability advertisement for supplied fields.
Keep existing security, resource and partition-environment restrictions.
For a supplied field, require observed IsClassicHMCManagement=true,
IsHMCPowerVMManagementController=false and IsPowerVMManagementController=false
from the selected-system resource. This admits a contemporaneously observed native profile-creation configuration,
not a proven mode mapping; other or unavailable tuples refuse before submission.
Do not invent release floors, mutual exclusions, or implicit AME factor changes.

Read the contained inactive resource independently after creation, comparing supplied
values with `PartitionMemoryConfiguration/MemoryEncryptionHardwareAccessEnabled`
and `MemoryExpansionHardwareAccessEnabled`. The captured schema makes both optional
read-only booleans. Report the named observed fields without calling them desired,
pending or current; historical equal native desired/current values cannot settle that
mapping. This proves only the bounded access-field observation. Missing, malformed,
mismatched or unavailable evidence stops later workflow effects and remains explicit.
No automatic retry, rollback, activation, or extra profile application is introduced.

## Consequences

Some native creations can succeed while verification fails; retain the created resource
and require inspection before retry. Existing explicit apply remains separate and waits
for the existing ownership/domain barriers plus requested hardware-field verification.
The named live proof covers one inactive owned AIX/Linux true/true fixture only.
Other combinations remain testable product contracts without claimed native proof.

## Considered & rejected

- Extend `LparSecurityPolicy`. verified: at base `40f9f85672dee57c6dcc03d6446534efab93e43e`,
  `operations/lpar/security_policy.py` uses that model for configuration updates;
  creation-only fields there would advertise an unsupported update contract.
- Add a general hardware-policy object or native proxy. judgment: two fixed optional
  creation attributes do not warrant another policy registry or command surface.
- Treat lexical catalogs as REST schema. verified: retained source XSD body SHA256
  `3043acbf8820d68b21e94044a26ee9195d31243bc26ff7e10a95c70bd5c35b32`
  names `MemoryExpansionHardwareAccessEnabled`, whereas the lexical compression alias
  does not identify that XML leaf. Shape evidence is separate from mutation evidence.
- Leave the fields unavailable. judgment: this does not satisfy the authorized C10
  typed creation, CLI/MCP, verification and reproducible procedure criteria.
