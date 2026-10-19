# ADR 0246: Caller-declared secret redaction at the SSH dispatch boundary

## Status
Accepted

## Context
Issue #655 (epic #620 requirement 18). `run_hmc_command` builds every failure message from
the command it ran: a timeout and a connection failure quote `cmd`, a non-zero exit quotes
`cmd` and the remote stdout or stderr verbatim, and each translated error chains the
asyncssh exception, whose `command`, `stdout` and `stderr` attributes repeat the same text.
asyncssh also logs every command at INFO on the `asyncssh` logger
(`asyncssh/channel.py:1250`, asyncssh 2.24.1 as locked).
The `-i` record builder quotes a refused value back, and ADR 0045 records the resulting
instruction not to route a credential attribute through it unredacted. None of this matters while no command
carries a secret; the credential-bearing children of #620 (#673–#677, #682, #689, #695) will
send passwords, activation codes and key material through this seam.

## Decision
The caller declares the secret values a command carries; the transport executes the command
unchanged and redacts every declared value from every diagnostic it produces.

- `run_hmc_command(config, cmd, *, include_stderr=False, secrets=())`. `secrets` is a
  collection of non-empty strings; an empty string is a `ValueError` before any connection.
- With `secrets` empty the function is byte-for-byte today's: same command, same messages,
  same exception chain. Existing callers do not change.
- With `secrets` non-empty, redaction replaces the raw value, its stripped value, and the
  spelling of each inside a POSIX single-quoted shell word (`'` written `'"'"'`, as
  `shlex.quote` emits) — every occurrence, overlapping ones merged — with `<redacted>`. It is
  applied to the command **before** the error builders render it with
  `!r` — repr escapes quotes, backslashes and control characters, so redacting the rendered
  message alone misses them — and again to the finished message, which covers remote
  stdout/stderr and asyncssh text. The resulting `HMCCLIError` is raised with no `__cause__`
  and no `__context__`, so no rendering reaches the asyncssh exception. Remote text keeps its
  HSCL code and wording.
- While a secret-bearing command runs, a filter on the `asyncssh` logger replaces the declared
  values in each record's rendered message at asyncssh's default debug level, the only level
  this package runs (it never calls `asyncssh.set_debug_level`).
- `build_attribute_record(..., secret=())` names attributes whose refused value is rendered
  `<redacted>`; the attribute name, the offending character and the reason stay.

## Consequences
- A secret-bearing operation must declare its values; an undeclared secret is not redacted.
  Review of each credential-bearing child checks its declaration.
- Redaction matches text, so a remote echo that transforms the value (truncation, case
  change, re-encoding) is not recognised, a command that spells a secret other than raw or
  `shlex.quote`d is not recognised, and a short secret over-redacts unrelated text.
- The log arm depends on asyncssh logging the exec command on the `asyncssh` logger itself,
  as 2.24.1 does; a loopback test drives the real exec path, so the locked version is checked,
  and a library consumer on another asyncssh release in the allowed range should re-verify.
- At asyncssh debug level 3 the library appends hex packet dumps to its records; the filter
  does not parse them. A caller that raises the level accepts that.
- Losing the exception chain on the secret path removes the asyncssh exception from
  tracebacks; the message keeps the termination status and remote text.
- `transport.py` and `commands.py` sit in every SSH operation's import closure, so their
  live evidence is not current for the next release until re-observed (ADR 0212).

## Considered & rejected
- **Caller-supplied display string.** judgment: leaves the remote stdout/stderr echo and the
  chained exception unredacted, which is where the HMC repeats a refused value.
- **Drop remote output entirely on the secret path.** judgment: removes the HMC's own refusal
  text, the actionable part of a failure. Whether the HMC echoes a credential transformed is
  unobserved — no credential-bearing refusal has been captured yet — so the first credential
  child's live run checks the echo form and can reopen this.
- **Separate `run_hmc_secret_command` function.** judgment: a second dispatch path for one
  keyword's worth of behaviour; callers and tests would have to keep two in agreement.
- **Answer the HMC's password prompt instead of passing the value.** verified: the reference
  documents the prompt per command and option (`docs/refs/hmc-commands-p10/commands/chhmcusr.md`
  `-v`/`--localpasswd`, `mkauthkeys.md` `--passwd`), not as a general CLI input channel, so it
  cannot be applied at the shared seam; driving it is an interactive dialog, an epic #620
  non-goal, and the other diagnostics would still need redaction.
- **Do nothing until the first credential-bearing child.** judgment: epic #620 orders this
  seam first so each child reuses one reviewed boundary instead of inventing its own.
