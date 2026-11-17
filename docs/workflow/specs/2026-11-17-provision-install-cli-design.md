# Installer provisioning from the CLI

## Goal and constraints

Issue #1903 extends `hmcpctl lpars provision` to the installer modes already supported
by its MCP counterpart. [ADR 0393](../../adr/0393-cli-installer-provisioning.md) records
the interface and exit decision. Python >=3.11. Use the existing Typer, Pydantic and
operation engine. Verification is offline.

## Requests

Read `--install PATH` once on a new request, using a byte-bounded read and the shared
`LparInstall` model. A 2 MiB source cap accommodates 16 maximum-length keys even as
JSON-escaped supplementary characters and a full prepared producer result plus its
wrapper. Reject unknown fields on the typed install, media and network objects before
preview or journal admission. Pass the open prepared `producer_result` dictionary to
the shared binder. Boot mode belongs on the command line. File and schema errors
give short actionable guidance without exposing caller data. The independent 64 KiB limit on the
canonical journal request applies at submission.
Pass parsed values through the existing request validation and preflight. Prepared
media needs `--mac`; built media gets its MAC from the created adapter. Existing
URL allowlisting, builder configuration, disk ownership and media binding still apply.
Install defaults plain power-on to false. Explicit power-on reaches the shared refusal.
Boot defaults to immediate. Omitted new options retain the historical argument mapping.

A continuation loads the journaled request and obtains the writer-window assertion
from this invocation for resume and boot. Explicit request options must agree with
their recorded values. Detect which flags the caller passed, including explicit
defaults. For CLI flags addressing a field inside a stored composite, compare only
the specified field, preserving its stored siblings. Use the recorded request for
execution. When the initial invocation omitted `--boot`, a continuation must also omit
it to preserve the recorded field set. The engine checks the operation, connection,
conflicts and state transitions before starting a worker. Dry-run cannot continue a record.

The worker receives the engine's 600-second wait budget. The existing workflow owns its
deadline, polling interval, repeat behavior and power effects. A deferred
`ready_to_boot` pause reports that media is staged. A terminal `boot_started` reports
the firmware handoff and the limits of that evidence. These intended outcomes exit
zero alongside historical configured and explicitly abandoned outcomes. Pauses for
attention and failures exit nonzero. Confirmation goes to stderr. JSON stdout
preserves the existing result envelope.

## Failure model

- Actors and deployments — local CLI operators, including scripts, with access to
  a connection profile and its journal.
- Invariants and assets — exact request identity, operator-selected target and media,
  controlled power-on, confidential input and accurate exit/output.
- Accepted failures — bounded polling can end before boot evidence arrives. The
  operation records the reason and retains its existing recovery options.
- Covered elsewhere — operation effects, URL and builder policy and the interpretation
  of reference codes belong to ADRs 0319, 0324, 0326 and 0328.

## Trust boundaries and verification

The operator selects a local file, which may contain malformed or sensitive producer
data. Bound and type it before admission. The shared request validator checks media
and adapter binding; the shared preflight applies deployment policy. The journal
compares explicitly supplied request values and binds the connection. Preserve normal
terminal-field encoding and the shared JSON result. The CLI uses operator credentials
under its existing policy. Guide and help tell operators to serialize writes to the
chosen VolumeGroup for the whole operation. Disk creation rewrites that group, beyond
the scope of the profile writer-window assertion. Changes to the MCP authorizer, producer, or live boot are
outside this change.

Exercise both media modes, malformed and oversized input, public-key and producer size
boundaries, refusal before writes, dry-run, missing and fresh assertions, matching and
conflicting continuation values, historical replay, the body wait, and JSON and text
outcomes in isolated CLI tests. Use the built-in help and CLI guide for usage.
