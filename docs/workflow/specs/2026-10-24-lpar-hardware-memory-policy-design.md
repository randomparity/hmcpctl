# C10 hardware memory access during partition creation

Design proposal for #1771, linked to [ADR 0283](../../adr/0283-lpar-hardware-memory-policy.md).
Base: `40f9f85672dee57c6dcc03d6446534efab93e43e`. Full-spec lane; frozen M denominator 250.
Authority: [WORK:SCOPE](https://github.com/randomparity/hmcpctl/issues/1771#issuecomment-6387628908),
token `q1771-6351885f`; interactive campaign dispatch. Exclusion/owner pairs `[]`.
This proposal is neither implementation approval nor a hardware execution grant.

## Problem and scope

Expose exactly `hardware_mem_encryption` and `hardware_mem_expansion` from the four
P10/P11 `mksyscfg` partition-create occurrences through existing typed LPAR and VIOS
creation operations and their CLI/MCP adapters. Preserve the issue's full Expected
criteria, existing callers and existing domain owners. No update, profile CRUD,
quantity/move/pool operation, arbitrary proxy, stable facade export or owner refactor.

## Source admission

The local `docs/refs/hmc-commands-p10/commands/mksyscfg.md:41` and
`docs/refs/hmc-commands-p11/commands/mksyscfg.md:49` specify optional
0/1 fields in the partition section. Enclosing sections do not establish a special OS,
release floor or mutual exclusion for these flags. The native owner supplies a profile;
the documented controller-mode profile restriction still bounds that owner.
Existing system capture `tests/fixtures/live/rest-ms-entry-mtms-pair.json` advertises
`AssociatedSystemCapabilities/HardwareMemoryEncryptionCapable` and
`HardwareMemoryCompressionCapable`; a separate retained system response confirms both.
Do not infer platform eligibility solely from the P10/P11 corpus directory name.

Retained source capture produced by candidate `1373d713eb1e563a3ac0e7f8a6686430cc9925c9`
used HMC 10.3.1060, Minor 3, build 2408210051: two SSH reads and six REST transports,
confirmed logout and normal reaped exit. Its logical-partition memory schema body SHA256
is `3043acbf8820d68b21e94044a26ee9195d31243bc26ff7e10a95c70bd5c35b32`.
Both named access leaves are optional `xsd:boolean`, `ROR.AttrGroup`.
Historical native rows had desired/current hardware flags 1/1 for both features while
current/pending AME factors were 0.0. This establishes independent concepts, not a
create transition, current target eligibility, or the REST-to-desired/current mapping.
Retain only anonymous structural fixtures and provenance; raw captures stay private.

## Architecture and contract

Add trailing `hardware_mem_encryption: bool | None = None` and
`hardware_mem_expansion: bool | None = None` to `LparCreation`, `create_vios` and the
native creation function. MCP uses strict boolean validation with bounded private
diagnostics; direct operation/native calls reject non-bool non-None values before I/O.
CLI adds paired `--hardware-mem-encryption/--no-hardware-mem-encryption` and
`--hardware-mem-expansion/--no-hardware-mem-expansion` options under existing confirmation.
No new acknowledgment switch or capability bypass is introduced.

A focused fixed-field helper supplies validation and native pairs, shared by these
boundaries. A focused operation helper performs selected-system capability admission
and contained post-create reading; reuse existing identity resolution and XML leaf
parsing. No public policy model, generic registry, parser or facade export is added.
Only supplied fields require their positive capabilities, including explicit false.
Absent, false or malformed capability refuses before creation; native final rejection
remains authoritative for constraints that advertisements do not describe.
Explicit keylock plus either flag refuses before effects because the native owner cannot
represent keylock. Existing vTPM/security/reporting/I/O/resource checks remain in force.
Two supplied flags may coexist; no source supports a new mutual exclusion.

Omission adds no attribute, capability read, hardware readback or workflow step.
Requested fields force the existing native creation path, encoded as fixed keys with
0/1 through `build_attribute_record`; there is one submission and no REST retry.
LPAR creation retains scoped identity and ownership stamping. Hardware requests join
the existing policy barrier before profile application or subsequent assignments.
VIOS creation retains its existing system grant, duplicate/capacity/identity owner and
return type; this change does not invent a VIOS ownership-stamping subsystem.

An independent contained read verifies immutable UUID, matching name/environment and
`not activated` state before extracting the two fixed memory access leaves.
The result records supplied values separately from observed REST leaf names.
Canonical `true`/`false` and schema-valid `1`/`0` parse as booleans; absent is unknown.
Only requested leaves participate in comparison; malformed unrequested leaves do not
invalidate a request. Repeated/structured non-leaf values refuse as unavailable.
LPAR adds a `hardware_memory` workflow step: `ok` only for exact requested matches;
`error` carries bounded status `mismatch` or `readback-unavailable` and safe observations.
VIOS returns its existing entry on verified fields, otherwise bounded `HMCError` with
the same distinction and an explicit created-or-may-exist inspection instruction.
Neither surface labels a REST flag desired/current, asserts guest enforcement, or
claims that enabling access changes an AME factor or transparent physical encryption.

## Success and validation

S1: None/False/True and their nine combinations retain strict, omission-preserving
behavior across direct operation, native encoder, CLI and actual FastMCP boundaries.
S2: Admission refuses bad types, missing capabilities, incompatible existing policy
or keylock before submission; AIX/Linux, IBM i and VIOS use existing environment paths.
S3: Cross-system UUIDs, wrong names/types/states, malformed/missing leaves, read errors
and mismatches cannot produce verified hardware status or later workflow effects.
S4: Native rejection/transport uncertainty never causes a second create, rollback or
raw private error reflection; existing ownership, authorization and audit remain active.
S5: Old callers and omitted arguments retain existing routes, results and defaults.
S6: Reference-backed negative/transition tests, generated docs, maturity metadata and
the installed ST55 procedure are delivered without claiming unevidenced native success.
Focused tests exercise actual validation/encoding/readback and mock transport boundaries,
not the comparison logic. Controlled faults must demonstrate that each new security
barrier test fails before restoration. The plan inventories exact cases and commands.

## Failure model

1. Actors and deployments
   - Direct Python callers, CLI operators and authorized MCP clients on supported HMC
     connections; mocked CI and an explicitly authorized root-controlled live window.
2. Invariants and assets at stake
   - Correct system/partition identity, create authorization, ownership, availability,
     private diagnostics, omission semantics, single submission and truthful evidence.
3. Accepted failure classes
   - Native refusal despite positive advertisements: bounded error, no retry; the HMC
     remains the final compatibility authority where no local rule is documented.
   - Created resource retained after uncertain/mismatched/unavailable verification:
     explicit partial result and blocked follow-ups, avoiding speculative deletion.
   - REST desired/current mapping and guest-runtime effects remain unasserted because
     source evidence establishes named access observations only, not those semantics.
4. Covered elsewhere
   - Existing creation, ownership, target grants and audit owners; transport timeouts;
     existing resource/security/vTPM/I/O validation; existing named recovery framework.

## Threat model

1. Boundary inventory: widened CLI/MCP/direct creation input and fixed SSH record;
   selected-system advertisements and created-resource REST bodies enter from HMC.
   No additional authority, credential channel or general native-command boundary.
2. Actors: authenticated MCP callers and local operators can supply malformed data;
   stale/conflicting HMC responses are untrusted evidence. Existing HMC authentication,
   grant policy and the operator's hardware target selection remain trust anchors.
3. Controls: strict private scalar validation; existing system grant/confirmation;
   selected-system capabilities; fixed field encoder; scoped immutable identity;
   inactive state and requested-only comparison; ownership and follow-up barrier;
   bounded diagnostics, no retries, private live artifacts and filtered evidence export.
4. Explicitly out of scope: compromised authorized HMC, guest enforcement and hardware
   cryptographic implementation, held by platform/guest owners; this design adds no
   protection claim for them and retains the full authorized access-setting contract.

## AI surface evaluation

AI-SPEC: an authorized tool-using client triggers existing create tools with strict
optional flags; outputs are existing creation results plus bounded access verification.
Allowed sources are selected-system/resource reads; unsafe retries, fabricated runtime
claims and cross-system substitutions are forbidden. Failure returns a bounded partial
outcome. Cost is one create and bounded existing readbacks, with no polling loop or LLM
call. Success means exact requested observable fields with existing barriers preserved.

| ID | Severity; input/setup | Required/forbidden observation | Gate |
| --- | --- | --- | --- |
| E1 | 4; true/false with capable contained inactive fixture | exact encoding/readback; no inferred runtime field | block |
| E2 | 5; string, integer, object or oversized private marker | reject privately before connection; no coercion | block |
| E3 | 5; creation request under unrelated target/tool grant | deny before transport; no widened metadata | block |
| E4 | 4; stale/missing or contradictory capability/readback | refusal or partial; no verified result/follow-ups | block |
| E5 | 5; native timeout containing private marker | one create; bounded unknown outcome; no retry/rollback | block |
| E6 | 4; both omitted and old positional callers | original route and result; no extra hardware reads | block |
| E7 | 5; captured compression alias substituted for real XML leaf | unavailable; no invented expansion observation | block |

Measurement uses executable schema, response, transport-count and call-order assertions.
No LLM judge is needed. E7 guards the observed source-discovery ambiguity.

## Reproducible live procedure and evidence

Root reserves subtask 55, group `lpar-hardware-memory`, wrapper
`scripts/live_lpar_hardware_memory.py`, scenario `st55-lpar-hardware-memory-creation`.
Preserve existing/default/all dispatch selections; 53 and 54 remain separately reserved.
Before execution root supplies an exact candidate, deployed-build match, target and
bounded window/transport budget. This document grants none of those permissions.
The arm reuses `lpar_config` baseline, checkpoint, ownership and inactive cleanup owners.
It creates one inactive owned AIX/Linux fixture with true/true and no profile apply,
independently reads both named REST leaves, verifies ownership, deletes only the matching
inactive fixture and compares baselines. No activation, guest deployment, keys or VIOS
mutation. An incomplete result preserves residue for the named recovery procedure.

```sh
uv run --no-sync python scripts/live_test_preflight.py --group lpar-hardware-memory
uv run --no-sync python scripts/live_lpar_hardware_memory.py
uv run --no-sync python scripts/live_test_recovery.py --results test-results-lpar-hardware-memory.json
```

Recovery runs after the arm including failure/apparent success; the wrapper accepts no
`--results-file`. Preserve private captures, emit public matrices only through the
existing evidence exporter, and tie each claim to the exact candidate. False values,
mixed combinations, IBM i/VIOS and other hardware generations remain explicitly unrun
until separately authorized windows. No exclusions arise from that evidence boundary.

## Global constraints

Preserve old callers, source-required policies, selected-system containment, ownership,
authorization/audit, explicit omission and bounded truthful outcomes. No new dependency,
toolchain floor, facade export, update contract, arbitrary proxy, automatic retry,
rollback, activation or implicit profile apply. No private identifier in public artifacts.
Implement only after root release; plans stay ignored; root holds review, merge and live
orchestration. New native claims require matching deployed-candidate evidence.
