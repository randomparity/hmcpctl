# ADR 0365: Use the supported network inventory boundary

## Status

Accepted following explicit operator approval on 2026-11-11.

## Context

Network creation/update, reconfiguration and an Ethernet experiment bypass
list_adapters to call a creation-specific private client method. The public
method already requires complete network inventory; only reconfiguration needs
the existing HTTP204 absence-proof exception. The private method also leaks
through the mixin's static host protocol.

## Decision

Extend list_adapters with keyword-only allow_empty_network=False. Network
inventory remains strict by default; True accepts HTTP204 as no adapters.
Other kinds retain list_child behavior. Keep full-feed and entry validation,
UUID checks and response errors in one private module function in client_adapters.
Move the existing parser body there and remove the obsolete method/protocol member.

Migrate all seven production reads. The reconfiguration current_adapters caller
alone opts in; creation/update and the experiment keep strict admission.
No MCP/CLI parameter, new request, mutation or authorization change is introduced.

## Consequences

Existing public calls remain compatible. Direct private callers/fakes migrate
as one change without an alias. HTTP204 absence is explicit at its actual caller,
while malformed or partial inventory cannot become proof of absence.

## Considered & rejected

- Keep private operation reads: judgment: duplicates the supported inventory seam.
- Add another public network-list method: judgment: existing list_adapters already
  owns network listing, so another entry point would split responsibility.
- Accept HTTP204 by default: judgment: would weaken the existing strict listing
  contract instead of retaining its deliberately scoped absence-proof exception.
