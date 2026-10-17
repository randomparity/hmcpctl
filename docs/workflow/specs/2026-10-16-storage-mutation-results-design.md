# Storage mutation results

## Problem and scope

Issue #1699 replaces implicit VG/VIOS dumps in the thirteen storage mutations in
`cli_commands/storage/resources.py` with concise, truthful results. The operation
layer remains the authority for mutation completion and refusal. Existing
`output.print_inventory` owns table/TSV/JSON encoding; extend its callers, not
its encoding policy. [ADR 0230](../../adr/0230-concise-storage-mutation-results.md)
records the compatibility choice. Selectors, consent and final journey remain
owned by #1697, #1703 and #1704.

## Behavior

Each mutation gains `--format table|tsv|json` and `--json/-j`; default table on
TTY and TSV on redirection follows #1698. Resolve conflicts before backend I/O.
A single key/value result names `operation`, `target`, parent `vios`, relevant
`vg`/`lpar`, and `outcome`. Successful operation return means `request_succeeded` with `verification` saying
`final state not verified`, including empty responses and no-ops. Clients can
accept HTTP 202 while discarding status; this is not completion evidence. Do not
claim creation/deletion from that return alone. Exceptions retain stderr/exit1.
Existing confirmation prompts go to stderr (including their destination-aware
field encoding), preserving consent flags/timing while keeping JSON/TSV clean.
An output-owned confirm_on_stderr wrapper uses stdlib redirect_stdout around
existing typer.confirm: Typer sends the final prompt character through input()
even with err=True. Extend the terminal AST guard for this named stderr sink.
A returned JobID resource uses existing job_outcome classification: preserve job
ID/status/error, report submitted for pending/unknown and failed for errors, with
nonzero failure exit; no new polling or backend requests.

`--details` explicitly emits the original operation result as one JSON document;
it conflicts with table/TSV. Its help and migration note warn that the result may
contain hardware identifiers. Ordinary JSON is the concise projection, except
attach-disk and upload-iso retain their established explicit JSON envelopes and
fields; `--format json` aliases those same envelopes. They remain documented
exceptions, not silently replaced contracts. Details on operations returning
only a location/string may return that original value rather than a document.

Mapping, mount, unmount and detach summaries carry structured change_location
and its human summary; unmount/detach state backing media/storage is retained.
Attach keeps its nonzero incomplete-workflow exit and JSON envelope; human/TSV
results show target, dry_run/request_succeeded/incomplete, each step status, error or
pending-job evidence, warnings and change location. Omit successful step raw
resources. Dry-run is validation, never completion. Job steps, when present,
retain job identifiers/status; the protected JSON workflow_completed field remains
unchanged. Human success describes workflow requests, not verified final state.
Upload preserves JSON and reports observed status, target and checksum/size;
only its established verified `uploaded` status is completion evidence.

## Validation and constraints

Python 3.11–3.14 on declared amd64; no dependency or backend execution changes.
Test the actual Typer parser and output boundaries with synthetic operations;
cover each mutation's default, JSON and explicit details, option conflicts before
I/O, partial/errors/dry-run, truthful repository no-op, preserved envelopes,
change location, terminal controls and real redirected stdout. Retain storage
safety tests for mapped disks, mounted media, ownership, containment and If-Match.
Run focused app/storage tests plus lint/type; final `just verify`, pinned prek
and CI. No live HMC evidence is claimed. Update docs/cli.md and affected examples.

## Failure model

- Actors and deployments: local operators on terminals and agents reading pipes;
  offline tests and installed CLI, not an interactive web UI.
- Invariants/assets: truthful destructive-operation outcomes; one JSON result;
  existing explicit envelopes; safety evidence; terminal field integrity.
- Accepted failure classes: backend exceptions keep the existing runtime error
  contract (stderr, nonzero), not a new universal JSON error schema; no new
  read-after-write proof; results explicitly state when final state is unverified.
- Covered elsewhere: selection #1697, consent #1703, inventory escaping #1698,
  terminal policy ADR0229, composed journey #1704, live tests separately authorized.

## Threat model

- Boundary: HMC-derived result fields and operator names reach terminal or pipe;
  no new network boundary, credential source, authorization grant or command path.
- Actors: operator controls selectors; remote HMC controls returned metadata.
- Controls: existing print_inventory/VerbatimTable and terminal_field own encoding;
  structured JSON serialization preserves data. Explicit details opt into raw
  resource metadata. Backend ownership/containment/If-Match checks remain intact.
- Out of scope: malicious HMC execution and credential policy, held by existing
  client/operation boundaries; no live mutation is part of this implementation.
