# Force-save recovery-copy name length (#1868)

## Problem

`save_current_partition_profile(overwrite=True)` first writes a recovery copy of the
destination profile (ADR 0242). The copy's name is `"hmcprec-" + secrets.token_hex(12)`
(`src/hmcpctl/operations/lpar/profiles/core.py`), which is 32 characters long. A live capture on
2026-11-03 (V10R3 SP1060, POWER9, one owned scratch partition) recorded the clone PUT
answered with HTTP 500. The answer was `REST0142` wrapping `HSCL059F The user-defined name
"hmcprec-…" is too long. The user-defined name cannot be longer than 31 characters.` In the
same session, a PUT of the same projected document with a 21-character name returned 200. The
refused and accepted documents were both projections of the profile the HMC served in that
session, and differed only in `ProfileName` and the emptied root `Metadata`. The HMC's message
names the name. The cause is the generated name's length, not the projection in
`clone_profile_document`.
Because overwrite always clones first, force-save is unreachable on that HMC.

Two secondary defects hid the cause. `write_native_profile`
(`src/hmcpctl/client/client_profiles.py`) raises `HMCError` without the response body, so the
HMC's `Message` never reaches the caller. Its sibling client methods in `client/core.py` pass
the body. The recovery step's message also says only "requires reconciliation". It does not
say that no overwrite was submitted.

## Decision

1. Generate the recovery name as `"hmcprec-" + secrets.token_hex(8)`: 24 characters, 64
   random bits. The `hmcprec-` prefix is unchanged, so the live arm's namespace check
   (`scripts/live_test/arms/partition_profiles.py`) still matches it.
2. `write_native_profile` passes only the HMC's `<Message>` text (or nothing) to `HMCError`,
   never the raw body: that body echoes the session cookie and `X-API-Session` in
   `<RequestHeaders>`, and `HMCError` would otherwise keep 4096 bytes of it in `.body` and fall
   back to the first 500 characters when `<Message>` is absent.
3. `_recovery`'s `except (HMCError, ValueError, TypeError)` — which covers the clone write,
   its readback and comparison, the post-copy freshness checks, and a selected existing
   recovery profile — makes the outcome's `error` begin `recovery step failed; overwrite not
   submitted; reconcile recovery profile <name> if present:` followed by the underlying error.
   The wording stays true on each of those paths. The asynchronous-copy (202) message gains
   `overwrite not submitted`. `recovery_profile` keeps reporting the name. Exits before that
   `try` (the name-collision refusal and the pre-copy freshness check) write nothing and keep
   their text.
4. ADR 0242 gains an appended, dated section recording the observed limit and the captured
   refusal. Its recovery-copy requirement is unchanged, so ADR 0316 is not used.

Not changed:

- `profile_name()`. It also validates partition names, which the HMC allows to be longer.
  No separate 31-character guard is added: the generator emits a constant 24 characters and
  the stateful test transport pins the limit.
- The `ProfileName` passed to `SaveCurrentConfig`. No capture shows that job's limit.
- The root `Metadata` the projection retains. ADR 0242 forbids omitting it. The accepted
  diagnostic PUT omitted it, so the live proof below is the first evidence that the HMC
  accepts it.

## Failure model

- **Actors and deployments:** an operator or agent calling the LPAR/VIOS save-current tool, or
  its CLI mirror, against an HMC at V10R3/V11R2 inside the ADR 0053 envelope.
- **Invariants and assets:**
  - the destination profile is never force-saved without a verified recovery copy;
  - a recovery copy that may exist is always named in the outcome;
  - no session token or cookie reaches tool output.
- **Accepted failure classes:**
  - Another HMC refusal of the clone (not HSCL059F). It surfaces with the HMC's `Message`
    and leaves the destination untouched. That is the classified-refusal path criterion 3
    asks for.
  - Root `Metadata` refused on PUT. The live proof would expose it, and a new design cycle
    owns the response.
- **Covered elsewhere:**
  - the save-current job outcome misread: #1867;
  - provision persistence: #1840;
  - `HMCError.body` retains up to 4096 bytes of the raw body, a property of every
    `client/core.py` error that predates this change.

### Threat model

- **Boundary:** an HMC error body enters tool output. The HMC echoes request headers, including
  the cookie and `x-api-session`, in `<RequestHeaders>`.
- **Actor:** the authenticated operator who reads the tool output. The HMC is trusted for
  content but not for leaking its echo.
- **Control:** `write_native_profile` extracts `<Message>` and hands `HMCError` nothing else.
  Focused tests over the captured body, a Message-less cut and a non-XML cut assert that no
  session token appears in the error text or `.body`.
- **Out of scope:** the other client paths that hand raw HMC bodies to `HMCError`; reported as a
  follow-up candidate.

## Validation

- `focused-test`: the recovery name the product generates is at most 31 characters, and the
  force-save completes against a stateful transport that refuses longer names with the
  captured 500 (`tests/lpar/test_partition_profile_operations.py`). Red on main: HTTP 500,
  no job.
- `focused-test`: a clone PUT answered with the captured 500 (a test forces the pre-fix
  32-character name) yields an outcome whose `error` names the recovery step, says no overwrite was submitted, carries `HSCL059F`, and
  includes neither the captured session nor the cookie tokens. No job is submitted, and
  `recovery_profile.name` is set (`tests/lpar/test_partition_profile_operations.py`). Red on
  main: the message lacks both texts.
- `focused-test`: `write_native_profile` on 500 raises `HMCError` whose text carries the
  captured `Message` (`tests/unit/test_client_profiles.py`). Red on main.
- Fixture: `tests/fixtures/live/rest-profile-create-name-too-long.json`, tokenized from the
  private capture and cited in its README.
- `task-test-not-applicable`: the ADR 0242 amendment. It is prose; `just adr-numbering` checks
  only its name and H1.
- Live proof (its own operator grant, requested through the campaign lease; preflight and
  recovery per `docs/live-testing.md`; an untracked private script only if that grant admits
  one, because the named `partition-profiles` arm needs a prepared target and generates
  38-character scratch names):
  on one owned scratch partition, run create, save-current into a new profile named
  `hmcpctl_cap` (11 characters), then force-save over the scratch `default_profile`.
  `recovery_verified` must be true and the force-save job must be submitted. Its reported
  execution may be `unknown` (#1867), so the profile readback decides the result. Then
  teardown, a `CLEAN` recovery, and a public matrix from `scripts/live_test_evidence.py`.
