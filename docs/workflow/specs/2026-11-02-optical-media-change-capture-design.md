# Optical media change: live capture arm (#1284)

## Problem

hmcpctl cannot change the medium in an existing virtual optical device, and the write shape
has never been captured. `tests/fixtures/live/rest-vios-scsi-mapping.json` and
`rest-ms-vios-feed-media.json` are GET captures only. In the #1280 run the operator swapped media
only through `viosvrcmd` (`unloadopt -vtd`, `loadopt -disk … -vtd`), and after an immediate
power-off both needed `-release`. `_VTD_NAME_IN_USE` in `src/hmcpctl/client/client_storage.py`
was seen once and no capture pins it. Since #1288 `hmc_mount_optical_media` refuses a mapped
VTD before any POST, so Q4's 500 can only come from a raw read-modify-write outside the tool.
The load/unload operation (#1285) depends on these answers.

## Scope

Decision: a committed, opt-in arm, not an uncommitted operator probe. The window's mutations
then run as reviewed code, preflight and recovery cover them, and the arm can be re-run on the
V11R2 HMC once that HMC is authorized for mutation. A new runner group `vmedia-media-change`
dispatches `[16, 71, 22]`: ST16 resolves the VIOS, the test partition and the repository (it
creates a repository only when the VIOS has none, as the vmedia arm already does), ST71 is the
new capture, and ST22's existing teardown removes any run medium ST71 left. The group is not in
`all` or `vmedia`, 71 joins the runner's bare-run exclusion set, and ST71 SKIPs unless
`state.group` is its own group (as `vios_backup.py` does), so no other invocation reaches it.
Its wrapper is `scripts/live_vmedia_media_change.py`. The arm was numbered 70 when its 2026-11-02 windows ran; it became 71
when #1226's `lpar-reconfigure` arm landed on `main` with 70 first.

ST71 (`scripts/live_test/vmedia_media_change.py`) runs only when the test partition is
unprotected and `Not Activated`, the repository has 2 GiB free, and ST16 resolved the VIOS
UUID, its partition id and the partition UUID. Otherwise it records SKIP rows and touches
nothing. Every HMC call inside ST71 runs under `live_test.capture.capture()` (default, redacting
mode), writing `hmc-captures/vmedia-media-change-<8 hex>.capture.jsonl`, which is git-ignored.
Rows use `state.record`/`state.skip`, never `record_verified`: a capture promotes no maturity.
Every row for a tool call is labelled `<tool name> (<step>)`, because recovery matches a row
by its label's first word.
Steps, in order:

1. Baselines: VIOS storage mappings and both vSCSI adapter listings (existing `storage`
   helpers). Create blank media A and B (`hmcpctl_live_<8 hex>`, 1 GiB, names recorded in the
   new artifact `vmedia_change_names` before each create). Mount A to the test partition with
   the tool, read the VTD name V from its optical mapping, and re-read both adapter listings
   (the post-mount set; the mount may add an adapter pair). V must match
   `[A-Za-z0-9_]{1,15}` before it enters any command; else stop.
2. Read-only `viosvrcmd -m <sys> --id <vios id> -c "help loadopt"`, `"help unloadopt"`,
   `"lsvopt -vtd V"`.
3. Q4: `_rmw_vios_mapping` appending `build_virtual_optical_mapping_document(B, link, V)`,
   bypassing the #1283 refusal. Record the status and verbatim body; then re-read mappings and
   adapters. Any adapter not in step 1's post-mount set is the orphan answer and a
   `MANUAL RECOVERY REQUIRED` row (the arm never removes an adapter). A mapping count change
   (the POST was accepted) stops ST71.
4. Q1: `_rmw_vios_mapping` replacing V's `Storage/VirtualOpticalMedia` with one naming only B.
   Record the response, then the tool's optical mappings and `lsvopt -vtd V`. Re-derive V from
   the lp3 mapping naming A or B (a changed name is part of the Q1 answer and is validated
   again); when none remains, Q2 and Q3 SKIP with that reason.
5. Q2: `unloadopt -vtd V`, `lsvopt -vtd V`, `loadopt -disk A -vtd V`, `lsvopt -vtd V`.
6. Q3: power the test partition on (tool, `wait=True`, 120 s) and off (`immediate=True`), then
   `unloadopt -vtd V`; only if that fails, `unloadopt -release -vtd V`; then
   `loadopt -disk B -vtd V`; only if that fails and `help loadopt` listed `-release`,
   `loadopt -release -disk B -vtd V`; then `lsvopt -vtd V`.
7. Cleanup, in a `finally`: power off if ST71 powered on and the state is not `Not Activated`;
   when V's drive is empty, `loadopt -disk A -vtd V` (once more with `-release` if that
   fails) so the tool can address the mapping; unmount whichever run medium the mapping names
   (tool); delete A and B (tool); compare mappings and adapters with step 1's baselines. An
   empty drive, a run medium still mapped or
   listed, or any drift is a `MANUAL RECOVERY REQUIRED` row naming the command that clears it.

Recovery: `_VMEDIA_SUBTASKS` gains 71, `iso_names` reads `vmedia_change_names`, and
`powered_on` reads ST71's power-on rows, so the existing test-partition classes (mapping left,
unmapped server adapter, run media left, partition running) witness ST71. One new class reads
`hmc_list_storage_mappings` and reports a mapping toward the test partition with an identity
but no backing (an empty VTD, which the optical listing never shows), naming
`rmvdev -vtd <V>`, only when its device is one ST71 recorded in a `vtd` row before touching
it; an operator's own empty device is never reported. `run_media_names`
also reads `vmedia_change_names`, so ST22 removes what ST71 left.

The window runs on the lab-reachable host, per docs/live-testing.md: preflight
`--group vmedia-media-change`, the wrapper, then `scripts/live_test_recovery.py` and
`scripts/live_test_evidence.py` over `test-results-vmedia-media-change.json`, at the commit that
adds the arm; later commits change only fixtures, docs and the pin test, so that commit is an
ancestor of the PR head and the PR cites it. The raw capture and results stay on that host and
move to the operator's private evidence directory (`~/hmc-live-evidence/<date>-<profile>/`)
before export; the README records them as retained privately. The window's outcome is recorded
as a `WORK:TRAJECTORY` note on #1284.

After the window: tokenize the capture with `scripts/live_capture_export.py tokenize
--private '<lab-pattern>'`, the pattern covering the system, VIOS and test-partition names and
the HMC host prefix (docs/live-testing.md), and commit one fixture per decisive record (the Q1 and Q4 responses; the `help`, `loadopt`,
`unloadopt` and `-release` CLI records that ran) with README rows. Update `_VTD_NAME_IN_USE`'s
comment, and its value only if the capture contradicts it, with a test reading the Q4 fixture.
When a plain `unloadopt`/`loadopt` succeeds after the power-off, its `-release` form is not
run, and the README records "`-release` not needed at this level; with-release unexercised".
When Q1 or Q2 shows a working load/unload path, the unmount docstring in
`src/hmcpctl/operations/storage/resources.py` stops calling that survey outstanding and cites the
fixture and #1285. Record Q1–Q4's answers in the README and `docs/live-testing.md`; Q1 on V11R2 stays unanswered
(read-only authority). A CLIRunner job is not exercised: it is a REST transport for the same
VIOS command and hmcpctl's CLI transport is SSH; this is recorded as unanswered too.
Exclusions: #1285's operation, physical optical, repository creation and ISO upload as features.

### Failure model

1. Actors and deployments: one operator on the lab-reachable host, under an authorized
   window on the V10R3 test boundary; CI over fixtures and fakes.
2. Invariants and assets: the test VIOS's other mappings and adapters; the test partition's
   power state; the operator's media; fixtures carry no lab identifier.
3. Accepted failure classes: a Q4 orphan adapter needs a manual `chhwres -o r` (bounded:
   one adapter, named in a row and by recovery); an empty VTD left when cleanup's reload also
   fails needs a manual `rmvdev -vtd V` (named in a row and by recovery's empty-VTD class); one
   firmware level is captured.
4. Covered elsewhere: tokenization (`live_capture_export.py`); the `-release` behaviour of a
   product operation (#1285).

### Threat model

- Boundaries: HMC answers (VTD name, mapping list) become `viosvrcmd` arguments; the
  operator's config supplies the system name.
- Actor: the configured HMC, trusted for identity but not for shape; the local operator.
- Controls: V is validated against the pattern in step 1; media names are generated from
  `secrets.token_hex`; the system name is `shlex.quote`d as the vmedia arm already does; the
  capture redacts sessions and secrets, and raw files stay git-ignored.
- Out of scope: a hostile HMC; it already holds every credential this arm uses.

## Success

1. With a scripted HMC, ST71 issues the step 1–7 calls in order and leaves no run medium,
   mapping or power-on behind; each refusal path (protected, running, no room, bad V) touches
   nothing; a failed `unloadopt` triggers exactly one `-release` retry.
2. Recovery reports a run medium named in `vmedia_change_names`, and a running partition after
   the arm's own ST71 power-on row, and an empty VTD toward the test partition; ST22 removes a leftover `vmedia_change_names` medium; a
   bare runner selection and a bare `71` touch nothing.
3. The wrapper dispatches exactly `--group vmedia-media-change`; preflight names the arm.
4. The committed fixtures answer Q1–Q4 (or record why one is unanswered), carry no lab
   identifier, and a test pins `_VTD_NAME_IN_USE` against the Q4 fixture.
5. Recovery after the window reports clean (or names each manual row), and the evidence
   matrix from `scripts/live_test_evidence.py` stamps the arm's commit.
6. `just verify` and `prek run --all-files` pass.

## Validation

- Success 1: focused-test, `tests/test_live_vmedia_media_change_arm.py` (fake HMC/VIOS).
- Success 2: focused-test, `tests/scripts/test_live_test_recovery.py`,
  `tests/test_live_vmedia_arm.py`.
- Success 3: focused-test, `tests/scripts/test_live_vmedia_media_change.py`,
  `tests/scripts/test_live_test_preflight.py`.
- Success 4: focused-test for the `_VTD_NAME_IN_USE` pin; task-test-not-applicable for the
  fixtures' identifier scan — the exporter's fail-closed scan with `--private`, then
  `rg -n -e '<lab-pattern>'` over the new fixture files, empty before commit.
- Success 5: task-test-not-applicable — a live run's output, not a repository behaviour.
- Docs and README prose: task-test-not-applicable; prose gated by `just doc-freshness`.
