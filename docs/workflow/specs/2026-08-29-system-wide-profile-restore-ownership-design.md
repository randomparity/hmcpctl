# System-wide profile restore ownership design

Issue: [#449](https://github.com/randomparity/hmc-mcp/issues/449)
Decision: [ADR 0092](../../adr/0092-uniform-lpar-ownership-authorization-rule.md)

## Goal and scope

`hmc_restore_lpar_profiles` must authorize every LogicalPartition on the selected managed system
before `rstprofdata` can rewrite any profile. The change keeps the existing explicit
`system_wide_restore_approved` acknowledgement, adds `ownership_override: bool = False`, reuses
ADR 0071's one-feed bulk ownership read, and leaves the SSH restore transport unchanged. Backup is
confirmed read-only with respect to partitions and profiles and remains in ADR 0092 §3.4a.

No facade export, ownership-token grammar, dependency, persistence, or access-policy grant changes.
The bounded audit contract change adds `lpar-profile-restore` to `OwnershipOperation` and its
matching `docs/authorization-audit.md` row so denials name this guard truthfully. The
managed-system grant and ADR 0011 ownership check remain separate boundaries.

## Decision and alternatives

The operations layer gains one internal guard and one mutation operation:

```python
async def _authorize_system_lpar_profile_restore(
    hmc: HMCClient,
    system_name_or_uuid: str,
    *,
    ownership_override: bool = False,
) -> str:
    """Authorize current LPARs and return the HMC CLI system name."""

async def restore_system_lpar_profiles(
    hmc: HMCClient,
    system_name_or_uuid: str,
    file_path: str,
    *,
    ownership_override: bool = False,
) -> str:
    """Authorize and restore all LPAR profiles on one managed system."""
```

Without an override it resolves the managed system, reads `list_lpar_ownership` once, and validates
that every returned row's `lpar_name` is a string containing at least one non-whitespace character
and its description is either a string or `None`. It then passes each description through
`authorize_lpar_ownership_description`. A foreign owner or malformed
`[hmc-mcp` token raises the existing `PermissionError` naming that partition. An absent description
or unrelated free text remains unowned and allowed under ADR 0011; ADR 0071's `unparsed` fact is
preserved rather than reinterpreted as foreign ownership. An unavailable feed or structurally
incomplete row raises an actionable `ValueError`. The first foreign or malformed row raises the
existing partition-specific denial. Even after a clean feed, the operation refuses without an
override because the opaque backup can contain definitions absent from that feed. Every failure
occurs before the SSH restore.

With `ownership_override=True`, the operation skips the ownership feed, emits one existing
`ownership-override` audit record with the selected system and `lpar="*"`, and returns the system
name. The wildcard states the override's actual scope without creating a second audit event schema.
Skipping the read matches ADR 0092's existing override rule: an operator-approved exception must
remain usable when the ownership dependency itself is degraded.

A denial emitted while checking a current partition uses the new `lpar-profile-restore`
`OwnershipOperation`; using `lpar-mutation` would falsely identify the single-partition guard as
the rejecting entry point.

The current feed cannot reveal a partition definition that exists only in the HMC-side backup. The
bulk guard therefore identifies current blockers but cannot authorize the complete restore. Only
the explicit audited override covers that unknowable remainder; the acknowledgement remains
accident prevention, not an unaudited ownership bypass. The combined operation calls the guard and
then the existing SSH helper. The tool keeps acknowledgement validation first, opens one
profile-selected `HMCClient`, and delegates the whole mutation to that operation.

Alternatives rejected:

- One SSH description read per partition: ADR 0071 verified that the REST list feed already carries
  every description; N fresh SSH logins add cost and a larger partial-failure window.
- Keep only the destructive managed-system grant and acknowledgement: direct Python and CLI
  consumers do not cross the MCP access-policy boundary, so this leaves every currently visible
  ADR 0011 owner unchecked.
- Treat every `unparsed=True` row as malformed: ADR 0011 explicitly permits descriptions with no
  ownership token. Only ownership-looking malformed text is denied by the shared parser contract.
- Parse the HMC-side backup to obtain an exact affected set: the accepted contract exposes only an
  HMC filesystem path and no supported backup-content reader; inventing a parser would add an
  unverified file format and another trust boundary.
- Read inventory before honoring an override: this makes the escape hatch unusable during the
  degraded inventory condition for which an operator may need it.
- Let a clean current feed proceed without override: a full restore can consume backup-only data,
  so this would turn `system_wide_restore_approved` from accident prevention into an unaudited
  ownership bypass.

## Errors and ordering

The precondition order is deterministic: explicit system-wide acknowledgement; selector and system
name resolution; override audit or complete bulk authorization; SSH restore. A false acknowledgement
performs no HMC I/O. A foreign or malformed token, missing partition name, non-string description,
REST resolution failure, or ownership-feed failure performs no restore SSH connection. The first
ownership blocker is returned using the existing partition-specific error and denial audit event.

An empty current-partition list still requires the audited override because it says nothing about
the backup file's opaque contents. Duplicate rows are each checked and do not widen access.

## Threat model

### Boundary inventory and actors

- Existing widened boundary: an MCP, CLI, or direct Python caller controls the system selector,
  acknowledgement, and override. The caller may be another authenticated agent sharing an HMC.
- Existing HMC boundary: REST returns partition identities and descriptions controlled by the HMC
  and operators outside this process. The feed can be unavailable or structurally incomplete.
- Existing SSH boundary: `rstprofdata` rewrites all profiles on the selected system.

The local operator is trusted to set `system_wide_restore_approved` and `ownership_override` only
after deliberate approval. Other agents and HMC-supplied description text are not trusted.

### Controls

- Acknowledgement prevents accidental crossing into a system-wide mutation.
- The complete current REST feed is checked before SSH, and incomplete identity/description data
  fails closed. The shared parser and authorization function remain the only token grammar and
  owner comparison. Only the audited override, not the acknowledgement or a false inventory claim,
  covers backup-only data.
- Override bypasses reads only after an explicit argument and emits the existing warning-level
  audit event before restore. Caller-controlled values remain bounded by the audit encoder.
- Errors name the blocking partition or failed inventory action but do not expose credentials.

Out of scope: cryptographic ownership tokens, HMC-user isolation, validating the contents of the
HMC-side backup file, and changing MCP access-policy grants. Those are separate boundaries already
recorded by ADRs 0011, 0036, 0039, and 0092. The SSH restore transport remains unchanged.

## Verification

- Existing successful restore tests use the audited override, skip the ownership feed, and still
  assert the exact `rstprofdata` command and raw output.
- A foreign-owned partition blocks and is named before any SSH connection.
- A malformed ownership-looking token blocks, while unrelated free text remains allowed.
- Missing, empty, whitespace-only, or non-string partition identity; non-string description; and an
  unavailable ownership feed each raise an actionable error before SSH.
- A clean or empty feed still fails closed with an actionable opaque-backup error.
- An explicit override skips the feed, emits one wildcard-scoped existing audit event, and restores.
- The acknowledgement test still proves zero HMC calls.
- Focused tests, `just verify`, and `UV_NO_SYNC=1 uv run --no-sync prek run --all-files` pass.

## Global constraints

- Python 3.11 through 3.14 on amd64 and arm64 Ubuntu runners must behave identically.
- Add no dependency, persistence, facade export, or ownership-token grammar. The only audit schema
  change is the authorized `lpar-profile-restore` member of `OwnershipOperation`, accompanied by
  the audit document and contract tests required by ADR 0100.
- Keep functions at or below 100 lines, complexity at or below 8, and lines at or below 100
  characters unless repository tooling defines otherwise.
- Branch: `feat/restore-profile-ownership-449`; base branch: `main`.
- Guardrails: `just verify`; `UV_NO_SYNC=1 uv run --no-sync prek run --all-files`.
