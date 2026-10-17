# Development

[Documentation index](index.md) · [Contributing](../CONTRIBUTING.md)

## Stack

- **Python ≥3.11**, managed with [uv](https://docs.astral.sh/uv/). The project supports every
  stable, non-EOL CPython release at or above that floor.
- **MCP server**: [FastMCP](https://gofastmcp.com/) (stdio or streamable HTTP)
- **CLI**: [Typer](https://typer.tiangolo.com/) + Rich tables
- **REST transport**: httpx (async), XML parsed with defusedxml
- **CLI passthrough**: [asyncssh](https://asyncssh.readthedocs.io) — tools that
  shell out to HMC CLI commands (`lssyscfg`, `lshwres`, `chsyscfg`, ...) over
  SSH

## Setup

From a source checkout, run `just setup` to install the locked development environment
and repository hooks. Use `uv run --no-sync` before CLI commands in this environment.
See [Contributing](../CONTRIBUTING.md) for the full change and verification workflow.

## Testing

### 1. Unit tests (no HMC needed)

The client and XML parser are tested against an HMC mocked with
[respx](https://lundberg.github.io/respx/) — no real hardware required. The
default command reports only the configured test and coverage result:

```bash
just test
# test: passed; configured coverage gate passed
```

Use `just test-verbose` for native pytest diagnostics and missing-lines coverage.

### 2. MCP protocol smoke test (no HMC needed)

Verifies the MCP handshake in process with a real FastMCP client and reports
the exposed tool count:

```bash
just smoke
# Connected. <N> tools exposed.
```

Use `just smoke-verbose` to list every exposed tool when diagnosing the registry.

### 3. Live check against a real HMC

With credentials configured (TOML profile, env vars, or flags), the cheapest end-to-end check
is `console info` — one Logon + one ManagementConsole GET:

```bash
hmcpctl console info
hmcpctl systems list      # should print a table of your Power servers
```

If `console info` prints the HMC version, auth, TLS and the session
lifecycle all work; everything else uses the same path.

## Layout

```
src/hmcpctl/
  *.py              # package-level modules and composition roots
  api.py             # supported connection/configuration facade (ADR 0118)
  config.py          # TOML profiles, environment values, and CLI configuration
  errors.py          # shared HMC error hierarchy
  resource_identity.py # managed-system, LPAR, and VIOS selector resolution
  audit/             # audit records and non-blocking diagnostic transport
  authorization/     # access policy plus dispatch-time scope enforcement
  client/            # HMCClient transport, response parsing, and domain mixins
  documents/         # domain XML request builders and common envelopes
  jobs/*.py          # job outcome normalization, polling, and request builders
  operations/        # presentation-neutral workflows and authorization policy
    affinity/, inventory/, lpar/, metrics/, storage/
    systems/, templates/, updates/, users/, vios/, virtualization/
                    # domain workflows; see each package for its resource scope
  server_tools/      # MCP adapters: inventory, lpar, metrics, storage, systems,
                     # templates, users, vios, and virtualization
  cli_commands/      # Typer adapters: config, jobs, lpar, metrics, storage, systems,
                     # vios, and virtualization
  snapshots/         # portable LPAR snapshots and affinity assessment
  ssh/*.py           # asyncssh transport and resource-specific HMC CLI commands
  tool_registry.py   # local MCP tool collection and ToolSecurity metadata
  _app.py            # FastMCP factory and shared execution helpers
  server.py          # MCP composition, startup validation, and serving bootstrap
  cli.py             # CLI registration aggregator
tests/           # pytest + respx, no real HMC needed
scripts/         # repository guardrails, generators, test runners, smoke checks,
                 # and live-test harnesses
```

## Notes on the HMC API

- Auth: `PUT /rest/api/web/Logon` with a LogonRequest XML body returns an
  `X-API-Session` token, sent as a header on every subsequent call;
  `DELETE /rest/api/web/Logon` logs off.
- Resources are Atom feeds of vendor media type
  `application/vnd.ibm.powervm.uom+xml; type=<ResourceType>`.
- `.../quick/<Property>` returns a single property cheaply;
  `.../search/(<Property>==<Value>)` filters server-side.
- State changes are asynchronous **jobs**: POST a JobRequest to
  `/rest/api/uom/<Type>/<uuid>/do/<Operation>`, then poll
  `/rest/api/uom/Job/<job-uuid>`.

## Releasing

A release is a version in `pyproject.toml` and a Git tag on the same commit
([ADR 0033](adr/0033-static-package-version.md)); the tag is annotated. Nothing is published
to a package index, so the tag is what a consumer pins; the [README](../README.md#install)
shows the form.

Versions are semver. The tag is `vX.Y.Z`, and the GitHub release is titled
`hmcpctl X.Y.Z (<short-sha>)`, where `<short-sha>` is the tag commit's
`git rev-parse --short` ID. The version
is static in `pyproject.toml`, as ADR 0033 sets out. The changelog heading stays
`## [X.Y.Z] - YYYY-MM-DD`, because a commit cannot name its own SHA.

Live verification is a release stage ([ADR 0212](adr/0212-release-scoped-live-verification.md)).
Between releases, installed CLI/MCP discovery and generated tool documentation retain the last
release's verification, without runtime age decay. Ordinary development changes do not require
live reruns or rerun issues. An operator may authorize an ad-hoc window; its catalog evidence
counts toward a candidate only while the closure matches and the gate's age ceiling holds.

A release runs in the order of the subsections below: scope it, name the candidate and freeze
`main`, verify the candidate live, read the evidence gate, review the docs and cut the
changelog, tag it, then reopen `main`. Each subsection says who decides what. The operator is
the maintainer who owns the release.

### Scope

A release is scoped by a release epic, an issue that names:

- the consumer contract the release serves, and
- the committed claimed-set file under `docs/capabilities/releases/vX.Y.Z.json`, whose
  `operations` rows name each `operation`, evidencing `arm` and `scenario`. The offline
  `just capability-inventory` guard validates these triples against registered scenarios.

The claimed set is what the release promises; nothing outside it is promised, whatever its
verification state. The historical `v0.1.0.json` contains 15 MCP claims from #871, the Tier A
kdive adopt contract and Tier B bare-CEC path. It does not claim CLI wrappers. For a new
release, commit and review its own versioned claims file before naming the candidate.
Changing claims after the window requires fresh validation and matching evidence for added
claims; never infer coverage from a similarly named scenario. Report CLI evidence or gaps
separately as [live testing](live-testing.md#cli-coverage) describes.

Before naming the candidate, run the
[coupled dependency bump](../CONTRIBUTING.md#coupled-dependency-bumps) for `fastmcp-slim`,
`mcp` and `mcp-types`, which Dependabot does not update.

### Freeze

The candidate is the `main` commit whose claimed operations' code the release ships and the
live window runs on. The operator names it on the release epic before the first arm runs, and
the freeze starts then; it ends when the tag is pushed.

During the freeze, nothing may merge that changes the import closure of a claimed operation:
the `.py` modules under `src/hmcpctl/` that
[ADR 0127](adr/0127-derived-live-verification-staleness.md) fingerprints for it. That test is
the whole rule. Changes that pass it include:

- documentation, including the docs review and changelog cut below;
- the release-prep commit of [tag and release](#tag-and-release) step 1, which changes
  `pyproject.toml`, `uv.lock`, the changelog and the README's pinned tag; and
- evidence commits that promote observations into the catalog, including the
  `src/hmcpctl/_operation_maturity.json` projection they regenerate, which is data, not a
  module.

A merge that moves a claimed closure invalidates its candidate evidence, even though the
packaged historical state stays frozen. Name the new candidate, obtain authorization bound
to that commit, and run the release-window command for its claims before reading the gate
again. Never treat a supposedly harmless delta as evidence. This is a release-freeze rule,
not an instruction to run hardware after ordinary development merges.

### Live window

The operator authorizes every hardware window explicitly, naming its arms, mutation boundary
and VIOS access when needed, including the independent read-only baseline. Record the public-safe
authorization on the release epic; keep actual system identifiers and the command's exact
candidate-bound authorization JSON private. A development or PR approval is not a hardware grant.

Follow [the release-window procedure](live-testing.md#2-run-the-authorized-window) from a clean,
non-default candidate worktree created at the named commit. Its dry run contacts no HMC and
prints the authorization template. One authorized invocation then captures independent
baselines, runs each selected arm as preflight → named arm → recovery, compares after-baselines,
generates evidence, promotes observations on a new local branch, and reads the claimed-set gate.
Recovery runs after an attempted arm, even if the arm fails; an unclean or unreadable recovery
stops the window. Failed phases, baseline drift or unreadable evidence block promotion; a
written explanation does not turn a failed comparison into a pass.

The independent baseline reads system identity, partitions/states, slot owners, profile
`io_slots`, SR-IOV resources and, when authorized, VIOS identity, mappings, volume groups and
volumes. It preserves structural identity, including UUIDs, and compares the same selectors
before and after the window. Keep other actors quiescent. See the
[baseline commands and failure exits](live-testing.md#the-inventory-reads) for manual inspection.

The command never pushes, tags or commits to the original candidate branch. Review its
promotion PR before merging: only the catalog, release snapshot, packaged projection and
generated tool references belong in that evidence commit. Raw results, authorization, logs
and baseline snapshots remain private. Publish only the generated, filtered evidence matrices
with their actual tested candidate; record the promotion and gate-read commits separately.

### Evidence gate

Read the gate on the clean commit carrying the reviewed promotion, then again at the intended
tag commit. Substitute the release's committed claims file for `vX.Y.Z.json`:

```sh
uv run --no-sync python scripts/check_capability_inventory.py \
  --require-current docs/capabilities/releases/vX.Y.Z.json
```

`--require-current` selects and reports the claimed operations and exits nonzero unless each
is `current`; it includes closure matching and the 90-day age ceiling at read time. No manual
row comparison is needed. It cannot be combined with the global `--fail-on-stale` flag.
An added claim, changed closure, failed/missing observation or expired evidence holds the tag;
resolve it in an operator-authorized release window and review the resulting promotion.

`just verification-report` remains a global candidate diagnostic. Record its five-state counts
(current, stale, failed, unevidenced and unrecorded) on the epic and in Known issues, but unclaimed
non-current operations do not block the claimed-set gate. The optional global diagnostic is:

```sh
uv run --no-sync python scripts/check_capability_inventory.py \
  --verification-report --fail-on-stale
```

Its nonzero stale result is informational for release claims, not a reason to run unclaimed
arms. CI no longer runs a per-commit verification-report job or a weekly stale-failure job.

The packaged snapshot and the live gate answer different questions. The snapshot preserves
historical release state without age decay; the gate tests candidate eligibility now. A long
freeze can therefore fail the age gate without changing packaged state. Record the actual
candidate, promotion commit and gate-read commit on the release epic.

### Docs and changelog

Before the tag, review the user-facing documentation against the candidate, and cut the
changelog, which is step 1 of [tag and release](#tag-and-release). Both land on `main`
during the freeze. For 0.1.0 that work is #1468.

Contributors record each user-facing change under `[Unreleased]` in `CHANGELOG.md`, in the
usual [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories, ADR 0118 facade
changes included. A release's changelog entry keeps those itemized categories, and it is also
the GitHub release body. At the cut it gains:

- a short statement of the release's scope and the consumer contract it serves;
- a **Verified** section naming the claimed operations, each verified `current` at the gate;
  and
- a **Known issues** list that names concrete problems, including the global
  verification counts from the [evidence gate](#evidence-gate). Every release's entry needs
  one; an entry without it is incomplete.

0.1.0 is the one exception: the first release, with nothing shipped before it, so its entry
carries no itemized changes. Its itemized pre-release history was dropped from the changelog,
after the guidance an operator needs that only the changelog stated moved into the user
documentation; Git history keeps it.

### Tag and release

The operator authorizes the tag after the gate passes, and authorizes the GitHub release
separately, after the tag. An agent or automated workflow never creates the tag or the
release.

1. On a branch, set `[project] version` to the new `X.Y.Z` and run `uv lock`; `uv lock --check`
   must then pass, because `uv sync --locked` in `just setup` refuses a lock that disagrees.
   Cut the changelog: move the `[Unreleased]` entries under a dated `## [X.Y.Z] - YYYY-MM-DD`
   heading in `CHANGELOG.md`, keeping their categories, and add the scope statement,
   **Verified** and **Known issues** sections described under
   [docs and changelog](#docs-and-changelog) (`tests/unit/test_changelog.py` requires the
   declared version to have a heading). Point the changelog's link references at
   `vX.Y.Z` (the `[unreleased]` compare base and an `[X.Y.Z]` tag link), and update the
   README's pinned tag. When `pyproject.toml` already declares `X.Y.Z` and the changelog
   already has its heading but the version was never tagged, as `0.1.0` was, skip the bump
   and replace both that existing section and the `[Unreleased]` entries, itemized
   pre-release history included, with a one-off minimal entry dated for the release (scope,
   Verified and Known issues only), leaving an empty `[Unreleased]` heading. Git keeps the
   history. This applies to 0.1.0 alone; later releases keep their itemized categories.
   Merge that change to `main`.
2. Choose the tag commit: the `main` commit that change merged as, or a later `main` commit
   the freeze admits. Its CI must be green and `just verify` must pass on a clean checkout.
   Also run the release-mode inventory check; ordinary verification deliberately permits
   development closures to differ from the last release snapshot:

   ```sh
   uv run --no-sync python scripts/check_capability_inventory.py --release
   uv run --no-sync python tests/validate_release_artifacts.py dist . --release
   ```

   `just verify` builds the fresh wheel and sdist consumed by the second command. Release
   validation recomputes snapshot verification from the catalog and candidate closures;
   it does not apply the age ceiling. A mismatch holds the tag for reviewed release evidence.
3. On that exact checkout, require both the claimed-set gate and release-mode validation:

   ```sh
   uv run --no-sync python scripts/check_capability_inventory.py \
     --require-current docs/capabilities/releases/vX.Y.Z.json --release
   ```

   A failed claim holds the tag even when the snapshot matches. Follow the
   [freeze](#freeze) and [evidence gate](#evidence-gate) rules, then repeat from step 2
   at the new evidence-bearing commit. Record the passing tag-commit read on the epic.
   Re-read `just verification-report`; if its global counts differ from Known issues,
   correct that entry, merge it, and repeat from step 2 on the resulting commit.
4. Tag that exact commit and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "hmcpctl X.Y.Z" <tag-commit-sha>
   git push origin vX.Y.Z
   ```

   Check that `git rev-parse vX.Y.Z^{commit}` prints the tag commit and that
   `git show vX.Y.Z:pyproject.toml` declares `X.Y.Z`.
5. Create the GitHub release from the pushed tag. Its body is the version's changelog entry:
   copy the entry, without its `## [X.Y.Z]` heading, into the notes file. The title carries
   the tag commit's short ID:

   ```bash
   short_sha=$(git rev-parse --short "vX.Y.Z^{commit}")
   gh release create vX.Y.Z --verify-tag --title "hmcpctl X.Y.Z ($short_sha)" \
     --notes-file <notes.md>
   ```

### After the release

Pushing the tag ends the freeze, and `main` reopens to every change. Close the release epic
and open the next one, or adopt an existing epic for it, naming its claimed operations and
consumer contract as in [scope](#scope). The 0.2.0 epic is #1215.
