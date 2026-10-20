# ADR 0114: Track Ruff's Default Rule Set, Pinning Back What 0.16 Dropped

## Status

Accepted

## Context

The repository has never carried a `[tool.ruff]` table. Ruff therefore lints at whatever
its own defaults are, and that has been an implicit policy rather than a recorded one:
under `ruff==0.15.22` the default set was 59 rules, all from pycodestyle `E` and pyflakes
`F`.

Ruff 0.16.0 changed the defaults. It enables 413 rules by default, and it *removes*
eighteen rules that were default before: `E401`, `E402`, `E701`, `E702`, `E703`, `E711`,
`E712`, `E713`, `E714`, `E721`, `E731`, `E741`, `E742`, `E743`, `F403`, `F405`, `F406`,
and `F722`. So the bump is not a set of new diagnostics over a stable policy; it replaces
the policy. Measured on `3bf3f892`, `uvx ruff@0.16.4 check .` reports 712 findings where
`uvx ruff@0.15.22 check .` reports none, and `--show-settings` resolves 413 enabled rules
against 59 before.

Two facts make the removal half load-bearing rather than incidental. Ten
`# noqa: E402` directives already exist in `tests/` (`tests/unit/test_ownership.py`,
`tests/scripts/test_inventory.py`, `tests/test_live_runner.py`), placed against
deliberate mid-module imports. With `E402` no longer enabled, those ten stop suppressing
anything and `RUF100` — which *is* newly enabled — reports each one as an unused
directive. Taking the upgrade without a decision about the removed rules therefore does
not merely lose coverage quietly; it actively pushes the repository toward deleting ten
documented suppressions in order to make the gate green.

The question this record settles is what the repository's lint policy *is*, now that
"whatever Ruff enables by default" has stopped being a stable answer.

## Decision

Continue to track Ruff's default rule set — still no `select`, so a future default gains
a rule here — and add a `[tool.ruff.lint]` table carrying exactly three entries:

1. `extend-select` listing the eighteen rules 0.16 dropped, restoring the coverage the
   repository enforced under 0.15.22 and keeping the ten existing `# noqa: E402`
   directives meaningful.
2. `flake8-bugbear.extend-immutable-calls` listing `typer.Option`, `typer.Argument`,
   `hmc_mcp.documents.LparResources`, and
   `hmc_mcp.operations.lpar.assignments.LparPcieAssignments`. `B008` exists to catch a
   *mutable* default shared across calls. The first two are Typer's declarative parameter
   constructors, which the framework requires in that position; the last two are
   `@dataclass(frozen=True)`. This is Ruff's own setting for the rule, not a suppression
   of it — `B008` stays enabled and still fires on a genuinely mutable default.
3. `per-file-ignores` silencing `PLE2502` in `tests/unit/test_audit.py` and
   `tests/unit/test_ownership.py`. Those two modules assert that the audit stream cannot
   be forged with bidirectional and control characters, so their fixtures must literally
   contain U+202E and U+2028. The rule's premise — that such characters in source are
   unintended — is false for exactly these files.

Every other finding is fixed in code, except where fixing it would change a contract this
record does not own. Those sites keep the behaviour and carry a **coded, per-site
`# noqa: <CODE> - <reason>`**, which is the fourth and narrowest mechanism: it names the
rule, states why the rule's premise is false there, and leaves the rule enforced
everywhere else. The families with such sites, with the count each contributes:

| Rule | Sites | Why the rule's premise is false there |
|---|---|---|
| `BLE001` | 20 | Reviewed per site. Each records why the broad catch is the contract — a readback reconciled by the caller, a diagnostic that must not fail its call, an audit sink whose totality is the point. |
| `TRY004` | 12 | **All twelve retained, not seven.** Seven are contract-bearing `src/` sites: a Pydantic validator that needs `ValueError` to become a `ValidationError`, three functions whose `ValueError` is frozen by ADR 0029's exported manifest, the caller-token guard an ADR 0011 best-effort boundary depends on, and one invariant check on a stdlib return value. The other five raise the type their own tests assert — four `ValueError` sites in `scripts/check_python_support.py` that `tests/scripts/test_check_python_support.py` parametrizes `pytest.raises(ValueError)` over, and one `AssertionError` in `tests/test_live_runner.py`. |
| `DTZ011` | 7 | The ADR 0011 ownership stamp records the operator's local calendar date, and the tests that assert it must stay in lockstep. |
| `PLC0414` | 4 | PEP 484 explicit re-exports in `hmc_mcp.cli`. |
| `S110` | 3 | Reviewed per site, alongside the `BLE001` directive each sits with. |
| `RUF022` | 1 | `hmc_mcp.api.__all__` is grouped by subsystem to mirror ADR 0029's inventory block, and `tests/unit/test_public_api.py` asserts that exact order; sorting it breaks the manifest. |
| `DTZ001` | 1 | A parametrized case in `tests/lpar/test_provision_tool.py` whose naive `datetime` **is** the invalid evidence under test — `operations/affinity.py` rejects a `captured_at` with no tzinfo, so adding one would silently delete the case. |

Counts are directive counts over tracked `.py` files and are checkable with
`git grep -c '# noqa: <CODE>'`. `RUF100` is enabled and the gate is green, so none of
these directives is dead.

No rule is disabled repo-wide, no directory is excluded, and no bare `# noqa` is added.

## Consequences

The lint policy is now explicit where it diverges from Ruff's defaults and implicit
everywhere else, which is the smallest configuration that survives the bump. A future
Ruff release that changes defaults again will surface as findings rather than as silent
coverage loss for the eighteen pinned rules, but the same silent loss remains possible
for any rule Ruff drops later — this record fixes the instance, not the class, and the
`extend-select` list is the place a future migration re-checks.

The bulk of the change is roughly 650 code fixes across `scripts/`, `src/`, and `tests/`,
which is a large diff carrying real behavioural risk in the minority of families whose
fixes are not mechanical. That cost is paid once here.

Three `# noqa` directives naming rules that are not enabled and are not being enabled —
two `S603`, one `PLC0415` — lose their directive form and keep their rationale as plain
comments. Enabling those rules to preserve the directives was rejected below.

`hmc_mcp.documents.LparResources` and `hmc_mcp.operations.lpar.assignments.LparPcieAssignments`
are named in configuration by their fully qualified paths, so moving or renaming either
type silently re-arms `B008` against ten call sites.

**The narrowness this record promises is enforced by review, not by a gate, and that is an
accepted consequence.** `tests/test_ci_pipeline.py` already asserts a close analogue for
the type checker — `"rules" not in project["tool"]["ty"]` — and deliberately stops there;
nothing makes the equivalent assertion about `[tool.ruff.lint]`. So a later change can add
a blanket `ignore`, an `exclude`, or a bare `# noqa` and every gate stays green. Adding a
config-shape gate was considered out of scope for the migration that introduces the
policy, and **#604 owns it** — it extends those `pyproject` assertions to pin the
`[tool.ruff.lint]` table's shape. Until it lands, review is the only control.

The two per-file-ignored modules lose Ruff's only detector for invisible bidirectional and
control characters, so `tests/unit/test_audit.py` and `tests/unit/test_ownership.py` — and
only those two — can subsequently acquire an *unintended* U+202E or U+2028 with nothing
objecting. The compensating control is that both files assert on the exact code points
they carry, so a change to those fixtures fails the suite rather than passing silently.

## Considered & rejected

- **Freeze the rule set at 0.15.22's defaults with `select = ["E4", "E7", "E9", "F"]`.**
  verified: `uv run --no-sync ruff check . --select E4,E7,E9,F` at ruff 0.16.4 on
  `56224333` reports no findings, so this makes the upgrade a no-op with a green gate.
  judgment: it converts an implicit policy into an explicit refusal of every rule Ruff
  now considers a default, which is the "broad suppression of meaningful diagnostics"
  #597 exists to avoid, and it buys the version number without the reason to want it.
- **Take 0.16's defaults as they are and let the eighteen removed rules lapse.**
  verified: `uv run --no-sync ruff check . --select E401,E402,E701,E702,E703,E711,E712,E713,E714,E721,E731,E741,E742,E743,F403,F405,F406,F722 --ignore-noqa`
  reports 10 `E402` findings at `56224333`, each already carrying a deliberate
  `# noqa: E402`; leaving the rules lapsed turns all ten directives into `RUF100`
  findings whose only fix is deleting them.
- **Enumerate the enabled rule set explicitly with `select`, instead of tracking defaults.**
  verified: `uv run --no-sync ruff check . --show-settings` resolves 413 enabled rules at
  0.16.4, or 431 with the eighteen restored, so the list is writable. This is the one
  alternative that removes the residual named above — a future default change would then
  surface as an opt-in rather than as silent removal. judgment: it moves the cost from
  once per defaults overhaul to once per bump, since a 431-entry list has to be diffed
  against Ruff's own on every upgrade to stay honest, and a list that drifts is worse than
  no list. Tracking defaults keeps the repository aligned with upstream's judgment by
  default and pays a large cost rarely; enumerating pays a small cost always and makes
  divergence the default outcome. The recurrence the Consequences section concedes is the
  accepted price of that.
- **Blanket-`ignore` the largest new families (`I001`, `PLC0414`, `SIM117`, `B008`) to
  bound the diff.** judgment: four `ignore` entries would erase 482 of the 712 findings
  without a claim that the repository's intent differs from any of the four rules, which
  is making the gate green by blinding it.
- **Enable `S603` and `PLC0415` so the three dead `# noqa` directives stay live.**
  verified: `uv run --no-sync ruff check . --select S603` reports 24 findings and
  `--select PLC0415` reports 238, at ruff 0.16.4 on `56224333`. Adopting either rule is a
  larger change than this migration, and neither was ever enabled here.
- **`chmod +x` the six scripts carrying a shebang, instead of removing the shebangs.**
  verified: `_copy_tracked_project` in `tests/test_ci_pipeline.py` copies with
  `shutil.copy2`, which preserves the mode bit, so the dirty-project fixture would still
  pass. judgment: the `justfile` invokes all ten scripts as
  `uv run --no-sync python scripts/<name>.py` and four of the ten already carry no
  shebang, so removing the six is what makes the files agree with how they are run.
- **Split the migration across several pull requests, one rule family at a time.**
  verified: `just lint` is a single `uv run --no-sync ruff check .` over the whole
  repository (`justfile:20`), so a partially adopted bump leaves the gate red on `main`
  between merges. judgment: the split decision belongs to the operator regardless, and
  the gate's shape removes the option that would have made it attractive.
- **Do nothing and stay on 0.15.22.** judgment: it defers a defaults migration whose cost
  grows with every subsequent release, and leaves the repository pinned to a superseded
  version for no stated benefit.

## Amendment (2026-10-19, #1722)

Ruff 0.17.0 dropped seven more rules from its defaults and added none: `DTZ001`,
`DTZ005`, `DTZ006`, `DTZ007`, `DTZ011`, `DTZ012`, and `DTZ901`. The same mechanism as
above applies — the eight `# noqa: DTZ011` and `# noqa: DTZ001` directives in the table
became `RUF100` findings — so all seven join `extend-select`, not only the two that have
directives. With them restored, `--show-settings` at 0.17.0 resolves the same 431
enabled rules as 0.16.10.
