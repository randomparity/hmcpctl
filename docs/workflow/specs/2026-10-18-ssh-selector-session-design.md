# Reuse the REST session during SSH selector resolution

Scope: user approval “Approve all five scopes”, item4, recorded in the private
session ledger. Interactive cleanup; owner root. Complexity M, fixed denominator
250; full-spec because this changes pre-release internal signatures. Exclusions:
no new dependencies, authorization, persistence, external writes or live hardware.
Existing configuration-only entry points and transport fallback are protected.

## Outcome and architecture
Selectors accept their existing configuration argument as HMCConfig | HMCClient.
A client argument supplies its own config for SSH, binding REST and SSH destination
without independently supplied configurations. Keep the parameter name config so
existing keyword callers remain valid. Preserve all four None-sensitive overloads.
Borrowed clients are neither entered nor closed. Configuration-only calls that need
UUID resolution open one client context for the complete system-then-LPAR lookup;
name-only calls create none. Single-selector entry points share this execution path. The standalone LPAR
resolver keeps its system_name argument as a literal CLI fallback scope, even
when that scope is UUID-shaped; only the pair API resolves the system selector.

A narrow internal resolver performs REST lookups on an explicit client. Catch
HMCTransportError separately for each lookup and use the existing CLI resolver;
other errors propagate. The resolved system name feeds partition SSH fallback.
Acquisition failures must also retain the existing transport-only fallback for both
selectors, with no second authentication attempt. Avoid treating a lookup error as
an acquisition error and repeating a lookup or fallback. Cancellation propagates,
and owned contexts close on every exit. No new context-manager framework.

## Consumers and transition
Pass caller-owned HMCClient from affinity/ssh.py, virtualization/vnic.py,
lpar/{assignments,inspect,provision}.py and dedicated PCIe operation paths.
The PCIe _system_name helper should accept the client so it stops discarding it.
Retain configuration-only _app.py and vios/labels.py callers: neither owns a client.
Do not change similarly named resource_identity functions. Remove the superseded
per-selector context-creation implementation; no compatibility aliases.

## Failure model and validation
No fallback for HMCError, invalid/missing resources, or cancellation. Preserve SSH
errors and REST lookup ordering. Test actual selector execution with REST methods,
client acquisition and SSH transport at mocked external boundaries. Required cases:
borrowed two-UUID calls create/enter/close no client; owned two-UUID calls acquire
and close exactly one; names/None acquire none; one UUID resolves correctly;
first/second lookup transport failures and acquisition failure preserve fallback;
non-transport errors and cancellation propagate while owned contexts close.
Exercise at least one real operation path to prove caller reuse beyond helper tests.
New ownership expectations must fail against the previous implementation or a
controlled extra-context fault. Existing selector/affinity/vNIC/PCIe/provision/
inspection behavior, lint, formatting and types must pass. Root owns assembled
full verification and pinned all-files hooks. No live testing is authorized.
