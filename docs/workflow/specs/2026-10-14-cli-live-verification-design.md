# CLI observations in the bare-CEC arm

## Problem and authority

Issue #1477 and the operator's 2026-10-13 campaign decision select actual bounded
CLI-path steps with `record_verified` observations. WORK:SCOPE q1477-b215b195
is the authority; parent #1506 permits these additions to existing arms.
[ADR 0221](../../adr/0221-cli-live-observations.md) records the choice.

## Architecture and scope

Extend the existing bare-CEC arm at its established firmware observation points:
run `python -I -m hmcpctl lpars state <fixture-uuid>`,
`lpars refcodes <system> <fixture-name> --count 5 --json`, and
`jobs show <activation-job-id>`. These are three actual installed CLI paths,
using the same interpreter/environment as the arm and its resolved HMC config.
They perform reads only; existing MCP fixture mutation and recovery remain owners.
No caller migration or obsolete path is needed: this is an extension.

A small live_test module owns bounded subprocess execution and CLI assertions.
Use argument arrays without a shell; normalize HMC environment from RunState's
resolved config, suppress color and inherited profile selection. A missing config
fails closed. Capture at most 64 KiB of stdout; discard stderr so warnings cannot corrupt the
response parser or disclose diagnostics; deadline 120 seconds;
kill and reap the CLI child on timeout, excess output, or cancellation. Keep the
child in the arm process group so the outer window cancellation also reaches it.
Failures record safe diagnostics, not credentials or raw subprocess error text.

State must equal the MCP-observed firmware state; reference codes must be a
nonempty JSON list with this fixture's name and at most five rows; job JSON must
identify the submitted job, be found, and show successful completion. Nonzero exit,
invalid encoding/JSON, or a failed postcondition records failed CLI evidence.
Use `record_verified(surface="cli")` with scenario `st35-bare-cec-cli`.
Keep the CLI observation on its result row, including operation, scenario,
assertions and cleanup. Fixed identifier-free tool labels visibly name the CLI command
in the existing public matrix allowlist; test that renderer without extending its schema.
Do not put it in the MCP catalog sidecar, whose one
observation per operation cannot represent independent surfaces. Default MCP
recording stays unchanged. CLI rows inherit the results document's candidate,
clean-tree, schema and run provenance; no CLI closure currency is claimed.

The release window includes an explicit per-claimed-operation CLI coverage map
in dry-run and private final summary. Initialize gaps for the selected claims;
only CLI rows from a successful complete current-candidate bare-CEC run with
passed assertions can report exercised coverage. Failure, absent steps, and
operations outside these three remain explicit gaps. This is supplementary
surface evidence, not a change to the existing claimed-set promotion/gate.
The summary qualifies coverage by candidate and overall window result.

## Success

- The three CLI paths execute inside the admitted bare-CEC arm and use the existing
  fixture/job; their postconditions pass through record_verified.
- CLI failures fail the arm and existing finally cleanup still runs.
- CLI-only evidence cannot replace an MCP observation or promote mock runs.
- Each selected claimed operation has an explicit CLI coverage or gap in the window
  report. Historical v0.1.0 claims and their CLI exclusion remain unchanged.
- Recipe text separates historical MCP verification from future CLI step evidence.

## Failure model

- Actors and deployments: trusted local operator; Linux release runner; offline CI
  tests with transport/process boundaries doubled. Real hardware run requires later
  operator authorization and is not implementation proof.
- Invariants and assets: correct HMC/fixture identity, owned process cleanup, honest
  surface provenance, original partition/slot recovery and historical claims.
- Accepted failure classes: other CLI wrappers remain explicitly uncovered; a CLI
  timeout may fail an otherwise healthy arm; output above 64 KiB fails closed.
- Covered elsewhere: non-CLI assertions and recovery belong to existing arms;
  promotion and release orchestration to #1512/#1513; model #1507/#1508;
  closure narrowing #1495; development/live-testing runbooks #1514.

## Threat model

- Added boundaries: local CLI subprocess arguments/environment and captured output;
  CLI evidence rows consumed by release-window summary. No widened mutation boundary.
- Actors: trusted operator and HMC; remote response data is untrusted. Local checkout
  and interpreter are trusted under the existing clean candidate requirement.
- Controls: fixed command paths, argument arrays and isolated Python; HMC config
  pinned in child environment; byte/time bounds and child reaping; structured parsing
  and identity assertions; current candidate/complete-run checks before coverage.
- Out of scope: hostile local operator or modified interpreter, already outside the
  release runner trust model; automatic authorization or public raw evidence uploads.

## Validation

Offline tests run the installed CLI parser/runtime with REST/SSH boundaries doubled,
not operation logic; test malformed output, wrong identities, nonzero exits, timeout,
output flood and cancellation. Exercise arm insertion and record separation; verify
window coverage shows gaps unless matching candidate evidence passes. Controlled
faults must turn new assertions red. Run focused tests and repository static gates,
then exact-head just verify and pinned prek before delivery. No live-HMC claim follows.
