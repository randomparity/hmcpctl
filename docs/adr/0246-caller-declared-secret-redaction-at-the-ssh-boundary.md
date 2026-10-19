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
The `-i` record builder quotes a refused value back. None of this matters while no command
carries a secret; the credential-bearing children of #620 (#673–#677, #682, #689, #695) will
send passwords, activation codes and key material through this seam.

## Decision
The caller declares the secret values a command carries; the transport executes the command
unchanged and redacts every declared value from every diagnostic it produces.

- `run_hmc_command(config, cmd, *, include_stderr=False, secrets=())`. `secrets` is a
  collection of non-empty strings; an empty string is a `ValueError` before any connection.
- With `secrets` empty the function is byte-for-byte today's: same command, same messages,
  same exception chain. Existing callers do not change.
- With `secrets` non-empty, a translated `HMCCLIError` is re-raised as a new `HMCCLIError`
  whose message has each declared value replaced by `<redacted>`, raised with no `__cause__`
  and no `__context__`, so no rendering of the exception reaches the asyncssh exception.
  Replacement covers the raw value and its form inside a POSIX single-quoted shell word
  (`'` written `'"'"'`), longest first. Remote text keeps its HSCL code and wording.
- While a secret-bearing command runs, a filter on the `asyncssh` logger replaces the declared
  values in every record's rendered message.
- `build_attribute_record(..., secret=())` names attributes whose refused value is rendered
  `<redacted>`; the attribute name, the offending character and the reason stay.

## Consequences
- A secret-bearing operation must declare its values; an undeclared secret is not redacted.
  Review of each credential-bearing child checks its declaration.
- Redaction matches text, so a remote echo that transforms the value (truncation, case
  change, re-encoding) is not recognised, and a short secret over-redacts unrelated text.
- Losing the exception chain on the secret path removes the asyncssh exception from
  tracebacks; the message keeps the termination status and remote text.
- `transport.py` and `commands.py` sit in every SSH operation's import closure, so their
  live evidence is not current for the next release until re-observed (ADR 0212).

## Considered & rejected
- **Caller-supplied display string.** judgment: leaves the remote stdout/stderr echo and the
  chained exception unredacted, which is where the HMC repeats a refused value.
- **Drop remote output entirely on the secret path.** judgment: removes the HMC's own refusal
  text, the actionable part of a failure, to defend against transformed echoes the HMC CLI is
  not known to produce.
- **Separate `run_hmc_secret_command` function.** judgment: a second dispatch path for one
  keyword's worth of behaviour; callers and tests would have to keep two in agreement.
- **Answer the HMC's password prompt instead of passing the value.** verified: the reference
  documents the prompt per command and option (`docs/refs/hmc-commands-p10/commands/chhmcusr.md`
  `-v`/`--localpasswd`, `mkauthkeys.md` `--passwd`), not as a general CLI input channel, so it
  cannot be applied at the shared seam; driving it is an interactive dialog, an epic #620
  non-goal, and the other diagnostics would still need redaction.
- **Do nothing until the first credential-bearing child.** judgment: epic #620 orders this
  seam first so each child reuses one reviewed boundary instead of inventing its own.
