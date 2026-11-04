# 0329 — The host handoff document reads each section as its own tool and declares from the provision record

## Status

Accepted (2026-11-03), issue #1852 (piece 2 of #1231). Extends ADR 0193 Decision 1, which it
leaves in force, and amends the H1 spec's *Delegated authorization* row for
`hmc_prepare_host_handoff`.

## Context

- The H1 spec's `prepare` document lists observed adapters, MACs, disks and mounted media, but
  its delegated row names only `hmc_get_lpar`, `hmc_list_lpar_ownership` and
  `hmc_get_vios_storage_detail`. Finding the serving VIOSes needs `hmc_list_vios`, and adapters
  and MACs come from the client network adapter feed, which `hmc_list_adapters` reads.
- `declared` facts come "from the resource ledger", but the provision ledger records only
  partition, disk and media identities. The install profile, network and keys are in the
  provision operation's recorded request, which ADR 0190 prunes 30 days after it ends.
- ADR 0193 says nothing on ownership stamps or on releasing a partition that no longer resolves.

## Decision

1. **Required and per-section reads.** `prepare` refuses unless the policy permits
   `hmc_get_lpar`, `hmc_list_lpar_ownership` and `hmc_get_vios_storage_detail`, and admits the
   first two for the partition. The storage section (`hmc_list_vios`, then
   `hmc_get_vios_storage_detail` admitted per VIOS) and the adapters section (`hmc_list_adapters`,
   admitted with the caller's selector) are reported `ok`, `unavailable` or `denied`, as ADR 0200
   does for inspection: which VIOSes serve the partition is known only after their details are
   read, so a VIOS outside the grant's targets is reported rather than refusing the handoff.
2. **Declared from the provision record.** The partition's `partition` ledger entry names its
   provision operation; that operation's recorded request supplies the install profile, network,
   and for built media the login user and SHA-256 key fingerprints, beside that operation's state
   and outcome: `declared` is what was requested, not what was applied. No entry reports `none`; a
   pruned or unreadable record reports `unavailable`. Nothing is inferred from the HMC.
3. **Release resolves like prepare.** Release resolves the partition through the HMC with the
   tool's own grant; a partition that no longer resolves is released by the operator (ADR 0193
   Decision 7).
4. **Ownership is reported, not enforced.** A hold may be placed on a partition whose ADR 0011
   stamp names another agent; `observed.ownership` carries the stamp.

## Consequences

- A policy that withholds `hmc_list_vios` or `hmc_list_adapters` still gets a handoff, with those
  sections `denied`; the H1 row is amended to say so.
- `declared` is readable by any caller granted the tool and the required reads for the partition,
  whichever agent or connection provisioned it, unlike `hmc_operation_status`. It carries network
  configuration, a login user and key fingerprints, none of them secret.
- `declared` disappears 30 days after the provision ends, and is `none` for partitions provisioned
  by another host or by hand.
- An agent granted `hmc_prepare_host_handoff` can hold a partition another agent stamped; the
  operator's `hmcpctl holds release` undoes it.

## Considered & rejected

- **Require `hmc_list_vios` and `hmc_list_adapters` up front.** judgment: fit. It refuses a whole
  handoff for facts kdive re-checks, where ADR 0200 already reports a withheld section.
- **Read adapters under `hmc_get_lpar`'s grant.** judgment: fit. It discloses through one grant
  what ADR 0189 Decision 2 makes a separate delegated tool.
- **Record the install declaration in the ledger at provision time.** judgment: cost. It edits the
  provision operation, which #1850 is changing in parallel, and leaves every existing partition
  without one.
- **Follow the H1 row literally (do nothing).** judgment: fit. It omits the VIOS listing and
  adapter reads the same spec's document requires, and refuses a handoff when one VIOS outside a
  grant's targets is listed.
- **Release by partition UUID from the store alone.** judgment: complexity. A second resolution
  path needs its own system-matching rules, and the operator command already covers vanished
  partitions.
- **Refuse a hold on a partition stamped for another agent.** judgment: fit. ADR 0011 stamps are
  advisory, and ADR 0193 names no ownership precondition.
