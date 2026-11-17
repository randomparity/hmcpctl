# 0387 — Rebalance the four test groups at the CLI integration seam

## Status

Accepted (2026-11-17). Supersedes ADR 0309's application-directory-only group cut;
its remaining coverage/artifact contracts and ADR 0383's concurrent scheduling remain.

## Context

After #2001–#2004, fresh successful normal CI runs
[44039914716](https://github.com/randomparity/hmcpctl/actions/runs/44039914716) and
[44038622015](https://github.com/randomparity/hmcpctl/actions/runs/44038622015)
share full tree `5547af187cc82b8038c2e36befa60afd9813184e` with main `3f4a390c7`.
The broad direct-command and HTTPS CLI integration files contribute 119–170 summed
seconds on Python 3.11 across those samples. Application remains heavy, while moving
the entire CLI prefix can make domain-and-unit heavier than the old application group.

## Decision

Move `tests/app/test_cli_commands.py` and `tests/app/test_cli_e2e.py` into
`domain-and-unit`. Keep feature-specific CLI/presentation files in `application`.
Keep the literal cut in the existing runner; retain default catch-all ownership.
Keep `procedure-arms` and `tooling`, the four public names and check names, two-worker
cap, artifact topology and per-version combined coverage gate unchanged.
Prove the repository node-ID union and non-overlap through real collections on each
normal CI interpreter. Record hosted timing samples in the PR without promising an SLA.

## Consequences

The historical domain-and-unit name now includes two broad CLI integration suites.
Files stay in place with the same node IDs and fixtures. Renamed suites and future files
keep their directory/default owner until a measured change. Complementary file/directory
exclusions avoid pytest normalizing explicit descendants away beneath a parent root.
A smaller application workload can expose procedure or tooling as the pacer; shared
runner variability and setup/build/cache differences limit two-before/one-after evidence.

## Considered & rejected

- **Keep the current cut.** verified: complete JUnit profiles from both successful
  runs above show 3.11 application sums 863/581 seconds versus domain 325/315;
  their two selected files account for 170/119 seconds respectively.
- **Move every CLI-prefixed file.** verified: the same JUnit profiles give 3.11
  domain-plus-CLI sums 750/603 versus old application 863/581; this can overshoot.
- **Add a fifth group or change workers/floor.** judgment: outside the exact approved
  four-group scope; owned by maintainer/separate ADR or maintainer informed by #2000.
- **Dynamic duration bin packing or physical test relocation.** judgment: more policy
  and identity surface than a bounded two-file integration seam requires.
