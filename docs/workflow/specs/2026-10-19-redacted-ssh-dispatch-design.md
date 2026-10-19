# Redacted SSH dispatch for secret-bearing HMC CLI operations — design

Issue #655 (W22 of epic #620). Decision record: [ADR 0246](../../adr/0246-caller-declared-secret-redaction-at-the-ssh-boundary.md).

## Problem

`run_hmc_command` (`src/hmcpctl/ssh/transport.py`) quotes the command in its timeout,
connection and process-failure messages, quotes remote stdout/stderr verbatim, chains the
asyncssh exception (which repeats the command and output), and asyncssh logs the command at
INFO. `build_attribute_record` (`src/hmcpctl/ssh/commands.py`) quotes a refused value. A
command carrying a password, activation code or private key would leak it through any of
these. This slice adds the seam; it adds no credential-bearing operation.

## Design

One keyword on the existing dispatch, one on the existing record builder. No new module.

**Transport.** `run_hmc_command(config, cmd, *, include_stderr=False, secrets: Collection[str] = ())`.

1. Validate `secrets` first: any empty string raises `ValueError("secrets must be non-empty
   strings; an empty secret would redact every position of the diagnostic")`. A bare `str`
   passed as the collection is also a `ValueError` (it would be iterated as characters).
2. With no secrets, the body runs exactly as today.
3. With secrets, the existing body runs inside a context that adds a `logging.Filter` to
   `logging.getLogger("asyncssh")` and removes it in `finally`. The filter replaces the
   declared values in `record.getMessage()`, stores the result in `record.msg`, sets
   `record.args = ()`, and returns `True`.
4. An `HMCCLIError` raised by the body is caught, its `str()` redacted, and a new
   `HMCCLIError` raised **after** the `except` block exits, so the new error has
   `__cause__ is None` and `__context__ is None`.
5. Redaction (`_redact(text, secrets)`): for each secret, the forms are the raw value and the
   value with `'` replaced by `'"'"'` (its spelling inside a POSIX single-quoted word, which
   is what `shlex.quote` produces for the value alone or inside a larger quoted record).
   All forms, longest first, are replaced by the module constant `REDACTED = "<redacted>"`.

`run_hmc_cli` and `open_hmc_connection` are unchanged (console path excluded by charter).

**Record builder.** `build_attribute_record(pairs, *, quoted=(), surface="-i", secret=())`.
`secret` names attributes; `_validated_value` gains `redact: bool = False`, and when set every
refusal message renders the value as `<redacted>` instead of `{text!r}`. Attribute name,
character and reason are unchanged. The credential warning in the docstring is replaced by a
pointer to `secret=`.

**Parsers.** No change. `parse_hmc_delimited_rows`, `parse_hmc_result_rows` and
`_parse_lshwres_output` report header mismatch, column counts and `csv.Error` text, none of
which quotes a field value; tests pin that over output containing a secret.

## Success

1. For each failure class — timeout (connect and run), `OSError`/`ValueError` connection,
   `asyncssh.ProcessError` (exit status and signal, secret in stdout and in stderr) and
   `asyncssh.Error` — a secret-bearing call raises `HMCCLIError` where neither `str(error)`
   nor `"".join(traceback.format_exception(error))` contains any declared secret, and
   `__cause__`/`__context__` are `None`.
2. Those messages keep the non-secret context: the failure class wording, termination
   status, HSCL code, and the non-secret parts of the command.
3. The command passed to `connection.run` is the caller's `cmd`, unmodified.
4. A record emitted on the `asyncssh` logger during a secret-bearing call renders without the
   secret; the filter is removed afterwards, including after a failure.
5. Parse failures over output containing a secret do not include it.
6. `build_attribute_record(..., secret={"passwd"})` refusing a `passwd` value names
   `passwd` and the character but not the value; non-secret attributes still quote theirs.
7. Calls without `secrets` keep today's messages and `__cause__` (existing tests unchanged).

## Failure model

**Actors and deployments**
- MCP server and CLI processes run by an operator, plus library callers of the domain
  modules; future credential-bearing operations (#673–#677, #682, #689, #695) as callers.
- The HMC as a remote peer whose stdout/stderr is untrusted text (ADR 0042).

**Invariants and assets at stake**
- Declared secret values never appear in an `HMCCLIError` message, its rendered traceback,
  or an `asyncssh` log record produced during the call.
- Non-secret callers' commands, messages and exception chains are unchanged.

**Accepted failure classes**
- A secret the caller did not declare is not redacted — the declaration is the contract;
  each credential-bearing child's review checks it.
- A remote echo that transforms the value (truncated, re-cased, re-encoded) is not matched;
  no HMC refusal recorded in this repository shows a transformed echo, and dropping
  remote text would remove the actionable HSCL message (ADR 0246, rejected alternative).
- Over-redaction of unrelated text equal to a short secret; bounded to readability.
- Exceptions other than the four translated classes (e.g. `CancelledError`) propagate
  unchanged; none carries the command text.
- An asyncssh record logged with `exc_info` keeps its traceback text; asyncssh logs no
  command-bearing exception on the exec path.

**Covered elsewhere**
- Credential-bearing operations and their declarations → #673–#677, #682, #689, #695.
- Live-runner evidence redaction → ADR 0120 (operator).
- Console streaming path (`open_hmc_connection`) → operator.
- Audit records carry no command text or error text (ADR 0040 record fields); the
  stderr sink renders exceptions via `StreamSafeFormatter`, covered by Success 1's traceback check.

### Threat model

- **Boundaries widened:** caller → transport (secret values enter); HMC → transport
  (remote output enters error text); transport → logging and exception consumers (MCP error
  results, audit/stderr sink, live harness, CLI output).
- **Actors:** a reader of logs, MCP error results, CLI output or harness artefacts who must
  not learn a credential; the HMC as an untrusted text source. The caller is trusted to
  declare its secrets.
- **Controls:** redaction of every translated message; dropped exception chain; scoped
  `asyncssh` log filter; secret-aware record-builder refusals. Failure leaks nothing more
  than the redacted message.
- **Out of scope:** process memory, SSH wire (encrypted by SSH), the HMC's own logs, and
  undeclared secrets.

## Validation

All focused tests live in `tests/unit/test_ssh_redaction.py` (new), asyncssh mocked as in
`tests/unit/test_ssh.py`.

| Contract | Mode | Test |
|---|---|---|
| Success 1–3 per failure class | focused-test | parametrized `test_secret_failure_is_redacted` |
| Success 1 quoting form | focused-test | `test_secret_with_single_quote_is_redacted_in_quoted_record` |
| Success 4 log filter | focused-test | `test_asyncssh_log_record_is_redacted_and_filter_removed` |
| Success 5 parsers | focused-test | `test_parse_failures_do_not_echo_secret_output` |
| Success 6 builder | focused-test | `test_secret_attribute_refusal_omits_value` |
| Success 7 default path | focused-test | existing `tests/unit/test_ssh.py` unchanged + `test_no_secrets_keeps_cause` |
| empty / bare-str secrets | focused-test | `test_invalid_secrets_rejected_before_connect` |

## Live procedure (not a merge gate)

No registered operation changes, so there is no catalog scenario. Exact reproducible
procedure, read-only, for the release window or an authorized ad-hoc window
([live-testing](../../live-testing.md) preflight and recovery apply), on a build whose
commit matches the deployed revision:

1. Prerequisites: a configured read-only HMC profile; a canary `C = "w22-canary-<random>"`.
2. Process failure: `run_hmc_command(cfg, f"lssyscfg -r sys -m {shlex.quote(C)} -F name",
   secrets=[C])`; expect `HMCCLIError`; assert `C` absent from `str(err)` and from
   `traceback.format_exception(err)`, and an `HSCL` code present.
3. Connection failure: same call with a config whose host refuses port 22 (e.g. a reserved
   documentation address); assert `C` absent.
4. Timeout: same call with `ssh_timeout=0.001`; assert `C` absent.
5. Run with `logging.getLogger("asyncssh").setLevel(logging.INFO)` and a capturing handler;
   assert no captured record contains `C`.

Remaining gap: unrun against a live HMC; the transport's operations keep their current
maturity and their evidence is not current for the next release until re-observed (ADR 0212).
