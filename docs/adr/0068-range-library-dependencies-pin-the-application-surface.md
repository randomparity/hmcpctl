# ADR 0068: Range the library dependencies, pin the application surface

## Status

Accepted (2026-08-22). Amended 2026-10-13 (#1539) — see *Amendment* below.

## Context

ADR 0001 pinned every direct dependency to one registry release with `==`,
justified by a premise about what this repository is: "Consumers install the
pinned direct versions because this repository is an application, not a
compatibility-oriented library dependency." ADR 0029 made hmc-mcp exactly the
thing that premise excluded. It declares `hmc_mcp.api` as a supported reusable
Python API with an exhaustive compatibility manifest and a strict pre-1.0
release policy, and moves all presentation behind an `app` optional extra
specifically so a bare installation serves library consumers.

Under exact pins the practical effect on a library consumer is hard: any
environment already containing a different patch release of `asyncssh`,
`defusedxml`, `httpx`, `pydantic`, `pydantic-settings`, or `typing-extensions`
cannot install
hmc-mcp at all. `pydantic` is the sharpest edge — nearly every modern Python
application already depends on it, and `pydantic==2.13.4` refuses `2.13.5`.

There is no conflict today. A resolution probe on 2026-08-22 (CPython 3.11,
darwin/arm64) resolved `fastapi + sqlalchemy + asyncpg + alembic` together with
all six exact pins successfully. This decision is insurance against the first
divergence, taken while its cost is one record instead of a downstream consumer
blocked on an upstream release schedule.

## Decision

The direct-dependency declarations split into three regimes.

**Library runtime dependencies declare compatible ranges.**
`[project.dependencies]` carries one floor at the current locked release and
one upper bound chosen against each project's stated compatibility position,
not a blanket rule:

```toml
dependencies = [
    "asyncssh>=2.24.0,<3",
    "defusedxml>=0.7.1,<1",
    "httpx>=0.28.1,<1",
    "pydantic>=2.13.4,<3",
    "pydantic-settings>=2.15.0,<3",
    "truststore>=0.10.4,<1",
    "typing-extensions>=4.16.0,<5",
]
```

- `asyncssh <3`: incompatible API changes in AsyncSSH's history concentrate at
  major boundaries (the 1.x → 2.x transition); 2.x minors have been additive
  for the connection, channel, and SFTP surface hmc-mcp drives.
- `defusedxml <1`: defusedxml publishes no semver contract, but the
  `ElementTree` parse surface hmc-mcp consumes has been stable across the
  0.7 → 0.8 line; the 1.0 boundary is the honest cap for an undeclared-policy
  package.
- `httpx <1`: this is the range hmc-mcp's own application-surface dependencies
  already declare — `mcp` 1.x requires `httpx>=0.27.1,<1.0.0` and
  `fastmcp-slim` 3.x requires `httpx>=0.28.1,<1` — so the library install path
  makes exactly the promise about httpx that its dependency family already
  makes.
- `pydantic <3`: pydantic's published version policy promises no intentional
  breaking changes in 2.x minor releases and retains deprecations until V3.
- `pydantic-settings <3`: it tracks pydantic's major line; behaviour changes
  within 2.x arrive through documented release notes on the stable
  `BaseSettings` surface hmc-mcp uses.
- `truststore <1`: truststore publishes no formal semver contract, but its
  `SSLContext` surface (a subclass of `ssl.SSLContext`) has been stable across
  0.x and the project's own changelog reserves major boundaries for breaking
  changes; the `<1` cap is the honest pre-1.0 boundary (#1602, ADR 0222).
- `typing-extensions <5`: the project follows semantic versioning and reserves
  its next major for incompatible changes; hmc-mcp imports `TypedDict` directly
  for supported operation contracts.

**Everything ADR 0001 described correctly stays exact.** The `app` optional
extra, the `dev` dependency group, and the hatchling build requirement keep
`==`: those serve the CLI/MCP application and repository development, where
ADR 0001's reproducibility argument holds as written.

**The lock stays committed and exact.** `uv.lock` continues to record one
complete resolution; relaxing the declared ranges does not relax the lock, and
the regenerated lock resolves every package to the version it resolved before
this change. Dependabot keeps updating declarations and lock together in
reviewable pull requests.

**The floors are tested claims.** CI gains a job that installs the validated
wheel with every runtime dependency forced to its declared floor and exercises
the installed public API, so each range's low end is exercised rather than
assumed. `tests/test_supply_chain.py` enforces the split itself: library
runtime dependencies must be ranges, the application surface must stay `==`,
and the locked versions must satisfy every declared range.

## Consequences

- Consumers can co-install hmc-mcp with environments that carry different patch
  releases of the six shared runtime dependencies.
- Risk shifts from "cannot install" to "a resolver may select a combination
  nobody exercised"; the floor job bounds the bottom of that space, and the
  weekly Dependabot cadence plus the retained-wheel smoke jobs cover moving up.
- Caps turn each upper-bound crossing into a reviewed declaration change
  instead of a silent major-version entry into consumer resolutions.
- The split can erode one specifier at a time unless guarded; the supply-chain
  tests make both halves of the split load-bearing.
- ADR 0001 remains accepted for the application extra, the development group,
  and the build backend; only its runtime-manifest decision is superseded.

## Considered & rejected

**Relax every declaration.** The application surface loses nothing from exact
pins and keeps reproducibility; widening the dev group or build backend would
trade away ADR 0001's still-valid guarantee for no consumer benefit.

**Keep `==` until a conflict actually blocks someone.** The probe shows today
is clean precisely because no consumer has shipped a colliding patch yet. Once
one does, the remedy lands on another project's release calendar.

**Declare lower bounds only.** Without caps, the next major of pydantic or
httpx enters consumer resolutions silently; a cap forces this repository to
review and re-declare compatibility at each boundary.

**Tighten pre-1.0 caps to the next minor (for example `httpx<0.29`).** Honest
about 0.x churn, but it contradicts the declarations of mcp and fastmcp-slim —
which would then be uninstallable beside hmc-mcp's stricter range — and
re-creates the exact install blocker this record removes.

**Express the split as extras instead of ranges.** Moving the runtime
dependencies behind an extra fragments every import-bearing consumer's install
command and leaves the bare install without its transport and validation
dependencies.

## Amendment (2026-10-13, #1539)

Two sets of application-surface pins must move together: `fastmcp-slim` is
pinned in both the `app` extra and the `dev` group, and each `mcp` release
requires the `mcp-types` release of the same version. Dependabot updates one
requirement at a time, so every grouped run recorded these as unresolvable and
left them in place. Dependabot now ignores `fastmcp-slim`, `mcp` and
`mcp-types`, and the coupled-bump procedure in `CONTRIBUTING.md` moves them by
hand, together with the lock. The pins stay exact; only who moves them changes.
An `ignore` also covers Dependabot security updates, which are not enabled for
this repository; the procedure names the advisory check that stands in for them.
