# Supported network inventory boundary

## Authority and outcome

The operator explicitly approved the prepared network refactor on 2026-11-11.
[ADR 0365](../../adr/0365-use-the-supported-network-inventory-boundary.md) records
one supported client seam and its explicit absence-proof option. The frozen
scope is network_inventory_private_boundary, one coupled Cast unit.

## Components and migration

HMCClient.list_adapters(lpar_uuid, adapter_type, *, allow_empty_network=False)
retains adapter-type validation and list_child for non-network kinds. Network
listing delegates to _complete_network_adapter_feed(self, lpar_uuid, *,
allow_empty=False), a private function in the same client module. This is the
existing parser moved without algorithm changes; remove the old private method
and its AdaptersClient protocol member. The keyword affects network HTTP204 only.
Keep UUID validation, one GET, headers, status handling, complete-feed/entry
validation, pagination refusal and existing errors at that transport boundary.

Migrate five reads in operations/virtualization/adapters.py and the Ethernet
experiment's contained inventory read to list_adapters(uuid, "ClientNetworkAdapter").
Reconfiguration current_adapters uses allow_empty_network=True, retaining its
existing absence-proof policy. No caller interprets HTTP directly. Keep effect
admission, parent/ownership checks, TLS fatality and observation ordering intact.

Migrate the nine direct test owners identified by the old method search: client
adapter, request-path safety, network creation/update, reconfiguration, two handoff
modules, hold coverage and Ethernet arm tests. The mixed reconfiguration fake
already has a non-network list_adapters override; combine it with its inherited
network listing rather than shadowing network behavior. Rename the AST request
construction-site inventory to the module function; retain the parser's original self receiver so existing
UUID metadata proof remains valid without broadening the guard.

## Success

The seven production readers use the supported listing method, and the old private
client method/protocol dependency is absent. Default network listing still refuses
HTTP204; its explicit opt-in accepts only that additional empty response. Blank,
wrong-root, malformed, paginated and ambiguous-entry responses retain refusal.
Non-network listing, 207 MCP schemas, six-name facade, effect request sequences,
public output and authorization remain unchanged for the affected consumers.

## Failure model

- Actors/deployments: existing Python client and CLI/MCP domain callers, registered
  Ethernet experiment and supported provider responses; offline fake/HTTP tests.
- Invariants/assets: truthful complete inventory, explicit absence evidence,
  selected identity/ownership, TLS cutoff and unchanged request/effect ordering.
- Accepted classes: existing remote uncertainty and weak provider concurrency
  behavior are unchanged; this interface refactor adds no transaction/retry or
  hardware maturity guarantee. Non-bool Python inputs get no new validation promise.
- Covered elsewhere: UUID/path validation, client TLS/transport and domain mutation
  authorization retain their owners. Existing caller serialization duties remain.

## Validation

Prove the supported keyword with a real HTTP204 fake before implementation, and
retain default refusal tests. Extend empty/invalid-feed tests across both flag
values and non-network delegation. A complete caller migration test verifies the
unsupported method is not required by actual domain workflows, not just its name.
Compare the moved parser AST modulo receiver rename and old/new response results.
Run the nine-owner baseline and affected network/creation/reconfiguration/handoff/
Ethernet suites, public facade and 207 wire schemas. Controlled faults must break
strict refusal/opt-in or call routing; restore exact source bytes afterward.
Run pinned hooks, inspect the full diff and commit one conventional task.
Final exact-HEAD full branch verification and bounded whole-delta review remain.
