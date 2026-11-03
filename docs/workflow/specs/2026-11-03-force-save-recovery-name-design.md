# Force-save recovery-copy name length (#1868)

## Problem

`save_current_partition_profile(overwrite=True)` first writes a recovery copy of the
destination profile (ADR 0242). The copy's name is `"hmcprec-" + secrets.token_hex(12)`
(`src/hmcpctl/operations/lpar/profiles.py`), which is 32 characters long. A live capture on
2026-11-03 (V10R3 SP1060, POWER9, one owned scratch partition) recorded the clone PUT
answered with HTTP 500. The answer was `REST0142` wrapping `HSCL059F The user-defined name
"hmcprec-…" is too long. The user-defined name cannot be longer than 31 characters.` In the
same session, a PUT of the same projected document with a 21-character name returned 200. The
cause is the generated name's length, not the projection in `clone_profile_document`.
Because overwrite always clones first, force-save is unreachable on that HMC.

Two secondary defects hid the cause. `write_native_profile`
(`src/hmcpctl/client/client_profiles.py`) raises `HMCError` without the response body, so the
HMC's `Message` never reaches the caller. Its sibling client methods in `client/core.py` pass
the body. The recovery step's message also says only "requires reconciliation". It does not
say that no overwrite was submitted.

## Decision

1. Generate the recovery name as `"hmcprec-" + secrets.token_hex(8)`: 24 characters, 64
   random bits. The `hmcprec-` prefix is unchanged, so the live arm's namespace check
   (`scripts/live_test/partition_profiles.py`) still matches it.
2. `clone_profile_document` refuses a name longer than 31 characters with `ValueError` before
   anything is written. This guards the one HMC-created name hmcprec controls. It cites
   HSCL059F.
3. `write_native_profile` passes `response.text` to `HMCError`, as `client/core.py` does. The
   shared `HMCError` renders only the `<Message>` element (bounded to 500 characters). Its
   raw-body fallback applies only when no `Message` is present.
4. When the recovery copy cannot be created or verified, the outcome's `error` begins
   `recovery copy failed; overwrite not submitted; reconcile retained recovery profile <name>
   if present:`, followed by the underlying error. `recovery_profile` keeps reporting the
   generated name, as it does today.
5. ADR 0242 gains an appended, dated section recording the observed limit and the captured
   refusal. Its recovery-copy requirement is unchanged, so ADR 0316 is not used.

Not changed:

- `profile_name()`. It also validates partition names, which the HMC allows to be longer.
- The `ProfileName` passed to `SaveCurrentConfig`. No capture shows that job's limit.
- The root `Metadata` the projection retains. ADR 0242 forbids omitting it. The live proof
  below shows whether the HMC accepts it.

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
- **Control:** `HMCError` renders `find_text(body, "Message")`. That excludes
  `RequestHeaders`, and a focused test asserts that no session token appears in the outcome.
- **Out of scope:** the raw-body fallback for non-XML or Message-less bodies (existing
  behavior, unchanged).

## Validation

- `focused-test`: the recovery name the product generates is at most 31 characters, and the
  force-save completes against a stateful transport that refuses longer names with the
  captured 500 (`tests/lpar/test_partition_profile_operations.py`). Red on main: HTTP 500,
  no job.
- `focused-test`: `clone_profile_document` refuses a 32-character name and accepts a
  31-character one (`tests/lpar/test_partition_profiles.py`). Red on main: no refusal.
- `focused-test`: a clone PUT answered with the captured 500 yields an outcome whose `error`
  names the recovery-copy step, says no overwrite was submitted, carries `HSCL059F`, and
  includes neither the captured session nor the cookie tokens. No job is submitted, and
  `recovery_profile.name` is set (`tests/lpar/test_partition_profile_operations.py`). Red on
  main: the message lacks both texts.
- `focused-test`: `write_native_profile` on 500 raises `HMCError` whose text carries the
  captured `Message` (`tests/unit/test_client_profiles.py`). Red on main.
- Fixture: `tests/fixtures/live/rest-profile-create-name-too-long.json`, tokenized from the
  private capture and cited in its README.
- `task-test-not-applicable`: the ADR 0242 amendment. It is prose; `just adr-numbering` checks
  only its name and H1.
- Live proof (operator lease, `docs/live-testing.md`): on one owned scratch partition, run
  create, save-current into a new profile, then force-save over the scratch `default_profile`.
  `recovery_verified` must be true and the force-save job must be submitted. Its reported
  execution may be `unknown` (#1867), so the profile readback decides the result. Then
  teardown and a `CLEAN` recovery.
