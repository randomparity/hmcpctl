# Shared native-record grammar ownership

## Decision

Move `build_attribute_record`, `build_filter`, `_validated_value`, `_ATTRIBUTE_NAME`
and their delimiter table from `ssh/commands.py` to `documents/native_records.py`.
The existing description validator imports the table as `RECORD_DELIMITERS`.
Both encoders share their existing validator and table; there is no second
implementation, compatibility export or reverse dependency on SSH. The new owner
imports only the standard library and existing root errors. Response parsing,
`HMC_NO_RESULTS` and refusal-code handling remain in `ssh/commands.py`.

All 41 existing direct consumers in product code, tests and scripts import the
actual owner. This includes the lazy filter import in system-profile operations
and all seventeen live-test script consumers. Command literals and script entry
points remain unchanged. The documents package describes native request records
as well as its existing XML builders, without adding initializer imports.

## Protected contracts

Relocate the complete declarations and constants without rewriting their bodies;
only the shared table's binding changes. Preserve order, empty and duplicate
attribute refusal, malformed names, secret-name mismatch refusal, credential
redaction, delimiter and control rules, final quoted-pair restriction and the
existing explicit vNIC nontrailing opt-in. Whole-record shell quoting remains
separate. Existing grammar and builder-site guards retain their assertions.

Both finite `SOURCE_PATHS` sets, in the AME and boot source arms, retain
`ssh/commands.py` for response parsing and add `documents/native_records.py`
because their already selected encoder bodies move there. Existing source-bound
captures become stale. Their formats, versions, observations and support claims
remain unchanged; no historical capture is refreshed by this refactor.

## Failure model

Required boundaries are document validation without SSH imports, complete direct
caller migration, one grammar owner, retained static guards and redaction, exact
quote interpretation, and source witnesses covering the moved bodies. Old private
imports and old source-bound captures becoming stale are accepted. ADRs 0061 and
0311 retain their existing grammar and live-evidence limits.

## Verification

A fresh subprocess blocks SSH imports, imports `LparCreationMemory`, and exercises
real valid and delimiter-refused cases. Its baseline failure is the old document
import. Existing grammar, redaction, command and model tests protect behavior;
normalized declaration and complete consumer AST comparisons protect relocation.
No new behavioral test duplicates the unchanged encoder algorithm.

Both source arms use actual codec bytes to prove that a codec mutation changes
the selected fingerprint and rejects a stale capture. Removing the codec from
each selected set is a controlled fault that must fail these tests. Parser source
selection remains covered. All 207 actual registered MCP models must remain equal
and their application import closures must follow the actual owner.

Run affected document, SSH, native-profile, vNIC, VIOS, record-producing operation,
source-arm, producer and recovery tests, applicable documentation and scenario
guards, lint, format, type and smoke checks, and all fourteen normal hooks. The
assembled branch also runs full verification and review. No live hardware action
is part of this ownership change.
