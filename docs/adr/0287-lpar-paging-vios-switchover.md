# ADR 0287: LPAR paging-VIOS switchover

## Status

Proposed for #1776 under the existing campaign reservation0287; two design passes dispositioned, awaiting A1.
C07 and #1777 source prerequisites are landed at base 7a74c4513cd13ab50b33fd16432faea0e8003d01;
normal gates remain required. This record grants no implementation or hardware execution.

## Context

Native mem so toggles the current provider; repetition can reverse a completed request.
Configured primary/secondary and current paging identity are different observations.
Two pool providers and a saved designation alone do not establish current redundancy.

## Decision

Propose one typed operation with expected-current provider and coordinated writer acknowledgment.
Require positive configured P/S with provenance, independent current redundant pairR and
{P,S}=R; verify effective current provider separately. Do not claim runtime role orientation.
Use fixed native writer, bounded contained observations and three-resource authorization.
Require finite-positive config.ssh_timeout before paging SSH or ownership-description reads;
all three new SSH entries independently admit that value, reusing existing transport/defaults.
HMC decides only approved undocumented release/RMC/service eligibility; required missing
identity/configuration/redundancy/current proof refuses or produces unverified postconditions.
Retain submission, observation and preservation separately; no replay or automatic reverse.

### Ownership rule for this operation

`switch_lpar_paging_vios` is Reconfiguring under ADR 0092 §2. It always reaches
existing ownership authorization for the contained LPAR and both paging providers;
foreign or malformed ownership refuses before native submission. There is no
`ownership_override` parameter or equivalent bypass on Python, CLI or MCP paths.
This deliberately narrows ADR 0092 §5 for this operation only; its classification
row and Status amendment record the exception. All unrelated operations retain §5.
The expected-provider and writer-window acknowledgements prevent stale intent;
they never replace ownership checks or MCP all-targets authorization.

## Consequences

Discovered second provider keeps MCP all-targets policy. Optional evidence may prevent an
attempt or completion proof; it cannot become false/zero/success. Fresh checks do not create
an atomic transaction or prevent automatic failover. Independent recovery may remain held.

## Considered & rejected

- Quantity API extension: verified:55d1e02f memory.py requires quantity/CPU/deferred facts;
  chhwres P10/P11:108–110 publishes separate so with no quantity/destination/wait/force.
- Configured pair or pool-count proof alone: verified:REDP4470 §4.4.10 distinguishes current
  provider after failover; Example4-4 supplies actual assigned redundant-device relationships.
- Active-only state gate: verified:retained Enumerations.xsd:6980–7052 has TODO descriptions,
  not usability semantics; operator A2 delegates undocumented service eligibility to HMC.
- Generic transaction/paging config framework: judgment: unnecessary surface for one writer.
- Do nothing: judgment: leaves the explicitly allocated switchover behavior absent.
