# 0324 — Built install media runs one configured build entry

## Status

Accepted (2026-11-03), issue #1843 (piece 2 of #1227). Fills in ADR 0191 Decision 4, which fixes
neither the configuration names nor the template syntax.

## Context

- ADR 0191 D3–D4: in built mode, one operator-configured build entry runs after creation. It is an
  executable path plus a fixed argument template from deployment configuration. Only the manifest
  file varies, and no shell runs.
- The producer is iso-chain-loader. Its ADR 0015 takes a per-partition `iso-chain-target-v1`
  request file, and it prints one canonical `iso-chain-media-v1` line on stdout, at most 64 KiB.
  The request names `profile`, `lpar` (an identifier `[a-z][a-z0-9-]{0,31}` that becomes the
  installer hostname), `mac`, `network`, `operation_binding`, `ssh_authorized_keys` and
  `login_user`. Child tool output goes to stderr.
- `HMCConfig` fields are environment variables or TOML profile keys. pydantic-settings decodes a
  tuple-typed field from a JSON environment value, and a TOML profile supplies a native array.
- Provision runs its body on a worker thread and journals each write as an effect (ADR 0195).

## Decision

1. **Configuration.** `iso_build_entry` (`HMC_ISO_BUILD_ENTRY`) is an absolute executable path.
   `iso_build_arguments` (`HMC_ISO_BUILD_ARGUMENTS`) is a tuple of at most 64 strings: a JSON array
   in the environment, an array in a TOML profile. Exactly one element is the literal `{target}`.
   Nothing is substituted inside an element. Both fields are empty by default, which refuses built
   mode. A field set without the other is a configuration error. The build uses the configuration
   in force when its leg runs; an unset command then ends the run `failed`.
2. **Invocation.** hmcpctl writes the request into a new private directory with mode 0700, as a
   file with mode 0600. It runs `[entry, *arguments]` with `{target}` replaced by that file's path,
   through `exec`, never a shell. stdin is empty. stderr goes to hmcpctl's stderr. The environment is
   hmcpctl's own with every `HMC_*` name removed. The child runs in its own session, and hmcpctl
   removes the directory afterwards.
3. **Bounds.** hmcpctl reads at most 64 KiB of stdout. The run is limited to 3600 seconds. More
   output or a timeout kills the child's process group. The outcome is known once no child was
   started or the child has been reaped. Every error in that state ends the operation `failed`:
   writing the request, a spawn error, a non-zero exit, a timeout, an output overrun, and a result
   that does not parse or bind. Only an interruption while the child may still run leaves the
   build `uncertain`. The message never quotes the output.
4. **Result.** stdout must be one UTF-8 JSON object, optionally followed by a newline. Duplicate keys
   are refused. The keys are exactly the prepared set plus `operation_binding` and `url`. They bind
   as follows:
   - the prepared fields, including the adapter's actual MAC, bind as in ADR 0191 D3;
   - `operation_binding` equals the `operation_id`;
   - `url` is http(s), at most 2048 characters, and its origin is on `HMC_ISO_URL_ALLOWLIST`.
5. **Journal.** The build is effect `build`, kind `media.iso_build`, step `build_media`. It runs
   after the network and disk legs and before `bind_media`. Its identity is the bound
   `{url, iso_sha256, iso_size, manifest_sha256, mac}`, which the upload uses. A resume reuses an
   applied build. A build left `uncertain` classifies `needs_attention`, because no read can tell
   whether it ran, and hmcpctl does not run it twice.
6. **Partition name.** Built mode requires the partition name to be a producer `lpar` identifier
   that does not end in `-`. Anything else is refused before anything is recorded. The name is
   never rewritten.

## Consequences

- A failed or interrupted build can leave a published ISO on the media host. The operator removes
  it, as the producer's guide already requires. That is safe only while no operation sits between
  its build and its upload.
- A timeout kills only the process group. A containerized build whose container runs under the
  engine's own runtime may go on publishing after the run ended `failed`.
- An interrupted build pauses `needs_attention`. The operator abandons the operation and
  decommissions the partition, its adapter and its disk. A new provision then builds again for a
  new partition with a new MAC.
- The HMC session stays open, unused, through the build. If the HMC expires it, the upload fails
  and the run pauses. A resume reuses the applied build.
- A build longer than an hour fails.
- The builder sees the server's environment without its `HMC_*` names. A secret held in another
  variable still reaches the builder, which is an operator-installed program.

## Considered & rejected

- **One field holding the whole argv.** judgment: fit; ADR 0191 D4 names an executable and a template
  separately, and a separate path is easier to check before a build.
- **A shell-style string split with `shlex`.** judgment: fit; quoting rules invite a template that
  means something different from what the operator wrote, and JSON is exact.
- **`{target}` substituted inside an element (`--target={target}`).** judgment: cost; the producer
  takes the path as its own argument, and whole-element substitution cannot form an unintended
  argument.
- **A JSON string field for the template.** judgment: fit; a tuple field reads as an array from both
  sources, and a string would need its own parser.
- **A configurable timeout.** judgment: cost; a third field for a value no deployment has asked to
  change.
- **Classify an uncertain build `not_applied` and rebuild.** judgment: fit. An interruption that
  ends hmcpctl leaves the child running in its own session, and no read tells whether it is still
  publishing. A rebuild would race it. iso-chain-loader's `docs/guides/target-bound-media.md`
  (main `1e8540b`) adds that every run publishes a new ISO.
- **Read the result from a file.** verified: iso-chain-loader ADR 0015 (main `1e8540b`) sends the
  result to stdout, because a result path would be a second varying argument.
- **Pass the full environment.** judgment: fit; `HMC_PASSWORD` and the other `HMC_*` names are the
  server's credentials, and the builder has no use for them.
